# WIP — 下一步要做的事

> 当前 `paper_mm` 闭环已通，但**还没用归档数据评估过策略**。这是当前最大的盲点。
> 本文档定义"评估 → 分流"的两阶段路线，**不写未优先级化的扩展清单**。

---

## 0. 当前实现位置（对照 `intro.md` §4 路线图）

| Layer | 状态 | 落点 |
|---|---|---|
| 1 多场所行情（Binance/Bybit OB + AggTrade，seq gap/重连） | ✅ | `data/orderbook/*`、`data/trade/*`（含 `bybit_spot.py`） |
| 2 OMS（含 ghost fill / PENDING_CANCEL） | ✅ | `data/exchange/oms.py` |
| 3 Paper trading（FIFO FillSimulator + QuoteDiffer + PnL） | ✅ | `cmd/paper_mm.py` 全闭环 |
| 4 公平价（mid/micro/OBI） | ✅ | `FairValueEngine` |
| 5 GLT 价差（σ/A/k 滚动校准、q 偏斜） | ✅ | `GltSpreadEngine` |
| 6 Ladder 形态 | ✅ | `pkg/quant/ladder.py` |
| 7 LOB 结构感知 | ❌ | — |
| 8 库存对冲 | ❌（阶段一不做） | — |
| 9 逆向选择监控 | ⚠️ 离线 `markout_backfill` + `replay_review` 报告就绪；**在线 Tier 1 仍缺** | `data/archive/markout_backfill.py`、`cmd/replay_review.py` |
| 10 极端市场检测 | ❌ | — |
| 11 风控硬约束 | ❌（CLAUDE.md 阶段一明确要求） | — |

---

## 1. 优先级：先评估再扩展

**不要急着上层 7-10。** 当前 GLT 参数（`γ=0.1, Q_max=10, vol_window=30s, lot=0.001`）在真实行情下的真实表现是黑盒——不知道是亏是赚、toxic 占比多少、哪条路径延迟卡了你。

**先用现有归档建立 baseline，再根据 baseline 暴露的问题决定下一步做哪一层。**

---

## 2. Step 0 — 硬阻塞：补 Bybit AggTrade tracker  ✅ 已完成

> 落点：`data/trade/bybit_spot.py` + `make_trade_tracker` 新增 `BYBIT_SPOT` 分支。
> 验证：当前 paper session (`target=bybit_spot, reference=binance_spot`) 已能产生 157 个 fill、aggTrade 驱动 FillSimulator 正常。

历史背景（保留作参考）：`target = bybit_spot` 之后，`paper_mm` 启动就崩。原因：

- `MMService.__init__` 把 `_trade_tracker` 建在 target 上（`mm_service.py:85`，`exchange=exchange`）。
- `data/trade/__init__.py:make_trade_tracker` 当前只支持 `BINANCE_SPOT`，其他 case 直接 `raise ValueError`。
- `FillSimulator` 完全靠 target 的 aggTrade 驱动模拟成交（`paper_executor_service.py:212`）—— target 没成交流，paper 闭环跑不通。

**要做：** 新建 `data/trade/bybit_spot.py`，实现 `TradeStreamRepo`。

| 项 | 值 |
|---|---|
| WS endpoint | `wss://stream.bybit.com/v5/public/spot`（已被 OB tracker 用，可共用连接也可分开） |
| 订阅 | `{"op": "subscribe", "args": ["publicTrade.BTCUSDT"]}` |
| 消息 schema | `data: [{T: ms, p: price, v: qty, S: "Buy"\|"Sell", i: trade_id, BT: false}]` |
| `TradeTick.side` 映射 | `S=="Buy"` → BUY aggressor（taker 买）；和 Binance `is_buyer_maker=false` 一致 |
| 重连 / seq | aggTrade 流没有 seq gap 概念，断线重连即可，丢的成交不补 |
| `recv_ts` | `time.monotonic_ns()`，同 Binance |

把 `make_trade_tracker` 的 match 分支加一条 `case Exchange.BYBIT_SPOT` 即收工。

**为什么 Step 1 才做不到这件事就能开始：** baseline 必须基于 target 真实的成交模拟，否则 fills 表是空的，全部下游分析都没数据。

---

## 3. Step 1 — 必做：cross-venue + 24h paper + baseline 复盘

