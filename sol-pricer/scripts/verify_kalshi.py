"""One-shot: verify Kalshi auth works and inspect KXSOLD market metadata.

Goal: confirm what Kalshi settles SOL hourly markets against (Coinbase? a CF index?)
so we know which spot source to track in sol_feed.py.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.kalshi_client import KalshiClient

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
EDT = ZoneInfo("America/New_York")
TICKER_RE = re.compile(r"^KXSOLD-(\d{2})([A-Z]{3})(\d{2})(\d{2})$")


def event_close_utc(event_ticker: str) -> datetime | None:
    m = TICKER_RE.match(event_ticker)
    if not m:
        return None
    yy, mon, dd, hh = m.groups()
    if mon not in MONTHS:
        return None
    local = datetime(2000 + int(yy), MONTHS[mon], int(dd), int(hh), 0, tzinfo=EDT)
    return local.astimezone(timezone.utc)


SETTLEMENT_FIELDS = (
    "settlement_value",
    "settlement_source",
    "rules_primary",
    "rules_secondary",
    "expected_expiration_time",
    "close_time",
    "expiration_time",
    "underlying",
)


def trunc(v: object, n: int = 400) -> object:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"... [+{len(v) - n} chars]"
    return v


def main() -> int:
    series = "KXSOLD"
    with KalshiClient() as kc:
        evs = kc.list_events(series_ticker=series, status="open", limit=200).get("events", [])
        if not evs:
            evs = kc.list_events(series_ticker=series, limit=200).get("events", [])
        if not evs:
            print(f"No events for series {series}.")
            return 2

        # Parse close time from the ticker (event payload doesn't include close_time in summary).
        now = datetime.now(timezone.utc)
        annotated = [(event_close_utc(e["event_ticker"]), e) for e in evs]
        future = sorted(
            [(ct, e) for ct, e in annotated if ct is not None and ct > now],
            key=lambda pair: pair[0],
        )
        if not future:
            print("No future hourlies parsed from tickers; falling back to first.")
            ev = evs[0]
            mins_to_close = float("nan")
        else:
            mins_to_close, ev = (future[0][0] - now).total_seconds() / 60.0, future[0][1]
            print(f"Nearest open hourly: {ev['event_ticker']} — closes in {mins_to_close:.1f} min")
        print()
        print(f"Event ticker:  {ev['event_ticker']}")
        print(f"Title:         {ev.get('title')}")
        print(f"Sub-title:     {ev.get('sub_title')}")
        print()

        ms = kc.list_markets(event_ticker=ev["event_ticker"], limit=500).get("markets", [])
        rows: list[tuple[float, float, float, str]] = []
        for m in ms:
            try:
                rows.append((
                    float(m.get("floor_strike") or 0),
                    float(m.get("yes_bid_dollars") or 0),
                    float(m.get("yes_ask_dollars") or 0),
                    m["ticker"],
                ))
            except (TypeError, ValueError):
                continue
        rows.sort()
        print(f"{len(rows)} strikes. Sample ladder:")
        print(f"{'strike':>10} {'yes_bid':>8} {'yes_ask':>8}  {'mid':>6}")
        step = max(1, len(rows) // 12)
        for k, b, a, _ in rows[::step]:
            print(f"{k:>10,.2f} {b:>8.3f} {a:>8.3f}  {(b + a) / 2:>5.3f}")
        print()

        if not ms:
            return 0

        full = kc.get_market(ms[0]["ticker"]).get("market", {})
        print(f"=== Settlement-relevant fields for {full.get('ticker')} ===")
        for f in SETTLEMENT_FIELDS:
            if f in full:
                print(f"{f}:")
                print(f"  {trunc(full[f])}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
