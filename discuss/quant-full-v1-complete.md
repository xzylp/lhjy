# A股量化交易系统完整方案 v1
# Part 1：系统架构 + 数据契约 + Agent编排设计

> 综合 quant-strategy-plan-v1.md / codex-v2 评审 / supplement-v1 / 多Agent协议 v1
> 基于 ashare-system-v2 实际代码结构
> 日期：2026-04-06

---

## 一、全局架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                     OpenClaw Gateway (quant profile)             │
│                     port 18890 / ~/.openclaw-quant               │
└──────────────────────────┬──────────────────────────────────────┘
                           │ webchat / API
                    ┌──────▼──────┐
                    │    main     │  唯一前台入口，转发给 ashare
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │   ashare    │  量化中台主持人 + 消息总线
                    └──────┬──────┘
          ┌────────┬────────┼────────┬────────┐
          ▼        ▼        ▼        ▼        ▼
    ┌─────────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────────┐
    │ runtime │ │resea-│ │stra- │ │ risk │ │ executor │
    │         │ │ rch  │ │ tegy │ │      │ │          │
    └────┬────┘ └──────┘ └──────┘ └──────┘ └──────────┘
         │                                       ▲
         ▼                                       │
  ashare-system-v2                        最终执行候选
  FastAPI 服务层                          (1-3只，已通过讨论)
  ├── 市场状态机
  ├── 板块联动引擎
  ├── 战法路由
  ├── 因子引擎
  ├── 退出监控
  └── 回测归因
```

### 核心分工原则

- `main`：唯一对外入口，不做量化判断
- `ashare`：主持人 + 消息总线，不替代专业判断，负责分发/归档/收敛
- `ashare-runtime`：调用 FastAPI 服务，生成候选池（10-20只）
- `ashare-research`：事件/催化/情绪分析
- `ashare-strategy`：战法路由 + 候选排序
- `ashare-risk`：风控约束，拥有 LIMIT/REJECT 否决权
- `ashare-audit`：讨论质量监督，不主导排名
- `ashare-executor`：只接收最终 1-3 只，负责执行回执

---

## 二、量化决策四层漏斗（FastAPI 服务层）

```
Layer 0: 市场状态机
  输入：SentimentIndicators + LimitStats
  输出：regime + allowed_playbooks + position_ceiling
  代码：sentiment/regime.py（新建）

Layer 1: 板块联动引擎
  输入：板块分钟线行情
  输出：List[SectorProfile]（含 life_cycle）
  代码：sentiment/sector_cycle.py（新建）

Layer 2: 战法路由 + 个股排序
  输入：MarketProfile + SectorProfile + 候选股
  输出：List[PlaybookContext]（战法标签 + 置信度 + 排序）
  代码：strategy/router.py（新建）+ playbooks/（新建）

Layer 3: 退出监控
  输入：持仓 ExitContext + 实时行情 + SectorProfile
  输出：ExitSignal（5种退出场景）
  代码：strategy/exit_engine.py（新建）
```

---

## 三、contracts.py 扩展方案

在现有 `MarketProfile` 末尾追加（全部有默认值，不破坏现有调用）：

```python
# contracts.py 新增内容

from typing import Literal
from pydantic import BaseModel, Field

# ── 板块画像 ──────────────────────────────────────────────

class SectorProfile(BaseModel):
    """板块联动画像，由 SectorCycle 生成"""
    sector_name: str
    life_cycle: Literal["start", "ferment", "climax", "retreat"] = "start"
    strength_score: float = 0.0        # 综合强度 0-1
    zt_count: int = 0                  # 板块内涨停数
    up_ratio: float = 0.0             # 板块内上涨比例
    breadth_score: float = 0.0        # 广度得分
    reflow_score: float = 0.0         # 回流强度（正=流入，负=流出）
    leader_symbols: list[str] = Field(default_factory=list)
    active_days: int = 0              # 连续活跃天数
    zt_count_delta: int = 0           # 涨停数变化（正=扩散，负=退潮）


# ── 战法上下文 ────────────────────────────────────────────

PlaybookName = Literal[
    "leader_chase",
    "divergence_reseal",
    "sector_reflow_first_board",
]

class PlaybookContext(BaseModel):
    """战法路由输出，传给 BuyDecisionEngine"""
    playbook: PlaybookName
    symbol: str
    sector: str
    entry_window: str = ""             # 建议买入时间窗口，如 "09:30-10:00"
    confidence: float = 0.0           # 战法置信度 0-1
    rank_in_sector: int = 0           # 板块内排名
    exit_params: dict = Field(default_factory=dict)  # 战法专属退出参数


# ── 退出上下文 ────────────────────────────────────────────

class ExitContext(BaseModel):
    """持仓退出监控上下文"""
    symbol: str
    playbook: PlaybookName
    entry_price: float
    entry_time: str                    # "HH:MM" 格式
    holding_minutes: int = 0
    sector_name: str = ""
    is_limit_up: bool = False
    is_bomb: bool = False              # 是否炸板
    sector_retreat: bool = False       # 所属板块是否退潮
    relative_strength_5m: float = 0.0 # 相对板块5分钟超额收益


# ── MarketProfile 扩展（在现有类末尾追加字段）──────────────

# 在 MarketProfile 中新增：
#   regime: Literal["trend","rotation","defensive","chaos"] = "defensive"
#   regime_score: float = 0.0
#   allowed_playbooks: list[PlaybookName] = []
#   market_risk_flags: list[str] = []
#   sector_profiles: list[SectorProfile] = []
```

**修改方式**：在 `contracts.py` 的 `MarketProfile` 类末尾追加5个字段，不改动现有字段。

---

## 四、市场状态机设计（sentiment/regime.py）

### 4.1 输入指标（全部来自已有模块）

```python
# 来自 sentiment/indicators.py（已有）
limit_up_count        # 涨停数
limit_down_count      # 跌停数
board_fail_rate       # 炸板率
seal_rate             # 封板率
max_consecutive_up    # 最高连板数
up_down_ratio         # 上涨/下跌比

# 来自 monitor/limit_analyzer.py（已有）
prev_day_premium      # 昨日涨停今日均溢价

# 新增计算（在 regime.py 内部计算）
theme_concentration   # 前3题材涨停数/总涨停数
```

### 4.2 regime 判定规则树

```python
def classify_regime(
    zt_count: int,
    board_fail_rate: float,
    seal_rate: float,
    max_consecutive: int,
    up_down_ratio: float,
    prev_day_premium: float,
    theme_concentration: float,
) -> tuple[str, list[str], list[str]]:
    """
    返回：(regime, allowed_playbooks, risk_flags)
    偏保守原则：边界情况归入更保守的状态
    """
    risk_flags = []

    # 极端弱势 → chaos，直接空仓
    if zt_count < 15 or up_down_ratio < 0.5:
        return "chaos", [], ["涨停数不足", "市场极弱"]

    # 退潮防守
    if board_fail_rate > 0.40 and prev_day_premium < -0.02:
        risk_flags.append("高炸板率")
        risk_flags.append("昨日涨停今日低开")
        return "defensive", [], risk_flags

    # 强趋势
    if zt_count > 40 and seal_rate > 0.70 and max_consecutive >= 5:
        return "trend", ["leader_chase", "divergence_reseal"], []

    # 题材轮动
    if 20 <= zt_count <= 40 and theme_concentration < 0.60:
        return "rotation", ["sector_reflow_first_board", "divergence_reseal"], []

    # 默认保守
    if board_fail_rate > 0.30:
        risk_flags.append("炸板率偏高")
    return "defensive", [], risk_flags
