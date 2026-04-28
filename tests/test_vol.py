import math

import pytest

from src.vol import annualized_vol, MINUTES_PER_YEAR


def test_constant_series_is_zero_vol():
    assert annualized_vol([100.0] * 10) == 0.0


def test_known_two_point_jump():
    # One return of ln(1.01) ≈ 0.00995. With n=1, sample var requires n>1, so we expect 0.
    # Use 3 points instead so we have 2 returns.
    closes = [100.0, 101.0, 100.0]
    # returns: ln(1.01), ln(100/101) = -ln(1.01)
    r = math.log(1.01)
    # mean = 0, var = (r^2 + r^2) / (n-1) = 2r^2 / 1 = 2r^2
    expected_minutely = math.sqrt(2 * r * r)
    expected = expected_minutely * math.sqrt(MINUTES_PER_YEAR)
    assert annualized_vol(closes) == pytest.approx(expected, rel=1e-12)


def test_too_few_points_raises():
    with pytest.raises(ValueError):
        annualized_vol([100.0])


def test_btc_like_magnitudes_are_in_realistic_range():
    # Synthetic 1m series with ~0.1% per-minute std → annualized ~ 0.001 * sqrt(525600) ≈ 0.725 (72%).
    import random

    random.seed(0)
    series = [60_000.0]
    for _ in range(60):
        series.append(series[-1] * math.exp(random.gauss(0, 0.001)))
    sigma = annualized_vol(series)
    assert 0.5 < sigma < 1.0  # 50–100% annualized is normal BTC realized
