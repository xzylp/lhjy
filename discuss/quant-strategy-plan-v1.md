# A股量化交易系统 — 切实可行方案 v1

> 基于 ashare-system-v2 现有骨架，结合讨论存档，制定可落地的量化策略全景方案。
> 日期：2026-04-06

---

## 一、现状诊断

当前系统已有的能力：

| 模块 | 现状 | 核心问题 |
|------|------|---------|
| 选股漏斗 | 5层过滤 + 因子/AI评分 | 线性打分，无市场状态感知 |
| 因子引擎 | 注册表 + 批量计算框架 | 因子数量少，缺板块联动类 |
| 策略层 | 动量/反转/突破/日内 | 策略孤立，无战法路由 |
| 风控 | 规则拦截 | 退出逻辑固定，非战法化 |
| AI模型 | XGBoost/LSTM框架已建 | 未训练，无实际输出 |
| 市场状态 | 仅"冰点"判断 | 缺完整状态机 |

**核心矛盾**：系统是"选股系统"，不是"交易系统"。选出来的票不知道什么时候买、用什么战法买、什么时候走。

---

## 二、目标架构：四层决策漏斗

```
┌─────────────────────────────────────────────────────┐
│  Layer 0: 市场状态机 (Market State Machine)           │
│  输出：当前状态 + 允许战法 + 仓位上限                   │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│  Layer 1: 板块联动引擎 (Sector Linkage Engine)        │
│  输出：活跃板块排名 + 扩散方向 + 轮动节奏               │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│  Layer 2: 战法路由 + 个股排序 (Strategy Router)       │
│  输出：候选股列表 + 战法标签 + 置信度                   │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│  Layer 3: 执行 + 退出监控 (Execution & Exit)          │
│  输出：下单指令 + 实时退出信号                          │
└─────────────────────────────────────────────────────┘
```

---

## 三、Layer 0：市场状态机

### 3.1 状态定义

| 状态 | 核心特征 | 仓位上限 | 允许战法 |
|------|---------|---------|---------|
| `STRONG_TREND` 强趋势 | 涨停数>40，封板率>70%，连板梯队≥5板 | 80% | 龙头追板、分歧回封 |
| `ROTATION` 题材轮动 | 涨停数20-40，板块切换明显，有新题材启动 | 60% | 回流首板、低位补涨 |
| `DEFENSIVE` 退潮防守 | 炸板率>40%，昨日连板今日大面积低开 | 20% | 仅低吸核心 |
| `CHAOS` 混沌震荡 | 涨停数<15，无主线，资金分散 | 0% | 空仓 |

### 3.2 量化指标体系

```python
# 状态识别所需的7个核心指标（每日盘后计算，次日使用）
market_state_indicators = {
    # 强度指标
    "zt_count":           "当日涨停数（剔除ST、一字板）",
    "seal_rate":          "封板成功率 = 收盘仍涨停 / 曾涨停",
    "bomb_rate":          "炸板率 = 炸板次数 / 曾涨停",
    "max_consecutive":    "连板梯队最高板数",

    # 广度指标
    "theme_concentration":"前3题材涨停数 / 总涨停数",
    "breadth":            "上涨股数 / (上涨+下跌)",

    # 延续性指标
    "yesterday_followthrough": "昨日涨停今日均涨幅",
}

# 状态判定规则树（优先级从高到低）
def classify_market_state(indicators):
    if indicators["zt_count"] < 15 or indicators["breadth"] < 0.35:
        return "CHAOS"
    if indicators["bomb_rate"] > 0.40 and indicators["yesterday_followthrough"] < -0.02:
        return "DEFENSIVE"
    if indicators["zt_count"] > 40 and indicators["seal_rate"] > 0.70:
        return "STRONG_TREND"
    if 20 <= indicators["zt_count"] <= 40 and indicators["theme_concentration"] < 0.60:
        return "ROTATION"
    return "DEFENSIVE"  # 默认保守
```

