# 2026-04-20 实盘问题修复任务清单

> 来源：[today_live_tracking_issues_20260420.md](docs/today_live_tracking_issues_20260420.md)
> 创建时间：2026-04-20 15:00

---

## 问题 1：`meeting_state.json` 膨胀 131MB，反噬全部轻接口

**根因**：`StateStore` 把 discussion_context（单日 19.6MB）、gateway 回执、执行历史、supervision 历史全部塞进同一个 JSON 文件，每次 `get()`/`set()` 都对 131MB 文件加 `flock` + 全量读写。

**当前各键体积分布**（实测）：

| 键 | 体积 |
|----|------|
| `latest_discussion_context` | 19.6 MB |
| `discussion_context:2026-04-20` | 19.6 MB |
| 历史 `discussion_context:*` (5天) | 15.9 MB |
| `agent_supervision_history` | 2.5 MB |
| gateway/execution 相关 | < 0.5 MB |

### 任务

- [x] **T1.1** 新建 `discussion_state_store`：把 `discussion_context:*` 和 `latest_discussion_context` 拆到独立文件 `discussion_state.json`
  - 文件：`src/ashare_system/container.py` — 新增 `get_discussion_state_store()` 工厂
  - 文件：`src/ashare_system/scheduler.py` — discussion context 的读写全部改用 `discussion_state_store`
  - 文件：`src/ashare_system/apps/system_api.py` — discussion 相关端点改用新 store
- [x] **T1.2** 新建 `execution_gateway_state_store`：把 gateway pending/receipt/history 拆到独立文件 `execution_gateway_state.json`
  - 文件：`src/ashare_system/container.py` — 新增 `get_execution_gateway_state_store()` 工厂
  - 文件：`src/ashare_system/apps/system_api.py` — `_get_execution_gateway_state_store()` 返回独立 store 而非 meeting_state
  - 文件：`src/ashare_system/execution_gateway.py` — 适配新 store
- [x] **T1.3** discussion_context 按日自动清理：只保留最近 3 天的 `discussion_context:{date}` 键，更早的自动归档到 `archive/` 目录
  - 文件：`src/ashare_system/scheduler.py` — 在盘后任务中增加 `_prune_discussion_context()` 步骤
- [x] **T1.4** `agent_supervision_history` 裁剪：从无限增长改为只保留最近 200 条
  - 文件：`src/ashare_system/supervision_tasks.py` 或 `supervision_state.py` — 写入后截断
- [x] **T1.5** 迁移脚本：首次启动时，自动将已有 `meeting_state.json` 中的 discussion/gateway 键迁移到新文件，并从原文件中删除
  - 文件：新建 `src/ashare_system/infra/state_migration.py`
  - 在 `app.py` 启动 hook 中调用一次
- [ ] **T1.6** 验证：迁移后 `meeting_state.json` 应 < 5MB，轻接口响应 < 200ms
  - 说明：代码侧迁移、拆分、清理链已补齐，但该项需要在真实运行态对现网状态文件和轻接口延迟做再次实测，当前未在文档中虚报。

---

## 问题 2：选股展示残留"基础样本池"占位文案

**根因**：`_pick_reason()` 在 `status == "selected"` 时，优先级写反了——返回 `case.runtime_snapshot.summary`（基础粗筛文案）**优先于**讨论结论。

```python
# 当前代码 (candidate_case.py:1900)
return case.runtime_snapshot.summary or (latest_points or ...)[0]
#      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#      这个 or 链导致只要 runtime_snapshot.summary 非空就直接返回，
#      永远不会走到 latest_points（Agent 讨论结论）
```

### 任务

- [x] **T2.1** 修复 `_pick_reason()` 优先级：讨论结论 > resolved_points > selected_points > runtime_snapshot.summary
  - 文件：`src/ashare_system/discussion/candidate_case.py` L1895-1907
  - 改为：`return (latest_points or case.round_2_summary.resolved_points or case.round_1_summary.selected_points or [case.runtime_snapshot.summary])[0]`
- [x] **T2.2** 修复 `_reason_item()` 中 `headline_reason` 的 fallback 链：同样确保讨论结论优先
  - 文件：`src/ashare_system/discussion/candidate_case.py` L890
  - 当前：`case.selected_reason or case.rejected_reason or case.runtime_snapshot.summary`
  - 改为：先检查 `selected_reason` 是否为讨论产物，如果是 `runtime_snapshot.summary` 原文则降级
- [x] **T2.3** 修复 `_pool_item()` 和 `_summary_case_item()` 中的 `reason` 字段（L840, L861）：同样修正优先级
- [x] **T2.4** 增加单元测试：构造一个 case 同时有 `runtime_snapshot.summary`（旧文案）和 `opinions`（新讨论结论），断言 `headline_reason` 返回讨论结论

---

## 问题 3：`headline_reason` 显示偏差——API 层多处 fallback 不一致

**根因**：`headline_reason` 的计算散落在 6 个位置，fallback 链不统一：

