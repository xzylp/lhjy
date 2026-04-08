# OpenClaw Linux + QMT Windows VM 部署草案 v1

> 状态：Draft  
> 日期：2026-04-08

## 1. 目标

本文档定义 `ashare-system-v2` 面向未来生产部署的目标形态：

- Linux 服务器运行 `OpenClaw Gateway + main/ashare 团队 + ashare-system-v2`
- Windows 虚拟机运行 `QMT / XtQuant / Windows Execution Gateway`
- Agent 只参与决策、审议、复盘和研究，不直接持有下单权
- 自我进化只进入离线研究闭环，不直接改写实盘执行链

## 2. 生产拓扑

```text
Feishu / WebChat
  -> OpenClaw Gateway (Linux)
  -> main
  -> ashare / ashare-runtime / ashare-research / ashare-strategy / ashare-risk / ashare-audit / ashare-executor
  -> ashare-system-v2 FastAPI (Linux)
  -> approved execution_intents / audits / reports / state
  -> Windows Execution Gateway (Windows VM)
  -> QMT / XtQuant
  -> order receipts / fills / reconciliation
  -> ashare-system-v2
  -> main 汇总回复
```

## 3. 控制面与执行面边界

### 3.1 Linux 控制面

Linux 侧负责：

- 候选生成
- 多 Agent 讨论
- 风控预检
- `execution_intent` 生成与审计
- 盘后 review / attribution / metrics / monitor summary
- 人工审批、熔断、只读查询、运行编排

Linux 侧不直接负责：

- 持有 QMT 会话
- 直接提交 QMT 订单
- 直接管理券商桌面环境

### 3.2 Windows 执行面

Windows VM 侧负责：

- QMT / miniQMT 进程保活
- 账户连接与交易终端状态保持
- 执行 gateway 对 `execution_intent` 的最终落地
- 订单状态回传
- 成交回报与对账输入

Windows VM 侧不负责：

- 讨论与策略判断
- 研究摘要
- 风控规则主判定
- 自主生成下单候选

## 4. 推荐通信方式

首选：

- `Windows Execution Gateway` 主动轮询 Linux 上已批准的 `execution_intents`

不推荐：

- 让 Linux 上的 Agent 直接远程 RPC 下单
- 让多个 Agent 共享 QMT 调用权限

推荐轮询链路：

```text
Linux
  -> approved execution_intents
Windows Gateway poll
  -> submit/cancel/query QMT
  -> receipts / order status / fills
Linux
  -> reconciliation / review-board / audit
```

原因：

- 更容易做幂等
- 更容易做断线恢复
- 不要求 Windows VM 暴露复杂入站接口
- 更容易收口为唯一执行写口

### 4.1 最小 worker 入口

当前仓库已提供最小协议 worker 入口：

- CLI：`ashare-execution-gateway-worker`
- 模块：`ashare_system.windows_execution_gateway_worker`

最小单次联调命令：

```bash
ashare-execution-gateway-worker \
  --control-plane-base-url http://127.0.0.1:8100 \
  --source-id windows-vm-a \
  --deployment-role primary_gateway \
  --bridge-path "linux_openclaw -> windows_gateway -> qmt_vm" \
  --executor-mode fail_unconfigured \
  --once
```

说明：

- 默认 `executor-mode=fail_unconfigured`，不会在未接真实执行器时误触 live。
- 若 Windows VM 已具备真实 QMT / XtQuant 环境，可显式使用 `--executor-mode xtquant`，worker 会直接创建 `XtQuantExecutionAdapter` 并调用真实 `place_order`。
- 若只做协议联调，可临时改为 `--executor-mode noop_success`，但该模式只验证 `poll -> claim -> receipt`，不代表真实 QMT 可用。
- Linux 控制面环境变量应固定为 `ASHARE_EXECUTION_PLANE=windows_gateway`，不再假设 Linux 本地下单。

## 5. 核心硬约束

### 5.1 单一执行写口

- 整个系统只能有一个真正调用 QMT 下单的组件：`Windows Execution Gateway`
- `main / ashare / ashare-executor / FastAPI` 都不能直接越过 gateway 下单

### 5.2 幂等与订单状态机

- 每个 `execution_intent` 必须有稳定唯一键
- Windows Gateway 必须支持重复拉取不重复下单
- 订单状态至少要覆盖：
  - `pending`
  - `submitted`
  - `accepted`
  - `partial_filled`
  - `filled`
  - `canceled`
  - `rejected`
  - `unknown_needs_reconcile`

