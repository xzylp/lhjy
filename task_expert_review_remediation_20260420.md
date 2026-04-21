# 2026-04-20 专家评审改造任务清单

> 来源：[expert_review_packet_20260420.md](docs/expert_review_packet_20260420.md)
> 定位：不是修 bug，而是从"工程能跑"推进到"有机会稳定盈利"的关键能力补齐。
> 五大维度对齐原始设计目标，每项任务标注代码落点和验收条件。

---

## 维度一：Agent 市场理解 — 从"受约束选择"到"主动推理"

### 现状诊断

当前 `market_hypothesis` 虽然作为字段存在于 `RuntimeComposeIntent` 和 compose API 中，但 Agent 填写的 hypothesis 本质上是对已有候选的事后解释，而非事前推理：

1. **因子/战法选择被预设集合硬约束**：`factor_registry.py` 的 `suggest_factors()` 只在已注册因子中做关键词匹配（L1385），Agent 无法表达"我认为当前应该看什么"
2. **playbook 选择同理**：`playbook_registry.py:suggest_playbooks()` 用 `_tokenize_text(market_hypothesis)` 做字符串匹配（L432），贴合度取决于 playbook 描述词是否恰好命中
3. **候选排序被 `selection_score` 主导**：runtime 返回的候选本质是 screener 排序结果，Agent 的 hypothesis 只影响因子权重微调，不影响候选来源

### 改造任务

- [x] **A1.1** 新增市场状态感知模块：`src/ashare_system/market/regime_detector.py`
  - 输入：当日板块涨跌幅、板块成交额、涨停/跌停分布、北向资金流向、主力净流入前5行业
  - 输出：`MarketRegime` 结构体，包含：
    - `regime_label`: `strong_rotation | sector_breakout | index_rebound | weak_defense | panic_sell`
    - `hot_sector_chain`: 按扩散逻辑排序的热点链（龙头→跟风→补涨）
    - `sector_strength_rank`: 板块强度排名（前10）
    - `money_flow_signal`: 资金流向信号
    - `confidence`: 置信度
  - 验收：给定 20260420 的市场数据，输出的 `hot_sector_chain` 应与当日实际涨停板块构成 ≥60% 重合

- [x] **A1.2** 将 `MarketRegime` 注入 compose 主链
  - 文件：`src/ashare_system/apps/runtime_api.py` — compose 和 compose-from-brief 端点
  - 文件：`src/ashare_system/strategy/strategy_composer.py` L218 附近
  - 改动：在构建 compose context 时，自动调用 `regime_detector` 获取当前市场状态，作为 `market_context` 传入
  - Agent 的 `market_hypothesis` 与 `MarketRegime` 做交叉验证：如果 hypothesis 与实际 regime 矛盾，生成 `regime_mismatch_warning`

- [x] **A1.3** 因子推荐从"关键词匹配"升级为"regime 驱动"
  - 文件：`src/ashare_system/strategy/factor_registry.py` — `suggest_factors()` 方法
  - 当前：只用 `_tokenize_text(market_hypothesis)` 做字符串匹配
  - 改为：优先按 `MarketRegime.regime_label` 查预设的 regime→factor 映射表，hypothesis 关键词作为辅助权重
  - 新增映射表：
    ```python
    REGIME_FACTOR_MAP = {
        "strong_rotation": ["turnover_acceleration", "sector_momentum", "limit_up_gene"],
        "sector_breakout": ["sector_relative_strength", "volume_breakout", "institutional_flow"],
        "index_rebound": ["oversold_bounce", "beta_momentum", "value_reversion"],
        "weak_defense": ["dividend_yield", "low_volatility", "quality_score"],
        "panic_sell": ["crash_resilience", "institutional_holding", "balance_sheet_strength"],
    }
    ```

- [x] **A1.4** playbook 推荐同步升级
  - 文件：`src/ashare_system/strategy/playbook_registry.py` — `suggest_playbooks()` 方法
  - 改为：按 `MarketRegime` 的 regime_label 优先匹配 playbook 的 `applicable_regimes` 标签
  - 每个 playbook 增加 `applicable_regimes: list[str]` 字段

