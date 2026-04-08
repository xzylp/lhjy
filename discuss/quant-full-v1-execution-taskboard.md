# A股量化交易系统完整方案 v1 执行任务板

> 基于：
> - [quant-full-v1-complete.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-complete.md)
> - [quant-full-v1-task-breakdown.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-task-breakdown.md)
> - [执行记录-codex.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/执行记录-codex.md)
>
> 更新时间：2026-04-08

## 1. 状态说明

- `DONE`：已完成并有代码/测试支撑
- `DOING`：正在实现或刚接入，仍需补验证
- `TODO`：尚未开始
- `BLOCKED`：受环境或依赖限制，暂时不能推进

---

## 2. 总体进度

| 里程碑 | 目标 | 当前状态 | 说明 |
|---|---|---|---|
| M0 | 数据契约扩展与最小集成 | `DONE` | `MarketProfile / SectorProfile / PlaybookContext / ExitContext` 已落地 |
| M1 | 市场状态机 + 板块联动引擎 | `DONE` | `regime`、`sector_cycle` 已接入情绪链路 |
| M2 | 战法路由 + 买入决策改造 | `DONE` | `router` 与 `buy_decision` 已打通 |
| M3 | 个股股性画像 + 龙头相对排名 | `DONE` | 已新增画像、前排排序和路由增强 |
| M4 | 退出引擎 | `DONE` | 已新增 `ExitEngine` 并接入 `sell_decision` 回退 |
| M5 | OpenClaw 编排和讨论协议落地 | `DONE` | 最小协议层、meeting/finalize packet、兼容修复、strategy/runtime dossier API、真实 regime/playbook 持久化已完成 |
| M6 | 回测归因与学习闭环 | `DOING` | 已落地最小归因对象层与查询接口，待继续扩成完整回测/学习闭环 |

---

## 3. 已完成任务

### T01. 扩展量化核心契约

- 状态：`DONE`
- 文件：
  - [contracts.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/contracts.py)
- 结果：
  - 已有：
    - `RegimeName`
    - `SectorProfile`
    - `PlaybookContext`
    - `ExitContext`
  - 本轮补充：
    - `StockBehaviorProfile`
    - `LeaderRankResult`
    - `ExitSignal`
- 验收：
  - 可序列化
  - 能被 `strategy` 和 `risk` 层消费

### T02. 市场状态机与板块联动

- 状态：`DONE`
- 文件：
  - [regime.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/sentiment/regime.py)
  - [sector_cycle.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/sentiment/sector_cycle.py)
  - [calculator.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/sentiment/calculator.py)
  - [emotion_shield.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/risk/emotion_shield.py)
- 结果：
  - `MarketProfile` 已具备 `regime / regime_score / allowed_playbooks / sector_profiles`
  - 情绪和 `regime` 已在同一对象中合流
  - 冰点、高潮的战法限制已接入

### T03. 战法路由与买入决策改造

- 状态：`DONE`
- 文件：
  - [router.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/router.py)
  - [buy_decision.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/buy_decision.py)
  - [leader_chase.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/playbooks/leader_chase.py)
  - [divergence_reseal.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/playbooks/divergence_reseal.py)
  - [sector_reflow.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/playbooks/sector_reflow.py)
- 结果：
  - 已从“统一阈值打分”升级到“先路由战法，再输出买入候选”
  - 保留了旧入口兼容

### T04. 个股股性画像

- 状态：`DONE`
- 文件：
  - [stock_profile.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/stock_profile.py)
- 结果：
  - 已支持构建：
    - `board_success_rate_20d`
    - `bomb_rate_20d`
    - `next_day_premium_20d`
    - `reseal_rate_20d`
    - `optimal_hold_days`
    - `style_tag`
    - `avg_sector_rank_30d`
    - `leader_frequency_30d`
- 当前取舍：
  - 先使用日级稳定字段
  - 不依赖逐笔

### T05. 龙头相对排名

- 状态：`DONE`
- 文件：
  - [leader_rank.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/leader_rank.py)
  - [router.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/router.py)
- 结果：
  - 已输出：
    - `leader_score`
    - `zt_order_rank`
    - `is_core_leader`
  - 已接入 `router`
  - 路由排序不再只看板块强度，也开始考虑前排和股性

### T06. 战法化退出引擎

- 状态：`DONE`
- 文件：
  - [exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/exit_engine.py)
  - [sell_decision.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/sell_decision.py)
- 结果：
  - 已覆盖：
    - `entry_failure`
    - `board_break`
    - `sector_retreat`
    - `no_seal_on_surge`
    - `time_stop`
  - 未命中新规则时，仍回退旧 ATR 卖出逻辑

### T07. 基线与阶段测试

- 状态：`DONE`
- 测试文件：
  - [test_quant_first_batch.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_quant_first_batch.py)
  - [test_phase3_strategy_risk_backtest.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase3_strategy_risk_backtest.py)
  - [test_phase_stock_profile.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_stock_profile.py)
  - [test_phase_leader_rank.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_leader_rank.py)
  - [test_phase_exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_exit_engine.py)
