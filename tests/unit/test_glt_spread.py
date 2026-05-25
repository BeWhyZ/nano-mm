"""Unit tests for GltSpreadEngine re-quote hysteresis (Ex-4 gate).

Stubs RollingRealizedVol / IntensityCalibrator with fixed (σ, A, k) so the
gate logic is the only variable under test.  The pure GLT formula is covered
by test_glt.py; here we only verify when self._state is replaced vs. reused.
"""
from __future__ import annotations

import time

import structlog

from biz.domain.book import OrderBookSnapshot, PriceLevel
from biz.usecase.glt_spread import GltSpreadEngine
from config import SpreadConfig


# ---------------------------------------------------------------------------
# Stubs & helpers
# ---------------------------------------------------------------------------


class _StubVol:
    def __init__(self, sigma: float = 2.0) -> None:
        self._sigma = sigma

    @property
    def sigma(self) -> float:
        return self._sigma

    def on_mid(self, mid: float, ts_ns: int) -> None:  # noqa: ARG002
        pass


class _StubIntensity:
    def __init__(self, A: float = 50.0, k: float = 1.5) -> None:
        self._params = (A, k)

    @property
    def params(self) -> tuple[float, float]:
        return self._params

    def on_trade(self, price: float, mid: float, ts_ns: int) -> None:  # noqa: ARG002
        pass


def _make_cfg(**overrides) -> SpreadConfig:
    defaults: dict = dict(
        gamma=0.1, Q_max=10.0, lot_size=0.001, q_hard_cap=0.95,
        vol_window_sec=30.0, vol_min_samples=30,
        intensity_window_sec=60.0, intensity_min_trades=50, intensity_min_filled_bins=3,
        price_tick=0.01, spread_floor_bps=0.75,
        ladder_n_levels=3, ladder_delta_coef=0.5,
        ladder_weights=(0.15, 0.30, 0.55), ladder_n_shrink=2,
        requote_inner_move_ticks=1,
        requote_q_norm_threshold=0.05,
        requote_max_age_ms=1000.0,
    )
    defaults.update(overrides)
    return SpreadConfig(**defaults)


def _make_engine(cfg: SpreadConfig | None = None) -> GltSpreadEngine:
    eng = GltSpreadEngine("BTCUSDT", cfg or _make_cfg(), structlog.get_logger())
    eng._vol = _StubVol()
    eng._intensity = _StubIntensity()
    return eng


def _snap(mid: float, venue: str = "bybit_spot") -> OrderBookSnapshot:
    half = 0.005  # 1 cent inside-spread → 1-tick book
    return OrderBookSnapshot(
        symbol="BTCUSDT",
        venue=venue,
        bids=[PriceLevel(mid - half, 1.0)],
        asks=[PriceLevel(mid + half, 1.0)],
        event_ts=0,
        send_ts=0,
        recv_ts=time.monotonic_ns(),
        seq=1,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_first_emit_always_publishes():
    eng = _make_engine()
    assert eng.state is None
    eng.on_book(_snap(50000.0))
    state = eng.state
    assert state is not None
    assert state.bids
    assert state.asks


def test_sub_tick_drift_reuses_quotes():
    eng = _make_engine()
    eng.on_book(_snap(50000.0))
    state1 = eng.state
    assert state1 is not None
    prev_bids, prev_asks = state1.bids, state1.asks

    # mid moves exactly 1 tick — threshold is "> 1 tick", so NOT triggered.
    eng.on_book(_snap(50000.01))
    state2 = eng.state
    assert state2 is state1, "gate should suppress emit within band"
    assert state2.bids is prev_bids
    assert state2.asks is prev_asks


def test_breakout_emits_new_quotes():
    eng = _make_engine()
    eng.on_book(_snap(50000.0))
    state1 = eng.state
    assert state1 is not None
    inner_bid_before = state1.bids[0].price

    # mid moves 5 ticks → ideal inner bid shifts > 1 tick from anchor.
    eng.on_book(_snap(50000.05))
    state2 = eng.state
    assert state2 is not state1
    assert state2.bids[0].price != inner_bid_before


def test_q_norm_jump_forces_reemit():
    eng = _make_engine()
    eng.on_book(_snap(50000.0))
    state1 = eng.state

    # mid unchanged, but q_norm jumps past 0.05 threshold.
    eng.on_inventory(0.10)
    eng.on_book(_snap(50000.0))
    state2 = eng.state
    assert state2 is not state1


def test_q_norm_micro_jump_within_band_reuses():
    eng = _make_engine()
    eng.on_book(_snap(50000.0))
    state1 = eng.state

    # q_norm moves 0.01 — well under 0.05 threshold.
    eng.on_inventory(0.01)
    eng.on_book(_snap(50000.0))
    state2 = eng.state
    assert state2 is state1


def test_max_age_force_reemit():
    cfg = _make_cfg(
        requote_max_age_ms=20.0,         # heartbeat fires fast
        requote_inner_move_ticks=10_000,  # effectively disable price path
        requote_q_norm_threshold=100.0,   # disable inventory path
    )
    eng = _make_engine(cfg)
    eng.on_book(_snap(50000.0))
    state1 = eng.state

    time.sleep(0.05)  # 50ms ≫ 20ms heartbeat
    eng.on_book(_snap(50000.0))
    state2 = eng.state
    assert state2 is not state1, "heartbeat should force re-emit after max_age"


def test_ladder_shape_change_forces_reemit():
    eng = _make_engine()
    eng.on_book(_snap(50000.0))
    state1 = eng.state
    assert state1.bids and state1.asks

    # Push q_norm past q_hard_cap → bid side should empty out.
    eng.on_inventory(0.96)
    eng.on_book(_snap(50000.0))
    state2 = eng.state
    assert state2 is not state1
    assert not state2.bids        # bidding disabled at cap
    assert state2.asks            # ask side still active
