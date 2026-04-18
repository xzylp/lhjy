# OpenClaw 子代理委派模板

> 更新时间: 2026-04-06
> 目的: 固定 `ashare -> research / strategy / risk / audit / runtime` 的委派口径，避免运行时退回“泛泛要求返回 JSON 数组”。

## 1. 总原则

- 主持人 `ashare` 只做分发、归档、收敛，不代替专业判断。
- 所有讨论任务默认先把以下上下文传给子代理：
  - `trade_date`
  - `round`
  - `case_ids`
  - `agent-packets.items`
  - `shared_context`
  - `workspace_context`
  - `preferred_read_order`
- Round 2 必须额外传：
  - `controversy_summary_lines`
  - `round_2_guidance`
  - `substantive_gap_case_ids`
- 子代理输出必须是 JSON 数组，不要散文。

## 2. 标准 Opinion 字段

所有讨论类子代理统一输出：

```json
[
  {
    "case_id": "case-20260406-600000-SH",
    "round": 2,
    "agent_id": "ashare-strategy",
    "stance": "support",
    "confidence": "high",
    "reasons": ["..."],
    "evidence_refs": ["..."],
    "thesis": "...",
    "key_evidence": ["..."],
    "evidence_gaps": ["..."],
    "questions_to_others": ["..."],
    "challenged_by": ["ashare-risk"],
    "challenged_points": ["仓位限制是否影响胜出顺序"],
    "previous_stance": "watch",
    "changed": true,
    "changed_because": ["风险条件已澄清"],
    "resolved_questions": ["排序胜出逻辑已补强"],
    "remaining_disputes": []
  }
]
```

## 3. Round 1 模板

### 3.1 Runtime 候选生成

```text
请运行当日候选生成，并只返回一个 JSON 对象。

要求：
1. 调用 runtime/pipeline 或现有运行入口。
2. 确认 candidate_case 已落库。
3. 不写散文。

输出字段固定为：
- status
- trade_date
- job_id
- case_ids
- blockers
```

### 3.2 Research Round 1

```text
基于给定 trade_date、case_ids、agent-packets.items、shared_context、workspace_context，返回 ashare-research 的 Round 1 opinion JSON 数组。

要求：
1. 覆盖全部给定 case_id。
2. 每条 opinion 至少包含 1 条 thesis、2 条关键证据、1 条证据缺口或不确定项。
3. reasons/evidence_refs 不可空。
4. 只输出 JSON 数组。
```

### 3.3 Strategy Round 1

```text
基于给定 trade_date、case_ids、agent-packets.items、shared_context、workspace_context，返回 ashare-strategy 的 Round 1 opinion JSON 数组。

要求：
1. 覆盖全部给定 case_id。
2. 必须写出排序理由或落选理由。
3. 至少指出 1 个可能削弱当前排序结论的风险点或待验证点。
4. 只输出 JSON 数组。
```

### 3.4 Risk Round 1

```text
基于给定 trade_date、case_ids、agent-packets.items、shared_context、workspace_context，返回 ashare-risk 的 Round 1 opinion JSON 数组。

要求：
1. 覆盖全部给定 case_id。
2. stance 只用 support/watch/limit/reject。
3. 若给出 limit/reject，必须写可解除条件。
4. 只输出 JSON 数组。
```

### 3.5 Audit Round 1

```text
基于给定 trade_date、case_ids、agent-packets.items、shared_context、workspace_context，返回 ashare-audit 的 Round 1 opinion JSON 数组。

要求：
1. 覆盖全部给定 case_id。
2. 重点指出证据缺口、逻辑断层、未回应问题。
3. 不承担排序职责。
4. 只输出 JSON 数组。
```

## 4. Round 2 模板

Round 2 是争议讨论，不是重复表态。四个讨论子代理都必须满足：

- 只覆盖 `round_2_target_case_ids`
- 必须读取 `controversy_summary_lines` 与 `round_2_guidance`
- 每条 opinion 至少补齐以下字段之一：
  - `challenged_by`
  - `challenged_points`
  - `previous_stance`
  - `changed`
  - `changed_because`
  - `resolved_questions`
  - `remaining_disputes`

### 4.1 Research Round 2

```text
你现在参与 Round 2 争议讨论。请仅针对 round_2_target_case_ids 返回 ashare-research 的 JSON opinion 数组。

你必须：
1. 逐条回应 controversy_summary_lines / round_2_guidance。
2. 说明回应了谁的质疑。
3. 说明补到了哪些新增研究证据。
4. 若观点变化，写 previous_stance / changed / changed_because。
5. 若仍无法解决，写 remaining_disputes。

禁止：
- 重复 Round 1 reasons
- 只写“继续支持”而不解释回应内容
```

### 4.2 Strategy Round 2

