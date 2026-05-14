"""
Cartea-Jaimungal (a.k.a. Guéant-Lehalle-Tapia) asymptotic closed-form quotes.

Steady-state, infinite-horizon solution to the inventory-aware MM problem with
Poisson fill intensity λ(δ) = A · exp(-k · δ):

    δ_b*(q) ≈ (1/γ) ln(1 + γ/k) + (2q+1)/2 · u
    δ_a*(q) ≈ (1/γ) ln(1 + γ/k) - (2q-1)/2 · u
    ψ*(q)  = δ_b* + δ_a* = (2/γ) ln(1 + γ/k) + u

with the inventory-skew unit

    u = sqrt( σ²γ / (2kA) · (1 + γ/k)^(1 + k/γ) )

Unit convention (must be self-consistent across all four parameters):
    σ : price · second^(-1/2)
    A : trades · second^(-1)
    k : 1 / price
    γ : 1 / price                (inverse of risk-aversion in price units)

q is *lot count*. Callers that work in normalized inventory q_norm ∈ [-1, 1]
must convert with q_lot = q_norm × Q_max before calling `quotes`.

Output δ_b, δ_a are non-negative *distances from mid* in the same price units
as σ/k/γ. Final quote prices = (mid - δ_b, mid + δ_a).
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GLTParams:
    gamma: float
    sigma: float
    A: float
    k: float

    def __post_init__(self) -> None:
        if self.gamma <= 0.0:
            raise ValueError(f"gamma must be > 0, got {self.gamma}")
        if self.sigma < 0.0:
            raise ValueError(f"sigma must be >= 0, got {self.sigma}")
        if self.A <= 0.0:
            raise ValueError(f"A must be > 0, got {self.A}")
        if self.k <= 0.0:
            raise ValueError(f"k must be > 0, got {self.k}")


def reservation_half_spread(p: GLTParams) -> float:
    """(1/γ) ln(1 + γ/k) — symmetric component, independent of σ and q."""
    return math.log1p(p.gamma / p.k) / p.gamma


def inventory_skew_unit(p: GLTParams) -> float:
    """sqrt(σ²γ / (2kA) · (1 + γ/k)^(1 + k/γ)) — per-lot inventory skew magnitude.

    Computed in log space to stay numerically stable when γ/k is large or small.
    """
    if p.sigma == 0.0:
        return 0.0
    # log(base) = 2 log σ + log γ - log 2 - log k - log A
    log_base = (
        2.0 * math.log(p.sigma)
        + math.log(p.gamma)
        - math.log(2.0)
        - math.log(p.k)
        - math.log(p.A)
    )
    # log((1 + γ/k)^(1 + k/γ)) = (1 + k/γ) · log1p(γ/k)
    log_expo = (1.0 + p.k / p.gamma) * math.log1p(p.gamma / p.k)
    return math.exp(0.5 * (log_base + log_expo))


def quotes(p: GLTParams, q_lot: float) -> tuple[float, float]:
    """Return (δ_b, δ_a) — non-negative distances from mid in price units.

    q_lot is inventory in lot units (= q_norm × Q_max when caller works in
    normalized inventory).
    """
    half = reservation_half_spread(p)
    unit = inventory_skew_unit(p)
    delta_b = half + (2.0 * q_lot + 1.0) / 2.0 * unit
    delta_a = half - (2.0 * q_lot - 1.0) / 2.0 * unit
    return delta_b, delta_a


def total_spread(p: GLTParams) -> float:
    """ψ*(q) = δ_b + δ_a = (2/γ) ln(1 + γ/k) + u. Note: independent of q."""
    return 2.0 * reservation_half_spread(p) + inventory_skew_unit(p)
