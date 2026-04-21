# ashare-system-v2 技术手册

> 版本：2026-04-18 当前框架版  
> 用途：作为本仓库的技术档案，描述当前真实架构、代码完成度、关键模块、运行链路与剩余代码差距。  
> 口径：本手册以仓库当前代码和 `task.md` 为准，不以早期设想文档为准。

## 1. 项目定位

`ashare-system-v2` 不是一个单纯的选股脚本，也不是一个只会跑 pipeline 的 FastAPI 服务。

当前它的正式定位是：

- Agent 驱动的 A 股量化交易控制面
- runtime 原子策略仓库
- 讨论、监督、治理、执行电子围栏与审计留痕中枢

职责边界如下：

- Agent 负责：
  - 理解市场
  - 组织候选发现
  - 选择或组合工具
  - 发起讨论与质询
  - 形成提案与执行建议
- 程序负责：
  - 数据获取与 serving
  - factor / playbook / compose 执行
  - 风控与执行预检
  - 讨论状态机
  - 审计、治理、评分与学习闭环
  - 飞书问答、监督和状态输出

## 2. 当前总体架构

### 2.1 顶层拓扑

```text
Feishu / Web / Operator
  -> ashare-system-v2 FastAPI control plane
  -> runtime / discussion / supervision / governance / reports
  -> Go data platform / Windows execution gateway / state stores
```

如果按正式部署口径展开：

```text
Linux control plane
  ├─ ashare-system-v2 API
  ├─ scheduler
  ├─ go-data-platform
  ├─ feishu-longconn
  └─ openclaw-gateway

Windows execution side
  ├─ windows execution gateway
  └─ QMT
```

本文重点只看代码框架，不讨论 QMT 接线是否可用。

### 2.2 五层结构

当前主线已经不是“固定 runtime 流程”，而是五层结构：

#### L1 市场理解层

- 主体：Agent
- 目标：给出市场阶段、主线方向、风险偏好、持仓动作
- 主要输入：
  - `workspace-context`
  - `market-context`
  - `event-context`
  - `account-state`
  - `execution-precheck`

#### L2 候选发现层

- 主体：程序提供入口，Agent 自主选择
- 当前候选入口已包含：
  - 全市场扫描
  - 板块热点
  - 新闻催化
  - 持仓监控
  - 日内做 T
  - 尾盘潜伏
  - Agent 自提机会票

#### L3 Compose 编排层

- 主体：Agent 输出 brief，runtime 执行
- 当前已落：
  - `POST /runtime/jobs/compose`
  - `POST /runtime/jobs/compose-from-brief`
  - `GET /runtime/capabilities`
  - `GET /runtime/strategy-repository`

#### L4 风控与执行层

- 主体：程序
- 当前已落：
  - 执行预检
  - execution intents
  - dispatch / preview / submitted / blocked
  - 电子围栏
  - 执行回执与对账

#### L5 学习与治理层

- 主体：程序，供 Agent 消费
- 当前已落：
  - agent score
  - outcome ledger
  - learned asset
  - advice queue
  - review board
  - nightly sandbox

## 3. 当前主链

### 3.1 盘前到盘后主链

```text
调度 / 手动触发
  -> runtime pipeline / intraday / event fetch
  -> candidate cases
  -> discussion cycle bootstrap
  -> round 1
  -> refresh
  -> round 2 / finalize
  -> execution precheck
  -> execution intents
  -> dispatch preview / submitted
  -> execution reconciliation
  -> review board
  -> nightly sandbox
  -> settlement / learning / governance
```

### 3.2 当前主要状态载体

- `workspace_context`
- `runtime_context`
- `discussion_context`
- `monitor_context`
- `client_brief`
- `execution_precheck`
- `execution_dispatch_latest`
- `latest_execution_reconciliation`
- `latest_review_board`
- `latest_nightly_sandbox`

### 3.3 当前关键 API 入口

#### runtime / strategy

- `GET /runtime/health`
- `GET /runtime/capabilities`
- `GET /runtime/strategy-repository`
- `GET /runtime/strategy-repository/panel`
- `POST /runtime/jobs/pipeline`
- `POST /runtime/jobs/intraday`
- `POST /runtime/jobs/compose`
- `POST /runtime/jobs/compose-from-brief`
- `POST /runtime/learned-assets/transition`
- `GET /runtime/learned-assets/panel`
- `GET /runtime/learned-assets/advice`
- `POST /runtime/learned-assets/advice/resolve`

