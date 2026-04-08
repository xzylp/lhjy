# ashare-system-v2 项目交接文档

> 最后更新: 2026-04-08

## 一、项目定位

`ashare-system-v2` 是 A 股量化运行中台，服务于 OpenClaw 的量化团队协作，不再把前台对话、量化中台调度、运行执行逻辑混在一起。

当前正式目标链路：

```text
Feishu / WebChat
  -> OpenClaw Gateway
  -> main
  -> ashare
  -> ashare-runtime / ashare-research / ashare-strategy / ashare-risk / ashare-executor / ashare-audit
  -> ashare-system-v2 FastAPI
  -> approved execution_intents / state / reports / audits
  -> Windows Execution Gateway
  -> QMT / XtQuant / receipts / fills / reconcile
  -> main 汇总后对外回复
```

## 二、当前状态

### 2.1 已完成

- `main` 已确认为唯一前台入口。
- `openclaw.json` 已调整为 `feishu -> main`、`webchat/main -> main`。
- `main` 已允许分发 `ashare`。
- `ashare` 已允许分发 6 个量化子角色：
  - `ashare-runtime`
  - `ashare-research`
  - `ashare-strategy`
  - `ashare-risk`
  - `ashare-executor`
  - `ashare-audit`
- `start_unattended.ps1` 已从 gateway 生命周期中解耦，只负责 Windows 侧 FastAPI + scheduler。
- `start_openclaw_gateway.sh` 已独立为 gateway 启动入口。
- `task.md` 已更新为 `main -> ashare -> 子团队` 的正式目标。
- `technical-manual.md` 已更新为当前架构和 WSL 直连方案。

### 2.2 本轮新收口

- 删除了旧的中转脚本与相关表述。
- 新增 `scripts/ashare_api.sh`，作为 WSL 侧统一的直连访问入口。
- `start_unattended.ps1` 与 `restart_api_service.ps1` 现统一生成服务 endpoint manifest，并以动态探测方式供 WSL 访问。
- `verify_openclaw_chain.sh` 改为显式创建新的 `main` 会话做验证，不删历史 session，也不依赖复用旧长会话。
- 新增 Windows ops proxy 方案：
  - `scripts/windows_ops_proxy.py`
  - `scripts/windows_ops_proxy.cmd`
  - `scripts/windows_ops_api.sh`
  - `.ashare_state/ops_proxy_endpoints.json`
  - `.ashare_state/ops_proxy_token.txt`
- `system_api` 现对 `serving/latest_dossier_pack.json` 提供读优先支持：
  - `/system/precompute/dossiers/latest`
  - `/system/monitoring/cadence`
- `system_api` 已新增 `GET /system/discussions/agent-packets`，作为讨论子代理统一 dossier 读口。
- `monitor_api` 的 `/monitor/pool` 现已补 serving 回退，运行内存股池为空时可直接展示最新 dossier 候选池。
- 已修复 ops proxy manifest 链路中的两个实际问题：
  - `write_ops_proxy_endpoints.ps1` 的 PowerShell 变量插值错误
  - `windows_ops_api.sh` / `ashare_api.sh` 对 PowerShell UTF-8 BOM manifest 的读取兼容
- 已验证 Windows ops proxy 实际监听 `0.0.0.0:18791`，并可通过代理远程执行 Windows 侧 `pytest`。
- Windows ops proxy 仅作为测试 / 运维辅助链路，不是正式运行时依赖。
- `.env.example`、`scripts/start.sh`、`scripts/health_check.sh`、`scripts/start_openclaw_gateway.sh` 已统一补上 `ASHARE_EXECUTION_PLANE=windows_gateway` 口径，不再默认 Linux 本地下单。
- Linux control plane 启动检查现统一要求结合只读聚合口：
  - `GET /system/deployment/linux-control-plane-startup-checklist`
  - `GET /system/deployment/windows-execution-gateway-onboarding-bundle`
- Windows Execution Gateway 主备固定值统一为：
  - 主：`source_id=windows-vm-a`，`deployment_role=primary_gateway`，`bridge_path=linux_openclaw -> windows_gateway -> qmt_vm`
  - 备：`source_id=windows-vm-b`，`deployment_role=backup_gateway`，`bridge_path=linux_openclaw -> windows_gateway_backup -> qmt_vm`

### 2.3 仍待完成

- 仓库内 `openclaw/` 工作流定义和当前运行工作区仍需持续对齐，避免后续漂移。
- gateway 启动后的实链验证仍需完整跑一遍，并覆盖多个量化子角色。
- 验证脚本需要继续收紧到“保留历史会话，只验证新活动”的默认模式。
- `ashare` 与子代理的讨论编排虽然已对齐到 cycle API，但 OpenClaw 线上工作区仍需按仓库 prompt 持续同步。
- `finalize` 现已返回 `client_brief`，后续主调度与推送应优先复用该结构，避免重复查询与口径漂移。

