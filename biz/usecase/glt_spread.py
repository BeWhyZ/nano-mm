"""
GLT (Cartea-Jaimungal asymptotic) spread engine.

Three event entry points:
    on_book(snap)       — refresh σ, recompute quote (the heartbeat)
    on_trade(tick)      — feed aggTrade into the intensity calibrator
    on_inventory(q_norm) — set normalized inventory in [-1, 1]

`state` property returns the latest QuoteState, or None until first valid
snapshot. When σ or (A, k) are not yet calibrated, state.bid and state.ask
are None (engine declines to quote — Phase-1 cold-start discipline).

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
from pkg.quant import GLTParams, IntensityCalibrator, RollingRealizedVol, quotes


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
            self._state = QuoteState(
                symbol=self._symbol,
                venue=venue,
                mid=mid,
                bid=None,
                ask=None,
                sigma=sigma or 0.0,
                A=(intens[0] if intens else 0.0),
                k=(intens[1] if intens else 0.0),
                gamma=self._cfg.gamma,
                q_norm=self._q_norm,
                ts_ns=ts_ns,
            )
            return

        A, k = intens
        try:
            params = GLTParams(gamma=self._cfg.gamma, sigma=sigma, A=A, k=k)
        except ValueError as exc:
            self.lg.warning("glt_params_invalid", error=str(exc), sigma=sigma, A=A, k=k)
            self._state = QuoteState(
                symbol=self._symbol, venue=venue, mid=mid,
                bid=None, ask=None,
                sigma=sigma, A=A, k=k, gamma=self._cfg.gamma,
                q_norm=self._q_norm, ts_ns=ts_ns,
            )
            return

        q_lot = self._q_norm * self._cfg.Q_max
        delta_b, delta_a = quotes(params, q_lot)

        # Negative δ means the model wants to cross — a sign that γ/Q_max are
        # mis-tuned for current σ. Suppress the offending side and log; the
        # other side may still be valid.
        if delta_b < 0.0 or delta_a < 0.0:
            self.lg.warning(
                "glt_quote_cross",
                delta_b=delta_b, delta_a=delta_a,
                sigma=sigma, A=A, k=k, q_norm=self._q_norm,
            )

        bid_px = mid - delta_b
        ask_px = mid + delta_a
        if self._cfg.price_tick > 0.0:
            bid_px = _round_down(bid_px, self._cfg.price_tick)
            ask_px = _round_up(ask_px, self._cfg.price_tick)

        taper = max(0.0, 1.0 - abs(self._q_norm))
        base_size = self._cfg.lot_size * taper

        # Hard cap: stop adding to inventory once at cap.
        bid_size = base_size if self._q_norm < self._cfg.q_hard_cap else 0.0
        ask_size = base_size if self._q_norm > -self._cfg.q_hard_cap else 0.0

        bid = (
            Quote(side=OrderSide.BUY, price=bid_px, size=bid_size)
            if bid_size > 0.0 and delta_b >= 0.0
            else None
        )
        ask = (
            Quote(side=OrderSide.SELL, price=ask_px, size=ask_size)
            if ask_size > 0.0 and delta_a >= 0.0
            else None
        )

        self._state = QuoteState(
            symbol=self._symbol,
            venue=venue,
            mid=mid,
            bid=bid,
            ask=ask,
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
            bid_px=round(bid_px, 4) if bid else None,
            ask_px=round(ask_px, 4) if ask else None,
            bid_size=bid_size,
            ask_size=ask_size,
            sigma=round(sigma, 6),
            A=round(A, 3),
            k=round(k, 6),
            q_norm=round(self._q_norm, 3),
        )


def _round_down(x: float, tick: float) -> float:
    return (x // tick) * tick


def _round_up(x: float, tick: float) -> float:
    return -(((-x) // tick) * tick)