#### discussion / execution

- `GET /system/discussions/client-brief`
- `GET /system/discussions/meeting-context`
- `GET /system/discussions/reply-pack`
- `GET /system/discussions/final-brief`
- `GET /system/discussions/cycles/{trade_date}`
- `POST /system/discussions/cycles/bootstrap`
- `POST /system/discussions/cycles/{trade_date}/rounds/{round}/start`
- `POST /system/discussions/cycles/{trade_date}/refresh`
- `POST /system/discussions/cycles/{trade_date}/finalize`
- `GET /system/discussions/execution-precheck`
- `GET /system/discussions/execution-intents`
- `POST /system/discussions/execution-intents/dispatch`
- `GET /system/discussions/execution-dispatch/latest`

#### supervision / feishu / ops

- `GET /system/workspace-context`
- `GET /system/monitoring/cadence`
- `GET /system/agents/supervision-board`
- `POST /system/agents/supervision/check`
- `POST /system/feishu/ask`
- `POST /system/feishu/events`
- `GET /system/feishu/briefing`
- `GET /system/feishu/longconn/status`
- `POST /system/startup-recovery/run`
- `POST /system/execution-reconciliation/run`
- `GET /system/deployment/service-recovery-readiness`
- `GET /system/deployment/controlled-apply-readiness`

## 4. 源码结构与职责

### 4.1 目录结构

```text
src/ashare_system/
├── apps/                 # API 路由层，system/runtime/research/market/execution
├── data/                 # archive、serving、event bus、auction、workspace 聚合
├── discussion/           # case、cycle、summary、brief、packet、finalize
├── infra/                # adapter、go client、audit、healthcheck、state
├── learning/             # score、settlement、attribution、prompt patch、registry update
├── monitor/              # cadence、heartbeat、event watch、polling state
├── notify/               # 飞书模板、推送、变化通知
├── risk/                 # execution guard、规则与电子围栏
├── strategy/             # factor/playbook/composer/repository/nightly sandbox
├── scheduler.py          # 调度总入口
├── container.py          # 依赖装配
└── run.py                # CLI
```

### 4.2 关键模块

#### `apps/system_api.py`

- 控制面主路由
- 负责：
  - 讨论态
  - 执行态
  - supervision
  - 飞书问答
  - readiness / deployment 检查
  - startup recovery / reconciliation

#### `apps/runtime_api.py`

- runtime 工具库入口
- 负责：
  - pipeline / intraday / compose
  - capabilities
  - strategy repository
  - learned assets

#### `strategy/factor_registry.py`

- 当前第一批因子库注册表
- 不是早期 README 中那种“150+ 因子”的状态
- 当前真实口径应理解为：
  - 已完成第一批核心因子落地
  - 还没到“完整 Alpha Factory 工业规模”

#### `strategy/playbook_registry.py`

- 当前第一批战法注册表
- 已从“写死策略模板”转为“原子战法 + 统一 evaluate 接口”

#### `strategy/strategy_composer.py`

- 当前 runtime 编排核心
- 负责：
  - 组合 factor / playbook
  - 处理约束
  - 形成候选解释
  - 接 learned asset 偏置

#### `discussion/`

- 当前多 Agent 讨论协议与状态机核心
- 包含：
  - case
  - cycle
  - round 推进
  - final brief / reply pack / meeting context

#### `scheduler.py`

- 当前所有自动任务的总编排入口
- 已接：
  - 竞价
  - 微观巡检
  - 事件扫描
  - review board
  - nightly sandbox
  - settlement
  - 增量训练

## 5. 代码面完成度

### 5.1 已完成部分

按 `task.md` 当前统计：

- 功能模块 A-H 完成度：`47 / 47 = 100%`
- 集合竞价、盘中微观、波峰波谷出场、事件响应、多 Agent 论证、归因自进化、夜间沙盘、数据链整固均已落地

主框架层面已完成：

- Agent 中心化改造骨架
- runtime 工具库化
- strategy repository
- learned asset 状态流转第一版
- discussion cycle 泛化
- supervision 任务派发与质量口径
- 飞书交易台化问答
- Linux 控制面运行与健康检查

### 5.2 部分完成部分

