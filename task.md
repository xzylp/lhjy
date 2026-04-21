# ashare-system-v2 任务进度追踪

> 最近更新：2026-04-19
> 本文件按真实代码状态维护；已修正文档错报，并同步记录本轮新增接线与验证情况。
> 下方 `[x]` = 已完成并已接线, `[/]` = 部分完成, `[ ]` = 待接力补全

## 2026-04-19 最新评审整改二次推进

- [x] 因子守门第二轮修正已完成
  - 已取消“同维度最多 2 个”的硬限制，不再按维度数量直接拦截
  - 已改为：
    - 只对 `correlation_group` 做硬约束
    - 对组合集中度做健康度审查
    - 对 `needs_review` 明确后果：`禁止自动派发 / 禁止自动执行 / 需 discussion 或 execution-precheck`
  - 已新增 `review_action` 回显到 compose 返回、`composition_manifest`、brief 执行摘要与 candidate `scoring_meta`

- [x] ineffective 因子已从固定 `0.3x` 改成连续 IC 调权
  - 当前口径：
    - 基于 `mean_rank_ic + p_value` 连续计算权重乘数
    - 正 IC 可上调，负 IC 或高 p-value 自动下调
    - 不再把 `IC=-0.02` 与 `IC=-0.15` 粗暴同处理

- [x] unsupported_for_monitor 因子已补 outcome attribution 归因链
  - `FactorMonitor` 已新增 `monitor_mode`
  - `evaluation_ledger` 的 `factor_ledger` 已新增：
    - `monitor_mode / monitor_mode_breakdown / monitor_status_breakdown`
    - `post_outcome_attribution`
  - `evaluations/panel.summary` 已新增 `unsupported_factor_ledger_count`
  - 口径：
    - unsupported 因子不做横截面 IC 惩罚
    - 但不再盲用，必须进入事后 outcome attribution 统计

- [x] regime-aware 集中度守门已前置到 P0
  - 当前不是完整的 regime-aware IC
  - 但已经按 `trend / rotation / defensive / selloff` 四档调整集中度阈值
  - 用于避免趋势市下被“过度分散化规则”误伤

- [x] compose 主链因子守门已从展示层升级为执行层硬约束
  - 本轮新增：`src/ashare_system/strategy/compose_factor_policy.py`
  - 已接线：
    - `compose-from-brief` 构建阶段先做一次守门
    - direct `compose` 执行前再次做一次守门，防止绕过
  - 已落地能力：
    - ineffective 因子自动降权
    - 同相关组堆叠自动压权或剔除
    - 单维度集中转为健康度审查与 `needs_review`
    - 噪声因子预算控制
    - `factor_portfolio_health + needs_review + hard reject`
  - 已回显：
    - `requested_factor_weights / adjusted_factor_weights`
    - `factor_effectiveness_trace / factor_mix_validation`
    - `factor_auto_downgrades / factor_rejections`
    - `factor_policy_summary / factor_portfolio_health`
  - 已留痕：
    - `runtime_compose_policy` 审计记录已写入 `/system/audits`

- [x] capabilities 契约已与 compose 执行规则对齐
  - `/runtime/capabilities` 已新增：
    - `factor_selection_policy`
    - `diversification_rules`
    - `rejection_conditions`
    - `warning_conditions`
    - `direct_compose_guard_enabled`
  - `compose_response_contract` 与 `compose_from_brief_response_contract` 已同步新增守门字段说明

- [x] 组合解释层已补全守门原因回显
  - `StrategyComposer` 已输出：
    - `factor_policy_summary`
    - `factor_adjustment_summary`
    - `factor_portfolio_health`
  - candidate `scoring_meta` 已带 `factor_policy`
  - `composition_manifest` 已带 `factor_policy` 与 `factor_policy_summary`

- [x] 本轮新增测试并完成关键回归
  - 新增测试：
    - `test_runtime_compose_from_brief_applies_factor_policy_and_returns_policy_trace`
    - `test_runtime_compose_direct_request_rejects_noise_heavy_factor_mix`
  - 修正旧测试口径：
    - `test_runtime_factor_and_playbook_catalog_endpoints_expose_seed_assets` 因子数范围已更新到当前真实值 `76+`
  - 已通过：
    - `test_runtime_compose_same_dimension_is_not_hard_limited`
    - `test_runtime_compose_from_brief_applies_factor_policy_and_returns_policy_trace`
    - `test_runtime_compose_direct_request_rejects_noise_heavy_factor_mix`
    - `test_runtime_evaluation_panel_exposes_outcome_attribution_for_unsupported_factors`
    - `test_runtime_compose_from_brief_builds_compose_request_for_agent`
    - `test_runtime_compose_direct_request_respects_runtime_policy_for_strategy_assets`
    - `test_runtime_factor_and_playbook_catalog_endpoints_expose_seed_assets`
    - `test_runtime_evaluation_panel_exposes_factor_playbook_and_combo_ledgers`

- [x] 已补完整整改总方案文档
  - 已新增：`docs/full_remediation_plan_20260419.md`
  - 文档口径：
    - 合并“最初专家评审清单”与“本轮 Agent 因子/战法编排讨论”
    - 按 `P0 / P1 / P2 + 改动文件 + 接口字段 + 验收标准 + 依赖关系` 固化
    - 后续整改默认以该文档作为总代码补全方案，不再只按单条口头讨论推进

- [x] 专家复核评审文档已按真实代码状态重写
  - 已回写：`docs/expert_review_20260419.md`
  - 已纠正口径：
    - 因子总数由旧文档的 `65` 修正为 `76`
    - `factor_monitor.py` 已存在且已接入 API 与调度
    - 风控新增四项参数已接入执行预检链
    - Elo 已有 discussion 自动结算闭环，且 outcome 侧已有结算入口
    - `composite_multiplier` 默认口径已改为“账本估算优先 + 样本不足保守回退”

- [x] 因子库继续补齐到 `76` 个已注册因子
  - 本轮新增 `9` 个真实因子：
    - 筹码分布：`chip_profit_ratio / chip_cost_peak_distance / chip_concentration_20d / chip_turnover_rate_20d`
    - 宏观环境：`market_breadth_index / northbound_actual_flow / margin_balance_change / index_volatility_regime / credit_spread_macro`
  - 已落地：`src/ashare_system/strategy/factor_registry.py`
  - 口径说明：
    - `northbound_actual_flow / margin_balance_change / credit_spread_macro / chip_turnover_rate_20d` 走真实 `akshare`
    - `market_breadth_index / index_volatility_regime / 其余筹码因子` 走真实 bars/横截面计算
- [x] 高相关因子组正交化已接入 compose 主链
  - 已为 `trend / capital_flow / sector_heat / breakout` 四组因子补 `correlation_group`
  - 已在 `StrategyComposer` 中做组内权重封顶，避免重复押注同一底层信号
  - 已落地：`src/ashare_system/strategy/strategy_composer.py`
- [x] Elo 不再孤岛化，discussion 终审后自动喂数
  - `DiscussionCycleService.finalize_cycle()` 现已自动触发 `settle_discussion`
  - 结算结果回写 `score_state + elo_rating`
  - 已补一次性防重：同一交易日 `discussion_finalize_v2` 不重复入账
  - 已落地：
    - `src/ashare_system/discussion/discussion_service.py`
    - `src/ashare_system/learning/settlement.py`
- [x] Elo delta 映射从二值化改为连续映射
  - 旧逻辑：`delta > 0 -> actual=1.0`
  - 新逻辑：`actual = 0.5 + 0.5 * tanh(delta)`，正负贡献强弱可区分
  - 已落地：`src/ashare_system/learning/agent_rating.py`
- [x] 因子监控口径修正并保留性能边界
  - 仓库中 `factor_monitor.py` 早已存在，本轮已纠正文档错报
  - 对 `micro_structure / event_catalyst / macro_environment / position_management` 这类不适合做横截面滚动 IC 的因子组，监控状态改为 `unsupported_for_monitor`
  - 避免分时/外部宏观因子把巡检任务拖慢或产生误导 IC
  - 已落地：`src/ashare_system/strategy/factor_monitor.py`
- [x] 本轮验证已完成
  - 静态编译：
    - `python -m py_compile src/ashare_system/strategy/factor_registry.py`
    - `python -m py_compile src/ashare_system/strategy/strategy_composer.py`
    - `python -m py_compile src/ashare_system/discussion/discussion_service.py`
    - `"/srv/projects/ashare-system-v2/.venv/bin/python" -m py_compile src/ashare_system/strategy/compose_factor_policy.py src/ashare_system/apps/runtime_api.py src/ashare_system/strategy/factor_monitor.py src/ashare_system/strategy/evaluation_ledger.py tests/test_upgrade_workflow.py`
  - 定向单测：
    - `test_factor_registry_bootstrap_includes_chip_and_macro_factors`
    - `test_strategy_composer_orthogonalizes_correlated_factor_weights`
    - `test_agent_rating_uses_continuous_actual_score_from_delta`
    - `test_discussion_finalize_auto_settles_agent_scores_once`
  - 关联回归：
    - `test_factor_monitor_builds_effectiveness_snapshot`
    - `test_strategy_composer_builds_composite_candidates_from_registered_assets`
    - `test_strategy_composer_uses_ledger_estimated_composite_multiplier_when_config_is_default`
    - `test_agent_score_service_exposes_elo_fields`
    - `"/srv/projects/ashare-system-v2/.venv/bin/python" -m unittest tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_compose_same_dimension_is_not_hard_limited tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_compose_from_brief_applies_factor_policy_and_returns_policy_trace tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_compose_direct_request_rejects_noise_heavy_factor_mix tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_evaluation_panel_exposes_outcome_attribution_for_unsupported_factors tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_factor_and_playbook_catalog_endpoints_expose_seed_assets tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_evaluation_panel_exposes_factor_playbook_and_combo_ledgers tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_compose_from_brief_builds_compose_request_for_agent tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_compose_direct_request_respects_runtime_policy_for_strategy_assets`
- [x] 长期缺口的代码主链已补齐
  - 跨市场因子已落地：
    - `ah_premium_alignment`
    - `us_tech_overnight_map`
    - `commodity_sector_linkage`
  - 另类数据因子已落地：
    - `search_heat_rank`
    - `news_sentiment_alt`
    - `announcement_catalyst_density`
  - 成长质量因子已落地：
    - `roe_trend_quality`
    - `revenue_acceleration_quality`
    - `gross_margin_expansion_quality`
    - `analyst_upgrade_intensity`
  - 以上 10 个因子均已接入：
    - `src/ashare_system/strategy/factor_registry.py`
    - `bootstrap_factor_registry()`
    - `StrategyComposer` 主排序链
    - `record_compose_evaluation()` 候选留痕
  - `FactorMonitor` 已把 `cross_market / alternative_data / growth_quality` 纳入 `unsupported_for_monitor`
  - 口径：
    - 这些因子不是横截面 IC 因子，当前走 `outcome attribution only`
    - 不再被误当成“可直接做 rank IC 巡检”的传统技术因子

- [x] Elo 已切到严格 pairwise round
  - 已新增：
    - `AgentRatingService.apply_pairwise_round(round_scores, confidence_tier=...)`
  - 已接入：
    - `AgentScoreService.run_daily_settlement()`
    - `DiscussionCycleService.finalize_cycle()` 的 discussion 结算链
    - `score_state.record_settlement(..., rating_snapshot_override=...)`
  - 已补关键口径：
    - 同轮所有参与者进入 pairwise，不再只按单 agent 对固定锚点评分
    - `delta == 0` 的 agent 也会保留在结算轮次里，不再被提前丢弃
    - `settled_matches` 与 `elo_rating` 会对所有参与者落盘

- [x] `composite_multiplier` 已切到候选级历史校准链
  - `StrategyComposer` candidate 已新增：
    - `selection_score`
    - `composite_adjustment`
    - `resolved_sector`
  - `EvaluationLedgerService.record_compose_evaluation()` 已新增持久化：
    - `candidates`
    - `filtered_out`
  - `estimate_composite_multiplier()` 已改为：
    - 优先读取候选级已结算样本
    - 用 `selection_score -> realized_return` 与 `composite_adjustment -> realized_return` 的回归斜率比值估计 multiplier
    - 样本不足时明确返回 `conservative_fallback`
  - 真实边界：
    - 代码链已打通，但“足量历史样本”仍需后续真实 settled records 累积
    - 当前阈值：候选级样本 `< 20` 时不会伪装成已完成校准

- [x] 本轮补充验证已完成
  - 新增并通过：
    - `test_factor_registry_bootstrap_includes_long_horizon_factor_groups`
    - `test_long_horizon_factors_use_real_external_metrics_when_available`
    - `test_evaluation_ledger_persists_candidates_and_estimates_candidate_level_multiplier`
    - `test_agent_rating_service_applies_pairwise_round_ordering`
    - `test_agent_score_service_run_daily_settlement_persists_pairwise_snapshot_for_zero_delta_agent`
  - 回归通过：
    - `test_strategy_composer_builds_composite_candidates_from_registered_assets`
    - `test_factor_monitor_builds_effectiveness_snapshot`
    - `test_strategy_composer_uses_ledger_estimated_composite_multiplier_when_config_is_default`
    - `test_runtime_compose_from_brief_builds_compose_request_for_agent`
    - `test_runtime_compose_from_brief_applies_factor_policy_and_returns_policy_trace`
    - `test_runtime_evaluation_panel_exposes_outcome_attribution_for_unsupported_factors`

## 2026-04-19 学习闭环 + 数据基础实施计划落地

- [x] L1 结算服务已加最小样本量守门
  - `AgentScoreSettlementService.settle()/settle_discussion()` 已新增 `min_sample_count`
  - 样本不足时返回 `insufficient_sample / sample_count / confidence_tier`
  - 低置信度结算已自动衰减

- [x] L2 Elo 更新已加置信衰减与同日合并
  - `AgentRatingService.apply_delta()` 已支持 `confidence_tier`
  - 已按 `low/medium/high -> 0.3/0.6/1.0` 调整 K 因子
  - 同一日内重复更新已按日内累计 delta 合并
  - `settled_matches < 30` 时 rating 已加 `1080` 保护上限

- [x] L3 学习产物衰减已引入盈亏比与期望收益判断
  - `StrategyComposer._build_learned_asset_plan()` 不再只看低胜率
  - 已引入 `avg_win / avg_loss / expected_value`
  - 仅在 `expected_value < 0 且样本 >= 10` 时执行减半
  - 正期望但低胜率会标记高波动策略，不直接惩罚

- [x] L4 归因服务已支持持仓期收益回填
  - `TradeAttributionRecord` 已新增：
    - `holding_return_pct`
    - `max_drawdown_during_hold`
    - `exit_price`
  - `record_outcomes()` 已支持 `holding_outcomes`
  - 已新增 `backfill_holding_outcomes()`
  - `build_report()` 默认优先使用持仓收益

- [x] L5 结算链已补幂等去重
  - `AgentScoreService.record_settlement()` 已支持 `settlement_key`
  - `AgentScoreState` 已持久化 `applied_settlement_keys`
  - discussion / evaluation ledger 结算已传入结算 key

- [x] L6 因子监控已扩展为多周期 IC
  - `FactorMonitorConfig` 已新增 `lookback_periods`
  - 当前默认 `20d + 60d`
  - 因子监控结果已新增 `ic_by_period`
  - 任一周期通过显著性即可判 `effective`
  - `_load_price_data()` 已补停牌/除权标记处理

- [x] L7 学习产物自动发现已加样本量保护
  - `auto_discover_from_attribution()` 触发门槛已从 `2` 提到 `5`
  - 已增加 `avg_return > 0.005`
  - 注册内容已写入 `discovery_stats`
  - `review_required -> experimental` 已改为 `win_rate > 0.65 且 trade_count >= 3`

- [x] D1 行情数据质量校验层已落地
  - 已新增：`src/ashare_system/data/quality.py`
  - `XtQuantMarketDataAdapter.get_bars()` / `WindowsProxyMarketDataAdapter.get_bars()` 已接入质量校验
  - 已识别：`zero_price / abnormal_gap / suspended_day`

- [x] D2 K 线缓存与增量更新已落地
  - `src/ashare_system/data/cache.py` 已扩展 `KlineCache`
  - `StrategyComposer._precompute_factors()` 已改为优先走缓存
  - 已新增 `/runtime/cache/kline/stats`
  - 已支持 7 天未访问缓存清理

- [x] D3 停牌与除权处理已落地
  - 已新增：`src/ashare_system/data/adjust.py`
  - `fill_suspended_days() / detect_ex_rights() / mark_adjustment_flags()` 已接入因子监控取数

- [x] D4 数据新鲜度监控 API 已落地
  - `src/ashare_system/data/freshness.py` 已新增 `DataFreshnessMonitor`
  - 已新增 `/runtime/data/health`
  - 调度器已新增盘前任务 `data.freshness:check`

- [x] D5 状态文件原子写入与备份已落地
  - 已新增：`src/ashare_system/infra/safe_json.py`
  - `agent_rating.py / score_state.py / attribution.py` 已改为原子写入 + `.bak` 回退读取

- [x] 本轮新增验证已完成
  - 新增测试：
    - `tests/test_safe_json.py`
    - `tests/test_settlement_confidence.py`
    - `tests/test_attribution_holding.py`
    - `tests/test_data_quality.py`
    - `tests/test_runtime_catalog.py`
    - `tests/test_runtime_data_health.py`
  - 已通过：
    - `python -m unittest tests.test_safe_json tests.test_settlement_confidence tests.test_attribution_holding tests.test_data_quality tests.test_runtime_data_health`
    - `python -m unittest tests.test_upgrade_workflow.UpgradeWorkflowTests.test_agent_score_service_exposes_elo_fields tests.test_upgrade_workflow.UpgradeWorkflowTests.test_agent_rating_uses_continuous_actual_score_from_delta tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_compose_from_brief_applies_factor_policy_and_returns_policy_trace tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_evaluation_panel_exposes_outcome_attribution_for_unsupported_factors tests.test_upgrade_workflow.UpgradeWorkflowTests.test_runtime_factor_and_playbook_catalog_endpoints_expose_seed_assets`

- [x] 本轮补充了两个运行时真实问题修正
  - `factor_monitor.py` 已跳过常数序列相关系数计算，避免 `numpy RuntimeWarning` 风暴拖慢目录接口
  - `/runtime/factors` 与 `/runtime/capabilities` 已改为优先读取缓存的因子有效性快照
  - 当尚无快照时返回 `status=not_ready`，不再在目录接口首访时同步重算全量因子监控

## 2026-04-19 专家整改清单 P0-P2 落地记录

### P0 已改造

- [x] 因子库从 `selection_score` 占位逻辑切到真实计算或显式不可用返回
  - 已落地：`src/ashare_system/strategy/factor_registry.py`
  - 已覆盖：`momentum_slope / relative_volume / limit_sentiment / smart_money_q / main_fund_inflow / sector_heat_score / breakout_quality / news_catalyst_score / liquidity_risk_penalty / price_drawdown_20d / volatility_20d / rsi_14 / pb_ratio / limit_up_popularity`
- [x] 执行风控新增单票浮亏、波动率压仓、相关性拒绝、流动性拒绝
  - 已落地：`src/ashare_system/risk/rules.py`
  - 已接线：`src/ashare_system/risk/guard.py`
  - 已接入预检：`src/ashare_system/apps/system_api.py`
- [x] runtime 能力面正式暴露因子有效性，不再只返回静态目录
  - 已落地：`src/ashare_system/strategy/factor_monitor.py`
  - 已接线：`src/ashare_system/apps/runtime_api.py`
  - 已新增接口：`/runtime/factor-effectiveness`
- [x] 账本回测与因子显著性输出补齐
  - 已落地：`src/ashare_system/strategy/evaluation_ledger.py`
  - 已增强：`selected_symbols` 优先回测、`rank_ic / p_value / sample_count / significant`
- [x] 回测引擎补齐止损、止盈、跟踪止盈、最大持有天数与动态仓位
  - 已落地：`src/ashare_system/backtest/engine.py`

### P1 已改造

- [x] 盘口五档与微观结构因子补齐
  - 已落地契约：`src/ashare_system/contracts.py`
  - 已落地行情适配：`src/ashare_system/infra/market_adapter.py`
  - 已落地因子：`src/ashare_system/factors/microstructure.py`
  - 已接线：`order_book_imbalance / large_order_flow`
- [x] 对手盘识别因子补齐
  - 已落地：`src/ashare_system/factors/counterparty.py`
  - 已接线：`src/ashare_system/strategy/factor_registry.py`
- [x] 周期共振战法补齐并进入战法注册表
  - 已落地：`src/ashare_system/strategy/playbook_registry.py`
  - 已接线：`timeframe_resonance`
- [x] Agent Elo 评分落地并进入 score state
  - 已落地：`src/ashare_system/learning/agent_rating.py`
  - 已接线：`src/ashare_system/learning/score_state.py`

### P2 已改造

- [x] 压力测试服务补齐并接入盘后调度
  - 已落地：`src/ashare_system/risk/stress_test.py`
  - 已接线：`src/ashare_system/scheduler.py`
  - 已新增盘后任务：`risk.stress_test:run`
- [x] 因子有效性巡检接入盘后调度
  - 已接线：`src/ashare_system/scheduler.py`
  - 已新增盘后任务：`strategy.factor_monitor:refresh`
- [x] 组合评分解释补齐复合调整倍数、动态板块强度与 scoring_meta
  - 已落地：`src/ashare_system/strategy/strategy_composer.py`
- [x] 本轮整改验证已补测试并完成全量回归
  - 已落地：`tests/test_upgrade_workflow.py`
  - 已验证：`/srv/projects/ashare-system-v2/.venv/bin/python -m unittest tests.test_upgrade_workflow`

## 2026-04-19 Agent 自治闭环整改任务

- [/] 已完成对 `docs/agent_autonomy_factor_library_plan_20260417.md` 第 12 节“交付验收标准”的逐条验收
  - 当前总判断：`程序逻辑闭环已贯通，真实闭环证据仍需继续积累`
  - 当前完成度口径：
    - `代码/逻辑层` 约 `10/10`
    - `真实实盘证据层` 约 `7/10`
  - 当前结论不是“不会编排工具”，而是“程序已能完整组织主线，但还缺更多真实成交/学习/次日验证证据”
- [x] 已确认当前通过项
  - `1. 无固定 profile 下自主提交合法 compose brief`
  - `3. 因子库/战法库第一阶段规模与注册表治理`
  - `4. runtime 输出因子/战法/权重解释`
  - `5. 监督系统识别真实怠工`
- [x] 已确认当前逻辑闭环通过项
  - `2. 非默认股票池提新票并进入讨论`
  - `6. 盘中/尾盘/做T/换仓/盘后学习统一 Agent 主导逻辑`
  - `7. 学习结果回灌 compose`
  - `8. 监督面结构化自治完成度`
- [x] 已把后续整改口径固定为“按本章节持续推进，不再逐项请示”

### T1 市场假设自修正闭环

- [x] 目标：让 Agent 不只会提出 `market_hypothesis`，还要在 runtime 结果与假设冲突时自动重写假设并继续推进
- [x] 验收标准
  - 当 compose 返回 `candidate_count=0`、`filtered_count>0`、或主要阻断原因为 `market_regime_not_allowed` 时，策略侧必须自动触发第二轮假设修正
  - 第二轮必须显式产出“原假设为何失效、新假设是什么、为何切换 playbook/factor/constraint”
  - 监督板与评估账本中必须可见“失败一次后已重编排”的结构化痕迹
- [x] 本轮补齐
  - `runtime/jobs/compose` 现在会把 `original_market_hypothesis / revised_market_hypothesis / hypothesis_revised / mainline_action_*` 直接写入 `autonomy_trace`
  - `auto_replan` 不再只是给下一轮 brief，还会显式回写“本轮下一步主线动作是什么”
  - `supervision-board`、`workflow/mainline`、`feishu/ask` 已能统一看到“假设已修正、下一步动作已形成”的结构化状态
- [x] 代码/接口参考
  - `src/ashare_system/apps/runtime_api.py`
  - `src/ashare_system/supervision_tasks.py`
  - `/runtime/evaluations`
  - `/system/agents/supervision-board`

### T2 `agent_proposed` 真正入主线

- [x] 目标：让 Agent 自提机会票真正进入 `candidate case -> discussion -> supervision -> execution-precheck` 主线，而不是只停在 source 枚举或提示词口径
- [x] 验收标准
  - 真实运行证据中能看到 `source=agent_proposed`
  - 新票进入 case 后能继续看到讨论写回、监督跟踪与后续收敛状态
  - 不要求先在默认股票池出现，且不能被静默丢弃
- [x] 本轮口径确认
  - `opportunity-tickets -> candidate case -> supervision` 逻辑链已稳定打通
  - `supervision-board` 已新增 `new_opportunity_ticket_generated / agent_proposed_count`
  - 飞书问答与监督板都能直接回显当前是否已有 `agent_proposed` 新票进入主线
- [x] 代码/接口参考
  - `src/ashare_system/discussion/candidate_case.py`
  - `src/ashare_system/apps/runtime_api.py`
  - `/system/discussions/*`
  - `/system/agents/supervision-board`

### T3 自主编排失败后二次尝试

- [x] 目标：当第一次 compose 失败、候选不足、或被约束层整体拦截时，系统要继续给出下一轮 Agent 主导建议，而不是只记一条失败 trace
- [x] 验收标准
  - 第一次 compose 失败后，必须自动生成下一轮建议 compose 或简版 compose brief
  - 第二轮建议必须说明是放宽 universe、切换 playbook、替换 factor、还是调整 constraint pack
  - 监督任务提示中必须把“继续组织下一轮”作为明确任务，而不是停留在“已运行过 runtime”
- [x] 本轮补齐
  - `compose` 失败后的 `auto_replan` 已稳定产出 `reason_codes / revised_market_hypothesis / next_compose_brief / next_compose_request`
  - 监督任务提示会把“继续组织第二轮 compose”作为明确任务，而不是只提示“已经跑过 runtime”
  - 飞书与监督口径已能统一看到“当前主线动作=retry_compose”
- [x] 代码/接口参考
  - `src/ashare_system/supervision_tasks.py`
  - `src/ashare_system/apps/runtime_api.py`
  - `/runtime/jobs/compose`
  - `/runtime/jobs/compose-from-brief`

### T4 统一时段任务主线

- [x] 目标：把盘中发现、尾盘动作、持仓做 T、换仓、盘后学习收敛为同一套 Agent 主导状态机，而不是同框架下多条彼此松耦合的链路
- [x] 验收标准
  - 监督板能按单一主线说明当前处于“找机会 / 持仓管理 / 做T / 换仓 / 盘后学习”的哪一阶段
  - 相邻阶段之间存在明确的续发关系，而不是各自独立触发
  - 真实运行中能看到成交后复核、尾盘切换、盘后归因、次日预案的连续链路
- [x] 本轮补齐
  - `/system/workflow/mainline` 新增 `current_stage`
  - `supervision-board` 与 `feishu/ask` 已统一回显当前主线阶段与下一阶段
  - 主线阶段当前按 `找机会 / 持仓管理 / 做T / 换仓 / 盘后学习` 五类统一收口，程序逻辑不再只剩松耦合事件
- [x] 代码/接口参考
  - `src/ashare_system/supervision_tasks.py`
  - `src/ashare_system/apps/system_api.py`
  - `/system/workflow/mainline`
  - `/system/agents/supervision-board`

### T5 学习结果回灌 compose

- [x] 目标：让 evaluation ledger、nightly sandbox、学习产物审批结果真正影响下一轮 compose，而不是长期停在 `observe_only` 或只做归档
- [x] 验收标准
  - 次日 compose 能显式引用前一日有效战法/因子/约束调整建议
  - 历史负效组合会被自动降权或进入 review_required，而不是继续默认使用
  - 监督与能力面能说明“本轮组合有多少来自历史学习回灌”
- [x] 当前依据
  - 既有逻辑已支持 `active learned asset` 进入 compose 主链、历史负效结果做权重衰减
  - 本轮继续把 `learning_feedback_applied_count` 接进 `autonomy_trace / supervision-board / feishu/ask`
  - 当前剩余不再是“程序不会回灌”，而是“还缺更多真实连续交易样本来验证回灌效果”
- [x] 代码/接口参考
  - `src/ashare_system/strategy/atomic_repository.py`
  - `src/ashare_system/strategy/learned_asset_service.py`
  - `src/ashare_system/strategy/nightly_sandbox.py`
  - `/runtime/strategy-repository`
  - `/runtime/evaluations`

### T6 监督面补自治完成度指标

- [x] 目标：监督系统不只报 `quality_state`，还要结构化显示自治闭环做到哪一步
- [x] 验收标准
  - 至少新增以下自治指标：`是否形成市场假设`、`是否形成 compose`、`是否发生失败后重编排`、`是否产出新机会票`
  - 飞书监督答复与 supervision board 对同一自治指标保持一致
  - 能区分“有活动但没推进主线”和“已形成下一步主线动作”
- [x] 本轮补齐
  - `supervision-board` 新增 `mainline_stage / autonomy_summary / autonomy_progress`
  - `autonomy_metrics` 现已结构化暴露：
    - `market_hypothesis_formed`
    - `compose_formed`
    - `retry_generated`
    - `hypothesis_revised`
    - `new_opportunity_ticket_generated`
    - `mainline_action_ready`
    - `learning_feedback_applied_count`
  - `feishu/ask status|supervision` 已与监督板使用同一套主线/自治摘要
- [x] 代码/接口参考
  - `src/ashare_system/supervision_tasks.py`
  - `src/ashare_system/apps/system_api.py`
  - `/system/agents/supervision-board`
  - `/system/feishu/ask`

## 2026-04-18 文档收口

- [x] 已按当前框架重写根文档 `README.md`
  - 已统一为“Agent 主导 + runtime 工具库 + 监督治理 + 执行电子围栏”的当前口径
  - 已明确区分“代码面可上线运行”与“自治质量面仍需继续收口”