- 验证命令：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_quant_first_batch.py tests/test_phase3_strategy_risk_backtest.py tests/test_phase_stock_profile.py tests/test_phase_leader_rank.py tests/test_phase_exit_engine.py`
- 当前结果：
  - `102 passed`

---

## 4. 下一批任务

### T08. 讨论协议最小对象层

- 状态：`DONE`
- 对应里程碑：M5
- 目标：
  - 固定多 agent 的输入输出协议
  - 为 `runtime / research / strategy / risk / audit` 提供统一 dossier / packet / finalize 结构
- 已落地：
  - [protocol.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/protocol.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [team_registry.final.json](/mnt/d/Coding/lhjy/ashare-system-v2/openclaw/team_registry.final.json)
  - [ashare.txt](/mnt/d/Coding/lhjy/ashare-system-v2/openclaw/prompts/ashare.txt)
- 验收：
  - `agent_packets / meeting_context / finalize_packet` 已固定
  - finalize 返回 `finalize_packet`
  - envelope 已补齐 `generated_at` 与新旧字段别名兼容

### T09. strategy/runtime API 对外暴露 playbook 结果

- 状态：`DONE`
- 对应里程碑：M5
- 目标：
  - 让外部可查询：
    - 当前 `regime`
    - 热门板块
    - playbook 分配
    - 候选 dossier
- 已落地：
  - [strategy_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/strategy_api.py)
  - [runtime_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
  - 新增接口：
    - `/strategy/context/latest`
    - `/strategy/candidates/latest`
    - `/runtime/context/latest`
    - `/runtime/dossiers/latest`
    - `/runtime/dossiers/{trade_date}/{symbol}`
- 当前取舍：
  - 当前已优先持久化真实 `market_profile / sector_profiles / playbook_contexts`
  - 若行情侧缺少有效 `pre_close`，运行时会回退到 `runtime_heuristic_fallback`
  - 板块联动优先用 dossier `sector_tags`，缺失时回退候选池聚类
- 验收：
  - `strategy/runtime` 接口能返回真实 `regime / allowed_playbooks / playbook_contexts`
  - dossier 单票接口可看到 `playbook_context`

### T10. OpenClaw prompt 与消息总线收口

- 状态：`DONE`
- 对应里程碑：M5
- 目标：
  - 固定 ashare 主持人和子代理的消息结构
  - 减少自由文本，增加结构化摘要
- 已落地：
  - `ashare` prompt 已对齐 packet / finalize 结构
  - team registry 已加入协议字段约束
  - 旧 discussion 语义已做兼容：
    - `support / watch`
    - `support_points / oppose_points`
    - `selection_*` 别名
- 验收：
  - round 1 / round 2 / finalize 可表达
  - prompt 与协议字段一致

### T11. 回测归因对象层

- 状态：`DONE`
- 对应里程碑：M6
- 目标：
  - 按：
    - `playbook`
    - `regime`
    - `exit_reason`
    聚合收益和命中率
- 建议文件：
  - `backtest/`
  - `learning/`
  - `reports/`
- 验收：
  - 能输出最小 attribution report
- 已落地：
  - [attribution.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/learning/attribution.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - 新增接口：
    - `/system/learning/attribution`
    - `/system/learning/trade-review`
  - `agent-scores/settlements` 已同步写入归因结果
- 当前取舍：
  - 先以 `next_day_close_pct` 作为最小 outcome，支持可选 `exit_reason / holding_days / playbook / regime`
  - `playbook / regime` 优先读 settlement 显式字段，其次回退 runtime context / dossier

### T12. 学习闭环与 trade review

- 状态：`DOING`
- 对应里程碑：M6
- 目标：
  - 为后续学习环提供对象和摘要接口
  - 不只看总收益，要能看“哪种战法、哪种状态、哪种退出原因更有效”
- 验收：
  - 至少形成一版 trade review / attribution 聚合链路
- 当前进度：
  - 已有 `trade_review` 摘要接入归因报告
  - `execution_reconciliation` 已能把真实成交均价、数量、订单号回写到 attribution
  - `execution_api` 手动 SELL 已支持把 `exit_reason / playbook / regime / trade_date` 写入 order journal
  - `scheduler.tail_market` 已接入最小自动卖出扫描：
    - 优先消费 `runtime_context.playbook_contexts / sector_profiles / market_profile`
    - 缺少上下文时回退到 `SellDecisionEngine` 的 ATR / time-stop 规则
  - `execution_intents / execution_dispatch` 买入链路已补齐：
    - `trade_date`
    - `playbook`
    - `regime`
    - `resolved_sector`
    这些字段现在会从 precheck 一路带到 BUY request 和 order journal
  - 自动 SELL 现在会把 `trade_date / playbook / regime / exit_reason / request / submitted_at` 写入 `execution_order_journal`
  - 自动 SELL 成交后，`/system/execution-reconciliation/run` 已能把这些元数据回写到 attribution
  - 已新增尾盘扫描观测接口：
    - `/system/tail-market/run`
    - `/system/tail-market/latest`
    - `/system/tail-market/history`
  - `tail_market` 退出上下文已继续加厚：
    - 优先读取 serving 层最新 `dossier_pack`
    - 复用 `symbol_context.market_relative.relative_strength_pct`
    - 复用 `symbol_context.behavior_profile`
    - 对 `leader / defensive` 股性开始区分持有容忍度
  - `runtime/jobs/pipeline` 现已前移生成最小 `behavior_profiles`：
    - 写入 `runtime_context.behavior_profiles`
    - 写入 `dossier_pack.behavior_profiles`
    - 单票 dossier 同步落到 `symbol_context.behavior_profile`
    - `StrategyRouter` 已开始消费这批画像做 playbook 置信度和 leader rank 辅助
  - 股性画像已开始形成独立 artifact/cache：
    - `serving/latest_stock_behavior_profiles.json`
    - research state 已持久化 `latest_stock_behavior_profiles`
    - research state 已持久化 `stock_behavior_profile_history`
    - 同日 precompute 会优先复用 `artifact_cache`
    - 跨日 bars 缺失时会优先回退 `history_cache`
    - 已新增独立刷新入口 `refresh-profiles`
    - `DossierPrecomputeService.refresh_behavior_profiles(...)` 可单独刷新画像，不触发 dossier pack 重算
  - `precompute` 现已支持多根日线输入：
    - `market_adapter.get_bars(..., count=N)`
    - `DataPipeline.get_daily_bars(..., count=N)`
    - dossier 预计算会先尝试基于近 60 根日线构建 `StockBehaviorProfile`
    - runtime 侧会优先复用 dossier 画像，缺失时才回退启发式推断
  - `tail_market` 已开始接入分时级快撤信号：
    - 读取 `5m bars`
    - 计算 `intraday_change_pct / intraday_drawdown_pct / rebound_from_low_pct`
    - 结合 `monitor_context.recent_events` 的负面预警计数
    - 对 leader 风格标的增加“冲高回落不修复”的提前退出
  - `tail_market` 已开始接入板块内相对强弱快撤：
    - 会补抓同板块同行 `5m bars`
    - 计算 `sector_intraday_change_pct / sector_relative_strength_5m`
    - 对明显弱于同行的 leader 风格标的提前触发退出
  - `tail_market` 已开始接入板块相对强弱时序弱化：
    - 计算最近 3 根 `5m` 的 `sector_relative_trend_5m`
    - 统计 `sector_underperform_bars_5m`
    - 对连续多根都弱于同行的 leader 风格标的提前触发退出
  - `tail_market` 已继续接入更细的 1m 微结构快撤：
    - 会同步拉取 `1m bars`
    - 补充最近收益、局部回撤、连续转弱等微结构指标
    - 新增 `sector_sync_weak / microstructure_fast_exit / micro_rebound_failed` 审计标签
    - 退出原因仍暂时统一映射到 `time_stop`
  - `tail_market -> attribution` 学习链已开始沉淀退出事实：
    - 自动 SELL journal 会写入 `exit_context_snapshot`
    - 自动 SELL journal 会写入 `review_tags`
    - `execution_reconciliation -> learning/attribution` 会把这些事实回写到归因记录
    - `trade-review` 摘要已开始统计 `exit_tag_counts`
  - 学习查询与参数建议已开始消费退出事实：
    - `/learning/attribution` 支持按 `review_tag / exit_context_key / exit_context_value` 过滤
    - `/learning/trade-review` 会返回 `parameter_hints`
    - 当前已能对板块退潮、连续掉队、盘中弱化给出最小调参建议
  - 参数建议已可转成参数提案：
    - `/learning/parameter-hints/proposals` 支持预览
    - `apply=true` 时会复用现有参数提案链落 proposal event
    - 当前仍要求人工确认，不做自动生效策略
  - 参数提案已补审批基线与回滚基线：
    - 预览项会返回 `approval_policy`
    - 预览项会返回 `rollback_baseline`
    - 可区分 `auto_approvable` 与 `manual_review`
  - 参数提案已按审批基线分流：
    - `auto_approvable` 建议在 `apply=true` 时直接进入批准/生效链
    - `manual_review` 建议在 `apply=true` 时只生成 `evaluating` 提案
    - 响应会返回 `execution_summary.effective_event_count / pending_review_event_count`
  - 已生效参数提案开始沉淀效果追踪基线：
    - proposal event 会补写 `source_filters`
    - proposal event 会补写 `approval_policy_snapshot`
    - proposal event 会补写 `rollback_baseline`
  - 参数治理已新增效果追踪与回滚预览：
    - `GET /system/learning/parameter-hints/effects`
    - 可回看已生效提案的 `sample_count / avg_next_day_close_pct / win_rate`
    - 可标记 `rollback_recommended`
    - `POST /system/learning/parameter-hints/rollback-preview`
    - 可基于过滤样本预览建议回滚项和恢复值
  - 参数治理已新增受控 rollback event 执行链：
    - `POST /system/learning/parameter-hints/rollback-apply`
    - rollback event 会落 `event_type=param_rollback`
    - rollback event 会记录 `rollback_of_event_id`
    - 默认只对“当前仍是该提案生效中”的低风险参数直接回滚
    - 若已不是当前生效事件，则会跳过，避免重复回滚历史提案
  - 参数治理已补观察窗口与高风险审批：
    - proposal / rollback event 会写入 `observation_window`
    - 高风险 rollback 会写入 `approval_ticket`
    - `POST /system/learning/parameter-hints/rollback-approval`
    - 支持 `approve / release / reject`
    - `effects` 已开始回看 `post_rollback_tracking`
    - `GET /system/learning/parameter-hints/inspection`
    - `POST /system/learning/parameter-hints/inspection/run`
    - 可直接查看待处理高风险 rollback 与观察窗口到期预警
    - inspection 已会给出固定动作建议：
      - `continue_observe`
      - `manual_release_or_reject`
      - `consider_rollback_preview`
      - `review_and_keep / confirm_rollback_effect / review_rollback_effect`
    - inspection 已会直接附带下一步操作入口：
      - `operation_targets`
      - `action_items`
      - 可直接指向 `rollback-preview / rollback-approval / effects / inspection`
    - scheduler 已新增 `governance.parameter_hints:inspection`
    - 盘后会执行独立“参数治理巡检”任务，把巡检摘要写入审计
  - 学习复盘视图已补按标的/按原因聚合：
    - `/learning/attribution`
    - `/learning/trade-review`
    - 支持 `symbol / reason` 过滤
    - 返回 `by_symbol / by_reason`
  - `strategy/context/latest` 与 `strategy/candidates/latest`
    - 已可直接看到 `behavior_profiles`
    - 候选项已可看到 `style_tag / optimal_hold_days / leader_frequency_30d / avg_sector_rank_30d`
  - 下一步从“最小自动卖出闭环”继续升级到：
    - 更真实的持仓入场上下文
    - 把 `StockProfileBuilder` 的真实历史计算接成日更产线，替换当前残余轻量启发式画像
    - 个股股性、板块联动、分时微结构共同驱动的更细粒度退出条件
    - review 反推参数更新，而不只做归因展示

---

## 5. 当前风险和限制

- `StockProfileBuilder` 已有 artifact/cache 产物，并已接入独立“股性画像刷新”盘后调度；但画像字段仍缺少更细的分时级事实输入。
- `ExitEngine` 已开始消费股性画像，但目前只落了最小 leader/defensive 差异化规则，仍偏保守。
- `runtime/jobs/pipeline` 已持久化 `MarketProfile` 与 playbook assignment，但在 mock 行情或缺少有效昨收时会走启发式 fallback。
- 当前 `behavior_profiles` 已进主链，并已开始在 precompute 侧复用多日 bars；但其中涨停成功/炸板/回封等字段仍缺少分时级事实输入，离完整历史统计还有差距。
- 板块归属仍以事件标签和轻量 sector lookup 为主，尚未形成独立的行业映射底座。
- `M6` 已有最小归因对象层，并已接入 execution reconciliation 的真实成交回写。
- 手动 SELL 与 `tail_market` 自动 SELL 都已可带出 `exit_reason / playbook / regime`，BUY 侧派发 request/journal 也已开始沉淀这些字段。
- `tail_market` 已能读取 dossier 的 `behavior_profile + market_relative`，画像产线也已形成独立定时任务；但当前调度仍以日级刷新为主，尚未补齐更细粒度的盘中再校正。
- 分时级退出已接上 `5m + 1m` 微结构、monitor 负面预警、板块同行相对强弱、连续掉队时序信号，但仍缺更完整的盘口/逐笔结构和更长序列联动。
- 学习侧已经能按 `review_tags / exit_context / symbol / reason` 查询退出事实，并能生成带观察窗口、审批票据、回滚链的参数治理事件；`inspection/run` 与盘后“参数治理巡检”也已接入，但高风险审批和 rollback 后再评估仍属于最小实现。
- 已能通过 attribution / trade-review 做按标的和按原因复盘，但还缺独立的专门复盘页面或更聚焦的 tail-market 复盘接口。
- 测试命令需要注意环境：
  - 需要 `PYTHONPATH=src`
  - 当前环境下 `pytest` 默认捕获可能受临时目录限制，建议用 `-s -p no:cacheprovider`

---

## 6. 建议推进顺序

1. 继续做 `T12`
2. 把效果追踪推进到自动动作闭环：
   - 把最小观察窗口变成稳定调度/巡检机制
   - rollback 后的二次效果跟踪继续细化
   - 高风险 rollback 的人工审批/放行接口继续补完整状态机
3. 做一次总回归：
   - 路由
   - 买入
   - 退出
   - discussion 协议
   - attribution 聚合
   - parameter governance

---

## 7. 进度查看入口

- 方案总文档：
  - [quant-full-v1-complete.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-complete.md)
- 任务拆解：
  - [quant-full-v1-task-breakdown.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-task-breakdown.md)
- 执行记录：
  - [执行记录-codex.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/执行记录-codex.md)
- 进度任务板：
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

---

## 8. 与总方案的差异盘点

### 8.1 完成度判断

- 按 [quant-full-v1-complete.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-complete.md) 的整体目标估算：
  - 主链完成度约 `75%-80%`
  - Part 1 量化主链基本完成
  - Part 2 OpenClaw/讨论协议属于“能力已部分内嵌，但文件拆分和文档契约未补齐”
  - Part 3 Task 里的回测层、因子补齐层、讨论工程化层仍有明显缺口

### 8.2 已完成但实现路径与方案不同

- discussion 层的部分能力没有按方案拆成独立文件，而是内聚在现有模块中：
  - [candidate_case.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/candidate_case.py)
  - [protocol.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/protocol.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
- 学习闭环不只是最小 attribution：
  - 已扩到 `parameter-hints/proposals`
  - `effects / rollback-preview / rollback-apply / rollback-approval`
  - `inspection / inspection/run`
  - scheduler 盘后治理巡检

### 8.3 明确未完成项

- 因子补齐层未完成：
  - `src/ashare_system/factors/behavior/sector_linkage.py`
  - `src/ashare_system/factors/behavior/board_behavior.py`
  - `tests/test_phase_sector_linkage.py`
- 退出监控独立模块未完成：
  - `src/ashare_system/monitor/exit_monitor.py`
- 回测层未完成：
  - `src/ashare_system/backtest/playbook_runner.py`
  - `src/ashare_system/backtest/attribution.py`
  - `tests/test_phase_playbook_backtest.py`
- discussion/OpenClaw 工程化拆分未完成：
  - `src/ashare_system/discussion/contracts.py`
  - `src/ashare_system/discussion/opinion_validator.py`
  - `src/ashare_system/discussion/round_summarizer.py`
  - `src/ashare_system/discussion/finalizer.py`
- OpenClaw/讨论协议文档未完成：
  - `docs/openclaw-agent-constraints-v1.md`
  - `docs/openclaw-agent-prompts-v1.md`
  - `docs/implementation-drafts/openclaw-agent-routing-matrix-v1.md`
  - `docs/implementation-drafts/openclaw-prompt-io-contracts-v1.md`
  - `docs/implementation-drafts/discussion-state-output-matrix.md`
  - `docs/implementation-drafts/client-brief-output-template-v1.md`

---

## 9. 下一阶段并行任务拆分

### 9.1 总原则

- `Main` 只守高冲突主链：
  - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [candidate_case.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/candidate_case.py)
  - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
- `A / B / C` 只改各自独立写入范围，避免抢同一文件。
- 并行线程优先做“新增文件或低耦合文件”，不要先去碰主链路由和调度主文件。

### 9.2 Main 线

- 状态：`DOING`
- 目标：
  - 继续推进 `T12`，把学习治理从 inspection/action_items 推进到更聚焦的高优先级待办视图
  - 继续守住 scheduler/governance/tail_market 主链集成
- 具体任务：
  - Main-1：把 inspection 的 `action_items` 再压缩成高优先级待办视图
    - 状态：`DONE`
    - 已输出：
      - `high_priority_action_items`
      - `high_priority_action_item_count`
    - 已默认过滤低信号 `continue_observe`
  - Main-2：补 `tail-market` 更聚焦的 review/inspection 汇总接口
    - 状态：`DONE`
    - 已新增：
      - `GET /system/tail-market/review`
    - 已支持按：
      - `source=latest/history`
      - `symbol`
      - `exit_reason`
      - `review_tag`
      查看 tail-market 扫描退出摘要
  - Main-3：在不新增平行状态机前提下，继续细化 rollback 后二次效果追踪
    - 状态：`DONE`
    - 已新增：
      - `post_rollback_tracking.followup_status`
      - `post_rollback_tracking.recommended_action`
      - `post_rollback_tracking.operation_targets`
    - 已支持给出更直接的后续结论：
      - `not_started`
      - `insufficient_samples`
      - `continue_observe`
      - `recovery_confirmed`
      - `manual_review_required`
    - 已复用现有治理动作语义：
      - `continue_observe`
      - `confirm_rollback_effect`
      - `review_rollback_effect`
      - `manual_release_or_reject`
  - Main-4：阶段性总回归，覆盖：
    - 状态：`DOING`
    - `strategy/runtime`
    - `tail_market`
    - `discussion`
    - `system_governance`
    - 当前已验证：
      - `PYTHONPATH=src pytest tests/test_phase_strategy_runtime_api.py tests/test_discussion_helpers.py tests/test_phase1.py::TestScheduler::test_scheduler_tasks tests/test_phase1.py::TestTailMarketBehaviorAwareExit::test_tail_market_scan_uses_1m_microstructure_fast_exit -q`
      - `11 passed`
      - `PYTHONPATH=src pytest tests/test_system_governance.py -k "post_rollback_tracking or parameter_hint_proposals_auto_approve_monitor_params or high_risk_rollback_requires_manual_release or parameter_hint_inspection or learning_review_views_support_symbol_and_reason" -q`
      - `8 passed`

### 9.3 A 线：因子与退出监控

- 状态：`DOING`
- 推荐并行度：高
- 目标：
  - 补齐方案中缺失的因子注册与退出监控独立模块
- 具体任务：
  - A-1：新建 `src/ashare_system/factors/behavior/sector_linkage.py`
    - 先落最小 7 个板块联动因子注册
    - 不引入逐笔依赖
  - A-2：新建 `src/ashare_system/factors/behavior/board_behavior.py`
    - 补涨停/炸板/回封相关专项因子
  - A-3：新建 `src/ashare_system/monitor/exit_monitor.py`
    - 作为 `market_watcher` 下游
    - 先封装读取持仓、调用 `ExitEngine`、落监控结果
  - A-4：补测试
    - `tests/test_phase_sector_linkage.py`
    - `tests/test_phase_exit_monitor.py` 或合并到现有 phase 测试
  - 当前完成：
    - `sector_linkage.py` 已落最小 7 个板块联动 behavior 因子
    - `board_behavior.py` 已落涨停/炸板/回封专项因子
    - `exit_monitor.py` 已提供 `check / check_batch / summarize`
    - `factors/__init__.py` 已接入 behavior 因子自动注册
    - `market_watcher.py` 已接入 exit monitor 下游缓存与暴露，不改 `check_once()` 返回类型
    - `tests/test_phase_sector_linkage.py`
    - `tests/test_phase_exit_monitor.py`
  - 当前结果：
    - `PYTHONPATH=src pytest tests/test_phase_sector_linkage.py tests/test_phase_exit_monitor.py`
    - `8 passed`

### 9.4 B 线：回测与归因层

- 状态：`DOING`
- 推荐并行度：高
- 目标：
  - 补齐方案里缺失的 `playbook_runner`，把当前线上归因扩成可离线复盘的回测层
- 具体任务：
  - B-1：新建 `src/ashare_system/backtest/playbook_runner.py`
    - 按 `playbook / regime / exit_reason` 运行最小回测
  - B-2：新建 `src/ashare_system/backtest/attribution.py`
    - 输出分战法、分市场状态、分退出原因的摘要
  - B-3：补 `tests/test_phase_playbook_backtest.py`
  - B-4：补回测层与现有 learning/attribution 的边界说明
    - 避免新旧 attribution 命名冲突
  - 当前完成：
    - `playbook_runner.py` 已落最小离线回测骨架
    - `backtest/attribution.py` 已落离线回测 attribution
    - `engine.py` 已补 `BacktestResult.trades -> trade records` 适配桥接
    - `playbook_runner.run()` 已支持直接消费 `BacktestResult`
    - `attribution.py` 已补 `overview / export_payload`
    - `tests/test_phase_playbook_backtest.py`
  - 当前结果：
    - `PYTHONPATH=src TMPDIR=/tmp pytest tests/test_phase_playbook_backtest.py -q`
    - `4 passed`

### 9.5 C 线：discussion/OpenClaw 工程化

- 状态：`DOING`
- 推荐并行度：中高
- 目标：
  - 把已内嵌在 discussion 现有实现里的能力拆成更稳定的工程文件和文档契约
- 具体任务：
  - C-1：新建 `src/ashare_system/discussion/contracts.py`
    - 收口扩展 opinion schema
  - C-2：新建 `src/ashare_system/discussion/opinion_validator.py`
    - 固化 Round 1 / Round 2 最小校验规则
  - C-3：新建 `src/ashare_system/discussion/round_summarizer.py`
    - 从 opinions 提炼 `controversy_summary_lines / support_points / oppose_points / evidence_gaps`
  - C-4：新建 `src/ashare_system/discussion/finalizer.py`
    - 固化 `selected / watchlist / rejected / execution_candidates` 汇总
  - C-5：补 6 份文档
    - `docs/openclaw-agent-constraints-v1.md`
    - `docs/openclaw-agent-prompts-v1.md`
    - `docs/implementation-drafts/openclaw-agent-routing-matrix-v1.md`
    - `docs/implementation-drafts/openclaw-prompt-io-contracts-v1.md`
    - `docs/implementation-drafts/discussion-state-output-matrix.md`
    - `docs/implementation-drafts/client-brief-output-template-v1.md`
  - 当前完成：
    - `discussion/contracts.py`
    - `discussion/opinion_validator.py`
    - `discussion/round_summarizer.py`
    - `discussion/finalizer.py`
    - `discussion_service.py` 已补 `build_summary_snapshot(...)` / `build_finalize_bundle(...)`
    - `state_machine.py` 已补 `needs_round_2(...)` / `can_finalize_from_summary(...)`
    - `tests/test_discussion_helpers.py`
    - 6 份 OpenClaw / discussion 文档
  - 当前结果：
    - `PYTHONDONTWRITEBYTECODE=1 ... pytest -p no:cacheprovider tests/test_discussion_helpers.py tests/test_phase5_monitor_notify_report.py -k "discussion_helpers or DiscussionFinalizeNotifier"`
    - `8 passed, 43 deselected`

### 9.6 当前不建议并行改动的高冲突文件

- [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
- [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
- [candidate_case.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/candidate_case.py)
- [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
- [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)

### 9.7 线程首批任务包

#### Main 首批任务包

- Main-P1：高优先级治理待办视图
  - 文件：
    - [inspection.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/governance/inspection.py)
    - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
    - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - 目标：
    - 从 `action_items` 再压出真正的高优先级待办
    - 默认过滤掉低信号 `continue_observe`
  - 验收：
    - inspection 返回高优先级待办列表
    - scheduler 审计能看到高优先级待办数量
  - 当前状态：`DONE`
- Main-P2：tail-market 聚焦复盘接口
  - 文件：
    - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
    - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
    - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
  - 目标：
    - 给 `tail_market` 增加更聚焦的 review/inspection 查询视图
  - 验收：
    - 能按 `review_tags / symbol / exit_reason` 看自动卖出摘要
  - 当前状态：`DONE`
- Main 推荐回归：
  - `PYTHONPATH=src pytest tests/test_system_governance.py -k "parameter_hint or learning_review" -q`
  - `PYTHONPATH=src pytest tests/test_phase1.py -k "tail_market or scheduler" -q`

#### A 线首批任务包

- A-P1：板块联动因子最小落地
  - 文件：
    - `src/ashare_system/factors/behavior/sector_linkage.py`
    - `tests/test_phase_sector_linkage.py`
  - 目标：
    - 先把方案里 7 个 sector linkage 指标注册进去
    - 测试覆盖基本输入输出，不接逐笔
  - 验收：
    - 因子能注册
    - 至少覆盖板块强度、活跃天数、回流分数、离散度
- A-P2：涨停板专项因子
  - 文件：
    - `src/ashare_system/factors/behavior/board_behavior.py`
    - `tests/test_phase_sector_linkage.py` 或独立 `tests/test_phase_board_behavior.py`
  - 目标：
    - 把 `seal_success / bombed / afternoon_resealed / next_day_return` 相关统计因子独立出来
  - 验收：
    - 能被股性画像或后续回测层复用
- A-P3：退出监控独立模块
  - 文件：
    - `src/ashare_system/monitor/exit_monitor.py`
    - `tests/test_phase_exit_monitor.py`
  - 目标：
    - 读取持仓、拼 ExitContext、调用 ExitEngine、产出结构化监控结果
  - 验收：
    - 不直接下单
    - 只做 signal/check 产出
- A 推荐回归：
  - `PYTHONPATH=src pytest tests/test_phase_sector_linkage.py -q`
  - `PYTHONPATH=src pytest tests/test_phase_exit_monitor.py -q`
  - 当前状态：`DONE`

#### B 线首批任务包

- B-P1：playbook runner 最小回测骨架
  - 文件：
    - `src/ashare_system/backtest/playbook_runner.py`
    - `tests/test_phase_playbook_backtest.py`
  - 目标：
    - 支持按 `playbook / regime / exit_reason` 过滤并运行最小回测
  - 验收：
    - 能输出 trade list 和 summary
- B-P2：backtest attribution
  - 文件：
    - `src/ashare_system/backtest/attribution.py`
    - `tests/test_phase_playbook_backtest.py`
  - 目标：
    - 输出分战法、分市场状态、分退出原因聚合
  - 验收：
    - 避免与 `learning/attribution.py` 重名语义冲突
    - 文档或模块注释说明“一个偏离线回测，一个偏线上事实归因”
- B-P3：回测层边界说明
  - 文件：
    - [执行记录-codex.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/执行记录-codex.md)
    - 或新增 implementation draft 文档
  - 目标：
    - 明确 playbook_runner 与现有 backtest/engine、learning/attribution 的边界
- B 推荐回归：
  - `PYTHONPATH=src pytest tests/test_phase_playbook_backtest.py -q`
  - 当前状态：`DONE`

#### C 线首批任务包

- C-P1：discussion schema 与 validator
  - 文件：
    - `src/ashare_system/discussion/contracts.py`
    - `src/ashare_system/discussion/opinion_validator.py`
  - 目标：
    - 固化 Round 1 / Round 2 opinion 最小校验规则
  - 验收：
    - 至少校验 thesis、key_evidence、evidence_gaps、round_2 响应字段
- C-P2：round summarizer 与 finalizer
  - 文件：
    - `src/ashare_system/discussion/round_summarizer.py`
    - `src/ashare_system/discussion/finalizer.py`
  - 目标：
    - 从 opinions 提炼 `controversy_summary_lines`
    - 汇总 `selected / watchlist / rejected / execution_candidates`
  - 验收：
    - 先不强改主链，只提供可接入 helper
- C-P3：OpenClaw/讨论文档首批
  - 文件：
    - `docs/openclaw-agent-constraints-v1.md`
    - `docs/openclaw-agent-prompts-v1.md`
    - `docs/implementation-drafts/discussion-state-output-matrix.md`
  - 目标：
    - 先补最关键的约束、提示词、状态产物矩阵
  - 验收：
    - 文档内容与当前实现一致，不写未来空想接口
- C 推荐回归：
  - 若只做文档，可不跑代码测试
  - 若补了 helper，建议补最小 discussion 单测或至少 `py_compile`
  - 当前状态：`DONE`

### 9.9 第二批并行任务包

#### A 线第二批

- A-P4：因子自动注册接入
  - 文件：
    - `src/ashare_system/factors/__init__.py`
    - 必要时补现有因子引擎初始化入口
    - `tests/test_phase_sector_linkage.py`
  - 目标：
    - 把 `sector_linkage.py` / `board_behavior.py` 接入全局自动注册链
  - 验收：
    - 不靠手工 import 也能注册
  - 当前状态：`DONE`
- A-P5：exit_monitor 接入 market_watcher 下游
  - 文件：
    - `src/ashare_system/monitor/market_watcher.py`
    - `src/ashare_system/monitor/exit_monitor.py`
    - 对应测试
  - 目标：
    - 在不下单的前提下，把 exit monitor 信号纳入盯盘输出或审计
  - 验收：
    - `market_watcher` 能产出 exit monitor signals
    - 不直接改 execution/scheduler 报单逻辑
  - 当前状态：`DONE`

#### B 线第二批

- B-P4：补 playbook runner 与现有 backtest/engine 的桥接
  - 文件：
    - `src/ashare_system/backtest/playbook_runner.py`
    - 必要时少量改 `backtest/engine.py` 调用层
    - `tests/test_phase_playbook_backtest.py`
  - 目标：
    - 不重复造轮子，明确 runner 如何调用底层 engine
  - 验收：
    - runner 能消费 engine 输出或最小兼容适配
  - 当前状态：`DONE`
- B-P5：补离线回测 summary/export 结构
  - 文件：
    - `src/ashare_system/backtest/attribution.py`
    - `tests/test_phase_playbook_backtest.py`
  - 目标：
    - 输出更适合 review 的 summary_lines / by_playbook / by_regime / by_exit_reason
  - 验收：
    - 返回结构可直接用于后续 API 或文档接入
  - 当前状态：`DONE`

#### C 线第二批

- C-P4：discussion helper 接入点最小化集成设计
  - 文件：
    - `src/ashare_system/discussion/discussion_service.py`
    - `src/ashare_system/discussion/state_machine.py`
    - 仅在低风险范围内补 helper 调用点或 guard 设计说明
  - 目标：
    - 先把 validator / summarizer / finalizer 的接入点钉住
  - 验收：
    - 不重写主链
    - 至少形成一个可并入主线的小接入点
  - 当前状态：`DONE`
- C-P5：discussion 最小单测
  - 文件：
    - 新增 discussion helper tests
  - 目标：
    - 给 contracts / validator / summarizer / finalizer 各补最小单测
  - 验收：
    - 后续主线接入时不需要再猜 helper 行为
  - 当前状态：`DONE`

### 9.8 可直接发给线程的任务提示词

#### 发给 Main

```text
你负责 Main 主线，只能改以下高冲突主链文件：
- src/ashare_system/governance/inspection.py
- src/ashare_system/apps/system_api.py
- src/ashare_system/scheduler.py
- tests/test_system_governance.py
- tests/test_phase1.py

