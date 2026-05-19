#!/usr/bin/env python3
"""
Watch GLT-derived quotes for a Binance Spot symbol.

Usage:
    uv run python -m cmd.watch_quote [SYMBOL]
    uv run python -m cmd.watch_quote ETH_USDT

The first ~30-60 s prints "calibrating..." until sigma and (A, k) are ready.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import aiohttp

import config
from pkg import logger
from server.mm_server import MMServer


def _fmt_quote(s: Any) -> str:
    if s.bid is None and s.ask is None:
        return (
            f"{s.symbol:<10} mid={s.mid:>10.4f}  "
            f"calibrating s={s.sigma:.4f} A={s.A:.2f} k={s.k:.4f}"
        )
    bid_str = f"{s.bid.price:.4f}x{s.bid.size:g}" if s.bid else "  -  "
    ask_str = f"{s.ask.price:.4f}x{s.ask.size:g}" if s.ask else "  -  "
    return (
        f"{s.symbol:<10} mid={s.mid:>10.4f}  "
        f"bid={bid_str:<22} ask={ask_str:<22}  "
        f"s={s.sigma:.4f} A={s.A:.1f} k={s.k:.4f} q={s.q_norm:+.2f}"
    )


def _print_state(s: Any) -> None:
    print("\r" + _fmt_quote(s).ljust(140), end="", flush=True)


async def main(symbol: str) -> None:
    cfg = config.load()
    logger.configure(level=cfg.log.level, log_dir=cfg.log.dir)
    lg = logger.get_logger("watch_quote")
    print(f"Watching GLT quotes for {symbol} -- Ctrl-C to stop")
    async with aiohttp.ClientSession() as session:
        srv = MMServer(
            symbol=symbol,
            session=session,
            cfg=cfg,
            on_quote=_print_state,
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
    sym = sys.argv[1].upper() if len(sys.argv) > 1 else "BTC_USDT"
    asyncio.run(main(sym))
