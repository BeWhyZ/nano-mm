"""Rolling realized volatility from a mid-price stream.

Uses the standard realized-variance estimator on log-returns, normalised by
the actual observed window length (handles irregular sampling correctly):

    RV     = Σ_i r_i²        with r_i = log(mid_i / mid_{i-1})
    σ²_log = RV / Δt_total
    σ_price ≈ σ_log × P_latest

The price-unit conversion uses the latest mid as the local price level, which
is what the GLT formula expects when σ is quoted in price·sec^{-1/2}.
"""
from __future__ import annotations

import math
from collections import deque

_NS_PER_S = 1_000_000_000.0


class RollingRealizedVol:
    """Time-bounded ring of (ts_ns, log_mid) samples, computes σ on demand.

    Caller invokes `on_mid` on every L2 update; `sigma` returns price-vol
    per √sec, or None until the window is sufficiently populated.
    """

    def __init__(self, window_sec: float = 30.0, min_samples: int = 30) -> None:
        if window_sec <= 0.0:
            raise ValueError(f"window_sec must be > 0, got {window_sec}")
        if min_samples < 2:
            raise ValueError(f"min_samples must be >= 2, got {min_samples}")
        self._window_ns = int(window_sec * _NS_PER_S)
        self._min_samples = min_samples
        self._buf: deque[tuple[int, float, float]] = deque()  # (ts_ns, log_mid, mid)

    def on_mid(self, mid: float, ts_ns: int) -> None:
        if mid <= 0.0:
            return
        self._buf.append((ts_ns, math.log(mid), mid))
        cutoff = ts_ns - self._window_ns
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()

    @property
    def sigma(self) -> float | None:
        """σ in price · sec^(-1/2), or None when insufficient data."""
        if len(self._buf) < self._min_samples:
            return None
        # Realized variance on log-returns
        rv = 0.0
        prev_log = self._buf[0][1]
        for _, log_mid, _ in list(self._buf)[1:]:
            r = log_mid - prev_log
            rv += r * r
            prev_log = log_mid
        dt_ns = self._buf[-1][0] - self._buf[0][0]
        if dt_ns <= 0:
            return None
        dt_s = dt_ns / _NS_PER_S
        sigma_log = math.sqrt(rv / dt_s)
        # Convert log-vol to price-vol using latest mid (small-return approx)
        return sigma_log * self._buf[-1][2]

    @property
    def n_samples(self) -> int:
        return len(self._buf)

    @property
    def window_seconds(self) -> float:
        if len(self._buf) < 2:
            return 0.0
        return (self._buf[-1][0] - self._buf[0][0]) / _NS_PER_S
