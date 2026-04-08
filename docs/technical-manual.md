# ashare-system-v2 技术手册

> 版本: 0.3.0  
> 更新日期: 2026-04-06

## 一、系统定位

`ashare-system-v2` 是 A 股量化运行中台，负责向 OpenClaw 量化团队提供统一的运行、研究、策略、风控、执行、审计和讨论治理数据面。

系统目标不是让前台 agent 直接“懂所有量化细节”，而是把量化能力沉淀为稳定 API，再由 OpenClaw 的 `main -> ashare -> ashare子团队` 协作链进行编排。

## 二、当前正式架构

### 2.1 顶层职责

```text
Feishu / WebChat
  -> OpenClaw Gateway
  -> main
  -> ashare
  -> ashare-runtime / ashare-research / ashare-strategy / ashare-risk / ashare-executor / ashare-audit
  -> ashare-system-v2 FastAPI
  -> QMT / XtQuant / state / reports / audits
  -> main 汇总后对外回复
```

### 2.2 OpenClaw 职责边界

| Agent | 定位 | 主要职责 |
|------|------|----------|
| `main` | 唯一前台入口 | 判断任务类型、路由专业团队、统一对外回复 |
| `ashare` | 量化中台总调度 | 承接股票与量化问题，拆分给量化子角色并汇总 |
| `ashare-runtime` | 运行面 | 健康检查、pipeline、intraday、运行报告 |
| `ashare-research` | 研究面 | 研究同步、新闻/公告入库、研究摘要 |
| `ashare-strategy` | 策略面 | 候选股解释、排序依据、策略比较、观察名单 |
| `ashare-risk` | 风控闸门 | allow / limit / reject、账户与配置约束检查 |
| `ashare-executor` | 执行面 | 账户、持仓、订单、成交、已放行执行意图落地 |
| `ashare-audit` | 审计复核 | 审计、会议、运行报告、研究摘要复盘 |

### 2.3 正式路由原则

- `main` 是唯一前台入口，飞书与 webchat 不再直连 `ashare`。
- `ashare` 不是前台客服，而是量化域总调度。
- `ashare` 默认先分流，再汇总，不越权替代子角色。
- 最终用户可见回复始终由 `main` 输出。

## 三、系统源码结构

```text
ashare-system-v2/
├── src/ashare_system/
│   ├── apps/              # FastAPI 路由层
│   ├── strategy/          # 选股、决策、仓位
│   ├── risk/              # 规则、守卫、情绪保护
│   ├── data/              # 数据获取、清洗、缓存
│   ├── factors/           # 因子计算与筛选
│   ├── ai/                # 模型推理与训练框架
│   ├── report/            # 运行与盘后报告
│   ├── notify/            # 飞书消息
│   ├── learning/          # 学习与复盘
│   ├── infra/             # XtQuant/QMT/状态/Audit 基础设施
│   ├── scheduler.py       # 定时调度
│   ├── container.py       # 依赖注入
│   └── run.py             # CLI 入口
├── scripts/
├── docs/
├── openclaw/
└── tests/
```

## 四、OpenClaw 与程序的连接方式

### 4.1 正式连接方式

- OpenClaw 子角色读取和调用 `ashare-system-v2` 的 FastAPI。
- Windows 侧 FastAPI 启动后会写入 `.ashare_state/service_endpoints.json`。
- WSL 侧通过 `scripts/ashare_api.sh` 先读 manifest，再动态探测并访问 Windows 服务。
- `start_unattended.ps1` 只负责 Windows 侧 FastAPI + scheduler，不再管理 gateway。
- gateway 由 `scripts/start_openclaw_gateway.sh` 单独启动和重启。

### 4.2 设计原则

- 不把 gateway 生命周期和量化服务生命周期耦合在同一脚本里。
- 不让前台 `main` 直接背负运行、研究、执行等量化职责。
- 不依赖旧 session、旧 memory 或旧 prompt 去“猜”量化入口。
- 用清晰路由和 WSL 直连代替隐式包装和历史会话补丁。
- 配置变更默认通过新会话生效，保留历史会话用于追溯，不把清历史当成上线前提。

### 4.3 未来生产部署目标

