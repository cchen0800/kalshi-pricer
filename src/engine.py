"""Main loop: every N seconds, fetch → price → compare → log.

Read-only. No order placement anywhere in this module.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from src.btc_feed import CoinbaseFeed, closes
from src.db import PollRow, insert_polls, open_db
from src.kalshi_client import KalshiClient
from src.pricer import edge_cents, prob_above_strike
from src.vol import annualized_vol

log = logging.getLogger("engine")

EDT = ZoneInfo("America/New_York")
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
TICKER_RE = re.compile(r"^KXBTCD-(\d{2})([A-Z]{3})(\d{2})(\d{2})$")


@dataclass
class EngineConfig:
    poll_interval_seconds: int = 30
    edge_threshold_cents: float = 5.0
    vol_window_minutes: int = 60
    db_path: str = "./pricer.db"
    series: str = "KXBTCD"


def event_close_utc(event_ticker: str) -> datetime | None:
    m = TICKER_RE.match(event_ticker)
    if not m:
        return None
    yy, mon, dd, hh = m.groups()
    if mon not in MONTHS:
        return None
    return datetime(2000 + int(yy), MONTHS[mon], int(dd), int(hh), 0, tzinfo=EDT).astimezone(
        timezone.utc
    )


def find_nearest_open_event(kc: KalshiClient, series: str) -> tuple[str, datetime] | None:
    """Returns (event_ticker, close_utc) for the nearest hourly that hasn't closed yet."""
    evs = kc.list_events(series_ticker=series, status="open", limit=200).get("events", [])
    if not evs:
        evs = kc.list_events(series_ticker=series, limit=200).get("events", [])
    now = datetime.now(timezone.utc)
    candidates = []
    for e in evs:
        ct = event_close_utc(e["event_ticker"])
        if ct is not None and ct > now:
            candidates.append((e["event_ticker"], ct))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1])
    return candidates[0]


def _f(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_poll_rows(
    *,
    ts_ms: int,
    event_ticker: str,
    markets: Iterable[dict],
    spot: float,
    sigma: float,
    minutes_left: float,
) -> list[PollRow]:
    rows: list[PollRow] = []
    for m in markets:
        strike = _f(m.get("floor_strike"))
        if strike is None or strike <= 0:
            continue
        yes_bid = _f(m.get("yes_bid_dollars"))
        yes_ask = _f(m.get("yes_ask_dollars"))
        bid_size = _f(m.get("yes_bid_size_fp"))
        ask_size = _f(m.get("yes_ask_size_fp"))
        volume = _f(m.get("volume_fp"))

        model_prob = prob_above_strike(spot, strike, sigma, minutes_left)
        # Signed edge vs. mid (positive = model > mid, suggests buy YES).
        if yes_bid is not None and yes_ask is not None:
            mid_cents = (yes_bid + yes_ask) * 50.0
            edge = model_prob * 100.0 - mid_cents
        else:
            edge = 0.0

        rows.append(PollRow(
            ts_ms=ts_ms,
            event_ticker=event_ticker,
            market_ticker=m["ticker"],
            strike=strike,
            spot=spot,
            sigma=sigma,
            minutes_left=minutes_left,
            model_prob=model_prob,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            yes_bid_size=bid_size,
            yes_ask_size=ask_size,
            volume=volume,
            edge_cents=edge,
        ))
    return rows


def actionable_edge(row: PollRow) -> tuple[str, float]:
    """Return (side, cents) of best lift-the-market edge. side ∈ {'BUY_YES','SELL_YES','NONE'}."""
    if row.yes_ask is not None:
        buy = row.model_prob * 100.0 - row.yes_ask * 100.0
    else:
        buy = float("-inf")
    if row.yes_bid is not None:
        sell = row.yes_bid * 100.0 - row.model_prob * 100.0
    else:
        sell = float("-inf")
    if buy >= sell and buy > 0:
        return "BUY_YES", buy
    if sell > 0:
        return "SELL_YES", sell
    return "NONE", 0.0


def run_one_poll(
    *,
    kc: KalshiClient,
    feed: CoinbaseFeed,
    cfg: EngineConfig,
    db,
) -> None:
    found = find_nearest_open_event(kc, cfg.series)
    if found is None:
        log.warning("no open hourly event found")
        return
    event_ticker, close_utc = found
    minutes_left = max(0.0, (close_utc - datetime.now(timezone.utc)).total_seconds() / 60.0)
    if minutes_left <= 0:
        log.info("event %s already closed", event_ticker)
        return

    spot_obj = feed.get_spot()
    candles = feed.get_1m_candles(cfg.vol_window_minutes)
    if len(candles) < 2:
        log.warning("not enough candles to compute vol (%d)", len(candles))
        return
    sigma = annualized_vol(closes(candles))

    markets = kc.list_markets(event_ticker=event_ticker, limit=500).get("markets", [])
    ts_ms = int(time.time() * 1000)
    rows = build_poll_rows(
        ts_ms=ts_ms,
        event_ticker=event_ticker,
        markets=markets,
        spot=spot_obj.price,
        sigma=sigma,
        minutes_left=minutes_left,
    )
    n = insert_polls(db, rows)

    flagged = [r for r in rows if abs(actionable_edge(r)[1]) > cfg.edge_threshold_cents]
    flagged.sort(key=lambda r: -abs(actionable_edge(r)[1]))

    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] "
        f"{event_ticker}  spot=${spot_obj.price:,.2f}  σ={sigma:.1%}  "
        f"T-{minutes_left:5.1f}min  rows={n}  flagged={len(flagged)}"
    )
    for r in flagged[:8]:
        side, cents = actionable_edge(r)
        bid = f"{r.yes_bid:.3f}" if r.yes_bid is not None else "  —  "
        ask = f"{r.yes_ask:.3f}" if r.yes_ask is not None else "  —  "
        print(
            f"   K=${r.strike:>10,.2f}  model={r.model_prob*100:5.1f}¢  "
            f"bid={bid} ask={ask}  → {side} +{cents:.1f}¢"
        )


def run(cfg: EngineConfig, stop_event: threading.Event | None = None) -> None:
    """Run the polling loop.

    If `stop_event` is supplied (e.g. from a dashboard host running this in a
    background thread), the loop exits when the event is set. Otherwise it
    runs until KeyboardInterrupt.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with KalshiClient() as kc, CoinbaseFeed() as feed, open_db(cfg.db_path) as db:
        log.info("engine started — db=%s poll=%ds threshold=%.1f¢",
                 cfg.db_path, cfg.poll_interval_seconds, cfg.edge_threshold_cents)
        while True:
            if stop_event is not None and stop_event.is_set():
                log.info("stop event set, exiting")
                return
            t0 = time.time()
            try:
                run_one_poll(kc=kc, feed=feed, cfg=cfg, db=db)
            except KeyboardInterrupt:
                log.info("interrupted, exiting")
                return
            except Exception:
                log.exception("poll failed; will retry next interval")
            elapsed = time.time() - t0
            remaining = max(0.0, cfg.poll_interval_seconds - elapsed)
            # Sleep in small increments so stop_event is responsive.
            if stop_event is not None:
                deadline = time.time() + remaining
                while time.time() < deadline:
                    if stop_event.is_set():
                        return
                    time.sleep(min(0.5, deadline - time.time()))
            else:
                time.sleep(remaining)
