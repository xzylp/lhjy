# A股量化交易系统完整方案 v1 任务拆解

> 基于 [quant-full-v1-complete.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-complete.md)
> 面向 `ashare-system-v2` 当前实际代码结构
> 目标：把完整方案拆成可编码、可测试、可验收的任务清单
> 日期：2026-04-06

---

## 1. 拆解原则

这份任务拆解遵循四个原则：

- **优先打通主链路**：先让“市场状态 -> 板块联动 -> 战法路由 -> 买入决策”跑起来。
- **先规则后增强**：先用规则型可解释系统落地，再补股性画像、退出监控、归因回测。
- **严格贴合现有目录**：不另起平行系统，尽量复用 `sentiment/`, `strategy/`, `risk/`, `discussion/`, `backtest/`, `apps/`。
- **每个阶段都能验收**：每个任务包结束时都要能通过测试，并能回答“系统行为具体变了什么”。

---

## 2. 里程碑总览

## M0：数据契约扩展与最小集成

目标：

- 扩展 contracts
- 建立新模块骨架
- 不破坏现有测试

输出：

- `MarketProfile` 扩展字段
- `SectorProfile`
- `PlaybookContext`
- `ExitContext`
- 新模块空骨架和基础测试

## M1：市场状态机 + 板块联动引擎

目标：

- 系统能输出日级 `regime`
- 系统能输出板块画像和生命周期

输出：

- `sentiment/regime.py`
- `sentiment/sector_cycle.py`
- sector linkage 因子

## M2：战法路由 + 买入决策改造

目标：

- 不再只按统一分数阈值买入
- 改成先路由战法、再在战法内筛选

输出：

- `strategy/router.py`
- `strategy/playbooks/`
- `buy_decision.py` 改造

## M3：个股股性画像 + 龙头相对排名

目标：

- 系统能识别板块前排和历史股性

输出：

- `strategy/stock_profile.py`
- `strategy/leader_rank.py`

## M4：退出引擎

目标：

- 从 ATR 通用退出升级到战法化退出

输出：

- `strategy/exit_engine.py`
- `sell_decision.py` 底层保留并接入回退逻辑

## M5：OpenClaw 编排和讨论协议落地

目标：

- 多 agent 输入输出结构固定
- `ashare` 作为消息总线和主持人真正可用

输出：

- discussion dossier / packet / finalize 结构
- OpenClaw prompt 调整

## M6：回测归因与学习闭环

目标：

- 不只看总收益，要看按战法、按状态、按退出原因的表现

输出：

- playbook backtest
- attribution report
- trade review 闭环

---

## 3. 任务树

## Task Group A：数据契约与模块骨架

### A1. 扩展 `contracts.py`

文件：

- [contracts.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/contracts.py)

任务：

- 在 `MarketProfile` 追加字段：
  - `regime`
  - `regime_score`
  - `allowed_playbooks`
  - `market_risk_flags`
  - `sector_profiles`
- 新增 `SectorProfile`
- 新增 `PlaybookContext`
- 新增 `ExitContext`
- 为后续 `StockBehaviorProfile`、`LeaderRankResult`、`ExitSignal` 预留契约位置

验收：

- 现有依赖 `MarketProfile` 的测试不破坏
- 新模型可以 `model_dump()` / `model_validate()`

测试：

- 新增 `tests/test_contracts_quant_extensions.py`

依赖：

- 无

优先级：

- P0

### A2. 建立新模块骨架

文件：

- `src/ashare_system/sentiment/regime.py`
- `src/ashare_system/sentiment/sector_cycle.py`
- `src/ashare_system/strategy/router.py`
- `src/ashare_system/strategy/exit_engine.py`
- `src/ashare_system/strategy/leader_rank.py`
- `src/ashare_system/strategy/stock_profile.py`
- `src/ashare_system/strategy/playbooks/__init__.py`
- `src/ashare_system/strategy/playbooks/leader_chase.py`
- `src/ashare_system/strategy/playbooks/divergence_reseal.py`
- `src/ashare_system/strategy/playbooks/sector_reflow.py`

任务：

- 建立模块文件和最小接口
- 确保 import 路径可用

验收：

- 项目可以成功 import 新模块

测试：

- 新增 `tests/test_import_quant_modules.py`

依赖：

- A1

