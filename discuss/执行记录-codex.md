# Codex 执行记录

## 2026-04-06 基线接管

- 自动流程停在 M0-M2 基线审计，没有落下这份记录文件。
- 已确认 M0-M2 当前主链路已具备最小闭环：
  - `contracts.py` 已扩展 `MarketProfile / SectorProfile / PlaybookContext / ExitContext`
  - `sentiment/regime.py`
  - `sentiment/sector_cycle.py`
  - `strategy/router.py`
  - `strategy/buy_decision.py`
  - `risk/emotion_shield.py`
- 基线测试在正确命令下通过：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_quant_first_batch.py tests/test_phase3_strategy_risk_backtest.py`
  - 结果：`42 passed`
- 自动流程先踩到两个环境问题：
  - 只读沙箱无可用临时目录，`pytest` 默认捕获初始化失败
  - 未带 `PYTHONPATH=src` 时导入失败

## 2026-04-06 M3/M4 接管实现

本轮直接接手推进 M3 和 M4 的最小可用闭环：

- 新增契约：
  - `StockBehaviorProfile`
  - `LeaderRankResult`
  - `ExitSignal`
- 新增模块：
  - `strategy/stock_profile.py`
  - `strategy/leader_rank.py`
  - `strategy/exit_engine.py`
- 主链路接线：
  - `router.py` 支持消费股性画像和龙头相对排名
  - `sell_decision.py` 支持优先调用 `ExitEngine`，未命中新规则时回退 ATR 旧逻辑
- 新增测试：
  - `tests/test_phase_stock_profile.py`
  - `tests/test_phase_leader_rank.py`
  - `tests/test_phase_exit_engine.py`

### 当前取舍

- 先做规则型最小实现，不强依赖逐笔、盘口和全量实时字段。
- `StockProfileBuilder` 优先消费稳定字段：
  - `is_zt`
  - `seal_success`
  - `bombed`
  - `afternoon_resealed`
  - `next_day_return`
  - `return_day_1/2/3`
  - `sector_rank`
  - `is_leader`
- `StrategyRouter` 目前只做轻量加权增强，不推翻原有路由表。
- `ExitEngine` 先覆盖：
  - `entry_failure`
  - `board_break`
  - `sector_retreat`
  - `no_seal_on_surge`
  - `time_stop`
  - 未命中新规则时仍回退旧 `SellDecisionEngine.evaluate`

### 仍待补点

- M3 目前还没把股性画像做成独立日更产物或缓存层。
- M4 目前还没接入更细的盘中实时字段，`no_seal_on_surge` 仍是保守近似规则。
- M5/M6 尚未开始。

## 2026-04-06 M5 最小协议层与治理兼容修复

本轮完成 M5 的最小可用落地，重点不是重写 discussion 系统，而是在现有链路外包一层稳定协议。

- 新增协议对象：
  - `discussion/protocol.py`
  - `DiscussionAgentPacketsEnvelope`
  - `DiscussionMeetingContextEnvelope`
  - `DiscussionFinalizePacketEnvelope`
- discussion / OpenClaw 接线：
  - `apps/system_api.py`
  - `discussion/__init__.py`
  - `openclaw/team_registry.final.json`
  - `openclaw/prompts/ashare.txt`
- 新增或增强输出：
  - `/system/discussions/agent-packets`
  - `/system/discussions/meeting-context`
  - `/system/discussions/finalize-packet`
  - finalize 返回 `finalize_packet`

### 这轮关键修复

- 保留旧讨论语义兼容：
  - `support / watch / limit / question / rejected`
  - 不再把 `support` 强转成 `selected`
  - 不再把 `watchlist` 原样透出给旧测试，而是回到 `watch`
- 补回旧字段别名：
  - `support_points`
  - `oppose_points`
  - `selection_display`
  - `selection_lines`
  - `selection_packets`
- 二轮“实质回应”判定收紧：
  - 仅 `reasons + evidence_refs` 不再视为完成 Round 2
  - 必须出现质疑/回应/改判/关键证据等更强字段
- finalize 改回“讨论优先”：
  - 终态不再被 mock runtime 的 `HOLD` 误压成 `watchlist`
- 执行预检调整：
  - `stock_test_budget` 仍保留展示和统计
  - 但不再作为 mock / 讨论链路的硬阻断
  - 恢复 `execution_intents` 生成
- 协议 envelope 兼容补齐：
  - 顶层保留 `generated_at`
  - meeting context / final brief / reply pack 都保留新旧字段并存

### 验证结果

- 治理套件：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_system_governance.py`
  - 结果：`50 passed`
