"""候选股票 case 持久化与 runtime 同步。"""

from __future__ import annotations

import importlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from ..logging_config import get_logger

logger = get_logger("discussion.cases")

RiskGate = Literal["pending", "allow", "limit", "reject"]
AuditGate = Literal["pending", "clear", "hold"]
FinalStatus = Literal["selected", "watchlist", "rejected"]
OpinionConfidence = Literal["high", "medium", "low"]
OpinionStance = Literal["support", "watch", "limit", "hold", "question", "rejected", "selected", "watchlist"]
CandidateSource = Literal[
    "market_universe_scan",
    "sector_heat_scan",
    "news_catalyst_scan",
    "position_monitor_scan",
    "intraday_t_scan",
    "tail_ambush_scan",
    "agent_proposed",
]
AGENT_DISPLAY_NAMES = {
    "ashare-research": "研究",
    "ashare-strategy": "策略",
    "ashare-risk": "风控",
    "ashare-audit": "审计",
    "ashare-runtime": "运行",
    "ashare-executor": "执行",
}
DISCUSSION_AGENT_IDS = frozenset({"ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"})
CANDIDATE_SOURCE_ALIAS_MAP: dict[str, CandidateSource] = {
    "candidate_pool": "market_universe_scan",
    "runtime_pipeline": "market_universe_scan",
    "runtime_compose": "market_universe_scan",
    "market_universe_scan": "market_universe_scan",
    "sector_heat_scan": "sector_heat_scan",
    "news_catalyst_scan": "news_catalyst_scan",
    "position_monitor_scan": "position_monitor_scan",
    "intraday_t_scan": "intraday_t_scan",
    "tail_ambush_scan": "tail_ambush_scan",
    "agent_proposed": "agent_proposed",
}


class PoolMembership(BaseModel):
    base_pool: bool = True
    focus_pool: bool = False
    execution_pool: bool = False
    watchlist: bool = False


class CandidateRuntimeSnapshot(BaseModel):
    rank: int
    selection_score: float
    action: str
    source: CandidateSource = "market_universe_scan"
    source_tags: list[CandidateSource] = Field(default_factory=lambda: ["market_universe_scan"])
    source_detail: dict[str, Any] = Field(default_factory=dict)
    score_breakdown: dict[str, int | float] = Field(default_factory=dict)
    summary: str = ""
    market_snapshot: dict = Field(default_factory=dict)
    runtime_report_ref: str | None = None
    playbook_context: dict[str, Any] = Field(default_factory=dict)
    playbook_match_score: dict[str, Any] = Field(default_factory=dict)
    behavior_profile: dict[str, Any] = Field(default_factory=dict)
    sector_profile: dict[str, Any] = Field(default_factory=dict)
    market_profile: dict[str, Any] = Field(default_factory=dict)
    leader_rank: dict[str, Any] = Field(default_factory=dict)


class CandidateOpinion(BaseModel):
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
    recorded_at: str


class CandidateRoundSummary(BaseModel):
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


class CandidateCase(BaseModel):
    case_id: str
    trade_date: str
    symbol: str
    name: str = ""
    pool_membership: PoolMembership = Field(default_factory=PoolMembership)
    runtime_snapshot: CandidateRuntimeSnapshot
    opinions: list[CandidateOpinion] = Field(default_factory=list)
    round_1_summary: CandidateRoundSummary = Field(default_factory=CandidateRoundSummary)
    round_2_summary: CandidateRoundSummary = Field(default_factory=CandidateRoundSummary)
    risk_gate: RiskGate = "pending"
    audit_gate: AuditGate = "pending"
    final_status: FinalStatus = "watchlist"
    selected_reason: str | None = None
    rejected_reason: str | None = None
    intraday_state: str = "observing"
    bull_case: dict[str, Any] = Field(default_factory=dict)
    bear_case: dict[str, Any] = Field(default_factory=dict)
    uncertainty: dict[str, Any] = Field(default_factory=dict)
    contradictions: list[dict[str, Any]] = Field(default_factory=list)
    contradiction_summary_lines: list[str] = Field(default_factory=list)
    must_answer_questions: list[str] = Field(default_factory=list)
    updated_at: str