| 位置 | 当前 fallback 链 |
|------|------------------|
| `candidate_case.py:_reason_item` L890 | `selected_reason or rejected_reason or runtime_snapshot.summary` |
| `system_api.py` L1578 | `selected_reason or rejected_reason or runtime_snapshot.summary` |
| `system_api.py` L4518 | `selected_reason or runtime_snapshot.summary` |
| `round_summarizer.py` L207 | `selected_reason or rejected_reason or runtime_snapshot.summary` |
| `finalizer.py` L209 | `headline_reason or selected_reason or rejected_reason` |
| `scheduler.py` L1419 | 直接传入 `signal_reason` |

### 任务

- [x] **T3.1** 统一 `headline_reason` 计算为单一函数：在 `candidate_case.py` 中新增 `@staticmethod resolve_headline_reason(case) -> str`
  - 优先级链：`latest_opinion_reasons > selected_reason > rejected_reason > round_2_resolved > round_1_selected > runtime_snapshot.summary`
- [x] **T3.2** 全部主链调用点改为调用 `resolve_headline_reason()`
  - 文件：`candidate_case.py` L890
  - 文件：`system_api.py` L1578, L4518
  - 文件：`round_summarizer.py` L207
  - 文件：`finalizer.py` L209
  - 文件：`scheduler.py` L1419
  - 说明：`finalizer.py` 继续消费上游已经统一好的 `headline_reason` 字段，不再额外重算；执行侧的 `scheduler.py` 卖出信号继续保留实时信号原因，不强行复用讨论口径。
- [x] **T3.3** `_pool_item()` 和 `_summary_case_item()` 中的 `reason` 字段也统一使用

---

## 问题 4：`go_platform` 执行模式被误判为 `execution_adapter_unavailable`

**根因**：`system_api.py` L5297-5309 的判断逻辑中，当 `execution_plane != "windows_gateway"` 时进入 else 分支。该分支只识别 `execution_mode in {"xtquant", "go_platform", "windows_proxy"}`（L5303），但还有一个兜底 else（L5308-5309）会将**任何未识别的 execution_mode**标记为 `execution_adapter_unavailable`。

实际场景中，`execution_mode` 可能是 `"go_platform"` 但 `execution_plane` 被设为 `"local_xtquant"`（默认值），导致进入了错误分支。

### 任务

- [x] **T4.1** 修复 execution_plane 推断逻辑：当 `execution_mode == "go_platform"` 时，如果 `execution_plane` 仍为默认值 `"local_xtquant"`，自动纠正为 `"go_platform"`
  - 文件：`src/ashare_system/apps/system_api.py` L5261 附近
  - 在读取 `execution_plane` 后增加：`if execution_mode == "go_platform" and execution_plane == "local_xtquant": execution_plane = "go_platform"`
- [x] **T4.2** 在 `execution_mode` 兜底分支增加日志告警，而非静默返回 unavailable
  - 文件：`src/ashare_system/apps/system_api.py` L5308-5309
  - 改为 `logger.warning("未识别的 execution_mode: %s", execution_mode)` + 标记 `execution_mode_unknown` 而非 `execution_adapter_unavailable`
- [x] **T4.3** healthcheck 中 `go_platform` 的优先级检测要包含探活
  - 文件：`src/ashare_system/infra/healthcheck.py` L60-63
  - 增加 `go_platform.enabled and go_platform.base_url` 可达性检查

---

## 问题 5：盯盘链有动作但无交易价值

**根因**：盯盘任务（持仓快巡、持仓深巡、盯盘巡检、微观巡检）的输出仅生成 opinion 写回和 `meeting_state` 记录，但没有触发**盘中执行意图**（`execution_intent`）的生成链路。当前只有 discussion 终审后的 finalize 才会生成 intent。

### 任务

- [x] **T5.1** 在盯盘巡检输出中增加 `intraday_signal` 类型：当检测到持仓股跌破止损/止盈线时，生成 `SELL` 类型的 `execution_intent`
  - 文件：`src/ashare_system/scheduler.py` — 在 `_run_position_watch_task()` 的输出处理中增加意图生成
  - 条件触发：`next_day_close_pct < -3%`（深跌），或 `intraday_return > +8%`（冲高回落预警）
- [x] **T5.2** 在微观巡检中增加盘中新机会捕捉：当发现非持仓股出现涨停基因（涨幅 > 5% + 成交量放大 2x + 板块联动）时，生成 `BUY` 类型候选提案
  - 文件：`src/ashare_system/scheduler.py` — 在 `_run_micro_inspection_task()` 中增加候选注入
  - 注入到 `candidate_cases` 而非直接生成 intent（仍需走讨论流程）
- [x] **T5.3** 增加盘中快速讨论通道：针对 T5.1 的止损信号，跳过完整 2 轮讨论，直接由 risk agent 单独审批后生成 intent
  - 文件：`src/ashare_system/discussion/discussion_service.py` — 新增 `fast_track_exit()` 方法
  - 只需 `ashare-risk` 立场为 `reject`（对持仓）即可触发卖出 intent
- [x] **T5.4** 在盘中巡检总结通知中增加"交易动作建议"字段，而非仅展示巡检结论
  - 文件：`src/ashare_system/notify/templates.py` — 通知模板增加 `action_suggestions` 区块