### 3.3 情绪周期叠加

在市场状态之上，叠加情绪周期（冰点→回暖→主升→高潮），两者共同决定仓位上限：

```
情绪高潮 + STRONG_TREND → 仓位上限降至60%（防止追高）
情绪冰点 + 任何状态    → 仓位上限0%（禁止买入）
情绪主升 + STRONG_TREND → 仓位上限80%（最佳做多窗口）
```

---

## 四、Layer 1：板块联动引擎

### 4.1 核心指标（替换现有"热门板块加权15%"的简单逻辑）

```python
sector_linkage_metrics = {
    # 强度
    "zt_count":          "板块内涨停数",
    "seal_rate":         "板块内封板率",
    "leader_strength":   "龙头封单量 / 龙头流通市值",

    # 广度（breadth）
    "up_ratio":          "板块内上涨股数比例",
    "diffusion_speed":   "涨停扩散速度（首板到第N板的时间）",

    # 轮动信号
    "rotation_score":    "资金从主线流入本板块的净流量",
    "relative_strength": "板块涨幅 - 同期大盘涨幅",

    # 持续性
    "consecutive_days":  "板块连续活跃天数",
    "yesterday_rank":    "昨日板块强度排名（判断是否新启动）",
}
```

### 4.2 板块生命周期识别

```
启动期：zt_count从0→3+，diffusion_speed加快，rotation_score由负转正
发酵期：zt_count持续增加，breadth扩大，龙头连板
高潮期：zt_count达峰，炸板开始出现，换手率极高
退潮期：zt_count下降，炸板率上升，资金流出
```

不同生命周期对应不同战法：
- 启动期：低吸布局，仓位轻
- 发酵期：追板龙头，仓位重
- 高潮期：减仓，只做分歧回封
- 退潮期：清仓，不参与

---

## 五、Layer 2：战法路由 + 个股排序

### 5.1 四大战法定义

**战法A：龙头追板**
- 触发：`STRONG_TREND` + 板块处于发酵期
- 目标：板块内最强承接、最高辨识度的票
- 排序逻辑：板块内涨停顺序 × 封单强度 × 带动扩散数 × 历史封板率
- 买点：涨停板封板后，封单稳定，不炸板

**战法B：分歧回封**
- 触发：`STRONG_TREND` + 昨日连板今日低开分歧
- 目标：分歧洗盘后重新封板，最高赔率点
- 排序逻辑：低开幅度（适中最佳）× 缩量程度 × 历史回封率 × 股性
- 买点：午后回封确认，封单快速堆积

**战法C：板块回流首板**
- 触发：`ROTATION` + 主线退潮，新板块启动
- 目标：新板块内第一个涨停的票
- 排序逻辑：板块内相对位置 × 前期联动历史 × 量能放大倍数
- 买点：涨停瞬间或涨停前最后一档

**战法D：低吸核心**
- 触发：`DEFENSIVE` + 核心票回调至支撑位
- 目标：辨识度最高的核心票，不做二三线
- 排序逻辑：历史低吸成功率 × 支撑位有效性 × 缩量程度
- 买点：缩量回调至均线或前期平台

### 5.2 个股股性画像（60日滚动统计）

```python
stock_profile = {
    # 涨停行为
    "board_success_rate":    "涨停封板成功率（近60日）",
    "bomb_rate":             "炸板率（近60日）",
    "next_day_premium":      "涨停次日平均溢价（近20次）",
    "afternoon_reseal_rate": "午后回封率（近20次）",

    # 持仓特征
    "optimal_hold_days":     "最优持有天数（统计最大收益对应持仓时长）",
    "style":                 "高换手激进 / 缩量加速 / 大单驱动",

    # 板块地位
    "sector_rank_history":   "近30日在所属板块内的平均排名",
    "leader_frequency":      "作为板块龙头的频率",
}
```