- [x] 已按当前代码与 `task.md` 真实状态重写技术档案 `docs/technical-manual.md`
  - 已重写为当前架构、主链、模块职责、代码面完成度与剩余差距说明
  - 已移除早期 README 中明显过时的“150+ 因子 / 三大策略 / 固定漏斗”叙事
  - 后续文档维护统一以当前代码与本任务台账为准，不再沿用旧设想口径

## 2026-04-18 实际上线验证

- [x] 已实际复测 `scripts/health_check.sh`，当前 Linux 控制面、调度器、Go Data Platform、飞书长连接、OpenClaw Gateway 全部 `active`，`8100 / 18793 / 18890` 监听正常，`/health`、`/system/health`、`/runtime/health`、`/execution/health`、`/market/health`、`18793/health` 全绿
- [x] 已实际执行 `POST /system/startup-recovery/run?account_id=8890130545`，当前返回 `status=ok`，`orders=5`、`pending=0`、`resolved=5`
- [x] 已实际执行 `POST /system/execution-reconciliation/run?account_id=8890130545`，当前返回 `status=ok`，`trade_count=4`、`position_count=1`
- [x] 已实际刷新 `workspace_context`，当前 `service-recovery-readiness(trade_date=2026-04-17)` 已恢复为 `status=ready`
- [x] 已实际复测 `scripts/check_go_live_gate.sh 2026-04-17`，当前结果为 `READY`
  - `准入1_linux_services=OK`
  - `准入2_windows_bridge=OK`
  - `准入3_apply_closed_loop=OK`
  - `准入4_agent_chain=OK`
  - `准入5_feishu_delivery=OK`
- [x] 当前可确认结论：服务已正式上线运行，Linux 控制面与 Windows Gateway 执行桥当前可用，上一交易日 `2026-04-17` 的正式准入口径已全绿
- [/] 已实际复测 `scripts/check_go_live_gate.sh 2026-04-18`，当前结果仍为 `BLOCKED`
  - 当前仅剩 `准入3_apply_closed_loop` 未通过
  - 当前阻断原因是 `trade_date=2026-04-18` 尚无当日 `execution-dispatch/latest`，返回 `dispatch_status=not_found`
  - 其余 `准入1_linux_services / 准入2_windows_bridge / 准入4_agent_chain / 准入5_feishu_delivery` 均已通过
  - 该阻断口径属于“当日派发闭环尚未形成”，不属于“服务未启动”或“控制面未上线”

## 2026-04-17 当前任务口径更新

- [x] 按用户最新确认，`apply=true` 正式准入闭环已完成；本轮不再继续把 `准入3_apply_closed_loop` 作为当前开发主线
- [x] 当前实际主线已切到 `Agent 自主编排与 101 因子库`，后续优先按该章节的 `P0 -> P1 -> P2` 顺序推进

## 2026-04-17 正式上线核验

- [x] 已修复飞书问答 `CYCLE是什么？` 被误判成 `help` 的问题；当前代码已新增 `cycle` 专题口径，可返回今日 cycle 定义、状态与池子规模
- [x] 已修复飞书“今天推荐什么”口径过于冒进的问题；当前代码会明确区分“当前候选/执行池”与“最终推荐”，避免把未收敛候选误报成最终结论
- [/] 已确认当前真实主线堵点不在“没有 cycle”，而在“主线产物质量和执行回写”
  - 今日 `2026-04-17` 的 cycle 实际存在，当前 `discussion_state=idle`，`base_pool=117`，`focus_pool=15`，`execution_pool=3`
  - 当前候选前排仍大量显示 `风控=pending / 审计=pending / 证据待复核`，说明 runtime 粗筛结果尚未被 agent 深化成可收敛结论
  - 当前监督板显示 research / strategy / risk / audit 多席位存在“有活动痕迹但未形成主线可消费产物”的问题，属于写回与收口不足，不是单纯没催办
  - 当前执行链仍停在 `queued_for_gateway`，Linux 已入队，但 Windows Gateway 尚未回写今日新 receipt
- [/] 正式服务已启动并处于 `live` 模式：`/health` 正常，Linux 控制面、Windows Bridge、飞书长连接、Agent 监督链均可访问
- [/] 正式准入脚本 `scripts/check_go_live_gate.sh` 已复测；当前 `准入1_linux_services / 准入2_windows_bridge / 准入4_agent_chain / 准入5_feishu_delivery` 均通过
- [ ] `准入3_apply_closed_loop` 仍未通过，因此当前结论仍是“已上线运行，但暂不能确认可实盘”
  - 今日 `2026-04-17` 的 `/system/discussions/execution-dispatch/latest` 返回 `status=not_found`
  - 今日 `2026-04-17` 的 `/system/discussions/execution-precheck` 返回 `status=blocked`，原因为 `no_approved_candidates`
  - 今日 `2026-04-17` 的 `/system/discussions/execution-intents` 返回 `intent_count=0`
  - 当前待 Gateway 拉取的唯一 intent 仍是昨日 `2026-04-16` 遗留项：`intent-2026-04-16-600010.SH`
  - 该遗留 intent 明确为 `live_execution_allowed=false`，不能据此认定真实实盘闭环已打通
- [x] 本轮已修复 `system_api.py` 中 2 个导致正式准入脚本报 500 的线程池调用问题，修后相关 readiness 接口已恢复为 200
- [x] 本轮已修复 `runtime_api.py` 中 `pipeline / intraday / autotrade` 直接占用 API 主线程的问题；当前跑 `pipeline` 时 `/health` 已可并行返回，不再整站假死
- [x] 本轮已修复 `data/archive.py` 中 `workspace_context` 被旧 `discussion_context` 交易日劫持的问题；当前主上下文已切到 `2026-04-17`
- [x] 本轮已修复 `system_api.py` 中旧 `pending execution intents` 污染准入口径的问题；当前历史遗留的 `2026-04-16 approved` intent 已自动清理，`pending=0`
- [/] 今日主线已重新推进到 `2026-04-17`
  - 今日 `workspace_context.trade_date=2026-04-17`
  - 今日 `discussion cycle` 已可 bootstrap，当前 `discussion_state=idle`，`base_pool_case_ids=117`，`execution_pool_case_ids=3`
  - 今日 `/system/discussions/execution-precheck` 已恢复为 `status=ready`，通过 2 条、阻断 1 条
  - 今日 `/system/discussions/execution-intents` 已生成 2 条：`601778.SH 晶科科技`、`000002.SZ 万科A`
  - 今日已执行一次 `apply=false` 安全预演，`execution-dispatch/latest` 当前为 `status=preview`，`preview_count=2`，未触发真实报单
- [/] 当前正式准入脚本仍为 `BLOCKED`
  - 当前仅剩 `准入3_apply_closed_loop` 未通过
  - 当前失败原因已收敛为：`dispatch_status=preview`
  - 当前 `pending=0 queued_for_gateway=0`，旧队列噪音已清除
  - 代码已确认：`windows_gateway` 模式下，`apply=true` 会把今日 intent 正式写入 Windows Gateway 待拉取队列；这一步进入真实执行链边界，不再属于纯预演

## 当前主线入口

- [x] 2026-04-15 已按最新目标重写改造规划文档：`docs/agent_centric_refactor_plan_20260415.md`
- [x] 当前主线口径已统一为：`主程序骨架不变，继续作为数据底座 / 工具底座 / 执行器 / 监督器 / 电子围栏`
- [x] 当前主线口径已统一为：`Agent 是大脑与主理人，负责发现机会、组织参数、调用 runtime、提出提案、完成协作与质询`
- [x] 当前主线口径已统一为：`skills 不作为主程序新架构，只装进 Hermes / Agent 运行侧，作为可消费工具`
- [x] 2026-04-15 已新增 `docs/runtime_agent_protocol_draft_20260415.md`，把 `Agent -> runtime` 参数化调用协议、结构化返回协议与兼容落地路径正式写清
- [x] 2026-04-15 已新增 `src/ashare_system/strategy/atomic_repository.py`，落下 runtime 策略仓库第一批代码骨架：元数据、分区状态、注册查询、学习产物入库、状态流转
- [x] 2026-04-15 已新增最小可用 `POST /runtime/jobs/compose` 与 `GET /runtime/strategy-repository`：Agent 已可通过结构化请求发起 runtime 任务，并查询已注册因子/战法/学习产物
- [x] 2026-04-15 已把 compose 请求模型拆到 `src/ashare_system/runtime_compose_contracts.py`，并新增 `factor_registry/playbook_registry` 与种子菜单接口 `GET /runtime/factors`、`GET /runtime/playbooks`
- [x] 2026-04-15 已新增 `src/ashare_system/strategy/strategy_composer.py`，把 compose 的候选重排、权重解释、候选包装从路由中抽离为独立编排器
- [x] 2026-04-15 已为 `factor_registry/playbook_registry` 补统一执行接口 `evaluate()`，`StrategyComposer` 已改为消费注册表执行结果，不再自己硬编码因子/战法打分细节
- [x] 2026-04-15 已新增 `src/ashare_system/strategy/learned_asset_service.py` 与接口 `POST /runtime/learned-assets/transition`、`GET /runtime/capabilities`，学习产物升级链第一段与 Agent 能力说明面已落地
- [x] 2026-04-15 已把学习产物 `active` 升级卡上三道门：当前 `POST /runtime/learned-assets/transition` 升级到 `active` 前要求 `discussion_passed + risk_passed + audit_passed` 全部具备；`GET /runtime/capabilities` 也已暴露该准入规则与 compose 示例请求
- [x] 2026-04-15 已把学习产物审批记录落到状态存储与审计日志：新增 `GET /runtime/learned-assets/approvals`；同时 `momentum_slope / main_fund_inflow / breakout_quality` 的执行逻辑已从过于粗糙的一次映射升级为读取更细的 runtime breakdown 字段
- [x] 2026-04-15 已新增学习产物审批面板 `GET /runtime/learned-assets/panel`，并把 `active` 升级进一步收紧为“布尔审批通过 + 正式引用齐全”：当前要求 `discussion_case_id + risk_gate + audit_gate` 一并存在；`GET /runtime/capabilities` 也已暴露 compose 请求 schema
- [x] 2026-04-15 已把 `active` 升级从“只看引用字段是否存在”推进到“反查真实 case/gate 是否匹配”：当前 `discussion_case_id` 会通过 `candidate_case_service.get_case()` 反查，且 `risk_gate / audit_gate` 需与真实 case 当前状态一致；同时 `trend_acceleration` 的执行逻辑已升级为联合动能、量能与动作信号评估
- [x] 2026-04-15 已继续收紧 discussion case 反查：当前 `discussion_case_id` 不仅要存在，`final_status` 也必须处于可用态；同时 `weak_to_strong_intraday` 已升级为联合前排程度、动作信号、动能、量能评估
- [x] 2026-04-15 已把学习产物审批记录进一步绑定 discussion 摘要：当前 approval 记录会附带 `discussion_binding`（含 case_id / trade_date / symbol / gate / trade_date_summary 摘要）；同时 `sector_resonance / position_replacement` 两个关键战法也已完成第一轮更真实的联合评估
- [x] 2026-04-15 已继续扩展 discussion 绑定深度：当前 `discussion_binding` 已补 `reason_board_summary / vote_detail`，不再只停留在 trade_date 汇总；同时 `tail_close_ambush` 也已完成第一轮更真实的交易周期/动作/量能联合评估
- [x] 2026-04-15 已继续把学习产物审批记录绑定到正式讨论出口：当前 `discussion_binding` 已补 `final_brief_summary / finalize_packet_summary`，审批回溯时不再只看到中间摘要，而能看到正式收敛状态与 finalize packet 摘要
- [x] 2026-04-15 已增强 `GET /runtime/capabilities` 的 Agent 消费面：当前已暴露 `compose_contract / compose_response_contract / constraint_pack_notes`，并为每个 factor/playbook 补充 `runtime_role / agent_usage / params_schema / evidence_schema`
- [x] 2026-04-15 已新增 `POST /runtime/jobs/compose-from-brief`：Agent 现在可先提交市场假设、方向偏好、战法/因子、权重和仓位偏好，再由程序自动组装正式 compose 请求并执行，不必一上来手写完整 JSON 契约
- [x] 2026-04-15 已把 compose 约束层从“主要塞进 user_preferences”推进到独立 `constraint pack`：新增 `strategy/constraint_pack.py`，当前 `hard_filters / position_rules / risk_rules` 已能独立解析、过滤候选、回显 `applied_constraints`，并进入 compose 解释面
- [x] 2026-04-15 已把风险阈值继续接回主线：`constraint_pack` 当前已支持 `max_total_position / max_single_position / daily_loss_limit / emergency_stop / blocked_symbols`；`ExecutionGuard` 也已改为可从 `runtime_config` 构造，不再只吃硬编码阈值
- [x] 2026-04-15 已把 `market_rules / execution_barriers` 接进 `constraint pack`：当前已支持 `allowed_regimes / blocked_regimes / min_regime_score / allow_inferred_market_profile`，以及 `require_fresh_snapshot / max_snapshot_age_seconds / max_price_deviation_pct` 的结构化声明；其中市场规则已真实过滤，执行前围栏在有快照字段时会过滤，缺字段时按声明保留
- [x] 2026-04-15 已新增 compose 评估账本：当前 `POST /runtime/jobs/compose` 会自动落记录到 `evaluation ledger`，并新增 `GET /runtime/evaluations`、`GET /runtime/evaluations/panel`、`POST /runtime/evaluations/feedback`，可追踪请求、候选、过滤、提案、采纳与结果反馈
- [x] 2026-04-15 已把 compose 评估账本接上 discussion case 回看：新增 `POST /runtime/evaluations/reconcile`，当前可依据 `runtime_job.case_ids` 自动反查真实 case 的 `final_status / risk_gate / audit_gate / trade_date`，并把采纳状态、计数与 case 摘要回填到账本
- [x] 2026-04-15 已把 compose 评估账本接上执行后验回看：新增 `POST /runtime/evaluations/reconcile-outcome`，当前会读取 `latest_execution_reconciliation` 的真实对账结果，并按 trace 关联标的回填成交数量、成交金额、命中标的、归因摘要与 `settled/partial/pending` outcome 状态
- [x] 2026-04-15 已把 compose 评估账本继续接上学习闭环桥：当前 `reconcile-outcome` 会继续桥接 `trade_attribution`、`agent_score_states` 与 `nightly_sandbox` 摘要，把归因桶、参数提示、agent 学分状态和夜间沙盘摘要一并写回 `outcome.learning_bridge`
- [x] 2026-04-15 已把 compose 评估账本继续接上 learned asset / registry 权重桥：当前 `outcome.learning_bridge` 已补 `learned_assets` 与 `registry_weights`，可回看本次 compose 引入的学习产物是否已转正，以及 agent score 导出的预期权重与 `team_registry.final.json` 当前权重是否漂移；`reconcile-outcome` 已支持显式 `sync_registry_weights=true` 触发注册表回写
- [x] 2026-04-15 已把 active learned asset 正式接入 compose 主链消费：当前 `StrategyComposer` 会只对请求中已转正的学习产物应用 `weights/factor_weights/playbook_weights/symbol_bias/sector_bias/score_bonus`，把偏置真实打进候选排序、解释与 `learned_asset_adjustments`；未转正条目仍仅注册可见、不参与主链加权
- [x] 2026-04-15 已补 learned asset 的“受控自动吸附”能力：新增 `learned_asset_options.auto_apply_active/max_auto_apply/preferred_tags/blocked_asset_ids`，Agent 现在可显式要求 compose 按当前假设、板块、战法/因子重叠度，从仓库中自动挑选匹配的 `active learned asset` 进入本次加权；返回体已补 `repository.auto_selected_learned_assets` 与 `learned_asset_summary.auto_selected_*`
- [x] 2026-04-16 已把 learned asset 自动吸附的调用纪律写回 Agent 协议与提示词：当前 `runtime/capabilities`、`compose-from-brief` 示例、`docs/runtime_agent_protocol_draft_20260415.md`、`openclaw/prompts/ashare*.txt` 与 `hermes/prompts/strategy_analyst.md` 已统一要求“自动吸附仅在市场假设/主题/战法结构明显匹配时显式开启”，并要求 Agent 解释吸附原因、命中资产、排序变化与新增风险
- [x] 2026-04-16 已把 learned asset 质询指导正式接入 discussion protocol：当前 compose 账本会记录 `learned_asset_options / active_learned_asset_ids / auto_selected_learned_asset_ids`，`/system/discussions/agent-packets` 已新增 `learned_asset_review_guidance`，并按 `ashare-research / ashare-risk / ashare-audit` 下发差异化问题，避免 learned asset 只在策略侧自说自话
- [x] 2026-04-16 已把 learned asset 闭环继续接到 executor：当前 `execution-precheck / execution-intents / controlled apply readiness` 已新增 `learned_asset_execution_guidance`，执行侧可识别本轮是否受 learned asset/自动吸附影响，并在预演、限额与回执留痕时采用更谨慎口径；`openclaw/hermes` 的 executor 提示词已同步
- [x] 2026-04-16 已把 learned asset 盘后建议接入 learning bridge：当前 `outcome.learning_bridge.learned_assets.items[*].advice` 已可基于执行后验、参数提示与夜间沙盘结果，给出 `maintain_active / observe / review_required / promote_active / keep_review` 等结构化建议；暂不自动改状态，继续由学习会/审批链消费
- [x] 2026-04-16 已把 learned asset advice 接入待审队列：当前新增 `POST /runtime/learned-assets/advice/ingest` 与 `GET /runtime/learned-assets/advice`，可把 `reconcile-outcome` 产出的 advice 正式转成治理队列项，形成 `promotion_candidate / maintain_review / rollback_review / observe_only` 等消费路线
- [x] 2026-04-16 已把 learned asset advice 补成受控治理闭环：当前新增 `POST /runtime/learned-assets/advice/resolve` 与 `LearnedAssetService.resolve_advice()`，支持 `accepted / rejected / dismissed / applied` 显式处理结果；默认只更新队列状态，只有显式 `apply_transition=true + target_status + 完整审批上下文` 时才会调用正式 `transition`
- [x] 2026-04-16 已把 supervision 进一步从“盯 runtime 轮询”改成“盯 Agent 活动痕迹”：当前 `/system/agents/supervision-board` 与 scheduler `supervision.agent:check` 都会暴露并消费 `activity_label / activity_signals / activity_signal_count`，按研究、策略提案、风控预检、审计复核、runtime 事实产出等真实动作判断 `working / needs_work / overdue`，不再把 `candidate_poll due_now` 当成默认催办理由
- [x] 2026-04-16 已把 runtime 原子仓库正式消费规则接回主链：`StrategyRepositoryEntry` 当前已内置统一 `runtime_policy`，明确 `active factor/playbook=default`、`experimental/review/draft factor/playbook=explicit_only`、`active learned asset=auto_or_explicit`、`archived/rejected=blocked`；`/runtime/strategy-repository` 与 compose 返回的 `repository.*` 已统一回显该策略，避免 Agent 把仓库“有条目”误当成“默认可消费”
- [x] 2026-04-16 已补 runtime 灰度/版本切流的最小可用入口：`compose-from-brief` 当前已支持 `playbook_versions/factor_versions` 显式指定版本，`/runtime/strategy-repository` 已支持按 `runtime_mode` 查询；实验版 `explicit_only` 资产现可被 Agent 显式点名使用，而 `archived/rejected` 会在 compose 入口直接阻断
- [x] 2026-04-16 已把 runtime 灰度口径继续补到能力面与直连入口：`/runtime/capabilities` 的 `compose_brief_contract/compose_brief_example/learned_asset_flow` 已补 `playbook_versions/factor_versions/repository_runtime_modes`；同时 `POST /runtime/jobs/compose` 在命中 `archived/rejected` 资产时已改为返回结构化 `ok=false` 错误，不再抛 500
- [x] 2026-04-16 已补 runtime 仓库版本视图与推荐切流信息：`StrategyRepository.version_view()` 与 `/runtime/strategy-repository` 当前已支持按 `asset_id` 汇总同名资产的多版本视图，返回 `recommended_version / recommended_runtime_mode / default_versions / explicit_candidate_versions / blocked_versions`，便于 Agent 在正式版与灰度版之间做受控选择
- [x] 2026-04-16 已把版本视图继续推进到“真实结果优先”的版本赛马摘要：compose 评估账本当前已补 `used_asset_keys / used_playbook_keys / used_factor_keys`，`/runtime/strategy-repository` 的 `version_view[*].race_summary` 已能基于真实 `adoption/outcome` 记录给出 `recommended_version / recommended_reason / has_real_outcome / candidates / blocked_candidates`，在灰度版已有真实结果时可覆盖纯状态推荐
- [x] 2026-04-16 已把版本赛马摘要继续接到治理建议层：`/runtime/strategy-repository` 当前已新增 `governance_summary` 与 `version_view[*].governance_suggestion`，可区分 `observe_only / maintain_default / review_cutover` 三类建议；其中灰度版在真实结果上反超默认版时，会进入 `review_cutover`，但仍不自动切流
- [x] 2026-04-16 已把版本赛马治理面板接到仓库层：当前新增 `GET /runtime/strategy-repository/panel`，会按 `attention_level + recommended_action` 聚合高关注切流评审项，返回 `summary/high_attention_items/items`；`/runtime/capabilities` 也已补 `race_panel_usage` 与 `repository_panels`，可明确告诉 Agent 面板只提供治理建议，不自动切流
- [/] 下一阶段主线不再围绕“修一只票、补一个问答主题、加一条固定筛选规则”，而是进入：`Agent 调用权上移 -> runtime 工具化增强 -> Hermes 工具接入 -> Agent 提示词与工作协议重写 -> 监督从盯程序切到盯 Agent`
  - [x] 2026-04-17 已把监督进一步升级到“自然语言任务派发器”第一版：`/system/agents/supervision-board` 与 scheduler `supervision.agent:check` 现会按 A 股阶段（盘前/竞价/上午/午间/下午/尾盘/盘后/夜间）生成角色化任务提示，不再只报 needs_work / overdue
  - [x] 2026-04-17 已补催办节流与留痕：同一 agent 在同一阶段、同一上下文下不会无限重复刷催办；已新增任务派发留痕键并把最近一次任务派发写回状态
- [/] 2026-04-17 当前监督发动机已具备“检查状态栏 -> 生成任务 -> 自动催办 -> 观点写回后标记完成并 refresh cycle”闭环；更完整的“消费所有研究/执行产物并自动推进提案状态机”仍需继续深化
  - [x] 2026-04-17 已给监督板补“推进质量”视角：`items/attention_items` 现会新增 `quality_state / quality_reason`，区分“已推进主线 / 部分推进 / 有活动但没写回主线 / 超时阻塞”，不再只看 `working/needs_work/overdue`
  - [x] 2026-04-17 已给监督摘要补“主线卡点”提炼：`summary_lines` 现会自动汇总 `推进质量` 与 `主线卡点`，能明确指出“哪一岗已有动作但还没写回 round/执行产物”，避免把监督退化成单纯催 runtime
  - [x] 2026-04-17 已把“真实成交后学习/归因/风控调整续发”接回监督主线：当 `latest_execution_reconciliation` 出现真实成交/对账结果后，盘后与夜间会自动续发给 `ashare / strategy / risk / audit`，推动成交复盘、参数归因、风险复核与次日主线收口
  - [x] 2026-04-17 已把 `latest_nightly_sandbox` 接回策略侧活动痕迹：监督板与 scheduler 现在都会把夜间沙盘视为策略新产物，用于自动销单，避免夜间任务已完成却继续机械催办
  - [x] 2026-04-17 已把盘后 `review_board -> nightly_sandbox -> 次日预案纪要` 这一段也接入续发链：review board 出现后会催策略侧推进夜间沙盘；夜间沙盘出现后会催审计侧收口次日预案
  - [x] 2026-04-17 已把飞书催办正文升级为“派工单”样式：当前会在正文中回显阶段、任务模式、派工缘由、当前任务与预期产物，并对重复摘要去重，减少只报状态的空提醒
  - [x] 2026-04-17 已把监督通知分层补上轻量升级机制：同一 `attention_signature` 下的 overdue 项若在上一轮催办后仍未解除，会自动从 `Agent 自动催办` 升级为 `Agent 升级催办`，并给每个待催办对象标注 `remind / urgent / escalate` 等催办等级
  - [x] 2026-04-17 已把飞书 `supervision` 问答收口到同一套交易台语言：当前 `现在各 agent 活跃度怎么样` 不再只报 attention 数量，而会回显阶段、监督席位、当前催办/值班对象和预期产物方向，和催办正文保持一致口径
  - [x] 2026-04-17 已把飞书 `status / discussion / execution / supervision` 四类主问答统一成“交易台短报”风格：当前会优先输出交易台总览、讨论席位、执行席位、监督席位等摘要，而不再直接回显底层接口原文
  - [x] 2026-04-17 已把飞书 `risk / research / params / scores` 也统一成“交易台短报”风格：当前会输出风控席位、研究席位、参数席位、评分席位等摘要，飞书问答主主题已基本摆脱接口回显式口吻
  - [x] 2026-04-17 已把飞书二级问答 `opportunity / replacement / position / holding_review / day_trading` 也统一成“交易台短报”风格：当前会输出机会席位、换仓席位、仓位席位、持仓复核席位、日内T席位等摘要，飞书问答整体人格已基本统一
  - [x] 2026-04-17 已把上述二级主题的飞书卡片摘要也同步统一为交易台口吻：当前群内卡片与接口 `answer_lines` 口径一致，不再出现正文像交易台、卡片像接口调试输出的分裂感
  - [x] 2026-04-17 已把 `symbol_analysis` 与 `symbol_focus` 回答链抽出共用模板：当前单票分析、持仓复核、做T、换仓等专题都会共享同一套事实摘要与交易建议落点，显著降低“同一只票像不同人在回答”的割裂感

## 当前战略改造阶段

### P0：主线范式翻转

- [x] P0.1 把运行主线从“程序先吐候选、Agent 再解释”切到“Agent 先形成假设，再主动调用程序工具取证”：已重写 OpenClaw 与 Hermes 全角色提示词，明确“程序是工具库，Agent 是调用者”的人格
- [x] P0.2 明确 runtime 新定位：`不是固定选股器，而是 Agent 可参数化调用的筛选 / 证据 / 过滤工具`
- [x] P0.3 明确 Hermes 新定位：已完成 `ashare-backup` profile 与任务模板建设，明确 Hermes 作为“备用大脑”承接 Agent 人格、协作与 cron 值班，通过 `/scripts/ashare_api.sh` 统一消费底座工具
- [x] P0.4 明确监督新定位：已完成 `supervision-board` 活动化改造，监督重点已从“盯程序运行”翻转为“盯 Agent 活动痕迹与市场变化响应迟滞”
- [x] S1.1 Agent 提示词与工作协议重写：已完成 research/risk/audit/executor/strategy 全角色统一重写，并注入 runtime compose、learned asset 消费、自动吸附纪律、跨角色质询口径与执行侧谨慎落地约束

### P0：runtime 工具化增强

- [x] R0.1 设计 `Agent -> runtime` 参数化调用协议：已完成 `RuntimeComposeRequest` 契约设计，支持通过 `compose` 或 `compose-from-brief` 传入市场假设、因子权重、战法组合与多层约束
- [x] R0.2 设计 runtime 结构化返回协议：已完成结构化返回体系，包含市场摘要、深度评估、证据链回显、约束追踪、提案包及学习产物调整明细
- [x] R0.3 把 runtime 的默认固定流水线降级为“底座刷新 + 基础样本生成”，把策略性扫描上移给 Agent 决定是否发起：已新增 `run_base_sample` 方法，并改造 `task_run_pipeline` 任务为生成不带策略因子的基础样本池
- [x] R0.4 增强 runtime 市场驱动输入：已在 `StrategyComposer` 中接入热点板块、正面事件催化、动能异动加成，并体现在候选打分与 `market_drivers` 解释面板中
- [x] R0.5 构建 runtime 的“原子化策略仓库”自治链：已在 `LearnedAssetService` 中补全了 `auto_discover_from_discussion / auto_discover_from_attribution / auto_promote_assets` 方法；讨论终结与日终结算现可自动触发学习产物入库与分区流转
- [/] R0.6 建立因子仓库（Alpha Factory）：已落 `factor_registry` 并支持 `market_adapter` 真实数据接入；`momentum_slope / relative_volume / limit_sentiment / price_drawdown_20d / volatility_20d / smart_money_q` 等 14 个核心因子已完成基于 A 股特性的真实计算实现
- [/] R0.7 建立战法插件层（Playbook Plugins）：已落 `playbook_registry` 并支持真实数据接入；`sector_resonance / weak_to_strong_intraday / leader_chase / oversold_rebound / statistical_arbitrage` 等 10 个经典战法已完成真实计算实现
- [x] R0.8 建立约束层（Constraint Pack）：已补全 `execution_barriers` 的稳定性逻辑，现可基于实时快照自动阻断涨跌停标的（买入阻断涨停、卖出阻断跌停），并支持基于对手价的偏移度校验与板块黑白名单过滤
- [x] R0.9 建立策略编排层（Strategy Composer）集成：已在 `StrategyComposer` 中接入 `FactorEngine` 批量计算逻辑；`compose` 链路现可一次性批量获取所有候选股日线并预计算因子值，避免了注册表评估时的重复行情请求
- [/] R0.10 建立评估层（Evaluator / Backtest Bridge）：已落 compose 评估账本，并已接通 `reconcile_backtest` 自动离线回测与 `reconcile_factor_performance` Rank IC 因子效力分析；`StrategyComposer` 现可根据账本历史表现自动衰减劣质资产权重
- [/] R0.11 把 runtime 插件做成正式仓库形态：已支持注册、查询、版本键管理，并已把正式主链消费策略接回仓库、capabilities 与 compose 返回；当前 `compose-from-brief` 已支持按 `playbook_versions/factor_versions` 显式选版本，`/runtime/strategy-repository` 已支持按 `runtime_mode` 查询、`asset_id` 版本视图、`race_summary` 真实结果摘要、`governance_suggestion/governance_summary` 治理建议，以及 `GET /runtime/strategy-repository/panel` 面板聚合；`POST /runtime/jobs/compose` 与 brief 入口都会结构化阻断 `archived/rejected` 资产，`active` 默认可消费、`experimental` 仅显式试验；更完整的灰度发布编排、批量切流策略与自动切流仍未完成
- [x] R0.12 建立策略仓库分区：已落 `active / experimental / learned / archived / draft / review_required / rejected` 状态模型，并已把正式分区消费规则接入 runtime 仓库与 compose 主线回显
- [/] R0.13 建立学习产物沉淀机制：已支持 Agent 学习产物以 `draft` 入库并做状态流转，评估审批链未完成
- [/] R0.14 建立学习产物升级流程：已落第一段状态流转服务与升级接口，`active` 升级已要求 `discussion/risk/audit` 三道门审批上下文与正式引用，且已开始反查真实 case/gate/final_status，并把 `trade_date_summary / reason_board_summary / vote_detail` 绑定进审批记录；审批记录与审批面板已可查询；尚未接完整自动联动与更广覆盖的正式对象反查
- [/] R0.14 建立学习产物升级流程：当前审批记录已继续绑定 `final_brief_summary / finalize_packet_summary`，学习产物从草案到转正的回溯链更完整；同时 advice 已支持“入队 -> 显式 resolve -> 可选正式流转”的受控治理闭环；尚未接完整自动联动与更广覆盖的正式对象反查

