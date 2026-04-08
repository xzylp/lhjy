# A股量化交易系统改造计划（Codex评审版）

> 对 [quant-strategy-plan-v1.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-strategy-plan-v1.md) 的补充评审与落地方案  
> 日期：2026-04-06

---

## 1. 我的总体判断

`v1` 方案方向是对的，尤其是这三点：

- 不能再把系统只当“选股器”，必须变成“有交易语境的决策系统”。
- 必须补上市场状态、板块联动、个股股性、退出逻辑。
- 退出和风控不能再是通用模板，必须跟战法绑定。

但 `v1` 还有三个明显风险：

- **一次性改太大**：市场状态机、板块生命周期、四大战法、股性画像、退出监控、模型训练一起上，交付面太宽，容易做成一套很完整的文档，但跑不出第一版稳定结果。
- **日线和分时混在一起**：像“情绪周期”“板块生命周期”“龙头带动扩散”有的更适合日级，有的更适合分钟级；如果不先拆层，后面指标会互相污染。
- **数据可得性和实时性要先核实**：例如封单量、炸板速度、午后回封率、板块扩散速度，这些指标都很好，但依赖分钟线甚至逐笔/盘口数据，不能默认当前链路已经稳定可用。

所以我的观点不是否定 `v1`，而是：

- **保留 v1 的战略方向**
- **降低第一阶段实现野心**
- **先做“可回测、可解释、可上线”的规则型交易框架**
- **再逐步把复杂度推高到股性和战法学习**

---

## 2. 我建议的目标形态

我不建议继续用“全市场线性总分 -> 统一买卖阈值”的范式。

我建议改成下面这条链：

```text
日级市场状态
  -> 日级板块/题材排序
  -> 候选池压缩
  -> 战法路由
  -> 战法内相对排序
  -> 仓位计划
  -> 分时执行与退出
  -> 战法归因与复盘
```

关键差别：

- **不再问“这只票总分多少”**
- 而是先问：
  - 今天能不能做
  - 今天该做哪一类机会
  - 这只票是不是该类机会里最优的前几只
  - 做错了怎么立刻撤

---

## 3. 对 v1 方案的具体意见

## 3.1 我认同的部分

- `Layer 0/1/2/3` 的分层思路是合理的。
- “龙头识别必须做相对排名，不是绝对涨幅”这个判断是对的。
- “退出逻辑要战法化”是最关键的改造点之一。
- “股性画像”对 A 股短线是高价值模块，不能再用通用动量替代。

## 3.2 我建议修正的部分

### A. 市场状态不要一开始做得太复杂

`STRONG_TREND / ROTATION / DEFENSIVE / CHAOS` 可以保留，但第一阶段不要再叠加太多“情绪周期 x 市场状态”的交叉规则。

第一版建议：

- 先保留现有四阶段情绪 `冰点/回暖/主升/高潮`
- 再新增一个独立字段 `regime`
- 先只做 4 个 regime：
  - `trend`
  - `rotation`
  - `defensive`
  - `chaos`

这样不会破坏现有 [MarketProfile](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/contracts.py) 的使用方式。

### B. 板块联动要先做“可计算版本”，不要上来就追求最完整

第一阶段不要强依赖逐笔级别特征，例如：

- 封单量 / 流通市值
- 炸板速度
- 扩散时间差

这些可以列为二阶段增强。

第一阶段先用稳定拿得到的分钟或日线数据做：

- 板块相对强度
- 板块涨停数
- 板块上涨占比
- 板块前排强度
- 板块延续天数
- 板块日内回流幅度

### C. 战法不要一开始做成“完全插件化交易宇宙”

先只做三类最有交易价值、最容易回测的 playbook：

- `leader_chase`
- `divergence_reseal`
- `sector_reflow_first_board`

`low_buy_core` 可以放到第二阶段，因为低吸对支撑位定义、股性和大盘容错要求更高，回测也更容易失真。

### D. 退出逻辑要先落“分时失败退出”，再做“收益最大化退出”

第一阶段优先级应该是：

1. 开仓失败立撤
2. 板块退潮离场
3. 炸板/回封失败离场
4. 时间止损
5. 分批止盈

原因很简单：快进快出的收益核心不是止盈做得多花，而是错了以后撤得快。

---

## 4. 和现有 v2 代码骨架的衔接判断

`ashare-system-v2` 当前骨架并不是空白，已经有可以复用的支点：

