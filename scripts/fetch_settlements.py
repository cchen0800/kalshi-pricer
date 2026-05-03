"""Fetch Coinbase BTC-USD 1m candles around each event close → cache settlement prices.

For each KXBTCD-YYMMMDDHH event, Kalshi settles against the BRTI averaged over
the final 60 seconds. We approximate with Coinbase: fetch the 1m candle that
starts at close-1m (i.e. its window is [close-60s, close)). Settlement price ≈
average of OHLC of that candle (rough 60s-mean proxy).

Writes a `settlements` table to pricer.db:
  event_ticker | close_utc_s | settle_price | source ('coinbase_1m')
"""
from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.engine import event_close_utc

DB_PATH = ROOT / "pricer.db"
PRODUCT = "BTC-USD"
COINBASE = "https://api.exchange.coinbase.com"


def fetch_minute_candle(client: httpx.Client, close_dt: datetime) -> tuple[float, float, float, float] | None:
    """Return (open, high, low, close) of the 1-min candle whose window is the
    final 60s before close. Coinbase: granularity=60, params start/end ISO."""
    end = close_dt
    start = end.replace(second=0)  # bucket-aligned start
    # Coinbase wants ISO strings; window must be < 300 candles.
    params = {
        "granularity": 60,
        "start": (start.replace(tzinfo=timezone.utc)).isoformat().replace("+00:00", "Z"),
        "end": (end.replace(tzinfo=timezone.utc)).isoformat().replace("+00:00", "Z"),
    }
    r = client.get(f"{COINBASE}/products/{PRODUCT}/candles", params=params, timeout=10.0)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return None
    # Coinbase: [time, low, high, open, close, volume]; pick the one whose time == start.
    target = int(start.timestamp())
    for row in rows:
        if int(row[0]) == target:
            return (float(row[3]), float(row[2]), float(row[1]), float(row[4]))
    # Fall back to nearest
    rows.sort(key=lambda r_: abs(int(r_[0]) - target))
    row = rows[0]
    return (float(row[3]), float(row[2]), float(row[1]), float(row[4]))


def main() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS settlements (
            event_ticker TEXT PRIMARY KEY,
            close_utc_s  INTEGER NOT NULL,
            open_px      REAL NOT NULL,
            high_px      REAL NOT NULL,
            low_px       REAL NOT NULL,
            close_px     REAL NOT NULL,
            settle_price REAL NOT NULL,
            source       TEXT NOT NULL
        )"""
    )
    events = [r[0] for r in conn.execute("SELECT DISTINCT event_ticker FROM polls").fetchall()]
    already = {r[0] for r in conn.execute("SELECT event_ticker FROM settlements").fetchall()}
    todo = [e for e in events if e not in already]
    print(f"events: total={len(events)} already_cached={len(already)} todo={len(todo)}")

    now_utc = datetime.now(timezone.utc)
    with httpx.Client() as client:
        for et in todo:
            close_dt = event_close_utc(et)
            if close_dt is None:
                print(f"  [skip] cannot parse close for {et}")
                continue
            if close_dt > now_utc:
                print(f"  [skip] {et} not yet closed (close={close_dt.isoformat()})")
                continue
            try:
                ohlc = fetch_minute_candle(client, close_dt)
            except Exception as e:
                print(f"  [err] {et}: {e}")
                continue
            if ohlc is None:
                print(f"  [miss] {et}: no candle")
                continue
            o, h, l, c = ohlc
            # 60s average proxy: (open + close) / 2 is the simplest unbiased estimate
            # for a brownian-motion segment; (h+l)/2 is range-midpoint. Use the mean
            # of all four as a robust estimator.
            settle = (o + h + l + c) / 4.0
            conn.execute(
                "INSERT OR REPLACE INTO settlements VALUES (?,?,?,?,?,?,?,?)",
                (et, int(close_dt.timestamp()), o, h, l, c, settle, "coinbase_1m"),
            )
            print(f"  {et}  close={close_dt.isoformat()}  OHLC={o:.2f}/{h:.2f}/{l:.2f}/{c:.2f}  settle≈{settle:.2f}")
            time.sleep(0.15)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
