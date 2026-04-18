# Agent 接口与机器人主线流程

## 目标

本文件定义三件事：

1. 程序给 agent 暴露什么接口能力。
2. 机器人面板该怎么组织版面。
3. 从盘前到盘后的主线流程怎么跑。

核心边界保持不变：

- agent 是大脑，负责发现机会、组织讨论、形成提案、完成量化交易判断。
- 程序是手和脚，负责数据提供、执行落地、流程监督、业绩考核、风控数据与电子围栏。

## Agent 接口面

### 全局入口

- `/system/workspace-context`
  - 用途：先看全局态势。
- `/data/catalog`
  - 用途：告诉 agent 数据在哪、该先读什么。
- `/system/monitoring/cadence`
  - 用途：告诉 agent 当前 candidate/focus/execution 节奏。
- `/system/discussions/meeting-context`
  - 用途：看统一讨论上下文。
- `/system/discussions/client-brief`
  - 用途：看当前对外简报。

### 角色分工

#### `ashare`

- 读取：
  - `/system/workspace-context`
  - `/system/monitoring/cadence`
  - `/system/discussions/meeting-context`
  - `/system/discussions/client-brief`
  - `/system/feishu/briefing`
- 写入：
  - `/system/discussions/cycles/bootstrap`
  - `/system/discussions/cycles/{trade_date}/rounds/{round_number}/start`
  - `/system/discussions/cycles/{trade_date}/refresh`
  - `/system/discussions/cycles/{trade_date}/finalize`
  - `/system/adjustments/natural-language`
  - `/system/params/proposals`

#### `ashare-runtime`

- 读取：
  - `/data/runtime-context/latest`
  - `/data/monitor-context/latest`
  - `/system/cases`
  - `/system/monitoring/cadence`
- 触发：
  - `/runtime/jobs/pipeline`
  - `/runtime/jobs/intraday`

#### `ashare-research`

- 读取：
  - `/data/event-context/latest`
  - `/data/dossiers/latest`
  - `/system/research/summary`
  - `/system/discussions/agent-packets`
- 触发：
  - `/research/sync`
  - `/research/events/news`
  - `/research/events/announcements`

#### `ashare-strategy`

- 读取：
  - `/data/market-context/latest`
  - `/data/dossiers/latest`
  - `/strategy/strategies`
  - `/strategy/screen`
  - `/system/discussions/agent-packets`

#### `ashare-risk`

- 读取：
  - `/data/market-context/latest`
  - `/data/event-context/latest`
  - `/data/dossiers/latest`
  - `/system/params`
  - `/system/agent-scores`

#### `ashare-audit`

- 读取：
  - `/system/discussions/meeting-context`
  - `/system/discussions/reply-pack`
  - `/system/discussions/finalize-packet`
  - `/system/agent-scores`
- 触发：
  - `/system/agent-scores/settlements`

#### `ashare-executor`

- 读取：
  - `/system/discussions/execution-precheck`
  - `/system/discussions/execution-intents`
  - `/system/discussions/execution-dispatch/latest`
- 触发：
  - `/system/discussions/execution-intents/dispatch`

## 机器人版面

机器人不应该把所有接口平铺给用户，而应该组织成五块：

### 1. 当前状态

- 目标：给知情权
- 主入口：`/system/feishu/briefing`
- 回退入口：
  - `/system/workspace-context`
  - `/system/monitoring/cadence`

### 2. 当前建议

- 目标：给用户看 selected/watchlist/rejected
- 主入口：`/system/discussions/client-brief`
- 回退入口：
  - `/system/discussions/final-brief`
  - `/system/discussions/reply-pack`

### 3. 执行状态

- 目标：给用户看 execution precheck / preview / submitted / blocked
- 主入口：`/system/discussions/execution-dispatch/latest`
- 回退入口：
  - `/system/discussions/execution-precheck`

### 4. 调参与治理

- 目标：给调参权
- 主入口：`/system/feishu/rights`
- 动作入口：
  - `/system/feishu/adjustments/natural-language`
  - `/system/params/proposals`
  - `/system/agent-scores`

### 5. 问答入口

- 目标：给询问权
- 主入口：`/system/feishu/ask`
- 支持主题：
  - `status`
  - `discussion`
  - `execution`
  - `params`
  - `scores`

## 主线流程

### 盘前预热

- 先看：
  - `/system/workspace-context`
  - `/system/monitoring/cadence`
  - `/system/feishu/briefing`
- 再做：
  - `/runtime/jobs/pipeline`
  - `/system/precompute/dossiers`

### 盘中发现

- 运行、研究、策略并行工作。
- 先读统一包：
  - `/system/discussions/agent-packets`
- 再按职责下钻：
  - runtime -> `/data/runtime-context/latest`
  - research -> `/data/event-context/latest`
  - strategy -> `/data/market-context/latest`

### 讨论收敛

- `ashare` 组织讨论。
- 相关接口：
  - `/system/discussions/cycles/bootstrap`
  - `/system/discussions/cycles/{trade_date}/rounds/{round_number}/start`
  - `/system/discussions/cycles/{trade_date}/refresh`
  - `/system/discussions/cycles/{trade_date}/finalize`

### 执行预演与派发

- 先预检，再看 intents，再决定 preview 或 apply。
- 相关接口：
  - `/system/discussions/execution-precheck`
  - `/system/discussions/execution-intents`
  - `/system/discussions/execution-intents/dispatch`

### 通知、调参、考核

- 飞书知情：
  - `/system/feishu/briefing`
  - `/system/feishu/briefing/notify`
- 飞书调参：
  - `/system/feishu/adjustments/natural-language`
- 飞书询问：
  - `/system/feishu/ask`
- 绩效考核：
  - `/system/agent-scores`
  - `/system/agent-scores/settlements`

### 盘后学习与夜间沙盘

- 读取：
  - `/system/research/summary`
  - `/system/discussions/finalize-packet`
- 进入学习/治理：
  - `/system/params/proposals`

## 已落地的程序入口

为了让机器人和 agent 不靠硬编码猜流程，控制面已新增三类结构化入口：

- `/system/agents/capability-map`
- `/system/robot/console-layout`
- `/system/workflow/mainline`

建议用法：

- agent 初始化时先读 `/system/agents/capability-map`
- 机器人面板初始化时先读 `/system/robot/console-layout`
- 主协调流程初始化时先读 `/system/workflow/mainline`
