# ashare-system-v2 系统逻辑说明书

> 版本日期：2026-04-19  
> 文档定位：解释当前仓库里已经实现的真实程序逻辑，不替代 README 的实测结论，也不把早期设想当成现状。  
> 阅读目标：回答这个项目现在到底是怎么运转的，Agent 在哪里负责理解和组织，程序在什么地方负责约束、执行、治理、催办和对外输出。

---

## 1. 项目到底在做什么

`ashare-system-v2` 的目标不是做一个固定规则的选股器，而是做一套 A 股 Agent 交易控制面。

这个控制面的设计边界很明确：

- Agent 负责理解市场、提出假设、组织战法与因子、发起讨论、决定是否进入执行。
- 程序负责提供数据、编排 runtime、生成候选、做风控围栏、控制执行、记录回执、形成学习账本，并通过飞书和监督系统治理 Agent。

所以它不是“程序直接替 Agent 下结论”，也不是“Agent 绕过程序自己裸奔下单”，而是：

```text
市场事实 -> Agent 形成假设 -> Agent 组织 compose -> runtime 产出候选与证据
-> discussion 收敛 -> execution precheck/intents/dispatch
-> Windows Gateway / QMT -> receipt / reconciliation
-> learning / governance / 催办 / 次日预案
```

当前主入口主要分成两面：

- `src/ashare_system/apps/runtime_api.py`
  负责策略工具面、compose、因子/战法仓库、学习资产和账本。
- `src/ashare_system/apps/system_api.py`
  负责讨论主线、执行主线、监督治理、飞书问答、主线流程展示与自动催办。

---

## 2. 当前系统总拓扑

从部署视角看，系统分成四层：

### 2.1 控制面

- Linux 上的 `ashare-system-v2 FastAPI`
- `scheduler.py` 定时器
- 飞书长连与通知派发
- 监督、治理、审计、讨论状态机

### 2.2 数据与策略工具面

- 市场数据、事件、档案、runtime context、monitor context
- 因子注册表、战法注册表、策略原子仓库
- compose 编排与评估账本

### 2.3 执行桥

- Linux 控制面生成 execution intent
- Windows execution gateway 拉取 intent
- QMT 执行并回传 receipt / positions / asset / orders / trades

### 2.4 Agent 协作面

- 主协调 `ashare`
- 运行 `ashare-runtime`
- 研究 `ashare-research`
- 策略 `ashare-strategy`
- 风控 `ashare-risk`
- 审计 `ashare-audit`
- 执行 `ashare-executor`

程序里已经把这些角色的读写接口、工作包、监督规则和催办口径定义出来，但“真正的自然语言推理大脑”仍依赖外部 Agent 消费这些合同，而不是完全封装在仓库内部。

---

## 3. 核心状态载体

这个系统不是靠一两个接口临时拼起来的，而是靠一组状态载体把流程串起来。

### 3.1 市场与运行上下文

- `workspace_context`
- `market_context`
- `event_context`
- `runtime_context`
- `monitor_context`

这些上下文供 Agent 先看市场、再决定要不要调工具。

### 3.2 候选与讨论载体

在 `src/ashare_system/discussion/candidate_case.py` 中，候选票被结构化成 `CandidateCase`，核心字段包括：

- `runtime_snapshot`
- `opinions`
- `round_1_summary`
- `round_2_summary`
- `risk_gate`
- `audit_gate`
- `final_status`
- `bull_case / bear_case / uncertainty`
- `must_answer_questions`

这意味着“验票”不是一句口头判断，而是一张可追溯的 case。

### 3.3 讨论周期载体

在 `src/ashare_system/discussion/discussion_service.py` 中，`DiscussionCycle` 负责保存：

- base pool / focus pool / execution pool
- round 1 / round 2 / round 3+ 状态
- blockers
- summary snapshot
- finalize 时间点

因此讨论不是散乱聊天，而是一个状态机。

### 3.4 执行与学习载体

- `latest_execution_precheck`
- `latest_execution_dispatch`
- `latest_execution_reconciliation`
- `compose_evaluations`
- `learned_assets`
- `agent_score_states`

这几类状态把“候选是否能下”“下了以后怎样”“哪些战法该升级或降权”全部串起来。