```

### 4.3 与 emotion_shield.py 的对接

```python
# risk/emotion_shield.py 新增方法
def get_allowed_playbooks(self, profile: MarketProfile) -> list[str]:
    """结合 regime 和 sentiment_phase 输出最终允许战法"""
    # 情绪冰点一票否决
    if profile.sentiment_phase == "冰点":
        return []
    # 情绪高潮降级（防追高）
    if profile.sentiment_phase == "高潮" and profile.regime == "trend":
        return ["divergence_reseal"]  # 只允许分歧回封，不允许追板
    return profile.allowed_playbooks
```

---

## 五、板块联动引擎设计（sentiment/sector_cycle.py）

### 5.1 SectorCycle 核心逻辑

```python
class SectorCycle:
    """
    板块生命周期识别
    输入：板块分钟线数据（不依赖逐笔/盘口）
    输出：List[SectorProfile]
    """

    def build_profiles(self, sector_data: dict) -> list[SectorProfile]:
        profiles = []
        for sector_name, data in sector_data.items():
            profile = self._analyze_sector(sector_name, data)
            profiles.append(profile)
        # 按 strength_score 降序
        return sorted(profiles, key=lambda x: x.strength_score, reverse=True)

    def _analyze_sector(self, name: str, data: SectorData) -> SectorProfile:
        zt_count = data.zt_count
        up_ratio = data.up_count / max(data.total_count, 1)
        breadth = self._calc_breadth(data)
        reflow = self._calc_reflow_score(data)
        strength = self._calc_strength(data, up_ratio, breadth)
        lifecycle = self._classify_lifecycle(data, up_ratio, reflow)

        return SectorProfile(
            sector_name=name,
            life_cycle=lifecycle,
            strength_score=strength,
            zt_count=zt_count,
            up_ratio=up_ratio,
            breadth_score=breadth,
            reflow_score=reflow,
            leader_symbols=data.top_symbols[:3],
            active_days=data.active_days,
            zt_count_delta=data.zt_count - data.prev_zt_count,
        )

    def _classify_lifecycle(self, data, up_ratio, reflow) -> str:
        delta = data.zt_count - data.prev_zt_count

        if data.zt_count <= 1 and reflow > 0.1:
            return "start"
        if data.zt_count >= 3 and up_ratio > 0.6 and data.active_days <= 3:
            return "ferment"
        if delta < 0 and data.board_fail_rate > 0.30:
            return "climax"
        if delta < -2 and reflow < -0.1:
            return "retreat"
        return "start"
```

### 5.2 板块联动因子注册（factors/behavior/sector_linkage.py）

```python
# 注册7个板块联动因子到因子引擎

@registry.register("sector_return_rel_market", "sector", "板块相对大盘超额收益")
def sector_return_rel_market(df): ...

@registry.register("sector_up_ratio", "sector", "板块内上涨股数比例")
def sector_up_ratio(df): ...

@registry.register("sector_limit_up_count", "sector", "板块涨停数")
def sector_limit_up_count(df): ...

@registry.register("sector_front_rank_strength", "sector", "板块前排强度（龙头封单/流通市值）")
def sector_front_rank_strength(df): ...

@registry.register("sector_active_days", "sector", "板块连续活跃天数")
def sector_active_days(df): ...

@registry.register("sector_reflow_score", "sector", "板块资金回流强度")
def sector_reflow_score(df): ...

@registry.register("sector_dispersion_score", "sector", "板块内涨幅离散度")
def sector_dispersion_score(df): ...
```

---

## 六、战法路由设计（strategy/router.py）

```python
ROUTE_TABLE = {
    # (regime, sector_lifecycle) → playbook
    ("trend",    "ferment"):  "leader_chase",
    ("trend",    "climax"):   "divergence_reseal",
    ("rotation", "start"):    "sector_reflow_first_board",
    ("rotation", "ferment"):  "sector_reflow_first_board",
    ("rotation", "climax"):   "divergence_reseal",
}

class StrategyRouter:
    def route(
        self,
        profile: MarketProfile,
        sector_profiles: list[SectorProfile],
        candidates: list[str],
        stock_info: dict,
    ) -> list[PlaybookContext]:

        if not profile.allowed_playbooks:
            return []  # chaos/defensive → 空仓

        results = []
        for symbol in candidates:
            sector = self._get_sector(symbol, stock_info)
            sp = self._find_sector_profile(sector, sector_profiles)
            if sp is None:
                continue

            key = (profile.regime, sp.life_cycle)
            playbook = ROUTE_TABLE.get(key)
            if playbook not in profile.allowed_playbooks:
                continue

            ctx = PlaybookContext(
                playbook=playbook,
                symbol=symbol,
                sector=sector,
                confidence=self._calc_confidence(sp, profile),
                rank_in_sector=self._get_sector_rank(symbol, sp),
                exit_params=EXIT_PARAMS[playbook],
            )
            results.append(ctx)

        return results
```

### 各战法退出参数

```python
EXIT_PARAMS = {
    "leader_chase": {
        "max_hold_minutes": 240,   # 当日
        "open_failure_minutes": 5,
        "time_stop": "14:50",
        "win_rate": 0.58,
        "pl_ratio": 2.0,
        "atr_pct": 0.015,
    },
    "divergence_reseal": {
        "max_hold_minutes": 480,   # 可隔夜
        "open_failure_minutes": 30,
        "time_stop": "14:50",
        "win_rate": 0.55,
        "pl_ratio": 1.8,
        "atr_pct": 0.02,
    },
    "sector_reflow_first_board": {
        "max_hold_minutes": 240,
        "open_failure_minutes": 10,
        "time_stop": "14:50",
        "win_rate": 0.52,
        "pl_ratio": 1.6,
        "atr_pct": 0.02,
    },
}
```

---

## 七、个股股性画像（strategy/stock_profile.py）

```python
class StockProfileBuilder:
    """60日滚动股性画像，每日盘后更新"""

    WINDOW = 60  # 统计窗口（交易日）

    def build(self, symbol: str, history: pd.DataFrame) -> StockBehaviorProfile:
        zt_days = history[history["is_zt"] == True]

        board_success_rate = len(zt_days[zt_days["seal_success"]]) / max(len(zt_days), 1)
        bomb_rate = len(zt_days[zt_days["bombed"]]) / max(len(zt_days), 1)
        next_day_premium = zt_days["next_day_return"].mean() if len(zt_days) > 0 else 0.0
        reseal_rate = len(zt_days[zt_days["afternoon_resealed"]]) / max(len(zt_days), 1)

        # 最优持有天数：统计涨停后持有1/2/3日的最大收益
        optimal_hold = self._calc_optimal_hold(zt_days, history)

        # 风格标签
        style = self._classify_style(history)

        return StockBehaviorProfile(
            symbol=symbol,
            board_success_rate_20d=board_success_rate,
            bomb_rate_20d=bomb_rate,
            next_day_premium_20d=next_day_premium,
            reseal_rate_20d=reseal_rate,
            optimal_hold_days=optimal_hold,
            style_tag=style,
            avg_sector_rank_30d=self._avg_sector_rank(symbol, history),
            leader_frequency_30d=self._leader_frequency(symbol, history),
        )
