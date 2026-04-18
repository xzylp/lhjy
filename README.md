# ashare-system-v2

A 股智能量化交易系统 v2，默认部署口径为 `Linux control plane + Windows Execution Gateway + QMT`，不再假设 Linux 本地直连下单。

## 核心特性

- **多市场支持**: 主板(600/000)、创业板(300)、科创板(688)、北交所
- **智能选股漏斗**: 初筛 → 环境 → 因子 → AI → 行业分散 → Top-N
- **三大实战策略**: 动量策略、均值回归、放量突破
- **动态风控**: 6大风控规则、凯利仓位、情绪保护
- **AI 模型**: XGBoost 选股评分、LSTM/Transformer 趋势预测、PPO 仓位优化
- **实时监控**: 涨跌幅监控、龙虎榜分析、异常预警
- **无人值守**: Windows 计划任务自动启动 + 崩溃自愈
- **Agent 对话**: 通过 OpenClaw + 飞书实现自然语言交互

## 技术架构

```
┌─────────────────────────────────────────────────────────────┐
│                  OpenClaw Agent / main                │
│                (Linux control plane)                  │
└─────────────────────────────┬───────────────────────────┘
                             │ HTTP :8100
     ┌───────────────────────┴───────────────────────┐
     │                  FastAPI                      │
     │  ┌─────────┐ ┌─────────┐ ┌────────────────┐   │
     │  │ 系统API  │ │ 市场API │ │ 执行意图/审计 │   │
     │  └─────────┘ └─────────┘ └────────────────┘   │
     └────────────────────────────────────────────────┘
                              │
     ┌────────────────────────┬──────────────────────┐
     ▼                        ▼                      ▼
┌────────────┐        ┌────────────┐        ┌──────────────────┐
│ 候选/风控/审计 │        │ state/reports │        │ Windows Execution│
│ 讨论与编排     │        │ 只读聚合      │        │ Gateway + QMT    │
└────────────┘        └────────────┘        └──────────────────┘
```

## 目录结构

```
ashare-system-v2/
├── src/ashare_system/           # 核心代码 (119 个 Python 文件)
│   ├── ai/                     # AI 模型 (XGBoost, LSTM, Transformer)
│   ├── ai_advanced/            # 高级 AI (Ensemble, PPO)
│   ├── apps/                  # FastAPI 路由
│   ├── backtest/               # 回测引擎
│   ├── data/                  # 数据获取与清洗
│   ├── factors/               # 因子库 (150+)
│   │   ├── base/             # 基础因子
│   │   ���── micro/            # 微观结构
│   │   ├── behavior/         # 行为金融
│   │   └── macro/           # 宏观因子
│   ├── infra/               # 基础设施 (QMT, 过滤器)
│   ├── learning/             # 持续学习
│   ├── monitor/             # 实时监控
│   ├── notify/             # 飞书推送
│   ├── report/             # 日报生成
│   ├── risk/               # 风控规则
│   ├── sentiment/           # 市场情绪
│   ├── strategy/           # 交易策略
│   └── scheduler.py          # 任务调度
├── scripts/                  # 运维脚本
│   ├── ashare_service.sh     # Linux systemd 服务启停
│   ├── ashare_scheduler_service.sh # Linux scheduler 服务启停
│   ├── ashare_feishu_longconn_service.sh # 飞书长连接 worker 启动包装
│   ├── ashare_feishu_longconn_ctl.sh # 飞书长连接 systemd 服务启停
│   ├── install_linux_systemd_service.sh # 安装 Linux systemd 服务
│   ├── install_linux_scheduler_service.sh # 安装 Linux scheduler 服务
│   ├── install_feishu_longconn_service.sh # 安装飞书长连接 systemd 服务
│   ├── install_openclaw_gateway_service.sh # 安装 OpenClaw systemd 服务
│   ├── start.sh             # Linux 启动
│   ├── start_openclaw_gateway.sh # Linux 侧 OpenClaw Gateway 启动
│   ├── print_windows_gateway_handoff.sh # 输出 Windows 接线摘要
│   ├── start_unattended.ps1  # Windows 无人值守启动
│   ├── setup_autostart.ps1  # 配置开机自启
│   ├── openclaw_gateway_service.sh # OpenClaw systemd 服务启停
│   └── health_check.sh      # Linux control plane 健康检查
├── openclaw/               # OpenClaw 配置
│   ├── prompts/ashare.txt  # Agent Prompt
│   └── workflow.final.json
├── tests/                  # 测试用例 (119 个)
└── .venv/                  # 虚拟环境
```

## 快速开始

### 1. 环境准备

```bash
# Linux control plane / WSL
cd /mnt/d/Coding/lhjy/ashare-system-v2

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

### 2. 配置 .env

```bash
# 复制并编辑
cp .env.example .env