---

## 4. Agent 如何感知市场

### 4.1 市场感知不是直接靠单一选股器

当前程序侧已经把市场感知拆成多源输入，而不是只看一个候选池：

- 行情与市场画像
- 板块热度
- 新闻/公告催化
- 持仓与账户状态
- monitor 的异动提醒
- execution precheck 的执行阻断结果

这些能力在 `runtime_api.py` 的 `/runtime/capabilities` 里被显式暴露为工具目录，分成：

- `market_data`
- `discussion_and_collaboration`
- `execution_and_account`
- `strategy_and_runtime`
- `learning_and_governance`

也就是说，Agent 先要理解“今天市场在发生什么”，再决定调什么工具，而不是反过来。

### 4.2 程序如何帮助 Agent 判断“当前该干什么”

在 `src/ashare_system/apps/system_api.py` 中，`_resolve_mainline_stage_payload(...)` 会根据以下信号动态判定主线阶段：

- 当前市场时间段 `resolve_market_phase(...)`
- 是否已有持仓
- 当前仓位占比是否接近上限
- execution dispatch 当前状态
- 是否已发生成交 / 对账
- 最近 compose 是否失败且已触发 `auto_replan`
- 最近是否已有学习反馈回灌

主线阶段不是写死的，而是动态落到下面几个标签之一：

- `opportunity_discovery`
- `position_management`
- `replacement_review`
- `day_trading`
- `postclose_learning`

这一步非常关键。它决定系统今天主线是“继续找机会”，还是“先换仓”，还是“优先做 T”，还是“进入盘后学习”。

### 4.3 Agent 自己理解市场到什么程度

截至 2026 年 4 月 19 日，程序已经把“让 Agent 理解市场所需的输入、阶段判断、工作包、催办合同、compose 轻量入口”都提供出来了，但仍要实事求是地区分三层：

- 已实现：程序能把市场事实、阶段、执行阻断、板块催化、候选状态组织成 Agent 可消费的结构化上下文。
- 已实现：程序能根据主线阶段自动给不同 Agent 派不同任务，要求输出市场假设、compose 草案、风险结论、审计纪要等。
- 尚不能仅靠仓库本身证明：外部 Agent 一定能持续、稳定、正确地“自主理解市场并长期做出高质量判断”。

换句话说，仓库已经把“自主理解市场”的基础设施打通了，但市场理解质量仍取决于外部 Agent 的实际执行质量。

---

## 5. Agent 如何组织因子、战法与 compose

### 5.1 不是固定 profile，而是自组装 compose

当前 runtime 主线的核心不是“跑某个固定策略模板”，而是 `compose`。

在 `src/ashare_system/apps/runtime_api.py` 中：

- `/runtime/jobs/compose`
- `/runtime/jobs/compose-from-brief`

是两种主要入口。

其中 `/runtime/jobs/compose-from-brief` 是给 Agent 的轻量入口。Agent 可以只先交：

- `intent_mode`
- `objective`
- `market_hypothesis`
- `focus_sectors / avoid_sectors`
- `playbooks / factors`
- `weights`
- `allowed_regimes / blocked_regimes`
- `holding_symbols`
- `blocked_symbols`
- 仓位与风控约束

程序再把 brief 组装成正式 `RuntimeComposeRequest`。

### 5.2 因子和战法如何被组织

在 `runtime_api.py` 里，compose 进入后会做几件事：

1. 校验 playbook/factor 是否在仓库中可用。
2. 把 compose 使用到的资产登记到 repository payload。
3. 把约束统一解析成 constraint pack。
4. 根据 universe 生成 pipeline 请求。
5. 先跑 pipeline，再对 top picks 做约束过滤。
6. 把结果交给 `StrategyComposer.build_output(...)`。

### 5.3 `StrategyComposer` 在做什么

`src/ashare_system/strategy/strategy_composer.py` 是真正把“Agent 组织的策略意图”转成“可讨论候选”的地方。

它会做四件关键事：

1. 提取市场驱动因素  
   包括热点板块、正面事件催化、monitor 异动加成。

2. 注入学习反馈  
   `learned_asset_plan` 会对 playbook、factor、symbol、sector 产生偏置，形成：
   - `playbook_weight_bias`
   - `factor_weight_bias`
   - `symbol_score_bias`
   - `sector_score_bias`

