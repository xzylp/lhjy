# ashare-system-v2 终极技术升级方案与改造任务书

## 一、终极技术升级方案（7 大维度对照）

| 维度 | 核心要点 | 现状判断 | 升级目标 |
| :--- | :--- | :--- | :--- |
| **集合竞价抢跑** | 09:15-09:24 竞价量价监控 → PROMOTE/KILL 筛选 | ❌ 完全缺失，09:30才开始 | **引入独立 AuctionEngine，盘前提前建立优选排序** |
| **盘中微观节奏** | 1分钟级 PEAK_FADE / VALLEY_HOLD / RHYTHM_BREAK 识别 | ⚠️ exit_engine 逻辑好，但粒度 5min 太粗 | **引入 MicroRhythmTracker 实现1分钟级捕捉并对接退出执行** |
| **波峰波谷自适应** | 按 playbook × regime 动态调整 ATR 止盈止损倍数 | ⚠️ 全部硬编码了一套固定参数 | **SellDecisionEngine 取消固定参数，实现剧本与阵型感知的多轨动态参数** |
| **事件驱动总线** | asyncio.Queue 实时响应 NEGATIVE_NEWS / PRICE_ALERT | ❌ 纯 cron 定时轮询驱动，响应慢 | **构建 EventBus 实时调度内核与快速通道管道** |
| **多 Agent 深度论证** | 动态轮次 + 证据矛盾检测 + 加权投票收敛 | ⚠️ 框架在但相互断裂（固定死两轮、缺少实质拉扯） | **重写 StateMachine，加入能自纠错的实质论证流与权重汇聚** |
| **闭环归因自进化** | attribution → score → prompt_patcher → registry 流程打通 | ❌ self_evolve / continuous 完全是孤岛 | **补齐 PromptPatcher，从每日结算数据中提出并修改系统自身的提示词实现真进化** |
| **夜间沙盘推演** | 23:00 自动回放 watchlist + 模拟参数动态自调优势 | ❌ replay 方法建好但从未调用闭环 | **部署 NightlySandbox 推演总结影响次日打分权重** |

---

## 二、框架任务单（48 个原子任务）

当前状态标注说明：
- `[x]` = 核心底座与框架代码**已完成**
- `[ ]` = 需后续开发人员接手继续写配置/连线

### 模块 A：集合竞价抢跑引擎 [P0]
- [x] A1.1 新建 `data/auction_fetcher.py` — AuctionFetcher 类 + fetch_snapshots 方法骨架
- [ ] A1.2 补全 `auction_fetcher.py` HTTP 调用 Gateway (`_fetch_from_gateway`)
- [ ] A1.3 补全 `auction_fetcher.py` 降级读取 akshare (`_fetch_from_akshare`)
- [x] A1.4修改 `contracts.py` 新增 AuctionSnapshot + AuctionSignal 数据模型
- [x] A2.1 新建 `strategy/auction_engine.py` — AuctionEngine 竞价预判与打分实现
- [ ] A2.2 继续在 `auction_engine.py` 补充板块级竞价共振逻辑
- [ ] A2.3 继续在 `auction_engine.py` 补充与 T-1 竞价量价异常对比分析
- [x] A2.4 修改 `buy_decision.py` — `generate()` 接收 auction_signals，实现 PROMOTE 加分，KILL 直接淘汰
- [ ] A3.1 修改 `scheduler.py` — 显式注册 09:20 + 09:24 竞价捕获 cron 任务

### 模块 B：盘中微观节奏捕捉 [P1]
- [x] B1.1 修改 `contracts.py` 新增 MicroBarSnapshot + MicroSignal 数据模型
- [x] B1.2 新建 `monitor/micro_rhythm.py` — MicroRhythmTracker 骨架实现
- [x] B2.1 修改 `exit_engine.py` — `check()` 接纳 micro_signal 参数进行快速退场
- [ ] B3.1 修改 `scheduler.py` — 增设 `*/1` 的持仓微观状态巡检任务

### 模块 C：波峰波谷自适应出场 [P2]
- [x] C1.1 修改 `sell_decision.py` — 梳理 PLAYBOOK_EXIT_PARAMS 动态参数表
- [x] C1.2 修改 `sell_decision.py` — 改造 `evaluate()` 使其接受 playbook + regime 两个维度参数
- [x] C2.1 修改 `sell_decision.py` — 实现对 REGIME_MODIFIERS 阵型修正系数的乘数放大计算逻辑