### 3.1 前置：切到 cross-venue（必须）

当前 `target == reference == binance_spot`，`mid_ref_at_fill` 和 `mid_target` 是同一本 OB ——
**mark-out 会被自己的报价污染**（自家 ask 被吃 → 自家 mid 上移 → markout 看起来正的，假象）。

机构典型配置：在 spread 更宽的 venue 挂单，用价格发现所做参考。

```yaml
# etc/nano-mm.yaml
venues:
  target: bybit_spot          # 做市所（spread 宽，对手少）
  reference: binance_spot     # 参考所（价格发现）
```

**注意 [Ex-1] 部署铁律：** 真盘节点必须靠近 **bybit**（不是 binance）。stale price 是模型问题（可补偿），stale cancel 是 PnL 漏洞。Phase 1 paper 不下真单可以忽略，但要意识到这个矛盾。

**另一个隐含问题：** `price_tick` 当前在 yaml 里硬编码 `0.01`（Binance BTC/USDT 用的）。Bybit BTC/USDT 也是 `0.01`，目前一致；切换 symbol 时要 double check 两边的 tick 是否一致（不一致需要重新设计 QuoteDiffer 的量化逻辑）。

### 3.2 跑 paper（6~24h）

```bash
uv run python -m cmd.paper_mm BTC_USDT
```

观察要点已经写在 `paper_executor.md` §9，跑的时候肉眼盯一遍：
- 标定完成（30~60s）后是否稳定输出 ladder
- 出现 fill 后 q_norm 是否真的回灌到下一档 ladder

### 3.3 写 `cmd/replay_review.py`（一次性脚本，~150 行）

读 SQLite + Parquet 归档，输出 baseline 报告。要回答的具体问题：

| 维度 | 指标 | 期望/警戒值 |
|---|---|---|
| 成交质量 | `markout_1s` / `5s` / `60s` 的 mean / median / P10 / P90 | 1s 均值 ≥ −0.3 tick |
| 毒性占比 | `toxic_ratio` = P(markout_1s < −0.5 tick) | < 30% |
| Spread 捕获 | `realized_spread / quoted_spread` | ≥ 0.3 |
| Ladder 分桶 | 按 `ladder_level` 分桶看 markout — toxic 是否集中在最内档 | 内外档 markout 差距 < 1 tick |
| 库存 | q_norm 时间序列、cap 触达次数、单边滞留时长 | 不长时间贴 cap |
| 拒单 | `order_events.event_type='REJECT'` 计数 + reason 分布 | post_only_cross 偶发 |
| 决策延迟 | `quote_emit_ts → fill.recv_ts` P50/P99（不是路径延迟，是"挂多久才有成交"） | 用于和 σ/spread 联立分析 |

**输出格式：** 直接 `print` 表格（用 `tabulate` 或手撸 f-string），不要做 dashboard——一次性脚本，看完就改。

### 3.4 Step 1 完成的判断标准

能用一段话回答："**当前策略在 BTC/USDT 上 24h paper 的真实表现是 X，主要损失来源是 Y。**"
回答不出来 → Step 1 没做完，禁止进入 Step 2。

### 3.5 实测 baseline — preliminary (0.5h, 157 fills, markout 90/157)

> 不是 Step 1 验收数据（远未达 6h 最低要求），但已经能定方向。

```
SESSION   bybit_spot target / binance_spot reference  1776s (0.5h)
FILLS     157 (buy 70 / sell 87 / ghost 40)   markout_done 90/157
MARKOUT bps          mean    p10     p50     p90
  1s     n=89      −0.237  −2.146  −0.338  +1.629
  5s     n=72      −0.631  −1.756  −0.600  +1.365
 30s     n=68      −0.555  −3.005  −1.202  +2.520
 60s     n=72      +0.567  −4.023  +1.969  +4.826
TOXIC (markout < −0.5×tick)
  1s 53.9%  5s 76.4%  30s 66.2%  60s 34.7%
SPREAD CAPTURE
  quoted   = +0.032 bps   realized@1s = −0.474 bps   ratio = −14.86  ← WARN
LADDER markout_1s
  L0 −0.237 / 54.8% toxic    L1 −0.214 / 51.7%    L2 −0.261 / 55.2%
INVENTORY     q_norm ≈ 0, no cap touches.  realized PnL −0.0013 USDT
REJECTS       240 / 17 733 = 1.4%  全部 post_only_cross
QUOTE AGE     mean 233ms  p50 67ms  p99 1158ms
```

