# OpenClaw Replay / Proposal Packet v1

更新时间：2026-04-08

事实来源：

- `src/ashare_system/discussion/discussion_service.py`
- `src/ashare_system/discussion/opinion_ingress.py`
- `src/ashare_system/discussion/round_summarizer.py`

## 目标

在现有 `preview / writeback / summary` helper 基础上，补一层统一 packet，供以下场景复用：

- 盘后 replay
- 自我进化研究
- 离线评估与样本回放

该 packet 明确是离线用途：

- `offline_only = true`
- `live_trigger = false`

它不直接触发 live，也不替代现有 `write_openclaw_opinions(...)`。

## 入口

```python
packet = cycle_service.build_openclaw_replay_proposal_packet(
    payload,
    trade_date="2026-04-07",
    expected_round=1,
    expected_agent_id="ashare-research",
    expected_case_ids=["case-20260407-600519-SH"],
)
```

## 顶层结构

```python
{
    "packet_type": "openclaw_replay_proposal",
    "packet_id": str,
    "trade_date": str,
    "generated_at": str,
    "source": "discussion_service",
    "offline_only": True,
    "live_trigger": False,
    "source_refs": list[dict],
    "archive_tags": list[str],
    "research_track": str,
    "archive_manifest": dict,
    "latest_descriptor": dict,
    "contract_sample": dict,
    "preview": dict,
    "summary_snapshot": dict,
    "writeback_preview": dict,
    "replay_packet": dict,
    "proposal_packet": dict,
}
```

## 字段说明

### archive-ready 元数据

- `packet_id`
  归档友好的唯一标识，格式为 `<packet_type>-<trade_date_yyyymmdd>-<generated_at_compact>`。
- `source_refs`
  当前 packet 的来源引用列表，默认至少包含：
  - `discussion_cycle`
  - `summary_snapshot`
  - `openclaw_preview`
- `archive_tags`
  供 Linux/OpenClaw 侧直接做样本归档、检索和分桶的标签列表。
- `research_track`
  当前 packet 对应的研究轨道：
  - 合并 packet：`replay_proposal_dual_track`
  - replay packet：`post_close_replay`
  - proposal packet：`self_evolution_research`
- `archive_manifest`
  归档辅助结构，给 Linux/OpenClaw 侧直接落地 artifact 和 latest alias。
- `latest_descriptor`
  latest 拉取与 review 引用的最小描述符，字段对齐 OpenClaw 归档侧的直接消费需求。
- `contract_sample`
  统一 contract sample，内置 replay/proposal descriptor 最小示例、manifest/descriptor 对照关系和 Linux/OpenClaw 最小使用样例。

### `archive_manifest`（archive helper）

结构：

```python
{
    "version": "v1",
    "packet_type": str,
    "packet_id": str,
    "trade_date": str,
    "research_track": str,
    "artifact_name": str,
    "archive_group": str,
    "archive_path": str,
    "latest_aliases": list[str],
    "recommended_tracks": [
        {
            "research_track": "post_close_replay" | "self_evolution_research",
            "archive_group": str,
            "archive_path_prefix": str,
            "artifact_name_pattern": str,
            "latest_alias": str,
        }
    ],
    "archive_tags": list[str],
    "offline_only": True,
    "live_trigger": False,
}
```

说明：

- `artifact_name`：当前 packet 建议归档文件名，默认 `<packet_id>.json`
- `archive_group`：同一交易日统一归档分组，默认 `openclaw/discussion/<trade_date_yyyymmdd>`
- `archive_path`：当前 packet 的建议落盘路径，默认 `<archive_group>/<research_track>/<artifact_name>`
- `latest_aliases`：建议维护的 latest alias（含通用 latest 与 packet_type latest）
- `recommended_tracks`：明确 replay/proposal 双轨归档建议
  - replay：`post_close_replay`
  - proposal：`self_evolution_research`
  - 每条建议会同时给出 `archive_group`、`archive_path_prefix`、`artifact_name_pattern`、`latest_alias`

公开 helper：

```python
manifest = cycle_service.build_openclaw_archive_manifest(packet)
```

该 helper 允许 Linux/OpenClaw 侧对已生成 packet 二次重建 manifest，而不必手写文件名规则。

### `latest_descriptor`（latest helper）

结构：

```python
{
    "version": "v1",
    "packet_type": str,
    "packet_id": str,
    "research_track": str,
    "artifact_name": str,
    "archive_path": str,
    "latest_aliases": list[str],
    "source_refs": list[dict],
    "archive_tags": list[str],
    "offline_only": True,
    "live_trigger": False,
}
```

说明：

- `packet_type / packet_id / research_track`：标识当前 latest 指向的样本
- `artifact_name / archive_path / latest_aliases`：latest 拉取所需的最小定位信息
- `source_refs / archive_tags`：review 与追溯引用字段，可直接写入审计/复盘索引
- `offline_only=true`、`live_trigger=false`：保持纯离线研究语义

公开 helper：

```python
descriptor = cycle_service.build_openclaw_latest_descriptor(packet)
```

该 helper 允许 Linux/OpenClaw 侧基于已有 packet 直接重建 latest descriptor，不需要重复拼接路径规则。

### `contract_sample`（统一 contract sample helper）

结构：