### 5.3 龙头识别（相对排名，非绝对涨幅）

```python
# 龙头得分 = 板块内相对强弱，不是自己涨了多少
leader_score = (
    sector_rank_by_zt_time * 0.30 +      # 板块内第几个涨停（越早越强）
    seal_ratio * 0.25 +                   # 封单/流通市值
    diffusion_contribution * 0.20 +       # 带动同题材上涨股数
    volume_rank_in_sector * 0.15 +        # 板块内换手率排名
    price_position_vs_sector * 0.10       # 相对板块均价的位置
)
```

---

## 六、Layer 3：退出逻辑（战法化）

### 6.1 四种退出场景

| 场景 | 触发条件 | 动作 | 优先级 |
|------|---------|------|--------|
| 开仓失败立撤 | 买入后5分钟内，相对板块超额收益为负且扩大 | 无条件平仓，不等止损位 | P0 |
| 炸板立撤 | 封板后炸板，封单消失速度>50%/分钟 | 立即全仓卖出 | P0 |
| 冲高不封减仓 | 冲击涨停但未封住，或封板后炸板 | 减仓50-100% | P1 |
| 板块退潮清仓 | 所属板块zt_count开始下降，breadth跌破阈值 | 不管盈亏清仓 | P1 |
| 时间止损 | 超过战法预设持仓时间窗口 | 收盘前强制平仓 | P2 |

### 6.2 各战法退出参数

```python
exit_params = {
    "LEADER_CHASE": {
        "max_hold_days": 1,          # 追板当日不走，次日开盘评估
        "open_failure_window": 5,    # 开盘5分钟判断
        "time_stop": "14:50",        # 尾盘前平仓
    },
    "DIVERGENCE_RESEAL": {
        "max_hold_days": 2,          # 回封后可持1-2日
        "reseal_confirm_time": 30,   # 回封后30分钟确认
        "time_stop": "14:50",
    },
    "SECTOR_ROTATION": {
        "max_hold_days": 1,          # 首板当日
        "sector_retreat_threshold": 0.3,  # 板块breadth跌破30%清仓
        "time_stop": "14:50",
    },
    "LOW_BUY_CORE": {
        "max_hold_days": 3,          # 低吸可持多日
        "stop_loss": -0.05,          # 跌破支撑位止损
        "time_stop": None,           # 可隔夜
    },
}
```

---

## 七、因子体系扩充方案

### 7.1 现有因子的问题

现有因子以技术指标为主（MA/MACD/RSI），缺少A股短线最有效的因子类别。

### 7.2 优先补充的因子（按ROI排序）

**第一批（P0，直接影响选股质量）：**

```python
# 涨停板专项因子
zt_factors = {
    "zt_seal_time":      "首次封板时间（越早越强）",
    "zt_seal_ratio":     "封单量 / 流通市值",
    "zt_bomb_count":     "当日炸板次数",
    "zt_reseal_speed":   "炸板后回封速度（分钟）",
    "zt_consecutive":    "连续涨停天数",
}

# 板块联动因子
sector_factors = {
    "sector_zt_count":   "所属板块涨停数",
    "sector_rank":       "板块内涨停顺序排名",
    "sector_breadth":    "板块内上涨比例",
    "sector_diffusion":  "板块扩散速度",
    "sector_lifecycle":  "板块生命周期阶段（0-3）",
}

# 资金流向因子
flow_factors = {
    "big_order_ratio":   "大单净买入 / 流通市值",
    "north_flow":        "北向资金净买入（陆股通）",
    "margin_change":     "融资余额变化率",
    "dragon_tiger":      "龙虎榜机构净买入",
}
```

**第二批（P1，提升精度）：**

