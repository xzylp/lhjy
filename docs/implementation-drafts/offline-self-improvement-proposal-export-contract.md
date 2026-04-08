# Offline Self-Improvement Proposal/Export 契约

## 目标

固化 `ashare_system.backtest.playbook_runner` 的 `proposal/export packet`、serving-ready latest export、archive-ready manifest 与 latest descriptor 输出结构，供 Main/API 直接消费。

该结构只服务离线回测研究与自我进化流程，不可直接推送到 live 执行链路。

## 边界

- 输入来源：
  - `offline trade list`
  - 或 `backtest.engine.BacktestResult` 适配后的离线成交样本
- 输出用途：
  - 离线复盘评审
  - 自我进化策略迭代建议
  - API 透传展示
- 明确不做：
  - 线上事实归因
  - 直接下单触发
  - 替代 `learning attribution`

## Runner 返回字段

`PlaybookBacktestRunResult` 关键字段：

- `self_improvement_proposal`
- `self_improvement_export`
- `serving_ready_latest_export`（可直接落盘 latest 文件）
- `archive_ready_manifest`（可直接给 Linux 主控或离线调度做归档）
- `latest_descriptor`（统一引用 latest serving 与 archive ref）
- 兼容别名：
  - `proposal_packet`（同 `self_improvement_proposal`）
  - `export_packet`（同 `self_improvement_export`）

## self_improvement_proposal

最小建议结构：

- `available`
- `packet_scope`（固定 `offline_self_improvement_proposal`）
- `semantics_note`
- `live_execution_allowed`（固定 `false`）
- `generated_at`
- `filters`
- `input_source`
- `summary_lines`
- `proposal`
  - `selected_weakest_bucket`（来自 attribution）
  - `selected_compare_view`（来自 attribution）
  - `selected_metric_bucket`（来自 metrics）
  - `actions`（仅离线改进动作）
  - `metrics_anchor.self_improvement_inputs`
  - `attribution_anchor.self_improvement_inputs`

## self_improvement_export

最小导出结构：

- `available`
- `packet_scope`（固定 `offline_self_improvement_export`）
- `semantics_note`
- `live_execution_allowed`（固定 `false`）
- `generated_at`
- `filters`
- `input_source`
- `summary_lines`
- `proposal_packet`
- `attribution`（透传 attribution.export_payload）
- `metrics`（透传 metrics.export_payload）

## serving_ready_latest_export

用于 Main/Linux 控制面直接写出 `latest_offline_self_improvement_export.json`，避免在调用方再手写包装层。

入口：

- `runner.run(...).serving_ready_latest_export`
- `runner.build_serving_ready_latest_export(result_or_payload)`
- `runner.write_latest_offline_self_improvement_export(result_or_payload, output_path=...)`

最小结构：

- `available`
- `packet_scope`（仍固定 `offline_self_improvement_export`）
- `semantics_note`
- `live_execution_allowed`（固定 `false`）
- `generated_at`
- `serving_ready`（固定 `true`）
- `artifact_name`（固定 `latest_offline_self_improvement_export.json`）
- `consumers`
- `filters`
- `input_source`
- `summary_lines`
- `proposal_packet`
- `attribution`
- `metrics`

说明：

- 顶层结构仍然保持 `offline_self_improvement_export` 语义，不额外包一层 `payload`
- helper 会强制 `live_execution_allowed=false`
- 若存在 `proposal_packet`，helper 会强制 `proposal_packet.live_execution_allowed=false`

## archive-ready manifest

用于 Main/Linux 控制面在 latest 基础上做历史归档，避免调用方重复拼接 artifact 名称和归档路径策略。

入口：

- `runner.build_archive_ready_manifest(result_or_payload)`
- `runner.write_archive_offline_self_improvement_export(result_or_payload, output_dir=...)`

最小结构：

- `available`
- `manifest_scope`（固定 `offline_self_improvement_archive_manifest`）
- `packet_scope`（固定 `offline_self_improvement_export`）
- `semantics_note`
- `live_execution_allowed`（固定 `false`）
- `archive_ready`（固定 `true`）
- `offline_only`（固定 `true`）
- `generated_at`
- `source_generated_at`
- `trade_date`
- `artifact_name`（示例：`offline_self_improvement_export-20260407-20260407T100000.json`）
- `archive_group`（示例：`offline_self_improvement/2026-04-07`）
- `relative_archive_path`（示例：`offline_self_improvement/2026-04-07/{artifact_name}`）
- `latest_serving_path`（固定前缀：`serving/latest_offline_self_improvement_export.json`）
- `serving_artifact_name`
- `consumers`
- `archive_tags`
- `manifest`
  - `packet_scope`
  - `input_source`
  - `filters`
  - `summary_line_count`
  - `serving_ready`