本轮目标：
1. 把 inspection 现有的 action_items 再压成真正的高优先级待办视图，默认过滤低信号 continue_observe。
2. 给 tail_market 补一个更聚焦的 review/inspection 汇总接口，能按 review_tags / symbol / exit_reason 查看自动卖出摘要。
3. 不新增平行状态机，继续细化 rollback 后二次效果追踪。

约束：
- 不要动 candidate_case.py 之外的 discussion 大改，不要重构协议层。
- 不要改 A/B/C 线负责的新文件。
- 继续保持 scheduler / system_api 向后兼容。

交付要求：
- 列出改动文件
- 给出行为变化
- 跑最小相关回归
- 回传失败风险和剩余缺口
```

#### 发给 A 线

```text
你负责 A 线：因子与退出监控，只改你自己的独立文件，不要碰 scheduler.py、system_api.py、candidate_case.py。

本轮目标文件：
- src/ashare_system/factors/behavior/sector_linkage.py
- src/ashare_system/factors/behavior/board_behavior.py
- src/ashare_system/monitor/exit_monitor.py
- tests/test_phase_sector_linkage.py
- tests/test_phase_exit_monitor.py

本轮目标：
1. 新建 sector_linkage.py，先落最小 7 个板块联动因子注册，不引入逐笔依赖。
2. 新建 board_behavior.py，补涨停/炸板/回封相关专项因子。
3. 新建 exit_monitor.py，作为 market_watcher 下游，只做 signal/check，不直接下单。
4. 补对应测试，至少覆盖注册成功、基本输入输出和最小监控结果。

