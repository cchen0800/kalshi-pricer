"""Main loop: every N seconds, fetch → price → compare → log.

Read-only. No order placement anywhere in this module.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from src.btc_feed import CoinbaseFeed, closes, ohlc
from src.calibration import IsotonicCalibrator, identity as identity_calibrator, load as load_calibrator
from src.db import PollRow, ShadowSignal, insert_polls, insert_shadow_signals, open_db
from src.kalshi_client import KalshiClient
from src.positions import kalshi_fee_cents
from src.pricer import edge_cents, prob_above_strike, prob_above_strike_path_dependent
from src.realized import RealizedAverager
from src.vol import annualized_vol, yang_zhang_vol

BRTI_AVERAGING_WINDOW_SECONDS = 60.0  # Kalshi BRTI averages over the final 60s before close.

log = logging.getLogger("engine")

EDT = ZoneInfo("America/New_York")
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
TICKER_RE = re.compile(r"^KXBTCD-(\d{2})([A-Z]{3})(\d{2})(\d{2})$")


@dataclass
class EngineConfig:
    poll_interval_seconds: int = 30
    fast_poll_interval_seconds: int = 3   # used when seconds_to_settlement < FAST_POLL_THRESHOLD_S
    edge_threshold_cents: float = 5.0
    vol_window_minutes: int = 60
    db_path: str = "./pricer.db"
    series: str = "KXBTCD"
    vol_estimator: str = "yang_zhang"  # 'yang_zhang' | 'close'
    calibrator_path: str = "./calibrator.json"  # missing file → identity (no-op)
    # PR #6B: when True, the σ estimation window shrinks toward
    # max(VOL_WINDOW_MIN_FLOOR_MIN, ceil(seconds_to_settlement/60)) so recent
    # regime is weighted more for short-horizon events. Default False = use
    # full vol_window_minutes regardless of horizon (current behavior).
    match_vol_window_to_horizon: bool = False
    # PR #6C: annualized log-return drift μ for the lognormal pricer. Default
    # 0.0 preserves the zero-drift assumption. Plumbing only — no estimator
    # in v0; user can pin a static μ from outside analysis.
    spot_drift_per_year: float = 0.0


VOL_WINDOW_MIN_FLOOR_MIN = 20  # Yang-Zhang on fewer bars gets jumpy.


FAST_POLL_THRESHOLD_S = 90.0  # switch to fast cadence inside the last 90s of an event


def event_close_utc(event_ticker: str) -> datetime | None:
    m = TICKER_RE.match(event_ticker)
    if not m:
        return None
    yy, mon, dd, hh = m.groups()
    if mon not in MONTHS:
        return None
    return datetime(2000 + int(yy), MONTHS[mon], int(dd), int(hh), 0, tzinfo=EDT).astimezone(
        timezone.utc
    )


def find_nearest_open_event(kc: KalshiClient, series: str) -> tuple[str, datetime] | None:
    """Returns (event_ticker, close_utc) for the nearest hourly that hasn't closed yet."""
    evs = kc.list_events(series_ticker=series, status="open", limit=200).get("events", [])
    if not evs:
        evs = kc.list_events(series_ticker=series, limit=200).get("events", [])
    now = datetime.now(timezone.utc)
    candidates = []
    for e in evs:
        ct = event_close_utc(e["event_ticker"])
        if ct is not None and ct > now:
            candidates.append((e["event_ticker"], ct))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1])
    return candidates[0]