**4 个不容忽视的读数（按优先级）：**

1. **Toxic 不是策略问题，是时间问题** — L0/L1/L2 markout 几乎相同（−0.21 ~ −0.26 bps），toxic 比例同质（52–55%）。如果是 queue-position 风险，内档应该明显更毒；现在三档一致说明**主要损失是"在错误的时间挂着错误的价"**，而非"挂在错误的位置"。这把 Step 2 的候选从 Layer 6（ladder 内档收紧）排除。
2. **quote_age p99 = 1.16s** — 接近 12× p50（67ms），尾部分布极重。和 1.4% `post_only_cross` reject 一并看：QuoteDiffer 在 fair price 快速移动时撤改不及时 → 老报价被吃 / 新报价被市场穿。**这是 Inf-2 全链路打点 + Px-4 重报价阈值的活儿，比纯 Layer 9 监控更接近根因**。
3. **capture ratio −14.86 数值离谱，先查公式** — `spread_at_fill_bps` 均值 0.032 bps ≈ 34 ticks（按 BTC ≈ $108k 估算），与 realized −0.474 bps 量纲差 15 倍。要么 `spread_at_fill_bps` 写入时已经按某种 normalize 处理过，要么 markout 单位和 spread 不在同一尺度。**进入 Step 2 之前必须先 audit `_section_spread_capture` 与 `fills.spread_at_fill_bps` 的写入路径**，否则任何"加宽 spread"的决策都在错的尺子上做。
4. **库存维度今天不是矛盾** — q_norm 全程贴 0、没碰 cap、realized PnL 接近 0。本期暂不需要 Layer 11 风控硬约束（保留为下一轮再评）。

### 3.6 Step 1 收尾前必须做的事（按顺序）

| # | 任务 | 工作量 | 状态 |
|---|---|---|---|
| 1 | **§3.7 数据层审计修复**（mid_ref bug + capture-ratio 公式 + markout_from_emit + order_activity 节） | 0.5–1 天 | ✅ done |
| 2 | 把 markout backfill 跑完（旧 0.5h session 当前 90/157 = 57%），确认尾段 fills 的 markout 不会反转结论 | 0.5h（后台 job） | ⬜ pending |
| 3 | 重跑 paper ≥ 6h（建议挑欧/美盘交替 12h+ 段），让 toxic_ratio CI 收紧 | 6–24h 挂机 | ⬜ pending（修完 Bug 1 后必须重跑） |
| 4 | 上面三件齐了再用一段话回答 §3.4 的问题 | — | ⬜ Step 2 启动 gate |

### 3.7 数据层审计（2026-05-25）

> 看完 `cmd/replay_review.py` + `data/archive/*` + `pkg/storage/schema_sql.py` 后的结论：**当前 baseline 的"warn"信号大方向能信，数值层面有两处被污染，重跑前必须修。**

#### A. 已确认 / 已修：`mid_ref_at_fill` 数据污染（Bug 1）

`paper_executor_service.py` 在 archive 写 `orders.mid_ref_at_submit` 与 `fills.mid_ref_at_fill` 时，把 target 的 mid 直接当作 ref mid 写入：

- 现象：DB 校验 `SELECT min/max(mid_target - mid_ref) FROM fills` 全为 `0.0`。
- 影响：
  - `fills.mid_ref_at_fill` 这一列在当前归档里**完全失效**。
  - `markout_from_emit_5s/60s` 也跟着失效（backfiller 用它作 baseline，等价于 "ref_mid_at_τ − target_mid_at_fill"，跨所偏离时尺度错)。
  - 现行 `markout_1s/5s/30s/60s` 不受影响（backfill 用 `(mid_τ_ref − fill_price)`，与 mid_ref_at_fill 列无关），所以 §3.5 的核心 markout 数字仍可信。
  - `quote_snapshots`（Parquet）走的是 `mm_service._on_quote`，那里正确取了 ref 引擎的 mid，**Parquet 不受影响**。
- 修复：本轮已在 `paper_executor_service.py:288, 369` 改为 `self._mm.get_fair_price(reference=True).mid`，下一次 paper 重跑后 SQLite 这两列即正确。