约束：
- 不要改 Main 主链文件。
- 不要把 exit_monitor 接进 scheduler，先把模块本身和测试做好。
- 优先做“最小可用 + 可测试”，避免过度设计。

交付要求：
- 列出新增/修改文件
- 说明每个模块输出什么
- 给出建议接入点，但不要主动改主链接入
- 运行相关 pytest
```

#### 发给 B 线

```text
你负责 B 线：回测与归因层，只改 backtest 相关文件和自己的测试/文档，不要碰 scheduler.py、system_api.py、learning/attribution.py 主线。

本轮目标文件：
- src/ashare_system/backtest/playbook_runner.py
- src/ashare_system/backtest/attribution.py
- tests/test_phase_playbook_backtest.py

本轮目标：
1. 新建 playbook_runner.py，支持按 playbook / regime / exit_reason 跑最小回测。
2. 新建 backtest/attribution.py，输出分战法、分市场状态、分退出原因聚合。
3. 补 tests/test_phase_playbook_backtest.py，覆盖最小 trade list 和 summary。

约束：
- 不要和现有 learning/attribution.py 混成同一语义。
- 请在模块注释或返回结构里明确：这是离线回测 attribution，不是线上事实归因。
- 优先做可离线运行的最小骨架，不接真实生产调度。

交付要求：
- 列出文件
- 说明 backtest attribution 与 learning attribution 的边界
- 运行相关 pytest
```

#### 发给 C 线

```text
你负责 C 线：discussion/OpenClaw 工程化。尽量只改 discussion 新文件和 docs，不要碰 scheduler.py、system_api.py、candidate_case.py 主链逻辑。

本轮目标文件：
- src/ashare_system/discussion/contracts.py
- src/ashare_system/discussion/opinion_validator.py
- src/ashare_system/discussion/round_summarizer.py
- src/ashare_system/discussion/finalizer.py
- docs/openclaw-agent-constraints-v1.md
- docs/openclaw-agent-prompts-v1.md
- docs/implementation-drafts/discussion-state-output-matrix.md
- docs/implementation-drafts/openclaw-agent-routing-matrix-v1.md
- docs/implementation-drafts/openclaw-prompt-io-contracts-v1.md
- docs/implementation-drafts/client-brief-output-template-v1.md

本轮目标：
1. 先把 discussion schema、validator、summarizer、finalizer 做成可独立复用 helper。
2. 先不强接主链，只提供最小可调用接口和说明。
3. 文档必须和当前实现一致，不写未来空想接口。

约束：
- 不要大改 candidate_case.py 现有行为。
- 不要把文档写成泛泛原则，必须贴当前字段和当前状态机。
- 若只补 helper，至少做 py_compile 或最小单测。

交付要求：
- 列出新增文件
- 说明 helper 以后怎么接入现有 discussion 主链
- 说明文档与当前实现的对应关系
```

### 9.10 第三批并行任务包

#### Main 线第三批

- Main-P5：统一盘后 review board
  - 文件：
    - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
    - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
    - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
  - 目标：
    - 把当前已经分散存在的：
      - 参数治理高优先级待办
      - tail-market review 摘要
      - discussion finalize / execution readiness
      汇总成单一 review board 视图
  - 验收：
    - 至少有一个稳定 API 可直接看盘后重点事项
    - scheduler 盘后审计可带出 review board 摘要
  - 当前状态：`DONE`
  - 当前结果：
    - 已新增 `GET /system/reports/review-board`
    - `postclose_master` 已补 `latest_review_board`
    - scheduler 参数治理巡检审计已补 `review_board_summary`
    - `PYTHONPATH=src pytest tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_endpoints_expose_latest_and_history tests/test_system_governance.py::SystemGovernanceTests::test_parameter_hint_inspection_lists_pending_high_risk_and_window_alerts tests/test_system_governance.py::SystemGovernanceTests::test_post_rollback_tracking_marks_recovery_confirmed tests/test_phase1.py::TestScheduler::test_scheduler_tasks tests/test_phase1.py::TestScheduler::test_build_postclose_review_board_summary_aggregates_sections -q`
    - `6 passed`
- Main-P6：主链接入点总回归补全
  - 文件：
    - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - 目标：
    - 把 Main-P1 ~ Main-P5 串成更稳定的代表性回归组
  - 验收：
    - 覆盖 `runtime / tail_market / governance / discussion-review-board`
  - 当前状态：`DONE`
  - 当前结果：
    - `PYTHONPATH=src pytest tests/test_phase_strategy_runtime_api.py tests/test_discussion_helpers.py tests/test_phase1.py::TestScheduler::test_scheduler_tasks tests/test_phase1.py::TestScheduler::test_build_postclose_review_board_summary_aggregates_sections tests/test_phase1.py::TestTailMarketBehaviorAwareExit::test_tail_market_scan_uses_1m_microstructure_fast_exit tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_endpoints_expose_latest_and_history tests/test_system_governance.py::SystemGovernanceTests::test_parameter_hint_inspection_lists_pending_high_risk_and_window_alerts tests/test_system_governance.py::SystemGovernanceTests::test_post_rollback_tracking_marks_recovery_confirmed -q`
    - `18 passed`

#### A 线第三批

- A-P6：exit monitor 信号聚合与摘要
  - 文件：
    - `src/ashare_system/monitor/exit_monitor.py`
    - `src/ashare_system/monitor/market_watcher.py`
    - `tests/test_phase_exit_monitor.py`
  - 目标：
    - 在现有 `last_exit_signals` 基础上，补统一摘要输出
    - 至少聚合：
      - `by_symbol`
      - `by_reason`
      - `by_severity`
      - `summary_lines`
  - 验收：
    - `market_watcher` 能直接返回最近一次 exit signals summary
    - 不改 `check_once()` 返回类型
  - 当前状态：`DONE`
- A-P7：exit monitor 与 tail-market 标签对齐
  - 文件：
    - `src/ashare_system/monitor/exit_monitor.py`
    - `tests/test_phase_exit_monitor.py`
  - 目标：
    - 尽量把 exit monitor 的 reason/tag 命名与现有 `tail_market` / `exit_engine` 审计标签对齐
  - 验收：
    - 不新造一套命名体系
    - 测试锁定关键 tag/reason
  - 当前状态：`DONE`

#### B 线第三批

- B-P6：离线回测弱点分桶与对比视图
  - 文件：
    - `src/ashare_system/backtest/attribution.py`
    - `tests/test_phase_playbook_backtest.py`
  - 目标：
    - 在 `overview / export_payload` 基础上补：
      - `weakest_buckets`
      - `compare_views`
    - 支持按 `playbook / regime / exit_reason` 做最小对比
  - 验收：
    - 返回结构可直接用于 review 或后续 API 接入
  - 当前状态：`DONE`
- B-P7：离线回测导出契约文档
  - 文件：
    - `docs/implementation-drafts/` 下新增或补充回测导出契约文档
    - 必要时同步 [执行记录-codex.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/执行记录-codex.md)
  - 目标：
    - 固化 `offline_backtest attribution` 的导出字段与边界
  - 验收：
    - 明确它如何被后续 system API / report 层消费
  - 当前状态：`DONE`

#### C 线第三批

- C-P6：OpenClaw opinion ingress adapter
  - 文件：
    - `src/ashare_system/discussion/` 下新增 opinion ingress/adapter helper
    - `tests/test_discussion_helpers.py`
  - 目标：
    - 把外部 agent 返回的 opinion payload 规范化成现有 batch writeback 可消费结构
    - 内部复用 `validate_opinion_batch(...)`
  - 验收：
    - 不改主链状态机
    - helper 单独调用时可完成 normalize + validate
  - 当前状态：`DONE`
- C-P7：discussion helper 接入说明收口
  - 文件：
    - `docs/implementation-drafts/openclaw-agent-routing-matrix-v1.md`
    - `docs/implementation-drafts/openclaw-prompt-io-contracts-v1.md`
    - `docs/implementation-drafts/discussion-state-output-matrix.md`
  - 目标：
    - 把 “validator -> summarizer -> finalizer -> writeback ingress” 的真实接入顺序写清楚
  - 验收：
    - 文档能够直接指导 Main 后续把 validator 接到写回入口
  - 当前状态：`DONE`

### 9.11 第三批可直接发给线程的提示词

#### 发给 Main

```text
你负责 Main 主线，只能改以下高冲突主链文件：
- src/ashare_system/apps/system_api.py
- src/ashare_system/scheduler.py
- tests/test_system_governance.py
- tests/test_phase1.py

