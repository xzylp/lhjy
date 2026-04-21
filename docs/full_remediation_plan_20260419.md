# ashare-system-v2 完整整改补全方案

> 版本：2026-04-19  
> 性质：总代码补全方案  
> 目标：把“最初专家评审清单”与“本轮 Agent 因子/战法编排讨论”合并成同一份可执行整改文档，作为后续持续开发与验收依据。

---

## 1. 文档目的

本方案不是单点修 bug 清单，而是面向正式上线前的主链整改方案。  
整改目标分三层：

1. 把已有的能力面、因子库、战法库、风控链、Elo 治理链从“能展示”推进到“强制生效”。
2. 防止 Agent 用“堆很多因子”伪装“真正理解市场”。
3. 建立从感知层到决策层、执行层、归因层、权重更新层的可审计闭环。

---

## 2. 当前问题总览

### 2.1 最初专家评审主问题

1. 因子数量虽增长，但感知层信息没有强制进入执行链。
2. 宏观、筹码、跨市场、另类数据四大维度长期缺口未完全补齐。
3. 风控新规则若不进入 `ExecutionGuard.approve()` 主链，规则层等于空转。
4. Elo 若没有自动喂数与真实结果对照，只是“评分外壳”。
5. `composite_multiplier` 若没有回测标定，会把排序结果带偏。
6. Agent 感知层不完整，容易出现“知道接口存在，但不真正用它”。

### 2.2 本轮讨论新增主问题

1. Agent 可能只是随意拉几个因子，因子数变多不代表信号独立。
2. `/runtime/capabilities` 已经提供 `factor_effectiveness`，但 compose 逻辑还没有强制吃进去。
3. 即便 `compose-from-brief` 被修，直接 `compose` 仍可能绕过约束。
4. 系统虽然已有部分相关性组约束骨架，但还没有和“因子有效性”联动。
5. 当前更缺“组合层纪律”而不是“继续盲目扩因子数”。

### 2.3 整改总原则

1. 规则必须落在服务端主链，不依赖 prompt 自觉。
2. 约束必须同时覆盖 `compose-from-brief` 与直接 `compose`。
3. 不能只给出“可用目录”，必须给出“强制执行边界”。
4. 不能只调单因子权重，必须评估“因子组合健康度”。
5. 每次自动干预都必须可解释、可审计、可复盘。

---

## 3. 整改优先级总表

### P0：上线前必须完成

1. 因子有效性强制注入 compose 主链
2. 因子组合约束引擎
3. 因子组合健康度评分
4. 解释层与审计层同步回显
5. 风控规则接线与参数实喂复核
6. Elo 自动喂数闭环复核与补强
7. `composite_multiplier` 保守化与校准入口
8. capabilities 契约与执行规则对齐

### P1：正式上线后一到两轮迭代完成

1. 分 market regime 的因子有效性
2. 战法有效性监控
3. learned asset 与当前有效性联动
4. 因子选择理由结构化
5. 宏观/筹码/成长质量维度继续补齐
6. Agent 感知层扩展到持仓、讨论、执行容量

### P2：中期增强

1. 跨市场联动因子
2. 另类数据因子
3. PCA / 因子降维 / 组内正交增强
4. Elo 向 pairwise 或分角色对战制演进
5. 参数长期回测标定与版本治理

---

## 4. P0 任务清单

### P0-1 因子有效性强制注入 compose 主链

#### 目标

把 `/runtime/capabilities` 中的 `factor_effectiveness` 从“展示信息”升级为“执行前硬约束”。

#### 问题说明

- 当前 `runtime_api.py` 中 `/runtime/capabilities` 已返回 `effectiveness_status` 与 `effectiveness`。
- 但 `_build_compose_request_from_brief()` 只做注册校验、权重归一化与仓库访问控制，没有根据有效性调整权重。
- 直接调用 `/runtime/jobs/compose` 时也没有相同规则。

#### 改动文件

- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
- [factor_monitor.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/factor_monitor.py)
- [strategy_composer.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/strategy_composer.py)
- 建议新增 [compose_factor_policy.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/compose_factor_policy.py)

#### 代码补全点

