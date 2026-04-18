# Codex 服务器侧交接包

生成时间：2026-04-12
适用场景：假设 Windows 侧连通与本地服务已经可用，后续主线工作转移到服务器侧继续推进。

## 1. 当前主线目标

当前唯一主线是：

```text
Linux control plane -> Windows Execution Gateway -> QMT
```

已明确原则：

- Linux 不直接持有 QMT 会话
- 正式执行写口只能是 Windows Execution Gateway
- Windows 侧负责：QMT、XtQuant、执行桥、运维接口
- Linux 侧负责：control plane、OpenClaw、调度、只读聚合、验收、主控编排

## 2. 当前服务器事实

### 服务器信息

- 主机：`yxz@192.168.99.16`
- Tailscale：`100.75.91.75`
- 项目目录：`/srv/projects/ashare-system-v2`
- 状态目录：`/srv/data/ashare-state`
- 对外主控 API：`http://192.168.99.16:8100`

### systemd 与服务

- `ashare-system-v2.service`：已部署，正常
- `openclaw-gateway.service`：已部署，正常

### OpenClaw 状态

- 版本：`2026.4.10`
- 当前 profile：量化专用，无 `main`
- 有效目录：`/home/yxz/.openclaw-quant`
- `~/.openclaw` 已指向 `.openclaw-quant`
- 默认 agent：`ashare`
- 当前 7 个量化 agents：
  - `ashare`
  - `ashare-runtime`
  - `ashare-research`
  - `ashare-strategy`
  - `ashare-risk`
  - `ashare-executor`
  - `ashare-audit`

### OpenClaw 登录与 Feishu

- 内网 HTTP 调试已开启：
  - `gateway.controlUi.allowInsecureAuth=true`
  - `gateway.controlUi.dangerouslyDisableDeviceAuth=true`
- OpenClaw 侧 Feishu 路由已配置，`status --all` 显示 `Feishu ON / OK`
- 但 Linux 控制面自己的飞书推送环境变量目前未完整配置，因此控制面主动通知未完全打通

## 3. 当前 Linux 控制面的运行事实

### 已经验证正常的部分

- 可正常跑 paper pipeline
- 最近已跑出 runtime 候选
- `/system/config` 可读
- `/system/reports/postclose-master` 可读
- `/system/reports/review-board` 可读
- `/system/deployment/windows-execution-gateway-onboarding-bundle` 可读
- `/system/readiness` 当前语义已修复为：非 live 可 `ok=true` 且 `status=degraded`

### 当前典型状态

- `run_mode=paper`
- `readiness.ok=true`
- `readiness.status=degraded`

### degraded 的主要原因

截至当前，degraded 主要来自：

- Windows execution bridge 未形成稳定真实上报
- `account_access` / `account_identity` 仍有 warning
- 执行桥健康趋势与账户读取尚未完全转绿

## 4. 当前网络与拓扑结论

### 网络边界

- 宿主机局域网：`192.168.99.16`
- 宿主机 Tailscale：`100.75.91.75`
- libvirt NAT 网关：`192.168.122.1`
- Windows VM：`192.168.122.66`

### 已验证事实

- Linux 到 Windows VM 网络可达
- `192.168.122.66` 可 ping 通
- `58310` 端口曾经可达，但只能视为代理口，不是正式业务接口
- 当前主文档已统一：不能把 `58310` 写成正式执行桥端口

## 5. 共享目录与已准备工件

共享目录：

- `\\192.168.122.1\\vmshare`

### commands 中已存在

- `WINDOWS_GATEWAY_联调说明.md`
- `WINDOWS_执行桥与QMT守护服务技术要求.md`
- `CODEX_VM_CONTEXT_HANDOFF_20260412.md`
- `windows_gateway_bootstrap.ps1`
- `windows_gateway_once.ps1`
- `windows_gateway_forever.ps1`

### packages 中已存在

- `ashare-system-v2` 项目镜像目录
- `XtQuant.tar.gz`
- `xtquantservice.tar.gz`
- `windows-runtime-bundle_20260412`
- `windows-runtime-bundle_20260412.zip`

## 6. 已经给 Windows 侧的技术要求

Windows 侧要求已经单独写入：

- `\\192.168.122.1\\vmshare\\commands\\WINDOWS_执行桥与QMT守护服务技术要求.md`

核心要求包括：

- QMT 守护
- Windows Execution Gateway 常驻程序
- ops/diagnostic HTTP 接口
- execution bridge health 主动上报
- 数据拉取能力
  - 账户状态
  - 资金
  - 持仓
  - 委托
  - 成交
- 交易执行能力
  - place_order
  - cancel_order
  - receipt 回写

## 7. 已存在可复用的 Windows 侧代码

当前项目里已有可复用组件，已单独打成运行包：

- `src/ashare_system/windows_execution_gateway_worker.py`
- `src/ashare_system/infra/qmt_launcher.py`
- `scripts/windows_ops_proxy.py`
- `scripts/windows_service.ps1`
- `scripts/manual_service.ps1`
- `scripts/start_unattended.ps1`
- `scripts/stop_unattended.ps1`
- `scripts/health_check.ps1`
- `scripts/write_ops_proxy_endpoints.ps1`
- `scripts/windows_service_gui.py`

