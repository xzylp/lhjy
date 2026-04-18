# Ubuntu Server + Windows(QMT) 部署与运行最终稿

## 审核结论

这份方案经过当前 `ashare-system-v2` 项目结构对照后，结论如下：

- 方向正确，可以作为服务器基础架设的主线方案
- 现在可以直接作为“基础设施 + 应用运行口径”的统一依据
- 可以按本文完成：
  - `Ubuntu Server`
  - `KVM / libvirt / Cockpit`
  - `Windows VM`
  - `QMT` 安装环境
  - `/srv` 目录结构
  - 基础 SSH / 防火墙 / 时区 / 备份目录
  - Linux control plane 运行目录
  - Windows Execution Gateway 联调地址
- 项目侧已同步收口的运行事实：
  - `scripts/start.sh` 已支持 Ubuntu `.venv/bin/python`、WSL `.venv/Scripts/python.exe` 与 `ASHARE_PYTHON_BIN`
  - `scripts/health_check.sh` 已按 `windows_gateway` 执行平面输出 Linux 启动检查项
  - `scripts/start_openclaw_gateway.sh` 已支持 `OPENCLAW_BIN` 或 `command -v openclaw`
  - `GET /system/deployment/linux-control-plane-startup-checklist`
  - `GET /system/reports/postclose-deployment-handoff`
  - `GET /system/reports/postclose-master`
    都已经可作为部署后的只读验收入口

当前审核通过的前提是：

- Linux 作为长期控制面与服务宿主机
- Windows VM 只保留 `QMT + Windows Execution Gateway`
- `ashare-system-v2` 的正式执行写口仍然是 Windows Execution Gateway

## 本次确认的硬件基线

当前已确定的机器规格：

- CPU：`Intel Core Ultra 9 285H`
- 内存：`32GB DDR5`
- 磁盘：`1TB SSD`

本文后续所有容量和虚拟机建议，默认都以这套硬件为基线，不再按多档规格展开。

## 目标

本文档只讨论一种方案：

- 迷你主机安装 `Ubuntu Server 24.04 LTS`
- Linux 作为长期运行的主宿主机
- Windows 只作为虚拟机运行 `QMT`
- 后续 `ashare-system`、`OpenClaw`、数据库、监控、备份优先放在 Linux

这是一份给主开发者审核的简化方案，不展开 Desktop、双系统、Docker 跑 Windows 等分支。

## 核心结论

推荐最终结构：

```text
物理机
├── Ubuntu Server 24.04 LTS
│   ├── OpenClaw
│   ├── ashare-system
│   ├── Docker / Compose
│   ├── 数据库与监控
│   └── KVM / libvirt
└── Windows 虚拟机
    ├── QMT
    └── 与 Linux 通讯的桥接程序
```

职责边界：

- Linux：主业务、服务编排、日志、备份、监控
- Windows：只保留 `QMT` 和必要的数据通讯

## 为什么选这个方案

原因只有三条：

1. `QMT` 运行在 Windows 环境最稳。
2. Linux 更适合长期跑服务、监控、备份和自动化。
3. 让 Windows 退化成 QMT 执行层，后续维护成本最低。

不采用以下方案：

- Linux Docker 直接跑 Windows/QMT：不可行
- 整套系统长期放在 Windows：不利于稳定运行
- 一开始装 Ubuntu Desktop：不是当前主目标

## 推荐组件

- 宿主机系统：`Ubuntu Server 24.04 LTS`
- 虚拟化：`KVM + QEMU + libvirt`
- 虚拟机管理：`Cockpit + cockpit-machines`
- Windows 远程管理：`RDP`

## 存储规划

当前只有一块 `1TB SSD`，建议原则是：

- 不做复杂分区
- 系统保持简单
- 所有长期业务目录统一放到 `/srv`

推荐目录结构：

```text
/srv
├── vm/
│   └── windows/
│       ├── images/
│       ├── iso/
│       └── snapshots/
├── projects/
│   ├── ashare-system/
│   └── openclaw/
├── data/
│   ├── postgres/
│   ├── redis/
│   ├── qmt-sync/
│   └── app-data/
├── containers/
│   ├── openclaw/
│   ├── monitoring/
│   └── reverse-proxy/
└── backup/
    ├── configs/
    ├── db/
    └── vm-meta/
```

