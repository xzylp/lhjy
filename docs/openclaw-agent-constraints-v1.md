# OpenClaw Agent Constraints v1

更新时间：2026-04-07

本文只描述当前仓库已经实现或已经在 helper 中固定下来的 discussion / OpenClaw 约束。

## 1. 当前讨论代理集合

当前讨论主集合固定为：

- `ashare-research`
- `ashare-strategy`
- `ashare-risk`
- `ashare-audit`

显示名映射与当前实现一致：

- `ashare-research` -> `研究`
- `ashare-strategy` -> `策略`
- `ashare-risk` -> `风控`
- `ashare-audit` -> `审计`
- `ashare-runtime` -> `运行`
- `ashare-executor` -> `执行`

其中 `runtime` 和 `executor` 目前不在 discussion 轮次覆盖校验集合内。

## 2. 当前 opinion 必填结构

当前 helper schema 见：

- `src/ashare_system/discussion/contracts.py`
- `src/ashare_system/discussion/opinion_validator.py`

单条 opinion 当前字段为：

- `case_id`
- `round`
- `agent_id`
- `stance`
- `confidence`
- `reasons`
- `evidence_refs`
- `thesis`
- `key_evidence`
- `evidence_gaps`
- `questions_to_others`
- `challenged_by`
- `challenged_points`
- `previous_stance`
- `changed`
- `changed_because`
- `resolved_questions`
- `remaining_disputes`
- `recorded_at`

## 3. 当前 stance 规范

当前合法 stance 以 `contracts.OpinionStance` 为准：

- `support`
- `watch`
- `limit`
- `hold`
- `question`
- `rejected`
- `selected`
- `watchlist`

当前归一化别名规则在 `opinion_validator.normalize_opinion_stance` 中固定：

- `selected` / `select` / `approve` / `allow` / `bullish` 归一到 `support`
- `watchlist` / `neutral` 归一到 `watch`
- `reject` / `oppose` / `against` / `bearish` / `block` 归一到 `rejected`

## 4. Round 1 当前约束

当前 `validate_opinion_payload` 的硬规则：

- `reasons` 至少需要 1 条有效文本，否则报 `reasons_missing`
- `evidence_refs` 为空会报 warning `evidence_refs_missing`
- `agent_id` 不在四个讨论代理内会报 warning `unknown_discussion_agent`

Round 1 当前没有额外强制字段。

## 5. Round 2 当前约束

Round 2 当前不是“重复表态”，而是必须包含实质回应。当前 helper 使用的实质回应字段集合为：

- `challenged_by`
- `challenged_points`
- `questions_to_others`
- `previous_stance`
- `changed`
- `changed_because`
- `resolved_questions`
- `remaining_disputes`
- `thesis`
- `key_evidence`
- `evidence_gaps`

如果 Round 2 opinion 不满足上述任一有效字段，`validate_opinion_payload` 会报：

- `round_2_not_substantive`

## 6. 当前轮次完成标准

当前实现中，一轮 coverage 完成的标准不是“有意见”，而是四个讨论代理都已覆盖：

- 见 `round_summarizer.round_agent_coverage`
- 与 `candidate_case.py` 当前 `_round_agent_coverage` 一致

Round 2 substantive ready 的标准为：

- 四个讨论代理都存在 Round 2 最新 opinion
- 每条 Round 2 opinion 都满足实质回应判定

## 7. 当前 gate 与最终结论约束

当前 helper 的 gate / final status 推导与现有主链一致：

- 风控 gate：`pending | allow | limit | reject`
- 审计 gate：`pending | clear | hold`
- 最终结论：`selected | watchlist | rejected`

关键规则：

- risk 最新 stance 为 `rejected` -> `risk_gate=reject`
- risk 最新 stance 为 `limit/hold/question/watch/watchlist` -> `risk_gate=limit`
- audit 最新 stance 为 `rejected/hold/question` -> `audit_gate=hold`
- `audit_gate=clear` 且 `risk_gate in {allow, limit}` 且至少 2 个 `support/selected` 投票 -> `selected`
- `risk_gate=reject` 必定 `rejected`

## 8. 当前不包含的内容

以下内容本轮没有在 helper 中实现，也不应写进上游提示词：

- 自动调度子代理
- 自动写回 `candidate_case.json`
- 自动驱动 `discussion_service` 状态迁移
- 自动提交 execution intent

这些仍由现有主链决定；本轮 helper 只做可复用 schema、校验、聚合与最终摘要拼装。