- [x] **A1.5** 候选来源多元化：不再只依赖 screener 预排序
  - 文件：`src/ashare_system/scheduler.py` — 盘前 `买入清单` 任务
  - 新增候选注入源：
    1. 板块龙头提取：从 `hot_sector_chain` 中提取每个板块的领涨股（涨幅最大+成交额最大）
    2. 概念主线跟踪：跟踪连续2日涨停板集中的概念标签，提取同概念未涨停股
    3. 资金异动：主力净流入 top20 且不在已有候选池中的标的
  - 这些候选标记 `source=regime_driven`，与 screener 候选 `source=market_universe_scan` 并存

---

## 维度二：盘中速度与贴合主线 — 从"延迟统计"到"秒级决策"

### 现状诊断

1. `run_fast_opportunity_scan()` 的触发条件（`change_pct >= 0.06 && spread_pct <= 0.012`）对 A 股超短来说太粗：
   - 涨到 6% 再发现已经错过最佳买点
   - 没有分析涨速（前5分钟涨幅斜率）、封板强度、板块联动同步性
2. 盘中止损链虽然有 `fast_track_exit`，但触发阈值在 `scheduler.py` 的 `_check_sell_signal()` 中是固定百分比，没有基于 ATR 或波动率的自适应止损
3. 做 T 链只有 `DayTradingEngine`，但它的信号未接入快退审批链

### 改造任务

- [x] **B1.1** 涨停基因早期识别：在涨幅达到 3% 时就开始跟踪
  - 文件：`src/ashare_system/scheduler.py` — `run_fast_opportunity_scan()` L1905
  - 当前：`change_pct >= max(limit_pct - 0.025, 0.06)` → 对主板意味着要 7.5% 才触发
  - 改为分级识别：
    - `3%-5%`：标记为 `early_momentum`，记录涨速斜率和买盘强度，不注入候选
    - `5%-7%`：如果涨速斜率 > 2%/5min 且 5m 量比 ≥ 1.5，标记为 `acceleration`，注入候选
    - `7%+`：维持现有 `pre_limit_up` 逻辑
  - 新增涨速斜率计算：`slope = (bar[-1].close - bar[-3].close) / bar[-3].close / 3`（最近3根5min K线斜率）

- [x] **B1.2** 板块联动同步性检测
  - 文件：`src/ashare_system/scheduler.py` — `run_fast_opportunity_scan()` 中 `sector_linked` 判断
  - 当前：`primary_sector in hot_sectors or sector_link_count >= 2` — 太宽松
  - 改为：
    - 板块内 ≥3 只股票同步涨幅 > 3% → `sector_sync_strong`
    - 板块内龙头已涨停 + ≥2 只跟风涨 > 5% → `sector_cascade`
    - 无同步信号 → `sector_isolated`（降低优先级或排除）

- [x] **B1.3** 自适应止损替换固定百分比
  - 文件：`src/ashare_system/strategy/sell_decision.py` — `_check_sell_signal()` 或等效位置
  - 当前：固定 `-3%` 触发止损
  - 改为：`stop_loss = max(-2 * ATR_pct, -0.05)`，其中 ATR_pct 用 `_estimate_position_atr()` 已有函数
  - 增加"浮盈回撤止损"：`peak_drawdown > 0.5 * max_unrealized_gain` 时触发

- [x] **B1.4** 做T信号接入快退审批链
  - 文件：`src/ashare_system/scheduler.py` — position_watch 处理做T信号的分支
  - 文件：`src/ashare_system/strategy/day_trading.py` — `DayTradingEngine` 输出
  - 改动：做T卖出信号走 `_fast_track_exit_review()` 审批，不再只做preview
  - 做T买回信号走候选注入（注入为 `source=day_trading_rebuy`），走快速讨论

- [x] **B1.5** 盘中决策延迟监控
  - 文件：新建 `src/ashare_system/monitor/latency_tracker.py`
  - 记录每个盘中关键链路的延迟：
    - `opportunity_scan → candidate_inject`：目标 < 2s
    - `sell_signal → fast_track_review → order_submit`：目标 < 5s
    - `position_watch_cycle`：目标 < 3s/轮
  - 超过阈值时通过 `notify/templates.py` 发出告警

---

## 维度三：受控准入口径统一 — 消除运营/准入/执行三方矛盾

### 现状诊断