1. 在 `_build_compose_request_from_brief()` 注入因子有效性快照。
2. 在 `run_compose()` 前再次执行同样的策略，防止绕过 brief 链路。
3. 引入 `apply_factor_effectiveness_policy()`：
   - `effective / ineffective`：都按 `mean_rank_ic + p_value` 连续调权
   - 线性调权主口径：`1 + mean_rank_ic * 5.0`
   - 超出显著性阈值后叠加显著性折减，并做上下限裁剪
   - `unknown / unavailable / unsupported_for_monitor`：不自动惩罚，但加 warning
4. 把调整结果回写到 compose 响应与审计日志。

#### 新增接口字段

- `requested_factor_weights`
- `adjusted_factor_weights`
- `factor_effectiveness_trace`
- `effectiveness_warnings`
- `factor_policy_summary`

#### 验收标准

1. 选中 `ineffective` 因子时，返回结果里能明确看到权重被自动下调。
2. 直接调用 `/runtime/jobs/compose` 与 `/runtime/jobs/compose-from-brief` 行为一致。
3. `unsupported_for_monitor` 因子不会被误杀，只会被标记“当前无监控结论”。
4. 本次自动调整可在审计日志中复原。

#### 依赖关系

- 依赖现有 `FactorMonitor.build_effectiveness_snapshot()`
- 为 P0-2 与 P0-3 的前置输入

---

### P0-2 因子组合约束引擎

#### 目标

防止 Agent 通过“随意拉多个因子”制造伪多因子分散。

#### 问题说明

- 因子数量增加不等于信号独立。
- 若多个因子来自同一维度或同一相关组，数量反而会放大同质信号。
- 当前系统有部分 `correlation_group` 正交化，但还不够，也没有和有效性联动。

#### 改动文件

- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
- [strategy_composer.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/strategy_composer.py)
- [factor_registry.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/factor_registry.py)
- 建议新增 [compose_factor_policy.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/compose_factor_policy.py)

#### 代码补全点

1. 新增 `validate_factor_mix()`。
2. 新增相关组与预算规则：
   - 至少 `3` 个有效或条件可用因子
   - 不再限制“同维度最多几个”，允许单维度集中，但要显式做集中度审查
   - 同一 `correlation_group` 最多 `1` 个主因子 + `1` 个辅助因子
   - `ineffective` 因子最多 `1` 个
   - `unknown/unavailable` 因子最多 `2` 个
3. 连续监控因子按 IC 与显著性自动调权，相关组超限时拒绝 compose。
4. 相关组限额要和有效性策略串联：
   - 先做 IC 连续调权
   - 再做相关组封顶

#### 新增接口字段

- `factor_mix_validation`
- `factor_rejections`
- `factor_auto_downgrades`
- `diversification_warnings`
- `needs_review`

#### 验收标准

1. 同维度堆叠不再被硬拦截，但系统能明确识别集中度并触发 `needs_review`。
2. 高相关因子一起入选时，最终有效权重不会线性累加。
3. “因子数量多但有效因子少”的请求会被标记为 `needs_review` 或拒绝。
4. 结果里能说明“不是维度太少，而是组合过度集中或伪分散”。

#### 依赖关系

- 依赖 P0-1 的有效性结果
- 为 P0-3 健康度评分提供输入

---

### P0-3 因子组合健康度评分

#### 目标

让系统能区分“真实多因子组合”和“伪分散堆料组合”。

#### 改动文件

- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
- [strategy_composer.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/strategy_composer.py)

#### 代码补全点

新增 `factor_portfolio_health`：

- `factor_count`
- `effective_factor_count`
- `dimension_count`
- `ineffective_factor_count`
- `unknown_factor_count`
- `correlation_conflict_count`
- `dominant_dimension_ratio`
- `effective_weight_concentration`
- `health_status`

`health_status` 初版建议四档：

- `healthy`
- `concentrated`
- `noise_heavy`
- `pseudo_diversified`

#### 验收标准

1. 一次 compose 后，能一眼判断这次是不是伪多因子。
2. 若主权重集中在单一维度，健康度必须自动降级。
3. 健康度不只是展示项，要能进入 `needs_review` 判断。
4. `needs_review=true` 时，必须明确转为“禁止自动派发/禁止自动执行，需 discussion 或 execution-precheck 人工确认”。

