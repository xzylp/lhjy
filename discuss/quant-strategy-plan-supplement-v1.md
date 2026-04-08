# A股量化交易系统 — 补充方案与Agent流程设计

> 基于 quant-strategy-plan-codex-v2.md 评审，结合当前项目实际结构的落地补充
> 日期：2026-04-06

---

## 一、当前项目结构现状（实际扫描）

```
src/ashare_system/
├── contracts.py              ← MarketProfile 在此，需扩展
├── sentiment/
│   ├── cycle.py              ← 情绪四阶段已实现（SentimentCycle）
│   ├── indicators.py         ← SentimentIndicators + calc_sentiment_score 已实现
│   ├── calculator.py
│   ├── position_map.py
│   └── turning_point.py
├── strategy/
│   ├── screener.py           ← 五层漏斗，热门板块加权15%（需重构）
│   ├── buy_decision.py       ← 按 score >= 60 统一阈值买入（需重构）
│   ├── sell_decision.py      ← ATR止损+移动止损+时间止损（保留作底层）
│   ├── position_mgr.py
│   ├── registry.py
│   └── [无 router.py / playbooks/ / exit_engine.py]  ← 全部缺失
├── factors/
│   ├── engine.py             ← 批量计算引擎已实现
│   ├── registry.py           ← 注册表已实现
│   ├── pipeline.py           ← 标准化pipeline已实现
│   ├── behavior/
│   │   ├── herd.py           ← CSAD羊群效应因子
│   │   └── overreaction.py   ← 过度反应反转因子
│   │   └── [无 board_behavior.py / sector_linkage.py] ← 缺失
│   └── micro/
│       ├── orderbook.py
│       └── tick_features.py
├── risk/
│   ├── emotion_shield.py     ← 情绪保护已实现（allow_chase/allow_board）
│   ├── guard.py
│   └── rules.py
├── monitor/
│   ├── market_watcher.py     ← 全天盯盘服务已实现
│   ├── limit_analyzer.py     ← 涨停分析已实现（炸板率计算）
│   ├── alert_engine.py
│   └── [无 exit_monitor.py]  ← 缺失
├── backtest/
│   ├── engine.py
│   ├── metrics.py
│   └── [无 playbook_runner.py / attribution.py] ← 缺失
└── ai/
    ├── xgb_scorer.py         ← XGBoost框架已建，未训练
    ├── lstm_trend.py
    └── nlp_sentiment.py
```

**关键发现**：
- `sentiment/indicators.py` 已有 `limit_up_count / board_fail_rate / max_consecutive_up`，是 regime 识别的直接输入，不需要重新采集
- `monitor/limit_analyzer.py` 已有 `calc_board_fail_rate()`，可直接复用
- `risk/emotion_shield.py` 已有 `allow_chase / allow_board`，regime 扩展后直接对接
- `strategy/buy_decision.py` 的 `score >= 60` 阈值是最需要改掉的地方

---

## 二、需要新增的文件清单（精确到路径）

### Phase 0：数据契约扩展

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/ashare_system/contracts.py` | **修改** | MarketProfile 新增 regime/allowed_playbooks/sector_profiles |
| `src/ashare_system/contracts.py` | **修改** | 新增 SectorProfile、PlaybookContext、ExitContext 数据类 |

### Phase 1：市场状态机

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/ashare_system/sentiment/regime.py` | **新建** | regime 识别逻辑，复用 SentimentIndicators |
| `src/ashare_system/risk/emotion_shield.py` | **修改** | 对接 regime，扩展 allowed_playbooks 输出 |
| `tests/test_phase_market_regime.py` | **新建** | 4种状态的边界测试 |

