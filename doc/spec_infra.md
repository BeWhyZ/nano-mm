# nano-mm 基础设施层规范（阶段一）

本文档约束阶段一的两个子系统：**L2 订单簿同步** 与 **订单状态机**。规范优先于实现；任何实现偏离此处的语义都视为 bug。

范围：
- 现货双所：Binance Spot（主做市所）+ Bybit V5 Spot（参考所）
- OB 本地镜像：top-20 视图，差分模式（Binance 全量 diff + REST snapshot；Bybit V5 自带 snapshot+delta）
- OMS：完整状态机，支持 ghost fill、in-flight 重连

---

## 1. L2 订单簿同步协议

### 1.1 三个时间戳约定

所有 OB / fill / order event 必须同时携带：

| 字段 | 含义 | 用途 |
|------|------|------|
| `event_ts` | 交易所撮合/事件发生时间 | lead-lag、信号 |
| `send_ts` | 交易所推送时间（若提供） | 单边延迟测量 |
| `recv_ts` | 本地 socket 接收到的单调时钟 | 端到端延迟、过期判断 |

`recv_ts` 用 `time.monotonic_ns()`，不要用 `time.time()`（系统时钟回拨会破坏时序）。

### 1.2 Binance Spot（主做市所）

**WS**：`wss://stream.binance.com:9443/stream?streams=<symbol>@depth@100ms`

每条 diff 消息字段：
- `E` — event time (ms)
- `U` — first update ID in this event
- `u` — final update ID in this event
- `b` — bid updates `[[price, qty], ...]`，`qty==0` 表示删除该档
- `a` — ask updates，同上

**REST snapshot**：`GET https://api.binance.com/api/v3/depth?symbol=<S>&limit=1000`
返回 `lastUpdateId` 与全量 `bids/asks`。

**同步算法**（必须严格按此顺序，参考 Binance 官方文档 "How to manage a local order book correctly"）：

1. 打开 WS，立即开始 buffer 所有 diff event；**不要丢任何一条**。
2. 等 WS 进入 stable 状态（≥1 条 event）后，REST 拉 snapshot，记其 `lastUpdateId = S`。
3. 丢弃 buffer 中所有 `u < S` 的 event。
4. 第一条要应用的 event 必须满足 `U ≤ S+1 AND u ≥ S+1`。否则丢 snapshot 重拉。
5. 之后每条 event 必须满足 `U == prev.u + 1`。**任何一条不满足 → 整本 OB 作废 → 回到 step 1**。
6. 应用 event：对每个 `[price, qty]`，`qty == 0` 删档，否则覆盖该档。

**踩坑**：
- Binance diff 是 **累积式**：同一个 100ms 窗口内多次更新合并；不能当成 incremental tick 处理。
- 在 step 3-4 之间错位过的 OB 是最难发现的 bug，因为 BBO 看起来正常，但 mid 和深层 price level 错了。
- 重连后 **必须**重新走完整流程，不能复用旧 OB。

### 1.3 Bybit V5 Spot（参考所）

**WS**：`wss://stream.bybit.com/v5/public/spot`

订阅：`{"op": "subscribe", "args": ["orderbook.50.BTCUSDT"]}`（深度档位：1/50/200，spot 上 50 已经够）

消息结构：
```json
{
  "topic": "orderbook.50.BTCUSDT",
  "type": "snapshot" | "delta",
  "ts": <ms>,            // exchange send time
  "data": {
    "s": "BTCUSDT",
    "b": [[price, qty], ...],
    "a": [[price, qty], ...],
    "u": <seq>,          // update id
    "seq": <cross_seq>   // matching engine cross-sequence
  }
}
```

**同步算法**：

1. 订阅后第一条消息 `type=="snapshot"`：清空本地 OB，写入全量。
2. 之后所有 `type=="delta"`：必须满足 `data.u == prev.u + 1`。**否则**整本作废，重新订阅，等下一个 snapshot。
3. delta 应用规则与 Binance 一致：`qty==0` 删档，否则覆盖。

**注意**：
- Bybit V5 spot 的 snapshot 不需要单独 REST，订阅时自带 — 比 Binance 简单。
- `seq`（cross_seq）不是连续的，**不能**用它做 gap 检测；用 `u`。
- 偶现 `type` 字段缺失或 `data.u` 不存在的情况要防御（mark dirty，强制 resnapshot）。

### 1.4 本地 OB 数据结构