#### 依赖关系

- 依赖 P0-1、P0-2 的结果

---

### P0-4 解释层与审计层同步回显

#### 目标

不能只偷偷改权重，必须让 Agent 和操作者知道系统做了哪些自动干预。

#### 改动文件

- [strategy_composer.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/strategy_composer.py)
- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
- [system_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py)

#### 代码补全点

1. 在 explanations 中增加：
   - 哪些因子是主驱动
   - 哪些因子被自动降权
   - 哪些因子因相关性冲突被压缩
2. 在 compose 响应中回显：
   - `factor_policy_summary`
   - `factor_rejections`
   - `factor_auto_downgrades`
3. 在 audit 中记录：
   - 请求原始权重
   - 调整后权重
   - 触发规则
   - 最终健康度

#### 验收标准

1. 最终结果里能回答“为什么这次不是 Agent 原样方案”。
2. 一次 compose 的自动干预可以在 audit 中完整回放。

#### 依赖关系

- 依赖 P0-1、P0-2、P0-3

---

### P0-5 风控规则主链复核与增强

#### 目标

确保风控新规则不仅存在于 `rules.py`，而且真正使用当前请求与账户态数据。

#### 问题说明

最初评审已经指出，若 `realized_volatility`、`position_correlation`、`daily_turnover_amount`、`single_position_pnl_pct` 不传入 `ExecutionGuard.approve()`，新增规则永远不触发。

#### 改动文件

- [guard.py](/srv/projects/ashare-system-v2/src/ashare_system/risk/guard.py)
- [rules.py](/srv/projects/ashare-system-v2/src/ashare_system/risk/rules.py)
- [system_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py)

#### 代码补全点

1. 复核现有四项参数实喂是否覆盖全部执行入口。
2. 给预检响应增加规则命中明细：
   - 哪条风控规则命中
   - 依据数据是什么
   - 是降级、限额还是拒绝
3. 对缺失数据的情况显式返回，而不是静默 `None`。

#### 新增接口字段

- `risk_rule_trace`
- `risk_inputs_snapshot`
- `risk_degradation_reason`

#### 验收标准

1. 四条新增规则在预检响应中可见命中轨迹。
2. 缺数据时系统明确告知“规则未判断”，而不是默认通过。
3. discussion / execution-precheck / dispatch 三条链对风险口径一致。

#### 依赖关系

- 独立推进，但属于正式上线前硬门槛

---

### P0-6 Elo 自动喂数闭环补强

#### 目标

确保 Agent Elo 不是孤立评分，而是持续接收讨论终审与结果归因的真实反馈。

#### 问题说明

最初评审指出：Elo 算法本身没问题，问题在于“谁来自动喂结果”。

#### 改动文件

- [discussion_service.py](/srv/projects/ashare-system-v2/src/ashare_system/discussion/discussion_service.py)
- [settlement.py](/srv/projects/ashare-system-v2/src/ashare_system/learning/settlement.py)
- [agent_rating.py](/srv/projects/ashare-system-v2/src/ashare_system/learning/agent_rating.py)
- [score_state.py](/srv/projects/ashare-system-v2/src/ashare_system/learning/score_state.py)

#### 代码补全点

1. 复核 `finalize_cycle()` 自动结算是否覆盖所有终审出口。
2. 结果归因侧 `reconcile_outcome()` 与 discussion 结算侧保持统一的 `delta -> actual` 映射。
3. 在 score state 中记录“本次结算来源”：
   - `discussion_finalize`
   - `execution_outcome`
   - `postclose_reconcile`

#### 新增接口字段

- `rating_update_source`
- `rating_update_trace`
- `discussion_settlement_applied`
- `outcome_settlement_applied`

#### 验收标准

1. discussion 终审后能自动看到 Elo 变化。
2. outcome 归因后不会重复结算同一事件。
3. 任一 Agent 的 Elo 更新都能追到来源。

#### 依赖关系

- 与 P0-4 审计层联动

---

### P0-7 `composite_multiplier` 保守化与校准入口

#### 目标

避免复合调整倍数成为新的“拍脑袋放大器”。

#### 问题说明