### Phase 2：板块联动引擎

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/ashare_system/sentiment/sector_cycle.py` | **新建** | 板块生命周期识别（start/ferment/climax/retreat） |
| `src/ashare_system/factors/behavior/sector_linkage.py` | **新建** | 板块联动因子注册（7个指标） |
| `tests/test_phase_sector_linkage.py` | **新建** | 板块排序和生命周期验证 |

### Phase 3：个股股性画像 + 龙头排名

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/ashare_system/strategy/stock_profile.py` | **新建** | 60日滚动股性画像（封板率/炸板率/次日溢价） |
| `src/ashare_system/strategy/leader_rank.py` | **新建** | 板块内相对排名（替换绝对涨幅） |
| `src/ashare_system/factors/behavior/board_behavior.py` | **新建** | 涨停板专项因子注册 |
| `tests/test_phase_stock_profile.py` | **新建** | 股性画像滚动计算验证 |
| `tests/test_phase_leader_rank.py` | **新建** | 龙头相对排名验证 |

### Phase 4：战法路由

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/ashare_system/strategy/router.py` | **新建** | StrategyRouter，读 MarketProfile+SectorProfile 路由战法 |
| `src/ashare_system/strategy/playbooks/__init__.py` | **新建** | playbooks 包 |
| `src/ashare_system/strategy/playbooks/leader_chase.py` | **新建** | 龙头追板战法 |
| `src/ashare_system/strategy/playbooks/divergence_reseal.py` | **新建** | 分歧回封战法 |
| `src/ashare_system/strategy/playbooks/sector_reflow.py` | **新建** | 板块回流首板战法 |
| `src/ashare_system/strategy/buy_decision.py` | **重构** | 改为消费 PlaybookContext，不再读统一 score 阈值 |
| `src/ashare_system/strategy/screener.py` | **修改** | _boost_hot_sectors 替换为 SectorProfile 排序 |
| `tests/test_phase_playbook_router.py` | **新建** | 不同 regime 下战法路由验证 |

### Phase 5：退出引擎

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/ashare_system/strategy/exit_engine.py` | **新建** | 战法化退出引擎（5种退出场景） |
| `src/ashare_system/strategy/sell_decision.py` | **保留** | 作为 ATR 底层组件，被 ExitEngine 调用 |
| `src/ashare_system/monitor/exit_monitor.py` | **新建** | 实时退出信号监控，对接 MarketWatcher |
| `tests/test_phase_exit_engine.py` | **新建** | 5种退出场景覆盖测试 |

### Phase 6：回测归因

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/ashare_system/backtest/playbook_runner.py` | **新建** | 按战法运行回测 |
| `src/ashare_system/backtest/attribution.py` | **新建** | 按 regime/playbook/exit_reason 归因 |
| `src/ashare_system/backtest/metrics.py` | **修改** | 扩展分战法绩效指标 |
| `tests/test_phase_playbook_backtest.py` | **新建** | 战法收益拆分验证 |

---

## 三、contracts.py 扩展方案（具体字段）

当前 `MarketProfile` 只有：
```python
sentiment_phase, sentiment_score, turning_signal,
macro_score, hot_sectors, risk_events, position_ceiling
```

需要新增的字段（向后兼容，全部有默认值）：

```python
# 新增到 MarketProfile
regime: Literal["trend", "rotation", "defensive", "chaos"] = "defensive"
regime_score: float = 0.0          # regime 置信度 0-1
allowed_playbooks: list[str] = []  # 当前允许的战法列表
market_risk_flags: list[str] = []  # 风险标记（如"高炸板率"）
sector_profiles: list[SectorProfile] = []  # 活跃板块列表
```

新增数据类：

```python
class SectorProfile(BaseModel):
    sector_name: str
    life_cycle: Literal["start", "ferment", "climax", "retreat"] = "start"
    strength_score: float = 0.0
    zt_count: int = 0
    up_ratio: float = 0.0
    breadth_score: float = 0.0
    reflow_score: float = 0.0
    leader_symbols: list[str] = []
    active_days: int = 0

class PlaybookContext(BaseModel):
    playbook: Literal["leader_chase", "divergence_reseal", "sector_reflow_first_board"]
    symbol: str
    sector: str
    entry_window: str = ""
    confidence: float = 0.0
    rank_in_sector: int = 0
    exit_params: dict = {}

