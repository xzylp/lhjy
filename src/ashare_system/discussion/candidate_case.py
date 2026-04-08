"""候选股票 case 持久化与 runtime 同步。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field

from ..logging_config import get_logger

logger = get_logger("discussion.cases")

RiskGate = Literal["pending", "allow", "limit", "reject"]
AuditGate = Literal["pending", "clear", "hold"]
FinalStatus = Literal["selected", "watchlist", "rejected"]
OpinionConfidence = Literal["high", "medium", "low"]
OpinionStance = Literal["support", "watch", "limit", "hold", "question", "rejected", "selected", "watchlist"]
AGENT_DISPLAY_NAMES = {
    "ashare-research": "研究",
    "ashare-strategy": "策略",
    "ashare-risk": "风控",
    "ashare-audit": "审计",
    "ashare-runtime": "运行",
    "ashare-executor": "执行",
}
DISCUSSION_AGENT_IDS = frozenset({"ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"})


class PoolMembership(BaseModel):
    base_pool: bool = True
    focus_pool: bool = False
    execution_pool: bool = False
    watchlist: bool = False


class CandidateRuntimeSnapshot(BaseModel):
    rank: int
    selection_score: float
    action: str
    score_breakdown: dict[str, int | float] = Field(default_factory=dict)
    summary: str = ""
    market_snapshot: dict = Field(default_factory=dict)
    runtime_report_ref: str | None = None


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
    updated_at: str


class CandidateCaseService:
    """候选 case 服务。"""

    def __init__(self, storage_path: Path, now_factory: Callable[[], datetime] | None = None) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_factory = now_factory or datetime.now

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
                    score_breakdown=item.get("score_breakdown", {}),
                    summary=item.get("summary", ""),
                    market_snapshot=item.get("market_snapshot", {}),
                    runtime_report_ref=job_id,
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
            status_groups[item.final_status].append(
                {
                    "case_id": item.case_id,
                    "symbol": item.symbol,
                    "name": item.name,
                    "rank": item.runtime_snapshot.rank,
                    "selection_score": item.runtime_snapshot.selection_score,
                    "risk_gate": item.risk_gate,
                    "audit_gate": item.audit_gate,
                    "reason": item.selected_reason or item.rejected_reason or item.runtime_snapshot.summary,
                }
            )
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
        candidate_pool = [
            {
                "case_id": item.case_id,
                "symbol": item.symbol,
                "name": item.name,
                "rank": item.runtime_snapshot.rank,
                "selection_score": item.runtime_snapshot.selection_score,
                "risk_gate": item.risk_gate,
                "audit_gate": item.audit_gate,
                "reason": item.selected_reason or item.rejected_reason or item.runtime_snapshot.summary,
            }
            for item in items
        ]
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
        disputed_cases = [item for item in items if item.round_2_summary.remaining_disputes or item.round_1_summary.questions_for_round_2]
        revised_cases = [item for item in items if item.round_2_summary.revision_notes]
        if disputed_cases:
            summary_lines.append(
                "争议焦点：" + "；".join(
                    f"{item.symbol} {item.name or item.symbol} -> "
                    f"{(item.round_2_summary.remaining_disputes or item.round_1_summary.questions_for_round_2)[0]}"
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
        return {
            "trade_date": trade_date,
            "case_count": len(items),
            "candidate_pool": candidate_pool,
            "selected_count": len(status_groups["selected"]),
            "watchlist_count": len(status_groups["watchlist"]),
            "rejected_count": len(status_groups["rejected"]),
            "risk_gate_counts": risk_counts,
            "audit_gate_counts": audit_counts,
            "round_coverage": round_coverage,
            "substantive_gap_case_ids": substantive_gap_case_ids,
            "controversy_summary_lines": [self._controversy_line(item) for item in disputed_cases[:5]],
            "round_2_guidance": [self._round_2_gap_line(item) for item in items if item.case_id in set(substantive_gap_case_ids)][:5],
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
        return {
            "trade_date": trade_date,
            "case_count": board["case_count"],
            "selected_count": board["selected_count"],
            "selection_count": board["selected_count"],
            "watchlist_count": board["watchlist_count"],
            "rejected_count": board["rejected_count"],
            "overview_lines": overview_lines,
            "debate_focus_lines": debate_focus_lines,
            "challenge_exchange_lines": self._build_challenge_exchange_lines(board),
            "persuasion_summary_lines": persuasion_summary_lines,
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

        return {
            "trade_date": trade_date,
            "candidate_pool": [self._pool_item(item) for item in items],
            "focus_pool": [self._pool_item(item) for item in focus_cases],
            "execution_pool": [self._pool_item(item) for item in execution_cases],
            "watchlist": [self._pool_item(item) for item in items if item.final_status == "watchlist"],
            "rejected": [self._pool_item(item) for item in items if item.final_status == "rejected"],
            "counts": {
                "candidate_pool": len(items),
                "focus_pool": len(focus_cases),
                "execution_pool": len(execution_cases),
                "watchlist": sum(1 for item in items if item.final_status == "watchlist"),
                "rejected": sum(1 for item in items if item.final_status == "rejected"),
            },
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
            "reason": case.selected_reason or case.rejected_reason or case.runtime_snapshot.summary,
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
                "summary": case.runtime_snapshot.summary,
                "score_breakdown": case.runtime_snapshot.score_breakdown,
                "market_snapshot": case.runtime_snapshot.market_snapshot,
                "runtime_report_ref": case.runtime_snapshot.runtime_report_ref,
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
            },
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
    def _controversy_line(case: CandidateCase) -> str:
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

    def _apply_summaries(self, case: CandidateCase) -> CandidateCase:
        round_1 = [item for item in case.opinions if item.round == 1]
        round_2 = [item for item in case.opinions if item.round == 2]

        case.round_1_summary = CandidateRoundSummary(
            selected_points=self._collect_reason_points(round_1, {"support", "selected"}),
            support_points=self._collect_reason_points(round_1, {"support", "selected"}),
            rejected_points=self._collect_reason_points(round_1, {"rejected", "limit", "hold", "watch", "watchlist"}),
            oppose_points=self._collect_reason_points(round_1, {"rejected", "limit", "hold", "watch", "watchlist"}),
            evidence_gaps=self._collect_evidence_gaps(round_1),
            questions_for_round_2=self._collect_reason_points(round_1, {"question", "watch", "watchlist", "limit", "hold", "rejected"}),
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