- 因子引擎：`src/ashare_system/factors/`
- 策略目录：`src/ashare_system/strategy/`
- 风控目录：`src/ashare_system/risk/`
- 回测目录：`src/ashare_system/backtest/`
- 情绪保护：`src/ashare_system/risk/emotion_shield.py`
- 市场画像契约：`src/ashare_system/contracts.py`

所以我不建议另起一套平行架构，而是按现有骨架补下面这些模块：

```text
src/ashare_system/
  sentiment/
    regime.py
    sector_cycle.py
  strategy/
    router.py
    playbooks/
      leader_chase.py
      divergence_reseal.py
      sector_reflow.py
    leader_rank.py
    stock_profile.py
    exit_engine.py
  factors/
    behavior/
      board_behavior.py
      sector_linkage.py
    micro/
      intraday_strength.py
  backtest/
    playbook_runner.py
    attribution.py
```

---

## 5. 详细改造计划

## 5.1 Phase 0：先把数据契约和时序分层做对

### 目标

让后面的市场状态、板块联动、战法路由、退出逻辑都建立在统一数据对象上，而不是每个模块自己算一遍。

### 要做的事

- 扩展 [MarketProfile](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/contracts.py)：
  - 新增 `regime`
  - 新增 `regime_score`
  - 新增 `allowed_playbooks`
  - 新增 `hot_sector_profiles`
- 增加 `SectorProfile`、`StockBehaviorProfile`、`PlaybookContext`、`ExitContext` 契约。
- 在 `precompute.py` 里拆出两个层次：
  - **日级快照**：情绪、板块、股性画像
  - **分时快照**：日内强度、回封、退潮、失败退出触发

### 交付物

- 新的数据模型
- 一套统一 precompute 输出结构
- 对应测试和样例 payload

### 验收标准

- 同一只股票、同一天、同一时刻，战法路由和退出引擎读取的是同一份上下文。
- 不再允许策略模块自行隐式重复计算市场状态。

---

## 5.2 Phase 1：市场状态机升级，但先做“稳态版”

### 目标

在现有“情绪四阶段”之上，增加更贴近交易执行的 `regime`，但不把状态机做成不可维护的规则森林。

### 第一版输入指标

- 涨停家数
- 跌停家数
- 炸板率
- 封板率
- 最高连板
- 上涨占比
- 前一日涨停溢价
- 前三题材集中度

### 输出

- `sentiment_phase`
- `regime`
- `position_ceiling`
- `allowed_playbooks`
- `market_risk_flags`

### 代码落点

- 新增 `src/ashare_system/sentiment/regime.py`
- 对接 `src/ashare_system/risk/emotion_shield.py`
- 在 [contracts.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/contracts.py) 扩展 `MarketProfile`

### 测试

- 新增 `tests/test_phase_market_regime.py`
- 覆盖：
  - 极弱盘面判为 `chaos`
  - 高封板率高连板判为 `trend`
  - 轮动但不集中判为 `rotation`
  - 高炸板低溢价判为 `defensive`

### 我对这一阶段的要求

- 不追求“完全拟合所有盘面”
- 先做到“错了也偏保守，不会激进误判”

---

## 5.3 Phase 2：板块联动引擎，替代“热门板块简单加权”

### 目标

把“板块热度”从一个单值因子，升级成可以支持战法路由的 `SectorProfile`。

### 第一版指标

- `sector_return_rel_market`
- `sector_up_ratio`
- `sector_limit_up_count`
- `sector_front_rank_strength`
- `sector_active_days`
- `sector_reflow_score`
- `sector_dispersion_score`

### 第一版先不做

- 封单量 / 流通市值
- 逐笔炸板速度
- 毫秒级扩散节奏

### 输出

每个板块输出：

- `life_cycle`: `start / ferment / climax / retreat`
- `strength_score`
- `leader_symbols`
- `breadth_score`
- `reflow_score`

### 代码落点

- 新增 `src/ashare_system/sentiment/sector_cycle.py`
- 新增 `src/ashare_system/factors/behavior/sector_linkage.py`
- 如果已有 `monitor/market_watcher.py` 数据入口，优先复用，不重复造轮子

### 测试

- 新增 `tests/test_phase_sector_linkage.py`
- 给定板块成分、涨停数、上涨占比、历史排名，验证生命周期识别和排序结果

### 风险提醒

板块这层最容易因为“题材映射不稳定”而漂。

所以第一版要允许：

- 行业板块
- 概念板块
- 自定义主题簇