`_build_controlled_apply_readiness()` 中 `max_equity_position_limit` 硬编码默认值 `0.2`（L3359），环境变量 `ASHARE_APPLY_READY_MAX_EQUITY_POSITION_LIMIT` 未设置时直接用 `0.2`。但运营侧 `runtime_config.equity_position_limit` 可能设为 `0.4`，导致仓位 30% 时受控 apply 就被拦截。

三个口径：
- **运营口径**（`runtime_config.equity_position_limit`）：`0.4`
- **受控准入口径**（`ASHARE_APPLY_READY_MAX_EQUITY_POSITION_LIMIT`）：默认 `0.2`
- **执行口径**（实际报单时的仓位检查）：读 `parameter_service` 或 `runtime_config`

### 改造任务

- [x] **C1.1** 受控准入口径自动对齐运营口径
  - 文件：`src/ashare_system/apps/system_api.py` L3356-3359
  - 改动：当 `max_equity_position_limit` 参数为 None 且环境变量未设置时，不再用硬编码 `0.2`，而是读 `runtime_config.equity_position_limit` 作为上限
  - fallback 链：`参数传入 > 环境变量 > runtime_config > parameter_service > 0.3（保守默认）`

- [x] **C1.2** 增加口径一致性检查端点
  - 文件：`src/ashare_system/apps/system_api.py`
  - 新增 `GET /system/deployment/parameter-consistency`
  - 返回：
    ```json
    {
      "equity_position_limit": {
        "runtime_config": 0.4,
        "controlled_apply": 0.2,
        "parameter_service": 0.4,
        "consistent": false,
        "recommendation": "将受控准入上限调整为 0.35 (运营口径 * 0.85 安全系数)"
      }
    }
    ```
  - 不一致时在 healthcheck 中标记 `parameter_drift_warning`

- [x] **C1.3** 历史挂单自动治理
  - 文件：`src/ashare_system/scheduler.py` — 盘后任务
  - 新增盘后任务 `ScheduledTask(name="挂单治理", cron="0 16 * * 1-5", handler="execution.stale_order:cleanup")`
  - 逻辑：
    1. 查询 `pending_execution_intents`，对超过 24h 未消费的 intent 标记 `stale`
    2. 查询 QMT 订单侧，对 `order_status` 为终态（已成/已撤/已废）的订单，自动关闭对应 receipt
    3. 对仍为 pending 但 QMT 侧已不存在的订单，标记 `orphaned` 并发出告警
  - 验收：执行后 readiness 中 `pending=0, stale=0`

- [x] **C1.4** readiness 分级：从 `blocked` 改为可降级执行
  - 文件：`src/ashare_system/apps/system_api.py` — `_build_controlled_apply_readiness()`
  - 当前：任何一项 check 失败就 `blocked`
  - 改为三级：
    - `ready`：全部通过
    - `degraded_allow`：仅仓位口径超限但在运营口径内，允许小额执行
    - `blocked`：核心安全项（紧急停牌、桥断连、QMT 离线）不通过

---

## 维度四：执行桥从"调试态"到"生产态"

### 现状诊断

今天拿到了单笔闭环证据，但生产态需要证明：并发稳定性、异常单收口、自动恢复。

### 改造任务

- [x] **D1.1** 并发执行压力测试框架
  - 文件：新建 `tests/test_execution_bridge_concurrent.py`
  - 模拟场景：
    1. 同时派发 5 笔 intent，验证 Windows worker 逐一 claim 且无重复
    2. 派发后 Linux 侧主动断网 10s，恢复后 receipt 应能补回
    3. QMT 返回拒单（`order_status=57`），receipt 应正确标记 `rejected`
  - 依赖：mock 模式下的 execution_gateway + Windows worker stub

- [x] **D1.2** receipt 状态机完善
  - 文件：`src/ashare_system/execution_gateway.py`
  - 当前 receipt 只有 `submitted` 状态
  - 增加状态流转：`pending → claimed → submitted → filled | rejected | cancelled | expired`
  - 每次状态变更写入 `execution_gateway_state.json` 的 `receipt_audit_trail`
  - 超过 5 分钟无状态更新的 claimed intent → 自动重试一次