对应说明：

- `\\192.168.122.1\\vmshare\\packages\\windows-runtime-bundle_20260412\\docs\\WINDOWS_现成运行包说明_20260412.md`

## 8. 固定身份与执行桥约定

Windows 主执行桥固定值：

- `source_id=windows-vm-a`
- `deployment_role=primary_gateway`
- `bridge_path=linux_openclaw -> windows_gateway -> qmt_vm`

备用约定：

- `source_id=windows-vm-b`
- `deployment_role=backup_gateway`
- `bridge_path=linux_openclaw -> windows_gateway_backup -> qmt_vm`

Linux control plane 固定地址：

- `http://192.168.99.16:8100`

## 9. 假设 Windows 已通后，服务器侧接下来的工作顺序

这里开始是“调到服务器继续工作”的主线清单。

### 第一优先级：验收执行桥是否真的进来

重点检查：

- `GET /system/reports/serving-latest-index`
- `GET /system/monitor/execution-bridge-health/template`
- `GET /system/reports/postclose-master`
- `GET /system/readiness`
- `GET /system/reports/review-board`
- `GET /system/discussions/execution-precheck?trade_date=...&account_id=...`
- `GET /system/discussions/execution-intents?trade_date=...&account_id=...`
- `GET /system/execution/gateway/receipts/latest`

成功标志：

- `latest_execution_bridge_health` 不为空
- `source_id == windows-vm-a`
- `bridge_path == linux_openclaw -> windows_gateway -> qmt_vm`
- `qmt_connected=true` 或至少不再是纯 unknown
- `readiness` 中 execution bridge 从 unknown 进入 healthy/degraded

### 第二优先级：验收账户读取链

重点目标：

- 不再出现“资产全 0”假阳性
- `account_access` / `account_identity` warning 明显减少
- precheck 能读到真实账户状态

应重点验证：

- balance
- positions
- today orders
- today trades
- pending orders

### 第三优先级：验收执行闭环

重点目标：

- Linux 产生 approved/queued intent
- Windows claim
- Windows submit/cancel
- receipt 回写 Linux
- Linux latest receipt 与 control_plane_gateway summary 更新

优先验收入口：

- `POST /system/discussions/execution-intents/dispatch`
- `GET /system/discussions/execution-dispatch/latest?trade_date=...`
- `GET /system/execution/gateway/receipts/latest`
- `GET /system/tail-market/latest`

### 第四优先级：把服务器只读聚合收口

当执行桥与账户链转绿后，再继续看：

- `review-board`
- `postclose-master`
- `postclose-deployment-handoff`
- execution bridge trend summary
- latest receipt summary
- control plane gateway summary

### 第五优先级：再回头补飞书主动通知与参数调整链

当前这部分不是阻断执行桥的第一优先级。

但后续可继续收口：

- Linux 控制面自己的飞书环境变量
- 调参自然语言别名扩展
- 推送式信息发布

## 10. 当前已知接口与关键入口

### Linux 主控接口

- `/system/deployment/windows-execution-gateway-onboarding-bundle`
- `/system/monitor/execution-bridge-health`（Windows Execution Gateway 健康上报写入口，`POST`）
- `/system/monitor/execution-bridge-health/template`
- `/system/reports/serving-latest-index`
- `/system/reports/postclose-master`
- `/system/reports/review-board`
- `/system/reports/postclose-deployment-handoff`
- `/system/readiness`
- `/system/discussions/execution-precheck`
- `/system/discussions/execution-intents`
- `/system/discussions/execution-intents/dispatch`
- `/system/execution/gateway/intents/pending`
- `/system/execution/gateway/intents/claim`
- `/system/execution/gateway/receipts`
- `/system/execution/gateway/receipts/latest`

### OpenClaw

- Dashboard：`http://192.168.99.16:18890/`
- 本地命令：`/home/yxz/.npm-global/bin/openclaw status --all`

## 11. 如果新的 Codex/Agent 在服务器内接手，建议先做什么

1. 先读本文件
2. 再读：
   - `/srv/projects/ashare-system-v2/docs/ubuntu-server-windows-qmt-guide.md`
   - `/srv/projects/ashare-system-v2/docs/linux-windows-qmt-network-report.md`
3. 假设 Windows 侧已经通，先去验收 execution bridge / account / receipt 三条链
4. 不要先去改大段代码
5. 只有在服务器侧验收发现真实字段不匹配时，再做针对性修复

## 12. 当前不应再走的错误方向

- 不要再尝试 Linux 直接连接 QMT
- 不要把 `58310` 误当正式业务端口
- 不要把 OpenClaw、control plane、execution gateway 的职责混在一起
- 不要在 Linux 侧伪造 execution bridge 成功态
- 不要先做 UI/文档美化，跳过 execution/account/receipt 实链验收

## 13. 接手后的最小成功标准

接手后的最小成功标准不是“服务都启动了”，而是：

- Linux 已收到 Windows execution bridge 健康上报
- Linux 能通过当前链路读到真实账户状态
- 至少一条 execution intent 能完整形成 queue -> claim -> receipt 闭环
- `review-board` / `postclose-master` 中能读到真实执行桥与 receipt 摘要
