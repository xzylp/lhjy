# 核心算法规格

> 本文档是 [technical-manual.md](technical-manual.md) 的补充，详细描述 v2 核心算法的规格和参数。

---

## 一、评分模型升级路线

### 1.1 v1 问题

```python
# v1: 基准分50 + 简单加法，量纲不统一
total = 50.0 + event_score + tech_score + sector_score + leader_score
```

- 基准分 50 → 天然偏向买入
- 各维度量纲不统一 (event ±30, sector ±15)
- 无法捕捉因子交互

### 1.2 v2 升级路线

```
Phase 1: 加权评分 + 因子标准化 (权重通过回测优化)
Phase 2: XGBoost 模型 (自动学习因子权重和交互)
Phase 3: 多模型集成 (XGBoost + Transformer + 规则)
```

### 1.3 Phase 1 加权评分规格

```python
# 所有因子先标准化到 [0, 1]
normalized = pipeline.winsorize(raw, 0.01, 0.99)
normalized = pipeline.zscore(normalized)
normalized = pipeline.neutralize(normalized, industry)

# 加权几何平均
score = product(factor_i ** weight_i) for i in active_factors
# 基准分 = 0，无偏置
# 权重通过回测 IC 加权确定
```

### 1.4 Phase 2 XGBoost 规格

| 参数 | 值 |
|------|-----|
| 目标 | AUC > 0.84 |
| 特征 | Top-N 因子 (IC > 0.03) |
| 标签 | T+3 收益率 > 0 |
| 训练集 | 滚动 12 个月 |
| 验证集 | 最近 3 个月 |
| 更新频率 | 每周末重训练 |

---

## 二、因子标准化 Pipeline

### 2.1 三阶段处理

```
原始因子值 → [去极值] → [标准化] → [中性化] → 干净因子值
```

### 2.2 去极值 (Winsorize)

```python
def winsorize(series: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
    """将 1% 和 99% 分位以外的值截断到边界"""
    lo = series.quantile(lower)
    hi = series.quantile(upper)
    return series.clip(lo, hi)
```

### 2.3 Z-Score 标准化

```python
def zscore(series: pd.Series) -> pd.Series:
    """转换为标准正态分布 (均值0, 标准差1)"""
    return (series - series.mean()) / series.std()
```

### 2.4 行业中性化

```python
def neutralize(factor: pd.Series, industry: pd.Series) -> pd.Series:
    """回归去除行业效应，只保留个股 alpha"""
    dummies = pd.get_dummies(industry)
    residuals = factor - dummies @ (dummies.T @ factor) / dummies.sum()
    return residuals
```

### 2.5 因子有效性验证

| 指标 | 含义 | 阈值 |
|------|------|------|
| IC (信息系数) | 因子值与未来收益的相关性 | > 0.03 |
| IR (信息比率) | IC 的均值/标准差 | > 0.5 |
| IC 衰减 | 近期 IC 相对历史 IC 的变化 | 衰减 > 50% 则剔除 |

---

## 三、凯利公式仓位管理

### 3.1 基础公式

```python
# 凯利公式: f* = (p * b - q) / b
# p = 胜率, q = 败率 (1-p), b = 赔率 (平均盈利/平均亏损)
kelly_fraction = (win_rate * profit_loss_ratio - (1 - win_rate)) / profit_loss_ratio

# 实际使用半凯利 (降低波动)
half_kelly = kelly_fraction / 2
```

### 3.2 多因子调整

```python
# 基础仓位
base_position = half_kelly * account_equity

# ATR 波动率调整
volatility_adj = target_risk / (atr_14 / price)

# 情绪周期系数
emotion_coeff = sentiment_phase_ceiling  # 冰点0.2/回暖0.6/主升0.8/高潮0.3

# 相关性惩罚 (持仓间相关性越高，新仓位越小)
correlation_adj = 1 - portfolio_correlation_penalty

# 最终仓位
final_position = min(base_position, volatility_adj) * emotion_coeff * correlation_adj
```

### 3.3 仓位上限约束

| 约束 | 值 |
|------|-----|
| 单票最大仓位 | 25% |
| 情绪冰点总仓位 | 20% |
| 情绪回暖总仓位 | 60% |
| 情绪主升总仓位 | 80% |
| 情绪高潮总仓位 | 30% |

---

## 四、ATR 自适应止损止盈

### 4.1 ATR 计算 (Wilder's)

```python
def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's ATR (指数加权平均)"""
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, min_periods=period).mean()
```

### 4.2 三层止损机制

**第一层：初始止损 (波动率自适应)**

```python
initial_stop = entry_price - 2 * atr_14
# 高波动股止损更宽，低波动股止损更紧
```

**第二层：盈利保护止损 (移动止损)**

```python
if unrealized_profit > 1 * atr:
    stop = entry_price                    # 保本
if unrealized_profit > 2 * atr:
    stop = entry_price + 1 * atr          # 锁定 1ATR 利润
# 持续跟随，最大只回撤 1.5 * ATR
trailing_stop = max(trailing_stop, current_price - 1.5 * atr)
```