- [x] **D1.3** bridge health 自动恢复
  - 文件：`src/ashare_system/scheduler.py` — 盘中任务
  - 新增 `ScheduledTask(name="桥健康守护", interval_seconds=30, handler="execution.bridge_guardian:check")`
  - 逻辑：
    - 如果 `latest_execution_bridge_health.reported_at` 超过 120s 未更新 → 告警
    - 如果超过 300s → 尝试通过 go_platform `/health` 探活
    - 如果探活失败 → 标记 `bridge_stale`，阻止新 intent 派发，发出紧急通知

- [x] **D1.4** 对账闭环
  - 文件：新建 `src/ashare_system/execution/reconciliation.py`
  - 盘后自动对账：
    1. 从 QMT 拉取当日全部订单（`/qmt/account/orders`）
    2. 与 `execution_gateway_state.json` 中的 receipt 逐笔比对
    3. 生成对账报告：`matched / unmatched_linux / unmatched_qmt / status_mismatch`
    4. 对 `status_mismatch` 的订单自动修正 receipt 状态
  - 挂到盘后任务：`ScheduledTask(name="执行对账", cron="10 16 * * 1-5", handler="execution.reconciliation:run")`

---

## 维度五：治理闭环 — 从"留痕"到"反向改变行为"

### 现状诊断

当前 Elo 分数、因子有效性、playbook 评分等治理数据虽然都在记录，但它们对 Agent 行为的反向影响路径缺失或太弱：
- `AgentScoreSettlementService` 算出的 `governance_score_delta` 写入了 `score_state`，但 compose 时没有读取这些分数来调整因子权重
- 因子有效性 `FactorMonitor` 的 `validity_flags` 虽然在 compose 中有检查，但无效因子只是 warning，不会被自动降权或移除

### 改造任务

- [x] **E1.1** Agent 信誉分直接影响 compose 权重
  - 文件：`src/ashare_system/strategy/strategy_composer.py` — compose 主链
  - 文件：`src/ashare_system/learning/score_state.py` — `export_weights()`
  - 改动：compose 时读取 `agent_score_service.export_weights(trade_date)`，对 `governance_score < 0.3` 的 Agent 的因子建议降权 50%
  - 对 `governance_score > 0.7` 的 Agent 的因子建议增权 20%
  - 权重调整记录到 compose trace 中，可追溯

- [x] **E1.2** 无效因子自动降权而非仅 warning
  - 文件：`src/ashare_system/strategy/compose_factor_policy.py`
  - 文件：`src/ashare_system/strategy/factor_monitor.py`
  - 当前：`factor_validity_flags` 为 False 时只输出 warning
  - 改为：
    - `validity=False` 且 `consecutive_invalid_days >= 5` → 自动将该因子权重设为 0，标记 `auto_disabled`
    - `validity=False` 且 `consecutive_invalid_days < 5` → 权重减半，标记 `degraded`
    - 恢复条件：连续 3 天 `validity=True` 后自动恢复原权重

- [x] **E1.3** 策略回溯 PnL 驱动 playbook 权重
  - 文件：`src/ashare_system/learning/attribution.py` — `TradeAttributionService`
  - 文件：`src/ashare_system/strategy/playbook_registry.py`
  - 改动：盘后结算时，根据 `attribution` 中每个 playbook 的实际 PnL 贡献，自动调整 playbook 的 `priority_score`
  - 规则：
    - 近 10 笔该 playbook 平均收益 > 0 → `priority_score += 0.1`
    - 近 10 笔该 playbook 平均收益 < -2% → `priority_score -= 0.2`
    - `priority_score < 0.1` → playbook 进入 `probation` 状态，不再被自动推荐
  - 验收：连续亏损的 playbook 应在 5 个交易日内被自动降权