---

## 执行优先级

```
紧急（今日/明日）
  T1.1 + T1.2 — 拆分状态文件，立即解除 131MB 锁竞争
  T2.1        — 修复 _pick_reason 优先级反转（一行代码）
  T4.1        — 修复 go_platform 执行模式误判

高优（本周内）
  T1.3 + T1.4 — 清理与裁剪
  T3.1 + T3.2 — 统一 headline_reason 计算
  T2.4        — 补充测试覆盖

中优（下周）
  T1.5        — 迁移脚本
  T5.1 + T5.3 — 盯盘止损联动
  T4.2 + T4.3 — 执行模式告警增强

低优（后续迭代）
  T5.2 + T5.4 — 盘中新机会捕捉 + 通知增强
  T1.6        — 最终验证
```

---

## 涉及文件索引

| 文件 | 关联任务 |
|------|---------|
| `src/ashare_system/container.py` | T1.1, T1.2 |
| `src/ashare_system/scheduler.py` | T1.1, T1.3, T5.1, T5.2 |
| `src/ashare_system/apps/system_api.py` | T1.1, T1.2, T3.2, T4.1, T4.2 |
| `src/ashare_system/discussion/candidate_case.py` | T2.1, T2.2, T2.3, T2.4, T3.1, T3.3 |
| `src/ashare_system/discussion/round_summarizer.py` | T3.2 |
| `src/ashare_system/discussion/finalizer.py` | T3.2 |
| `src/ashare_system/execution_gateway.py` | T1.2 |
| `src/ashare_system/supervision_tasks.py` | T1.4 |
| `src/ashare_system/infra/healthcheck.py` | T4.3 |
| `src/ashare_system/infra/state_migration.py` | T1.5（新建）|
| `src/ashare_system/app.py` | T1.5 |
| `src/ashare_system/discussion/discussion_service.py` | T5.3 |
| `src/ashare_system/notify/templates.py` | T5.4 |

---

## 本次整改落地结果

### 已完成改造

- `container.py` / `app.py` / `state_migration.py` 已把 `discussion_state.json`、`execution_gateway_state.json` 正式接入启动链，并在应用与调度器启动前执行历史状态迁移。
- `system_api.py` 已改为 discussion 相关端点优先读写 `discussion_state_store`，gateway pending/receipt/history 改走 `execution_gateway_state_store`，同时修复了 `build_router()` 参数签名与 `create_app()` 调用不一致的问题。
- `candidate_case.py` / `round_summarizer.py` / `system_api.py` 已统一 `headline_reason` 主链，`selected` 场景不再被 `runtime_snapshot.summary` 的“基础样本池”文案覆盖。
- `scheduler.py` 已补齐：
  - `discussion_context` 独立加载与盘后归档裁剪；
  - `agent_supervision_history` 保留最近 200 条；
  - `fast_position_watch` / `position_watch` 卖出信号增加 `intraday_signal`、`fast_track_review`、`action_suggestions`；
  - `fast_opportunity_scan` 增加 5m 量比、板块联动判断；
  - `task_check_micro` / `task_fast_position_watch` 会把满足“涨幅>5% + 5m量比>=2 + 板块联动”的机会票注入 `candidate_cases`。
- `discussion_service.py` 新增 `fast_track_exit()`，由 `ashare-risk` 直接写入 reject 立场，为秒级卖出链提供可追溯审批痕迹。
- `notify/templates.py` 新增盘中巡视通知模板，明确输出“交易动作建议”。
- `healthcheck.py` 已新增 `go_platform` `/health` 探活检查。

### 测试与验证

- 通过：`./.venv/bin/python -m unittest tests.test_upgrade_workflow.UpgradeWorkflowTests.test_headline_reason_prefers_latest_discussion_reason_over_runtime_summary`
- 通过：`./.venv/bin/python -m unittest tests.test_upgrade_workflow.UpgradeWorkflowTests.test_migrate_legacy_state_files_moves_discussion_and_gateway_keys`
- 通过：`./.venv/bin/python -m unittest tests.test_upgrade_workflow.UpgradeWorkflowTests.test_finalize_cycle_auto_dispatches_live_windows_gateway_intents`
- 通过：`./.venv/bin/python -m unittest tests.test_upgrade_workflow.UpgradeWorkflowTests.test_finalize_cycle_auto_dispatch_refreshes_discussion_context_status`
- 通过：`./.venv/bin/python -m py_compile src/ashare_system/apps/system_api.py src/ashare_system/scheduler.py src/ashare_system/discussion/discussion_service.py src/ashare_system/discussion/round_summarizer.py src/ashare_system/infra/healthcheck.py src/ashare_system/notify/templates.py tests/test_upgrade_workflow.py`

### 仍需真实运行态复验

- `T1.6` 尚未在当前文档中宣称完成。需要真实运行后再次实测 `meeting_state.json` 实际体积、轻接口延迟，以及盘中 `action_suggestions` 与 `fast_track_exit` 的连续触发效果。