3. 对候选重新打分  
   候选不仅看基础 `selection_score`，还看：
   - 热点板块命中
   - 事件催化命中
   - monitor 异动
   - learned asset 的正负反馈

4. 生成解释包  
   返回：
   - `market_summary`
   - `candidates`
   - `filtered_out`
   - `explanations`
   - `proposal_packet`

所以“组织因子和战法”并不是静态注册，而是把 Agent 给出的结构化意图，转成可以验票、可以讨论、可以继续收敛的候选包。

---

## 6. 战法选股、验票、讨论、收敛的代码主线

### 6.1 第一步：runtime 产生候选

compose 或 pipeline 产生候选后，会同步到 `CandidateCaseService`。

在 `candidate_case.py` 里，`sync_from_runtime_report(...)` 会把每个候选票转成 case，并写入：

- 来源 `source / source_tags`
- 排名 `rank`
- 选股分 `selection_score`
- `playbook_context`
- `behavior_profile / sector_profile / market_profile`
- bull / bear / uncertainty
- 必须回答的问题 `must_answer_questions`

这一步就是“候选入池”。

### 6.2 第二步：讨论周期接管

在 `discussion_service.py` 里：

- `bootstrap_cycle(...)` 建立当日讨论周期
- `start_round(...)` 启动某一轮讨论
- `refresh_cycle(...)` 刷新摘要、自动判断轮次是否完成
- `finalize_cycle(...)` 在满足条件时收敛成最终结果

系统支持：

- round 1
- round 2
- round 3+
- 自动续轮

也就是说，只要争议、反证或质询没有闭环，系统可以不止两轮。

### 6.3 第三步：多 Agent 验票

当前程序定义的验票角色主要是：

- `ashare-research`
  看事件、催化、题材逻辑、信息新鲜度。
- `ashare-strategy`
  看市场假设、战法适配、factor/playbook 组合是否合理。
- `ashare-risk`
  看风险边界、仓位、执行阻断、是否该 limit/reject。
- `ashare-audit`
  看证据链是否闭环、观点是否自洽、是否存在“说得热闹但没法执行”。

这些意见最终会写回 case，形成：

- `stance`
- `confidence`
- `reasons`
- `key_evidence`
- `evidence_gaps`
- `questions_to_others`
- `remaining_disputes`

这就是程序层面的“验票”。

### 6.4 第四步：系统如何判断要不要第二轮 compose

在 `runtime_api.py` 的 `_build_autonomy_retry_plan(...)` 中，如果出现以下情况，系统会自动判断上一轮 compose 失效：

- `zero_candidates`
- `market_regime_mismatch`
- `hypothesis_regime_mismatch`
- 主要被某类硬过滤集中打掉

然后会自动生成下一轮建议：

- 切换目标 `market_regime`
- 换一组 playbooks
- 换一组 factors
- 视情况扩大 universe
- 重写 `market_hypothesis`

这一步的结果会写入：

- `autonomy_trace`
- `auto_replan`

并且当前版本已经补上了这些关键字段：

- `original_market_hypothesis`
- `revised_market_hypothesis`
- `hypothesis_revised`
- `mainline_action_ready`
- `mainline_action_type`
- `mainline_action_summary`
- `learning_feedback_applied_count`

所以现在的系统不是“compose 失败就结束”，而是能自动告诉 Agent 下一轮怎么改。

### 6.5 第五步：讨论如何收敛成 selected / watchlist / rejected

最终讨论收敛不是只看运行分数，而是 case 上的多维状态共同决定：

- `final_status`
- `risk_gate`
- `audit_gate`
- 是否进入 `execution_pool`
- 讨论态是否允许 finalize

因此一个 runtime 排名前三的票，也可能被 risk/audit 挡掉，或者退到 watchlist。

---

## 7. 从讨论结果到真实下单的流程

### 7.1 执行前先做预检

`src/ashare_system/apps/system_api.py` 暴露了三段式执行接口：

- `GET /system/discussions/execution-precheck`
- `GET /system/discussions/execution-intents`
- `POST /system/discussions/execution-intents/dispatch`

执行预检会综合看：