本轮目标：
1. 把参数治理高优先级待办、tail-market review 摘要、discussion finalize/execution readiness 汇总成一个统一盘后 review board。
2. 给 review board 补稳定 API，并把摘要带进 scheduler 盘后审计。
3. 补一组更明确的代表性回归，覆盖 runtime / tail_market / governance / discussion-review-board。

约束：
- 不要重构 discussion 主状态机。
- 不要改 A/B/C 线负责的新模块内部实现。
- 继续保持 system_api / scheduler 向后兼容。

交付要求：
- 列出改动文件
- 说明 review board 输出什么
- 给出最小回归结果
- 说明还没覆盖的缺口
```

#### 发给 A 线

```text
你负责 A 线：因子与退出监控。只改 monitor 相关文件和自己的测试，不要碰 scheduler.py、system_api.py、candidate_case.py。

本轮目标文件：
- src/ashare_system/monitor/exit_monitor.py
- src/ashare_system/monitor/market_watcher.py
- tests/test_phase_exit_monitor.py

本轮目标：
1. 在现有 last_exit_signals 基础上，补 exit signals summary，至少输出 by_symbol / by_reason / by_severity / summary_lines。
2. 尽量把 exit monitor 的 reason/tag 命名与 tail_market / exit_engine 现有标签对齐。
3. 保持 check_once() 返回类型不变，不下单，不改 scheduler 行为。

交付要求：
- 列出修改文件
- 说明新增 summary 输出结构
- 跑相关 pytest
```

#### 发给 B 线

```text
你负责 B 线：回测与归因层。只改 backtest 相关文件、自己的测试和必要文档，不要碰 system_api.py、scheduler.py、learning/attribution.py。

本轮目标文件：
- src/ashare_system/backtest/attribution.py
- tests/test_phase_playbook_backtest.py
- docs/implementation-drafts/ 下必要文档

本轮目标：
1. 在现有 overview / export_payload 基础上补 weakest_buckets 和 compare_views。
2. 支持按 playbook / regime / exit_reason 做最小对比，结果可直接给 review/API 用。
3. 补导出契约说明，写清 offline_backtest attribution 的边界和消费方式。

交付要求：
- 列出修改文件
- 说明新增导出结构
- 跑相关 pytest
```

#### 发给 C 线

```text
你负责 C 线：discussion/OpenClaw 工程化。尽量只改 discussion helper、自己的测试和文档，不要碰 scheduler.py、system_api.py、candidate_case.py 主链。

本轮目标文件：
- src/ashare_system/discussion/ 下新增 opinion ingress/adapter helper
- tests/test_discussion_helpers.py
- docs/implementation-drafts/openclaw-agent-routing-matrix-v1.md
- docs/implementation-drafts/openclaw-prompt-io-contracts-v1.md
- docs/implementation-drafts/discussion-state-output-matrix.md

本轮目标：
1. 把外部 agent opinion payload 规范化成 batch writeback 可消费结构。
2. 内部复用 validate_opinion_batch(...)，先做 normalize + validate helper，不改主状态机。
3. 把 validator -> summarizer -> finalizer -> writeback ingress 的真实接入顺序写清楚。

交付要求：
- 列出新增/修改文件
- 说明 helper 以后如何接进写回入口
- 跑 discussion helper 相关测试
```

### 9.12 第四批并行任务包

#### Main 线第四批

- Main-P7：把 opinion_ingress 接进 discussion 写回入口
  - 文件：
    - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - 目标：
    - 在不改 discussion 主状态机的前提下，把外部 opinion payload 的写回入口统一改成：
      - `OpenClaw payload -> opinion_ingress -> record_opinions_batch`
  - 验收：
    - 写回入口不再自己猜字段
    - 与现有 batch opinion 写回兼容
  - 当前状态：`DONE`
  - 当前结果：
    - 已新增 `POST /system/discussions/opinions/openclaw-ingress`
    - `record_batch_opinions` 与 OpenClaw ingress 已共用 `_persist_discussion_writeback_items(...)`
    - ingress 入口会按 `trade_date` 自动建立 `symbol -> case_id` 映射，再调用 `adapt_openclaw_opinion_payload(...)`
- Main-P8：把 offline backtest attribution 暴露成主链可消费只读接口
  - 文件：
    - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - 目标：
    - 给 system API 或 report 层补一个只读入口
    - 直接返回：
      - `overview`
      - `weakest_buckets`
      - `compare_views`
  - 验收：
    - 不混入 learning attribution 语义
    - 返回结构可直接给盘后 review 使用
  - 当前状态：`DONE`
  - 当前结果：
    - 已新增 `GET /system/reports/offline-backtest-attribution`
    - 读取优先级为 `serving/latest_offline_backtest_attribution.json -> meeting_state_store["latest_offline_backtest_attribution"]`
    - 返回 `overview / weakest_buckets / compare_views / selected_weakest_bucket / selected_compare_view / summary_lines`
- Main-P9：第四批集成回归
  - 文件：
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
    - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
    - 必要时 `tests/test_discussion_helpers.py`
  - 目标：
    - 把 `review board + opinion_ingress + offline backtest API` 串成主链代表性回归
  - 验收：
    - 覆盖 `governance / discussion writeback / review-board / offline-backtest`
  - 当前状态：`DONE`
  - 当前结果：
    - 已新增：
      - `test_openclaw_opinion_ingress_endpoint_writes_batch`
      - `test_offline_backtest_attribution_report_endpoint_reads_latest_export`
    - 定向回归：
      - `PYTHONPATH=src pytest tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_opinion_ingress_endpoint_writes_batch tests/test_system_governance.py::SystemGovernanceTests::test_offline_backtest_attribution_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_endpoints_expose_latest_and_history -q`
      - `4 passed`

#### A 线第四批

- A-P8：把 exit monitor summary 做成可被主链稳定读取的快照
  - 文件：
    - `src/ashare_system/monitor/market_watcher.py`
    - `tests/test_phase_exit_monitor.py`
  - 目标：
    - 不只是存在 watcher 内存里
    - 要有明确、稳定、可重复读取的 latest summary 结构
  - 验收：
    - Main 后续可直接消费，不需要再猜字段
- A-P9：补 exit monitor summary 契约说明
  - 文件：
    - `docs/implementation-drafts/` 下新增或补充 monitor summary 契约文档
    - 必要时同步 `tests/test_phase_exit_monitor.py`
  - 目标：
    - 写清 `by_symbol / by_reason / by_severity / by_tag / summary_lines / items` 的消费约定
  - 验收：
    - review board 或 report 层能直接复用

#### B 线第四批

- B-P8：补 weakest_buckets / compare_views 的过滤与排序能力
  - 文件：
    - `src/ashare_system/backtest/attribution.py`
    - `tests/test_phase_playbook_backtest.py`
  - 目标：
    - 支持按 `playbook / regime / exit_reason` 精确取最弱桶和对比视图
  - 验收：
    - Main 接 API 时不需要再二次筛选整包数据
- B-P9：补 API-ready payload 示例
  - 文件：
    - `docs/implementation-drafts/offline-backtest-attribution-export-contract.md`
    - 必要时同步 `tests/test_phase_playbook_backtest.py`
  - 目标：
    - 在契约文档里给出最小 API-ready 示例 payload
  - 验收：
    - Main 接 system API 时可直接照文档出参落地

#### C 线第四批

- C-P8：补 opinion_ingress 的坏输入/脏输入覆盖
  - 文件：
    - `tests/test_discussion_helpers.py`
    - 必要时 `src/ashare_system/discussion/opinion_ingress.py`
  - 目标：
    - 把以下场景测透：
      - 缺 round
      - 缺 agent_id
      - symbol -> case_id 映射失败
      - 嵌套容器里 opinion 结构不完整
  - 验收：
    - Main 接写回入口时不需要额外补防守逻辑
- C-P9：补 writeback ingress 示例文档
  - 文件：
    - `docs/implementation-drafts/openclaw-prompt-io-contracts-v1.md`
    - `docs/implementation-drafts/openclaw-agent-routing-matrix-v1.md`
  - 目标：
    - 给出从原始 OpenClaw payload 到 writeback batch 的最小示例
  - 验收：
    - Main 接系统 API 时直接可照着接

### 9.13 第四批可直接发给线程的提示词

#### 发给 Main

```text
你负责 Main 主线，只能改以下高冲突主链文件：
- src/ashare_system/apps/system_api.py
- tests/test_system_governance.py
- tests/test_phase1.py

本轮目标：
1. 把 discussion 写回入口改成走 opinion_ingress，不再自己猜 OpenClaw payload 字段。
2. 给 offline backtest attribution 补一个主链只读接口，返回 overview / weakest_buckets / compare_views。
3. 补一组代表性回归，覆盖 discussion writeback、review board、offline-backtest API。

约束：
- 不要重构 discussion 主状态机。
- 不要改 learning attribution 主线语义。
- 尽量直接消费 C/B 线现有产物，不再重复造一层。

交付要求：
- 列出改动文件
- 说明新接口/新写回入口的输入输出
- 给出最小回归结果
- 说明剩余缺口
```

#### 发给 A 线

```text
你负责 A 线：monitor 侧稳定输出。只改 monitor 相关文件、自己的测试和必要文档，不要碰 scheduler.py、system_api.py、candidate_case.py。

本轮目标文件：
- src/ashare_system/monitor/market_watcher.py
- tests/test_phase_exit_monitor.py
- docs/implementation-drafts/ 下必要文档

本轮目标：
1. 把 exit monitor summary 做成可被主链稳定读取的 latest snapshot。
2. 写清 summary 字段契约，尤其是 by_symbol / by_reason / by_severity / by_tag / summary_lines / items。

交付要求：
- 列出修改文件
- 说明 latest snapshot 怎么取
- 跑相关 pytest
```

#### 发给 B 线

```text
你负责 B 线：离线回测导出结构继续收口。只改 backtest attribution、自己的测试和契约文档，不要碰 system_api.py、scheduler.py、learning/attribution.py。

本轮目标文件：
- src/ashare_system/backtest/attribution.py
- tests/test_phase_playbook_backtest.py
- docs/implementation-drafts/offline-backtest-attribution-export-contract.md

本轮目标：
1. 给 weakest_buckets / compare_views 补过滤与排序能力。
2. 在契约文档里给出 API-ready payload 示例。

交付要求：
- 列出修改文件
- 说明新增过滤/排序能力
- 跑相关 pytest
```

#### 发给 C 线

```text
你负责 C 线：discussion ingress 稳定性收口。只改 opinion_ingress、helper 测试和实现文档，不要碰 scheduler.py、system_api.py、candidate_case.py 主链。

本轮目标文件：
- src/ashare_system/discussion/opinion_ingress.py
- tests/test_discussion_helpers.py
- docs/implementation-drafts/openclaw-prompt-io-contracts-v1.md
- docs/implementation-drafts/openclaw-agent-routing-matrix-v1.md

本轮目标：
1. 把 opinion_ingress 的坏输入/脏输入场景测透。
2. 补从原始 OpenClaw payload 到 writeback batch 的最小示例。

