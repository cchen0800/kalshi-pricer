"""Diagnostic: why does the σ≥0.50 cell look net-negative in the bucketed
backtest, when engine.py:181 cites it as the *winning* regime from the
first 33 live trades?

Hypotheses to discriminate:
  H1 — One event is dragging σ≥0.50 down (small-N concentration risk).
  H2 — BUY_YES is the culprit; BUY_NO is fine. Live trader was BUY_YES-only,
       backtest now includes BUY_NO so the cell mixes apples/oranges.
  H3 — Calibrator changed which polls trigger; raw model_prob would still
       show σ≥0.50 as positive.
  H4 — Date drift: April 27 (21 events, 442k polls) vs April 28 (6 events,
       87k polls) regimes differ.

For each hypothesis, print a table that either confirms or rules it out.
"""
from __future__ import annotations

import math
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "pricer.db"

BUY_GATE_MIN_DIST_PCT = 0.0010
BUY_GATE_MIN_MINUTES = 15.0
BUY_GATE_MP_BAND_LO = 0.05
BUY_GATE_MP_BAND_HI = 0.85


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
        "SELECT event_ticker, settle_price, close_utc_s FROM settlements", conn
    )
    conn.close()
    df = polls.merge(settle, on="event_ticker", how="inner")
    df["yes_outcome"] = (df["settle_price"] > df["strike"]).astype(int)
    df["no_outcome"] = 1 - df["yes_outcome"]
    df["dist_pct"] = (df["strike"] - df["spot"]).abs() / df["spot"]
    df["mp_cal"] = df["model_prob_calibrated"].fillna(df["model_prob"])
    df["close_date"] = pd.to_datetime(df["close_utc_s"], unit="s").dt.date
    return df


def gate(df: pd.DataFrame, mp_col: str) -> pd.DataFrame:
    return df[
        (df["dist_pct"] >= BUY_GATE_MIN_DIST_PCT)
        & (df["minutes_left"] >= BUY_GATE_MIN_MINUTES)
        & (df[mp_col] >= BUY_GATE_MP_BAND_LO)
        & (df[mp_col] < BUY_GATE_MP_BAND_HI)
        & df["yes_bid"].notna()
        & df["yes_ask"].notna()
    ].copy()


def annotate(g: pd.DataFrame, mp_col: str) -> pd.DataFrame:
    g = g.copy()
    yes_fee = (0.07 * g["yes_ask"] * (1 - g["yes_ask"]) * 100).apply(math.ceil)
    g["yes_net_edge_cents"] = (g[mp_col] * 100 - g["yes_ask"] * 100) - yes_fee
    g["yes_pnl_cents"] = (g["yes_outcome"] - g["yes_ask"]) * 100 - yes_fee
    no_ask = g["no_ask"].fillna(1.0 - g["yes_bid"])
    no_fee = (0.07 * no_ask * (1 - no_ask) * 100).apply(math.ceil)
    g["no_ask_eff"] = no_ask
    g["no_net_edge_cents"] = ((1 - g[mp_col]) * 100 - no_ask * 100) - no_fee
    g["no_pnl_cents"] = (g["no_outcome"] - no_ask) * 100 - no_fee
    return g


def trades(g: pd.DataFrame, edge_floor: float) -> pd.DataFrame:
    if g.empty:
        return g.iloc[0:0]
    yes = g[g["yes_net_edge_cents"] >= edge_floor].assign(
        side="BUY_YES",
        signal_edge_cents=lambda x: x["yes_net_edge_cents"],
        pnl_cents=lambda x: x["yes_pnl_cents"],
    )
    no = g[g["no_net_edge_cents"] >= edge_floor].assign(
        side="BUY_NO",
        signal_edge_cents=lambda x: x["no_net_edge_cents"],
        pnl_cents=lambda x: x["no_pnl_cents"],
    )
    out = pd.concat([yes, no], ignore_index=True)
    if out.empty:
        return out
    return out.sort_values("ts_ms").groupby(
        ["event_ticker", "strike", "side"], as_index=False
    ).first()


def summary(t: pd.DataFrame) -> str:
    if t.empty:
        return "(no trades)"
    n = len(t)
    n_y = (t["side"] == "BUY_YES").sum()
    n_n = (t["side"] == "BUY_NO").sum()
    return (f"n={n:>3}  Y/N={n_y:>2}/{n_n:<2}  "
            f"mean={t['pnl_cents'].mean():+6.2f}¢  "
            f"win={(t['pnl_cents']>0).mean()*100:5.1f}%  "
            f"total={t['pnl_cents'].sum():+7.0f}¢")