```python
# 个股股性因子（60日滚动）
profile_factors = {
    "hist_board_rate":   "历史封板成功率",
    "hist_next_premium": "历史次日溢价均值",
    "hist_bomb_rate":    "历史炸板率",
    "optimal_hold":      "最优持有天数",
}

# 微观结构因子（需Level2）
microstructure_factors = {
    "bid_ask_imbalance": "买卖委托失衡度",
    "price_impact":      "单位成交量价格冲击",
    "amihud_illiquidity":"Amihud非流动性指标",
    "large_trade_ratio": "大单成交占比",
}
```

### 7.3 因子有效性验证标准

```python
factor_validity_criteria = {
    "ic_mean":    "> 0.03（绝对值）",
    "ic_ir":      "> 0.5",
    "win_rate":   "> 55%（多头分位组）",
    "decay_half": "> 5个交易日（因子半衰期）",
    "regime_stable": "在不同市场状态下IC方向一致",
}
```

---

## 八、AI模型使用策略

### 8.1 模型分工（避免重复建设）

| 模型 | 用途 | 输入 | 输出 |
|------|------|------|------|
| XGBoost | 个股次日涨跌概率 | 因子截面 | 概率分 0-1 |
| LSTM | 板块轮动预测 | 板块时序特征 | 下一活跃板块 |
| NLP情感 | 新闻/公告情绪 | 文本 | 情感分 -1~1 |
| 规则引擎 | 市场状态识别 | 市场指标 | 状态标签 |

**重要原则**：市场状态识别用规则树，不用ML模型。原因：状态识别需要可解释性和快速响应，ML模型在极端行情下容易失效。

### 8.2 XGBoost选股模型的正确使用方式

XGBoost的输出不是最终决策，而是Layer 2排序的一个输入：

```
XGBoost概率分（0.3权重）
+ 板块内相对强弱（0.3权重）
+ 个股股性画像（0.2权重）
+ 资金流向因子（0.2权重）
= 战法内综合排序分
```

不要让XGBoost直接决定买不买，它只负责在同战法候选中排序。

---

## 九、回测框架要求

### 9.1 A股特有约束（必须实现）

```python
backtest_constraints = {
    "t_plus_1":          True,   # 当日买入不能当日卖出
    "price_limit":       True,   # 涨跌停无法成交
    "market_impact":     True,   # 冲击成本（参与率>20%时显著）
    "no_lookahead":      True,   # 财报用披露日，不用报告期
    "include_delisted":  True,   # 必须包含退市股（防幸存者偏差）
    "slippage_model":    "sqrt", # 冲击成本 = k * sqrt(参与率)
    "commission":        0.0003, # 佣金万3
    "stamp_duty":        0.001,  # 印花税千1（卖出）
}
```

### 9.2 分市场状态回测

不要只看整体夏普比率，要分状态看：

```python
performance_by_state = {
    "STRONG_TREND":  {"win_rate": ?, "avg_return": ?, "sharpe": ?},
    "ROTATION":      {"win_rate": ?, "avg_return": ?, "sharpe": ?},
    "DEFENSIVE":     {"win_rate": ?, "avg_return": ?, "sharpe": ?},
}
# 如果某个状态下持续亏损，直接禁止该状态下交易
```

### 9.3 关键绩效指标

```python
kpi = {
    # 必须达标
    "annual_return":    "> 30%",
    "max_drawdown":     "< 15%",
    "calmar_ratio":     "> 2.0",
    "monthly_win_rate": "> 60%",

    # 参考指标
    "sharpe_ratio":     "> 1.5",
    "zt_capture_rate":  "涨停捕获率",
    "exit_efficiency":  "实际退出 vs 最优退出的比值",
    "strategy_win_rate_breakdown": "分战法胜率",
}
```

---

## 十、多Agent协商机制（对接现有框架）

### 10.1 Agent映射到现有模块

```
MarketStateAgent    → 新建 sentiment/market_state.py
SectorLinkageAgent  → 扩展 factors/sector_linkage.py
StockSelectionAgent → 改造 strategy/screener.py
RiskControlAgent    → 扩展 risk/ 模块
ExecutionMonitorAgent → 新建 monitor/exit_monitor.py
Orchestrator        → 改造 strategy/buy_decision.py
```

