"""actionable_edge returns net-of-fee edge — these tests lock that contract."""

from __future__ import annotations

import time

import pytest

from src.db import PollRow
from src.engine import (
    LEGACY_POLICY,
    SidePolicy,
    actionable_edge,
    build_poll_rows,
    build_shadow_signals,
)
from src.positions import kalshi_fee_cents


def make_row(
    *,
    model_prob: float,
    yes_bid: float | None,
    yes_ask: float | None,
    no_bid: float | None = None,
    no_ask: float | None = None,
    strike: float = 79_500.0,   # ~0.6% away from default spot — passes distance gate
    spot: float = 80_000.0,
    sigma: float = 0.60,        # passes σ gate
    minutes_left: float = 30.0, # passes time gate
) -> PollRow:
    return PollRow(
        ts_ms=int(time.time() * 1000),
        event_ticker="KXETHD-26MAY0113",
        market_ticker=f"KXETHD-26MAY0113-T{int(strike)}",
        strike=strike,
        spot=spot,
        sigma=sigma,
        minutes_left=minutes_left,
        model_prob=model_prob,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        yes_bid_size=100,
        yes_ask_size=100,
        no_bid=no_bid,
        no_ask=no_ask,
        volume=1000,
        edge_cents=0.0,
    )


def test_buy_edge_subtracts_fee():
    # Model 80c, ask 25c → gross 55c. Fee at 25c = ceil(0.07 * .25 * .75 * 100) = 2c.
    row = make_row(model_prob=0.80, yes_bid=0.20, yes_ask=0.25)
    side, edge = actionable_edge(row)
    assert side == "BUY_YES"
    expected_fee = kalshi_fee_cents(25, 1)
    assert edge == pytest.approx(55.0 - expected_fee)


def test_sell_edge_subtracts_fee():
    # Model 10c, bid 50c → gross sell 40c. Fee at 50c = ceil(0.07*.5*.5*100) = 2c.
    row = make_row(model_prob=0.10, yes_bid=0.50, yes_ask=0.55)
    side, edge = actionable_edge(row)
    assert side == "SELL_YES"
    expected_fee = kalshi_fee_cents(50, 1)
    assert edge == pytest.approx(40.0 - expected_fee)


def test_fee_kills_marginal_edge_at_50_50():
    # At a 50/50 contract the fee is largest (~2c). A 1c gross edge becomes negative net.
    row = make_row(model_prob=0.51, yes_bid=0.49, yes_ask=0.50)
    side, edge = actionable_edge(row)
    # Gross buy = 1c, fee at 50c = 2c → net -1c. Gross sell = -2c. Both negative → NONE.
    assert side == "NONE"
    assert edge == 0.0


def test_no_book_returns_none():
    row = make_row(model_prob=0.50, yes_bid=None, yes_ask=None)
    side, edge = actionable_edge(row)
    assert side == "NONE"
    assert edge == 0.0


def test_picks_larger_side():
    # Symmetric model, asymmetric book: ask much wider than bid → sell side bigger.
    row = make_row(model_prob=0.50, yes_bid=0.65, yes_ask=0.95)
    side, _edge = actionable_edge(row)
    assert side == "SELL_YES"


# ---- Regime gates for BUY_YES ----

def test_buy_gate_blocks_low_sigma():
    # Same buy-edge setup as test_buy_edge_subtracts_fee but σ below cutoff.
    row = make_row(model_prob=0.80, yes_bid=0.20, yes_ask=0.25, sigma=0.30)
    side, edge = actionable_edge(row)
    assert side == "NONE"
    assert edge == 0.0


def test_buy_gate_blocks_atm_strike():
    # Strike == spot → distance 0 < 0.10% gate.
    row = make_row(model_prob=0.80, yes_bid=0.20, yes_ask=0.25, strike=80_000.0, spot=80_000.0)
    side, edge = actionable_edge(row)
    assert side == "NONE"
    assert edge == 0.0