def main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH
    series = sys.argv[2] if len(sys.argv) > 2 else "KXBTCD-"
    df = load(db_path, series)
    print(f"loaded {len(df):,} poll-rows across {df['event_ticker'].nunique()} events "
          f"({df['close_date'].min()} → {df['close_date'].max()})")

    floor = 3.0

    # All hypotheses focus on σ≥0.50 cell with calibrated mp + non-σ gates ON
    g = annotate(gate(df, "mp_cal"), "mp_cal")
    g50 = g[g["sigma"] >= 0.50]
    t50 = trades(g50, floor)
    print(f"\nBASELINE  σ≥0.50, calibrated mp, floor=3¢:  {summary(t50)}")
    print(f"  (this is the cell that read -$2.46 in the bucketed backtest)")

    # ---------- H1: one bad event ----------
    print("\n" + "=" * 78)
    print("H1: per-event P&L decomposition (σ≥0.50)")
    print("=" * 78)
    if t50.empty:
        print("  no trades, skipping")
    else:
        per_evt = (t50.groupby("event_ticker")
                       .agg(n=("pnl_cents", "size"),
                            total=("pnl_cents", "sum"),
                            mean=("pnl_cents", "mean"),
                            win=("pnl_cents", lambda s: (s > 0).mean() * 100))
                       .sort_values("total"))
        print(per_evt.to_string(float_format=lambda x: f"{x:+.1f}"))
        # Drop worst event, recompute
        worst = per_evt.index[0]
        without_worst = t50[t50["event_ticker"] != worst]
        print(f"\n  drop worst ({worst}): {summary(without_worst)}")
        # Drop top-3 worst
        worst3 = per_evt.head(3).index.tolist()
        without_worst3 = t50[~t50["event_ticker"].isin(worst3)]
        print(f"  drop worst 3 ({', '.join(worst3)}): {summary(without_worst3)}")

    # ---------- H2: side breakdown ----------
    print("\n" + "=" * 78)
    print("H2: BUY_YES vs BUY_NO inside σ≥0.50")
    print("=" * 78)
    if t50.empty:
        print("  no trades")
    else:
        for s in ("BUY_YES", "BUY_NO"):
            print(f"  {s:<8}  {summary(t50[t50['side']==s])}")
        print("  → live trader (May 2-4) was BUY_YES-only. If BUY_YES is the bad")
        print("    side here too, the σ≥0.50 cell never had alpha — the live")
        print("    33-trade sample just got lucky.")

    # ---------- H3: calibrator effect ----------
    print("\n" + "=" * 78)
    print("H3: calibrated vs raw model_prob, both at σ≥0.50")
    print("=" * 78)
    g_raw = annotate(gate(df, "model_prob"), "model_prob")
    g_raw50 = g_raw[g_raw["sigma"] >= 0.50]
    t_raw = trades(g_raw50, floor)
    print(f"  calibrated:  {summary(t50)}")
    print(f"  raw:         {summary(t_raw)}")
    # Side split for raw
    if not t_raw.empty:
        for s in ("BUY_YES", "BUY_NO"):
            print(f"    raw {s:<8}  {summary(t_raw[t_raw['side']==s])}")

    # ---------- H4: date drift ----------
    print("\n" + "=" * 78)
    print("H4: per-date breakdown (σ≥0.50, calibrated, floor=3¢)")
    print("=" * 78)
    if t50.empty:
        print("  no trades")
    else:
        # Map event → close_date (events have a single close_date)
        evt_to_date = (df[["event_ticker", "close_date"]]
                       .drop_duplicates()
                       .set_index("event_ticker")["close_date"])
        t50 = t50.assign(close_date=t50["event_ticker"].map(evt_to_date))
        for d, grp in t50.groupby("close_date"):
            n_evts = grp["event_ticker"].nunique()
            print(f"  {d}  ({n_evts} events)  {summary(grp)}")

    # ---------- bonus: contrast with σ<0.50 BUY_NO winning cell ----------
    print("\n" + "=" * 78)
    print("CONTRAST: σ<0.50 BUY_NO (the cell the bucketed backtest flagged as +EV)")
    print("=" * 78)
    g_lo = g[g["sigma"] < 0.50]
    t_lo_no = trades(g_lo, floor)
    t_lo_no = t_lo_no[t_lo_no["side"] == "BUY_NO"]
    print(f"  {summary(t_lo_no)}")
    if not t_lo_no.empty:
        per_evt = (t_lo_no.groupby("event_ticker")
                          .agg(n=("pnl_cents", "size"),
                               total=("pnl_cents", "sum"))
                          .sort_values("total"))
        print(f"  per-event P&L (sorted):")
        print(per_evt.to_string(float_format=lambda x: f"{x:+.1f}"))


if __name__ == "__main__":
    main()
