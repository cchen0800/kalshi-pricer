"""Confirm BUY vs SELL asymmetry — is it consistent across events?"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
conn = sqlite3.connect(str(ROOT / "pricer.db"))
df = pd.read_sql_query("SELECT * FROM polls", conn).merge(
    pd.read_sql_query("SELECT event_ticker, settle_price FROM settlements", conn),
    on="event_ticker", how="inner")
conn.close()
df["outcome"] = (df["settle_price"] > df["strike"]).astype(int)
df["mid"] = (df["yes_bid"] + df["yes_ask"]) / 2.0
df["dist_pct"] = (df["strike"] - df["spot"]) / df["spot"] * 100.0
df["liq"] = df["yes_bid_size"].fillna(0) + df["yes_ask_size"].fillna(0)

disc = df[(df["minutes_left"].between(3, 15)) &
          (df["dist_pct"].abs() < 1.0) &
          (df["liq"] >= 100) &
          df["yes_bid"].notna() & df["yes_ask"].notna()]

buy_e = disc["model_prob"] * 100 - disc["yes_ask"] * 100
sell_e = disc["yes_bid"] * 100 - disc["model_prob"] * 100

buys = disc[buy_e > 5].copy()
buys["pnl"] = buys["outcome"] - buys["yes_ask"]; buys["side"] = "BUY"
sells = disc[sell_e > 5].copy()
sells["pnl"] = sells["yes_bid"] - sells["outcome"]; sells["side"] = "SELL"

# First-per-strike dedupe
def dedup(t):
    return t.sort_values("ts_ms").groupby(["event_ticker", "strike"], as_index=False).first()
buys = dedup(buys); sells = dedup(sells)

print("BUY_YES disciplined (T 3-15m, |d|<1%, liq>=100, edge>=5¢)")
print(f"  trades n={len(buys)}  mean P&L {buys['pnl'].mean()*100:+.2f}¢  win={(buys['pnl']>0).mean()*100:.1f}%")
e_buy = buys.groupby("event_ticker").agg(n=("pnl", "size"), m=("pnl", "mean"))
print(f"  event distribution: {(e_buy['m']>0).sum()}/{len(e_buy)} events profitable, {(e_buy['m']<0).sum()} losers")
print(f"  per-event mean¢: min={e_buy['m'].min()*100:+.1f}, median={e_buy['m'].median()*100:+.1f}, max={e_buy['m'].max()*100:+.1f}")
print()
print("SELL_YES disciplined")
print(f"  trades n={len(sells)}  mean P&L {sells['pnl'].mean()*100:+.2f}¢  win={(sells['pnl']>0).mean()*100:.1f}%")
e_sell = sells.groupby("event_ticker").agg(n=("pnl", "size"), m=("pnl", "mean"))
print(f"  event distribution: {(e_sell['m']>0).sum()}/{len(e_sell)} events profitable, {(e_sell['m']<0).sum()} losers")
print(f"  per-event mean¢: min={e_sell['m'].min()*100:+.1f}, median={e_sell['m'].median()*100:+.1f}, max={e_sell['m'].max()*100:+.1f}")
print()

# What about the wider window (more relaxed) to get more data?
print("=" * 60)
print("WIDER (T 3-30m, |d|<2%, liq>=100, edge>=5¢)")
print("=" * 60)
disc2 = df[(df["minutes_left"].between(3, 30)) &
           (df["dist_pct"].abs() < 2.0) &
           (df["liq"] >= 100) &
           df["yes_bid"].notna() & df["yes_ask"].notna()]
buy_e2 = disc2["model_prob"]*100 - disc2["yes_ask"]*100
sell_e2 = disc2["yes_bid"]*100 - disc2["model_prob"]*100
b2 = dedup(disc2[buy_e2 > 5].assign(pnl=lambda x: x["outcome"]-x["yes_ask"], side="BUY"))
s2 = dedup(disc2[sell_e2 > 5].assign(pnl=lambda x: x["yes_bid"]-x["outcome"], side="SELL"))
print(f"BUY  n={len(b2)} mean {b2['pnl'].mean()*100:+.2f}¢ win {(b2['pnl']>0).mean()*100:.1f}%")
print(f"SELL n={len(s2)} mean {s2['pnl'].mean()*100:+.2f}¢ win {(s2['pnl']>0).mean()*100:.1f}%")

# Statistical sanity: SE of the mean ¢ for SELL
import math
se_sell = s2['pnl'].std() / math.sqrt(len(s2)) * 100
se_buy = b2['pnl'].std() / math.sqrt(len(b2)) * 100 if len(b2) else float('nan')
print(f"\n  SELL  mean = {s2['pnl'].mean()*100:+.2f}¢  ±{se_sell:.2f}¢ (1 SE)")
print(f"  BUY   mean = {b2['pnl'].mean()*100:+.2f}¢  ±{se_buy:.2f}¢ (1 SE)")
print(f"  SELL t-stat (vs 0) = {s2['pnl'].mean()*100/se_sell:.2f}")
print(f"  BUY  t-stat (vs 0) = {b2['pnl'].mean()*100/se_buy:.2f}")
