"""Fill-intensity calibration: λ(δ) = A · exp(-k · δ) from market trades.

Uses the Avellaneda-Stoikov survival-function recipe: pool both aggressor
sides, sort trade distances δ_i = |trade_price - mid_at_trade|, and at each
threshold δ_t compute

    λ̂(δ_t) = #{ i : δ_i ≥ δ_t } / T_obs

Then OLS-fit log λ̂(δ_t) = log A - k · δ_t over a log-spaced grid of bps
thresholds. This method:

  * handles the (real) lower truncation at δ ≈ 0 cleanly (the survival
    function at δ=0 equals the total trade rate by construction)
  * sidesteps the log-bin-center bias that plagues histogram-based fits of
    exponentially-distributed data

k is reported in 1/price by rescaling with the window-averaged mid.
"""
from __future__ import annotations

import math
from collections import deque

_NS_PER_S = 1_000_000_000.0

# Log-spaced bps thresholds for survival evaluation.
_DEFAULT_THRESHOLDS_BPS: tuple[float, ...] = (
    0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0,
)


class IntensityCalibrator:
    """Sliding-window estimator for (A, k) in λ(δ) = A · exp(-k · δ)."""

    def __init__(
        self,
        window_sec: float = 60.0,
        min_trades: int = 50,
        min_filled_bins: int = 3,
        thresholds_bps: tuple[float, ...] = _DEFAULT_THRESHOLDS_BPS,
        min_count_per_threshold: int = 5,
    ) -> None:
        if window_sec <= 0.0:
            raise ValueError(f"window_sec must be > 0, got {window_sec}")
        if min_trades < 2:
            raise ValueError(f"min_trades must be >= 2, got {min_trades}")
        if min_filled_bins < 2:
            raise ValueError(f"min_filled_bins must be >= 2, got {min_filled_bins}")
        if len(thresholds_bps) < 3:
            raise ValueError("need at least 3 thresholds")
        if any(thresholds_bps[i] >= thresholds_bps[i + 1] for i in range(len(thresholds_bps) - 1)):
            raise ValueError("thresholds_bps must be strictly increasing")

        self._window_ns = int(window_sec * _NS_PER_S)
        self._min_trades = min_trades
        self._min_filled_bins = min_filled_bins
        self._thresholds_bps = tuple(thresholds_bps)
        self._min_count = min_count_per_threshold
        # buffer stores (ts_ns, delta_bps, mid_at_trade)
        self._buf: deque[tuple[int, float, float]] = deque()

    def on_trade(self, trade_price: float, mid_at_trade: float, ts_ns: int) -> None:
        if trade_price <= 0.0 or mid_at_trade <= 0.0:
            return
        delta = abs(trade_price - mid_at_trade)
        if delta <= 0.0:
            return
        delta_bps = delta * 1e4 / mid_at_trade
        self._buf.append((ts_ns, delta_bps, mid_at_trade))
        cutoff = ts_ns - self._window_ns
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()

    @property
    def n_trades(self) -> int:
        return len(self._buf)

    @property
    def params(self) -> tuple[float, float] | None:
        """Return (A in trades/sec, k in 1/price). None if not yet calibratable."""
        if len(self._buf) < self._min_trades:
            return None

        dt_ns = self._buf[-1][0] - self._buf[0][0]
        if dt_ns <= 0:
            return None
        T_obs = dt_ns / _NS_PER_S
        if T_obs <= 0.0:
            return None

        deltas_bps = [d for _, d, _ in self._buf]
        deltas_bps.sort()
        mid_sum = 0.0
        for _, _, m in self._buf:
            mid_sum += m
        mid_avg = mid_sum / len(self._buf)
        if mid_avg <= 0.0:
            return None

        xs: list[float] = []
        ys: list[float] = []
        n = len(deltas_bps)
        # `deltas_bps` is sorted ascending; survival count at threshold t is
        # the number of entries >= t. Walk a single index from the bottom.
        idx = 0
        for t in self._thresholds_bps:
            while idx < n and deltas_bps[idx] < t:
                idx += 1
            cnt = n - idx
            if cnt < self._min_count:
                continue
            rate = cnt / T_obs
            xs.append(t)
            ys.append(math.log(rate))

        if len(xs) < self._min_filled_bins:
            return None

        fit = _ols(xs, ys)
        if fit is None:
            return None
        slope_bps, intercept = fit
        if slope_bps >= 0.0:
            return None

        A = math.exp(intercept)
        k_bps = -slope_bps
        k_price = k_bps * 1e4 / mid_avg
        if k_price <= 0.0 or not math.isfinite(k_price) or not math.isfinite(A):
            return None
        return A, k_price


def _ols(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = 0.0
    den = 0.0
    for x, y in zip(xs, ys, strict=True):
        dx = x - mx
        num += dx * (y - my)
        den += dx * dx
    if den == 0.0:
        return None
    slope = num / den
    intercept = my - slope * mx
    return slope, intercept
