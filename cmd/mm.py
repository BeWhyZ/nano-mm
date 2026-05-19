#!/usr/bin/env python3
"""
mm — main market-making entry point.

This command grows with each development phase:
  Phase 1 (current): fair-value estimation + GLT spread generation
  Phase 2+: order management, inventory control, risk limits

Usage:
    uv run python -m cmd.mm [SYMBOL]
    uv run python -m cmd.mm BTC_USDT

Debug mode (raw data-layer orderbook viewer):
    Set config.debug.enabled = true, then:
    uv run python -m cmd.mm [--exchange EXCHANGE] [--symbol SYMBOL] [--depth N]
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import aiohttp

import config
from pkg import logger
from server.mm_server import MMServer

_TTY = sys.stdout.isatty()
_RED   = "\033[31m" if _TTY else ""
_GREEN = "\033[32m" if _TTY else ""
_CYAN  = "\033[36m" if _TTY else ""
_DIM   = "\033[2m"  if _TTY else ""
_RST   = "\033[0m"  if _TTY else ""

_ob_lines = 0


def _on_quote(s: Any) -> None:
    if s.bid is None and s.ask is None:
        line = (
            f"{s.symbol:<10} mid={s.mid:>10.4f}  "
            f"calibrating s={s.sigma:.4f} A={s.A:.2f} k={s.k:.4f}"
        )
    else:
        bid_str = f"{s.bid.price:.4f}x{s.bid.size:g}" if s.bid else "  -  "
        ask_str = f"{s.ask.price:.4f}x{s.ask.size:g}" if s.ask else "  -  "
        line = (
            f"{s.symbol:<10} mid={s.mid:>10.4f}  "
            f"bid={bid_str:<22} ask={ask_str:<22}  "
            f"s={s.sigma:.4f} A={s.A:.1f} k={s.k:.4f} q={s.q_norm:+.2f}"
        )
    print("\r" + line.ljust(140), end="", flush=True)


def _print_book(snap: Any, depth: int) -> None:
    global _ob_lines
    mid = snap.mid_price
    age = snap.age_ms()
    asks = snap.asks[:depth]
    bids = snap.bids[:depth]

    col_w = 12
    lines: list[str] = [
        f"{_CYAN}{snap.symbol:<12}{_RST}  {snap.venue}  seq={snap.seq}  age={age:>5.1f}ms",
        f"  {'PRICE':>{col_w}}  {'QTY':>{col_w}}",
        f"  {'─' * (col_w * 2 + 4)}",
    ]
    for level in reversed(asks):
        lines.append(f"  {_RED}{level.price:>{col_w}.6g}{_RST}  {level.qty:>{col_w}.6g}")
    if mid is not None:
        lines.append(f"  {_DIM}{'─' * 8} mid {mid:.6g} {'─' * 8}{_RST}")
    else:
        lines.append(f"  {'─' * (col_w * 2 + 4)}")
    for level in bids:
        lines.append(f"  {_GREEN}{level.price:>{col_w}.6g}{_RST}  {level.qty:>{col_w}.6g}")

    if _ob_lines and _TTY:
        sys.stdout.write(f"\033[{_ob_lines}A\033[J")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()
    _ob_lines = len(lines)


_DEBUG_DEPTH = 10


async def run_debug(cfg: config.Config, symbol: str) -> None:
    from data.orderbook import make_orderbook_tracker
    from pkg.constant import Exchange

    lg = logger.get_logger("mm.debug")
    print(f"[debug] data-layer orderbook  binance_spot / {symbol}  -- Ctrl-C to stop\n")

    async with aiohttp.ClientSession() as session:
        tracker = make_orderbook_tracker(
            exchange=Exchange.BINANCE_SPOT,
            symbol=symbol,
            session=session,
            on_update=lambda snap: _print_book(snap, _DEBUG_DEPTH),
            lg=lg,
            proxy=cfg.net.http_proxy,
        )
        try:
            await tracker.run()
        except asyncio.CancelledError:
            pass
        finally:
            if _TTY:
                print()


async def run_mm(symbol: str, cfg: config.Config) -> None:
    lg = logger.get_logger("mm")
    lg.info("mm_start", symbol=symbol)
    print(f"Starting MM for {symbol} -- Ctrl-C to stop")

    async with aiohttp.ClientSession() as session:
        srv = MMServer(
            symbol=symbol,
            session=session,
            cfg=cfg,
            on_quote=_on_quote,
            lg=lg,
            proxy=cfg.net.http_proxy,
        )
        try:
            await srv.run()
        except asyncio.CancelledError:
            pass
        finally:
            print()


if __name__ == "__main__":
    cfg = config.load()
    logger.configure(level=cfg.log.level, log_dir=cfg.log.dir)

    sym = sys.argv[1].upper() if len(sys.argv) > 1 else "BTC_USDT"
    try:
        if cfg.pricing_engine.debug:
            asyncio.run(run_debug(cfg, sym))
        else:
            asyncio.run(run_mm(sym, cfg))
    except KeyboardInterrupt:
        pass