当前文档描述的正式运行链路以开发机 `WSL + Windows` 联调为基础；未来生产部署目标改为：

- Linux 服务器运行：
  - `OpenClaw Gateway`
  - `main / ashare / ashare 子团队`
  - `ashare-system-v2 FastAPI`
- Windows 虚拟机运行：
  - `Windows Execution Gateway`
  - `QMT / XtQuant`

目标拓扑见：

- [openclaw-linux-qmt-deployment-v1.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/openclaw-linux-qmt-deployment-v1.md)

### 4.4 执行边界

- Agent 可以生成候选、讨论观点、execution intent 和 review 结论。
- Agent 不直接持有 QMT 下单权。
- 真正调用 QMT 的唯一写口应是 `Windows Execution Gateway`。
- Linux 控制面只输出：
  - `approved execution_intents`
  - 审批状态
  - 熔断状态
  - 审计链

### 4.5 自我进化边界

- 自我进化只允许进入：
  - prompt
  - routing
  - 离线回测
  - attribution / metrics / review 研究闭环
- 自我进化不允许直接进入：
  - live 执行权限
  - QMT 下单链
  - 风控阈值自动放宽
  - 自动部署到生产
- 任何“进化结果”都必须经过：
  - offline validation
  - paper / supervised replay
  - human review
  - 再进入生产

## 五、V2 当前可供 OpenClaw 使用的核心接口

### 5.1 Runtime

- `GET /runtime/health`
- `POST /runtime/jobs/pipeline`
- `POST /runtime/jobs/intraday`
- `POST /runtime/jobs/autotrade`
- `GET /runtime/reports/latest`

### 5.2 Research

- `GET /research/health`
- `POST /research/sync`
- `POST /research/events/news`
- `POST /research/events/announcements`
- `GET /research/summary`

### 5.3 Strategy

- `GET /strategy/strategies`
- `POST /strategy/screen`

### 5.4 Market

- `GET /market/health`
- `GET /market/universe`
- `GET /market/snapshots`
- `GET /market/bars`
- `GET /market/sectors`

### 5.5 Execution

- `GET /execution/health`
- `GET /execution/balance/{account_id}`
- `GET /execution/positions/{account_id}`
- `GET /execution/orders/{account_id}`
- `GET /execution/trades/{account_id}`
- `POST /execution/orders`

### 5.6 System / Audit / Governance

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
- `POST /system/discussions/opinions/batch`
- `POST /system/cases/{case_id}/rebuild`
- `POST /system/cases/rebuild`
- `GET /system/discussions/summary`
- `GET /system/discussions/reason-board`
- `GET /system/discussions/agent-packets`
- `GET /system/discussions/reply-pack`
- `GET /system/discussions/final-brief`
- `GET /system/discussions/client-brief`
- `GET /system/discussions/meeting-context`
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

## 六、v0.9 讨论流程骨架

### 6.1 最小状态链

```text
runtime pipeline
  -> candidate_case sync
  -> discussion cycle bootstrap
  -> round_1_running
  -> round_1_summarized
  -> round_2_running
  -> final_review_ready
  -> final_selection_ready / final_selection_blocked
```

### 6.2 协作要求

- `ashare-runtime` 先触发 pipeline，并返回 `trade_date` 与候选摘要。
- `ashare` 用 `trade_date` 初始化 cycle，不直接假设当日状态已存在。
- `ashare-research`、`ashare-strategy`、`ashare-risk`、`ashare-audit` 两轮都返回 opinion 数组。
- Round 2 只针对 `round_2_target_case_ids`，不再对全部 focus_pool 重跑。
- `ashare` 统一调用 `POST /system/discussions/opinions/batch` 归档，不允许子代理直接改写 case。
- Round 2 opinion 不能只重复结论；至少要包含“被谁质疑 / 回应了什么 / 是否改判 / 剩余争议”中的一个结构化字段，否则后端不会把该票视为二轮完成。
- `GET /system/discussions/summary`、`GET /system/discussions/agent-packets` 与 `GET /system/discussions/meeting-context` 会显式提供 `controversy_summary_lines`、`round_2_guidance`、`round_coverage`、`substantive_gap_case_ids`，供 `ashare` 在二轮前转给子角色。
- 终审必须经过 `POST /system/discussions/cycles/{trade_date}/finalize`。
- 若讨论尚未达到 `final_review_ready`，`finalize` 会返回 `finalize_skipped=true` 且原因为 `discussion_not_ready`，表示应先补齐二轮实质回应，而不是执行层故障。
- 只有 `final_selection_ready` 且存在 `execution_pool_case_ids` 时，才允许委派 `ashare-executor`。
- `finalize` 响应应直接携带 `client_brief`，供 `ashare` 和 `main` 复用，减少重复查询。
- 执行预演或执行提交统一走 `POST /system/discussions/execution-intents/dispatch`，不再由子代理自行拼接“执行结果”文本。
- 若需要回看最近一次执行预演或提交结果，统一读 `GET /system/discussions/execution-dispatch/latest`。

