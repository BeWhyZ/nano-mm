"""
Ladder shape generator on top of single-level GLT quotes.

Given the inner-most GLT distances (δ_b, δ_a) and the dispersion unit
u = inventory_skew_unit(params), expand to an N-level ladder per side:

    δ_b_i = δ_b + i · Δ,    Δ = delta_coef · u,    i = 0..N_b-1
    δ_a_i = δ_a + i · Δ,                            i = 0..N_a-1

Per-level size weight w_i comes from cfg.weights (internal-thin /
external-thick is the typical institutional shape). Total size scaling
(inventory taper, lot_size, hard cap) is the engine's responsibility —
this module only emits dimensionless (distance, weight) pairs.

Inventory asymmetry in the MVP is via N only: the hit side (bid when
q_norm > 0, ask when q_norm < 0) drops outer levels as |q_norm| grows.
Weights are NOT redistributed — the hit side simply quotes less total
size, which composes with the engine's taper for double shrinkage.
Price-level skew (δ_b ≠ δ_a) is delegated to pkg.quant.glt.quotes().
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LadderConfig:
    n_levels: int
    delta_coef: float                # Δ = delta_coef · u
    weights: tuple[float, ...]        # per-level size weight, len == n_levels
    n_shrink: int                     # max outer levels dropped on hit side at |q|=1

    def __post_init__(self) -> None:
        if self.n_levels < 1:
            raise ValueError(f"n_levels must be >= 1, got {self.n_levels}")
        if self.delta_coef < 0.0:
            raise ValueError(f"delta_coef must be >= 0, got {self.delta_coef}")
        if len(self.weights) != self.n_levels:
            raise ValueError(
                f"weights length {len(self.weights)} != n_levels {self.n_levels}"
            )
        if any(w < 0.0 for w in self.weights):
            raise ValueError(f"weights must be non-negative, got {self.weights}")
        if not math.isclose(sum(self.weights), 1.0, abs_tol=1e-6):
            raise ValueError(
                f"weights must sum to 1.0, got sum={sum(self.weights)}"
            )
        if not (0 <= self.n_shrink <= self.n_levels - 1):
            raise ValueError(
                f"n_shrink must be in [0, n_levels-1={self.n_levels - 1}], got {self.n_shrink}"
            )


@dataclass(frozen=True, slots=True)
class LadderLevel:
    delta_from_mid: float    # non-negative distance from mid (price units)
    size_weight: float       # fraction of base size to allocate at this level


def build_ladder(
    delta_b: float,
    delta_a: float,
    u: float,
    q_norm: float,
    cfg: LadderConfig,
) -> tuple[list[LadderLevel], list[LadderLevel]]:
    """Generate (bid_levels, ask_levels), inner-most first.

    Levels are emitted in ascending delta order: index 0 is the innermost
    (closest to mid), index n-1 is the outermost. Caller computes prices
    as `mid - delta` for bids and `mid + delta` for asks, and is responsible
    for tick rounding and monotonicity enforcement after rounding.
    """
    step = cfg.delta_coef * u

    inv = abs(q_norm)
    n_drop = min(int(math.floor(inv * cfg.n_shrink)), cfg.n_levels - 1)
    n_b = cfg.n_levels - n_drop if q_norm > 0.0 else cfg.n_levels
    n_a = cfg.n_levels - n_drop if q_norm < 0.0 else cfg.n_levels

    bids = [
        LadderLevel(delta_from_mid=delta_b + i * step, size_weight=cfg.weights[i])
        for i in range(n_b)
    ]
    asks = [
        LadderLevel(delta_from_mid=delta_a + i * step, size_weight=cfg.weights[i])
        for i in range(n_a)
    ]
    return bids, asks