最初评审指出：哪怕配置化了，只要默认值缺少回测支持，风险仍在。

#### 改动文件

- [strategy_composer.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/strategy_composer.py)
- [evaluation_ledger.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/evaluation_ledger.py)
- [runtime_config.py](/srv/projects/ashare-system-v2/src/ashare_system/runtime_config.py)

#### 代码补全点

1. 继续坚持“非默认配置值可用，但默认口径优先走账本估算”。
2. 增加样本不足保护：
   - 样本少于阈值时，只能走保守回退值
3. 在 explanations 里显式输出：
   - 当前 multiplier
   - 来源：配置 / 估算 / 保守回退

#### 新增接口字段

- `composite_adjustment_meta`
- `multiplier_source`
- `sample_support`

#### 验收标准

1. 不再出现无依据直接用高倍数放大排序的情况。
2. 任意一次 compose 都能解释当前 multiplier 从何而来。

#### 依赖关系

- 与 P0-4 解释层联动

---

### P0-8 capabilities 契约与执行规则对齐

#### 目标

让 Agent 在调用前就能知道真实规则，而不是只看到 advisory 文案。

#### 改动文件

- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py)

#### 代码补全点

在 `/runtime/capabilities` 中新增：

- `factor_selection_policy`
- `diversification_rules`
- `rejection_conditions`
- `warning_conditions`
- `direct_compose_guard_enabled`

#### 验收标准

1. capabilities 返回的规则与 compose 实际执行规则一致。
2. 不再出现“文档说 advisory_only，执行层却完全不管”的错位。

#### 依赖关系

- 应在 P0-1、P0-2 确认后同步更新

---

## 7. 2026-04-19 本轮实际完成报告

### 7.1 本轮完成状态总览

- P0-1：已完成
- P0-2：已完成
- P0-3：已完成
- P0-4：已完成
- P0-5：此前已完成，本轮未改主代码，仅保持兼容
- P0-6：此前已完成，本轮未改主代码，仅保持兼容
- P0-7：此前已完成为“保守口径 + 账本估算入口”，本轮未改主代码
- P0-8：已完成

### 7.2 本轮真实落地内容

#### P0-1 因子有效性强制注入 compose 主链

已落地：

- 新增 [compose_factor_policy.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/compose_factor_policy.py)
- 在 [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py) 中新增 `_apply_compose_factor_policy()`
- `_build_compose_request_from_brief()` 已先做一次策略守门
- `run_compose()` 已再次执行相同守门，防止 direct compose 绕过
- 守门结果会把 `requested_factor_weights / adjusted_factor_weights / factor_effectiveness_trace / effectiveness_warnings` 写入响应，并回写到 `request.market_context.factor_policy`

本轮真实规则：

- `effective / ineffective`：都按 `mean_rank_ic + p_value` 连续调权
- 连续调权口径：`1 + mean_rank_ic * 5.0`，再叠加显著性折减并做上下限裁剪
- `unsupported_for_monitor / unavailable / unknown`：不直接惩罚，但会进入 warning，并降低健康度
- `unsupported_for_monitor`：同时要求进入 outcome attribution 事后归因链

真实边界：

- 有效性阈值仍来自现有 `FactorMonitor` 配置，不是分 market regime 回测标定结果
- `factor_monitor.py` 本轮未改算法，只复用现有快照能力

#### P0-2 因子组合约束引擎

已落地：

- 已删除“同维度软上限”这类硬限制
- 同相关组软上限：`2`，第二个自动降权，更多直接剔除
- `ineffective` 预算：最多保留 `1` 个
- `unknown/unavailable` 预算：最多保留 `2` 个，超限自动压权
- 严重噪声组合会在 `run_compose()` 被执行层直接拒绝
- 同维度集中不再直接拦截，而是转为集中度审查 + `needs_review`

新增回显字段：

- `factor_mix_validation`
- `factor_rejections`
- `factor_auto_downgrades`
- `diversification_warnings`
- `needs_review`

真实边界：

- 当前配额是规则型阈值，不是历史最优参数
- 相关组仍依赖现有 `factor_registry` 的 `correlation_group` 标注质量
- regime-aware IC 还不是完全体；本轮仅把 regime-aware 的集中度阈值前置到了执行层