```python
{
    "version": "v1",
    "packet_type": str,
    "packet_id": str,
    "replay_descriptor_minimal": dict,
    "proposal_descriptor_minimal": dict,
    "manifest_latest_mapping": [
        {
            "manifest_field": str,
            "descriptor_field": str,
            "relation": "equal",
        }
    ],
    "linux_openclaw_samples": {
        "latest_pull": dict,
        "review_reference": dict,
        "archive_reference": dict,
    },
    "offline_only": True,
    "live_trigger": False,
}
```

最小示例字段（代码化）：

- `replay_descriptor_minimal`
  - `packet_type = "openclaw_replay_packet"`
  - `packet_id`
  - `research_track = "post_close_replay"`
  - `artifact_name`
  - `archive_path`
  - `latest_aliases`
  - `source_refs`
  - `archive_tags`
- `proposal_descriptor_minimal`
  - `packet_type = "openclaw_proposal_packet"`
  - `packet_id`
  - `research_track = "self_evolution_research"`
  - `artifact_name`
  - `archive_path`
  - `latest_aliases`
  - `source_refs`
  - `archive_tags`

`archive_manifest / latest_descriptor` 对照关系：

- `artifact_name`: `archive_manifest.artifact_name == latest_descriptor.artifact_name`
- `archive_path`: `archive_manifest.archive_path == latest_descriptor.archive_path`
- `latest_aliases`: `archive_manifest.latest_aliases == latest_descriptor.latest_aliases`
- `archive_tags`: `archive_manifest.archive_tags == latest_descriptor.archive_tags`

Linux/OpenClaw 最小样例：

- `linux_openclaw_samples.latest_pull`
  - 输入：`latest_aliases[0]`
  - 期望返回字段：`packet_type/packet_id/research_track/artifact_name/archive_path/latest_aliases`
- `linux_openclaw_samples.review_reference`
  - 使用：`packet_id + source_refs + archive_tags`
- `linux_openclaw_samples.archive_reference`
  - 使用：`archive_group + archive_path + artifact_name`

公开 helper：

```python
sample = cycle_service.build_openclaw_contract_sample(packet)
```

该 helper 允许 Linux/OpenClaw 侧基于已有 packet 快速生成统一 contract 样例，减少各侧手工拼装。

### `preview`

复用 `DiscussionCycleService.adapt_openclaw_opinion_payload(...)` 的主要结果，但去掉不可直接序列化的原始 `(case_id, CandidateOpinion)` 元组。

关键字段：

- `ok`
- `trade_date`
- `case_id_map`
- `default_case_id`
- `raw_count`
- `normalized_payloads`
- `normalized_items`
- `issues`
- `summary_lines`
- `covered_case_ids`
- `missing_case_ids`
- `duplicate_keys`
- `substantive_round_2_case_ids`

### `summary_snapshot`

直接复用 `build_summary_snapshot(trade_date)` 结果，表示当前 trade_date 的讨论汇总视图。

### `writeback_preview`

把原始 `writeback_items` 转成可 JSON 序列化的候选写回清单，仅做预览，不做实际写回。

结构：

```python
{
    "count": int,
    "case_ids": list[str],
    "items": [
        {
            "case_id": str,
            "symbol": str,
            "name": str,
            "round": int,
            "agent_id": str,
            "stance": str,
            "confidence": str,
            "reasons": list[str],
            "evidence_refs": list[str],
            "thesis": str,
            "key_evidence": list[str],
            "evidence_gaps": list[str],
            "questions_to_others": list[str],
            "remaining_disputes": list[str],
        }
    ],
    "summary_lines": list[str],
}
```

### `replay_packet`

面向盘后 replay 的上下文快照。

关键字段：

- `mode = "post_close_replay"`
- `offline_only = true`
- `selected_case_ids`
- `focus_pool_case_ids`
- `round_2_target_case_ids`
- `cycle`
- `summary_lines`

### `proposal_packet`

面向自我进化研究的候选 proposal 视图。

关键字段：

- `mode = "self_evolution_research"`
- `offline_only = true`
- `live_trigger = false`
- `writeback_count`
- `writeback_candidates`
- `covered_case_ids`
- `missing_case_ids`
- `duplicate_keys`
- `substantive_round_2_case_ids`
- `summary_lines`

## 使用建议

- 只想看 payload 规范化与覆盖问题时，优先读 `preview`
- 只想看候选写回建议时，优先读 `writeback_preview`
- 做盘后复盘或 case 回放时，优先读 `replay_packet`
- 做自我进化研究或 proposal 归档时，优先读 `proposal_packet`
- Linux/OpenClaw 归档样本时，优先使用：
  - `packet_id`
  - `research_track`
  - `archive_tags`
  - `source_refs`
  - `archive_manifest.artifact_name`
  - `archive_manifest.archive_path`
  - `archive_manifest.latest_aliases`
  - `archive_manifest.recommended_tracks`
  - `latest_descriptor`（用于 latest 拉取与 review 引用）
  - `contract_sample`（用于统一示例、字段校对与接入模板）

## 边界

- 该 helper 不会调用 `record_opinions_batch(...)`
- 该 helper 不会调用 `rebuild_case(...)`
- 该 helper 不会修改 cycle、state machine 或 live 调度状态
