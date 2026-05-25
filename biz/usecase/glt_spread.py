"""
GLT (Cartea-Jaimungal asymptotic) spread engine.

Three event entry points:
    on_book(snap)       — refresh σ, recompute quote (the heartbeat)
    on_trade(tick)      — feed aggTrade into the intensity calibrator
    on_inventory(q_norm) — set normalized inventory in [-1, 1]

`state` property returns the latest QuoteState, or None until first valid
snapshot. When σ or (A, k) are not yet calibrated, state.bids and state.asks
are empty tuples (engine declines to quote — Phase-1 cold-start discipline).

GLT gives the inner-most (δ_b, δ_a); the ladder module expands those into
N levels per side using the dispersion unit u. Tick rounding then enforces
strict price monotonicity so no two levels collide on the same tick.

The engine is stateless w.r.t. exchange I/O — caller is responsible for
turning Quote into actual orders.
"""
from __future__ import annotations

import time

import structlog

from biz.domain.book import OrderBookSnapshot
from biz.domain.order import OrderSide
from biz.domain.quote import Quote, QuoteState
from biz.domain.trade import TradeTick
from config import SpreadConfig
from pkg.quant import (
    GLTParams,
    IntensityCalibrator,
    LadderConfig,
    RollingRealizedVol,
    build_ladder,
    inventory_skew_unit,
    quotes,
)