### 5.3 熔断与人工接管

- live 执行必须可人工熔断
- Windows VM、QMT、账户、网关任一异常时，Linux 控制面必须默认降级为：
  - `paper`
  - `preview-only`
  - `blocked`

## 6. 自我进化边界

### 6.1 允许进入自我进化的范围

- prompt 优化建议
- 讨论路由与观点质量改进
- 离线回测参数候选
- attribution / metrics / monitor summary 的研究分析
- 失败案例归因与复盘标签

### 6.2 禁止自动进入生产的范围

- 自动修改 QMT 执行网关代码并直接部署
- 自动改变 live 下单权限
- 自动放宽风控阈值并直接生效
- 自动把离线回测表现好的策略直接推到 live

### 6.3 推荐晋升链路

```text
log / attribution / metrics / discussion review
  -> self-improvement proposal
  -> offline validation
  -> sandbox / paper replay
  -> human review
  -> merge / deploy
  -> supervised live
```

## 7. 部署配置收口

### 7.1 环境变量模板

推荐把 Linux 控制面的 `.env` 明确收口为：

```dotenv
ASHARE_RUN_MODE=dry-run
ASHARE_EXECUTION_MODE=mock
ASHARE_EXECUTION_PLANE=windows_gateway
ASHARE_SERVICE_HOST=127.0.0.1
ASHARE_SERVICE_PORT=8100
```

约束：

- `ASHARE_EXECUTION_PLANE=windows_gateway` 表示默认部署不假设 Linux 本地下单。
- Linux 侧负责控制面、审计、只读聚合和编排，不直接持有 QMT 会话。
- 即使 `ASHARE_EXECUTION_MODE=xtquant`，生产拓扑也应先确认真正的执行写口仍然是 Windows Execution Gateway。

### 7.2 Linux control plane 启动检查项

Linux 主控启动后，至少检查以下项目：

1. 本地服务可用：`/health`、`/system/health`、`/execution/health`
2. 启动脚本打印出的 `execution_plane=windows_gateway` 与当前部署一致
3. 读取只读聚合口：
   - `GET /system/deployment/linux-control-plane-startup-checklist`
   - `GET /system/deployment/windows-execution-gateway-onboarding-bundle`
4. 核对 Linux 控制面不直接持有 QMT 会话，也不把 `127.0.0.1` 的本地 QMT 当成正式下单路径
5. 若已接入 execution bridge health，上报实例的 `source_id / deployment_role / bridge_path` 必须落在固定口径内

### 7.3 Windows Gateway 启动参数样例

以下样例直接复用当前仓库已有的最小 worker 入口：

```powershell
$env:ASHARE_EXECUTION_PLANE = "windows_gateway"
ashare-execution-gateway-worker `
  --control-plane-base-url "http://linux-control-plane:8100" `
  --source-id "windows-vm-a" `
  --deployment-role "primary_gateway" `
  --bridge-path "linux_openclaw -> windows_gateway -> qmt_vm" `
  --executor-mode fail_unconfigured `
  --once
```

如果切换到备机，只替换两项：

- `source_id = "windows-vm-b"`
- `bridge_path = "linux_openclaw -> windows_gateway_backup -> qmt_vm"`

### 7.4 主备 source 值与 bridge_path 固定口径

部署、监控上报、review 和 handoff 统一复用以下固定值：

- Linux control plane
  - `source_id = "linux-openclaw-main"`
  - `deployment_role = "linux_control_plane"`
- Windows Gateway 主
  - `source_id = "windows-vm-a"`
  - `deployment_role = "primary_gateway"`
  - `bridge_path = "linux_openclaw -> windows_gateway -> qmt_vm"`
- Windows Gateway 备
  - `source_id = "windows-vm-b"`
  - `deployment_role = "backup_gateway"`
  - `bridge_path = "linux_openclaw -> windows_gateway_backup -> qmt_vm"`

这些值应与 `monitor-execution-bridge-health-contract.md` 中的 `source_value_samples` 保持一致，不再各文档各写一套。

## 8. 对当前仓库的对应关系

当前已具备的主链基础：

- discussion writeback / preview
- execution intents / dispatch / reconciliation
- review board
- offline backtest attribution / metrics
- exit monitor snapshot / trend summary

下一步最适合补齐的生产缺口：

- Windows Execution Gateway 契约与健康快照
- `execution_intent -> receipt -> reconcile` 的跨机轮询协议
- self-improvement proposal/export packet
- live/paper/supervised 三档执行治理