- [x] **E1.4** 治理仪表盘端点
  - 文件：`src/ashare_system/apps/system_api.py`
  - 新增 `GET /system/governance/dashboard`
  - 返回：
    ```json
    {
      "agent_scores": {"ashare-strategy": 0.65, "ashare-risk": 0.72, "ashare-macro": 0.55},
      "factor_health": {"momentum_5d": "active", "value_reversion": "degraded", "low_volatility": "auto_disabled"},
      "playbook_health": {"龙头首板": {"priority": 0.8, "recent_pnl": 0.032}, "趋势跟随": {"priority": 0.3, "recent_pnl": -0.018, "status": "probation"}},
      "recent_governance_actions": [
        {"action": "factor_auto_disabled", "target": "low_volatility", "reason": "连续5日无效", "at": "2026-04-19"},

---

## 建议实施顺序（按真实收益与上线价值重排）

> 重排原则：先消除“明明能交易却被假阻断”的问题，再提高盘中决策与执行质量，最后做 Agent 自主理解和治理强化。  
> 目标不是把任务做完，而是优先让系统更接近“可稳定实盘运行”。

### P0：先把真实交易主链稳定下来

#### P0-1 统一准入口径，消除假阻断

- `C1.1` 受控准入口径自动对齐运营口径
- `C1.2` 增加口径一致性检查端点
- `C1.4` readiness 分级：`ready / degraded_allow / blocked`

原因：

- 当前最直接拦住正式 apply 的不是桥，而是参数漂移和 readiness 判定过硬。
- 今天已经看到 `equity_position_limit=0.4` 与 `controlled-apply allowed<=0.3` 的真实冲突，这属于“假阻断”。

验收：

- `/system/deployment/controlled-apply-readiness` 不再因为运营口径与受控准入口径漂移而误拦。
- `/system/deployment/parameter-consistency` 能明确指出 drift 与建议修正值。

#### P0-2 收掉历史挂单与对账脏状态

- `C1.3` 历史挂单自动治理
- `D1.4` 对账闭环

原因：

- 当前 readiness 中仍有 `pending / stale`，会持续污染上线判断。
- 即使执行桥修通，如果历史挂单与 receipt 不收口，系统仍然不算生产态。

验收：

- readiness 中 `pending=0`
- readiness 中 `stale=0`
- Linux receipt 与 QMT 订单状态不再长期不一致

#### P0-3 把执行桥从“单笔闭环”推进到“生产态闭环”

- `D1.2` receipt 状态机完善
- `D1.3` bridge health 自动恢复
- `D1.1` 并发执行压力测试框架

原因：

- 今天已经拿到单笔 `claim -> submit -> receipt -> health` 证据，但这还不等于生产态稳定。
- 需要补齐状态流转、异常恢复和并发压测，才能把 Windows 执行桥从“调试通过”推进到“正式主链可依赖”。

验收：

- receipt 状态支持 `pending -> claimed -> submitted -> filled | rejected | cancelled | expired`
- bridge 超过阈值不更新时会自动告警和阻止新派发
- 并发 5 笔 intent 时无重复 claim、无重复 receipt、断网恢复后可补回状态

### P1：再补盘中速度与交易质量

#### P1-1 先把止损、止盈、做T 真正接上执行链

- `B1.3` 自适应止损替换固定百分比
- `B1.4` 做T 信号接入快退审批链

原因：

- 这两项最直接影响实盘盈亏，优先级高于“更早发现机会”。
- 当前盘中最需要解决的是“少亏”和“及时动作”，而不是先追求候选池更漂亮。

验收：

- 卖出信号不再只依赖固定 `-3%`
- 做T 卖出不再停留在 preview，而是进入快审与执行链

#### P1-2 量化盘中延迟，避免“看起来很快”

- `B1.5` 盘中决策延迟监控

原因：

- 没有延迟数据，就无法证明“秒级巡视”是否真的转化成秒级执行。

验收：

- 可以持续观测：
  - `opportunity_scan -> candidate_inject`
  - `sell_signal -> fast_track_review -> order_submit`
  - `position_watch_cycle`
- 超时时有明确告警

#### P1-3 再提前机会识别，并强化主线同步性

- `B1.1` 涨停基因早期识别
- `B1.2` 板块联动同步性检测

原因：

- 只有在卖出与执行链已经稳定后，提前识别机会才真正有价值。
- 否则会把系统变成“更早发现，但仍然来不及决策”的半成品。

验收：

- 盘中机会不再等到 6%-7% 才被发现
- 孤立上涨票会被降级，板块级联票会被提升

### P2：补 Agent 的真实市场理解

#### P2-1 先做 MarketRegime，再强制注入 compose 主链

- `A1.1` 市场状态感知模块
- `A1.2` 将 `MarketRegime` 注入 compose 主链

原因：

- 这是后面因子推荐、playbook 推荐、候选多元化的前提。
- 没有 regime，后面的“智能推荐”仍然只是关键词匹配换壳。

验收：

- compose trace 中出现 `regime_label`
- compose trace 中出现 `hot_sector_chain`
- hypothesis 与实际 regime 矛盾时能生成 `regime_mismatch_warning`

#### P2-2 再把因子与 playbook 推荐改成 regime 驱动

- `A1.3` 因子推荐从关键词匹配升级为 regime 驱动
- `A1.4` playbook 推荐同步升级

原因：

- 这一步决定 Agent 是不是仍然在“已有词库里碰运气”，还是开始按市场阶段组织工具。

验收：

- 不同 `regime_label` 下，推荐的 factor / playbook 明显变化
- hypothesis 关键词只做辅助，而不再是主驱动

#### P2-3 最后做候选来源多元化

- `A1.5` 候选来源多元化

原因：

- 先有市场状态，再谈从热点链、概念扩散、资金异动里抽票，否则只是盲目加候选噪声。

验收：

- 候选池里能区分 `regime_driven` 与 `market_universe_scan`
- 热点主线票在候选池中的占比明显提高

### P3：最后做治理反哺，让系统行为被治理结果改变

#### P3-1 先处理无效因子

- `E1.2` 无效因子自动降权而非仅 warning

原因：

- 这是最直接、最稳定、最不容易产生副作用的治理反馈。

验收：

- 连续无效因子会自动进入 `degraded` 或 `auto_disabled`

#### P3-2 再让 Agent 信誉分反向影响 compose

- `E1.1` Agent 信誉分直接影响 compose 权重

原因：

- 在市场理解和执行主链未稳定之前，过早放大 governance score 容易引入错误反馈。

验收：

- compose trace 中能看到 governance 权重调整
- 低信誉 Agent 的建议会被实际降权，而不是只记录不生效

#### P3-3 最后做 playbook 自适应与治理面板

- `E1.3` 策略回溯 PnL 驱动 playbook 权重
- `E1.4` 治理仪表盘端点

原因：

- 需要等 attribution 样本积累后，playbook 自动升降权才不会沦为拍脑袋治理。

验收：

- 连续亏损的 playbook 会进入 `probation`
- `/system/governance/dashboard` 能展示最近治理动作

---

## 建议实际推进顺序

```text
P0-1: C1.1 -> C1.2 -> C1.4
P0-2: C1.3 -> D1.4
P0-3: D1.2 -> D1.3 -> D1.1