- 内部存全量 levels（Binance 可能上千档，Bybit 50 档），用 `SortedDict` 或 sorted arrays（bid 降序、ask 升序）。
- 对外暴露 `top_k(k=20)` 视图；**不要**只存 top-20，因为 top-N 之外的更新会在某天突然挤入 top-20，丢失中间状态。
- 关键 API（在 `biz/repo/orderbook.py` 抽象）：
  - `best_bid_ask() -> (bid_px, bid_qty, ask_px, ask_qty)` — 不会跨 await
  - `top_k(k: int) -> OrderBookSnapshot`
  - `mid_price() -> float`
  - `micro_price(k: int = 5) -> float` — 加权 mid
  - `seq() -> int` — 当前应用到的最后 update id；策略层用它做 staleness 检查

### 1.5 重连与降级

- **心跳超时**：3 秒无任何消息 → 主动 close 并重连。Binance/Bybit 都自身有 ping/pong，但不要依赖它，自己加一层 watchdog。
- **重连退避**：`min(2^n * 100ms, 5s)`，n 是连续失败次数。
- **降级**：连续 3 次重连 + resnapshot 失败 → 标记该 symbol-venue 为 `STALE`，UseCase 应该在 BBO 用之前先 check `is_fresh()`，stale 则**完全停报价**而不是用旧数据。

### 1.6 归档（第一天就上）

- 原始 WS frame（解压后 JSON 字节）按 `(venue, channel, recv_ts_date)` 切片，写 zstd 压缩的 parquet。每行 schema:
  - `recv_ts: int64`
  - `event_ts: int64`
  - `venue: str`
  - `channel: str`
  - `payload: bytes`（原始 JSON，便于回放 + 兼容字段升级）
- 单进程 producer → asyncio.Queue → 单独 writer task。**writer 不能阻塞 ingest**：queue 满了直接 drop 并埋点告警，**永远**不要 backpressure 到 socket。

---

## 2. 订单状态机

参考 hummingbot 的 `InFlightOrder` 模型，但去掉对其 connector 框架的耦合。

### 2.1 状态定义

```
            ┌─────────────┐
            │ PENDING_NEW │  本地已生成 client_order_id，submit 在途
            └──────┬──────┘
                   │  ack（含 exchange_order_id）
                   ▼
            ┌──────────┐         partial fill          ┌────────────────────┐
            │   OPEN   │ ─────────────────────────────►│  PARTIALLY_FILLED  │
            └────┬─────┘                                └──────┬─────────────┘
                 │ full fill                                    │ full fill
                 │ ─────────────────────────────────────────►   ▼
                 │                                       ┌────────────┐
                 │                                       │   FILLED   │ (terminal)
                 │                                       └────────────┘
                 │ submit cancel                                ▲ partial → 余量被 cancel
                 ▼                                              │
            ┌────────────────┐    cancel ack                    │
            │ PENDING_CANCEL │ ──────────────────► ┌────────────┴───┐
            └────────────────┘                     │    CANCELED    │ (terminal)
                 │                                 └────────────────┘
                 │ fill (ghost fill: 合法!)
                 ▼
              (back to PARTIALLY_FILLED, 等下一个 cancel ack 或 full fill)

  PENDING_NEW ──reject──► REJECTED (terminal)
  PENDING_NEW ──timeout──► FAILED (terminal, 需要 REST 对账)
  任何活动态 ──TIF 到期──► EXPIRED (terminal)
```

### 2.2 关键转移规则

| 起始状态 | 事件 | 目标状态 | 备注 |
|----------|------|----------|------|
| PENDING_NEW | `ack` (with exchange_order_id) | OPEN | 记录 `exchange_order_id` |
| PENDING_NEW | `reject` | REJECTED | 终态；记录原因 |
| PENDING_NEW | `partial_fill` | PARTIALLY_FILLED | 合法：mtaker 极快 |
| PENDING_NEW | `full_fill` | FILLED | 合法 |
| PENDING_NEW | timeout（无响应 > 5s） | FAILED | 必须 REST 对账，不允许重试 submit |
| OPEN | `partial_fill` | PARTIALLY_FILLED | |
| OPEN | `full_fill` | FILLED | 终态 |
| OPEN / PARTIALLY_FILLED | `submit_cancel` | PENDING_CANCEL | 本地动作 |
| PENDING_CANCEL | `cancel_ack` | CANCELED | 终态 |
| PENDING_CANCEL | `partial_fill` | PENDING_CANCEL | **ghost fill 合法**，更新 filled_qty，仍等 cancel ack |
| PENDING_CANCEL | `full_fill` | FILLED | ghost fill 把单子吃满了，cancel ack 会变成 `CancelRejected`，忽略即可 |
| PENDING_CANCEL | `cancel_reject` | 回到 fill 前状态 | 一般是因为已成交 |