优先级：

- P0

---

## Task Group B：市场状态机

### B1. 实现 `regime` 分类函数

文件：

- `src/ashare_system/sentiment/regime.py`

任务：

- 实现 `classify_regime(...)`
- 输入：
  - `limit_up_count`
  - `board_fail_rate`
  - `seal_rate`
  - `max_consecutive_up`
  - `up_down_ratio`
  - `prev_day_premium`
  - `theme_concentration`
- 输出：
  - `regime`
  - `allowed_playbooks`
  - `risk_flags`

验收：

- 对极弱、退潮、强趋势、轮动四种场景给出稳定输出
- 边界场景偏保守

测试：

- 新增 `tests/test_phase_market_regime.py`

依赖：

- A1

优先级：

- P0

### B2. 与现有情绪模块对接

文件：

- [sentiment/calculator.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/sentiment/calculator.py)
- [risk/emotion_shield.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/risk/emotion_shield.py)

任务：

- 将现有 `sentiment_phase` 与新增 `regime` 组合成新的 `MarketProfile`
- 在 `EmotionShield` 中新增 `get_allowed_playbooks(profile)`
- 加入“冰点空仓、高潮降级追高”的覆盖逻辑

验收：

- `MarketProfile` 同时具有：
  - `sentiment_phase`
  - `regime`
  - `position_ceiling`
  - `allowed_playbooks`

测试：

- 扩展 `tests/test_phase3_strategy_risk_backtest.py`
- 新增 `tests/test_emotion_regime_integration.py`

依赖：

- B1

优先级：

- P0

### B3. 计算 `theme_concentration`

文件：

- `src/ashare_system/sentiment/regime.py`
- 可选复用：
  - [monitor/limit_analyzer.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/monitor/limit_analyzer.py)
  - [monitor/stock_pool.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/monitor/stock_pool.py)

任务：

- 基于板块或题材映射，计算前三题材涨停占比
- 先支持稳定来源，不做过度复杂题材聚类

验收：

- `theme_concentration` 能进入 regime 判定链

测试：

- 新增 `tests/test_theme_concentration.py`

依赖：

- B1

优先级：

- P1

---

## Task Group C：板块联动引擎

### C1. 设计 `SectorCycle` 数据输入结构

文件：

- `src/ashare_system/sentiment/sector_cycle.py`
- [infra/market_adapter.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/infra/market_adapter.py)
- [precompute.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/precompute.py)

任务：

- 明确 `SectorData` 的最小输入字段：
  - `zt_count`
  - `prev_zt_count`
  - `up_count`
  - `total_count`
  - `active_days`
  - `board_fail_rate`
  - `top_symbols`
  - 可选分钟线强度

验收：

- 数据结构固定，可被 router 和 exit engine 复用

测试：

- 新增 `tests/test_sector_cycle_contract.py`

依赖：

- A1

优先级：

- P0

### C2. 实现 `SectorCycle.build_profiles`

文件：

- `src/ashare_system/sentiment/sector_cycle.py`

任务：

- 实现：
  - `build_profiles`
  - `_calc_breadth`
  - `_calc_reflow_score`
  - `_calc_strength`
  - `_classify_lifecycle`
- 输出 `List[SectorProfile]`

验收：

- 可以按 `strength_score` 排序
- 可以判断：
  - `start`
  - `ferment`
  - `climax`
  - `retreat`

测试：

- 新增 `tests/test_phase_sector_linkage.py`

依赖：

- C1

优先级：

- P0

### C3. 注册板块联动因子

文件：

- `src/ashare_system/factors/behavior/sector_linkage.py`
- [factors/registry.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/factors/registry.py)

任务：

- 注册最少 5 到 7 个 sector 类因子：
  - `sector_return_rel_market`
  - `sector_up_ratio`
  - `sector_limit_up_count`
  - `sector_front_rank_strength`
  - `sector_active_days`
  - `sector_reflow_score`
  - `sector_dispersion_score`

验收：

- `registry.list_by_category("sector")` 能列出相关因子

测试：

- 扩展 `tests/test_phase2_factors.py`

依赖：

- C2

优先级：

- P1

### C4. 对接预计算和 API

文件：

- [precompute.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/precompute.py)
- [apps/market_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/market_api.py)
- [apps/runtime_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/runtime_api.py)