def _f(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_poll_rows(
    *,
    ts_ms: int,
    event_ticker: str,
    markets: Iterable[dict],
    spot: float,
    sigma: float,
    seconds_to_settlement: float,
    realized_partial_avg: float | None = None,
    averaging_window_seconds: float = BRTI_AVERAGING_WINDOW_SECONDS,
    calibrator: IsotonicCalibrator | None = None,
    drift_per_year: float = 0.0,
) -> list[PollRow]:
    """Price each strike and build PollRow records.

    `calibrator` is applied post-hoc to `model_prob`; the result is stored as
    `model_prob_calibrated`. The raw `model_prob` is preserved unchanged so
    diagnostics and offline backtests can compare. `edge_cents` here remains
    the raw-model-vs-mid edge — actionable_edge() reads it for the trade
    decision and will be retargeted to the calibrated value in PR #3.
    """
    if calibrator is None:
        calibrator = identity_calibrator()
    minutes_left = seconds_to_settlement / 60.0
    rows: list[PollRow] = []
    for m in markets:
        strike = _f(m.get("floor_strike"))
        if strike is None or strike <= 0:
            continue
        yes_bid = _f(m.get("yes_bid_dollars"))
        yes_ask = _f(m.get("yes_ask_dollars"))
        bid_size = _f(m.get("yes_bid_size_fp"))
        ask_size = _f(m.get("yes_ask_size_fp"))
        # Kalshi's markets endpoint returns NO-side prices but not NO-side
        # depth — only yes_*_size_fp is exposed. Fetch the orderbook endpoint
        # later if size for BUY_NO ever becomes load-bearing.
        no_bid = _f(m.get("no_bid_dollars"))
        no_ask = _f(m.get("no_ask_dollars"))
        volume = _f(m.get("volume_fp"))

        model_prob = prob_above_strike_path_dependent(
            spot=spot,
            strike=strike,
            sigma=sigma,
            seconds_to_settlement=seconds_to_settlement,
            realized_partial_avg=realized_partial_avg,
            averaging_window_seconds=averaging_window_seconds,
            drift_per_year=drift_per_year,
        )
        model_prob_calibrated = calibrator.apply(model_prob)
        # Signed edge vs. mid (positive = model > mid, suggests buy YES).
        if yes_bid is not None and yes_ask is not None:
            mid_cents = (yes_bid + yes_ask) * 50.0
            edge = model_prob * 100.0 - mid_cents
        else:
            edge = 0.0

        rows.append(PollRow(
            ts_ms=ts_ms,
            event_ticker=event_ticker,
            market_ticker=m["ticker"],
            strike=strike,
            spot=spot,
            sigma=sigma,
            minutes_left=minutes_left,
            model_prob=model_prob,
            model_prob_calibrated=model_prob_calibrated,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            yes_bid_size=bid_size,
            yes_ask_size=ask_size,
            no_bid=no_bid,
            no_ask=no_ask,
            volume=volume,
            edge_cents=edge,
        ))
    return rows


# ---- Regime gates for opening new long positions ----
# BUY_YES floor: identified from the first 33 live trades (May 2-4, 2026):
# wins concentrated at σ>55% / strike >0.4% from spot / T>30min, losses
# concentrated at σ<25% / ATM (±0.1%) / T<15min.
#
# BUY_NO floor: the bucketed backtest (scripts/backtest_lowvol_regime.py) on
# 27 settled events shows BUY_NO is +12.8¢/trade across σ<0.50 (n=134, 26
# events) but only +7.3¢/trade at σ≥0.50 (n=40, 5 events). Applying the
# BUY_YES floor to BUY_NO filters out the band where the alpha lives. The
# 0.20 floor is a "calm-market garbage" cutoff — well below the band where
# BUY_NO has shown positive EV — and is intentionally separate from the
# BUY_YES floor so this knob can move independently.
#
# SELLs (closing existing longs) are NOT gated — we always want to be able
# to realize a positive sell edge to exit cleanly.
BUY_YES_GATE_MIN_SIGMA = 0.50      # 60-min annualized realized vol
BUY_NO_GATE_MIN_SIGMA = 0.20
BUY_GATE_MIN_SIGMA = BUY_YES_GATE_MIN_SIGMA  # legacy alias for shadow logging
BUY_GATE_MIN_DIST_PCT = 0.0010     # |strike - spot| / spot
BUY_GATE_MIN_MINUTES = 15.0        # minutes left to close
# Calibrated mp band: backtest shows BUY/SELL alpha lives strictly inside
# this band. Outside it the calibrator is fit on sparse tail data and the
# extreme probabilities should not be acted on. Falls back to raw model_prob
# when calibrated is None (legacy rows / no calibrator file).
BUY_GATE_MP_BAND_LO = 0.05
BUY_GATE_MP_BAND_HI = 0.85


def _row_calibrated_mp(row: PollRow) -> float:
    """Use the row's calibrated mp if the engine populated it; fall back to
    raw model_prob otherwise so unit tests and pre-calibrator rows keep
    working without conditional plumbing at every call site."""
    return row.model_prob_calibrated if row.model_prob_calibrated is not None else row.model_prob


def _passes_buy_gates(row: PollRow, side: str) -> bool:
    """Per-side regime gate. Only the σ floor differs between BUY_YES and
    BUY_NO; dist / T / mp-band are shared. `side` must be 'BUY_YES' or
    'BUY_NO'."""
    if side == "BUY_YES":
        sigma_min = BUY_YES_GATE_MIN_SIGMA
    elif side == "BUY_NO":
        sigma_min = BUY_NO_GATE_MIN_SIGMA
    else:
        raise ValueError(f"_passes_buy_gates: side must be BUY_YES|BUY_NO, got {side!r}")
    if row.sigma is None or row.sigma < sigma_min:
        return False
    if row.spot is None or row.spot <= 0 or row.strike is None:
        return False
    if abs(row.strike - row.spot) / row.spot < BUY_GATE_MIN_DIST_PCT:
        return False
    if row.minutes_left is None or row.minutes_left < BUY_GATE_MIN_MINUTES:
        return False
    cal_mp = _row_calibrated_mp(row)
    if not (BUY_GATE_MP_BAND_LO <= cal_mp < BUY_GATE_MP_BAND_HI):
        return False
    return True


def _net_edges(row: PollRow) -> tuple[float | None, float | None]:
    """Return (buy_yes_net_cents, sell_yes_net_cents) — post-fee YES-side edges.

    Either may be None when the corresponding book side is missing. Negative
    values indicate the model disagrees with the book in that direction.
    Used by shadow signal logging (which writes a YES-only schema). For full
    side coverage including BUY_NO, see _all_net_edges()."""
    if row.yes_ask is not None:
        ask_cents = int(round(row.yes_ask * 100))
        fee = kalshi_fee_cents(max(1, min(99, ask_cents)), 1)
        buy: float | None = row.model_prob * 100.0 - row.yes_ask * 100.0 - fee
    else:
        buy = None
    if row.yes_bid is not None:
        bid_cents = int(round(row.yes_bid * 100))
        fee = kalshi_fee_cents(max(1, min(99, bid_cents)), 1)
        sell: float | None = row.yes_bid * 100.0 - row.model_prob * 100.0 - fee
    else:
        sell = None
    return buy, sell


def _buy_no_net(row: PollRow) -> float | None:
    """Post-fee edge of a BUY_NO at no_ask.

    Buying NO at no_ask wins (1 - no_ask) when YES does NOT settle, which the
    model says happens with prob (1 - model_prob). EV per contract:
        (1 - model_prob) * 100  - no_ask * 100  - fee(no_ask)
    Same structure as BUY_YES with (model_prob, yes_ask) → (1-model_prob, no_ask).
    Returns None when no_ask is missing.
    """
    if row.no_ask is None:
        return None
    ask_cents = int(round(row.no_ask * 100))
    fee = kalshi_fee_cents(max(1, min(99, ask_cents)), 1)
    return (1.0 - row.model_prob) * 100.0 - row.no_ask * 100.0 - fee


def _sell_no_net(row: PollRow) -> float | None:
    """Post-fee edge of a SELL_NO at no_bid (closing a long NO).

    Selling NO at no_bid means receiving no_bid in exchange for giving up a
    contract worth (1 - model_prob) in expectation. EV per contract:
        no_bid * 100  - (1 - model_prob) * 100  - fee(no_bid)
    Symmetric to SELL_YES with (yes_bid, model_prob) → (no_bid, 1-model_prob).
    Positive when the book is overpaying for our NO relative to model fair value.
    Returns None when no_bid is missing.
    """
    if row.no_bid is None:
        return None
    bid_cents = int(round(row.no_bid * 100))
    fee = kalshi_fee_cents(max(1, min(99, bid_cents)), 1)
    return row.no_bid * 100.0 - (1.0 - row.model_prob) * 100.0 - fee


def _gate_flags(row: PollRow) -> tuple[int, int, int, int]:
    """Per-gate pass booleans for shadow logging. Mirrors _passes_buy_gates."""
    sig = int(row.sigma is not None and row.sigma >= BUY_GATE_MIN_SIGMA)
    if row.spot is None or row.spot <= 0 or row.strike is None:
        dist = 0
    else:
        dist = int(abs(row.strike - row.spot) / row.spot >= BUY_GATE_MIN_DIST_PCT)
    t = int(row.minutes_left is not None and row.minutes_left >= BUY_GATE_MIN_MINUTES)
    cal_mp = _row_calibrated_mp(row)
    band = int(BUY_GATE_MP_BAND_LO <= cal_mp < BUY_GATE_MP_BAND_HI)
    return sig, dist, t, band


@dataclass(frozen=True)
class SidePolicy:
    """Per-bot policy for which sides actionable_edge() may return.

    The default LEGACY_POLICY preserves pre-PR-#3 behavior: BUY_YES is the
    only entry side, SELL_YES is unconditional. Selective profile flips this
    to BUY_NO entries (where the SELL-side alpha actually lives — see
    project_calibrated_no_entry_plan) by setting allow_buy_yes=False,
    allow_buy_no=True. sell_yes_to_close_only is informational at this
    layer; the executor's `_build_ticket` already enforces that SELL_YES
    requires existing inventory."""

    allow_buy_yes: bool = True
    allow_buy_no: bool = False
    sell_yes_to_close_only: bool = False


LEGACY_POLICY = SidePolicy(allow_buy_yes=True, allow_buy_no=False, sell_yes_to_close_only=False)


def actionable_edge(row: PollRow, policy: SidePolicy = LEGACY_POLICY) -> tuple[str, float]:
    """Return (side, cents) of best lift-the-market edge, NET of Kalshi taker fee.

    side ∈ {'BUY_YES','BUY_NO','SELL_YES','SELL_NO','NONE'}. Returned `cents`
    is what we'd actually pocket per contract after the exchange fee — at
    price=50¢ the fee is ~2¢, near 0¢/100¢ it's ~0¢, so 50/50 contracts must
    clear a higher gross edge.

    Both BUY sides are regime-gated (see _passes_buy_gates) — we only open
    new positions when σ is high enough, the strike is far enough from spot,
    and there's enough time left. The σ floor differs by side: BUY_YES uses
    BUY_YES_GATE_MIN_SIGMA (high — wins concentrate at high vol), BUY_NO
    uses the lower BUY_NO_GATE_MIN_SIGMA (NO-side alpha lives across all vol
    bands per the bucketed backtest). SELL_YES and SELL_NO are not gated;
    closing an existing long is always allowed when the model says the book
    is overpaying. Inventory is enforced downstream in the executor —
    actionable_edge surfaces the edge regardless of whether we hold the
    position. The `policy` argument toggles whether each side is eligible
    at all; default preserves legacy BUY_YES-only entry behavior.
    """
    buy_yes_or_none, sell_yes_or_none = _net_edges(row)
    buy_no_or_none = _buy_no_net(row)
    sell_no_or_none = _sell_no_net(row)
    buy_yes = buy_yes_or_none if buy_yes_or_none is not None else float("-inf")
    buy_no = buy_no_or_none if buy_no_or_none is not None else float("-inf")
    sell_yes = sell_yes_or_none if sell_yes_or_none is not None else float("-inf")
    sell_no = sell_no_or_none if sell_no_or_none is not None else float("-inf")

    buy_yes_eligible = (
        policy.allow_buy_yes and buy_yes > 0
        and _passes_buy_gates(row, "BUY_YES")
    )
    buy_no_eligible = (
        policy.allow_buy_no and buy_no > 0
        and _passes_buy_gates(row, "BUY_NO")
    )
    sell_yes_eligible = sell_yes > 0  # ungated; executor enforces inventory
    sell_no_eligible = sell_no > 0    # ungated; executor enforces inventory

    candidates: list[tuple[str, float]] = []
    if buy_yes_eligible:
        candidates.append(("BUY_YES", buy_yes))
    if buy_no_eligible:
        candidates.append(("BUY_NO", buy_no))
    if sell_yes_eligible:
        candidates.append(("SELL_YES", sell_yes))
    if sell_no_eligible:
        candidates.append(("SELL_NO", sell_no))
    if not candidates:
        return "NONE", 0.0
    candidates.sort(key=lambda t: -t[1])
    return candidates[0]


def build_shadow_signals(rows: Iterable[PollRow]) -> list[ShadowSignal]:
    """One row per (poll, market) where either raw side has positive net edge.

    Persists what the engine *thought* — not just what it traded — so future
    backtests can ask "what if σ floor were 0.40?" without re-running the
    pricer over poll history. Skipping rows where both sides are negative
    keeps volume sane (most rows in calm markets fall here)."""
    out: list[ShadowSignal] = []
    for row in rows:
        buy_net, sell_net = _net_edges(row)
        # Only log "interesting" rows — at least one side must look tradeable
        # before fees, otherwise this is just noise duplicating polls.
        if (buy_net is None or buy_net <= 0) and (sell_net is None or sell_net <= 0):
            continue
        sig_p, dist_p, time_p, band_p = _gate_flags(row)
        side, cents = actionable_edge(row)
        out.append(ShadowSignal(
            ts_ms=row.ts_ms,
            event_ticker=row.event_ticker,
            market_ticker=row.market_ticker,
            strike=row.strike,
            spot=row.spot,
            sigma=row.sigma,
            minutes_left=row.minutes_left,
            model_prob=row.model_prob,
            yes_bid=row.yes_bid,
            yes_ask=row.yes_ask,
            buy_edge_net_cents=buy_net,
            sell_edge_net_cents=sell_net,
            gate_sigma_passed=sig_p,
            gate_dist_passed=dist_p,
            gate_time_passed=time_p,
            gate_mp_band_passed=band_p,
            chosen_side=side,
            chosen_edge_cents=cents,
        ))
    return out


def run_one_poll(
    *,
    kc: KalshiClient,
    feed: CoinbaseFeed,
    cfg: EngineConfig,
    db,
    on_poll: Callable[[list[PollRow]], None] | None = None,
    averagers: dict[str, RealizedAverager] | None = None,
    calibrator: IsotonicCalibrator | None = None,
) -> float | None:
    """Run one poll cycle. Returns seconds_to_settlement of the active event,
    or None if no open event was found. The outer loop uses this to switch
    between normal and fast-poll cadence near expiry."""
    found = find_nearest_open_event(kc, cfg.series)
    if found is None:
        log.warning("no open hourly event found")
        return None
    event_ticker, close_utc = found
    now = datetime.now(timezone.utc)
    seconds_to_settlement = max(0.0, (close_utc - now).total_seconds())
    minutes_left = seconds_to_settlement / 60.0
    if seconds_to_settlement <= 0:
        log.info("event %s already closed", event_ticker)
        return seconds_to_settlement

    spot_obj = feed.get_spot()
    # PR #6B: optionally shrink the σ window toward the event horizon when
    # cfg.match_vol_window_to_horizon is set, floored at VOL_WINDOW_MIN_FLOOR_MIN
    # to keep Yang-Zhang stable.
    if cfg.match_vol_window_to_horizon:
        eff_window_min = min(
            cfg.vol_window_minutes,
            max(VOL_WINDOW_MIN_FLOOR_MIN, int(seconds_to_settlement // 60) + 1),
        )
    else:
        eff_window_min = cfg.vol_window_minutes
    candles = feed.get_1m_candles(eff_window_min)
    if len(candles) < 2:
        log.warning("not enough candles to compute vol (%d)", len(candles))
        return seconds_to_settlement
    if cfg.vol_estimator == "yang_zhang":
        sigma = yang_zhang_vol(ohlc(candles))
    elif cfg.vol_estimator == "close":
        sigma = annualized_vol(closes(candles))
    else:
        raise ValueError(f"unknown vol_estimator: {cfg.vol_estimator!r}")

    # Maintain a per-event spot buffer so we can compute the realized portion of
    # the BRTI averaging window. When we're inside the final 60s, this becomes
    # the "locked in" component of the settlement value.
    realized_partial_avg: float | None = None
    if averagers is not None:
        avg = averagers.setdefault(event_ticker, RealizedAverager())
        avg.add(spot_obj.epoch_ms / 1000.0, spot_obj.price)
        if seconds_to_settlement < BRTI_AVERAGING_WINDOW_SECONDS:
            close_s = close_utc.timestamp()
            realized_partial_avg = avg.average(
                window_start_s=close_s - BRTI_AVERAGING_WINDOW_SECONDS,
                window_end_s=min(close_s, now.timestamp()),
            )

    markets = kc.list_markets(event_ticker=event_ticker, limit=500).get("markets", [])
    ts_ms = int(time.time() * 1000)
    rows = build_poll_rows(
        ts_ms=ts_ms,
        event_ticker=event_ticker,
        markets=markets,
        spot=spot_obj.price,
        sigma=sigma,
        seconds_to_settlement=seconds_to_settlement,
        realized_partial_avg=realized_partial_avg,
        calibrator=calibrator,
        drift_per_year=cfg.spot_drift_per_year,
    )
    n = insert_polls(db, rows)
    shadows = build_shadow_signals(rows)
    if shadows:
        try:
            insert_shadow_signals(db, shadows)
        except Exception:
            log.exception("shadow_signals insert failed; continuing")

    flagged = [r for r in rows if abs(actionable_edge(r)[1]) > cfg.edge_threshold_cents]
    flagged.sort(key=lambda r: -abs(actionable_edge(r)[1]))

    realized_tag = (
        f"  realized=${realized_partial_avg:,.2f}" if realized_partial_avg is not None else ""
    )
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] "
        f"{event_ticker}  spot=${spot_obj.price:,.2f}  σ={sigma:.1%}  "
        f"T-{minutes_left:5.1f}min  rows={n}  flagged={len(flagged)}{realized_tag}"
    )
    for r in flagged[:8]:
        side, cents = actionable_edge(r)
        bid = f"{r.yes_bid:.3f}" if r.yes_bid is not None else "  —  "
        ask = f"{r.yes_ask:.3f}" if r.yes_ask is not None else "  —  "
        print(
            f"   K=${r.strike:>10,.2f}  model={r.model_prob*100:5.1f}¢  "
            f"bid={bid} ask={ask}  → {side} +{cents:.1f}¢"
        )

    if on_poll is not None:
        try:
            on_poll(rows)
        except Exception:
            log.exception("on_poll hook failed; continuing")

    return seconds_to_settlement


def run(
    cfg: EngineConfig,
    stop_event: threading.Event | None = None,
    on_poll: Callable[[list[PollRow]], None] | None = None,
) -> None:
    """Run the polling loop.

    If `stop_event` is supplied (e.g. from a dashboard host running this in a
    background thread), the loop exits when the event is set. Otherwise it
    runs until KeyboardInterrupt.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    averagers: dict[str, RealizedAverager] = {}
    calibrator = load_calibrator(cfg.calibrator_path)
    with KalshiClient() as kc, CoinbaseFeed() as feed, open_db(cfg.db_path) as db:
        log.info("engine started — db=%s poll=%ds threshold=%.1f¢",
                 cfg.db_path, cfg.poll_interval_seconds, cfg.edge_threshold_cents)
        # Prime the feed's candle cache from recent poll history so that, if
        # Coinbase /candles is in an outage at startup, we can still compute a
        # vol estimate from logged spot samples until /candles recovers.
        try:
            cutoff_ms = int((time.time() - 7200) * 1000)
            cur = db.execute(
                "SELECT DISTINCT ts_ms, spot FROM polls WHERE ts_ms >= ? ORDER BY ts_ms",
                (cutoff_ms,),
            )
            samples = [(int(row[0]) // 1000, float(row[1])) for row in cur.fetchall()]
            n_bars = feed.prime_candles_from_spots(samples)
            if n_bars:
                log.info("primed candle cache from %d spot samples → %d 1m bars",
                         len(samples), n_bars)
        except Exception:
            log.exception("candle-cache priming failed; continuing without it")
        while True:
            if stop_event is not None and stop_event.is_set():
                log.info("stop event set, exiting")
                return
            t0 = time.time()
            seconds_to_settlement: float | None = None
            try:
                seconds_to_settlement = run_one_poll(
                    kc=kc, feed=feed, cfg=cfg, db=db,
                    on_poll=on_poll, averagers=averagers,
                    calibrator=calibrator,
                )
            except KeyboardInterrupt:
                log.info("interrupted, exiting")
                return
            except Exception:
                log.exception("poll failed; will retry next interval")
            elapsed = time.time() - t0
            # Switch to fast cadence inside the BRTI averaging window neighborhood
            # so the RealizedAverager actually has samples to work with.
            if (
                seconds_to_settlement is not None
                and 0 < seconds_to_settlement < FAST_POLL_THRESHOLD_S
            ):
                interval = cfg.fast_poll_interval_seconds
            else:
                interval = cfg.poll_interval_seconds
            remaining = max(0.0, interval - elapsed)
            # Sleep in small increments so stop_event is responsive.
            if stop_event is not None:
                deadline = time.time() + remaining
                while time.time() < deadline:
                    if stop_event.is_set():
                        return
                    time.sleep(min(0.5, deadline - time.time()))
            else:
                time.sleep(remaining)
