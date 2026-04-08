# Agent 数据使用约定

> 更新时间: 2026-04-06
> 目的: 统一会议模式、子代理说明和数据消费顺序，避免各代理各读一套、各说一套。

## 1. 原则

1. 所有 Agent 优先消费统一数据结构，不直接围绕零散状态文件做判断。
2. 统一数据结构优先级高于自由发挥；自由检索只作为补证手段，不替代正式 serving 数据。
3. Agent 不是已有数据的被动评判者；当证据不足时，可以继续取数、补证、再推理。
4. 所有新增证据必须区分事实、来源时间和推断结论。

## 2. 统一读取顺序

会议模式和候选讨论时，所有代理默认按下面顺序读取：

1. `GET /system/discussions/agent-packets`
   - 这是讨论统一包。
   - 已合并 `case vote detail + dossier + symbol_context + event_context + agent_focus + workspace_context + preferred_read_order`。
2. `GET /data/workspace-context/latest`
   - 用来先看全局状态，决定要下钻到 runtime、discussion、monitor 还是 dossier。
3. `GET /data/catalog`
   - 用来回答“数据在哪”“是否最新”“应该先读哪个接口”。
4. `GET /data/market-context/latest`
5. `GET /data/event-context/latest`
6. `GET /data/symbol-contexts/latest`
7. `GET /data/dossiers/latest`
8. `GET /system/discussions/meeting-context` 或 `GET /data/discussion-context/latest`
9. `GET /data/monitor-context/latest`
10. `GET /data/runtime-context/latest`
11. 若以上仍不足，再按职责补读 runtime / research / strategy / execution 等接口。
12. 若系统内仍无足够证据，允许使用可用工具补检行情、公告、新闻、外部事实源。

## 3. 数据位置

统一存储根目录位于 `ASHARE_STORAGE_ROOT`。

核心目录：

- `raw/market/symbol/`
- `raw/market/index/`
- `raw/market/structure/`
- `raw/events/news/`
- `raw/events/announcements/`
- `raw/events/policy/`
- `normalized/events/`
- `features/market_context/`
- `features/event_context/`
- `features/symbol_context/`
- `features/dossiers/`
- `serving/`

正式 serving 文件：

- `serving/latest_market_context.json`
- `serving/latest_event_context.json`
- `serving/latest_symbol_contexts.json`
- `serving/latest_dossier_pack.json`
- `serving/latest_discussion_context.json`
- `serving/latest_monitor_context.json`
- `serving/latest_runtime_context.json`
- `serving/latest_workspace_context.json`

## 3.1 统一讨论包新增字段

`GET /system/discussions/agent-packets` 现在除 `items / shared_context / data_catalog_ref` 外，还应优先关注：

- `workspace_context`
  - 给子代理一跳可见当前 runtime / discussion / monitor / dossier 总览。
- `workspace_summary_lines`
  - 用于快速浏览，不必每次先额外探测全局接口。
- `preferred_read_order`
  - 告诉子代理在 packet 不足时，下一跳该先读什么。

## 4. 标准上下文

所有会议讨论与子代理推理，优先使用这四类标准对象：

- `market_context`
  - 大盘、指数、市场结构、情绪背景
- `event_context`
  - 新闻、公告、政策、事件聚合结果
- `symbol_context`
  - 个股相对大盘、板块和事件的标准化视图
- `dossier`
  - 面向候选讨论的统一证据包

## 5. 角色建议

- `ashare-research`
  - 优先看 `event_context + dossier + symbol_context`
- `ashare-strategy`
  - 优先看 `dossier + market_context + symbol_context`
- `ashare-risk`
  - 优先看 `market_context + event_context + dossier`
- `ashare-audit`
  - 优先看 `dossier + discussion + event_context + market_context`
- `ashare-runtime`
  - 负责触发候选生成，必要时回答 serving 是否已准备好

## 6. 何时允许继续取数

当出现以下情况时，代理不应停在“现有数据不足”：

- `agent-packets` 缺少关键字段
- dossier 已过期或明显不新鲜
- 重大公告、新闻、盘中行情尚未进入 serving
- 审计、风控、策略提出的关键质疑在统一包中没有证据

此时允许继续：

- 补读系统内部行情、研究、执行、监控接口
- 使用允许的工具检索行情、公告、新闻或外部事实源

但必须满足：

- 标明来源
- 标明时间
- 区分事实和推断
- 回写到 opinion 的 `reasons` / `evidence_refs`

## 7. 会议输出要求

会议讨论输出不是散文，而是结构化 opinion。

每条 opinion 至少包含：

- `case_id`
- `round`
- `agent_id`
- `stance`
- `confidence`
- `reasons`
- `evidence_refs`

若本轮用了外部补证，`evidence_refs` 不能留空。

推荐在 v1 讨论协议下同时补充：

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

## 8. 主持人委派模板

`ashare` 向 `research / strategy / risk / audit / runtime` 下发任务时，统一参考：

- [openclaw-subagent-delegation-templates.md](openclaw-subagent-delegation-templates.md)

特别是 Round 2：

- 不只传 `case_ids`
- 必须一起传 `controversy_summary_lines / round_2_guidance / substantive_gap_case_ids`
- 必须要求子代理返回至少一个“实质回应字段”，否则后端不会视为二轮完成
