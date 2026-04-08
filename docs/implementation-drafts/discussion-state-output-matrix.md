# Discussion State Output Matrix

更新时间：2026-04-07

## 1. 当前状态机

状态机事实来源：

- `src/ashare_system/discussion/state_machine.py`
- `src/ashare_system/discussion/discussion_service.py`

### 1.1 pool_state

- `day_open`
- `base_pool_ready`
- `focus_pool_building`
- `focus_pool_ready`
- `execution_pool_building`
- `execution_pool_ready`
- `execution_pool_blocked`

### 1.2 discussion_state

- `idle`
- `round_1_running`
- `round_1_summarized`
- `round_2_running`
- `final_review_ready`
- `final_selection_ready`
- `final_selection_blocked`

## 2. 当前状态与输出矩阵

### `base_pool_ready` + `idle`

当前可稳定输出：

- candidate pool 列表
- focus pool 初选列表
- discussion cycle snapshot

建议产物：

- `round_summarizer.build_trade_date_summary`

### `focus_pool_building` + `round_1_running`

当前可稳定输出：

- Round 1 case 清单
- OpenClaw 原始 opinion payload
- ingress 标准化后的 writeback batch
- Round 1 opinion batch 校验结果

建议产物：

- `opinion_ingress.adapt_openclaw_opinion_payload(...)`
- `opinion_validator.validate_opinion_batch(..., expected_round=1)`

### `focus_pool_ready` + `round_1_summarized`

当前可稳定输出：

- `round_1_summary`
- `risk_gate`
- `audit_gate`
- `final_status`
- `trade_date summary`
- `reason board`

建议产物：

- `round_summarizer.summarize_case`
- `round_summarizer.build_trade_date_summary`
- `round_summarizer.build_reason_board`

### `execution_pool_building` + `round_2_running`

当前可稳定输出：

- Round 2 target case 列表
- OpenClaw Round 2 原始 opinion payload
- ingress 标准化后的 writeback batch
- Round 2 substantive ready 判定
- controversy / gap lines

建议产物：

- `opinion_ingress.adapt_openclaw_opinion_payload(...)`
- `opinion_validator.validate_opinion_batch(..., expected_round=2)`
- `round_summarizer.round_2_has_substantive_response`

### `execution_pool_building` + `final_review_ready`

当前可稳定输出：

- `reply_pack`
- `final_brief`
- `finalize_bundle`

建议产物：

- `finalizer.build_reply_pack`
- `finalizer.build_final_brief`
- `discussion_service.build_finalize_bundle`

### `execution_pool_ready` + `final_selection_ready`

当前可稳定输出：

- `client_brief`
- `finalize_packet`

建议产物：

- `finalizer.build_finalize_bundle`

### `execution_pool_blocked` + `final_selection_blocked`

当前可稳定输出：

- blocked final brief
- blockers
- watchlist / rejected display
- blocked finalize bundle

建议产物：

- `finalizer.build_finalize_bundle`

## 3. 当前真实接入顺序

当前建议的真实接入顺序是：

1. OpenClaw 返回原始 payload
2. `DiscussionCycleService.adapt_openclaw_opinion_payload(...)`
3. `opinion_ingress.adapt_openclaw_opinion_payload(...)`
4. `DiscussionCycleService.write_openclaw_opinions(...)`
5. `CandidateCaseService.record_opinions_batch(...)`
6. `CandidateCaseService.rebuild_case(...)`
7. `round_summarizer.build_trade_date_summary(...)`
8. `DiscussionCycleService.build_summary_snapshot(...)`
9. `DiscussionCycleService.build_finalize_bundle(...)`
10. `finalizer.build_finalize_bundle(...)`

其中第 2、4、8、9 步是当前 discussion service 层已经存在的最小接入点。

## 4. 当前 finalize 的 blocked 语义

当前 helper 的 blocked 语义来自两个来源：

- `final_brief.status != "ready"`
- 或 `cycle.blockers` 非空

当前 `protocol.build_finalize_packet_envelope` 还会额外用以下条件标记 blocked：

- `blockers` 非空
- `selected_case_ids` 为空
