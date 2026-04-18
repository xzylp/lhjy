"""公共数据契约 — 全系统共享的 Pydantic 模型"""

from typing import Any, Literal

from pydantic import BaseModel, Field


# ── 交易相关 ──────────────────────────────────────────────

class BalanceSnapshot(BaseModel):
    account_id: str
    total_asset: float
    cash: float
    frozen_cash: float = 0.0


class PositionSnapshot(BaseModel):
    account_id: str
    symbol: str
    quantity: int
    available: int
    cost_price: float
    last_price: float


class OrderSnapshot(BaseModel):
    order_id: str
    account_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int
    price: float
    status: Literal["PENDING", "ACCEPTED", "PARTIAL_FILLED", "CANCEL_REQUESTED", "FILLED", "CANCELLED", "REJECTED", "UNKNOWN"]


class TradeSnapshot(BaseModel):
    trade_id: str
    order_id: str
    account_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int
    price: float


class PlaceOrderRequest(BaseModel):
    account_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)
    request_id: str
    decision_id: str | None = None
    trade_date: str | None = None
    playbook: str | None = None
    regime: str | None = None
    exit_reason: str | None = None


class CancelOrderRequest(BaseModel):
    account_id: str
    order_id: str
    request_id: str


class ExecutionIntentPacket(BaseModel):
    intent_scope: str = "execution_intent_packet"
    intent_id: str
    intent_version: str = "v1"
    generated_at: str
    trade_date: str
    account_id: str = ""
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    price: float | None = None
    order_type: str = "limit"
    time_in_force: str = "day"
    run_mode: str = "paper"
    execution_plane: str = "windows_gateway"
    approval_source: str = ""
    approved_by: str = ""
    approved_at: str = ""
    idempotency_key: str
    live_execution_allowed: bool = False
    offline_only: bool = False
    status: Literal[
        "approved",
        "claimed",
        "submitted",
        "partial_filled",
        "filled",
        "canceled",
        "rejected",
        "failed",
        "expired",
    ] = "approved"
    request: dict[str, Any] = Field(default_factory=dict)
    strategy_context: dict[str, Any] = Field(default_factory=dict)
    risk_context: dict[str, Any] = Field(default_factory=dict)
    discussion_context: dict[str, Any] = Field(default_factory=dict)
    claim: dict[str, Any] = Field(default_factory=dict)
    summary_lines: list[str] = Field(default_factory=list)


class ExecutionGatewayClaimInput(BaseModel):
    intent_id: str
    gateway_source_id: str
    deployment_role: str = ""
    bridge_path: str = ""
    claimed_at: str


class ExecutionGatewayReceiptInput(BaseModel):
    receipt_id: str
    intent_id: str
    intent_version: str = "v1"
    gateway_source_id: str
    deployment_role: str = ""
    bridge_path: str = ""
    reported_at: str
    submitted_at: str = ""
    status: Literal["submitted", "partial_filled", "filled", "canceled", "rejected", "failed"]
    broker_order_id: str = ""
    broker_session_id: str = ""
    exchange_order_id: str = ""
    error_code: str = ""
    error_message: str = ""
    order: dict[str, Any] = Field(default_factory=dict)
    fills: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: float | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    summary_lines: list[str] = Field(default_factory=list)


# ── 行情相关 ──────────────────────────────────────────────

class QuoteSnapshot(BaseModel):
    symbol: str
    name: str = ""
    last_price: float
    bid_price: float
    ask_price: float
    volume: float
    pre_close: float = 0.0


class BarSnapshot(BaseModel):
    symbol: str
    period: Literal["1m", "5m", "15m", "60m", "1d"]
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    trade_time: str
    pre_close: float = 0.0  # 前收盘价，用于计算涨跌幅


# ── 数据质量 ──────────────────────────────────────────────

class DataQuality(BaseModel):
    source: Literal["real", "cached", "unavailable"]
    completeness: float = 1.0
    freshness_minutes: int = 0
    issues: list[str] = Field(default_factory=list)


# ── 因子相关 ──────────────────────────────────────────────

class FactorValue(BaseModel):
    symbol: str
    factor_name: str
    value: float
    quality: DataQuality | None = None


class FactorValidation(BaseModel):
    factor_name: str
    ic: float = 0.0
    ir: float = 0.0
    is_valid: bool = False


# ── 情绪周期 ──────────────────────────────────────────────

SentimentPhase = Literal["冰点", "回暖", "主升", "高潮"]
RegimeName = Literal["trend", "rotation", "defensive", "chaos"]
SectorLifeCycle = Literal["start", "ferment", "climax", "retreat"]
PlaybookName = Literal[
    "leader_chase",
    "divergence_reseal",
    "sector_reflow_first_board",
]
StyleTag = Literal["leader", "reseal", "momentum", "defensive", "mixed"]
ExitUrgency = Literal["IMMEDIATE", "HIGH", "NORMAL"]


