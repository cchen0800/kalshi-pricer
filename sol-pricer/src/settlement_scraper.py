"""Periodic scrape of ground-truth resolution for *every* settled KXSOLD market.

Why: portfolio_settlements only records markets we held positions in, so we
can't measure model calibration over the full strike grid (which strikes won
vs. lost regardless of whether we bet on them). This scraper fills that gap.

Once a market is finalized at Kalshi the response includes:
    result: 'yes' | 'no'
    settlement_value_dollars: '0.0000' or '1.0000'
    settlement_ts: ISO timestamp
    expiration_value: the SRTI close print (string $ value) — the cleanest
                      ground-truth final spot for backtesting the pricer.

Idempotent: market_settlements has UNIQUE on market_ticker, INSERT OR IGNORE
makes re-runs no-ops. Best-effort: any HTTP / parse error is logged and
swallowed — trading is the priority.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from src.engine import event_close_utc
from src.kalshi_client import KalshiClient

log = logging.getLogger("settlement_scraper")

# Look back this far for events whose markets may have just finalized. Must
# be greater than Kalshi's settlement_timer_seconds (~60s) plus enough buffer
# that a brief outage doesn't lose data.
LOOKBACK = timedelta(hours=4)


def _f(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_iso_ms(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, AttributeError):
        return None


def _recent_closed_events(kc: KalshiClient, series: str, lookback: timedelta) -> list[str]:
    """Event tickers that closed within the lookback window.

    Kalshi's /events response only includes status='open' by default. We pull
    a broader list (no status filter) and parse close times from the ticker
    itself — KXSOLD-YYMMMDDHH encodes the close hour deterministically."""
    evs = kc.list_events(series_ticker=series, limit=200).get("events", [])
    now = datetime.now(timezone.utc)
    out: list[str] = []
    for e in evs:
        et = e.get("event_ticker")
        if not et:
            continue
        ct = event_close_utc(et)
        if ct is None:
            continue
        if (now - lookback) < ct < now:
            out.append(et)
    return out


def scrape_settlements(
    conn: sqlite3.Connection,
    kc: KalshiClient,
    *,
    series: str = "KXSOLD",
    lookback: timedelta = LOOKBACK,
) -> int:
    """Fetch markets for recently-closed events and upsert finalized rows.

    Returns count of new rows inserted. Markets in non-finalized status (e.g.
    still in the 60s settlement_timer window) are skipped — they'll be
    picked up on the next call."""
    now_ms = int(time.time() * 1000)
    new = 0
    for et in _recent_closed_events(kc, series, lookback):
        try:
            markets = kc.list_markets(event_ticker=et, limit=500).get("markets", [])
        except Exception as e:
            log.warning("list_markets(%s) failed: %s", et, e)
            continue
        for m in markets:
            status = (m.get("status") or "").lower()
            # Kalshi uses 'finalized' for fully-settled markets; 'settled' may
            # appear briefly during the 60s settlement timer.
            if status not in {"finalized", "settled"}:
                continue
            ticker = m.get("ticker")
            strike = _f(m.get("floor_strike"))
            if not ticker or strike is None:
                continue
            result = (m.get("result") or "").lower()
            if result not in {"yes", "no", "void"}:
                # Defensive: log but don't drop — store whatever Kalshi said.
                log.debug("unexpected result %r for %s", result, ticker)
            settled_yes = 1 if result == "yes" else 0
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO market_settlements (
                    recorded_ts_ms, settlement_ts_ms, event_ticker, market_ticker,
                    strike, result, settled_yes, expiration_value, volume,
                    open_interest, raw_response
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_ms,
                    _parse_iso_ms(m.get("settlement_ts")),
                    et,
                    ticker,
                    strike,
                    result,
                    settled_yes,
                    _f(m.get("expiration_value")),
                    _f(m.get("volume_fp")),
                    _f(m.get("open_interest_fp")),
                    json.dumps(m),
                ),
            )
            if cur.rowcount:
                new += 1
    return new


class SettlementScraper:
    """Throttled wrapper. Call .maybe_scrape() liberally — actual API calls
    happen at most once per `interval_s`. Default 5 minutes is plenty: events
    close hourly, so we'd only miss data if the trader were down for >4h."""

    def __init__(
        self,
        kc: KalshiClient,
        *,
        interval_s: float = 300.0,
        series: str = "KXSOLD",
    ) -> None:
        self.kc = kc
        self.interval_s = interval_s
        self.series = series
        self._last_run = 0.0

    def maybe_scrape(self, conn: sqlite3.Connection) -> None:
        now = time.time()
        if now - self._last_run < self.interval_s:
            return
        self._last_run = now
        try:
            n = scrape_settlements(conn, self.kc, series=self.series)
            if n:
                log.info("market_settlements: +%d new rows", n)
        except Exception:
            log.exception("settlement scrape failed; will retry")