以下条目已做出主体，但还没完全收口：

#### R0.6 因子仓库

现状：

- 第一批核心因子已落地并可运行
- 已接真实 `market_adapter`

未收口：

- 因子规模、分层、赛马密度还不够
- 还不能视为完整 Alpha Factory

#### R0.7 战法插件层

现状：

- 第一批经典战法已落地
- 已统一接入 registry / evaluate

未收口：

- 战法覆盖面还不够广
- 版本赛马还未完全成体系

#### R0.10 评估层

现状：

- 已有 compose 评估账本
- 已接离线回测与 Rank IC 分析

未收口：

- 真实结果驱动的评价面还需继续收紧
- 账本到治理建议的自动收口仍不完整

#### R0.11 runtime 仓库生产化

现状：

- 资产注册、查询、版本视图、runtime mode、panel、治理建议已在

未收口：

- 灰度发布编排不完整
- 批量切流未完成
- 自动切流未完成

#### R0.13 / R0.14 学习产物治理

现状：

- `draft / review_required / active / archived / rejected` 状态模型已在
- advice ingest / resolve / 可选 transition 已在

未收口：

- 审批评估链不完整
- 自动联动不足
- 更广的正式对象反查未完成

#### S1.3.3 监督消费闭环

现状：

- 写回观点后已能标记完成并 refresh cycle

未收口：

- 研究摘要
- 执行回执
- 盘后学习
- 更广义产物自动消费

#### S1.3.5 / 3.6 / 3.7 监督续发

现状：

- 已支持四岗材料齐备后的自动续发
- 已接仓位语境和执行结果语境

未收口：

- 满仓替换
- 真实成交后学习归因
- 盘后参数调整
- 更细粒度持仓管理

## 6. 距离“代码面完全上线”还差什么

如果只回答“服务能不能跑”，答案是：能。  
如果回答“代码面是否已经完全生产化”，答案是：还没有。

当前剩余差距不是原子功能缺失，而是以下四类收口：

### 6.1 监督自动推进链收口

还需要让更多真实产物自动推动主线，而不只靠观点写回推进 round。

### 6.2 学习产物治理闭环收口

还需要让 learned asset 从建议、审批、转正、复盘之间形成更完整的自动链。

### 6.3 runtime 仓库治理收口

还需要把“可注册、可查看、可建议”推进到“更完整的灰度治理和版本切流”。

### 6.4 交易台输出收口

还需要继续压缩“能答但不够像交易台”的边角场景，确保飞书、前端、briefing、client brief 输出口径统一。

## 7. 实际可运行状态

结合当前 `task.md`：

- Linux 控制面已实际通过健康检查
- Go 数据平台、调度器、飞书长连接、OpenClaw Gateway 均可在线
- 上一交易日 `2026-04-17` 的正式准入口径已实测全绿

对应记录见：

- [task.md](/srv/projects/ashare-system-v2/task.md)

因此，代码面现在更准确的判断是：

```text
已经具备上线运行条件，但还没到“所有自治闭环全部收官”的状态。
```

## 8. 推荐阅读顺序

### 如果你想看总体框架

- [README](/srv/projects/ashare-system-v2/README.md)
- [Agent 自主编排与 101 因子库实施细则](/srv/projects/ashare-system-v2/docs/agent_autonomy_factor_library_plan_20260417.md)

### 如果你想看主流程

- [Agent 接口与机器人主线流程](/srv/projects/ashare-system-v2/docs/agent_robot_mainline_workflow.md)
- [runtime Agent 协议草案](/srv/projects/ashare-system-v2/docs/runtime_agent_protocol_draft_20260415.md)
- [多 Agent 讨论协议 v1](/srv/projects/ashare-system-v2/docs/multi-agent-deliberation-protocol-v1.md)

### 如果你想看当前真实进度

- [task.md](/srv/projects/ashare-system-v2/task.md)

### 如果你想看飞书侧操作

- [飞书三权与机器人主线操作手册](/srv/projects/ashare-system-v2/docs/feishu_operator_manual_20260416.md)

## 9. 维护原则

后续更新本手册时，统一遵守这三个原则：

- 只写当前代码和任务进度已经成立的事实
- 不再沿用早期 README 中明显过时的“150+ 因子、三大策略、固定漏斗”叙事
- “可运行”和“完全收官”必须分开写，避免误导