#### B. 待修：`capture ratio` 分子分母不同尺度（Bug 2，§3.5 read #3 根因）

`replay_review._section_spread_capture` 用：

- 分子 `realized = 2 × markout / price × 1e4` — markout 是相对 **ref-venue** τ 秒 mid 的 1s 漂移（bps）。
- 分母 `quoted = fills.spread_at_fill_bps` — 写入路径在 `paper_executor_service.py:351`，取的是 **target 本地 BBO** 的瞬时全 spread（`snap.spread() / snap.mid_price`），**不是我们自己两边报价之间的内宽**。

→ "BBO 微观结构" vs "1s ref 漂移" 不构成 capture ratio，比值无几何意义，−14.86 不能当严重程度读。**正确做法二选一：**

| 方案 | 实现 | 取舍 |
|---|---|---|
| **a. 改分母为"我们的 inner_spread_bps"** | replay_review JOIN `quote_snapshots`（按 `quote_emit_ts_ns` 取离 fill 最近的快照），用其 `inner_spread_bps` 作分母 | 最贴近 textbook "maker quoted spread"；多一次 Parquet scan |
| **b. 改分母为"BBO at fill"，且改分子为 effective spread** | 分子改成 `2 × (P − M_target_at_fill) × side_sign / P × 1e4`（textbook effective spread），分母保留 BBO | 不需要 JOIN；衡量的是"我们成交位置 vs 市场内宽"，不是真正的"我们 quoted vs 实际拿到"|

推荐 **a**——和我们 Layer 9 在线 Tier 1 监控要看的是同一个概念（自家 quote 的 spread 被吃了多少）。

#### C. replay_review 覆盖度审计（按"它该回答什么"清单）

| 维度 | 当前 | 缺口 |
|---|---|---|
| markout（from fill） | ✅ 1/5/30/60s + p10/p50/p90 + toxic ratio | — |
| markout from emit | ❌ DB 有 `markout_from_emit_5s/60s` 列、有写入，**replay_review 完全没读**。这是诊断 "quote 在 emit 时是否已经 stale" 的关键 | 加一节 `MARKOUT FROM EMIT`，对比 from-fill 数值可区分"挂得就错" vs "挂时对、被吃前漂走" |
| ladder 分桶 | ✅ markout_1s / toxic_1s × level | 缺 quote_age × level、emit→fill 漂移 × level |
| quote staleness | ⚠️ 只有总体 mean/p50/p99（233/67/1158 ms） | 缺：① 按 side 拆（buy/sell 是否一边重尾），② 按是否被撤改前命中拆 |
| 撤改活动 | ❌ 完全无 | 缺：`orders.count / fills.count / order_events(CANCEL).count` 的比率（cancel-to-fill）、平均挂单存活时长 |
| reject 归因 | ✅ post_only_cross 240/17733 = 1.4% | 缺：按时间分桶看是否聚簇（一次大波动集中拒，还是均匀慢漏） |
| 时间序列视角 | ❌ 全是聚合 | 缺：滚动 30-fill toxic、滚动 5min cumulative PnL — 用来回答"toxic 是匀速还是几个段集中"（直接决定要不要 Layer 9 触发式避险）|
| 跨所偏离 | ❌（且 mid_ref 之前还是 corrupt） | 修完 Bug 1 后加 `mid_target − mid_ref` 的分布 + 在 reject / toxic 高峰段是否更大 |
| PnL | ✅ 终值 | 缺：max drawdown、cumulative-PnL 曲线 ASCII sparkline |
| Fee 模型 | ❌ | `maker_fee_bps` 当前为 0；replay_review 应明确打印 "fee model: maker=0bps"，提醒读数是 zero-fee 视角 |

#### D. 表数据存储审计：**结构充分，写入有 1 处污染**

