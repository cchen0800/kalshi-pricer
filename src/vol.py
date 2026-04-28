"""Realized vol estimator from 1m closes.

Standard close-to-close log-return std dev, annualized by sqrt(525600 minutes/year).
"""

from __future__ import annotations

import math
from typing import Sequence

MINUTES_PER_YEAR = 365.0 * 24.0 * 60.0


def annualized_vol(closes: Sequence[float]) -> float:
    if len(closes) < 2:
        raise ValueError("need at least 2 closes to compute a return")
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(rets)
    mean = sum(rets) / n
    # Sample variance (n-1) is the standard choice; with n=59 the bias is small either way.
    var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    minutely_sigma = math.sqrt(var)
    return minutely_sigma * math.sqrt(MINUTES_PER_YEAR)
