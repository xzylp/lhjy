# 服务重启 / 断链恢复压测方案

> 更新日期：2026-04-15
> 目标：把 `serve / scheduler / feishu-longconn / Windows 执行桥 / OpenClaw / Hermes` 的恢复演练流程固定下来，并明确每一步必须留存的证据。

---

## 1. 压测目标

本轮不验证收益，也不新增真实交易动作。

只验证三件事：

1. 服务重启后，控制面能否恢复到“可值班”状态。
2. 断链后，飞书、监督、问答、执行预演这些只读/预演链路能否恢复。
3. 主用 agent 链失稳时，备用链能否接管“读真实控制面并给出业务摘要”。

---

## 2. 演练对象

Linux 侧：

- `ashare-system-v2.service`
- `ashare-system-v2-scheduler.service`
- `ashare-feishu-longconn.service`
- OpenClaw gateway
- Hermes backup gateway

跨机链路：

- Windows `18791` gateway
- QMT VM reachability / receipt 回写链

---

## 3. 演练前固定口径

- 不做 `apply=true`
- 不修改仓位参数
- 所有验证先走只读或 `preview`
- 每一步都先执行证据收集脚本，再做动作，再收一次证据

统一前置检查：

- `bash scripts/check_service_recovery_readiness.sh`
- `bash scripts/check_apply_pressure_readiness.sh`

若任一检查已是 `BLOCKED`，先记录现状，再决定是否继续演练；不得编造成“恢复成功”。

---

## 4. 证据收集入口

统一使用：

```bash
bash scripts/collect_recovery_evidence.sh --tag before_restart
```

如需按标准顺序执行整轮恢复演练，优先使用编排脚本：

```bash
# 安全演练：默认 dry-run，只做前后证据留档和动作提示
bash scripts/run_recovery_pressure_sequence.sh

# 仅演练 Linux 三个核心服务
bash scripts/run_recovery_pressure_sequence.sh --components control-plane,scheduler,feishu

# 显式执行 Linux 侧 restart；OpenClaw/Hermes 仍以状态验证为主
bash scripts/run_recovery_pressure_sequence.sh --execute --components control-plane,scheduler,feishu,openclaw,hermes
```

脚本会归档这些证据：

- `/health`
- `/system/operations/components`
- `/system/readiness`
- `/system/deployment/service-recovery-readiness`
- `/system/deployment/controlled-apply-readiness`
- `/system/feishu/longconn/status`
- `/system/workspace-context`
- `/system/agents/supervision-board`
- `/system/feishu/briefing`
- `/system/discussions/execution-dispatch/latest`
- `/monitor/state`
- `systemctl --user status ashare-feishu-longconn.service --no-pager`
- `bash scripts/ashare_feishu_longconn_ctl.sh --user verify`

输出目录默认：

- `logs/recovery_evidence/<timestamp>_<tag>/`
- 编排脚本运行说明：`logs/recovery_evidence/<run_id>_sequence/`

编排脚本设计口径：

- 默认 `dry-run`，不会擅自执行 live restart。
- 仅在显式 `--execute` 时，才会调用 Linux 侧 `ashare_service.sh / ashare_scheduler_service.sh / ashare_feishu_longconn_ctl.sh restart`。
- `feishu` 组件当前会按用户级 `--user restart` 执行，并等待 `connected + is_fresh=true` 后再留 after 证据。
- `windows-bridge` 仍保留为人工动作步骤，避免仓库侧误触真实 Windows 端。
- `openclaw / hermes` 当前先做主备链在线性验证与留档，不在脚本里默认做高风险重启。

---

## 5. 标准演练顺序

### 5.1 Linux control plane 重启演练

步骤：

1. 收集 `before_control_plane_restart` 证据
2. 重启 `ashare-system-v2.service`
3. 等待健康检查恢复
4. 收集 `after_control_plane_restart` 证据

通过标准：

- `/health=status=ok`
- `service-recovery-readiness=status=ready|degraded`
- `workspace_context` 未完全丢失
- `supervision-board` 和 `feishu/briefing` 仍能返回

### 5.2 scheduler 重启演练

步骤：

1. 收集 `before_scheduler_restart`
2. 重启 `ashare-system-v2-scheduler.service`
3. 等待 `monitor/state` 中轮询状态恢复推进
4. 收集 `after_scheduler_restart`

通过标准：

- `service-recovery-readiness` 不因 scheduler 重启长期停在 `blocked`
- `monitor/state` 继续有新的 polling / monitor 痕迹

### 5.3 飞书长连接重启演练

步骤：

1. 收集 `before_feishu_restart`
2. 重启 `ashare-feishu-longconn.service`
3. 等待 `/system/feishu/longconn/status` 回到 `connected + is_fresh=true`
4. 收集 `after_feishu_restart`

通过标准：

- `reported_status=connected`
- `pid_alive=true`
- `is_fresh=true`

### 5.4 Windows 执行桥断链恢复演练

步骤：

1. 记录 `before_bridge_recovery`
2. 在 Windows 侧重启执行桥或恢复 QMT 链路
3. Linux 侧轮询 `/monitor/state`
4. 记录 `after_bridge_recovery`

通过标准：

- `windows_execution_gateway.reachable=true`
- `qmt_connected=true`
- `qmt_vm.reachable=true`
- `service-recovery-readiness` 不再因 execution bridge 项阻断

---

## 6. 主备 agent 恢复验证顺序

当前项目真实口径：

- 主链：OpenClaw
- 备链：Hermes `ashare-backup`

### 6.1 主链验证

先验证 OpenClaw 是否还能：

1. 读控制面 `/health`
2. 读 `workspace-context`
3. 读 `supervision-board`
4. 给出一句基于真实数据的简报

若主链仍出现 `NO_REPLY`、子代理结果转发不稳、模型回退漂移，则记为主链未恢复，不得硬判“正常”。

### 6.2 备链验证

若主链不稳，立刻验证 Hermes backup：

1. `gateway status` 在线
2. 能读取控制面真实接口
3. 能产出一条业务摘要
4. cron 不报错，或手动触发任务能成功落盘

### 6.3 切换判据

满足以下条件，可判定“主失稳，备可接管只读值班”：

- OpenClaw 主链连续两次无法稳定返回真实摘要
- Hermes backup 可以稳定读取控制面并生成业务摘要

注意：

- 这里的“接管”仅指研究、监督、摘要和值班
- 不意味着自动放开真实交易权限

---

## 7. 每次演练后必须回填的证据

至少保留：

- 重启动作时间
- 动作前后 `service-recovery-readiness`
- 动作前后 `controlled-apply-readiness`
- 动作前后 `operations/components`
- 动作前后 `feishu/longconn/status`
- 动作前后 `monitor/state`
- OpenClaw/Hermes 的一句真实摘要或失败证据

---

## 8. 当前还未完成的 live 动作

截至本文件更新时，仓库内已具备：

- 恢复检查控制面接口
- apply 准入控制面接口
- 两套检查脚本
- 证据收集脚本
- 恢复编排脚本 `scripts/run_recovery_pressure_sequence.sh`

但尚未完成：

- 真实 `systemctl restart ...` live 演练证据回填
- OpenClaw 主链与 Hermes 备链的同窗恢复演练记录
- Windows 执行桥真实断链恢复记录