```

---

## 八、龙头相对排名（strategy/leader_rank.py）

```python
class LeaderRanker:
    """板块内相对排名，替换绝对涨幅排名"""

    def rank(
        self,
        candidates: list[str],
        sector_data: dict,
        profiles: dict[str, StockBehaviorProfile],
    ) -> list[LeaderRankResult]:

        results = []
        for symbol in candidates:
            sector = sector_data.get(symbol, {})
            profile = profiles.get(symbol)

            score = self._calc_leader_score(symbol, sector, profile)
            results.append(LeaderRankResult(
                symbol=symbol,
                leader_score=score,
                zt_order_rank=sector.get("zt_order_rank", 99),
                is_core_leader=score > 0.7,
            ))

        return sorted(results, key=lambda x: x.leader_score, reverse=True)

    def _calc_leader_score(self, symbol, sector, profile) -> float:
        # 板块内第几个涨停（越早越强，满分1.0）
        zt_rank_score = max(0, 1 - sector.get("zt_order_rank", 10) * 0.1)
        # 封单/流通市值
        seal_score = min(sector.get("seal_ratio", 0) * 10, 1.0)
        # 带动同题材上涨数
        diffusion_score = min(sector.get("diffusion_count", 0) / 10, 1.0)
        # 历史封板率
        hist_score = profile.board_success_rate_20d if profile else 0.5
        # 流动性
        liq_score = min(sector.get("turnover_rate", 0) / 10, 1.0)

        return (
            zt_rank_score  * 0.30 +
            seal_score     * 0.25 +
            diffusion_score* 0.20 +
            hist_score     * 0.15 +
            liq_score      * 0.10
        )
```

---

## 九、退出引擎（strategy/exit_engine.py）

```python
class ExitEngine:
    """
    战法化退出引擎
    优先级：P0(立撤) > P1(快撤) > P2(减仓) > P3(时间) > P4(ATR底层)
    """

    def check(
        self,
        pos: PositionSnapshot,
        ctx: ExitContext,
        quote: QuoteSnapshot,
        sector: SectorProfile | None,
    ) -> ExitSignal | None:

        # P0: 开仓失败立撤（最高优先级，5分钟内弱于板块）
        if ctx.holding_minutes <= ctx.exit_params.get("open_failure_minutes", 5):
            if ctx.relative_strength_5m < -0.02:
                return ExitSignal(
                    symbol=ctx.symbol,
                    reason="entry_failure",
                    sell_ratio=1.0,
                    urgency="IMMEDIATE",
                )

        # P0: 炸板立撤
        if ctx.is_bomb:
            return ExitSignal(
                symbol=ctx.symbol,
                reason="board_break",
                sell_ratio=1.0,
                urgency="IMMEDIATE",
            )

        # P1: 板块退潮清仓
        if sector and sector.life_cycle == "retreat" and sector.zt_count_delta < -2:
            return ExitSignal(
                symbol=ctx.symbol,
                reason="sector_retreat",
                sell_ratio=1.0,
                urgency="URGENT",
            )

        # P2: 冲高不封减仓
        if quote.high >= quote.pre_close * 1.099 and not quote.is_limit_up:
            return ExitSignal(
                symbol=ctx.symbol,
                reason="no_seal_on_surge",
                sell_ratio=0.5,
                urgency="REDUCE_HALF",
            )

        # P3: 时间止损
        max_minutes = ctx.exit_params.get("max_hold_minutes", 240)
        if ctx.holding_minutes >= max_minutes:
            return ExitSignal(
                symbol=ctx.symbol,
                reason="time_stop",
                sell_ratio=1.0,
                urgency="CLOSE_EOD",
            )

        # P4: 回退 ATR 止损（sell_decision.py 底层）
        return self._atr_fallback(pos, quote)

    def _atr_fallback(self, pos, quote) -> ExitSignal | None:
        """回退到 SellDecisionEngine 的 ATR 逻辑"""
        from .sell_decision import SellDecisionEngine, PositionState
        engine = SellDecisionEngine()
        state = PositionState(
            symbol=pos.symbol,
            entry_price=pos.cost_price,
            atr=pos.cost_price * 0.02,
            holding_days=0,
            current_price=quote.last_price,
        )
        result = engine.evaluate(state)
        if result:
            return ExitSignal(
                symbol=pos.symbol,
                reason=result.reason.value,
                sell_ratio=result.sell_ratio,
                urgency="NORMAL",
            )
        return None
```

# A股量化交易系统完整方案 v1
# Part 2：OpenClaw Agent Team 编排 + 提示词约束 + 讨论协议落地

> 基于 docs/openclaw-quant-profile.md / multi-agent-deliberation-protocol-v1.md / openclaw-subagent-delegation-templates.md
> 结合当前 ashare-system-v2 的 discussion/ 目录与服务层骨架

---

## 一、OpenClaw Agent Team 目标形态

当前 `quant` profile 已具备的团队：

- `ashare`
- `ashare-runtime`
- `ashare-research`
- `ashare-strategy`
- `ashare-risk`
- `ashare-executor`
- `ashare-audit`

但现在更像“多角色收集意见”，还不是“多Agent协商发现决策系统”。

**第一稿目标**：把 OpenClaw agent team 固定成一个**可重复、可审计、可回放**的编排系统，明确：

1. 谁先产出候选池
2. 谁负责市场状态解释
3. 谁负责战法归类与排序
4. 谁拥有否决权
5. 谁只做质量审查
6. 哪些上下文每轮必须传
7. 哪些提示词禁止模糊表达

---

## 二、Agent Team 与 FastAPI 服务层的对应关系

| OpenClaw Agent | 主要职责 | 依赖的 ashare-system-v2 模块 |
|---------------|---------|-----------------------------|
| `ashare-runtime` | 触发候选池生成、装配 dossier/packet | `strategy/screener.py`, `sentiment/regime.py`, `sentiment/sector_cycle.py` |
| `ashare-research` | 事件催化、公告、题材叙事、舆情 | `discussion/client_brief.py`, `monitor/dragon_tiger.py`, `notify/` |
| `ashare-strategy` | playbook 匹配、相对强弱排序、入选逻辑 | `strategy/router.py`, `strategy/leader_rank.py`, `strategy/stock_profile.py` |
| `ashare-risk` | LIMIT/REJECT、仓位和执行限制 | `risk/emotion_shield.py`, `risk/guard.py`, `strategy/exit_engine.py` |
| `ashare-audit` | 检查证据链和讨论质量 | `discussion/discussion_service.py`, `discussion/state_machine.py` |
| `ashare-executor` | 只接收最终 1-3 只并下发执行 | `execution_*`, `pending_order_*`, `account_state.py` |
| `ashare` | 主持人 + 收敛器 + 状态推进 | `discussion/state_machine.py`, `discussion/discussion_service.py` |

---

## 三、Agent Team 编排总流程

### 3.1 日内主流程

```text
[盘前 08:45-09:20]
  ashare-runtime
    -> 拉前日回测归因、更新股性画像、装载今日基础上下文

[开盘后 09:30-09:40]
  ashare-runtime
    -> 调用 FastAPI：生成 base_pool（10-20只）
    -> 补齐 MarketProfile / SectorProfile / agent-packets
    -> 候选落库

[Round 1 并行]
  ashare
    -> 并行委派 research / strategy / risk / audit
    -> 独立输出 opinion JSON 数组

[Round 1 汇总]
  ashare
    -> 生成 discussion_brief / controversy_summary_lines
    -> 推进 state_machine: round_1_running -> round_1_summarized

[Round 2 仅针对争议 case]
  ashare
    -> 只对争议票委派 research / strategy / risk / audit
    -> 强制回应 challenged_points / remaining_disputes

[Finalize]
  ashare
    -> 汇总 selected / watchlist / rejected / why_selected
    -> 若 risk 阻断则 final_selection_blocked
    -> 若通过则输出 execution_candidates

[执行]
  ashare-executor
    -> 只接收 final_selection_ready 的 1-3 只票
    -> 回执 execution result / failures / risk overrides

