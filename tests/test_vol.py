import math
import random

import pytest

from src.vol import annualized_vol, yang_zhang_vol, MINUTES_PER_YEAR


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
    random.seed(0)
    series = [60_000.0]
    for _ in range(60):
        series.append(series[-1] * math.exp(random.gauss(0, 0.001)))
    sigma = annualized_vol(series)
    assert 0.5 < sigma < 1.0  # 50–100% annualized is normal BTC realized


# --- Yang-Zhang ---

def test_yz_constant_ohlc_is_zero():
    bars = [(100.0, 100.0, 100.0, 100.0)] * 10
    assert yang_zhang_vol(bars) == 0.0


def test_yz_too_few_bars_raises():
    with pytest.raises(ValueError):
        yang_zhang_vol([(100.0, 100.0, 100.0, 100.0)])


def test_yz_rejects_non_positive_prices():
    with pytest.raises(ValueError):
        yang_zhang_vol([(100.0, 100.0, 100.0, 100.0), (0.0, 100.0, 100.0, 100.0)])


def test_yz_degenerate_close_only_matches_overnight_var():
    # When H = L = O = C every bar, RS = 0 and OC = 0, so YZ collapses to var(overnight),
    # where overnight return is ln(O_i / C_{i-1}) = ln(C_i / C_{i-1}).
    closes = [100.0, 101.0, 100.0, 102.0, 101.0]
    bars = [(c, c, c, c) for c in closes]  # O=H=L=C each bar
    yz = yang_zhang_vol(bars)
    cc = annualized_vol(closes)
    assert yz == pytest.approx(cc, rel=1e-12)


def test_yz_uses_range_information():
    # Two series with identical closes but different intra-bar ranges.
    # The wider-ranged one should report higher YZ vol.
    closes = [100.0, 100.5, 100.0, 100.5, 100.0, 100.5]
    narrow = [(c, c + 0.05, c - 0.05, c) for c in closes]
    wide = [(c, c + 1.0, c - 1.0, c) for c in closes]
    assert yang_zhang_vol(wide) > yang_zhang_vol(narrow)


def test_yz_btc_like_magnitudes_realistic():
    # Synthetic 1m bars with realistic intra-bar range.
    random.seed(1)
    bars: list[tuple[float, float, float, float]] = []
    c_prev = 60_000.0
    for _ in range(60):
        o = c_prev * math.exp(random.gauss(0, 0.0001))   # tiny gap
        c = o * math.exp(random.gauss(0, 0.0008))         # ~0.08% bar move
        # High/low respect O and C and add a wick.
        wick = abs(random.gauss(0, 0.0005)) * o
        h = max(o, c) + wick
        l = min(o, c) - wick
        bars.append((o, h, l, c))
        c_prev = c
    sigma = yang_zhang_vol(bars)
    assert 0.3 < sigma < 1.5  # plausible BTC annualized realized band