class SectorProfile(BaseModel):
    """板块联动画像，由 SectorCycle 生成"""

    sector_name: str
    life_cycle: SectorLifeCycle = "start"
    strength_score: float = 0.0
    zt_count: int = 0
    up_ratio: float = 0.0
    breadth_score: float = 0.0
    reflow_score: float = 0.0
    leader_symbols: list[str] = Field(default_factory=list)
    active_days: int = 0
    zt_count_delta: int = 0


class MarketProfile(BaseModel):
    sentiment_phase: SentimentPhase
    sentiment_score: float = 0.0
    turning_signal: bool = False
    macro_score: float = 0.0
    hot_sectors: list[str] = Field(default_factory=list)
    risk_events: list[str] = Field(default_factory=list)
    position_ceiling: float = 0.6
    regime: RegimeName = "defensive"
    regime_score: float = 0.0
    allowed_playbooks: list[PlaybookName] = Field(default_factory=list)
    market_risk_flags: list[str] = Field(default_factory=list)
    sector_profiles: list[SectorProfile] = Field(default_factory=list)


# ── 策略信号 ──────────────────────────────────────────────

class Signal(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL", "HOLD"]
    strength: float = 0.0
    confidence: float = 0.0
    source_strategy: str = ""


class PositionPlan(BaseModel):
    symbol: str
    target_shares: int = 0
    target_value: float = 0.0
    kelly_fraction: float = 0.0
    atr_adjusted: float = 0.0
    emotion_ceiling: float = 0.8
    final_ratio: float = 0.0


class PlaybookMatchScore(BaseModel):
    """战法匹配分契约，供 runtime / buy_decision / discussion 统一消费。"""

    playbook: PlaybookName
    symbol: str
    qualified: bool = False
    score: float = 0.0
    reason: str = ""
    bull_evidence: list[str] = Field(default_factory=list)
    bear_evidence: list[str] = Field(default_factory=list)


class BullCase(BaseModel):
    """多头证据最小契约。"""

    thesis: str = ""
    key_facts: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)


class BearCase(BaseModel):
    """空头证据最小契约。"""

    thesis: str = ""
    key_risks: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)


class UncertaintyProfile(BaseModel):
    """不确定性画像最小契约。"""

    thesis: str = ""
    key_unknowns: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)


class CaseEvidence(BaseModel):
    """discussion 可直接消费的最小证据包。"""

    symbol: str
    playbook: str = ""
    reason: str = ""
    bull_case: BullCase = Field(default_factory=BullCase)
    bear_case: BearCase = Field(default_factory=BearCase)
    uncertainty: UncertaintyProfile = Field(default_factory=UncertaintyProfile)


class Contradiction(BaseModel):
    """单个 case 内的最小矛盾对象。"""

    case_id: str
    between: list[str] = Field(default_factory=list)
    type: str = ""
    question: str = ""
    must_resolve_before_round_2: bool = True
    evidence_refs: list[str] = Field(default_factory=list)


class CaseContradictionSummary(BaseModel):
    """单个 case 的矛盾汇总，供 discussion 主链直接挂载。"""

    case_id: str
    contradictions: list[Contradiction] = Field(default_factory=list)
    summary_lines: list[str] = Field(default_factory=list)
    must_answer_questions: list[str] = Field(default_factory=list)


class RankAction(BaseModel):
    """盘中候选重排动作。"""

    symbol: str
    action: Literal["UPGRADE", "DOWNGRADE", "FREEZE", "FREEZE_ALL"]
    reason: str = ""
    trigger: str = ""
    priority_delta: int = 0
    generated_at: str = ""


class IntradayRankResult(BaseModel):
    """盘中重排结果汇总。"""

    generated_at: str = ""
    actions: list[RankAction] = Field(default_factory=list)
    freeze_all_active: bool = False
    summary_lines: list[str] = Field(default_factory=list)


class PlaybookOverride(BaseModel):
    """学习治理生成的战法 override。"""

    playbook: str
    status: Literal["suspend", "boost"]
    reason: str = ""
    source: str = ""
    trade_date: str = ""
    expires_on: str = ""
    streak: int = 0


class PlaybookOverrideSnapshot(BaseModel):
    """战法 override 快照，供次日 route 读取。"""

    trade_date: str = ""
    generated_at: str = ""
    source: str = "auto_governance"
    overrides: list[PlaybookOverride] = Field(default_factory=list)
    streaks: dict[str, int] = Field(default_factory=dict)
    summary_lines: list[str] = Field(default_factory=list)