[盘中持续]
  ashare-runtime + ashare-risk
    -> 读 ExitMonitorAgent 退出信号
    -> 必要时触发减仓/平仓建议

[盘后]
  ashare-audit + ashare-runtime
    -> 归档讨论、交易、退出、次日积分
```

### 3.2 与 discussion/state_machine.py 的状态映射

当前状态机已有：

- `round_1_running`
- `round_1_summarized`
- `round_2_running`
- `final_review_ready`
- `final_selection_ready`
- `final_selection_blocked`

第一稿不重写状态机，只做两处增强：

1. 在 `discussion/state_machine.py` 旁边新增状态解释文档，定义每个状态要求的最小产物
2. 在 `discussion/discussion_service.py` 中增加状态守卫：
   - `round_2_running` 前必须存在 `controversy_summary_lines`
   - `final_review_ready` 前必须存在 `resolved_points/unresolved_points`

建议新增文档：
- `docs/implementation-drafts/discussion-state-output-matrix.md`

内容样例：

```markdown
| State | Required artifacts |
|------|--------------------|
| round_1_running | round_1 task packets, candidate case ids |
| round_1_summarized | discussion_brief, controversy_summary_lines |
| round_2_running | round_2_target_case_ids, round_2_guidance |
| final_review_ready | resolved_points, unresolved_points, persuasion_summary |
| final_selection_ready | selected, watchlist, rejected, execution_candidates |
```

---

## 四、各 Agent 的系统提示词约束（System Prompt Constraints）

这里不是 OpenClaw 底层 prompt 全文，而是**项目内必须落地的约束清单**。建议单独沉淀到：

- `docs/openclaw-agent-constraints-v1.md`

### 4.1 `ashare` 主持人约束

**职责边界**：
- 只能做分发、归档、争议提炼、收敛
- 不得替代 `strategy` 写排序理由
- 不得替代 `risk` 解除限制
- 不得替代 `audit` 关闭证据缺口

**必须做的事**：
- Round 1 结束后生成 `controversy_summary_lines`
- Round 2 只覆盖争议票，不得全量重评
- finalize 前必须检查所有 `limit/reject` 是否有解除条件或仍未解决

**禁止语句**：
- “综合来看支持买入”但不列出谁支持谁反对
- “风控已通过”但没有 risk opinion 引用
- “审计无问题”但没有 audit 结论引用

**输出约束**：
- 中间轮次：结构化 JSON
- 用户汇总：可读 markdown，但必须保留入选/观察/淘汰三组

### 4.2 `ashare-runtime` 约束

**职责边界**：
- 负责候选输入层，不参与最终推荐
- 负责补齐 packet，不负责解释战法胜负

**必须做的事**：
- 候选池必须落库
- 每个 case 必须具备最小 packet：
  - `MarketProfile`
  - `SectorProfile`（可为空数组，但字段必须存在）
  - 基础价格/成交额/涨跌幅
  - candidate 来源标签（factor/strategy/manual/monitor）
- 若 packet 缺核心字段，不直接开会，返回 blockers

**禁止语句**：
- “这票看起来不错”
- “建议优先关注”

### 4.3 `ashare-research` 约束

**职责边界**：
- 只负责催化、事件、叙事、舆情、行业逻辑
- 不负责给出最终买卖排序

**必须做的事**：
- 每个 case 至少提供：1 条 thesis、2 条关键证据、1 条 evidence_gap
- 若催化不存在，要明确说“叙事不成立/催化不足”
- Round 2 必须说明回应了谁的质疑

**禁止语句**：
- “基本面不错”但无证据
- “题材火热”但不引用板块数据或事件源
- “建议买入”作为主结论

### 4.4 `ashare-strategy` 约束

**职责边界**：
- 负责排序、playbook 匹配、相对强弱
- 不负责解除风控限制

**必须做的事**：
- 每个 case 必须说明：
  - 属于哪个 playbook
  - 在板块内为何领先/落后
  - 若落选，输给谁、输在哪
- 必须引用 `MarketProfile.regime` 和 `SectorProfile.life_cycle`
- Round 2 必须写 `previous_stance / changed / changed_because` 或 `remaining_disputes`

**禁止语句**：
- “相对更强”但不说明相对谁
- “看图不错”这类非结构化表述
- 使用统一总分替代 playbook 内排序

### 4.5 `ashare-risk` 约束

**职责边界**：
- 只做风控与执行约束
- 不负责最终排序

**必须做的事**：
- stance 只能用 `support/watch/limit/reject`
- 如果 `limit/reject`，必须给出：
  - 可解除条件
  - 限制的是仓位、时机、流动性还是执行方式
- 必须结合 `position_ceiling`、单票仓位、板块集中度、退出可行性

**禁止语句**：
- “有风险，谨慎”
- “建议控制仓位”但不说控制到多少
- 用宏观情绪代替具体执行风险

### 4.6 `ashare-audit` 约束

**职责边界**：
- 只审讨论质量与证据链完整性
- 不主导排名，也不解除风控

**必须做的事**：
- 指出：证据缺口、逻辑断裂、未回应问题
- Round 2 必须明确哪些缺口已关闭、哪些仍未关闭
- 若 `discussion_not_ready`，必须写出具体缺什么，不允许笼统说“信息不足”

**禁止语句**：
- “建议排第一”
- “直接否决该票”
- “无问题”但不说明检查了什么

### 4.7 `ashare-executor` 约束

**职责边界**：
- 只对 `final_selection_ready` 的 execution_candidates 执行
- 不重新讨论、不重新排序

**必须做的事**：
- 回执真实执行状态：accepted/partial/failed/skipped
- 若失败，必须说明是流动性、价格限制、风控阻断还是接口失败
- 记录与 `playbook`、`decision_id` 的映射

**禁止语句**：
- “已优化执行策略”但无执行明细
- 自行替换候选票

---

## 五、子代理委派模板升级版（第一稿）

建议新建文档：
- `docs/openclaw-agent-prompts-v1.md`

该文档不替代已有 `openclaw-subagent-delegation-templates.md`，而是在其基础上补充**量化上下文字段**。

### 5.1 Round 1 通用上下文

所有 Round 1 委派必须传：

```json
{
  "trade_date": "2026-04-06",
  "round": 1,
  "case_ids": ["..."],
  "agent-packets": {"items": [...]},
  "shared_context": {
    "market_profile": {...},
    "top_sector_profiles": [...],
    "portfolio_constraints": {...},
    "position_ceiling": 0.6
  },
  "workspace_context": {...},
  "preferred_read_order": [
    "market_profile",
    "sector_profiles",
    "candidate_dossiers",
    "recent_monitor_signals"
  ]
}
```

### 5.2 Runtime Round 1 提示词补充

```text
请运行候选池生成并只返回一个 JSON 对象。

额外要求：
1. 候选必须包含 strategy 来源标签和所属 sector。
2. 输出 market_profile、top_sector_profiles 摘要。
3. 若 regime=chaos 或 allowed_playbooks 为空，仍需返回 case_ids=[] 和 blockers，不要假装有候选。
4. 不要写任何主观推荐语。
```

### 5.3 Strategy Round 1 提示词补充

```text
基于给定 trade_date、case_ids、agent-packets.items、shared_context、workspace_context，返回 ashare-strategy 的 Round 1 opinion JSON 数组。

额外要求：
1. 每个 case 必须给出 playbook_name，若无有效 playbook 必须说明淘汰原因。
2. 必须引用 regime 和 sector life_cycle。
3. 必须说明该票在所属 sector 内的相对排序依据，而不是绝对涨幅。
4. 若 stance=reject/watch，必须说明它输给了哪个候选或输在什么维度。
5. 只输出 JSON 数组。
```

### 5.4 Risk Round 1 提示词补充

```text
基于给定上下文，返回 ashare-risk 的 Round 1 opinion JSON 数组。

