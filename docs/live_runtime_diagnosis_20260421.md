# 2026-04-21 开盘运行态诊断与改进清单

## 1. 执行摘要

本次 2026-04-21 上午盘中，系统并非“完全没有运行”，而是大量运行在巡检、状态刷新、讨论推进、桥健康守护等链路上；真正决定是否买入、生成执行意图并派发到 Windows/QMT 的主链没有闭合。

今天上午的真实结果是：

- 真实成交/报单侧，只有一笔盘中卖出被成功提交。
- 买入侧，`execution intent` 已生成，但直到当前仍未派发。
- 讨论链持续推进到 `round_summarized`，但没有进入“终审 -> 自动派发”的闭环。
- 盘中候选被 `market regime=chaos` 的规则整体冻结，进一步压死了新买入机会。
- 控制面在交易时段存在误导与性能问题：部分面板接口直接超时，`mainline_stage` 在上午盘中错误显示为“盘后学习”。

结论：

1. 今天上午“不买”的直接原因，不是 QMT 不通，也不是仓位/风控拦死。
2. 真正卡点在 Linux 侧主链编排：`开盘执行任务为空壳`、`自动派发只挂在 finalize`、`盘中冻结规则过粗`、`决策席位产物不足`。
3. 当前系统更像“运行态监控器 + 讨论状态机 + 持仓巡视器”，还没有达到“盘中持续发现机会并自动落单”的设计要求。

---

## 2. 今日真实运行时序

以下时间线基于 `ashare-scheduler.service` 日志、系统接口和状态快照整理。

### 2.1 09:20-09:21：系统开始跑盘中任务，但以巡视与监督为主

日志显示，`09:20:00` 开始同时触发：

- `Agent自主起手`
- `微观巡检`
- `持仓快巡视`
- `持仓深巡视`

调度定义见：

