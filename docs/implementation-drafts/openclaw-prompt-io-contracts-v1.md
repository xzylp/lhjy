# OpenClaw Prompt IO Contracts v1

更新时间：2026-04-07

事实来源：

- `src/ashare_system/discussion/contracts.py`
- `src/ashare_system/discussion/opinion_ingress.py`
- `src/ashare_system/discussion/opinion_validator.py`
- `src/ashare_system/discussion/round_summarizer.py`
- `src/ashare_system/discussion/finalizer.py`
- `src/ashare_system/discussion/protocol.py`

## 1. 子代理输出 contract

当前 discussion 子代理输出 contract 是：

- 顶层必须是 JSON 数组
- 数组每项必须可被 `DiscussionOpinion.model_validate` 接收

关键字段类型：

- `case_id`: `str`
- `round`: `int`
- `agent_id`: `str`
- `stance`: `OpinionStance`
- `confidence`: `high | medium | low`
- `reasons`: `list[str]`
- `evidence_refs`: `list[str]`
- `recorded_at`: ISO datetime 字符串

## 2. ingress 输入输出 contract

### `extract_opinion_items`

当前支持的 payload 形态：

- `list[dict]`
- 单条 opinion `dict`
- `{"opinions": [...]}`
- `{"items": [...]}`
- `{"results": [...]}`
- `{"output": ...}`
- `{"data": ...}`
- `{"payload": ...}`
- `{"result": ...}`

输出：

- `list[dict]`

### `normalize_openclaw_opinion_payloads`

输入：

- 原始 payload
- 可选 `expected_round`
- 可选 `expected_agent_id`
- 可选 `case_id_map`
- 可选 `default_case_id`

行为：

- 若缺 `round`，补 `expected_round`
- 若缺 `agent_id`，补 `expected_agent_id`
- 若缺 `case_id`，优先通过 `symbol -> case_id` 映射补齐

输出：

- `list[dict]`

### `adapt_openclaw_opinion_payload`

输入：

- 原始 payload
- 可选 `expected_round`
- 可选 `expected_agent_id`
- 可选 `expected_case_ids`
- 可选 `case_id_map`
- 可选 `default_case_id`

输出字段：

- `ok`
- `raw_count`
- `normalized_payloads`
- `normalized_items`
- `issues`
- `summary_lines`
- `covered_case_ids`
- `missing_case_ids`
- `duplicate_keys`
- `substantive_round_2_case_ids`
- `writeback_items`

其中：

- `normalized_items` 已是 `DiscussionOpinion` 兼容结构
- `writeback_items` 已是 `(case_id, CandidateOpinion)` 结构，可直接喂给 `CandidateCaseService.record_opinions_batch(...)`

### `DiscussionCycleService.adapt_openclaw_opinion_payload`

输入：

- 原始 payload
- 必填 `trade_date`
- 可选 `expected_round`
- 可选 `expected_agent_id`
- 可选 `expected_case_ids`
- 可选 `case_id_map`
- 可选 `default_case_id`

行为：

- 先按 `trade_date` 从 `CandidateCaseService.list_cases(...)` 构建 `symbol -> case_id`
- 再调用 `opinion_ingress.adapt_openclaw_opinion_payload(...)`
- 若外部显式传入 `case_id_map`，则在自动映射上补充/覆盖
- 若映射后只剩一个 case 且未显式传入 `default_case_id`，自动补默认 case_id

额外输出字段：

- `trade_date`
- `case_id_map`
- `default_case_id`

### 最小 writeback 示例

原始 OpenClaw payload：

```json
{
  "output": {
    "items": [
      {
        "symbol": "600519.SH",
        "stance": "selected",
        "reasons": ["研究支持"],
        "evidence_refs": ["news:1"]
      }
    ]
  }
}
```

直接经 service 适配：

```python
adapter_result = cycle_service.adapt_openclaw_opinion_payload(
    payload,
    trade_date="2026-04-07",
    expected_round=1,
    expected_agent_id="ashare-research",
    expected_case_ids=["case-20260407-600519-SH"],
)
```

关键输出：

```python
adapter_result["normalized_payloads"] == [
    {
        "symbol": "600519.SH",
        "case_id": "case-20260407-600519-SH",
        "round": 1,
        "agent_id": "ashare-research",
        "stance": "selected",
        "reasons": ["研究支持"],
        "evidence_refs": ["news:1"],
    }
]

adapter_result["writeback_items"] == [
    (
        "case-20260407-600519-SH",
        CandidateOpinion(
            round=1,
            agent_id="ashare-research",
            stance="support",
            reasons=["研究支持"],
            evidence_refs=["news:1"],
            ...
        ),
    )
]
```

说明：

- `selected` 会先被 validator 归一化为当前主链使用的 `support`
- `symbol` 不会写入 `CandidateOpinion`，只用于 ingress 阶段补 `case_id`
- 若 `case_id_map` 未命中，`writeback_items` 会为空，且 `issues` 中会出现 `missing_case_coverage`