任务：

- 让预计算产出板块画像
- 暴露读取接口，供 runtime 和 discussion 使用

验收：

- runtime 层能拿到当日 `sector_profiles`

测试：

- 新增 `tests/test_precompute_sector_profiles.py`
- 新增 API 测试

依赖：

- C2

优先级：

- P1

---

## Task Group D：战法路由

### D1. 定义路由表和输入输出

文件：

- `src/ashare_system/strategy/router.py`

任务：

- 固定 `ROUTE_TABLE`
- 定义 `StrategyRouter.route(...)`
- 输入：
  - `MarketProfile`
  - `sector_profiles`
  - `candidates`
  - `stock_info`
- 输出：
  - `List[PlaybookContext]`

验收：

- `chaos/defensive` 默认空输出
- `trend/rotation` 能按 `allowed_playbooks` 正确过滤

测试：

- 新增 `tests/test_phase_playbook_router.py`

依赖：

- B2
- C2

优先级：

- P0

### D2. 实现三个 playbook 的最小版本

文件：

- `src/ashare_system/strategy/playbooks/leader_chase.py`
- `src/ashare_system/strategy/playbooks/divergence_reseal.py`
- `src/ashare_system/strategy/playbooks/sector_reflow.py`

任务：

- 每个 playbook 提供：
  - `match(...)`
  - `score(...)`
  - `build_entry_window(...)`
  - `default_exit_params()`

验收：

- 三个 playbook 可独立打分
- router 可消费 playbook 输出

测试：

- 新增：
  - `tests/test_playbook_leader_chase.py`
  - `tests/test_playbook_divergence_reseal.py`
  - `tests/test_playbook_sector_reflow.py`

依赖：

- D1

优先级：

- P1

### D3. 改造 `BuyDecisionEngine`

文件：

- [strategy/buy_decision.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/buy_decision.py)

任务：

- 取消仅靠 `score >= BUY_SCORE_THRESHOLD` 的准入方式
- 改为消费：
  - `PlaybookContext`
  - playbook score
  - `PositionManager`
- 保留向后兼容入口，避免一次性打断现有测试

验收：

- 新版能按 playbook 生成买入候选
- 老测试尽量兼容，必要时同步更新

测试：

- 扩展 `tests/test_phase3_strategy_risk_backtest.py`
- 新增 `tests/test_buy_decision_playbook_mode.py`

依赖：

- D1
- D2

优先级：

- P0

### D4. 改造 `strategy_api` / `runtime_api`

文件：

- [apps/strategy_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/strategy_api.py)
- [apps/runtime_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/runtime_api.py)

任务：

- 为路由结果、playbook 候选、最终买入候选提供 API
- 允许 discussion 层读取候选 dossier

验收：

- 能通过 API 查询：
  - 当前 regime
  - 热门板块
  - 候选 playbook 分配

测试：

- 新增 `tests/test_strategy_api_playbooks.py`

依赖：

- D3

优先级：

- P1

---

## Task Group E：股性画像与龙头相对排名

### E1. 新增 `StockBehaviorProfile` 契约

文件：

- [contracts.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/contracts.py)

任务：

- 新增：
  - `board_success_rate_20d`
  - `bomb_rate_20d`
  - `next_day_premium_20d`
  - `reseal_rate_20d`
  - `optimal_hold_days`
  - `style_tag`
  - `avg_sector_rank_30d`
  - `leader_frequency_30d`

验收：

- 可序列化、可被 strategy 层消费

测试：

- 扩展 `tests/test_contracts_quant_extensions.py`

依赖：

- A1

优先级：

- P1

### E2. 实现 `StockProfileBuilder`

文件：

- `src/ashare_system/strategy/stock_profile.py`

任务：

- 基于 20 到 60 日历史构建股性画像
- 先用日级和分钟级稳定字段
- 不强依赖逐笔

验收：

- 能为样例股票输出稳定画像

测试：

- 新增 `tests/test_phase_stock_profile.py`

依赖：

- E1

优先级：

- P1

### E3. 实现 `LeaderRanker`

文件：

- `src/ashare_system/strategy/leader_rank.py`

任务：

- 用相对排名替代绝对涨幅
- 输出：
  - `leader_score`
  - `zt_order_rank`
  - `is_core_leader`

