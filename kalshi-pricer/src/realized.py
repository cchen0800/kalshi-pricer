"""Time-weighted spot accumulator for the BRTI averaging window.

The engine polls ~30s; the BRTI averaging window is 60s. So at peak we have
~2 in-window samples. We treat each sample as the spot value that holds until
the next sample (step function), and time-weight over the window.

Sparse samples mean this is a coarse approximation, but it's strictly better
than ignoring the locked-in portion of the average.
"""
from __future__ import annotations

from collections import deque


class RealizedAverager:
    """Sliding buffer of (epoch_s, price) samples with step-function averaging.

    Auto-prunes samples older than `keep_seconds` to bound memory across hours
    of polling. The buffer keeps one sample older than the cutoff so we always
    have a "leading" value to extrapolate from.
    """

    def __init__(self, keep_seconds: float = 600.0) -> None:
        self._samples: deque[tuple[float, float]] = deque()
        self._keep_seconds = keep_seconds

    def add(self, epoch_s: float, price: float) -> None:
        if price <= 0:
            raise ValueError("price must be positive")
        if self._samples and epoch_s <= self._samples[-1][0]:
            return  # ignore stale / duplicate timestamps
        self._samples.append((epoch_s, price))
        cutoff = epoch_s - self._keep_seconds
        while len(self._samples) > 1 and self._samples[1][0] < cutoff:
            self._samples.popleft()

    def average(self, window_start_s: float, window_end_s: float) -> float | None:
        """Time-weighted average of spot over [window_start_s, window_end_s].

        Step-function semantics: each sample's price holds until the next sample.
        Returns None when:
          - the window has zero or negative length
          - we have no samples
          - we have no sample at or before window_start AND no sample inside the
            window (i.e. nothing to extrapolate from)
        """
        if window_end_s <= window_start_s or not self._samples:
            return None

        # Locate the latest sample at or before window_start (the "leading" one).
        leading_price: float | None = None
        first_in_window_idx: int = len(self._samples)
        for i, (ts, p) in enumerate(self._samples):
            if ts <= window_start_s:
                leading_price = p
            else:
                first_in_window_idx = i
                break

        if leading_price is None:
            # No pre-window sample. Use the first in-window sample's price for
            # the leading gap (best guess in the absence of earlier data).
            if first_in_window_idx >= len(self._samples):
                return None
            first_ts, first_p = self._samples[first_in_window_idx]
            if first_ts >= window_end_s:
                return None
            leading_price = first_p

        # Build (start, end, price) intervals strictly within the window.
        cur_start = window_start_s
        cur_price = leading_price
        weighted = 0.0
        for i in range(first_in_window_idx, len(self._samples)):
            ts, p = self._samples[i]
            if ts >= window_end_s:
                break
            if ts > cur_start:
                weighted += (ts - cur_start) * cur_price
            cur_start = ts
            cur_price = p
        if cur_start < window_end_s:
            weighted += (window_end_s - cur_start) * cur_price

        return weighted / (window_end_s - window_start_s)

    def __len__(self) -> int:
        return len(self._samples)