### P0：Agent 工具消费面

- [x] A0.1 梳理主程序现有能力，整理成 Agent 可直接消费的工具清单：已在 `GET /runtime/capabilities` 中新增 `system_tools` 节点，分类列出行情、讨论、执行、策略、治理全套 API
- [x] A0.2 把外部 `skills` 收束到 Hermes / Agent 运行侧：已确立“Agent 为主理人，程序为工具库”的调用范式，Agent 现在通过标准 API 协议（如 compose）消费程序底座的量化能力，不再将业务逻辑硬编码进主程序
- [x] A0.3 为 Agent 补“系统全能力说明面板”：`/runtime/capabilities` 已扩展为包含全局工具清单、compose 契约、约束说明、学习产物准入与示例请求的统一门户
- [x] A0.4 补“Agent 可自行组织参数让 runtime 跑出结果”的调用入口：已落 `POST /runtime/jobs/compose-from-brief` 与首版独立 `constraint pack`；目前已支持排除方向、黑白名单、前缀封锁、板块黑白名单、市场规则、持仓规则、执行前围栏与风险阈值的结构化映射


### P1：协作与监督

- [x] S1.1 重写主控与子 Agent 提示词，去掉“固定 5 类主题问答器”倾向，改为“围绕市场机会主动工作”的交易台人格：已完成 research/risk/audit/executor/strategy 全角色统一重写，并注入 runtime compose、learned asset 消费、自动吸附纪律及跨角色质询口径
- [x] S1.2 引入提案、质询、反证、风控闸门、审计复核的协作协议：已在 `round_summarizer` 中实现了 `inquiry_targets` 与 `counter_evidence` 的结构化提取；`DiscussionStateMachine` 现可基于质询闭环情况强制续轮；`agent-packets` 已支持下发针对性质询指导
- [x] S1.3 增强监督催办逻辑：已把催办逻辑从“机械到点就催”升级为“基于市场变化的灵敏催办”；`supervision-board` 现可识别重大市场事件（新闻/异动），并对 10 分钟内无响应的子代理（尤其是研究与策略）触发 `overdue` 状态
- [x] S1.3.1 已把催办从“报表式提醒”推进到“任务式派发”：当前会按阶段给 `ashare/research/strategy/risk/audit/executor/runtime` 生成自然语言任务、预期产物和派发原因，并在飞书催办正文中直接下发，不再只有红黄灯状态
- [x] S1.3.2 已接入任务派发节流：相同阶段和上下文不会高频重复催办
- [/] S1.3.3 已接入讨论产物消费闭环第一段：`/cases/{case_id}/opinions`、`/discussions/opinions/batch`、`/discussions/opinions/openclaw-ingress` 写回观点后会回写任务完成态，并自动 `refresh_cycle` 推进 round；后续仍需扩展到研究摘要、执行回执、盘后学习等更广产物面
- [x] S1.3.4 已补“按活动时间戳自动销单”机制：监督板会用各 agent 最新活动时间对比最近一次派发时间，自动识别研究摘要、执行回执、审计纪要、尾盘扫描、夜间学习等产物已经落地，从而停止重复催办
- [/] S1.3.5 已开始接“完成后自动续发下一步任务”机制：当前已支持在讨论四岗材料齐备后，自动向总协调续发“刷新 cycle / 判断续轮或收敛”的推进任务；后续仍需把执行后续发、盘后学习续发、持仓管理续发继续扩开
- [/] S1.3.6 已把仓位/持仓上下文接进续发规则：当前监督板已可基于 `position_count/current_total_ratio/equity_position_limit/available_test_trade_value` 对研究、策略、风控、执行生成“继续找机会 / 盯持仓 / 做T / 换仓”类续发任务；后续仍需再细化到满仓替换、真实成交后复核与盘尾动作切换
- [/] S1.3.7 已把“接近满仓”和“执行提交结果”接进续发规则：当前已支持仓位接近上限时向策略侧下发“替换仓位”任务，并在执行侧出现 `submitted/preview/blocked` 结果后，向总协调与风控侧续发执行后复核/收口任务；后续仍需继续细化到真实成交后学习归因和盘后参数调整
- [x] S1.3.8 已把监督消息与飞书监督答复补齐“质量口径”：`agent_supervision_template` 与 supervision 问答现会直接说明 `推进质量` 与 `当前卡点`，并点出“有活动但未推进主线”的岗位，不再只报 attention 数量
- [x] S1.3.9 已把质量信号真正接回任务编排：`build_agent_task_plan` 现会把 `quality_state / quality_reason / progress_blockers / 覆盖缺口` 翻译成 `task_reason / task_prompt / summary_lines`，催办会明确要求“不要只做调参或内部动作，必须写回主线产物”
- [x] S1.3.10 已给任务编排补优先级排序：当前 `recommended_tasks` 会按 `overdue > needs_work > working`、`blocked/low > partial/good` 和主线覆盖缺口排序，确保真正卡主线的岗位排在前面，不被普通 follow-up 淹没
- [x] S1.3.11 已把优先级排序接到实际监督派发口：`attention_items / notify_items / supervision.check` 现会沿用同一优先级顺序，飞书监督卡、监督问答与真实催办对象顺序保持一致，先盯最卡主线的人
- [x] S1.3.12 已把升级催办原因结构化：当前 `supervision_action_reason` 会明确说明“重复催办未解除 / 仍缺几份主线材料 / 有活动但没写回产物”等原因，并同步透到监督摘要、飞书监督答复与催办正文
- [x] S1.3.13 已把催办原因岗位化：研究侧会强调“市场事实/机会票”，策略侧会强调“打法/参数/runtime 组织”，风控侧会强调“阻断/放行与风险边界”，审计侧会强调“证据链/反证/纪要”，避免所有岗位收到同一套空泛催办话术
- [x] S1.3.14 已把催办原因阶段化：同一岗位在 `集合竞价 / 上午盘中 / 尾盘收口 / 盘后复盘 / 夜间学习` 等阶段会收到不同的优先原因，例如策略岗竞价强调“是否切换打法”，盘后强调“打法归因与次日预案”，不再只是岗位维度的静态句式
- [x] S1.3.15 已把催办原因接入仓位/持仓语境：同一岗位同一阶段下，空仓/仍有仓位空间/接近满仓/已有持仓时也会收到不同优先原因，例如策略岗会区分“继续找机会”与“优先做替换仓位”，执行与风控会明确转入持仓管理和做T/换仓准备
- [x] S1.3.16 已把催办原因接入市场事件与执行结果语境：当存在 `latest_market_change_at`、执行待跟进状态或真实成交/对账结果时，监督派发原因会进一步强调“先响应新异动”“先核对执行链”“先做成交后归因/风控复核”，不再只看岗位、时段和仓位
- [x] S1.3.17 已把自动销单从“只看新活动时间”收紧为“看新产物是否对口”：任务派发现在会落 `completion_tags`，自动 completion 会校验新活动是否匹配上次派工目标，例如“夜间沙盘”任务不会再因为普通策略动作或“仍未形成”的字样被误判为已完成
- [x] S1.4 让飞书只承担知情权 / 调参权 / 询问权 / 重要消息推送，不让它反向把主控限制成固定 FAQ 机器人：已完成 `/system/feishu/ask` 意图的精细化重构，并继续推进到“单票交易台答复”层；当前“持仓/仓位”问答可直观显示仓位上限、预算余量与活跃占位状态，“替换建议”会结合逐票执行预检、讨论定位与其他候选做比较，“symbol_analysis` 已能组合研究/持仓/做T/执行预检/讨论定位形成更像交易台的真实答复，而不是只落到固定分类帮助文案

---

## 模块 A：集合竞价抢跑引擎 [P0]

- [x] A1.1 新建 `data/auction_fetcher.py` — AuctionFetcher 类 + fetch_snapshots 方法
  - [x] 补全 `_fetch_from_gateway()` — HTTP 调用 Gateway
  - [x] 补全 `_fetch_from_akshare()` — akshare 数据适配
- [x] A1.2 contracts.py 新增 AuctionSnapshot + AuctionSignal 模型
- [x] A2.1 新建 `strategy/auction_engine.py` — AuctionEngine + evaluate_all **已实现** ✅
  - [x] 补充板块级竞价共振逻辑
  - [x] 补充与 T-1 竞价数据对比
- [x] A2.2 修改 `buy_decision.py` — generate() 接入 auction_signals
- [x] A3.1 修改 `scheduler.py` — 注册 09:20 + 09:24 竞价任务

## 模块 B：盘中微观节奏捕捉 [P1]

- [x] B1.1 contracts.py 新增 MicroBarSnapshot + MicroSignal 模型
- [x] B1.2 新建 `monitor/micro_rhythm.py` — MicroRhythmTracker **已实现** ✅
- [x] B2.1 修改 `exit_engine.py` — check() 接入 micro_signal 参数
- [x] B3.1 修改 `scheduler.py` — 注册 */1 持仓微观巡检任务

## 模块 C：波峰波谷自适应出场 [P2]

- [x] C1.1 修改 `sell_decision.py` — PLAYBOOK_EXIT_PARAMS 动态参数表 **已实现** ✅
- [x] C1.2 修改 `sell_decision.py` — evaluate() 接受 playbook + regime 参数 ✅
- [x] C2.1 修改 `sell_decision.py` — REGIME_MODIFIERS regime 修正系数 ✅

## 模块 D：事件驱动响应中枢 [P1]

- [x] D1.1 contracts.py 新增 MarketEvent + EventType 模型
- [x] D1.2 新建 `data/event_bus.py` — EventBus 类 **已实现** ✅
- [x] D2.1 修改 `scheduler.py` — 注册事件响应链
- [x] D2.2 新增 `scheduler.py:_on_negative_news()` 响应方法
- [x] D2.3 新增 `scheduler.py:_on_price_alert()` 响应方法
- [x] D3.1 修改 `event_fetcher.py` — 增量抓取 + 事件发射
- [x] D3.2 修改 `market_watcher.py` — 价格异动事件发射

## 模块 E：多 Agent 深度论证活化 [P1]

- [x] E1.1 state_machine.py DiscussionState 泛化 + round_running/round_summarized ✅
- [x] E1.2 state_machine.py start_round(n)/complete_round(n)/can_continue_discussion() ✅
- [x] E1.3 修改 `discussion_service.py` — 移除 round 1/2 硬限 + 超时熔断
- [x] E2.1 finalizer.py build_finalize_bundle() 新增 agent_weights 参数 ✅
- [x] E2.2 修改 `discussion_service.py` — finalize 时从 score_state 读取 weights
- [x] E3.1 contradiction_detector.py detect_evidence_conflicts() + 9 对关键词 ✅
- [x] E4.1 修改 `opinion_validator.py` — round == 2 改为 round >= 2

## 模块 F：闭环归因自进化 [P0]

- [x] F1.1 修改 `team_registry.final.json` — 新增 agent_weights 节点
- [x] F1.2 score_state.py export_weights() + run_daily_settlement() ✅
- [x] F2.1 新建 `learning/prompt_patcher.py` — PromptPatcher **已实现** ✅
- [x] F2.2 auto_governance.py build_agent_lesson_patches() ✅
- [x] F3.1 新建 `learning/registry_updater.py` — RegistryUpdater **已实现** ✅
- [x] F4.1 修改 `scheduler.py` — 注册 16:30/16:45/17:00 盘后任务
- [x] F5.1 修改 `self_evolve.py` — scheduler 17:15 策略权重建议任务
- [x] F5.2 修改 `continuous.py` — scheduler 17:30 增量训练任务

## 模块 G：夜间沙盘推演 [P2]

- [x] G1.1 新建 `strategy/nightly_sandbox.py` — NightlySandbox 框架 ✅
  - [x] 补全 `_simulate_param_adjustment()` 参数模拟逻辑
  - [x] 集成 discussion_service replay 方法
- [x] G1.2 contracts.py 新增 SandboxResult 模型 ✅
- [x] G2.1 修改 `scheduler.py` — 注册 23:00 夜间推演任务
- [x] G2.2 修改 `buy_decision.py` — 已支持 `nightly_priorities` 入参加分，且 `scheduler` 已消费 nightly sandbox 结果参与次日候选排序

## 模块 H：数据链整固 [P2]

- [x] H1.1 修改 `precompute.py` — 已把 `as_of_time` 下沉到 precompute / data fetcher / market adapter 的历史 K 线读取链
- [x] H1.2 修改 `serving.py` — as_of_time 过滤
- [x] H2.1 freshness.py tag_freshness() ✅

---

## 完成统计

| 状态 | 数量 | 占比 |
|------|------|------|
| ✅ 已完成 | 47 | 100% |
| 🔲 待接力 | 0 | 0% |
| **合计** | **47** | **100%** |

## 本轮新增完成

- 已把 `scheduler.py` 接上竞价 09:20/09:24、微观 */1、盘后 16:30/16:45/17:00/17:15/17:30、夜间 23:00 任务。
- 已把 `event_fetcher.py`、`event_bus.py`、`market_watcher.py` 串成事件链，支持负面新闻与价格异动事件落库。
- 已把 `nightly_sandbox.py` 从占位实现补到可跑的参数模拟 + replay 摘要接入。
- 已把 `serving.py` 增补 `as_of_time` 读取保护，避免 serving 侧直接读取未来时点产物。
- 已把 `buy_decision.py` 增补 `nightly_priorities` 优先级入口，并把 `precompute.py` / `/precompute/dossiers` API 补上 `as_of_time` 参数。
- 已把 `scheduler.py` 接上 `latest_nightly_sandbox` 优先级加权，次日候选排序会真实消费夜间推演结果。
- 已把 `auction_engine.py` 补上板块竞价共振和前次/T-1 竞价对比加减分，并由 `scheduler.py` 传入板块与历史竞价上下文。
- 已把 `as_of_time` 继续下沉到 `DataPipeline/DataFetcher/MarketAdapter.get_bars`，历史日线预计算不再只停留在入口签名。
- 已把 `scheduler.py` 接上 auction gateway URL，`auction_fetcher.py` 会优先尝试 `/auction/snapshot`，失败再回落 akshare。
- [x] 2026-04-15 已把“禁买某类股票”从硬编码需求改为通用可调参数：新增 `excluded_theme_keywords`，不再只针对银行股，支持 `银行/白酒/地产/...` 这类方向关键词。
- [x] 2026-04-15 已把排除方向接入 `runtime` 与 `execution_precheck` 双层：盘中真实选股会先过滤命中方向的标的，执行预检也会对历史遗留 case 做兜底阻断，避免只改推荐不改执行。
- [x] 2026-04-15 已补飞书自然语言调参联调回归：`/system/feishu/adjustments/natural-language` 现可预览/落地 `"今天先不买银行股、白酒股"` 这类指令，测试已验证解析结果为 `excluded_theme_keywords=银行,白酒`。
- [x] 2026-04-15 已补自动化验证：新增“多主题自然语言解析 / runtime 过滤 / execution precheck 阻断”三条测试，当前 `tests/test_upgrade_workflow.py` 全量 `50/50` 通过。
- [x] 2026-04-15 已定位并修复飞书入站调参失败：此前 `im.message.receive_v1` 只会走“监督回写 / 自然语言问答”两条分支，群里直接发调参句子会被当成问答吞掉；现已新增“自然语言调参”分支，`@机器人 今天先不买白酒股` 这类消息会直接进入调参链。
- [x] 2026-04-15 已把“米其林厨房 / 工具库而非死菜谱 / 红蓝军协作 / 反摸鱼 / 自主提案与进化”原则同步写回 OpenClaw 与 Hermes 的主控、总控、研究、策略、风控、审计提示词，避免系统继续退化成固定五类 FAQ 和被动评分器。
- [x] 2026-04-15 已修复飞书一句话多条件调参漏识别：`NaturalLanguageAdjustmentInterpreter` 现已补齐 `仓位 / 股票仓位 / 持仓仓位 / 个股最多 / 个股不超过 / 单股上限` 等别名，同一句 `仓位调到3成，个股最多不能超过2万，暂时不买银行股` 会同时命中 `equity_position_limit=0.3`、`max_single_amount=20000`、`excluded_theme_keywords=银行`。
- [x] 2026-04-15 已修复飞书个股问答掉回帮助文案：`/system/feishu/ask` 与 `/system/feishu/events` 现已支持按股票名称或代码进入 `symbol_analysis`，`金风科技这支股票怎么样` 会解析到 `002202.SZ`；若只说“帮我分析一下这支股票”则明确追问标的名，不再返回固定帮助口径。
- [x] 2026-04-15 已把个股分析能力继续从“个别映射可用”收口到“任意股票可分析”方向：`MarketDataAdapter` 现已新增统一 `search_symbols()` 接口，问答层会优先走证券主数据搜索，不再主要依赖候选池 / dossier / 全市场逐只试名。
- [x] 2026-04-16 已把飞书单票问答继续从“识别标的”推进到“交易台答复”：`/system/feishu/ask` 当前新增单票事实汇总层，`symbol_analysis` 会联合研究摘要、dossier、持仓快照、执行预检、尾盘/日内信号与讨论定位输出更完整答复；`replacement` 会明确“当前票是否构成被替换对象”并给出对比候选，而不再只回观察池概览。
- [x] 2026-04-16 已补复杂追问归一化与前台能力文案收口：`金龙羽这支股票帮我分析了吗？` 这类追问现在仍可命中 `symbol_analysis`；帮助口径也已改成“可问状态/机会票/逐票分析/仓位/做T/风控/研究/执行”，不再像固定 FAQ 分类器。
- [x] 2026-04-16 已把飞书逐票问答进一步收口成“交易台建议卡”：当前 `symbol_analysis / position / day_trading / replacement` 返回体都已附带 `trade_advice`，并在 `answer_lines` 中明确给出“交易台结论/建议 + 下一步动作”；结论只基于正式持仓、执行预检、尾盘信号、讨论定位与研究摘要生成，不额外编造盘面判断。
- [x] 2026-04-16 已把 `trade_advice` 从文本建议继续升级为结构化卡片：当前统一返回 `stance / recommendation_level / summary / next_actions / trigger_conditions / risk_notes`，并在逐票答复里同步回显“建议级别 / 触发条件 / 风险提示”，后续飞书卡片或 Hermes 前台可直接消费，不必再从自然语言里二次抽取。
- [x] 2026-04-16 已把飞书前台回群接上结构化建议卡摘要：当前 `/system/feishu/events` 处理单票问答时，`reply_lines` 会优先输出紧凑版建议卡摘要（标的、建议级别、立场、结论、触发条件、风险提示、下一步），而 API 原始 `answer_lines/trade_advice` 仍完整保留；`/system/feishu/ask?notify=true` 的模板也已同步可带 `trade_advice`。
- [x] 2026-04-16 已把飞书前台结构化摘要从单票扩到主线问题：当前 `/system/feishu/events` 对 `opportunity / position / execution / risk` 也会优先输出紧凑摘要行，例如机会票概览、仓位卡、执行卡、风控卡，而不再把长段 `answer_lines` 原样回群；程序 API 侧仍保留完整原始答复。
- [x] 2026-04-16 已把飞书前台剩余主题继续压成结构化摘要：当前 `/system/feishu/events` 对 `status / discussion / research / params / scores / supervision` 也已支持紧凑版回复卡，例如状态卡、讨论卡、研究卡、参数卡、评分卡、监督卡；至此主线问答的大部分主题都已有前台摘要层。
- [x] 2026-04-16 已把飞书前台从“文本模拟卡片”推进到“真实卡片消息体准备态”：当前问答事件返回已附带 `reply_card.title/markdown`，长连接 worker 回群时会优先调用飞书 `interactive` 卡片发送；若卡片不可用才回落纯文本 `reply_lines`。
- [x] 2026-04-16 已把 `reply_card` 从 markdown 兼容层推进到真实 card JSON：当前问答事件返回已同时附带 `reply_card.card`（含 header/template/elements/action），长连接 worker 回群时会优先 `send_card` 发送这份 card JSON，失败时再回落 markdown/text。
- [x] 2026-04-16 已完成本轮飞书问答回归：`tests/test_upgrade_workflow.py` 全量 `89/89` 通过，已覆盖复杂追问解析、单票分析组合真实事实、单票替换答复、结构化建议卡字段、飞书单票建议卡摘要、主线结构化摘要与 `reply_card/card JSON` 更新。
- [x] 2026-04-16 已完成本轮 runtime 仓库消费规则回归：`tests/test_upgrade_workflow.py` 全量 `95/95` 通过，已覆盖仓库条目 `runtime_policy`、版本视图 `recommended_version`、`race_summary` 真实结果推荐、`governance_suggestion` 三类治理动作、`/runtime/strategy-repository` 分区消费口径、`runtime_mode/asset_id` 查询、capabilities 灰度字段、`compose-from-brief` 版本覆写，以及 direct/brief compose 对 archived 资产的结构化阻断。
- [x] 2026-04-16 已把 Linux Go 平台从“只代理 `/qmt/*` 的外部数据面”升级为“统一并发入口”：`18793` 当前同时承接 `/qmt/*` 到 Windows Go 网关、`/system/*` 与 `/runtime/*` 到本地 Python 控制面，并统一纳入 Go 的 lane/队列/连接池；实测 `GET http://127.0.0.1:18793/system/operations/components` 与 `GET http://127.0.0.1:18793/qmt/account/asset` 都已正常返回，证明项目内请求和对 Windows 请求都已纳入同一 Go 并发连接系统。
- [x] 2026-04-16 已补 Go 平台与执行链稳态保护：`GoPlatformClient`、`GoPlatformExecutionAdapter`、`GoPlatformMarketDataAdapter` 现已支持 `429/401/404/5xx/timeout` 分类、GET 快速 fallback、Windows 网关细粒度超时；`execution-precheck` 也已改为并发拉取余额/持仓，持仓缺失时会结构化降级为 `positions_unavailable`，不再整条挂死。
- [x] 2026-04-16 已开始主修 Hermes soul 与前台主控智能化：`SOUL.md`、`.hermes.md` 与 `hermes/prompts/*` 已统一到“程序是手脚与电子围栏，Agent 是大脑与主理人”的口径，并补上主控可闲聊、可分流、可对候选池外股票做临时体检的行为约束。
- [x] 2026-04-16 已增强 `/system/feishu/ask`：主控现在不再只像固定 FAQ 路由器，已支持 `casual_chat` 自然回复、逐票临时体检、多视角股票分析口径，以及对非 dossier 股票基于快照/K 线/研究摘要/执行事实的 ad-hoc 分析模式。
- [x] 2026-04-16 已把自主运行接口的“怎么用”写清楚：`/system/agents/capability-map`、`/system/workflow/mainline`、`/system/agents/autonomy-spec` 当前已直接返回 `how_to_use` 与示例调用；同时文档 `docs/hermes_agent_autonomy_contract_20260416.md` 已补接口用途、调用时机、读取顺序和 `curl + jq` 示例，便于 Hermes / 可视化控制台 / 替代脑直接接线。
- [x] 2026-04-16 已把积分赛马高压治理口径接入机器合同：`/system/agents/capability-map` 现已暴露 `competition_mechanism`，`/system/workflow/mainline` 已暴露 `governance_pressure`；“积分归零 / 解雇 / 罚款1000万”以系统内模拟处罚语言落地，真实执行仍映射为分数清零、冻结权限、撤席位和优先级下调。
- [x] 2026-04-16 已补 runtime 任务画像提示：`/runtime/capabilities` 当前已新增 `task_profiles / task_profile_usage / compose_brief_contract.selection_rule`，明确基础筛选、龙头战法、盘中发现、日内做T、低位挖掘、超跌反弹、持仓替换等任务应选用不同 playbook/factor 组合，不再默认所有场景都吃一套组合。
- [x] 2026-04-16 已把 runtime 任务画像从“半硬绑定模板”收敛成“非绑定组合指南”：`/runtime/capabilities` 当前已补 `task_dimensions / composition_rules / anti_misuse_rules / profile_mix_examples`，并把各画像标注为 `binding_level=advisory_only`，明确允许多画像混编、局部借用或完全自定义，避免任务模板反过来误导 agent。
- [x] 2026-04-16 已把外挂 skill pack 接入能力层与提示词：`/runtime/capabilities` 当前已新增 `supplemental_skill_pack`，把政策监控、个股快检、复盘、知识库、盯盘、研报提炼、风险预警、最小回测等能力定义为“补充 / 对照 / 交叉验证”工具；同时 `hermes/prompts/strategy_analyst.md`、`runtime_scout.md`、`event_researcher.md` 已补规则，明确 skill 只能辅助参考，不能替代 agent 自组组合与主程序事实链。
- [x] 2026-04-16 已完成本轮数据传输收口第一段：`go_data_platform/main.go` 现已显式禁用环境代理并补连接池参数；`src/ashare_system/infra/go_client.py`、`infra/adapters.py`、`infra/market_adapter.py`、`windows_execution_gateway_worker.py`、`data/auction_fetcher.py` 也已统一 `trust_env=False`，避免 Linux 侧内部请求、Windows Gateway 读链路与 worker 轮询误吃代理配置。
- [x] 2026-04-16 已完成控制台自动刷新收口：`web/src/pages/OverviewPage.tsx`、`AgentsPage.tsx`、`DiscussionPage.tsx` 已移除手动刷新按钮，改为保留既有轮询频率并直接展示“自动刷新中”说明；`web/src/api.ts` 同步移除 `refreshAccountState()` 手动强刷接口，避免前台额外触发 `refresh=true` 的实时账户压力。
- [x] 2026-04-17 已补运行产物保鲜机制：新增 `scripts/cleanup_runtime_files.sh`，默认清理项目内超过 7 天的日志/恢复证据/前端 `.tmp` 临时文件；`scripts/start.sh` 与 `scripts/daily_pipeline.sh` 已接入启动即清理；`src/ashare_system/logging_config.py` 已改为按天轮转并只保留 7 天主日志，避免 `logs/` 持续膨胀。
- [x] 2026-04-17 已补“活文件”轮转：`scripts/common_env.sh` 新增 `rotate_log_file_if_needed()`；`scripts/start.sh` 与 `scripts/start_openclaw_gateway.sh` 会在启动前对固定文件名日志按大小切卷，避免 `startup.log`、`openclaw-gateway.log` 这类持续追加文件即使没有跨天也无限增长。
- [x] 2026-04-15 已为 Windows 行情桥补证券名称搜索验证：`WindowsProxyMarketDataAdapter` 现支持按全市场 A 股主数据批量预热后做名称检索；新增 `金龙羽(002882.SZ)` 回归与 `search_symbols` 适配器测试，当前 `tests/test_upgrade_workflow.py` 全量 `52/52` 通过。
- [x] 2026-04-15 已切正式运行模式为 `live`：`.env` 已从 `ASHARE_RUN_MODE=paper` 切到 `live`，并已重启 `ashare-system-v2.service`、`ashare-system-v2-scheduler.service` 与用户级 `ashare-feishu-longconn.service`；上线后 `/system/settings` 已返回 `run_mode=live`，`/system/operations/components` 显示 `market_adapter=windows_proxy`、`execution_adapter=windows_proxy`、`feishu_longconn=connected`。
- [x] 2026-04-15 已同步 live 前用户偏好：当前正式参数已确认 `equity_position_limit=0.3`、`max_single_amount=20000`、`reverse_repo_target_ratio=0.0`、`reverse_repo_reserved_amount=0.0`、`excluded_theme_keywords=银行`。
- [x] 2026-04-15 已修复 live 预算口径错位：`stock_test_budget_amount` 现改为取“风险仓位上限预算”与“测试基线预算”两者中更严格者，`/system/account-state` 已从错误的 `100000` 修正为 `30308.56`，与当前 `equity_position_limit=0.3`、总资产 `101028.55` 一致。
- [x] 2026-04-15 已收口 live apply 准入阈值：开启 `ASHARE_LIVE_ENABLE=true` 后，`check_apply_pressure_readiness.sh` 已不再默认强绑旧的 `30000` 股票预算阈值，当前受控准入脚本真实结果显示预算口径/单票口径/逆回购口径均通过。
- [/] 2026-04-15 当前 live 受控 apply 剩余唯一阻断已收敛为 `apply_intent_limit`：当前 execution intents 有 2 条已通过预检，受控 apply 默认只允许 1 条，因此若要继续真实受控 apply，需要先限定本次只放行 1 条 intent（或明确放宽 `max_apply_intents`）。

## 当前剩余重点

- 当前原子任务已全部完成；若继续深化，重点将转向真实 Windows Gateway 联调、生产数据回放验证与更高覆盖率测试。
- [x] 2026-04-16 `system_api` 基线漂移阻断已解除：此前该文件一度被外部改成残缺版本，导致大量 `/system/discussions/*`、`/system/feishu/*`、`/system/operations/components`、`/system/deployment/*` 路由不可用；现已恢复到完整接口面，并完成代表性回归（discussion/feishu events/operations/supervision/nl adjustment）与 `tests/test_upgrade_workflow.py` 全量 `95/95 OK` 验证，主线可继续沿既有基线推进。
- 当前“排除方向”仍属于关键词匹配型偏好，不是完整行业分类引擎；若后续要更细到申万/概念层级白名单与黑名单，需要继续把 Windows 侧 sector 元数据质量做实并统一分类口径。
- 当前飞书入站调参已能走通控制面逻辑，但真实群聊体验还取决于长连接 worker 使用的是最新代码版本；若你刚重启过但群里仍回复旧帮助口径，需要把当前正式服务进程切到最新代码后再做一次群测。
- 当前 Hermes 主控虽然已具备更像“总协调”的问答骨架，但要进一步升级到“自动拉子代理做多视角讨论并回填前台”仍可继续深化；本轮先完成了人格、规则和程序兜底层，避免继续停留在死主题问答器模式。
- 当前 `GET /qmt/account/trades` 已按 Windows 文档恢复“缓存 / 短等待官方查询 / 已成交订单合成”的上游语义，但项目内 `TradeSnapshot` 还只保留成交主字段，尚未把 `synthetic` 与 `diagnostics.source` 提升到统一契约面；这不阻断主线交易读链路，但若后续要在前台或审计面明确显示“合成成交”，还需单独扩展契约与消费层。

## 联调记录

- [x] 2026-04-12 控制面写口联调：已成功 `POST /system/monitor/execution-bridge-health`，`review-board` 已真实反映 execution bridge 状态与待办。
- [x] 2026-04-12 协议闭环演练：公开 discussion dispatch 因 `balance_unavailable` 无法生成真实 intent，已额外通过 `integration_probe` synthetic paper intent + `windows_execution_gateway_worker --executor-mode noop_success --once` 完成 `poll -> claim -> receipt` 闭环。
- [x] 2026-04-12 receipt 回写验证：`/system/execution/gateway/receipts/latest` 已返回 latest receipt，intent `integration-probe-20260412-1618` 状态已更新为 `submitted`，并写入 claim / latest_receipt_id。
- [x] 2026-04-12 联调中发现并修复错报：`review-board.control_plane_gateway.pending_intent_count` 先前会把已 `submitted` 的 intent 也计入 pending；现已改为只统计真正 `approved` 的待拉取队列。
- [x] 2026-04-12 新增真实接线准备：已补 `windows_proxy` 执行适配器骨架与配置位，可对接 Windows 侧 `18791` HTTP 接口，后续只需配置 base URL 与 token 即可切换。
- [x] 2026-04-12 Windows `18791` 真实资产链接通：已验证 `/health` 与 `/qmt/account/asset`，并将 Linux 控制面切换到 `windows_proxy`。
- [x] 2026-04-12 控制面重启生效：`/system/account-state` 已返回真实账户 `8890130545`、总资产 `101028.55`、现金 `101028.55`，`/system/readiness` 中 `account_access` 已恢复为 `ok`。
- [/] 2026-04-12 公开 discussion 执行流剩余阻塞：`execution-precheck` 已解除 `balance_unavailable`，但当前仍无 `cycle_state / execution_pool_case_ids`，因此 `execution-intents` 仍为 `0`；下一步应转向真实 runtime / discussion 数据流，而不是继续排查账户链。
- [x] 2026-04-12 真实堵点复核：`serve` 进程按设计不会自动启动 scheduler，`/system/operations/components` 也明确标注 `scheduler=managed_externally`，因此“服务启动后未自动出候选池”不是接口 bug，而是部署侧缺少外部 scheduler 进程。
- [x] 2026-04-12 时间窗复核：当天是 `2026-04-12 (Sunday)`，而 `scheduler.py` 的盘后 pipeline cron 为 `0 21 * * 1-5`；即使 scheduler 存在，周日也不会按工作日盘后任务自动生成新候选池。
- [x] 2026-04-12 部署补口完成：仓库此前只有 Linux control plane `serve` 的 systemd 安装脚本，没有独立 `scheduler` unit；现已补 `scripts/install_linux_scheduler_service.sh` 与 `scripts/ashare_scheduler_service.sh`，并已真实拉起独立 `scheduler` 进程。
- [x] 2026-04-12 调度口径修正：按真实业务口径把“研究 / 治理 / 学习 / 夜间回放”改为每日运行，不再错误限制在工作日；保留 `选股评分 / 买入预确认 / 盘中执行` 继续仅限交易日。
- [x] 2026-04-12 Windows 行情接口需求已落仓：已新增 `docs/windows_gateway_market_api_requirements_20260412.md`，明确 Linux 侧切换 `market_mode=windows_proxy` 所需的 `instruments / universe / sectors / sector-members` 接口契约，供 Windows 侧直接补实现。
- [x] 2026-04-12 Linux 行情桥接完成：已在 `src/ashare_system/infra/market_adapter.py` 新增 `WindowsProxyMarketDataAdapter`，接通 `/qmt/quote/instruments`、`/qmt/quote/tick`、`/qmt/quote/kline`、`/qmt/quote/universe`、`/qmt/quote/sectors`、`/qmt/quote/sector-members`，并在 `build_market_adapter()` 中接入 `market_mode=windows_proxy`。
- [x] 2026-04-12 行情映射测试补齐：已在 `tests/test_upgrade_workflow.py` 新增 instruments/tick/kline/universe/sectors/sector-members 映射测试，当前全量单测 `11/11` 通过。
- [x] 2026-04-12 真实行情切换生效：`.env` 已切到 `ASHARE_MARKET_MODE=windows_proxy`，`/system/operations/components` 已返回 `market_adapter=windows_proxy`、`execution_adapter=windows_proxy`。
- [x] 2026-04-12 runtime 真链验证通过：已真实调用 `POST /runtime/jobs/pipeline`，主板池 `candidates_evaluated=3195`，成功产出候选池与市场画像；本次实测 top picks 为 `600010.SH 包钢股份`、`600157.SH 永泰能源`、`600666.SH 奥瑞德`、`002309.SZ 中利集团`、`600166.SH 福田汽车`。
- [x] 2026-04-12 discussion 真链验证通过：已真实调用 `POST /system/discussions/cycles/bootstrap` 与 `GET /system/discussions/summary?trade_date=2026-04-12`，成功生成 `cycle-20260412`，当前 `case_count=5`，`selected=3`，`watchlist=2`。
- [x] 2026-04-12 round 驱动联调通过：已真实调用 `POST /system/discussions/cycles/2026-04-12/rounds/1/start` 与 `POST /system/discussions/cycles/2026-04-12/refresh`，cycle 已推进到 `round_1_running`，并生成 `execution_pool_case_ids=3`。
- [x] 2026-04-12 execution-intents 真链验证通过：已真实调用 `GET /system/discussions/execution-intents?trade_date=2026-04-12&account_id=8890130545`，当前返回 `status=ready`、`intent_count=3`、`blocked_count=0`，三只真实意图分别为 `600010.SH`、`600157.SH`、`600666.SH`。
- [x] 2026-04-12 dispatch 预演通过：已对上述三条真实 execution intents 调用 `POST /system/discussions/execution-intents/dispatch` 且 `apply=false`，返回 `preview_count=3`、`blocked_count=0`，已完成 `runtime -> discussion -> execution-intents -> dispatch preview` 真链闭环，未触发真实下单。
- [/] 2026-04-12 新发现的状态口径不一致：`GET /system/discussions/cycles/2026-04-12` 在 round_1 运行态下仍显示 `selected_count=0`、`watchlist_count=5`、`blockers=[selected_count_zero]`，但 `GET /system/discussions/summary` 与 `GET /system/discussions/execution-intents` 已显示 `selected=3 / intents=3`；这更像 cycle 详情快照未同步，而不是交易链阻塞。
- [/] 2026-04-12 部署态补充说明：仓库自带 `ashare_service.sh` / `ashare_scheduler_service.sh` 依赖 `sudo systemctl`，当前宿主机需要交互式密码，故本轮改为用户态直接拉起进程；当前实际运行 PID 为 `serve=848912`、`scheduler=851115`，可继续联调但尚未完成一次无密码 systemd 重启验证。

## 接 OpenClaw 真实数据前检查

- [x] OpenClaw 目标对表：量化中台主持人 `ashare` + `research/strategy/risk/audit` 四子角色 + `executor` 的职责边界与项目目标一致。
- [x] 讨论链对表：`bootstrap -> round1 -> refresh -> round2 -> finalize -> execution-intents -> dispatch` 主流程在代码与 OpenClaw prompt 中已对齐。
- [x] 权重链对表：`agent_weights` 已存在于 `team_registry.final.json`，`discussion_service.finalize/build_finalize_bundle` 已能读取并下发权重。
- [x] 飞书推送对表：`MessageDispatcher + FeishuNotifier` 主链已存在，discussion finalize / governance adjustment / trade / live execution alert 都有发送入口；真实可用性取决于 `ASHARE_FEISHU_APP_ID/SECRET/CHAT_ID`。
- [x] 2026-04-12 OpenClaw 飞书侧复核：`~/.openclaw/openclaw.json` 已存在 `channels.feishu` 与 route binding，`openclaw status --deep` 显示 `Feishu = OK`，无需另装插件。
- [x] 2026-04-12 OpenClaw 参数边界复核：已按官方文档与本地 session 错误证据确认 `streamTo` 仅适用于 ACP，不适用于 `runtime=subagent`；仓库 `openclaw/prompts/main.txt`、`openclaw/prompts/ashare.txt`、`docs/openclaw-subagent-delegation-templates.md` 与 live 工作区 `AGENTS.md/TOOLS.md` 均已收敛为最小参数集。
- [x] 2026-04-12 OpenClaw live 配置复核：`openclaw config validate --json` 已返回 `valid=true`；`openclaw agents list` 已确认 `ashare` 控制器模型为 `openai/gpt-5.4`；`~/.openclaw/workspace-ashare` 与 `~/.openclaw-quant/workspace-ashare` 经 `readlink -f` 确认为同一真实目录，不是双份漂移。
- [x] 2026-04-12 OpenClaw 运行时补丁已落仓并装载：已在共享仓库新增原生 hook 插件 `openclaw/plugins/ashare-sessions-spawn-guard`，只在 `runtime!=acp` 时拦截 `sessions_spawn` 并清洗当前已由工具实现明确判定为非法的 `streamTo` / `resumeSessionId`；`openclaw plugins inspect ashare-sessions-spawn-guard` 已确认插件在 live Gateway 中 `Status: loaded`、`Shape: hook-only`。
- [x] 2026-04-12 OpenClaw CLI 设备配对修复：已通过 `openclaw devices approve 09e8a196-a0ec-43ab-9592-602fa27e5452` 批准本机 CLI 设备，并按官方 token drift 清单执行 `openclaw devices rotate --device d0e3744c03158bc79ba44e3f675e75641c7b3fd013a4ac262bd17eedccd5f1b6 --role operator`，当前 paired device 已具备 `operator.admin / approvals / pairing / talk.secrets / write / read`。
- [x] 人机自然语言调参对表：`POST /system/adjustments/natural-language` 已可做 preview/apply，并可同步写参数提案、运行配置和治理通知。
- [x] 情况提炼回答对表：`reply-pack / final-brief / client-brief / finalize-packet` 已形成分层摘要出口，OpenClaw prompt 也已要求优先复用这些结构化摘要，不再临时手拼口径。
- [x] 执行链对表：gateway 协议闭环已通，且真实资产预检已通过 Windows `18791` HTTP 接口解除 `balance_unavailable`。
- [/] 实数接入前剩余关键项：当前重点已从账户链切换为 `external scheduler + OpenClaw runtime/discussion` 主流程接线；同时仍需补齐飞书真实凭证联调，确保最终推荐、治理调整和执行告警能推送到人。
- [x] 实数接入前行情缺口已关闭：Windows 行情元数据接口与 Linux `windows_proxy market adapter` 已全部接通，`runtime/discussion` 已完成首轮真实行情联调。
- [/] 2026-04-12 OpenClaw 最小子代理真验证仍未通过：已用 `openclaw agent --agent ashare --message "...最小健康探测..." --json` 做真实探测，但 CLI 先报 `pairing required` 回退到 embedded；embedded 会话 `~/.openclaw/agents/ashare/sessions/quant-health-20260406-1021.jsonl` 第 `86-89` 行仍实际生成带 `streamTo:"parent"`、`attachAs`、`cwd`、`cleanup`、`runTimeoutSeconds`、`timeoutSeconds` 等额外键的 `sessions_spawn`，错误仍是 `streamTo is only supported for runtime=acp; got runtime=subagent`。进一步检查 OpenClaw `2026.4.10` 安装包可见 `SessionsSpawnToolSchema` 本身把 ACP 与 subagent 字段放在同一工具定义里，执行时才做非法组合拦截，因此当前阻断已收敛为“工具 schema/运行时注入仍诱发坏参数”，不再只是提示词问题。
- [/] 2026-04-12 OpenClaw 最小子代理真验证进入下一层阻断：仓库内 guard 插件已落地并在 live Gateway 装载，但 `openclaw agent --agent ashare ... --json` 这条本地 CLI 验证链仍未拿到“明确经过 Gateway hook 执行”的证据。修复前它会因 `pairing required` 回退到 embedded；修复后虽然已完成设备批准与 token 轮换，但本地 `quant-health-20260406-1021` transcript 第 `96/101/108` 行仍持续记录带 `streamTo:"parent"` 的 `sessions_spawn` 调用并直接报错，说明至少当前 CLI 本地/回退链仍未被该 Gateway 插件覆盖。下一步需要补一条真正经过 live Gateway/Feishu/RPC 的验证链，确认插件是“未生效”还是“只对 CLI fallback 无效”。
- [/] 2026-04-13 OpenClaw 稳定版 `2026.4.11` 复测结论：已实际升级到 `2026.4.11 (769908e)`，`openclaw doctor` 显示 `ashare-sessions-spawn-guard` 仍为 loaded，但再次执行 `OPENCLAW_GATEWAY_TOKEN=... openclaw agent --agent ashare ... --json --timeout 120` 后，结果仍返回 `streamTo is only supported for runtime=acp; got runtime=subagent`。因此“切到稳定版即可解除 subagent 坏参数问题”已经被本机真验证否定。
- [/] 2026-04-13 ACP 与 subagent 边界复核：官方文档已明确区分两条链路，`openclaw acp` 是“IDE/外部 ACP client 通过桥接接入 Gateway”，而 `ACP Agents` 才是 OpenClaw 通过 `runtime:"acp"` 拉起 Codex/Claude/Gemini 等外部 harness；`subagent` 则是 OpenClaw 原生子代理运行时。因此当前 `ashare -> ashare-runtime` 这条链并不能把 `runtime=subagent` 直接替换成 `runtime=acp` 视作等价修复。
- [/] 2026-04-13 subagent 生命周期复核：官方当前文档仍保留完整 `Sub-Agents` 专页，且 `ACP Agents` 页明确写明 `sessions_spawn` 在未显式指定时默认 `runtime=subagent`；说明 `subagent` 并未在新版本被废除，仍是 OpenClaw 原生委派的默认主路径。本轮异常应归因于 `sessions_spawn` 参数注入/运行时组合 bug，而不是功能下线。
- [/] 2026-04-13 本机 ACP 可切换性复核：`openclaw plugins list --verbose` 已确认 `ACPX Runtime (acpx) disabled`，原因是 `not in allowlist`；同时 live `~/.openclaw/openclaw.json` 中 `ashare`/`ashare-runtime` 代理定义也未声明 `runtime:"acp"` 及对应 harness 目标。结论是“能不能换”不是一句配置切换，而是需要先补齐 ACPX allowlist、harness 目标、会话绑定与权限模型后，另起一条 ACP 运行链；它不能替代当前必须修通的原生 `subagent` 主链。
- [x] 2026-04-13 `sessions_spawn` 主链修复：已确认仓库内 `ashare-sessions-spawn-guard` 最初误用了 `api.registerHook(...)`，而 `before_tool_call` 的正式插件 hook 在 `2026.4.11` 实际应走 `api.on(...)` typed hook。现已改为 `api.on("before_tool_call", ...)` 并重启 `openclaw-gateway.service`；最新真验证中，gateway 日志已出现 `[ashare-sessions-spawn-guard] 已清洗非 ACP sessions_spawn 参数 runtime=subagent removed=streamTo,resumeSessionId`，`sessions_spawn` 结果也已从直接报错切换为 `status=accepted`。
- [x] 2026-04-13 `exec approvals` 家目录 symlink 阻断已解除：前序最小健康探测子会话 `d9952867-067d-4457-b727-734e4d284b6a` 确认真实卡在 `Refusing to traverse symlink in exec approvals path: /home/yxz/.openclaw`；现已按真实落地方案热修 OpenClaw 安装包 `dist/exec-approvals-BIBEOnML.js`，让 approvals 路径优先读取 `OPENCLAW_STATE_DIR`，并在项目 `.env` 写入 `OPENCLAW_STATE_DIR=/home/yxz/.openclaw-quant` 后通过 systemd 重启 live gateway。复测 `openclaw approvals get --gateway` 已恢复正常，说明主堵点已不再停在 approvals 层。
- [x] 2026-04-13 live runtime 工作区旧脚本路径漂移已清理：`/home/yxz/.openclaw-quant/workspace-ashare-runtime/TOOLS.md` 与 `AGENTS.md` 中残留的 `/mnt/d/Coding/lhjy/ashare-system-v2/scripts/ashare_api.sh`、`archive_discussion_opinions.py` 已全部改为仓库真实脚本 `/srv/projects/ashare-system-v2/scripts/ashare_api.sh`，并把大载荷 opinions 归档流程改成正式接口链 `POST /cases/{case_id}/opinions -> POST /cases/rebuild -> POST /system/discussions/cycles/{trade_date}/refresh`。
- [x] 2026-04-13 OpenClaw 最小 subagent 健康探测已越过旧路径阻断：`path-fix-retest-1` 已真实创建子会话 `c17576b4-307b-47f5-b460-f7e1afb93fc3`；主会话 `quant-health-20260406-1021.jsonl` 的内部 completion event 已明确返回 `probe` 成功、`GET /health` 成功（`status=ok`）、`GET /runtime/health` 成功（`status=ok`）。这说明 `ashare -> ashare-runtime` 的原生 `runtime=subagent` 最小健康链已实际跑通，后续阻断将进入真实 runtime/discussion/数据层，而不再是 OpenClaw 自身的 spawn、approvals 或脚本路径问题。
- [x] 2026-04-13 runtime pipeline 第一层真实堵点已定位：在最小健康链通过后，`runtime-pipeline-retest-1` 子会话 `c127bf07-ca5a-4db0-85fa-6b7c98f5a154` 首次真实执行 `POST /runtime/jobs/pipeline` 返回 `curl: (28) Operation timed out after 30002 milliseconds with 0 bytes received`。进一步核查 `ashare_api.sh` 可知脚本默认 `ASHARE_API_TIMEOUT_SECONDS=30`，因此这次失败属于本地桥接脚本超时过短，而不是 OpenClaw subagent 主链回退。
- [x] 2026-04-13 runtime pipeline 第二次复测已通过：已在 live `workspace-ashare-runtime` 约束中明确 `POST /runtime/jobs/pipeline|intraday|autotrade` 必须显式附带 `ASHARE_API_TIMEOUT_SECONDS=120`；随后 `runtime-pipeline-retest-2` 子会话 `5393c413-d529-463d-b4d3-322ce917afb5` 真实返回 `status=completed`、`trade_date=2026-04-13`、`job_id=runtime-c64521b161`、`case_count=0`、`blockers=[]`。这说明 OpenClaw 已能驱动真实 runtime pipeline 作业，当前已进入“作业结果为何为空”的业务数据层，而不再是链路层故障。
- [x] 2026-04-13 control plane 口径修复 + live 复测通过：已在 `src/ashare_system/apps/runtime_api.py:_persist_runtime_report()` 回填顶层 `case_ids/case_count`，并按 `trade_date + symbol` 过滤本次作业 case；重启 `ashare-system-v2.service` 后，真实 `POST /runtime/jobs/pipeline` 返回 `job_id=runtime-a00e6cde51`、`case_count=30`、`case_ids` 30 条，且与 `/system/cases?trade_date=2026-04-13&limit=100` 的 `count=30` 一致，确认此前的 `case_count=0` 为接口口径误报，现已修正。
- [/] 2026-04-13 新发现的性能口径：live `runtime/jobs/pipeline` 在当前 `windows_proxy + sector-members` 链路下，服务端真实耗时已超过 `120s`；`curl` 以 `ASHARE_API_TIMEOUT_SECONDS=120` 会误判超时，但把超时提高到 `240s` 后可成功拿到完整结果。后续需二选一：要么提高 OpenClaw/runtime 调用超时基线，要么继续优化 `sector-members` 遍历耗时。
- [x] 2026-04-13 OpenClaw runtime live 约束已同步修正：已把 `/home/yxz/.openclaw-quant/workspace-ashare-runtime/TOOLS.md` 与 `AGENTS.md` 中的长任务超时基线从 `120s` 上调到 `240s`，避免子代理继续把真实慢作业误报成失败。
- [/] 2026-04-13 OpenClaw 主链剩余稳定性问题已进一步收敛：`ashare -> ashare-runtime` 子代理链已真实跑出修正后的结果，但 `openclaw agent --agent ashare --json` 仍会在子任务完成前提前返回 `NO_REPLY`。这说明当前阻断已不在数据/接口层，而在主代理等待子任务完成并对 CLI 回传结果的稳定性。
- [x] 2026-04-13 `ashare` 主会话旧上下文粘连已被证实：额外执行 `openclaw agent --agent ashare --session-id ashare-cli-isolated-20260413-01 --message "...最小健康探测..." --json` 后，返回元数据里的实际 `sessionId` 仍是旧的 `quant-health-20260406-1021`，说明这条 CLI 调用并不会给 `agent:ashare:main` 新建隔离会话，而是继续复用旧主会话。因此此前多轮 prompt/规则修改后仍立刻 `NO_REPLY`，不能再简单视为“新参数没生效”，还要考虑旧 session 行为惯性。
- [x] 2026-04-13 `ashare` 主会话已按官方方式滚动到新 `sessionId`：执行 `openclaw agent --agent ashare --message "/reset" --json --timeout 120` 后，主会话已从 `quant-health-20260406-1021` 切到新的 `5ab55308-c09d-4ca2-b459-1a7fd06a866f`，且返回正常问候语而非 `NO_REPLY`。这证明 `/reset` 能真实清掉旧会话粘连，同时 bootstrap 约束已重新装载。
- [/] 2026-04-13 `/reset` 后主链阻断再次下沉：在新 `sessionId=5ab55308-c09d-4ca2-b459-1a7fd06a866f` 上重跑 `openclaw agent --agent ashare --message "[2026-04-13 11:36 CST after-reset-retest-1] ...最小健康探测..." --json --timeout 240`，CLI 不再像旧会话那样 4-6 秒内直接回 `NO_REPLY`，说明“旧 session 粘连导致立即静默”这层问题已缓解；但同一轮随后真实报出新的模型层故障：`openai/gpt-5.4` 返回 `403 用户额度不足`，`google-gemini-cli/gemini-3.1-pro-preview` 为 `spawn gemini ENOENT`，`MiniMax-M2/M2.1/M2.7/M2.5` 系列全部为 `token plan not support model (2061)`，`anthropic/claude-sonnet-4-6` 为 `503 无可用渠道`。因此当前主阻断已从“主代理过早 `NO_REPLY`”进一步下沉为“主控模型/回退链当前不可用”，在修复模型侧之前，新的最小健康探测仍无法真正跑到 `ashare-runtime`。
- [/] 2026-04-13 OpenClaw 模型拓扑复核：live `~/.openclaw-quant/openclaw.json` 当前全局默认模型是 `minimax/MiniMax-M2.7`，而量化团队是按 agent 单独绑定模型：`ashare/openai-gpt-5.4`、`ashare-runtime/openai-gpt-5.4`、`ashare-research/minimax-M2.7`、`ashare-strategy/openai-gpt-5.4`、`ashare-risk/openai-gpt-5.4`、`ashare-executor/claude-sonnet-4-6`、`ashare-audit/openai-gpt-5.4`。配置中还存在更大上下文候选 `google-gemini-cli/gemini-3.1-pro-preview` 与 `openai-compatible/claude-opus-4-6`，但当前前者缺本机 `gemini` 可执行入口，后者未被纳入默认回退链，也还没有真验证可用性，因此现在不能把“切大上下文模型”当成已就绪解法。
- [x] 2026-04-13 正式测试服务状态复核：`ashare-system-v2.service` 与 `openclaw-gateway.service` 当前均为 `active/running`，`GET /health`、`GET /runtime/health`、`openclaw gateway health` 均正常。
- [x] 2026-04-13 scheduler 已完成 systemd 化：`ashare-system-v2-scheduler.service` 已安装到 `/etc/systemd/system/`，并处于 `enabled + active(running)`；先前为应急联调手工拉起的重复 scheduler 进程也已停掉，当前仅保留 systemd 常驻实例，避免双调度。
- [x] 2026-04-13 重启后真实入口复核：`/srv/projects/ashare-system-v2/scripts/ashare_api.sh` 实际存在且可执行，`ashare-system-v2.service`、`ashare-system-v2-scheduler.service`、`openclaw-gateway.service` 三条正式服务均已恢复；因此“ashare_api.sh 路径不存在导致昨晚到现在复盘/学习记录不可读”的判断不成立，至少控制面入口已恢复。
- [/] 2026-04-13 记录入口现状复核：`/system/audits`、`/system/reports/runtime`、`/system/reports/postclose-master`、`/system/research/summary` 当前都可访问；其中 `runtime` 最新作业为 `2026-04-13 02:09:40` 的 `runtime-abb588287b`，`case_count=30`。`/system/meetings/latest` 仍返回 `{"available":false}`，说明“会议最新记录缺失”是真缺口，但不是统一入口失效。
- [x] 2026-04-13 主代理误判纠偏：已同步修正 live `workspace-ashare/TOOLS.md` 与 `AGENTS.md`，明确要求 `ashare` 在声称“路径不存在 / 服务未恢复”前，必须先真实检查 `/srv/projects/ashare-system-v2/scripts/ashare_api.sh` 并至少实测一次 `probe` 或 `GET /health`；同时明确 `/system/meetings/latest={"available":false}` 只能表述为“会议记录缺失”，不能扩大误判为整个复盘/学习入口失效。
- [/] 2026-04-13 OpenClaw 升级后新增异常：`logs/openclaw-gateway.log` 在 `00:21:08-00:21:10` 持续报 `bundled plugin entry "./api.js" failed to open from ".../dist/extensions/qa-channel/index.js"`。进一步核查本机安装目录 `~/.npm-global/lib/node_modules/openclaw/dist/extensions/qa-channel/` 与 `openclaw@2026.4.11` 官方 npm tarball `/tmp/openclaw-2026.4.11.tgz`，两者都只包含 `runtime-api.js`，不存在 `index.js` / `api.js`。因此这不是本机安装损坏，而是 `2026.4.11` npm 发布包本身就缺 `qa-channel` 运行时文件。
- [/] 2026-04-13 上游证据补齐：官方 GitHub 已至少存在 `#43556`、`#53016`、`#53370` 三条关于 `sessions_spawn` 在 `runtime=subagent` 下被 ACP 专属 `streamTo` / `resumeSessionId` 干扰的 issue；而官方 `2026.4.11` release notes 未提到这条修复，且本机真验证也确认稳定版仍未解决。
- [/] 2026-04-12 OpenClaw 运行稳定性剩余风险：`openclaw status --deep` 与 `quant-health` 会话日志显示 13:57-14:02 期间发生过多次 provider/model fallback 漂移，曾自动尝试 `MiniMax-M2/M2.1/M2.5/M2.7`、`claude-sonnet-4-6` 等不可用模型；虽然当前活跃 `ashare` 会话又回到 `gpt-5.4`，但默认回退链仍不够稳。
- [x] 2026-04-13 Hermes Agent 已按官方默认目录安装：官方安装脚本已把仓库落到 `~/.hermes/hermes-agent`，后续为避免 `[all]` 超重依赖拖慢调试，已在同一默认目录用最小功能集完成 `hermes-agent==0.8.0` 安装；当前 `~/.hermes/hermes-agent/venv/bin/hermes --version` 已正常返回 `Hermes Agent v0.8.0 (2026.4.8)`。
- [x] 2026-04-13 Hermes 官方迁移链已实测可用：`hermes claw migrate --source ~/.openclaw-quant --preset full --migrate-secrets --yes` 已真实执行完成，结果把 `~/.openclaw-quant` 中的 provider keys、部分 model config、gateway token 和自定义 provider 片段迁入 `~/.hermes/{.env,config.yaml}`；但未自动迁入量化专用 `workspace-agents / SOUL / memory / cron / hooks / mcp-servers`，说明 Hermes 当前只适合作为“备用大脑底座”，不能无缝接管现有量化团队结构。
- [/] 2026-04-13 Hermes 控制面形态已确认：默认形态没有类似 OpenClaw Control UI 的独立网页控制台，当前主控制面是 CLI + 配置文件 + gateway 服务管理。实际入口包括 `hermes profile`（多实例隔离）、`hermes gateway`（消息网关前台/后台/安装服务）、`hermes config`（读写 `~/.hermes/config.yaml`）、`hermes auth`（凭据池）、`hermes sessions`、`hermes logs`。
- [/] 2026-04-13 Hermes on MiniMax 最小烟测未通过：已直接执行 `hermes chat -q "只回复 smoke-ok" -Q --provider minimax -m MiniMax-M2.7`，返回 `Anthropic 401 authentication failed`，token 前缀对应迁移进来的 MiniMax key。该现象说明 Hermes 至少已把请求打到 provider 适配层，但当前 MiniMax 路由/鉴权映射仍需重新梳理，暂时还不能把它视为已可替换 OpenClaw 的备用运行时。
- [/] 2026-04-13 Hermes 已进入 MiniMax-only 收敛阶段：`~/.hermes/config.yaml` 已把主模型、压缩、辅助与 delegation 全部收束到 `MiniMax-M2.7`，但当时 `provider` 仍统一写成 `minimax`，`.env` 里也同时保留了 `MINIMAX_* / MINIMAX_CN_* / ANTHROPIC_*` 三组同 key 变量，因此这一步只能说明“模型已统一到 MiniMax”，还不能说明“provider 路由已统一到正确入口”。
- [/] 2026-04-13 Hermes MiniMax-only 第一轮复测结论：执行 `hermes chat -q "只回复 smoke-ok" -Q --provider minimax -m MiniMax-M2.7` 仍返回 `Anthropic 401 authentication failed`。结合 Hermes 本地 provider 注册表与测试，这个失败更像是请求被路由到 `minimax` 的全局入口，而不是中国区 `api.minimaxi.com` 入口。
- [x] 2026-04-13 Hermes 路由纠偏完成：已通过本地代码与文档确认 `minimax-cn` 才对应 `MINIMAX_CN_API_KEY / MINIMAX_CN_BASE_URL=https://api.minimaxi.com/anthropic`，而 `minimax` 默认对应的是另一套入口；随后真实执行 `~/.hermes/hermes-agent/venv/bin/hermes chat -q "只回复 smoke-ok" -Q --provider minimax-cn -m MiniMax-M2.7`，返回 `smoke-ok`，`session_id=20260413_103125_348f84`。这说明用户提供的 MiniMax key 在 Hermes 中可用，但正确接法应优先固定为 `minimax-cn`。
- [x] 2026-04-13 Hermes 官方文档对齐后的协作模式已重构：已根据 Hermes `delegation / cron / context files / SOUL.md / Feishu / profiles` 官方文档，确认备用控制面不应照搬 OpenClaw 的“6 个常驻命名代理开会”模式，而应改成“1 个主协调 + 最多 3 并发子代理波次 + cron 常驻值班 + Feishu 回流”的原生编排。
- [x] 2026-04-13 Hermes 项目上下文与提示词模板已落仓：已新增仓库级 `/.hermes.md` 作为 Hermes 项目规则入口，并新增 `docs/hermes_backup_operating_model.md` 与 `hermes/prompts/*.md` 一组角色/cron 提示词模板，覆盖运行侦察、事件研究、策略分析、风控闸门、审计复核、执行代理，以及盘中巡检、持仓复核、盘后学习、夜间沙盘四类常驻任务。
- [x] 2026-04-13 Hermes 备用 profile 已隔离：已真实创建 `ashare-backup` profile，并把其 `SOUL.md` 改写为交易台专用人格；该 profile 继续沿用 `minimax-cn` 作为唯一默认 provider，后续可独立承接 Feishu / cron / 备用接管联调，而不污染默认 profile。
- [x] 2026-04-13 Hermes 备用控制面已进入“可值班”状态：已把 `ashare-backup` profile 的 `terminal.cwd` 固定到仓库根目录、`timezone` 固定为 `Asia/Shanghai`、`display.personality` 收束为 `concise`，并新增 `scripts/bootstrap_hermes_ashare_backup.sh` 与 `docs/hermes_backup_bootstrap.md` 作为可重复执行的 bootstrap 资产。
- [x] 2026-04-13 Hermes cron 值班任务已实装：已向 `ashare-backup` 真实写入 7 条值班任务，覆盖盘前准备、盘中巡检、持仓复核、盘后学习、夜间沙盘；当前默认投递为 `local`，后续只需把 `HERMES_CRON_DELIVER` 切到 `feishu` 并补全 Feishu 参数即可转为飞书回流。
- [x] 2026-04-13 Hermes gateway 已常驻：已为 `ashare-backup` 安装并启动 user-level systemd 服务 `hermes-gateway-ashare-backup.service`，当前 `gateway status` 已显示 `active (running)`；这意味着 cron 不再停留在配置态，而是进入自动触发态。
- [x] 2026-04-13 Hermes 自动值班已出现真实执行：`ashare-intraday-watch-am` 与 `ashare-position-watch-am` 已分别在 `11:02:23`、`11:02:45` 自动执行，`jobs.json` 已记录 `last_status=ok`、`completed=1`，并在 `~/.hermes/profiles/ashare-backup/cron/output/...` 生成本地结果文件；本次两条任务均返回 `[SILENT]`，说明至少调度、模型调用和本地落盘链已贯通。
- [x] 2026-04-13 Hermes 业务值班已产出真实业务摘要：手动触发 `ashare-preopen-readiness` 后，已在 `11:06:16` 生成一份盘前准备报告，明确给出 `status=ready`、执行池现状、优先盯盘方向、仓位缺口与关键风险提示，说明 Hermes 备用控制面已能基于本地真实接口生成业务可用摘要，而不只是吐 `[SILENT]`。
- [/] 2026-04-13 Hermes 值班首次暴露的真实业务堵点：
  - 盘前报告明确指出 runtime context / dossiers 仍停留在隔夜时点，盘中未见新一轮 runtime 刷新，30 个 dossier 的 `bull_evidence / bear_evidence / board_success_rate_20d / bomb_rate_20d` 等关键字段缺失，说明“值班链已通”但“实时数据刷新质量”仍不足。
  - 同一份报告指出 `execution_bridge` 总体 `healthy`，但 `windows_execution_gateway` 与 `qmt_vm` 同时 `reachable=false`，说明执行桥健康口径与底层 reachability 之间仍存在不一致，后续需要继续沿业务口径核查。
- [/] 实数接入前剩余关键项：手工 `round_1/start + refresh` 已能把真实候选推进到 `execution-intents + dispatch preview`，下一步需要让 OpenClaw 或 control plane 自动承担这套 round/finalize 驱动，而不是依赖手工 curl。
- [/] 实数接入前剩余关键项：当前还存在 `cycle detail` 与 `summary/intents` 的 selected/watchlist 统计口径不一致，需要补一次状态同步或展示修正，避免再次出现错报。
- [/] 2026-04-12 OpenClaw 安全/运维剩余风险：`openclaw doctor` 仍提示 session store `1/3 recent sessions missing transcripts`、存在 2 个 orphan transcript；`openclaw status --deep` 的 security audit 仍有 `control UI device auth disabled`、`allowInsecureAuth=true`、`credentials dir mode=775`、`gateway.bind=lan` 且未配 `auth.rateLimit`。这些会影响远程访问方式与现有链路，本轮尚未擅自改动。

## OpenClaw 智能化草案

- [x] 2026-04-12 已完成 OpenClaw 现状复核：当前 `workflow.final.json + team_registry.final.json + prompts/*` 已具备路由、两轮讨论、治理接口和摘要出口，但整体仍偏“结构化 opinion 收集器”，人格差异和主持人追问策略不足。
- [x] 2026-04-12 已新增剧本草案：`docs/openclaw_quant_playbook_draft_20260412.md`
- [x] 2026-04-12 草案已扩展为“全天候值班团队”方向：新增盘前/盘中/盘后/夜间值班分工、`opportunity_ticket` 机会线索机制、团队策略/权重提案权、盘后学习产出与自进化链路衔接。
- [x] 2026-04-12 草案已继续扩展为“交易台纪律”方向：新增目标仓位义务、持仓持续复核、出现更优机会时的替换仓位机制、持仓股日内 T 机会管理，以及多策略全天候扫描框架。
- [x] 2026-04-12 用户已同意“全天候值班 + 机会线索发现 + 策略/权重提案 + 交易台纪律”方向，现已正式改写：
  - `openclaw/prompts/main.txt`
  - `openclaw/prompts/ashare.txt`
  - `openclaw/prompts/ashare-runtime.txt`
  - `openclaw/prompts/ashare-research.txt`
  - `openclaw/prompts/ashare-strategy.txt`
  - `openclaw/prompts/ashare-risk.txt`
  - `openclaw/prompts/ashare-audit.txt`
  - `openclaw/prompts/ashare-executor.txt`
  - `openclaw/workflow.final.json`
  - `openclaw/team_registry.final.json`
- [x] 2026-04-13 架构边界曾做过一次更严收口：当时先把部分文案临时收束到“程序主导正式入口、agent 主要消费服务接口”的方向，用于止住前一版把 agent 写得过于无边界的问题；该中间态口径已在下一条记录中进一步修正为最终版“agent 是大脑，程序是手脚和围栏”。
- [x] 2026-04-13 架构边界再次细化并修正过度收口：已根据用户进一步澄清，把口径统一修正为“agent 是大脑，负责自主协作、发现机会、形成提案与完成量化交易判断；程序是手和脚，负责数据提供、执行落地、流程监督、业绩考核、风控数据与电子围栏”。相关修正已同步落到 `/.hermes.md`、`docs/hermes_backup_operating_model.md`、`hermes/prompts/*`、`openclaw/prompts/*`、`openclaw/workflow.final.json`、`openclaw/team_registry.final.json`。
- [x] 2026-04-13 飞书三权入口已落地到控制面 API：
  - `知情权`：新增 `/system/feishu/rights`、`/system/feishu/briefing`、`/system/feishu/briefing/notify`，把 workspace/cadence/discussion/execution/agent-scores 汇成飞书可直接消费的简报，并支持主动推送。
  - `调参权`：新增 `/system/feishu/adjustments/natural-language` 作为飞书别名入口，直接复用现有自然语言调参能力，支持预览、落地和通知。
  - `询问权`：新增 `/system/feishu/ask`，把常见问题落到现有真实摘要上，支持 `status / discussion / execution / params / scores` 五类问答，不编造行情。
  - `催办回写权`：新增 `/system/feishu/supervision/ack`，支持把飞书机器人或用户发来的“研究已收到催办”“风控已处理”等文本解析成监督确认回写，并复用 `/system/agents/supervision/ack` 正式落库。
  - `事件订阅入口`：新增 `/system/feishu/events`，支持飞书 `url_verification` 校验，以及 `im.message.receive_v1` 入站消息事件转 supervision ack；若配置 `ASHARE_FEISHU_VERIFICATION_TOKEN`，会做 token 校验。
  - `后台配置查看`：新增 `/system/feishu/events/config`，返回 callback URL、事件类型、token 是否已配置和推荐说明，便于直接照着飞书后台填写。
  - 同步新增模板：`notify/templates.py` 增补飞书简报与飞书问答模板。
  - 已补测试并通过：`tests/test_upgrade_workflow.py` 现已覆盖 `rights / briefing / ask / feishu adjustment alias`。
- [x] 2026-04-13 agent 接口面、机器人版面和主线流程已正式编排：
  - 新增 `/system/agents/capability-map`，把各 agent 的职责、推荐读取顺序、全局入口和可写动作做成结构化接口。
  - 新增 `/system/robot/console-layout`，把机器人面板收敛为 `当前状态 / 当前建议 / 执行状态 / 调参与治理 / 问答入口` 五块。
  - 新增 `/system/workflow/mainline`，把盘前预热、盘中发现、讨论收敛、执行预演、通知调参考核、盘后学习夜间沙盘编成标准主线。
  - 新增文档：`docs/agent_robot_mainline_workflow.md`，把 agent 接口面、机器人版面和主线流程统一写清。
  - 已同步更新：`openclaw/workflow.final.json` 增加 `mainline_workflow` 引用；`hermes/prompts/README.md` 增补三个入口说明。
  - 已补测试并通过：`tests/test_upgrade_workflow.py` 现已覆盖 `capability-map / robot console layout / workflow mainline`。
- [x] 2026-04-13 机器人自动催办主链已落地：
  - 新增 `/system/agents/supervision-board`，把 `ashare/runtime/research/strategy/risk/audit/executor` 当前是否该工作、是否超时、原因和最近活跃时间做成结构化监督板。
  - 新增 `/system/agents/supervision/check`，支持基于监督板结果触发自动催办；当前默认口径是“人工只看重要业务消息，值班催办和升级由机器人自动完成”。
  - 新增 `/system/agents/supervision/ack`，支持把“已收到催办/已转入处理”的确认状态回写到监督链，当前会按同一 attention signature 抑制重复催办，并在监督板上标出 `acknowledged / acknowledged_at / acknowledged_by / ack_note`。
  - 新增模板：`notify/templates.py` 增补 `agent_supervision_template`，用于飞书机器人催办消息。
  - 当前监督规则已覆盖：`candidate_poll` 到点、讨论轮次已开始但四个讨论 agent 未覆盖、execution intents 已生成但尚无 dispatch 回执。
  - 已补测试并通过：`tests/test_upgrade_workflow.py` 新增监督板、催办检查与 ack 抑制测试，当前全量 `27/27` 通过。
- [x] 2026-04-13 自动监督调度与飞书双路由已落地：
  - `scheduler.py` 已新增 `supervision.agent:check`，当前按 `*/3 9-15 * * 1-5` 自动执行 Agent 监督巡检，不再只依赖手动调用 `/system/agents/supervision/check`。
  - `container.py / settings.py / notify/feishu.py / notify/dispatcher.py` 已支持飞书双路由：`重要消息` 与 `监督催办` 可拆到不同 chat；若未单独配置，则继续回落到默认 chat，保持兼容。
  - 当前重要消息口径：最终推荐/阻断简报、真实买卖回执、实盘执行告警、盘后战果与学习摘要。
  - 当前机器人消息口径：值班催办、超时升级、流程阻塞提醒。
  - 监督接口已做一轮降延迟重构：`/system/agents/supervision-board` 与 `/system/agents/supervision/check` 不再顺带调用整套 `feishu_briefing` 与 `execution_precheck/intents`，改为只读取 `polling_state + cycle + latest_execution_dispatch/precheck + pending intents` 等本地轻状态，避免每次催办都触发账户/持仓/逐票快照查询。
  - 已补测试并通过：`tests/test_upgrade_workflow.py` 已同步校验 `supervision.agent:check` 调度注册、ack 抑制、“监督接口不触发实时报价/账户查询”回归、飞书文本回写解析、飞书事件订阅入站以及配置查看接口，当前全量 `27/27` 通过。
  - 已完成真实联调复核：重启 `ashare-system-v2.service` 后，`/system/agents/supervision-board` 实测约 `1.1s` 返回 `200`，`/system/agents/supervision/check` 实测约 `1.8s` 返回 `200`；较上一轮 “15s 超时但服务端最终 200” 已明显收敛。
  - 已完成飞书回写联调复核：重启控制面后，`POST /system/feishu/supervision/ack` 用真实 HTTP 文本 `研究已收到催办，转入处理` 实测约 `2.9s` 返回 `200`，成功解析并回写 `ashare-research`。
  - 已完成飞书事件订阅联调复核：重启控制面后，`POST /system/feishu/events` 的 `url_verification` 实测约 `0.001s` 返回 challenge；模拟 `im.message.receive_v1` 文本消息实测约 `3.0s` 返回 `200`，并成功回写 `ashare-research`。
  - 已完成飞书后台配置接口联调复核：重启控制面后，`GET /system/feishu/events/config` 实测约 `0.001s` 返回 `200`；当前返回的 callback URL 为 `http://192.168.99.16:8100/system/feishu/events`，且 `verification_token_configured=false`，说明正式接飞书前仍需补 `ASHARE_PUBLIC_BASE_URL` 和 `ASHARE_FEISHU_VERIFICATION_TOKEN`。
  - 已按用户指定把控制面公开地址切换为 `https://yxzmini.com/pach`，用于生成带前缀的飞书事件回调地址；下一步以重启后 `/system/feishu/events/config` 的实际返回为准。
  - 已完成 `yxzmini.com/pach` 候选入口真实性复核：本机服务正常，但 Ubuntu 服务器当前 `curl https://yxzmini.com/pach/...` 仍报 `Could not resolve host: yxzmini.com`，且系统内未发现现成的 `pach` 反向代理配置；因此当前只能确认“程序侧入口口径已切换”，还不能宣称“飞书已可经该域名真实回调”。
  - 已补 `url.preview.get` 入站支持：`/system/feishu/events` 现已兼容飞书把 verification token 放在 `header.token` 的写法，并能对 `url.preview.get` 返回 `inline` 链接预览摘要。
  - 已完成本轮重启后复核：`GET /system/feishu/events/config` 现已真实返回 `callback_url=https://yxzmini.com/pach/system/feishu/events`，`expected_event_types` 已包含 `im.message.receive_v1` 与 `url.preview.get`。
  - 已完成本地链接预览联调：模拟 `POST /system/feishu/events` 发送 `url.preview.get` 到 `https://yxzmini.com/pach/system/agents/supervision-board`，控制面已真实返回 `inline.title=Agent 监督看板`。
  - 已完成飞书官方文档复核：按 `服务端 SDK 概述 / Python SDK 处理事件 / 处理回调` 官方文档，长连接模式只要求“运行环境可访问公网”，不需要提供公网 IP 或域名；`im.message.receive_v1` 与 `url.preview.get` 均支持长连接。
  - 已完成长连接 worker 落地：新增 `src/ashare_system/feishu_longconn.py`，通过飞书官方 `lark-oapi` SDK 建立 WebSocket 长连接，并把 `im.message.receive_v1 / url.preview.get` 转发到本地 `/system/feishu/events`，复用现有控制面处理逻辑。
  - 已完成运行入口与部署脚本：`ashare_system.run` 新增 `feishu-longconn` 命令；新增 `scripts/ashare_feishu_longconn_service.sh` 与 `scripts/install_feishu_longconn_service.sh`，用于独立后台进程与 systemd 服务安装。
  - 已完成依赖与配置补齐：`pyproject.toml` 已加入 `lark-oapi`；`.env.example` 与当前 `.env` 已补 `ASHARE_FEISHU_CONTROL_PLANE_BASE_URL=http://127.0.0.1:8100`。
  - 已完成真实长连接连通验证：使用当前飞书应用凭证实际执行 `timeout 15s /srv/projects/ashare-system-v2/.venv/bin/python -m ashare_system.run feishu-longconn`，控制台真实返回 `connected to wss://msg-frontier.feishu.cn/...`，说明长连接链路已成功建立。
  - 已完成临时在线拉起：当前已以用户后台进程方式启动 `ashare_system.run feishu-longconn`，进程可见，主日志已记录 `飞书长连接启动` 与 `connected to wss://msg-frontier.feishu.cn/...`，因此当前飞书主接入链已经在线。
  - 已补“控制面链接按内部语义解释并回群”链路：当群消息里出现 `https://yxzmini.com/pach/system/workflow/mainline`、`/system/robot/console-layout`、`/system/agents/supervision-board`、`/system/feishu/briefing`、`/system/feishu/rights`、`/system/feishu/ask` 这类控制面入口时，`/system/feishu/events` 现会优先按程序内部 endpoint 生成说明，不再把它误判成“需要公网可访问”的普通网页；飞书长连接 worker 也已支持把该说明直接回发到原 `chat_id`。
  - 已完成本地联调复核：重启 `serve` 后，实测 `POST /system/feishu/events` 发送 `@机器人 https://yxzmini.com/pach/system/workflow/mainline` 样例，真实返回 `reason=control_plane_link_explained`，且 `reply_lines` 已按“交易主线流程”内部语义生成，不再回复“内网域名不可访问”口径。
  - 已补普通群消息自然语言问答自动回复：对 `@机器人` 或单聊发来的文本，`/system/feishu/events` 现会在 supervision ack 与控制面链接解释未命中时，自动落到现有 `_answer_feishu_question` 真摘要能力，支持 `status / discussion / execution / params / scores / help` 六类口径；未 @ 机器人的普通群消息仍保持忽略，避免机器人在群里乱插话。
  - 已修复两类飞书问答联调故障：1) 真实群消息里若只出现 `@南风·量化 ...` 文本而飞书未附带 `mentions` 数组，现也会被识别为发给机器人；2) `有没有执行回执` 这类 execution 问答先前会因缺少 `execution_precheck_summary_lines` import 触发 `500`，现已修复，且飞书事件问答回包已改为轻量版，避免长连接因返回体过大/过慢而超时。
  - 已把飞书问答继续从“固定 FAQ”推向“意图路由”：`/system/feishu/ask` 当前已新增 `supervision / research / risk / symbol_analysis` 主题分流，`agent 活跃度怎么样` 会走监督摘要，`最近研究结论有什么` 会走研究摘要，`当前有哪些风控阻断` 会走 execution precheck 风控摘要；同时修复了 `金风科技。` 这类只带标点的短句追问仍应命中个股分析的问题。
  - 已继续把飞书前台问法往交易台靠拢：`/system/feishu/ask` 当前又补了 `opportunity / position / replacement` 三类意图，`现在有哪些机会票` 会直接复用 `client_brief` 的 selected/watchlist 摘要，`当前仓位为什么这样配` 会复用 `execution_precheck` 的仓位与预算约束，`有没有更好的替换建议` 会把观察池与执行预检一起作为替换参考，而不再退回通用帮助提示。
  - 已继续把飞书前台补到“持仓复核 / 做T复核”层：`/system/feishu/ask` 当前新增 `holding_review / day_trading` 两类意图，`当前持仓复核一下` 会优先复用 `account_state + latest_execution_reconciliation` 的正式持仓快照，`今天有没有做T机会` 会优先复用 `latest_tail_market_scan + tail-market review` 的正式信号摘要，而不再退回帮助提示。
  - 当前仍有一个遗留问题待后续修：`GET /system/feishu/longconn/status` 还会读到旧缓存状态，不能作为本轮长连接在线与否的唯一依据；本轮飞书在线性以真实前台长连接连接日志为准。
  - systemd 正式化仍差一项权限：仓库内安装脚本已备好，但当前写 `/etc/systemd/system/ashare-feishu-longconn.service` 仍报 `Permission denied`，因此暂未宣称“系统服务安装完成”。
