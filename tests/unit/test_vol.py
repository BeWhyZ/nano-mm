"""Unit tests for pkg.quant.vol.RollingRealizedVol."""
from __future__ import annotations

import math
import random

import pytest

from pkg.quant.vol import RollingRealizedVol

_NS = 1_000_000_000


def test_insufficient_samples_returns_none():
    v = RollingRealizedVol(window_sec=10.0, min_samples=5)
    for i in range(3):
        v.on_mid(100.0, i * _NS)
    assert v.sigma is None


def test_constant_mid_gives_zero_vol():
    v = RollingRealizedVol(window_sec=10.0, min_samples=5)
    for i in range(20):
        v.on_mid(100.0, i * _NS)
    s = v.sigma
    assert s is not None
    assert s == pytest.approx(0.0, abs=1e-12)


def test_window_evicts_old_samples():
    v = RollingRealizedVol(window_sec=2.0, min_samples=2)
    # Fill 10s with constant 100
    for i in range(11):
        v.on_mid(100.0, i * _NS)
    # Then jump to 200 with strict 1s spacing
    for i in range(11, 15):
        v.on_mid(200.0, i * _NS)
    # Window only sees the new flat regime (after the jump propagated through)
    for i in range(15, 25):
        v.on_mid(200.0, i * _NS)
    assert v.sigma == pytest.approx(0.0, abs=1e-12)


def test_brownian_recovery():
    """Sanity check: with σ_log = 0.01 / √sec and dt=0.1s, recovered σ should match within ~10%."""
    rng = random.Random(42)
    sigma_log_per_sqrt_sec = 0.01
    dt_s = 0.1
    n_steps = 4000
    mid = 1000.0
    v = RollingRealizedVol(window_sec=n_steps * dt_s * 2, min_samples=10)
    for i in range(n_steps):
        # log-return ~ N(0, σ √dt)
        r = rng.gauss(0.0, sigma_log_per_sqrt_sec * math.sqrt(dt_s))
        mid *= math.exp(r)
        v.on_mid(mid, int(i * dt_s * _NS))

    s = v.sigma
    assert s is not None
    # σ_price ≈ σ_log × P_latest
    expected = sigma_log_per_sqrt_sec * mid
    # 4000 samples gives ~2-3% std error on σ; allow 15% slack for CI stability
    assert s == pytest.approx(expected, rel=0.15)


def test_non_positive_mid_ignored():
    v = RollingRealizedVol(window_sec=10.0, min_samples=3)
    v.on_mid(0.0, 0)
    v.on_mid(-1.0, _NS)
    assert v.n_samples == 0
