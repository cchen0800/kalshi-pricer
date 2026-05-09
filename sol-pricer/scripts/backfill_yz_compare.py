"""Re-fetch historical 1m OHLC, compute σ_yz vs σ_cc rolling, and compare what
the trading system would have done under each on the existing poll log.

Coinbase /candles is paged at 300 candles per response. We fetch the full range
in chunks, cache the result to research/ohlc_backfill.json, then compute rolling
σ at every minute boundary in the period.

Both regimes use the new path-dependent pricer + fee-adjusted edge so the only
thing that varies is the σ input.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.pricer import prob_above_strike_path_dependent
from src.vol import annualized_vol, yang_zhang_vol
CACHE = ROOT / "research" / "ohlc_backfill.json"
COINBASE = "https://api.exchange.coinbase.com"
CHUNK_MIN = 300  # Coinbase per-request candle cap
VOL_WINDOW_MIN = 60
MIN_MINUTES_TO_CLOSE = 2.0  # mirrors src/executor.py post-change
MIN_EDGE_CENTS = 8.0        # mirrors src/executor.py post-change
W_SECONDS = 60.0            # BRTI averaging window


# ---- fetch + cache --------------------------------------------------------

def fetch_chunk(start_unix: int, end_unix: int) -> list[tuple[int, float, float, float, float]]:
    """Returns [(epoch_s, open, high, low, close), ...] ascending."""
    params = {
        "granularity": 60,
        "start": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start_unix)),
        "end": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(end_unix)),
    }
    for attempt in range(5):
        r = httpx.get(f"{COINBASE}/products/SOL-USD/candles", params=params, timeout=10)
        if r.status_code == 429 or 500 <= r.status_code < 600:
            time.sleep(0.5 * (2 ** attempt))
            continue
        r.raise_for_status()
        rows = r.json()
        # Coinbase order: [time, low, high, open, close, volume], newest-first.
        out = [(int(t), float(o), float(h), float(l), float(c)) for t, l, h, o, c, _ in rows]
        out.sort()
        return out
    raise RuntimeError("retry budget exhausted")


def fetch_all(start_unix: int, end_unix: int) -> list[tuple[int, float, float, float, float]]:
    chunks: list[tuple[int, float, float, float, float]] = []
    cur = start_unix
    chunk_s = CHUNK_MIN * 60
    while cur < end_unix:
        nxt = min(cur + chunk_s, end_unix)
        ch = fetch_chunk(cur, nxt)
        chunks.extend(ch)
        cur = nxt
        time.sleep(0.2)
    # Dedupe by epoch_s (chunk boundaries can overlap by 1).
    seen: set[int] = set()
    out: list[tuple[int, float, float, float, float]] = []
    for row in chunks:
        if row[0] in seen:
            continue
        seen.add(row[0])
        out.append(row)
    out.sort()
    return out


def get_or_fetch(start_unix: int, end_unix: int):
    if CACHE.exists():
        d = json.loads(CACHE.read_text())
        if d.get("start") == start_unix and d.get("end") == end_unix:
            print(f"using cached OHLC: {len(d['rows'])} candles")
            return [tuple(r) for r in d["rows"]]
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    print(f"fetching {(end_unix - start_unix) // 60} minutes from Coinbase…")
    rows = fetch_all(start_unix, end_unix)
    CACHE.write_text(json.dumps({"start": start_unix, "end": end_unix, "rows": rows}))
    print(f"cached {len(rows)} candles to {CACHE.relative_to(ROOT)}")
    return rows


# ---- rolling σ ------------------------------------------------------------

def rolling_sigmas(
    rows: list[tuple[int, float, float, float, float]],
    window_min: int,
) -> pd.DataFrame:
    """For each candle bucket m, compute σ_yz and σ_cc using the prior `window_min`
    candles (m's window is [m-window_min, m-1] inclusive). Returns DataFrame
    indexed by epoch_s of the latest completed candle."""
    df = pd.DataFrame(rows, columns=["epoch_s", "o", "h", "l", "c"])
    df = df.sort_values("epoch_s").reset_index(drop=True)
    yz, cc, ts = [], [], []
    n = len(df)
    for i in range(window_min, n):
        win = df.iloc[i - window_min:i]  # 60 candles
        ohlc_t = list(zip(win["o"], win["h"], win["l"], win["c"]))
        try:
            yz.append(yang_zhang_vol(ohlc_t))
            cc.append(annualized_vol(list(win["c"])))
            ts.append(int(df.iloc[i]["epoch_s"]))
        except ValueError:
            continue
    return pd.DataFrame({"epoch_s": ts, "sigma_yz": yz, "sigma_cc": cc})


# ---- backtest --------------------------------------------------------------

def fee_cents(pc: int) -> int:
    p = max(1, min(99, pc)) / 100.0
    return math.ceil(0.07 * p * (1.0 - p) * 100.0)


def model_prob(spot: float, strike: float, sigma: float, seconds_left: float) -> float:
    return prob_above_strike_path_dependent(
        spot=spot, strike=strike, sigma=sigma,
        seconds_to_settlement=seconds_left,
    )


def attach_sigma_yz(polls: pd.DataFrame, sigmas: pd.DataFrame) -> pd.DataFrame:
    """Match each poll to the σ_yz at its most recent completed-minute boundary."""
    polls = polls.copy()
    # For poll at ts_ms = t, latest completed candle ends at floor(t/60)*60.
    polls["bar_epoch_s"] = (polls["ts_ms"] // 60_000) * 60
    sigmas = sigmas.rename(columns={"epoch_s": "bar_epoch_s"})
    merged = polls.merge(sigmas, on="bar_epoch_s", how="left")
    return merged


def first_trigger(df: pd.DataFrame, side: str, sigma_col: str) -> pd.DataFrame:
    """Recompute model_prob with `sigma_col`, fee-adjust, dedupe per (event,strike)."""
    out = df.dropna(subset=[sigma_col]).copy()
    out["mp"] = [
        model_prob(s, k, sg, mleft * 60.0)
        for s, k, sg, mleft in zip(out["spot"], out["strike"], out[sigma_col], out["minutes_left"])
    ]
    out["ask_c"] = (out["yes_ask"] * 100).round().clip(1, 99).astype(int)
    out["bid_c"] = (out["yes_bid"] * 100).round().clip(1, 99).astype(int)
    out["buy_fee"] = out["ask_c"].map(fee_cents)
    out["sell_fee"] = out["bid_c"].map(fee_cents)
    if side == "buy":
        out["edge"] = out["mp"] * 100 - out["yes_ask"] * 100 - out["buy_fee"]
        out["pnl"] = (out["outcome"] - out["yes_ask"]) * 100 - out["buy_fee"]
    else:
        out["edge"] = out["yes_bid"] * 100 - out["mp"] * 100 - out["sell_fee"]
        out["pnl"] = (out["yes_bid"] - out["outcome"]) * 100 - out["sell_fee"]
    triggered = out[out["edge"] > MIN_EDGE_CENTS].sort_values("ts_ms")
    return triggered.groupby(["event_ticker", "strike"], as_index=False).first()


def summarize(label: str, picks: pd.DataFrame) -> None:
    n = len(picks)
    if n == 0:
        print(f"{label:>14}: 0 trades")
        return
    pnl = picks["pnl"]
    se = pnl.std(ddof=1) / math.sqrt(n) if n > 1 else float("nan")
    print(
        f"{label:>14}: n={n:4d}  total={pnl.sum():+8.1f}¢  "
        f"mean={pnl.mean():+5.2f}¢ ±{se:.2f}  "
        f"win={(pnl>0).mean()*100:4.1f}%"
    )


def main() -> None:
    conn = sqlite3.connect(str(ROOT / "pricer.db"))
    polls = pd.read_sql_query("SELECT * FROM polls", conn)
    settles = pd.read_sql_query("SELECT event_ticker, settle_price FROM settlements", conn)
    conn.close()

    polls = polls[polls["yes_bid"].notna() & polls["yes_ask"].notna()]
    polls = polls[polls["minutes_left"] >= MIN_MINUTES_TO_CLOSE].copy()
    polls = polls.merge(settles, on="event_ticker", how="inner")
    polls["outcome"] = (polls["settle_price"] > polls["strike"]).astype(int)

    start_s = int(polls["ts_ms"].min() // 1000) - VOL_WINDOW_MIN * 60 - 60
    end_s = int(polls["ts_ms"].max() // 1000) + 60
    rows = get_or_fetch(start_s, end_s)

    print(f"computing rolling {VOL_WINDOW_MIN}m σ_yz / σ_cc on {len(rows)} candles…")
    sigmas = rolling_sigmas(rows, VOL_WINDOW_MIN)
    print(
        f"σ_cc  median={sigmas['sigma_cc'].median():.3f}  mean={sigmas['sigma_cc'].mean():.3f}\n"
        f"σ_yz  median={sigmas['sigma_yz'].median():.3f}  mean={sigmas['sigma_yz'].mean():.3f}\n"
        f"yz/cc ratio  median={(sigmas['sigma_yz']/sigmas['sigma_cc']).median():.3f}  "
        f"mean={(sigmas['sigma_yz']/sigmas['sigma_cc']).mean():.3f}"
    )

    polls = attach_sigma_yz(polls, sigmas)
    matched = polls["sigma_yz"].notna().sum()
    print(f"polls matched to σ_yz: {matched:,} / {len(polls):,}")

    print(f"\n--- side comparison (MIN_EDGE_CENTS={MIN_EDGE_CENTS:.0f}, T≥{MIN_MINUTES_TO_CLOSE:.0f}min) ---")
    for side in ("buy", "sell"):
        cc_picks = first_trigger(polls.assign(sigma_in=polls["sigma"]), side, "sigma_in")
        yz_picks = first_trigger(polls, side, "sigma_yz")
        summarize(f"{side.upper()} σ_cc", cc_picks)
        summarize(f"{side.upper()} σ_yz", yz_picks)
        # Trades the new regime stops or starts taking.
        cc_keys = set(zip(cc_picks["event_ticker"], cc_picks["strike"]))
        yz_keys = set(zip(yz_picks["event_ticker"], yz_picks["strike"]))
        only_cc = cc_keys - yz_keys
        only_yz = yz_keys - cc_keys
        if only_cc:
            killed = cc_picks[cc_picks.set_index(["event_ticker", "strike"]).index.isin(only_cc)]
            print(
                f"  {len(killed)} {side.upper()} trades dropped under σ_yz — "
                f"their actual P&L: total={killed['pnl'].sum():+.1f}¢  "
                f"mean={killed['pnl'].mean():+.2f}¢  win={(killed['pnl']>0).mean()*100:.1f}%"
            )
        if only_yz:
            added = yz_picks[yz_picks.set_index(["event_ticker", "strike"]).index.isin(only_yz)]
            print(
                f"  {len(added)} {side.upper()} trades added under σ_yz — "
                f"their actual P&L: total={added['pnl'].sum():+.1f}¢  "
                f"mean={added['pnl'].mean():+.2f}¢  win={(added['pnl']>0).mean()*100:.1f}%"
            )


if __name__ == "__main__":
    main()
