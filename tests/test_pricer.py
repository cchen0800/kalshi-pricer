"""Sanity checks for the lognormal pricer.

Reference values computed by hand from N(d2) with r = q = 0:
    d = (ln(S/K) - 0.5*sigma^2*T) / (sigma*sqrt(T))
    P(S_T > K) = Phi(d)
"""

import math

import pytest
from scipy.stats import norm

from src.pricer import (
    prob_above_strike,
    prob_above_strike_path_dependent,
    prob_below_strike,
    edge_cents,
    years_from_minutes,
)
from src.pricer import SECONDS_PER_YEAR


def test_atm_60min_50pct_vol():
    # Spot == Strike, 60 min to expiry, sigma = 50% annualized.
    spot = 60_000.0
    strike = 60_000.0
    sigma = 0.50
    minutes = 60.0

    T = years_from_minutes(minutes)
    expected = norm.cdf(-0.5 * sigma * math.sqrt(T))  # d collapses to -0.5*sigma*sqrt(T)

    p = prob_above_strike(spot, strike, sigma, minutes)
    assert p == pytest.approx(expected, abs=1e-9)
    # ATM with positive vol → just under 0.5 (variance drag).
    assert 0.49 < p < 0.50


def test_deep_itm_call_is_near_one():
    # Strike well below spot → model should be very confident.
    p = prob_above_strike(spot=60_000, strike=50_000, sigma=0.6, minutes_left=60)
    assert p > 0.999


def test_deep_otm_call_is_near_zero():
    p = prob_above_strike(spot=60_000, strike=80_000, sigma=0.6, minutes_left=60)
    assert p < 0.001


def test_above_and_below_sum_to_one():
    p_up = prob_above_strike(60_000, 61_000, 0.55, 45)
    p_dn = prob_below_strike(60_000, 61_000, 0.55, 45)
    assert p_up + p_dn == pytest.approx(1.0, abs=1e-12)


def test_zero_time_is_deterministic():
    assert prob_above_strike(60_000, 59_999, 0.5, 0) == 1.0
    assert prob_above_strike(60_000, 60_001, 0.5, 0) == 0.0
    assert prob_above_strike(60_000, 60_000, 0.5, 0) == 0.5


def test_zero_vol_is_deterministic():
    assert prob_above_strike(60_000, 59_999, 0.0, 60) == 1.0
    assert prob_above_strike(60_000, 60_001, 0.0, 60) == 0.0


def test_higher_vol_pushes_otm_call_up():
    # OTM call probability is monotonically increasing in vol.
    low = prob_above_strike(60_000, 62_000, 0.30, 60)
    high = prob_above_strike(60_000, 62_000, 0.90, 60)
    assert high > low


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        prob_above_strike(-1, 100, 0.5, 60)
    with pytest.raises(ValueError):
        prob_above_strike(100, 100, -0.1, 60)
    with pytest.raises(ValueError):
        prob_above_strike(100, 100, 0.5, -1)


def test_edge_cents_sign():
    # Model says 60%, market asks 55¢ → +5¢ edge.
    assert edge_cents(0.60, 55) == pytest.approx(5.0)
    # Model says 40%, market asks 55¢ → -15¢ edge.
    assert edge_cents(0.40, 55) == pytest.approx(-15.0)


# ---------- prob_above_strike_path_dependent ----------

def test_path_dep_atm_outside_window_close_to_endpoint():
    # T=300s (5min), W=60s. Effective τ = 300 - 40 = 260s vs endpoint 300s.
    # ATM should be slightly below 0.5 (no drift, but tiny variance → close to 0.5).
    p = prob_above_strike_path_dependent(
        spot=100_000, strike=100_000, sigma=0.5,
        seconds_to_settlement=300.0,
    )
    assert p == pytest.approx(0.5, abs=1e-6)  # symmetric around mean=spot


