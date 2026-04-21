# ashare-system-v2

面向 A 股的 Agent 交易控制面。

这个项目的目标，不是再做一个固定流程选股器，而是做一套 `Agent 主导决策 + runtime 工具库 + 监督治理 + 受控执行桥` 的正式交易控制面。  
程序负责市场数据、因子/战法、风控围栏、执行派发、审计与留痕；Agent 负责理解市场、提出假设、组织工具、形成提案、推动协作并决定是否进入执行。

本 README 不按“代码看起来支持什么”来写，而按 **真实实测结果** 来写，直接回答下面四个问题：

1. 项目设计上想成为什么。
2. 现在真实打通了什么。
3. 哪些只是骨架，哪些已经有实盘证据。
4. 当前距离“可正式上线、可稳定自治”还差什么。

## 1. 设计目标

以 [Agent 自主编排与 101 因子库实施细则](/srv/projects/ashare-system-v2/docs/agent_autonomy_factor_library_plan_20260417.md) 为准，当前主目标是：

- Agent 成为交易团队的大脑，能根据市场环境主动形成 `market_hypothesis`。
- Agent 不依赖固定模板，而是自己组织 `playbooks / factors / weights / constraints` 后调用 `runtime`。
- `runtime` 退回到工具层，负责执行 compose、给出候选、回显约束与证据。
- `system` 负责讨论、监督、执行预检、派发、回执、问答、运维和治理。
- 监督系统盯的是“市场响应质量”和“正式产出质量”，不是机械地统计是否调用过工具。
- 执行链必须可追溯，且 Linux 控制面、Windows Gateway、QMT、回执、对账之间要有真实链路证据。

换句话说，项目验收标准不是“接口齐了”，而是下面这条链是否真的成立：

```text
市场变化
-> Agent 形成市场假设
-> Agent 自组装 compose
-> runtime 返回候选与证据
-> discussion / supervision 收敛提案
-> execution precheck / intent / dispatch
-> Windows Gateway / QMT
-> receipt / reconciliation / learning / governance
```

## 2. 本次实测范围

本次 README 改写前，先做了真实链路检查。

- 当前检查时间：`2026-04-19 12:48` 左右
- 当前日期：`2026-04-19`
- 当前不是交易时段，因此：
  - `2026-04-19` 主要验证“控制面是否在线、QMT 读链路是否可用、runtime/监督是否仍在产出”
  - `2026-04-17` 作为最近一个有完整执行讨论痕迹的交易日，用来验证“执行池、派发、回执、准入、自治主线”是否真的发生过

本次只做 **只读验证与控制面验证**，没有主动下新的真实单。

## 3. 真实结论

一句话先说结论：

```text
真实控制面已在线，QMT 真实读链路已打通，Linux -> Windows Gateway -> QMT 的执行桥已存在真实派发痕迹；
但 Agent 自治闭环仍未完全收口，正式执行闭环也还不能宣称“已稳定打通”。
```

### 3.1 当前在线状态：真实在线，不是 mock

`2026-04-19` 实测：

- `bash scripts/health_check.sh`
  - `ashare-stack.target`、`ashare-system-v2.service`、`ashare-scheduler.service`
  - `ashare-go-data-platform.service`
  - `ashare-feishu-longconn.service`
  - `hermes-gateway-ashare-backup.service`
  - `openclaw-gateway.service`
  - 以上全部 `active`
- 端口监听正常：
  - `8100`
  - `18793`
  - `18890`
- HTTP 健康检查正常：
  - `/health` -> `status=ok`, `mode=live`
  - `/runtime/health` -> `run_mode=live`, `market_mode=go_platform`, `execution_mode=go_platform`
  - `/execution/health` -> `mode=go_platform`
  - `/market/health` -> `mode=go_platform`
  - `http://127.0.0.1:18793/health` -> `windows_proxy=enabled`, `python_control_plane=enabled`

这证明：

- Linux 控制面当前在线。
- Go Data Platform 当前在线。
- Windows 代理执行平面当前在线。
- 当前系统不是 mock 模式，而是 `live + go_platform` 运行。

### 3.2 QMT 真实读链路：已打通

`2026-04-19` 直接读取 Go 平台上的 QMT 账户接口：

