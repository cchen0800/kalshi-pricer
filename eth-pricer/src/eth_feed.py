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
        return Spot(
            price=float(d["price"]),
            bid=float(d["bid"]),
            ask=float(d["ask"]),
            epoch_ms=int(time.time() * 1000),
        )

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
        return Spot(price=last, bid=bid, ask=ask, epoch_ms=int(time.time() * 1000))

    def get_1m_candles(self, minutes: int = 60) -> list[Candle]:
        # Coinbase returns up to 300 candles, newest first: [time, low, high, open, close, volume].
        try:
            d = self._get(
                f"/products/{self.product}/candles",
                params={"granularity": 60},
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 502, 503, 504) and self._candles_cache:
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
        if candles:
            self._candles_cache = candles
        return candles[-minutes:] if len(candles) > minutes else candles

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


def _parse_bitstamp(d: dict) -> tuple[float, float, float]:
    return float(d["last"]), float(d["bid"]), float(d["ask"])


def _parse_kraken(d: dict) -> tuple[float, float, float]:
    if d.get("error"):
        raise RuntimeError(f"kraken error: {d['error']}")
    v = next(iter(d["result"].values()))
    return float(v["c"][0]), float(v["b"][0]), float(v["a"][0])


def _parse_gemini(d: dict) -> tuple[float, float, float]:
    return float(d["last"]), float(d["bid"]), float(d["ask"])