容量建议：

- Ubuntu 系统与基础软件：`100GB`
- Windows 虚拟机：`220GB - 260GB`
- Linux 项目与业务数据：`350GB+`
- 备份与预留空间：`200GB+`

建议始终保留至少 `15%` 可用空间。

## Windows 虚拟机建议

基于当前机器 `Core Ultra 9 285H + 32GB DDR5 + 1TB SSD`，建议初始分配：

- vCPU：`6`
- 内存：`10GB` 到 `12GB`
- 磁盘：`220GB qcow2`

原则：

- 先保证宿主机稳定
- 不要一次把资源给满
- Windows 只承载 QMT，不承担整套主业务

建议默认值：

- 先按 `6 vCPU / 12GB / 220GB` 建机
- 若后续发现 Linux 侧同时跑数据库、监控和回测更吃内存，再把 Windows VM 调回 `10GB`

## 网络建议

第一阶段统一用：

- `NAT`

原因：

- 配置最简单
- 最不容易把宿主机网络搞坏
- 足够完成 Windows 安装、QMT 安装和基本联通验证

后续如果明确需要 Windows 虚拟机拥有独立局域网 IP，再改成桥接。

这里需要明确一个审核口径：

- `NAT` 适合先完成宿主机、虚拟化、Windows VM、QMT 的基础搭建
- 但进入项目联调时，Windows VM 访问 Linux control plane 不能使用 `127.0.0.1`
- 到应用迁移阶段，必须改成“Windows VM 可达的 Linux 宿主机地址”，可以是：
  - libvirt NAT 网段下的宿主机地址
  - 桥接后的局域网地址
  - 其他已验证可达的固定地址

也就是说：

- `NAT` 可以先用
- 但 control plane 联调地址必须单独确认，不要把 `127.0.0.1` 写成最终方案

## 部署顺序

### 第一阶段：宿主机基础环境

1. 安装 `Ubuntu Server 24.04 LTS`
2. 勾选 `OpenSSH Server`
3. 配置时区、主机名、基础安全
4. 创建 `/srv` 目录结构
5. 安装 KVM/libvirt/Cockpit

推荐初始化命令：

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y curl wget git vim htop tmux jq net-tools ca-certificates
sudo timedatectl set-timezone Asia/Shanghai
sudo ufw allow 22/tcp
sudo ufw enable
```

### 第二阶段：安装虚拟化组件

```bash
sudo apt install -y qemu-kvm libvirt-daemon-system libvirt-clients virtinst bridge-utils ovmf
sudo apt install -y cockpit cockpit-machines
sudo systemctl enable --now libvirtd
sudo systemctl enable --now cockpit.socket
sudo ufw allow 9090/tcp
```

### 第三阶段：创建 Windows 虚拟机

1. 上传 Windows ISO
2. 上传 VirtIO 驱动 ISO
3. 在 Cockpit 中创建 Windows VM
4. 完成系统安装与驱动安装
5. 安装 QMT
6. 配置远程桌面

### 第四阶段：业务落位

1. Linux 部署 `OpenClaw`
2. Linux 部署 `ashare-system`
3. Windows 只保留 `QMT + 桥接层`
4. Linux 与 Windows 通过接口通讯

说明：

- 这一阶段只确认部署方向，不要求现在就把应用层脚本全部改完
- 服务器可以先搭好，应用迁移细节在后续实施清单中单独收口

### 第五阶段：运行收口

当服务器、虚拟机和 QMT 已经架好后，按下面顺序完成项目运行层：

1. Linux 控制面代码落位到固定目录，例如：
   - `/srv/projects/ashare-system-v2`
2. Linux 创建虚拟环境并安装项目：

```bash
cd /srv/projects/ashare-system-v2
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

3. Linux `.env` 最小建议值：

