"""Backtest BUY_GATE_MIN_SIGMA at 0.40 vs 0.50 (and intermediate values).

Question: would lowering the gate to 0.40 net positive EV after fees, or
just unlock fee-fodder trades?

Method:
  - Pull polls + settlements from `pricer.db` (run scripts/fetch_settlements.py
    to populate `settlements` first).
  - For each σ band, simulate the bot's BUY entries: take BUY_YES when
    model_prob*100 - yes_ask*100 - fees > MIN_EDGE_FLOOR, BUY_NO when the
    symmetric NO-side edge clears the floor.
  - Resolve at settlement, compute net P&L per contract after fees.
  - Dedupe to one trade per (event, strike, side) — first signal wins.
  - Apply non-σ gates (dist, minutes_left, mp-band) to mirror the live engine.

Outputs: trade count, mean P&L/contract, win rate, total P&L.
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

# ---- Live engine gate constants (mirror src/engine.py) ----
BUY_GATE_MIN_DIST_PCT = 0.0010
BUY_GATE_MIN_MINUTES = 15.0
BUY_GATE_MP_BAND_LO = 0.05
BUY_GATE_MP_BAND_HI = 0.85

# ---- Live executor floor (mirror src/executor.py BotProfile.min_edge_floor_cents) ----
# Selective uses 3¢; aggressive uses 2¢. We'll show both.

# ---- Kalshi fees ----
def fees_cents(price: float, n: int = 1) -> float:
    """Kalshi taker fee: ceil(0.07 * N * p * (1-p)) dollars → cents."""
    return math.ceil(0.07 * n * price * (1.0 - price) * 100) / 1.0


def load(db_path: Path, series_prefix: str = "KXBTCD-") -> pd.DataFrame:
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
    """Match the live engine's other BUY-side gates."""
    return df[
        (df["dist_pct"] >= BUY_GATE_MIN_DIST_PCT)
        & (df["minutes_left"] >= BUY_GATE_MIN_MINUTES)
        & (df["mp"] >= BUY_GATE_MP_BAND_LO)
        & (df["mp"] < BUY_GATE_MP_BAND_HI)
        & df["yes_bid"].notna()
        & df["yes_ask"].notna()
    ].copy()


def simulate(df: pd.DataFrame, *, sigma_min: float, edge_floor_cents: float) -> pd.DataFrame:
    """For each poll where the σ-gate passes, take BUY_YES or BUY_NO if
    net edge ≥ edge_floor_cents. Resolve at settlement."""
    g = df[df["sigma"] >= sigma_min].copy()
    if g.empty:
        return g

    # BUY_YES: pay yes_ask, collect 1 if YES wins (settle > strike), 0 else
    yes_fee = (0.07 * g["yes_ask"] * (1 - g["yes_ask"]) * 100).apply(math.ceil)
    g["yes_net_edge_cents"] = (g["mp"] * 100 - g["yes_ask"] * 100) - yes_fee
    g["yes_pnl_cents"] = (g["yes_outcome"] - g["yes_ask"]) * 100 - yes_fee

    # BUY_NO: pay no_ask (≈ 1 - yes_bid), collect 1 if NO wins
    no_ask = g["no_ask"].fillna(1.0 - g["yes_bid"])
    no_fee = (0.07 * no_ask * (1 - no_ask) * 100).apply(math.ceil)
    g["no_ask"] = no_ask
    g["no_net_edge_cents"] = ((1 - g["mp"]) * 100 - no_ask * 100) - no_fee
    g["no_pnl_cents"] = (g["no_outcome"] - no_ask) * 100 - no_fee

    buys_y = g[g["yes_net_edge_cents"] >= edge_floor_cents].assign(
        side="BUY_YES",
        entry_px=lambda x: x["yes_ask"],
        signal_edge_cents=lambda x: x["yes_net_edge_cents"],
        pnl_cents=lambda x: x["yes_pnl_cents"],
    )
    buys_n = g[g["no_net_edge_cents"] >= edge_floor_cents].assign(
        side="BUY_NO",
        entry_px=lambda x: x["no_ask"],
        signal_edge_cents=lambda x: x["no_net_edge_cents"],
        pnl_cents=lambda x: x["no_pnl_cents"],
    )
    trades = pd.concat([buys_y, buys_n], ignore_index=True)
    if trades.empty:
        return trades

    # Dedupe: one trade per (event, strike, side), earliest signal wins
    trades = trades.sort_values("ts_ms").groupby(
        ["event_ticker", "strike", "side"], as_index=False
    ).first()
    return trades


def fmt_block(label: str, t: pd.DataFrame) -> str:
    if t.empty:
        return f"  {label:<32}  (no trades)"
    n = len(t)
    n_y = (t["side"] == "BUY_YES").sum()
    n_n = (t["side"] == "BUY_NO").sum()
    mean = t["pnl_cents"].mean()
    win = (t["pnl_cents"] > 0).mean() * 100
    total = t["pnl_cents"].sum()
    avg_edge = t["signal_edge_cents"].mean()
    return (
        f"  {label:<32}  trades={n:>4}  yes/no={n_y}/{n_n}  "
        f"mean_pnl={mean:+5.2f}¢  win={win:5.1f}%  "
        f"total={total:+8.0f}¢  avg_edge={avg_edge:4.1f}¢"
    )


def main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH
    series = sys.argv[2] if len(sys.argv) > 2 else "KXBTCD-"
    print(f"loading from {db_path}  series={series}")
    df = load(db_path, series_prefix=series)
    print(f"  loaded {len(df):,} {series}* poll-rows across {df['event_ticker'].nunique()} settled events")
    df_g = apply_non_sigma_gates(df)
    print(f"  after non-σ gates (dist≥0.1%, T≥15m, mp∈[0.05,0.85)): {len(df_g):,} rows")
    print(f"  σ band sizes:  ≥0.50: {(df_g['sigma']>=0.50).sum():,}  "
          f"[0.40,0.50): {((df_g['sigma']>=0.40)&(df_g['sigma']<0.50)).sum():,}  "
          f"<0.40: {(df_g['sigma']<0.40).sum():,}")
    print()

    print("=" * 96)
    print(f"BACKTEST: BUY entries by σ-gate × edge-floor")
    print("=" * 96)
    for floor in (2.0, 3.0):
        print(f"\nedge floor = {floor:.0f}¢ ({'aggressive' if floor==2.0 else 'selective'} profile)")
        for sigma_min in (0.50, 0.40, 0.35, 0.30):
            t = simulate(df_g, sigma_min=sigma_min, edge_floor_cents=floor)
            print(fmt_block(f"σ ≥ {sigma_min:.2f}", t))

    # Isolate the new band: trades that fire ONLY at gate=0.40 but not at gate=0.50
    print()
    print("=" * 96)
    print("INCREMENTAL BAND: trades fired at σ∈[0.40,0.50) (new exposure if gate→0.40)")
    print("=" * 96)
    for floor in (2.0, 3.0):
        all_50 = simulate(df_g, sigma_min=0.50, edge_floor_cents=floor)
        all_40 = simulate(df_g, sigma_min=0.40, edge_floor_cents=floor)
        # Anti-join: trades in all_40 but not all_50
        if all_50.empty:
            inc = all_40
        else:
            key = ["event_ticker", "strike", "side"]
            mask = ~all_40.set_index(key).index.isin(all_50.set_index(key).index)
            inc = all_40[mask]
        label = f"floor={floor:.0f}¢, σ∈[0.40,0.50)"
        print(fmt_block(label, inc))


if __name__ == "__main__":
    main()