class ExitContext(BaseModel):
    symbol: str
    playbook: str
    entry_price: float
    entry_time: str
    holding_minutes: int = 0
    sector_name: str = ""
    is_limit_up: bool = False
    is_bomb: bool = False
    sector_retreat: bool = False
    relative_strength_5m: float = 0.0
```

---

## 四、Agent流程设计（当前系统的实际调用链）

### 4.1 现有调用链（问题所在）

```
scheduler.py
  └── buy_decision.py.generate()
        ├── screener.run()           ← 五层漏斗，输出候选池
        ├── factor_scores            ← 因子引擎线性打分
        ├── ai_scores                ← XGBoost评分（未训练，输出0）
        └── score >= 60 → 买入       ← 无市场状态感知，无战法区分
```

**问题**：整条链没有"今天能不能做"的判断，没有"该做哪种战法"的路由。

### 4.2 目标调用链（改造后）

```
scheduler.py（每5分钟盘中循环）
  │
  ├── [并行] MarketStateAgent
  │     ├── 读 SentimentIndicators（已有）
  │     ├── 读 LimitAnalyzer.stats（已有）
  │     └── → 输出 MarketProfile.regime + allowed_playbooks
  │
  ├── [并行] SectorLinkageAgent
  │     ├── 读板块行情数据
  │     ├── sector_cycle.py 识别生命周期
  │     └── → 输出 List[SectorProfile]
  │
  ├── [串行，依赖上两步] StrategyRouter
  │     ├── 读 MarketProfile.regime
  │     ├── 读 SectorProfile.life_cycle
  │     ├── regime == "chaos" → 直接返回空，空仓
  │     └── → 输出 List[PlaybookContext]
  │
  ├── [串行] StockRankingAgent（战法内排序）
  │     ├── 读 StockBehaviorProfile（stock_profile.py）
  │     ├── 读 LeaderRankResult（leader_rank.py）
  │     └── → 输出排序后的 PlaybookContext 列表
  │
  ├── [串行] RiskControlAgent（一票否决）
  │     ├── 读 EmotionShield（已有）
  │     ├── 检查仓位集中度
  │     └── → 过滤后的最终候选（最多3只）
  │
  └── [并行持续] ExitMonitorAgent
        ├── 读持仓的 ExitContext
        ├── exit_engine.py 检查5种退出场景
        └── → 输出 ExitSignal，触发 sell_decision
```

### 4.3 各Agent职责与代码落点

#### MarketStateAgent
```
职责：识别 regime，输出 allowed_playbooks
输入：SentimentIndicators（sentiment/indicators.py 已有）
      LimitStats（monitor/limit_analyzer.py 已有）
输出：MarketProfile.regime, allowed_playbooks, market_risk_flags
代码：sentiment/regime.py（新建）
对接：risk/emotion_shield.py（修改，增加 regime 判断）
```

regime 判定规则树（规则树，不用ML，偏保守）：
```python
def classify_regime(ind: SentimentIndicators) -> tuple[str, list[str]]:
    # 优先判断极端状态
    if ind.limit_up_count < 15 or ind.up_down_ratio < 0.5:
        return "chaos", []

    if ind.board_fail_rate > 0.40 and ind.prev_day_premium < -0.02:
        return "defensive", []

    if (ind.limit_up_count > 40
            and ind.seal_rate > 0.70
            and ind.max_consecutive_up >= 5):
        return "trend", ["leader_chase", "divergence_reseal"]

    if 20 <= ind.limit_up_count <= 40 and ind.theme_concentration < 0.60:
        return "rotation", ["sector_reflow_first_board", "divergence_reseal"]

    return "defensive", []
```

#### SectorLinkageAgent
```
职责：识别板块生命周期，输出 SectorProfile 列表
输入：板块成分行情（分钟线）
      LimitStats.limit_up_symbols（已有）
