"""Replay logged polls under two edge regimes and compare realized P&L.

Old regime: filter on GROSS edge (model_prob*100 - ask*100 for buys, mirror for sells).
New regime: filter on NET edge (gross minus Kalshi taker fee).

Both regimes pay the fee on every executed trade — the only thing that changes is
which trades pass the filter. We dedupe per (event, strike) by first-trigger, then
join to settlements for realized outcome.

YZ note: we cannot retro-recompute σ_yz on stored polls (only `sigma` is logged,
not raw OHLC). We do report stored σ_cc summary stats so the user can compare to
the live YZ smoke test going forward.
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
MIN_EDGE_CENTS = 15.0       # matches src/executor.py
MIN_MINUTES_TO_CLOSE = 5.0  # matches src/executor.py


def fee_cents(price_cents: int) -> int:
    """Per-contract Kalshi taker fee. Mirrors src/positions.kalshi_fee_cents."""
    p = max(1, min(99, price_cents)) / 100.0
    return math.ceil(0.07 * p * (1.0 - p) * 100.0)


def load() -> pd.DataFrame:
    conn = sqlite3.connect(str(ROOT / "pricer.db"))
    df = pd.read_sql_query("SELECT * FROM polls", conn).merge(
        pd.read_sql_query("SELECT event_ticker, settle_price FROM settlements", conn),
        on="event_ticker", how="inner",
    )
    conn.close()
    df = df[df["yes_bid"].notna() & df["yes_ask"].notna()].copy()
    df = df[df["minutes_left"] >= MIN_MINUTES_TO_CLOSE]
    df["outcome"] = (df["settle_price"] > df["strike"]).astype(int)
    df["ask_c"] = (df["yes_ask"] * 100).round().clip(1, 99).astype(int)
    df["bid_c"] = (df["yes_bid"] * 100).round().clip(1, 99).astype(int)
    df["buy_fee"] = df["ask_c"].map(fee_cents)
    df["sell_fee"] = df["bid_c"].map(fee_cents)
    df["buy_gross"] = df["model_prob"] * 100 - df["yes_ask"] * 100
    df["sell_gross"] = df["yes_bid"] * 100 - df["model_prob"] * 100
    df["buy_net"] = df["buy_gross"] - df["buy_fee"]
    df["sell_net"] = df["sell_gross"] - df["sell_fee"]
    # Realized per-contract P&L (in cents). Fee is real either way.
    df["buy_pnl_c"] = (df["outcome"] - df["yes_ask"]) * 100 - df["buy_fee"]
    df["sell_pnl_c"] = (df["yes_bid"] - df["outcome"]) * 100 - df["sell_fee"]
    return df


def first_trigger(df: pd.DataFrame, mask: pd.Series, side: str) -> pd.DataFrame:
    """First poll per (event, strike) where `mask` is True. Realized P&L for `side`."""
    triggered = df[mask].sort_values("ts_ms")
    picked = triggered.groupby(["event_ticker", "strike"], as_index=False).first()
    picked["pnl_c"] = picked[f"{side}_pnl_c"]
    picked["side"] = side.upper()
    return picked


def summarize(label: str, picks: pd.DataFrame) -> None:
    n = len(picks)
    if n == 0:
        print(f"{label:>14}: 0 trades")
        return
    pnl = picks["pnl_c"]
    total = pnl.sum()
    mean = pnl.mean()
    se = pnl.std(ddof=1) / math.sqrt(n) if n > 1 else float("nan")
    win = (pnl > 0).mean() * 100
    print(
        f"{label:>14}: n={n:4d}  total={total:+8.1f}¢  "
        f"mean={mean:+5.2f}¢ ±{se:.2f}  win={win:4.1f}%"
    )


def compare(df: pd.DataFrame, threshold: float) -> None:
    print(f"\n--- threshold = {threshold:.0f}¢ ---")
    for side in ("buy", "sell"):
        gross_mask = df[f"{side}_gross"] > threshold
        net_mask = df[f"{side}_net"] > threshold

        gross_picks = first_trigger(df, gross_mask, side)
        net_picks = first_trigger(df, net_mask, side)

        summarize(f"{side.upper()} OLD", gross_picks)
        summarize(f"{side.upper()} NEW", net_picks)

        # Trades the new filter killed: in OLD set, not in NEW set (by event+strike).
        if len(gross_picks):
            killed = gross_picks.merge(
                net_picks[["event_ticker", "strike"]],
                on=["event_ticker", "strike"], how="left", indicator=True,
            )
            killed = killed[killed["_merge"] == "left_only"]
            if len(killed):
                kpnl = killed["pnl_c"]
                print(
                    f"  {len(killed)} {side.upper()} trades killed by fee filter — "
                    f"their actual P&L: total={kpnl.sum():+.1f}¢  "
                    f"mean={kpnl.mean():+.2f}¢  win={(kpnl>0).mean()*100:.1f}%"
                )


def main() -> None:
    df = load()
    n_polls = len(df)
    n_events = df["event_ticker"].nunique()
    sigma = df["sigma"]
    print(f"polls: {n_polls:,}  events: {n_events}  T-minutes ≥ {MIN_MINUTES_TO_CLOSE:.0f}")
    print(
        f"stored σ (close-to-close): "
        f"min={sigma.min():.2f}  median={sigma.median():.2f}  "
        f"mean={sigma.mean():.2f}  max={sigma.max():.2f}"
    )

    # Compare at the thresholds that matter operationally.
    for th in (5.0, MIN_EDGE_CENTS):
        compare(df, th)

    print(
        "\nNote: σ_yz vs σ_cc cannot be backtested from this DB (no OHLC stored). "
        "After the next run, the live engine will log σ from yang_zhang_vol; "
        "compare distributions then."
    )


if __name__ == "__main__":
    main()