## 三、当前正式职责边界

| 角色 | 定位 | 职责 |
|------|------|------|
| `main` | 唯一前台 | 接收消息、路由专业团队、统一对外回复 |
| `ashare` | 量化中台 | 承接量化请求、拆任务、汇总结果 |
| `ashare-runtime` | 运行面 | health、pipeline、intraday、runtime report |
| `ashare-research` | 研究面 | sync、news、announcements、summary |
| `ashare-strategy` | 策略面 | 候选股解释、策略比较、观察名单 |
| `ashare-risk` | 风控面 | allow / limit / reject |
| `ashare-executor` | 执行面 | 账户、持仓、订单、成交、执行意图落地 |
| `ashare-audit` | 审计面 | audits、meetings、runtime report、postclose report |

## 四、当前可用接口

### 4.1 Runtime

- `GET /runtime/health`
- `POST /runtime/jobs/pipeline`
- `POST /runtime/jobs/intraday`
- `POST /runtime/jobs/autotrade`
- `GET /runtime/reports/latest`

### 4.2 Research

- `GET /research/health`
- `POST /research/sync`
- `POST /research/events/news`
- `POST /research/events/announcements`
- `GET /research/summary`

### 4.3 Strategy / Market / Execution

说明：

- 以下接口是服务层能力清单，不代表默认部署改回 Linux 本地直连下单。
- 当前正式部署口径仍是 `ASHARE_EXECUTION_PLANE=windows_gateway`，Linux 侧默认只负责 control plane。

- `GET /strategy/strategies`
- `POST /strategy/screen`
- `GET /market/universe`
- `GET /market/snapshots`
- `GET /market/bars`
- `GET /market/sectors`
- `GET /execution/health`
- `GET /execution/balance/{account_id}`
- `GET /execution/positions/{account_id}`
- `GET /execution/orders/{account_id}`
- `GET /execution/trades/{account_id}`
- `POST /execution/orders`

### 4.4 System / Audit

- `GET /system/health`
- `GET /system/home`
- `GET /system/overview`
- `GET /system/operations/components`
- `GET /system/audits`
- `GET /system/audits/by-decision`
- `GET /system/audits/by-experiment`
- `GET /system/research/summary`
- `GET /system/reports/runtime`
- `GET /system/reports/postclose-master`
- `GET /system/reports/postclose-master-template`
- `POST /system/meetings/record`
- `GET /system/meetings/latest`
- `GET /system/config`
- `GET /system/params`
- `POST /system/params/proposals`
- `GET /system/params/proposals`
- `GET /system/cases`
- `GET /system/cases/{case_id}`
- `GET /system/discussions/agent-packets`
- `POST /system/discussions/opinions/batch`
- `POST /system/cases/{case_id}/rebuild`
- `POST /system/cases/rebuild`
- `GET /system/discussions/summary`
- `GET /system/discussions/reason-board`
- `GET /system/discussions/reply-pack`
- `GET /system/discussions/final-brief`
- `GET /system/discussions/client-brief`
- `GET /system/discussions/cycles`
- `GET /system/discussions/cycles/{trade_date}`
- `POST /system/discussions/cycles/bootstrap`
- `POST /system/discussions/cycles/{trade_date}/rounds/{round}/start`
- `POST /system/discussions/cycles/{trade_date}/refresh`
- `POST /system/discussions/cycles/{trade_date}/finalize`
- `GET /system/discussions/execution-precheck`
- `GET /system/discussions/execution-intents`
- `POST /system/discussions/execution-intents/dispatch`
- `GET /system/discussions/execution-dispatch/latest`
- `GET /system/agent-scores`
- `POST /system/agent-scores/settlements`

## 五、WSL 与 Windows 通信原则

### 5.0 执行平面默认值

- 默认部署口径是 `ASHARE_EXECUTION_PLANE=windows_gateway`。
- Linux / WSL 侧是 control plane，不是 QMT 直连下单机。
- 如果后续需要单机或本地直连，只能作为显式例外配置处理，不能再把它写成默认样例。

### 5.1 正式方案

- Windows 服务地址不再写死；统一通过 `.ashare_state/service_endpoints.json` + `scripts/ashare_api.sh` 动态探测。
- Windows 侧默认以 `0.0.0.0` 绑定 FastAPI，manifest 优先发布 WSL/局域网可达地址；`127.0.0.1:8100` 仅作为本机回退探测项。
- WSL 不再通过 PowerShell 中转调用 Windows 服务。
- WSL 统一使用 `scripts/ashare_api.sh` 直连 Windows 上的 FastAPI。