额外要求：
1. 必须结合 position_ceiling、playbook、单票仓位上限、sector concentration、退出可行性。
2. 若给出 limit/reject，必须写 release_conditions。
3. 若 regime=chaos 或 sentiment_phase=冰点，默认 stance 不得高于 watch，除非明确给出例外理由。
4. 只输出 JSON 数组。
```

### 5.5 Round 2 通用上下文补充

除已有字段外，再强制传：

```json
{
  "round_2_target_case_ids": ["..."],
  "controversy_summary_lines": ["..."],
  "round_2_guidance": ["..."],
  "substantive_gap_case_ids": ["..."],
  "peer_opinion_refs": {
    "ashare-research": [...],
    "ashare-strategy": [...],
    "ashare-risk": [...],
    "ashare-audit": [...]
  }
}
```

### 5.6 Round 2 Strategy 提示词补充

```text
你现在参与 Round 2 争议讨论。请仅针对 round_2_target_case_ids 返回 ashare-strategy 的 JSON opinion 数组。

你必须：
1. 明确回应 peer_opinion_refs 中谁挑战了你的排序逻辑。
2. 必须说明当前 playbook 是否成立，若不成立要改判。
3. 必须写 previous_stance / changed / changed_because 或 remaining_disputes。
4. 禁止重复使用 Round 1 的原始 reasons 作为唯一结论。
```

---

## 六、Agent Team 的最小落地配置文件建议

建议新增：

1. `docs/implementation-drafts/openclaw-agent-routing-matrix-v1.md`
2. `docs/implementation-drafts/openclaw-prompt-io-contracts-v1.md`
3. `docs/implementation-drafts/discussion-state-output-matrix.md`

### 6.1 openclaw-agent-routing-matrix-v1.md 结构

```markdown
| phase | trigger | owner | downstream agents | output |
|------|---------|-------|-------------------|--------|
| preopen_context | 08:45 | ashare-runtime | none | market context bundle |
| candidate_generation | 09:30 | ashare-runtime | none | case_ids + packets |
| round_1 | case_ids ready | ashare | research/strategy/risk/audit | round1 opinions |
| round_1_summary | round1 opinions ready | ashare | none | controversy summary |
| round_2 | needs_round_2=true | ashare | research/strategy/risk/audit | round2 opinions |
| finalize | round2 complete | ashare | audit(optional check) | final selection |
| execution | final_selection_ready | ashare-executor | none | execution receipts |
| postclose_review | 15:05 | ashare-audit/runtime | none | attribution + score |
```

### 6.2 openclaw-prompt-io-contracts-v1.md 结构

定义每个 agent 输入/输出 JSON schema，避免运行时字段漂移。

例如 `ashare-strategy`：

```json
{
  "input": {
    "case_ids": ["string"],
    "market_profile": "MarketProfile",
    "sector_profiles": ["SectorProfile"],
    "candidate_packets": ["CandidatePacket"]
  },
  "output": [
    {
      "case_id": "string",
      "playbook_name": "leader_chase|divergence_reseal|sector_reflow_first_board",
      "stance": "support|watch|reject",
      "relative_rank_reason": ["string"],
      "evidence_gaps": ["string"]
    }
  ]
}
```

---

## 七、与 discussion/ 目录的代码落点

当前已有：
- `discussion/candidate_case.py`
- `discussion/client_brief.py`
- `discussion/discussion_service.py`
- `discussion/state_machine.py`

第一稿建议补充：

| 文件 | 操作 | 作用 |
|------|------|------|
| `src/ashare_system/discussion/contracts.py` | 新建 | opinion 扩展 schema、controversy summary schema |
| `src/ashare_system/discussion/opinion_validator.py` | 新建 | 校验 Round 1/2 opinion 是否满足结构要求 |
| `src/ashare_system/discussion/round_summarizer.py` | 新建 | 从 opinions 提炼 support_points / oppose_points / gaps |
| `src/ashare_system/discussion/finalizer.py` | 新建 | 汇总 selected/watchlist/rejected |
| `src/ashare_system/discussion/discussion_service.py` | 修改 | 接入 validator / summarizer / finalizer |
| `src/ashare_system/discussion/state_machine.py` | 小改 | 增加 helper，校验状态推进前置条件 |

### 7.1 opinion schema 建议

```python
class AgentOpinion(BaseModel):
    case_id: str
    round: int
    agent_id: str
    stance: Literal["support", "watch", "limit", "reject", "question"]
    confidence: Literal["low", "medium", "high"]
    reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    thesis: str = ""
    key_evidence: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    questions_to_others: list[str] = Field(default_factory=list)
    challenged_by: list[str] = Field(default_factory=list)
    challenged_points: list[str] = Field(default_factory=list)
    previous_stance: str = ""
    changed: bool = False
    changed_because: list[str] = Field(default_factory=list)
    resolved_questions: list[str] = Field(default_factory=list)
    remaining_disputes: list[str] = Field(default_factory=list)
    playbook_name: str = ""
    release_conditions: list[str] = Field(default_factory=list)
```

### 7.2 opinion_validator.py 规则

Round 1：
- thesis 非空
- 至少2条 key_evidence
- 至少1条 evidence_gaps
- research/strategy/risk/audit 必须覆盖全部 case

Round 2：
- challenged_by/challenged_points/changed/remaining_disputes 至少满足一类
- 必须只覆盖 round_2_target_case_ids
- 不允许 reasons 与 Round 1 完全相同

---

## 八、最终用户可见输出模板

建议新增：
- `docs/implementation-drafts/client-brief-output-template-v1.md`

最终给用户/前端的 brief 必须包含：

```markdown
## 今日市场
- sentiment_phase: 主升
- regime: trend
- allowed_playbooks: leader_chase, divergence_reseal
- 风险标记: 无

## 入选（selected）
### xxx 股票
- 战法：leader_chase
- 入选原因：...
- 风险约束：...
- 审计结论：...
- 谁支持/谁保留：...

## 观察（watchlist）
### xxx 股票
- 未入选原因：...
- 若满足哪些条件可升级：...

## 淘汰（rejected）
### xxx 股票
- 淘汰原因：...
- 关键证据缺口：...