- 当前账户现金与仓位
- 单票仓位上限
- 总仓位上限
- 是否命中选股排除偏好
- 是否涨停/跌停
- 实时快照是否新鲜
- 价格偏离是否过大
- 风控闸门与审计闸门是否放行
- 当前是否在交易时段

所以候选票不是一选出来就能下单。

### 7.2 execution intent 是什么

`execution-intents` 的作用是把“讨论收敛后的候选结果”变成“可派发的交易意图”。

也就是说，系统要先完成：

- 候选收敛
- 风控预检
- 预算与仓位判断

之后才生成 intent。

### 7.3 dispatch 如何落到真实执行桥

`dispatch` 之后，程序会：

1. 持久化 execution dispatch 结果。
2. 发送执行摘要通知。
3. 在 live 模式下触发执行告警通知。
4. 把 intent 交给 Windows Gateway/QMT 链路。

这一步是“从决策到执行”的分界线。

### 7.4 回执、对账和结果回灌

执行后，系统还会继续走：

- `POST /system/execution-reconciliation/run`
- `GET /system/execution-reconciliation/latest`

这一步不是可有可无的装饰，而是后续学习账本的基础。没有回执和对账，所有“策略有效性”都只是嘴上说说。

---

## 8. 学习账本、learned asset 与自进化

### 8.1 compose 会被正式记账

在 `src/ashare_system/strategy/evaluation_ledger.py` 中，`record_compose_evaluation(...)` 会把每次 compose 记录为一条正式账本。

它会保存：

- request payload
- runtime job
- market summary
- candidates / filtered_out
- proposal packet
- applied constraints
- repository summary
- autonomy trace
- retry plan
- adoption 状态
- outcome 状态

所以 compose 不是“一次性运行结果”，而是长期可回看的实验记录。

### 8.2 adoption 与 outcome 是两段式反馈

账本里把反馈分成两段：

1. `reconcile_adoption(...)`
   看 compose 产出的候选，后来到底有没有被 discussion 采纳。

2. `reconcile_outcome(...)`
   看最终有没有形成真实执行、真实持仓变化、真实收益表现。

这两段的意义不同：

- adoption 说明“观点有没有被团队接受”
- outcome 说明“接受以后有没有形成真实交易结果”

### 8.3 learned asset 怎么进入主线

当前系统已经支持 learned asset 生命周期，包括：

- transition
- approvals
- panel
- advice ingest / resolve

而且在 `StrategyComposer` 中，active learned asset 已经能真正影响：

- factor 权重
- playbook 权重
- symbol / sector 偏置

这意味着盘后学习不是只做报告，而是能实际反哺第二天 compose。

### 8.4 夜间沙盘和注册表更新

调度器里已经挂上这些任务：

- 学分结算
- Prompt 进化
- 注册表权重覆写
- 策略自进化
- 增量学习回放
- 参数治理巡检
- 账本回测验证
- 夜间沙盘推演

所以这个项目的“学习”不是孤立的一份复盘文档，而是会反馈到：

- score
- prompt
- registry weight
- learned asset
- next day plan

---

## 9. 飞书机器人如何治理 Agent 流程

### 9.1 飞书不是只发消息，而是控制台入口

`system_api.py` 中已经提供：

- `GET /system/feishu/briefing`
- `POST /system/feishu/briefing/notify`
- `POST /system/feishu/ask`
- `POST /system/feishu/events`

飞书侧现在至少承担四类工作：

- 知情
- 问答
- 调参入口
- 监督催办触达

### 9.2 飞书简报现在会带主线与自治进度

`_build_feishu_briefing_payload(...)` 当前会把这些内容汇总成摘要：

- workspace context
- cadence 节奏
- client brief
- execution dispatch
- `mainline_stage`
- `autonomy_summary`
- agent scores

也就是说，飞书看到的不再只是“今天推荐了几只票”，而是能看到：

- 当前主线在找机会、换仓、做 T 还是盘后学习
- Agent 自治进度走到了哪一步

### 9.3 飞书问答不是静态 FAQ

`_build_feishu_question_reply(...)` 当前支持按 topic 组织回答，已经覆盖：