class StructuredEvent(BaseModel):
    """结构化事件最小契约。"""

    symbol: str
    event_type: str
    impact: Literal["positive", "neutral", "negative", "block"] = "neutral"
    severity: str = "info"
    title: str
    published_at: str
    source: str
    tags: list[str] = Field(default_factory=list)
    category: Literal["news", "announcements", "policy"] = "announcements"
    impact_scope: Literal["market", "sector", "symbol", "macro", "unknown"] = "symbol"
    summary: str = ""
    name: str = ""
    evidence_url: str = ""


class EventFetchResult(BaseModel):
    """盘前结构化事件抓取结果。"""

    trade_date: str = ""
    generated_at: str = ""
    events: list[StructuredEvent] = Field(default_factory=list)
    summary_lines: list[str] = Field(default_factory=list)


class PlaybookContext(BaseModel):
    """战法路由输出，供买入决策和讨论系统消费"""

    playbook: PlaybookName
    symbol: str
    sector: str
    entry_window: str = ""
    confidence: float = 0.0
    rank_in_sector: int = 0
    leader_score: float = 0.0
    style_tag: str = ""
    exit_params: dict = Field(default_factory=dict)
    playbook_match_score: PlaybookMatchScore | None = None


class ExitContext(BaseModel):
    """持仓退出监控上下文"""

    symbol: str
    playbook: PlaybookName
    entry_price: float
    entry_time: str
    holding_minutes: int = 0
    holding_days: int = 0
    sector_name: str = ""
    is_limit_up: bool = False
    is_bomb: bool = False
    sector_retreat: bool = False
    relative_strength_5m: float = 0.0
    intraday_change_pct: float = 0.0
    intraday_drawdown_pct: float = 0.0
    rebound_from_low_pct: float = 0.0
    negative_alert_count: int = 0
    sector_intraday_change_pct: float = 0.0
    sector_relative_strength_5m: float = 0.0
    sector_relative_trend_5m: float = 0.0
    sector_underperform_bars_5m: int = 0
    optimal_hold_days: int = 1
    style_tag: str = ""
    avg_sector_rank_30d: float = 99.0
    leader_frequency_30d: float = 0.0
    exit_params: dict = Field(default_factory=dict)


class StockBehaviorProfile(BaseModel):
    """个股股性画像，由滚动窗口历史特征构建。"""

    symbol: str
    board_success_rate_20d: float = 0.0
    bomb_rate_20d: float = 0.0
    next_day_premium_20d: float = 0.0
    reseal_rate_20d: float = 0.0
    optimal_hold_days: int = 1
    style_tag: StyleTag = "mixed"
    avg_sector_rank_30d: float = 99.0
    leader_frequency_30d: float = 0.0


class LeaderRankResult(BaseModel):
    """板块内相对龙头排序结果。"""

    symbol: str
    sector: str = ""
    leader_score: float = 0.0
    zt_order_rank: int = 99
    seal_ratio: float = 0.0
    first_limit_time: str = ""
    open_times: int = 0
    is_core_leader: bool = False


class ExitSignal(BaseModel):
    """战法化退出信号。"""

    symbol: str
    reason: str
    sell_ratio: float = 1.0
    urgency: ExitUrgency = "NORMAL"
    current_price: float = 0.0
    reference_price: float = 0.0
    notes: list[str] = Field(default_factory=list)


# ── 集合竞价 ──────────────────────────────────────────────

AuctionAction = Literal["PROMOTE", "HOLD", "DEMOTE", "KILL"]


class AuctionSnapshot(BaseModel):
    """集合竞价快照，09:15 / 09:20 / 09:24 三个时间点各抓一次。"""

    symbol: str
    name: str = ""
    price: float                    # 竞价价格
    volume: int                     # 竞价成交量（手）
    prev_close: float               # 昨日收盘价
    prev_volume_5d_avg: int = 0     # 近 5 日平均成交量（手）
    timestamp: str = ""             # 快照时间，如 "09:24:00"
    open_change_pct: float = 0.0    # 竞价涨幅 = (price - prev_close) / prev_close


class AuctionSignal(BaseModel):
    """竞价研判信号。"""

    symbol: str
    action: AuctionAction = "HOLD"
    reason: str = ""
    auction_volume_ratio: float = 0.0   # 竞价量 / 5日均量
    open_change_pct: float = 0.0        # 竞价涨幅
    playbook: str = ""
    confidence: float = 0.0


# ── 微观节奏 ──────────────────────────────────────────────

MicroSignalType = Literal["PEAK_FADE", "VALLEY_HOLD", "RHYTHM_BREAK"]


class MicroBarSnapshot(BaseModel):
    """1 分钟 K 线微观快照。"""

    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: str = ""