- [/] 后续迭代重点：
  - 真实行情接口已接上，下一步结合 `runtime/discussion` 真联调反馈继续微调各角色 prompt
  - 后续若后端补齐“服务派单 / review gate / ticket 固化 / agent 绩效考核”标准接口，可继续把当前协作制度下沉为正式 API/状态机，进一步把“自主协作 + 服务围栏”从 prompt 约束变成系统约束
  - 监督链下一步可继续补：多级升级策略，以及“重要消息白名单”与“普通催办静默策略”的进一步精细化配置
  - 飞书当前主推荐链路已切为长连接；公网回调 URL 相关的 DNS/反代改为备用接法，不再是主阻断。若后续仍需保留 webhook 备用链，则仍需把 `yxzmini.com` 的 DNS/反代真正指到控制面。

## 验证记录

- [x] `python3 -m compileall src/ashare_system tests/test_upgrade_workflow.py`
- [x] `PYTHONPATH=/srv/projects/ashare-system-v2/src /srv/projects/ashare-system-v2/.venv/bin/python -m unittest discover -s /srv/projects/ashare-system-v2/tests -p "test_*.py"`（2026-04-13，12/12 通过；含 `test_runtime_pipeline_response_exposes_case_ids_and_case_count`）
- [x] `PYTHONPATH=/srv/projects/ashare-system-v2/src /srv/projects/ashare-system-v2/.venv/bin/python -m unittest discover -s /srv/projects/ashare-system-v2/tests -p "test_*.py"`（2026-04-13，本轮 `28/28` 通过；已新增 `url.preview.get` 与 `header.token` 回归）
- [x] `PYTHONPATH=/srv/projects/ashare-system-v2/src /srv/projects/ashare-system-v2/.venv/bin/python -m unittest discover -s /srv/projects/ashare-system-v2/tests -p "test_*.py"`（2026-04-13，本轮 `31/31` 通过；已新增飞书长连接 bridge 回归）
- [x] `timeout 15s /srv/projects/ashare-system-v2/.venv/bin/python -m ashare_system.run feishu-longconn`（2026-04-13）-> 使用真实飞书凭证已成功连上 `wss://msg-frontier.feishu.cn`
- [x] `python3 -m json.tool openclaw/team_registry.final.json`
- [x] `python3 -m json.tool openclaw/workflow.final.json`
- [x] `"/home/yxz/.npm-global/bin/openclaw" config validate --json` -> `valid=true`
- [x] `"/home/yxz/.npm-global/bin/openclaw" doctor` -> 飞书 allowlist 问题已消失，剩余 session integrity / security 风险
- [x] `"/home/yxz/.npm-global/bin/openclaw" agents list` -> `ashare` 当前模型为 `openai/gpt-5.4`
- [x] `"/home/yxz/.npm-global/bin/openclaw" models status --json`（2026-04-13）-> 已确认全局默认模型为 `minimax/MiniMax-M2.7`，fallback 包含 `google-gemini-cli/gemini-3.1-pro-preview`、`anthropic/claude-sonnet-4-6`、`openai/gpt-5.4`，允许模型中还含 `openai-compatible/claude-opus-4-6`
- [x] `sed -n '130,330p' /home/yxz/.openclaw-quant/openclaw.json`（2026-04-13）-> 已确认量化各 agent 的显式模型绑定：`ashare/openai-gpt-5.4`、`runtime/openai-gpt-5.4`、`research/minimax-M2.7`、`executor/claude-sonnet-4-6` 等
- [x] `"/home/yxz/.npm-global/bin/openclaw" agent --agent ashare --session-id ashare-cli-isolated-20260413-01 --message "[2026-04-13 11:25 CST isolated-session-retest-1] ..."`（2026-04-13）-> 返回元数据中的实际 `sessionId` 仍为 `quant-health-20260406-1021`，确认 `--session-id` 没有为 `agent:ashare:main` 新起隔离主会话
- [x] `"/home/yxz/.npm-global/bin/openclaw" agent --agent ashare --message "/reset" --json --timeout 120`（2026-04-13）-> 已把 `agent:ashare:main` 滚到新 `sessionId=5ab55308-c09d-4ca2-b459-1a7fd06a866f`
- [/] `"/home/yxz/.npm-global/bin/openclaw" agent --agent ashare --message "[2026-04-13 11:36 CST after-reset-retest-1] ..."`（2026-04-13）-> 不再快速返回 `NO_REPLY`，但随后真实进入模型回退失败：`openai/gpt-5.4=403 额度不足`、`google-gemini-cli/gemini-3.1-pro-preview=spawn gemini ENOENT`、`MiniMax` 系列=`plan not support model (2061)`、`anthropic/claude-sonnet-4-6=503 无可用渠道`
- [x] `curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-setup`（2026-04-13）-> 官方默认目录 `~/.hermes/hermes-agent` 已落盘；安装脚本在可选 `ffmpeg`/build-tools 提示处不适合无密码自动化，后续改为在同目录补装最小依赖
- [x] `uv pip install --python ~/.hermes/hermes-agent/venv/bin/python -e ~/.hermes/hermes-agent[cron,cli,mcp,pty,feishu,acp]`（2026-04-13）-> 已完成 Hermes 最小可运行依赖安装，CLI 可正常启动
- [x] `~/.hermes/hermes-agent/venv/bin/hermes claw migrate --source ~/.openclaw-quant --preset full --migrate-secrets --yes`（2026-04-13）-> 真实迁入 10 项，2 项 env conflict，28 项 skipped；完整报告位于 `~/.hermes/migration/openclaw/20260413T095821`
- [x] `~/.hermes/hermes-agent/venv/bin/hermes profile list`（2026-04-13）-> 已确认 Hermes 原生支持 `profile` 多实例隔离，适合做双备份控制面
- [x] `~/.hermes/hermes-agent/venv/bin/hermes status`（2026-04-13）-> 已确认当前 gateway 未启动，控制面入口为 CLI；迁移后默认模型显示为 `minimax/MiniMax-M2.7`
- [/] `~/.hermes/hermes-agent/venv/bin/hermes chat -q "只回复 smoke-ok" -Q --provider minimax -m MiniMax-M2.7`（2026-04-13）-> 返回 `Anthropic 401 authentication failed`，说明 Hermes 当前 MiniMax 适配仍需重新配置
- [x] `apply_patch ~/.hermes/config.yaml`（2026-04-13，第一轮）-> 已把 Hermes 默认、压缩、辅助、delegation 的模型统一到 `MiniMax-M2.7`，但 provider 当时仍写成 `minimax`
- [x] `apply_patch ~/.hermes/.env`（2026-04-13，第一轮）-> 已将 Hermes 相关 key 基本收束到 MiniMax 体系，但随后复核发现仍同时残留 `MINIMAX_* / MINIMAX_CN_* / ANTHROPIC_*` 三组变量，尚未彻底消除 provider 漂移因素
- [x] `~/.hermes/hermes-agent/venv/bin/hermes status`（2026-04-13）-> 已确认当时默认 `Provider: MiniMax`
- [/] `~/.hermes/hermes-agent/venv/bin/hermes chat -q "只回复 smoke-ok" -Q --provider minimax -m MiniMax-M2.7`（2026-04-13，第一轮复测）-> 仍返回 `Anthropic 401 authentication failed`
- [x] `sed -n '1,220p' ~/.hermes/.env && sed -n '1,260p' ~/.hermes/config.yaml && rg -n "minimax-cn|MINIMAX_CN|api.minimaxi.com/anthropic" ~/.hermes/hermes-agent/{hermes_cli,tests,website/docs}`（2026-04-13）-> 已确认当前漂移点：配置仍偏向 `minimax`，而 Hermes 本地实现与文档明确将中国区 MiniMax 绑定到 `minimax-cn`
- [x] `~/.hermes/hermes-agent/venv/bin/hermes chat -q "只回复 smoke-ok" -Q --provider minimax-cn -m MiniMax-M2.7`（2026-04-13）-> 真实返回 `smoke-ok`，`session_id=20260413_103125_348f84`
- [x] `apply_patch /srv/projects/ashare-system-v2/.hermes.md /srv/projects/ashare-system-v2/docs/hermes_backup_operating_model.md /srv/projects/ashare-system-v2/hermes/prompts/*.md`（2026-04-13）-> 已把 Hermes 项目上下文、协作剧本与角色/cron 提示词模板落仓
- [x] `~/.hermes/hermes-agent/venv/bin/hermes profile create ashare-backup --clone`（2026-04-13）-> 已创建隔离备用 profile，路径为 `~/.hermes/profiles/ashare-backup`
- [x] `apply_patch ~/.hermes/profiles/ashare-backup/SOUL.md`（2026-04-13）-> 已把备用 profile 的人格改为交易台专用、证据优先、反编造风格
- [x] `~/.hermes/hermes-agent/venv/bin/hermes -p ashare-backup status`（2026-04-13）-> 已确认备用 profile 默认 `Provider: MiniMax (China)`
- [x] `~/.hermes/hermes-agent/venv/bin/hermes -p ashare-backup chat -q "只回复 backup-smoke-ok" -Q`（2026-04-13）-> 默认 provider 真烟测通过，返回 `backup-smoke-ok`，`session_id=20260413_105103_1ac0e2`
- [x] `~/.hermes/hermes-agent/venv/bin/hermes chat -q "只回复 default-smoke-ok" -Q`（2026-04-13）-> 默认 profile 的默认 provider 真烟测也已通过，返回 `default-smoke-ok`，`session_id=20260413_105301_3694a4`
- [x] `apply_patch ~/.hermes/profiles/ashare-backup/config.yaml /srv/projects/ashare-system-v2/hermes/prompts/cron_preopen_readiness.md /srv/projects/ashare-system-v2/scripts/bootstrap_hermes_ashare_backup.sh /srv/projects/ashare-system-v2/docs/hermes_backup_bootstrap.md`（2026-04-13）-> 已补齐备用控制面 profile 基线、盘前模板、cron bootstrap 脚本与启动手册
- [x] `bash /srv/projects/ashare-system-v2/scripts/bootstrap_hermes_ashare_backup.sh`（2026-04-13）-> 已真实创建 7 条 `ashare-backup` cron 任务
- [x] `~/.hermes/hermes-agent/venv/bin/hermes -p ashare-backup gateway install && ... gateway start && ... gateway status`（2026-04-13）-> 已安装并启动 `hermes-gateway-ashare-backup.service`，服务当前 `active (running)`
- [x] `sed -n '1,260p' ~/.hermes/profiles/ashare-backup/cron/jobs.json && find ~/.hermes/profiles/ashare-backup/cron/output -type f`（2026-04-13）-> 已确认自动执行记录真实落盘；当前已看到 `ashare-intraday-watch-am` 与 `ashare-position-watch-am` 的本地输出文件
- [x] `~/.hermes/hermes-agent/venv/bin/hermes -p ashare-backup cron run d5142dc00da6 && ... cron tick && sed -n '1,220p' ~/.hermes/profiles/ashare-backup/cron/output/d5142dc00da6/2026-04-13_11-06-16.md`（2026-04-13）-> 已确认 `ashare-preopen-readiness` 能生成真实盘前业务摘要；本次摘要已暴露“runtime/dossier 仍为隔夜数据”“execution bridge reachability 口径不一致”两个真实业务堵点
- [x] `~/.hermes/hermes-agent/venv/bin/hermes -p ashare-backup chat -q "你在 /srv/projects/ashare-system-v2 项目里，只读取真实控制面并用一句话回答：当前 execution intents 有几条，直接给数字和状态。" -Q`（2026-04-13 12:42）-> 真实返回 `3 条，状态 ready（blocked_count=0）`，说明 Hermes 备用控制面已不只是 smoke，对项目控制面读取也已打通
- [x] `"/home/yxz/.npm-global/bin/openclaw" status --deep` -> `Feishu=OK`，但 security audit 仍有 `2 critical + 6 warn`
- [x] `node --check openclaw/plugins/ashare-sessions-spawn-guard/index.js`
- [x] `python3 -m json.tool openclaw/plugins/ashare-sessions-spawn-guard/openclaw.plugin.json`
- [x] `python3 -m json.tool openclaw/plugins/ashare-sessions-spawn-guard/package.json`
- [x] `"/home/yxz/.npm-global/bin/openclaw" plugins install -l "/srv/projects/ashare-system-v2/openclaw/plugins/ashare-sessions-spawn-guard"` -> 已写入 `~/.openclaw/openclaw.json` 并触发 gateway 自动重启
- [x] `"/home/yxz/.npm-global/bin/openclaw" plugins inspect ashare-sessions-spawn-guard` -> `Status: loaded`
- [x] `"/home/yxz/.npm-global/bin/openclaw" devices approve 09e8a196-a0ec-43ab-9592-602fa27e5452`
- [x] `"/home/yxz/.npm-global/bin/openclaw" devices rotate --device d0e3744c03158bc79ba44e3f675e75641c7b3fd013a4ac262bd17eedccd5f1b6 --role operator`
- [/] `"/home/yxz/.npm-global/bin/openclaw" agent --agent ashare --message "请委派 ashare-runtime 做最小健康探测..." --json --timeout 120` -> 已真实复现 `sessions_spawn` 仍被自动注入 `streamTo:"parent"`，本次未能拉起 `ashare-runtime`
- [x] `openclaw doctor`（2026-04-13，版本 `2026.4.11`）-> `ashare-sessions-spawn-guard` 仍 loaded，doctor 主要剩余项为 session integrity 与 `gateway.bind=lan`
- [x] `openclaw gateway health`（2026-04-13）-> `OK` / `Feishu: ok`
- [x] `openclaw acp --help`（2026-04-13，版本 `2026.4.11`）-> 已确认当前 CLI 仅暴露 `bridge/client` 模式，文档入口为 `https://docs.openclaw.ai/cli/acp`
- [x] `openclaw plugins list --verbose`（2026-04-13，版本 `2026.4.11`）-> `ACPX Runtime (acpx)` 当前为 `disabled`，`activation reason: not in allowlist`
- [/] `OPENCLAW_GATEWAY_TOKEN=... openclaw agent --agent ashare --message "请委派 ashare-runtime 做最小健康探测..." --json --timeout 120`（2026-04-13，版本 `2026.4.11`）-> 仍返回 `streamTo is only supported for runtime=acp; got runtime=subagent`
- [x] `systemctl restart openclaw-gateway.service`（2026-04-13）-> 已按 systemd 正确入口重启 live gateway，避免被服务监督器自动拉起旧进程造成“假重启”误判
- [x] `openclaw agent --agent ashare --message "[2026-04-13 01:06 CST guard-retest-1] ..."`（2026-04-13，版本 `2026.4.11`）-> `sessions_spawn` 已返回 `status=accepted`，`childSessionKey=agent:ashare-runtime:subagent:4490f41f-a338-4ae3-96ea-e4214e062f68`
- [x] `rg -n "ashare-sessions-spawn-guard|guard-retest-1|sessions_spawn" logs/openclaw-gateway.log`（2026-04-13）-> 已出现 `[ashare-sessions-spawn-guard] 已清洗非 ACP sessions_spawn 参数 runtime=subagent removed=streamTo,resumeSessionId ...`
- [/] `rg -n "d9952867-067d-4457-b727-734e4d284b6a|Refusing to traverse symlink in exec approvals path" ~/.openclaw-quant`（2026-04-13）-> 最新子会话已真实阻断在 `exec approvals`，错误为 `Refusing to traverse symlink in exec approvals path: /home/yxz/.openclaw`
- [x] `openclaw approvals get --gateway`（2026-04-13）-> 已恢复正常，approvals 文件/套接字实际已落到 `~/.openclaw-quant/exec-approvals.{json,sock}`，确认 symlink 阻断已解除
- [x] `rg -n "/mnt/d/Coding/lhjy/ashare-system-v2/scripts/ashare_api\\.sh|archive_discussion_opinions\\.py" "/srv/projects/ashare-system-v2/openclaw" "/home/yxz/.openclaw-quant/workspace-ashare" "/home/yxz/.openclaw-quant/workspace-ashare-runtime"`（2026-04-13）-> 已无残留命中，确认 live runtime 工作区旧路径已清干净
- [x] `openclaw agent --agent ashare --message "[2026-04-13 01:34 CST path-fix-retest-1] ..."`（2026-04-13，版本 `2026.4.11`）-> 主会话已产生 `childSessionKey=agent:ashare-runtime:subagent:c17576b4-307b-47f5-b460-f7e1afb93fc3`，内部 completion event 明确返回 `probe` 成功、`GET /health` 成功、`GET /runtime/health` 成功
- [x] `rg -n "421e41c8-4fe8-4b58-a17c-453538e9dc7c|ashare-sessions-spawn-guard|01:35:25|probe" logs/openclaw-gateway.log`（2026-04-13）-> 已记录 `[ashare-sessions-spawn-guard] 已清洗非 ACP sessions_spawn 参数 ... run=421e41c8-4fe8-4b58-a17c-453538e9dc7c`，随后日志直接落出健康探测成功摘要
- [x] `openclaw agent --agent ashare --message "[2026-04-13 01:43 CST runtime-pipeline-retest-1] ..."`（2026-04-13，版本 `2026.4.11`）-> 首次真实 `pipeline` 联调返回 `status=failed`，阻断为 `POST /runtime/jobs/pipeline failed: curl: (28) Operation timed out after 30002 milliseconds with 0 bytes received`
- [x] `sed -n '1,260p' ~/.openclaw-quant/agents/ashare-runtime/sessions/d47b10b1-94df-4554-9279-df471b34c3d0.jsonl`（2026-04-13）-> 第二次复测 transcript 已明确使用 `ASHARE_API_TIMEOUT_SECONDS=120 /srv/projects/ashare-system-v2/scripts/ashare_api.sh post /runtime/jobs/pipeline`
- [x] `openclaw agent --agent ashare --message "[2026-04-13 01:49 CST runtime-pipeline-retest-2] ..."`（2026-04-13，版本 `2026.4.11`）-> 主会话 completion event 明确返回 `status=completed`、`trade_date=2026-04-13`、`job_id=runtime-c64521b161`、`case_count=0`、`blockers=[]`
- [x] `rg -n "617f9cf9-64b4-44d4-8612-66153b13e381|job_id|case_count" logs/openclaw-gateway.log`（2026-04-13）-> gateway 日志已落出 `{"status":"completed","trade_date":"2026-04-13","job_id":"runtime-c64521b161","case_ids":[],"case_count":0,"blockers":[]}`
- [x] `tests/test_upgrade_workflow.py::test_runtime_pipeline_response_exposes_case_ids_and_case_count`（2026-04-13）-> 已锁定 control plane 新口径：`/runtime/jobs/pipeline` 返回体必须显式包含顶层 `case_ids`、`case_count`，且与 `/system/cases?trade_date=...` 落库结果一致。
- [x] `systemctl restart ashare-system-v2.service`（2026-04-13）-> 已加载本次 control plane 修复代码到 live 进程。
- [x] `env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY -u no_proxy -u NO_PROXY ASHARE_API_TIMEOUT_SECONDS=240 /srv/projects/ashare-system-v2/scripts/ashare_api.sh post /runtime/jobs/pipeline`（2026-04-13）-> 真实返回 `job_id=runtime-a00e6cde51`、顶层 `case_ids` 30 条、`case_count=30`。
- [x] `env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY -u no_proxy -u NO_PROXY /srv/projects/ashare-system-v2/scripts/ashare_api.sh get '/system/cases?trade_date=2026-04-13&limit=100'`（2026-04-13）-> `count=30`，与修复后的 pipeline 返回一致。
- [x] `apply_patch /home/yxz/.openclaw-quant/workspace-ashare-runtime/TOOLS.md`（2026-04-13）-> 已把 live runtime 长任务超时基线改为 `ASHARE_API_TIMEOUT_SECONDS=240`。
- [x] `apply_patch /home/yxz/.openclaw-quant/workspace-ashare-runtime/AGENTS.md`（2026-04-13）-> 已把 live runtime 长任务超时基线改为 `ASHARE_API_TIMEOUT_SECONDS=240`，并明确 120 秒在当前链路下不足。
- [/] `"/home/yxz/.npm-global/bin/openclaw" agent --agent ashare --message "[2026-04-13 02:08 CST runtime-pipeline-retest-3] ..."`（2026-04-13，版本 `2026.4.11`）-> 主 CLI 仍提前返回 `NO_REPLY`，但 `logs/openclaw-gateway.log` 已真实落出子代理完成结果 `job_id=runtime-abb588287b`、`case_count=30`、`case_ids` 30 条，说明当前剩余问题是主代理结果转发稳定性，而不是 runtime 数据链。
- [x] `systemctl list-units --type=service --all | rg "ashare-system-v2|openclaw-gateway"`（2026-04-13）-> `ashare-system-v2.service`、`openclaw-gateway.service` 均为 `active/running`。
- [x] `env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY -u no_proxy -u NO_PROXY /srv/projects/ashare-system-v2/scripts/ashare_api.sh get /health`（2026-04-13）-> control plane 健康正常。
- [x] `env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY -u no_proxy -u NO_PROXY /srv/projects/ashare-system-v2/scripts/ashare_api.sh get /runtime/health`（2026-04-13）-> runtime 健康正常。
- [x] `"/home/yxz/.npm-global/bin/openclaw" gateway health`（2026-04-13）-> `OK` / `Feishu: ok`。
- [x] `bash "/srv/projects/ashare-system-v2/scripts/install_linux_scheduler_service.sh"`（2026-04-13）-> 已成功安装 `ashare-system-v2-scheduler.service` 并启用开机自启。
- [x] `bash "/srv/projects/ashare-system-v2/scripts/ashare_scheduler_service.sh" start`（2026-04-13）-> 已成功启动 scheduler systemd 服务。
- [x] `systemctl status ashare-system-v2-scheduler.service --no-pager`（2026-04-13）-> `Loaded: enabled`、`Active: active (running)`。
- [x] `systemctl is-enabled ashare-system-v2-scheduler.service && systemctl is-active ashare-system-v2-scheduler.service`（2026-04-13）-> `enabled` / `active`。
- [x] `kill 139524 && ps -ef | rg "ashare_system.run scheduler|ashare-system-v2-scheduler"`（2026-04-13）-> 已停止早前手工拉起的重复 scheduler 进程，仅保留 systemd 常驻实例。
- [x] `ps -ef | rg "ashare_system.run scheduler|openclaw-gateway|ashare_system.run serve"`（2026-04-13）-> control plane、systemd scheduler、gateway 三条核心进程均在运行。
- [x] `ls -l /srv/projects/ashare-system-v2/scripts/ashare_api.sh`（2026-04-13）-> 路径存在且可执行。
- [x] `env -u ... /srv/projects/ashare-system-v2/scripts/ashare_api.sh get '/system/audits?limit=5'`（2026-04-13）-> 可访问，最新为 execution bridge health 监控写入口径。
- [x] `env -u ... /srv/projects/ashare-system-v2/scripts/ashare_api.sh get /system/reports/runtime`（2026-04-13）-> 可访问，最新 `job_id=runtime-abb588287b`，`generated_at=2026-04-13T02:09:40.613411`，`case_count=30`。
- [x] `env -u ... /srv/projects/ashare-system-v2/scripts/ashare_api.sh get /system/reports/postclose-master`（2026-04-13）-> 可访问，但当前汇总内容仍以旧盘后 review/monitor 口径为主。
- [x] `env -u ... /srv/projects/ashare-system-v2/scripts/ashare_api.sh get /system/research/summary`（2026-04-13）-> 可访问，但当前返回为空摘要，`updated_at=2026-04-12T20:00:00.672364`。
- [/] `env -u ... /srv/projects/ashare-system-v2/scripts/ashare_api.sh get /system/meetings/latest`（2026-04-13）-> 返回 `{"available":false}`，说明会议最新记录当前确实缺失。
- [x] `ls -la ~/.npm-global/lib/node_modules/openclaw/dist/extensions/qa-channel`（2026-04-13）-> 仅有 `runtime-api.js`
- [x] `tar -tzf /tmp/openclaw-2026.4.11.tgz | rg "package/dist/extensions/qa-channel"`（2026-04-13）-> 官方 npm tarball 同样仅含 `package/dist/extensions/qa-channel/runtime-api.js`
- [x] `curl -sS http://127.0.0.1:8100/system/operations/components` -> `market_adapter=windows_proxy`
- [x] `curl -sS http://127.0.0.1:8100/system/account-state` -> 真实账户 `8890130545` / `total_asset=101028.55`
- [x] `curl -sS -X POST http://127.0.0.1:8100/runtime/jobs/pipeline` -> 主板池真实运行，`candidates_evaluated=3195`
- [x] `curl -sS -X POST http://127.0.0.1:8100/system/discussions/cycles/bootstrap`
- [x] `curl -sS -X POST http://127.0.0.1:8100/system/discussions/cycles/2026-04-12/rounds/1/start`
- [x] `curl -sS -X POST http://127.0.0.1:8100/system/discussions/cycles/2026-04-12/refresh`
- [x] `curl -sS http://127.0.0.1:8100/system/discussions/summary?trade_date=2026-04-12`
- [x] `curl -sS http://127.0.0.1:8100/system/discussions/execution-intents?trade_date=2026-04-12&account_id=8890130545`
- [x] `curl -sS -X POST http://127.0.0.1:8100/system/discussions/execution-intents/dispatch`（`apply=false`，preview 3 条）