交付要求：
- 列出修改文件
- 说明哪些坏输入现在已被覆盖
- 跑 discussion helper 相关测试
```

### 9.14 第五批并行任务包

#### Main 线第五批

- Main-P10：把 offline backtest attribution 并入盘后总览主链
  - 文件：
    - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - 目标：
    - 把 B 线已经产出的离线回测 attribution 不只保留单独只读接口，还真正并进 `review-board / postclose-master`
  - 验收：
    - `review-board` 有 `offline_backtest` section
    - `postclose-master` 带出 `latest_offline_backtest_attribution`
  - 当前状态：`DONE`
  - 当前结果：
    - `review-board` 已新增 `offline_backtest` section、`offline_backtest_trade_count`
    - `postclose-master` 已带出 `latest_offline_backtest_attribution`
    - `PYTHONPATH=src pytest tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_offline_backtest_attribution_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_endpoints_expose_latest_and_history -q`
    - `3 passed`
- Main-P11：把 exit monitor latest snapshot 接进主链只读总览
  - 文件：
    - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - 依赖：
    - A 线第 5 批把 snapshot 稳定持久化到 monitor state 层
  - 目标：
    - 给主链一个稳定入口读取 `latest_exit_snapshot`
    - 并把它接到 `review-board / postclose-master`
  - 验收：
    - 不影响现有 `check_once() -> list[AlertEvent]` 兼容
    - 主链能看到 `signal_count / by_reason / summary_lines`
  - 当前状态：`IN_PROGRESS`
- Main-P12：第五批代表性回归
  - 文件：
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
    - 必要时 [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
  - 目标：
    - 把 `review-board + postclose-master + offline-backtest + exit-monitor snapshot` 串成一组主链回归
  - 验收：
    - 覆盖 `governance / tail-market / discussion / offline-backtest / exit-monitor`
  - 当前状态：`PENDING`

#### A 线第五批

- A-P10：把 latest exit snapshot 持久化到 monitor state 层
  - 文件：
    - `src/ashare_system/monitor/persistence.py`
    - `src/ashare_system/monitor/market_watcher.py`
    - `tests/test_phase_exit_monitor.py`
  - 目标：
    - 让 `MarketWatcher` 最近一次 exit snapshot 不只停留在内存态，而是进入 monitor state，供 Main 后续读取
  - 验收：
    - 不改 `check_once()` 返回类型
    - snapshot 字段契约稳定
  - 当前状态：`DISPATCHED`
- A-P11：补 monitor state / latest exit snapshot 契约说明
  - 文件：
    - `docs/implementation-drafts/` 下必要文档
  - 当前状态：`DISPATCHED`

#### B 线第五批

- B-P10：把 by_playbook / by_regime / by_exit_reason 指标补到 metrics 层
  - 文件：
    - `src/ashare_system/backtest/metrics.py`
    - `tests/test_phase_playbook_backtest.py`
  - 目标：
    - 对齐设计里的 H-3，让 metrics 层也能直接给出按战法/市场状态/退出原因拆分的绩效指标
  - 验收：
    - 保持 `offline_backtest` 语义
    - 不碰 learning attribution
  - 当前状态：`DISPATCHED`
- B-P11：如有必要补一份 metrics/export 契约说明
  - 文件：
    - `docs/implementation-drafts/` 下必要文档
  - 当前状态：`DISPATCHED`

#### C 线第五批

- C-P10：把 opinion_ingress 更进一步下沉到 discussion service 层
  - 文件：
    - `src/ashare_system/discussion/discussion_service.py`
    - `tests/test_discussion_helpers.py`
  - 目标：
    - 提供 service 级的 OpenClaw opinion 适配/写回 helper，避免未来所有入口都手工拼映射和写回
  - 约束：
    - 不改 `state_machine.py`
    - 不改 `system_api.py`
    - 不改 `candidate_case.py`
  - 当前状态：`DISPATCHED`
- C-P11：补 service 级接入说明
  - 文件：
    - `docs/implementation-drafts/` 下必要文档
  - 当前状态：`DISPATCHED`

### 9.15 第五批可直接发给线程的提示词

#### 发给 A 线

```text
你负责 A 线：monitor state 侧稳定持久化。只改 monitor 相关文件、自己的测试和必要文档，不要碰 scheduler.py、system_api.py。

本轮目标文件：
- src/ashare_system/monitor/persistence.py
- src/ashare_system/monitor/market_watcher.py
- tests/test_phase_exit_monitor.py
- docs/implementation-drafts/ 下必要文档

本轮目标：
1. 把 market_watcher 的 latest exit snapshot 持久化到 monitor state 层。
2. 保持 check_once() 返回 list[AlertEvent] 不变。
3. 写清主链后续怎么读到这个 snapshot。

交付要求：
- 列出修改文件
- 说明新增持久化入口/读取入口
- 跑相关 pytest
```

#### 发给 B 线

```text
你负责 B 线：离线回测 metrics 层补齐。只改 backtest/metrics.py、自己的测试和必要文档，不要碰 system_api.py、learning attribution 主线。

本轮目标文件：
- src/ashare_system/backtest/metrics.py
- tests/test_phase_playbook_backtest.py
- docs/implementation-drafts/ 下必要文档

本轮目标：
1. 把 by_playbook / by_regime / by_exit_reason 指标补进 metrics 层。
2. 保持 offline_backtest 语义，不要混入线上事实归因。

交付要求：
- 列出修改文件
- 说明新增指标/返回结构
- 跑相关 pytest
```

#### 发给 C 线

```text
你负责 C 线：discussion service 层继续小步接线。只改 discussion_service.py、自己的测试和必要文档，不要碰 state_machine.py、system_api.py、candidate_case.py。

本轮目标文件：
- src/ashare_system/discussion/discussion_service.py
- tests/test_discussion_helpers.py
- docs/implementation-drafts/ 下必要文档

本轮目标：
1. 把 opinion_ingress 下沉成 service 级 helper。
2. 提供最小的 OpenClaw opinion 适配/写回 service 入口，供 Main 后续消费。

交付要求：
- 列出修改文件
- 说明 service 新入口
- 跑相关 pytest
```

### 9.16 第五批结果回执

- Main-P11：`DONE`
  - `review-board` / `postclose-master` 已接入 `latest_exit_snapshot`
  - `exit_monitor` section 已进入主链总览
- Main-P12：`DONE`
  - 主线回归：
    - `PYTHONPATH=src pytest tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_opinion_ingress_endpoint_writes_batch tests/test_system_governance.py::SystemGovernanceTests::test_offline_backtest_attribution_report_endpoint_reads_latest_export tests/test_discussion_helpers.py -q`
    - `16 passed`
- A-P10 / A-P11：`DONE`
  - `MonitorStateService` 已持久化 `latest_exit_snapshot / exit_snapshot_history`
  - 契约文档已补
- B-P10 / B-P11：`DONE`
  - `offline_backtest metrics` 已在 `metrics.py` 成型并补契约文档
- C-P10 / C-P11：`DONE`
  - `DiscussionCycleService.write_openclaw_opinions(...)` 已成为 service 级正式入口
  - 文档已改为当前真实接法

### 9.17 第六批并行任务包

#### Main 线第六批

- Main-P13：补 OpenClaw preview 只读入口
  - 文件：
    - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - 当前状态：`DONE`
  - 当前结果：
    - 已新增 `POST /system/discussions/opinions/openclaw-preview`
- Main-P14：补 offline backtest metrics 主链只读入口
  - 文件：
    - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - 当前状态：`DONE`
  - 当前结果：
    - 已新增 `GET /system/reports/offline-backtest-metrics`
    - `postclose-master` 已带 `latest_offline_backtest_metrics`
- Main-P15：第六批代表性回归
  - 当前状态：`DONE`
  - 当前结果：
    - `PYTHONPATH=src pytest tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_opinion_ingress_endpoint_writes_batch tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_opinion_preview_endpoint_normalizes_without_writeback tests/test_system_governance.py::SystemGovernanceTests::test_offline_backtest_attribution_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_offline_backtest_metrics_report_endpoint_reads_latest_export tests/test_discussion_helpers.py -q`
    - `19 passed`

#### A 线第六批

- A-P12：exit snapshot trend summary
  - 当前状态：`DONE`
  - 当前结果：
    - `MonitorStateService.get_state()` 已带 `exit_snapshot_trend_summary`

#### B 线第六批

- B-P12：playbook runner 桥接 offline_backtest metrics
  - 当前状态：`DONE`
  - 当前结果：
    - `PlaybookBacktestRunResult` 已带 `metrics / metrics_export`

#### C 线第六批

- C-P12：writeback 后 summary 收口
  - 当前状态：`DONE`
  - 当前结果：
    - `write_openclaw_opinions(...)` 已带 `refresh_summary / refreshed_summary_snapshot / touched_case_summaries`

### 9.18 第七批主线起步

- Main-P16：把 offline_backtest metrics 与 exit trend 摘要并入 review-board
  - 文件：
    - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - 当前状态：`DONE`
  - 当前结果：
    - `review-board` 已新增 `offline_backtest_metrics` section
    - `exit_monitor.trend_summary` 已进入 `review-board`
    - 定向回归：
      - `PYTHONPATH=src pytest tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_offline_backtest_metrics_report_endpoint_reads_latest_export -q`
      - `2 passed`

### 9.19 第七批并行任务包

#### Main 线第七批

- Main-P17：固化 Linux/OpenClaw + Windows VM/QMT 的正式部署边界
  - 文件：
    - [technical-manual.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/technical-manual.md)
    - [openclaw-linux-qmt-deployment-v1.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/openclaw-linux-qmt-deployment-v1.md)
    - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)
  - 目标：
    - 把未来生产态从“开发机 WSL/Windows 联调”中分离出来，正式定义：
      - Linux 控制面
      - Windows 执行面
      - 单一执行写口
      - 自我进化边界
  - 当前状态：`DONE`
- Main-P18：定义第七批 A/B/C 的落地方向
  - 当前状态：`DONE`
  - 当前结果：
    - A 线聚焦执行网关健康快照
    - B 线聚焦 offline self-improvement proposal/export
    - C 线聚焦 OpenClaw replay/proposal packet

#### A 线第七批

- A-P13：把 Windows Execution Gateway / QMT VM 健康快照纳入 monitor state
  - 文件：
    - `src/ashare_system/monitor/persistence.py`
    - `tests/test_phase_exit_monitor.py`
    - 必要时 `docs/implementation-drafts/`
  - 目标：
    - 除 exit snapshot 外，再补一层执行面健康快照：
      - gateway 在线状态
      - QMT 连接状态
      - account_id / session freshness
      - 最近一次 poll / receipt 时间
  - 验收：
    - 主链后续可直接把执行面健康接进 `review-board / postclose-master`

#### B 线第七批

- B-P13：生成 offline self-improvement proposal/export packet
  - 文件：
    - `src/ashare_system/backtest/playbook_runner.py`
    - `src/ashare_system/backtest/attribution.py`
    - `src/ashare_system/backtest/metrics.py`
    - `tests/test_phase_playbook_backtest.py`
    - 必要时 `docs/implementation-drafts/`
  - 目标：
    - 基于 attribution + metrics 产出“可进化建议包”，供 Linux 控制面研究闭环消费
    - 输出应明确：
      - 最弱分桶
      - 弱 regime
      - 候选 playbook 调整方向
      - 仅 offline，不可直接推 live
  - 验收：
    - Main/API 后续不必自己拼 proposal 结构

#### C 线第七批

- C-P13：补 OpenClaw replay / proposal packet helper
  - 文件：
    - `src/ashare_system/discussion/discussion_service.py`
    - `tests/test_discussion_helpers.py`
    - 必要时 `docs/implementation-drafts/`
  - 目标：
    - 在 preview / writeback / summary 之上，再给 OpenClaw 一层可回放、可审计、可反思的 packet
    - 用于：
      - 盘后 replay
      - 自我进化 proposal 输入
      - 不直接触发 live 执行
  - 验收：
    - 形成稳定 packet，不要求改状态机

### 9.20 第七批可直接发给线程的提示词

#### 发给 A 线

```text
你负责 A 线：执行面健康快照。只改 monitor/persistence、自己的测试和必要文档，不要碰 system_api.py、scheduler.py。

本轮目标文件：
- src/ashare_system/monitor/persistence.py
- tests/test_phase_exit_monitor.py
- docs/implementation-drafts/ 下必要文档