输出：List[SectorProfile]，按 strength_score 排序
代码：sentiment/sector_cycle.py（新建）
      factors/behavior/sector_linkage.py（新建）
复用：monitor/market_watcher.py 的数据入口
```

生命周期判定逻辑：
```python
def classify_lifecycle(sector: SectorData) -> str:
    if sector.zt_count <= 1 and sector.reflow_score > 0:
        return "start"
    if sector.zt_count >= 3 and sector.up_ratio > 0.6 and sector.active_days <= 3:
        return "ferment"
    if sector.zt_count_delta < 0 and sector.board_fail_rate > 0.3:
        return "climax"
    if sector.zt_count_delta < -2 and sector.reflow_score < 0:
        return "retreat"
    return "start"
```

#### StrategyRouter
```
职责：根据 regime + sector life_cycle 路由战法
输入：MarketProfile（含 regime + allowed_playbooks）
      List[SectorProfile]
      候选股列表
输出：List[PlaybookContext]
代码：strategy/router.py（新建）
```

路由规则：
```python
ROUTE_TABLE = {
    ("trend",    "ferment"):  "leader_chase",
    ("trend",    "climax"):   "divergence_reseal",
    ("rotation", "start"):    "sector_reflow_first_board",
    ("rotation", "ferment"):  "sector_reflow_first_board",
}
# 不在表中的组合 → 空仓
```

#### StockRankingAgent
```
职责：战法内按相对强弱排序候选股
输入：PlaybookContext 列表
      StockBehaviorProfile（stock_profile.py）
      LeaderRankResult（leader_rank.py）
输出：排序后的 PlaybookContext 列表
代码：strategy/leader_rank.py（新建）
      strategy/stock_profile.py（新建）
```

leader_chase 排序权重：
```python
score = (
    zt_order_rank_in_sector    * 0.30 +  # 板块内第几个涨停（越早越强）
    seal_ratio                 * 0.25 +  # 封单/流通市值
    diffusion_contribution     * 0.20 +  # 带动同题材上涨数
    hist_board_success_rate    * 0.15 +  # 历史封板率（stock_profile）
    liquidity_score            * 0.10    # 流动性
)
```

divergence_reseal 排序权重：
```python
score = (
    hist_reseal_rate           * 0.35 +  # 历史回封成功率
    open_gap_score             * 0.25 +  # 低开幅度（适中最佳，-3%~-8%）
    volume_shrink_ratio        * 0.20 +  # 缩量程度（越缩越好）
    sector_still_strong        * 0.20    # 所属板块未退潮
)
```

sector_reflow 排序权重：
```python
score = (
    sector_relative_position   * 0.30 +  # 板块内相对位置（低位优先）
    volume_amplify_ratio       * 0.30 +  # 量能放大倍数
    prev_linkage_history       * 0.25 +  # 前期是否有过联动
    float_market_cap_score     * 0.15    # 流通市值适中（不太大不太小）
)
```

#### RiskControlAgent
```
职责：一票否决，输出最终允许买入的候选
输入：排序后的 PlaybookContext 列表
      当前持仓
      MarketProfile
输出：过滤后候选（最多3只）+ 每只的仓位比例
代码：risk/guard.py（修改，增加 playbook 感知）
      risk/emotion_shield.py（修改，对接 regime）
```

否决条件（任一触发即拒绝）：
```python
veto_conditions = [
    profile.regime == "chaos",                    # 混沌状态
    profile.sentiment_phase == "冰点",             # 情绪冰点
    portfolio.drawdown_today > 0.03,              # 当日已亏3%
    len(portfolio.positions) >= 5,                # 持仓已满
    single_sector_concentration > 0.60,           # 单板块超60%
]
```

#### ExitMonitorAgent
```
职责：实时监控持仓，触发退出信号
输入：持仓列表 + ExitContext
      实时行情（QuoteSnapshot）
      板块实时状态（SectorProfile）