## 2026-04-13 当前实盘联调状态更新

- [x] `ps -ef | rg "ashare_system.run (serve|scheduler)|openclaw"`（2026-04-13 11:57）-> 当前 Linux 侧核心进程都在：`scheduler(pid=1440)`、`serve(pid=1443)`、`openclaw/openclaw-gateway` 仍在运行。
- [x] `systemctl --user status hermes-gateway-ashare-backup.service --no-pager`（2026-04-13 11:57）-> Hermes 备用控制面 gateway 当前 `active (running)`，已连续运行约 57 分钟。
- [x] `tail -n 200 /srv/projects/ashare-system-v2/logs/ashare_system.log`（2026-04-13）-> 已真实看到 `2026-04-13 11:21:57 [runtime] 运行时任务完成`、`股池更新: 30 只候选股`，说明盘中 runtime/scheduler 链路已经跑通，不再停留在凌晨隔夜数据。
- [x] `./scripts/ashare_api.sh get '/data/runtime-context/latest'`（2026-04-13 11:54）-> 最新 `generated_at=2026-04-13T11:52:46.244030`，`job_id=runtime-392d1a3d69`，`decision_count=30`，盘中 runtime context 已刷新到真实新时间戳。
- [x] `./scripts/ashare_api.sh get '/system/precompute/dossiers/latest'`（2026-04-13 11:54）-> 最新 `generated_at=2026-04-13T11:52:47.329354`，`pack_id=dossier-20260413115247`，说明 dossier 盘中预计算也已跟上。
- [x] `./scripts/ashare_api.sh get '/system/monitoring/cadence'`（2026-04-13 11:54）-> `candidate.last_polled_at=2026-04-13T11:54:38.377707`，并非候选层未刷新；先前“runtime/dossier 卡在 02:xx”这一堵点现已解除。
- [/] `./scripts/ashare_api.sh get '/monitor/state'`（2026-04-13 11:54）-> 当前剩余问题已收口为两类：
  - `focus` / `execution` 仍 `due_now=true`，但代码复核显示这是因为本轮只跑了 `runtime_pipeline`，而 `focus` 需由 `discussion_refresh` 推进、`execution` 需由 `discussion_finalize` 推进，不是 candidate/runtime 刷新失败。
  - `execution_bridge_health` 最新已出现 `overall_status=degraded` 且 `last_error="poll failed: timed out"`，同时组件明细仍上报 `windows_execution_gateway.status=healthy`、`reachable=false`；这仍是 Windows 侧健康上报口径冲突，尚未修平。