- `status`
- `discussion`
- `execution`
- `position`
- `replacement`
- `risk`
- `holding_review`
- `day_trading`
- `opportunity`

并且 `status` / `supervision` 的回答已经对齐：

- 当前主线阶段
- 当前自治进度
- 当前执行概况

因此飞书现在已经是对外治理面，而不是单纯通知机器人。

---

## 10. 定时安排与自动流程安排

### 10.1 调度器按盘前、盘中、盘后三段组织

`src/ashare_system/scheduler.py` 把任务分成：

- `PRE_MARKET_TASKS`
- `INTRADAY_TASKS`
- `POST_MARKET_TASKS`

这三段不是装饰性的名字，而是明确挂了真实任务。

### 10.2 盘前任务

包括：

- 新闻扫描
- 竞价预分析
- 环境评分
- 买入清单
- 竞价快照
- 逆回购开盘回补

### 10.3 盘中任务

包括：

- 开盘执行
- 盯盘巡检
- 微观巡检
- Agent 监督巡检
- Agent 自主起手
- 午间快照
- 午后刷新
- 尾盘决策

这里最关键的是两条：

- `supervision.agent:check`
- `autonomy.agent:runtime_bootstrap`

前者是监督，后者是自动拉起 Agent 工作。

### 10.4 盘后任务

包括：

- 日终数据拉取
- 因子计算
- 日终复盘
- 学分结算
- Prompt 进化
- 注册表权重覆写
- 参数治理巡检
- 账本回测验证
- 夜间沙盘推演

这意味着项目已经把“第二天怎么变得更聪明”挂进调度器了。

---

## 11. 系统如何验收 Agent 作业

### 11.1 监督面板不是看“有没有调用接口”

`_build_agent_supervision_payload(...)` 和 `build_agent_task_plan(...)` 共同定义了当前监督逻辑。

监督关注的是：

- 当前主线阶段
- 当前自治进度
- 是否出现新事实、新提案、新参数变化
- 市场变了以后相关席位有没有响应
- 当前岗位有没有形成对应产物

也就是监督“产出质量与响应速度”，不是监督“今天调了几次接口”。

### 11.2 任务计划如何生成

`build_agent_task_plan(...)` 会结合：

- 当前阶段 `resolve_market_phase(...)`
- 当前持仓与预算
- 当前执行状态
- 最近市场是否发生变化
- 当前监督板 items 的活动痕迹
- 上一次催办是否完成

给每个 Agent 生成：

- `task_prompt`
- `task_reason`
- `expected_outputs`
- `dispatch_key`
- `dispatch_recommended`
- `market_response_state`

### 11.3 如何判断“作业做没做”

在 `supervision_tasks.py` 中，程序已经定义了 completion tag 机制。它不是随便看一句“我做了”，而是看活动是否真正覆盖到目标输出，例如：

- `research_output`
- `strategy_output`
- `nightly_sandbox`
- `next_day_plan`
- `audit_record`
- `risk_review`
- `execution_feedback`
- `runtime_refresh`
- `coordination`

只有活动痕迹和这些标签匹配，才会被视为真正完成。

### 11.4 如何催办

催办主要分三层：

1. 普通任务派发  
   生成 task prompt，提醒该岗位补齐产物。

2. 强提醒  
   当 attention item 反复未解除，进入升级态。

3. 自动续发  
   在同一阶段、同一 dispatch key 未完成时，按冷却时间再次派发。

系统里已经有这些配套函数：

- `record_agent_task_dispatch(...)`
- `record_agent_task_completion(...)`
- `sync_agent_task_completion_from_activity(...)`
- `record_supervision_notification(...)`

因此“催办”不是人工靠感觉催，而是系统记住：

- 上次什么时候派过
- 派给谁
- 期望什么产物
- 对方有没有形成对应活动

### 11.5 当前催办口径已经强调“市场响应迟滞”

这一版补齐后，监督不再只说“你还没发言”，而是已经能指出：

- 市场已经变化
- 但某岗位还没形成对应产物
- 需要直接进入第二轮 compose
- 需要给出假设修正说明

这说明治理已经从“形式催办”升级到“按主线卡点催办”。

---

## 12. Agent 自主起手是怎么跑起来的

### 12.1 自动工作包入口

系统专门提供了：