| 表 / 文件 | 完整度 | 备注 |
|---|---|---|
| `sessions` | ✅ | 含 git_sha / config_snapshot，可重现 |
| `orders` | ⚠️ | mid_ref_at_submit 同 Bug 1（已修） |
| `order_events` | ✅ | 含 reject_code / reason / status_after，支持 reject 归因 |
| `fills` | ⚠️ | mid_ref_at_fill 同 Bug 1（已修），其余列齐全 |
| `quote_snapshots` (Parquet) | ✅ | 含 `inner_spread_bps` / `inner_bid_size` / `inner_ask_size` — Bug 2 修复方案 a 的数据源 |
| `mid_tape` (Parquet) | ✅ | role={target,reference}，backfill 路径已验证 |
| `trade_tape` (Parquet) | ✅ | 双向（target + reference）的 aggTrade，将来做 Layer 9 aggTrade 预撤直接可用 |
| latency 直方图 | ⚠️ | `archive.observe_latency` 接口在，**Inf-2 全链路打点没接进 hot path**——这是 §4.1 主路径的硬阻塞，独立列入 §3.9 |

#### E. 本轮立即要做的 4 件小事（在重跑 ≥6h paper 之前）

1. ✅ 修 Bug 1（paper_executor_service.py:288, 369）
2. ✅ 改 `replay_review._section_spread_capture` 为方案 a — ASOF JOIN `quote_snapshots.inner_spread_bps`（按 fills.recv_ts_ns wall-clock）。fallback 仍保留 BBO 作 informational 行。
3. ✅ replay_review 加 `MARKOUT FROM EMIT`，直接读现有 `markout_from_emit_5s/60s` 列，输出 Δ = emit − fill 与诊断说明（注：依赖 Bug 1 修复后的 mid_ref，老 session 的 Δ 不可信）。
4. ✅ replay_review 加 `ORDER ACTIVITY` — orders / fills / cancels / rejects、fill rate、cancel-to-fill ratio、rest time p50/p90/p99（JOIN orders × order_events 的 `CANCEL_ACK | FILL`）。

> 这 4 件做完，baseline 工具就齐了。**但是 §3.6 #3 的长样本仍不可跳**——下面 §3.8 是新工具跑出的第一份信号，但样本太小。

### 3.8 工具升级后的二次读数（n=12, 新 session 还在跑）

> 仅作工具验证 + 新信号捕捉，**不能当作 baseline**——12 个 fill、全 sell、session 还 running。

```
SPREAD CAPTURE  (new denominator)
  our inner spread:        +0.752 bps   ← 来自 quote_snapshots.inner_spread_bps
  BBO at fill:             +0.013 bps   ← 之前被误当分母（informational only）
  2×markout @1s:           +2.034 bps
  realized = quoted + 2×m: +2.786 bps
  capture ratio @1s:       +3.704        (此刻样本只有 12，且全 sell；长样本下大概率回落)

MARKOUT FROM EMIT
  5s : from_fill +0.372  from_emit −0.007  Δ −0.379
  60s: from_fill −1.292  from_emit −1.671  Δ −0.379
  Δ < 0 → 60s 内 ref mid 整体相对 emit 时漂走更多；样本小，倾向"adverse selection 集中在 emit 后窗口内"

ORDER ACTIVITY  ← 之前完全没看到的维度
  orders placed:    4 550
  fills:            12  (0.26% per order)
  cancels acked:    4 481  (98.5% per order)
  rejects:          60   (1.32%)
  cancel-to-fill:   373.4x  ← QuoteDiffer 严重 churn
  rest time:        mean 648ms  p50 415ms  p90 1881ms  p99 2390ms
```

**最大的新信号：`cancel-to-fill 373x`。** 即使长样本会把这个数压下来，量级也指向"QuoteDiffer 在以 100% 的节奏撤改报价"。这是 §3.5 没看到的维度，比 quote_age p99 = 1.16s 更直接：

- QuoteDiffer 本身（`biz/usecase/quote_differ.py`）已经做了"exact-match-after-quantization keep"——意味着撤改的根因是 **GltSpreadEngine 在 quantize 后仍频繁生成不同价格**。
- CLAUDE.md `Ex-4 队列价值保护`原话："重报价阈值：公平价移动 > 1 tick **或** 库存超阈值，否则不动"——这条规则在当前实现里是缺失的。
- → **§4.1 主路径里 `[Px-4] re-quote 阈值显式化` 的优先级从"次要"提到"必须先做"**。Inf-2 打点用来定位 quote_age 的 1.16s 落点；re-quote 抑制直接降低 cancel-to-fill。两件事**可以并行**：Inf-2 是诊断、re-quote 抑制是治疗，互不冲突。

更新 §4.1：原"Inf-2 → Px-4 → Layer 9"按顺序的路径，改为"**Inf-2 ∥ Px-4，都搞定再上 Layer 9**"。