class MicroSignal(BaseModel):
    """微观节奏信号。"""

    symbol: str
    signal_type: MicroSignalType
    strength: float = 0.0           # 0~1，信号强度
    timestamp: str = ""
    bar_count: int = 1              # 触发该信号所用的 bar 数量
    notes: list[str] = Field(default_factory=list)


# ── 事件总线 ──────────────────────────────────────────────

EventType = Literal[
    "NEGATIVE_NEWS",
    "PRICE_ALERT",
    "AUCTION_SIGNAL",
    "DISCUSSION_TIMEOUT",
    "SETTLEMENT_COMPLETE",
    "MODEL_UPDATED",
]


class MarketEvent(BaseModel):
    """事件总线中的最小事件单元。"""

    event_type: EventType
    symbol: str | None = None
    payload: dict = Field(default_factory=dict)
    timestamp: str = ""
    priority: int = 0               # 0=普通 1=高 2=紧急
    source: str = ""


# ── 夜间沙盘 ──────────────────────────────────────────────

class SandboxResult(BaseModel):
    """夜间沙盘推演结果。"""

    trade_date: str = ""
    generated_at: str = ""
    tomorrow_priorities: list[str] = Field(default_factory=list)
    missed_opportunities: list[dict] = Field(default_factory=list)
    simulation_log: list[str] = Field(default_factory=list)
    summary_lines: list[str] = Field(default_factory=list)


# ── 数据时效 ──────────────────────────────────────────────

DataFreshnessLevel = Literal["REALTIME", "NEAR_REALTIME", "DELAYED", "STALE"]


# ── Prompt 自进化 ─────────────────────────────────────────

class Lesson(BaseModel):
    """单条 Agent 教训记录。"""

    text: str
    source: str = ""                # 触发来源，如 "attribution.by_playbook"
    agent_id: str = ""
    created_at: str = ""
    expires_at: str = ""            # 30 天后自动淘汰


class PatchResult(BaseModel):
    """Prompt patch 操作结果。"""

    agent_id: str
    lessons_before: int = 0
    lessons_after: int = 0
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    error: str | None = None


# ── 审计 ──────────────────────────────────────────────────

class AuditRecord(BaseModel):
    audit_id: str
    category: str
    message: str
    payload: dict = Field(default_factory=dict)


# ── 健康检查 ──────────────────────────────────────────────

class HealthStatus(BaseModel):
    status: Literal["ok", "degraded", "error"]
    service: str
    mode: str
    checks: list[dict] = Field(default_factory=list)


# ── Windows Gateway 执行桥协议 ─────────────────────────────

ExecutionIntentStatus = Literal[
    "approved",
    "claimed",
    "submitted",
    "partial_filled",
    "filled",
    "canceled",
    "rejected",
    "failed",
    "expired",
]


class ExecutionIntentPacket(BaseModel):
    intent_scope: str = "execution_intent_packet"
    intent_id: str
    intent_version: str = "v1"
    generated_at: str
    trade_date: str
    account_id: str = ""
    symbol: str
    side: Literal["BUY", "SELL"]
    price: float | None = None
    quantity: int = Field(gt=0)
    order_type: str = "limit"
    time_in_force: str = "day"
    run_mode: str = "paper"
    execution_plane: str = "windows_gateway"
    approval_source: str = ""
    approved_by: str = ""
    approved_at: str = ""
    idempotency_key: str
    live_execution_allowed: bool = False
    offline_only: bool = False
    status: ExecutionIntentStatus = "approved"
    request: dict[str, Any] = Field(default_factory=dict)
    strategy_context: dict[str, Any] = Field(default_factory=dict)
    risk_context: dict[str, Any] = Field(default_factory=dict)
    discussion_context: dict[str, Any] = Field(default_factory=dict)
    claim: dict[str, Any] = Field(default_factory=dict)
    summary_lines: list[str] = Field(default_factory=list)


class ExecutionGatewayClaimInput(BaseModel):
    intent_id: str
    gateway_source_id: str
    deployment_role: str = ""
    bridge_path: str = ""
    claimed_at: str


class ExecutionGatewayReceiptInput(BaseModel):
    receipt_id: str
    intent_id: str
    intent_version: str = "v1"
    gateway_source_id: str
    deployment_role: str = ""
    bridge_path: str = ""
    reported_at: str
    submitted_at: str = ""
    status: ExecutionIntentStatus
    broker_order_id: str = ""
    broker_session_id: str = ""
    exchange_order_id: str = ""
    error_code: str = ""
    error_message: str = ""
    order: dict[str, Any] = Field(default_factory=dict)
    fills: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: float | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    summary_lines: list[str] = Field(default_factory=list)