本轮目标：
1. 在现有 exit snapshot 之外，再补 Windows Execution Gateway / QMT VM 的健康快照与趋势摘要。
2. 输出要让 Main 后续可直接接入 review-board / postclose-master。

交付要求：
- 列出修改文件
- 说明新增 health snapshot / trend summary 结构
- 跑相关 pytest
```

#### 发给 B 线

```text
你负责 B 线：offline self-improvement proposal/export。只改 backtest 相关文件、自己的测试和必要文档，不要碰 learning attribution 和 system_api.py。

本轮目标文件：
- src/ashare_system/backtest/playbook_runner.py
- src/ashare_system/backtest/attribution.py
- src/ashare_system/backtest/metrics.py
- tests/test_phase_playbook_backtest.py
- docs/implementation-drafts/ 下必要文档

本轮目标：
1. 基于 attribution + metrics 产出可直接供 Main/OpenClaw 使用的 offline proposal/export packet。
2. 明确它只服务于离线研究与自我进化，不可直接推 live。

交付要求：
- 列出修改文件
- 说明 proposal/export 结构
- 跑相关 pytest
```

#### 发给 C 线

```text
你负责 C 线：OpenClaw replay / proposal packet helper。只改 discussion_service.py、自己的测试和必要文档，不要碰 state_machine.py、system_api.py、candidate_case.py。

本轮目标文件：
- src/ashare_system/discussion/discussion_service.py
- tests/test_discussion_helpers.py
- docs/implementation-drafts/ 下必要文档

本轮目标：
1. 在 preview / writeback / summary 基础上，补一层 replay/proposal packet helper。
2. 该 packet 供盘后 replay 和自我进化研究使用，不直接触发 live。

交付要求：
- 列出修改文件
- 说明 packet 结构与用途
- 跑相关 pytest
```

### 9.21 第七批主线接线完成

#### Main 线第七批收口

- Main-P19：把第七批 A/B/C 产物接入 `system_api` 主线
  - 文件：
    - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - 当前状态：`DONE`
  - 当前结果：
    - `review-board` 已新增 `execution_bridge_health` section，并补进：
      - `overall_status`
      - `attention_count`
      - `trend_summary`
    - `postclose-master` 已新增：
      - `latest_execution_bridge_health`
      - `latest_execution_bridge_health_trend_summary`
      - `latest_offline_self_improvement`
    - `system_api` 已新增只读接口：
      - `POST /system/discussions/opinions/openclaw-replay-packet`
      - `POST /system/discussions/opinions/openclaw-proposal-packet`
      - `GET /system/reports/offline-self-improvement`
    - `offline-self-improvement` 读取侧已兼容：
      - `self_improvement_export`
      - `export_packet`
      - `latest_offline_self_improvement_export.json`
      - `latest_offline_self_improvement.json`
    - replay / proposal packet 在主线仍固定：
      - `offline_only = true`
      - `live_trigger = false`
    - Linux/OpenClaw 与 Windows VM/QMT 的生产边界未被放宽：
      - Agent 仍只做研究、审议、复盘
      - 执行写口仍应收口到 Windows Execution Gateway
  - 定向回归：
    - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_replay_packet_endpoint_builds_offline_packet_without_writeback tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_proposal_packet_endpoint_builds_research_only_packet tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_opinion_ingress_endpoint_writes_batch tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_opinion_preview_endpoint_normalizes_without_writeback tests/test_system_governance.py::SystemGovernanceTests::test_offline_backtest_attribution_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_offline_backtest_metrics_report_endpoint_reads_latest_export -q`
    - `8 passed`

### 9.22 第八批主线与并行任务包

#### Main 线第八批

- Main-P20：补 Windows Execution Gateway -> Linux 主控 的 execution bridge health 上报入口
  - 文件：
    - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - 当前状态：`DONE`
  - 当前结果：
    - 已新增 `POST /system/monitor/execution-bridge-health`
    - Windows 侧可主动上报 `gateway_online / qmt_connected / component_health`
    - Linux 主控收到后会直接落到 `monitor_state_service.save_execution_bridge_health(...)`
    - `review-board / postclose-master / /monitor/state` 会读取同一份持久化快照
  - 定向回归：
    - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_execution_bridge_health_ingress_endpoint_persists_linux_windows_bridge_snapshot tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_replay_packet_endpoint_builds_offline_packet_without_writeback tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_proposal_packet_endpoint_builds_research_only_packet -q`
    - `5 passed`

- Main-P22：把 B 线 serving-ready self-improvement helper 接入主线消费
  - 文件：
    - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - 当前状态：`DONE`
  - 当前结果：
    - `_normalize_offline_self_improvement_payload(...)` 已优先识别 `serving_ready_latest_export`
    - `GET /system/reports/offline-self-improvement` 已透出：
      - `serving_ready`
      - `artifact_name`
    - 治理测试已改为直接调用：
      - `PlaybookBacktestRunner.write_latest_offline_self_improvement_export(...)`
    - Main 不再需要手写 `latest_offline_self_improvement_export.json`
  - 定向回归：
    - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion -q`
    - `2 passed`

