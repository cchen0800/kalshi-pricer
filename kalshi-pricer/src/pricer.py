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


def prob_above_strike(
    spot: float,
    strike: float,
    sigma: float,
    minutes_left: float,
    *,
    drift_per_year: float = 0.0,
) -> float:
    """P(S_T > K) under lognormal with optional log-return drift μ.

    drift_per_year=0.0 (default) recovers the original zero-drift formula and
    preserves all callers that don't pass a drift. Wired in PR #6C; no
    estimator in v0 — pass a pinned value if you want it active.
    """
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
    d = (math.log(spot / strike) + (drift_per_year - 0.5 * sigma * sigma) * T) / vol_t
    return float(norm.cdf(d))


def prob_below_strike(spot: float, strike: float, sigma: float, minutes_left: float) -> float:
    return 1.0 - prob_above_strike(spot, strike, sigma, minutes_left)


def edge_cents(model_prob: float, kalshi_yes_ask_cents: float) -> float:
    """Positive edge = model thinks YES is underpriced at the ask."""
    return model_prob * 100.0 - kalshi_yes_ask_cents


def prob_above_strike_path_dependent(
    *,
    spot: float,
    strike: float,
    sigma: float,
    seconds_to_settlement: float,
    realized_partial_avg: float | None = None,
    averaging_window_seconds: float = 60.0,
    drift_per_year: float = 0.0,
) -> float:
    """P(BRTI time-average over final W seconds > strike).

    Kalshi KXBTCD settles on the average of the CME CF Bitcoin Reference Rate
    over the final 60 seconds, not on a point reading. Two consequences:

      1. Outside the window (T > W): the *averaged* settlement has variance
         σ²·(T − 2W/3), not σ²·T. The 2W/3 reduction is meaningful only
         in the last few minutes; it shaves ~67% of the endpoint variance at
         T=W and ~13% at T=5W.

      2. Inside the window (T ≤ W): part of the average is locked in. With
         w_r = elapsed/W and w_n = T/W,
             E[A] = w_r·realized_avg + w_n·spot
             Var[A] = w_n²·σ²·T / 3
         The remaining-window contribution is the time-average of a Brownian
         path over [0, T], whose variance is σ²·T/3 (one-third of endpoint).

    Modelled as zero-drift arithmetic Brownian on absolute prices — exact to
    leading order at BTC/short-window scales (σ·√(60s) ≈ 0.07% on $100k).
    Pass `realized_partial_avg = None` if you have no in-window samples;
    we degrade by assuming the realized portion equals current spot.
    """
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if sigma < 0:
        raise ValueError("sigma must be non-negative")
    if seconds_to_settlement < 0:
        raise ValueError("seconds_to_settlement must be non-negative")
    if averaging_window_seconds <= 0:
        raise ValueError("averaging_window_seconds must be positive")

    W = averaging_window_seconds
    T = seconds_to_settlement
    sigma_abs = spot * sigma  # $ per √year

    if T >= W:
        # Drift contribution to the time-averaged price over the averaging
        # window: spot * μ * (T - W/2) / SECONDS_PER_YEAR. The "-W/2" picks
        # the midpoint of the averaging window as the representative time.
        # At μ=0 (default) this collapses to the zero-drift mean=spot.
        T_eff_year = max(0.0, (T - W / 2.0) / SECONDS_PER_YEAR)
        mean = spot * (1.0 + drift_per_year * T_eff_year)
        tau_years = max(0.0, (T - 2.0 * W / 3.0) / SECONDS_PER_YEAR)
    else:
        w_n = T / W
        w_r = 1.0 - w_n
        if realized_partial_avg is None:
            realized = spot
        elif realized_partial_avg <= 0:
            raise ValueError("realized_partial_avg must be positive")
        else:
            realized = realized_partial_avg
        # Inside the averaging window: only the unrealized portion is exposed
        # to drift. Drift over [0, T] integrated within the average has mean
        # contribution w_n * spot * μ * (T/2) / SECONDS_PER_YEAR.
        drift_term = w_n * spot * drift_per_year * (T / 2.0) / SECONDS_PER_YEAR
        mean = w_r * realized + w_n * spot + drift_term
        tau_years = (w_n * w_n) * T / (3.0 * SECONDS_PER_YEAR)

    if tau_years <= 0 or sigma_abs == 0:
        if mean > strike:
            return 1.0
        if mean < strike:
            return 0.0
        return 0.5

    sd = sigma_abs * math.sqrt(tau_years)
    return float(norm.cdf((mean - strike) / sd))
