# 项目架构设计

## 分层总览

```
cmd/
├── main.go / main.py        # 程序唯一入口，组装依赖并启动

server/
├── server.py                # 服务生命周期管理（启动/停止/信号处理）
├── handler/                 # 外部触发入口（WebSocket handler、REST、定时任务等）

biz/
├── usecase/                 # 业务逻辑对象（UseCase），核心做市逻辑在此
├── repo/                    # Repo 接口定义（抽象，不含实现）
└── domain/                  # 领域模型、值对象

data/
├── orderbook/               # 实现 biz/repo 中定义的 OrderBookRepo
├── exchange/                # 实现 ExchangeRepo（下单、查仓位）
└── cache/                   # 实现 CacheRepo（本地状态缓存）

pkg/
├── logger/                  # 日志
├── config/                  # 配置加载
├── metrics/                 # 监控埋点
└── math/                    # 公共数学工具（滚动统计、GLT 公式等）
```

## 调用链

```
cmd
 └─► server.New(...)          # 注入所有依赖，启动事件循环
      └─► biz.NewUseCase(...) # 接收 Repo 接口，执行做市逻辑
           └─► data.NewXxxRepo(...)  # 实现 Repo 接口，对接交易所/本地状态
```

---

## 各层职责

### `cmd` — 入口与依赖组装

- 唯一负责读取配置、实例化所有对象、注入依赖。
- **不含任何业务逻辑**，只做"连线"。

```python
# cmd/main.py
def main():
    cfg = config.load("config.toml")

    # data 层
    ob_repo   = data.OrderBookRepo(cfg.exchange)
    exch_repo = data.ExchangeRepo(cfg.exchange)

    # biz 层
    uc = biz.MarketMakingUseCase(
        ob_repo=ob_repo,
        exch_repo=exch_repo,
        cfg=cfg.strategy,
    )

    # server 层
    srv = server.Server(usecase=uc, cfg=cfg.server)
    srv.run()
```

---

### `server` — 生命周期管理

- 负责启动 WebSocket 连接、注册信号处理（SIGTERM/SIGINT）、优雅退出。
- 将外部事件（行情推送、成交回报）路由到 UseCase 的对应方法。
- **不含定价或风控逻辑**。

```python
# server/server.py
class Server:
    def __init__(self, usecase: biz.MarketMakingUseCase, cfg: ServerConfig):
        self._uc  = usecase
        self._cfg = cfg

    async def run(self):
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._stream_orderbook())
            tg.create_task(self._stream_fills())
            tg.create_task(self._quote_loop())

    async def _stream_orderbook(self):
        async for snap in self._cfg.ws.subscribe_orderbook():
            await self._uc.on_orderbook(snap)
```

---

### `biz` — 业务逻辑层（核心）

#### UseCase 对象

- 持有所有 Repo 接口（依赖抽象，不依赖具体实现）。
- 实现做市核心流程：公平价值计算 → 价差定价 → 报价管理 → 库存对冲。

```python
# biz/usecase/market_making.py
class MarketMakingUseCase:
    def __init__(
        self,
        ob_repo:   OrderBookRepo,
        exch_repo: ExchangeRepo,
        cfg:       StrategyConfig,
    ):
        self._ob   = ob_repo
        self._exch = exch_repo
        self._cfg  = cfg

    async def on_orderbook(self, snap: OrderBookSnapshot) -> None:
        mid   = self._ob.mid_price(snap)
        spread, skew = self._pricing_engine.quote(mid, self._inventory())
        await self._exch.replace_quotes(bid=mid - spread/2 + skew,
                                         ask=mid + spread/2 + skew)
```

#### Repo 接口定义

- **接口定义在 biz 层**，data 层负责实现，biz 层永远不 import data。

```python
# biz/repo/orderbook.py
from abc import ABC, abstractmethod

class OrderBookRepo(ABC):
    @abstractmethod
    def mid_price(self, snap: OrderBookSnapshot) -> float: ...

    @abstractmethod
    def best_bid_ask(self, snap: OrderBookSnapshot) -> tuple[float, float]: ...

# biz/repo/exchange.py
class ExchangeRepo(ABC):
    @abstractmethod
    async def replace_quotes(self, bid: float, ask: float) -> None: ...

    @abstractmethod
    async def get_position(self, symbol: str) -> Position: ...
```

---

### `data` — 接口实现层

- 实现 `biz/repo` 中定义的所有抽象接口。
- 对接交易所 REST/WebSocket API、本地 Redis/内存缓存。
- **不含任何定价或策略逻辑**。

```python
# data/exchange/binance.py
from biz.repo.exchange import ExchangeRepo

class BinanceExchangeRepo(ExchangeRepo):
    def __init__(self, client: BinanceClient):
        self._client = client          # 构造函数注入，方便 mock

    async def replace_quotes(self, bid: float, ask: float) -> None:
        await self._client.cancel_all()
        await self._client.place_limit(side="buy",  price=bid)
        await self._client.place_limit(side="sell", price=ask)

    async def get_position(self, symbol: str) -> Position:
        raw = await self._client.get_position(symbol)
        return Position(symbol=symbol, qty=raw["positionAmt"])
```

---

### `pkg` — 公共工具包

- **无业务语义**，任何层都可以 import。
- 典型内容：滚动统计（`RollingVol`）、GLT 公式、日志封装、配置 schema。

```python
# pkg/math/rolling.py
class RollingVol:
    """Welford online variance, O(1) per update."""
    def __init__(self, window: int): ...
    def update(self, price: float) -> None: ...
    def sigma(self) -> float: ...       # annualized vol
```

---

## 依赖方向约束

```
cmd  ──imports──►  server, biz, data, pkg
server  ────────►  biz, pkg
biz     ────────►  pkg          # 严禁 import data
data    ────────►  biz/repo, pkg
```

> **核心约束**：`biz` 永远不 import `data`，只依赖自己定义的抽象接口。这是保证可测试性和可替换性的根本。

---

## 构造函数规范

所有层级对象**只通过构造函数接收依赖**，禁止在方法内部自行创建依赖对象。

```python
# 正确：依赖从外部注入
class MarketMakingUseCase:
    def __init__(self, ob_repo: OrderBookRepo, exch_repo: ExchangeRepo): ...

# 错误：内部硬编码依赖
class MarketMakingUseCase:
    def __init__(self):
        self._exch = BinanceExchangeRepo(BinanceClient(...))  # 不可测试
```

好处：
1. 单元测试可以直接传入 mock 实现，无需启动真实交易所连接
2. 切换交易所（Binance → OKX）只需在 `cmd/main.py` 换一行构造代码
3. 未来接入依赖注入框架（如 `wire`/`lagom`）时无需改动业务代码