### `DiscussionCycleService.write_openclaw_opinions`

输入：

- 原始 payload
- 必填 `trade_date`
- 可选 `expected_round`
- 可选 `expected_agent_id`
- 可选 `expected_case_ids`
- 可选 `case_id_map`
- 可选 `default_case_id`
- 可选 `auto_rebuild`

行为：

- 先调用 `DiscussionCycleService.adapt_openclaw_opinion_payload(...)`
- `ok=True` 时调用 `CandidateCaseService.record_opinions_batch(...)`
- `auto_rebuild=True` 时按已写回 case_id 逐个调用 `CandidateCaseService.rebuild_case(...)`

输出字段：

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
- `writeback_items`
- `written_count`
- `written_case_ids`
- `rebuilt_case_ids`
- `items`
- `count`

### 最小 service writeback 示例

```python
write_result = cycle_service.write_openclaw_opinions(
    payload,
    trade_date="2026-04-07",
    expected_round=1,
    expected_agent_id="ashare-research",
    expected_case_ids=["case-20260407-600519-SH"],
    auto_rebuild=True,
)
```

关键输出：

```python
write_result["written_case_ids"] == ["case-20260407-600519-SH"]
write_result["rebuilt_case_ids"] == ["case-20260407-600519-SH"]
write_result["items"][0]["final_status"] == "watchlist"
```

## 3. validator 输入输出 contract

### `validate_opinion_payload`

输入：

- `dict | DiscussionOpinion`
- 可选 `expected_round`
- 可选 `expected_agent_id`

输出：

- `(DiscussionOpinion | None, list[OpinionValidationIssue])`

### `validate_opinion_batch`

输入：

- `list[dict | DiscussionOpinion]`
- 可选 `expected_round`
- 可选 `expected_agent_id`
- 可选 `expected_case_ids`

输出：

- `OpinionBatchValidationResult`

结果字段：

- `ok`
- `normalized_items`
- `issues`
- `covered_case_ids`
- `missing_case_ids`
- `duplicate_keys`
- `substantive_round_2_case_ids`
- `summary_lines`

## 4. summarizer 输入输出 contract

### `summarize_case`

输入：

- `DiscussionCaseRecord | dict`

输出：

- `DiscussionCaseRecord`

会更新的字段：

- `round_1_summary`
- `round_2_summary`
- `risk_gate`
- `audit_gate`
- `final_status`
- `selected_reason`
- `rejected_reason`
- `pool_membership.watchlist`
- `pool_membership.execution_pool`

### `build_trade_date_summary`

输入：

- `cases`
- `trade_date`

输出当前字段：

- `case_count`
- `candidate_pool`
- `selected_count`
- `watchlist_count`
- `rejected_count`
- `risk_gate_counts`
- `audit_gate_counts`
- `round_coverage`
- `substantive_gap_case_ids`
- `controversy_summary_lines`
- `round_2_guidance`
- `summary_lines`
- `summary_text`
- `selected`
- `watchlist`
- `rejected`

### `build_reason_board`

输入：

- `cases`
- `trade_date`

输出当前字段：

- `selected`
- `watchlist`
- `rejected`
- 各组 count

组内 item 字段来自 `build_reason_item`，包含：

- `headline_reason`
- `risk_gate`
- `audit_gate`
- `runtime_snapshot`
- `discussion.selected_points`
- `discussion.questions_for_round_2`
- `discussion.remaining_disputes`
- `discussion.challenge_exchange_lines`
- `discussion.revision_notes`
- `discussion.persuasion_summary`

## 5. finalizer 输入输出 contract

### `build_reply_pack`

输入：

- `trade_date`
- `reason_board`
- 3 个 limit 参数

输出当前字段：

- `overview_lines`
- `debate_focus_lines`
- `challenge_exchange_lines`
- `persuasion_summary_lines`
- `selected_lines`
- `watchlist_lines`
- `rejected_lines`
- `selected_display`
- `watchlist_display`
- `rejected_display`
- `selected`
- `watchlist`
- `rejected`

### `build_final_brief`

输入：

- `trade_date`
- `reply_pack`

输出当前字段：

- `status`
- `blockers`
- `selected_count`
- `watchlist_count`
- `rejected_count`
- `debate_focus_lines`
- `persuasion_summary_lines`
- `lines`
- `summary_text`

### `build_finalize_bundle`

输入：

- `trade_date`
- `cases`
- 可选 `cycle`
- 可选 `execution_precheck`
- 可选 `execution_dispatch`

输出：

- `DiscussionFinalizeBundle`

bundle 当前字段：

- `reason_board`
- `reply_pack`
- `final_brief`
- `client_brief`
- `finalize_packet`

## 6. finalize_packet contract

`finalize_packet` 仍复用现有：

- `protocol.build_finalize_packet_envelope`

当前关键字段：

- `status`
- `blocked`
- `blockers`
- `selected_case_ids`
- `execution_precheck`
- `execution_intents`
- `client_brief`
- `final_brief`
- `reply_pack`
- `shared_context`
