# 阶段任务与默认策略质量审计报告

日期：2026-04-20

## 执行摘要

- 这次核查确认，系统不是“没有阶段任务”，而是“默认入口长期过于粗糙”，导致后续竞价、盘中、尾盘虽然有任务，但吃进去的候选先天不够贴近市场主线。
- 已完成两类关键改造：
  - 把 `/runtime/jobs/pipeline` 从“成交量粗排 + 简单粗筛”升级为“默认超短活跃度评分 + 热点/股性/战法上下文”。
  - 把调度器 `task_run_pipeline` 从“基础样本池”升级为“默认策略候选池”，并补齐 `runtime_context` 持久化，供竞价与盘中任务直接消费。
- 真实结果已经发生变化：
  - 2026-04-20 22:23:59 的 `latest_runtime_context` 已变成热点强势票主导，而不再是上午那批冷门弱势老票。
  - `playbook_count` 已从 `0` 变成 `12`，说明集合竞价、开盘买入、盘中巡检现在已经能读到真实战法上下文。
- 仍有一个真实性能缺口：
  - live 环境下全量主板 `POST /runtime/jobs/pipeline` HTTP 响应仍然偏慢，25 秒内可能拿不到返回体，但后台实际会完成写盘。这说明“结果链”已改好，“接口时延”还需要继续压缩。

## 一、盘前到盘后的任务是否真的和策略挂钩

### 1. 盘前阶段

- `07:30 新闻扫描`
  - handler: `data.fetcher:fetch_news`
  - 策略挂钩：弱
  - 作用：主要产出 event context，供后续候选阻断与催化加成使用。
  - 评价：是上游情报层，不直接排序。

- `08:00 竞价预分析`
  - handler: `sentiment.calculator:pre_market`
  - 策略挂钩：中
  - 作用：更新市场情绪画像，为后续 screener / routing 提供 regime、仓位上限、允许战法。
  - 评价：是策略环境底座。

- `09:00 环境评分`
  - handler: `sentiment.calculator:compute_daily`
  - 策略挂钩：高
  - 作用：给出 `MarketProfile`，影响仓位上限、情绪阶段、regime 和 allowed_playbooks。
  - 评价：是默认策略链的第一道环境约束。

- `09:10 买入清单`
  - handler: `strategy.buy_decision:generate_buy_list`
  - 旧状态：弱挂钩
  - 原因：原实现是 `scheduler.task_run_pipeline`，代码里明确写着“降级为基础样本生成，不再执行因子/AI/环境打分”。
  - 现状态：已改造
  - 改后逻辑：
    - 先按默认活跃度评分筛出强势候选。
    - 再做热点/股性/战法上下文补齐。
    - 再把结果持久化到 `latest_runtime_context`，供竞价和盘中任务复用。
  - 评价：这是本次最关键的修复。

### 2. 集合竞价阶段

- `09:20 竞价快照`
  - handler: `strategy.auction:scan_0920`
- `09:24 竞价快照`
  - handler: `strategy.auction:scan_0924`
- 策略挂钩：高
- 代码链：
  - 调度器 `_run_auction_snapshot`
  - 从 `runtime_context.playbook_contexts` 读取 `playbook_map` 和 `sector_map`
  - 调用 `AuctionEngine.evaluate_all`
- 竞价真正挂钩的内容：
  - 不同 playbook 走不同阈值。
  - `leader_chase`、`divergence_reseal`、`sector_reflow_first_board` 各自有独立的量比、竞价涨幅、低开 kill 线。
  - 会叠加板块共振修正，不是单票静态阈值。
- 之前的断点：
  - `playbook_count=0`，导致竞价阶段只能退化成默认 `leader_chase`。
- 现在的状态：
  - 2026-04-20 22:23:59 的 `latest_runtime_context.playbook_count=12`
  - 说明集合竞价已经能吃到真实战法上下文，不再是盲判。

### 3. 开盘与盘中阶段

- `09:30 开盘执行`
  - handler: `strategy.buy_decision:execute_open`
  - 策略挂钩：高
  - 依赖：`playbook_contexts`、竞价信号、仓位上限、买入评分。
  - 评价：前提是盘前 runtime_context 要像样。本次改造已经补前提。

- `持仓快巡视`
  - handler: `position.watch:fast_realtime`
  - 频率：3 秒
  - 策略挂钩：中到高
  - 当前挂钩内容：
    - 读取 `runtime_context` 的 playbook/exit_params
    - 根据持仓 ATR、价差、瞬时波动、pending sell tracker 做快速止盈止损和做 T 触发
  - 评价：
    - 对“已有持仓”的秒级巡视是成立的。
    - 对“全市场秒级新机会”的覆盖仍不够广，因为它不是全市场逐笔扫描器。