## 七、运行原则

### 6.1 运行与研究

- `ashare-runtime` 负责“跑”和“读”，不负责放行和下单。
- `ashare-research` 负责把新闻、公告、事件整理成可审计输入。
- `ashare-strategy` 负责解释和排序，不直接下单。

### 6.2 风控与执行

- `ashare-risk` 是执行前闸门，没有 allow 不进入执行。
- `ashare-executor` 只接受字段完整、已放行的执行意图。
- `ashare-executor` 的正式职责是消费、编排和回看执行意图，不直接等价于“调用 QMT 下单”。
- 面向未来生产部署时，`ashare-executor` 的落地动作应通过 `Windows Execution Gateway` 转译到 QMT。
- 默认执行模式仍以 paper / supervised 为主，不默认推动 live。

### 6.3 审计与回放

- 运行结果、研究摘要、会议纪要、系统审计都应能被 `ashare-audit` 读取和归纳。
- 用户面向的总结由 `main` 输出，但底层证据链由 `ashare-system-v2` 和 `ashare-audit` 维护。

## 八、当前重点风险

### 7.1 非代码类风险

- 若当前配置、workspace 指令和仓库文档发生漂移，模型可能被旧工作流重新拉偏。
- 仓库内旧 8-agent 资料若不清理，会持续制造维护歧义。
- WSL 与 Windows 的 8100 可达性必须以实际探测为准，不能只凭旧会话判断。
- 常规验证应保留历史会话，靠新增 run 的时间戳或唯一标记确认链路，不把 reset 当成默认操作。

### 7.2 本轮不处理的能力缺口

- 150+ 因子尚未补齐。
- AI 模型训练和上线未完成。
- live 自动执行的治理接口仍需进一步细化。
- Linux 控制面与 Windows VM 执行面的跨机 intent / receipt 协议尚未正式固化。
- 自我进化 proposal 到上线审批的自动化闭环尚未落地。

## 九、推荐启动与验证

### 8.1 Windows 侧

```powershell
cd D:\Coding\lhjy\ashare-system-v2
.\scripts\start_unattended.ps1
```

### 8.2 WSL 侧

```bash
cd /mnt/d/Coding/lhjy/ashare-system-v2
./scripts/start_openclaw_gateway.sh
```

### 8.3 验证

```bash
./scripts/ashare_api.sh probe
curl http://127.0.0.1:18789/health
./scripts/ashare_api.sh GET /health
./scripts/ashare_api.sh GET /runtime/health
./scripts/ashare_api.sh GET /system/overview
./scripts/smoke_discussion_flow.sh 5
./scripts/smoke_v09_cycle.sh 5
./scripts/smoke_quant_joint.sh 10 8890130545
./scripts/verify_openclaw_chain.sh runtime
./scripts/verify_openclaw_chain.sh research
```

### 8.4 quant 联调补充

- `smoke_quant_joint.sh` 只做只读和预演，不提交真实订单。
- 该脚本覆盖：
  - `quant` profile 状态
  - `runtime pipeline`
  - `execution-precheck`
  - `execution-intents`
  - `dispatch apply=false`
  - `execution-dispatch/latest`
  - `client-brief / final-brief`
- 若 `execution-intents` 为空，优先检查当日是否无 `selected` 或被资金/仓位/时段阻断，不要先判定为网关故障。
