# ashare-system-v2 量化专家复核评审报告

> 评审日期：2026-04-19  
> 本版性质：按当前代码与定向验证结果回写的复核版  
> 评审范围：因子体系、风控执行链、Agent 评分闭环、Agent-量化耦合深度、剩余上线缺口

---

## 执行摘要

### 总体结论

- 评分结论：`B+ -> A-`
- 判断依据：本轮不是“文档修饰”，而是完成了因子补齐、风控接线、因子监控、Elo 自动结算与因子正交化等一组实质性改造，系统已经从“架构可讲通”推进到“关键工程闭环已打通”。

### 本轮核心改进

1. 因子库已扩展到 `76` 个注册因子，且本轮新增 `9` 个为真实计算因子，不再是 `selection_score` 的线性变形。
2. 筹码分布与宏观环境已从“完全空白”补到可用层：
   - 筹码：`chip_profit_ratio / chip_cost_peak_distance / chip_concentration_20d / chip_turnover_rate_20d`
   - 宏观：`market_breadth_index / northbound_actual_flow / margin_balance_change / index_volatility_regime / credit_spread_macro`
3. 因子有效性监控已存在且已接线：
   - `FactorMonitor`
   - `/runtime/factor-effectiveness`
   - `/runtime/capabilities`
   - 盘后调度 `strategy.factor_monitor:refresh`
4. 风控规则已从“规则层存在”推进到“执行链已喂入真实参数”：
   - `realized_volatility`
   - `position_correlation`
   - `daily_turnover_amount`
   - `single_position_pnl_pct`
5. 因子正交化已进入 `StrategyComposer` 主链，对 4 组高相关因子做组内权重封顶，开始抑制伪分散。
6. Elo 已不再完全孤岛化：
   - discussion 终审自动做治理结算
   - `delta -> actual` 从二值映射改为连续映射
   - outcome reconcile 侧已有结果结算入口
7. `composite_multiplier` 已不是死用 `10.0`：
   - 非默认配置值直接走运行时配置
   - 默认值口径下优先走 `evaluation_ledger.estimate_composite_multiplier()`
   - 样本不足时保守回退 `conservative_fallback=4.0`

### 仍然存在的关键缺口

1. 因子体系虽已明显增强，但距“101 因子路线”的完整状态仍缺三大块：
   - 跨市场联动
   - 另类数据
   - 成长质量
2. Elo 的治理闭环已打通，但仍不是严格 pairwise 对战制。
3. `composite_multiplier` 已有估算逻辑，但缺足够长样本回测验证。
4. 当前真实 QMT 联通条件仍受环境约束，本轮结论以“代码闭环 + 定向验证”为主，不宣称已完成长样本实盘证明。

---

## 第一部分：逐项核查

### P0.1 因子计算：`✅ 已完成本轮整改目标`

#### 已确认完成

- 因子总数已到 `76`。
- 本轮新增 `9` 个真实因子，补上了此前专家指出的两个核心空白维度：
  - `chip_distribution=4`
  - `macro_environment=5`
- 新增因子已接入 `factor_registry.py` 的真实执行分支，不是 `_simple_factor_executor` 占位。
- 因子正交化元数据 `correlation_group` 已写入因子定义，并在 compose 阶段真正使用。

#### 仍需保留的边界判断

- 这不等于“101 路线已完成”。
- 当前仍未覆盖：
  - 跨市场联动因子
  - 另类数据因子
  - 成长质量因子
- 因此本项结论是：`本轮整改目标已完成，但中长期扩展路线仍未完成`。

### P0.2 评分公式：`✅ 已完成`

#### 已确认完成

- `composite_multiplier` 已不是单纯硬编码常数。
- 现行解析逻辑：
  1. 若运行时配置给出非默认值，则直接采用配置值。
  2. 若仍处默认口径，则优先调用 `evaluation_ledger.estimate_composite_multiplier()`。
  3. 若已结算样本不足 `5` 条，则回退 `conservative_fallback=4.0`。

#### 仍需保留的边界判断

- 当前解决的是“拍脑袋固定值”问题，不是“已完成充分回测标定”。
- 该系数的长期稳健性仍需更多已结算样本支持。

### P0.3 因子监控：`✅ 已完成`

#### 已确认完成

- `src/ashare_system/strategy/factor_monitor.py` 已存在并已工作。
- 已接入：
  - 运行时能力面 `/runtime/capabilities`
  - 独立接口 `/runtime/factor-effectiveness`
  - 调度任务 `strategy.factor_monitor:refresh`
- 监控输出包含：
  - `mean_rank_ic`
  - `p_value`
  - `sample_count`
  - `status`

#### 正确的边界说明