## 争议与收敛
- 谁反对过谁
- 哪些问题在 Round 2 解决了
- 哪些问题仍未解决
```

---

## 九、Agent Team 第一稿的边界

这一稿**不做**：

- 让子代理自由互聊
- 让 audit 主导排序
- 让 runtime 带主观推荐
- 让 executor 替换候选
- 让 XGBoost 直接替代 strategy 排序
- 让 Round 2 变成全量重评

这一稿**必须做到**：

- 每一轮讨论有固定输入输出
- 每个 agent 有明确权责边界
- 每个 stage 有状态机前置条件
- 每个最终结论能追溯到具体 opinion 和争议解决过程

---

## 十、建议新增/修改的文档清单

### 新增文档

- `docs/openclaw-agent-constraints-v1.md`
- `docs/openclaw-agent-prompts-v1.md`
- `docs/implementation-drafts/openclaw-agent-routing-matrix-v1.md`
- `docs/implementation-drafts/openclaw-prompt-io-contracts-v1.md`
- `docs/implementation-drafts/discussion-state-output-matrix.md`
- `docs/implementation-drafts/client-brief-output-template-v1.md`

### 修改文档

- `docs/openclaw-quant-profile.md`
  - 增补 agent team 日内执行顺序
  - 增补 ashare 与 FastAPI 服务的接口边界
- `docs/openclaw-subagent-delegation-templates.md`
  - 增补量化上下文字段：market_profile / sector_profiles / playbook_name / release_conditions
- `docs/multi-agent-deliberation-protocol-v1.md`
  - 增补 strategy/risk 对 playbook 的引用要求

# A股量化交易系统完整方案 v1
# Part 3：Task 计划 + 文件分布 + 验收标准

> 基于 Part 1（架构/契约/量化层）+ Part 2（Agent编排/提示词）
> 结合 docs/task-breakdown.md 现有 Phase 1-3 骨架
> 日期：2026-04-06

---

## 一、全局文件分布总览

### 新增文件（按模块）

```
src/ashare_system/
├── contracts.py                          ← 修改：追加5个字段 + 3个新数据类
│
├── sentiment/
│   ├── regime.py                         ← 新建：市场状态机（regime 规则树）
│   └── sector_cycle.py                   ← 新建：板块生命周期识别
│
├── factors/
│   └── behavior/
│       ├── sector_linkage.py             ← 新建：7个板块联动因子注册
│       └── board_behavior.py             ← 新建：涨停板专项因子注册
│
├── strategy/
│   ├── router.py                         ← 新建：战法路由（StrategyRouter）
│   ├── stock_profile.py                  ← 新建：60日股性画像
│   ├── leader_rank.py                    ← 新建：板块内相对排名
│   ├── exit_engine.py                    ← 新建：战法化退出引擎
│   ├── buy_decision.py                   ← 修改：消费 PlaybookContext
│   ├── screener.py                       ← 修改：_boost_hot_sectors 替换
│   └── playbooks/
│       ├── __init__.py                   ← 新建
│       ├── leader_chase.py               ← 新建：龙头追板战法
│       ├── divergence_reseal.py          ← 新建：分歧回封战法
│       └── sector_reflow.py              ← 新建：板块回流首板战法
│
├── risk/
│   └── emotion_shield.py                 ← 修改：对接 regime，增加 get_allowed_playbooks
│
├── monitor/
│   └── exit_monitor.py                   ← 新建：实时退出信号监控
│
├── discussion/
│   ├── contracts.py                      ← 新建：opinion 扩展 schema
│   ├── opinion_validator.py              ← 新建：Round 1/2 结构校验
│   ├── round_summarizer.py               ← 新建：争议点提炼
│   ├── finalizer.py                      ← 新建：selected/watchlist/rejected 汇总
│   └── discussion_service.py             ← 修改：接入 validator/summarizer/finalizer
│
└── backtest/
    ├── playbook_runner.py                ← 新建：按战法运行回测
    ├── attribution.py                    ← 新建：按 regime/playbook/exit_reason 归因
    └── metrics.py                        ← 修改：扩展分战法绩效指标

tests/
├── test_phase_market_regime.py           ← 新建
├── test_phase_sector_linkage.py          ← 新建
├── test_phase_stock_profile.py           ← 新建
├── test_phase_leader_rank.py             ← 新建
├── test_phase_playbook_router.py         ← 新建
├── test_phase_exit_engine.py             ← 新建
└── test_phase_playbook_backtest.py       ← 新建

docs/
├── openclaw-agent-constraints-v1.md      ← 新建（Part 2 内容落地）
├── openclaw-agent-prompts-v1.md          ← 新建（委派模板升级版）
└── implementation-drafts/
    ├── openclaw-agent-routing-matrix-v1.md   ← 新建
    ├── openclaw-prompt-io-contracts-v1.md    ← 新建
    ├── discussion-state-output-matrix.md     ← 新建
    └── client-brief-output-template-v1.md    ← 新建