### 5.2 `ashare_api.sh` 逻辑

1. 先读取 `.ashare_state/service_endpoints.json`
2. 优先探测 `preferred_wsl_url`
3. 再探测 `candidate_urls`
4. 若 manifest 不可用，再回退到 `/etc/resolv.conf`、默认网关和 PowerShell 发现的地址

补充约束：
- `preferred_wsl_url` 表示“当前 WSL 可达的 Windows 服务地址”，不是固定回环地址。
- `127.0.0.1:8100` 仅表示 Windows 本机回环健康检查地址，不能在 WSL 联调说明中当作默认对外地址。

### 5.3 验证命令

```bash
./scripts/ashare_api.sh probe
./scripts/ashare_api.sh GET /health
./scripts/ashare_api.sh GET /runtime/health
./scripts/ashare_api.sh GET /system/overview
curl http://127.0.0.1:18789/health
```

### 5.4 会话原则

- OpenClaw 配置和工作区规则的变更，默认只要求“新会话”生效，不要求删除历史会话。
- 验证链路时，优先显式创建新的 `main` 会话，而不是复用长期累积上下文的旧前台会话。
- 历史 session 保留用于追溯；新规则验证靠新 session 完成。

### 5.5 Windows Ops Proxy 原则

- 代理独立于 ashare FastAPI 主服务存在，主服务挂掉时也能执行 `start / stop / tests / watchdog`。
- WSL / Codex 不再直接依赖 `powershell.exe` 去调用 Windows 侧脚本。
- 白名单动作目前包括：
  - 服务 `start / stop / restart / status`
  - watchdog `start / stop`
  - `run_tests.ps1`
  - `health_check.ps1`
  - 日志 tail
- 认证方式：
  - token 默认写入 `.ashare_state/ops_proxy_token.txt`
  - endpoint manifest 默认写入 `.ashare_state/ops_proxy_endpoints.json`
- WSL 统一使用 `scripts/windows_ops_api.sh` 调代理。

## 六、启动方式

### 6.1 Windows

```powershell
cd D:\Coding\lhjy\ashare-system-v2
.\scripts\start_unattended.ps1
```

如果部署 Windows Execution Gateway，建议同时固定：

- `ASHARE_EXECUTION_PLANE=windows_gateway`
- 主机默认 `source_id=windows-vm-a`
- 备机默认 `source_id=windows-vm-b`
- 主 `bridge_path=linux_openclaw -> windows_gateway -> qmt_vm`
- 备 `bridge_path=linux_openclaw -> windows_gateway_backup -> qmt_vm`

### 6.2 WSL

```bash
cd /mnt/d/Coding/lhjy/ashare-system-v2
./scripts/start_openclaw_gateway.sh
```

该入口仅启动 Linux control plane 的 OpenClaw Gateway，不代表本机直接持有 QMT 会话。

### 6.3 Windows Ops Proxy

```powershell
cd D:\Coding\lhjy\ashare-system-v2
.\scripts\windows_ops_proxy.cmd
```

无黑框常驻启动可用：

```powershell
wscript.exe D:\Coding\lhjy\ashare-system-v2\scripts\windows_ops_proxy.vbs
```

### 6.4 WSL 调用 Windows Ops Proxy

```bash
cd /mnt/d/Coding/lhjy/ashare-system-v2
./scripts/windows_ops_api.sh GET /health
./scripts/windows_ops_api.sh GET /status
./scripts/windows_ops_api.sh POST /actions/service '{"action":"status"}'
./scripts/windows_ops_api.sh POST /actions/tests '{"args":["tests/test_data_archive.py","-v"]}'
```

## 七、当前主要风险

- 若仓库内 OpenClaw 定义与 `~/.openclaw` 实际工作区再次漂移，模型可能重新走偏。
- 非破坏式 verifier 若只按时间戳判断，在并发人工对话场景下仍可能误判，需要继续收紧为 run 标记校验。
- 仓库内 `openclaw/workflow.final.json`、`team_registry.final.json` 和 prompts 仍需持续与线上配置一致。

## 八、下一步建议

1. 回写仓库内 OpenClaw workflow / registry / prompts 到新架构。
2. 完成非破坏式 verifier，默认保留历史 session / memory，只校验当前新增链路活动。
3. 把 `ashare` 的两轮讨论编排稳定落到 cycle API：
   - bootstrap cycle
   - start round 1
   - batch opinions
   - refresh
   - start round 2
   - finalize
4. 在非沙箱环境下执行一次完整验证：
   - Windows 启动服务
   - WSL 用 `ashare_api.sh` 直连
   - gateway 启动
   - `main -> ashare` 量化消息路由验证
