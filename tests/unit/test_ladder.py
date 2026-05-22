"""Unit tests for pkg.quant.ladder."""
from __future__ import annotations

import pytest

from pkg.quant.ladder import LadderConfig, LadderLevel, build_ladder


def _cfg(**kw) -> LadderConfig:
    base = dict(
        n_levels=3,
        delta_coef=0.5,
        weights=(0.15, 0.30, 0.55),
        n_shrink=2,
    )
    base.update(kw)
    return LadderConfig(**base)


# ------------------------------------------------------------------
# Config validation
# ------------------------------------------------------------------

def test_weights_length_mismatch_raises():
    with pytest.raises(ValueError, match="weights length"):
        LadderConfig(n_levels=3, delta_coef=0.5, weights=(0.5, 0.5), n_shrink=0)


def test_weights_sum_must_be_one():
    with pytest.raises(ValueError, match="weights must sum"):
        LadderConfig(n_levels=2, delta_coef=0.5, weights=(0.4, 0.4), n_shrink=0)


def test_negative_weights_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        LadderConfig(n_levels=2, delta_coef=0.5, weights=(1.2, -0.2), n_shrink=0)


def test_n_levels_must_be_positive():
    with pytest.raises(ValueError, match="n_levels"):
        LadderConfig(n_levels=0, delta_coef=0.5, weights=(), n_shrink=0)


def test_delta_coef_must_be_nonneg():
    with pytest.raises(ValueError, match="delta_coef"):
        LadderConfig(n_levels=2, delta_coef=-0.1, weights=(0.5, 0.5), n_shrink=0)


def test_n_shrink_must_be_in_range():
    with pytest.raises(ValueError, match="n_shrink"):
        LadderConfig(n_levels=3, delta_coef=0.5, weights=(0.3, 0.3, 0.4), n_shrink=3)
    with pytest.raises(ValueError, match="n_shrink"):
        LadderConfig(n_levels=3, delta_coef=0.5, weights=(0.3, 0.3, 0.4), n_shrink=-1)


# ------------------------------------------------------------------
# Output shape & symmetry
# ------------------------------------------------------------------

def test_symmetric_when_flat():
    cfg = _cfg()
    bids, asks = build_ladder(delta_b=1.0, delta_a=1.0, u=2.0, q_norm=0.0, cfg=cfg)
    assert len(bids) == len(asks) == 3
    for b, a in zip(bids, asks, strict=True):
        assert b.delta_from_mid == pytest.approx(a.delta_from_mid)
        assert b.size_weight == pytest.approx(a.size_weight)


def test_monotonic_deltas():
    cfg = _cfg()
    bids, asks = build_ladder(delta_b=2.0, delta_a=3.0, u=1.5, q_norm=0.0, cfg=cfg)
    for levels in (bids, asks):
        deltas = [lv.delta_from_mid for lv in levels]
        assert deltas == sorted(deltas)
        assert len(set(deltas)) == len(deltas)


def test_weight_sums_when_no_shrink():
    cfg = _cfg()
    bids, asks = build_ladder(delta_b=1.0, delta_a=1.0, u=1.0, q_norm=0.0, cfg=cfg)
    assert sum(lv.size_weight for lv in bids) == pytest.approx(1.0)
    assert sum(lv.size_weight for lv in asks) == pytest.approx(1.0)


def test_step_equals_delta_coef_times_u():
    cfg = _cfg(delta_coef=0.5)
    bids, _ = build_ladder(delta_b=1.0, delta_a=1.0, u=4.0, q_norm=0.0, cfg=cfg)
    expected_step = 0.5 * 4.0
    assert bids[1].delta_from_mid - bids[0].delta_from_mid == pytest.approx(expected_step)
    assert bids[2].delta_from_mid - bids[1].delta_from_mid == pytest.approx(expected_step)


