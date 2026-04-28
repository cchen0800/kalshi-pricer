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
    prob_below_strike,
    edge_cents,
    years_from_minutes,
)


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