- [scheduler.py](/srv/projects/ashare-system-v2/src/ashare_system/scheduler.py#L115)

这里说明系统进入了盘中工作态，但主力任务是：

- 高频持仓巡视
- 微观节奏巡检
- Agent 监督
- 桥健康守护

而不是持续的“执行窗口买入编排”。

### 2.2 09:21：讨论链启动 Round 1

日志：

- `09:21:04 discussion cycle updated -> focus_pool_building/round_1_running`

说明讨论链并未停摆，系统在推进候选讨论。

### 2.3 09:27：进入 Round 2 / Execution Pool 构建

日志：

- `09:27:03 discussion cycle updated -> execution_pool_building/round_2_running`

这表明系统已从 focus pool 推进到 execution pool，理论上已接近执行准备。

### 2.4 09:30：开盘执行任务触发，但它本身是空壳

关键日志：

- `09:30:00 Running job "开盘执行"`
- `09:30:02 [execution] 开盘执行任务进入纸面阻断`

对应代码：

- [scheduler.py](/srv/projects/ashare-system-v2/src/ashare_system/scheduler.py#L5680)

真实代码不是去读取 execution precheck / intents / dispatch，而只是写一条审计：

- `开盘执行任务进入纸面阻断`

这意味着：

- 调度层虽然配置了 `09:30` 的“开盘执行”
- 但执行函数没有真正发起买入主链
- 它不是失败了，而是根本没实现真实执行

这是今天上午买入链断掉的第一根主因。

### 2.5 09:39-09:51：讨论链继续推进，但没有进入终审自动派发

日志显示：

- `09:39` 多次进入 `execution_pool_building/round_running`
- `09:45` 仍在 `round_running`
- `09:48` 仍在 `round_running`
- `09:51:06` 进入 `execution_pool_building/round_summarized`

到 `09:51`，讨论链已经不是“没讨论完”，而是“讨论总结完了”，但仍没有看到：

- `讨论周期已终审`
- `讨论终审后已自动派发执行意图`
- `execution dispatch queued/submitted`

自动派发代码只挂在 finalize 路径上：

- [_maybe_auto_dispatch_execution_intents](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py#L6079)
- [discussion finalize 调用点](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py#L16271)

也就是说，当前架构下：

- `execution intents` 生成了，不等于会自动派发
- 只有走到 `finalize_cycle()`，才会触发自动派发
- 今天上午没有证据表明 finalize 被调起

这是第二根主因。

### 2.6 09:55：唯一一笔真实执行是盘中卖出

`/system/discussions/execution-dispatch/latest?trade_date=2026-04-21` 返回：

- 最新回执是 `intraday-sell-000010-SZ-89d0efab`
- `submitted_at=2026-04-21T09:55:34+08:00`

而同一个接口同时表明：

- `discussion dispatch queued=0`
- `pending_intent_count=0`
- `queued_for_gateway_count=0`

说明今天上午真正走通的是：

- 持仓快路径卖出链

没有走通的是：

- 讨论买入派发链

因此，“只有一个卖动作”这个现象是真实的，而且并不是 Windows 桥单点故障导致，而是业务链路本身只把卖出链走通了。

### 2.7 09:56 之后：盘中候选被 chaos 规则整体冻结

`/system/tail-market/latest?trade_date=2026-04-21` 返回的 `intraday_rank_result` 明确显示：

- `market_regime=chaos`
- `freeze_all=true`
- 所有候选动作都是 `FREEZE_ALL`
- 摘要：`市场进入 chaos，所有候选暂时冻结。`

对应代码：

- [intraday_ranker.py](/srv/projects/ashare-system-v2/src/ashare_system/monitor/intraday_ranker.py#L33)

当前实现是：

- 只要市场 `regime == chaos`
- 所有候选一律 `FREEZE_ALL`

这不是“谨慎降权”，而是“全盘冻结”。

这意味着：

- 即使执行池里已经有通过预检的票
- 盘中也可能因为该规则整体冻结
- 系统不会继续尝试做差异化买入

这是第三根主因。

### 2.8 10:12：当前已通过预检 2 只，但仍未派发

当前最新状态：

`/system/discussions/client-brief?trade_date=2026-04-21`

- `selected_count=2`
- 入选：
  - `000657.SZ 中钨高新`
  - `000400.SZ 许继电气`

`/system/discussions/execution-precheck?trade_date=2026-04-21`

- `approved_count=2`
- `blocked_count=0`
- `execution_pool_case_ids = [000657, 000400]`
- `session_open=true`

也就是说，到当前为止：

- 候选已经选出来了
- 风控/审计通过了
- 会话也是开盘状态
- 两只票都已满足预检

但：

`/system/discussions/execution-intents?trade_date=2026-04-21`

- `intent_count=2`

而：

`/system/discussions/execution-dispatch/latest?trade_date=2026-04-21`

- `status=not_found`
- `pending_intent_count=0`
- `discussion dispatch queued=0`

所以当前最准确的链路状态是：

`候选已入选 -> 预检通过 -> intent 已生成 -> 没有派发 -> 没有新买单`

这把卡点进一步收窄到了：

- 预检后
- 派发前

---

## 3. 当前系统实际在做什么

### 3.1 运行中的链路

今天上午运行最稳定的链路有：

1. 持仓快巡视
2. 持仓深巡视
3. 微观巡检
4. 桥健康守护
5. Agent 监督巡检
6. discussion cycle 状态推进
7. 订单/持仓/资产的高频轮询

这些链路从日志上看都是真实运行的，没有停。

### 3.2 没有真正闭合的链路

今天上午没有闭合的是：

1. 开盘执行链
2. discussion finalize 链
3. execution intent auto dispatch 链
4. 盘中新机会到买单的连续主链

### 3.3 各角色实时状态

监督面板在 `09:59` 的关键状态：

- `ashare-runtime = working`
- `ashare = working`
- `ashare-research = working`
- `ashare-strategy = overdue`
- `ashare-risk = needs_work`
- `ashare-audit = overdue`

监督面板直接指出的缺口是：

- 策略侧缺 `持仓动作判断 / 做T/换仓方案`
- 风控侧缺 `放行/阻断结论 / 成交后风险复核`
- 审计侧缺 `纪要复盘 / 证据复核`

这说明：

- 系统“有活动”
- 但活动不等于形成可执行主线产物

换句话说，今天上午不是 agent 没动，而是“动了很多，但很多动作没有产出能推动买入执行的正式结论”。

---

## 4. 根因分解

## 4.1 根因一：`开盘执行` 调度是空壳，不是真执行

证据：

- 调度注册存在 `09:30` 开盘执行任务。[scheduler.py](/srv/projects/ashare-system-v2/src/ashare_system/scheduler.py#L117)
- 实际任务函数只写审计，不做执行。[scheduler.py](/srv/projects/ashare-system-v2/src/ashare_system/scheduler.py#L5680)
- 日志写的是 `开盘执行任务进入纸面阻断`

影响：

- 系统名义上有 `execute_open`
- 但在最关键的 09:30 开盘时刻，没有真正去拉 precheck / intents / dispatch
- 设计上像“有开盘动作”，实现上只是“记录一条日志”

结论：

- 这是结构性缺陷，不是参数问题。

## 4.2 根因二：自动派发只挂在 finalize，上午没有走到这个触发点

证据：

- 自动派发在 `_maybe_auto_dispatch_execution_intents()` 中实现。[system_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py#L6079)
- 该函数只在 `finalize_cycle()` 路径中被调用。[system_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py#L16271)
- 今天上午没有看到 `讨论周期已终审` 的日志
- `execution intents=2`，但 `execution dispatch = not_found`

影响：

- 讨论做到 `round_summarized` 还不够
- 如果没人/没有程序推进到 finalize
- 就会出现“intent 已生成但永远不派发”的悬空状态

结论：

- 当前主链把“是否派发”绑得太晚，而且过度依赖终审。

## 4.3 根因三：盘中 `chaos` 规则过于粗暴，直接冻结所有候选

证据：

- `market regime=chaos` 时，代码对全部候选生成 `FREEZE_ALL`。[intraday_ranker.py](/srv/projects/ashare-system-v2/src/ashare_system/monitor/intraday_ranker.py#L33)
- 当天返回摘要：`市场进入 chaos，所有候选暂时冻结。`

影响：

- 这不是细粒度风控，而是一刀切封锁
- 会直接覆盖掉：
  - 执行池里本已通过预检的票
  - 板块中仍有相对强势的个股
  - 盘中反包、逆势、分歧转一致机会

结论：

- 该规则当前更像“风险开关”，不是“盘中重排器”。

### 4.3.1 `regime == chaos` 的判定理由是什么

这里要区分两层：

1. `chaos` 这个市场状态本身怎么判
2. 判出来以后执行层怎么处理

第一层目前并不是拍脑袋，而是规则型判定，来源于：

- [regime_detector.py](/srv/projects/ashare-system-v2/src/ashare_system/market/regime_detector.py#L52)

当前主规则是：

- 默认先按 `index_rebound / rotation` 起步
- 当 `limit_down_count >= max(limit_up_count + 10, 15)` 时，切到：
  - `regime_label = panic_sell`
  - `runtime_regime = chaos`
  - `confidence = 0.82`

对应证据文案是：

- `跌停/重挫扩散明显，limit_down_count=...`

所以 `chaos` 的含义不是“市场一般波动大”，而是：

- 跌停或重挫扩散明显
- 下跌扩散强于上涨扩散
- 市场处于恐慌或接近恐慌的弱势态

这套判定逻辑本身有合理性，理由是：

- 它试图把“系统性弱势”和“普通分歧轮动”区分开
- 在跌停扩散明显时，继续用常规追涨逻辑容易被集体闷杀
- 对超短系统来说，这类时段确实应当先收缩风险，而不是无脑开新仓

当前真正有问题的不是“为什么判 chaos”，而是判完以后执行层过度简化成：

- `chaos -> FREEZE_ALL`

这就把“风险收缩”做成了“全盘冻结”。更准确的做法应该是：

- 保留强势例外和已验证例外
- 对弱票冻结、对强票降权审查
- 把 `chaos` 变成更严格的执行门槛，而不是直接停止整个盘中新机会链

## 4.4 根因四：决策型席位掉队，研究和巡视在跑，策略/风控/审计没有持续形成正式产物

证据：

- 监督面板显示 `strategy overdue`、`risk needs_work`、`audit overdue`
- 研究侧仍在工作，并有 `agent_proposed` 候选产出
- 但策略侧没有新的 `持仓动作判断 / 做T/换仓方案`
- 风控侧缺新的 `放行/阻断结论`
- 审计侧缺新的 `证据复核`

影响：

- 候选和讨论可以继续滚动
- 但“能不能买、为什么买、现在放不放行”没有被写回主线
- 最终 execution intent 虽生成，但没人推动变成 execution dispatch

结论：

- 当前系统偏“材料生产”而不是“主线收敛”。

### 4.4.1 持仓换股/做T 是不是一开始就没设计进流程讨论链

结论先说：

- 不是没设计进来
- 但设计存在，不等于今天已经形成稳定闭环

当前代码里，“持仓管理 / 做T / 换仓”其实在多个层面都已经被设计进去了。

第一层是主线阶段定义里已经有：

- `position_management`
- `day_trading`
- `replacement_review`

见：

- [system_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py#L1934)

第二层是监督/催办逻辑里已经明确要求相关席位产出：

- `持仓动作判断`
- `做T/换仓方案`
- `做T/换仓预演`
- `response_lane=持仓做T/换仓`

见：

- [supervision_tasks.py](/srv/projects/ashare-system-v2/src/ashare_system/supervision_tasks.py#L632)

第三层是 runtime 任务画像里已经有：

- `day_trading`
- `replacement_review`

并且在 `defensive/chaos` 且已有持仓时，会主动偏向 `replacement_review`。

见：

- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py#L2982)
- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py#L1464)

第四层是盘中持仓巡视里，已经有真实的做T信号生成与回补票注入：

- `DayTradingEngine`
- `HIGH_SELL / LOW_BUY`
- `day_trading_rebuy_tickets`
- 将低吸回补票重新注入候选池

见：

- [scheduler.py](/srv/projects/ashare-system-v2/src/ashare_system/scheduler.py#L3452)
- [scheduler.py](/srv/projects/ashare-system-v2/src/ashare_system/scheduler.py#L3683)
- [scheduler.py](/srv/projects/ashare-system-v2/src/ashare_system/scheduler.py#L3719)
- [scheduler.py](/srv/projects/ashare-system-v2/src/ashare_system/scheduler.py#L4071)

所以严格讲：

- “持仓换股/做T”从设计上不是空白
- 甚至已经有调度、有提示词、有 runtime 画像、有部分信号生成器

但今天之所以你感受到“像没设计”，原因是它没有像新开仓那样形成稳定的主链闭环。当前问题主要有三点：

1. 它更多散落在 `supervision / runtime advisory / scheduler watch`，而不是集中在一个稳定的 `discussion -> finalize -> dispatch` 闭环里。
2. 持仓卖出快路径今天走通了，但“做T低吸回补 / 换仓替弱换强”的讨论收敛和自动派发没有稳定落地。
3. 策略席位今天上午掉队，导致“持仓动作判断”和“做T/换仓方案”没有持续写成正式主线结论。

因此，更准确的判断应该是：

- 不是“系统一开始没设计持仓换股/做T”
- 而是“这条链设计上存在，但落地上仍然是半闭环，今天没有真正跑成稳定执行链”

## 4.5 根因五：`mainline_stage` 在上午盘中误判为“盘后学习”

监督面板在上午盘中返回：

- `phase_code = morning_session`
- `mainline_stage.code = postclose_learning`

这是明显矛盾。

从代码推断，问题在于：

- `_resolve_mainline_stage_payload()` 里，只要 `latest_nightly_sandbox` 或 `latest_review_board` 非空，就直接进入 `postclose_learning` 分支。[system_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py#L1922)
- 这里没有按 `trade_date` / `phase_code` 对盘后产物做严格过滤

影响：

- 上午盘中控制面会告诉上层“当前主线是盘后学习”
- 监督和 Hermes 提示词可能被错误引导
- 人看面板也会误判系统当前重点

结论：

- 这是一个状态归因 bug，不是单纯展示问题。

## 4.6 根因六：控制面接口过重，交易时段存在超时

实测：

- `curl --max-time 5 /system/dashboard/mission-control`
- `curl --max-time 5 /system/workflow/mainline`
- `curl --max-time 5 /system/agents/supervision-board`
- `curl --max-time 5 /system/discussions/client-brief`

在交易时段都出现过：

- `Operation timed out after 5002/5003 milliseconds with 0 bytes received`

从代码看，主控面板会同步拼装很多重 payload：

- `readiness`
- `account_state`
- `supervision`
- `client_brief`
- `discussion_context`
- `runtime_context`
- `fast_opportunity`
- `position_watch`
- `execution_precheck`
- `dispatch`

见：

- [_build_dashboard_mission_control_payload](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py#L7488)

影响：

- 盘中高压时，控制面本身会变慢
- 用户看不到实时态
- 机器人/面板对运行事实的感知也可能滞后

结论：

- 这是性能问题，也是可观测性问题。

---

## 5. 当前已确认“不属于根因”的事项

以下事项今天上午不是主要根因：

### 5.1 不是仓位上限拦住了买入

当前预检显示：

- 股票仓位约 `8.6% / 40%`
- 预算仍有 `31791.45`
- 通过预检 `2` 只，阻断 `0` 只

所以不是仓位不够。

### 5.2 不是风控硬阻断导致无法买入

当前两只执行池标的：

- `000657.SZ`
- `000400.SZ`

状态均为：

- `risk_gate=allow`
- `audit_gate=clear`
- `approved=true`

所以不是风控闸门把它们拦掉。

### 5.3 不是 Windows / QMT 当时完全不通

证据：

- 今天 `09:55:34` 有真实卖单回执提交成功
- 全程账户/订单/持仓轮询大量成功
- `GET /qmt/account/orders`、`positions`、`asset`、`tick` 都正常返回

所以：

- Windows 桥不是今天上午“无买单”的主根因
- 当前主根因在 Linux 侧业务编排

---

## 6. 当前系统状态总评

如果按你最初的设计目标评估，当前系统更接近：

- “盘中运行监控与讨论框架”

而不是：

- “能在开盘时段持续自主发现机会、完成收敛、自动派发和落单的实盘系统”

当前已经具备的能力：

1. 盘中高频巡视
2. 持仓监控
3. 候选池与讨论状态机
4. 执行预检
5. execution intent 生成
6. Windows/QMT 桥接提交

当前仍未真正贯通的关键能力：

1. 开盘执行主链
2. 盘中持续自动 finalize / dispatch
3. chaos 市场下的精细化执行，而不是全冻结
4. 策略/风控/审计对执行链的持续产物回写
5. 实时控制面的快速、稳定、可读反馈

---

## 7. 改进清单

以下按优先级给出。

## 7.1 P0：必须立即改

### P0-1. 把 `开盘执行` 从空壳改成真实编排器

当前问题：

- `task_execute_open()` 只写审计，不执行真实业务。

必须改成：

1. 拉取 `execution_precheck`
2. 若有 `approved` 标的，生成/刷新 `execution_intents`
3. 若满足条件，直接发起 `dispatch`
4. 回写审计、回执、异常原因

否则 09:30 这个任务没有任何实盘意义。

### P0-2. 把“自动派发”从 `finalize` 单点触发改为“阶段性自动收口”

当前问题：

- 只有终审才自动 dispatch
- 讨论到 `round_summarized` 仍会悬空

建议：

1. 当 `round_summarized + approved_intents > 0 + session_open=true` 时，允许自动触发派发
2. 若风控/审计无新阻断，进入“执行预检通过后自动派发”
3. 对派发失败写明确原因，而不是静默停留在 pending

### P0-3. 把 `chaos -> FREEZE_ALL` 改为渐进式风险收缩

当前问题：

- chaos 市场下一刀切冻结所有候选

建议：

1. 先降仓/降权，而不是全冻结
2. 保留强势例外：
   - 龙头
   - 逆势强票
   - 已通过二次确认的候选
3. 区分：
   - `freeze_new_entries`
   - `freeze_weak_candidates`
   - `allow_only_verified_leaders`

而不是统一 `FREEZE_ALL`。

### P0-4. 修正 `mainline_stage` 判定逻辑

当前问题：

- 上午盘中被误判成 `postclose_learning`

建议：

1. `latest_review_board / latest_nightly_sandbox` 必须校验 `trade_date`
2. 盘中阶段优先由 `market phase` 决定，不允许被旧盘后产物覆盖
3. 若状态冲突，面板必须显示“状态冲突”，而不是直接落到盘后学习

### P0-5. 把 supervision 从“催办”升级为“强制落主线”

当前问题：

- supervision 能识别 strategy/risk/audit 超时
- 但不会强制触发下一步派发或失败升级

建议：

1. 当 `strategy overdue + approved precheck exists` 时，自动生成“执行推进任务”
2. 当 `risk needs_work + session_open=true` 时，必须在时限内给出放行/阻断结论
3. 当 `audit overdue` 时，至少不应阻塞 dispatch 主链

## 7.2 P1：近期应改

### P1-1. 增加 `09:30-10:30` 连续执行窗口任务

当前问题：

- 只有 `09:30` 一次性开盘执行

建议：

新增连续任务，例如每 30 秒或 1 分钟：

- 检查 execution pool
- 检查 approved precheck
- 检查 intent 未派发项
- 继续推进 dispatch / retry

否则一旦 09:30 当刻没闭合，后面整段窗口就空转。

### P1-2. 让 runtime 在盘中持续刷新，而不是停留在 09:16 的旧上下文

当前现象：

- runtime 报告最新时间仍停在 `09:16:18`

建议：

1. 盘中定时刷新 runtime context
2. 区分：
   - 盘前基础 runtime
   - 盘中增量 runtime
3. 控制面显示 runtime freshness

### P1-3. 控制面改成缓存型视图，不要盘中现拼重接口

当前问题：

- mission-control 等接口盘中直接超时

建议：

1. 把 `client_brief / supervision / precheck / dispatch / account_state` 做缓存拼装
2. 面板读缓存，不在请求时重建全量 payload
3. 对每块显示 `generated_at / stale_seconds`

### P1-4. 统一“候选池 / 执行池 / precheck / intents / dispatch”状态展示

当前现象：

- `client_brief`、`execution_pool_case_ids`、`execution_intents`、`execution_dispatch` 之间要跨接口拼

建议：

增加一张单独的“执行链总表”，直接展示：

- 入池
- 预检
- intent
- dispatch
- receipt
- fill

避免主控只看见“selected”，却看不见“实际上没 dispatch”。

## 7.3 P2：中期应改

### P2-1. 让策略链具备真正的盘中重编排能力

当前问题：

- strategy 主要还是 compose + 候选排序
- 盘中更像静态草案，而不是动态执行器

建议：

1. 盘中根据热点、强弱切换 playbook
2. 做 T、换仓、新开仓拆成三条独立执行逻辑
3. 让 strategy 直接对“已有持仓”和“新候选”共同编排

### P2-2. 让 risk / audit 的输出成为结构化执行输入

当前问题：

- supervision 能看见它们缺产物
- 但 execution dispatch 不直接消费这些席位的结构化结论

建议：

把它们产出的：

- 放行/阻断结论
- 证据复核
- 做T/换仓边界

变成 dispatch 前的结构化输入项。

---

## 8. 最终判断

今天上午系统的问题，不是单一 bug，而是以下五个问题叠加：

1. `开盘执行` 是空壳
2. `execution intent` 生成后没有自动派发
3. `chaos` 规则一刀切冻结所有候选
4. 策略/风控/审计产物没有及时回写执行主线
5. 控制面既慢又有状态误导

如果只修其中一个，系统仍然会继续表现为：

- 很忙
- 有巡检
- 有讨论
- 有候选
- 偶尔有卖出
- 但买入落单能力不稳定

要接近你要的“开盘即战斗、盘中持续寻机、自动收敛并落单”的设计目标，至少需要先完成：

- P0-1
- P0-2
- P0-3
- P0-4

这四项不改，明天继续跑，系统仍然大概率会停留在“看起来运行很多，真正买入很少”的状态。

补充两点结论，方便你直接拿去和专家对：

1. `regime == chaos` 不是无依据标签，而是“跌停/重挫扩散显著强于涨停扩散”的规则型判定；问题不在判定存在，而在执行层把它粗暴落成了 `FREEZE_ALL`。
2. “持仓换股/做T”不是最初没设计，而是已经设计进主线阶段、监督任务、runtime 画像和持仓巡视；问题在于这些设计没有汇成稳定的讨论收口与自动执行闭环。
