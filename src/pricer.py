"""Lognormal pricer for binary above/below contracts.

Model: S_T = S_0 * exp((-0.5 * sigma^2) * T + sigma * sqrt(T) * Z),  Z ~ N(0, 1)
       (drift = 0; appropriate for short-dated BTC with no carry)

P(S_T > K) = Phi( (ln(S_0/K) - 0.5 * sigma^2 * T) / (sigma * sqrt(T)) )

`sigma` is annualized vol; `T` is time-to-settlement in years.
"""

from __future__ import annotations

import math

from scipy.stats import norm

SECONDS_PER_YEAR = 365.0 * 24.0 * 60.0 * 60.0
MINUTES_PER_YEAR = 365.0 * 24.0 * 60.0


def years_from_minutes(minutes: float) -> float:
    return minutes / MINUTES_PER_YEAR


def prob_above_strike(spot: float, strike: float, sigma: float, minutes_left: float) -> float:
    """P(S_T > K) under zero-drift lognormal."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if sigma < 0:
        raise ValueError("sigma must be non-negative")
    if minutes_left < 0:
        raise ValueError("minutes_left must be non-negative")

    # Degenerate cases: no time left or no vol → deterministic.
    if minutes_left == 0 or sigma == 0:
        if spot > strike:
            return 1.0
        if spot < strike:
            return 0.0
        return 0.5

    T = years_from_minutes(minutes_left)
    vol_t = sigma * math.sqrt(T)
    d = (math.log(spot / strike) - 0.5 * sigma * sigma * T) / vol_t
    return float(norm.cdf(d))


def prob_below_strike(spot: float, strike: float, sigma: float, minutes_left: float) -> float:
    return 1.0 - prob_above_strike(spot, strike, sigma, minutes_left)


def edge_cents(model_prob: float, kalshi_yes_ask_cents: float) -> float:
    """Positive edge = model thinks YES is underpriced at the ask."""
    return model_prob * 100.0 - kalshi_yes_ask_cents
