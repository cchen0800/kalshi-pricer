"""Bucketed backtest: search for alpha at σ < 50% by sub-dividing the
low-vol regime along distance × minutes_left × side.

Motivation: the live BUY gate requires σ ≥ 0.50 (engine.py:186). That
threshold was fit on 33 trades from May 2-4; the σ ∈ [0.25, 0.50) band is
under-sampled rather than confirmed-bad. If a sub-cell (e.g. far-OTM,
T>30m, BUY_NO) is net-positive after fees, we can express it as a *second*
gate path without touching the σ≥0.50 regime.

Outputs one block per σ band:
  σ band → [dist bucket × T bucket × side] table of n, mean P&L, win%, total

Usage:
  python scripts/backtest_lowvol_regime.py [DB_PATH] [SERIES_PREFIX]
  defaults: pricer.db, KXBTCD-

Note on fees / mp-band: mirrors backtest_sigma_gate.py — Kalshi taker fee
ceil(0.07*p*(1-p)*100), calibrated mp band [0.05, 0.85), dist≥0.1%, T≥15m.
We deliberately keep the live non-σ gates ON so any cell that prints alpha
is alpha *additional* to what those gates already filter.
"""
from __future__ import annotations

import math
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "pricer.db"

# Mirror live engine gates (src/engine.py)
BUY_GATE_MIN_DIST_PCT = 0.0010
BUY_GATE_MIN_MINUTES = 15.0
BUY_GATE_MP_BAND_LO = 0.05
BUY_GATE_MP_BAND_HI = 0.85

SIGMA_BANDS = [
    ("σ < 0.25", 0.0, 0.25),
    ("σ [0.25,0.35)", 0.25, 0.35),
    ("σ [0.35,0.50)", 0.35, 0.50),
    ("σ ≥ 0.50  (current live regime)", 0.50, math.inf),
]

# Distance buckets: |strike - spot| / spot, in percent
DIST_BUCKETS = [
    ("near (0.1-0.3%)", 0.0010, 0.0030),
    ("mid  (0.3-0.6%)", 0.0030, 0.0060),
    ("far  (≥0.6%)",   0.0060, math.inf),
]

# Time-to-close buckets (minutes)
T_BUCKETS = [
    ("T 15-25m", 15.0, 25.0),
    ("T 25-40m", 25.0, 40.0),
    ("T ≥40m",   40.0, math.inf),
]


def fee_cents(price: float) -> float:
    return math.ceil(0.07 * price * (1.0 - price) * 100)


def load(db_path: Path, series_prefix: str) -> pd.DataFrame:
    conn = sqlite3.connect(str(db_path))
    polls = pd.read_sql_query(
        f"""SELECT ts_ms, event_ticker, market_ticker, strike, spot, sigma,
                  minutes_left, model_prob, model_prob_calibrated,
                  yes_bid, yes_ask, no_bid, no_ask
           FROM polls
           WHERE event_ticker LIKE '{series_prefix}%'""",
        conn,
    )
    settle = pd.read_sql_query(
        "SELECT event_ticker, settle_price FROM settlements", conn
    )
    conn.close()
    df = polls.merge(settle, on="event_ticker", how="inner")
    df["yes_outcome"] = (df["settle_price"] > df["strike"]).astype(int)
    df["no_outcome"] = 1 - df["yes_outcome"]
    df["dist_pct"] = (df["strike"] - df["spot"]).abs() / df["spot"]
    df["mp"] = df["model_prob_calibrated"].fillna(df["model_prob"])
    return df


def apply_non_sigma_gates(df: pd.DataFrame) -> pd.DataFrame:
    return df[
        (df["dist_pct"] >= BUY_GATE_MIN_DIST_PCT)
        & (df["minutes_left"] >= BUY_GATE_MIN_MINUTES)
        & (df["mp"] >= BUY_GATE_MP_BAND_LO)
        & (df["mp"] < BUY_GATE_MP_BAND_HI)
        & df["yes_bid"].notna()
        & df["yes_ask"].notna()
    ].copy()


def annotate_edges(g: pd.DataFrame) -> pd.DataFrame:
    """Attach per-side net edge and realized P&L for every poll row."""
    g = g.copy()
    yes_fee = (0.07 * g["yes_ask"] * (1 - g["yes_ask"]) * 100).apply(math.ceil)
    g["yes_net_edge_cents"] = (g["mp"] * 100 - g["yes_ask"] * 100) - yes_fee
    g["yes_pnl_cents"] = (g["yes_outcome"] - g["yes_ask"]) * 100 - yes_fee

    no_ask = g["no_ask"].fillna(1.0 - g["yes_bid"])
    no_fee = (0.07 * no_ask * (1 - no_ask) * 100).apply(math.ceil)
    g["no_ask_eff"] = no_ask
    g["no_net_edge_cents"] = ((1 - g["mp"]) * 100 - no_ask * 100) - no_fee
    g["no_pnl_cents"] = (g["no_outcome"] - no_ask) * 100 - no_fee
    return g