验收：

- 同板块内前排优先级明显高于跟风

测试：

- 新增 `tests/test_phase_leader_rank.py`

依赖：

- E2
- C2

优先级：

- P1

### E4. 将股性和龙头信息接入 router / playbook

文件：

- `src/ashare_system/strategy/router.py`
- `src/ashare_system/strategy/playbooks/*.py`

任务：

- playbook score 增加：
  - 股性画像项
  - 龙头相对排名项

验收：

- routing / ranking 不再只依赖市场状态和板块生命周期

测试：

- 扩展 playbook 测试

依赖：

- E3

优先级：

- P1

---

## Task Group F：退出引擎

### F1. 定义 `ExitSignal` 契约

文件：

- [contracts.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/contracts.py)

任务：

- 新增：
  - `reason`
  - `sell_ratio`
  - `urgency`
  - 可选上下文字段

验收：

- 能统一表达：
  - 开仓失败立撤
  - 炸板立撤
  - 板块退潮
  - 冲高不封减仓
  - 时间止损

测试：

- 扩展 contracts 测试

依赖：

- A1

优先级：

- P1

### F2. 实现 `ExitEngine.check`

文件：

- `src/ashare_system/strategy/exit_engine.py`

任务：

- 实现优先级：
  - `entry_failure`
  - `board_break`
  - `sector_retreat`
  - `no_seal_on_surge`
  - `time_stop`
  - ATR fallback

验收：

- 明显优先于 ATR 通用退出
- 能输出可解释退出原因

测试：

- 新增 `tests/test_phase_exit_engine.py`

依赖：

- F1
- C2

优先级：

- P1

### F3. 与 `sell_decision.py` 联动

文件：

- [strategy/sell_decision.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/sell_decision.py)
- `src/ashare_system/strategy/exit_engine.py`

任务：

- 保留 ATR 逻辑作为底层 fallback
- 不再把 ATR 作为最高优先级退出策略

验收：

- 退出触发顺序与设计一致

测试：

- 扩展现有 sell decision 测试

依赖：

- F2

优先级：

- P1

### F4. 接入执行与通知

文件：

- [apps/execution_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/execution_api.py)
- [notify/live_execution_alerts.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/notify/live_execution_alerts.py)
- [execution_safety.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/execution_safety.py)

任务：

- 当退出信号触发时：
  - 记录原因
  - 生成执行请求
  - 发通知

验收：

- 实盘/模拟执行都能带上退出原因

测试：

- 新增 `tests/test_execution_exit_signal_flow.py`

依赖：

- F2

优先级：

- P2

---

## Task Group G：OpenClaw 编排与讨论协议

### G1. 固定候选 dossier 结构

文件：

- [discussion/candidate_case.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/candidate_case.py)
- [discussion/client_brief.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/client_brief.py)

任务：

- 为每只候选生成标准 dossier：
  - `MarketProfile`
  - `SectorProfile`
  - `PlaybookContext`
  - 研究要点
  - 风险要点
  - 执行建议

验收：

- 多 agent 读取上下文一致，不再自由发挥数据结构

测试：

- 新增 `tests/test_candidate_dossier.py`

依赖：

- B2
- C2
- D3

优先级：

- P1

### G2. 固定讨论状态机输入输出

文件：

- [discussion/state_machine.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/state_machine.py)
- [discussion/discussion_service.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/discussion_service.py)

任务：

- 固定阶段：
  - 候选装配
  - research 说明
  - strategy 排序
  - risk 审批
  - audit 复核
  - finalize
- 固定每阶段输入输出字段

验收：

- 任一阶段失败能定位
- 讨论结果可回放

测试：

- 扩展 `tests/test_system_governance.py`
- 新增 `tests/test_discussion_pipeline.py`

依赖：

- G1

优先级：

- P1

### G3. 调整 OpenClaw prompt 与协议文档

文件：

- `openclaw/prompts/ashare-runtime.txt`
- `openclaw/prompts/ashare-research.txt`
- `openclaw/prompts/ashare-strategy.txt`
- `openclaw/prompts/ashare-risk.txt`
- `openclaw/prompts/ashare-audit.txt`
- `openclaw/prompts/ashare.txt`

任务：

- 明确每个 agent 的：
  - 职责边界
  - 必答字段
  - 禁止模糊表达
  - 否决权规则

