import pytest

from src.realized import RealizedAverager


def test_empty_returns_none():
    a = RealizedAverager()
    assert a.average(0.0, 60.0) is None


def test_single_pre_window_sample_holds_through_window():
    a = RealizedAverager()
    a.add(epoch_s=0.0, price=100.0)
    # Window [60, 120]: nothing inside, but the one pre-window sample carries.
    assert a.average(60.0, 120.0) == pytest.approx(100.0)


def test_two_samples_inside_window_step_function():
    a = RealizedAverager()
    a.add(epoch_s=0.0, price=100.0)   # leading
    a.add(epoch_s=70.0, price=110.0)  # mid-window
    # Window [60, 120]: 100 from t=60 to t=70 (10s), 110 from t=70 to t=120 (50s).
    expected = (10 * 100 + 50 * 110) / 60
    assert a.average(60.0, 120.0) == pytest.approx(expected)


def test_no_pre_window_uses_first_in_window():
    a = RealizedAverager()
    a.add(epoch_s=70.0, price=110.0)
    # Window [60, 120]: no pre-window sample, fall back to extrapolating the
    # first in-window sample backwards.
    assert a.average(60.0, 120.0) == pytest.approx(110.0)


def test_no_useful_sample_returns_none():
    a = RealizedAverager()
    a.add(epoch_s=200.0, price=100.0)  # only sample is AFTER the window ends
    assert a.average(0.0, 60.0) is None


def test_zero_or_negative_window_returns_none():
    a = RealizedAverager()
    a.add(epoch_s=0.0, price=100.0)
    assert a.average(60.0, 60.0) is None
    assert a.average(60.0, 30.0) is None


def test_non_monotonic_timestamp_ignored():
    a = RealizedAverager()
    a.add(epoch_s=10.0, price=100.0)
    a.add(epoch_s=5.0, price=999.0)   # earlier — ignored
    a.add(epoch_s=10.0, price=998.0)  # equal — ignored
    assert len(a) == 1
    assert a.average(0.0, 60.0) == pytest.approx(100.0)


def test_rejects_non_positive_price():
    a = RealizedAverager()
    with pytest.raises(ValueError):
        a.add(epoch_s=0.0, price=0.0)
    with pytest.raises(ValueError):
        a.add(epoch_s=0.0, price=-1.0)


def test_pruning_keeps_one_leading_sample():
    a = RealizedAverager(keep_seconds=100.0)
    a.add(epoch_s=0.0, price=100.0)
    a.add(epoch_s=50.0, price=200.0)
    a.add(epoch_s=200.0, price=300.0)  # cutoff = 200-100 = 100. The 50.0 sample stays as leading.
    assert len(a) == 2
    # 100.0 sample at t=0 is dropped (older than cutoff and not the leading one).
    assert a.average(150.0, 250.0) == pytest.approx(
        (50 * 200 + 50 * 300) / 100
    )


def test_three_samples_full_average():
    a = RealizedAverager()
    a.add(epoch_s=0.0, price=100.0)
    a.add(epoch_s=80.0, price=200.0)   # at window start
    a.add(epoch_s=110.0, price=300.0)  # mid-window
    # Window [80, 140]: 200 from t=80 to t=110 (30s), 300 from t=110 to t=140 (30s).
    expected = (30 * 200 + 30 * 300) / 60
    assert a.average(80.0, 140.0) == pytest.approx(expected)