# 关键配置
ASHARE_RUN_MODE=dry-run           # dry-run | paper | real
ASHARE_EXECUTION_MODE=mock        # mock | xtquant
ASHARE_EXECUTION_PLANE=windows_gateway
ASHARE_XTQUANT_ROOT=../XtQuant
ASHARE_PYTHON_BIN=/srv/projects/ashare-system-v2/.venv/bin/python   # Ubuntu Server 可选
OPENCLAW_BIN=/usr/local/bin/openclaw                                # Ubuntu Server 可选
ASHARE_PUBLIC_BASE_URL=http://192.168.99.16:8100                    # Windows Gateway 接线推荐
```

说明：

- 默认部署不假设 Linux 本地直连 QMT。
- `ASHARE_EXECUTION_PLANE=windows_gateway` 表示 Linux 侧只负责 control plane，真正执行写口在 Windows Gateway。
- Windows Gateway 的启动参数样例与主备 `source_id / deployment_role / bridge_path` 固定值，见 [openclaw-linux-qmt-deployment-v1.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/openclaw-linux-qmt-deployment-v1.md)。

### 3. 启动服务

```bash
# 方式一: 启动 Linux control plane (推荐)
./scripts/start.sh

# 方式二: 启动 Linux 侧 OpenClaw Gateway
./scripts/start_openclaw_gateway.sh

# 方式三: Windows 无人值守模式 (开机自启)
powershell.exe -ExecutionPolicy Bypass -File scripts\start_unattended.ps1

# 方式四: 手动启动 API
./.venv/bin/python -m ashare_system.run serve
```

Ubuntu Server 正式部署建议改用 `systemd` 常驻：

```bash
cd /srv/projects/ashare-system-v2
./scripts/install_linux_systemd_service.sh
./scripts/ashare_service.sh start
./scripts/ashare_service.sh status
./scripts/install_linux_scheduler_service.sh
./scripts/ashare_scheduler_service.sh start
./scripts/install_feishu_longconn_service.sh
./scripts/ashare_feishu_longconn_ctl.sh start
./scripts/ashare_feishu_longconn_ctl.sh status
./scripts/install_openclaw_gateway_service.sh
./scripts/openclaw_gateway_service.sh start
./scripts/print_windows_gateway_handoff.sh
```

### 4. 健康检查

```bash
# 检查服务状态
./scripts/health_check.sh
```

Linux control plane 启动后，还应额外查看：

- `GET /system/deployment/linux-control-plane-startup-checklist`
- `GET /system/deployment/windows-execution-gateway-onboarding-bundle`
- `GET /system/operations/components`
- `GET /system/feishu/longconn/status`

### 5. 访问 API

```bash
# WSL 侧探测当前可达地址
./scripts/ashare_api.sh probe

# 系统健康
./scripts/ashare_api.sh GET /health

# 账户余额
./scripts/ashare_api.sh GET /execution/balance/8890130545

# 当前配置
./scripts/ashare_api.sh GET /system/config
```

## Agent 对话示例

通过飞书与 `main -> ashare` 链路对话:

| 用户指令 | 系统动作 |
|---------|---------|
| "开通创业板" | `POST /system/config {"scope.allow_chinext": true}` |
| "每次最多买2只" | `POST /system/config {"max_buy_count": 2}` |
| "单票最多投3万" | `POST /system/config {"max_single_amount": 30000}` |
| "暂停交易" | `POST /system/config {"emergency_stop": true}` |
| "今天市场怎么样" | `main` 转 `ashare`，由 `ashare-research` / `ashare-runtime` 汇总 |
| "推荐什么股票" | `main` 转 `ashare`，由 `ashare-runtime` 触发运行链并返回结果 |

## 动态配置参数

| 参数 | 说明 | 默认值 |
|-----|------|-------|
| `max_buy_count` | 每次最多买入几只 | 3 |
| `max_hold_count` | 最多同时持有几只 | 5 |
| `max_total_position` | 总仓位上限 0-1 | 0.8 |
| `max_single_amount` | 单票最高金额(元) | 50000 |
| `scope.allow_main_board` | 允许主板600/000 | true |
| `scope.allow_chinext` | 允许创业板300 | false |
| `scope.allow_star` | 允许科创板688 | false |
| `daily_loss_limit` | 日亏损上限 | 0.05 |
| `emergency_stop` | 紧急停止交易 | false |

## 测试

```bash
# 运行所有测试
pytest

# 运行特定模块
pytest tests/test_strategy/
pytest tests/test_risk/

# 生成覆盖率报告
pytest --cov=src/ashare_system --cov-report=html
```

## 当前完成度

| 模块 | 完成度 | 状态 |
|-----|-------|------|
| 数据管道 | 90% | 可用 |
| ��子库 (150+) | 95% | 可用 |
| 三大策略 | 100% | 可用 |
| AI 模型 | 80% | 需实盘验证 |
| 风控规则 | 95% | 可用 |
| 选股漏斗 | 100% | 可用 |
| 回测引擎 | 90% | 可用 |
| 调度器 | 85% | 需实盘验证 |
| 飞书推送 | 100% | 可用 |
| 无人值守 | 100% | 可用 |
| 测试用例 (119个) | 100% | 通过 |

## 依赖环境

- Python 3.12+
- Windows 10/11 或 WSL2
- 国金证券 QMT 交易端
- 飞书机器人 (可选)

## 注意事项

1. **实盘前必须测试**: 先用 dry-run/mock 模式验证流程
2. **风控优先**: emergency_stop=true 时拒绝所有买入
3. **T+1 规则**: A 股当日买入次日才能卖出
4. **涨跌停限制**: 创业板/科创板 20%，主板 10%，北交所 30%
5. **数据延迟**: 行情数据有 15 分钟延迟，实盘需注意

## 许可证

MIT License