### 2.3 不变量（必须用断言/property test 覆盖）

1. `filled_qty + remaining_qty == original_qty`，永远成立（在状态转移之后）
2. `filled_qty` 只能单调增
3. 终态进入后不可再变；任何对终态订单的事件必须打 warning + drop（不能 raise）
4. 每个 `Fill` 用 `trade_id` 全局去重；同一个 trade_id 第二次到达 → drop
5. Fill 乱序到达：用 `event_ts` 排序入队，超过窗口（500ms）的乱序 → 告警但仍应用

### 2.4 Ghost fill 处理

具体场景：本地 `t0` 发出 cancel，`t1` exchange 已经撮合了一部分，`t2` 我们收到 fill，`t3` 我们收到 cancel ack（reject 或 partial-canceled）。

正确处理：
- `t2`：在 PENDING_CANCEL 状态下接受 fill，更新 `filled_qty`、`remaining_qty`，更新库存 `q`，**保持** PENDING_CANCEL。
- `t3`：
  - 如果 cancel_ack 带 `canceled_qty > 0`：进 CANCELED，最终 `filled + canceled == original`。
  - 如果 fill 已经吃满（`filled_qty == original_qty`）：忽略 cancel reject，状态进 FILLED（如果还没进）。

**禁止**：在 PENDING_CANCEL 收到 fill 后 raise / drop 该 fill。这是 hummingbot 早期版本踩过的坑，会导致库存少算。

### 2.5 库存同步

每个 fill 必须在状态机 apply 之后**同一个事务内**更新本地 inventory：

```python
# 伪代码
def on_fill(self, fill: Fill) -> None:
    order = self._orders[fill.client_order_id]
    if fill.trade_id in order.applied_trade_ids:
        return  # dedup
    order.apply_fill(fill)
    self._inventory.apply_fill(fill)  # 同步更新 q
    order.applied_trade_ids.add(fill.trade_id)
```

不要把 inventory 更新放在 "after all events processed" — 中间状态被读到就是错的。

### 2.6 启动期对账

进程启动时（包括崩溃重启）：
1. REST 拉所有 open orders；
2. REST 拉最近 N 分钟的 fills；
3. 与本地持久化的订单状态对比，差异部分以 exchange 为准；
4. 对账完成前**不允许**发新订单。

---

## 3. 关键失败模式（开发期主动埋点）

| 失败模式 | 监控指标 | 阈值 | 响应 |
|---------|---------|------|------|
| OB seq gap | `ob_seq_gap_total{venue,symbol}` | > 0 触发 resnapshot；> 5/min 告警 | 整本 OB 作废 + 重订阅 |
| OB stale | `ob_age_ms{venue,symbol}` | > 500ms | 报价层暂停 |
| End-to-end latency | `ws_recv_lag_ms` (recv_ts - event_ts) | p99 > 200ms | 检查网络/colo |
| Order timeout | `order_timeout_total` | > 0 立即对账 | REST reconcile |
| Ghost fill | `ghost_fill_total` | 仅统计，不告警 | — |
| Cancel reject (already filled) | `cancel_reject_filled_total` | > N/min 告警 | 库存可能漂移，强制对账 |

---

## 4. 实现优先级

1. 域模型：`OrderBookSnapshot`、`Order`、`Fill`、`OrderStatus`（biz/domain）
2. Repo 接口：`OrderBookRepo`、`ExchangeRepo`、`OrderTrackerRepo`（biz/repo）
3. Binance Spot OB tracker（含 sequence sync + reconnect）
4. Bybit V5 Spot OB tracker
5. OMS 状态机（纯逻辑，不依赖任何交易所）
6. Binance Spot exchange adapter（submit/cancel/fill stream）
7. Bybit Spot exchange adapter
8. 归档 writer（parquet）
9. Server 装配 + cmd/main.py

每一步都要有 unit test（OB sync 的序号边界情况、状态机的所有转移）。集成测试用录制的 WS pcap 回放。
