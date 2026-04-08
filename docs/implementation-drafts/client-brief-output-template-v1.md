# Client Brief Output Template v1

更新时间：2026-04-07

本文对应当前：

- `src/ashare_system/discussion/client_brief.py`
- `src/ashare_system/discussion/finalizer.py`

## 1. 当前 client brief 生成链路

当前 helper 生成顺序是：

1. `build_reason_board`
2. `build_reply_pack`
3. `build_final_brief`
4. `build_client_brief_payload`
5. `build_finalize_packet_envelope`

## 2. 当前 client brief 顶层字段

当前 `build_client_brief_payload` 输出字段包括：

- `trade_date`
- `status`
- `blockers`
- `selected_count`
- `watchlist_count`
- `rejected_count`
- `selected_display`
- `watchlist_display`
- `rejected_display`
- `overview_lines`
- `debate_focus_lines`
- `challenge_exchange_lines`
- `persuasion_summary_lines`
- `selected_lines`
- `watchlist_lines`
- `rejected_lines`
- `execution_precheck_lines`
- `execution_dispatch_lines`
- `execution_precheck_status`
- `execution_precheck_available`
- `execution_precheck_approved_count`
- `execution_precheck_blocked_count`
- `execution_dispatch_available`
- `execution_dispatch_status`
- `execution_dispatch_submitted_count`
- `execution_dispatch_preview_count`
- `execution_dispatch_blocked_count`
- `execution_dispatch_receipt_count`
- `lines`
- `summary_text`
- `reply_pack`
- `final_brief`
- `execution_precheck`
- `execution_dispatch`
- 可选 `cycle`

## 3. 当前 lines 组织顺序

当前 `lines` 的拼接顺序固定为：

1. `overview_lines`
2. 若有 `debate_focus_lines`，插入标题 `争议焦点:`
3. 若有 `challenge_exchange_lines`，插入标题 `关键交锋:`
4. 若有 `persuasion_summary_lines`，插入标题 `讨论收敛:`
5. 若 `final_brief.status == "ready"`，插入标题 `最终推荐:`
6. 否则优先输出 `当前观察池:` 或 `当前淘汰池:`
7. 若有预检，插入标题 `执行预检:`
8. 若有执行回执，插入标题 `执行回执:`

## 4. 当前 ready / blocked 语义

当前 `final_brief.status` 的规则很简单：

- 只要 `reply_pack.selected` 非空，则 `ready`
- 否则 `blocked`

blocked 时默认 blocker：

- `no_selected_candidates`

## 5. 当前最小输出样例

```json
{
  "trade_date": "2026-04-07",
  "status": "ready",
  "blockers": [],
  "selected_count": 2,
  "watchlist_count": 1,
  "rejected_count": 3,
  "overview_lines": [
    "交易日 2026-04-07，候选 6 只，入选 2 只，观察 1 只，淘汰 3 只。",
    "当前优先执行池关注 贵州茅台, 宁德时代。"
  ],
  "debate_focus_lines": [
    "600519.SH 贵州茅台：量能确认是否足够"
  ],
  "selected_lines": [
    "600519.SH 贵州茅台 | 排名=1 | 分数=92.0 | 风控=allow 审计=clear | 理由=强势候选"
  ],
  "lines": [
    "交易日 2026-04-07，候选 6 只，入选 2 只，观察 1 只，淘汰 3 只。",
    "当前优先执行池关注 贵州茅台, 宁德时代。",
    "争议焦点:",
    "600519.SH 贵州茅台：量能确认是否足够",
    "最终推荐:",
    "600519.SH 贵州茅台 | 排名=1 | 分数=92.0 | 风控=allow 审计=clear | 理由=强势候选"
  ],
  "summary_text": "..."
}
```