class GltSpreadEngine:

    def __init__(
        self,
        symbol: str,
        cfg: SpreadConfig,
        lg: structlog.stdlib.BoundLogger,
    ) -> None:
        self._symbol = symbol.upper()
        self._cfg = cfg
        self.lg = lg.bind(component="glt_spread", symbol=self._symbol)

        self._vol = RollingRealizedVol(
            window_sec=cfg.vol_window_sec,
            min_samples=cfg.vol_min_samples,
        )
        self._intensity = IntensityCalibrator(
            window_sec=cfg.intensity_window_sec,
            min_trades=cfg.intensity_min_trades,
            min_filled_bins=cfg.intensity_min_filled_bins,
        )
        self._ladder_cfg = LadderConfig(
            n_levels=cfg.ladder_n_levels,
            delta_coef=cfg.ladder_delta_coef,
            weights=tuple(cfg.ladder_weights),
            n_shrink=cfg.ladder_n_shrink,
        )
        self._q_norm: float = 0.0
        self._latest_mid: float | None = None
        self._state: QuoteState | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_book(self, snap: OrderBookSnapshot) -> None:
        if not snap.is_fresh:
            return
        mid = snap.mid_price
        if mid is None:
            return

        ts_ns = time.monotonic_ns()
        self._vol.on_mid(mid, ts_ns)
        self._latest_mid = mid
        self._recompute(snap.venue, mid, ts_ns)

    def on_trade(self, tick: TradeTick) -> None:
        # Use the most recent mid as the reference; this is accurate to within
        # one L2 tick (~100ms on Binance Spot). For a tighter projection, the
        # caller can pre-correlate book and trade events.
        if self._latest_mid is None:
            return
        self._intensity.on_trade(tick.price, self._latest_mid, tick.recv_ts)

    def on_inventory(self, q_norm: float) -> None:
        # Clip defensively; out-of-range inputs are a caller bug but must not
        # crash the engine mid-session.
        if q_norm > 1.0:
            q_norm = 1.0
        elif q_norm < -1.0:
            q_norm = -1.0
        self._q_norm = q_norm

    @property
    def state(self) -> QuoteState | None:
        return self._state

    @property
    def symbol(self) -> str:
        return self._symbol

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _recompute(self, venue: str, mid: float, ts_ns: int) -> None:
        sigma = self._vol.sigma
        intens = self._intensity.params

        if sigma is None or intens is None:
            self._state = self._empty_state(
                venue, mid, ts_ns,
                sigma=sigma or 0.0,
                A=(intens[0] if intens else 0.0),
                k=(intens[1] if intens else 0.0),
            )
            return

        A, k = intens
        try:
            params = GLTParams(gamma=self._cfg.gamma, sigma=sigma, A=A, k=k)
        except ValueError as exc:
            self.lg.warning("glt_params_invalid", error=str(exc), sigma=sigma, A=A, k=k)
            self._state = self._empty_state(venue, mid, ts_ns, sigma=sigma, A=A, k=k)
            return

        q_lot = self._q_norm * self._cfg.Q_max
        delta_b, delta_a = quotes(params, q_lot)
        u = inventory_skew_unit(params)

        # Spread floor: clamp δ so neither side quotes tighter than the
        # adverse-selection band.  Applied before ladder expansion so every
        # level inherits the floor.
        if self._cfg.spread_floor_bps > 0.0:
            floor = self._cfg.spread_floor_bps * mid / 2.0 / 1e4
            delta_b = max(delta_b, floor)
            delta_a = max(delta_a, floor)

        # Negative δ means the model wants to cross — a sign that γ/Q_max are
        # mis-tuned for current σ. Suppress the offending side and log; the
        # other side may still be valid.
        if delta_b < 0.0 or delta_a < 0.0:
            self.lg.warning(
                "glt_quote_cross",
                delta_b=delta_b, delta_a=delta_a,
                sigma=sigma, A=A, k=k, q_norm=self._q_norm,
            )

        bid_levels, ask_levels = build_ladder(
            delta_b=delta_b,
            delta_a=delta_a,
            u=u,
            q_norm=self._q_norm,
            cfg=self._ladder_cfg,
        )

        taper = max(0.0, 1.0 - abs(self._q_norm))
        base_size = self._cfg.lot_size * taper

        # Hard cap: stop adding to inventory once at cap.
        bid_active = self._q_norm < self._cfg.q_hard_cap and delta_b >= 0.0 and base_size > 0.0
        ask_active = self._q_norm > -self._cfg.q_hard_cap and delta_a >= 0.0 and base_size > 0.0

        bids = (
            self._build_bid_quotes(mid, bid_levels, base_size)
            if bid_active else ()
        )
        asks = (
            self._build_ask_quotes(mid, ask_levels, base_size)
            if ask_active else ()
        )

        self._state = QuoteState(
            symbol=self._symbol,
            venue=venue,
            mid=mid,
            bids=bids,
            asks=asks,
            sigma=sigma,
            A=A,
            k=k,
            gamma=self._cfg.gamma,
            q_norm=self._q_norm,
            ts_ns=ts_ns,
        )

        self.lg.debug(
            "glt_quote",
            mid=round(mid, 4),
            delta_b=round(delta_b, 4),
            delta_a=round(delta_a, 4),
            u=round(u, 6),
            n_bids=len(bids),
            n_asks=len(asks),
            sigma=round(sigma, 6),
            A=round(A, 3),
            k=round(k, 6),
            q_norm=round(self._q_norm, 3),
        )

    def _empty_state(
        self, venue: str, mid: float, ts_ns: int,
        sigma: float, A: float, k: float,
    ) -> QuoteState:
        return QuoteState(
            symbol=self._symbol, venue=venue, mid=mid,
            bids=(), asks=(),
            sigma=sigma, A=A, k=k, gamma=self._cfg.gamma,
            q_norm=self._q_norm, ts_ns=ts_ns,
        )

    def _build_bid_quotes(
        self, mid: float, levels, base_size: float,
    ) -> tuple[Quote, ...]:
        # Bid prices = mid - delta, monotonically decreasing in level index.
        tick = self._cfg.price_tick
        out: list[Quote] = []
        prev_px: float | None = None
        for lv in levels:
            raw_px = mid - lv.delta_from_mid
            px = _round_down(raw_px, tick) if tick > 0.0 else raw_px
            # Enforce strict monotonicity after rounding.
            if prev_px is not None and px >= prev_px:
                px = prev_px - tick if tick > 0.0 else prev_px - 1e-12
            if px <= 0.0:
                continue
            size = base_size * lv.size_weight
            if size <= 0.0:
                continue
            out.append(Quote(side=OrderSide.BUY, price=px, size=size))
            prev_px = px
        return tuple(out)

    def _build_ask_quotes(
        self, mid: float, levels, base_size: float,
    ) -> tuple[Quote, ...]:
        tick = self._cfg.price_tick
        out: list[Quote] = []
        prev_px: float | None = None
        for lv in levels:
            raw_px = mid + lv.delta_from_mid
            px = _round_up(raw_px, tick) if tick > 0.0 else raw_px
            if prev_px is not None and px <= prev_px:
                px = prev_px + tick if tick > 0.0 else prev_px + 1e-12
            size = base_size * lv.size_weight
            if size <= 0.0:
                continue
            out.append(Quote(side=OrderSide.SELL, price=px, size=size))
            prev_px = px
        return tuple(out)


def _round_down(x: float, tick: float) -> float:
    return (x // tick) * tick


def _round_up(x: float, tick: float) -> float:
    return -(((-x) // tick) * tick)