```

### 不动的文件（明确边界）

```
src/ashare_system/
├── sell_decision.py          ← 保留，作 ATR 底层，被 exit_engine 调用
├── position_mgr.py           ← 保留，仓位计算，入参从战法读取
├── factors/engine.py         ← 保留，只新增因子注册
├── ai/xgb_scorer.py          ← 暂不训练，等 playbook 框架稳定后接入
├── backtest/engine.py        ← 保留，playbook_runner 调用它
├── monitor/market_watcher.py ← 保留，exit_monitor 作为下游
├── sentiment/cycle.py        ← 保留，regime.py 作为补充层
└── discussion/state_machine.py ← 只小改，不重写
```

---

## 二、Task 计划（按 Phase 拆分，精确到文件）

---

### Phase A：数据契约扩展

**目标**：扩展 contracts.py，为后续所有模块提供统一数据类型，不破坏现有调用。

#### Task A-1：扩展 MarketProfile + 新增数据类

- 文件：`src/ashare_system/contracts.py`
- 操作：在 `MarketProfile` 末尾追加5个字段（全部有默认值）
- 新增3个数据类：`SectorProfile`、`PlaybookContext`、`ExitContext`
- 新增类型别名：`PlaybookName`

验收标准：
- [ ] 现有所有测试通过（不破坏现有调用）
- [ ] `from ashare_system.contracts import SectorProfile, PlaybookContext, ExitContext` 可执行
- [ ] `MarketProfile()` 不传新字段时，默认值正确

---

### Phase B：市场状态机

**目标**：让系统知道"今天能不能做"，混沌状态直接空仓。

#### Task B-1：实现 sentiment/regime.py

- 文件：`src/ashare_system/sentiment/regime.py`
- 核心类：`RegimeClassifier`
- 方法：`classify(indicators) -> tuple[str, list[str], list[str]]`
- 复用：`SentimentIndicators`（已有）、`LimitStats`（已有）
- 规则树：7个指标，4种状态，偏保守判断

验收标准：
- [ ] `regime=chaos` 时 `allowed_playbooks=[]`
- [ ] `regime=trend` 时 `allowed_playbooks=["leader_chase","divergence_reseal"]`
- [ ] 边界值测试：`zt_count=15`、`board_fail_rate=0.40` 各自归入正确状态

#### Task B-2：修改 risk/emotion_shield.py

- 文件：`src/ashare_system/risk/emotion_shield.py`
- 新增方法：`get_allowed_playbooks(profile: MarketProfile) -> list[str]`
- 逻辑：情绪冰点一票否决；情绪高潮+trend 降级为只允许 divergence_reseal

验收标准：
- [ ] `sentiment_phase=冰点` 时返回 `[]`
- [ ] `sentiment_phase=高潮 + regime=trend` 时只返回 `["divergence_reseal"]`

#### Task B-3：新建 tests/test_phase_market_regime.py

覆盖：
- [ ] 4种 regime 的边界条件
- [ ] emotion_shield 与 regime 联动
- [ ] MarketProfile 新字段默认值

---

### Phase C：板块联动引擎

**目标**：识别板块生命周期，替换现有"热门板块加权15%"的简单逻辑。

#### Task C-1：实现 sentiment/sector_cycle.py

- 文件：`src/ashare_system/sentiment/sector_cycle.py`
- 核心类：`SectorCycle`
- 方法：`build_profiles(sector_data) -> list[SectorProfile]`
- 生命周期判定：start/ferment/climax/retreat 四阶段
- 输入：板块分钟线数据（不依赖逐笔）

验收标准：
- [ ] 输出按 `strength_score` 降序排列
- [ ] `zt_count_delta < -2 + reflow < 0` 时判定为 retreat
- [ ] `zt_count >= 3 + up_ratio > 0.6 + active_days <= 3` 时判定为 ferment

#### Task C-2：新建 factors/behavior/sector_linkage.py

- 文件：`src/ashare_system/factors/behavior/sector_linkage.py`
- 注册7个因子：
  - `sector_return_rel_market`
  - `sector_up_ratio`
  - `sector_limit_up_count`
  - `sector_front_rank_strength`
  - `sector_active_days`
  - `sector_reflow_score`
  - `sector_dispersion_score`

验收标准：
- [ ] 7个因子均可通过 `registry.get("sector_up_ratio")` 获取
- [ ] 因子计算不报错（可用 mock 数据）

#### Task C-3：修改 strategy/screener.py

- 文件：`src/ashare_system/strategy/screener.py`
- 改动：将 `_boost_hot_sectors` 替换为 `_rank_by_sector_profile`
- 入参：`sector_profiles: list[SectorProfile]`（从 `MarketProfile.sector_profiles` 读取）
- 生命周期权重：`ferment=1.3, start=1.1, climax=0.9, retreat=0.5`

验收标准：
- [ ] `retreat` 板块的票排名低于 `ferment` 板块的票
- [ ] 现有 screener 集成测试通过

#### Task C-4：新建 tests/test_phase_sector_linkage.py

覆盖：
- [ ] 板块生命周期判定的4种场景
- [ ] screener 排序结果与 sector lifecycle 的对应关系

---

### Phase D：个股股性画像 + 龙头排名

**目标**：用相对排名替换绝对涨幅，让龙头识别有量化依据。

#### Task D-1：实现 strategy/stock_profile.py

- 文件：`src/ashare_system/strategy/stock_profile.py`
- 核心类：`StockProfileBuilder`
- 方法：`build(symbol, history_df) -> StockBehaviorProfile`
- 统计窗口：60日滚动
- 输出字段：
  - `board_success_rate_20d`
  - `bomb_rate_20d`
  - `next_day_premium_20d`
  - `reseal_rate_20d`
  - `optimal_hold_days`
  - `style_tag`
  - `avg_sector_rank_30d`
  - `leader_frequency_30d`

验收标准：
- [ ] 历史数据不足时返回默认值，不报错
- [ ] 60日窗口滚动计算结果与手工计算一致

#### Task D-2：实现 strategy/leader_rank.py

- 文件：`src/ashare_system/strategy/leader_rank.py`
- 核心类：`LeaderRanker`
- 方法：`rank(candidates, sector_data, profiles) -> list[LeaderRankResult]`
- 排序权重（leader_chase）：
  - `zt_order_rank_in_sector * 0.30`
  - `seal_ratio * 0.25`
  - `diffusion_contribution * 0.20`
  - `hist_board_success_rate * 0.15`
  - `liquidity_score * 0.10`

验收标准：
- [ ] 板块内第1个涨停的票得分高于第3个
- [ ] 历史封板率高的票在同等条件下排名更高

#### Task D-3：新建 factors/behavior/board_behavior.py

- 文件：`src/ashare_system/factors/behavior/board_behavior.py`
- 注册5个涨停板专项因子：
  - `zt_seal_time`（首次封板时间）
  - `zt_seal_ratio`（封单/流通市值）
  - `zt_bomb_count`（当日炸板次数）
  - `zt_reseal_speed`（炸板后回封速度）
  - `zt_consecutive`（连续涨停天数）

验收标准：
- [ ] 5个因子均可注册并计算
- [ ] `zt_seal_ratio` 在无 Level2 数据时返回 NaN，不报错

#### Task D-4：新建测试

- `tests/test_phase_stock_profile.py`：股性画像滚动计算
- `tests/test_phase_leader_rank.py`：龙头相对排名

---

### Phase E：战法路由

**目标**：让系统知道"该用什么战法"，不同市场状态激活不同战法。

#### Task E-1：实现 strategy/router.py

- 文件：`src/ashare_system/strategy/router.py`
- 核心类：`StrategyRouter`
- 方法：`route(profile, sector_profiles, candidates, stock_info) -> list[PlaybookContext]`
- 路由表：`ROUTE_TABLE`（regime + lifecycle → playbook）
- 若 `allowed_playbooks=[]` 直接返回空列表

验收标准：
- [ ] `regime=chaos` 时返回 `[]`
- [ ] `regime=trend + lifecycle=ferment` 时路由到 `leader_chase`
- [ ] `regime=rotation + lifecycle=start` 时路由到 `sector_reflow_first_board`

#### Task E-2：新建 strategy/playbooks/ 目录

三个战法文件，第一稿只需骨架（定义输入输出，不需要完整实现）：

- `strategy/playbooks/__init__.py`
- `strategy/playbooks/leader_chase.py`：`LeaderChasePlaybook.score(ctx) -> float`
- `strategy/playbooks/divergence_reseal.py`：`DivergenceResealPlaybook.score(ctx) -> float`
- `strategy/playbooks/sector_reflow.py`：`SectorReflowPlaybook.score(ctx) -> float`

每个战法文件包含：
- 战法描述（docstring）
- 适用条件（regime + lifecycle）
- 排序权重定义
- `EXIT_PARAMS` 常量

验收标准：
- [ ] 三个战法可 import
- [ ] `EXIT_PARAMS` 包含 `max_hold_minutes / time_stop / win_rate / pl_ratio`

#### Task E-3：重构 strategy/buy_decision.py

- 文件：`src/ashare_system/strategy/buy_decision.py`
- 改动：`generate()` 方法改为消费 `list[PlaybookContext]`
- 移除：`score >= BUY_SCORE_THRESHOLD` 统一阈值判断
- 保留：`PositionManager` 调用，但仓位参数从 `ctx.exit_params` 读取

验收标准：
- [ ] `allowed_playbooks=[]` 时返回空列表
- [ ] 最多返回3个 `BuyCandidate`
- [ ] 现有集成测试通过

#### Task E-4：新建 tests/test_phase_playbook_router.py

覆盖：
- [ ] 6种 regime+lifecycle 组合的路由结果
- [ ] buy_decision 消费 PlaybookContext 的完整流程

---

### Phase F：退出引擎

**目标**：战法化退出替换固定止损止盈，"失败即撤"优先级最高。

#### Task F-1：实现 strategy/exit_engine.py

- 文件：`src/ashare_system/strategy/exit_engine.py`
- 核心类：`ExitEngine`
- 方法：`check(pos, ctx, quote, sector) -> ExitSignal | None`
- 5种退出场景（按优先级）：
  1. `entry_failure`（P0，开仓5分钟内弱于板块）
  2. `board_break`（P0，炸板）
  3. `sector_retreat`（P1，板块退潮）
  4. `no_seal_on_surge`（P2，冲高不封，减仓50%）
  5. `time_stop`（P3，超过战法持仓时间）
  6. ATR 底层回退（P4，调用 sell_decision.py）

验收标准：
- [ ] P0 场景优先于 P3
- [ ] `board_break=True` 时返回 `sell_ratio=1.0, urgency=IMMEDIATE`
- [ ] `no_seal_on_surge` 时返回 `sell_ratio=0.5, urgency=REDUCE_HALF`
- [ ] 无任何退出信号时返回 `None`

#### Task F-2：新建 monitor/exit_monitor.py

- 文件：`src/ashare_system/monitor/exit_monitor.py`
- 核心类：`ExitMonitor`
- 方法：`check_all(positions, quotes, sector_profiles) -> list[ExitSignal]`
- 对接：`MarketWatcher`（已有），每次 `check_once()` 后触发
- 输出：`ExitSignal` 列表，传给 `sell_decision` 执行

验收标准：
- [ ] 空持仓时返回空列表
- [ ] 多持仓时每只独立检查，互不影响

#### Task F-3：新建 tests/test_phase_exit_engine.py

覆盖：
- [ ] 5种退出场景各自触发
- [ ] 优先级顺序（P0 先于 P3）
- [ ] ATR 底层回退路径

---

### Phase G：讨论层升级

**目标**：让 Agent 讨论有结构约束，每轮有前置条件，每个 opinion 有校验。

#### Task G-1：新建 discussion/contracts.py

- 文件：`src/ashare_system/discussion/contracts.py`
- 内容：`AgentOpinion` 扩展 schema（含 Round 2 字段）
- 字段：`thesis / key_evidence / evidence_gaps / challenged_by / changed / remaining_disputes / playbook_name / release_conditions`

验收标准：
- [ ] `AgentOpinion()` 可实例化
- [ ] Round 1 必填字段：`thesis / key_evidence / evidence_gaps`
- [ ] Round 2 必填字段：`challenged_by / changed`

#### Task G-2：新建 discussion/opinion_validator.py

- 文件：`src/ashare_system/discussion/opinion_validator.py`
- 核心类：`OpinionValidator`
- 方法：
  - `validate_round1(opinions) -> ValidationResult`
  - `validate_round2(opinions, round1_opinions) -> ValidationResult`
- Round 1 规则：thesis 非空、至少2条 key_evidence、至少1条 evidence_gaps
- Round 2 规则：不允许 reasons 与 Round 1 完全相同、必须有实质回应字段

验收标准：
- [ ] 缺少 thesis 时返回 `ValidationResult(valid=False, errors=[...])`
- [ ] Round 2 原样复制 Round 1 时被拦截

#### Task G-3：新建 discussion/round_summarizer.py

- 文件：`src/ashare_system/discussion/round_summarizer.py`
- 核心类：`RoundSummarizer`
- 方法：`summarize(opinions) -> DiscussionBrief`
- 输出：`support_points / oppose_points / risk_constraints / audit_questions / evidence_gaps / needs_round_2`

验收标准：
- [ ] `risk.stance=reject` 时 `needs_round_2=True`
- [ ] `audit.evidence_gaps` 非空时 `needs_round_2=True`

#### Task G-4：新建 discussion/finalizer.py

- 文件：`src/ashare_system/discussion/finalizer.py`
- 核心类：`DiscussionFinalizer`
- 方法：`finalize(opinions, discussion_briefs) -> FinalResult`
- 输出：`selected / watchlist / rejected / execution_candidates / why_selected / why_not_selected`

验收标准：
- [ ] `risk.stance=reject` 且无 release_conditions 时不进入 selected
- [ ] `execution_candidates` 最多3只

#### Task G-5：修改 discussion/discussion_service.py

- 接入 `OpinionValidator`（每轮 opinion 落库前校验）
- 接入 `RoundSummarizer`（Round 1 结束后自动生成 controversy_summary）
- 接入 `DiscussionFinalizer`（finalize 阶段调用）
- 增加状态守卫：`round_2_running` 前必须存在 `controversy_summary_lines`

验收标准：
- [ ] 缺少 controversy_summary 时无法推进到 round_2_running
- [ ] finalize 前 risk 仍有 reject 且无 release_conditions 时返回 `final_selection_blocked`

---

### Phase H：回测归因

**目标**：能按战法、regime、退出原因拆分绩效，知道哪个战法在哪种市场状态下赚钱。

#### Task H-1：新建 backtest/playbook_runner.py

- 文件：`src/ashare_system/backtest/playbook_runner.py`
- 核心类：`PlaybookRunner`
- 方法：`run(playbook_name, date_range, universe) -> PlaybookBacktestResult`
- 依赖：`backtest/engine.py`（已有，不改）

验收标准：
- [ ] 可单独运行 `leader_chase` 战法的回测
- [ ] 输出包含每笔交易的 `playbook / regime / exit_reason`

#### Task H-2：新建 backtest/attribution.py

- 文件：`src/ashare_system/backtest/attribution.py`
- 核心类：`Attribution`
- 方法：`analyze(trades) -> AttributionReport`
- 拆分维度：`regime / playbook / exit_reason / sector_lifecycle`

验收标准：
- [ ] 可输出"leader_chase 在 trend 状态下的胜率"
- [ ] 可输出"entry_failure 退出的平均亏损"

#### Task H-3：修改 backtest/metrics.py

- 新增分战法绩效指标：
  - `win_rate_by_playbook`
  - `avg_return_by_regime`
  - `exit_reason_distribution`
  - `calmar_by_playbook`

验收标准：
- [ ] `metrics.by_playbook()` 返回各战法的独立绩效
- [ ] 现有 metrics 测试通过

---

### Phase I：文档落地

**目标**：把 Part 2 的 Agent 约束和提示词固化到文档，供 OpenClaw 配置使用。

#### Task I-1：新建 docs/openclaw-agent-constraints-v1.md

内容：7个 Agent 的职责边界、必须做的事、禁止语句（来自 Part 2 第四节）

#### Task I-2：新建 docs/openclaw-agent-prompts-v1.md

内容：Round 1/2 各 Agent 的委派提示词升级版（来自 Part 2 第五节）

#### Task I-3：新建 docs/implementation-drafts/ 下4个文档

- `openclaw-agent-routing-matrix-v1.md`：日内执行顺序矩阵
- `openclaw-prompt-io-contracts-v1.md`：每个 Agent 的 JSON schema
- `discussion-state-output-matrix.md`：状态机前置条件矩阵
- `client-brief-output-template-v1.md`：用户可见输出模板

#### Task I-4：更新现有文档

- `docs/openclaw-quant-profile.md`：增补 agent team 日内执行顺序
- `docs/openclaw-subagent-delegation-templates.md`：增补量化上下文字段
- `docs/multi-agent-deliberation-protocol-v1.md`：增补 strategy/risk 对 playbook 的引用要求

---

## 三、Task 优先级与依赖关系

```
A（契约扩展）
  └── B（状态机）
        └── C（板块联动）
              └── D（股性画像）
                    └── E（战法路由）
                          ├── F（退出引擎）
                          └── G（讨论层）
                                └── H（回测归因）

