"""Discover Kalshi's ETH market series and settlement methodology.

Tries candidate series tickers (KXETHD, KXETH, KXETHW, KXETHU) and also scans
all open events for any whose event_ticker contains 'ETH'. For the first
matching series, dumps:
  - event_ticker format (sample tickers + parse attempt)
  - market_ticker format (strike encoding)
  - settlement_value / settlement_source / rules_primary / underlying
  - cadence (hourly? daily?) + strike-ladder size

Output is hand-read; informs the regex and config for the eth-pricer mirror.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.kalshi_client import KalshiClient

CANDIDATE_SERIES = ["KXETHD", "KXETH", "KXETHW", "KXETHU", "KXETHM"]

SETTLEMENT_FIELDS = (
    "settlement_value",
    "settlement_source",
    "rules_primary",
    "rules_secondary",
    "expected_expiration_time",
    "close_time",
    "expiration_time",
    "underlying",
    "settlement_timer_seconds",
    "settlement_value_units",
)


def trunc(v: object, n: int = 500) -> object:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"... [+{len(v) - n} chars]"
    return v


def probe_series(kc: KalshiClient, series: str) -> list[dict]:
    """Return open events for series, or empty list."""
    try:
        resp = kc.list_events(series_ticker=series, status="open", limit=50)
    except Exception as e:
        print(f"  [{series}] error: {e}")
        return []
    evs = resp.get("events", []) or []
    return evs


def scan_open_events_for_eth(kc: KalshiClient) -> list[dict]:
    """Pull open events without a series filter and grep tickers/titles for ETH."""
    print("Scanning all open events for ETH-related tickers...")
    matches: list[dict] = []
    cursor: str | None = None
    pages = 0
    while pages < 10:
        params: dict[str, str | int] = {"limit": 200, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = kc._request("GET", "/events", params=params)
        except Exception as e:
            print(f"  scan error: {e}")
            break
        evs = resp.get("events", []) or []
        for e in evs:
            tk = (e.get("event_ticker") or "").upper()
            ttl = (e.get("title") or "").upper()
            if "ETH" in tk or "ETHEREUM" in ttl or "ETHER" in ttl:
                matches.append(e)
        cursor = resp.get("cursor")
        pages += 1
        if not cursor:
            break
    return matches


def summarize_series_from_events(evs: list[dict]) -> str | None:
    """Infer series ticker from a batch of events (the prefix before the first '-')."""
    series = Counter()
    for e in evs:
        tk = e.get("event_ticker") or ""
        if "-" in tk:
            series[tk.split("-", 1)[0]] += 1
    if not series:
        return None
    return series.most_common(1)[0][0]


def dump_series(kc: KalshiClient, series: str, evs: list[dict]) -> None:
    print(f"\n========== SERIES: {series} ({len(evs)} open events) ==========\n")

    # Sample event tickers — first 8.
    print("Sample event tickers:")
    for e in evs[:8]:
        print(f"  {e.get('event_ticker')!r:50s}  title={e.get('title')!r}")
    print()

    # Pick one event with a non-empty market list.
    chosen_event = None
    chosen_markets: list[dict] = []
    for e in evs[:6]:
        et = e.get("event_ticker")
        if not et:
            continue
        try:
            ms = kc.list_markets(event_ticker=et, limit=500).get("markets", []) or []
        except Exception as exc:
            print(f"  list_markets({et}) failed: {exc}")
            continue
        if ms:
            chosen_event, chosen_markets = e, ms
            break

    if not chosen_event:
        print("  No event had any markets — series may be paused or pre-launch.")
        return

    et = chosen_event["event_ticker"]
    print(f"Inspecting event: {et}")
    print(f"  Title: {chosen_event.get('title')}")
    print(f"  Sub-title: {chosen_event.get('sub_title')}")
    print(f"  Markets: {len(chosen_markets)}")
    print()

    print("Sample market tickers (first 6):")
    for m in chosen_markets[:6]:
        print(f"  {m.get('ticker')!r}")
    print()

    # Strike ladder snapshot.
    rows: list[tuple[float, float, float, str]] = []
    for m in chosen_markets:
        try:
            rows.append((
                float(m.get("floor_strike") or 0),
                float(m.get("yes_bid_dollars") or 0),
                float(m.get("yes_ask_dollars") or 0),
                m.get("ticker") or "",
            ))
        except (TypeError, ValueError):
            continue
    rows.sort()
    if rows:
        print(f"Strike ladder ({len(rows)} strikes). Sample:")
        print(f"  {'strike':>12} {'yes_bid':>8} {'yes_ask':>8}")
        step = max(1, len(rows) // 10)
        for k, b, a, _ in rows[::step]:
            print(f"  {k:>12,.4f} {b:>8.3f} {a:>8.3f}")
        print()

    # Full market detail for settlement fields.
    full = kc.get_market(chosen_markets[0]["ticker"]).get("market", {})
    print(f"=== Settlement-relevant fields for {full.get('ticker')} ===")
    for f in SETTLEMENT_FIELDS:
        if f in full:
            print(f"{f}:")
            print(f"  {trunc(full.get(f))}")
    # Also dump full keys list so we don't miss anything.
    print()
    print("All fields on market object:")
    print(f"  {sorted(full.keys())}")

    # Try a regex match against current TICKER_RE to see if same date format.
    print()
    pattern_btc = re.compile(rf"^{series}-(\d{{2}})([A-Z]{{3}})(\d{{2}})(\d{{2}})$")
    matches = sum(1 for e in evs if pattern_btc.match(e.get("event_ticker") or ""))
    print(f"Events matching `^{series}-YYMMM DDHH$` pattern: {matches}/{len(evs)}")
    if matches == 0:
        print("  → date format differs from BTC's. Inspect sample tickers above.")


def main() -> int:
    with KalshiClient() as kc:
        # Phase A: try named candidates.
        print("Phase A: probing named candidate series...")
        hits: list[tuple[str, list[dict]]] = []
        for s in CANDIDATE_SERIES:
            evs = probe_series(kc, s)
            print(f"  {s}: {len(evs)} open events")
            if evs:
                hits.append((s, evs))

        # Phase B: scan all open events for ETH mentions.
        print()
        scan_hits = scan_open_events_for_eth(kc)
        print(f"Found {len(scan_hits)} open events with 'ETH' in ticker/title.")
        if scan_hits:
            print("Sample:")
            for e in scan_hits[:10]:
                print(f"  {e.get('event_ticker')!r:50s}  {e.get('title')!r}")
            inferred = summarize_series_from_events(scan_hits)
            print(f"\nInferred dominant series prefix from scan: {inferred}")
            if inferred and inferred not in [s for s, _ in hits]:
                # Re-probe inferred series via the proper filter.
                evs = probe_series(kc, inferred)
                if evs:
                    hits.append((inferred, evs))

        if not hits:
            print("\nNo ETH series found. Mirror is not viable until Kalshi lists ETH markets.")
            return 1

        for series, evs in hits:
            dump_series(kc, series, evs)

        return 0


if __name__ == "__main__":
    raise SystemExit(main())
