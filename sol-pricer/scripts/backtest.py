"""Backtest the kalshi-pricer model against logged polls + Coinbase settlements.

Run scripts/fetch_settlements.py first to populate the `settlements` table.

Outputs (stdout):
  - Calibration: model_prob deciles vs empirical hit rate
  - Calibration: market mid deciles vs empirical hit rate (baseline)
  - Brier scores: model vs market (lower = better)
  - Per-poll edge → P&L simulation (one trade per poll where flagged)
  - Bucketed P&L (by minutes_left, distance-to-spot, liquidity, side)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "pricer.db"


def load() -> pd.DataFrame:
    conn = sqlite3.connect(str(DB_PATH))
    polls = pd.read_sql_query(
        """SELECT ts_ms, event_ticker, market_ticker, strike, spot, sigma,
                  minutes_left, model_prob, yes_bid, yes_ask, yes_bid_size,
                  yes_ask_size, volume, edge_cents
           FROM polls""",
        conn,
    )
    settle = pd.read_sql_query(
        "SELECT event_ticker, settle_price, close_px FROM settlements", conn
    )
    conn.close()
    df = polls.merge(settle, on="event_ticker", how="inner")
    df["outcome"] = (df["settle_price"] > df["strike"]).astype(int)
    df["mid"] = (df["yes_bid"] + df["yes_ask"]) / 2.0
    df["dist_pct"] = (df["strike"] - df["spot"]) / df["spot"] * 100.0
    df["liq"] = df["yes_bid_size"].fillna(0) + df["yes_ask_size"].fillna(0)
    return df


def reliability_curve(df: pd.DataFrame, prob_col: str, n_bins: int = 10) -> pd.DataFrame:
    valid = df[df[prob_col].notna()].copy()
    valid["bin"] = pd.cut(valid[prob_col], bins=np.linspace(0, 1, n_bins + 1),
                          include_lowest=True, labels=False)
    g = valid.groupby("bin", observed=True).agg(
        n=("outcome", "size"),
        mean_pred=(prob_col, "mean"),
        empirical=("outcome", "mean"),
    ).reset_index()
    return g


def brier(df: pd.DataFrame, prob_col: str) -> float:
    v = df[df[prob_col].notna()]
    return float(((v[prob_col] - v["outcome"]) ** 2).mean())


def log_loss(df: pd.DataFrame, prob_col: str, eps: float = 1e-6) -> float:
    v = df[df[prob_col].notna()].copy()
    p = v[prob_col].clip(eps, 1 - eps)
    return float(-(v["outcome"] * np.log(p) + (1 - v["outcome"]) * np.log(1 - p)).mean())


def simulate_trades(df: pd.DataFrame, threshold_cents: float = 5.0) -> pd.DataFrame:
    """For every poll, if |lift-the-market edge| > threshold, simulate the trade
    at the quoted ask/bid. Resolve at settlement.

    Returns one row per simulated trade.
        BUY_YES: pay ask, get 1 if outcome=YES else 0  → pnl = outcome - ask
        SELL_YES: receive bid, pay 1 if outcome=YES else 0 → pnl = bid - outcome
    """
    valid = df[df["yes_bid"].notna() & df["yes_ask"].notna()].copy()
    buy_edge_cents = valid["model_prob"] * 100 - valid["yes_ask"] * 100
    sell_edge_cents = valid["yes_bid"] * 100 - valid["model_prob"] * 100

    buys = valid[buy_edge_cents > threshold_cents].copy()
    buys["side"] = "BUY_YES"
    buys["edge_at_signal"] = buy_edge_cents[buy_edge_cents > threshold_cents]
    buys["pnl"] = buys["outcome"] - buys["yes_ask"]
    buys["entry_px"] = buys["yes_ask"]

    sells = valid[sell_edge_cents > threshold_cents].copy()
    sells["side"] = "SELL_YES"
    sells["edge_at_signal"] = sell_edge_cents[sell_edge_cents > threshold_cents]
    sells["pnl"] = sells["yes_bid"] - sells["outcome"]
    sells["entry_px"] = sells["yes_bid"]

    trades = pd.concat([buys, sells], ignore_index=True)
    return trades


def first_signal_per_strike(trades: pd.DataFrame) -> pd.DataFrame:
    """Dedupe to one trade per (event, strike, side): take the earliest signal."""
    return trades.sort_values("ts_ms").groupby(
        ["event_ticker", "strike", "side"], as_index=False
    ).first()


def bucketize(s: pd.Series, edges: list[float], labels: list[str]) -> pd.Series:
    return pd.cut(s, bins=edges, labels=labels, include_lowest=True)


def fmt_pct(x: float) -> str:
    return f"{x*100:5.1f}%" if pd.notna(x) else "  n/a"


def fmt_signed(x: float, w: int = 7) -> str:
    if pd.isna(x):
        return f"{'  n/a':>{w}}"
    return f"{x:+{w}.4f}"


def main() -> None:
    df = load()
    print(f"loaded {len(df):,} (poll, strike) rows across {df['event_ticker'].nunique()} settled events")
    print(f"  spot range: ${df['spot'].min():,.0f} – ${df['spot'].max():,.0f}")
    print(f"  settle range: ${df['settle_price'].min():,.0f} – ${df['settle_price'].max():,.0f}")
    print(f"  base rate (overall YES outcome): {df['outcome'].mean()*100:.1f}%")
    print()

    print("=" * 72)
    print("CALIBRATION  (all polls, all strikes, all minutes_left)")
    print("=" * 72)
    print(f"{'bin':<10}{'n':>9}{'mean_pred':>12}{'empirical':>12}  diff")
    print("-" * 72)
    rc_model = reliability_curve(df, "model_prob")
    for _, r in rc_model.iterrows():
        diff = r["empirical"] - r["mean_pred"]
        print(f"  model {int(r['bin']):>2}  {int(r['n']):>9,}  {r['mean_pred']*100:9.1f}%  {r['empirical']*100:9.1f}%  {diff*100:+5.1f}pp")
    print()
    rc_mid = reliability_curve(df.dropna(subset=["mid"]), "mid")
    for _, r in rc_mid.iterrows():
        diff = r["empirical"] - r["mean_pred"]
        print(f"  mid   {int(r['bin']):>2}  {int(r['n']):>9,}  {r['mean_pred']*100:9.1f}%  {r['empirical']*100:9.1f}%  {diff*100:+5.1f}pp")
    print()

    print("=" * 72)
    print("SCORES  (lower = better)")
    print("=" * 72)
    valid_both = df.dropna(subset=["mid"])
    print(f"  Brier   model = {brier(valid_both, 'model_prob'):.5f}")
    print(f"  Brier   mid   = {brier(valid_both, 'mid'):.5f}")
    print(f"  LogLoss model = {log_loss(valid_both, 'model_prob'):.5f}")
    print(f"  LogLoss mid   = {log_loss(valid_both, 'mid'):.5f}")
    print()

    # Sliced calibration: how does the model do *near the money*, *in the sweet spot window*?
    print("=" * 72)
    print("CALIBRATION SLICES  (model vs market, by regime)")
    print("=" * 72)
    slices = {
        "near-money (|d|<0.5%) + T 3-15m": df[(df["dist_pct"].abs() < 0.5) & (df["minutes_left"].between(3, 15))],
        "near-money (|d|<0.5%) + T 15-60m": df[(df["dist_pct"].abs() < 0.5) & (df["minutes_left"] > 15)],
        "near-money (|d|<0.5%) + T <3m":   df[(df["dist_pct"].abs() < 0.5) & (df["minutes_left"] < 3)],
        "tails (|d|>=2%)":                  df[df["dist_pct"].abs() >= 2.0],
    }
    print(f"{'slice':<38}{'n':>9}{'Brier-model':>14}{'Brier-mid':>14}")
    for name, sub in slices.items():
        if len(sub) == 0:
            continue
        sub_v = sub.dropna(subset=["mid"])
        if len(sub_v) == 0:
            continue
        bm = brier(sub_v, "model_prob")
        bx = brier(sub_v, "mid")
        flag = "  <- model better" if bm < bx else "  <- market better"
        print(f"  {name:<36}{len(sub_v):>9,}  {bm:>10.5f}    {bx:>10.5f}{flag}")
    print()

    # ---- Trade simulation ----
    for thresh in (3.0, 5.0, 10.0):
        trades = simulate_trades(df, threshold_cents=thresh)
        if len(trades) == 0:
            continue
        # All-signals view (each poll counted): proxies "if I traded EVERY signal independently"
        # First-signal view: proxies "one position per strike, entered on the first flag"
        first = first_signal_per_strike(trades)

        print("=" * 72)
        print(f"TRADE SIMULATION  (threshold = {thresh:.0f}¢)")
        print("=" * 72)
        for label, t in (("ALL-SIGNALS", trades), ("FIRST-PER-STRIKE", first)):
            n = len(t)
            n_buy = (t["side"] == "BUY_YES").sum()
            n_sell = (t["side"] == "SELL_YES").sum()
            mean_pnl = t["pnl"].mean()
            win = (t["pnl"] > 0).mean()
            mean_edge = t["edge_at_signal"].mean()
            print(f"  {label}: trades={n:,}  buys={n_buy:,} sells={n_sell:,}  "
                  f"mean P&L = {mean_pnl*100:+.2f}¢/contract  win={win*100:.1f}%  "
                  f"avg signaled edge = {mean_edge:.1f}¢")
            if n == 0:
                continue
            t = t.copy()
            t["T_bin"] = bucketize(t["minutes_left"],
                                    [-0.001, 3, 15, 30, 9999],
                                    ["<3m", "3-15m", "15-30m", ">30m"])
            t["d_bin"] = bucketize(t["dist_pct"].abs(),
                                    [-0.001, 0.25, 0.5, 1.0, 2.0, 9999],
                                    ["≤0.25%", "0.25-0.5%", "0.5-1%", "1-2%", ">=2%"])
            t["liq_bin"] = bucketize(t["liq"].fillna(0),
                                      [-1, 50, 200, 1000, 1e9],
                                      ["<50", "50-200", "200-1k", ">=1k"])
            print(f"    by minutes_left:")
            for bin_, sub in t.groupby("T_bin", observed=True):
                if len(sub) == 0:
                    continue
                print(f"      T={bin_:<8} n={len(sub):>5,}  mean P&L {sub['pnl'].mean()*100:+6.2f}¢  "
                      f"win {(sub['pnl']>0).mean()*100:5.1f}%  edge_signal {sub['edge_at_signal'].mean():4.1f}¢")
            print(f"    by |strike-spot|/spot:")
            for bin_, sub in t.groupby("d_bin", observed=True):
                if len(sub) == 0:
                    continue
                print(f"      d={str(bin_):<10} n={len(sub):>5,}  mean P&L {sub['pnl'].mean()*100:+6.2f}¢  "
                      f"win {(sub['pnl']>0).mean()*100:5.1f}%  edge_signal {sub['edge_at_signal'].mean():4.1f}¢")
            print(f"    by liquidity (top-of-book bid+ask size):")
            for bin_, sub in t.groupby("liq_bin", observed=True):
                if len(sub) == 0:
                    continue
                print(f"      liq={str(bin_):<8} n={len(sub):>5,}  mean P&L {sub['pnl'].mean()*100:+6.2f}¢  "
                      f"win {(sub['pnl']>0).mean()*100:5.1f}%  edge_signal {sub['edge_at_signal'].mean():4.1f}¢")
            print(f"    by side:")
            for bin_, sub in t.groupby("side", observed=True):
                print(f"      {bin_}: n={len(sub):>5,}  mean P&L {sub['pnl'].mean()*100:+6.2f}¢  "
                      f"win {(sub['pnl']>0).mean()*100:5.1f}%  edge_signal {sub['edge_at_signal'].mean():4.1f}¢")
            print()
        print()

    # ---- Best-of-both: filter the noise stuff per the GUIDE ----
    print("=" * 72)
    print("DISCIPLINED RULESET  (T 3-15m + |d|<1% + liq>=100 + edge>=5¢)")
    print("=" * 72)
    disc = df[(df["minutes_left"].between(3, 15)) &
              (df["dist_pct"].abs() < 1.0) &
              (df["liq"] >= 100)]
    trades = simulate_trades(disc, threshold_cents=5.0)
    if len(trades) > 0:
        first = first_signal_per_strike(trades)
        print(f"  ALL-SIGNALS: trades={len(trades):,}  mean P&L {trades['pnl'].mean()*100:+.2f}¢  "
              f"win {(trades['pnl']>0).mean()*100:.1f}%  avg edge {trades['edge_at_signal'].mean():.1f}¢")
        print(f"  FIRST-PER-STRIKE: trades={len(first):,}  mean P&L {first['pnl'].mean()*100:+.2f}¢  "
              f"win {(first['pnl']>0).mean()*100:.1f}%  avg edge {first['edge_at_signal'].mean():.1f}¢")
        for bin_, sub in trades.groupby("side", observed=True):
            print(f"    {bin_}: n={len(sub):>5,}  mean P&L {sub['pnl'].mean()*100:+6.2f}¢  "
                  f"win {(sub['pnl']>0).mean()*100:5.1f}%")
    else:
        print("  no trades after filters")


if __name__ == "__main__":
    main()
