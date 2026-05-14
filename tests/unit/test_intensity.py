"""Unit tests for pkg.quant.intensity.IntensityCalibrator."""
from __future__ import annotations

import math
import random

import pytest

from pkg.quant.intensity import IntensityCalibrator

_NS = 1_000_000_000


def test_insufficient_trades_returns_none():
    cal = IntensityCalibrator(window_sec=60.0, min_trades=50)
    for i in range(10):
        cal.on_trade(trade_price=100.5, mid_at_trade=100.0, ts_ns=i * _NS)
    assert cal.params is None


def test_synthetic_recovery():
    """Generate Poisson(λ(δ)) arrivals for known (A, k), check recovery within tolerance."""
    rng = random.Random(0)
    A_true = 200.0           # 200 trades/sec at the mid
    k_true_bps = 0.5         # per bps
    mid = 1000.0
    k_true_price = k_true_bps * 1e4 / mid

    T_obs = 120.0
    cal = IntensityCalibrator(
        window_sec=T_obs * 2,
        min_trades=50,
        min_filled_bins=3,
    )

    # Sample δ_bps from the exponential distribution with rate k_true_bps,
    # restrict to the calibrator's bin range [0.5, 128] bps.
    n_trades = int(A_true * T_obs)
    base_ts = 0
    for i in range(n_trades):
        # δ_bps ~ Exp(k_true_bps) truncated to [0.5, 128]
        while True:
            delta_bps = rng.expovariate(k_true_bps)
            if 0.5 <= delta_bps < 128.0:
                break
        delta_price = delta_bps * mid / 1e4
        # Random side direction
        sign = 1.0 if rng.random() < 0.5 else -1.0
        trade_price = mid + sign * delta_price
        ts_ns = base_ts + int(i / n_trades * T_obs * _NS)
        cal.on_trade(trade_price=trade_price, mid_at_trade=mid, ts_ns=ts_ns)

    params = cal.params
    assert params is not None
    A_est, k_est = params

    # With truncated exponential samples, intercept is biased low; aim for
    # order-of-magnitude k recovery within ~30% and finite A.
    assert math.isfinite(A_est) and A_est > 0
    assert k_est == pytest.approx(k_true_price, rel=0.3)


def test_intensity_filters_zero_distance_trades():
    cal = IntensityCalibrator(window_sec=10.0, min_trades=2)
    cal.on_trade(trade_price=100.0, mid_at_trade=100.0, ts_ns=0)
    assert cal.n_trades == 0


def test_intensity_window_eviction():
    cal = IntensityCalibrator(window_sec=1.0, min_trades=2)
    cal.on_trade(trade_price=100.5, mid_at_trade=100.0, ts_ns=0)
    cal.on_trade(trade_price=100.6, mid_at_trade=100.0, ts_ns=_NS // 2)
    assert cal.n_trades == 2
    # Fast-forward past the window
    cal.on_trade(trade_price=100.7, mid_at_trade=100.0, ts_ns=5 * _NS)
    assert cal.n_trades == 1


def test_intensity_rejects_positive_slope():
    """If by chance bins fit positive slope (rate increasing with δ), reject as noise."""
    cal = IntensityCalibrator(
        window_sec=10.0, min_trades=4, min_filled_bins=2,
    )
    # Stuff lots of trades at large δ and few at small δ
    cal.on_trade(trade_price=110.0, mid_at_trade=100.0, ts_ns=0)
    cal.on_trade(trade_price=110.0, mid_at_trade=100.0, ts_ns=_NS // 4)
    cal.on_trade(trade_price=110.0, mid_at_trade=100.0, ts_ns=_NS // 3)
    cal.on_trade(trade_price=100.05, mid_at_trade=100.0, ts_ns=_NS // 2)
    assert cal.params is None