`write_archive_offline_self_improvement_export(...)` 额外返回：

- `written_path`
- `content_type`（固定 `application/json`）
- `encoding`（固定 `utf-8`）
- `write_mode`（固定 `append_archive`）

说明：

- archive helper 复用 serving-ready export 作为归档源，不改变 offline/self-improvement 语义。
- `live_execution_allowed` 与 `semantics_note` 在 manifest 层继续保留并显式输出。

## latest descriptor / archive ref

用于 Linux 主控或离线调度直接读取 latest serving 与 archive 引用，避免调用方重复拼接双引用。

入口：

- `runner.run(...).latest_descriptor`
- `runner.build_latest_descriptor(result_or_payload, archive_manifest=...)`
- `runner.build_archive_ref(result_or_payload, archive_manifest=...)`
- `runner.write_latest_offline_self_improvement_descriptor(result_or_payload, output_path=...)`

最小结构：

- `descriptor_scope`（固定 `offline_self_improvement_latest_descriptor`）
- `research_track`（固定 `self_evolution_research`）
- `semantics_note`
- `live_execution_allowed`（固定 `false`）
- `offline_only`（固定 `true`）
- `generated_at`
- `consumers`
- `latest`
  - `available`
  - `artifact_name`（latest serving 文件名）
  - `serving_path`
  - `packet_scope`
  - `input_source`
- `archive_ref`
  - `manifest_scope`
  - `artifact_name`
  - `archive_group`
  - `relative_archive_path`
  - `archive_path`（当前等于 `relative_archive_path`）
  - `trade_date`
  - `research_track`
  - `semantics_note`
  - `live_execution_allowed=false`
- `guardrails`
  - `live_execution_allowed=false`
  - `research_track`
  - `semantics_note`

示例：

```json
{
  "version": "v1",
  "descriptor_scope": "offline_self_improvement_latest_descriptor",
  "available": true,
  "packet_scope": "offline_self_improvement_export",
  "research_track": "self_evolution_research",
  "semantics_note": "该 proposal/export packet 只服务离线研究与自我进化，不可直接推 live。",
  "live_execution_allowed": false,
  "offline_only": true,
  "generated_at": "2026-04-07T10:00:00",
  "source_generated_at": "2026-04-07T10:00:00",
  "consumers": ["main", "openclaw"],
  "latest": {
    "available": true,
    "artifact_name": "latest_offline_self_improvement_export.json",
    "serving_path": "serving/latest_offline_self_improvement_export.json",
    "packet_scope": "offline_self_improvement_export",
    "input_source": "trade_list"
  },
  "archive_path": "offline_self_improvement/2026-04-07/offline_self_improvement_export-20260407-20260407T100000.json",
  "archive_ref": {
    "manifest_scope": "offline_self_improvement_archive_manifest",
    "artifact_name": "offline_self_improvement_export-20260407-20260407T100000.json",
    "archive_group": "offline_self_improvement/2026-04-07",
    "relative_archive_path": "offline_self_improvement/2026-04-07/offline_self_improvement_export-20260407-20260407T100000.json",
    "archive_path": "offline_self_improvement/2026-04-07/offline_self_improvement_export-20260407-20260407T100000.json",
    "trade_date": "2026-04-07",
    "research_track": "self_evolution_research",
    "semantics_note": "该 proposal/export packet 只服务离线研究与自我进化，不可直接推 live。",
    "live_execution_allowed": false
  },
  "guardrails": {
    "live_execution_allowed": false,
    "research_track": "self_evolution_research",
    "semantics_note": "该 proposal/export packet 只服务离线研究与自我进化，不可直接推 live。"
  }
}
```

## descriptor contract sample helper

用于 Linux 主控或 OpenClaw 集成侧直接照抄 `latest_descriptor` 的消费样例，而不是自己再整理字段路径说明。

入口：

- `runner.run(...).descriptor_contract_sample`
- `runner.build_descriptor_contract_sample(result_or_payload, archive_manifest=...)`

最小结构：

- `contract_scope`（固定 `offline_self_improvement_descriptor_contract_sample`）
- `descriptor_scope`
- `research_track`
- `semantics_note`
- `live_execution_allowed`
- `latest_descriptor_sample`
- `archive_ref_sample`
- `consumption_order`
- `recommended_fields`

`consumption_order` 当前固定建议：

1. 先读 `latest_descriptor.latest`
2. 再读 `latest_descriptor.archive_ref`
3. 最后读 `latest_descriptor.guardrails`