def test_buy_gate_blocks_near_close():
    # 10 minutes left < 15-minute floor.
    row = make_row(model_prob=0.80, yes_bid=0.20, yes_ask=0.25, minutes_left=10.0)
    side, edge = actionable_edge(row)
    assert side == "NONE"
    assert edge == 0.0


def test_sell_not_gated_by_regime():
    # Same low-σ ATM near-close conditions that block buys must not block sells:
    # closing an existing long always has to be possible.
    row = make_row(
        model_prob=0.10, yes_bid=0.50, yes_ask=0.55,
        strike=80_000.0, spot=80_000.0, sigma=0.20, minutes_left=3.0,
    )
    side, edge = actionable_edge(row)
    assert side == "SELL_YES"
    expected_fee = kalshi_fee_cents(50, 1)
    assert edge == pytest.approx(40.0 - expected_fee)


def test_sell_no_edge_subtracts_fee():
    # Model says P(yes)=30%, so NO is worth 70¢ in expectation. If no_bid=80¢
    # the book overpays us 10¢ gross; fee at 80¢ ≈ 2¢ → net ~8¢.
    # All other paths must be negative so SELL_NO actually wins:
    #   yes_ask=0.40 → BUY_YES = 30 - 40 - fee < 0
    #   yes_bid=0.05 → SELL_YES = 5 - 30 - fee < 0
    #   no_ask=0.95  → BUY_NO  = 70 - 95 - fee < 0
    row = make_row(
        model_prob=0.30,
        yes_bid=0.05, yes_ask=0.40,
        no_bid=0.80, no_ask=0.95,
    )
    side, edge = actionable_edge(row)
    assert side == "SELL_NO"
    expected_fee = kalshi_fee_cents(80, 1)
    assert edge == pytest.approx(10.0 - expected_fee)


def test_sell_no_not_gated_by_regime():
    # Closing a long NO must work even in calm-market / near-close conditions
    # that gate every BUY side. Mirror of test_sell_not_gated_by_regime.
    row = make_row(
        model_prob=0.30,
        yes_bid=0.05, yes_ask=0.40,
        no_bid=0.80, no_ask=0.95,
        strike=80_000.0, spot=80_000.0, sigma=0.20, minutes_left=3.0,
    )
    side, edge = actionable_edge(row)
    assert side == "SELL_NO"
    expected_fee = kalshi_fee_cents(80, 1)
    assert edge == pytest.approx(10.0 - expected_fee)


def test_sell_no_with_no_no_bid_returns_none():
    # No no_bid → SELL_NO has no exit price → falls through.
    row = make_row(model_prob=0.30, yes_bid=0.05, yes_ask=0.40,
                   no_bid=None, no_ask=0.95)
    side, _ = actionable_edge(row)
    assert side == "NONE"


def test_buy_falls_through_to_sell_when_gated():
    # Buy is bigger gross but blocked by the σ gate; sell side is positive
    # and ungated, so we should return SELL_YES (not NONE).
    # Need: model > ask (buy edge), bid > model (sell edge), buy > sell on
    # gross terms. e.g. model=0.40, ask=0.05 (buy gross 35c), bid=0.80 (sell
    # gross 40c). Hm — sell is bigger here. Try model=0.50, ask=0.10 (buy
    # 40c), bid=0.65 (sell 15c) → buy > sell, but σ blocks buy.
    row = make_row(model_prob=0.50, yes_bid=0.65, yes_ask=0.10, sigma=0.20)
    side, _edge = actionable_edge(row)
    assert side == "SELL_YES"


# ---- Shadow signal logging ----
# Shadow signals capture every "interesting" model decision — even when gates
# block trading — so we can backtest gate sensitivity without re-running the
# pricer over poll history.