验收：

- 同一候选多轮讨论输出格式稳定

测试：

- 可用 smoke discussion flow 脚本验证

依赖：

- G2

优先级：

- P2

---

## Task Group H：回测归因与学习闭环

### H1. 扩展回测执行器以支持 playbook

文件：

- [backtest/engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/backtest/engine.py)
- 新增 `src/ashare_system/backtest/playbook_runner.py`

任务：

- 按 playbook 执行交易规则
- 支持不同 playbook 的退出参数

验收：

- 回测不再只认一个统一进出场逻辑

测试：

- 新增 `tests/test_phase_playbook_backtest.py`

依赖：

- D2
- F2

优先级：

- P2

### H2. 增加归因维度

文件：

- 新增 `src/ashare_system/backtest/attribution.py`
- [report/strategy_report.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/report/strategy_report.py)

任务：

- 增加按以下维度归因：
  - `regime`
  - `sentiment_phase`
  - `playbook`
  - `sector_life_cycle`
  - `exit_reason`

验收：

- 能回答：
  - 哪个战法在什么市场赚钱
  - 哪种退出最有效

测试：

- 新增 `tests/test_backtest_attribution.py`

依赖：

- H1

优先级：

- P2

### H3. 接入学习闭环

文件：

- [learning/trade_review.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/learning/trade_review.py)
- [learning/continuous.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/learning/continuous.py)
- [learning/score_state.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/learning/score_state.py)

任务：

- 将：
  - playbook 表现
  - exit reason 表现
  - regime 表现
    反馈到参数和复盘中

验收：

- 交易复盘可以看到战法层面的表现差异

测试：

- 新增 `tests/test_learning_playbook_feedback.py`

依赖：

- H2

优先级：

- P3

---

## 4. 推荐执行顺序

## 第一批：先打通最小闭环

顺序：

1. A1
2. A2
3. B1
4. B2
5. C1
6. C2
7. D1
8. D3

阶段目标：

- 系统能输出：
  - 市场状态
  - 板块画像
  - 战法候选
  - playbook 模式买入建议

这时已经从“统一总分交易”升级到“有市场状态与战法路由的交易系统骨架”。

## 第二批：让前排识别和快撤成立

顺序：

1. D2
2. E1
3. E2
4. E3
5. E4
6. F1
7. F2
8. F3

阶段目标：

- 系统能分清板块前排和跟风
- 退出不再只依赖 ATR

## 第三批：把多 agent 和回测闭环做完整

顺序：

1. G1
2. G2
3. G3
4. H1
5. H2
6. H3
7. F4
8. C3
9. C4

阶段目标：

- OpenClaw 协作链稳定
- 回测和学习闭环形成

---

## 5. 每个里程碑的验收问题

## M1 结束时必须能回答

- 今天市场属于哪个 `regime`？
- 当前允许哪几种 playbook？
- 当前最强板块是谁？处于哪个生命周期？

## M2 结束时必须能回答

- 为什么这只票被分配到 `leader_chase` 而不是 `sector_reflow_first_board`？
- 为什么另一只票没有进入候选？

## M3 结束时必须能回答

- 这只票在板块内是前排还是跟风？
- 这只票历史上是爱炸板还是爱回封？

## M4 结束时必须能回答

- 这次卖出是因为开仓失败、炸板、板块退潮，还是 ATR 底层止损？

## M5 结束时必须能回答

- 同一轮讨论里，research、strategy、risk、audit 的依据是否来自同一份 dossier？

## M6 结束时必须能回答

- 哪种 playbook 在 `trend` 最赚钱？
- 哪种退出规则最保命？

---

## 6. 现在最应该开工的任务

如果只启动第一轮编码，我建议只做下面这些：

- A1 扩展 `contracts.py`
- A2 建立模块骨架
- B1 实现 `regime.py`
- B2 把 `regime` 接入 `MarketProfile` 与 `emotion_shield.py`
- C1 定义 `SectorData` / `SectorProfile` 输入输出
- C2 实现 `SectorCycle.build_profiles`
- D1 实现 `StrategyRouter`
- D3 改造 `BuyDecisionEngine`

这是最小且有效的一批。

做完这批后，系统行为会发生实质性变化，但改动面仍然可控。