#### P0-3 因子组合健康度评分

已落地字段：

- `factor_count`
- `active_factor_count`
- `effective_factor_count`
- `usable_factor_count`
- `dimension_count`
- `ineffective_factor_count`
- `unknown_factor_count`
- `unsupported_factor_count`
- `correlation_conflict_count`
- `dominant_dimension_ratio`
- `effective_weight_concentration`
- `health_status`

已接线逻辑：

- `health_status` 四档：`healthy / concentrated / noise_heavy / pseudo_diversified`
- `needs_review` 不再是纯提示，已由健康度与约束结果共同驱动
- `needs_review=true` 时，已明确转为：
  - `auto_dispatch_allowed=false`
  - `auto_execution_allowed=false`
  - `position_size_multiplier` 自动下调
  - 下一步必须走 `discussion / execution-precheck`

真实边界：

- 健康度目前是规则解释分，不是收益预测分
- 尚未引入 PCA、协方差矩阵或滚动正交统计

#### P0-4 解释层与审计层同步回显

已落地：

- [strategy_composer.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/strategy_composer.py) 已输出：
  - `factor_policy_summary`
  - `factor_portfolio_health`
  - `factor_adjustment_summary`
- 每个 candidate 的 `scoring_meta.factor_policy` 已包含：
  - `needs_review`
  - `health_status`
  - `requested_factor_weights`
  - `adjusted_factor_weights`
- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py) 返回顶层：
  - `factor_policy_summary`
  - `factor_portfolio_health`
  - `factor_mix_validation`
  - `factor_effectiveness_trace`
  - `factor_auto_downgrades`
  - `factor_rejections`
  - `effectiveness_warnings`
  - `diversification_warnings`
  - `needs_review`
  - `review_action`
- `composition_manifest.evidence` 已加入 `factor_policy_summary`
- `composition_manifest.factor_policy` 已加入权重前后对照与健康度
- 审计已新增 `runtime_compose_policy` 记录，可通过现有 `/system/audits` 查看
- `StrategyComposer` 已把 `review_action_summary` 写进解释层

真实边界：

- 本轮未新增单独的 policy panel；当前依赖 compose 返回和审计流查看
- [system_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py) 本轮未改代码

#### P0-8 capabilities 契约与执行规则对齐

已落地：

- `/runtime/capabilities` 新增：
  - `factor_selection_policy`
  - `diversification_rules`
  - `rejection_conditions`
  - `warning_conditions`
  - `direct_compose_guard_enabled`
- `/runtime/evaluations/panel` 现已把 unsupported 因子纳入事后归因面，不再是纯盲区
- `compose_contract` 已明确说明：
  - ineffective 因子会被自动降权或拒绝
  - 服务端主链会强制执行守门规则
- `compose_response_contract` 与 `compose_from_brief_response_contract` 已新增守门相关字段声明

真实边界：

- capabilities 已与当前执行规则同步，但后续如果阈值变化，仍需同步更新文档契约

### 7.3 本轮实际改动文件

