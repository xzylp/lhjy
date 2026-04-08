# OpenClaw Agent Routing Matrix v1

更新时间：2026-04-07

本文只记录当前仓库已经存在的角色分工和当前 helper 的真实接入顺序。

## 1. 当前路由角色

### `ashare`

职责：

- 作为主持入口
- 负责分发 case 集合
- 汇总 opinion
- 选择何时调用 summarizer / finalizer helper

当前本轮不负责：

- 自动落库 candidate_case
- 自动调 discussion 状态机

### `ashare-research`

输入重点：

- case 基础信息
- shared context
- dossier / 研究事件证据引用

输出重点：

- `DiscussionOpinion`
- thesis / key_evidence / evidence_gaps

### `ashare-strategy`

输入重点：

- runtime_snapshot
- score / rank / action
- 其他代理质疑点

输出重点：

- 排序逻辑
- selected / watch / rejected 倾向

### `ashare-risk`

输入重点：

- 价格 / 执行 / 风险限制
- 争议 case 的约束解除条件

输出重点：

- support / limit / rejected
- 限制条件与解除条件

### `ashare-audit`

输入重点：

- 上述所有代理的论证闭环

输出重点：

- 证据缺口
- 未回应问题
- 审计 hold 的原因

## 2. 当前 helper 接入顺序

当前推荐顺序不是直接 `validator -> writeback`，而是：

1. `DiscussionCycleService.adapt_openclaw_opinion_payload(...)`
2. `opinion_ingress.adapt_openclaw_opinion_payload(...)`
3. `opinion_validator.validate_opinion_batch(...)`
4. `DiscussionCycleService.write_openclaw_opinions(...)`
5. `CandidateCaseService.record_opinions_batch(...)`
6. `CandidateCaseService.rebuild_case(...)`
7. `round_summarizer.build_trade_date_summary(...)`
8. `DiscussionCycleService.build_summary_snapshot(...)`
9. `DiscussionCycleService.build_finalize_bundle(...)`
10. `finalizer.build_finalize_bundle(...)`

其中第 1 步会先按 `trade_date` 自动构建 `symbol -> case_id` 映射，再下沉到第 2 步；第 4 步会继续复用 `CandidateCaseService.record_opinions_batch(...)` / `rebuild_case(...)`，所以入口侧不需要再手工拼 `case_id_map` 和 writeback。

```python
adapter_result = cycle_service.adapt_openclaw_opinion_payload(
    payload,
    trade_date=trade_date,
    expected_round=round_number,
    expected_agent_id=agent_id,
    expected_case_ids=case_ids,
)

write_result = cycle_service.write_openclaw_opinions(
    payload,
    trade_date=trade_date,
    expected_round=round_number,
    expected_agent_id=agent_id,
    expected_case_ids=case_ids,
    auto_rebuild=True,
)
```

最小示例：

```python
payload = {
    "output": {
        "items": [
            {
                "symbol": "600519.SH",
                "stance": "selected",
                "reasons": ["研究支持"],
                "evidence_refs": ["news:1"],
            }
        ]
    }
}
adapter_result = cycle_service.adapt_openclaw_opinion_payload(
    payload,
    trade_date="2026-04-07",
    expected_round=1,
    expected_agent_id="ashare-research",
    expected_case_ids=["case-20260407-600519-SH"],
)

write_result = cycle_service.write_openclaw_opinions(
    payload,
    trade_date="2026-04-07",
    expected_round=1,
    expected_agent_id="ashare-research",
    expected_case_ids=["case-20260407-600519-SH"],
    auto_rebuild=True,
)
```

这一段已经能把：

- 原始 `output.items`
- 缺失的 `round`
- 缺失的 `agent_id`
- `trade_date -> symbol -> case_id`
- `selected -> support`
- batch writeback
- case rebuild

全部收口成主链可直接写回的 `(case_id, CandidateOpinion)`。

## 3. 当前 helper 接入路由

### 路由到 opinion ingress

场景：

- 子代理返回 opinion JSON 后
- payload 形态可能是 list / `opinions` / `items` / `output`

调用：

```python
adapter_result = cycle_service.adapt_openclaw_opinion_payload(
    payload,
    trade_date="2026-04-07",
    expected_round=1,
    expected_agent_id="ashare-research",
    expected_case_ids=case_ids,
)
```

输出：

- `case_id_map`
- `default_case_id`
- `normalized_payloads`
- `normalized_items`
- `issues`
- `writeback_items`

### 路由到 service writeback helper

场景：

- 入口已经拿到 OpenClaw 原始 payload
- 希望 service 一次性完成 normalize + validate + writeback + rebuild

调用：

```python
write_result = cycle_service.write_openclaw_opinions(
    payload,
    trade_date="2026-04-07",
    expected_round=1,
    expected_agent_id="ashare-research",
    expected_case_ids=case_ids,
    auto_rebuild=True,
)
```

输出：

- `case_id_map`
- `default_case_id`
- `writeback_items`
- `written_count`
- `written_case_ids`
- `rebuilt_case_ids`
- `items`

### 路由到 validator

场景：

- 只想校验已经标准化好的 opinion 列表
- 或需要单独调用 validator 观察问题明细

调用：

```python
validate_opinion_batch(
    payloads,
    expected_round=1,
    expected_agent_id="ashare-research",
    expected_case_ids=case_ids,
)
```

### 路由到 summarizer

场景：

- 一轮 opinion 收齐后
- 需要生成 trade_date summary / reason board

调用：

```python
summary = build_trade_date_summary(cases, trade_date)
board = build_reason_board(cases, trade_date)
```

### 路由到 finalizer

场景：

- discussion 已收敛，准备拼 reply pack / final brief / client brief

调用：

```python
bundle = build_finalize_bundle(
    trade_date=trade_date,
    cases=cases,
    cycle=cycle_payload,
    execution_precheck=execution_precheck,
    execution_dispatch=execution_dispatch,
)
```

## 4. 当前未做自动路由的部分

本轮没有实现：

- OpenClaw 网关自动按 state 派发到具体代理
- `ashare` 自动读取系统 endpoint 并触发 helper

所以当前 routing matrix 的含义是“主持人应该如何调用 service/helper”，不是“系统已自动从网关接入主链”。