def test_shadow_signal_logged_when_buy_edge_blocked_by_gate():
    # Same buy-edge setup as the σ-gate test: actionable_edge returns NONE
    # because σ is too low, but the model still saw a positive raw buy edge.
    # That's exactly what shadow_signals must capture.
    row = make_row(model_prob=0.80, yes_bid=0.20, yes_ask=0.25, sigma=0.30)
    sigs = build_shadow_signals([row])
    assert len(sigs) == 1
    s = sigs[0]
    # Buy raw edge is positive (we want this market) but σ gate fails.
    assert s.buy_edge_net_cents is not None and s.buy_edge_net_cents > 0
    assert s.gate_sigma_passed == 0
    assert s.gate_dist_passed == 1   # default strike 79_500 vs spot 80_000
    assert s.gate_time_passed == 1
    assert s.chosen_side == "NONE"   # what actionable_edge() actually returned
    assert s.chosen_edge_cents == 0.0


def test_shadow_signal_logged_when_buy_passes_all_gates():
    row = make_row(model_prob=0.80, yes_bid=0.20, yes_ask=0.25)
    sigs = build_shadow_signals([row])
    assert len(sigs) == 1
    s = sigs[0]
    assert s.gate_sigma_passed == 1
    assert s.gate_dist_passed == 1
    assert s.gate_time_passed == 1
    assert s.chosen_side == "BUY_YES"
    assert s.chosen_edge_cents > 0


def test_no_shadow_when_model_inside_book():
    # model=50c, bid=49c, ask=51c — both raw edges negative, nothing to log.
    row = make_row(model_prob=0.50, yes_bid=0.49, yes_ask=0.51)
    sigs = build_shadow_signals([row])
    assert sigs == []


def test_shadow_signal_records_both_side_edges():
    # Symmetric setup that has positive sell-side raw edge (bid > model + fee).
    row = make_row(model_prob=0.30, yes_bid=0.50, yes_ask=0.95)
    sigs = build_shadow_signals([row])
    assert len(sigs) == 1
    s = sigs[0]
    # Sell raw is positive: bid 50c, model 30c → gross 20c, minus ~2c fee.
    assert s.sell_edge_net_cents is not None and s.sell_edge_net_cents > 0
    # Buy raw is deeply negative: ask 95c, model 30c → -65c.
    assert s.buy_edge_net_cents is not None and s.buy_edge_net_cents < 0


def test_shadow_signal_dist_gate_flag_matches_distance():
    # Strike == spot → dist gate fails.
    atm = make_row(model_prob=0.80, yes_bid=0.20, yes_ask=0.25,
                   strike=80_000.0, spot=80_000.0)
    sigs = build_shadow_signals([atm])
    assert len(sigs) == 1
    assert sigs[0].gate_dist_passed == 0


def test_shadow_signal_time_gate_flag_matches_minutes_left():
    near_close = make_row(model_prob=0.80, yes_bid=0.20, yes_ask=0.25,
                          minutes_left=10.0)
    sigs = build_shadow_signals([near_close])
    assert len(sigs) == 1
    assert sigs[0].gate_time_passed == 0


def _market_dict(**overrides):
    base = {
        "ticker": "KXETHD-26MAY0113-T80000",
        "floor_strike": 80_000.0,
        "yes_bid_dollars": "0.40",
        "yes_ask_dollars": "0.42",
        "yes_bid_size_fp": "120",
        "yes_ask_size_fp": "85",
        "no_bid_dollars": "0.58",
        "no_ask_dollars": "0.60",
        "volume_fp": "1000",
    }
    base.update(overrides)
    return base