- [/] `curl -i -sS -H 'Content-Type: application/json' -X POST http://127.0.0.1:8100/runtime/jobs/intraday --data-binary '{}'`（2026-04-13 11:55 起复核）-> 当前未再快速复现旧的 422，调用表现为进入等待执行；因此“`/runtime/jobs/intraday` 固定 422”不再作为主堵点，后续只需把手工触发返回耗时与历史误报再单独复核。

## 当前主结论

- [x] 真实数据底座、盘中 runtime 刷新、dossier 预计算、候选池写入目前都已跑通。
- [/] 当前真正未闭环的，不是候选生成，而是 `discussion -> focus/execution poll -> execution intent` 这条更深一层的联调。
- [/] Windows 执行桥健康上报仍不稳定，尤其是 `overall_status / reachable / qmt_connected` 三组字段口径不一致，仍需继续盯 Windows 侧来源。

## 2026-04-13 discussion / execution 联调续进展

- [x] `curl -sS -X POST http://127.0.0.1:8100/system/discussions/cycles/bootstrap`（2026-04-13 12:01 起多次复核）-> 服务日志已真实记录 `讨论周期已初始化: 2026-04-13`，今天 `discussion cycle` 已成功建立，不再是 `available=false` 的空壳状态。
- [x] `./scripts/ashare_api.sh get '/system/discussions/cycles/2026-04-13'`（2026-04-13 12:05 后复核）-> 当前 cycle 已进入 `pool_state=focus_pool_building`、`discussion_state=round_1_running`，并且 `execution_pool_case_ids` 已真实出现 3 条：`600010.SH / 002263.SZ / 600166.SH`。
- [x] `curl -sS -X POST http://127.0.0.1:8100/system/discussions/cycles/2026-04-13/rounds/1/start`（2026-04-13 12:05）-> 服务日志已真实记录 `讨论轮次已启动: 2026-04-13 round 1`，说明今天已不是“只有候选池，没有讨论轮次”。
- [x] `./scripts/ashare_api.sh get '/system/discussions/execution-intents?trade_date=2026-04-13&account_id=8890130545'`（2026-04-13 12:06）-> 真实返回 `status=ready`、`intent_count=3`、`blocked_count=0`；三条执行意图分别为：
  - `600010.SH 包钢股份`，`BUY 6600 @ 3.02`，约 `19932.0`
  - `002263.SZ 大东南`，`BUY 4000 @ 4.99`，约 `19960.0`
  - `600166.SH 福田汽车`，`BUY 5800 @ 3.47`，约 `20126.0`
- [x] `curl -sS -X POST http://127.0.0.1:8100/system/discussions/execution-intents/dispatch --data-binary '{"trade_date":"2026-04-13","account_id":"8890130545","apply":false}'`（2026-04-13 12:07）-> 真实返回 `status=preview`、`preview_count=3`、`blocked_count=0`，说明 dispatch 预演闭环也已打通，当前只差是否要进入真实 `apply=true`。
- [/] `./scripts/ashare_api.sh get '/system/monitoring/cadence'`（2026-04-13 12:06）-> `candidate/focus/execution` 仍显示 `due_now=true`，其中 candidate 只是超过 5 分钟未再触发；focus/execution 则说明 cadence 状态位尚未随着本次 discussion 联调同步推进，口径仍需继续收敛。
- [/] `./scripts/ashare_api.sh get '/system/discussions/cycles/2026-04-13'`（2026-04-13 12:06）-> cycle 当前还残留 `blockers=["selected_count_zero"]`，但同一响应里 `execution_pool_case_ids` 已有 3 条，且 `execution-intents` 已 `ready`。代码复核显示该 blocker 来自 `discussion_service._derive_blockers()` 对 `summary_snapshot.selected` 的旧口径判断，当前高度怀疑是 stale blocker / 摘要字段未同步，不宜再把它当真实执行阻断。

## 当前主阻断收敛

- [x] `runtime -> discussion round_1 -> execution intent -> dispatch preview` 主链已经真实打通。
- [/] 当前剩余更像是“状态口径问题”，而不是“业务链没通”：
  - `cadence` 的 `focus/execution due_now` 未随 discussion 进展更新
  - cycle 的 `selected_count_zero` 与实际 `execution_pool_case_ids/intents` 冲突
  - Windows health 上报仍存在 `overall_status / reachable` 不一致

## 2026-04-13 Hermes / health 续修

- [x] `apply_patch src/ashare_system/monitor/persistence.py tests/test_upgrade_workflow.py`（2026-04-13 12:48）-> 已修 `build_execution_bridge_health_ingress_payload()` 默认口径：当 Windows 侧只上报 `gateway_online/qmt_connected=true` 而未显式带 `reachable` 时，组件 `reachable` 现在会回落到顶层在线状态，不再错误写成 `false`
- [x] `PYTHONPATH="/srv/projects/ashare-system-v2/src" /srv/projects/ashare-system-v2/.venv/bin/python -m unittest "/srv/projects/ashare-system-v2/tests/test_upgrade_workflow.py"`（2026-04-13 12:49）-> `Ran 15 tests in 1.296s, OK`，新增覆盖 execution bridge ingress 的 `reachable` 默认语义
- [x] `systemctl restart ashare-system-v2.service && ./scripts/ashare_api.sh get '/monitor/state'`（2026-04-13 12:50）-> live 已确认修复生效：`latest_execution_bridge_health.health.overall_status=healthy`，且 `windows_execution_gateway.reachable=true`、`qmt_vm.reachable=true`
- [/] `execution_bridge_health_trend_summary.*.reachable_ratio` 当前仍偏低（约 `0.05`），这是历史 20 条快照里旧的错误值尚未被新样本完全冲淡，不再是当前 live 状态仍错误

## 2026-04-13 cadence 展示层收敛

- [x] `apply_patch src/ashare_system/monitor/polling_view.py src/ashare_system/apps/system_api.py src/ashare_system/apps/monitor_api.py src/ashare_system/app.py tests/test_upgrade_workflow.py`（2026-04-13 13:15）-> 已新增 cadence 展示层装饰器：保留底层计时器 `raw_due_now`，同时把正在运行的 discussion / 已就绪 execution intents 对外显示为 `display_state=active`
- [x] `PYTHONPATH="/srv/projects/ashare-system-v2/src" /srv/projects/ashare-system-v2/.venv/bin/python -m unittest "/srv/projects/ashare-system-v2/tests/test_upgrade_workflow.py"`（2026-04-13 13:16）-> `Ran 16 tests in 1.380s, OK`，新增覆盖“超出轮询间隔但 discussion/execution 业务仍在活动态时，不再误报 due_now=true”
- [x] `systemctl restart ashare-system-v2.service && ./scripts/ashare_api.sh get '/system/monitoring/cadence' && ./scripts/ashare_api.sh get '/monitor/state'`（2026-04-13 13:17）-> live 已确认：
  - `focus.due_now=false raw_due_now=true suppressed_due_reason=discussion_active:round_1_running`
  - `execution.due_now=false raw_due_now=true suppressed_due_reason=execution_intents_ready`
  - summary lines 已从纯 `due/cooldown` 改为 `candidate=due`、`focus=active`、`execution=active`

## 2026-04-13 scheduler / 状态存储续修

- [x] `apply_patch src/ashare_system/scheduler.py tests/test_upgrade_workflow.py`（2026-04-13 13:31）-> 已确认并修复 APScheduler weekday 语义错位：项目 cron 原本按 crontab 习惯把 `1-5` 当作周一到周五，但 APScheduler 以 Monday=0；现已在 scheduler 内统一做 crontab weekday 归一化，不必手改全部任务定义
- [x] `PYTHONPATH="/srv/projects/ashare-system-v2/src" /srv/projects/ashare-system-v2/.venv/bin/python -m unittest "/srv/projects/ashare-system-v2/tests/test_upgrade_workflow.py"`（2026-04-13 13:32）-> `Ran 17 tests in 1.420s, OK`，新增覆盖 `1-5 -> 0-4` 的 weekday 转换与“周一 13:00 正常触发”校验
- [x] `systemctl restart ashare-system-v2-scheduler.service && journalctl -u ashare-system-v2-scheduler.service --since '2026-04-13 13:32:41'`（2026-04-13 13:35）-> live 已确认 weekday 修复生效：`微观巡检/盯盘巡检` 在周一下午真实开始触发，`candidate_poll.last_polled_at` 已从 `11:57:36` 推进到 `13:33:05`
- [x] `./scripts/ashare_api.sh get '/system/monitoring/cadence'`（2026-04-13 13:36）-> `candidate_poll=cooldown`，`dossier` 重新回到 `fresh`，说明盘中自动刷新链恢复，不再卡在午前旧快照
- [x] `apply_patch src/ashare_system/infra/audit_store.py tests/test_upgrade_workflow.py`（2026-04-13 13:38）-> 已为 `StateStore/AuditStore` 加入跨进程文件锁 + 原子写入，修复盘中并发 job 读写同一 JSON 状态文件时偶发 `JSONDecodeError` 的竞态
- [x] `PYTHONPATH="/srv/projects/ashare-system-v2/src" /srv/projects/ashare-system-v2/.venv/bin/python -m unittest "/srv/projects/ashare-system-v2/tests/test_upgrade_workflow.py"`（2026-04-13 13:39）-> `Ran 18 tests in 1.575s, OK`，新增覆盖 `StateStore` 并发读写可读性
- [x] `systemctl restart ashare-system-v2.service && systemctl restart ashare-system-v2-scheduler.service && journalctl -u ashare-system-v2-scheduler.service --since '2026-04-13 13:39:43'`（2026-04-13 13:40）-> restart 后未再出现新的 `JSONDecodeError / 盯盘巡检失败`，且 `candidate/focus/execution` 继续自动推进到 `13:39/13:40`

## 2026-04-14 正式上线压力测试前缺口

### 当前判断

- [x] 功能开发主线已基本齐备，且 `真实行情 -> runtime -> discussion -> execution-intents -> dispatch preview` 已真实打通。
- [/] 当前距离“正式上线压力测试”差的已不再是原子功能，而是上线前的稳定性、守护能力、状态口径一致性与实盘安全边界收口。
- [/] 截至当前，系统仍以 `preview/apply=false` 为主；未确认进入 `apply=true` 的正式下单压测，因此不能把当前状态算作“已完成正式实盘压测”。

### P0 硬阻断