def trades_in(g: pd.DataFrame, *, edge_floor_cents: float) -> pd.DataFrame:
    """For a pre-filtered slice, take BUY_YES and BUY_NO whenever the net
    edge clears the floor. Dedupe per (event, strike, side), earliest wins."""
    if g.empty:
        return g.iloc[0:0]
    yes = g[g["yes_net_edge_cents"] >= edge_floor_cents].assign(
        side="BUY_YES",
        signal_edge_cents=lambda x: x["yes_net_edge_cents"],
        pnl_cents=lambda x: x["yes_pnl_cents"],
    )
    no = g[g["no_net_edge_cents"] >= edge_floor_cents].assign(
        side="BUY_NO",
        signal_edge_cents=lambda x: x["no_net_edge_cents"],
        pnl_cents=lambda x: x["no_pnl_cents"],
    )
    trades = pd.concat([yes, no], ignore_index=True)
    if trades.empty:
        return trades
    return trades.sort_values("ts_ms").groupby(
        ["event_ticker", "strike", "side"], as_index=False
    ).first()


def fmt_row(label: str, t: pd.DataFrame) -> str:
    if t.empty:
        return f"    {label:<22}  (none)"
    n = len(t)
    n_y = (t["side"] == "BUY_YES").sum()
    n_n = (t["side"] == "BUY_NO").sum()
    mean = t["pnl_cents"].mean()
    win = (t["pnl_cents"] > 0).mean() * 100
    total = t["pnl_cents"].sum()
    avg_edge = t["signal_edge_cents"].mean()
    return (
        f"    {label:<22}  n={n:>4}  Y/N={n_y}/{n_n}  "
        f"mean={mean:+5.2f}¢  win={win:5.1f}%  "
        f"total={total:+7.0f}¢  avg_edge={avg_edge:4.1f}¢"
    )


def cell(g: pd.DataFrame, sigma_lo: float, sigma_hi: float,
         dist_lo: float, dist_hi: float,
         t_lo: float, t_hi: float, side: str | None,
         floor: float) -> pd.DataFrame:
    sl = g[(g["sigma"] >= sigma_lo) & (g["sigma"] < sigma_hi)
           & (g["dist_pct"] >= dist_lo) & (g["dist_pct"] < dist_hi)
           & (g["minutes_left"] >= t_lo) & (g["minutes_left"] < t_hi)]
    tr = trades_in(sl, edge_floor_cents=floor)
    if side is not None and not tr.empty:
        tr = tr[tr["side"] == side]
    return tr


def print_band(label: str, sigma_lo: float, sigma_hi: float,
               g: pd.DataFrame, floor: float) -> None:
    band_rows = g[(g["sigma"] >= sigma_lo) & (g["sigma"] < sigma_hi)]
    print(f"\n  {label}   (poll rows in band: {len(band_rows):,})")
    if band_rows.empty:
        print("    (no rows)")
        return
    # Aggregate row across the whole band (both sides)
    agg = trades_in(band_rows, edge_floor_cents=floor)
    print(fmt_row("ALL  · either side", agg))
    for d_lab, d_lo, d_hi in DIST_BUCKETS:
        for t_lab, t_lo, t_hi in T_BUCKETS:
            for s in ("BUY_YES", "BUY_NO"):
                tr = cell(g, sigma_lo, sigma_hi, d_lo, d_hi, t_lo, t_hi, s, floor)
                lab = f"{d_lab[:4]}|{t_lab[2:]}|{s[4:]}"
                print(fmt_row(lab, tr))


def main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH
    series = sys.argv[2] if len(sys.argv) > 2 else "KXBTCD-"
    print(f"loading from {db_path}  series={series}")
    df = load(db_path, series_prefix=series)
    print(f"  {len(df):,} poll rows across {df['event_ticker'].nunique()} settled events")
    df_g = apply_non_sigma_gates(df)
    print(f"  after non-σ gates (dist≥0.1%, T≥15m, mp∈[0.05,0.85)): {len(df_g):,} rows")
    df_g = annotate_edges(df_g)

    for floor in (3.0, 5.0):
        print()
        print("=" * 100)
        print(f"EDGE FLOOR = {floor:.0f}¢   "
              f"({'selective live' if floor==3.0 else 'higher floor — recommended for low-vol cells'})")
        print("=" * 100)
        for label, lo, hi in SIGMA_BANDS:
            print_band(label, lo, hi, df_g, floor)


if __name__ == "__main__":
    main()