- `GET /system/agents/runtime-work-packets`
- `GET /system/agents/autonomy-spec`
- `GET /system/workflow/mainline`
- `GET /system/agents/supervision-board`

这些接口共同组成“Agent 工作合同”。

### 12.2 scheduler 如何触发 Agent 自主起手

在 `scheduler.py` 的 `task_agent_runtime_autonomy()` 中，系统会：

1. 读取 `runtime-work-packets`
2. 对 `ashare-strategy / ashare-research / ashare-risk / ashare-audit` 逐个检查
3. 如果该岗位当前确实 `needs_work` 或 `overdue`
4. 就尝试自动完成该岗位的写回动作

其中：

- `ashare-strategy`
  会优先尝试读取最近 compose 结果并写回；
  如果没有，就按 `compose_brief_hint` 给的提示直接运行一次 compose。
- `ashare-research / ashare-risk / ashare-audit`
  则通过各自的 writeback 接口回写结构化意见。

### 12.3 这是否等于“Agent 已完全自主”

还不能这样说。

当前更准确的表述是：

- 程序已经具备“自动把岗位拉起来干活”的合同、调度和写回机制。
- 程序已经能自动识别策略侧要不要再 compose 一轮。
- 但真正的市场理解质量、文本推理质量、观点优劣，仍依赖外部 Agent 的表现。

所以现在更像“受控自治”，而不是“完全脱离监督的自由自治”。

---

## 13. 对外接口如何展示

### 13.1 给 Agent 用的接口

主要看三组：

- `/runtime/capabilities`
- `/runtime/strategy-repository`
- `/system/agents/autonomy-spec`

这三组回答的是：

- 你可以用哪些工具
- 这些工具长什么样
- 你该在什么时候、以什么角色、按什么规则工作

### 13.2 给主控与运维看的接口

主要看：

- `/system/workflow/mainline`
- `/system/agents/supervision-board`
- `/system/robot/console-layout`
- `/system/feishu/briefing`

这几组回答的是：

- 当前主线是什么
- 哪些席位在工作、哪些在拖延
- 控制台应该展示哪些模块
- 飞书应该怎么汇总当前情况

其中当前几组接口已经补到可以直接做“主控看板”：

- `/system/workflow/mainline`
  当前会返回动态 `current_stage`，不是静态流程图。
- `/system/agents/supervision-board`
  当前会返回 `mainline_stage`、`autonomy_summary`、`autonomy_progress`、`autonomy_metrics`、`attention_items`、`notify_items`。
- `/system/feishu/ask`
  其中 `status` 和 `supervision` 类回答，已经会对齐主线阶段和自治进度。

### 13.3 给执行席位看的接口

主要看：

- `/system/discussions/execution-precheck`
- `/system/discussions/execution-intents`
- `/system/discussions/execution-dispatch/latest`
- `/system/execution-reconciliation/latest`

这几组回答的是：

- 能不能下
- 下什么
- 派发到了哪
- 有没有收到回执

### 13.4 给学习与治理看的接口

主要看：

- `/runtime/evaluations`
- `/runtime/evaluations/panel`
- `/runtime/learned-assets/panel`
- `/runtime/learned-assets/advice`
- `/system/agent-scores`
- `/system/audits`

这几组回答的是：

- compose 做得怎么样
- 哪些资产该升、该降、该审
- 哪些 Agent 长期拖后腿

---

## 14. 对外接口如何调整

如果后续要继续增强，调整优先级建议按下面顺序走。

### 14.1 第一优先级：让主线接口更像“操作台”

当前最核心的外部展示接口其实已经出现了：

- `/system/workflow/mainline`
- `/system/agents/supervision-board`
- `/system/feishu/briefing`

建议后续继续强化这三组的一致性，避免不同入口给出不同结论。

### 14.2 第二优先级：把 compose 输入改得更适合 Agent

虽然 `/runtime/jobs/compose-from-brief` 已经够用了，但后续还可以继续增强：

- 更明确的失败原因分类
- 更强的反证与放弃项结构
- 更清晰的持仓替换意图字段
- 更直接的“第二轮 compose”建议模板

### 14.3 第三优先级：把学习资产的生效证据直接暴露出去

