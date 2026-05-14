"""Unit tests for pkg.quant.glt (Cartea-Jaimungal asymptotic closed-form)."""
from __future__ import annotations

import math

import pytest

from pkg.quant.glt import (
    GLTParams,
    inventory_skew_unit,
    quotes,
    reservation_half_spread,
    total_spread,
)


def _p(**kw) -> GLTParams:
    base = dict(gamma=0.1, sigma=2.0, A=50.0, k=1.5)
    base.update(kw)
    return GLTParams(**base)


# ------------------------------------------------------------------
# Parameter validation
# ------------------------------------------------------------------

@pytest.mark.parametrize("field,bad", [
    ("gamma", 0.0), ("gamma", -1.0),
    ("sigma", -0.1),
    ("A", 0.0), ("A", -1.0),
    ("k", 0.0), ("k", -1.0),
])
def test_invalid_params_raise(field: str, bad: float):
    with pytest.raises(ValueError):
        _p(**{field: bad})


# ------------------------------------------------------------------
# Symmetry and inventory skew
# ------------------------------------------------------------------

def test_zero_inventory_symmetric():
    p = _p()
    db, da = quotes(p, q_lot=0.0)
    half = reservation_half_spread(p)
    unit = inventory_skew_unit(p)
    assert db == pytest.approx(half + 0.5 * unit)
    assert da == pytest.approx(half + 0.5 * unit)
    # Symmetry under q=0
    assert db == pytest.approx(da)


def test_positive_inventory_pushes_ask_closer():
    p = _p()
    db_long, da_long = quotes(p, q_lot=3.0)
    db_flat, da_flat = quotes(p, q_lot=0.0)
    # Long inventory → want to sell harder (ask closer) and buy less (bid further)
    assert da_long < da_flat
    assert db_long > db_flat


def test_negative_inventory_pushes_bid_closer():
    p = _p()
    db_short, da_short = quotes(p, q_lot=-3.0)
    db_flat, _ = quotes(p, q_lot=0.0)
    assert db_short < db_flat
    # Symmetry: short(-q) ≡ swapped long(q)
    db_long, da_long = quotes(p, q_lot=3.0)
    assert db_short == pytest.approx(da_long)
    assert da_short == pytest.approx(db_long)


def test_total_spread_invariant_under_q():
    p = _p()
    psi = total_spread(p)
    for q in [-5.0, -1.0, 0.0, 1.0, 5.0]:
        db, da = quotes(p, q_lot=q)
        assert db + da == pytest.approx(psi)


def test_total_spread_formula():
    p = _p()
    expected = 2.0 * reservation_half_spread(p) + inventory_skew_unit(p)
    assert total_spread(p) == pytest.approx(expected)


# ------------------------------------------------------------------
# Scaling behaviour
# ------------------------------------------------------------------

def test_zero_sigma_zero_skew_unit():
    p = _p(sigma=0.0)
    assert inventory_skew_unit(p) == 0.0
    db, da = quotes(p, q_lot=5.0)
    # With σ=0, only the symmetric ln term remains
    half = reservation_half_spread(p)
    assert db == pytest.approx(half)
    assert da == pytest.approx(half)


def test_skew_unit_scales_with_sigma():
    p1 = _p(sigma=1.0)
    p2 = _p(sigma=2.0)
    # u ∝ σ
    assert inventory_skew_unit(p2) == pytest.approx(2.0 * inventory_skew_unit(p1))


def test_extreme_gamma_does_not_explode():
    # γ very small (risk-neutral limit): half ≈ 1/k, finite
    p_small = _p(gamma=1e-6)
    half = reservation_half_spread(p_small)
    assert math.isfinite(half)
    # γ very large (very risk-averse): half ≈ ln(γ/k)/γ → 0
    p_large = _p(gamma=100.0)
    assert math.isfinite(reservation_half_spread(p_large))
    assert math.isfinite(inventory_skew_unit(p_large))


def test_reservation_half_known_limit():
    # As γ → 0, (1/γ) ln(1 + γ/k) → 1/k
    p = _p(gamma=1e-8, k=2.0)
    assert reservation_half_spread(p) == pytest.approx(1.0 / 2.0, rel=1e-3)
