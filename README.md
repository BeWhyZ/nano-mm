# nano-mm

Crypto spot market-making system (Binance + Bybit). Phase 1 focus: single-exchange fair-value
estimation and GLT (Cartea-Jaimungal) spread generation.

## Architecture

```
cmd/mm.py                        ← single MM entry point (grows with each phase)
    │
    ▼
service/fair_value_service.py    ← owns book subscriptions + FairValueEngine
    │  get_fair_price()              multi-exchange fan-in; reference venue first
    │  register_book_listener()      shared book feed (avoids duplicate subscriptions)
    ▼
data/orderbook/  (one tracker per exchange per symbol)
biz/usecase/fair_value.py  (FairValueEngine, one instance per exchange)

server/glt_spread_server.py      ← registers with FairValueService; no own book sub
    │  get_fair_price() for mid reference
    ▼
data/trade/  (AggTrade tracker — Binance today)
biz/usecase/glt_spread.py  (GltSpreadEngine: vol + intensity calibration → quotes)
```

### Layer responsibilities

| Layer | Directory | Role |
|---|---|---|
| Entry points | `cmd/` | Bootstrap config, logger, session; wire services |
| Service | `service/` | Owns infra lifecycle; exposes domain interfaces (`get_fair_price`) |
| Server | `server/` | Thin orchestrators — only call service, no direct tracker/engine construction |
| Use Case | `biz/usecase/` | Stateful business logic (fair-value, GLT) |
| Domain | `biz/domain/` | Immutable value objects |
| Repo | `biz/repo/` | Abstract port interfaces |
| Data | `data/` | Concrete repo implementations (WS trackers, REST) |
| Quant | `pkg/quant/` | Stateless math primitives (GLT formula, vol, intensity) |
| Utils | `pkg/` | Logger, metrics, symbol, exchange constants |

### Exchange enum

All exchange identities are declared in `pkg/constant/__init__.py` as `Exchange(StrEnum)`.
The tracker factory in `data/orderbook/__init__.py` maps `Exchange → OrderBookRepo`.

## Debug commands

```bash
# Live fair-value metrics (mid, micro, spread, OBI)
uv run python -m cmd.watch_book BTC_USDT

# Live GLT quotes (bid/ask, σ, A, k, inventory)
uv run python -m cmd.watch_quote BTC_USDT
```

## Development

```bash
# Add / remove dependencies
uv add <package>
uv remove <package>

# Run tests
uv run pytest

# Lint & format
uv run ruff check .
uv run ruff format .

# Type check
uv run mypy .

# Sync after pulling
uv sync
```