### 3.9 Inf-2 全链路打点（§4.1 主路径的硬前置）

`archive.observe_latency` 接口已经在 `ArchiveRepo`，但 hot path 没真的调用。**进入 Step 2 第一件事就是把它接起来**，否则 quote_age p99 = 1.16s 的归因是黑盒——分不清是：

- (a) Px 路径：fair price 更新慢（reference book 增量更新 → FairValueEngine.state 刷新慢）
- (b) 策略路径：fair price 已更新，但 GLT engine / QuoteDiffer 没及时触发 emit
- (c) Ex 路径：emit 已下发，但撤改 ack 慢

最小打点集（按 CLAUDE.md Inf-2）：

| 阶段 | 时间戳 | 落点建议 |
|---|---|---|
| `feed_recv_ts` | WS frame 到达本进程 | OrderBookTracker（target + reference） |
| `book_update_ts` | book 应用 diff 后 | FairValueEngine.on_update 末端 |
| `fair_price_ts` | state 刷新完成 | FairValueEngine.state 写入处 |
| `decision_ts` | GLT engine 算出新 quote | GltSpreadEngine.recompute |
| `emit_ts` | QuoteDiffer 决定下/撤改 | service/mm_service 或 paper_executor |
| `send_ts` | Exchange adapter 调用前 | data/exchange/* |
| `target_ack_ts` | 收到 target 的 ack | OMS / paper FillSimulator |
| `fill_ts` | 成交事件 | OMS / paper FillSimulator |

> 不需要全部一次接全。**先接 `fair_price_ts → emit_ts → send_ts → target_ack_ts` 四段**，足够定位 1.16s 主要落在哪一段。

---

## 4. Step 2 — 依 Step 1 baseline 结果分流

按 Step 1 暴露的问题选**一条**路径做（不要并行铺开）：

| Step 1 暴露的问题 | Step 2 要做 | 对应 Layer |
|---|---|---|
| `toxic_ratio > 35%` 或 `markout_1s 均值 < −0.5 tick` | 在线 Tier 1 监控 + 自动避险（滚动 30 fills markout → 拉宽 spread / 暂停报价） | 9 |
| 内档（`ladder_level=0`）toxic 显著高于外档 | aggTrade 预撤 + ladder 内档量化收紧 | 6 + 9 |
| `q_norm` 长期单边偏离 0 或反复贴 cap | 风控硬约束（单边敞口 cap、单笔最大下单量、kill switch） | 11 |
| post_only_cross reject 频发 | 加 [Inf-2] 全链路打点定位 stale 段 | Inf-2 |
| GLT σ 校准过抖（reqote 频率异常高） | re-quote 阈值显式化（fair price 移动 > N tick 才动），加 quote_diff 抑制 | 5 |
| 上面都正常但 PnL 仍为负 | 检查 fee 模型（`maker_fee_bps` 当前为 0，实盘有 rebate）+ 拉长样本 | — |

**不做并行**：任何 Step 2 改动后必须用 `replay_review.py` 重跑一次 baseline，确认指标方向正确再继续。

### 4.1 预判：当前 baseline + §3.8 二次读数指向的路径

> 仍以 §3.6 跑完 6h 长样本为准。下面是基于 preliminary 数据的**最大似然分流**，不是已决定的路。

主路（**[Inf-2] 全链路打点** ∥ **[Px-4] re-quote 阈值显式化**）并行 → 再上 **[Layer 9] 在线 Tier 1**。理由：

- §3.5 给的 toxic 信号：L0/L1/L2 toxic 同质 → 损失不是空间维度（ladder 位置），而是**时间维度**（quote staleness）。
- §3.8 给的 churn 信号：cancel-to-fill = 373x、fill rate 0.26% → QuoteDiffer 在以 ~100% 节奏撤改。即使有 Inf-2 打点，**不抑制 re-quote 频率，queue position 永远攒不起来**，Layer 9 的滚动 markout 监控也会被噪声淹没。
- 两件事互不冲突且互相佐证：Inf-2 定位 1.16s 落在哪一段（Px 还是 Ex），Px-4 直接降低 cancel-to-fill；做完一起回灌再决定 Layer 9 形态。
- 1.4% `post_only_cross` 是 stale 路径的副作用，会跟着 [Px-4] 一起消化。

**先不做的**：Layer 6 内档收紧（toxic 不集中在 L0）、Layer 11 风控硬约束（q_norm 健康）、fee 模型校准（PnL 接近 0，量级不是 fee 决定的）。

### 4.2 进入 Step 2 的硬门槛（不能跳过）

1. §3.6 任务全部完成（公式 audit + backfill 100% + ≥6h 样本）
2. §3.5 的 4 条读数在长样本下仍然成立（toxic 同质 + quote_age 重尾 + capture 异常 + q_norm 健康）
3. Step 2 单条路径做完必须用 `replay_review.py` 重跑，对比 §3.5 的数字方向

---

## 5. 明确不做的事（避免在错误的地方花时间）

| 不做 | 原因 |
|---|---|
| Layer 7 LOB 结构感知 | 还没量化"我的报价被吃在什么队列位置"。`FillSimulator.queue_ahead` 已经有，先看数据。 |
| Layer 8 库存对冲 | CLAUDE.md 阶段一明确不做（涉及外所 + delta-neutral） |
| 真盘 `BinanceSpot` exchange adapter | paper 数据还没看，上真盘是送钱 |
| 延迟微优化（numpy 重写、orjson 替换等） | 还没建 Inf-2 全链路打点；CLAUDE.md 原话："禁止任何代码层微优化" |
| Binance AggTrade tracker 作为 reference taker 流 | reference 只用 mid 做归因 → 不补；Binance trade tracker 已存在但 cross-venue 模式下不订阅 |

---

## 6. 待回答 / 待确认

1. **跑多久？** 建议 ≥ 6h，理想 24h。低于 6h 样本量不够算 toxic ratio。**当前 0.5h，必须重跑。**
2. **跑哪个 symbol？** BTC_USDT 优先（tick 大、深度厚、价格稳定，便于校准）。Bybit 上 BTCUSDT 同样是 tick=0.01，与现有 `spread_engine.price_tick` 一致。
3. **paper 期是否要在 Bybit 上做 `qty_step` 校准？** Bybit BTC/USDT 现货最小下单量是 0.000048（约 ~$3），当前 `paper.qty_step=0.00001` 偏小但不影响 paper（实盘前必须改）。
4. **是否同时观测两边 aggTrade？** Step 1 不需要；Step 2 如果选"aggTrade 预撤"路径，再考虑订阅 Binance aggTrade 作为 reference 信号源（数据回传也是 cross-venue Px 路径的核心，不在 Step 1 范围）。
5. **`spread_at_fill_bps` 写入公式是否正确？** ✅ 已查：写入的是 target 本地 BBO（不是我们 quote 的 inner spread），公式本身没错但**不能当作 capture ratio 的分母**——见 §3.7 B 与 Bug 2 修复方案。
6. **markout backfill 是否会落后？** 0.5h session 跑完只有 90/157 fills 完成 markout，可能是 backfiller 节奏 / 60s 窗口边界问题。在长样本前确认 backfill job 能持续追平。
7. **quote_age p99 = 1.16s 的归因？** 现在无法区分是 QuoteDiffer 抑制阈值过宽、撤单 ack 慢，还是策略层 fair price 刷新慢。**根因前置在 §3.9 Inf-2 打点**，不是当前能凭数据回答的问题。
8. **`mid_ref_at_fill` 跨所偏离信号何时可读？** 答：Bug 1 已修，下一次 paper 重跑后 SQLite 才会有正确的 ref mid；老 session 数据这一列永久污染（但可以从 Parquet `quote_snapshots.mid_ref` 反算回填，非必需）。

---

## 7. 时间盒

- Step 0（Bybit trade tracker）：**0.5–1 天** ✅ done
- Step 1（cross-venue 切换 + 24h paper + replay_review）：**3 个工作日内完成**
  - cross-venue + replay_review 脚本：✅ done
  - 0.5h preliminary run：✅ done（§3.5）
  - **剩余：§3.6 三件事（公式 audit + backfill 收尾 + ≥6h 长样本）**
- Step 1 → Step 2 决策点：1 个评审日（看报告决定走哪条路）— 预判路径在 §4.1
- Step 2 单条路径：1-2 周

超出时间盒 → 停下来 review，不要默默扩展范围。