- `持仓深巡视`
  - handler: `position.watch:check_realtime`
  - 频率：30 秒
  - 策略挂钩：高
  - 作用：比快巡视更完整地结合 discussion/runtime/event context 做持仓处理。
  - 评价：更适合结构化卖出和尾盘决策，不是抢瞬时盘口。

- `盯盘巡检`
  - handler: `monitor.market_watcher:check_once`
  - 频率：5 分钟
  - 策略挂钩：中
  - 作用：生成异动告警，写入 event bus 和 monitor state。
  - 评价：偏监控，不是极致超短入口。

- `微观巡检`
  - handler: `monitor.market_watcher:check_micro`
  - 频率：1 分钟
  - 策略挂钩：中
  - 作用：补充更细颗粒度的市场异动。
  - 评价：比 5 分钟更快，但仍不是真正的逐秒全市场抢筹引擎。

- `盘中机会快扫`
  - handler 内部：`run_fast_opportunity_scan`
  - 策略挂钩：中
  - 当前挂钩内容：
    - 读取 `runtime_context.hot_sectors`
    - 读取 `playbook_contexts`
    - 结合 5 分钟量比、动量斜率、板块同步信号识别 `early_momentum / acceleration / pre_limit_up / abnormal_drop`
  - 评价：
    - 对“盘中机会”已经不是纯涨停播报，开始识别预涨停、加速、异常下跌。
    - 但扫描对象仍受 `runtime_context` 候选池和近期 watchlist 限制，不是全市场覆盖。

### 4. 尾盘阶段

- `14:30 尾盘决策`
  - handler: `strategy.sell_decision:tail_market`
  - 策略挂钩：高
  - 作用：调用 `run_tail_market_scan -> run_position_watch_scan`
  - 依赖：runtime_context、discussion_context、event_context、playbook exit params
  - 评价：链路完整，偏持仓治理和尾盘处理，不是主打抓新热点。

### 5. 盘后阶段

- `因子计算`
  - handler: `factors.engine:compute_all`
- `因子有效性巡检`
  - handler: `strategy.factor_monitor:refresh`
- `股性画像刷新`
  - handler: `strategy.stock_profile:refresh`
- `账本回测验证`
  - handler: `strategy.evaluation_ledger:reconcile_backtest`
- 策略挂钩：高
- 作用：为第二天默认策略、compose、战法路由提供因子、股性、回放与有效性依据。
- 评价：这层更多是“学习与治理”，不是 T+0 执行动作。

## 二、这次默认策略到底改了什么

### 1. `/runtime/jobs/pipeline`

- 改造前
  - 按 `volume -> price delta -> price` 粗排。
  - `StockScreener.run` 没吃到有效 factor_scores。
  - `_score_snapshot` 对负动量票惩罚太轻。
  - 结果容易把高成交但弱趋势的老票顶上来。

- 改造后
  - 先用新的 `score_runtime_snapshot` 做默认超短活跃度评分。
  - 评分结构更偏超短：
    - 正动量强化
    - 接近涨停区间加分
    - 负动量和冷门票明确降权
    - 排名惩罚仍保留，但不再让高成交弱票天然占优
  - `StockScreener.run` 现在直接吃 `factor_scores=seed_score_map` 和 `profile=market_profile`
  - 之后继续做：
    - 板块画像 `sector_profiles`
    - 股性画像 `behavior_profiles`
    - 战法路由 `playbook_contexts`
    - 主线贴合重排 `market_alignment`

### 2. 调度器 `task_run_pipeline`

- 改造前
  - 只是“基础样本池”。
  - 摘要文案明确要求 Agent 之后再组织 compose。
  - 不会把默认策略结果落到 `latest_runtime_context`。

- 改造后
  - 直接生成默认策略候选池。
  - 同时持久化：
    - `latest_runtime_context`
    - `sector_profiles`
    - `behavior_profiles`
    - `playbook_contexts`
  - 这样 09:20 / 09:24 集合竞价、09:30 开盘执行、盘中快扫就不再吃旧上下文。

### 3. `playbook_count=0` 的断点

- 断点原因
  - 市场画像给的是 `chaos`
  - 我第一轮只补了 `allowed_playbooks`
  - 但 `StrategyRouter` 只有 `trend / rotation` 才有 route
  - 所以 playbook 仍然分不出来

- 已修复
  - 增加 conservative probe routing
  - 当市场主链已经有明显热点强度，但情绪侧仍未正式放开战法时：
    - 允许保守地把路由 regime 调整为 `rotation` 或 `trend`
    - 只用于默认策略链路的战法路由，不等于放松实盘风控