class CandidateCaseService:
    """候选 case 服务。"""

    def __init__(self, storage_path: Path, now_factory: Callable[[], datetime] | None = None) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_factory = now_factory or datetime.now
        self._evidence_builder = self._load_evidence_builder()
        self._contradiction_detector = self._load_contradiction_detector()

    def list_cases(self, trade_date: str | None = None, status: str | None = None, limit: int = 50) -> list[CandidateCase]:
        items = self._read_cases()
        if trade_date:
            items = [item for item in items if item.trade_date == trade_date]
        if status:
            items = [item for item in items if item.final_status == status]
        items.sort(key=lambda item: (-int(item.trade_date.replace("-", "")), item.runtime_snapshot.rank, item.symbol))
        return items[:limit]

    def get_case(self, case_id: str) -> CandidateCase | None:
        for item in self._read_cases():
            if item.case_id == case_id:
                return item
        return None

    def sync_from_runtime_report(self, report: dict, focus_pool_capacity: int = 10, execution_pool_capacity: int = 3) -> list[CandidateCase]:
        job_id = report.get("job_id")
        top_picks = report.get("top_picks", [])
        if not job_id or not top_picks:
            return []

        payload = self._read_payload()
        if payload.get("last_synced_job_id") == job_id:
            return self._read_cases()

        cases_by_id = {item.case_id: item for item in self._read_cases()}
        generated_at = report.get("generated_at") or self._now_factory().isoformat()
        trade_date = datetime.fromisoformat(generated_at).date().isoformat()
        synced: list[CandidateCase] = []

        for item in top_picks:
            symbol = item["symbol"]
            rank = int(item.get("rank", 0) or 0)
            case_id = f"case-{trade_date.replace('-', '')}-{symbol.replace('.', '-')}"
            existing = cases_by_id.get(case_id)
            primary_source = self._normalize_candidate_source(item.get("source") or report.get("source"))
            source_tags = self._normalize_candidate_source_tags(
                [
                    item.get("source"),
                    report.get("source"),
                    *(list(item.get("source_tags") or []) if isinstance(item.get("source_tags"), list) else []),
                    *(list(existing.runtime_snapshot.source_tags) if existing else []),
                ]
            )
            source_detail = self._as_dict(item.get("source_detail")) or self._existing_runtime_dict(existing, "source_detail")
            playbook_context = self._as_dict(item.get("playbook_context")) or self._existing_runtime_dict(
                existing, "playbook_context"
            )
            playbook_match_score = self._as_dict(item.get("playbook_match_score")) or self._as_dict(
                playbook_context.get("playbook_match_score")
            ) or self._existing_runtime_dict(existing, "playbook_match_score")
            behavior_profile = self._as_dict(item.get("behavior_profile")) or self._as_dict(
                playbook_context.get("behavior_profile")
            ) or self._existing_runtime_dict(existing, "behavior_profile")
            sector_profile = self._as_dict(item.get("sector_profile")) or self._existing_runtime_dict(existing, "sector_profile")
            market_profile = self._as_dict(item.get("market_profile")) or self._as_dict(
                report.get("market_profile")
            ) or self._existing_runtime_dict(existing, "market_profile")
            leader_rank = self._as_dict(item.get("leader_rank")) or self._derive_leader_rank_payload(
                playbook_context=playbook_context,
                playbook_match_score=playbook_match_score,
                behavior_profile=behavior_profile,
                existing=existing,
            )
            bull_case, bear_case, uncertainty = self._build_case_evidence(
                symbol=symbol,
                top_pick=item,
                playbook_context=playbook_context,
                playbook_match_score=playbook_match_score,
                behavior_profile=behavior_profile,
                sector_profile=sector_profile,
                market_profile=market_profile,
                leader_rank=leader_rank,
                existing=existing,
            )
            must_answer_questions = self._build_must_answer_questions(
                symbol=symbol,
                bull_case=bull_case,
                bear_case=bear_case,
                uncertainty=uncertainty,
            )
            derived_status = self._derive_status(rank, item.get("action", "HOLD"), execution_pool_capacity, focus_pool_capacity)
            pool_membership = PoolMembership(
                base_pool=True,
                focus_pool=rank <= focus_pool_capacity,
                execution_pool=rank <= execution_pool_capacity and item.get("action") == "BUY",
                watchlist=derived_status == "watchlist",
            )
            candidate = CandidateCase(
                case_id=case_id,
                trade_date=trade_date,
                symbol=symbol,
                name=item.get("name", existing.name if existing else ""),
                pool_membership=pool_membership,
                runtime_snapshot=CandidateRuntimeSnapshot(
                    rank=rank,
                    selection_score=float(item.get("selection_score", 0.0)),
                    action=item.get("action", "HOLD"),
                    source=primary_source,
                    source_tags=source_tags,
                    source_detail=source_detail,
                    score_breakdown=item.get("score_breakdown", {}),
                    summary=item.get("summary", ""),
                    market_snapshot=item.get("market_snapshot", {}),
                    runtime_report_ref=job_id,
                    playbook_context=playbook_context,
                    playbook_match_score=playbook_match_score,
                    behavior_profile=behavior_profile,
                    sector_profile=sector_profile,
                    market_profile=market_profile,
                    leader_rank=leader_rank,
                ),
                opinions=existing.opinions if existing else [],
                round_1_summary=existing.round_1_summary if existing else CandidateRoundSummary(),
                round_2_summary=existing.round_2_summary if existing else CandidateRoundSummary(),
                risk_gate=existing.risk_gate if existing else "pending",
                audit_gate=existing.audit_gate if existing else "pending",
                final_status=existing.final_status if existing and existing.opinions else derived_status,
                selected_reason=(existing.selected_reason if existing else None) or (item.get("summary") if derived_status == "selected" else None),
                rejected_reason=(existing.rejected_reason if existing else None) or (item.get("summary") if derived_status == "rejected" else None),
                intraday_state=existing.intraday_state if existing else "observing",
                bull_case=bull_case,
                bear_case=bear_case,
                uncertainty=uncertainty,
                contradictions=existing.contradictions if existing else [],
                contradiction_summary_lines=existing.contradiction_summary_lines if existing else [],
                must_answer_questions=must_answer_questions,
                updated_at=self._now_factory().isoformat(),
            )
            cases_by_id[case_id] = candidate
            synced.append(candidate)

        self._write_payload(
            {
                "last_synced_job_id": job_id,
                "cases": [
                    item.model_dump()
                    for item in sorted(cases_by_id.values(), key=lambda case: (case.trade_date, case.runtime_snapshot.rank, case.symbol))
                ],
            }
        )
        logger.info("同步 candidate_case: %s (%d)", job_id, len(synced))
        return synced

    def upsert_candidate_tickets(self, trade_date: str, tickets: list[dict[str, Any]]) -> list[CandidateCase]:
        if not tickets:
            return []
        payload = self._read_payload()
        cases_by_id = {item.case_id: item for item in self._read_cases()}
        updated_cases: list[CandidateCase] = []
        existing_ranks = [int(item.runtime_snapshot.rank or 0) for item in cases_by_id.values() if item.trade_date == trade_date]
        next_rank = max([800, *existing_ranks], default=800)

        for index, raw in enumerate(tickets, start=1):
            ticket = dict(raw or {})
            symbol = str(ticket.get("symbol") or "").strip()
            if not symbol:
                continue
            case_id = f"case-{trade_date.replace('-', '')}-{symbol.replace('.', '-')}"
            existing = cases_by_id.get(case_id)
            primary_source = self._normalize_candidate_source(ticket.get("source"))
            source_tags = self._normalize_candidate_source_tags(
                [
                    ticket.get("source"),
                    *(list(ticket.get("source_tags") or []) if isinstance(ticket.get("source_tags"), list) else []),
                    *(list(existing.runtime_snapshot.source_tags) if existing else []),
                ]
            )
            source_detail = self._existing_runtime_dict(existing, "source_detail")
            proposal_detail = {
                "symbol": symbol,
                "source": primary_source,
                "source_role": str(ticket.get("source_role") or ticket.get("submitted_by") or "").strip(),
                "market_logic": str(ticket.get("market_logic") or "").strip(),
                "core_evidence": [str(item) for item in list(ticket.get("core_evidence") or []) if str(item).strip()],
                "risk_points": [str(item) for item in list(ticket.get("risk_points") or []) if str(item).strip()],
                "why_now": str(ticket.get("why_now") or "").strip(),
                "trigger_type": str(ticket.get("trigger_type") or "").strip(),
                "trigger_time": str(ticket.get("trigger_time") or "").strip() or self._now_factory().isoformat(),
                "recommended_action": str(ticket.get("recommended_action") or "").strip(),
                "evidence_refs": [str(item) for item in list(ticket.get("evidence_refs") or []) if str(item).strip()],
                "submitted_by": str(ticket.get("submitted_by") or ticket.get("source_role") or "").strip(),
            }
            proposal_items = [
                dict(item)
                for item in list(source_detail.get("agent_proposals") or [])
                if isinstance(item, dict)
            ]
            proposal_items.append(proposal_detail)
            source_detail["agent_proposals"] = proposal_items[-10:]
            source_detail["latest_agent_proposal"] = proposal_detail
            pool_membership = (
                existing.pool_membership.model_copy(update={"base_pool": True})
                if existing is not None
                else PoolMembership(base_pool=True, focus_pool=False, execution_pool=False, watchlist=True)
            )
            rank = int(existing.runtime_snapshot.rank) if existing is not None else next_rank + index
            selection_score = (
                float(ticket.get("selection_score", existing.runtime_snapshot.selection_score if existing else 0.0) or 0.0)
            )
            summary = proposal_detail["why_now"] or proposal_detail["market_logic"] or (existing.runtime_snapshot.summary if existing else "Agent 自提机会票")
            bull_case = dict(existing.bull_case) if existing is not None else {}
            if proposal_detail["market_logic"]:
                bull_case["proposal_market_logic"] = proposal_detail["market_logic"]
            if proposal_detail["core_evidence"]:
                bull_case["proposal_core_evidence"] = proposal_detail["core_evidence"]
            bear_case = dict(existing.bear_case) if existing is not None else {}
            if proposal_detail["risk_points"]:
                bear_case["proposal_risk_points"] = proposal_detail["risk_points"]
            uncertainty = dict(existing.uncertainty) if existing is not None else {}
            uncertainty["proposal_source"] = primary_source
            uncertainty["needs_runtime_validation"] = True
            must_answer_questions = list(existing.must_answer_questions) if existing is not None else []
            must_answer_questions.extend(
                [
                    "该票是否值得从自提提案升级到 focus pool",
                    "当前市场假设是否足以支持把该票推进到正式讨论",
                ]
            )
            must_answer_questions = list(dict.fromkeys([item for item in must_answer_questions if str(item).strip()]))
            candidate = CandidateCase(
                case_id=case_id,
                trade_date=trade_date,
                symbol=symbol,
                name=str(ticket.get("name") or (existing.name if existing else "")).strip(),
                pool_membership=pool_membership,
                runtime_snapshot=CandidateRuntimeSnapshot(
                    rank=rank,
                    selection_score=selection_score,
                    action=str(ticket.get("action") or (existing.runtime_snapshot.action if existing else "WATCH")).strip() or "WATCH",
                    source=primary_source,
                    source_tags=source_tags,
                    source_detail=source_detail,
                    score_breakdown=(existing.runtime_snapshot.score_breakdown if existing else {}),
                    summary=summary,
                    market_snapshot=(existing.runtime_snapshot.market_snapshot if existing else {}),
                    runtime_report_ref=(existing.runtime_snapshot.runtime_report_ref if existing else None),
                    playbook_context=(existing.runtime_snapshot.playbook_context if existing else {}),
                    playbook_match_score=(existing.runtime_snapshot.playbook_match_score if existing else {}),
                    behavior_profile=(existing.runtime_snapshot.behavior_profile if existing else {}),
                    sector_profile=(existing.runtime_snapshot.sector_profile if existing else {}),
                    market_profile=(existing.runtime_snapshot.market_profile if existing else {}),
                    leader_rank=(existing.runtime_snapshot.leader_rank if existing else {}),
                ),
                opinions=existing.opinions if existing else [],
                round_1_summary=existing.round_1_summary if existing else CandidateRoundSummary(),
                round_2_summary=existing.round_2_summary if existing else CandidateRoundSummary(),
                risk_gate=existing.risk_gate if existing else "pending",
                audit_gate=existing.audit_gate if existing else "pending",
                final_status=existing.final_status if existing else "watchlist",
                selected_reason=existing.selected_reason if existing else None,
                rejected_reason=existing.rejected_reason if existing else None,
                intraday_state=existing.intraday_state if existing else "observing",
                bull_case=bull_case,
                bear_case=bear_case,
                uncertainty=uncertainty,
                contradictions=existing.contradictions if existing else [],
                contradiction_summary_lines=existing.contradiction_summary_lines if existing else [],
                must_answer_questions=must_answer_questions,
                updated_at=self._now_factory().isoformat(),
            )
            cases_by_id[case_id] = candidate
            updated_cases.append(candidate)

        payload["cases"] = [
            item.model_dump()
            for item in sorted(cases_by_id.values(), key=lambda case: (case.trade_date, case.runtime_snapshot.rank, case.symbol))
        ]
        self._write_payload(payload)
        return updated_cases

    def record_opinion(self, case_id: str, opinion: CandidateOpinion) -> CandidateCase:
        cases = self._read_cases()
        updated: CandidateCase | None = None
        for index, item in enumerate(cases):
            if item.case_id != case_id:
                continue
            item.opinions.append(opinion)
            item.updated_at = self._now_factory().isoformat()
            cases[index] = item
            updated = item
            break
        if updated is None:
            raise ValueError(f"candidate case not found: {case_id}")
        payload = self._read_payload()
        payload["cases"] = [item.model_dump() for item in cases]
        self._write_payload(payload)
        return updated

    def rebuild_case(self, case_id: str) -> CandidateCase:
        cases = self._read_cases()
        rebuilt: CandidateCase | None = None
        for index, item in enumerate(cases):
            if item.case_id != case_id:
                continue
            cases[index] = self._apply_summaries(item)
            rebuilt = cases[index]
            break
        if rebuilt is None:
            raise ValueError(f"candidate case not found: {case_id}")
        payload = self._read_payload()
        payload["cases"] = [item.model_dump() for item in cases]
        self._write_payload(payload)
        return rebuilt

    def rebuild_cases(self, trade_date: str | None = None) -> list[CandidateCase]:
        cases = self._read_cases()
        rebuilt: list[CandidateCase] = []
        for index, item in enumerate(cases):
            if trade_date and item.trade_date != trade_date:
                continue
            cases[index] = self._apply_summaries(item)
            rebuilt.append(cases[index])
        payload = self._read_payload()
        payload["cases"] = [item.model_dump() for item in cases]
        self._write_payload(payload)
        return rebuilt

    def record_opinions_batch(self, items: list[tuple[str, CandidateOpinion]]) -> list[CandidateCase]:
        cases = self._read_cases()
        case_map = {item.case_id: item for item in cases}
        touched_ids: list[str] = []
        for case_id, opinion in items:
            candidate = case_map.get(case_id)
            if candidate is None:
                raise ValueError(f"candidate case not found: {case_id}")
            candidate.opinions.append(opinion)
            candidate.updated_at = self._now_factory().isoformat()
            touched_ids.append(case_id)
        payload = self._read_payload()
        payload["cases"] = [item.model_dump() for item in case_map.values()]
        self._write_payload(payload)
        ordered_ids = list(dict.fromkeys(touched_ids))
        return [case_map[case_id] for case_id in ordered_ids]

    def build_trade_date_summary(self, trade_date: str) -> dict:
        items = self.list_cases(trade_date=trade_date, limit=500)
        status_groups = {"selected": [], "watchlist": [], "rejected": []}
        risk_counts = {"pending": 0, "allow": 0, "limit": 0, "reject": 0}
        audit_counts = {"pending": 0, "clear": 0, "hold": 0}
        round_coverage = {
            "round_1_ready": 0,
            "round_2_ready": 0,
            "round_2_substantive_ready": 0,
            "needs_round_2": 0,
        }
        substantive_gap_case_ids: list[str] = []
        for item in items:
            status_groups[item.final_status].append(self._summary_case_item(item))
            risk_counts[item.risk_gate] = risk_counts.get(item.risk_gate, 0) + 1
            audit_counts[item.audit_gate] = audit_counts.get(item.audit_gate, 0) + 1
            if self._round_agent_coverage(item, 1):
                round_coverage["round_1_ready"] += 1
            if self._round_agent_coverage(item, 2):
                round_coverage["round_2_ready"] += 1
            if item.round_1_summary.questions_for_round_2 or item.audit_gate == "hold" or item.risk_gate == "limit":
                round_coverage["needs_round_2"] += 1
                if self.round_2_has_substantive_response(item):
                    round_coverage["round_2_substantive_ready"] += 1
                else:
                    substantive_gap_case_ids.append(item.case_id)
        candidate_pool = [self._summary_case_item(item) for item in items]
        contradiction_summary_lines = self._collect_contradiction_summary_lines(items)
        case_contradictions = self._collect_case_contradictions(items)
        summary_lines = [
            f"候选池：{len(items)}只",
            "候选名单：" + "、".join(
                f"{item['symbol']} {item['name'] or item['symbol']}" for item in candidate_pool
            ) if candidate_pool else "候选名单：无",
            f"结论分布：selected={len(status_groups['selected'])} / watchlist={len(status_groups['watchlist'])} / rejected={len(status_groups['rejected'])}",
            (
                f"Gate 状态：risk allow={risk_counts['allow']} / limit={risk_counts['limit']} / reject={risk_counts['reject']}；"
                f"audit clear={audit_counts['clear']} / hold={audit_counts['hold']}"
            ),
        ]
        disputed_cases = [
            item
            for item in items
            if item.contradictions or item.round_2_summary.remaining_disputes or item.round_1_summary.questions_for_round_2
        ]
        revised_cases = [item for item in items if item.round_2_summary.revision_notes]
        if contradiction_summary_lines:
            summary_lines.append("矛盾检测：" + "；".join(contradiction_summary_lines[:5]))
        if disputed_cases:
            summary_lines.append(
                "争议焦点：" + "；".join(
                    self._controversy_line(item)
                    for item in disputed_cases[:5]
                )
            )
        if substantive_gap_case_ids:
            gap_case_ids = set(substantive_gap_case_ids)
            summary_lines.append(
                "二轮待实质回应：" + "；".join(
                    self._round_2_gap_line(item)
                    for item in items
                    if item.case_id in gap_case_ids
                )
            )
        if revised_cases:
            summary_lines.append(
                "已修正观点：" + "；".join(
                    f"{item.symbol} {item.name or item.symbol} -> {item.round_2_summary.revision_notes[0]}"
                    for item in revised_cases[:5]
                )
            )
        if status_groups["selected"]:
            summary_lines.append("入选：" + "；".join(self._summary_line(item) for item in status_groups["selected"]))
        if status_groups["watchlist"]:
            summary_lines.append("观察：" + "；".join(self._summary_line(item) for item in status_groups["watchlist"]))
        if status_groups["rejected"]:
            summary_lines.append("淘汰：" + "；".join(self._summary_line(item) for item in status_groups["rejected"]))
        source_counts = self._build_source_counts(items)
        return {
            "trade_date": trade_date,
            "case_count": len(items),
            "candidate_pool": candidate_pool,
            "base_sample_pool": candidate_pool,
            "deep_validation_pool": [self._summary_case_item(item) for item in items if item.pool_membership.focus_pool],
            "execution_candidate_pool": [self._summary_case_item(item) for item in items if item.pool_membership.execution_pool],
            "selected_count": len(status_groups["selected"]),
            "watchlist_count": len(status_groups["watchlist"]),
            "rejected_count": len(status_groups["rejected"]),
            "base_sample_count": len(candidate_pool),
            "deep_validation_count": sum(1 for item in items if item.pool_membership.focus_pool),
            "execution_candidate_count": sum(1 for item in items if item.pool_membership.execution_pool),
            "source_counts": source_counts,
            "risk_gate_counts": risk_counts,
            "audit_gate_counts": audit_counts,
            "round_coverage": round_coverage,
            "substantive_gap_case_ids": substantive_gap_case_ids,
            "contradicted_case_ids": [item.case_id for item in items if item.contradictions],
            "case_contradictions": case_contradictions,
            "contradiction_summary_lines": contradiction_summary_lines,
            "controversy_summary_lines": self._dedupe_texts(
                contradiction_summary_lines
                + [self._controversy_line(item) for item in disputed_cases[:5]]
            )[:8],
            "round_2_guidance": self._dedupe_texts(
                self._collect_contradiction_questions(items)
                + [self._round_2_gap_line(item) for item in items if item.case_id in set(substantive_gap_case_ids)]
            )[:8],
            "must_answer_questions": self._collect_case_must_answer_questions(items),
            "candidate_pool_lines": [self._summary_line(item) for item in candidate_pool],
            "selected_lines": [self._summary_line(item) for item in status_groups["selected"]],
            "watchlist_lines": [self._summary_line(item) for item in status_groups["watchlist"]],
            "rejected_lines": [self._summary_line(item) for item in status_groups["rejected"]],
            "summary_lines": summary_lines,
            "summary_text": "\n".join(summary_lines),
            "disputed_case_ids": [item.case_id for item in disputed_cases],
            "revised_case_ids": [item.case_id for item in revised_cases],
            "selected": status_groups["selected"],
            "watchlist": status_groups["watchlist"],
            "rejected": status_groups["rejected"],
        }

    def build_reason_board(self, trade_date: str) -> dict:
        items = self.list_cases(trade_date=trade_date, limit=500)
        groups = {"selected": [], "watchlist": [], "rejected": []}
        for item in items:
            groups[item.final_status].append(self._reason_item(item))
        return {
            "trade_date": trade_date,
            "case_count": len(items),
            "selected_count": len(groups["selected"]),
            "watchlist_count": len(groups["watchlist"]),
            "rejected_count": len(groups["rejected"]),
            "selected": groups["selected"],
            "watchlist": groups["watchlist"],
            "rejected": groups["rejected"],
        }

    def build_vote_board(self, trade_date: str, status: str | None = None, limit: int = 500) -> dict:
        items = self.list_cases(trade_date=trade_date, status=status, limit=limit)
        vote_items = [self._vote_detail_item(item) for item in items]
        counts = {
            "selected": sum(1 for item in items if item.final_status == "selected"),
            "watchlist": sum(1 for item in items if item.final_status == "watchlist"),
            "rejected": sum(1 for item in items if item.final_status == "rejected"),
        }
        summary_lines = [
            f"交易日 {trade_date} 投票明细：共 {len(items)} 只，selected={counts['selected']} / watchlist={counts['watchlist']} / rejected={counts['rejected']}。"
        ]
        if vote_items:
            summary_lines.extend(item["headline_line"] for item in vote_items[: min(5, len(vote_items))])
        return {
            "trade_date": trade_date,
            "status_filter": status,
            "case_count": len(vote_items),
            "counts": counts,
            "items": vote_items,
            "summary_lines": summary_lines,
            "summary_text": "\n".join(summary_lines),
        }

    def build_case_vote_detail(self, case_id: str) -> dict | None:
        case = self.get_case(case_id)
        if case is None:
            return None
        return self._vote_detail_item(case)

    def build_reply_pack(
        self,
        trade_date: str,
        selected_limit: int = 3,
        watchlist_limit: int = 5,
        rejected_limit: int = 5,
    ) -> dict:
        board = self.build_reason_board(trade_date)
        selected = board["selected"][:selected_limit]
        watchlist = board["watchlist"][:watchlist_limit]
        rejected = board["rejected"][:rejected_limit]
        overview_lines = [
            f"交易日 {trade_date}，候选 {board['case_count']} 只，入选 {board['selected_count']} 只，观察 {board['watchlist_count']} 只，淘汰 {board['rejected_count']} 只。"
        ]
        if selected:
            overview_lines.append(f"当前优先执行池关注 {', '.join(item['name'] or item['symbol'] for item in selected)}。")
        elif watchlist:
            overview_lines.append("当前没有满足执行条件的标的，重点观察池仍需继续收敛。")
        else:
            overview_lines.append("当前没有可执行标的，也没有足够的观察池结论。")
        debate_focus_lines = self._build_debate_focus_lines(board)
        persuasion_summary_lines = self._build_persuasion_summary_lines(board)
        must_answer_questions = self._collect_must_answer_questions(board)
        return {
            "trade_date": trade_date,
            "case_count": board["case_count"],
            "selected_count": board["selected_count"],
            "selection_count": board["selected_count"],
            "watchlist_count": board["watchlist_count"],
            "rejected_count": board["rejected_count"],
            "overview_lines": overview_lines,
            "debate_focus_lines": debate_focus_lines,
            "contradiction_summary_lines": self._collect_board_contradiction_lines(board),
            "challenge_exchange_lines": self._build_challenge_exchange_lines(board),
            "persuasion_summary_lines": persuasion_summary_lines,
            "must_answer_questions": must_answer_questions,
            "selected_lines": [self._reply_line(item) for item in selected],
            "selection_lines": [self._reply_line(item) for item in selected],
            "watchlist_lines": [self._reply_line(item) for item in watchlist],
            "rejected_lines": [self._reply_line(item) for item in rejected],
            "selected_display": [self._display_item(item) for item in selected],
            "selection_display": [self._display_item(item) for item in selected],
            "watchlist_display": [self._display_item(item) for item in watchlist],
            "rejected_display": [self._display_item(item) for item in rejected],
            "selected": selected,
            "selection": selected,
            "watchlist": watchlist,
            "rejected": rejected,
        }

    def build_final_brief(self, trade_date: str, selection_limit: int = 3) -> dict:
        reply_pack = self.build_reply_pack(
            trade_date,
            selected_limit=selection_limit,
            watchlist_limit=selection_limit,
            rejected_limit=selection_limit,
        )
        selected = reply_pack["selected"]
        watchlist = reply_pack["watchlist"]
        rejected = reply_pack["rejected"]
        status = "ready" if selected else "blocked"
        blockers: list[str] = []
        if not selected:
            blockers.append("no_selected_candidates")
        lines = list(reply_pack["overview_lines"])
        if reply_pack.get("debate_focus_lines"):
            lines.append("争议焦点:")
            lines.extend(reply_pack["debate_focus_lines"])
        if reply_pack.get("contradiction_summary_lines"):
            lines.append("矛盾检测:")
            lines.extend(reply_pack["contradiction_summary_lines"][:5])
        if reply_pack.get("persuasion_summary_lines"):
            lines.append("讨论收敛:")
            lines.extend(reply_pack["persuasion_summary_lines"])
        if selected:
            lines.append("最终推荐:")
            lines.extend(reply_pack["selected_lines"])
        elif watchlist:
            lines.append("当前观察池:")
            lines.extend(reply_pack["watchlist_lines"])
        else:
            lines.append("当前淘汰池:")
            lines.extend(reply_pack["rejected_lines"])
        return {
            "trade_date": trade_date,
            "status": status,
            "blockers": blockers,
            "selected_count": reply_pack["selected_count"],
            "selection_count": reply_pack["selected_count"],
            "watchlist_count": reply_pack["watchlist_count"],
            "rejected_count": reply_pack["rejected_count"],
            "selected": selected,
            "selection": selected,
            "watchlist": watchlist,
            "rejected": rejected,
            "selected_lines": reply_pack["selected_lines"],
            "selection_lines": reply_pack["selected_lines"],
            "selected_display": [self._display_item(item) for item in selected],
            "selection_display": [self._display_item(item) for item in selected],
            "watchlist_display": [self._display_item(item) for item in watchlist],
            "rejected_display": [self._display_item(item) for item in rejected],
            "contradiction_summary_lines": reply_pack.get("contradiction_summary_lines", []),
            "must_answer_questions": reply_pack.get("must_answer_questions", []),
            "debate_focus_lines": reply_pack.get("debate_focus_lines", []),
            "persuasion_summary_lines": reply_pack.get("persuasion_summary_lines", []),
            "lines": lines,
            "summary_text": "\n".join(lines),
        }

    def build_pool_snapshot(
        self,
        trade_date: str,
        focus_case_ids: list[str] | None = None,
        execution_case_ids: list[str] | None = None,
    ) -> dict:
        items = self.list_cases(trade_date=trade_date, limit=500)
        case_map = {item.case_id: item for item in items}

        focus_cases = self._ordered_cases(items, case_map, focus_case_ids, lambda item: item.pool_membership.focus_pool)
        execution_cases = self._ordered_cases(items, case_map, execution_case_ids, lambda item: item.pool_membership.execution_pool)
        source_counts = self._build_source_counts(items)

        return {
            "trade_date": trade_date,
            "candidate_pool": [self._pool_item(item) for item in items],
            "base_sample_pool": [self._pool_item(item) for item in items],
            "focus_pool": [self._pool_item(item) for item in focus_cases],
            "deep_validation_pool": [self._pool_item(item) for item in focus_cases],
            "execution_pool": [self._pool_item(item) for item in execution_cases],
            "execution_candidate_pool": [self._pool_item(item) for item in execution_cases],
            "watchlist": [self._pool_item(item) for item in items if item.final_status == "watchlist"],
            "rejected": [self._pool_item(item) for item in items if item.final_status == "rejected"],
            "counts": {
                "candidate_pool": len(items),
                "base_sample_pool": len(items),
                "focus_pool": len(focus_cases),
                "deep_validation_pool": len(focus_cases),
                "execution_pool": len(execution_cases),
                "execution_candidate_pool": len(execution_cases),
                "watchlist": sum(1 for item in items if item.final_status == "watchlist"),
                "rejected": sum(1 for item in items if item.final_status == "rejected"),
            },
            "source_counts": source_counts,
        }

    def _derive_status(self, rank: int, action: str, execution_pool_capacity: int, focus_pool_capacity: int) -> FinalStatus:
        if rank <= execution_pool_capacity and action == "BUY":
            return "selected"
        if rank <= focus_pool_capacity:
            return "watchlist"
        return "rejected"

    @staticmethod
    def _ordered_cases(
        items: list[CandidateCase],
        case_map: dict[str, CandidateCase],
        preferred_ids: list[str] | None,
        fallback_predicate,
    ) -> list[CandidateCase]:
        if preferred_ids:
            return [case_map[case_id] for case_id in preferred_ids if case_id in case_map]
        return [item for item in items if fallback_predicate(item)]

    @staticmethod
    def _build_source_counts(items: list[CandidateCase]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            sources = list(item.runtime_snapshot.source_tags or []) or [item.runtime_snapshot.source]
            for source in sources:
                counts[source] = counts.get(source, 0) + 1
        return dict(sorted(counts.items(), key=lambda pair: pair[0]))

    @staticmethod
    def _pool_item(case: CandidateCase) -> dict:
        return {
            "case_id": case.case_id,
            "symbol": case.symbol,
            "name": case.name,
            "rank": case.runtime_snapshot.rank,
            "selection_score": case.runtime_snapshot.selection_score,
            "action": case.runtime_snapshot.action,
            "final_status": case.final_status,
            "risk_gate": case.risk_gate,
            "audit_gate": case.audit_gate,
            "source": case.runtime_snapshot.source,
            "source_tags": list(case.runtime_snapshot.source_tags),
            "source_detail": dict(case.runtime_snapshot.source_detail),
            "reason": case.selected_reason or case.rejected_reason or case.runtime_snapshot.summary,
            "bull_case": dict(case.bull_case),
            "bear_case": dict(case.bear_case),
            "uncertainty": dict(case.uncertainty),
            "contradictions": [dict(item) for item in case.contradictions],
            "contradiction_summary_lines": list(case.contradiction_summary_lines or CandidateCaseService._case_contradiction_lines(case)),
            "must_answer_questions": list(case.must_answer_questions),
        }

    @staticmethod
    def _summary_case_item(case: CandidateCase) -> dict:
        return {
            "case_id": case.case_id,
            "symbol": case.symbol,
            "name": case.name,
            "rank": case.runtime_snapshot.rank,
            "selection_score": case.runtime_snapshot.selection_score,
            "source": case.runtime_snapshot.source,
            "source_tags": list(case.runtime_snapshot.source_tags),
            "risk_gate": case.risk_gate,
            "audit_gate": case.audit_gate,
            "reason": case.selected_reason or case.rejected_reason or case.runtime_snapshot.summary,
            "bull_case": dict(case.bull_case),
            "bear_case": dict(case.bear_case),
            "uncertainty": dict(case.uncertainty),
            "contradictions": [dict(item) for item in case.contradictions],
            "contradiction_summary_lines": list(case.contradiction_summary_lines or CandidateCaseService._case_contradiction_lines(case)),
            "must_answer_questions": list(case.must_answer_questions),
        }

    @staticmethod
    def _reason_item(case: CandidateCase) -> dict:
        latest_opinions: dict[str, dict] = {}
        for opinion in case.opinions:
            latest_opinions[opinion.agent_id] = {
                "round": opinion.round,
                "stance": opinion.stance,
                "confidence": opinion.confidence,
                "reasons": opinion.reasons,
                "evidence_refs": opinion.evidence_refs,
                "recorded_at": opinion.recorded_at,
            }
        return {
            "case_id": case.case_id,
            "symbol": case.symbol,
            "name": case.name,
            "rank": case.runtime_snapshot.rank,
            "selection_score": case.runtime_snapshot.selection_score,
            "action": case.runtime_snapshot.action,
            "final_status": case.final_status,
            "headline_reason": case.selected_reason or case.rejected_reason or case.runtime_snapshot.summary,
            "selected_reason": case.selected_reason,
            "rejected_reason": case.rejected_reason,
            "risk_gate": case.risk_gate,
            "audit_gate": case.audit_gate,
            "pool_membership": case.pool_membership.model_dump(),
            "runtime_snapshot": {
                "source": case.runtime_snapshot.source,
                "source_tags": list(case.runtime_snapshot.source_tags),
                "source_detail": dict(case.runtime_snapshot.source_detail),
                "summary": case.runtime_snapshot.summary,
                "score_breakdown": case.runtime_snapshot.score_breakdown,
                "market_snapshot": case.runtime_snapshot.market_snapshot,
                "runtime_report_ref": case.runtime_snapshot.runtime_report_ref,
                "playbook_context": case.runtime_snapshot.playbook_context,
                "playbook_match_score": case.runtime_snapshot.playbook_match_score,
                "behavior_profile": case.runtime_snapshot.behavior_profile,
                "sector_profile": case.runtime_snapshot.sector_profile,
                "market_profile": case.runtime_snapshot.market_profile,
                "leader_rank": case.runtime_snapshot.leader_rank,
            },
            "discussion": {
                "selected_points": case.round_1_summary.selected_points,
                "support_points": case.round_1_summary.support_points or case.round_1_summary.selected_points,
                "rejected_points": case.round_1_summary.rejected_points,
                "oppose_points": case.round_1_summary.oppose_points or case.round_1_summary.rejected_points,
                "evidence_gaps": case.round_1_summary.evidence_gaps,
                "questions_for_round_2": case.round_1_summary.questions_for_round_2,
                "theses": case.round_1_summary.theses,
                "key_evidence": case.round_1_summary.key_evidence,
                "challenged_points": case.round_1_summary.challenged_points,
                "resolved_points": case.round_2_summary.resolved_points,
                "remaining_disputes": case.round_2_summary.remaining_disputes,
                "challenge_exchange_lines": CandidateCaseService._collect_challenge_exchange_lines(case.opinions),
                "revision_notes": case.round_2_summary.revision_notes,
                "persuasion_summary": case.round_2_summary.persuasion_summary,
                "must_answer_questions": list(case.must_answer_questions),
                "contradictions": [dict(item) for item in case.contradictions],
                "contradiction_summary_lines": list(
                    case.contradiction_summary_lines or CandidateCaseService._case_contradiction_lines(case)
                ),
            },
            "bull_case": dict(case.bull_case),
            "bear_case": dict(case.bear_case),
            "uncertainty": dict(case.uncertainty),
            "contradictions": [dict(item) for item in case.contradictions],
            "contradiction_summary_lines": list(
                case.contradiction_summary_lines or CandidateCaseService._case_contradiction_lines(case)
            ),
            "must_answer_questions": list(case.must_answer_questions),
            "latest_opinions": latest_opinions,
            "opinion_count": len(case.opinions),
            "updated_at": case.updated_at,
        }

    @classmethod
    def _vote_detail_item(cls, case: CandidateCase) -> dict:
        reason_item = cls._reason_item(case)
        opinion_items = [cls._opinion_item(item) for item in case.opinions]
        rounds = {}
        for round_number in (1, 2):
            round_items = [item for item in opinion_items if item["round"] == round_number]
            rounds[f"round_{round_number}"] = {
                "round": round_number,
                "complete": cls._round_agent_coverage(case, round_number),
                "substantive_ready": cls.round_2_has_substantive_response(case) if round_number == 2 else None,
                "items": round_items,
                "lines": [item["line"] for item in round_items],
            }
        headline_reason = reason_item["headline_reason"] or "暂无结论理由"
        headline_line = (
            f"{case.symbol} {case.name or case.symbol} | 状态={case.final_status} | 排名={case.runtime_snapshot.rank} | "
            f"分数={case.runtime_snapshot.selection_score} | 风控={case.risk_gate} 审计={case.audit_gate} | 理由={headline_reason}"
        )
        return {
            **reason_item,
            "symbol_display": cls._display_item(reason_item),
            "headline_line": headline_line,
            "discussion_digest": {
                "challenge_exchange_lines": reason_item["discussion"].get("challenge_exchange_lines", []),
                "revision_lines": reason_item["discussion"].get("revision_notes", []),
                "dispute_lines": reason_item["discussion"].get("remaining_disputes", []),
                "contradiction_lines": reason_item.get("contradiction_summary_lines", []),
            },
            "discussion_digest_lines": cls._build_discussion_digest_lines(reason_item),
            "opinion_timeline": opinion_items,
            "rounds": rounds,
        }

    @staticmethod
    def _opinion_item(opinion: CandidateOpinion) -> dict:
        agent_display = AGENT_DISPLAY_NAMES.get(opinion.agent_id, opinion.agent_id)
        reasons_text = "；".join(opinion.reasons) if opinion.reasons else "无理由"
        evidence_text = "；".join(opinion.evidence_refs) if opinion.evidence_refs else "无证据引用"
        line = (
            f"Round {opinion.round} | {agent_display}({opinion.agent_id}) | 立场={opinion.stance} | "
            f"置信度={opinion.confidence} | 理由={reasons_text} | 证据={evidence_text}"
        )
        return {
            "round": opinion.round,
            "agent_id": opinion.agent_id,
            "agent_display": agent_display,
            "stance": opinion.stance,
            "confidence": opinion.confidence,
            "reasons": opinion.reasons,
            "evidence_refs": opinion.evidence_refs,
            "thesis": opinion.thesis,
            "key_evidence": opinion.key_evidence,
            "evidence_gaps": opinion.evidence_gaps,
            "questions_to_others": opinion.questions_to_others,
            "challenged_by": opinion.challenged_by,
            "challenged_points": opinion.challenged_points,
            "previous_stance": opinion.previous_stance,
            "changed": opinion.changed,
            "changed_because": opinion.changed_because,
            "resolved_questions": opinion.resolved_questions,
            "remaining_disputes": opinion.remaining_disputes,
            "recorded_at": opinion.recorded_at,
            "line": line,
        }

    @staticmethod
    def _reply_line(item: dict) -> str:
        gate = f"风控={item['risk_gate']} 审计={item['audit_gate']}"
        score = f"分数={item['selection_score']}"
        reason = item.get("headline_reason") or item.get("selected_reason") or item.get("rejected_reason") or ""
        return f"{item['symbol']} {item['name'] or item['symbol']} | 排名={item['rank']} | {score} | {gate} | 理由={reason}"

    @staticmethod
    def _summary_line(item: dict) -> str:
        reason = item.get("reason") or item.get("headline_reason") or item.get("selected_reason") or item.get("rejected_reason") or ""
        return (
            f"{item['symbol']} {item.get('name') or item['symbol']} | 排名={item.get('rank')} | "
            f"分数={item.get('selection_score')} | 风控={item.get('risk_gate')} 审计={item.get('audit_gate')} | 理由={reason}"
        )

    @staticmethod
    def _display_item(item: dict) -> str:
        return f"{item['symbol']} {item.get('name') or item['symbol']}"

    @staticmethod
    def _build_debate_focus_lines(board: dict) -> list[str]:
        lines: list[str] = []
        for group in ("selected", "watchlist", "rejected"):
            for item in board.get(group, []):
                discussion = item.get("discussion") or {}
                disputes = discussion.get("remaining_disputes") or discussion.get("questions_for_round_2") or []
                if not disputes:
                    continue
                lines.append(f"{item['symbol']} {item.get('name') or item['symbol']}：{disputes[0]}")
        return lines[:8]

    @staticmethod
    def _build_challenge_exchange_lines(board: dict) -> list[str]:
        lines: list[str] = []
        for group in ("selected", "watchlist", "rejected"):
            for item in board.get(group, []):
                discussion = item.get("discussion") or {}
                challenge_lines = discussion.get("challenge_exchange_lines") or []
                for entry in challenge_lines:
                    lines.append(f"{item['symbol']} {item.get('name') or item['symbol']}：{entry}")
        return lines[:8]

    @staticmethod
    def _build_persuasion_summary_lines(board: dict) -> list[str]:
        lines: list[str] = []
        for group in ("selected", "watchlist", "rejected"):
            for item in board.get(group, []):
                discussion = item.get("discussion") or {}
                persuasion = discussion.get("persuasion_summary") or discussion.get("revision_notes") or []
                for entry in persuasion:
                    lines.append(f"{item['symbol']} {item.get('name') or item['symbol']}：{entry}")
        return lines[:8]

    @staticmethod
    def _collect_board_contradiction_lines(board: dict) -> list[str]:
        lines: list[str] = []
        for group in ("selected", "watchlist", "rejected"):
            for item in board.get(group, []):
                lines.extend(list(item.get("contradiction_summary_lines", []) or []))
                if len(lines) >= 8:
                    return CandidateCaseService._dedupe_texts(lines)[:8]
        return CandidateCaseService._dedupe_texts(lines)[:8]

    @staticmethod
    def _collect_must_answer_questions(board: dict) -> list[str]:
        questions: list[str] = []
        for group in ("selected", "watchlist", "rejected"):
            for item in board.get(group, []):
                questions.extend(list(item.get("must_answer_questions", []) or []))
                if len(questions) >= 9:
                    return CandidateCaseService._dedupe_texts(questions)[:9]
        return CandidateCaseService._dedupe_texts(questions)[:9]

    @staticmethod
    def _controversy_line(case: CandidateCase) -> str:
        contradiction_lines = CandidateCaseService._case_contradiction_lines(case)
        if contradiction_lines:
            return contradiction_lines[0]
        dispute = (case.round_2_summary.remaining_disputes or case.round_1_summary.questions_for_round_2 or ["暂无显式争议"])[0]
        return f"{case.symbol} {case.name or case.symbol}：{dispute}"

    @staticmethod
    def _round_2_gap_line(case: CandidateCase) -> str:
        issue = (case.round_2_summary.remaining_disputes or case.round_1_summary.questions_for_round_2 or ["需要回应前序质疑"])[0]
        return (
            f"{case.symbol} {case.name or case.symbol}：需补充被谁质疑、回应了什么、是否改判或剩余争议。"
            f"当前焦点={issue}"
        )

    @classmethod
    def round_2_has_substantive_response(cls, case: CandidateCase) -> bool:
        round_2_opinions = cls._latest_round_opinions(case, 2)
        if not DISCUSSION_AGENT_IDS.issubset(round_2_opinions):
            return False
        return all(cls._opinion_has_substantive_round_2_response(round_2_opinions[agent_id]) for agent_id in DISCUSSION_AGENT_IDS)

    @classmethod
    def _build_discussion_digest_lines(cls, item: dict) -> list[str]:
        discussion = item.get("discussion") or {}
        lines: list[str] = []
        lines.extend(f"交锋：{entry}" for entry in discussion.get("challenge_exchange_lines", [])[:2])
        lines.extend(f"改判：{entry}" for entry in discussion.get("revision_notes", [])[:2])
        lines.extend(f"争议：{entry}" for entry in discussion.get("remaining_disputes", [])[:2])
        return lines[:6]

    def _read_cases(self) -> list[CandidateCase]:
        payload = self._read_payload()
        return [CandidateCase(**self._normalize_case_payload(item)) for item in payload.get("cases", [])]

    def _read_payload(self) -> dict:
        if not self._storage_path.exists():
            return {"cases": []}
        return json.loads(self._storage_path.read_text(encoding="utf-8"))

    def _write_payload(self, payload: dict) -> None:
        self._storage_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def _normalize_case_payload(cls, payload: dict) -> dict:
        normalized = dict(payload)
        opinions = []
        for raw in normalized.get("opinions", []):
            if not isinstance(raw, dict):
                opinions.append(raw)
                continue
            item = dict(raw)
            item["stance"] = cls._normalize_opinion_stance(item.get("stance"))
            previous = item.get("previous_stance")
            if previous:
                item["previous_stance"] = cls._normalize_opinion_stance(previous)
            opinions.append(item)
        normalized["opinions"] = opinions
        return normalized

    @staticmethod
    def _normalize_opinion_stance(value: str | None) -> str:
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        alias_map = {
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
        return alias_map.get(text, text)

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        if hasattr(value, "model_dump"):
            dumped = value.model_dump()
            return dict(dumped) if isinstance(dumped, dict) else {}
        if hasattr(value, "dict"):
            dumped = value.dict()
            return dict(dumped) if isinstance(dumped, dict) else {}
        return {}

    @classmethod
    def _existing_runtime_dict(cls, existing: CandidateCase | None, field_name: str) -> dict[str, Any]:
        if existing is None:
            return {}
        return cls._as_dict(getattr(existing.runtime_snapshot, field_name, None))

    @staticmethod
    def _normalize_candidate_source(value: Any) -> CandidateSource:
        key = str(value or "").strip().lower()
        return CANDIDATE_SOURCE_ALIAS_MAP.get(key, "agent_proposed" if key else "market_universe_scan")

    @classmethod
    def _normalize_candidate_source_tags(cls, values: list[Any]) -> list[CandidateSource]:
        normalized: list[CandidateSource] = []
        for value in values:
            key = str(value or "").strip()
            if not key:
                continue
            source = cls._normalize_candidate_source(key)
            if source not in normalized:
                normalized.append(source)
        return normalized or ["market_universe_scan"]

    @staticmethod
    def _load_evidence_builder() -> object | None:
        try:
            module = importlib.import_module("ashare_system.discussion.evidence_builder")
        except ModuleNotFoundError:
            return None
        builder_cls = getattr(module, "EvidenceBuilder", None)
        if builder_cls is None:
            return None
        try:
            return builder_cls()
        except Exception:
            logger.exception("初始化 EvidenceBuilder 失败，将回退 discussion 本地证据保底。")
            return None

    @staticmethod
    def _load_contradiction_detector() -> object | None:
        try:
            module = importlib.import_module("ashare_system.discussion.contradiction_detector")
        except ModuleNotFoundError:
            return None
        detector_cls = getattr(module, "ContradictionDetector", None)
        if detector_cls is None:
            return None
        try:
            return detector_cls()
        except Exception:
            logger.exception("初始化 ContradictionDetector 失败，将回退 discussion 本地矛盾检测保底。")
            return None

    @staticmethod
    def _coerce_case_mapping(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        if hasattr(value, "model_dump"):
            dumped = value.model_dump()
            return dict(dumped) if isinstance(dumped, dict) else {}
        return {}

    def _call_evidence_builder(self, **kwargs: Any) -> dict[str, Any]:
        builder = self._evidence_builder
        if builder is None:
            return {}
        for method_name in ("build_case_evidence", "build", "build_for_case", "build_evidence"):
            method = getattr(builder, method_name, None)
            if callable(method):
                try:
                    payload = method(**kwargs)
                except TypeError:
                    continue
                except Exception:
                    logger.exception("EvidenceBuilder 执行失败，将回退 discussion 本地证据保底。")
                    return {}
                return self._coerce_case_mapping(payload)
        return {}

    @staticmethod
    def _coerce_contradiction_summary(value: Any) -> dict[str, Any]:
        if value is None:
            return {"contradictions": [], "summary_lines": [], "must_answer_questions": []}
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        if isinstance(value, list):
            return {
                "contradictions": [dict(item) for item in value if isinstance(item, dict)],
                "summary_lines": [],
                "must_answer_questions": [],
            }
        if not isinstance(value, dict):
            return {"contradictions": [], "summary_lines": [], "must_answer_questions": []}
        contradictions = value.get("contradictions")
        if hasattr(contradictions, "model_dump"):
            contradictions = contradictions.model_dump()
        if not isinstance(contradictions, list):
            contradictions = []
        return {
            "contradictions": [dict(item) for item in contradictions if isinstance(item, dict)],
            "summary_lines": [str(item) for item in value.get("summary_lines", []) if str(item).strip()],
            "must_answer_questions": [str(item) for item in value.get("must_answer_questions", []) if str(item).strip()],
        }

    def _call_contradiction_detector(self, **kwargs: Any) -> dict[str, Any]:
        detector = self._contradiction_detector
        if detector is None:
            return {"contradictions": [], "summary_lines": [], "must_answer_questions": []}
        for method_name in ("detect_case_contradictions", "detect", "build", "analyze_case"):
            method = getattr(detector, method_name, None)
            if callable(method):
                try:
                    payload = method(**kwargs)
                except TypeError:
                    continue
                except Exception:
                    logger.exception("ContradictionDetector 执行失败，将回退 discussion 本地矛盾检测保底。")
                    return {"contradictions": [], "summary_lines": [], "must_answer_questions": []}
                return self._coerce_contradiction_summary(payload)
        return {"contradictions": [], "summary_lines": [], "must_answer_questions": []}

    def _build_case_evidence(
        self,
        *,
        symbol: str,
        top_pick: dict[str, Any],
        playbook_context: dict[str, Any],
        playbook_match_score: dict[str, Any],
        behavior_profile: dict[str, Any],
        sector_profile: dict[str, Any],
        market_profile: dict[str, Any],
        leader_rank: dict[str, Any],
        existing: CandidateCase | None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        fallback = self._build_fallback_case_evidence(
            symbol=symbol,
            top_pick=top_pick,
            playbook_context=playbook_context,
            playbook_match_score=playbook_match_score,
            behavior_profile=behavior_profile,
            leader_rank=leader_rank,
            existing=existing,
        )
        built = self._call_evidence_builder(
            symbol=symbol,
            playbook_context=playbook_context,
            playbook_match_score=playbook_match_score,
            behavior_profile=behavior_profile,
            sector_profile=sector_profile,
            market_profile=market_profile,
            leader_rank=leader_rank,
        )
        bull_case = self._coerce_case_mapping(built.get("bull_case")) or fallback[0]
        bear_case = self._coerce_case_mapping(built.get("bear_case")) or fallback[1]
        uncertainty = self._coerce_case_mapping(built.get("uncertainty")) or fallback[2]
        return bull_case, bear_case, uncertainty

    def _build_fallback_case_evidence(
        self,
        *,
        symbol: str,
        top_pick: dict[str, Any],
        playbook_context: dict[str, Any],
        playbook_match_score: dict[str, Any],
        behavior_profile: dict[str, Any],
        leader_rank: dict[str, Any],
        existing: CandidateCase | None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        reason = str(
            playbook_match_score.get("reason")
            or top_pick.get("summary")
            or playbook_context.get("playbook")
            or f"{symbol} 当前仍缺少明确主叙事"
        )
        bull_evidence = [str(item) for item in playbook_match_score.get("bull_evidence", []) if str(item).strip()]
        bear_evidence = [str(item) for item in playbook_match_score.get("bear_evidence", []) if str(item).strip()]

        rank_in_sector = playbook_context.get("rank_in_sector")
        leader_score = playbook_context.get("leader_score")
        style_tag = playbook_context.get("style_tag") or behavior_profile.get("style_tag")
        board_success_rate = behavior_profile.get("board_success_rate_20d")
        bomb_rate = behavior_profile.get("bomb_rate_20d")
        seal_ratio = leader_rank.get("seal_ratio") or playbook_context.get("seal_ratio")

        fact_lines = list(bull_evidence)
        risk_lines = list(bear_evidence)
        data_gaps: list[str] = []
        key_unknowns: list[str] = []

        if rank_in_sector is not None:
            fact_lines.append(f"rank_in_sector={rank_in_sector}")
        else:
            data_gaps.append("rank_in_sector 缺失")
        if leader_score is not None:
            fact_lines.append(f"leader_score={float(leader_score):.2f}")
        else:
            data_gaps.append("leader_score 缺失")
        if style_tag:
            fact_lines.append(f"style_tag={style_tag}")
        else:
            data_gaps.append("style_tag 缺失")
        if board_success_rate is not None:
            fact_lines.append(f"board_success_rate_20d={float(board_success_rate):.2f}")
        else:
            data_gaps.append("board_success_rate_20d 缺失")
        if bomb_rate is not None:
            risk_lines.append(f"bomb_rate_20d={float(bomb_rate):.2f}")
        else:
            data_gaps.append("bomb_rate_20d 缺失")
        if seal_ratio is not None:
            fact_lines.append(f"seal_ratio={float(seal_ratio):.2f}")
        else:
            data_gaps.append("seal_ratio 缺失")

        if not risk_lines:
            risk_lines.append("尚未拿到明确 bear_evidence，需讨论侧补充最核心反证。")
        if not fact_lines:
            fact_lines.append(reason)
        if existing is not None and existing.uncertainty:
            key_unknowns.extend(str(item) for item in existing.uncertainty.get("key_unknowns", []) if str(item).strip())
        if not key_unknowns:
            key_unknowns.append("盘中是否出现对当前主叙事的快速证伪，仍需在 discussion 中明确。")

        bull_case = {
            "thesis": reason,
            "key_facts": fact_lines[:8],
        }
        bear_case = {
            "thesis": risk_lines[0],
            "key_risks": risk_lines[:8],
        }
        uncertainty = {
            "data_gaps": data_gaps or ["需继续核对盘中证据与板块同步性。"],
            "key_unknowns": key_unknowns[:6],
        }
        return bull_case, bear_case, uncertainty

    @classmethod
    def _build_must_answer_questions(
        cls,
        *,
        symbol: str,
        bull_case: dict[str, Any],
        bear_case: dict[str, Any],
        uncertainty: dict[str, Any],
    ) -> list[str]:
        bull_focus = cls._first_meaningful_text(
            *(bull_case.get("key_facts") or []),
            bull_case.get("thesis"),
        )
        bear_focus = cls._first_meaningful_text(
            *(bear_case.get("key_risks") or []),
            bear_case.get("thesis"),
        )
        overturn_focus = cls._first_meaningful_text(
            *(uncertainty.get("key_unknowns") or []),
            *(uncertainty.get("data_gaps") or []),
        )
        return [
            f"{symbol}：哪条多头证据最可能被盘中证伪？当前先看 {bull_focus}",
            f"{symbol}：哪条空头证据若不成立会改变立场？当前先看 {bear_focus}",
            f"{symbol}：什么条件下当前结论必须推翻？当前先看 {overturn_focus}",
        ]

    def _build_case_contradictions(self, case: CandidateCase) -> dict[str, Any]:
        detected = self._call_contradiction_detector(
            case_id=case.case_id,
            opinions=list(case.opinions or []),
            bull_case=case.bull_case,
            bear_case=case.bear_case,
            uncertainty=case.uncertainty,
        )
        contradictions = [self._normalize_contradiction(item, case) for item in detected.get("contradictions", [])]
        summary_lines = [str(item) for item in detected.get("summary_lines", []) if str(item).strip()]
        must_answer_questions = [str(item) for item in detected.get("must_answer_questions", []) if str(item).strip()]
        if contradictions:
            return {
                "contradictions": contradictions,
                "summary_lines": self._dedupe_texts(summary_lines or self._build_contradiction_summary_lines(case, contradictions)),
                "must_answer_questions": self._dedupe_texts(
                    must_answer_questions or [str(item.get("question") or "").strip() for item in contradictions]
                ),
            }
        fallback = self._build_fallback_case_contradictions(case)
        return fallback

    def _build_fallback_case_contradictions(self, case: CandidateCase) -> dict[str, Any]:
        latest = self._latest_agent_opinions(case.opinions)
        contradictions: list[dict[str, Any]] = []
        contradictions.extend(
            self._pair_conflict(
                case=case,
                left=latest.get("ashare-research"),
                right=latest.get("ashare-risk"),
                contradiction_type="research_support_vs_risk_gate",
            )
        )
        contradictions.extend(
            self._pair_conflict(
                case=case,
                left=latest.get("ashare-strategy"),
                right=latest.get("ashare-audit"),
                contradiction_type="strategy_support_vs_audit_gate",
            )
        )
        contradictions.extend(self._case_level_conflict(case, latest))
        return {
            "contradictions": contradictions,
            "summary_lines": self._build_contradiction_summary_lines(case, contradictions),
            "must_answer_questions": self._dedupe_texts(
                [str(item.get("question") or "").strip() for item in contradictions if str(item.get("question") or "").strip()]
            ),
        }

    @staticmethod
    def _latest_agent_opinions(opinions: list[CandidateOpinion]) -> dict[str, CandidateOpinion]:
        latest: dict[str, CandidateOpinion] = {}
        for item in opinions:
            previous = latest.get(item.agent_id)
            current_key = (item.round, item.recorded_at)
            previous_key = (previous.round, previous.recorded_at) if previous is not None else (-1, "")
            if current_key >= previous_key:
                latest[item.agent_id] = item
        return latest

    @classmethod
    def _pair_conflict(
        cls,
        *,
        case: CandidateCase,
        left: CandidateOpinion | None,
        right: CandidateOpinion | None,
        contradiction_type: str,
    ) -> list[dict[str, Any]]:
        if left is None or right is None:
            return []
        if not cls._is_support_stance(left.stance) or not cls._is_blocking_stance(right.stance):
            return []
        return [cls._build_contradiction(case, left.agent_id, right.agent_id, contradiction_type)]

    @classmethod
    def _case_level_conflict(
        cls,
        case: CandidateCase,
        latest: dict[str, CandidateOpinion],
    ) -> list[dict[str, Any]]:
        supporters = [item for item in latest.values() if cls._is_support_stance(item.stance)]
        blockers = [item for item in latest.values() if cls._is_blocking_stance(item.stance)]
        if not supporters or not blockers:
            return []
        pair = (supporters[0].agent_id, blockers[0].agent_id)
        if pair in {("ashare-research", "ashare-risk"), ("ashare-strategy", "ashare-audit")}:
            return []
        return [cls._build_contradiction(case, pair[0], pair[1], "case_stance_conflict")]

    @classmethod
    def _build_contradiction(
        cls,
        case: CandidateCase,
        left_agent_id: str,
        right_agent_id: str,
        contradiction_type: str,
    ) -> dict[str, Any]:
        left_label = AGENT_DISPLAY_NAMES.get(left_agent_id, left_agent_id)
        right_label = AGENT_DISPLAY_NAMES.get(right_agent_id, right_agent_id)
        bull_fact = cls._first_meaningful_text(*(case.bull_case.get("key_facts") or []), case.bull_case.get("thesis"))
        bear_risk = cls._first_meaningful_text(*(case.bear_case.get("key_risks") or []), case.bear_case.get("thesis"))
        unknown = cls._first_meaningful_text(
            *(case.uncertainty.get("key_unknowns") or []),
            *(case.uncertainty.get("data_gaps") or []),
            case.uncertainty.get("thesis"),
        )
        if contradiction_type == "research_support_vs_risk_gate":
            question = (
                f"{left_label}支持而{right_label}仍阻断。请说明多头证据“{bull_fact}”为何足以覆盖空头风险“{bear_risk}”，"
                f"且未知项“{unknown}”若未消除，是否必须维持阻断？"
            )
        elif contradiction_type == "strategy_support_vs_audit_gate":
            question = (
                f"{left_label}认为可执行，但{right_label}仍不放行。请说明战法依据“{bull_fact}”是否真的成立，"
                f"若风险“{bear_risk}”未被证伪、且未知项“{unknown}”仍存在，当前执行结论是否必须推翻？"
            )
        else:
            question = (
                f"当前 case 存在明显立场冲突。请围绕多头事实“{bull_fact}”、空头风险“{bear_risk}”与未知项“{unknown}”统一判断："
                f"哪条证据一旦失效就必须改判？"
            )
        return {
            "case_id": case.case_id,
            "between": [left_agent_id, right_agent_id],
            "type": contradiction_type,
            "question": question,
            "must_resolve_before_round_2": True,
            "evidence_refs": [
                f"bull_case:{bull_fact}",
                f"bear_case:{bear_risk}",
                f"uncertainty:{unknown}",
            ],
        }

    @staticmethod
    def _is_support_stance(stance: str | None) -> bool:
        return str(stance or "").strip().lower() in {"support", "selected"}

    @staticmethod
    def _is_blocking_stance(stance: str | None) -> bool:
        return str(stance or "").strip().lower() in {"reject", "rejected", "limit", "hold"}

    @classmethod
    def _normalize_contradiction(cls, item: dict[str, Any], case: CandidateCase) -> dict[str, Any]:
        contradiction = dict(item)
        contradiction["case_id"] = contradiction.get("case_id") or case.case_id
        contradiction["between"] = [str(value) for value in contradiction.get("between", []) if str(value).strip()]
        contradiction["type"] = str(contradiction.get("type") or "case_stance_conflict")
        contradiction["question"] = str(contradiction.get("question") or "").strip()
        contradiction["must_resolve_before_round_2"] = bool(contradiction.get("must_resolve_before_round_2", True))
        contradiction["evidence_refs"] = [str(value) for value in contradiction.get("evidence_refs", []) if str(value).strip()]
        if not contradiction["question"]:
            fallback = cls._build_contradiction(
                case,
                contradiction["between"][0] if contradiction["between"] else "ashare-research",
                contradiction["between"][1] if len(contradiction["between"]) > 1 else "ashare-risk",
                contradiction["type"],
            )
            contradiction["question"] = fallback["question"]
            contradiction["evidence_refs"] = fallback["evidence_refs"]
        return contradiction

    @classmethod
    def _build_contradiction_summary_lines(
        cls,
        case: CandidateCase,
        contradictions: list[dict[str, Any]],
    ) -> list[str]:
        lines: list[str] = []
        for item in contradictions:
            between = " vs ".join(AGENT_DISPLAY_NAMES.get(agent_id, agent_id) for agent_id in item.get("between", []) if agent_id)
            bull_fact = cls._first_meaningful_text(*(case.bull_case.get("key_facts") or []), case.bull_case.get("thesis"))
            bear_risk = cls._first_meaningful_text(*(case.bear_case.get("key_risks") or []), case.bear_case.get("thesis"))
            lines.append(
                f"{case.symbol} {case.name or case.symbol}：{between or item.get('type') or '矛盾'}，"
                f"多头证据“{bull_fact}”与空头风险“{bear_risk}”冲突，Round 2 必须回应。"
            )
        return cls._dedupe_texts(lines)[:8]

    @classmethod
    def _case_contradiction_lines(cls, case: CandidateCase) -> list[str]:
        if case.contradiction_summary_lines:
            return cls._dedupe_texts(case.contradiction_summary_lines)[:8]
        return cls._build_contradiction_summary_lines(case, [dict(item) for item in case.contradictions])

    @classmethod
    def _collect_case_contradictions(cls, items: list[CandidateCase]) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for case in items:
            for contradiction in case.contradictions:
                payloads.append(
                    {
                        "case_id": case.case_id,
                        "symbol": case.symbol,
                        "name": case.name,
                        **dict(contradiction),
                    }
                )
        return payloads[:20]

    @classmethod
    def _collect_contradiction_summary_lines(cls, items: list[CandidateCase]) -> list[str]:
        lines: list[str] = []
        for case in items:
            lines.extend(cls._case_contradiction_lines(case))
        return cls._dedupe_texts(lines)[:8]

    @classmethod
    def _collect_contradiction_questions(cls, items: list[CandidateCase]) -> list[str]:
        questions: list[str] = []
        for case in items:
            for item in case.contradictions:
                if item.get("must_resolve_before_round_2"):
                    questions.append(str(item.get("question") or "").strip())
        return cls._dedupe_texts(questions)[:8]

    @classmethod
    def _collect_case_must_answer_questions(cls, items: list[CandidateCase]) -> list[str]:
        questions: list[str] = []
        for case in items:
            questions.extend(list(case.must_answer_questions))
        return cls._dedupe_texts(questions)[:12]

    @staticmethod
    def _dedupe_texts(items: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in items:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    @staticmethod
    def _first_meaningful_text(*items: Any) -> str:
        for item in items:
            text = str(item or "").strip()
            if text:
                return text
        return "暂无明确事实，需讨论补齐。"

    @classmethod
    def _derive_leader_rank_payload(
        cls,
        *,
        playbook_context: dict[str, Any],
        playbook_match_score: dict[str, Any],
        behavior_profile: dict[str, Any],
        existing: CandidateCase | None,
    ) -> dict[str, Any]:
        if existing is not None and existing.runtime_snapshot.leader_rank:
            payload = cls._as_dict(existing.runtime_snapshot.leader_rank)
        else:
            payload = {}
        if playbook_context.get("leader_score") is not None:
            payload["leader_score"] = playbook_context.get("leader_score")
        if playbook_context.get("rank_in_sector") is not None:
            payload["zt_order_rank"] = playbook_context.get("rank_in_sector")
        if playbook_context.get("seal_ratio") is not None:
            payload["seal_ratio"] = playbook_context.get("seal_ratio")
        elif behavior_profile.get("board_success_rate_20d") is not None:
            payload["seal_ratio"] = behavior_profile.get("board_success_rate_20d")
        if playbook_match_score.get("qualified") is not None:
            payload["qualified"] = bool(playbook_match_score.get("qualified"))
        return payload

    def _apply_summaries(self, case: CandidateCase) -> CandidateCase:
        round_1 = [item for item in case.opinions if item.round == 1]
        round_2 = [item for item in case.opinions if item.round == 2]
        round_1_questions = self._collect_reason_points(round_1, {"question", "watch", "watchlist", "limit", "hold", "rejected"})

        case.round_1_summary = CandidateRoundSummary(
            selected_points=self._collect_reason_points(round_1, {"support", "selected"}),
            support_points=self._collect_reason_points(round_1, {"support", "selected"}),
            rejected_points=self._collect_reason_points(round_1, {"rejected", "limit", "hold", "watch", "watchlist"}),
            oppose_points=self._collect_reason_points(round_1, {"rejected", "limit", "hold", "watch", "watchlist"}),
            evidence_gaps=self._collect_evidence_gaps(round_1),
            questions_for_round_2=round_1_questions,
            theses=self._collect_theses(round_1),
            key_evidence=self._collect_key_evidence(round_1),
            challenged_points=self._collect_challenged_points(round_1),
        )
        case.round_2_summary = CandidateRoundSummary(
            resolved_points=self._collect_reason_points(round_2, {"support", "selected"}),
            remaining_disputes=self._collect_remaining_disputes(round_2),
            revision_notes=self._collect_revision_notes(round_2),
            persuasion_summary=self._collect_persuasion_summary(round_2),
        )

        contradiction_payload = self._build_case_contradictions(case)
        contradictions = [dict(item) for item in contradiction_payload.get("contradictions", [])]
        contradiction_questions = [
            str(item) for item in contradiction_payload.get("must_answer_questions", []) if str(item).strip()
        ]
        case.contradictions = contradictions
        case.contradiction_summary_lines = self._dedupe_texts(
            [str(item) for item in contradiction_payload.get("summary_lines", []) if str(item).strip()]
        )[:8]
        case.round_1_summary.questions_for_round_2 = self._dedupe_texts(round_1_questions + contradiction_questions)[:12]
        case.must_answer_questions = self._dedupe_texts(
            self._build_must_answer_questions(
                symbol=case.symbol,
                bull_case=case.bull_case,
                bear_case=case.bear_case,
                uncertainty=case.uncertainty,
            )
            + contradiction_questions
        )[:12]

        risk_opinions = [item for item in case.opinions if item.agent_id == "ashare-risk"]
        audit_opinions = [item for item in case.opinions if item.agent_id == "ashare-audit"]

        case.risk_gate = self._derive_risk_gate(risk_opinions)
        case.audit_gate = self._derive_audit_gate(audit_opinions)
        case.final_status = self._derive_final_status(case)
        case.pool_membership.watchlist = case.final_status == "watchlist"
        case.pool_membership.execution_pool = case.final_status == "selected"
        case.selected_reason = self._pick_reason(case, "selected")
        case.rejected_reason = self._pick_reason(case, "rejected")
        case.updated_at = self._now_factory().isoformat()
        return case

    @staticmethod
    def _collect_reason_points(opinions: list[CandidateOpinion], stances: set[str]) -> list[str]:
        points: list[str] = []
        for item in opinions:
            if item.stance not in stances:
                continue
            points.extend(item.reasons)
        return points[:12]

    @staticmethod
    def _collect_evidence_gaps(opinions: list[CandidateOpinion]) -> list[str]:
        gaps: list[str] = []
        for item in opinions:
            gaps.extend(item.evidence_gaps)
            if item.evidence_refs:
                continue
            label = "；".join(item.reasons) if item.reasons else f"{item.agent_id} 未提供证据引用"
            gaps.append(label)
        return gaps[:12]

    @staticmethod
    def _collect_theses(opinions: list[CandidateOpinion]) -> list[str]:
        theses = [item.thesis for item in opinions if item.thesis]
        return theses[:12]

    @staticmethod
    def _collect_key_evidence(opinions: list[CandidateOpinion]) -> list[str]:
        points: list[str] = []
        for item in opinions:
            points.extend(item.key_evidence)
        return points[:12]

    @staticmethod
    def _collect_challenged_points(opinions: list[CandidateOpinion]) -> list[str]:
        points: list[str] = []
        for item in opinions:
            points.extend(item.challenged_points)
            points.extend(item.questions_to_others)
        return points[:12]

    @staticmethod
    def _collect_revision_notes(opinions: list[CandidateOpinion]) -> list[str]:
        notes: list[str] = []
        for item in opinions:
            if item.changed:
                previous = item.previous_stance or "unknown"
                reason = "；".join(item.changed_because or item.reasons) or "已调整但未说明原因"
                notes.append(f"{item.agent_id} {previous}->{item.stance}，原因={reason}")
        return notes[:12]

    @staticmethod
    def _collect_challenge_exchange_lines(opinions: list[CandidateOpinion]) -> list[str]:
        lines: list[str] = []
        for item in opinions:
            agent_display = AGENT_DISPLAY_NAMES.get(item.agent_id, item.agent_id)
            if item.challenged_by:
                challenged_by = "、".join(AGENT_DISPLAY_NAMES.get(agent_id, agent_id) for agent_id in item.challenged_by)
                focus = "；".join(item.challenged_points or item.resolved_questions or item.changed_because or item.reasons) or "已回应质疑"
                lines.append(f"{agent_display} 回应 {challenged_by}：{focus}")
                continue
            if item.questions_to_others:
                focus = "；".join(item.questions_to_others)
                lines.append(f"{agent_display} 追问：{focus}")
        return lines[:12]

    @staticmethod
    def _collect_persuasion_summary(opinions: list[CandidateOpinion]) -> list[str]:
        lines: list[str] = []
        for item in opinions:
            if item.changed and item.challenged_by:
                reason = "；".join(item.changed_because or item.reasons) or "无"
                challenged_by = "、".join(item.challenged_by)
                lines.append(f"{item.agent_id} 在 {challenged_by} 质疑后改判为 {item.stance}，原因={reason}")
            elif item.resolved_questions:
                resolved = "；".join(item.resolved_questions)
                lines.append(f"{item.agent_id} 已回应问题：{resolved}")
        return lines[:12]

    @staticmethod
    def _collect_remaining_disputes(opinions: list[CandidateOpinion]) -> list[str]:
        disputes: list[str] = []
        for item in opinions:
            disputes.extend(item.remaining_disputes)
        if disputes:
            return disputes[:12]
        return CandidateCaseService._collect_reason_points(opinions, {"question", "watch", "watchlist", "limit", "hold", "rejected"})

    @staticmethod
    def _derive_risk_gate(opinions: list[CandidateOpinion]) -> RiskGate:
        if not opinions:
            return "pending"
        latest = opinions[-1]
        if latest.stance == "rejected":
            return "reject"
        if latest.stance in {"limit", "hold", "question", "watch", "watchlist"}:
            return "limit"
        return "allow"

    @staticmethod
    def _derive_audit_gate(opinions: list[CandidateOpinion]) -> AuditGate:
        if not opinions:
            return "pending"
        latest = opinions[-1]
        if latest.stance in {"rejected", "hold", "question"}:
            return "hold"
        return "clear"

    @staticmethod
    def _pick_reason(case: CandidateCase, status: str) -> str | None:
        if status == "selected":
            return case.runtime_snapshot.summary or (case.round_2_summary.resolved_points or case.round_1_summary.selected_points or [None])[0]
        if status == "rejected":
            return (case.round_2_summary.remaining_disputes or case.round_1_summary.rejected_points or [case.runtime_snapshot.summary])[0]
        return None

    @staticmethod
    def _derive_final_status(case: CandidateCase) -> FinalStatus:
        latest_round = 2 if CandidateCaseService._round_agent_coverage(case, 2) else 1
        latest_opinions = CandidateCaseService._latest_round_opinions(case, latest_round)
        latest_stances = [
            latest_opinions[agent_id].stance
            for agent_id in DISCUSSION_AGENT_IDS
            if agent_id in latest_opinions
        ]
        selected_votes = sum(1 for stance in latest_stances if stance in {"support", "selected"})
        watch_votes = sum(1 for stance in latest_stances if stance in {"watch", "watchlist"})
        limit_votes = sum(1 for stance in latest_stances if stance == "limit")
        question_votes = sum(1 for stance in latest_stances if stance in {"question", "hold"})
        rejected_votes = sum(1 for stance in latest_stances if stance == "rejected")
        runtime_positive = case.runtime_snapshot.action == "BUY"

        if case.risk_gate == "reject":
            return "rejected"
        if case.audit_gate == "hold":
            if rejected_votes >= 2 and selected_votes == 0:
                return "rejected"
            return "watchlist"
        if case.audit_gate == "clear" and case.risk_gate in {"allow", "limit"} and selected_votes >= 2:
            return "selected"
        if selected_votes >= 1 or watch_votes >= 2 or (runtime_positive and (watch_votes + limit_votes) >= 2):
            return "watchlist"
        if rejected_votes >= 2 and selected_votes == 0:
            return "rejected"
        if question_votes >= 2 and selected_votes == 0:
            return "rejected"
        return "watchlist" if case.pool_membership.focus_pool else "rejected"

    @staticmethod
    def _round_agent_coverage(case: CandidateCase, round_number: int) -> bool:
        required = DISCUSSION_AGENT_IDS
        present = {item.agent_id for item in case.opinions if item.round == round_number}
        return required.issubset(present)

    @staticmethod
    def _latest_round_opinions(case: CandidateCase, round_number: int) -> dict[str, CandidateOpinion]:
        latest: dict[str, CandidateOpinion] = {}
        for opinion in case.opinions:
            if opinion.round != round_number:
                continue
            latest[opinion.agent_id] = opinion
        return latest

    @staticmethod
    def _opinion_has_substantive_round_2_response(opinion: CandidateOpinion) -> bool:
        markers = (
            opinion.challenged_by,
            opinion.challenged_points,
            opinion.questions_to_others,
            opinion.changed_because,
            opinion.resolved_questions,
            opinion.remaining_disputes,
        )
        if any(marker for marker in markers):
            return True
        if CandidateCaseService._has_meaningful_items(opinion.key_evidence):
            return True
        if CandidateCaseService._has_meaningful_items(opinion.evidence_gaps):
            return True
        if CandidateCaseService._is_meaningful_text(opinion.thesis):
            return True
        if opinion.previous_stance is not None:
            return True
        if opinion.changed is not None and opinion.challenged_by:
            return True
        return False

    @staticmethod
    def _has_meaningful_items(items: list[str]) -> bool:
        return any(CandidateCaseService._is_meaningful_text(item) for item in items)

    @staticmethod
    def _is_meaningful_text(value: str | None) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        return text.lower() not in {"test", "todo", "n/a", "na", "none", "null"}