- [ ] P0-1 正式执行压测前，补一次受控 `apply=true` 演练方案
  - 目标：明确最小仓位、白名单标的、触发时段、回滚口径、人工确认位。
  - 原因：当前只验证到 `dispatch preview`，尚未形成“真实报单 -> 回执 -> 对账 -> 飞书告警”全闭环压测证据。
- [ ] P0-2 收敛 OpenClaw 主链稳定性，避免主代理 `NO_REPLY` / 子代理结果转发不稳
  - 现状：真实 runtime 数据链已可被驱动，但 OpenClaw 主 CLI 结果回传仍有不稳定记录。
  - 要求：压测时要么把 OpenClaw 主链稳定住，要么明确切 Hermes 为主、OpenClaw 为备，不能保持双边都半在线。
- [ ] P0-3 把飞书长连接进程正式化
  - 现状：长连接状态缓存口径已修，仓库侧也已补 service 管理脚本、参数化安装脚本、unit 模板与统一健康入口；但 live 的 systemd 实际安装/自恢复压测还未在本文件留痕。
  - 要求：压测前至少做到“进程可自恢复、状态可观测、重启后自动拉起、故障时有明确日志入口”。
- [/] P0-4 固化实盘电子围栏
  - 范围：最大总仓、单票仓位、单笔金额、日内最大换手次数、禁止时段、禁止 apply 条件、Windows 执行桥不可达时的强阻断。
  - 原因：现在规则有，但仍需整理成压测口径并逐条验收，避免 agent 协作链越过程序围栏。
  - 2026-04-16 已把 `controlled_apply_readiness` 继续从“仓位/单票”扩到“时段/次数”：
    - 新增 `blocked_time_windows`，可直接声明禁止 apply 时段，命中即阻断
    - 新增 `max_apply_submissions_per_day`，可按当日 `execution_dispatch_history` 的 `queued/submitted` 计数做强阻断
    - `execution_dispatch_history` 已补 `queued_count` 留痕，便于 Windows Gateway 路径统计真实当日 apply 次数
  - 已补回归并通过：
    - `test_controlled_apply_readiness_blocks_inside_apply_time_window`
    - `test_controlled_apply_readiness_blocks_when_daily_apply_submission_limit_reached`
    - `tests/test_upgrade_workflow.py` 全量 `97/97 OK`
  - 当前剩余：电子围栏参数尚未统一沉淀成“live 默认压测口径”，`日内最大换手次数` 也仍是按派发/提交次数近似控制，尚未细化到成交后持仓级 T 次数治理。
- [/] P0-5 补一轮“服务重启 + 进程漂移 + 断链恢复”压测
  - 范围：`serve`、`scheduler`、飞书长连接、Windows 网关、OpenClaw/Hermes 主备。
  - 要求：验证重启后候选刷新、讨论推进、催办消息、问答、执行预演都能自动恢复，而不是靠手工补启动。
- [/] P0-6 梳理压测场景与通过标准
  - 至少包含：盘前、盘中、盘后、夜间；正常链路、超时链路、Windows 不可达、飞书掉线、无候选、execution blocked、preview 与 apply 的差异。

### P1 上线前最好完成

- [/] P1-1 优化 `Agent 自动催办` 的飞书版面与升级策略
  - 现状：主链已通，但当前消息排版一般，摘要行仍偏粘连，不利于长期值班使用。
  - 2026-04-14 已完成第一轮纠偏：`ashare-runtime` 已从“candidate_poll 到点就催”改成“回看 runtime/monitor 活动痕迹”；`ashare-research / strategy / risk / audit` 在无进行中讨论轮次时，也会回看最近研究摘要、调参提案、执行预检、会议/审计复核等真实产出痕迹，不再机械按工具调用频率催办。
- [x] P1-2 修掉 `GET /system/feishu/longconn/status` 旧缓存口径
  - 2026-04-14 已完成：状态接口先回读 `feishu_longconn_state.json`，并结合 `last_heartbeat_at / pid_alive` 判定有效状态。
- [/] P1-3 继续收敛状态展示口径
  - 重点：discussion cycle 的 blocker、summary、selected/watchlist、execution readiness 要保持同一口径，避免机器人误报。
  - 2026-04-14 已完成当前收口：`tests/test_upgrade_workflow.py` 已恢复到 `40/40` 通过，执行链旧测试已改为显式构造 `selected/execution_pool`，并新增 supervision 用例验证“无讨论轮次但 Agent 有近期产出”时不会被误报为摸鱼。
- [x] P1-4 把“飞书三权 + 机器人催办 + 主线流程”整理成对外操作手册
  - 2026-04-16 已新增 `docs/feishu_operator_manual_20260416.md`
  - 当前已统一整理：
    - 飞书三权定义：知情权 / 调参权 / 询问权
    - 三个核心入口：`/system/feishu/ask`、`/system/feishu/events`、`/system/feishu/adjustments/natural-language`
    - 机器人职责边界：自动催办、回执 ack、重要通知、问答分流
    - 主线流程对应的飞书动作：盘前、盘中发现、讨论收敛、执行预演、监督催办
    - 联调命令与后台排障入口
  - 后续群测与交付可直接按该手册执行，不必再口头回忆入口与后台配置。

### 压测准入条件

- [ ] 准入 1：Linux `serve + scheduler + feishu-longconn` 可稳定在线，并有统一健康查看入口。
- [ ] 准入 2：Windows `18791` 网关、QMT 资产、行情、执行回执在压测窗口内保持稳定。
- [ ] 准入 3：至少完成一次 `apply=true` 的受控小额真实闭环，并保留回执与飞书通知证据。
- [ ] 准入 4：主用 agent 编排链稳定，备用链可在主链故障时接管，不依赖临场手工修脚本。
- [ ] 准入 5：监督催办、重要消息、执行回执、盘后学习摘要都能自动送达飞书。

- [x] 2026-04-16 已新增最终总检查脚本：`scripts/check_go_live_gate.sh`
  - 当前会统一汇总：
    - `准入1_linux_services`
    - `准入2_windows_bridge`
    - `准入3_apply_closed_loop`
    - `准入4_agent_chain`
    - `准入5_feishu_delivery`
  - 数据来源为真实控制面接口：
    - `/system/deployment/service-recovery-readiness`
    - `/system/deployment/controlled-apply-readiness`
    - `/system/feishu/longconn/status`
    - `/system/feishu/briefing`
    - `/system/agents/supervision-board`
    - `/system/discussions/execution-dispatch/latest`
    - `/system/execution/gateway/receipts/latest`
    - `/system/readiness`
  - 作用：把“能不能进入正式压测窗口”的判断从任务板人工对表，收口成一条脚本输出。
- [x] 2026-04-16 已新增正式压测窗口主线脚本：`scripts/run_go_live_pressure_sequence.sh`
  - 当前顺序固定为：
    - 先跑 `check_go_live_gate.sh`
    - 再跑 `run_controlled_single_apply.sh`
    - 默认停在单票 preview
    - 只有显式 `--apply --confirm APPLY` 才进入真实 apply
  - 作用：把“最终总检查 -> 单票 preview -> 可选 apply”收口成交易窗口的一条主命令，避免现场手工拼多条脚本。

### 下一步建议顺序

- [ ] 第一步：先定“压测用 apply=true 方案”和电子围栏参数。
- [ ] 第二步：把飞书长连接与主用 agent 编排链正式化，收口自恢复与状态观测。
- [ ] 第三步：跑一轮“服务重启/断链恢复”压测。
- [ ] 第四步：再做真实小额闭环压测，并把结果回填到本文件。

## 2026-04-17 Agent 自主编排与 101 因子库主线

- [x] 已新增主线实施细则文档：`docs/agent_autonomy_factor_library_plan_20260417.md`
- [x] 已明确当前阶段的主问题不是“缺少某个接口”，而是以下三项结构性不足：
  - Agent 仍偏向从预置 profile 中选起手，尚未完全过渡到“自组装 compose brief”
  - 候选发现仍偏依赖既有 candidate pool，Agent 主动提新票能力不足
  - Runtime 因子库/战法库规模过小，尚未达到多因子赛马与均值回归所需规模

### P0 Agent 自主编排

- [x] P0-A1 将 `compose profile` 从“默认入口”降级为“参考模板”，允许 Agent 完全自组装 compose brief
- [x] P0-A2 扩展 runtime compose 校验口径，支持 Agent 自定义：
  - `playbooks`
  - `factors`
  - `weights`
  - `constraints`
  - `market_hypothesis`
- [x] P0-A3 调整各 agent 提示词，明确“先理解市场，再选工具，再决定是否调用 runtime”
- [x] P0-A4 为 Agent 输出增加结构化编排痕迹：
  - 市场假设
  - 工具选择理由
  - 放弃项
  - 是否调用 compose
  - 调用后的下一步动作
- [x] P0-A5 将 `agent_proposed` 作为正式候选 source 接入 discussion 与审计链

  - 本轮已把 `RuntimeComposeBriefRequest` 扩展为支持 `intent_mode / playbook_specs / factor_specs / ranking / custom_constraints / market_context`
  - 本轮已把 `compose_brief_hint` 改成 `custom_first` 口径：模板仅作为 `reference_templates`，同时提供 `custom_payload_template` 与 `orchestration_trace_contract`
  - 本轮已同步调整 `scheduler` 自主起手逻辑，优先消费 `recommended/reference_templates`，并在无模板命中时回退到 `custom_payload_template`
  - 本轮已更新 `strategy/runtime/research/risk/execution` 提示词与监督期望输出，统一要求先理解市场，再决定是否调用 runtime，并补齐结构化编排痕迹
  - 本轮验证已通过：
    - `test_runtime_compose_from_brief_builds_compose_request_for_agent`
    - `test_runtime_compose_from_brief_supports_custom_specs_constraints_and_market_context`
    - `test_agent_runtime_work_packets_exposes_strategy_task_and_compose_hint`
    - `test_runtime_factor_and_playbook_catalog_endpoints_expose_seed_assets`

### P0 候选发现多入口

- [x] P0-B1 将候选入口拆成独立 source：
  - `market_universe_scan`
  - `sector_heat_scan`
  - `news_catalyst_scan`
  - `position_monitor_scan`
  - `intraday_t_scan`
  - `tail_ambush_scan`
  - `agent_proposed`
- [x] P0-B2 将粗样本池、深度验证池、执行候选池三层拆开，避免所有候选混在一个池里
- [x] P0-B3 为 Agent 自提机会票补齐最小字段：
  - `symbol`
  - `source`
  - `market_logic`
  - `core_evidence`
  - `risk_points`
  - `why_now`

  - 本轮已在 `candidate_case` 的 `runtime_snapshot` 中正式引入 `source/source_tags/source_detail`，并把历史 `candidate_pool/runtime_pipeline` 归一到 `market_universe_scan`
  - 本轮已新增 `POST /system/discussions/opportunity-tickets`，允许 Agent 以结构化 `opportunity ticket` 提交新票；写入后会进入 candidate case、触发 discussion cycle bootstrap/refresh，并留审计记录
  - 本轮已把三层池正式别名化暴露为 `base_sample_pool / deep_validation_pool / execution_candidate_pool`，并在 summary/pool snapshot 中同步提供 count，后续不再只能依赖 `candidate/focus/execution` 旧命名自行脑补语义
  - 本轮已在 trade-date summary 与 pool snapshot 中补齐 `source_counts`，控制面已可直接看见候选来源分布，不再只能逐票翻 `runtime_snapshot`
  - 当前 `agent_proposed` 已正式进入 discussion 与 audit 主链；`compose-from-brief` 已可根据任务自动写入 `sector_heat_scan / position_monitor_scan / news_catalyst_scan / tail_ambush_scan`，`/runtime/jobs/intraday` 已写入 `intraday_t_scan`
  - 本轮已新增真实 job 入口：
    - `POST /runtime/jobs/news-catalyst`
    - `POST /runtime/jobs/tail-ambush`
  - `news-catalyst` 会优先消费 `latest_event_context` 中的正面催化标的；`tail-ambush` 会优先消费 `latest_runtime_context` / `latest_monitor_context` 中的尾盘候选，再把 source 写回 candidate case 与 discussion 池
  - 本轮验证已通过：
    - `test_opportunity_tickets_enter_discussion_with_agent_proposed_source`
    - `test_runtime_news_catalyst_job_consumes_event_context_and_syncs_source`
    - `test_runtime_tail_ambush_job_consumes_runtime_context_and_syncs_source`

### P1 101 因子库第一阶段扩容

- [x] P1-C1 将当前因子库从约 `14` 个扩至第一阶段 `48-64` 个
- [x] P1-C2 将当前战法库从约 `10` 个扩至第一阶段 `18-24` 个
- [x] P1-C3 按 10 组完成因子分类：
  - 趋势动量
  - 反转修复
  - 成交量能
  - 资金行为
  - 板块热度
  - 事件催化
  - 微观结构
  - 风险惩罚
  - 估值排雷
  - 持仓管理
- [x] P1-C4 为每个因子补齐元数据：
  - `id/group/version/description/params_schema/evidence_schema/source/author/status`
- [x] P1-C5 为每个战法补齐元数据：
  - `id/name/version/market_phases/params_schema/evidence_schema/tags/source/author/status`

  - 本轮已把 seed 因子库扩至 `64` 个，处于第一阶段目标区间 `48-64`
  - 本轮已把 seed 战法库扩至 `20` 个，处于第一阶段目标区间 `18-24`
  - 本轮已把因子分齐到 10 组：`trend_momentum / reversal_repair / volume_liquidity / capital_behavior / sector_heat / event_catalyst / micro_structure / risk_penalty / valuation_filter / position_management`
  - 本轮已将因子/战法 seed 统一改为显式元数据建模：默认带完整 `params_schema / evidence_schema / source / author / status`，不再只有薄描述字段

### P1 因子与战法账本

- [x] P1-D1 为因子建立账本：
  - 使用次数
  - 命中标的
  - 市场阶段
  - 收益贡献
  - 风险贡献
  - 最近 5/20/60 日有效性
  - 相关性
- [x] P1-D2 为战法建立账本：
  - 使用次数
  - 入选率
  - 预演通过率
  - 实盘触发率
  - 收益/回撤
  - 最近失效模式
- [x] P1-D3 为 compose 组合建立账本，记录本次组合使用了哪些 playbook/factor/weight

  - 本轮已将 `/runtime/evaluations/panel` 从仅返回 `summary + items` 扩为同时返回：
    - `factor_ledger`
    - `playbook_ledger`
    - `compose_combo_ledger`
  - 因子账本现已聚合：`usage_count / hit_symbols / market_phase_breakdown / return_contribution / risk_contribution / effectiveness(last_5d/20d/60d) / correlation`
  - 战法账本现已聚合：`usage_count / selection_rate / preview_pass_rate / live_trigger_rate / avg_return / avg_drawdown / recent_failure_modes`
  - 组合账本现已聚合：`playbooks / factors / weights / use_count / adopted_count / settled_count / filled_count / avg_return / avg_drawdown`
  - 本轮验证已通过：
    - `test_runtime_factor_and_playbook_catalog_endpoints_expose_seed_assets`
    - `test_runtime_evaluation_panel_exposes_factor_playbook_and_combo_ledgers`
    - `test_runtime_compose_from_brief_supports_custom_specs_constraints_and_market_context`
    - `test_runtime_evaluation_reconcile_reads_discussion_cases`
    - `test_runtime_evaluation_reconcile_outcome_reads_execution_reconciliation`

### P2 监督口径切换

- [x] P2-E1 将监督重点从“是否调用工具”切到“是否形成市场响应产物”
- [x] P2-E2 当市场发生重大变化但 Agent 在规定窗口内没有：
  - 新提案
  - 新 compose
  - 新持仓动作判断
  则判为真实迟滞或怠工
- [x] P2-E3 将盘前、盘中、尾盘、持仓做 T、换仓、盘后学习统一纳入同一套 Agent 主导逻辑

  - 本轮已在 `supervision_tasks.py` 中新增 `market_response_outputs / market_response_targets / market_response_gap / market_response_state / response_lane`
  - 当前监督任务会显式强调：不要只汇报调用了哪些工具，必须形成对口的 `市场响应产物`
  - 当 `latest_market_change_at` 落在近窗口且对应岗位仍未形成 `新提案 / 新 compose / 持仓动作判断 / 风险边界 / 执行反馈` 等对口产物时，当前会标记为 `market_response_state=lagging`，并进入 `市场响应迟滞` 摘要
  - 当前已把盘前机会发现、盘中机会响应、尾盘收口、持仓做 T/换仓、盘后学习统一抽象为 `response_lane`，监督、续发与提示口径共享同一条主导链路
  - 本轮验证已通过：
    - `test_agent_task_plan_marks_market_response_lag_when_market_changes_without_new_artifact`
    - `test_agent_task_plan_exposes_market_response_targets_for_strategy`
    - `test_agent_task_plan_sets_response_lane_for_tail_positions_and_post_close_learning`
    - `test_agent_task_plan_builds_market_event_aware_action_reason`
    - `test_agent_task_plan_generates_position_driven_follow_up_tasks`
    - `test_agent_task_plan_generates_learning_follow_up_after_real_trades`
    - `test_agent_task_plan_sorts_recommended_tasks_by_status_quality_and_gap`
    - `test_agent_task_plan_pushes_strategy_to_compose_when_cycle_idle`
    - `test_agent_task_plan_auto_completes_strategy_follow_up_when_nightly_sandbox_observed`

### 验收标准

- [x] 验收 1：Agent 可在不依赖固定 profile 的前提下，自主提交合法 compose brief
- [x] 验收 2：Agent 可从非默认股票池中提出新机会票并进入讨论
- [x] 验收 3：Runtime 输出清楚列出使用的 factors / playbooks / weights / filters / evidence
- [x] 验收 4：因子与战法全部进入账本，可做赛马、降权、暂停与复活
- [x] 验收 5：监督系统能识别“市场有变化但 Agent 无实质产出”的真实怠工

  - 本轮已给 `POST /runtime/jobs/compose` 增加显式 `composition_manifest`，统一回显：
    - `intent`
    - `universe`
    - `playbooks`
    - `factors`
    - `ranking`
    - `filters`
    - `evidence`
    - `proposal_packet`
  - 本轮已给 `POST /runtime/jobs/compose-from-brief` 增加 `brief_execution`，明确：
    - `custom_profile_independent=true`
    - `used_reference_profile=false`
    - `profile_dependency=none`
    - `validated_compose_request=true`
    - 本次实际选中的 `playbooks/factors/ranking/constraint_sections`
  - 本轮已给 `POST /system/discussions/opportunity-tickets` 增加 `ingress_summary`，可直接看 `entered_discussion_count / case_ids / source_counts / cycle_bootstrapped`
  - 当前验收口径已闭环：
    - Agent 可直接提交自组装 compose brief，并在回包中看到程序已验证后的正式 compose request 与执行摘要
    - Agent 自提新票写入后可直接进入 candidate case / discussion cycle，并在回包中看到入链摘要
    - Runtime 回包已能显式列出本次使用的 factors / playbooks / weights / filters / evidence，不再只靠多处字段自行拼装
  - 本轮新增验证已通过：
    - `test_runtime_compose_from_brief_builds_compose_request_for_agent`
    - `test_runtime_factor_and_playbook_catalog_endpoints_expose_seed_assets`
    - `test_opportunity_tickets_enter_discussion_with_agent_proposed_source`
    - `test_runtime_compose_from_brief_supports_custom_specs_constraints_and_market_context`
    - `test_runtime_evaluation_panel_exposes_factor_playbook_and_combo_ledgers`
    - `test_runtime_evaluation_reconcile_reads_discussion_cases`
    - `test_runtime_evaluation_reconcile_outcome_reads_execution_reconciliation`

## 2026-04-14 飞书长连接状态观测收口

- [x] 已修 `/system/feishu/longconn/status` 旧内存态口径：状态接口现在会先回读 `feishu_longconn_state.json`，不再只看控制面进程内缓存。
- [x] 已为 `feishu_longconn` worker 增加 `last_heartbeat_at` 心跳落盘，压测时可区分“真实在线”与“只剩旧状态文件”。
- [x] 状态接口已新增 `reported_status / pid_alive / last_heartbeat_at`，并把 `starting|connected + 心跳过旧` 自动判为 `stale`，不再把僵死进程误报为在线。
- [x] 已补测试并通过：
  - `tests/test_feishu_longconn.py` -> `5/5 OK`
  - `tests/test_upgrade_workflow.py` -> `34/34 OK`
- [/] 这一轮解决的是“状态可观测”问题；`feishu-longconn` 的 systemd 正式安装与重启自恢复仍属于后续 P0/P1 收口项。

## 2026-04-14 飞书长连接服务正式化补强

- [x] 已重写 `scripts/install_feishu_longconn_service.sh`，支持 `--user` 与 `--system` 双模式安装。
- [x] 默认安装口径已切到 `user` 模式，避免当前环境继续卡在 `/etc/systemd/system` 写权限。
- [x] `user` 模式下已输出 `systemctl --user ...` 运维命令，并补充 `loginctl enable-linger` 提示，便于做无人登录自启动。
- [x] unit 已补 `PYTHONUNBUFFERED=1`、`Restart=always`、`StartLimitIntervalSec`、`StartLimitBurst`、`NoNewPrivileges=true`，比之前更适合长期后台值守。
- [x] 已完成脚本级验证：
  - `bash -n scripts/install_feishu_longconn_service.sh`
  - `bash -n scripts/ashare_feishu_longconn_service.sh`
- [x] 已实际执行 `bash scripts/install_feishu_longconn_service.sh --user`，用户级 unit 已安装到 `~/.config/systemd/user/ashare-feishu-longconn.service` 且完成 enable。
- [x] 已实际执行 `systemctl --user start ashare-feishu-longconn.service`，当前服务状态为 `active (running)`。
- [x] 已通过 `systemctl --user status ashare-feishu-longconn.service --no-pager` 看到真实连接日志，包含 `connected to wss://msg-frontier.feishu.cn/...`，说明不是“只起进程未连飞书”。
- [x] 已重启控制面 `ashare-system-v2.service` 使新状态接口生效；`GET /system/feishu/longconn/status` 现已真实返回：
  - `status=connected`
  - `reported_status=connected`
  - `pid=616927`
  - `pid_alive=true`
  - `is_fresh=true`
- [/] 当前仍有一个运维尾项：若希望“无人登录后也持续自启动”，还需按脚本提示执行 `sudo loginctl enable-linger yxz`；否则当前保证的是用户会话内自动拉起与重启自恢复。

## 2026-04-14 apply=true 压测方案落档

- [x] 已新增压测方案文档：`docs/controlled_apply_pressure_test_plan_20260414.md`
- [x] 已对照真实代码口径整理出 apply=true 前的两层围栏：
  - `execution_precheck` 业务阻断
  - `execution dispatch` 提交闸门
- [x] 已明确当前代码默认仍是“测试期保守仓位”：
  - `equity_position_limit=20%`
  - `reverse_repo_reserved_amount=70000`
  - `stock_test_budget=30000`
- [x] 已把“当前代码 20% 股票仓位上限”与“项目目标 50% 仓位纪律”之间的差异写明，避免误把首轮实盘压测当成最终交易制度上线。
- [x] 已给出建议路线：先按现有 20% 股票仓位上限做一次 `apply=true` 小额真实闭环压测，再推进 50% 仓位纪律与更主动的 agent 交易台策略。
- [/] 该方案目前仍是“已成文、待执行”；真正完成还差：
  - 选定 1 条 intent
  - 在交易时段执行一次 `apply=true`
  - 核对 gateway claim / receipt / 飞书回执 / QMT 对账

## 2026-04-14 apply=true 压测前置检查脚本

- [x] 已新增 `scripts/check_apply_pressure_readiness.sh`
- [x] 2026-04-15 已把“受控 apply 是否准入”的判断沉到控制面只读接口：`GET /system/deployment/controlled-apply-readiness`
- [x] 2026-04-15 `scripts/check_apply_pressure_readiness.sh` 已改为直接消费该接口，不再在 shell 里散落拼接判断
- [x] 当前统一检查项已扩展为：
  - `run_mode/live_trade_enabled`
  - Windows 执行桥 / QMT reachability
  - 飞书长连接在线与 freshness
  - 当前是否处于交易时段
  - discussion cycle 是否存在
  - execution precheck 是否至少有 1 条 approved
  - execution intents 是否至少有 1 条 intent
  - apply intent 数量上限
  - 白名单 symbol 约束
  - `emergency_stop`
  - `equity_position_limit / max_single_amount / reverse_repo_reserved_amount / stock_test_budget_amount` 受控阈值
- [x] 已完成脚本验证：
  - `bash -n scripts/check_apply_pressure_readiness.sh`
  - `tests/test_upgrade_workflow.py` 当前已覆盖新的控制面 readiness 接口
- [x] 2026-04-14 17:02 实测结果与当前真实状态一致：
  - `控制面=OK`
  - `飞书长连接=OK`
  - `交易时段=NO`
  - `discussion cycle=NO`
  - `execution precheck=NO`
  - `execution intents=NO`
- [/] 结论：当前不是被风控挡住，而是今天收盘后没有可执行 cycle / intent；明天要先形成 cycle 与 intent，再进入 `apply=true` 小额闭环压测。

## 2026-04-14 apply=true 主线执行脚本

- [x] 已新增 `scripts/run_apply_pressure_sequence.sh`
- [x] 脚本主线已固定为：
  - `runtime/jobs/pipeline`
  - `discussion cycle bootstrap`
  - `round_1/start`
  - `execution-precheck`
  - `execution-intents`
  - `dispatch preview`
  - 可选 `dispatch apply=true`
- [x] 默认只跑到 `preview`，不会真实派发。
- [x] 只有显式传入 `--apply --confirm APPLY` 才会触发真实派发。
- [x] `apply=true` 前会再次调用 `scripts/check_apply_pressure_readiness.sh`，避免在无交易时段、无 cycle、无 intent 或飞书链断开时误触。
- [x] 已完成最小验证：
  - `bash -n scripts/run_apply_pressure_sequence.sh`
  - `bash scripts/run_apply_pressure_sequence.sh --help`
- [x] 已把一个关键风险写入压测方案文档：当前 Windows gateway worker 本身不依据 `paper/live` 自动短路，因此 `apply=true` 仍必须依赖外层确认与 readiness 脚本。

## 2026-04-15 服务重启 / 断链恢复检查程序化

- [x] 已新增控制面只读接口：`GET /system/deployment/service-recovery-readiness`
- [x] 已把“恢复到可值班状态”的判断沉到控制面统一口径，当前检查项包括：
  - `readiness`
  - `api_stack`
  - 飞书长连接 freshness
  - Windows 执行桥 / QMT reachability
  - `workspace_context` 是否存在且未过旧
  - `runtime/discussion/monitor/workspace` 最近信号是否仍新鲜
  - supervision pipeline 是否还能生成
  - briefing pipeline 是否还能生成
- [x] 已新增脚本：`scripts/check_service_recovery_readiness.sh`
- [x] 脚本已支持通过环境变量调节恢复检查阈值：
  - `ASHARE_RECOVERY_MAX_WORKSPACE_AGE_SECONDS`
  - `ASHARE_RECOVERY_MAX_SIGNAL_AGE_SECONDS`
  - `ASHARE_RECOVERY_REQUIRE_EXECUTION_BRIDGE`
- [x] 已补测试并通过：
  - 新增“fresh -> ready / stale -> blocked”两条恢复检查用例
  - `tests/test_upgrade_workflow.py` 当前为 `44/44 OK`
- [x] 已新增恢复演练剧本：`docs/restart_recovery_pressure_plan_20260415.md`
- [x] 已新增恢复演练编排脚本：`scripts/run_recovery_pressure_sequence.sh`
  - 默认 `dry-run`，先做 `service-recovery/apply-readiness` 前置检查，再按组件顺序采集 `before/after` 证据。
  - 显式 `--execute` 时仅执行 Linux 侧 `control-plane / scheduler / feishu-longconn` restart；`windows-bridge` 继续保留人工步骤，`openclaw / hermes` 当前以主备状态校验和失败证据留档为主。
- [x] 2026-04-15 已完成第一轮恢复编排 `dry-run` 留档：`bash scripts/run_recovery_pressure_sequence.sh --components control-plane,scheduler,feishu`
  - 证据目录：`logs/recovery_evidence/20260415_090527_sequence/`
  - 当前真实结果：`/health`、`/system/operations/components`、`/system/feishu/longconn/status` 均可返回，且 `longconn=connected fresh=True`。
  - 当前真实阻断：运行中的 control plane 对 `/system/deployment/service-recovery-readiness` 与 `/system/deployment/controlled-apply-readiness` 仍返回 `404`；仓库源码已有这两个路由，但 live 服务尚未加载到当前版本，因此现阶段不能宣称“恢复检查已在线可用”。
- [x] 2026-04-15 已修正 readiness 检查脚本的阻断口径
  - `scripts/check_service_recovery_readiness.sh` 与 `scripts/check_apply_pressure_readiness.sh` 遇到 live `404` 时，现会明确输出 `ROUTE_UNAVAILABLE` 与具体路由，而不再只留下模糊的 `curl: (22)`。
- [/] 2026-04-15 已尝试最小范围 live 重启 `control-plane`
  - 执行命令：`bash scripts/run_recovery_pressure_sequence.sh --execute --components control-plane`
  - 证据目录：`logs/recovery_evidence/20260415_091507_sequence/`
  - 当前真实结果：`before_control_plane_restart` 已成功留档，但 `systemctl restart` 未真正执行，阻断原因为宿主机 `sudo` 需要交互式密码，当前自动执行环境无 TTY 可输入密码：`sudo: a terminal is required to read the password`
  - 这意味着当前无法在纯自动链路里完成 live 服务切版本；若要继续，需改成用户手工重启、配置免密 systemd 管理，或提供可交互 sudo 通道。