- `GET /qmt/account/asset`
  - 账户：`8890130545`
  - 总资产：`101334.14`
  - 可用现金：`83784.14`
  - 持仓市值：`17550.0`
- `GET /qmt/account/positions`
  - 当前持仓：`603069.SH`
  - 持仓数量：`1000`
  - 可用数量：`1000`
  - 成本价：`17.24441`
  - 市价口径：`17.55`
- `GET /qmt/account/orders`
  - 当前返回空列表
- `GET /qmt/account/trades`
  - 当前返回空列表

同时返回的运行时诊断明确显示：

- Windows 侧 Python 正在运行
- QMT 安装路径存在
- `userdata_mini` 已被连接
- `connect_result=0`
- `subscribe_result=0`

这证明：

- QMT 不是只在代码配置里“假装存在”，而是当前确实能读到账户和持仓。
- 至少 **行情/账户/持仓只读链路** 已被真实验证。

### 3.3 交易控制面：执行桥存在真实痕迹，但闭环未完全成功

针对 `2026-04-17` 这个最近有执行痕迹的交易日，实测结果如下。

#### 3.3.1 派发与回执

`GET /system/discussions/execution-dispatch/latest?trade_date=2026-04-17`

- `status=queued_for_gateway`
- `queued_count=1`
- `submitted_count=0`
- `preview_count=0`
- `blocked_count=2`

其中 1 条真实派发意图是：

- 标的：`000002.SZ`
- 名称：`万 科Ａ`
- 方向：`BUY`
- 数量：`1500`
- 价格：`3.96`
- 估算金额：`5940.0`
- `execution_plane=windows_gateway`
- `gateway_pull_path=/system/execution/gateway/intents/pending`

同时，这个接口还给出了最新回执摘要：

- `latest receipt status=failed`
- `gateway_source_id=windows-vm-a`
- `bridge_path=linux_openclaw -> windows_gateway -> qmt_vm`
- 回执上报时间：`2026-04-18T06:35:31+08:00`

这说明：

- Linux 控制面确实已经把执行 intent 正式交给 Windows Gateway。
- 执行桥不是概念设计，而是有真实 intent 和 receipt。
- 但这次正式 intent **没有形成成功成交**，最新留痕是 `failed`。

#### 3.3.2 执行预检与候选阻断

`GET /system/discussions/execution-precheck?trade_date=2026-04-17&account_id=8890130545`

- `approved_count=0`
- `blocked_count=0`
- `status=blocked`
- `degrade_reason=no_approved_candidates`
- 账户资产口径：
  - `total=101334.14`
  - `cash=83784.14`
  - `equity=17550.0`

`GET /system/discussions/execution-intents?trade_date=2026-04-17`

- `intent_count=0`
- `blocked_count=0`
- 当前这一时点没有新的可执行 intent

但 `execution-dispatch/latest` 仍保留了该交易日较早时段的真实派发记录，说明：

- 控制面做过真实收敛与派发尝试
- 但不是整个交易日都持续有可批准候选

#### 3.3.3 受控 apply 准入

`GET /system/deployment/controlled-apply-readiness?...trade_date=2026-04-17...`

返回 `status=blocked`，主要阻断项有：

- `execution_precheck`
- `execution_intents`
- `equity_position_limit`
  - 当前口径：`0.3`
  - 受控 apply 阈值：`allowed<=0.2`
- `reverse_repo_reserved_amount`
  - 当前：`0.0`
  - 要求：`>=70000.0`

这说明：

- 当前系统并不是“只要在线就自动能下单”。
- 正式 apply 仍受账户结构、预检结果、资金保留规则等硬围栏控制。

#### 3.3.4 准入脚本总判断

`bash scripts/check_go_live_gate.sh "2026-04-17"`

返回：

- 总体：`BLOCKED`
- `准入2_windows_bridge: OK`
- `准入3_apply_closed_loop: OK`
- `准入1_linux_services: NO`
- `准入4_agent_chain: NO`

脚本摘要同时指出：

- `runtime=yes`
- `discussion=no`
- `monitor=yes`
- `dossier=yes`
- 当前主控执行队列里 `queued_for_gateway=0`
- 历史派发结果里 `queued=1 submitted=0 preview=0 blocked=2`

这个结果很关键。它说明当前问题不是“系统服务挂了”，而是：