I（文档）可并行，不阻塞代码
```

**关键路径**：A → B → C → E → F

最高 ROI 的单步：**Task B-1（regime.py）**，实现后系统立刻具备"混沌空仓"能力，直接减少最大亏损来源。

---

## 四、各 Phase 验收总标准

| Phase | 核心验收 |
|-------|---------|
| A | 现有测试全通过，新数据类可 import |
| B | 4种 regime 边界测试通过，情绪冰点一票否决 |
| C | 板块生命周期4阶段判定正确，screener 排序与 lifecycle 一致 |
| D | 股性画像60日滚动计算正确，龙头相对排名优于绝对涨幅 |
| E | 6种路由组合正确，buy_decision 不再用统一 score 阈值 |
| F | 5种退出场景优先级正确，ATR 底层回退路径可用 |
| G | opinion 校验拦截无效输入，状态机前置条件守卫生效 |
| H | 可按战法/regime/退出原因拆分绩效 |
| I | 7个 Agent 约束文档完整，提示词模板覆盖 Round 1/2 |

---

## 五、第一稿不做的事（明确边界）

- 不训练 XGBoost（等 playbook 框架稳定后接入）
- 不实现 low_buy_core 战法（放二阶段）
- 不接入 Level2 逐笔数据（先用分钟线）
- 不实现 Agent 自由互聊（统一通过 ashare 中转）
- 不实现次日积分系统（等讨论层稳定后接入）
- 不重写 backtest/engine.py（只在上层新增 playbook_runner）
- 不重写 discussion/state_machine.py（只小改，增加守卫）