- 不是所有因子都适合做横截面滚动 IC。
- 当前以下分组会明确标记为 `unsupported_for_monitor`，这是正确降噪，不是缺失：
  - `micro_structure`
  - `event_catalyst`
  - `macro_environment`
  - `position_management`

### P0.4 风控规则：`✅ 已完成`

#### 已确认完成

- `rules.py` 的新增四条规则已经进入 `ExecutionGuard.approve()` 调用链。
- `system_api.py` 已在执行前预检中计算并传入：
  - `realized_volatility`
  - `position_correlation`
  - `daily_turnover_amount`
  - `single_position_pnl_pct`
- 因此专家上一轮指出的“参数默认 None，规则永不触发”问题，本轮已修复。

#### 仍需保留的边界判断

- 规则已接线，不代表阈值已经过长期实盘最优验证。
- 当前结论应表述为：`规则生效链已打通`，而不是“风控参数已完成最优标定”。

### P1.4 Elo 系统：`✅ 主闭环已完成，仍有增强项`

#### 已确认完成

- discussion 终审时，系统会自动基于 `final_status / risk_gate / audit_gate` 对讨论 Agent 做治理结算。
- 同一交易日使用 `discussion_finalize_v2` 做防重，避免重复入账。
- `delta -> actual` 映射已经从简单二值化改为连续映射：
  - `actual = 0.5 + 0.5 * tanh(delta)`
- 结果归因侧已有 `reconcile_outcome()` 结算入口，可把 next-day outcome 回写到 Agent score。

#### 仍需保留的边界判断

- 当前 Elo 仍不是严格的 pairwise match 机制。
- discussion 治理结算已自动化，但收益兑现侧仍以归因/对账链触发为主，不宜夸大为“全链路自动对战式学习”。

---

## 第二部分：76 因子覆盖与 101 路线差距

### 2.1 当前 14 个维度覆盖表

| 维度 | 当前因子数 | 覆盖评分 | 说明 |
| --- | ---: | --- | --- |
| 趋势动量 | 8 | ★★★★☆ | 主体完整，但组内相关性高 |
| 量价流动性 | 6 | ★★★★☆ | 已可用，仍可补历史分位化 |
| 微观结构 | 8 | ★★★★☆ | 实时盘口类较完整 |
| 板块热度 | 8 | ★★★★☆ | 热点扩散与龙头带动已具备 |
| 资金行为 | 7 | ★★★★☆ | 已有主力流、北向代理与部分真实流向 |
| 反转修复 | 8 | ★★★★☆ | 均值回归与超跌修复已较完整 |
| 事件催化 | 5 | ★★★☆☆ | 仍偏依赖事件输入质量 |
| 估值过滤 | 5 | ★★★☆☆ | 有基础估值，但不能替代成长质量 |
| 风险惩罚 | 8 | ★★★★☆ | 组合风控与个股惩罚已较扎实 |
| 持仓管理 | 4 | ★★★☆☆ | 已接主线，但组合 beta 管理仍可加强 |
| 筹码分布 | 4 | ★★★☆☆ | 本轮已从 0 补到可用 |
| 宏观环境 | 5 | ★★★☆☆ | 本轮已从 0 补到可用 |
| 跨市场联动 | 0 | ★☆☆☆☆ | 仍为空白 |
| 另类数据 | 0 | ★☆☆☆☆ | 仍为空白 |

### 2.2 关键发现

1. 技术面、微观结构、板块热度、反转修复已经从“样子货”进入“可组合使用”阶段。
2. 宏观与筹码不再是空白，本轮修复具有决定性意义。
3. 但跨市场与另类数据仍为 0，这意味着系统还不具备更高阶的全天候感知能力。
4. 成长质量目前没有独立因子簇，不能用现有估值类因子冒充。

### 2.3 本轮新增因子价值判断

#### 筹码分布类

- `chip_profit_ratio`
- `chip_cost_peak_distance`
- `chip_concentration_20d`
- `chip_turnover_rate_20d`

价值：把 A 股特有的筹码结构正式纳入主因子层，显著优于此前用波动压缩或 selection_score 派生代理筹码。

#### 宏观环境类

- `market_breadth_index`
- `northbound_actual_flow`
- `margin_balance_change`
- `index_volatility_regime`
- `credit_spread_macro`

价值：开始让系统感知市场 beta、杠杆情绪、北向增量资金与风险偏好状态。

### 2.4 仍应纳入下一轮的高优先级因子

#### P1：成长质量

- `roe_trend`
- `revenue_acceleration`
- `gross_margin_expansion`
- `analyst_revision_score`

#### P2：跨市场联动