- 新增 [compose_factor_policy.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/compose_factor_policy.py)
- 修改 [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
- 修改 [strategy_composer.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/strategy_composer.py)
- 修改 [test_upgrade_workflow.py](/srv/projects/ashare-system-v2/tests/test_upgrade_workflow.py)

### 7.4 本轮验证结果

已执行并通过：

- `python -m py_compile src/ashare_system/strategy/compose_factor_policy.py`
- `python -m py_compile src/ashare_system/apps/runtime_api.py`
- `python -m py_compile src/ashare_system/strategy/strategy_composer.py`
- `python -m py_compile tests/test_upgrade_workflow.py`
- `python -m unittest tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_compose_from_brief_applies_factor_policy_and_returns_policy_trace`
- `python -m unittest tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_compose_direct_request_rejects_noise_heavy_factor_mix`
- `python -m unittest tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_compose_same_dimension_is_not_hard_limited`
- `python -m unittest tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_evaluation_panel_exposes_outcome_attribution_for_unsupported_factors`
- `python -m unittest tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_compose_from_brief_builds_compose_request_for_agent`
- `python -m unittest tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_compose_direct_request_respects_runtime_policy_for_strategy_assets`
- `python -m unittest tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_factor_and_playbook_catalog_endpoints_expose_seed_assets`
- `python -m unittest tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_evaluation_panel_exposes_factor_playbook_and_combo_ledgers`

收口回归：

- 上述 8 个关键测试已做组合回归，结果 `OK`

### 7.5 当前仍未完成的真实缺口

- P1 未完成：
  - 分 market regime 的因子有效性
  - 战法有效性监控
  - learned asset 与当前有效性联动
  - 因子选择理由结构化到 contract 字段
  - 宏观 / 筹码 / 成长质量维度继续扩充
  - Agent 感知层继续扩到持仓、讨论、执行容量一体化感知
- P2 未完成：
  - 跨市场因子
  - 另类数据因子
  - PCA / 因子降维 / 动态正交增强
  - Elo pairwise / 分角色对战制
  - 长样本参数标定与版本治理

### 7.6 结论

本轮不是只补文档，而是把“因子有效性展示层”升级成了“compose 主链守门层”。  
当前 Agent 仍不是完全自由推理，但已经从“可以无成本乱选因子”收敛到“可选，但要接受服务端强制压权、剔除、复核和拒绝”。

---

## 5. P1 任务清单

### P1-1 分 market regime 的因子有效性

#### 目标

从“最近 20 天全局有效否”升级到“当前市场状态下有效否”。

#### 改动文件

- [factor_monitor.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/factor_monitor.py)
- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py)

#### 代码补全点

1. 有效性快照按以下维度分桶：
   - `market_phase`
   - `trade_horizon`
   - `objective_family`
2. compose 时优先读取“当前 regime 对应快照”。

#### 验收标准

1. 同一个因子在 `trend` 与 `range` 下可得出不同有效性状态。
2. compose 使用的是当前 regime 快照，不是全局平均。

---

### P1-2 战法有效性监控

#### 目标

不仅因子受监管，playbook 也要受监管。

#### 改动文件

- [strategy_composer.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/strategy_composer.py)
- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
- 建议新增 [playbook_monitor.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/playbook_monitor.py)

#### 代码补全点

1. 生成 `playbook_effectiveness` 快照。
2. 当前 regime 下失效的 playbook 自动降权或受限。

#### 验收标准

1. playbook 不再被永远视为“默认可信”。
2. compose 结果能解释当前选中的战法是否处于有效状态。

---

### P1-3 learned asset 与当前有效性联动

#### 目标

防止历史学习产物在错误 regime 下把当前组合拉偏。

#### 改动文件

- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
- [strategy_composer.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/strategy_composer.py)

#### 代码补全点

1. learned asset 提供的 `factor_weight_bias` 进入前先过有效性规则。
2. 若 learned asset 推高的是当前 `ineffective` 因子，必须被削弱或拦截。

#### 验收标准

1. learned asset 不能把当前失效因子重新抬成高权重。
2. 结果里能说明 learned asset 对本轮组合的真实影响。

---

### P1-4 因子选择理由结构化

#### 目标

让 Agent 不只是提交 `id + weight`，而要说明每个因子补充了什么信息。

#### 改动文件

- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
- 相关 compose contract 定义文件

#### 代码补全点

给 `factor_specs` 增加可选字段：

- `role`
- `dimension`
- `reason`

#### 验收标准

1. 每个被选因子都能说明“补充了哪个盲点”。
2. 两个高度同质因子不能再用空理由混过去。

---

### P1-5 维度继续补齐：宏观 / 筹码 / 成长质量

#### 目标

在已有基础上，把“当前能用”继续推进到“维度更完整”。

#### 改动文件

- [factor_registry.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/factor_registry.py)
- 相关因子实现文件

#### 建议新增因子

成长质量：

- `roe_trend`
- `revenue_acceleration`
- `gross_margin_expansion`
- `analyst_revision_score`

#### 验收标准

1. 成长质量不能再由估值因子代替。
2. capabilities 中维度覆盖表能反映新增结果。

---

### P1-6 Agent 感知层扩展

#### 目标

把 Agent 感知层从“因子有效性 + 任务画像”扩展成真正决策前上下文。

#### 改动文件

- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
- [system_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py)

#### 感知信息建议补齐