- 服务面和执行桥面基本在线
- 但讨论态/主线收口/Agent 链条还没有达到脚本要求的 go-live 准入标准

### 3.4 Agent 自治：骨架已成，质量还不够

#### 3.4.1 已证明的部分

`GET /runtime/evaluations?limit=3` 可以看到真实 compose 留痕：

- 最近一次记录时间：`2026-04-19T11:55:44.945147`
- `request_id=formal-prod-check-latency-20260419`
- `mode=news_catalyst_scan`
- `market_hypothesis=聚焦 AI 智能体与机器人方向，避免银行与高股息防御`
- 使用的 playbook：
  - `trend_acceleration:v1`
  - `sector_resonance:v1`
- 使用的 factor：
  - `momentum_slope:v1`
  - `sector_heat_score:v1`
- 约束已被显式回写：
  - 排除 `银行`、`高股息`
  - `max_single_amount=20000.0`
  - `equity_position_limit=0.3`

这证明：

- Agent 已经不是只能走固定 `pipeline`。
- `compose-from-brief` 的真实链路是存在的。
- Agent 可以把“市场假设 + 因子/战法/约束”结构化写进 runtime。

#### 3.4.2 还没证明的部分

同一组 `runtime/evaluations` 也表明：

- `candidate_count=0`
- `selected_symbols=[]`
- `watchlist_symbols=[]`
- `adoption.status=pending`
- `outcome.status=pending`

`GET /runtime/strategy-repository` 也显示：

- 当前仓库共有 `84` 个 active 资产
  - `factor=64`
  - `playbook=20`
- 但治理建议大量是 `observe_only`
- 原因基本一致：`暂无真实 adoption/outcome 结果`

这说明：

- 因子库/战法库的“菜单”已经有了。
- 但“这些资产已经被真实采用、真实结算、真实赛马、真实淘汰”的证据还不够。

#### 3.4.3 监督系统能发现怠工，但还没有完全收口

`GET /system/agents/supervision-board?trade_date=2026-04-17&overdue_after_seconds=180`

可以看到：

- `cycle_state=round_1_running`
- `ashare` 总协调席位：`overdue`
- `ashare-strategy`：`overdue`
- `ashare-executor`：`needs_work`
- `ashare-research`：`working`
- `ashare-risk`：`working`
- `ashare-audit`：`working`

监督板已经能指出真实缺口：

- 总协调没有把执行反馈接回主线
- strategy 没有把打法切换结论补齐
- executor 缺少新的正式执行反馈

这证明：

- 监督系统已经具备“识别谁没完成正式产物”的能力
- 但它还没有把这些缺口稳定推进到一个已经收口的自治闭环

## 4. 按设计目标逐项判断

### 4.1 已达成

- **Linux 控制面在线**
  - 服务、端口、健康检查全部通过
- **Go 数据平台在线**
  - `18793/health` 返回正常
- **QMT 真实只读链路已打通**
  - 能读到账户、持仓、资产
- **Agent -> runtime compose 链路已存在真实留痕**
  - 能回显市场假设、因子、战法、约束、使用资产
- **监督系统已能识别席位怠工与缺口**
  - 能标记 `overdue / needs_work`
- **程序逻辑层的自治闭环已补齐**
  - 已能显式输出 `market_hypothesis`
  - 已能在失败后生成第二轮 `compose`
  - 已能把 `agent_proposed` 新票接进讨论与监督主线
  - 已能结构化判断当前主线处于 `找机会 / 持仓管理 / 做T / 换仓 / 盘后学习`
  - 已能在监督板与飞书回答里统一展示自治完成度

### 4.2 部分达成

- **Linux -> Windows Gateway -> QMT 的执行桥**
  - 已有真实 intent、gateway source、receipt
  - 但当前证据是 `queued / failed`，还不能算“稳定成功闭环”
- **Agent 自主理解市场并组织工具**
  - 已能提交 compose brief 并携带市场假设与约束
  - 从程序逻辑上，假设修正、失败后重编排、主线动作回写都已接通
  - 但还没有足够真实 adoption/outcome 证明其自治质量稳定有效
- **监督驱动主线收口**
  - 已能识别卡点
  - 且现在已能结构化给出 `mainline_stage / autonomy_summary / mainline_action_ready`
  - 但没有证据表明它已稳定完成“发现卡点 -> 催办 -> 补产物 -> round 收敛”