- `hs_premium_discount`
- `commodity_sector_link`
- `us_tech_overnight`

#### P2：另类数据

- 搜索热度
- 舆情情绪
- 公告语义评分

---

## 第三部分：高相关因子正交化现状

### 已落地的正交化分组

当前已对以下 4 组高相关因子建立 `correlation_group`，并在 `StrategyComposer` 中执行组内权重封顶：

1. `trend_cluster`
2. `capital_flow_cluster`
3. `sector_heat_cluster`
4. `breakout_cluster`

### 当前处理逻辑

- 同组因子若同时被选中，不再把所有权重原样叠加。
- compose 阶段会计算组内绝对权重和，并将其缩放到“组内最大单因子权重”上限。
- 该逻辑已能抑制“以为做了多因子分散，实际在重复押注同一底层信号”的问题。

### 当前仍未做到的部分

- 尚未引入历史相关矩阵驱动的动态正交化。
- 尚未引入 PCA/主成分式降维。
- 因而当前结论是：`已完成工程级止血，不是统计学习意义上的最终正交化`。

---

## 第四部分：Agent-量化耦合闭环现状

### 当前闭环结构

当前系统已经具备如下主链雏形：

`感知层 -> 决策层 -> 执行层 -> 归因层 -> 权重更新层`

### 已经打通的部分

#### 感知层

- `FactorMonitor` 向 Agent 暴露因子有效性。
- runtime 能力面已能返回因子状态，而非只返回静态因子目录。

#### 决策层

- Agent 通过 compose 组织 playbook、factor、constraint。
- `StrategyComposer` 会处理因子正交化、学习产物加权、复合调整倍数与市场驱动加成。

#### 执行层

- `ExecutionGuard` 已基于真实风控参数拦截不合规买单。

#### 归因层

- `evaluation_ledger` 已支持 settled outcome、factor/playbook/combo 账本与复合倍数估算。

#### 权重更新层

- discussion 终审后，治理型 Agent 已自动结算 Elo。
- outcome reconcile 可继续把结果侧 delta 回写到评分状态。

### 当前仍然不足的地方

1. 感知层还不够广：
   - 缺跨市场
   - 缺另类数据
   - 缺成长质量
2. Agent 目前是“基于程序给的信息做组织”，还不是“能自主理解更广泛市场并自建新感知通道”。
3. 权重更新仍偏累计增量制，不是完整对战制。

### 结论

当前系统已经不是“单向输入模式”的初级形态，但也还没有达到“多市场感知、自适应学习、强对战反馈”的完全体。

---

## 第五部分：详细任务清单

### 已完成任务

#### P0 已完成

1. 补充筹码分布因子：已完成
2. 补充宏观环境因子：已完成
3. 因子有效性监控：已完成
4. 风控规则接入执行链：已完成
5. 修复 `composite_multiplier` 固定口径：已完成

#### P1 已完成或主体完成

1. 因子正交化第一阶段：已完成
2. Elo discussion 自动喂数：已完成
3. Elo 连续映射：已完成
4. 结果结算入口接入 score state：已完成

### 剩余任务

#### P1：1 到 2 月内应推进

1. 补成长质量因子簇
2. 给 `composite_multiplier` 做长样本回测标定
3. 强化 outcome 侧自动结算调度，减少人工触发依赖
4. 将 Elo 从累计增量制继续推进到 pairwise/相对对战制

#### P2：3 到 6 月内应推进

1. 补跨市场联动因子簇
2. 补另类数据因子簇
3. 做更强的因子相关矩阵治理或 PCA 降维

---

## 第六部分：恢复评审结论

### 代码质量评价

- 本轮改造属于真实工程补强，代码链路可追踪，测试口径明确，不属于“需求降级”或“文档式完成”。

### 上线判断

- 若以“可继续小规模实盘观察”为标准：`可以`
- 若以“大规模放量实盘”为标准：`仍需谨慎`

### 最终评价

- 架构先进性：`A-`
- 工程闭环度：`A-`
- 因子体系完整度：`B+`
- Agent 自主学习成熟度：`B+`
- 综合评级：`A-`

### 风险提示

1. 当前最主要的真实缺口已从“假因子、空风控、空 Elo”转移到“感知维度不够广、长期样本不够长”。
2. 因子体系已可用，但尚未达到跨市场/另类数据驱动的更高阶状态。
3. 真实 QMT 连通、成交反馈与长样本验证仍是后续实盘观察重点。

### 最终一句话结论

系统已经从“概念完整但量化层偏薄”进入“关键工程闭环基本打通、可以带着边界进入连续实测”的阶段；后续重点不再是补假功能，而是扩感知、做长样本、压实盘证据。