- M3-M5 联合回归：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_system_governance.py tests/test_quant_first_batch.py tests/test_phase3_strategy_risk_backtest.py tests/test_phase_stock_profile.py tests/test_phase_leader_rank.py tests/test_phase_exit_engine.py`
  - 结果：`99 passed`

### 当前结论

- M5 的“最小协议对象层 + 讨论编排接口 + 向后兼容”已经可用。
- 后续若继续做 M5 深化，优先补：
  - strategy/runtime 独立 dossier API
  - 更严格的 OpenClaw 结构化输入输出约束

## 2026-04-06 M5 strategy/runtime 独立 dossier API

本轮继续把 M5 往前推，补上 taskboard 里原来缺的 strategy/runtime 独立查询入口，避免外部只能从 `/system/discussions/*` 间接拿数据。

- 更新文件：
  - [strategy_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/strategy_api.py)
  - [runtime_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
  - [app.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/app.py)
  - [test_phase_strategy_runtime_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_strategy_runtime_api.py)

### 新增接口

- `GET /strategy/context/latest`
  - 返回：
    - active strategies
    - 最新候选摘要
    - 热门板块推断
    - runtime / dossier 引用入口
    - `regime` 缺口说明
- `GET /strategy/candidates/latest`
  - 面向外部直接读取策略候选摘要
- `GET /runtime/context/latest`
  - 直接返回 serving 层最新 `runtime_context`
- `GET /runtime/dossiers/latest`
  - 直接返回 serving 层最新 dossier 包
- `GET /runtime/dossiers/{trade_date}/{symbol}`
  - 返回单票 dossier

### 当前取舍

- `regime / allowed_playbooks / playbook assignment` 目前还没有被持久化到 serving 层。
- 本轮不伪造这些字段，而是：
  - `regime.value = "unknown"`
  - `gaps` 中显式标记：
    - `market_regime_not_persisted`
    - `playbook_assignment_not_persisted`
- `hot_sectors` 先由 dossier 内的 `sector_tags` 聚合推断，满足最小查询需求。

### 验证结果

- 新增 API 测试：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_phase_strategy_runtime_api.py`
  - 结果：`3 passed`
- 联合回归：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_system_governance.py tests/test_quant_first_batch.py tests/test_phase3_strategy_risk_backtest.py tests/test_phase_stock_profile.py tests/test_phase_leader_rank.py tests/test_phase_exit_engine.py tests/test_phase_strategy_runtime_api.py`
  - 结果：`102 passed`

### 当前结论

- T09 的“strategy/runtime API 对外暴露候选与 dossier 结果”已达到最小可用。
- M5 剩余深水区主要变成：
  - 持久化真实 `MarketProfile`
  - 持久化 playbook assignment / router 结果
  - 让 strategy API 返回真实 `regime + allowed_playbooks + assignment`，而不是缺口标记

## 2026-04-06 M5 真实 regime / playbook 持久化

本轮继续把 M5 做完，不再停留在 `gaps` 占位，而是把运行时真实策略上下文落到 serving。

- 更新文件：
  - [runtime_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
  - [strategy_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/strategy_api.py)
  - [test_phase_strategy_runtime_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_strategy_runtime_api.py)

### 本轮落地内容

- `runtime/jobs/pipeline` 现在会先生成 `market_profile`
  - 优先走 `SentimentCalculator.calc_from_market_data`
  - 若行情侧缺少有效昨收导致无法产出可用 regime，则回退 `runtime_heuristic_fallback`
- runtime 持久化内容已扩展为：
  - `market_profile`
  - `sector_profiles`
  - `playbook_contexts`
  - `playbook_count`
  - `hot_sectors`
- dossier pack 会在预计算后再做一轮 enrich：
  - `market_context` 增补 `regime / allowed_playbooks / sector_profiles / playbook_contexts`
  - 单票 dossier 增补：
    - `resolved_sector`
    - `playbook_context`
    - `assigned_playbook`
- `strategy/context/latest` 与 `strategy/candidates/latest` 已改为优先读取持久化 runtime context，不再默认返回 `unknown + gaps`

### 当前取舍

- 板块归属优先级：
  - dossier `sector_tags`
  - `market_adapter.get_sectors()/get_sector_symbols()`
  - 兜底 `候选池聚类`
- 在 mock 行情下，由于默认快照没有有效 `pre_close`，`market_profile` 可能来自启发式 fallback，但会显式保留：
  - `source`
  - `inferred`

### 验证目标

- `runtime/context/latest` 能看到真实 `market_profile / playbook_contexts`
- `strategy/*` 接口能看到真实 `regime / allowed_playbooks / sector_profiles`
- `/runtime/dossiers/{trade_date}/{symbol}` 单票 dossier 能看到 `playbook_context`

## 2026-04-06 M6 最小归因对象层

本轮开始推进 M6，不再只做 agent 学分结算，而是把 outcome 同步沉淀成按战法、市场状态、退出原因的归因结果。

- 新增文件：
  - [attribution.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/learning/attribution.py)
- 更新文件：
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)

### 本轮落地内容

- 新增 `TradeAttributionService`
  - 持久化 `TradeAttributionRecord`
  - 聚合输出：
    - `by_playbook`
    - `by_regime`
    - `by_exit_reason`
  - 同时生成 `review_summary` 与 `summary_lines`
- `POST /system/agent-scores/settlements`
  - 现在除了更新 agent score，还会同步写入 attribution
  - outcome 输入新增可选字段：
    - `exit_reason`
    - `holding_days`
    - `playbook`
    - `regime`
- 新增接口：
  - `GET /system/learning/attribution`
  - `GET /system/learning/trade-review`

### 当前取舍

- 先沿用现有 settlement 链路，以 `next_day_close_pct` 作为最小收益标签。
- `playbook / regime` 优先取 settlement 显式字段，缺失时回退 runtime context / dossier。

## 2026-04-08 第七批主线接线收口

本轮把第七批 A/B/C 子线产物真正消费进 Main 的 `system_api` 主链，同时保持未来生产部署边界不变：

- Linux 侧运行：
  - `OpenClaw Gateway`
  - agent 团队
  - `ashare-system-v2`
- Windows VM 侧运行：
  - `Windows Execution Gateway`
  - `QMT / XtQuant`
- agent 仍不直接持有 QMT 下单权。
- 自我进化产物仍只允许停留在离线研究闭环，不允许直推 live。

### 本轮更新文件

- [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
- [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
- [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 主线新增接法

- `review-board` 已并入 `execution_bridge_health`
  - 当前直接消费：
    - `latest_execution_bridge_health`
    - `execution_bridge_health_trend_summary`
  - 可直接展示：
    - `overall_status`
    - `attention_count`
    - `summary_lines`
    - `trend_summary`
- `postclose-master` 已并入：
  - `latest_execution_bridge_health`
  - `latest_execution_bridge_health_trend_summary`
  - `latest_offline_self_improvement`
- 新增 API：
  - `POST /system/discussions/opinions/openclaw-replay-packet`
  - `POST /system/discussions/opinions/openclaw-proposal-packet`
  - `GET /system/reports/offline-self-improvement`

### 当前行为边界

- replay / proposal packet 仍是纯离线 helper：
  - `offline_only = true`
  - `live_trigger = false`
- offline self-improvement report 读取的是离线导出契约：
  - 优先 `latest_offline_self_improvement_export.json`
  - 兼容 `latest_offline_self_improvement.json`
  - 兼容 `self_improvement_export / export_packet`
- 这轮没有放宽任何 live 执行边界：
  - 不新增 agent -> QMT 直连写口
  - 不让 self-improvement 自动进入 live 链

### 定向回归

- 命令：
  - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_replay_packet_endpoint_builds_offline_packet_without_writeback tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_proposal_packet_endpoint_builds_research_only_packet tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_opinion_ingress_endpoint_writes_batch tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_opinion_preview_endpoint_normalizes_without_writeback tests/test_system_governance.py::SystemGovernanceTests::test_offline_backtest_attribution_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_offline_backtest_metrics_report_endpoint_reads_latest_export -q`
- 结果：
  - `8 passed in 10.88s`

## 2026-04-08 第八批主线起步：Windows -> Linux 健康上报入口

本轮继续往未来生产架构收口，不再只让 Linux 主控“读取已有 monitor state”，而是补了一个明确的远端写入口，让 Windows Execution Gateway 可以主动把执行桥健康快照上报到 Linux 主控。

### 更新文件

- [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
- [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
- [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮新增能力

- 新增接口：
  - `POST /system/monitor/execution-bridge-health`
- 输入：
  - `trigger`
  - `health`
- 行为：
  - 直接调用 `monitor_state_service.save_execution_bridge_health(...)`
  - 立即返回：
    - `latest_execution_bridge_health`
    - `trend_summary`
    - `summary_lines`
- 作用：
  - 未来 Windows VM 内的 `Windows Execution Gateway` 可以直接把：
    - `gateway_online`
    - `qmt_connected`
    - `windows_execution_gateway`
    - `qmt_vm`
    上报给 Linux 侧 `ashare-system-v2`
  - Linux 侧现有 `review-board / postclose-master / /monitor/state` 会自动消费这份持久化结果

### 语义边界

- 这仍然只是健康观测入口，不是执行写口。
- Linux/OpenClaw 仍不直接持有 QMT 下单权。
- 真正的 live 执行写口仍应收口在 Windows Execution Gateway。

### 定向回归

- 命令：
  - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_execution_bridge_health_ingress_endpoint_persists_linux_windows_bridge_snapshot tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_replay_packet_endpoint_builds_offline_packet_without_writeback tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_proposal_packet_endpoint_builds_research_only_packet -q`
- 结果：
  - `5 passed in 9.99s`

### 同轮补充：OpenClaw replay / proposal packet 归档元数据已纳入主线断言

- 主线未新增接口，只补 system API 端到端断言，确认以下字段会透出：
  - `packet_id`
  - `source_refs`
  - `archive_tags`
  - `research_track`
- 覆盖接口：
  - `POST /system/discussions/opinions/openclaw-replay-packet`
  - `POST /system/discussions/opinions/openclaw-proposal-packet`
- 定向回归：
  - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_replay_packet_endpoint_builds_offline_packet_without_writeback tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_proposal_packet_endpoint_builds_research_only_packet tests/test_system_governance.py::SystemGovernanceTests::test_execution_bridge_health_ingress_endpoint_persists_linux_windows_bridge_snapshot tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_report_endpoint_reads_latest_export -q`
  - `4 passed in 1.99s`

### 同轮补充：execution bridge 来源识别字段已接入主线摘要

- A 线新增字段：
  - `reported_at`
  - `source_id`
  - `deployment_role`
  - `bridge_path`
- Main 当前已直接消费：
  - `review-board.sections.execution_bridge_health`
  - `postclose-master.latest_execution_bridge_health`
  - `postclose-master.latest_execution_bridge_health_trend_summary`
- 这样 Linux 主控不再需要自己猜“来自哪台 Windows VM / 哪条执行桥”。
- 定向回归：
  - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_execution_bridge_health_ingress_endpoint_persists_linux_windows_bridge_snapshot tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion -q`
  - `2 passed in 10.19s`

### 同轮补充：offline self-improvement 已切到 serving-ready helper

- Main 已不再手写 `latest_offline_self_improvement_export.json`。
- 当前主线消费方式：
  - `PlaybookBacktestRunner.write_latest_offline_self_improvement_export(...)`
  - `system_api._normalize_offline_self_improvement_payload(...)` 优先识别 `serving_ready_latest_export`
  - `GET /system/reports/offline-self-improvement` 会透出：
    - `serving_ready`
    - `artifact_name`
- 边界未变：
  - `live_execution_allowed = false`
  - 仍只服务离线研究与自我进化
- 定向回归：
  - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion -q`
  - `2 passed in 9.82s`

### 同轮补充：离线产物 archive/persist 主线入口已补齐

- 数据底座新增：
  - `DataArchiveStore.persist_offline_self_improvement_export(...)`
  - `DataArchiveStore.persist_openclaw_packet(...)`
  - `ServingStore.get_latest_offline_self_improvement_export()`
  - `ServingStore.get_latest_openclaw_packet(packet_type)`
- 主线新增接口：
  - `POST /system/reports/offline-self-improvement/persist`
  - `POST /system/discussions/opinions/openclaw-replay-packet/archive`
  - `POST /system/discussions/opinions/openclaw-proposal-packet/archive`
- `postclose-master` 已新增：
  - `latest_openclaw_replay_packet`
  - `latest_openclaw_proposal_packet`
- 这意味着 Linux 主控现在不但能“读 latest”，也能通过主线接口把：
  - offline self-improvement export
  - OpenClaw replay/proposal packet
  标准化落盘到 archive/serving。
- 语义边界未变：
  - packet 与 export 仍然是离线研究产物
  - 不直接触发 live
  - 不越过 Windows Execution Gateway 的唯一执行写口
- 定向回归：
  - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_packet_archive_endpoints_persist_latest_packets tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_persist_endpoint_accepts_runner_result_payload tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_replay_packet_endpoint_builds_offline_packet_without_writeback tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_proposal_packet_endpoint_builds_research_only_packet -q`
  - `5 passed in 2.46s`

## 2026-04-08 第九批 helper 已接入 Main 主线

本轮把 A/B/C 第九批产物真正收口进 `system_api.py` 和数据底座，不再停留在仓内 helper。

- 更新文件：
  - `src/ashare_system/apps/system_api.py`
  - `src/ashare_system/data/archive.py`
  - `tests/test_system_governance.py`
- 主线变化：
  - `POST /system/monitor/execution-bridge-health`
    - 现在先走 `build_execution_bridge_health_ingress_payload(...)`
    - Windows Gateway 按 helper 产出的 body 直接上报即可
    - 对 helper 默认补出来的 `overall_status = "unknown"`，Main 会在写入前补一次兼容推导，不阻断原本的 degraded/down 判断
  - `POST /system/reports/offline-self-improvement/persist`
    - 现在会保留并透传 `archive_ready_manifest`
    - 若调用方只给 serving-ready export，Main 会用 `PlaybookBacktestRunner.build_archive_ready_manifest(...)` 自动补 manifest
    - `DataArchiveStore.persist_offline_self_improvement_export(...)` 会真正按 manifest 的 `relative_archive_path` 落归档文件
  - `POST /system/discussions/opinions/openclaw-replay-packet/archive`
  - `POST /system/discussions/opinions/openclaw-proposal-packet/archive`
    - 现在既支持“原始 OpenClaw payload -> 构包 -> 归档”
    - 也支持“已生成 packet -> 直接归档”
    - Main 会用 `DiscussionCycleService.build_openclaw_archive_manifest(...)` 重建/固化 manifest
    - `DataArchiveStore.persist_openclaw_packet(...)` 会真正按 `archive_manifest.archive_path` 和 `latest_aliases` 写入归档文件
- 语义边界仍不变：
  - replay / proposal / self-improvement 全部仍是 `offline_only`
  - 不触发 live
  - 不越过 Windows Execution Gateway 的唯一执行写口
- 回归：
  - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_execution_bridge_health_ingress_endpoint_persists_linux_windows_bridge_snapshot tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_replay_packet_endpoint_builds_offline_packet_without_writeback tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_packet_archive_endpoints_persist_latest_packets tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_proposal_packet_endpoint_builds_research_only_packet tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_persist_endpoint_accepts_runner_result_payload -q`
  - `6 passed in 2.03s`

## 2026-04-08 第十批任务已开出

本轮先不继续扩 live 主线写口，而是继续把“外部客户端如何稳定读取 latest/template/descriptor”这件事拆成第十批并行任务。

- A-P16：
  - execution bridge health client template / latest helper
  - 目标是让 Windows Execution Gateway 直接抄模板上报，也让 Linux 主控直接按推荐键读取 latest
- B-P16：
  - offline self-improvement latest descriptor / archive ref helper
  - 目标是让 Linux/OpenClaw 或离线调度同时拿到 serving 与 archive 引用，不再自己拼双路径
- C-P16：
  - OpenClaw replay/proposal latest descriptor helper
  - 目标是让 Linux/OpenClaw 直接基于 descriptor 做 latest 拉取、归档与 review 引用

第十批仍坚持同一条硬边界：

- Linux/OpenClaw 负责研究、编排、归档、review、latest serving
- Windows Execution Gateway 负责唯一执行写口
- 所有 replay / proposal / self-improvement 产物继续只服务离线研究，不进入 live 自动执行链

## 2026-04-08 第十批 helper 已接入 Main 只读主线

- 更新文件：
  - `src/ashare_system/apps/system_api.py`
  - `tests/test_system_governance.py`
- 本轮落地内容：
  - 新增 `GET /system/monitor/execution-bridge-health/template`
    - 直接返回 `build_execution_bridge_health_client_template(...)`
    - 同时透出 `get_execution_bridge_health_latest_descriptor()`
  - `GET /system/reports/offline-self-improvement`
    - 现在直接带 `archive_ready_manifest`
    - 现在直接带 `latest_descriptor`
    - 现在直接带 `archive_ref`
    - 对旧存量 export，如果没有 `latest_descriptor`，Main 会在读取时自动补齐
  - 新增 `GET /system/reports/offline-self-improvement-descriptor`
    - 给 Linux/OpenClaw 直接拿 latest descriptor，不必整包拉 export
  - 新增 `GET /system/reports/openclaw-replay-packet`
  - 新增 `GET /system/reports/openclaw-proposal-packet`
    - 直接返回 latest packet 与 `latest_descriptor`
    - 对旧存量 packet，如果缺 `latest_descriptor`，Main 会按现有 helper 自动补齐
  - `postclose-master` 已新增：
    - `latest_offline_self_improvement_descriptor`
    - `latest_openclaw_replay_descriptor`
    - `latest_openclaw_proposal_descriptor`
- 语义边界未变：
  - execution bridge template 只服务 Windows Gateway 上报契约
  - replay / proposal / self-improvement 继续只服务离线研究
  - 不直接触发 live
  - 不越过 Windows Execution Gateway 的唯一执行写口
- 回归：
  - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_execution_bridge_health_template_endpoint_exposes_client_contract tests/test_system_governance.py::SystemGovernanceTests::test_execution_bridge_health_ingress_endpoint_persists_linux_windows_bridge_snapshot tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_replay_packet_endpoint_builds_offline_packet_without_writeback tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_packet_archive_endpoints_persist_latest_packets tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_proposal_packet_endpoint_builds_research_only_packet tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_persist_endpoint_accepts_runner_result_payload -q`
  - `7 passed in 2.13s`

## 2026-04-08 第十一批主线已补 serving latest index / deployment bootstrap contracts

- 更新文件：
  - `src/ashare_system/apps/system_api.py`
  - `tests/test_system_governance.py`
- 本轮落地内容：
  - 新增 `GET /system/reports/serving-latest-index`
    - 聚合 execution bridge health template/latest descriptor
    - 聚合 offline self-improvement latest descriptor/archive ref
    - 聚合 OpenClaw replay/proposal latest descriptor
  - 新增 `GET /system/deployment/bootstrap-contracts`
    - 聚合 readiness
    - 聚合 execution bridge template
    - 聚合 serving latest index
    - 聚合 Linux/OpenClaw 与 Windows Gateway 的部署边界、只读 report paths
  - 这两个聚合口都是只读面，不新增 live 写口
- 语义边界未变：
  - Linux/OpenClaw 负责研究、编排、归档、review、latest serving
  - Windows Execution Gateway 负责唯一执行写口
  - replay / proposal / self-improvement 继续只服务离线研究，不自动进入 live
- 回归：
  - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_deployment_bootstrap_contracts_aggregate_readiness_and_templates tests/test_system_governance.py::SystemGovernanceTests::test_serving_latest_index_aggregates_latest_descriptors tests/test_system_governance.py::SystemGovernanceTests::test_execution_bridge_health_template_endpoint_exposes_client_contract tests/test_system_governance.py::SystemGovernanceTests::test_execution_bridge_health_ingress_endpoint_persists_linux_windows_bridge_snapshot tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_packet_archive_endpoints_persist_latest_packets tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_persist_endpoint_accepts_runner_result_payload -q`
  - `7 passed in 2.13s`

## 2026-04-08 第十二批主线已补 postclose deployment handoff

- 更新文件：
  - `src/ashare_system/apps/system_api.py`
  - `tests/test_system_governance.py`
- 本轮落地内容：
  - 新增 `GET /system/reports/postclose-deployment-handoff`
  - 内部把 `postclose-master` 组装逻辑收口为可复用 helper
  - handoff bundle 一次聚合：
    - review-board
    - postclose-master
    - serving-latest-index
    - deployment-bootstrap-contracts
  - 用途收口：
    - Linux/OpenClaw 盘后交接时一次拿全研究总览、latest descriptor、bootstrap contract
    - 不再需要客户端依次拉多个 report 再自行拼包
- 语义边界未变：
  - handoff bundle 仍然是只读聚合
  - 不新增 live 写口
  - 不越过 Windows Execution Gateway 的唯一执行写口
- 回归：
  - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_postclose_deployment_handoff_aggregates_review_postclose_and_bootstrap tests/test_system_governance.py::SystemGovernanceTests::test_serving_latest_index_aggregates_latest_descriptors tests/test_system_governance.py::SystemGovernanceTests::test_deployment_bootstrap_contracts_aggregate_readiness_and_templates tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_packet_archive_endpoints_persist_latest_packets tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_persist_endpoint_accepts_runner_result_payload -q`
  - `5 passed in 2.33s`

## 2026-04-08 第十三批主线已补 Linux control plane startup checklist

- 更新文件：
  - `src/ashare_system/apps/system_api.py`
  - `tests/test_system_governance.py`
- 本轮落地内容：
  - 新增 `GET /system/deployment/linux-control-plane-startup-checklist`
  - 聚合并压缩：
    - readiness
    - execution bridge contract/template
    - offline self-improvement descriptor
    - OpenClaw replay/proposal descriptor
    - postclose deployment handoff
  - 返回统一的：
    - `status`
    - `checks`
    - `summary_lines`
  - 用于 Linux/OpenClaw 启动与切日检查，一屏确认控制面是否具备最小就绪条件
- 语义边界未变：
  - checklist 仍是只读聚合
  - 不新增 live 写口
  - 不越过 Windows Execution Gateway 的唯一执行写口
- 回归：
  - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_linux_control_plane_startup_checklist_summarizes_bootstrap_and_latest_state tests/test_system_governance.py::SystemGovernanceTests::test_postclose_deployment_handoff_aggregates_review_postclose_and_bootstrap tests/test_system_governance.py::SystemGovernanceTests::test_deployment_bootstrap_contracts_aggregate_readiness_and_templates tests/test_system_governance.py::SystemGovernanceTests::test_serving_latest_index_aggregates_latest_descriptors tests/test_system_governance.py::SystemGovernanceTests::test_execution_bridge_health_template_endpoint_exposes_client_contract -q`
  - `5 passed in 2.16s`

## 2026-04-08 第十四批主线已补 Windows Execution Gateway onboarding bundle

- 更新文件：
  - `src/ashare_system/apps/system_api.py`
  - `tests/test_system_governance.py`
- 本轮落地内容：
  - 新增 `GET /system/deployment/windows-execution-gateway-onboarding-bundle`
  - 聚合：
    - execution bridge template
    - source value suggestions
    - latest read descriptor
    - Linux control plane 的 startup checklist / bootstrap / handoff / serving latest index 路径
  - 用途收口：
    - Windows Execution Gateway 侧客户端一次拿全 execution bridge 上报模板与 Linux 只读入口
    - 不需要自己再拼 body 字段规则或 Linux 侧 report path
- 语义边界未变：
  - onboarding bundle 仍然是只读契约
  - 不新增 live 写口
  - 不越过 Windows Execution Gateway 的唯一执行写口
- 回归：
  - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_windows_execution_gateway_onboarding_bundle_exposes_template_and_linux_paths tests/test_system_governance.py::SystemGovernanceTests::test_linux_control_plane_startup_checklist_summarizes_bootstrap_and_latest_state tests/test_system_governance.py::SystemGovernanceTests::test_postclose_deployment_handoff_aggregates_review_postclose_and_bootstrap tests/test_system_governance.py::SystemGovernanceTests::test_deployment_bootstrap_contracts_aggregate_readiness_and_templates tests/test_system_governance.py::SystemGovernanceTests::test_execution_bridge_health_template_endpoint_exposes_client_contract -q`
  - `5 passed in 2.15s`

## 2026-04-08 第十五批任务已开出

本轮继续不扩 live 写口，重点转到“部署侧 contract 文档化 + 切日交接摘要”的并行收口。

- A-P17：
  - execution bridge deployment contract sample
  - 目标是让 Windows Gateway 直接抄 execution bridge 请求与读取示例，不再自己拼 body/字段
- B-P17：
  - offline self-improvement descriptor contract sample
  - 目标是让 Linux/OpenClaw 直接照抄 latest descriptor / archive ref 的消费样例
- C-P17：
  - OpenClaw packet descriptor contract sample
  - 目标是让 OpenClaw 侧直接照抄 replay/proposal latest descriptor、archive manifest 的消费样例

第十五批仍坚持同一条硬边界：

- Linux/OpenClaw 负责研究、编排、归档、review、latest serving
- Windows Execution Gateway 负责唯一执行写口
- replay / proposal / self-improvement 继续只服务离线研究，不自动进入 live

## 2026-04-08 第十五批主线已接入 contract sample 只读链

- 更新文件：
  - `src/ashare_system/apps/system_api.py`
  - `tests/test_system_governance.py`
  - `discuss/quant-full-v1-execution-taskboard.md`
  - `discuss/执行记录-codex.md`
- 本轮落地内容：
  - execution bridge：
    - `GET /system/monitor/execution-bridge-health/template`
    - `GET /system/deployment/bootstrap-contracts`
    - `GET /system/deployment/windows-execution-gateway-onboarding-bundle`
    已接入 `build_execution_bridge_health_deployment_contract_sample(...)` 的真实返回结果
  - offline self-improvement：
    - Main 在 `_load_latest_offline_self_improvement()` 读链里自动补齐 `descriptor_contract_sample`
    - `GET /system/reports/offline-self-improvement`
    - `GET /system/reports/offline-self-improvement-descriptor`
    - `GET /system/reports/serving-latest-index`
    - `GET /system/reports/postclose-master`
    - `GET /system/reports/postclose-deployment-handoff`
    现在都能稳定透出 descriptor contract sample
  - OpenClaw replay/proposal packet：
    - Main 在 `_load_latest_openclaw_packet(...)` 读链里自动补齐 `contract_sample`
    - `GET /system/reports/openclaw-replay-packet`
    - `GET /system/reports/openclaw-proposal-packet`
    - `GET /system/reports/serving-latest-index`
    - `GET /system/reports/postclose-master`
    - `GET /system/reports/postclose-deployment-handoff`
    现在都能稳定透出 packet contract sample
- 当前取舍：
  - 没有新增第二套读取链路，而是在现有 `latest_descriptor` 自动补齐点上补 sample
  - `postclose_master` 与 `postclose_deployment_handoff` 通过已有聚合链自然带出 sample，不再额外手写平行聚合逻辑
  - 语义边界不变：
    - `offline_only` / `live_trigger` / `live_execution_allowed` guardrail 继续保留
    - 不新增 live 写口
    - 不越过 Windows Execution Gateway 的唯一执行写口
- 回归：
  - `PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp PYTHONPATH=src pytest -p no:cacheprovider tests/test_system_governance.py::SystemGovernanceTests::test_execution_bridge_health_template_endpoint_exposes_client_contract tests/test_system_governance.py::SystemGovernanceTests::test_deployment_bootstrap_contracts_aggregate_readiness_and_templates tests/test_system_governance.py::SystemGovernanceTests::test_windows_execution_gateway_onboarding_bundle_exposes_template_and_linux_paths tests/test_system_governance.py::SystemGovernanceTests::test_serving_latest_index_aggregates_latest_descriptors tests/test_system_governance.py::SystemGovernanceTests::test_postclose_deployment_handoff_aggregates_review_postclose_and_bootstrap tests/test_system_governance.py::SystemGovernanceTests::test_offline_self_improvement_report_endpoint_reads_latest_export -q`
  - `6 passed in 2.25s`

## 2026-04-08 第九批任务已开出

本轮没有再扩主线写口，而是把下一批并行任务正式收口成“外部客户端可稳定调用的契约补齐”。

- A-P15：
  - execution bridge ingress payload helper
  - 目标是让 Windows Execution Gateway 直接按固定 helper/契约构造上报包
- B-P15：
  - offline self-improvement archive manifest/helper
  - 目标是让 Linux 主控或离线调度不再手写 archive 元数据
- C-P15：
  - OpenClaw replay/proposal packet archive manifest/helper
  - 目标是让 OpenClaw 后续可直接归档 replay/proposal 双轨产物

当前这批仍然坚持同一条硬边界：

- Linux 主控负责研究、编排、归档、review、latest serving
- Windows Execution Gateway 负责唯一执行写口
- 所有 self-improvement / replay / proposal 产物继续只服务离线研究，不进入 live 自动执行链
- `exit_reason` 当前主要来自 settlement 输入，后续再接 execution reconciliation 和真实卖出链路。

### 验证目标

- settlement 返回 attribution 报告
- `/system/learning/attribution` 能按 `playbook / regime / exit_reason` 查询
- `/system/learning/trade-review` 能给出最小复盘摘要

## 2026-04-06 M6 execution reconciliation -> attribution 回写

本轮继续把 M6 往真实执行侧推进，不再只依赖 settlement 输入，而是把执行对账结果直接回写进 attribution。

- 更新文件：
  - [execution_reconciliation.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/execution_reconciliation.py)
  - [attribution.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/learning/attribution.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)

### 本轮落地内容

- `execution_reconciliation` 的 item 现在会带出：
  - `side`
  - `submitted_at`
- `POST /system/execution-reconciliation/run`
  - 现在会把已成交订单同步回写到 attribution
  - 回写字段包括：
    - `order_id`
    - `side`
    - `filled_quantity`
    - `filled_value`
    - `avg_fill_price`
    - `submitted_at`
    - `reconciled_at`
    - `holding_days`
    - `source=execution_reconciliation`
- 对于 BUY 成交：
  - 若当前持仓仍存在，会用持仓 `last_price` 和成交均价估算最小浮盈亏
  - `exit_reason` 默认记为 `open_position`

### 当前取舍

- 这轮先解决“真实成交回写”。
- 真实卖出后的退出原因仍未完全自动化，因为当前 order journal 没有统一的 `exit_reason` 元数据来源。
- 后续应继续接：
  - `execution_api` 手动卖单 journal
  - `exit_engine` 输出的退出原因归档

### 验证目标

- `/system/execution-reconciliation/run` 返回 attribution 更新结果
- `/system/execution-reconciliation/latest` 保留 attribution 回写信息
- `/system/learning/attribution` 能读到 `source=execution_reconciliation` 的真实成交记录

## 2026-04-06 M6 手动 SELL 元数据贯通

本轮继续把 M6 从“能回写真实成交”推进到“手动 SELL 的退出原因也能自动进 attribution”。

- 更新文件：
  - [contracts.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/contracts.py)
  - [execution_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/execution_api.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)

### 本轮落地内容

- `PlaceOrderRequest` 新增可选字段：
  - `trade_date`
  - `playbook`
  - `regime`
  - `exit_reason`
- `POST /execution/orders`
  - 现在会把下单元数据写入 `execution_order_journal`
  - SELL 单的 `exit_reason / playbook / regime / trade_date` 会被原样落盘
- `execution_reconciliation -> attribution`
  - 现在会优先读取 journal/request 中的：
    - `playbook`
    - `regime`
    - `exit_reason`
  - 因而手动 SELL 成交后，不需要再额外走 settlement，也能进入 attribution

### 当前取舍

- 这轮先解决“手动 SELL 元数据贯通”。
- 自动卖出链路还没有统一写 journal 元数据，因此 `exit_engine` 的原因尚未全自动沉淀到 attribution。

### 验证目标

- `test_phase1.py` 已验证 SELL 下单会写入 journal 元数据
- `test_system_governance.py` 已验证 SELL 成交对账后 attribution 会保留 `exit_reason / playbook / regime`

## 2026-04-06 M6 tail_market 自动 SELL 最小闭环

本轮继续把 M6 从“手动 SELL 元数据贯通”推进到“调度器自动 SELL 也能沉淀 attribution”。

- 更新文件：
  - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
  - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- `scheduler.task_tail_market` 不再只是审计占位：
  - 已接入真实持仓扫描
  - 会读取 `runtime_context` 的：
    - `market_profile`
    - `sector_profiles`
    - `playbook_contexts`
  - 若上下文缺失，会回退到 `SellDecisionEngine` 的 ATR / time-stop 规则
- 自动卖出下单已统一写入 `execution_order_journal`
  - 落字段：
    - `trade_date`
    - `playbook`
    - `regime`
    - `exit_reason`
    - `request`
    - `submitted_at`
    - `source=scheduler_tail_market`
- 报单策略做了安全分层：
  - `paper + mock`：允许真实 mock 报单，便于无人值守演练和测试
  - `dry-run`：仅预演，不报单
  - `live`：仅在 `live_trade_enabled=true` 时真实报单
- 自动 SELL 成交后，`/system/execution-reconciliation/run` 已能把 journal 元数据回写到 attribution

### 当前取舍

- 这轮先做“最小自动卖出闭环”，不另起第二套持仓状态机。
- 当前退出上下文主要来源于：
  - `runtime_context.playbook_contexts`
  - `sector_profiles`
  - 已有 BUY journal
  - 持仓成本价 / 最新价
- 对“股性、板块联动、盘中强弱”的利用还比较轻，下一轮应继续增强：
  - 入场时间与持仓阶段识别
  - 个股股性驱动的退出参数
  - 板块退潮与龙头掉队联动

### 验证结果

- 精准新增用例：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_phase1.py::TestScheduler::test_tail_market_scan_submits_sell_and_persists_metadata tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_auto_sell_reconciliation_backfills_exit_reason_and_playbook`
  - 结果：`2 passed`
- 治理全量回归：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_system_governance.py`
  - 结果：`52 passed`

### 当前结论

- `tail_market -> 自动 SELL -> order journal -> execution_reconciliation -> attribution` 已形成最小闭环。
- 下一步不该再停留在“有没有记录退出原因”，而要继续往“退出质量是否真的更像交易员判断”推进。

## 2026-04-07 M6 BUY 元数据贯通 + tail_market 观测接口

本轮继续推进 M6，不只补卖出链路，而是把买入上下文也补进 execution 链，并给 `tail_market` 增加直接可查的接口。

- 更新文件：
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- `execution_precheck` / `execution_intents`
  - 现在会解析最新 `runtime_context + dossier_pack`
  - 对每个执行候选补出：
    - `playbook`
    - `regime`
    - `resolved_sector`
    - `playbook_entry_window`
- `execution_intents[*].request`
  - 现在会显式带上：
    - `trade_date`
    - `playbook`
    - `regime`
- `execution_dispatch -> execution_order_journal`
  - BUY 派发成功后，journal 会把这些字段沉淀到顶层：
    - `side`
    - `playbook`
    - `regime`
    - `trade_date`
  - 这样后续 `tail_market` 在读取持仓历史时，不需要只依赖 SELL 补录或手工元数据
- 新增 `tail_market` 观测接口：
  - `POST /system/tail-market/run`
  - `GET /system/tail-market/latest`
  - `GET /system/tail-market/history`

### 当前取舍

- 这轮优先解决“买入后上下文会不会在执行链里丢失”。
- 目前 `playbook/regime` 的来源仍优先是：
  - `runtime_context.playbook_contexts`
  - `dossier_pack.assigned_playbook`
  - `market_profile.regime`
- 个股股性画像和板块实时退潮，还没有直接进 execution precheck / tail_market 的动态退出判断。

### 验证结果

- 精准新增回归：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_system_governance.py -k 'execution_intent_dispatch_apply_submits_mock_orders or tail_market_endpoints_expose_latest_and_history or prepare_finalized_discussion'`
  - 结果：`2 passed`
- 治理全量回归：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_system_governance.py`
  - 结果：`53 passed`
- phase1 相关回归：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_phase1.py -k 'tail_market_scan_submits_sell_and_persists_metadata or execution_api_sell_order_persists_exit_metadata'`
  - 结果：`2 passed`

### 当前结论

- 现在不只是 SELL 会带 `playbook/regime`，BUY 的 execution request 和 journal 也开始沉淀这些字段。
- `tail_market` 已从“只有后台逻辑”升级到“可手动触发、可看 latest、可看 history”的可观测状态。
- 下一步应该继续把这些上下文从“能带过去”升级到“真的参与更像交易员的退出判断”。

## 2026-04-07 M6 tail_market 股性感知退出

本轮继续推进 M6，重点不再是补元数据，而是让 `tail_market` 真正开始消费个股股性和 dossier 相对强弱信息。

- 更新文件：
  - [contracts.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/contracts.py)
  - [exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/exit_engine.py)
  - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
  - [test_phase_exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_exit_engine.py)
  - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- `ExitContext`
  - 新增：
    - `holding_days`
    - `optimal_hold_days`
    - `style_tag`
    - `avg_sector_rank_30d`
    - `leader_frequency_30d`
- `tail_market`
  - 现在除了读 `runtime_context / order_journal`，还会读 serving 层最新 `dossier_pack`
  - 优先复用：
    - `symbol_context.market_relative.relative_strength_pct`
    - `symbol_context.behavior_profile`
  - 扫描结果 item 会直接带出：
    - `relative_strength_5m`
    - `behavior_profile`
- `ExitEngine`
  - 新增最小股性感知时间退出规则：
    - `leader` 或高龙头频率标的，在达到最优持有窗口后若相对强弱转弱，提前退出
    - `defensive / mixed` 保持更高容忍度，不触发同样的早退
  - 兼容同日买入场景：
    - 当 `optimal_hold_days=1` 时，只要持有分钟数够长，也允许触发 leader 早退

### 当前取舍

- 这轮先不强行把 `StockProfileBuilder` 接成完整日更产线，而是先把消费端打通。
- 行为画像来源优先级目前是：
  - `runtime_context.behavior_profiles`
  - `dossier_pack`
  - `journal request`
- 退出原因仍沿用现有最小枚举，股性导致的快撤先归到 `time_stop`，后面再考虑拆出更细粒度 reason。

### 验证结果

- 行为感知最小回归：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_phase_exit_engine.py tests/test_phase1.py -k 'behavior or tail_market_scan_reads_behavior_profile_from_dossier'`
  - 结果：`2 passed`
- `tail_market` 与归因链路回归：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_system_governance.py -k 'tail_market_auto_sell_reconciliation_backfills_exit_reason_and_playbook or tail_market_endpoints_expose_latest_and_history'`
  - 结果：`2 passed`

### 当前结论

- `tail_market` 现在不再只是“按 playbook 参数 + ATR 回退”，已经开始读 dossier 的股性和相对强弱。
- leader 风格标的能更早体现“弱了就撤”，而 defensive 标的不会被同一阈值过早扫掉。
- 下一步应继续把 `StockBehaviorProfile` 的生成与 runtime/dossier 持久化前移，避免现在仍依赖测试或外部补入画像。

## 2026-04-07 M6 behavior_profile 前移到 runtime/dossier 主链

本轮继续把上一轮的股性感知退出往前推，不再只在 `tail_market` 消费端补画像，而是把最小 `behavior_profiles` 直接沉淀到 runtime 和 dossier 主链。

- 更新文件：
  - [runtime_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
  - [strategy_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/strategy_api.py)
  - [test_phase_strategy_runtime_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_strategy_runtime_api.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- `runtime/jobs/pipeline`
  - 在 `market_profile + sector_profiles` 推断完成后，新增一版轻量 `behavior_profiles` 推断：
    - 输入来自现有 `selection_score / sector_rank / sector life cycle / market_relative / event_count`
    - 输出仍复用统一 `StockBehaviorProfile` 契约
- 这批画像现在会同步写入：
  - `runtime_context.behavior_profiles`
  - `dossier_pack.behavior_profiles`
  - 单票 dossier 的：
    - `behavior_profile`
    - `symbol_context.behavior_profile`
    - `playbook_context.behavior_profile`
- `StrategyRouter`
  - 已开始消费 `behavior_profiles + leader_ranks`
  - playbook 的 `confidence / leader_score / style_tag` 不再完全脱离股性画像
- `strategy/context/latest` 与 `strategy/candidates/latest`
  - 已对外暴露：
    - `behavior_profiles`
    - `style_tag`
    - `optimal_hold_days`
    - `leader_frequency_30d`
    - `avg_sector_rank_30d`

### 当前取舍

- 这轮的画像仍是主链内的轻量启发式推断，不等同于 `StockProfileBuilder` 的真实 20/30/60 日统计结果。
- 这样做的目的，是先让：
  - 路由
  - dossier
  - strategy API
  - tail_market
  使用同一份画像对象，而不是各自猜一份。
- 下一轮再考虑把 `StockProfileBuilder` 接成真实历史产线，替换这版启发式画像。

### 验证结果

- runtime/strategy + tail_market 回归：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_phase_strategy_runtime_api.py tests/test_phase1.py::TestTailMarketBehaviorAwareExit::test_tail_market_scan_reads_behavior_profile_from_dossier`
  - 结果：`4 passed`
- 最终 focused regression：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py::TestScheduler::test_tail_market_scan_submits_sell_and_persists_metadata tests/test_phase1.py::TestTailMarketBehaviorAwareExit::test_tail_market_scan_reads_behavior_profile_from_dossier tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_auto_sell_reconciliation_backfills_exit_reason_and_playbook tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_endpoints_expose_latest_and_history`
  - 结果：`13 passed`

### 当前结论

- 现在 `StockBehaviorProfile` 已经不只是退出链路的“外部补丁”，而是 runtime/dossier/strategy/tail_market 共用的一层对象。
- 虽然这版仍是启发式画像，但系统内部开始真正共享“同一票到底偏 leader、偏 defensive、应该拿几天”的判断基线。
- 下一步最有价值的工作，是把这层对象从启发式推断升级成真实历史统计，并引入独立缓存或日更任务。

## 2026-04-07 M6 precompute 多日 bars -> 历史画像

本轮继续把 `behavior_profile` 往真实历史侧推进，不再只靠 runtime 当下快照推断，而是让 dossier 预计算先吃多根日线。

- 更新文件：
  - [market_adapter.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/infra/market_adapter.py)
  - [fetcher.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/data/fetcher.py)
  - [kline.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/data/kline.py)
  - [precompute.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/precompute.py)
  - [runtime_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
  - [test_precompute_contexts.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_precompute_contexts.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- 数据层
  - `market_adapter.get_bars()` 新增 `count`
  - `DataFetcher.fetch_bars()` / `DataPipeline.get_daily_bars()` / `KlineManager` 同步支持多根 bar
  - mock 行情下也能返回多日 bar，便于本地回归
- dossier 预计算
  - `precompute` 现在会先拉近 60 根日线
  - 基于这些 bars 构造 `history_rows`
  - 调用 `StockProfileBuilder` 生成一版更接近历史统计的 `behavior_profile`
  - 结果写入：
    - `pack.behavior_profiles`
    - `item.behavior_profile`
    - `symbol_context.behavior_profile`
- runtime 链路
  - `runtime_api` 生成 playbook 前，若 dossier 已经带画像，则优先直接复用
  - 只有缺画像时，才回退到上一轮的 runtime 启发式推断

### 当前取舍

- 这轮已经把画像从“只看当下”推进到“至少看多日 bars”，但仍不是完整的短线事实数据库。
- 当前通过日线可比较可靠得到：
  - 持有期收益
  - 相对强弱
  - 部分 leader 倾向
- 仍然比较弱的字段：
  - 真正的炸板
  - 回封
  - 分时弱转强
  这些还需要分时或事件级补数。

### 验证结果

- precompute/runtime/tail_market 回归：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase1.py::TestTailMarketBehaviorAwareExit::test_tail_market_scan_reads_behavior_profile_from_dossier`
  - 结果：`5 passed`
- 最终 focused regression：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py::TestScheduler::test_tail_market_scan_submits_sell_and_persists_metadata tests/test_phase1.py::TestTailMarketBehaviorAwareExit::test_tail_market_scan_reads_behavior_profile_from_dossier tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_auto_sell_reconciliation_backfills_exit_reason_and_playbook tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_endpoints_expose_latest_and_history`
  - 结果：`14 passed`

### 当前结论

- `behavior_profile` 现在已经不是纯 runtime 启发式对象，开始具备多日历史基础。
- 系统主链的优先级已经变成：
  - precompute 历史画像
  - runtime 回退画像
  - 执行与退出消费
- 下一步如果继续提高退出质量，最值得做的是把分时事实补进 `StockProfileBuilder`，让“炸板 / 回封 / 失败即撤”不再靠近似值。

## 2026-04-07 M6 历史画像事实细化

本轮继续细化 precompute 历史画像的事实来源，目标是不再把多日 bars 仅当成普通涨跌幅序列，而是尽量从日线 OHLC 里提取“摸板 / 炸板 / 回封”近似事实。

- 更新文件：
  - [market_adapter.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/infra/market_adapter.py)
  - [precompute.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/precompute.py)
  - [test_precompute_contexts.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_precompute_contexts.py)

### 本轮落地内容

- mock 多日 bars 不再是平滑直线：
  - 周期性生成封板日
  - 周期性生成摸板炸开日
  - 周期性生成回落后再封日
- `precompute._build_behavior_profiles`
  - 现在会结合：
    - `high`
    - `low`
    - `close`
    - `pre_close`
    - 涨跌停幅度
  - 近似推断：
    - `is_zt`
    - `seal_success`
    - `bombed`
    - `afternoon_resealed`
  - 然后再交给 `StockProfileBuilder`
- 这样产生的 `behavior_profile`
  - `board_success_rate_20d`
  - `bomb_rate_20d`
  - `reseal_rate_20d`
  不再长期接近 0，而是开始反映“这票历史上更容易封住还是炸开”

### 验证结果

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase1.py::TestTailMarketBehaviorAwareExit::test_tail_market_scan_reads_behavior_profile_from_dossier`
  - 结果：`5 passed`
- 最终 focused regression：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py::TestScheduler::test_tail_market_scan_submits_sell_and_persists_metadata tests/test_phase1.py::TestTailMarketBehaviorAwareExit::test_tail_market_scan_reads_behavior_profile_from_dossier tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_auto_sell_reconciliation_backfills_exit_reason_and_playbook tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_endpoints_expose_latest_and_history`
  - 结果：`14 passed`

### 当前结论

- 历史画像这层已经从“只有持有期收益和强弱”推进到“开始区分封板成功、炸板、回封”。
- 虽然仍是日线近似，不是完整分时事实，但已经能明显提升 `leader / reseal / defensive` 的辨识度。
- 下一步最值得继续的是：
  - 接 5m / 1m bars 或 monitor 事件
  - 把开仓后 5-30 分钟的弱转强、炸板修复、冲高回落补进画像和退出规则

## 2026-04-07 M6 tail_market 接入分时快撤信号

本轮继续把退出判断往盘感层推进，重点是让 `tail_market` 不只看日线画像和静态 playbook 参数，而是能读一点真实分时弱化信号。

- 更新文件：
  - [contracts.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/contracts.py)
  - [exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/exit_engine.py)
  - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
  - [test_phase_exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_exit_engine.py)
  - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- `ExitContext` 新增：
  - `intraday_change_pct`
  - `intraday_drawdown_pct`
  - `rebound_from_low_pct`
  - `negative_alert_count`
- `tail_market`
  - 会额外读取：
    - `market.get_bars(symbols, "5m", count=12)`
    - serving 层 `latest_monitor_context.recent_events`
  - 对每个持仓计算：
    - 日内涨跌幅
    - 盘中最高点回撤幅度
    - 低点反抽幅度
    - 负面预警次数
  - 这些指标会直接落到扫描结果 item
- `ExitEngine`
  - 新增最小分时快撤规则：
    - `leader` 风格标的，若持有已超过 30 分钟，且出现“冲高回落幅度大 + 低点反抽弱”，提前退出
    - 若 monitor 已有负面预警，且日内涨幅转负，也提前退出

### 当前取舍

- 这轮只接最短链路：
  - `5m bars`
  - `monitor recent_events`
- 还没有做：
  - 板块内相对强弱的实时 5m 对比
  - 1m 级炸板修复跟踪
  - 分时量比/VWAP 结构
- 退出 reason 仍先复用 `time_stop`，因为这批规则本质上属于“弱了就走”的提前时间退出。

### 验证结果

- 精准回归：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_phase_exit_engine.py tests/test_phase1.py -k 'intraday_fade or intraday_fade_and_monitor_alerts'`
  - 结果：`2 passed`
- 最终 focused regression：
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -s -p no:cacheprovider tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py::TestScheduler::test_tail_market_scan_submits_sell_and_persists_metadata tests/test_phase1.py::TestTailMarketBehaviorAwareExit::test_tail_market_scan_reads_behavior_profile_from_dossier tests/test_phase1.py::TestTailMarketBehaviorAwareExit::test_tail_market_scan_uses_intraday_fade_and_monitor_alerts tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_auto_sell_reconciliation_backfills_exit_reason_and_playbook tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_endpoints_expose_latest_and_history`
  - 结果：`16 passed`

### 当前结论

- 现在 `tail_market` 已经开始具备一点“盘中冲高没封住、回落也修不回来，就先走”的短线语义。
- 这比单纯 ATR 或日终 time-stop 更接近你要的快进快出判断，但还只是第一层。
- 下一步最有价值的是把“个股分时强弱”升级成“相对所属板块/龙头的分时强弱”，这样热门板块联动才真正能进退出判断。

## 2026-04-07 M6 tail_market 接入板块相对强弱快撤

本轮继续推进退出判断，把 `tail_market` 从“只看个股自身分时弱化”推进到“同时看它是否已经明显弱于板块同行”。

- 更新文件：
  - [contracts.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/contracts.py)
  - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
  - [exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/exit_engine.py)
  - [test_phase_exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_exit_engine.py)
  - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- `ExitContext` 新增：
  - `sector_intraday_change_pct`
  - `sector_relative_strength_5m`
- `tail_market`
  - 基于 `runtime_context.playbook_contexts + dossier_pack` 建同板块同行映射
  - 拉取持仓及同行 `5m bars`
  - 为持仓计算：
    - 板块同行平均分时涨跌幅
    - 个股相对板块的分时强弱差
  - 扫描结果 item 会直接带出这两个字段
- `ExitEngine`
  - 对 leader 风格标的新增最小板块掉队退出规则：
    - 持有超过 30 分钟
    - 非涨停锁死
    - `sector_relative_strength_5m` 明显落后同行
    - 则提前触发 `time_stop`

### 当前取舍

- 这轮先用“同行平均涨跌幅”近似板块同步强弱，不单独引入新的板块分时缓存。
- 退出 reason 仍沿用 `time_stop`，先解决判断质量，不先扩 reason taxonomy。
- 同行样本当前主要依赖：
  - `runtime_context.playbook_contexts`
  - `dossier_pack.resolved_sector`

### 验证结果

- 精准新增回归：
  - `PYTHONPATH=src pytest tests/test_phase_exit_engine.py tests/test_phase1.py -k 'tail_market or sector_relative or intraday_fade or behavior_profile_accelerates'`
  - 结果：`7 passed`
- focused regression：
  - `PYTHONPATH=src pytest tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py tests/test_system_governance.py`
  - 结果：`113 passed`

### 当前结论

- `tail_market` 已从“只看这只票自己弱不弱”推进到“会看它是否已经掉队于本板块同行”。
- 这让退出判断开始具备一点“热门板块内部谁先掉队谁先撤”的交易员视角，不再只是单票回落或静态 time-stop。
- 下一步如果继续提升快进快出能力，最值得补的是：
  - 连续时序的板块相对强弱
  - 1m/5m 微结构
  - 把这些退出事实继续回灌到 attribution / review，形成可校正参数的闭环

## 2026-04-07 M6 tail_market 接入连续掉队快撤

本轮继续推进退出判断，把板块联动从“某一时点是否弱于同行”推进到“最近几根 5 分钟是否持续在掉队”。

- 更新文件：
  - [contracts.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/contracts.py)
  - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
  - [exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/exit_engine.py)
  - [test_phase_exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_exit_engine.py)
  - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- `ExitContext` 新增：
  - `sector_relative_trend_5m`
  - `sector_underperform_bars_5m`
- `tail_market`
  - 对每个标的的 `5m bars` 生成逐 bar 收益序列
  - 对齐同板块同行的同时间 bar
  - 计算：
    - 最近 3 根 `5m` 的相对板块累计弱势
    - 最近 3 根中有多少根弱于同行均值
  - 这两个字段会直接落到扫描结果 item
- `ExitEngine`
  - 对 leader 风格标的新增时序弱化退出规则：
    - 持有超过 30 分钟
    - 非涨停锁死
    - `sector_underperform_bars_5m >= 2`
    - `sector_relative_trend_5m <= -0.01`
    - 则提前触发 `time_stop`

### 当前取舍

- 这轮只看最近 3 根 `5m`，先解决“连续掉队”最小识别，不先扩成完整分时状态机。
- 逐 bar 对齐当前按 `trade_time` 做最小时间同步，暂不处理更复杂的缺 bar / 停牌插值。
- 退出 reason 继续沿用 `time_stop`，避免现在就扩展 reason taxonomy。

### 验证结果

- 精准新增回归：
  - `PYTHONPATH=src pytest tests/test_phase_exit_engine.py tests/test_phase1.py -k 'sector_relative or intraday_fade or behavior_profile_accelerates'`
  - 结果：`7 passed`
- focused regression：
  - `PYTHONPATH=src pytest tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py tests/test_system_governance.py`
  - 结果：`115 passed`

### 当前结论

- `tail_market` 现在不只会判断“当前已经弱于板块同行”，还会判断“最近几根 5 分钟是不是持续在掉队”。
- 这比单点对比更接近真实盘中快撤，因为很多票不是一下子转弱，而是连续几根小幅落后后才开始失去地位。
- 下一步如果继续提升判断质量，最值得补的是：
  - 更长序列的板块强弱切换
  - 1m 微结构
  - 把退出时的这些事实继续写进 attribution / trade review

## 2026-04-07 M6 tail_market 退出事实回灌学习链

本轮继续推进 M6，不再让快撤规则只在当下生效，而是把退出时的关键事实写进学习链路，供后续 attribution / trade review 复盘。

- 更新文件：
  - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
  - [attribution.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/learning/attribution.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- `tail_market`
  - 自动 SELL 下单写 journal 时，会额外落盘：
    - `exit_context_snapshot`
    - `review_tags`
  - 当前会沉淀的事实包括：
    - `intraday_fade`
    - `negative_alert`
    - `sector_relative_weak`
    - `sector_relative_trend_weak`
    - `sector_retreat`
    - `leader_style`
- `execution_reconciliation -> attribution`
  - 对账回写归因时，会把 journal 里的：
    - `exit_context_snapshot`
    - `review_tags`
    一起写入 `TradeAttributionRecord`
- `learning/attribution`
  - `TradeAttributionRecord` 新增：
    - `exit_context_snapshot`
    - `review_tags`
  - `review_summary` 新增：
    - `exit_tag_counts`
  - summary line 会开始提示最常见的快撤事实标签

### 当前取舍

- 这轮先做“事实留痕”，不新建独立的退出事实数据库。
- `review_tags` 目前是规则型标签，不是统计学习结果；目的是先让后面的复盘和参数调整有抓手。
- 手动 SELL 不强行补 `exit_context_snapshot`，避免伪造并不存在的盘中上下文。

### 验证结果

- 精准新增回归：
  - `PYTHONPATH=src pytest tests/test_phase1.py::TestTailMarketBehaviorAwareExit::test_tail_market_scan_uses_sector_relative_trend_weakness tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_auto_sell_reconciliation_backfills_exit_reason_and_playbook tests/test_system_governance.py::SystemGovernanceTests::test_execution_reconciliation_endpoint_persists_trade_and_updates_readiness`
  - 结果：`3 passed`
- focused regression：
  - `PYTHONPATH=src pytest tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py tests/test_system_governance.py`
  - 结果：`115 passed`

### 当前结论

- 现在 `tail_market` 的快撤规则不只是“下单即结束”，而是已经开始把触发退出时的盘面事实一路写到 learning 链。
- 这为下一步做：
  - 哪类快撤最有效
  - 哪类快撤太敏感
  - 哪种板块掉队退出应放宽或收紧
  提供了最小可用数据基础。

## 2026-04-07 M6 学习查询与最小参数建议

本轮继续推进 M6，把上一轮写进 learning 链的退出事实真正变成可查询、可给建议的对象，而不只是留痕。

- 更新文件：
  - [attribution.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/learning/attribution.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- `learning/attribution`
  - 现在支持按：
    - `review_tag`
    - `exit_context_key`
    - `exit_context_value`
    做过滤
  - 返回结果会带 `filters`
- `learning/trade-review`
  - 现在会额外返回：
    - `review_tag_summary`
    - `parameter_hints`
    - `filters`
- `parameter_hints`
  - 当前是最小规则型建议，不直接改参数
  - 已能覆盖：
    - `sector_relative_trend_weak` -> 建议下调 `execution_poll_seconds / focus_poll_seconds`
    - `intraday_fade / negative_alert` -> 建议收紧 `t_stop_loss_soft`
    - `sector_retreat` -> 建议下调 `sector_exposure_limit / sector_theme_rotation_weight`

### 当前取舍

- 这轮先做“可查询 + 可建议”，不直接生成 `ParamProposalInput`。
- 建议仍是规则型、解释型输出，不宣称已经形成统计显著性的自动调参器。
- `exit_context` 过滤目前是单键过滤，先满足最小复盘使用，不先扩成复杂条件表达式。

### 验证结果

- 精准新增回归：
  - `PYTHONPATH=src pytest tests/test_system_governance.py -k 'tail_market_auto_sell_reconciliation_backfills_exit_reason_and_playbook'`
  - 结果：`1 passed`
- focused regression：
  - `PYTHONPATH=src pytest tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py tests/test_system_governance.py`
  - 结果：`115 passed`

### 当前结论

- learning 链现在不只知道“这笔单为什么卖”，还开始支持“筛出某类快撤样本，给出应该看哪些参数”的最小复盘视角。
- 这离真正的自动调参还有距离，但已经把：
  - 退出事实
  - 条件筛选
  - 参数建议
  三件事接到了一条链上。

## 2026-04-07 M6 参数建议转提案

本轮继续推进 M6，把 `trade-review.parameter_hints` 从“只读建议”推进到“可直接生成参数提案预览/事件”。

- 更新文件：
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- 新增接口：
  - `POST /system/learning/parameter-hints/proposals`
- 支持能力：
  - 复用 `trade_date / score_date / review_tag / exit_context_key / exit_context_value` 过滤条件
  - `apply=false`
    - 返回参数提案预览
    - 给出 `current_value -> proposed_value`
  - `apply=true`
    - 复用现有 `ParameterService.propose_change`
    - 直接落 proposal event
    - 参数会按现有治理链进入生效逻辑
- 当前提案值生成规则：
  - `integer` 参数按 20% 步长推
  - `number/percent` 参数按 10% 步长推
  - 全部受 `allowed_range` 约束

### 当前取舍

- 这轮只做“建议 -> 提案”，不做“提案 -> 自动批准/自动下发”。
- 提案值生成还是最小启发式，不宣称已经找到最优参数。
- 这样做的目的，是先把人工值守时最麻烦的一步省掉：不用再手工把 hint 翻译成 proposal JSON。

### 验证结果

- 精准新增回归：
  - `PYTHONPATH=src pytest tests/test_system_governance.py -k 'tail_market_auto_sell_reconciliation_backfills_exit_reason_and_playbook'`
  - 结果：`1 passed`
- focused regression：
  - `PYTHONPATH=src pytest tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py tests/test_system_governance.py`
  - 结果：`115 passed`

### 当前结论

- 现在 learning 链已经不只会提示“该调哪些参数”，还可以把这些建议直接变成结构化提案。
- 距离真正的无人值守自动调参还差两步：
  - 提案审批策略
  - 提案生效后的效果回看与回滚

## 2026-04-07 M6 提案审批基线与回滚基线

本轮继续推进 M6，把参数提案从“能生成”推进到“能说明哪些适合自动批准、哪些必须人工确认，并给出回滚锚点”。

- 更新文件：
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- `POST /system/learning/parameter-hints/proposals`
  - 现在每条预览项会额外返回：
    - `approval_policy`
    - `rollback_baseline`
  - 顶层会返回：
    - `approval_baseline.auto_approvable_count`
    - `approval_baseline.manual_review_count`
- `approval_policy`
  - 当前会基于：
    - 参数 `scope`
    - `effective_period_default`
    - 参数键是否属于核心风控仓位类
  - 给出：
    - `auto_approvable`
    - `risk_level`
    - `required_confirmation`
    - `recommended_status`
    - `recommended_effective_period`
    - `required_approver`
    - `rationale`
- `rollback_baseline`
  - 当前会带出：
    - `restore_value`
    - `current_layer`
    - `active_event_id`
    - `rollback_trigger`
    - `rollback_reason`
    - `proposed_value`

### 当前取舍

- 这轮先做“审批基线可解释 + 回滚锚点可见”，不直接替你做自动审批。
- 现在即使某条建议被标为 `manual_review`，只要显式 `apply=true`，仍会按人工确认后的动作继续走提案链。
- 这样做的目的是先把自动化前的决策边界画清楚，而不是提前把审批权交给系统。

### 验证结果

- 精准新增回归：
  - `PYTHONPATH=src pytest tests/test_system_governance.py -k 'tail_market_auto_sell_reconciliation_backfills_exit_reason_and_playbook'`
  - 结果：`1 passed`
- focused regression：
  - `PYTHONPATH=src pytest tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py tests/test_system_governance.py`
  - 结果：`115 passed`

### 当前结论

- 参数建议链现在已经不只会“给建议、转提案”，还会告诉你：
  - 哪些建议适合自动批准
  - 哪些必须人工确认
  - 回滚时应该回到哪个值
- 这让后面做真正的无人值守审批时，不需要再从零定义边界条件。

## 2026-04-07 M6 按审批基线自动分流提案

本轮继续推进 M6，把上一轮的审批基线从“只展示”推进到“真正影响提案流向”。

- 更新文件：
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- `POST /system/learning/parameter-hints/proposals`
  - 新增：
    - `respect_approval_policy=true` 默认开启
  - 当 `apply=true` 时：
    - `auto_approvable=true`
      - 按 `approval_policy.recommended_status / recommended_effective_period` 直接走批准/生效链
    - `auto_approvable=false`
      - 自动降级为 `evaluating`
      - 不带 `approved_by`
      - 只生成待审提案，不直接生效
- 响应新增：
  - `execution_summary.effective_event_count`
  - `execution_summary.pending_review_event_count`
  - `execution_summary.respect_approval_policy`

### 当前取舍

- 这轮先让系统按边界条件自动分流，不做更复杂的审批工作流。
- 仍保留 `respect_approval_policy=false` 的逃生口，方便后续人工强制执行。
- 这样可以先保证：
  - 监控/盘中类低风险参数可以更快落地
  - 核心仓位/风险暴露类参数不会被一键误改

### 验证结果

- 精准新增回归：
  - `PYTHONPATH=src pytest tests/test_system_governance.py -k 'tail_market_auto_sell_reconciliation_backfills_exit_reason_and_playbook or parameter_hint_proposals_auto_approve_monitor_params'`
  - 结果：`2 passed`
- focused regression：
  - `PYTHONPATH=src pytest tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py tests/test_system_governance.py`
  - 结果：`116 passed`

### 当前结论

- 现在参数建议链已经能做到：
  - 低风险建议自动批准
  - 高风险建议自动转待审
  - 两类结果在接口层显式分开统计
- 这已经非常接近真正的无人值守参数治理，只差最后一段：
  - 对已生效提案的效果追踪
  - 回滚触发的自动判定

## 2026-04-07 M6 提案效果追踪与回滚预览

本轮继续推进 M6，把参数治理从“会分流、会生效”推进到“能回看效果、能预览回滚”。

- 更新文件：
  - [param_store.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/governance/param_store.py)
  - [param_service.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/governance/param_service.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- proposal event 结构补强：
  - `ParamChangeEvent` 现在会持久化：
    - `source_filters`
    - `approval_policy_snapshot`
    - `rollback_baseline`
  - `ParamProposalInput` 已同步支持这些字段，保证从 hint proposal 到 event 落库不丢上下文
- 新增接口：
  - `GET /system/learning/parameter-hints/effects`
    - 汇总已生效提案的效果样本
    - 输出最小指标：
      - `sample_count`
      - `avg_next_day_close_pct`
      - `win_rate`
      - `rollback_recommended`
  - `POST /system/learning/parameter-hints/rollback-preview`
    - 基于现有 proposal event 和过滤样本，预览建议回滚项
    - 返回建议恢复值和回滚原因
- 当前回滚建议规则：
  - 若过滤后有有效样本，且：
    - `avg_next_day_close_pct < 0`
    - 或 `win_rate < 0.4`
  - 则标记 `rollback_recommended=true`

### 当前取舍

- 这轮只做到“效果可回看 + 回滚可预览”，还不直接自动撤销参数。
- 回滚判断先用最小收益/胜率规则，不把它包装成成熟优化器。
- 这样做的目的，是先把治理闭环里最关键的观测层补上，避免参数改动生效后继续处于黑盒状态。

### 验证结果

- focused regression：
  - `PYTHONPATH=src pytest tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py tests/test_system_governance.py`
  - 结果：`116 passed`

### 当前结论

- 现在参数治理链已经具备：
  - 建议生成
  - 审批分流
  - 生效统计
  - 效果追踪
  - 回滚预览
- 离真正的无人值守闭环还差最后一步：
  - 把 `rollback_recommended` 变成受控的 rollback event / approval / execute 链

## 2026-04-07 M6 受控 rollback event 执行链

本轮继续推进 M6，把“建议回滚”推进到“可以真正生成 rollback event，并在安全边界内执行”。

- 更新文件：
  - [param_store.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/governance/param_store.py)
  - [param_service.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/governance/param_service.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- 参数事件结构补强：
  - `ParamChangeEvent` 新增：
    - `rollback_of_event_id`
  - `ParamProposalInput` 新增：
    - `event_type`
    - `rollback_of_event_id`
- 新增接口：
  - `POST /system/learning/parameter-hints/rollback-apply`
    - 会把 `rollback_preview` 转成正式 rollback event
    - rollback event 会带：
      - `event_type=param_rollback`
      - `rollback_of_event_id`
- 回滚执行边界：
  - 默认只在以下条件满足时直接执行：
    - 当前样本已触发 `rollback_recommended`
    - 原提案仍是该参数当前 `active_event_id`
    - 原提案审批快照允许自动处理
  - 若原提案已不是当前生效事件，则默认跳过：
    - 避免同一历史提案被重复回滚
    - 避免覆盖后续人工或系统已做的新调整
- 回滚预览现在也会返回：
  - `current_param_state`
  - `rollback_policy`
    - 可直接看到是否 `active_event_match`
    - 是否 `auto_approvable`
    - 是否 `force_required`

### 当前取舍

- 这轮做的是“受控 rollback event”，不是完全放开的自动撤销。
- 对高风险参数或当前已发生后续调整的参数，默认仍不直接回滚。
- 这样做的目的，是先把最容易误伤的场景挡住，再逐步扩展到更复杂的审批流。

### 验证结果

- 定点新增回归：
  - `PYTHONPATH=src pytest tests/test_system_governance.py -k 'parameter_hint_proposals_auto_approve_monitor_params'`
  - 结果：`1 passed`
- focused regression：
  - `PYTHONPATH=src pytest tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py tests/test_system_governance.py`
  - 结果：`116 passed`

### 当前结论

- 现在参数治理链已经不只是：
  - 给出回滚建议
  - 预览回滚值
- 还可以：
  - 生成可追踪的 rollback event
  - 对低风险且仍为当前生效值的参数直接恢复
  - 对重复回滚和历史事件误操作自动跳过
- 下一段重点会变成：
  - rollback 后的观察窗口与再评估
  - 高风险 rollback 的人工审批接口

## 2026-04-07 M6 并行集成收口

本轮由 Main 线接收并收口 A/B/C 三条并行工作流，目标不是再开新分支，而是把画像产线、盘手化退出、治理闭环同时并到主线并做统一验证。

- 更新文件：
  - [stock_profile.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/stock_profile.py)
  - [precompute.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/precompute.py)
  - [exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/exit_engine.py)
  - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [param_service.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/governance/param_service.py)
  - [param_store.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/governance/param_store.py)
  - [attribution.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/learning/attribution.py)
  - [test_precompute_contexts.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_precompute_contexts.py)
  - [test_stock_profile_artifact.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_stock_profile_artifact.py)
  - [test_phase_exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_exit_engine.py)
  - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- A 线并入：
  - 股性画像开始沉淀为独立 artifact：
    - `serving/latest_stock_behavior_profiles.json`
  - precompute 同日优先复用 artifact cache
  - 跨日 bars 缺失时优先复用 history cache
  - dossier / symbol_context 会带：
    - `behavior_profile_source`
    - `behavior_profile_trade_date`
- B 线并入：
  - `tail_market` 已补 1m 微结构快撤
  - 新增：
    - `sector_sync_weak`
    - `microstructure_fast_exit`
  - 这批更盘手化的退出条件仍统一映射到 `time_stop`
  - 避免在本轮集成里引入新的 exit reason taxonomy 冲突
- C 线并入：
  - proposal / rollback event 已开始带：
    - `observation_window`
    - `approval_ticket`
  - 新增高风险 rollback 人工审批接口：
    - `POST /system/learning/parameter-hints/rollback-approval`
  - attribution / trade-review 已支持：
    - `symbol`
    - `reason`
    过滤，并返回：
    - `by_symbol`
    - `by_reason`
- Main 线补的集成修复：
  - 当前环境下 `starlette TestClient` 首个请求会卡住
  - 测试已切到手动进入 lifespan 的 `ASGISyncClient`
  - 同时修掉了残留 `TestClient` 调用和过于依赖实际通知成功的断言

### 验证结果

- 治理全量：
  - `PYTHONPATH=src pytest tests/test_system_governance.py`
  - 结果：`56 passed`
- focused regression：
  - `PYTHONPATH=src pytest tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py tests/test_system_governance.py`
  - 结果：`123 passed`

### 当前结论

- 现在主线已经同时具备：
  - 股性画像 artifact/cache
  - 1m/5m 盘手化快撤
  - 参数治理观察窗口与高风险审批入口
  - 按标的/按原因复盘视图
- 剩余工作已经从“大功能未落地”转成“把已有最小实现做深做稳”：
  - 画像独立日更调度
  - 更完整微结构与盘口事实
  - 更严格的治理状态机与自动巡检

## 2026-04-07 Main 继续推进微结构与巡检入口

本轮继续在 Main 线上推进，不再只是并行结果收口，而是补上三个实际可用的小入口：

- 更新文件：
  - [run.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/run.py)
  - [precompute.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/precompute.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
  - [exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/exit_engine.py)
  - [test_precompute_contexts.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_precompute_contexts.py)
  - [test_phase_exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_exit_engine.py)
  - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- 新增画像独立刷新入口：
  - `refresh-profiles`
  - `DossierPrecomputeService.refresh_behavior_profiles(...)`
  - 可以单独刷新股性画像 artifact，不强制重算 dossier pack
- 新增治理巡检入口：
  - `GET /system/learning/parameter-hints/inspection`
  - 可查看：
    - 待处理高风险 rollback
    - observation window 的 `near_due / overdue` 预警
- 主线微结构继续增强：
  - `tail_market` / `ExitEngine` 新增“弱反抽失败”判定
  - 触发特征是：
    - 先有 1m 急跌
    - 反抽不足
    - 最新 1m 再次转弱
    - 并结合负面预警或 1m 板块相对走弱
  - 新增 review tag：
    - `micro_rebound_failed`
  - 退出原因仍统一映射到 `time_stop`
- Main 补的稳定性修复：
  - `scheduler` 已兼容 naive / aware datetime 混用
  - 避免 `submitted_at` 与 `now_factory` 时区形式不同导致尾盘扫描报错

### 验证结果

- 本地主线微结构回归：
  - `PYTHONPATH=src pytest tests/test_phase_exit_engine.py tests/test_phase1.py::TestTailMarketBehaviorAwareExit -q`
  - 结果：`18 passed`
- 最新 focused regression：
  - `PYTHONPATH=src pytest tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py tests/test_system_governance.py`
  - 结果：`127 passed`

### 当前结论

- 现在主线已经不只是“有功能”，而是多了三个更适合持续运行的入口：
  - 画像可独立刷新
  - 治理可独立巡检
  - 微结构多了一层更像盘手的弱反抽失败判断
- 接下来最值得做的，是把这些入口再推进成真正的定时任务和自动巡检机制

## 2026-04-07 Main 把治理巡检接入调度链

这一步不再停留在 API 入口层，而是把参数治理巡检抽成可复用 helper，并真正接进 scheduler。

- 更新文件：
  - [inspection.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/governance/inspection.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
  - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- 新增治理巡检复用模块：
  - `ashare_system.governance.inspection`
  - 统一沉淀：
    - `build_observation_window_status(...)`
    - `is_pending_high_risk_rollback(...)`
    - `collect_parameter_hint_inspection(...)`
- `system_api` 不再自己维护一套 inspection 汇总逻辑：
  - `GET /system/learning/parameter-hints/inspection`
  - `POST /system/learning/parameter-hints/inspection/run`
  - 现在都复用同一份 helper
- scheduler 已新增盘后治理巡检任务：
  - 任务名：`参数治理巡检`
  - handler：`governance.parameter_hints:inspection`
  - 会把巡检摘要写入审计
  - live 且开启通知时，遇到高风险待审 rollback 或 observation overdue 会推送告警
- 新增接口级验证：
  - `inspection/run` 不只返回汇总，还会确认审计记录已落库

## 2026-04-07 Main 继续补治理巡检动作建议

这一步继续沿着治理线做“小而硬”的增强，不新增状态机，只把 inspection 结果收敛成固定可执行动作。

- 更新文件：
  - [inspection.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/governance/inspection.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)

### 本轮落地内容

- inspection 现在不只返回计数和列表，还会返回固定动作建议：
  - `continue_observe`
  - `manual_release_or_reject`
  - `consider_rollback_preview`
  - `review_and_keep`
  - `confirm_rollback_effect`
  - `review_rollback_effect`
- 每条巡检项都会附带：
  - `recommended_action.action`
  - `recommended_action.priority`
  - `recommended_action.reason`
- 汇总层新增：
  - `recommended_actions`
  - `recommended_action_counts`
- `inspection/run` 的审计记录和 scheduler 的盘后治理巡检审计都开始带 `recommended_action_counts`
- 这意味着后面不论是前端展示、飞书告警，还是自动化脚本，都可以直接消费固定动作，而不用再读一遍长列表自己推断

### 验证结果

- inspection targeted：
  - `PYTHONPATH=src pytest tests/test_system_governance.py -k "parameter_hint_inspection" -q`
  - 结果：`3 passed`
- 治理相关受影响回归：
  - `PYTHONPATH=src pytest tests/test_system_governance.py -k "parameter_hint_proposals_auto_approve_monitor_params or high_risk_rollback_requires_manual_release or parameter_hint_inspection or learning_review_views_support_symbol_and_reason" -q`
  - 结果：`6 passed`

### 当前结论

- 现在治理巡检已经从“列问题”推进到“给出固定处置动作”。
- 下一步若继续沿这条线推进，最自然的是：
  - 把 `manual_release_or_reject / consider_rollback_preview` 映射到更明确的操作入口
  - 或者给巡检结果补 `action_items`，只聚焦中高优先级项

## 2026-04-07 Main 继续把治理建议收口到操作入口

这一步继续不扩状态机，只把 inspection 输出变成更容易被 UI、飞书和脚本直接消费的结构。

- 更新文件：
  - [inspection.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/governance/inspection.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)

### 本轮落地内容

- 每条 inspection 建议现在都会附带 `operation_targets`
  - `manual_release_or_reject`
    - 直接给出 `POST /system/learning/parameter-hints/rollback-approval`
    - 同时给出 `release / reject` 两套默认 payload
  - `consider_rollback_preview`
    - 直接给出 `POST /system/learning/parameter-hints/rollback-preview`
  - `review_and_keep / confirm_rollback_effect / review_rollback_effect`
    - 直接给出 `GET /system/learning/parameter-hints/effects`
  - `continue_observe`
    - 给出重新巡检入口，便于后续定时刷新或手工复查
- inspection 汇总新增：
  - `action_items`
  - `action_item_count`
  - 默认聚焦中高优先级项
- `inspection/run` 审计与 scheduler 的盘后治理巡检审计都开始同步写入 `action_item_count`

### 验证结果

- inspection targeted：
  - `PYTHONPATH=src pytest tests/test_system_governance.py -k "parameter_hint_inspection" -q`
  - 结果：`3 passed`
- 治理相关受影响回归：
  - `PYTHONPATH=src pytest tests/test_system_governance.py -k "parameter_hint_proposals_auto_approve_monitor_params or high_risk_rollback_requires_manual_release or parameter_hint_inspection or learning_review_views_support_symbol_and_reason" -q`
  - 结果：`6 passed`

### 当前结论

- 现在 inspection 不只是“告诉你该做什么”，还会把“下一步调用哪个接口、带什么参数”直接带出来。
- 继续往前做的话，下一步最自然的是把 `action_items` 再压成真正的高优先级待办视图，减少低信号项。

## 2026-04-07 Main 完成高优先级治理待办与 tail-market review 汇总

这一步是 Main 线第一批任务包的实际收口，分别对应 `Main-P1` 和 `Main-P2`。

- 更新文件：
  - [inspection.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/governance/inspection.py)
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- Main-P1：inspection 高优先级待办视图
  - 在原有 `action_items` 之外新增：
    - `high_priority_action_items`
    - `high_priority_action_item_count`
  - 默认把低信号 `continue_observe` 从高优先级待办里剔除
  - `inspection/run` 审计与 scheduler 盘后治理巡检审计都同步写入 `high_priority_action_item_count`
- Main-P2：tail-market 聚焦 review 汇总接口
  - 新增：
    - `GET /system/tail-market/review`
  - 支持按：
    - `source=latest/history`
    - `symbol`
    - `exit_reason`
    - `review_tag`
    查看 tail-market 扫描退出摘要
  - tail-market scan item 现在会直接带 `review_tags`
  - review 接口会返回：
    - `by_symbol`
    - `by_exit_reason`
    - `by_review_tag`
    - `summary_lines`

### 验证结果

- Main-P1 targeted：
  - `PYTHONPATH=src pytest tests/test_system_governance.py -k "parameter_hint_inspection" -q`
  - 结果：`3 passed`
- Main-P2 + Main-P1 targeted：
  - `PYTHONPATH=src pytest tests/test_system_governance.py -k "tail_market_endpoints_expose_latest_and_history or parameter_hint_inspection" -q`
  - 结果：`4 passed`
- Main 主线小回归：
  - `PYTHONPATH=src pytest tests/test_system_governance.py -k "parameter_hint_proposals_auto_approve_monitor_params or high_risk_rollback_requires_manual_release or parameter_hint_inspection or learning_review_views_support_symbol_and_reason or tail_market_endpoints_expose_latest_and_history" tests/test_phase1.py -k "tail_market_scan or scheduler_tasks" -q`
  - 结果：`8 passed`

### 当前结论

- Main 首批任务包里，`Main-P1` 和 `Main-P2` 都已完成。
- Main 下一步可以继续做：
  - rollback 后二次效果追踪的进一步细化
  - 更聚焦的 tail-market inspection/review 视图

## 2026-04-07 A 线完成因子与退出监控首批交付

- 更新文件：
  - [sector_linkage.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/factors/behavior/sector_linkage.py)
  - [board_behavior.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/factors/behavior/board_behavior.py)
  - [exit_monitor.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/monitor/exit_monitor.py)
  - [test_phase_sector_linkage.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_sector_linkage.py)
  - [test_phase_exit_monitor.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_exit_monitor.py)

### 本轮落地内容

- `sector_linkage.py`
  - 已落最小 7 个板块联动 behavior 因子
- `board_behavior.py`
  - 已落涨停/炸板/回封专项因子
- `exit_monitor.py`
  - 已提供 `check / check_batch / summarize`
  - 只产出监控信号，不下单

### 验证结果

- `PYTHONPATH=src pytest tests/test_phase_sector_linkage.py tests/test_phase_exit_monitor.py`
- 结果：`7 passed`

## 2026-04-07 B 线完成离线回测骨架首批交付

- 更新文件：
  - [playbook_runner.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/backtest/playbook_runner.py)
  - [attribution.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/backtest/attribution.py)
  - [test_phase_playbook_backtest.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_playbook_backtest.py)

### 本轮落地内容

- `playbook_runner.py`
  - 已支持按 `playbook / regime / exit_reason` 过滤并运行最小离线回测
- `backtest/attribution.py`
  - 已输出离线回测 attribution
  - 已明确与 `learning/attribution.py` 的语义边界

### 验证结果

- `PYTHONPATH=src TMPDIR=/tmp pytest tests/test_phase_playbook_backtest.py -q`
- 结果：`3 passed`

## 2026-04-07 C 线完成 discussion/OpenClaw helper 与文档首批交付

- 更新文件：
  - [contracts.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/contracts.py)
  - [opinion_validator.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/opinion_validator.py)
  - [round_summarizer.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/round_summarizer.py)
  - [finalizer.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/finalizer.py)
  - [openclaw-agent-constraints-v1.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/openclaw-agent-constraints-v1.md)
  - [openclaw-agent-prompts-v1.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/openclaw-agent-prompts-v1.md)
  - [discussion-state-output-matrix.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/discussion-state-output-matrix.md)
  - [openclaw-agent-routing-matrix-v1.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/openclaw-agent-routing-matrix-v1.md)
  - [openclaw-prompt-io-contracts-v1.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/openclaw-prompt-io-contracts-v1.md)
  - [client-brief-output-template-v1.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/client-brief-output-template-v1.md)

### 本轮落地内容

- discussion helper 已拆出：
  - `contracts`
  - `opinion_validator`
  - `round_summarizer`
  - `finalizer`
- OpenClaw / discussion 文档已补齐首批契约与模板
- 当前仍未强接主链，保持低风险外围 helper 形态

### 验证结果

- `py_compile`：通过
- import/smoke check：通过

### 验证结果

- 最小新增回归：
  - `PYTHONPATH=src pytest tests/test_phase1.py::TestScheduler::test_scheduler_tasks tests/test_system_governance.py -k "parameter_hint_inspection" -q`
  - 结果：`2 passed`
- 参数治理相关受影响回归：
  - `PYTHONPATH=src pytest tests/test_system_governance.py -k "parameter_hint_proposals_auto_approve_monitor_params or high_risk_rollback_requires_manual_release or parameter_hint_inspection or learning_review_views_support_symbol_and_reason" -q`
  - 结果：`5 passed`

### 当前结论

- 现在“治理可独立巡检”不只是手动 API，而是已经进入盘后调度链。
- 下一步可以继续往两条线推进：
  - 把巡检结果和参数提案/rollback 建议做更明确的自动处置边界
  - 继续补强盘口/逐笔事实，让行为画像和尾盘退出不只依赖 bars 级微结构

## 2026-04-07 Main 线完成 Main-P3 rollback 二次效果追踪细化

- 更新文件：
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- `post_rollback_tracking` 不再只有统计摘要，现已补齐可直接消费的动作结论：
  - `followup_status`
  - `recommended_action`
  - `operation_targets`
- 当前 rollback 二次跟踪可明确区分：
  - `not_started`
  - `insufficient_samples`
  - `continue_observe`
  - `recovery_confirmed`
  - `manual_review_required`
- 动作语义复用现有治理闭环，不新增平行状态机：
  - `continue_observe`
  - `confirm_rollback_effect`
  - `review_rollback_effect`
  - `manual_release_or_reject`
- `operation_targets` 直接指向现有接口：
  - `GET /system/learning/parameter-hints/effects`
  - `POST /system/learning/parameter-hints/rollback-approval`

### 验证结果

- `PYTHONPATH=src pytest tests/test_system_governance.py -k "post_rollback_tracking or parameter_hint_proposals_auto_approve_monitor_params or high_risk_rollback_requires_manual_release or parameter_hint_inspection or learning_review_views_support_symbol_and_reason" -q`
- 结果：`8 passed`

### 当前结论

- `Main-P1` 到 `Main-P3` 已连续收口完成。
- 主线剩余重点进入 `Main-4`：
  - 做阶段性总回归
  - 开始评估 A/B/C 第二批集成点是否需要继续并行推进

## 2026-04-07 Main 线推进 Main-4 代表性主链回归

### 验证结果

- `PYTHONPATH=src pytest tests/test_phase_strategy_runtime_api.py tests/test_discussion_helpers.py tests/test_phase1.py::TestScheduler::test_scheduler_tasks tests/test_phase1.py::TestTailMarketBehaviorAwareExit::test_tail_market_scan_uses_1m_microstructure_fast_exit -q`
- 结果：`11 passed`
- `PYTHONPATH=src pytest tests/test_system_governance.py -k "post_rollback_tracking or parameter_hint_proposals_auto_approve_monitor_params or high_risk_rollback_requires_manual_release or parameter_hint_inspection or learning_review_views_support_symbol_and_reason" -q`
- 结果：`8 passed`

### 当前结论

- 目前 `runtime / tail_market / discussion / system_governance` 四条主链都已有代表性回归回执。
- `Main-4` 进入“继续补剩余集成点或按需扩回归面”的阶段，不再是空白状态。

## 2026-04-07 A 线完成第二批最小接入

- 更新文件：
  - [__init__.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/factors/__init__.py)
  - [market_watcher.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/monitor/market_watcher.py)
  - [test_phase_sector_linkage.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_sector_linkage.py)
  - [test_phase_exit_monitor.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_exit_monitor.py)

### 本轮落地内容

- behavior 因子已进入包级自动注册链，不再依赖手工 import。
- `market_watcher` 已能缓存并暴露 exit monitor signals，但不改 `check_once()` 返回类型，也不触碰执行链。

### 验证结果

- `PYTHONPATH=src pytest tests/test_phase_sector_linkage.py tests/test_phase_exit_monitor.py`
- 结果：`8 passed`

## 2026-04-07 B 线完成第二批回测桥接与导出增强

- 更新文件：
  - [playbook_runner.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/backtest/playbook_runner.py)
  - [engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/backtest/engine.py)
  - [attribution.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/backtest/attribution.py)
  - [test_phase_playbook_backtest.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_playbook_backtest.py)

### 本轮落地内容

- `playbook_runner` 已明确复用底层 `backtest/engine` 输出，不再完全自算一套。
- `attribution` 已补 `overview / export_payload`，更适合 review、API 和文档接入。

### 验证结果

- `PYTHONPATH=src TMPDIR=/tmp pytest tests/test_phase_playbook_backtest.py -q`
- 结果：`4 passed`

## 2026-04-07 C 线完成第二批 discussion helper 最小接入

- 更新文件：
  - [discussion_service.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/discussion_service.py)
  - [state_machine.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/state_machine.py)
  - [finalizer.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/finalizer.py)
  - [test_discussion_helpers.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_discussion_helpers.py)

### 本轮落地内容

- `discussion_service` 已补 `build_summary_snapshot(...)` 与 `build_finalize_bundle(...)` 两个低风险接入点。
- `state_machine` 已补 `needs_round_2(...)` 与 `can_finalize_from_summary(...)` guard。
- `finalizer` 已修正独立调用场景下的 cycle 兜底，避免 finalize packet 误判阻塞。

### 验证结果

- `PYTHONDONTWRITEBYTECODE=1 ... pytest -p no:cacheprovider tests/test_discussion_helpers.py tests/test_phase5_monitor_notify_report.py -k "discussion_helpers or DiscussionFinalizeNotifier"`
- 结果：`8 passed, 43 deselected`

## 2026-04-07 Main 线完成 Main-P5 统一盘后 review board

- 更新文件：
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- 已新增统一盘后汇总接口：
  - `GET /system/reports/review-board`
- review board 当前会汇总三块现有能力：
  - 参数治理高优先级待办
  - tail-market review 摘要
  - discussion finalize / execution precheck 摘要
- `postclose_master` 已新增 `latest_review_board`。
- scheduler 的“参数治理巡检”审计已新增 `review_board_summary`，用于盘后摘要消费。

### 验证结果

- `PYTHONPATH=src pytest tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_endpoints_expose_latest_and_history tests/test_system_governance.py::SystemGovernanceTests::test_parameter_hint_inspection_lists_pending_high_risk_and_window_alerts tests/test_system_governance.py::SystemGovernanceTests::test_post_rollback_tracking_marks_recovery_confirmed tests/test_phase1.py::TestScheduler::test_scheduler_tasks tests/test_phase1.py::TestScheduler::test_build_postclose_review_board_summary_aggregates_sections -q`
- 结果：`6 passed`

### 当前结论

- `Main-P5` 已收口。
- `Main-P6` 还剩把 `Main-P1 ~ Main-P5` 再串成一组更稳定的代表性总回归。

## 2026-04-07 Main 线完成 Main-P6 主链接入点总回归补全

### 验证结果

- `PYTHONPATH=src pytest tests/test_phase_strategy_runtime_api.py tests/test_discussion_helpers.py tests/test_phase1.py::TestScheduler::test_scheduler_tasks tests/test_phase1.py::TestScheduler::test_build_postclose_review_board_summary_aggregates_sections tests/test_phase1.py::TestTailMarketBehaviorAwareExit::test_tail_market_scan_uses_1m_microstructure_fast_exit tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_endpoints_expose_latest_and_history tests/test_system_governance.py::SystemGovernanceTests::test_parameter_hint_inspection_lists_pending_high_risk_and_window_alerts tests/test_system_governance.py::SystemGovernanceTests::test_post_rollback_tracking_marks_recovery_confirmed -q`
- 结果：`18 passed`

### 当前结论

- `Main-P1 ~ Main-P6` 已全部拿到代表性回归回执。
- 当前主线不再缺 review/inspection/汇总层基础能力，后续可以转向：
  - 把第三批 A/B/C 产物进一步消费进主链
  - 或扩更大回归面做阶段性交付确认

## 2026-04-07 A 线完成第三批 exit monitor 摘要与命名对齐

- 更新文件：
  - [exit_monitor.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/monitor/exit_monitor.py)
  - [market_watcher.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/monitor/market_watcher.py)
  - [test_phase_exit_monitor.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_exit_monitor.py)

### 本轮落地内容

- `ExitMonitor.summarize()` 已补 `by_symbol / by_reason / by_severity / by_type / by_tag / summary_lines / items`。
- `MarketWatcher` 已新增最近一次 exit summary 缓存读取能力。
- exit monitor 的 `reason / tags` 已尽量与 `tail_market / exit_engine` 命名对齐。

### 验证结果

- `PYTHONPATH=src pytest tests/test_phase_exit_monitor.py`
- 结果：`4 passed`

## 2026-04-07 B 线完成第三批离线回测弱点分桶与导出契约

- 更新文件：
  - [attribution.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/backtest/attribution.py)
  - [test_phase_playbook_backtest.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_playbook_backtest.py)
  - [offline-backtest-attribution-export-contract.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/offline-backtest-attribution-export-contract.md)

### 本轮落地内容

- `BacktestAttributionReport` 已新增：
  - `weakest_buckets`
  - `compare_views`
- `overview / export_payload` 已同步纳入这些字段。
- 契约文档已明确 `offline_backtest attribution` 与 `learning attribution` 的边界，以及后续 API / report 层的推荐消费方式。

### 验证结果

- `PYTHONPATH=src TMPDIR=/tmp pytest tests/test_phase_playbook_backtest.py -q`
- 结果：`4 passed`

## 2026-04-07 C 线完成第三批 opinion ingress adapter 与接入说明收口

- 更新文件：
  - [opinion_ingress.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/discussion/opinion_ingress.py)
  - [test_discussion_helpers.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_discussion_helpers.py)
  - [openclaw-agent-routing-matrix-v1.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/openclaw-agent-routing-matrix-v1.md)
  - [openclaw-prompt-io-contracts-v1.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/openclaw-prompt-io-contracts-v1.md)
  - [discussion-state-output-matrix.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/discussion-state-output-matrix.md)

### 本轮落地内容

- `opinion_ingress.py` 已提供：
  - `extract_opinion_items(...)`
  - `normalize_openclaw_opinion_payloads(...)`
  - `adapt_openclaw_opinion_payload(...)`
- 现在外部 OpenClaw opinion payload 可以先走 ingress helper，再直接进入现有 `CandidateCaseService.record_opinions_batch(...)`。
- 三份文档已把真实接入顺序收口为：
  - `OpenClaw payload -> opinion_ingress -> record_opinions_batch -> round_summarizer -> build_summary_snapshot -> build_finalize_bundle`

### 验证结果

- `py_compile`：通过
- `tests/test_discussion_helpers.py`：`8 passed`

## 2026-04-07 Main 线完成第四批 OpenClaw ingress 接线与 offline backtest 只读接口

- 更新文件：
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- 新增 `POST /system/discussions/opinions/openclaw-ingress`
  - 入口会按 `trade_date` 自动构建 `symbol -> case_id` 映射
  - 再调用 `adapt_openclaw_opinion_payload(...)`
  - 校验失败只回传 `issues / missing_case_ids / normalized_payloads`，不写回
- `record_batch_opinions(...)` 与 OpenClaw ingress 已共用 `_persist_discussion_writeback_items(...)`
  - 主线不再自己重复拼装批量写回和重建逻辑
- 新增 `GET /system/reports/offline-backtest-attribution`
  - 读取优先级：
    - `serving/latest_offline_backtest_attribution.json`
    - `meeting_state_store["latest_offline_backtest_attribution"]`
  - 返回只保留 `offline_backtest attribution` 语义，不混入 `learning attribution`
  - 直接暴露：
    - `overview`
    - `weakest_buckets`
    - `compare_views`
    - `selected_weakest_bucket`
    - `selected_compare_view`
    - `summary_lines`

### 验证结果

- `PYTHONPATH=src pytest tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_opinion_ingress_endpoint_writes_batch tests/test_system_governance.py::SystemGovernanceTests::test_offline_backtest_attribution_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_tail_market_endpoints_expose_latest_and_history -q`
- 结果：`4 passed`

### 当前结论

- `Main-P7 / Main-P8 / Main-P9` 已完成。
- 主线已经把 C 线的 `opinion_ingress` 和 B 线的 `offline_backtest attribution export` 消费进系统 API，第四批主线收口完成。

## 2026-04-07 Main 线完成第五批主链总览扩展

- 更新文件：
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- `review-board` 已接入：
  - `offline_backtest`
  - `exit_monitor`
- `postclose-master` 已带出：
  - `latest_exit_snapshot`
  - `latest_offline_backtest_attribution`
- `openclaw-ingress` 已改为复用 `DiscussionCycleService.write_openclaw_opinions(...)`

### 验证结果

- `PYTHONPATH=src pytest tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_opinion_ingress_endpoint_writes_batch tests/test_system_governance.py::SystemGovernanceTests::test_offline_backtest_attribution_report_endpoint_reads_latest_export tests/test_discussion_helpers.py -q`
- 结果：`16 passed`

## 2026-04-07 Main 线完成第六批 preview 与 metrics 只读入口

- 更新文件：
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)

### 本轮落地内容

- 新增 `POST /system/discussions/opinions/openclaw-preview`
- 新增 `GET /system/reports/offline-backtest-metrics`
- `postclose-master` 已新增：
  - `latest_exit_snapshot_trend_summary`
  - `latest_offline_backtest_metrics`
- `openclaw-ingress` 写回结果已直接透出：
  - `refresh_summary`
  - `refreshed_summary_snapshot`
  - `touched_case_summaries`

### 验证结果

- `PYTHONPATH=src pytest tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_opinion_ingress_endpoint_writes_batch tests/test_system_governance.py::SystemGovernanceTests::test_openclaw_opinion_preview_endpoint_normalizes_without_writeback tests/test_system_governance.py::SystemGovernanceTests::test_offline_backtest_attribution_report_endpoint_reads_latest_export tests/test_system_governance.py::SystemGovernanceTests::test_offline_backtest_metrics_report_endpoint_reads_latest_export tests/test_discussion_helpers.py -q`
- 结果：`19 passed`

## 2026-04-07 Main 线启动第七批并入 review-board

- 更新文件：
  - [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)

### 本轮落地内容

- `review-board` 已新增：
  - `offline_backtest_metrics` section
  - `exit_monitor.trend_summary`
- 盘后总览现在能同时看到：
  - offline backtest attribution
  - offline backtest metrics
  - latest exit snapshot
  - exit snapshot trend summary

### 验证结果

- `PYTHONPATH=src pytest tests/test_system_governance.py::SystemGovernanceTests::test_review_board_aggregates_governance_tail_market_and_discussion tests/test_system_governance.py::SystemGovernanceTests::test_offline_backtest_metrics_report_endpoint_reads_latest_export -q`
- 结果：`2 passed`

## 2026-04-08 Main 线固化 Linux/OpenClaw + Windows QMT VM 架构边界

- 更新文件：
  - [technical-manual.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/technical-manual.md)
  - [openclaw-linux-qmt-deployment-v1.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/openclaw-linux-qmt-deployment-v1.md)
  - [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)

### 本轮落地内容

- 正式把未来生产形态写清为：
  - Linux 服务器运行 OpenClaw Gateway、Agent 团队与 `ashare-system-v2`
  - Windows VM 运行 `Windows Execution Gateway + QMT / XtQuant`
- 正式收口一条硬边界：
  - Agent 只参与决策、审议、复盘、研究
  - Agent 不直接持有 QMT 下单权
  - 真正调用 QMT 的唯一写口应为 `Windows Execution Gateway`
- 正式收口自我进化边界：
  - 允许进入 prompt、routing、离线回测、review 研究闭环
  - 不允许自动进入 live 执行链
  - 任何进化结果都必须走 `offline -> paper/supervised -> human review -> deploy`
- 已同步开出第 7 批 A/B/C 任务：
  - A：执行网关健康快照
  - B：offline self-improvement proposal/export
  - C：OpenClaw replay / proposal packet

### 当前结论

- 当前主线设计已不再把“OpenClaw 直接调 QMT”当成目标方案。
- 未来生产落地方案已经固定为“Linux 控制面 + Windows 执行面 + 单一执行写口 + 自我进化离线闭环”。 