当前 learned asset 已经能回灌排序，但外部接口还可以更直观地展示：

- 哪个 asset 影响了这次 compose
- 对候选排序具体抬升或压制了多少
- 是因为什么历史表现被激活或冻结

这会让“系统真的在学习”更容易被人验收。

---

## 15. 当前是否已经具备“自己理解市场、自己组织影子执行任务”的能力

这部分必须讲清楚，不能空喊“已经自治”。

### 15.1 已经到位的部分

- 已有多角色 Agent 分工。
- 已有市场阶段识别与主线阶段识别。
- 已有 runtime compose 轻量入口与正式入口。
- 已有失败后自动第二轮 compose 建议。
- 已有 candidate case、discussion cycle、execution precheck、dispatch、reconciliation。
- 已有 supervision board、runtime work packets、autonomy spec、feishu briefing。
- 已有学习账本、learned asset 回灌、夜间沙盘和参数治理任务。

程序意义上，这条链已经是通的。

### 15.2 还不能夸大为“完全自主”的部分

- 外部 Agent 是否真的持续消费这些合同、持续高质量输出，不由仓库代码单独保证。
- 市场理解仍主要通过“外部 Agent + 程序上下文合同”的方式完成，不是仓库内部藏着一个完整自主大脑。
- 实盘链路虽然已有真实执行桥与回执痕迹，但还不能据此宣称“自治交易闭环已稳定成熟”。

### 15.3 最准确的当前口径

截至 2026 年 4 月 19 日，最准确的结论是：

```text
这个项目已经具备“受控自治交易控制面”的程序能力。
它已经能让多个 Agent 基于市场阶段、compose、讨论、执行闸门和监督催办协同工作。
但它距离“无需人工担保、可长期稳定自治实盘”的最终目标，还有真实交易表现与长期治理效果要继续证明。
```

---

## 16. 当前已打通、已验证、仍需补证的边界

### 16.1 程序逻辑已打通

- 市场感知上下文
- 动态主线阶段
- compose 与 compose-from-brief
- auto replan
- candidate case 与 discussion cycle
- execution precheck / intents / dispatch
- supervision board / task dispatch / completion sync
- feishu briefing / ask
- evaluation ledger / learned assets / nightly sandbox

### 16.2 已有真实链路证据

结合当前 README 口径，已拿到真实证据的主要是：

- 控制面在线
- QMT 账户、持仓、资产只读链路可用
- Linux -> Windows Gateway -> QMT 的 intent / receipt 桥有真实痕迹

### 16.3 仍需继续补证

- Agent 自治链在连续交易日内是否稳定产出高质量提案
- 第二轮 compose 在真实行情中是否能持续改善结果
- learned asset 回灌是否显著改善候选质量
- 飞书催办与监督是否真的减少主线迟滞
- 实盘执行成功率、对账闭环、收益归因是否长期可靠

### 16.4 当前已有自动化测试覆盖的关键主线

结合 `tests/test_upgrade_workflow.py`，当前已经有自动化测试覆盖以下关键能力：

- `/system/workflow/mainline` 暴露动态 `current_stage`
- `/system/agents/supervision-board` 暴露主线阶段与自治进度
- `/system/feishu/ask` 的状态与监督回答包含主线与自治进度
- compose 失败后能生成第二轮 `auto_replan`
- `agent_proposed` 自提票能进入讨论主线
- supervision 能把策略席推入第二轮 compose
- active learned asset 能进入 runtime compose 主线

---

## 17. 结论

如果只问“代码逻辑有没有补通”，答案是：主线已经补通，而且不只是接口补齐，而是把下面这些链条真正接上了：

- 市场感知
- compose 编排
- 候选验票
- 多 Agent 讨论
- 执行预检与派发
- 回执对账
- 学习回灌
- 飞书治理
- 定时催办

如果继续追问“它是不是已经等于一个完全成熟、完全自主、稳定赚钱的交易 Agent 团队”，答案仍然是：还不能这么下结论。

当前系统更准确的定位是：

```text
它已经是一套结构完整、主线贯通、具备受控自治能力的 A 股 Agent 交易控制面；
下一阶段需要继续用真实交易日、真实执行结果和真实治理效果来证明它是否达到正式上线标准。
```