### 模块 D：事件驱动响应中枢 [P1]
- [x] D1.1 修改 `contracts.py` 新增 MarketEvent + EventType 事件总线所用模型
- [x] D1.2 新建 `data/event_bus.py` — 搭建基于 Asyncio 的事件总线与订阅器
- [ ] D2.1 修改 `scheduler.py` — 初始化总线，注册事件监听组
- [ ] D2.2 修改 `scheduler.py` — 新增 `_on_negative_news()` 方法桥接新闻信号事件
- [ ] D2.3 修改 `scheduler.py` — 新增 `_on_price_alert()` 方法桥接异常涨跌事件
- [ ] D3.1 修改 `event_fetcher.py` — 实现增量抓取流并自动对外发射事件通知
- [ ] D3.2 修改 `market_watcher.py` — 挂钩内部价格计算节点，发射价格监控事件

### 模块 E：多 Agent 深度论证活化 [P1]
- [x] E1.1 修改 `state_machine.py` 泛化讨论态为动态流：增设 `round_running`/`round_summarized`
- [x] E1.2 修改 `state_machine.py` 补充通用 `start_round(n)` 及条件续作 `can_continue_discussion()`
- [x] E1.3 修改 `discussion_service.py` 移除 1、2 轮硬切代码，加入动态续轮和超时熔断控制
- [x] E2.1 修改 `finalizer.py` `build_finalize_bundle()` 从外部引入 `agent_weights` 使其成为影响决策因数
- [ ] E2.2 修改 `discussion_service.py` 收官时统一读取当前 `score_state` 给到最终决定者
- [x] E3.1 修改 `contradiction_detector.py` 补充 9 对关键证据点映射，加入 `detect_evidence_conflicts()` 检测
- [x] E4.1 修改 `opinion_validator.py` 将次序 2 轮特权限制解绑至 `>= 2` 以适配多轮次论证

### 模块 F：闭环归因自进化 [P0]
- [x] F1.1 修改 `team_registry.final.json` 为中台添加各 agent 的 `agent_weights` 打分控制节点
- [x] F1.2 修改 `score_state.py` 追加 `export_weights()` / `run_daily_settlement()` 基础工具
- [x] F2.1 新建 `learning/prompt_patcher.py` 撰写针对 `system_prompt` 的覆写模块 (Lessons_Patch机制)
- [x] F2.2 修改 `auto_governance.py` 给其附加由 Attribution 衍生提取为 Lessons Patch 的逻辑转换器
- [x] F3.1 新建 `learning/registry_updater.py` 完成针对 JSON 配置热更新的底层控制类
- [ ] F4.1 修改 `scheduler.py` 按照收盘时序依次安排 16:30/16:45/17:00 三阶段闭环执行队列
- [ ] F5.1 修改 `self_evolve.py` 完成底层更新后与次日战略权重调配计划任务绑定
- [ ] F5.2 修改 `continuous.py` 追加当日未利用大回撤数据的次日自我验证流程调度

### 模块 G：夜间沙盘推演 [P2]
- [x] G1.1 新建 `strategy/nightly_sandbox.py` 夜间大模型策略兵棋推演控制台
- [ ] G1.2 继续在 `nightly_sandbox.py` 撰写 `_simulate_param_adjustment()` 历史对比模拟推算器
- [ ] G1.3 在 `nightly_sandbox.py` 内实现向后衔接并整合 `discussion_service` 的 Replay 能力模块
- [x] G2.1 修改 `contracts.py` 追加对于 Sandbox 模拟的记录推算输出 SandboxResult 模型
- [ ] G3.1 修改 `scheduler.py` 向夜晚 23:00 填补触发执行
- [ ] G3.2 修改 `buy_decision.py` 使次日竞价能前置消费并利用夜间 `sandbox` 的结论指导

### 模块 H：数据链整固 [P2]
- [ ] H1.1 修改 `precompute.py` 引入全链路时间截面强制 `as_of_time` 锁定防止未来函数
- [ ] H1.2 修改 `serving.py` 同步调整所有的 serving 端出货必须带上 `as_of_time` 约束条件
- [x] H2.1 新建/修改 `freshness.py` 建立数据保质期 `tag_freshness()` 方法

> 原始归档：/home/yxz/.gemini/antigravity/brain/2f546fc9-c38b-4fac-854f-897d3d5c5360/task.md
