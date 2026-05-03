"""Diagnostic follow-ups on the headline backtest:
  - Per-event P&L distribution (concentration vs persistence)
  - Why BUY_YES near-money loses: drift bias of zero-drift model
  - Re-do calibration excluding deep tails (the bins that dominate raw counts)
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
    polls = pd.read_sql_query("SELECT * FROM polls", conn)
    settle = pd.read_sql_query("SELECT event_ticker, settle_price FROM settlements", conn)
    conn.close()
    df = polls.merge(settle, on="event_ticker", how="inner")
    df["outcome"] = (df["settle_price"] > df["strike"]).astype(int)
    df["mid"] = (df["yes_bid"] + df["yes_ask"]) / 2.0
    df["dist_pct"] = (df["strike"] - df["spot"]) / df["spot"] * 100.0
    df["liq"] = df["yes_bid_size"].fillna(0) + df["yes_ask_size"].fillna(0)
    return df


def simulate(df: pd.DataFrame, threshold: float = 5.0) -> pd.DataFrame:
    valid = df[df["yes_bid"].notna() & df["yes_ask"].notna()].copy()
    buy_e = valid["model_prob"] * 100 - valid["yes_ask"] * 100
    sell_e = valid["yes_bid"] * 100 - valid["model_prob"] * 100
    buys = valid[buy_e > threshold].copy()
    buys["side"] = "BUY_YES"; buys["edge"] = buy_e[buy_e > threshold]
    buys["pnl"] = buys["outcome"] - buys["yes_ask"]
    sells = valid[sell_e > threshold].copy()
    sells["side"] = "SELL_YES"; sells["edge"] = sell_e[sell_e > threshold]
    sells["pnl"] = sells["yes_bid"] - sells["outcome"]
    return pd.concat([buys, sells], ignore_index=True)


def main() -> None:
    df = load()
    print(f"loaded {len(df):,} rows, {df['event_ticker'].nunique()} events")
    print()

    # 1) PER-EVENT P&L for the disciplined ruleset
    print("=" * 78)
    print("PER-EVENT P&L  (disciplined: T 3-15m, |d|<1%, liq>=100, edge>=5¢)")
    print("=" * 78)
    disc = df[(df["minutes_left"].between(3, 15)) &
              (df["dist_pct"].abs() < 1.0) &
              (df["liq"] >= 100)]
    trades = simulate(disc, threshold=5.0)
    if len(trades) > 0:
        # Use first-per-strike (one position per strike per event)
        trades = trades.sort_values("ts_ms").groupby(
            ["event_ticker", "strike", "side"], as_index=False).first()
        per_event = trades.groupby("event_ticker").agg(
            n=("pnl", "size"),
            mean_pnl_cents=("pnl", lambda s: s.mean() * 100),
            total_pnl=("pnl", "sum"),
            buys=("side", lambda s: (s == "BUY_YES").sum()),
            sells=("side", lambda s: (s == "SELL_YES").sum()),
        ).sort_values("total_pnl")
        print(f"{'event':<22}{'n':>4}{'buys':>6}{'sells':>6}{'mean¢':>8}{'total$':>10}")
        for et, r in per_event.iterrows():
            print(f"  {et:<20}{int(r['n']):>4}{int(r['buys']):>6}{int(r['sells']):>6}"
                  f"{r['mean_pnl_cents']:+8.2f}{r['total_pnl']:+10.3f}")
        print(f"  {'TOTAL':<20}{int(per_event['n'].sum()):>4}"
              f"{int(per_event['buys'].sum()):>6}{int(per_event['sells'].sum()):>6}"
              f"{trades['pnl'].mean()*100:+8.2f}{trades['pnl'].sum():+10.3f}")
        winners = (per_event['total_pnl'] > 0).sum()
        losers = (per_event['total_pnl'] < 0).sum()
        print(f"\n  events profitable: {winners}/{len(per_event)}  "
              f"({winners/len(per_event)*100:.0f}%)  losers: {losers}")
    print()

    # 2) Drift bias: did BTC trend during our sample?
    print("=" * 78)
    print("DRIFT BIAS  (zero-drift assumption vs realized direction)")
    print("=" * 78)
    # For each event, compute spot-at-first-poll vs settlement.
    first = df.sort_values("ts_ms").groupby("event_ticker").first()
    first["realized_drift_pct"] = (first["settle_price"] - first["spot"]) / first["spot"] * 100
    print(f"  realized hourly returns (open-poll spot → settle):")
    print(f"    mean = {first['realized_drift_pct'].mean():+.3f}%")
    print(f"    median = {first['realized_drift_pct'].median():+.3f}%")
    print(f"    sd = {first['realized_drift_pct'].std():.3f}%")
    print(f"    down hours: {(first['realized_drift_pct']<0).sum()}/{len(first)}")
    print(f"    up   hours: {(first['realized_drift_pct']>0).sum()}/{len(first)}")
    print()
    # For ATM strikes (the 50/50 region), model says 50%. What did empirics say?
    atm = df[df["dist_pct"].abs() < 0.1]
    print(f"  ATM (|d|<0.1%) polls: {len(atm):,}")
    print(f"    avg model_prob = {atm['model_prob'].mean()*100:.1f}%")
    print(f"    avg market mid = {atm['mid'].mean()*100:.1f}%")
    print(f"    empirical YES rate = {atm['outcome'].mean()*100:.1f}%")
    print(f"  → If empirical << 50%, the ATM book leaned bearish & was right; zero-drift was wrong.")
    print()

    # 3) Calibration WITHOUT the deep-tail bins (where everyone trivially agrees)
    print("=" * 78)
    print("CALIBRATION  (excluding deep tails; |d|<2%)")
    print("=" * 78)
    inner = df[df["dist_pct"].abs() < 2.0].dropna(subset=["mid"])
    print(f"  n = {len(inner):,}  base rate = {inner['outcome'].mean()*100:.1f}%")
    for col in ("model_prob", "mid"):
        b = inner.copy()
        b["bin"] = pd.cut(b[col], bins=np.linspace(0, 1, 11), include_lowest=True, labels=False)
        g = b.groupby("bin", observed=True).agg(n=("outcome", "size"),
                                                  pred=(col, "mean"),
                                                  emp=("outcome", "mean")).reset_index()
        print(f"\n  {col}:")
        print(f"  {'bin':>3}{'n':>9}{'pred':>9}{'emp':>9}{'diff':>9}")
        for _, r in g.iterrows():
            print(f"  {int(r['bin']):>3}{int(r['n']):>9,}{r['pred']*100:>8.1f}%{r['emp']*100:>8.1f}%"
                  f"{(r['emp']-r['pred'])*100:>+8.1f}pp")
        # Brier on inner band only
        bs = ((b[col] - b['outcome'])**2).mean()
        print(f"  Brier {col} (inner) = {bs:.5f}")
    print()

    # 4) BUY vs SELL signal directionality. If the model is biased upward,
    # BUY signals (model > ask) should systematically be wrong.
    print("=" * 78)
    print("SIGNAL DIRECTIONALITY  (inner band, |d|<1%, T 3-15m)")
    print("=" * 78)
    sub = df[(df["minutes_left"].between(3, 15)) & (df["dist_pct"].abs() < 1.0)]
    sub = sub.dropna(subset=["mid"])
    sub = sub.copy()
    sub["model_minus_mid"] = sub["model_prob"] - sub["mid"]  # positive = model thinks YES underpriced
    bins = pd.cut(sub["model_minus_mid"]*100, bins=[-100, -10, -5, -2, 2, 5, 10, 100],
                  labels=["m-mid<-10", "-10..-5", "-5..-2", "-2..+2", "+2..+5", "+5..+10", ">+10"])
    g = sub.groupby(bins, observed=True).agg(n=("outcome", "size"),
                                              avg_model=("model_prob", "mean"),
                                              avg_mid=("mid", "mean"),
                                              empirical=("outcome", "mean")).reset_index()
    print(f"  Across the (model − mid) spread, who's right empirically?")
    print(f"  {'bucket':<14}{'n':>8}{'avg_model':>11}{'avg_mid':>10}{'empirical':>11}{'mid_err':>9}{'mod_err':>9}")
    for _, r in g.iterrows():
        mid_err = abs(r["avg_mid"] - r["empirical"])
        mod_err = abs(r["avg_model"] - r["empirical"])
        winner = "MID" if mid_err < mod_err else "MOD"
        print(f"  {str(r['model_minus_mid']):<14}{int(r['n']):>8,}"
              f"{r['avg_model']*100:>10.1f}%{r['avg_mid']*100:>9.1f}%{r['empirical']*100:>10.1f}%"
              f"{mid_err*100:>8.1f}{mod_err*100:>8.1f}  {winner}")


if __name__ == "__main__":
    main()
