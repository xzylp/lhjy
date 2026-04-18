# 正式上线前压测场景矩阵

> 更新日期：2026-04-15
> 目标：把正式上线前必须验证的场景、入口、关键证据与通过标准固定下来，后续按此逐项回填，不凭印象判断“已经稳定”。

---

## 1. 使用原则

- 所有场景先走 `preview` 或只读验证，除非已进入受控 `apply=true` 小额演练窗口。
- 所有场景都要留前后证据，统一通过 `scripts/collect_recovery_evidence.sh` 或 `scripts/run_recovery_pressure_sequence.sh` 归档。
- 主链摘要必须来自真实接口，不允许编造行情、讨论结果或执行回执。
- 若主链 OpenClaw 不稳，必须补 Hermes backup 的同窗验证记录。
- 进入真实压测窗口前，先统一执行：

```bash
bash scripts/check_go_live_gate.sh
```

该脚本会一次汇总：

- Linux 服务 / 飞书长连接恢复态
- Windows 执行桥准入态
- `apply=true` 真闭环证据是否已经形成
- 主备 agent 值班链是否仍可读
- 飞书重要消息与监督链是否处于可送达状态

---

## 2. 场景分层

### 2.1 时间维度

1. 盘前：关注 readiness、候选准备、飞书简报、主备 agent 值班状态。
2. 盘中：关注 runtime 产出、讨论推进、监督催办、执行预演、执行桥健康。
3. 盘后：关注复盘摘要、参数提案、执行回执、审计与学习结果。
4. 夜间：关注研究值班、夜间沙盘、次日优先级产物、主备 gateway 稳定性。

### 2.2 故障维度

1. 正常链路
2. Linux 服务重启
3. Windows 执行桥不可达
4. 飞书长连接掉线
5. OpenClaw 主链失稳
6. 无候选 / 无 selected
7. execution blocked
8. preview 与 apply 差异

---

## 3. 核心矩阵

### 3.1 盘前

- 场景：正常链路
  - 入口：`/system/readiness`、`/system/feishu/briefing`、`/system/workspace-context`
  - 证据：`health`、`readiness`、`workspace_context`、`feishu_briefing`
  - 通过标准：readiness 非 `blocked`；飞书简报可生成；workspace context 未过期

- 场景：Linux 服务重启后恢复
  - 入口：`scripts/run_recovery_pressure_sequence.sh --components control-plane,scheduler,feishu`
  - 证据：前后 `service-recovery-readiness`、`operations/components`、`monitor/state`
  - 通过标准：control plane / scheduler / feishu 长连接恢复；监督板与问答入口可读

- 场景：OpenClaw 主链失稳，Hermes 备链兜底
  - 入口：OpenClaw gateway 状态、Hermes backup gateway 状态、真实摘要输出
  - 证据：主链失败记录、备链摘要、备链任务落盘
  - 通过标准：主链失败时，备链能继续产出真实控制面摘要

### 3.2 盘中

- 场景：正常链路
  - 入口：`/runtime/jobs/pipeline`、`/system/discussions/summary`、`/system/agents/supervision-board`
  - 证据：runtime 候选、discussion summary、supervision board、execution preview
  - 通过标准：候选与讨论能推进；监督板根据真实产出判断 Agent 是否迟滞，而非机械催 runtime

- 场景：Windows 执行桥不可达
  - 入口：`/monitor/state`、`/system/deployment/service-recovery-readiness`
  - 证据：execution bridge health、service recovery readiness、dispatch preview 失败原因
  - 通过标准：系统明确阻断 `apply`；机器人或简报能给出真实阻断原因；不得伪造已恢复

- 场景：无候选 / 无 selected
  - 入口：`/system/discussions/cycles/*`、`/system/discussions/execution-intents`
  - 证据：cycle detail、summary、blockers、execution intents
  - 通过标准：状态口径一致；能区分“市场确无机会”和“链路未推进”

- 场景：execution blocked
  - 入口：`/system/discussions/execution-intents`、`/system/discussions/execution-dispatch/latest`
  - 证据：blocked 条目、阻断原因、风控阈值命中项
  - 通过标准：blocked 原因可追溯；不生成误导性“已提交”表述

### 3.3 盘后

- 场景：正常链路
  - 入口：`/system/reports/runtime`、`/system/research/summary`、`/system/meetings/latest`
  - 证据：盘后摘要、学习摘要、会议纪要、参数提案
  - 通过标准：至少能产出真实复盘或学习摘要；参数提案可追溯到事实依据

- 场景：飞书掉线恢复
  - 入口：`/system/feishu/longconn/status`、飞书机器人收发消息
  - 证据：长连接状态、心跳时间、恢复后的回执消息
  - 通过标准：掉线后可恢复为 `connected + is_fresh=true`；重要业务消息可恢复送达

### 3.4 夜间

- 场景：研究值班与沙盘
  - 入口：夜间 cron、Hermes/OpenClaw 值班、`/system/workflow/mainline`
  - 证据：夜间任务输出、学习纪要、sandbox 结果
  - 通过标准：夜间不是空转；至少有研究/学习/沙盘其一的真实产出

---

## 4. preview 与 apply 的分层验收

- `preview`
  - 目标：验证 runtime -> discussion -> execution-intents -> dispatch preview 链路完整
  - 必要证据：候选、selected、execution pool、preview 回执、飞书通知
  - 未通过表现：无 candidate、无 selected、execution intent 为 0、preview reason 不清楚

- `apply`
  - 目标：验证真实小额报单 -> 回执 -> 对账 -> 飞书通知 -> 风控留痕
  - 必要证据：apply 准入检查、真实报单回执、账户变化、飞书回执、审计记录
  - 强约束：仅限受控窗口、小额、白名单标的、人工知情；Windows 执行桥异常时必须阻断

---

## 5. 当前回填状态

- 已有：
  - 恢复检查接口
  - apply 准入接口
  - 证据收集脚本
  - 恢复编排脚本
  - `preview` 真链闭环证据

- 仍缺：
  - 真实 `apply=true` 小额闭环证据
  - Windows 执行桥真实恢复压测证据
  - OpenClaw 主链失稳时 Hermes 备链接管的同窗证据
  - 盘前 / 盘后 / 夜间场景逐项回填