- Main-P23：补离线产物 archive/persist 主线入口
  - 文件：
    - [archive.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/data/archive.py)
    - [serving.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/data/serving.py)
    - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
    - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - 当前状态：`DONE`
  - 当前结果：
    - `DataArchiveStore` 已新增：
      - `persist_offline_self_improvement_export(...)`
      - `persist_openclaw_packet(...)`
    - `ServingStore` 已新增：
      - `get_latest_offline_self_improvement_export()`
      - `get_latest_openclaw_packet(packet_type)`
    - `system_api` 已新增：
      - `POST /system/reports/offline-self-improvement/persist`
      - `POST /system/discussions/opinions/openclaw-replay-packet/archive`
      - `POST /system/discussions/opinions/openclaw-proposal-packet/archive`
    - `postclose-master` 已新增：
      - `latest_openclaw_replay_packet`
      - `latest_openclaw_proposal_packet`
    - 当前 Linux 主控已具备：
      - 离线 self-improvement export 标准落盘
      - OpenClaw replay/proposal packet 标准落盘
      - latest serving 统一读取
  - 定向回归：
    - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_packet_archive_endpoints_persist_latest_packets tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_persist_endpoint_accepts_runner_result_payload tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_replay_packet_endpoint_builds_offline_packet_without_writeback tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_proposal_packet_endpoint_builds_research_only_packet -q`
    - `5 passed`

### 9.23 第九批主线与并行任务包

#### Main 线第九批

- Main-P24：开第九批 A/B/C 任务，并围绕“外部客户端如何稳定调用 Linux 主控”继续收口
  - 当前状态：`DONE`
  - 当前目标：
    - 把 execution bridge、offline self-improvement、OpenClaw packet 三类产物从“仓内 helper/接口”进一步收口成外部可稳定调用的契约与归档清单

#### A 线第九批

- A-P15：补 execution bridge ingress payload helper
  - 文件：
    - `src/ashare_system/monitor/persistence.py`
    - `tests/test_phase_exit_monitor.py`
    - `docs/implementation-drafts/monitor-execution-bridge-health-contract.md`
  - 目标：
    - 在 persistence/contract 层补一个稳定的 ingress payload helper 或等价结构，供 Windows Execution Gateway 直接构造上报包
    - 明确：
      - health 顶层必需字段
      - remote source 标识字段
      - 空值/缺省约定
      - 与 Linux 主控 `POST /system/monitor/execution-bridge-health` 的对应关系
  - 边界：
    - 不改 `system_api.py`
    - 不碰调度与执行链

#### B 线第九批

- B-P15：补 offline self-improvement archive manifest/helper
  - 文件：
    - `src/ashare_system/backtest/playbook_runner.py`
    - `tests/test_phase_playbook_backtest.py`
    - `docs/implementation-drafts/offline-self-improvement-proposal-export-contract.md`
  - 目标：
    - 在 serving-ready latest export 基础上，再补 archive-ready manifest/helper
    - 至少明确：
      - artifact_name
      - relative archive path 或 manifest
      - consumers / semantics note / live guardrail 的保留策略
    - 让 Linux 主控或离线调度不需要自己再拼 archive 元数据
  - 边界：
    - 不碰 learning attribution
    - 不改 `system_api.py`

#### C 线第九批

- C-P15：补 OpenClaw packet archive manifest/helper
  - 文件：
    - `src/ashare_system/discussion/discussion_service.py`
    - `tests/test_discussion_helpers.py`
    - `docs/implementation-drafts/openclaw-replay-proposal-packet-v1.md`
  - 目标：
    - 在现有 replay/proposal/archive-ready 元数据上，再补 archive manifest/helper
    - 至少明确：
      - artifact_name
      - archive_path
      - latest alias / archive group
      - replay/proposal 双轨归档建议
    - 让 Linux/OpenClaw 后续能直接归档而不再手写文件名规则
  - 边界：
    - 继续保持 `offline_only = true`
    - 不触发 live
    - 不改 `system_api.py` / `state_machine.py` / `candidate_case.py`

- Main-P21：开第八批 A/B/C 任务
  - 当前状态：`DONE`

- Main-P22：把第九批 A/B/C helper 接入主线
  - 当前状态：`DONE`
  - 文件：
    - `src/ashare_system/apps/system_api.py`
    - `src/ashare_system/data/archive.py`
    - `tests/test_system_governance.py`
  - 收口结果：
    - execution bridge ingress 入口改为消费 `build_execution_bridge_health_ingress_payload(...)`
    - offline self-improvement persist 入口保留/补齐 `archive_ready_manifest`
    - OpenClaw replay/proposal archive 入口同时支持“原始 payload 归档”和“已生成 packet 归档”
    - `DataArchiveStore` 现已真正按 offline self-improvement / OpenClaw packet 的 manifest 落 archive 路径和 latest alias
  - 验收：
    - Linux/OpenClaw 或离线调度不再自己拼 archive path / alias 规则
    - 相关产物保持 `offline_only` / `live_trigger = false`

### 9.24 第十批主线与并行任务包

#### Main 线第十批

- Main-P25：开第十批 A/B/C 任务，并围绕“外部客户端直接拿 latest/template，不自己猜字段/路径”继续收口
  - 当前状态：`DONE`
  - 当前目标：
    - 为后续 Linux/OpenClaw 与 Windows Execution Gateway 提供更稳定的只读 latest / template / descriptor 契约
    - 保持当前硬边界不变：
      - Linux/OpenClaw 负责研究、编排、归档、review、latest serving
      - Windows Execution Gateway 负责唯一执行写口
      - self-improvement / replay / proposal 继续只服务离线研究
  - 收口结果：
    - 新增 `GET /system/monitor/execution-bridge-health/template`
    - `GET /system/reports/offline-self-improvement` 现在直接透出 `latest_descriptor` 与 `archive_ref`
    - 新增 `GET /system/reports/offline-self-improvement-descriptor`
    - 新增 `GET /system/reports/openclaw-replay-packet`
    - 新增 `GET /system/reports/openclaw-proposal-packet`
    - `postclose-master` 已新增：
      - `latest_offline_self_improvement_descriptor`
      - `latest_openclaw_replay_descriptor`
      - `latest_openclaw_proposal_descriptor`
  - 验收：
    - Windows Gateway 可直接抄 template
    - Linux/OpenClaw 可直接拿 latest descriptor/report，不再自己拼 serving/archive 引用
    - 相关产物继续保持 `offline_only` / `live_trigger = false`

### 9.25 第十一批主线任务包

#### Main 线第十一批

- Main-P26：补 serving latest index 与 deployment bootstrap contracts 聚合只读口
  - 当前状态：`DONE`
  - 文件：
    - `src/ashare_system/apps/system_api.py`
    - `tests/test_system_governance.py`
  - 收口结果：
    - 新增 `GET /system/reports/serving-latest-index`
    - 新增 `GET /system/deployment/bootstrap-contracts`
    - `serving latest index` 聚合：
      - execution bridge health template/latest descriptor
      - offline self-improvement latest descriptor/archive ref
      - OpenClaw replay/proposal latest descriptor
    - `deployment bootstrap contracts` 聚合：
      - readiness
      - execution bridge template
      - serving latest index
      - Linux/OpenClaw 与 Windows Gateway 的部署边界与只读 report paths
  - 验收：
    - Linux/OpenClaw 启动时可一次拿全 template + latest descriptor + readiness
    - 不新增 live 写口
    - 不越过 Windows Execution Gateway 的唯一执行写口

### 9.26 第十二批主线任务包

#### Main 线第十二批

- Main-P27：补 postclose deployment handoff 聚合只读口
  - 当前状态：`DONE`
  - 文件：
    - `src/ashare_system/apps/system_api.py`
    - `tests/test_system_governance.py`
  - 收口结果：
    - 新增 `GET /system/reports/postclose-deployment-handoff`
    - 内部把 `postclose-master` 组装逻辑抽成复用 helper
    - `postclose deployment handoff` 聚合：
      - review-board
      - postclose-master
      - serving-latest-index
      - deployment-bootstrap-contracts
    - 为 Linux/OpenClaw 的盘后交接提供单次读取入口
  - 验收：
    - Linux/OpenClaw 在盘后交接时可一次拿全研究总览、latest descriptor、bootstrap contract
    - 不新增 live 写口
    - 不越过 Windows Execution Gateway 的唯一执行写口

### 9.27 第十三批主线任务包

#### Main 线第十三批

- Main-P28：补 Linux control plane startup checklist 只读口
  - 当前状态：`DONE`
  - 文件：
    - `src/ashare_system/apps/system_api.py`
    - `tests/test_system_governance.py`
  - 收口结果：
    - 新增 `GET /system/deployment/linux-control-plane-startup-checklist`
    - 聚合：
      - readiness
      - execution bridge contract/template
      - offline self-improvement descriptor
      - OpenClaw replay/proposal descriptor
      - postclose handoff
    - 将控制面启动检查压成：
      - `status`
      - `checks`
      - `summary_lines`
  - 验收：
    - Linux/OpenClaw 启动或切日时可一屏看完关键依赖与 latest 契约
    - 不新增 live 写口
    - 不越过 Windows Execution Gateway 的唯一执行写口

### 9.28 第十四批主线任务包

#### Main 线第十四批

- Main-P29：补 Windows Execution Gateway onboarding bundle 只读口
  - 当前状态：`DONE`
  - 文件：
    - `src/ashare_system/apps/system_api.py`
    - `tests/test_system_governance.py`
  - 收口结果：
    - 新增 `GET /system/deployment/windows-execution-gateway-onboarding-bundle`
    - 聚合：
      - execution bridge template
      - source value suggestions
      - latest read descriptor
      - Linux control plane 的 startup checklist / bootstrap / handoff / serving latest index 路径
    - 让 Windows 侧客户端一次拿全上报模板与 Linux 只读入口
  - 验收：
    - Windows Execution Gateway 不再自己拼 execution bridge body 规则或 Linux 侧 report 路径
    - 不新增 live 写口
    - 不越过 Windows Execution Gateway 的唯一执行写口

### 9.29 第十五批主线与并行任务包

#### Main 线第十五批

- Main-P30：开第十五批 A/B/C 任务，并围绕“部署侧 contract 文档化 + 切日交接摘要”继续收口
  - 当前状态：`DONE`
  - 当前目标：
    - 把 Linux/OpenClaw 与 Windows Execution Gateway 两侧已落地的只读契约补成统一文档和最小消费样例
    - 为后续切日/盘后交接补一个更适合 Linux 控制面的单屏交接口
  - 主线接入结果：
    - `system_api.py` 已把第十五批 contract sample 接进现有只读主线，不新增平行 helper
    - execution bridge：
      - `GET /system/monitor/execution-bridge-health/template`
      - `GET /system/deployment/windows-execution-gateway-onboarding-bundle`
      - `GET /system/deployment/bootstrap-contracts`
      现已直接透出 deployment contract sample
    - offline self-improvement：
      - `GET /system/reports/offline-self-improvement`
      - `GET /system/reports/offline-self-improvement-descriptor`
      - `GET /system/reports/serving-latest-index`
      - `GET /system/reports/postclose-master`
      - `GET /system/reports/postclose-deployment-handoff`
      现已自动补齐 `descriptor_contract_sample`
    - OpenClaw replay/proposal packet：
      - `GET /system/reports/openclaw-replay-packet`
      - `GET /system/reports/openclaw-proposal-packet`
      - `GET /system/reports/serving-latest-index`
      - `GET /system/reports/postclose-master`
      - `GET /system/reports/postclose-deployment-handoff`
      现已自动补齐 `contract_sample`
  - 验收：
    - Main 读取口能直接返回 sample，不需要 Linux/OpenClaw/Windows 客户端再猜字段
    - sample 会沿 `serving_latest_index -> postclose_master -> postclose_deployment_handoff` 自然透传
    - 不新增 live 写口，不越过 Windows Execution Gateway 的唯一执行写口

#### A 线第十五批

- A-P17：补 execution bridge deployment contract sample
  - 文件：
    - `src/ashare_system/monitor/persistence.py`
    - `tests/test_phase_exit_monitor.py`
    - `docs/implementation-drafts/monitor-execution-bridge-health-contract.md`
  - 目标：
    - 在现有 execution bridge template/latest descriptor 基础上，补统一 deployment contract sample helper
    - 至少明确：
      - Windows Gateway 最小 POST body 示例
      - Linux latest/trend 读取示例
      - primary/backup gateway 取值样例
      - 最小 HTTP/curl 风格消费样例或等价样本结构
    - 让 Windows 侧集成不需要再自己拼请求/读取示例
  - 边界：
    - 不改 `system_api.py`
    - 不碰调度、执行链、QMT 适配层

#### B 线第十五批

- B-P17：补 offline self-improvement descriptor contract sample
  - 文件：
    - `src/ashare_system/backtest/playbook_runner.py`
    - `tests/test_phase_playbook_backtest.py`
    - `docs/implementation-drafts/offline-self-improvement-proposal-export-contract.md`
  - 目标：
    - 在 latest descriptor / archive ref 基础上，补统一 contract sample helper
    - 至少明确：
      - latest descriptor 最小示例
      - archive ref 最小示例
      - serving latest / archive 的消费顺序
      - Main/OpenClaw 的推荐读取字段
    - 让 Linux 主控或 OpenClaw 集成侧直接照抄 descriptor 消费样例
  - 边界：
    - 不碰 learning attribution
    - 不改 `system_api.py`

#### C 线第十五批

- C-P17：补 OpenClaw packet descriptor contract sample
  - 文件：
    - `src/ashare_system/discussion/discussion_service.py`
    - `tests/test_discussion_helpers.py`
    - `docs/implementation-drafts/openclaw-replay-proposal-packet-v1.md`
  - 目标：
    - 在 replay/proposal latest descriptor 基础上，补统一 contract sample/helper
    - 至少明确：
      - replay/proposal descriptor 最小示例
      - archive manifest / latest descriptor 对照关系
      - Linux/OpenClaw 拉 latest、做 review 引用、做 archive 引用的最小样例
    - 让 OpenClaw 侧不再自己拼 descriptor 消费方式
  - 边界：
    - 继续保持 `offline_only = true`
    - 不触发 live
    - 不改 `system_api.py` / `state_machine.py` / `candidate_case.py`

#### A 线第十批

- A-P16：补 execution bridge health client template / latest helper
  - 文件：
    - `src/ashare_system/monitor/persistence.py`
    - `tests/test_phase_exit_monitor.py`
    - `docs/implementation-drafts/monitor-execution-bridge-health-contract.md`
  - 目标：
    - 在现有 `build_execution_bridge_health_ingress_payload(...)` 基础上，继续补“客户端可直接抄”的 template / descriptor helper
    - 至少明确：
      - request body 最小模板
      - top-level health 默认字段
      - latest 读取推荐字段
      - Linux/OpenClaw 与 Windows Gateway 的 source/deployment_role/bridge_path 取值建议
    - 让 Windows Execution Gateway 不用翻文档拼 body，让 Linux 主控不再自己猜 latest 读取键
  - 边界：
    - 不改 `system_api.py`
    - 不碰调度、执行链、QMT 适配层

#### B 线第十批

- B-P16：补 offline self-improvement latest descriptor / archive ref helper
  - 文件：
    - `src/ashare_system/backtest/playbook_runner.py`
    - `tests/test_phase_playbook_backtest.py`
    - `docs/implementation-drafts/offline-self-improvement-proposal-export-contract.md`
  - 目标：
    - 在 serving-ready export 与 archive-ready manifest 基础上，再补 latest descriptor / archive ref helper
    - 至少明确：
      - latest artifact name
      - serving path
      - archive path
      - consumers
      - research_track / semantics_note / live guardrail
    - 让 Linux 主控或离线调度能直接消费 latest descriptor，而不是自己同时拼 serving 与 archive 引用
  - 边界：
    - 不碰 learning attribution
    - 不改 `system_api.py`

#### C 线第十批

- C-P16：补 OpenClaw replay/proposal latest descriptor helper
  - 文件：
    - `src/ashare_system/discussion/discussion_service.py`
    - `tests/test_discussion_helpers.py`
    - `docs/implementation-drafts/openclaw-replay-proposal-packet-v1.md`
  - 目标：
    - 在现有 packet + archive_manifest 基础上，再补 latest descriptor/helper
    - 至少明确：
      - packet_type
      - packet_id
      - research_track
      - artifact_name
      - archive_path
      - latest_aliases
      - source_refs / archive_tags
    - 让 Linux/OpenClaw 以后可直接基于 descriptor 做 latest 拉取、归档、review 引用
  - 边界：
    - 继续保持 `offline_only = true`
    - 不触发 live
    - 不改 `system_api.py` / `state_machine.py` / `candidate_case.py`

#### A 线第八批

- A-P14：固化 execution bridge 远端上报契约
  - 文件：
    - `src/ashare_system/monitor/persistence.py`
    - `tests/test_phase_exit_monitor.py`
    - `docs/implementation-drafts/monitor-execution-bridge-health-contract.md`
  - 目标：
    - 在现有 health snapshot 上补远端上报必需字段：
      - `reported_at`
      - `source_id`
      - `deployment_role`
      - `bridge_path`（如 `linux_openclaw -> windows_gateway -> qmt_vm`）
    - 明确 Linux 主控消费口径与空值约定
  - 验收：
    - Main 不需要自己拼“来自哪台 Windows VM / 哪条执行桥”的识别字段

#### B 线第八批

- B-P14：把 offline self-improvement export 固化成 serving-ready 写出助手
  - 文件：
    - `src/ashare_system/backtest/playbook_runner.py`
    - `tests/test_phase_playbook_backtest.py`
    - `docs/implementation-drafts/offline-self-improvement-proposal-export-contract.md`
  - 目标：
    - 提供 `runner` 侧直接写出/返回 serving-ready latest export 的 helper
    - 避免 Main 或 Linux 控制面重复手写 `latest_offline_self_improvement_export.json`
  - 验收：
    - 产物仍明确 `live_execution_allowed = false`
    - 输出仍只服务离线研究与自我进化

#### C 线第八批

- C-P14：给 replay / proposal packet 补 archive-ready 元数据
  - 文件：
    - `src/ashare_system/discussion/discussion_service.py`
    - `tests/test_discussion_helpers.py`
    - `docs/implementation-drafts/openclaw-replay-proposal-packet-v1.md`
  - 目标：
    - 在现有 packet 上补：
      - `packet_id`
      - `source_refs`
      - `archive_tags`
      - `research_track`
    - 让 Linux/OpenClaw 侧后续可直接归档盘后 replay / 自我进化样本
  - 验收：
    - 仍不触发 live
    - 包体继续保持 JSON 可序列化