P1-1: B1.3 -> B1.4
P1-2: B1.5
P1-3: B1.1 -> B1.2

P2-1: A1.1 -> A1.2
P2-2: A1.3 -> A1.4
P2-3: A1.5

P3-1: E1.2
P3-2: E1.1
P3-3: E1.3 -> E1.4
```

---

## 这版重排的核心判断

1. 先解决“系统明明能交易，却被错误准入拦住”的问题。
2. 再解决“盘中能不能快、能不能少亏”的问题。
3. 再解决“Agent 是否真的懂市场”的问题。
4. 最后才做“治理如何反向改变系统行为”的问题。

换句话说，优先级不是按“架构看起来高级不高级”排，而是按：

```text
真实可交易
-> 真实可控风险
-> 真实盘中有效
-> 真实市场理解
-> 真实治理反哺
```

来排。
        {"action": "playbook_probation", "target": "趋势跟随", "reason": "近10笔平均收益-1.8%", "at": "2026-04-20"}
      ]
    }
    ```

---

## 执行优先级

```
紧急（本周内）
  C1.1 — 受控准入口径对齐（一处配置改动，立即解除 blocked）
  C1.3 — 历史挂单治理（清除 readiness 中的 pending/stale/warning）
  A1.1 — 市场状态感知模块（后续所有 regime 驱动改造的基础）
  D1.3 — bridge health 自动恢复（生产态必备）

高优（下周）
  A1.2 + A1.3 — 将 regime 注入 compose 和因子推荐
  B1.1 + B1.2 — 涨停基因早期识别 + 板块联动检测
  E1.1 + E1.2 — Agent 信誉分影响权重 + 无效因子自动降权
  D1.4 — 执行对账闭环

中优（两周内）
  A1.4 + A1.5 — playbook regime 标签 + 候选来源多元化
  B1.3 + B1.4 — 自适应止损 + 做T信号接入
  C1.2 + C1.4 — 口径一致性检查 + readiness 分级
  E1.3 — PnL 驱动 playbook 权重

低优（后续迭代）
  D1.1 — 并发压测
  D1.2 — receipt 状态机完善
  B1.5 — 延迟监控
  E1.4 — 治理仪表盘
```