def test_path_dep_otm_short_window_lower_than_endpoint():
    # Path-dependent should give a LOWER probability than the standard endpoint
    # pricer for OTM calls near expiry, because Var[avg] < Var[endpoint].
    spot, strike, sigma, T = 100_000.0, 100_500.0, 0.6, 60.0  # T=W
    p_path = prob_above_strike_path_dependent(
        spot=spot, strike=strike, sigma=sigma, seconds_to_settlement=T,
    )
    p_std = prob_above_strike(spot, strike, sigma, minutes_left=T / 60)
    assert p_path < p_std


def test_path_dep_recovers_endpoint_when_T_far_exceeds_W():
    # T=3600s (1hr), W=60s. Effective τ = 3600-40 = 3560 vs 3600. Tiny diff.
    spot, strike, sigma = 100_000.0, 101_000.0, 0.5
    p_path = prob_above_strike_path_dependent(
        spot=spot, strike=strike, sigma=sigma, seconds_to_settlement=3600.0,
    )
    p_std_normal = norm.cdf((spot - strike) / (spot * sigma * math.sqrt(3600.0 / SECONDS_PER_YEAR)))
    # Compare path-dependent against same normal model with full T (no averaging correction).
    # The averaging knocks ~1% off variance → very close numerically.
    assert abs(p_path - p_std_normal) < 0.01


def test_path_dep_zero_time_is_deterministic():
    # T=0 with no realized → mean = spot, Var = 0.
    assert prob_above_strike_path_dependent(
        spot=100_000, strike=99_999, sigma=0.5, seconds_to_settlement=0.0,
    ) == 1.0
    assert prob_above_strike_path_dependent(
        spot=100_000, strike=100_001, sigma=0.5, seconds_to_settlement=0.0,
    ) == 0.0


def test_path_dep_locked_in_realized_dominates_at_end():
    # T=1s left in 60s window with realized_avg = 110_000. Even if spot just dipped
    # to 100_000, the locked 59s of the average pin us above 109_000 → near-certain.
    p = prob_above_strike_path_dependent(
        spot=100_000, strike=109_000, sigma=0.5,
        seconds_to_settlement=1.0,
        realized_partial_avg=110_000,
    )
    # mean = 59/60 * 110000 + 1/60 * 100000 = 109_833. Way above 109_000.
    assert p > 0.99


def test_path_dep_realized_above_strike_offsets_low_spot():
    # Realized portion strongly ITM, current spot below strike, halfway through.
    # mean = 0.5 * 102_000 + 0.5 * 99_000 = 100_500. K=100_000 → mean above K.
    p = prob_above_strike_path_dependent(
        spot=99_000, strike=100_000, sigma=0.5,
        seconds_to_settlement=30.0,
        realized_partial_avg=102_000,
    )
    assert p > 0.5


def test_path_dep_inside_window_no_realized_matches_T_eq_W_outside():
    # Just-entered window (T=W) with no realized data: should equal the outside formula at T=W.
    spot, strike, sigma, W = 100_000.0, 100_300.0, 0.5, 60.0
    p_inside = prob_above_strike_path_dependent(
        spot=spot, strike=strike, sigma=sigma,
        seconds_to_settlement=W - 1e-6,
        realized_partial_avg=None,
        averaging_window_seconds=W,
    )
    p_outside = prob_above_strike_path_dependent(
        spot=spot, strike=strike, sigma=sigma,
        seconds_to_settlement=W,
        averaging_window_seconds=W,
    )
    assert abs(p_inside - p_outside) < 1e-3


def test_path_dep_invalid_inputs_raise():
    with pytest.raises(ValueError):
        prob_above_strike_path_dependent(
            spot=0, strike=100, sigma=0.5, seconds_to_settlement=60,
        )
    with pytest.raises(ValueError):
        prob_above_strike_path_dependent(
            spot=100, strike=100, sigma=-0.1, seconds_to_settlement=60,
        )
    with pytest.raises(ValueError):
        prob_above_strike_path_dependent(
            spot=100, strike=100, sigma=0.5, seconds_to_settlement=-1,
        )
    with pytest.raises(ValueError):
        prob_above_strike_path_dependent(
            spot=100, strike=100, sigma=0.5, seconds_to_settlement=30,
            realized_partial_avg=-50,
        )