`recommended_fields` 当前至少包含：

- `main`
  - `descriptor_scope`
  - `research_track`
  - `serving_path`
  - `latest_artifact_name`
  - `archive_path`
  - `trade_date`
  - `guardrail`
- `openclaw`
  - `research_track`
  - `packet_scope`
  - `input_source`
  - `archive_ref`
  - `semantics_note`
  - `live_execution_allowed`

最小示例：

```json
{
  "contract_scope": "offline_self_improvement_descriptor_contract_sample",
  "descriptor_scope": "offline_self_improvement_latest_descriptor",
  "research_track": "self_evolution_research",
  "semantics_note": "该 proposal/export packet 只服务离线研究与自我进化，不可直接推 live。",
  "live_execution_allowed": false,
  "latest_descriptor_sample": {
    "descriptor_scope": "offline_self_improvement_latest_descriptor"
  },
  "archive_ref_sample": {
    "relative_archive_path": "offline_self_improvement/2026-04-07/offline_self_improvement_export-20260407-20260407T100000.json"
  },
  "consumption_order": [
    {
      "step": 1,
      "target": "serving_latest",
      "read_from": "latest_descriptor.latest"
    },
    {
      "step": 2,
      "target": "archive_ref",
      "read_from": "latest_descriptor.archive_ref"
    },
    {
      "step": 3,
      "target": "guardrails",
      "read_from": "latest_descriptor.guardrails"
    }
  ],
  "recommended_fields": {
    "main": {
      "serving_path": "latest_descriptor.latest.serving_path",
      "archive_path": "latest_descriptor.archive_ref.archive_path"
    },
    "openclaw": {
      "archive_ref": "latest_descriptor.archive_ref.relative_archive_path",
      "semantics_note": "latest_descriptor.guardrails.semantics_note"
    }
  }
}
```

## API-Ready / Serving-Ready 示例

```json
{
  "available": true,
  "packet_scope": "offline_self_improvement_export",
  "semantics_note": "该 proposal/export packet 只服务离线研究与自我进化，不可直接推 live。",
  "live_execution_allowed": false,
  "generated_at": "2026-04-07T10:00:00",
  "serving_ready": true,
  "artifact_name": "latest_offline_self_improvement_export.json",
  "consumers": ["main", "openclaw"],
  "filters": {
    "playbook": "leader_chase"
  },
  "input_source": "trade_list",
  "summary_lines": [
    "离线回测命中 2 笔样本，总收益 3.00%，来源 trade_list。"
  ],
  "proposal_packet": {
    "packet_scope": "offline_self_improvement_proposal",
    "live_execution_allowed": false
  },
  "attribution": {
    "overview": {
      "trade_count": 2
    }
  },
  "metrics": {
    "overview": {
      "total_trades": 2
    }
  }
}
```

## Main/API 消费建议

1. 需要落盘 latest 文件时，优先使用 `runner.write_latest_offline_self_improvement_export(...)`，避免控制面重复手写 `latest_offline_self_improvement_export.json`。
2. 需要归档元数据时，优先使用 `runner.run(...).archive_ready_manifest`、`runner.build_archive_ready_manifest(...)` 或 `runner.write_archive_offline_self_improvement_export(...)`，不要在 Linux 主控里重复拼 `artifact_name / relative_archive_path / latest_serving_path`。
3. 主控若需要同时消费 latest 与 archive 引用，优先读取 `runner.run(...).latest_descriptor` 或 `runner.build_latest_descriptor(...)`，不要自行拼 serving 与 archive 双引用。
4. 若需要直接照抄字段路径约定，优先读取 `runner.run(...).descriptor_contract_sample` 或 `runner.build_descriptor_contract_sample(...)`。
5. 若只做接口透传，可直接读取 `runner.run(...).self_improvement_export`，避免在 API 层重组 attribution + metrics。
6. 若读取 serving 文件，按顶层 `offline_self_improvement_export` 结构直接消费，不需要再额外下钻包装层。
7. 页面摘要展示优先使用：
   - `self_improvement_proposal.summary_lines`
   - `self_improvement_proposal.proposal.actions`
8. 所有外发接口、archive manifest、latest descriptor 与 contract sample 都保留并透传：
   - `live_execution_allowed=false`
   - `semantics_note`
9. 若暂无样本（`available=false`），直接展示空建议，不触发任何执行动作。
10. 该导出、归档 manifest、latest descriptor 与 contract sample 只服务离线研究与自我进化，不替代 `learning attribution`，也不可直接推送到 live 执行链路。