输出：List[ExitSignal]
代码：strategy/exit_engine.py（新建）
      monitor/exit_monitor.py（新建）
```

退出优先级（按顺序检查，命中即返回）：
```python
def check_exit(pos: Position, ctx: ExitContext, quote: Quote, sector: SectorProfile) -> ExitSignal | None:
    # P0: 开仓失败立撤（最高优先级）
    if ctx.holding_minutes <= 5 and ctx.relative_strength_5m < -0.02:
        return ExitSignal(reason="entry_failure", urgency="IMMEDIATE")

    # P0: 炸板立撤
    if ctx.is_bomb:
        return ExitSignal(reason="board_break", urgency="IMMEDIATE")

    # P1: 板块退潮清仓
    if sector.life_cycle == "retreat" and sector.zt_count_delta < -2:
        return ExitSignal(reason="sector_retreat", urgency="URGENT")

    # P2: 冲高不封减仓
    if quote.touched_limit_up and not quote.is_limit_up:
        return ExitSignal(reason="no_seal_on_surge", urgency="REDUCE_HALF")

    # P3: 时间止损
    max_minutes = ctx.exit_params.get("max_hold_minutes", 240)
    if ctx.holding_minutes >= max_minutes:
        return ExitSignal(reason="time_stop", urgency="CLOSE_EOD")

    # P4: 回退ATR止损（sell_decision.py 底层）
    return sell_decision.check_atr_stop(pos, quote)
```

---

## 五、buy_decision.py 重构方案（精确改法）

**现在的问题**（第44-46行）：
```python
qualified = [s for s in candidates if scores.get(s, 0) >= BUY_SCORE_THRESHOLD]
qualified.sort(key=lambda s: scores.get(s, 0), reverse=True)
qualified = qualified[:top_n]
```

**改造后**：
```python
class BuyDecisionEngine:
    def generate(
        self,
        playbook_contexts: list[PlaybookContext],  # 来自 StrategyRouter
        account_equity: float,
        prices: dict[str, float],
        profile: MarketProfile,
    ) -> list[BuyCandidate]:
        # 1. regime 一票否决
        if not profile.allowed_playbooks:
            logger.info("regime=%s 无允许战法，空仓", profile.regime)
            return []

        # 2. 按战法置信度排序（不再用统一 score 阈值）
        ranked = sorted(playbook_contexts, key=lambda x: x.confidence, reverse=True)

        # 3. 按战法参数计算仓位（不再用固定 ATR 倍数）
        results = []
        for ctx in ranked[:3]:
            price = prices.get(ctx.symbol, 0)
            if price <= 0:
                continue
            inp = PositionInput(
                symbol=ctx.symbol,
                win_rate=ctx.exit_params.get("win_rate", 0.55),
                profit_loss_ratio=ctx.exit_params.get("pl_ratio", 1.6),
                atr=price * ctx.exit_params.get("atr_pct", 0.02),
                price=price,
                account_equity=account_equity,
            )
            plan = self.position_mgr.calc(inp, profile)
            results.append(BuyCandidate(
                symbol=ctx.symbol,
                score=ctx.confidence,
                position_plan=plan,
            ))
        return results
```

---

## 六、screener.py 修改方案（精确改法）

只改第162-170行的 `_boost_hot_sectors`，其余五层漏斗不动：

```python
# 改前（第162行）
@staticmethod
def _boost_hot_sectors(pool, info, hot, scores):
    def sort_key(s):
        si = info.get(s)
        base = scores.get(s, 0)
        if si and si.industry in hot:
            return base * 1.15
        return base
    return sorted(pool, key=sort_key, reverse=True)

