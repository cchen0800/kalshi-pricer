"""Coinbase Exchange BTC-USD spot + 1m candles.

Public endpoints, no auth needed. We use Coinbase as a *proxy* for CF Benchmarks
BRTI (Kalshi's actual settlement reference). See README for the basis caveat.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Sequence

import httpx

DEFAULT_HOST = "https://api.exchange.coinbase.com"
DEFAULT_PRODUCT = "BTC-USD"


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

    def __enter__(self) -> "CoinbaseFeed":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

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
        d = self._get(f"/products/{self.product}/ticker")
        return Spot(
            price=float(d["price"]),
            bid=float(d["bid"]),
            ask=float(d["ask"]),
            epoch_ms=int(time.time() * 1000),
        )

    def get_1m_candles(self, minutes: int = 60) -> list[Candle]:
        # Coinbase returns up to 300 candles, newest first: [time, low, high, open, close, volume].
        d = self._get(
            f"/products/{self.product}/candles",
            params={"granularity": 60},
        )
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
        return candles[-minutes:] if len(candles) > minutes else candles


def closes(candles: Sequence[Candle]) -> list[float]:
    return [c.close for c in candles]
