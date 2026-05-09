"""Realized vol estimators.

`annualized_vol` — close-to-close log-return std dev. Throws away H/L info.

`yang_zhang_vol` — Yang & Zhang (2000) estimator combining overnight (gap),
open-to-close, and Rogers-Satchell (1991) range variances. With BTC's 24/7
tape the "overnight" term is the prev_close → cur_open jitter. Drift-independent
and several times more efficient than close-to-close on the same input.

Both are annualized by sqrt(525600 minutes/year).
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


def yang_zhang_vol(ohlc: Sequence[tuple[float, float, float, float]]) -> float:
    """Yang-Zhang annualized vol from a sequence of (open, high, low, close) bars.

    YZ_var_per_bar = var_overnight + k * var_open_to_close + (1 - k) * var_RS
        where var_RS is Rogers-Satchell, and k = 0.34 / (1.34 + (m+1)/(m-1))
        is chosen to minimize estimator variance (Yang & Zhang 2000, eq. 8).
    """
    n = len(ohlc)
    if n < 2:
        raise ValueError("need at least 2 bars")

    overnight: list[float] = []   # ln(O_i / C_{i-1})
    open_close: list[float] = []  # ln(C_i / O_i)
    rs: list[float] = []          # Rogers-Satchell per-bar contribution
    for i in range(1, n):
        o, h, l, c = ohlc[i]
        c_prev = ohlc[i - 1][3]
        if min(o, h, l, c, c_prev) <= 0:
            raise ValueError("non-positive price in OHLC")
        overnight.append(math.log(o / c_prev))
        open_close.append(math.log(c / o))
        rs.append(math.log(h / c) * math.log(h / o) + math.log(l / c) * math.log(l / o))

    m = len(overnight)
    if m < 2:
        return 0.0

    mean_on = sum(overnight) / m
    mean_oc = sum(open_close) / m
    var_on = sum((x - mean_on) ** 2 for x in overnight) / (m - 1)
    var_oc = sum((x - mean_oc) ** 2 for x in open_close) / (m - 1)
    var_rs = sum(rs) / m  # RS is mean-zero by construction; no centering.

    k = 0.34 / (1.34 + (m + 1) / (m - 1))
    yz_var = var_on + k * var_oc + (1.0 - k) * var_rs
    if yz_var < 0:
        # RS can go slightly negative on degenerate data (e.g. H=L=O=C). Floor at 0.
        yz_var = 0.0
    return math.sqrt(yz_var) * math.sqrt(MINUTES_PER_YEAR)