- **策略仓治理**
  - 仓库、版本、治理建议、面板都已存在
  - 学习回灌、active learned asset 入主链、历史负效衰减在代码层已可跑通
  - 但真实赛马、真实淘汰、真实学习回灌证据仍不足

### 4.3 本轮周日逻辑补齐

这轮没有新增真实下单，而是按周日可做的范围，把之前 README 里标成“未完全达标”的程序逻辑层补齐了：

- **市场假设自修正**
  - compose 失败后，`autonomy_trace` 现在会显式记录：
    - 原假设
    - 修正后假设
    - 是否发生 `hypothesis_revised`
    - 下一步主线动作是什么
- **失败后二次编排**
  - `auto_replan` 不再只是给个下一轮 brief，而是连同 `mainline_action_type / mainline_action_summary` 一起写回
- **统一主线阶段**
  - `/system/workflow/mainline` 新增 `current_stage`
  - `supervision-board` 和 `feishu/ask` 统一回显当前阶段与下一阶段
- **自治完成度指标**
  - 监督板现在可以直接看到：
    - `market_hypothesis_formed`
    - `compose_formed`
    - `retry_generated`
    - `hypothesis_revised`
    - `new_opportunity_ticket_generated`
    - `mainline_action_ready`
    - `learning_feedback_applied_count`
- **飞书口径对齐**
  - `status` 和 `supervision` 问答现在都能直接回答“当前主线是什么”“自治进度做到哪一步”

### 4.4 仍不能夸大的部分

- **不能宣称 Agent 已能稳定自主交易**
  - 当前没有足够真实成交与归因结果支撑这个结论
- **不能宣称受控 apply 已通过正式上线门槛**
  - `controlled_apply_readiness` 当前仍是 `blocked`
- **不能宣称 runtime 仓库已经被真实效果验证**
  - `adoption/outcome` 大量仍是 `pending`
- **不能宣称自治闭环已经完整**
  - 监督板和 go-live gate 仍明确显示主线缺口

## 5. 当前最接近真实情况的项目判断

截至 `2026-04-19`，这个项目最准确的表述是：

```text
它已经不是一个纸面方案，也不是纯 mock 演示。
它已经具备真实在线的 Linux 控制面、Go 平台、Windows Gateway 和 QMT 只读链路，
也已经出现过真实执行 intent、真实 gateway receipt、真实账户和持仓数据。

但它还不是一个可以直接宣称“Agent 已稳定理解市场并自主完成实盘闭环”的成品。
当前更准确的状态是：
控制面已上线，自治编排骨架已成，真实执行桥已接通，
周日可补的程序逻辑层已经补齐，
自治质量、正式执行成功率、学习回灌与治理闭环仍需继续补证据。
```

## 6. 当前最需要补强的点

结合本次实测，当前最需要补强的是下面这些，而不是继续堆新接口：

1. **把执行回执真正收回主线**
   - 当前已经有 `queued_for_gateway` 与 `failed receipt`
   - 但总协调、strategy、executor 没有把这条真实执行反馈完整收口成次日动作与学习结论

2. **补真实成功执行证据**
   - 现在已经证明“能入队、能回执、能对账”
   - 但还缺少“真实下单成功 -> 持仓变化 -> 回执一致 -> 复盘归因”的完整成功样本

3. **让 supervision 的催办真正变成闭环推进**
   - 目前能发现 `overdue`
   - 还要证明它能持续把缺口补成正式产物，而不是长期停留在 `round_1_running`

4. **让学习结果真正回灌下一轮 compose**
   - 现在策略仓是“有菜单、少结算”
   - 还不是“有真实效果赛马后自动影响下一轮编排”

5. **把“市场理解”从 prompt 口径变成可验证结果**
   - 当前能看到 `market_hypothesis`
   - 但还要继续证明：
     - 假设失败后会不会重编排
     - 市场变化后会不会自动换打法
     - 执行失败后会不会形成新主线动作

## 7. 仓库结构

