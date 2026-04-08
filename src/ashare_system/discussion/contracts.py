"""discussion / OpenClaw 工程化 helper 的共享 schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


RiskGate = Literal["pending", "allow", "limit", "reject"]
AuditGate = Literal["pending", "clear", "hold"]
FinalStatus = Literal["selected", "watchlist", "rejected"]
OpinionConfidence = Literal["high", "medium", "low"]
OpinionStance = Literal["support", "watch", "limit", "hold", "question", "rejected", "selected", "watchlist"]
PoolState = Literal[
    "day_open",
    "base_pool_ready",
    "focus_pool_building",
    "focus_pool_ready",
    "execution_pool_building",
    "execution_pool_ready",
    "execution_pool_blocked",
]
DiscussionState = Literal[
    "idle",
    "round_1_running",
    "round_1_summarized",
    "round_2_running",
    "final_review_ready",
    "final_selection_ready",
    "final_selection_blocked",
]

DISCUSSION_AGENT_IDS: frozenset[str] = frozenset(
    {"ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"}
)
AGENT_DISPLAY_NAMES: dict[str, str] = {
    "ashare-research": "研究",
    "ashare-strategy": "策略",
    "ashare-risk": "风控",
    "ashare-audit": "审计",
    "ashare-runtime": "运行",
    "ashare-executor": "执行",
}
ROUND_2_SUBSTANTIVE_FIELDS: tuple[str, ...] = (
    "challenged_by",
    "challenged_points",
    "questions_to_others",
    "previous_stance",
    "changed",
    "changed_because",
    "resolved_questions",
    "remaining_disputes",
    "thesis",
    "key_evidence",
    "evidence_gaps",
)
STANCE_ALIAS_MAP: dict[str, OpinionStance] = {
    "selected": "support",
    "select": "support",
    "support": "support",
    "approve": "support",
    "approved": "support",
    "allow": "support",
    "bullish": "support",
    "watchlist": "watch",
    "watch": "watch",
    "neutral": "watch",
    "limit": "limit",
    "conditional": "limit",
    "hold": "hold",
    "question": "question",
    "ask": "question",
    "query": "question",
    "rejected": "rejected",
    "reject": "rejected",
    "oppose": "rejected",
    "against": "rejected",
    "bearish": "rejected",
    "block": "rejected",
}


class DiscussionPoolMembership(BaseModel):
    base_pool: bool = True
    focus_pool: bool = False
    execution_pool: bool = False
    watchlist: bool = False


class DiscussionRuntimeSnapshot(BaseModel):
    rank: int
    selection_score: float
    action: str
    score_breakdown: dict[str, int | float] = Field(default_factory=dict)
    summary: str = ""
    market_snapshot: dict[str, Any] = Field(default_factory=dict)
    runtime_report_ref: str | None = None


class DiscussionOpinion(BaseModel):
    case_id: str = ""
    round: int
    agent_id: str
    stance: OpinionStance
    confidence: OpinionConfidence = "medium"
    reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    thesis: str = ""
    key_evidence: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    questions_to_others: list[str] = Field(default_factory=list)
    challenged_by: list[str] = Field(default_factory=list)
    challenged_points: list[str] = Field(default_factory=list)
    previous_stance: OpinionStance | None = None
    changed: bool | None = None
    changed_because: list[str] = Field(default_factory=list)
    resolved_questions: list[str] = Field(default_factory=list)
    remaining_disputes: list[str] = Field(default_factory=list)
    recorded_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class DiscussionRoundSummary(BaseModel):
    selected_points: list[str] = Field(default_factory=list)
    support_points: list[str] = Field(default_factory=list)
    rejected_points: list[str] = Field(default_factory=list)
    oppose_points: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    questions_for_round_2: list[str] = Field(default_factory=list)
    resolved_points: list[str] = Field(default_factory=list)
    remaining_disputes: list[str] = Field(default_factory=list)
    theses: list[str] = Field(default_factory=list)
    key_evidence: list[str] = Field(default_factory=list)
    challenged_points: list[str] = Field(default_factory=list)
    revision_notes: list[str] = Field(default_factory=list)
    persuasion_summary: list[str] = Field(default_factory=list)


class DiscussionCaseRecord(BaseModel):
    case_id: str
    trade_date: str
    symbol: str
    name: str = ""
    pool_membership: DiscussionPoolMembership = Field(default_factory=DiscussionPoolMembership)
    runtime_snapshot: DiscussionRuntimeSnapshot
    opinions: list[DiscussionOpinion] = Field(default_factory=list)
    round_1_summary: DiscussionRoundSummary = Field(default_factory=DiscussionRoundSummary)
    round_2_summary: DiscussionRoundSummary = Field(default_factory=DiscussionRoundSummary)
    risk_gate: RiskGate = "pending"
    audit_gate: AuditGate = "pending"
    final_status: FinalStatus = "watchlist"
    selected_reason: str | None = None
    rejected_reason: str | None = None
    intraday_state: str = "observing"
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class DiscussionCycleSnapshot(BaseModel):
    cycle_id: str = ""
    trade_date: str
    pool_state: PoolState
    discussion_state: DiscussionState
    base_pool_case_ids: list[str] = Field(default_factory=list)
    focus_pool_case_ids: list[str] = Field(default_factory=list)
    round_2_target_case_ids: list[str] = Field(default_factory=list)
    execution_pool_case_ids: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    summary_snapshot: dict[str, Any] = Field(default_factory=dict)
    started_at: str | None = None
    round_1_started_at: str | None = None
    round_1_completed_at: str | None = None
    round_2_started_at: str | None = None
    round_2_completed_at: str | None = None
    finalized_at: str | None = None
    updated_at: str | None = None


class OpinionValidationIssue(BaseModel):
    level: Literal["error", "warning"]
    code: str
    message: str
    field: str | None = None
    case_id: str | None = None
    agent_id: str | None = None
    round: int | None = None


class OpinionBatchValidationResult(BaseModel):
    ok: bool = True
    normalized_items: list[DiscussionOpinion] = Field(default_factory=list)
    issues: list[OpinionValidationIssue] = Field(default_factory=list)
    covered_case_ids: list[str] = Field(default_factory=list)
    missing_case_ids: list[str] = Field(default_factory=list)
    duplicate_keys: list[str] = Field(default_factory=list)
    substantive_round_2_case_ids: list[str] = Field(default_factory=list)
    summary_lines: list[str] = Field(default_factory=list)


class DiscussionFinalizeBundle(BaseModel):
    trade_date: str
    reason_board: dict[str, Any]
    reply_pack: dict[str, Any]
    final_brief: dict[str, Any]
    client_brief: dict[str, Any] | None = None
    finalize_packet: dict[str, Any] = Field(default_factory=dict)