- 当前持仓状态
- 当前讨论状态
- 当前执行容量
- 当前风控边界
- 当前市场阶段

#### 验收标准

1. Agent 不再需要自己拼多个接口才能理解当前局面。
2. compose 前关键信息可以一次读全。

---

## 6. P2 任务清单

### P2-1 跨市场联动因子

#### 建议方向

- `hs_premium_discount`
- `commodity_sector_link`
- `us_tech_overnight`

#### 目标

让系统能感知 A 股以外的风险偏好与映射信号。

---

### P2-2 另类数据因子

#### 建议方向

- 搜索热度
- 舆情情绪
- 公告语义评分

#### 目标

引入传统量价之外的补充信息源，但要晚于 P0/P1 基础闭环。

---

### P2-3 PCA / 因子降维 / 组内正交增强

#### 目标

把当前“规则型去重”升级到更高阶的统计层正交。

#### 改动文件

- [strategy_composer.py](/srv/projects/ashare-system-v2/src/ashare_system/strategy/strategy_composer.py)
- 新增因子相关性分析模块

---

### P2-4 Elo 对战制增强

#### 目标

从结果增量映射走向更严格的 pairwise 或分角色对战。

#### 改动文件

- [agent_rating.py](/srv/projects/ashare-system-v2/src/ashare_system/learning/agent_rating.py)
- [settlement.py](/srv/projects/ashare-system-v2/src/ashare_system/learning/settlement.py)

---

### P2-5 参数长期校准与版本治理

#### 目标

让关键参数不再只靠经验值，而有长样本支持与版本可追溯。

#### 覆盖对象

- `composite_multiplier`
- 集中度阈值
- 无效因子预算
- 相关组上限

---

## 7. 建议新增测试

建议新增测试文件：

- `tests/test_runtime_compose_policy.py`
- `tests/test_runtime_capabilities_policy.py`
- `tests/test_strategy_composer_factor_health.py`
- `tests/test_risk_rule_trace.py`
- `tests/test_agent_rating_settlement_trace.py`

建议覆盖：

1. `ineffective` 因子自动降权
2. 直接 `compose` 不可绕过
3. 组合集中度触发 `needs_review` 或阻断
4. 同相关组压权
5. `factor_portfolio_health` 输出正确
6. `unsupported_for_monitor` 不被误罚
7. 风控四项新增参数命中链路可见
8. discussion 与 outcome 结算不会重复喂数

---

## 8. 总体验收标准

### 8.1 P0 完成标准

1. Agent 即使不主动读 `capabilities`，服务端也会强制执行因子选择规则。
2. 多因子组合不再以“数量”论英雄，而以“有效 + 分散 + 可解释”为准。
3. 风控规则接线、Elo 喂数、multiplier 解释口径均进入主链。
4. compose 结果与审计日志可完整解释一次自动干预。

### 8.2 P1 完成标准

1. 感知层不再是静态目录，而是能反映当前 regime 与持仓状态的动态上下文。
2. 因子与战法有效性都能随市场状态变化。
3. learned asset 不再越权覆盖当前真实市场状态。

### 8.3 P2 完成标准

1. 因子体系具备更强的跨市场与另类数据感知能力。
2. 排序参数、正交化与 Elo 治理开始进入更高阶统计与长期校准阶段。

---

## 9. 推荐实施顺序

1. `P0-1` 因子有效性强制注入 compose 主链
2. `P0-2` 因子组合约束引擎
3. `P0-3` 因子组合健康度评分
4. `P0-4` 解释层与审计层回显
5. `P0-5` 风控规则主链复核
6. `P0-6` Elo 自动喂数闭环补强
7. `P0-7` multiplier 保守化与校准入口
8. `P0-8` capabilities 契约对齐
9. `P1-1 ~ P1-6`
10. `P2-1 ~ P2-5`

---

## 10. 最终判断

当前系统真正缺的，不是“更多因子名字”，而是“更强的组合约束与执行闭环”。  

如果不完成 P0，Agent 再会调用接口，也可能只是“看过能力目录后继续凭感觉选”。  
如果完成 P0，系统才会从“Agent 有引导的自主选择”升级到“Agent 在程序约束下做有质量的自主选择”。
