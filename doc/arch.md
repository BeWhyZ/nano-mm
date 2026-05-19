# 项目架构设计

## 分层总览

```
cmd/
├── mm.py                    # 程序唯一入口，只读配置、创建 server、调 run()

server/
├── mm_server.py             # 完整做市服务：内部创建并连线所有 service
├── fair_value_server.py     # 公平价服务（debug 用：watch_book）
├── glt_spread_server.py     # GLT 报价服务（内部持有 FairValueService）

service/
├── fair_value_service.py    # API 定义：get_fair_price() / register_book_listener()
│                            # 内部：持有 data 层 trackers + biz/usecase/FairValueEngine

biz/
├── usecase/                 # 业务逻辑对象（UseCase），核心做市逻辑在此
├── repo/                    # Repo 接口定义（抽象，不含实现）
└── domain/                  # 领域模型、值对象

data/
├── orderbook/               # 实现 OrderBookRepo（BinanceSpot、BybitSpot + 工厂函数）
├── exchange/                # 实现 ExchangeRepo（下单、查仓位）
└── trade/                   # 实现 TradeStreamRepo（aggTrade 流）

pkg/
├── logger/                  # 日志
├── constant/                # Exchange 枚举、ExchangeApi 常量
├── metrics/                 # 监控埋点
└── quant/                   # 公共数学工具（RollingVol、GLT 公式、IntensityCalibrator）
```

## 调用链

```
cmd
 └─► server.MMServer(cfg, session, ...)   # 只组装，只启动
      └─► service.FairValueService(...)   # 暴露 API，管理订阅生命周期
      │    └─► data.make_orderbook_tracker(exchange, ...)  # 具体 WS 连接
      │    └─► biz.FairValueEngine(...)                    # 公平价计算
      │
      └─► server.GltSpreadServer(fair_svc, ...)
           └─► fair_svc.register_book_listener(...)  # 复用已有订阅
           └─► data.BinanceSpotAggTradeTracker(...)  # 独立成交流
           └─► biz.GltSpreadEngine(...)              # GLT 报价计算
```

---

## 各层职责

### `cmd` — 入口，只做"启动"

- 读取配置、初始化日志、创建 aiohttp session。
- **只** import `server` 层，创建 server 对象后调用 `run()`。
- 不感知任何 service / biz / data 细节。

```python
# cmd/mm.py
async def main(symbol: str) -> None:
    cfg = config.load()
    async with aiohttp.ClientSession() as session:
        srv = MMServer(symbol, session, cfg, on_quote=_on_quote, lg=lg)
        await srv.run()
```

---

### `server` — 生命周期管理

- 负责**创建并连线** service 层对象，启动所有异步任务，处理 SIGTERM/Ctrl-C。
- **不含**定价、风控、业务决策逻辑。
- 可以 import `service`，不应直接 import `biz/usecase` 或 `data`。

```python
# server/mm_server.py
class MMServer:
    def __init__(self, symbol, session, cfg, on_quote, lg, ...):
        self._fair_svc = FairValueService(symbol, exchanges, session, cfg.pricing_engine, lg)
        self._glt = GltSpreadServer(symbol, self._fair_svc, session, cfg.spread_engine, on_quote, lg)

    async def run(self) -> None:
        await asyncio.gather(self._fair_svc.run(), self._glt.run())
```

---

### `service` — API 定义层（核心新增）

- **对外**：定义稳定的业务接口（如 `get_fair_price()`），供 server 层消费。
- **对内**：调用 `biz/usecase` 中的 UC 实现业务逻辑；管理 `data` 层 trackers 的生命周期。
- 是 `biz` 与 `data` 的组合根（composition root）：它知道"用哪个交易所的哪个 UC 来回答这个问题"。

```python
# service/fair_value_service.py
class FairValueService:
    """
    对外接口：
      get_fair_price(exchange?)          -> FairPriceState | None
      register_book_listener(exchange, cb)  供 GLT 等复用订阅，避免重复 WS 连接

    内部：
      为每个 exchange 创建 OrderBookRepo (data) + FairValueEngine (biz/usecase)
      WS 回调 -> engine.on_tick() -> 通知所有 listener
    """
    def __init__(self, symbol, exchanges: list[Exchange], session, cfg, lg): ...
    def get_fair_price(self, exchange=None) -> FairPriceState | None: ...
    def register_book_listener(self, exchange, cb) -> None: ...
    async def run(self) -> None: ...
```

---

### `biz` — 业务逻辑层（核心，不感知外部世界）

- `usecase/`：持有 Repo 接口，执行做市核心流程（公平价计算、GLT 定价）。
- `repo/`：接口定义在 `biz` 层，`data` 层负责实现，`biz` 永远不 import `data`。
- `domain/`：领域模型、值对象（OrderBookSnapshot、TradeTick、QuoteState…）。

```python
# biz/usecase/fair_value.py
class FairValueEngine:
    def on_tick(self, snap: OrderBookSnapshot) -> None: ...
    @property
    def state(self) -> FairPriceState | None: ...

# biz/usecase/glt_spread.py
class GltSpreadEngine:
    def on_book(self, snap: OrderBookSnapshot) -> None: ...
    def on_trade(self, tick: TradeTick) -> None: ...
    def on_inventory(self, q_norm: float) -> None: ...
    @property
    def state(self) -> QuoteState | None: ...
```

---

### `data` — Repo 实现层

- 实现 `biz/repo` 中定义的所有抽象接口。
- 对接交易所 REST / WebSocket API。
- **不含任何定价或策略逻辑**。
- `data/orderbook/__init__.py` 提供工厂函数 `make_orderbook_tracker(exchange, ...)` 将 `Exchange` 枚举映射到具体 tracker。

---

### `pkg` — 公共工具包

- **无业务语义**，任何层都可以 import。
- `pkg/constant/`：`Exchange(StrEnum)` 枚举 + `ExchangeApi` 常量（REST/WS URL）。
- `pkg/quant/`：GLT 公式、RollingRealizedVol、IntensityCalibrator。

---

## 依赖方向约束

```
cmd      ──────────►  server, pkg
server   ──────────►  service, pkg
service  ──────────►  biz, data, pkg
data     ──────────►  biz/repo, pkg
biz      ──────────►  pkg          # 严禁 import data
```

> **核心约束**：`biz` 永远不 import `data`，只依赖自己定义的抽象接口。
> `cmd` 永远不 import `service`、`biz`、`data`，只调用 `server`。

---

## 构造函数规范

所有层级对象**只通过构造函数接收依赖**，禁止在方法内部自行创建依赖对象。

```python
# 正确：依赖从外部注入
class GltSpreadEngine:
    def __init__(self, symbol: str, cfg: SpreadConfig, lg: BoundLogger): ...

# 错误：内部硬编码依赖
class GltSpreadEngine:
    def __init__(self):
        self._vol = RollingRealizedVol(window_sec=30)   # 参数无法覆盖，测试困难
```

好处：
1. 单元测试可以直接传入 mock / stub，无需启动真实交易所连接。
2. 切换交易所（Binance → OKX）只需在 service 层换一行构造代码，biz 完全不动。
3. 未来接入依赖注入框架（如 `lagom`）时无需改动业务代码。
