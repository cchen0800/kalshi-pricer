"""Coinbase Exchange ETH-USD spot + 1m candles.

Public endpoints, no auth needed. We use Coinbase as a *proxy* for CF Benchmarks
ERTI (Kalshi's actual settlement reference for KXETHD). See README for the basis caveat.

Spot fallback (used when Coinbase /ticker is 5xx) is a multi-source median
across Bitstamp, Kraken, and Gemini — three of the six exchanges in the ERTI
basket. Median across multiple constituents tracks ERTI more closely than any
single exchange and degrades gracefully if one source is slow or down.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

import httpx

log = logging.getLogger(__name__)

DEFAULT_HOST = "https://api.exchange.coinbase.com"
DEFAULT_PRODUCT = "ETH-USD"

_FALLBACK_SOURCES: tuple[tuple[str, str], ...] = (
    ("bitstamp", "https://www.bitstamp.net/api/v2/ticker/ethusd/"),
    ("kraken",   "https://api.kraken.com/0/public/Ticker?pair=ETHUSD"),
    ("gemini",   "https://api.gemini.com/v1/pubticker/ethusd"),
)
_FALLBACK_TIMEOUT_S = 3.0

# Coinbase has been observed serving stale cached ticker payloads during
# incidents — HTTP 200 with the same `time`/`price` for hours, sometimes with
# ask < bid. Treat ticker server-time older than this as stale and route to
# the fallback. Coinbase normally serves ticker within 1–2s of real time.
_STALENESS_THRESHOLD_S = 30.0
# /candles can stick at the same hour-old bars during the same incidents.
# 1m bars are normally <120s old at the head; reject if the newest bar is
# older than this and rebuild from the spot ring buffer.
_CANDLES_STALENESS_THRESHOLD_S = 180.0


@dataclass
class Spot:
    price: float       # last trade price
    bid: float
    ask: float
    epoch_ms: int      # client receipt time, not exchange time


@dataclass
class Candle:
    epoch_s: int       # bucket start (unix seconds)
    open: float
    high: float
    low: float
    close: float
    volume: float


class CoinbaseFeed:
    def __init__(
        self,
        host: str = DEFAULT_HOST,
        product: str = DEFAULT_PRODUCT,
        timeout: float = 5.0,
    ) -> None:
        self.host = host.rstrip("/")
        self.product = product
        self._http = httpx.Client(timeout=timeout, base_url=self.host)
        self._http_fallback = httpx.Client(timeout=_FALLBACK_TIMEOUT_S)
        # Stale-cache for /candles. During Coinbase Exchange incidents the
        # candles endpoint can 503 for hours. Cache last-good response so the
        # engine can keep polling on a slowly-aging vol estimate.
        self._candles_cache: list[Candle] = []
        # Ring buffer of recent (epoch_s, price) spot samples. Populated on
        # every successful get_spot() and used to rebuild candles when
        # Coinbase /candles serves stale 200s (the same incident class as the
        # /ticker staleness — 200 OK with last bar hours old). Holds 2 hours
        # which is more than enough for the engine's 60-bar vol window.
        self._spot_samples: list[tuple[int, float]] = []

    def __enter__(self) -> "CoinbaseFeed":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()
        self._http_fallback.close()

    def _get(self, path: str, params: dict | None = None) -> object:
        for attempt in range(4):
            try:
                resp = self._http.get(path, params=params)
            except httpx.RequestError:
                if attempt == 3:
                    raise
                time.sleep(0.3 * (2**attempt))
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt == 3:
                    resp.raise_for_status()
                time.sleep(0.3 * (2**attempt))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("retry budget exhausted")

    def get_spot(self) -> Spot:
        try:
            d = self._get(f"/products/{self.product}/ticker")
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 502, 503, 504):
                return self._get_spot_fallback()
            raise
        bid = float(d["bid"])
        ask = float(d["ask"])
        age_s = _ticker_age_seconds(d.get("time"))
        if (age_s is not None and age_s > _STALENESS_THRESHOLD_S) or ask < bid:
            log.warning(
                "coinbase ticker rejected (age=%s, bid=%.2f, ask=%.2f); using fallback",
                f"{age_s:.0f}s" if age_s is not None else "?",
                bid, ask,
            )
            return self._get_spot_fallback()
        spot = Spot(
            price=float(d["price"]),
            bid=bid,
            ask=ask,
            epoch_ms=int(time.time() * 1000),
        )
        self._record_spot_sample(spot)
        return spot

    def _record_spot_sample(self, spot: Spot) -> None:
        ts_s = spot.epoch_ms // 1000
        self._spot_samples.append((ts_s, spot.price))
        cutoff = ts_s - 7200
        if self._spot_samples[0][0] < cutoff:
            self._spot_samples = [s for s in self._spot_samples if s[0] >= cutoff]

    def _get_spot_fallback(self) -> Spot:
        # Multi-source median across ERTI basket members. See module docstring.
        parsers: dict[str, Callable[[dict], tuple[float, float, float]]] = {
            "bitstamp": _parse_bitstamp,
            "kraken":   _parse_kraken,
            "gemini":   _parse_gemini,
        }
        quotes: list[tuple[str, float, float, float]] = []
        for name, url in _FALLBACK_SOURCES:
            try:
                resp = self._http_fallback.get(url)
                resp.raise_for_status()
                last, bid, ask = parsers[name](resp.json())
                quotes.append((name, last, bid, ask))
            except Exception as e:
                log.warning("fallback source %s failed: %s", name, e)
        if not quotes:
            raise RuntimeError("all spot fallback sources failed")
        last = _median([q[1] for q in quotes])
        bid = _median([q[2] for q in quotes])
        ask = _median([q[3] for q in quotes])
        log.info("spot fallback: median last=$%.2f bid=$%.2f ask=$%.2f from %s",
                 last, bid, ask, "+".join(q[0] for q in quotes))
        spot = Spot(price=last, bid=bid, ask=ask, epoch_ms=int(time.time() * 1000))
        self._record_spot_sample(spot)
        return spot

    def get_1m_candles(self, minutes: int = 60) -> list[Candle]:
        # Coinbase returns up to 300 candles, newest first: [time, low, high, open, close, volume].
        try:
            d = self._get(
                f"/products/{self.product}/candles",
                params={"granularity": 60},
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 502, 503, 504):
                rebuilt = self._rebuild_candles_from_samples()
                if rebuilt:
                    return rebuilt[-minutes:] if len(rebuilt) > minutes else rebuilt
                if self._candles_cache:
                    return self._candles_cache[-minutes:] if len(self._candles_cache) > minutes else self._candles_cache
            raise
        candles = [
            Candle(
                epoch_s=int(row[0]),
                low=float(row[1]),
                high=float(row[2]),
                open=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in d
        ]
        candles.sort(key=lambda c: c.epoch_s)  # ascending

        # Stale-candles detection: same incident class as stale /ticker. When
        # Coinbase serves 200 OK with hour-old bars, σ collapses to ~0 and the
        # engine's σ-clamp pins it to ref. Detect and rebuild from the spot
        # ring buffer so σ tracks live volatility.
        if candles:
            head_age = time.time() - candles[-1].epoch_s
            if head_age > _CANDLES_STALENESS_THRESHOLD_S:
                rebuilt = self._rebuild_candles_from_samples()
                if rebuilt:
                    log.warning(
                        "coinbase candles rejected (head age=%.0fs); rebuilt %d bars from spot samples",
                        head_age, len(rebuilt),
                    )
                    self._candles_cache = rebuilt
                    return rebuilt[-minutes:] if len(rebuilt) > minutes else rebuilt
                log.warning(
                    "coinbase candles stale (head age=%.0fs) but only %d spot samples — using stale",
                    head_age, len(self._spot_samples),
                )
            self._candles_cache = candles
        return candles[-minutes:] if len(candles) > minutes else candles

    def _rebuild_candles_from_samples(self) -> list[Candle]:
        # In-memory variant of prime_candles_from_spots() — uses the live
        # spot ring buffer rather than DB-replayed samples.
        if not self._spot_samples:
            return []
        buckets: dict[int, list[float]] = {}
        for ts, p in self._spot_samples:
            buckets.setdefault(int(ts) // 60 * 60, []).append(float(p))
        bars: list[Candle] = []
        for bucket_ts in sorted(buckets):
            prices = buckets[bucket_ts]
            bars.append(
                Candle(
                    epoch_s=bucket_ts,
                    open=prices[0],
                    high=max(prices),
                    low=min(prices),
                    close=prices[-1],
                    volume=0.0,
                )
            )
        return bars

    def prime_candles_from_spots(self, samples: list[tuple[int, float]]) -> int:
        """Bootstrap the candles cache from a sequence of (epoch_seconds, price)
        spot samples — used at engine startup when /candles is in an outage.
        Buckets samples into 1-minute OHLC bars; bars with a single sample have
        open=high=low=close (yang_zhang_vol handles this degenerate case).
        Returns the number of bars built."""
        if not samples:
            return 0
        buckets: dict[int, list[tuple[int, float]]] = {}
        for ts, p in samples:
            buckets.setdefault(int(ts) // 60 * 60, []).append((int(ts), float(p)))
        bars: list[Candle] = []
        for bucket_ts in sorted(buckets):
            pts = sorted(buckets[bucket_ts])  # by ts
            prices = [p for _, p in pts]
            bars.append(
                Candle(
                    epoch_s=bucket_ts,
                    open=prices[0],
                    high=max(prices),
                    low=min(prices),
                    close=prices[-1],
                    volume=0.0,
                )
            )
        self._candles_cache = bars
        return len(bars)


def closes(candles: Sequence[Candle]) -> list[float]:
    return [c.close for c in candles]


def ohlc(candles: Sequence[Candle]) -> list[tuple[float, float, float, float]]:
    return [(c.open, c.high, c.low, c.close) for c in candles]


def _median(values: list[float]) -> float:
    vs = sorted(values)
    n = len(vs)
    if n == 1:
        return vs[0]
    if n % 2:
        return vs[n // 2]
    return (vs[n // 2 - 1] + vs[n // 2]) / 2


def _ticker_age_seconds(raw: object) -> float | None:
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds()


def _parse_bitstamp(d: dict) -> tuple[float, float, float]:
    return float(d["last"]), float(d["bid"]), float(d["ask"])


def _parse_kraken(d: dict) -> tuple[float, float, float]:
    if d.get("error"):
        raise RuntimeError(f"kraken error: {d['error']}")
    v = next(iter(d["result"].values()))
    return float(v["c"][0]), float(v["b"][0]), float(v["a"][0])


def _parse_gemini(d: dict) -> tuple[float, float, float]:
    return float(d["last"]), float(d["bid"]), float(d["ask"])