```text
你现在参与 Round 2 争议讨论。请仅针对 round_2_target_case_ids 返回 ashare-strategy 的 JSON opinion 数组。

你必须：
1. 逐条回应 controversy_summary_lines / round_2_guidance。
2. 明确谁挑战了你的排序逻辑。
3. 说明是否接受对方部分观点。
4. 说明排序是否修正，以及修正原因。
5. 若仍保留原判断，也要写 resolved_questions 或 remaining_disputes。
```

### 4.3 Risk Round 2

```text
你现在参与 Round 2 争议讨论。请仅针对 round_2_target_case_ids 返回 ashare-risk 的 JSON opinion 数组。

你必须：
1. 逐条回应 controversy_summary_lines / round_2_guidance。
2. 明确当前限制条件是否已满足。
3. 若 stance 从 limit/reject 调整，写 previous_stance / changed / changed_because。
4. 若仍未放行，写 remaining_disputes 和可解除条件。
```

### 4.4 Audit Round 2

```text
你现在参与 Round 2 争议讨论。请仅针对 round_2_target_case_ids 返回 ashare-audit 的 JSON opinion 数组。

你必须：
1. 逐条检查 controversy_summary_lines / round_2_guidance 是否被正面回应。
2. 明确哪些证据缺口已关闭。
3. 明确哪些问题仍未关闭。
4. 不替代 strategy 排名，也不替代 risk 放行。
```

## 5. `ashare` 推荐委派写法

### 5.0 最小稳定参数原则

对 `runtime:"subagent"` 的 `sessions_spawn`，默认只使用最小参数集：

```text
agentId
label
mode:"run"
runtime:"subagent"
task
```

只有明确需要固定模型时，才额外传 `model`。

不要把 ACP 语义或空默认值混进普通子代理调用，尤其不要追加：

- `streamTo`
- `attachAs`
- `attachments`
- `cleanup`
- `cwd`
- `lightContext`
- `resumeSessionId`
- `runTimeoutSeconds`
- `timeoutSeconds`
- `thread`
- `sandbox`

原因：

1. 官方文档明确 `streamTo` 只适用于 ACP，不适用于 `runtime:"subagent"`。
2. 额外可选键越多，模型越容易生成“看起来完整、实际漂移”的工具调用。
3. 盘中值班链路追求稳定收敛，优先保证子代理能稳定启动和返回结构化结果，而不是追求花哨的流式转发。
4. 当前 OpenClaw `2026.4.10` 的 `sessions_spawn` schema 把 ACP 与 subagent 参数放在同一个工具定义里，执行时才对非法组合报错；因此不能因为工具面板里“看得到这些字段”就把它们填进去。
5. 如果一次调用已经报 `streamTo is only supported for runtime=acp; got runtime=subagent`，不要再用空字符串、`0`、空对象去做“去参数重试”；这类重试仍可能被同一工具层继续补回坏参数。

## 5.1 Round 1

```text
sessions_spawn({
  agentId:"ashare-research",
  label:"round1-research",
  mode:"run",
  runtime:"subagent",
  task:"基于给定 trade_date、case_ids、agent-packets.items、shared_context、workspace_context，返回 ashare-research 的 Round 1 opinion JSON 数组。覆盖全部 case_id；每条至少 1 条 thesis、2 条关键证据、1 条证据缺口；只输出 JSON 数组。"
})
```

## 5.2 Round 2

```text
sessions_spawn({
  agentId:"ashare-risk",
  label:"round2-risk",
  mode:"run",
  runtime:"subagent",
  task:"你现在参与 Round 2 争议讨论。仅针对 round_2_target_case_ids 返回 ashare-risk 的 JSON opinion 数组。必须逐条回应 controversy_summary_lines / round_2_guidance，明确限制条件是否已满足；若 stance 变化，写 previous_stance / changed / changed_because；若仍未放行，写 remaining_disputes 和可解除条件；只输出 JSON 数组。"
})
```

## 6. 主持人自检清单

在 `ashare` 写入 `POST /system/discussions/opinions/batch` 之前，自检：

1. 是否只对目标 case 发起了 Round 2。
2. 是否把 `controversy_summary_lines / round_2_guidance / substantive_gap_case_ids` 传给了子代理。
3. 返回的每条 Round 2 opinion 是否包含至少一个“实质回应字段”。
4. 是否仍有纯口号式内容，例如“继续支持”“审计通过”“风险已澄清”但没有回应对象和回应内容。
5. 若 `finalize` 返回 `discussion_not_ready`，是否先补齐二轮结构化回应，而不是误判为程序故障。
6. `runtime:"subagent"` 的 `sessions_spawn` 是否严格保持最小参数集，没有混入 `streamTo` 或其他 ACP / 空默认值参数。
7. 若第一次已收到 `streamTo ... runtime=subagent` 报错，是否立即把它升级为工具层阻断，而不是继续对子角色做空值重试。
