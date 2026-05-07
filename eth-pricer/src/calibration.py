"""Isotonic-regression calibrator for the lognormal pricer's `model_prob`.

Backtest of polls JOIN settlements (5d / 27 events on the BTC DB) shows the
raw model is biased high in the mid band — predicts ~24% / 38% in deciles
4-5 where realized rate is ~15% / 26%. A monotone empirical mapping
recovers ~2-2.5x of the SELL-side P&L without changing the pricer math.

The calibrator is fit offline by `scripts/fit_calibrator.py` and persisted
to a JSON file (knot points). The engine loads it at startup and applies
it row-by-row in `build_poll_rows`. When the file is missing, `identity()`
gives `calibrated == raw` and the engine logs a warning — fail-open here
because PR #1 is supposed to be a no-op behaviorally; only the displayed
column changes.
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

log = logging.getLogger("calibration")


@dataclass(frozen=True)
class IsotonicCalibrator:
    """Piecewise-constant isotonic map represented by sorted knot points.

    `xs` are unique, ascending raw probabilities; `ys` are the corresponding
    calibrated probabilities, also ascending (monotone non-decreasing). For
    inputs outside [xs[0], xs[-1]] we clamp to the endpoint values, then
    linearly interpolate between knots inside the range.
    """

    xs: tuple[float, ...]
    ys: tuple[float, ...]
    n_train: int = 0           # rows used to fit; 0 for identity
    fit_brier: float | None = None
    cal_brier: float | None = None

    def __post_init__(self) -> None:
        if len(self.xs) != len(self.ys):
            raise ValueError("xs and ys length mismatch")
        if len(self.xs) < 2:
            raise ValueError("need at least 2 knots")
        if any(self.xs[i] >= self.xs[i + 1] for i in range(len(self.xs) - 1)):
            raise ValueError("xs must be strictly ascending")
        if any(self.ys[i] > self.ys[i + 1] for i in range(len(self.ys) - 1)):
            raise ValueError("ys must be non-decreasing (isotonic)")

    def apply(self, p: float) -> float:
        """Map a single raw probability to its calibrated value."""
        if p <= self.xs[0]:
            return self.ys[0]
        if p >= self.xs[-1]:
            return self.ys[-1]
        # bisect_left returns the index of the first xs[i] >= p; since p is
        # strictly between xs[0] and xs[-1] (by the guards above), 1 <= i <= n-1.
        i = bisect_left(self.xs, p)
        x0, x1 = self.xs[i - 1], self.xs[i]
        y0, y1 = self.ys[i - 1], self.ys[i]
        if x1 == x0:
            return y0
        return y0 + (y1 - y0) * (p - x0) / (x1 - x0)

    def to_json(self) -> str:
        return json.dumps(
            {
                "xs": list(self.xs),
                "ys": list(self.ys),
                "n_train": self.n_train,
                "fit_brier": self.fit_brier,
                "cal_brier": self.cal_brier,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, s: str) -> "IsotonicCalibrator":
        d = json.loads(s)
        return cls(
            xs=tuple(d["xs"]),
            ys=tuple(d["ys"]),
            n_train=int(d.get("n_train", 0)),
            fit_brier=d.get("fit_brier"),
            cal_brier=d.get("cal_brier"),
        )


def identity() -> IsotonicCalibrator:
    """A no-op calibrator: maps p → p across [0, 1]. Used when no fit file exists."""
    return IsotonicCalibrator(xs=(0.0, 1.0), ys=(0.0, 1.0), n_train=0)


def load(path: str | Path) -> IsotonicCalibrator:
    """Load a calibrator from JSON; return identity if the file is missing.

    A missing file is *not* an error — it just means we haven't fit one yet
    on this DB (e.g. fresh deploy, eth-pricer with no settlements scraped).
    Logs a warning so it's visible.
    """
    p = Path(path)
    if not p.exists():
        log.warning(
            "calibrator file %s missing; using identity (model_prob_calibrated == model_prob)",
            p,
        )
        return identity()
    try:
        cal = IsotonicCalibrator.from_json(p.read_text())
    except Exception:
        log.exception("failed to load calibrator %s; falling back to identity", p)
        return identity()
    log.info(
        "loaded calibrator from %s (n_knots=%d, n_train=%d, fit_brier=%s, cal_brier=%s)",
        p, len(cal.xs), cal.n_train, cal.fit_brier, cal.cal_brier,
    )
    return cal


def save(calibrator: IsotonicCalibrator, path: str | Path) -> None:
    Path(path).write_text(calibrator.to_json())


# ---- Pool-Adjacent-Violators (PAV) isotonic regression ----
#
# Standard O(n) algorithm: walk left-to-right, merge any block whose mean
# drops below its left neighbor, repeat until monotone. Implemented inline
# (rather than pulling in scikit-learn) because adding a 200MB dep for one
# estimator isn't worth it.

def _pav(xs: Sequence[float], ys: Sequence[float], weights: Sequence[float]) -> tuple[list[float], list[float]]:
    """Pool adjacent violators on (xs, ys, weights) — xs already grouped/sorted.

    Returns the unique (x, fitted_y) knot points where fitted_y is the
    block-weighted mean. Caller is responsible for de-duping x ties before
    calling.
    """
    n = len(xs)
    if n == 0:
        raise ValueError("empty input")
    # Each block stored as [sum_wy, sum_w, x_right]
    blocks: list[list[float]] = []
    for i in range(n):
        cur = [ys[i] * weights[i], weights[i], xs[i]]
        blocks.append(cur)
        # Merge backward while the new block's mean violates monotonicity.
        while len(blocks) >= 2:
            a = blocks[-2]
            b = blocks[-1]
            mean_a = a[0] / a[1]
            mean_b = b[0] / b[1]
            if mean_a <= mean_b:
                break
            # Pool a and b.
            blocks.pop()
            blocks.pop()
            blocks.append([a[0] + b[0], a[1] + b[1], b[2]])
    fitted_xs: list[float] = []
    fitted_ys: list[float] = []
    for sum_wy, sum_w, x_right in blocks:
        fitted_xs.append(x_right)
        fitted_ys.append(sum_wy / sum_w)
    return fitted_xs, fitted_ys


def fit(probs: Sequence[float], outcomes: Sequence[int]) -> IsotonicCalibrator:
    """Fit p → realized-rate via isotonic regression (PAV).

    `probs` are raw model probabilities in [0, 1]. `outcomes` are 0/1 labels.
    Returns an `IsotonicCalibrator` whose `apply` interpolates between the
    PAV-fitted knot points. The first knot is anchored at x=0 → y=0 and
    the last at x=1 → y=1 unless the data already extends to those bounds,
    so calls outside the training range degrade sensibly to "trust the
    model at extremes."
    """
    if len(probs) != len(outcomes):
        raise ValueError("probs and outcomes length mismatch")
    if len(probs) < 2:
        raise ValueError("need at least 2 samples to fit")
    if any(p < 0 or p > 1 for p in probs):
        raise ValueError("probs must be in [0, 1]")
    if any(o not in (0, 1) for o in outcomes):
        raise ValueError("outcomes must be 0 or 1")

    # Group ties on the same x: weight = count, y = mean outcome at that x.
    pairs = sorted(zip(probs, outcomes), key=lambda t: t[0])
    grouped_xs: list[float] = []
    grouped_ys: list[float] = []
    grouped_w: list[float] = []
    i = 0
    n = len(pairs)
    while i < n:
        j = i
        s = 0.0
        while j < n and pairs[j][0] == pairs[i][0]:
            s += pairs[j][1]
            j += 1
        grouped_xs.append(pairs[i][0])
        grouped_ys.append(s / (j - i))
        grouped_w.append(float(j - i))
        i = j

    fitted_xs, fitted_ys = _pav(grouped_xs, grouped_ys, grouped_w)

    # Anchor endpoints at (0, 0) and (1, 1) so out-of-range queries get a
    # sane answer. We only prepend / append if the actual data didn't reach
    # the boundary AND the boundary value preserves monotonicity.
    xs = list(fitted_xs)
    ys = list(fitted_ys)
    if xs[0] > 0.0 and ys[0] >= 0.0:
        xs.insert(0, 0.0)
        ys.insert(0, 0.0)
    if xs[-1] < 1.0 and ys[-1] <= 1.0:
        xs.append(1.0)
        ys.append(1.0)

    # PAV can produce duplicate x positions if endpoint anchoring met a
    # zero-mass block at the same x — collapse defensively.
    dedup_xs: list[float] = [xs[0]]
    dedup_ys: list[float] = [ys[0]]
    for x, y in zip(xs[1:], ys[1:]):
        if x == dedup_xs[-1]:
            dedup_ys[-1] = y          # take latest (rightmost) value
            continue
        dedup_xs.append(x)
        dedup_ys.append(y)

    # Brier score on the training sample (raw vs calibrated).
    cal = IsotonicCalibrator(xs=tuple(dedup_xs), ys=tuple(dedup_ys), n_train=len(probs))
    fit_b = sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)
    cal_b = sum((cal.apply(p) - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)
    return IsotonicCalibrator(
        xs=cal.xs, ys=cal.ys, n_train=len(probs),
        fit_brier=fit_b, cal_brier=cal_b,
    )