**第三层：时间止损**

```python
if holding_days >= 3 and unrealized_profit <= 0:
    trigger_sell_evaluation()  # 避免资金长期被套在横盘股
```

### 4.3 分批止盈

```python
# 第一目标: +3 ATR → 减仓 50%
if current_price >= entry_price + 3 * atr:
    sell(position * 0.5)

# 第二目标: +5 ATR → 减仓 30%
if current_price >= entry_price + 5 * atr:
    sell(position * 0.3)

# 剩余 20%: 移动止损跟踪
```

---

## 五、情绪四阶段模型

### 5.1 情绪量化指标

| 指标 | 计算方式 | 权重 |
|------|----------|------|
| 涨停家数 | 当日涨停股票数 | 0.25 |
| 跌停家数 | 当日跌停股票数 (负向) | 0.20 |
| 炸板率 | 曾涨停但未封住 / 曾涨停总数 | 0.15 |
| 连板高度 | 最高连板天数 | 0.15 |
| 涨跌比 | 上涨家数 / 下跌家数 | 0.15 |
| 两市成交额 | 沪深总成交额 (亿) | 0.10 |

### 5.2 阶段判定规则

```python
def determine_phase(score: float) -> str:
    """基于综合情绪得分判定阶段"""
    if score < 25:
        return "冰点"      # 极度悲观
    elif score < 50:
        return "回暖"      # 底部回升
    elif score < 75:
        return "主升"      # 赚钱效应扩散
    else:
        return "高潮"      # 过热，风险累积
```

### 5.3 拐点识别信号

| 转换 | 信号 |
|------|------|
| 冰点→回暖 | 连续 2 日涨停家数 > 30 且跌停 < 5 |
| 回暖→主升 | 连板高度 ≥ 5 且炸板率 < 30% |
| 主升→高潮 | 涨停家数 > 100 或成交额 > 1.5万亿 |
| 高潮→冰点 | 跌停家数 > 涨停家数 × 3 或连板断裂 |

### 5.4 阶段→操作映射

| 阶段 | 仓位上限 | 允许操作 | 禁止操作 |
|------|----------|----------|----------|
| 冰点 | 20% | 低吸超跌 | 追高、打板 |
| 回暖 | 60% | 低吸 + 半仓试错 | 满仓 |
| 主升 | 80% | 追强 + 打板 + 加仓 | — |
| 高潮 | 30% | 减仓 + 兑现利润 | 追高、加仓 |

---

## 六、数据清洗 Pipeline

### 6.1 阶段 1: 过滤 (剔除不可用数据)

```python
filters = [
    lambda df: df[df["volume"] > 0],                    # 停牌股
    lambda df: df[df["list_days"] >= 20],                # 次新股
    lambda df: df[~df["name"].str.contains("ST|退")],    # ST/退市
    lambda df: df[df["kline_count"] >= 60],              # K线不足
]
```

### 6.2 阶段 2: 修复 (处理数据缺陷)

```python
# 除权除息: 使用前复权价格
prices = adjust_price(prices, method="qfq")

# 缺失值: 前值填充，超过连续 3 个缺失标记不可用
filled = series.ffill(limit=3)
unavailable = filled.isna()

# 异常值: 涨跌幅超过 ±11% 的非涨跌停日
anomaly = (abs(change_pct) > 11) & (~is_limit)
```

### 6.3 阶段 3: 标准化

```python
# 与因子标准化 pipeline 共用
cleaned = winsorize(filled, 0.01, 0.99)
cleaned = zscore(cleaned)
cleaned = neutralize(cleaned, industry)
```

---

## 七、v1 致命问题修复对照

| # | v1 问题 | v2 修复方案 | 实现位置 |
|---|---------|------------|----------|
| 1 | AkShare 失败注入假数据 | 返回空列表 + 质量标记 | `data/fetcher.py` |
| 2 | 评分基准分 50 偏向买入 | 基准分 0 + 因子标准化 | `strategy/buy_decision.py` |
| 3 | 宏观过滤形同虚设 | 涨跌停比+大盘涨跌+成交额 | `strategy/screener.py` |
| 4 | NLP 仅 8 关键词 | 200+ 词典→FinBERT→LLM | `ai/nlp_sentiment.py` |
| 5 | 固定 5%/8% 止损止盈 | ATR 自适应 + 移动止损 | `strategy/sell_decision.py` |
| 6 | 固定 20% 仓位 | 凯利公式 × ATR × 情绪 | `strategy/position_mgr.py` |
| 7 | RSI 用 Cutler's | 改用 Wilder's 指数平滑 | `factors/base/technical.py` |
| 8 | 板块热度仅采样 10 只 | 20-30 只 + 多日 + 资金流 | `strategy/screener.py` |