```bash
ASHARE_RUN_MODE=paper
ASHARE_EXECUTION_MODE=mock
ASHARE_EXECUTION_PLANE=windows_gateway
ASHARE_MARKET_MODE=mock
ASHARE_STORAGE_ROOT=/srv/data/ashare-state
ASHARE_LOGS_DIR=/srv/projects/ashare-system-v2/logs
ASHARE_SERVICE_HOST=0.0.0.0
ASHARE_SERVICE_PORT=8100
ASHARE_PUBLIC_BASE_URL=http://192.168.99.16:8100

# 可选但推荐显式写死，避免 systemd / cron / tmux 环境差异
ASHARE_PYTHON_BIN=/srv/projects/ashare-system-v2/.venv/bin/python
OPENCLAW_BIN=/home/yxz/.npm-global/bin/openclaw
OPENCLAW_GATEWAY_BIND=lan
OPENCLAW_GATEWAY_PORT=18890
OPENCLAW_GATEWAY_ALLOW_UNCONFIGURED=true
```

4. Linux 启动控制面：

```bash
cd /srv/projects/ashare-system-v2
./scripts/start.sh
```

正式运行建议改为 `systemd`：

```bash
cd /srv/projects/ashare-system-v2
./scripts/install_linux_systemd_service.sh
./scripts/ashare_service.sh start
./scripts/ashare_service.sh status
./scripts/install_openclaw_gateway_service.sh
./scripts/openclaw_gateway_service.sh start
./scripts/openclaw_gateway_service.sh status
```

5. Linux 启动 OpenClaw gateway：

```bash
cd /srv/projects/ashare-system-v2
./scripts/start_openclaw_gateway.sh /srv/projects/ashare-system-v2/logs/openclaw-gateway.log
```

6. Linux 健康检查：

```bash
cd /srv/projects/ashare-system-v2
./scripts/health_check.sh
```

这里要特别强调：

- Ubuntu Server 正式部署不要再依赖 `.venv/Scripts/python.exe`
- Windows VM 访问 Linux control plane 时，不要使用 `127.0.0.1`
- 应固定为 Windows VM 可达的 Linux 地址，例如：
  - NAT 宿主机地址
  - 桥接后的局域网地址
  - 或你已经验证可达的固定 DNS / 内网地址
- 推荐把这个地址显式写到 `ASHARE_PUBLIC_BASE_URL`，这样 `/system/deployment/windows-execution-gateway-onboarding-bundle` 里的 worker 命令和 curl 样例会直接输出正确地址

## 当前项目的目标运行结构

当前 `ashare-system-v2` 的正式口径已经不是“Linux 直接下单”，而是：

```text
Ubuntu Server
├── ashare-system-v2 API / scheduler / review / learning
├── OpenClaw gateway
└── 只读聚合与控制面

Windows VM
├── QMT
└── Windows Execution Gateway
```

也就是说：

- Linux 是 `control plane`
- Windows 是 `execution plane`
- `ASHARE_EXECUTION_PLANE=windows_gateway` 应视为正式部署默认值

## Windows Gateway 联调要求

Windows VM 不需要承载完整业务，只要满足下面最小集合：

1. `QMT` 正常登录并保持会话
2. Windows Execution Gateway 可以访问 Linux control plane
3. Windows Gateway 能按既定字段上报执行桥健康状态

主备固定口径继续沿用项目现有约定：

- 主：
  - `source_id=windows-vm-a`
  - `deployment_role=primary_gateway`
  - `bridge_path=linux_openclaw -> windows_gateway -> qmt_vm`
- 备：
  - `source_id=windows-vm-b`
  - `deployment_role=backup_gateway`
  - `bridge_path=linux_openclaw -> windows_gateway_backup -> qmt_vm`

## 部署后的最小验收

服务器搭好后，不要只看“服务跑起来了”，至少按下面顺序验收：

### 1. Linux 本机验收

```bash
cd /srv/projects/ashare-system-v2
./scripts/health_check.sh
curl "http://127.0.0.1:8100/system/deployment/linux-control-plane-startup-checklist"
curl "http://127.0.0.1:8100/system/reports/postclose-deployment-handoff"
curl "http://127.0.0.1:8100/system/reports/postclose-master"
```

应重点看：

- API 已启动
- startup checklist 返回 `status`
- handoff bundle 可读
- postclose master 可读

### 2. Windows VM 到 Linux 联通验收

从 Windows VM 上验证：

- 能访问 Linux control plane 地址和端口
- 不是访问 `127.0.0.1`
- 能拿到：
  - `/system/deployment/windows-execution-gateway-onboarding-bundle`
  - `/system/deployment/linux-control-plane-startup-checklist`

### 3. 执行桥验收

当 Windows Gateway 开始上报后，应确认：