三种来源并存，但排序逻辑统一。

---

## 5.4 Phase 3：个股股性画像与龙头相对排名

### 目标

把“龙头”从绝对涨幅定义，改成“板块内相对地位 + 历史行为画像”的组合。

### 股性画像字段

- `board_success_rate_20d`
- `bomb_rate_20d`
- `next_day_premium_20d`
- `reflow_reseal_rate_20d`
- `avg_sector_rank_30d`
- `leader_frequency_30d`
- `style_tag`

### 龙头相对排名字段

- `zt_order_rank_in_sector`
- `turnover_rank_in_sector`
- `relative_return_rank_in_sector`
- `sector_diffusion_contribution`
- `intraday_resilience_score`

### 输出

- `StockBehaviorProfile`
- `LeaderRankResult`
- `is_core_leader`

### 代码落点

- 新增 `src/ashare_system/strategy/stock_profile.py`
- 新增 `src/ashare_system/strategy/leader_rank.py`
- 可补因子实现到 `factors/behavior/board_behavior.py`

### 测试

- 新增 `tests/test_phase_stock_profile.py`
- 新增 `tests/test_phase_leader_rank.py`

### 我特别强调的一点

“股性画像”必须是**滚动统计产物**，不能在买入时临时现算一堆离散规则。

否则：

- 解释性差
- 回测和实盘不一致
- 运行时开销也会膨胀

---

## 5.5 Phase 4：战法路由，不再让所有股票走同一买入逻辑

### 目标

把当前“统一筛选 + 统一分数阈值买入”改成“先决定该跑哪种 playbook，再在 playbook 内排序和下单”。

### 第一阶段保留的三个 playbook

#### A. `leader_chase`

- 触发：`regime=trend` 且板块 `life_cycle=ferment`
- 目标：板块内最强前排
- 关键输入：
  - 龙头排名
  - 板块强度
  - 股性画像
  - 情绪允许追高

#### B. `divergence_reseal`

- 触发：`regime=trend` 或 `rotation`，前排核心出现可控分歧后重转强
- 关键输入：
  - 分时回封确认
  - 回封历史成功率
  - 所属板块没有同步退潮

#### C. `sector_reflow_first_board`

- 触发：`regime=rotation`，新方向启动或旧方向回流
- 关键输入：
  - 板块回流强度
  - 首板位置
  - 量能放大

### 路由器职责

- 读 `MarketProfile`
- 读 `SectorProfile`
- 读 `StockBehaviorProfile`
- 返回：
  - `playbook_name`
  - `candidate_rank`
  - `entry_window`
  - `confidence`

### 代码落点

- 新增 `src/ashare_system/strategy/router.py`
- 新增 `src/ashare_system/strategy/playbooks/`
- 现有 [buy_decision.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/buy_decision.py) 改成：
  - 不再直接按单一 `score >= threshold`
  - 改为消费 playbook 候选和 playbook 评分

### 测试

- 新增 `tests/test_phase_playbook_router.py`
- 验证不同 `regime` 下只允许对应战法

---

## 5.6 Phase 5：退出引擎重写，优先实现“失败即撤”

### 目标

把当前 ATR 通用卖出模型升级成“通用退出底座 + 战法专属退出规则”。

### 现有问题

当前 [sell_decision.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/sell_decision.py) 更适合波段或短波段，不足以处理：

- 板块退潮
- 炸板
- 回封失败
- 开仓后弱于板块的快撤

### 第一版退出优先级

1. `entry_failure_exit`
2. `board_break_exit`
3. `sector_retreat_exit`
4. `time_stop_exit`
5. `atr_trailing_exit`

### 退出引擎输入

- 持仓状态
- playbook 类型
- 分时相对强弱
- 所属板块实时状态
- 是否封板 / 是否炸板

### 代码落点

- 新增 `src/ashare_system/strategy/exit_engine.py`
- 现有 `sell_decision.py` 作为通用底层组件保留
- 新的 `ExitEngine` 调用：
  - playbook 级规则
  - 再回退 ATR 规则

### 测试

- 新增 `tests/test_phase_exit_engine.py`
- 必须覆盖：
  - 开仓后 5 分钟弱于板块立即离场
  - 龙头炸板离场
  - 板块退潮离场
  - 时间止损

---

## 5.7 Phase 6：回测和归因，不只看总收益

### 目标

回测不再只输出总收益、胜率、回撤，而是输出“哪种战法赚钱，哪种市场状态亏钱，哪种退出最有效”。

