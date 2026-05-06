"""Tests for src/calibration.py."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest

from src.calibration import (
    IsotonicCalibrator,
    fit,
    identity,
    load,
    save,
)


def test_identity_maps_to_self():
    cal = identity()
    for p in (0.0, 0.1, 0.5, 0.9, 1.0):
        assert cal.apply(p) == pytest.approx(p, abs=1e-9)


def test_identity_n_train_zero():
    assert identity().n_train == 0


def test_apply_clamps_below_first_knot():
    cal = IsotonicCalibrator(xs=(0.2, 0.8), ys=(0.1, 0.9))
    assert cal.apply(0.0) == pytest.approx(0.1)
    assert cal.apply(0.1) == pytest.approx(0.1)


def test_apply_clamps_above_last_knot():
    cal = IsotonicCalibrator(xs=(0.2, 0.8), ys=(0.1, 0.9))
    assert cal.apply(0.9) == pytest.approx(0.9)
    assert cal.apply(1.0) == pytest.approx(0.9)


def test_apply_linear_interp_between_knots():
    cal = IsotonicCalibrator(xs=(0.0, 1.0), ys=(0.0, 1.0))
    assert cal.apply(0.25) == pytest.approx(0.25)
    assert cal.apply(0.75) == pytest.approx(0.75)
    cal2 = IsotonicCalibrator(xs=(0.0, 0.5, 1.0), ys=(0.0, 0.4, 0.5))
    # halfway between (0, 0) and (0.5, 0.4) → 0.2
    assert cal2.apply(0.25) == pytest.approx(0.2)
    # halfway between (0.5, 0.4) and (1.0, 0.5) → 0.45
    assert cal2.apply(0.75) == pytest.approx(0.45)


def test_constructor_rejects_non_monotone_y():
    with pytest.raises(ValueError, match="non-decreasing"):
        IsotonicCalibrator(xs=(0.0, 1.0), ys=(0.5, 0.0))


def test_constructor_rejects_non_strict_x():
    with pytest.raises(ValueError, match="ascending"):
        IsotonicCalibrator(xs=(0.0, 0.0), ys=(0.0, 1.0))


def test_json_roundtrip(tmp_path: Path):
    cal = IsotonicCalibrator(
        xs=(0.0, 0.3, 0.7, 1.0),
        ys=(0.0, 0.2, 0.6, 1.0),
        n_train=1234,
        fit_brier=0.1,
        cal_brier=0.08,
    )
    p = tmp_path / "cal.json"
    save(cal, p)
    loaded = load(p)
    assert loaded.xs == cal.xs
    assert loaded.ys == cal.ys
    assert loaded.n_train == 1234
    assert loaded.fit_brier == pytest.approx(0.1)
    assert loaded.cal_brier == pytest.approx(0.08)


def test_load_missing_file_returns_identity(tmp_path: Path, caplog):
    cal = load(tmp_path / "does-not-exist.json")
    assert cal.n_train == 0
    assert cal.apply(0.42) == pytest.approx(0.42)


def test_load_bad_json_falls_back_to_identity(tmp_path: Path, caplog):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    cal = load(p)
    assert cal.n_train == 0
    assert cal.apply(0.5) == pytest.approx(0.5)


def test_fit_recovers_identity_on_calibrated_data():
    """If outcomes already match probs (perfect calibration), the fit should
    be approximately the identity, give or take sampling noise."""
    rng = random.Random(0)
    probs = []
    outcomes = []
    for _ in range(5000):
        p = rng.random()
        probs.append(p)
        outcomes.append(1 if rng.random() < p else 0)
    cal = fit(probs, outcomes)
    # Sample at a few points; tolerance is loose because of sampling noise.
    for q in (0.1, 0.3, 0.5, 0.7, 0.9):
        assert abs(cal.apply(q) - q) < 0.05, f"identity recovery failed at q={q}: got {cal.apply(q)}"


def test_fit_corrects_systematic_overprediction():
    """Generate biased probabilities (model says p, truth is p/2 in low band)
    and verify the calibrator pulls them down."""
    rng = random.Random(42)
    probs = []
    outcomes = []
    for _ in range(10000):
        p = rng.random()
        probs.append(p)
        # If model says p, true rate is p/2 in [0, 0.5], else p.
        true_rate = p * 0.5 if p < 0.5 else p
        outcomes.append(1 if rng.random() < true_rate else 0)
    cal = fit(probs, outcomes)
    # At p=0.4 the truth is 0.2; calibrator should map closer to 0.2 than to 0.4.
    out = cal.apply(0.4)
    assert out < 0.30, f"expected calibrator to shrink 0.4 below 0.30, got {out}"
    assert out > 0.10
    # At p=0.8 the truth is 0.8; calibrator should leave it alone.
    out_high = cal.apply(0.8)
    assert abs(out_high - 0.8) < 0.05


def test_fit_output_is_monotone():
    """Even on noisy data, the fitted curve must be non-decreasing."""
    rng = random.Random(7)
    probs = [rng.random() for _ in range(2000)]
    outcomes = [1 if rng.random() < p else 0 for p in probs]
    cal = fit(probs, outcomes)
    for i in range(len(cal.xs) - 1):
        assert cal.ys[i] <= cal.ys[i + 1] + 1e-9


def test_fit_rejects_bad_inputs():
    with pytest.raises(ValueError):
        fit([0.5], [1])  # too few
    with pytest.raises(ValueError):
        fit([0.5, 1.5], [0, 1])  # prob out of range
    with pytest.raises(ValueError):
        fit([0.5, 0.6], [0, 2])  # outcome not 0/1
    with pytest.raises(ValueError):
        fit([0.5, 0.6], [0])     # length mismatch


def test_apply_endpoint_anchor():
    """fit() anchors at (0, 0) and (1, 1) so out-of-train queries are sane."""
    rng = random.Random(0)
    # Training data in [0.2, 0.8] only.
    probs = [0.2 + 0.6 * rng.random() for _ in range(2000)]
    outcomes = [1 if rng.random() < p else 0 for p in probs]
    cal = fit(probs, outcomes)
    assert cal.apply(0.0) == pytest.approx(0.0, abs=0.05)
    assert cal.apply(1.0) == pytest.approx(1.0, abs=0.05)