- `/system/reports/postclose-master` 中存在 `latest_execution_bridge_health`
- `/system/reports/postclose-master` 中存在 `latest_review_board`
- `/system/reports/postclose-master` 中存在：
  - `latest_priority_board`
  - `latest_governance_effects`

这说明盘后总入口已经能直接读到统一治理结果，而不是还要客户端自己拼接。

## 运行建议

当前阶段建议的运行方式是：

- Linux control plane 优先用正式 `systemd service`
- 临时调试才用 `tmux`
- Windows VM 保持 QMT 与 Gateway 常驻
- 盘后检查优先看：
  - `/system/reports/postclose-master`
  - `/system/reports/postclose-deployment-handoff`
  - `/system/reports/review-board`

推荐的控制面运维命令：

```bash
cd /srv/projects/ashare-system-v2
./scripts/ashare_service.sh status
./scripts/ashare_service.sh logs 100
./scripts/openclaw_gateway_service.sh status
./scripts/openclaw_gateway_service.sh logs 100
./scripts/print_windows_gateway_handoff.sh
./scripts/health_check.sh
```

当前不建议：

- 让 Linux 直接持有 QMT 会话
- 在 Ubuntu 上继续使用写死的 Windows Python 路径
- 把 Windows VM 再扩成整套主业务宿主机

## 对当前项目的建议

如果当前项目还在 Windows 侧运行，短期可以接受，但只建议作为过渡状态。

建议目标态：

- Linux：运行 `ashare-system`
- Windows：只运行 `QMT`

这样后续：

- 监控更清晰
- 备份更简单
- 服务更稳定
- 系统职责边界更明确

结合当前仓库，审核意见再补三条：

1. 当前项目主线已经按 `Linux control plane + Windows Execution Gateway + QMT` 收口，方向与本文一致。
2. 当前仓库里的核心启动脚本已补齐 Ubuntu Server 运行口径，但正式生产环境仍建议你把 `.env`、日志目录、服务守护方式固定下来，不要继续依赖开发态默认值。
3. 因此，本文现在通过的是“基础设施 + 当前应用运行口径审核”，不是“所有生产守护细节都已经自动化完成”。

## 风险点

当前方案最主要的风险只有三项：

1. 单盘无冗余，必须尽早做备份
2. 如果 Windows VM 里塞太多业务，会重新变成“Windows 承载一切”
3. 如果 Linux 与 Windows 的桥接协议不清晰，后续迁移会反复返工

## 给主开发者的审核点

建议重点审核以下问题：

1. `ashare-system` 哪些模块必须留在 Windows，哪些可以迁到 Linux
2. Linux 与 Windows 之间采用什么通讯方式
3. Windows VM 最小保留能力是什么
4. `/srv` 目录结构是否符合后续部署与备份习惯
5. 是否先按 NAT 落地，再决定是否桥接
6. Windows VM 到 Linux control plane 的实际访问地址如何固定

## 最终结论

这份方案现在保留且已经收口的一条主线是：

- `Ubuntu Server` 做宿主机
- `Windows 虚拟机` 只跑 `QMT + Windows Execution Gateway`
- 主业务逐步迁到 Linux control plane

作为当前项目的最终审核意见：

- 本方案通过，可作为你当前服务器部署与后续运行的依据
- 基础设施、应用运行口径、只读验收入口，已经可以按本文直接执行
- 后续如果再补，只需要补：
  - systemd / 开机自启细节
  - Windows Gateway 服务化细节
  - 备机切换与备份恢复流程

基于当前已确认硬件：

- `Core Ultra 9 285H`
- `32GB DDR5`
- `1TB SSD`

可以直接按本文开始：

1. 安装 `Ubuntu Server 24.04 LTS`
2. 安装 `KVM / libvirt / Cockpit`
3. 创建并配置 `Windows VM`
4. 在 Windows VM 内安装 `QMT`
5. 完成 `/srv` 目录与基础安全配置
6. 在 Linux 落位 `ashare-system-v2`
7. 用本文的运行口径完成 control plane、OpenClaw gateway、Windows Gateway 联调
8. 用本文的只读验收入口完成部署验收

这份文档到这里为止，不再只是“基础设施审核稿”，而是当前项目可落地执行的部署与运行稿。