---

## 2026-04-20 完成注脚

- A1.1 已完成：新增 `src/ashare_system/market/regime_detector.py`，输出 `regime_label / hot_sector_chain / sector_strength_rank / money_flow_signal / confidence`；真实边界：当前是规则型市场状态识别，不是历史回测最优参数。
- A1.2 已完成：`compose` 与 `compose-from-brief` 已强制注入 `detected_market_regime`，并生成 `regime_mismatch_warning`；验证：`tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_compose_from_brief_injects_detected_market_regime`。
- A1.3 已完成：`factor_registry` 已支持按 regime 推荐因子，关键词退为辅助权重。
- A1.4 已完成：`playbook_registry` 已支持 `applicable_regimes / priority_score / probation`，并按 regime 输出推荐战法。
- A1.5 已完成：`scheduler.py` 的基础样本生成已合并 `regime_driven` 候选，补入热点龙头、主线补涨、资金异动三类来源；真实边界：资金异动在无主力净流入明细时退化为成交额代理，不伪造外部资金数据；验证：`tests.test_position_watch.PositionWatchTests.test_build_regime_driven_candidate_payloads_marks_regime_sources`。

- B1.1 已完成：`run_fast_opportunity_scan()` 改为 `early_momentum / acceleration / pre_limit_up` 三级识别，3% 起跟踪，5%-7% 走斜率与量比门槛；验证：`tests.test_position_watch.PositionWatchTests.test_run_fast_opportunity_scan_detects_early_and_acceleration_stages`。
- B1.2 已完成：盘中机会扫描新增 `sector_sync_strong / sector_cascade / sector_isolated` 板块同步性判定，并把孤立票降级。
- B1.3 已完成：快路径止损改为 `max(-2 * ATR_pct, -0.05)`，并接入峰值回撤保护；实现落点在 `scheduler.py` 的快巡视链，`sell_decision.py` 同步补了 `DAY_TRADING_HIGH_SELL` 退出语义。
- B1.4 已完成：做T高抛已走 `_fast_track_exit_review()` + 执行/排队链，做T低吸会注入 `source=day_trading_rebuy` 候选并进入快讨论；验证：`tests.test_position_watch.PositionWatchTests.test_run_position_watch_scan_injects_day_trading_rebuy_candidates`。
- B1.5 已完成：新建 `src/ashare_system/monitor/latency_tracker.py`，记录 `opportunity_scan -> candidate_inject`、`sell_signal -> order_submit`、`position_watch_cycle` 三类链路，并在快巡视任务中触发延迟告警模板；真实边界：当前阈值是工程基线，不是实盘统计学 SLA。

- C1.1 已完成：受控准入仓位上限 fallback 已改为 `参数 > 环境变量 > runtime_config > parameter_service > 0.3`。
- C1.2 已完成：新增 `GET /system/deployment/parameter-consistency`；验证：`tests.test_upgrade_workflow.UpgradeWorkflowTests.test_parameter_consistency_endpoint_reports_drift`。
- C1.3 已完成：挂单治理、桥守护、对账任务均已接入 scheduler，盘后可自动清理 stale intent / orphaned receipt。
- C1.4 已完成：`controlled-apply-readiness` 已区分 `ready / degraded_allow / blocked`，且单纯 `parameter_drift_warning` 不再错误降级；验证：`tests.test_upgrade_workflow.UpgradeWorkflowTests.test_controlled_apply_readiness_ready_for_single_whitelisted_intent`。