def test_step_scales_linearly_with_u():
    cfg = _cfg()
    bids_lo, _ = build_ladder(delta_b=1.0, delta_a=1.0, u=1.0, q_norm=0.0, cfg=cfg)
    bids_hi, _ = build_ladder(delta_b=1.0, delta_a=1.0, u=2.0, q_norm=0.0, cfg=cfg)
    step_lo = bids_lo[1].delta_from_mid - bids_lo[0].delta_from_mid
    step_hi = bids_hi[1].delta_from_mid - bids_hi[0].delta_from_mid
    assert step_hi == pytest.approx(2.0 * step_lo)


def test_inner_level_starts_at_glt_delta():
    cfg = _cfg()
    bids, asks = build_ladder(delta_b=2.5, delta_a=3.5, u=1.0, q_norm=0.0, cfg=cfg)
    assert bids[0].delta_from_mid == pytest.approx(2.5)
    assert asks[0].delta_from_mid == pytest.approx(3.5)


# ------------------------------------------------------------------
# Inventory asymmetry (N-shrink)
# ------------------------------------------------------------------

def test_long_inventory_shrinks_bid_levels():
    # n_shrink=2, |q|=1 → drop 2 levels on bid side, keep all on ask
    cfg = _cfg(n_shrink=2)
    bids, asks = build_ladder(delta_b=1.0, delta_a=1.0, u=1.0, q_norm=1.0, cfg=cfg)
    assert len(bids) == 1
    assert len(asks) == 3


def test_short_inventory_shrinks_ask_levels():
    cfg = _cfg(n_shrink=2)
    bids, asks = build_ladder(delta_b=1.0, delta_a=1.0, u=1.0, q_norm=-1.0, cfg=cfg)
    assert len(bids) == 3
    assert len(asks) == 1


def test_partial_inventory_partial_shrink():
    # |q|=0.5, n_shrink=2 → floor(0.5*2)=1 dropped from hit side
    cfg = _cfg(n_shrink=2)
    bids, asks = build_ladder(delta_b=1.0, delta_a=1.0, u=1.0, q_norm=0.5, cfg=cfg)
    assert len(bids) == 2
    assert len(asks) == 3


def test_at_least_one_level_remains_on_hit_side():
    # Even with extreme |q|, hit side keeps at least 1 level
    # (engine's q_hard_cap is what fully halts that side, not the ladder)
    cfg = _cfg(n_shrink=2)
    bids, asks = build_ladder(delta_b=1.0, delta_a=1.0, u=1.0, q_norm=1.0, cfg=cfg)
    assert len(bids) >= 1
    bids, asks = build_ladder(delta_b=1.0, delta_a=1.0, u=1.0, q_norm=-1.0, cfg=cfg)
    assert len(asks) >= 1


def test_no_shrink_config_keeps_all_levels():
    cfg = _cfg(n_shrink=0)
    for q in [-1.0, -0.5, 0.0, 0.5, 1.0]:
        bids, asks = build_ladder(delta_b=1.0, delta_a=1.0, u=1.0, q_norm=q, cfg=cfg)
        assert len(bids) == 3
        assert len(asks) == 3


def test_safe_side_unaffected_by_inventory():
    cfg = _cfg(n_shrink=2)
    # long → ask is safe side
    _, asks_long = build_ladder(delta_b=1.0, delta_a=1.0, u=1.0, q_norm=0.8, cfg=cfg)
    _, asks_flat = build_ladder(delta_b=1.0, delta_a=1.0, u=1.0, q_norm=0.0, cfg=cfg)
    assert len(asks_long) == len(asks_flat) == 3
    for a, b in zip(asks_long, asks_flat, strict=True):
        assert a.delta_from_mid == pytest.approx(b.delta_from_mid)
        assert a.size_weight == pytest.approx(b.size_weight)


def test_zero_u_collapses_to_single_distance():
    # With u=0 (σ=0 case), all levels share the same delta
    cfg = _cfg()
    bids, _ = build_ladder(delta_b=1.5, delta_a=1.5, u=0.0, q_norm=0.0, cfg=cfg)
    assert all(lv.delta_from_mid == pytest.approx(1.5) for lv in bids)