# 改后
@staticmethod
def _rank_by_sector_profile(pool, info, sector_profiles: list, scores):
    strength = {sp.sector_name: sp.strength_score for sp in sector_profiles}
    lifecycle_mult = {"start": 1.1, "ferment": 1.3, "climax": 0.9, "retreat": 0.5}
    lifecycle = {sp.sector_name: sp.life_cycle for sp in sector_profiles}

    def sort_key(s):
        si = info.get(s)
        base = scores.get(s, 0)
        if not si:
            return base
        mult = lifecycle_mult.get(lifecycle.get(si.industry, "retreat"), 1.0)
        boost = 1 + strength.get(si.industry, 0) * 0.2
        return base * mult * boost
    return sorted(pool, key=sort_key, reverse=True)
```

同时把 `run()` 方法第91行的调用改为：
```python
# 改前
pool = self._boost_hot_sectors(pool, stock_info, profile.hot_sectors, scores)
# 改后
pool = self._rank_by_sector_profile(pool, stock_info, profile.sector_profiles, scores)
```

---

## 七、不动的部分（明确边界）

| 文件 | 原因 |
|------|------|
| `sell_decision.py` | 保留作 ATR 底层，被 ExitEngine 调用 |
| `position_mgr.py` | 仓位计算逻辑保留，入参从战法读取 |
| `factors/engine.py` | 因子引擎框架不动，只新增因子注册 |
| `ai/xgb_scorer.py` | 暂不训练，等 playbook 框架稳定后再接入 |
| `backtest/engine.py` | 底层回测引擎不动，新增 playbook_runner 调用它 |
| `monitor/market_watcher.py` | 不动，exit_monitor 作为它的下游 |
| `sentiment/cycle.py` | 不动，regime.py 作为它的补充层 |

---

## 八、实施步骤（8步，每步有明确交付物）

| 步骤 | 内容 | 交付物 | 预估 |
|------|------|--------|------|
| Step 1 | 扩展 contracts.py | 新字段+新数据类，现有测试全通过 | 1天 |
| Step 2 | 实现 sentiment/regime.py | regime 规则树 + emotion_shield 对接 + 测试 | 1天 |
| Step 3 | 实现 sector_cycle.py + sector_linkage.py | SectorProfile 生成器 + 因子注册 + 测试 | 2天 |
| Step 4 | 实现 strategy/router.py + playbooks/ 骨架 | StrategyRouter + 3个战法骨架 + 测试 | 1天 |
| Step 5 | 实现 stock_profile.py + leader_rank.py | 60日滚动画像 + 相对排名 + 测试 | 2天 |
| Step 6 | 重构 buy_decision.py + 修改 screener.py | 消费 PlaybookContext，集成测试通过 | 1天 |
| Step 7 | 实现 exit_engine.py + exit_monitor.py | 5种退出场景 + 实时监控 + 测试 | 2天 |
| Step 8 | 实现 playbook_runner.py + attribution.py | 分战法回测 + 归因报告 | 2天 |

---

## 九、与 codex-v2 评审的差异说明

| codex-v2 建议 | 本方案处理 |
|--------------|-----------|
| 先做"稳态版"市场状态机 | 采纳，规则树不用ML，偏保守判断 |
| 板块联动先用日线/分钟线，不依赖逐笔 | 采纳，sector_linkage.py 只用分钟线 |
| 先做3个战法，low_buy_core 放二阶段 | 采纳，playbooks/ 只建3个 |
| 退出优先"失败即撤"，再做止盈优化 | 采纳，exit_engine 5种场景按优先级排 |
| 不要先上 XGBoost 总分预测 | 采纳，xgb_scorer 暂不接入主链路 |
| 不破坏现有 MarketProfile 使用方式 | 采纳，所有新字段有默认值 |

**本方案额外补充（codex-v2 未覆盖）**：
- 明确了每个 Agent 的代码落点和复用现有模块的方式
- 给出了 buy_decision.py 和 screener.py 的具体改法（精确到代码行）
- 明确了哪些文件不动，避免改造范围蔓延
- 给出了8步可执行的实施顺序，每步有明确交付物
- 补充了各战法的排序权重公式
- 补充了 regime 判定规则树的完整代码