### 10.2 协商流程

```python
# 每个交易日盘中循环（每5分钟）
def decision_cycle():
    # 1. 并行获取感知
    market_state = market_state_agent.get_state()
    sector_signals = sector_agent.get_active_sectors()

    # 2. 状态机路由
    if market_state.code == "CHAOS":
        return hold_cash()

    active_strategies = market_state.allowed_strategies

    # 3. 候选筛选（现有screener改造）
    candidates = screener.run(
        strategy_filter=active_strategies,
        sector_filter=sector_signals.top_sectors
    )

    # 4. 战法内排序（替换线性打分）
    ranked = stock_selection_agent.rank_by_strategy(
        candidates, active_strategies
    )

    # 5. 风控一票否决
    approved = risk_agent.filter(ranked, portfolio)

    # 6. 退出监控（并行运行）
    exit_signals = exit_monitor.check_positions(portfolio)

    return TradeDecision(buy=approved[:3], exit=exit_signals)
```

---

## 十一、实施路线图

### Phase 1：补地基（2-3周）

优先级最高，不做这步后面全是空中楼阁。

- [ ] 市场状态机（7个指标 + 规则树）
- [ ] 板块联动引擎（替换现有热门板块加权逻辑）
- [ ] 涨停板专项因子（zt_seal_time/ratio/bomb_count等）
- [ ] 个股股性画像（60日滚动统计）

### Phase 2：改战法（1-2周）

- [ ] 四大战法定义 + 路由逻辑
- [ ] 龙头识别改为相对排名
- [ ] 退出逻辑战法化（替换固定止损止盈）
- [ ] 分战法回测验证

### Phase 3：接AI（1-2周）

- [ ] XGBoost训练（用新因子体系）
- [ ] 模型输出接入战法排序（作为权重之一，非唯一决策）
- [ ] 分市场状态的模型有效性验证

### Phase 4：完善监控（1周）

- [ ] 实时退出监控（ExecutionMonitorAgent）
- [ ] 市场状态实时更新（盘中每5分钟）
- [ ] 飞书推送接入

### Phase 5：回测闭环（持续）

- [ ] 完整回测框架（含T+1、涨跌停、冲击成本）
- [ ] 分状态绩效分析
- [ ] 参数自动优化

---

## 十二、核心设计原则

1. **空仓是最好的仓位** — 混沌状态直接空仓，不降仓
2. **退出优先于买入** — 系统80%的精力在"什么时候走"
3. **战法纯粹性** — 每个战法只在自己适合的市场状态下激活
4. **相对强弱优先** — 龙头识别用板块内排名，不用绝对涨幅
5. **可解释性** — 每笔交易必须能追溯到哪个状态、哪个战法、哪个信号
6. **数据质量 > 模型复杂度** — 先把因子做对，再上复杂模型
7. **分状态验证** — 整体夏普好看没用，要每个状态下都能赚钱

---

## 附录：与现有代码的对接点

| 新增能力 | 对接位置 | 改动类型 |
|---------|---------|---------|
| 市场状态机 | `sentiment/` 新增 `market_state.py` | 新建 |
| 板块联动引擎 | `factors/sector_linkage.py` | 新建 |
| 涨停板因子 | `factors/` 注册新因子 | 扩展 |
| 股性画像 | `factors/behavior/` | 扩展 |
| 战法路由 | `strategy/screener.py` 改造 | 重构 |
| 龙头识别 | `strategy/screener.py` `_boost_hot_sectors` 替换 | 重构 |
| 战法化退出 | `strategy/sell_decision.py` | 重构 |
| 退出监控 | `monitor/` 新增 `exit_monitor.py` | 新建 |
| XGBoost接入 | `ai/xgb_scorer.py` 已有框架，补训练逻辑 | 扩展 |