```text
ashare-system-v2/
├── src/ashare_system/
│   ├── apps/                   # FastAPI 路由，含 runtime/system/market/execution
│   ├── discussion/             # case / cycle / brief / finalize / dispatch
│   ├── infra/                  # Go client、执行桥、审计、健康检查
│   ├── learning/               # 评估、归因、学习产物治理
│   ├── monitor/                # 心跳、事件、监督、看板
│   ├── notify/                 # 飞书消息、长连接、问答
│   ├── risk/                   # execution precheck 与电子围栏
│   ├── strategy/               # factor / playbook / composer / repository
│   ├── scheduler.py            # 调度主链
│   └── run.py                  # CLI 入口
├── scripts/                    # 启停、健康检查、API、准入检查脚本
├── docs/                       # 设计、技术手册、实验档案
├── openclaw/                   # Agent 运行侧配置与提示
├── web/                        # 控制台前端
├── tests/                      # unittest
├── task.md                     # 当前未完成项与整改记录
└── README.md
```

## 8. 最小复现实验

如果你要自己复查当前状态，先跑这几组命令。

### 8.1 控制面健康检查

```bash
cd /srv/projects/ashare-system-v2
bash scripts/health_check.sh
```

重点看：

- 是否仍是 `mode=live`
- `market_mode` / `execution_mode` 是否仍为 `go_platform`
- `18793/health` 是否仍返回 `windows_proxy=enabled`

### 8.2 QMT 真实读链路

```bash
curl -sS --max-time 30 "http://127.0.0.1:18793/qmt/account/asset"
curl -sS --max-time 30 "http://127.0.0.1:18793/qmt/account/positions"
curl -sS --max-time 30 "http://127.0.0.1:18793/qmt/account/orders"
curl -sS --max-time 30 "http://127.0.0.1:18793/qmt/account/trades"
```

### 8.3 最近交易日执行态

```bash
bash scripts/ashare_api.sh get "/system/discussions/execution-dispatch/latest?trade_date=2026-04-17"
bash scripts/ashare_api.sh get "/system/discussions/execution-precheck?trade_date=2026-04-17&account_id=8890130545"
bash scripts/ashare_api.sh get "/system/deployment/controlled-apply-readiness?trade_date=2026-04-17&account_id=8890130545&require_live=true&require_trading_session=false&include_details=false"
bash scripts/check_go_live_gate.sh "2026-04-17"
```

### 8.4 自治态

```bash
bash scripts/ashare_api.sh get "/system/agents/supervision-board?trade_date=2026-04-17&overdue_after_seconds=180"
bash scripts/ashare_api.sh get "/runtime/evaluations?limit=3"
bash scripts/ashare_api.sh get "/runtime/strategy-repository"
```

## 9. 推荐阅读顺序

先看设计目标：

- [Agent 自主编排与 101 因子库实施细则](/srv/projects/ashare-system-v2/docs/agent_autonomy_factor_library_plan_20260417.md)

再看代码与模块：

- [技术手册](/srv/projects/ashare-system-v2/docs/technical-manual.md)

再看当前缺口和整改记录：

- [任务进度追踪](/srv/projects/ashare-system-v2/task.md)

如果要看 Agent 到 runtime 的契约：

- [运行时 Agent 协议草案](/srv/projects/ashare-system-v2/docs/runtime_agent_protocol_draft_20260415.md)

## 10. 最终判断

如果你的问题是：

- “这是不是一个只会写文档和 mock 的项目？”
  - 不是。它已经有真实在线控制面、真实 QMT 读链路、真实执行 intent 和真实 gateway receipt。

- “是不是已经达到设计要求？”
  - 还没有完全达到。设计目标要求的是 Agent 主导的完整自治交易闭环，而当前真实证据只证明了控制面上线、执行桥存在、自治骨架成型，还没有证明稳定自治成功。

- “现阶段最准确的状态是什么？”
  - `真实上线中的控制面 + 已接通的执行桥 + 尚未完全收口的自治闭环`。

- “接下来应该盯什么？”
  - 盯真实成功执行样本、执行回执回主线、监督催办收口、学习回灌下一轮 compose，而不是继续把 README 写得更乐观。
我把客户端压测结果和服务端 request_id 日志对上了，发现还有一个关键事实：2 workers / 20 里几个 90ms+ 的样本，
  服务端真实处理只用了 2ms 左右，说明那部分长尾已经不在 Windows 网关服务本身，而是在当前压测客户端对外网卡地址
  的本机调度噪声。回复稿里我会把“客户端视角”和“服务端视角”同时写清楚，避免误判。