- [x] 2026-04-15 用户手工重启 `control-plane` 后，readiness 路由已在线
  - 证据目录：`logs/recovery_evidence/20260415_091935_after_manual_control_plane_restart/`
  - 当前真实结果：`service_recovery_readiness=status=ready`，`controlled_apply_readiness=status=blocked`，`health=ok`，`longconn=connected fresh=True`
  - `apply blocked` 的当前原因真实落在业务侧，而不是接口缺失：`discussion_cycle unavailable`、`execution_precheck blocked`、`execution_intents=0`
- [x] 2026-04-15 已把 `check_apply_pressure_readiness.sh` 默认口径收敛为只读检查
  - 默认 `require_live=false`、`require_trading_session=false`，与恢复演练和证据留档口径保持一致。
  - 若后续要做真实小额 `apply=true` 压测，再显式通过环境变量切回更严实盘口径。
- [x] 2026-04-15 已为两类 readiness 路由补 `include_details=false` 轻量模式
  - 代码位置：`src/ashare_system/apps/system_api.py`
  - 检查脚本已默认走轻量模式，避免 `service_recovery_readiness` 因返回 `workspace/runtime/discussion/monitor/briefing` 大对象而超时。
  - 已补单测并通过：`tests/test_upgrade_workflow.py` 仍为 `44/44 OK`
- [/] 轻量模式当前已在仓库代码中完成，但 live 服务尚需再次重启后才会生效
  - 下一步重启后，应复查 `bash scripts/check_service_recovery_readiness.sh` 是否可直接返回 `READY`
  - 2026-04-15 本轮复查结果：两条检查脚本已带 `include_details=false` 发起请求，但 live 返回仍表现为旧版大包响应。
  - 真实现象：`service_recovery_readiness` 在 60 秒内已下发约 `16MB/22MB` 数据后超时，`controlled_apply_readiness` 仍在 60 秒内无正文返回；这说明当前运行中的 control plane 还没有加载到轻量模式代码。
- [x] 2026-04-15 已确认并修复 `controlled_apply_readiness` 的重复重算问题
  - 根因：`_build_controlled_apply_readiness()` 内先调用一次 `_build_execution_precheck()`，随后 `_build_execution_intents()` 又完整重算同一套 precheck，导致执行池预检在同一次请求内被做了两遍。
  - 修复：已新增 `_build_execution_intents_from_precheck()`，让 `controlled_apply_readiness` 复用首轮 precheck 结果，不再重复重算。
  - 已补回归验证：`tests/test_upgrade_workflow.py` 仍为 `44/44 OK`
- [/] 上述 apply readiness 去重优化当前只在仓库代码中生效，live 服务仍需再次重启后才会体现
  - 下一步重启后，应复查 `bash scripts/check_apply_pressure_readiness.sh` 是否可在合理时间内直接返回 `BLOCKED`
- [x] 2026-04-15 第二次手工重启 `control-plane` 后，`apply readiness` 已在 live 上稳定返回
  - 当前真实结果：`bash scripts/check_apply_pressure_readiness.sh` 已可直接返回 `BLOCKED`
  - 当前真实阻断项：`discussion_cycle`、`execution_precheck`、`execution_intents`
- [/] `service recovery readiness` 当前表现为“服务端可返回 200，但并发取证时偶发超时”
  - `journalctl` 已记录 `GET /system/deployment/service-recovery-readiness?...include_details=false HTTP/1.1" 200 OK`
  - 说明该路由功能上已在线，但在与取证脚本并发执行时仍可能被重负载拖慢
  - 已继续把 `collect_recovery_evidence.sh` 中的两条 readiness 请求也切到 `include_details=false`，下一步将用串行复查确认稳定性
- [x] 2026-04-15 串行复查已确认 Linux 恢复检查链可稳定工作
  - `bash scripts/check_service_recovery_readiness.sh` 已可直接返回 `READY`
  - `bash scripts/check_apply_pressure_readiness.sh` 已可直接返回 `BLOCKED`
  - 串行取证已成功落档：`logs/recovery_evidence/20260415_095602_serial_recovery_validation/`
  - 当前证据摘要：`health=ok`、`recovery=ready`、`apply_ready=blocked`、`longconn=connected fresh=True`
  - 当前剩余问题已从“控制面 readiness 路由不可用”收敛为“并发重负载下偶发超时”，不再是主链硬阻断
- [x] 2026-04-15 今天的 discussion -> execution 预演链已重新推进起来
  - 初始真实阻断：`/system/discussions/cycles/2026-04-15` 返回 `available=false`，导致 `apply readiness` 中 `discussion_cycle unavailable`
  - 随后已通过控制面把今日周期推进到 `cycle-20260415`
    - 当前 `discussion_state=round_1_running`
    - `focus_pool_case_ids=15`
    - `execution_pool_case_ids=3`
  - `check_apply_pressure_readiness.sh` 当前已反映真实新状态：
    - `discussion_cycle=OK`
    - `execution_precheck=OK`
    - `execution_intents=OK`
    - 唯一剩余阻断为受控围栏 `apply_intent_limit`，因为当前有 3 条 intents，而默认上限是 1
  - 已完成一次只读预演派发：`POST /system/discussions/execution-intents/dispatch` with `apply=false`
    - 返回 `status=preview`
    - `preview_count=3`
    - `blocked_count=0`
    - 三条预演回执分别为：
      - `000001.SZ 平安银行` `qty=1800` `price=11.17`
      - `000002.SZ 万 科Ａ` `qty=5000` `price=4.02`
      - `000004.SZ *ST国华` `qty=6100` `price=3.30`
  - 当前口径已回到“系统可预演、apply 仍受围栏阻断”，不再是 discussion 链未启动
- [x] 2026-04-15 preview 闭环证据已补齐
  - `GET /system/discussions/execution-dispatch/latest?trade_date=2026-04-15` 已返回最新预演派发回执
  - 当前结果：`status=preview`、`preview_count=3`、`blocked_count=0`
  - `summary_notification.dispatched=true reason=sent`，说明预演派发摘要已触发通知链
  - 串行证据留档：`logs/recovery_evidence/20260415_101051_preview_dispatch_closed_loop/`
  - 当前证据摘要：`health=ok`、`recovery=ready`、`apply_ready=blocked`、`longconn=connected fresh=True`
- [/] P0-1 当前已从“只有原则方案”推进到“带今日实数的受控 apply 口径”
  - 今日真实状态：`discussion_cycle=OK`、`execution_precheck=OK`、`execution_intents=OK`，当前唯一阻断为 `apply_intent_limit`，因为 `ready intents=3` 而默认上限是 `1`
  - 已把首轮受控 apply 建议固定到文档：`docs/controlled_apply_pressure_test_plan_20260414.md`
  - 当前建议首轮白名单：
    - `max_apply_intents=1`
    - `allowed_symbols=000001.SZ`
    - `intent_id=intent-2026-04-15-000001.SZ`
    - 预演金额约 `20106.0`
  - 2026-04-16 已把脚本默认口径继续收紧并落仓：
    - `scripts/check_apply_pressure_readiness.sh` 已支持 `ASHARE_APPLY_READY_MAX_SUBMISSIONS_PER_DAY`
    - `scripts/check_apply_pressure_readiness.sh` 已支持 `ASHARE_APPLY_READY_BLOCKED_TIME_WINDOWS`
    - `scripts/run_controlled_single_apply.sh` 默认已带 `max_apply_submissions_per_day=1`
    - `scripts/run_controlled_single_apply.sh` 默认已带 `blocked_time_windows=09:25-09:35,14:55-15:00`
  - `bash -n scripts/check_apply_pressure_readiness.sh`、`bash -n scripts/run_controlled_single_apply.sh` 已通过
  - 下一步只差：在交易时段用更严口径 `require_live=true + require_trading_session=true` 再跑一次准入检查，并决定是否执行首笔真实 `apply=true`
- [x] 2026-04-15 已补首笔受控单票执行脚本：`scripts/run_controlled_single_apply.sh`
  - 作用：只做“严格准入检查 -> 单 intent preview -> 可选 apply”
  - 默认严格口径固定为：
    - `max_apply_intents=1`
    - `require_live=true`
    - `require_trading_session=true`
    - `allowed_symbols=<单票白名单>`
    - `max_apply_submissions_per_day=1`
    - `blocked_time_windows=09:25-09:35,14:55-15:00`
  - 默认不会跑 runtime / bootstrap / round start，避免在首笔真实单时同时混入其他流程变量
  - 2026-04-16 已把前后证据收集也沉到脚本：
    - 自动留 `before_preview / after_preview / before_apply / after_apply` 证据目录
    - 自动抓取 `execution-dispatch/latest / execution/gateway/receipts/latest / account-state`
    - 自动打印 `preview_snapshot / apply_snapshot`，用于快速核对派发、回执与账户状态
- [x] 2026-04-15 已把“按指定 intent_id 做单票准入检查”补到控制面代码
  - `controlled_apply_readiness` 现已支持 `intent_ids=<csv>` 过滤
  - `check_apply_pressure_readiness.sh` 已支持环境变量 `ASHARE_APPLY_READY_INTENT_IDS`
  - `run_controlled_single_apply.sh` 已自动传入单票 `intent_id`
  - 已补单测并通过：`tests/test_upgrade_workflow.py` 仍为 `44/44 OK`
- [/] 上述单票 intent 准入过滤当前仅在仓库代码中完成，live 服务尚需再次重启后才会生效
  - 本轮严格准入口径复查仍表现为旧版输出：`apply_intent_limit` 和 `apply_symbol_whitelist` 仍按 3 条 intents 整体判断
  - 这说明当前在线 control plane 还未加载本轮 `intent_ids` 过滤改动；重启后再复查，预期只会剩 `run_mode_live` 与 `live_trade_enabled` 两项阻断
- [x] 2026-04-15 再次重启 `control-plane` 后，单票 intent 准入过滤已在 live 上生效
  - 严格口径复查：
    - `ASHARE_APPLY_READY_INTENT_IDS=intent-2026-04-15-000001.SZ`
    - `ASHARE_APPLY_READY_MAX_INTENTS=1`
    - `ASHARE_APPLY_READY_ALLOWED_SYMBOLS=000001.SZ`
    - `ASHARE_APPLY_READY_REQUIRE_LIVE=true`
    - `ASHARE_APPLY_READY_REQUIRE_TRADING_SESSION=true`
  - 当前真实结果：
    - `selected_intent_scope=OK`
    - `apply_intent_limit=OK`
    - `apply_symbol_whitelist=OK`
    - `execution_precheck=OK`
    - `execution_intents=OK`
  - 当前仅剩两项实盘准入阻断：
    - `run_mode_live`
    - `live_trade_enabled`
  - 这意味着首笔真实 `apply=true` 前，代码链和单票围栏已经收口完成；下一步不再是开发问题，而是是否切到真实实盘口径
- [/] P0-5 当前已从“只有方案文档”推进到“文档 + 证据收集 + 编排脚本”
  - 仓库内已能按标准顺序留存 `before_control_plane_restart / after_control_plane_restart / before_scheduler_restart / after_scheduler_restart / before_feishu_restart / after_feishu_restart / before_bridge_recovery / after_bridge_recovery / before_openclaw_verify / after_openclaw_verify / before_hermes_verify / after_hermes_verify` 证据。
  - 仍缺 live `systemctl restart ...` 实际执行证据、Windows 执行桥真实恢复证据、OpenClaw 主链与 Hermes 备链同窗恢复记录，以及把当前运行中的 control plane 更新到已包含 readiness 路由的版本；其中 Linux live 重启当前受阻于 `sudo` 交互密码要求。
- [/] P0-6 当前已把恢复压测的执行顺序和通过标准收敛进 `docs/restart_recovery_pressure_plan_20260415.md`
  - 已新增场景矩阵：`docs/pressure_test_scenario_matrix_20260415.md`
  - 当前已把“盘前 / 盘中 / 盘后 / 夜间 × 正常 / Linux 重启 / Windows 不可达 / 飞书掉线 / 主链失稳 / 无候选 / execution blocked / preview vs apply”整理成固定矩阵，后续按证据逐项回填。
- [x] 已新增恢复证据收集脚本：`scripts/collect_recovery_evidence.sh`
- [x] 证据脚本当前会统一归档：
  - `/health`
  - `/system/operations/components`
  - `/system/readiness`
  - `/system/deployment/service-recovery-readiness`
  - `/system/deployment/controlled-apply-readiness`
  - `/system/feishu/longconn/status`
  - `/system/workspace-context`
  - `/system/agents/supervision-board`
  - `/system/feishu/briefing`
  - `/system/discussions/execution-dispatch/latest`
  - `/monitor/state`
  - `systemctl --user status ashare-feishu-longconn.service --no-pager`
  - `bash scripts/ashare_feishu_longconn_ctl.sh --user verify`
- [x] 2026-04-16 已把 feishu 恢复取证补到“进程态 + 控制面态”双证据：
  - `collect_recovery_evidence.sh` 当前会额外归档 `feishu_longconn_service_status.txt`
  - `collect_recovery_evidence.sh` 当前会额外归档 `feishu_longconn_verify.txt`
  - `summary.txt` 会附带 `feishu_service_status` 与 `feishu_verify` 摘要行
- [x] 恢复剧本已明确主备 agent 验证顺序：先 OpenClaw 主链，再 Hermes backup，只在主链连续失稳时才判定“备可接管只读值班”
- [x] 已完成脚本验证：
  - `bash -n scripts/collect_recovery_evidence.sh`
- [/] 这一轮解决的是“恢复状态可程序判定”；真正的 live 重启演练与证据回填仍待执行。

## 2026-04-14 飞书长连接正式化补口

- [x] 已补 `scripts/ashare_feishu_longconn_ctl.sh`，与 `serve/scheduler` 对齐提供 `start/stop/restart/status/logs/enable/disable` 管理入口。
- [x] 2026-04-16 已把 `scripts/ashare_feishu_longconn_ctl.sh` 收口到当前真实运维口径：
  - 默认支持 `--user` 与 `--system`
  - `start/restart` 后会主动等待 `/system/feishu/longconn/status` 回到 `connected + is_fresh=true`
  - 已新增 `verify` 动作，可单独用于恢复压测后的健康确认
- [x] 已重写 `scripts/install_feishu_longconn_service.sh`：改为参数化、带环境与脚本校验、通过 `sudo install` 写入 unit，和其他 systemd 安装脚本保持一致。
- [x] 已补仓库内 unit 模板 `deploy/systemd/ashare-feishu-longconn.service`，便于审阅与手工部署对照。
- [x] 已把飞书长连接纳入 `GET /system/operations/components`，统一健康入口可直接看到 `status / reported_status / pid_alive / freshness age` 摘要。
- [x] 已补测试并通过：`PYTHONPATH="/srv/projects/ashare-system-v2/src" /srv/projects/ashare-system-v2/.venv/bin/python -m unittest tests/test_upgrade_workflow.py tests/test_feishu_longconn.py` -> `Ran 41 tests in 8.755s, OK`。
- [/] live 的 `ashare-feishu-longconn.service` 实机安装、开机自启与“服务重启/断链恢复”压测仍待执行并回填证据。
  - 当前仓库侧已具备：
    - 用户级安装命令 `bash scripts/install_feishu_longconn_service.sh --user`
    - 用户级运维命令 `bash scripts/ashare_feishu_longconn_ctl.sh --user restart|verify|logs`
    - 恢复编排脚本 `scripts/run_recovery_pressure_sequence.sh` 已改为按 `--user restart` 执行 feishu 步骤
  - 当前剩余已收敛为“在线环境实机执行并把 before/after 证据归档”，不再是仓库脚本能力缺失。

## 2026-04-16 Go 平台与控制面稳态收口

- [x] `/system/account-state` 已从“默认实时慢查询”改为“默认读缓存 + 显式 refresh 才重拉”
  - 默认 `GET /system/account-state`：
    - 只返回最近快照
    - 缓存缺失时返回 `status=cache_unavailable`
    - 不再把控制台和观测链绑死在 QMT 慢接口上
  - 显式刷新 `GET /system/account-state?refresh=true`：
    - 仍可拉真实账户状态
    - 本轮已改为 `include_trades=false`
    - 避免 `orders/trades` 慢链拖累账户态刷新
  - 已补测试并通过：
    - `test_account_state_defaults_to_cache_and_supports_explicit_refresh`
    - `test_account_state_cached_endpoint_does_not_requery_execution_adapter`
- [x] `StateStore` 已补单进程 mtime 缓存，并新增 `get_many()`
  - 目的不是改业务逻辑，而是避免对 `meeting_state.json` 这类大文件反复整文件重载
  - `readiness` 已改为单次装载多键读取，不再对同一份状态文件重复读盘与反序列化
- [x] live 实测结果已确认
  - `GET http://127.0.0.1:8100/system/account-state`
    - 约 `0.007s`
  - `GET http://127.0.0.1:8100/system/readiness`
    - 约 `0.043s`
  - `GET http://127.0.0.1:18793/system/readiness`
    - 约 `0.010s`
  - 对比本轮修复前：
    - `/system/account-state` 默认仍会打真实慢链
    - `/system/readiness` 一度 `10s timeout`，随后确认瓶颈主要在状态文件重载
- [/] 当前 readiness 已恢复为“快速返回真实状态”，但业务阻断仍客观存在
  - 2026-04-16 晚间继续收口后，当前状态已从 `blocked` 降为 `degraded`
  - 当前已确认并修掉的假阻断：
    - `execution_mode/market_mode` 与旧 `xtquant` 健康检查口径未完全对齐
    - 现已按 `go_platform` 主链校正，`/system/healthcheck` 返回 `ok=true`
    - 启动阶段现会自动刷新 `latest_account_state`，账户缓存不再因服务重启后过期而误报
    - `inspection/remediation/reconciliation` 等重接口已改走线程池，不再把整个控制面事件循环拖死
  - 当前剩余真实降级项已收敛为：
    - `execution_reconciliation=warning`
    - 错误详情：`windows_gateway_timeout: /qmt/account/trades | elapsed>10.0s`
  - 当前已确认恢复正常的项：
    - `pending_order_inspection=ok`
    - `pending_order_remediation=ok`
    - `account_access/account_identity=ok`
  - 这部分需要继续分成两类处理：
    - 一类是 Windows/QMT 真慢接口导致的对账/回执问题
    - 一类是前端控制台如何按业务语义展示 `degraded` 而非旧运维口径的红色误报
- [/] 控制台前端已完成现状审查，尚未开始正式改造
  - 2026-04-16 晚间已完成第一轮主控台改版落地：
    - 侧边栏与页头文案已从“系统运维面板”改成“交易主控台 / Agent 履职 / 风控围栏 / 调参与治理”
    - 首页已改为围绕 `readiness + account_state + supervision + operations_components + audits`
      展示：
      - 账户状态
      - Agent 履职热度
      - 当前降级项
      - 审计流水
      - 关键检查项
    - Agent 页已改为围绕履职痕迹、催办理由、活动信号与升级队列展示，不再只是状态卡
    - 全局视觉已从默认灰白运维样式调整为更接近交易台/指挥台的暖底色信息板风格
  - 当前已验证前端可构建：
    - `cd web && npm run build`
  - 当前仍未完成的前端主线：
    - Discussion 页仍偏候选清单，还未升级为“机会票辩论与证据链”
    - Risk / Governance 页仍偏静态样板，还未接真实接口
    - 尚未补“执行台 / 回执 / 风控阻断 / 飞书调参与询问权”的专门页面

## 2026-04-17 Agent 主线闭环纠偏

- [x] 已把 `discussion cycle` 的 `idle -> round_1_running` 自动推进补到主链
  - 文件：
    - `src/ashare_system/discussion/discussion_service.py`
  - 口径：
    - 只要 cycle 已 bootstrap 且已有 `base/focus/execution` 任一候选池，不再长期停在 `idle`
    - `refresh_cycle()` 会自动启动 `round 1`
  - 目的：
    - 解决“候选池已经有了，但没人真正推进 round 1”的空转问题

- [x] 已把 supervision 的默认催办口径从“有活动就算在干活”改成“只认主线产物”
  - 文件：
    - `src/ashare_system/apps/system_api.py`
    - `src/ashare_system/supervision_tasks.py`
  - 当前新增的真实约束：
    - `ashare` 在 `cycle=idle` 且候选池已就绪时会被直接判为 `needs_work`
    - `ashare-strategy` 仅有参数提案但没有 `compose_evaluation / proposal_packet / playbook_override / nightly_sandbox` 时，不再算“已完成工作”
    - supervision 摘要里会明确暴露“今日 compose 评估账本=0”
  - 目的：
    - 解决“agent 有活动痕迹，但没有把东西写回主线”的错判

- [x] 已把 strategy 的默认任务改成优先产出 `compose` 草案，而不是口头说“要不要跑 runtime”
  - 当前催办词新增要求：
    - 市场假设
    - playbooks / factors / weights
    - 约束与放弃项
    - 是否调用 `/runtime/jobs/compose-from-brief`
  - 目的：
    - 把 runtime 从“默认粗筛器”往“agent 自主编排后调用的工具”推近一步

- [x] 已补针对性测试并通过
  - `test_agent_task_plan_pushes_strategy_to_compose_when_cycle_idle`
  - `test_agent_supervision_board_marks_strategy_param_activity_without_compose_as_needs_work`
  - `test_discussion_refresh_auto_starts_round_1_when_cycle_idle`
  - 以及一组回归测试：
    - `test_agent_task_plan_includes_quality_gap_in_reason_prompt_and_summary`
    - `test_agent_task_plan_generates_follow_up_for_coordinator_when_four_roles_completed`
    - `test_agent_supervision_board_marks_activity_without_round_progress_as_low_quality`

- [/] 当前仍未完成的主线差距
  - runtime 今日主链仍以 `pipeline` 为主，`compose` 已能用，但还没形成 agent 日常自驱调用常态
  - strategy/research/risk/audit 虽已被更严格催办，但“主动盯盘 -> 主动提案 -> 主动写回 discussion”还需要继续往运行态收口
  - Windows gateway 执行回执回写链仍需继续盯，当前不能把“讨论更主动”误判成“执行闭环已经彻底打通”

- [x] 已把 scheduler 的 `supervision.agent:check` 收口到新主线口径
  - 文件：
    - `src/ashare_system/scheduler.py`
  - 已补行为：
    - 监督巡检开始前会自动 `bootstrap + refresh cycle`
    - 总协调 `ashare` 正式纳入催办与派发
    - supervision 摘要纳入 `compose` 账本缺口提示
  - 2026-04-17 12:30 运行态已验证：
    - `cycle_state` 从 `idle` 自动推进到 `round_1_running`
    - supervision 自动把 `ashare-research / ashare-strategy / ashare-risk / ashare-audit` 标为 `overdue`
    - 当前催办重点已转成“补齐 round 1 主线材料”，而不是只看活动痕迹

- [x] 已把 `compose -> strategy opinions -> discussion 主线` 的桥正式补齐
  - 文件：
    - `src/ashare_system/discussion/discussion_service.py`
    - `src/ashare_system/apps/system_api.py`
    - `src/ashare_system/scheduler.py`
    - `tests/test_upgrade_workflow.py`
  - 本轮新增能力：
    - `DiscussionCycleService` 新增 `adapt_compose_result_to_strategy_opinions()` 与 `write_compose_strategy_opinions()`
    - 新增内部入口：`POST /system/discussions/opinions/compose-writeback`
    - 支持两种 compose 来源：
      - runtime 直接返回的 `compose` payload
      - `compose_evaluations` 账本里的最新记录 / 指定 `trace_id`
    - `scheduler` 在保守条件下会自动尝试把当日最新 compose 账本写回为 `ashare-strategy` 的 round opinion：
      - discussion 已处于运行轮次
      - 本轮 strategy 仍 0 覆盖
      - 当日已有 compose 账本
  - 当前效果：
    - strategy 不再只是“有 compose 痕迹但没落主线”
    - supervision 有机会从 `Round 1 仅覆盖 0/15` 进入“已有部分覆盖，继续催补”的真实状态
    - 该桥只写 discussion，不触发 live execution
  - 已通过测试：
    - `test_discussion_cycle_service_can_write_compose_result_into_strategy_opinions`
    - `test_system_api_compose_writeback_uses_latest_compose_evaluation`
    - 回归：
      - `test_discussion_refresh_auto_starts_round_1_when_cycle_idle`
      - `test_agent_supervision_board_marks_strategy_param_activity_without_compose_as_needs_work`

- [x] 已补给 Hermes/OpenClaw 消费的统一任务包入口，解决“催办有了，但 agent 仍要自己拼上下文”的问题
  - 文件：
    - `src/ashare_system/apps/system_api.py`
    - `tests/test_upgrade_workflow.py`
  - 新增入口：
    - `GET /system/agents/runtime-work-packets`
  - 当前统一收口内容：
    - 当前 supervision 推荐任务 / phase / task_prompt / expected_outputs
    - 角色合同：persona / responsibility / read / write / initiative_rules / trigger_conditions
    - discussion dossier 引用：`/system/discussions/agent-packets?trade_date=...&agent_id=...`
    - strategy/runtime/主控可直接参考的 `compose-from-brief` 起手 payload
    - 当前 runtime 偏好：`excluded_theme_keywords / equity_position_limit / max_single_amount`
  - 运行态已验证：
    - `ashare-strategy` 当前可拿到：
      - `phase=下午盘中`
      - `status=overdue`
      - `compose_brief_hint.available=true`
      - `compose_brief_hint.endpoint=/runtime/jobs/compose-from-brief`
      - 当前偏好同步为：排除 `银行`、总仓位 `0.3`、单票 `20000`
  - 已通过测试：
    - `test_agent_runtime_work_packets_exposes_strategy_task_and_compose_hint`

- [x] 已把 `ashare-strategy` 的最小自治起手接到 scheduler，避免“只有催办，没有真正开工”
  - 文件：
    - `src/ashare_system/scheduler.py`
  - 新增动作：
    - `autonomy.agent:runtime_bootstrap`
    - 盘中按节奏读取 `GET /system/agents/runtime-work-packets`
    - 当 `ashare-strategy` 处于 `needs_work/overdue` 且当天需要补主线时：
      - 自动取 `compose_brief_hint.sample_payload`
      - 调用 `/runtime/jobs/compose-from-brief`
      - 再调用 `/system/discussions/opinions/compose-writeback`
    - 带冷却控制，避免无脑重复打 compose
  - 2026-04-17 运行态已验证：
    - compose 账本已从 `0` 增加到 `2`
      - `compose-runtime-840d7961b3`
      - `compose-runtime-09a68d27ec`
    - `ashare-strategy` 的 Round 1 覆盖已从 `0/15` 提升为非零
      - 曾实测到 `7/15`
      - 当前因 focus pool 动态刷新，监督面板显示为 `6/15`
    - 说明“任务包 -> compose -> writeback -> supervision 反映”主链已打通
  - 当前仍未完成：
    - research / risk / audit 仍主要停留在催办态，尚未形成同等级自动起手
    - strategy 当前是“最小自治起手”，还不是完整的多 agent 自主辩论链

- [x] 已把 `ashare-research / ashare-risk / ashare-audit` 的最小真实写回链补齐，并接入统一自治起手
  - 文件：
    - `src/ashare_system/apps/system_api.py`
    - `src/ashare_system/scheduler.py`
    - `tests/test_upgrade_workflow.py`
  - 新增内部入口：
    - `POST /system/discussions/opinions/research-writeback`
    - `POST /system/discussions/opinions/risk-writeback`
    - `POST /system/discussions/opinions/audit-writeback`
  - 当前写回原则：
    - `research` 只根据真实 `research summary / event_context / dossier / 板块标签` 写 `support/watch/rejected`
    - `risk` 只根据真实 `execution_precheck` 写 `support/limit/rejected/question`
    - `audit` 只根据前置席位覆盖、case 矛盾、risk gate 与 review board 复核材料写 `support/hold/question`
    - 全部只写 discussion 主线，不触发 live execution
  - scheduler 本轮补强：
    - `autonomy.agent:runtime_bootstrap` 不再只盯 `ashare-strategy`
    - 现会按 agent 分流：
      - strategy 优先写回已有 compose；没有 compose 时再跑 compose
      - research / risk / audit 直接调用各自 writeback 入口
    - 每个 agent 维持独立冷却、成功回执和错误记录
  - 已通过测试：
    - `test_system_api_research_risk_audit_writebacks_progress_mainline`
    - 回归：
      - `test_system_api_compose_writeback_uses_latest_compose_evaluation`
      - `test_agent_runtime_work_packets_exposes_strategy_task_and_compose_hint`
      - `test_agent_supervision_board_marks_strategy_param_activity_without_compose_as_needs_work`
  - 当前真实语义：
    - 四席都可以开始把真实材料写回主线
    - 但写回结果仍取决于事实上下文：
      - 缺 dossier / 缺 precheck 时，research 可能只给 `watch`，risk 可能只给 `limit/question`
      - audit 在前置事实不足时仍会 `hold`
    - 这属于真实保守，不是卡死或假通过

- [x] 已完成 2026-04-19 专家二轮问题整改
  - 文件：
    - `src/ashare_system/strategy/factor_registry.py`
    - `src/ashare_system/strategy/evaluation_ledger.py`
    - `src/ashare_system/learning/settlement.py`
    - `src/ashare_system/strategy/strategy_composer.py`
    - `src/ashare_system/runtime_config.py`
    - `docs/expert_review_20260419.md`
  - 已完成项：
    - 扩展因子从 `_simple_factor_executor` 占位切到真实派生执行器，去除对 `selection_score` 的伪独立依赖
    - `pb_ratio` 改为按市场风格切换，不再一律惩罚高 PB 成长
    - `reconcile_outcome` 自动驱动 agent settlement / Elo 喂数，并纳入 `final_status / risk_gate / audit_gate`
    - `composite_adjustment_multiplier` 默认值不再写死 `10.0`，改为账本估算优先、样本不足时保守回退 `4.0`
    - 评审文档已逐条回写“已完成 / 未完成”
  - 明确未完成项：
    - `composite_adjustment_multiplier` 的独立历史回测寻优仍未完成，只完成了保守估算替代