def test_build_poll_rows_ingests_no_side_prices():
    rows = build_poll_rows(
        ts_ms=1,
        event_ticker="KXETHD-26MAY0113",
        markets=[_market_dict()],
        spot=80_000.0,
        sigma=0.5,
        seconds_to_settlement=1800,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r.no_bid == pytest.approx(0.58)
    assert r.no_ask == pytest.approx(0.60)
    # YES-side untouched.
    assert r.yes_bid == pytest.approx(0.40)
    assert r.yes_ask == pytest.approx(0.42)


def test_build_poll_rows_no_side_missing_is_none():
    m = _market_dict()
    del m["no_bid_dollars"]
    del m["no_ask_dollars"]
    rows = build_poll_rows(
        ts_ms=1,
        event_ticker="KXETHD-26MAY0113",
        markets=[m],
        spot=80_000.0,
        sigma=0.5,
        seconds_to_settlement=1800,
    )
    assert rows[0].no_bid is None
    assert rows[0].no_ask is None


def test_polls_table_persists_no_side_prices(tmp_path):
    from src.db import insert_polls, open_db

    rows = build_poll_rows(
        ts_ms=1,
        event_ticker="KXETHD-26MAY0113",
        markets=[_market_dict()],
        spot=80_000.0,
        sigma=0.5,
        seconds_to_settlement=1800,
    )
    with open_db(tmp_path / "p.db") as db:
        insert_polls(db, rows)
        no_bid, no_ask = db.execute(
            "SELECT no_bid, no_ask FROM polls"
        ).fetchone()
    assert no_bid == pytest.approx(0.58)
    assert no_ask == pytest.approx(0.60)


# ---- SidePolicy tests ----

def test_legacy_policy_default_preserves_buy_yes():
    """No-policy-arg call must behave exactly as before PR #3."""
    row = make_row(model_prob=0.80, yes_bid=0.20, yes_ask=0.25)
    side, _ = actionable_edge(row)
    assert side == "BUY_YES"


def test_disallow_buy_yes_falls_through_to_none_when_no_other_signal():
    """allow_buy_yes=False with no NO-side and no SELL edge → NONE."""
    row = make_row(model_prob=0.80, yes_bid=0.20, yes_ask=0.25)
    pol = SidePolicy(allow_buy_yes=False, allow_buy_no=False, sell_yes_to_close_only=False)
    side, _ = actionable_edge(row, pol)
    assert side == "NONE"


def test_buy_no_emits_when_allowed_and_profitable():
    """allow_buy_no=True + cheap no_ask vs (1 - model_prob) → BUY_NO with
    correctly-signed net edge."""
    # Model says YES at 30%, so NO is worth 70¢. If no_ask=0.25 we pay 25¢
    # for a 70¢-EV contract → 45¢ gross BUY_NO edge minus fee.
    row = make_row(model_prob=0.30, yes_bid=0.20, yes_ask=0.25, no_ask=0.25)
    pol = SidePolicy(allow_buy_yes=False, allow_buy_no=True, sell_yes_to_close_only=False)
    side, edge = actionable_edge(row, pol)
    assert side == "BUY_NO"
    expected_fee = kalshi_fee_cents(25, 1)
    # (1 - 0.30) * 100 - 25 - fee = 70 - 25 - fee = 45 - fee
    assert edge == pytest.approx(45.0 - expected_fee)


def test_buy_no_passes_at_sigma_below_buy_yes_floor():
    """BUY_NO uses a lower σ floor than BUY_YES (0.20 vs 0.50). σ=0.30 sits
    in the band where BUY_YES is gated out but BUY_NO should still fire —
    that's the entire point of the side-aware gate."""
    row = make_row(model_prob=0.30, yes_bid=0.20, yes_ask=0.25,
                   no_ask=0.25, sigma=0.30)
    pol = SidePolicy(allow_buy_yes=False, allow_buy_no=True, sell_yes_to_close_only=False)
    side, _ = actionable_edge(row, pol)
    assert side == "BUY_NO"


def test_buy_no_blocked_below_its_own_sigma_floor():
    """σ=0.10 is below BUY_NO_GATE_MIN_SIGMA (0.20) — calm-market garbage
    cutoff still applies even on the NO side."""
    row = make_row(model_prob=0.30, yes_bid=0.20, yes_ask=0.25,
                   no_ask=0.25, sigma=0.10)
    pol = SidePolicy(allow_buy_yes=False, allow_buy_no=True, sell_yes_to_close_only=False)
    side, _ = actionable_edge(row, pol)
    assert side == "NONE"


def test_buy_no_with_no_no_ask_returns_none():
    """no_ask missing → BUY_NO has no price → NONE."""
    row = make_row(model_prob=0.30, yes_bid=0.20, yes_ask=0.25, no_ask=None)
    pol = SidePolicy(allow_buy_yes=False, allow_buy_no=True, sell_yes_to_close_only=False)
    side, _ = actionable_edge(row, pol)
    assert side == "NONE"


def test_selective_policy_picks_buy_no_over_sell_yes_when_higher():
    """Selective profile = (False, True, True). When BUY_NO and SELL_YES
    both have positive edge, the larger one wins."""
    # YES bid 50¢, model 10¢ → SELL edge ~40 - fee.
    # NO ask 50¢, model 10¢ → BUY_NO edge: 90 - 50 - fee ≈ 38.
    row = make_row(model_prob=0.10, yes_bid=0.50, yes_ask=0.55, no_ask=0.50)
    pol = SidePolicy(allow_buy_yes=False, allow_buy_no=True, sell_yes_to_close_only=True)
    side, _ = actionable_edge(row, pol)
    assert side == "BUY_NO"


def test_selective_policy_emits_sell_yes_when_no_other_signal():
    """Selective should still surface SELL_YES — executor handles inventory."""
    # No NO-side quote, but a SELL_YES edge exists.
    row = make_row(model_prob=0.10, yes_bid=0.50, yes_ask=0.55, no_ask=None)
    pol = SidePolicy(allow_buy_yes=False, allow_buy_no=True, sell_yes_to_close_only=True)
    side, _ = actionable_edge(row, pol)
    assert side == "SELL_YES"


def test_buy_yes_still_wins_when_higher_under_legacy_policy():
    """Legacy policy: when both BUY_YES and SELL_YES have edge, larger wins."""
    # model 80, yes_ask 10 → BUY_YES edge ~70
    # model 80, yes_bid 90 → SELL_YES edge ~10 (model says undervalued)
    row = make_row(model_prob=0.80, yes_bid=0.90, yes_ask=0.10)
    side, _ = actionable_edge(row, LEGACY_POLICY)
    assert side == "BUY_YES"


def test_executor_selective_profile_blocks_buy_yes_signal(tmp_path):
    """End-to-end through executor: with selective's policy a strong BUY_YES
    setup becomes 'no rows above min_edge_cents' (BUY_YES filtered out by
    policy, no NO-side quote, no SELL_YES edge)."""
    import sqlite3
    from src.db import connect
    from src.executor import BOT_PROFILES, Executor

    db = connect(tmp_path / "t.db")
    try:
        ex = Executor(db, trader=None, live=False, profile=BOT_PROFILES["selective"])
        # Big BUY_YES: model 80c, ask 25c. No NO-side quote.
        row = make_row(model_prob=0.80, yes_bid=0.20, yes_ask=0.25)
        d = ex.handle_poll([row])
        assert d.placed is False
        assert "min_edge_cents" in d.reason
    finally:
        db.close()


def test_executor_selective_profile_places_buy_no(tmp_path):
    """With selective's policy + a profitable NO ask, actionable_edge yields
    BUY_NO and the executor places a side='no' buy at no_ask cents."""
    from src.db import connect
    from src.executor import BOT_PROFILES, Executor

    db = connect(tmp_path / "t.db")
    try:
        ex = Executor(db, trader=None, live=False, profile=BOT_PROFILES["selective"])
        row = make_row(model_prob=0.10, yes_bid=0.20, yes_ask=0.25, no_ask=0.50)
        d = ex.handle_poll([row])
        assert d.placed is True
        assert d.ticket is not None
        assert d.ticket.action == "buy"
        assert d.ticket.side == "no"
        assert d.ticket.limit_price_cents == 50
    finally:
        db.close()


# ---- PR #5: calibrated mp band gate ----

def test_band_gate_blocks_buy_yes_above_upper_bound():
    """model_prob 0.86 (≥ 0.85 upper bound) → BUY_YES blocked even with edge."""
    row = make_row(model_prob=0.86, yes_bid=0.20, yes_ask=0.25)
    side, _ = actionable_edge(row)
    assert side == "NONE"


def test_band_gate_blocks_buy_no_below_lower_bound():
    """model_prob 0.04 (< 0.05 lower bound) → BUY_NO blocked under selective.
    Use a small yes_bid so SELL_YES edge is also negative — isolates the
    band gate as the only thing that could have allowed BUY_NO."""
    row = make_row(model_prob=0.04, yes_bid=0.02, yes_ask=0.05, no_ask=0.50)
    pol = SidePolicy(allow_buy_yes=False, allow_buy_no=True, sell_yes_to_close_only=True)
    side, _ = actionable_edge(row, pol)
    assert side != "BUY_NO"


def test_band_gate_does_not_block_sell_yes():
    """SELL_YES must remain reachable even outside the band — closing
    inventory is unconditional regardless of where the model sits."""
    row = make_row(model_prob=0.86, yes_bid=0.95, yes_ask=0.99)  # mp above band
    side, _ = actionable_edge(row)
    # No SELL edge (model 86 vs bid 95, 95-86-fee = ~7c) → SELL_YES wins.
    assert side == "SELL_YES"


def test_band_gate_uses_calibrated_when_available():
    """If row.model_prob_calibrated is set and inside the band, the gate
    passes even when raw model_prob is outside it."""
    row = make_row(model_prob=0.90, yes_bid=0.20, yes_ask=0.25)
    # Calibrator pulls the mp back inside the band.
    row = PollRow(
        ts_ms=row.ts_ms, event_ticker=row.event_ticker, market_ticker=row.market_ticker,
        strike=row.strike, spot=row.spot, sigma=row.sigma, minutes_left=row.minutes_left,
        model_prob=row.model_prob, yes_bid=row.yes_bid, yes_ask=row.yes_ask,
        yes_bid_size=row.yes_bid_size, yes_ask_size=row.yes_ask_size,
        no_bid=row.no_bid, no_ask=row.no_ask, volume=row.volume,
        edge_cents=row.edge_cents, model_prob_calibrated=0.70,
    )
    side, _ = actionable_edge(row)
    assert side == "BUY_YES"


def test_calibrated_prob_shrinks_buy_no_edge():
    """When the calibrator raises model_prob (toward YES), BUY_NO edge must
    shrink because P(NO) = 1 - calibrated_mp is lower than 1 - raw_mp.
    This is the core fix: edge computation must use calibrated probability."""
    base = make_row(model_prob=0.30, yes_bid=0.20, yes_ask=0.25, no_ask=0.25)
    # Calibrator says YES is actually 0.45, not 0.30.
    calibrated = PollRow(
        ts_ms=base.ts_ms, event_ticker=base.event_ticker,
        market_ticker=base.market_ticker, strike=base.strike, spot=base.spot,
        sigma=base.sigma, minutes_left=base.minutes_left,
        model_prob=base.model_prob, yes_bid=base.yes_bid, yes_ask=base.yes_ask,
        yes_bid_size=base.yes_bid_size, yes_ask_size=base.yes_ask_size,
        no_bid=base.no_bid, no_ask=base.no_ask, volume=base.volume,
        edge_cents=base.edge_cents, model_prob_calibrated=0.45,
    )
    pol = SidePolicy(allow_buy_yes=False, allow_buy_no=True, sell_yes_to_close_only=True)
    _, edge_raw = actionable_edge(base, pol)
    _, edge_cal = actionable_edge(calibrated, pol)
    # Raw: (1-0.30)*100 - 25 - fee = 45 - fee
    # Cal: (1-0.45)*100 - 25 - fee = 30 - fee
    assert edge_cal < edge_raw
    fee = kalshi_fee_cents(25, 1)
    assert edge_raw == pytest.approx(45.0 - fee)
    assert edge_cal == pytest.approx(30.0 - fee)


def test_calibrated_prob_expands_buy_no_edge():
    """When the calibrator lowers model_prob (away from YES), BUY_NO edge
    grows because P(NO) = 1 - calibrated_mp is higher."""
    base = make_row(model_prob=0.30, yes_bid=0.20, yes_ask=0.25, no_ask=0.50)
    calibrated = PollRow(
        ts_ms=base.ts_ms, event_ticker=base.event_ticker,
        market_ticker=base.market_ticker, strike=base.strike, spot=base.spot,
        sigma=base.sigma, minutes_left=base.minutes_left,
        model_prob=base.model_prob, yes_bid=base.yes_bid, yes_ask=base.yes_ask,
        yes_bid_size=base.yes_bid_size, yes_ask_size=base.yes_ask_size,
        no_bid=base.no_bid, no_ask=base.no_ask, volume=base.volume,
        edge_cents=base.edge_cents, model_prob_calibrated=0.20,
    )
    pol = SidePolicy(allow_buy_yes=False, allow_buy_no=True, sell_yes_to_close_only=True)
    _, edge_raw = actionable_edge(base, pol)
    _, edge_cal = actionable_edge(calibrated, pol)
    assert edge_cal > edge_raw


def test_selective_buy_no_policy_blocks_outside_price_band():
    pol = SidePolicy(
        allow_buy_yes=False,
        allow_buy_no=True,
        buy_no_min_ask=0.50,
        buy_no_max_ask=0.75,
    )
    cheap = make_row(model_prob=0.10, yes_bid=0.02, yes_ask=0.25, no_ask=0.49)
    rich = make_row(model_prob=0.10, yes_bid=0.02, yes_ask=0.25, no_ask=0.76)
    ok = make_row(model_prob=0.10, yes_bid=0.02, yes_ask=0.25, no_ask=0.50)
    assert actionable_edge(cheap, pol)[0] == "NONE"
    assert actionable_edge(rich, pol)[0] == "NONE"
    assert actionable_edge(ok, pol)[0] == "BUY_NO"


def test_selective_buy_no_policy_uses_sigma_override():
    pol = SidePolicy(allow_buy_yes=False, allow_buy_no=True, buy_no_min_sigma=0.30)
    row = make_row(model_prob=0.10, yes_bid=0.02, yes_ask=0.25, no_ask=0.50, sigma=0.29)
    assert actionable_edge(row, pol)[0] == "NONE"


def test_buy_gate_blocks_low_vol_unit_distance():
    # Percent distance passes: 0.125% > 0.10%. Expected-move distance fails.
    row = make_row(
        model_prob=0.80,
        yes_bid=0.20,
        yes_ask=0.25,
        strike=80_100.0,
        spot=80_000.0,
        sigma=0.60,
        minutes_left=30.0,
    )
    assert actionable_edge(row)[0] == "NONE"


def test_calibrated_prob_grows_sell_no_edge():
    """When calibrator raises model_prob, SELL_NO edge grows because our NO
    position is worth less (lower P(NO)), so selling at no_bid is better."""
    base = make_row(model_prob=0.30, yes_bid=0.05, yes_ask=0.40,
                    no_bid=0.80, no_ask=0.95)
    calibrated = PollRow(
        ts_ms=base.ts_ms, event_ticker=base.event_ticker,
        market_ticker=base.market_ticker, strike=base.strike, spot=base.spot,
        sigma=base.sigma, minutes_left=base.minutes_left,
        model_prob=base.model_prob, yes_bid=base.yes_bid, yes_ask=base.yes_ask,
        yes_bid_size=base.yes_bid_size, yes_ask_size=base.yes_ask_size,
        no_bid=base.no_bid, no_ask=base.no_ask, volume=base.volume,
        edge_cents=base.edge_cents, model_prob_calibrated=0.45,
    )
    _, edge_raw = actionable_edge(base)
    _, edge_cal = actionable_edge(calibrated)
    # Both should be SELL_NO. Cal should be larger because P(NO) is lower.
    fee = kalshi_fee_cents(80, 1)
    # Raw: 80 - (1-0.30)*100 - fee = 80 - 70 - fee = 10 - fee
    # Cal: 80 - (1-0.45)*100 - fee = 80 - 55 - fee = 25 - fee
    assert edge_cal > edge_raw
    assert edge_raw == pytest.approx(10.0 - fee)
    assert edge_cal == pytest.approx(25.0 - fee)


def test_shadow_signal_includes_no_side_edges():
    """Shadow signals must now include BUY_NO and SELL_NO edge fields."""
    row = make_row(model_prob=0.30, yes_bid=0.05, yes_ask=0.40,
                   no_bid=0.80, no_ask=0.25)
    sigs = build_shadow_signals([row])
    assert len(sigs) == 1
    s = sigs[0]
    assert s.no_bid == pytest.approx(0.80)
    assert s.no_ask == pytest.approx(0.25)
    # BUY_NO: (1-0.30)*100 - 25 - fee(25) = 45 - fee
    assert s.buy_no_edge_net_cents is not None and s.buy_no_edge_net_cents > 0
    # SELL_NO: 80 - (1-0.30)*100 - fee(80) = 10 - fee
    assert s.sell_no_edge_net_cents is not None and s.sell_no_edge_net_cents > 0


def test_shadow_signal_respects_policy():
    """build_shadow_signals with selective policy should yield BUY_NO as
    chosen_side, not BUY_YES."""
    row = make_row(model_prob=0.30, yes_bid=0.20, yes_ask=0.25, no_ask=0.25)
    pol = SidePolicy(allow_buy_yes=False, allow_buy_no=True, sell_yes_to_close_only=True)
    sigs = build_shadow_signals([row], policy=pol)
    assert len(sigs) == 1
    assert sigs[0].chosen_side == "BUY_NO"
    # Same row with legacy policy should yield BUY_YES (or NONE if gates block)
    sigs_legacy = build_shadow_signals([row])
    assert sigs_legacy[0].chosen_side != "BUY_NO"


def test_shadow_sigma_flag_uses_policy_specific_buy_no_floor():
    """Selective BUY_NO telemetry should not inherit BUY_YES's higher sigma
    floor. This keeps shadow scans aligned with the executor's policy."""
    row = make_row(
        model_prob=0.10,
        yes_bid=0.02,
        yes_ask=0.25,
        no_ask=0.50,
        sigma=0.30,
    )
    pol = SidePolicy(
        allow_buy_yes=False,
        allow_buy_no=True,
        sell_yes_to_close_only=True,
        buy_no_min_sigma=0.30,
        buy_no_min_ask=0.50,
        buy_no_max_ask=0.75,
    )
    sigs = build_shadow_signals([row], policy=pol)
    assert len(sigs) == 1
    assert sigs[0].chosen_side == "BUY_NO"
    assert sigs[0].gate_sigma_passed == 1


def test_band_gate_inclusive_at_lo_exclusive_at_hi():
    """Band semantics: [0.05, 0.85). Exact 0.05 passes; exact 0.85 blocks."""
    # At mp=0.05, NO is "worth" 95c. no_ask=0.50 → BUY_NO net = 95-50-fee(50)=43c.
    row_at_lo = make_row(model_prob=0.05, yes_bid=0.01, yes_ask=0.02, no_ask=0.50)
    pol = SidePolicy(allow_buy_yes=False, allow_buy_no=True, sell_yes_to_close_only=True)
    side_lo, _ = actionable_edge(row_at_lo, pol)
    assert side_lo == "BUY_NO"  # 0.05 is inside the band
    # Exclusive at hi: BUY_YES at exactly 0.85 is band-blocked. yes_bid=0.20
    # gives no SELL edge (20-85-fee = negative), so the result must be NONE.
    row_at_hi = make_row(model_prob=0.85, yes_bid=0.20, yes_ask=0.25)
    side_hi, _ = actionable_edge(row_at_hi)
    assert side_hi == "NONE"