## 三、真实验证结果

### 1. 改造前的真实状态

- `2026-04-20 10:32:08`
- `latest_runtime_context`
  - `playbook_count=0`
  - 候选以 `000002.SZ / 000016.SZ / 000010.SZ / 000008.SZ ...` 为主
  - 明显偏冷门地产、旧题材、弱势老票
  - 不符合“贴近热点主线、涨停基因、超短风格”的目标

### 2. 改造后的真实状态

- `2026-04-20 22:23:59`
- `latest_runtime_context`
  - `job_type=pipeline`
  - `decision_count=12`
  - `buy_count=10`
  - `hold_count=2`
  - `playbook_count=12`

- 当前前排候选
  - `002361.SZ 神剑股份`
  - `002309.SZ 中利集团`
  - `600601.SH 方正科技`
  - `600103.SH 青山纸业`
  - `002217.SZ 合力泰`

- 当前候选表现
  - 已明显偏向涨停/强势方向
  - 已出现 `CPO / ETC / BC电池 / 东盟北部湾 / DeepSeek / 中盘` 等当前活跃方向
  - 已不再是上午那批冷门弱势老票

- 当前战法路由
  - `playbook_count=12`
  - 前 8 个可见上下文均已分配 `leader_chase`
  - 说明：
    - 默认候选已接到战法路由
    - 集合竞价可以读取真实 playbook
    - 开盘买入和盘中任务可以读取真实 playbook_context

### 3. 真实性能缺口

- live 环境下 `POST /runtime/jobs/pipeline` 仍然存在接口时延问题
  - 25 秒内可能拿不到 HTTP 响应体
  - 但后台 serving 文件会更新
- 这不是“没执行”，而是“执行完成但接口返回慢”
- 结论
  - 候选质量主链已经改好
  - 接口时延还需要继续专项治理

## 四、对“集合竞价挂什么任务、快盘策略质量怎么样”的结论

### 集合竞价挂钩结论

- 现在集合竞价挂钩的是：
  - 默认候选池
  - 默认路由后的 `playbook_contexts`
  - `AuctionEngine` 的 playbook 阈值
  - 板块竞价共振判断
- 这意味着：
  - 竞价不再是统一模板
  - 同一只票在不同战法下，PROMOTE / HOLD / DEMOTE / KILL 阈值不同

### 快盘策略质量结论

- 对“持仓秒级巡视”，质量已经够用
  - 3 秒快巡视已挂 playbook exit params
  - 已支持新仓不可卖也进入巡视
  - 已支持 pending sell 跟踪，避免重复提交

- 对“盘中新机会秒级全市场捕捉”，质量仍是中等
  - 已有 `early_momentum / acceleration / pre_limit_up / abnormal_drop`
  - 但扫描范围仍依赖 runtime_context 候选池和 watchlist
  - 还不是全市场逐秒扫射型机会引擎

- 对“超短风格贴市场”，这次默认入口已经明显改善
  - 候选已经从冷门弱势票切到强势主线票
  - 但更激进的盘口级抢首板、抢分歧转强，仍需要更高频 tick / orderbook 支撑

## 五、剩余未完成项

- P0
  - 压缩 `/runtime/jobs/pipeline` 的 live 时延
  - 避免“后台更新了，但前端/调用方等不到响应”

- P1
  - 把 `run_fast_opportunity_scan` 从“候选池机会扫描”扩成“更广市场机会扫描”
  - 让盘中新机会不只依赖盘前已入池标的

- P1
  - 给 `leader_chase` 之外的 playbook 增加更多真实分配比例
  - 当前主线强势日容易大量路由到 `leader_chase`

- P1
  - 进一步提高 `resolved_sector` 精度
  - 当前仍会出现个别票被归到泛化板块，如 `中盘`

- P2
  - 把盘口级封单、撤单、逐笔主动买卖量接入盘中快策略
  - 这是“快人一步”抓预涨停的关键

## 结论

- 现在可以明确说：
  - 阶段任务本身大多是存在的
  - 之前真正拖后腿的是默认入口质量太差

- 本次改造后：
  - 默认候选已经更贴市场主线
  - `playbook_contexts` 已真实打通
  - 集合竞价、开盘、盘中快扫终于开始和战法挂钩

- 还不能夸大的一点：
  - 这套系统离“全市场秒级超短猎手”还有距离
  - 当前更准确的评价是：
    - 盘前默认质量已明显上台阶
    - 战法链已经打通
    - 盘中广域机会捕捉和接口时延仍需继续补