- D1.1 已完成：新增 `tests/test_execution_bridge_concurrent.py`，覆盖多 intent claim、拒单回执、stale claim retry；真实边界：这是 mock 压测框架，不是 Windows 生产链路并发实测。
- D1.2 已完成：`execution_gateway.py` 已补全 `claimed / submitted / partial_filled / filled / rejected / canceled / expired` 状态流转、回执审计轨迹与 stale claim retry。
- D1.3 已完成：盘中已注册 `execution.bridge_guardian:check`，桥断连会阻断新 intent 派发并输出守护状态。
- D1.4 已完成：新增 `src/ashare_system/execution/reconciliation.py` 并挂到盘后任务，支持 Linux/QMT 订单对账与状态修正。

- E1.1 已完成：compose 主链已读取 agent score 并对低/高信誉 Agent 建议做权重调整；真实边界：当前 `governance_score` 由 `new_score / 20` 近似，不是独立治理字段。
- E1.2 已完成：无效因子已按连续无效天数执行 `degraded / auto_disabled`，连续恢复后自动复权。
- E1.3 已完成：结算归因会生成 playbook priority update 并回灌注册表；真实边界：当前 priority 仍以内存态更新为主，未形成独立持久化版本库。
- E1.4 已完成：新增 `GET /system/governance/dashboard`，汇总 `agent_scores / factor_health / playbook_health / recent_governance_actions`；验证：`tests.test_upgrade_workflow.UpgradeWorkflowTests.test_governance_dashboard_exposes_agent_factor_and_playbook_health`。

- 补充修正 1：执行桥已允许 `claimed -> rejected / canceled / filled` 直接终态回执，避免券商拒单被误判为非法状态流转；验证：`tests.test_execution_bridge_concurrent.ExecutionBridgeConcurrentTests.test_gateway_receipt_rejected_and_stale_claim_retry`。
- 补充修正 2：讨论 finalize 的自动学分结算不再跳过零 delta 但带 settlement_key 的场景，`settled_matches` 会真实累加；验证：`tests.test_upgrade_workflow.UpgradeWorkflowTests.test_discussion_finalize_auto_settles_agent_scores_once`。

## 涉及文件索引

| 文件 | 关联任务 |
|------|---------|
| `src/ashare_system/market/regime_detector.py` | A1.1（新建）|
| `src/ashare_system/apps/runtime_api.py` | A1.2 |
| `src/ashare_system/strategy/strategy_composer.py` | A1.2, E1.1 |
| `src/ashare_system/strategy/factor_registry.py` | A1.3 |
| `src/ashare_system/strategy/playbook_registry.py` | A1.4, E1.3 |
| `src/ashare_system/scheduler.py` | A1.5, B1.1, B1.2, B1.4, C1.3, D1.3 |
| `src/ashare_system/strategy/sell_decision.py` | B1.3 |
| `src/ashare_system/strategy/day_trading.py` | B1.4 |
| `src/ashare_system/monitor/latency_tracker.py` | B1.5（新建）|
| `src/ashare_system/apps/system_api.py` | C1.1, C1.2, C1.4, E1.4 |
| `src/ashare_system/execution_gateway.py` | D1.2 |
| `src/ashare_system/execution/reconciliation.py` | D1.4（新建）|
| `src/ashare_system/strategy/compose_factor_policy.py` | E1.2 |
| `src/ashare_system/strategy/factor_monitor.py` | E1.2 |
| `src/ashare_system/learning/score_state.py` | E1.1 |
| `src/ashare_system/learning/attribution.py` | E1.3 |
| `tests/test_execution_bridge_concurrent.py` | D1.1（新建）|

---

## 核心判断总结

| 维度 | 当前状态 | 目标状态 | 最关键差距 |
|------|---------|---------|-----------|
| Agent 市场理解 | 关键词匹配选因子/战法 | regime 驱动的主动推理 | 缺 `regime_detector`，因子选择不受市场状态驱动 |
| 盘中速度 | 涨到 6% 才发现、固定止损 | 3% 起跟踪、ATR 自适应止损 | 触发太晚，止损太粗 |
| 口径统一 | 三方口径各自为政 | 自动对齐+一致性检查 | `controlled_apply` 默认 0.2 vs 运营 0.4 |
| 执行桥 | 单笔闭环证据 | 并发稳定+自动恢复+对账 | 缺恢复守护、缺对账、缺并发验证 |
| 治理闭环 | 留痕但不反向影响 | 分数/PnL 直接调权重 | Elo→compose 链路断裂 |