### 新增归因维度

- 按 `regime`
- 按 `sentiment_phase`
- 按 `playbook`
- 按 `sector_life_cycle`
- 按 `exit_reason`

### 关键指标

- playbook 胜率
- playbook 收益因子
- 平均持有时长
- 首 30 分钟失败率
- 板块退潮离场后的保命效果

### 代码落点

- 新增 `src/ashare_system/backtest/playbook_runner.py`
- 新增 `src/ashare_system/backtest/attribution.py`
- 扩展现有 `metrics.py`

### 测试

- 新增 `tests/test_phase_playbook_backtest.py`
- 不是只验证算得出数，而是验证能按战法拆分收益

---

## 6. 实施顺序建议

我建议按下面顺序，不要并行铺太开：

### 第一批，必须先做

1. `Phase 0` 数据契约
2. `Phase 1` 市场状态机
3. `Phase 2` 板块联动引擎
4. `Phase 4` 战法路由骨架

原因：

- 没有这几层，后面的股性和退出都挂不上去

### 第二批，高价值

1. `Phase 3` 股性画像与龙头排名
2. `Phase 5` 退出引擎

原因：

- 这两层最直接决定“能不能抓住前排”和“能不能快撤”

### 第三批，再做增强

1. `Phase 6` 归因回测
2. 封单量、炸板速度等高频增强特征
3. 机器学习排序器

原因：

- 前两批不稳，机器学习只会放大噪音

---

## 7. 开发任务拆分

## 7.1 P0 任务包：先把骨架通起来

- 扩展 `MarketProfile`
- 新增 `SectorProfile`
- 新增 `PlaybookContext`
- 实现 `sentiment/regime.py`
- 实现 `sentiment/sector_cycle.py`
- 实现 `strategy/router.py`
- 增加基础测试

### 交付标准

- API 可以给出：
  - 今日市场状态
  - 热门板块
  - 每只候选对应的战法标签

## 7.2 P1 任务包：让“前排识别”成立

- 实现 `stock_profile.py`
- 实现 `leader_rank.py`
- 增加 board behavior 因子
- 把 `leader_chase` 和 `divergence_reseal` 跑起来

### 交付标准

- 同一板块内，系统能稳定把真正前排排在前面

## 7.3 P2 任务包：让“快进快出”成立

- 实现 `exit_engine.py`
- 对接板块退潮、炸板、失败快撤
- 扩展监控和通知

### 交付标准

- 开仓失败后，不需要等 ATR 止损才能离场

## 7.4 P3 任务包：让“评估闭环”成立

- 回测归因
- 战法表现统计
- 市场状态表现统计
- 战法参数回放

### 交付标准

- 能明确回答：
  - 哪个战法在什么市场最赚钱
  - 哪种退出规则最有效
  - 哪些状态应该少做或不做

---

## 8. 不建议现在就做的事情

- 不要先上 XGBoost 做总分预测，先把 playbook 框架立起来。
- 不要先做过度复杂的“情绪 x 状态 x 板块周期”三维乘法规则表。
- 不要先依赖逐笔或盘口级特征作为主特征，先用分钟/日线版本跑通。
- 不要同时实现四五种低吸/反包/龙回头衍生战法，先把三种主战法做稳。

---

## 9. 最终建议

如果要一句话总结我的意见：

**`v1` 的战略方向正确，但实施顺序需要重排。**

正确的打法不是“把所有短线认知一次性塞进系统”，而是：

1. 先建立市场状态和板块联动这两个上游约束
2. 再建立战法路由
3. 再补股性和龙头排序
4. 最后重写退出和回测归因

这样做的好处是：

- 第一阶段就能看到系统行为变化
- 第二阶段就能明显改善“抓不住前排”的问题
- 第三阶段才能真正改善“快进快出”的效果

---

## 10. 我建议下一步直接开工的最小范围

如果现在就要进入编码，我建议首批只做这 4 件事：

1. 扩展 `MarketProfile`，新增 `regime` 和 `allowed_playbooks`
2. 做一个可运行的 `SectorProfile` 生成器
3. 做 `StrategyRouter`，先只支持 `leader_chase / divergence_reseal / sector_reflow_first_board`
4. 把 `BuyDecisionEngine` 改成“按 playbook 候选买入”，不再直接读统一总分阈值

这四步做完，系统就已经从“总分选股器”迈向“有交易语境的交易系统”了。
