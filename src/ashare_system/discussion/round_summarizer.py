"""discussion round summary / board 聚合 helper。"""

from __future__ import annotations

from typing import Iterable

from .contracts import (
    AGENT_DISPLAY_NAMES,
    DISCUSSION_AGENT_IDS,
    AuditGate,
    DiscussionCaseRecord,
    DiscussionOpinion,
    DiscussionRoundSummary,
    FinalStatus,
    RiskGate,
)


def summarize_case(case: DiscussionCaseRecord | dict) -> DiscussionCaseRecord:
    record = case if isinstance(case, DiscussionCaseRecord) else DiscussionCaseRecord.model_validate(case)
    round_1 = [item for item in record.opinions if item.round == 1]
    round_2 = [item for item in record.opinions if item.round == 2]

    # S1.2: 提取反证与质询目标
    counter_evidence = _collect_reason_points(round_1, {"rejected", "oppose", "question", "limit", "hold"})
    inquiry_targets = []
    for op in round_1:
        if op.stance in {"rejected", "limit", "hold", "question"}:
            # 针对负面观点，生成质询目标 (通常指向策略或研究)
            for reason in op.reasons[:2]:
                inquiry_targets.append({
                    "from_agent": op.agent_id,
                    "to_agent": "ashare-strategy" if op.agent_id != "ashare-strategy" else "ashare-research",
                    "question": reason,
                    "case_id": record.case_id
                })

    record.round_1_summary = DiscussionRoundSummary(
        selected_points=_collect_reason_points(round_1, {"support", "selected"}),
        support_points=_collect_reason_points(round_1, {"support", "selected"}),
        rejected_points=_collect_reason_points(round_1, {"rejected", "limit", "hold", "watch", "watchlist"}),
        oppose_points=_collect_reason_points(round_1, {"rejected", "limit", "hold", "watch", "watchlist"}),
        evidence_gaps=_collect_evidence_gaps(round_1),
        questions_for_round_2=_collect_reason_points(round_1, {"question", "watch", "watchlist", "limit", "hold", "rejected"}),
        theses=_collect_theses(round_1),
        key_evidence=_collect_key_evidence(round_1),
        challenged_points=_collect_challenged_points(round_1),
        counter_evidence=counter_evidence,
        inquiry_targets=inquiry_targets,
    )
    record.round_2_summary = DiscussionRoundSummary(
        resolved_points=_collect_reason_points(round_2, {"support", "selected"}),
        remaining_disputes=_collect_remaining_disputes(round_2),
        revision_notes=_collect_revision_notes(round_2),
        persuasion_summary=_collect_persuasion_summary(round_2),
    )

    risk_opinions = [item for item in record.opinions if item.agent_id == "ashare-risk"]
    audit_opinions = [item for item in record.opinions if item.agent_id == "ashare-audit"]
    record.risk_gate = derive_risk_gate(risk_opinions)
    record.audit_gate = derive_audit_gate(audit_opinions)
    record.final_status = derive_final_status(record)
    record.pool_membership.watchlist = record.final_status == "watchlist"
    record.pool_membership.execution_pool = record.final_status == "selected"
    record.selected_reason = pick_reason(record, "selected")
    record.rejected_reason = pick_reason(record, "rejected")
    return record


def summarize_cases(cases: Iterable[DiscussionCaseRecord | dict]) -> list[DiscussionCaseRecord]:
    return [summarize_case(item) for item in cases]


def build_trade_date_summary(cases: Iterable[DiscussionCaseRecord | dict], trade_date: str) -> dict:
    items = summarize_cases(cases)
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
        summary_item = _summary_item(item)
        status_groups[item.final_status].append(summary_item)
        risk_counts[item.risk_gate] += 1
        audit_counts[item.audit_gate] += 1
        if round_agent_coverage(item, 1):
            round_coverage["round_1_ready"] += 1
        if round_agent_coverage(item, 2):
            round_coverage["round_2_ready"] += 1
        if item.round_1_summary.questions_for_round_2 or item.audit_gate == "hold" or item.risk_gate == "limit":
            round_coverage["needs_round_2"] += 1
            if round_2_has_substantive_response(item):
                round_coverage["round_2_substantive_ready"] += 1
            else:
                substantive_gap_case_ids.append(item.case_id)

    candidate_pool = [_summary_item(item) for item in items]
    summary_lines = [
        f"候选池：{len(items)}只",
        "候选名单：" + "、".join(f"{item['symbol']} {item['name'] or item['symbol']}" for item in candidate_pool) if candidate_pool else "候选名单：无",
        f"结论分布：selected={len(status_groups['selected'])} / watchlist={len(status_groups['watchlist'])} / rejected={len(status_groups['rejected'])}",
        (
            f"Gate 状态：risk allow={risk_counts['allow']} / limit={risk_counts['limit']} / reject={risk_counts['reject']}；"
            f"audit clear={audit_counts['clear']} / hold={audit_counts['hold']}"
        ),
    ]
    disputed_cases = [item for item in items if item.round_2_summary.remaining_disputes or item.round_1_summary.questions_for_round_2]
    revised_cases = [item for item in items if item.round_2_summary.revision_notes]
    if disputed_cases:
        summary_lines.append("争议焦点：" + "；".join(_controversy_line(item) for item in disputed_cases[:5]))
    if substantive_gap_case_ids:
        gap_case_ids = set(substantive_gap_case_ids)
        summary_lines.append("二轮待实质回应：" + "；".join(_round_2_gap_line(item) for item in items if item.case_id in gap_case_ids))
    
    # S1.2: 明确质询要求
    all_inquiry_targets = []
    for item in items:
        all_inquiry_targets.extend(item.round_1_summary.inquiry_targets)
    if all_inquiry_targets:
        summary_lines.append("本轮质询要求：" + "；".join(
            f"@{it['to_agent']} 应回应 {it['from_agent']} 对 {it['case_id']} 的质询: {it['question']}"
            for it in all_inquiry_targets[:8]
        ))

    if revised_cases:
        summary_lines.append("已修正观点：" + "；".join(
            f"{item.symbol} {item.name or item.symbol} -> {item.round_2_summary.revision_notes[0]}"
            for item in revised_cases[:5]
        ))
    if status_groups["selected"]:
        summary_lines.append("入选：" + "；".join(_summary_line(item) for item in status_groups["selected"]))
    if status_groups["watchlist"]:
        summary_lines.append("观察：" + "；".join(_summary_line(item) for item in status_groups["watchlist"]))
    if status_groups["rejected"]:
        summary_lines.append("淘汰：" + "；".join(_summary_line(item) for item in status_groups["rejected"]))

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
        "controversy_summary_lines": [_controversy_line(item) for item in disputed_cases[:5]],
        "round_2_guidance": [_round_2_gap_line(item) for item in items if item.case_id in set(substantive_gap_case_ids)][:5],
        "all_inquiry_targets": all_inquiry_targets,
        "candidate_pool_lines": [_summary_line(item) for item in candidate_pool],
        "selected_lines": [_summary_line(item) for item in status_groups["selected"]],
        "watchlist_lines": [_summary_line(item) for item in status_groups["watchlist"]],
        "rejected_lines": [_summary_line(item) for item in status_groups["rejected"]],
        "summary_lines": summary_lines,
        "summary_text": "\n".join(summary_lines),
        "disputed_case_ids": [item.case_id for item in disputed_cases],
        "revised_case_ids": [item.case_id for item in revised_cases],
        "selected": status_groups["selected"],
        "watchlist": status_groups["watchlist"],
        "rejected": status_groups["rejected"],
    }


def build_reason_board(cases: Iterable[DiscussionCaseRecord | dict], trade_date: str) -> dict:
    items = summarize_cases(cases)
    groups = {"selected": [], "watchlist": [], "rejected": []}
    for item in items:
        groups[item.final_status].append(build_reason_item(item))
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


def build_reason_item(case: DiscussionCaseRecord | dict) -> dict:
    item = summarize_case(case)
    latest_opinions: dict[str, dict] = {}
    for opinion in item.opinions:
        latest_opinions[opinion.agent_id] = {
            "round": opinion.round,
            "stance": opinion.stance,
            "confidence": opinion.confidence,
            "reasons": opinion.reasons,
            "evidence_refs": opinion.evidence_refs,
            "recorded_at": opinion.recorded_at,
        }
    return {
        "case_id": item.case_id,
        "symbol": item.symbol,
        "name": item.name,
        "rank": item.runtime_snapshot.rank,
        "selection_score": item.runtime_snapshot.selection_score,
        "action": item.runtime_snapshot.action,
        "final_status": item.final_status,
        "headline_reason": item.selected_reason or item.rejected_reason or item.runtime_snapshot.summary,
        "selected_reason": item.selected_reason,
        "rejected_reason": item.rejected_reason,
        "risk_gate": item.risk_gate,
        "audit_gate": item.audit_gate,
        "pool_membership": item.pool_membership.model_dump(),
        "runtime_snapshot": {
            "summary": item.runtime_snapshot.summary,
            "score_breakdown": item.runtime_snapshot.score_breakdown,
            "market_snapshot": item.runtime_snapshot.market_snapshot,
            "runtime_report_ref": item.runtime_snapshot.runtime_report_ref,
        },
        "discussion": {
            "selected_points": item.round_1_summary.selected_points,
            "support_points": item.round_1_summary.support_points or item.round_1_summary.selected_points,
            "rejected_points": item.round_1_summary.rejected_points,
            "oppose_points": item.round_1_summary.oppose_points or item.round_1_summary.rejected_points,
            "evidence_gaps": item.round_1_summary.evidence_gaps,
            "questions_for_round_2": item.round_1_summary.questions_for_round_2,
            "theses": item.round_1_summary.theses,
            "key_evidence": item.round_1_summary.key_evidence,
            "challenged_points": item.round_1_summary.challenged_points,
            "resolved_points": item.round_2_summary.resolved_points,
            "remaining_disputes": item.round_2_summary.remaining_disputes,
            "challenge_exchange_lines": _collect_challenge_exchange_lines(item.opinions),
            "revision_notes": item.round_2_summary.revision_notes,
            "persuasion_summary": item.round_2_summary.persuasion_summary,
        },
        "latest_opinions": latest_opinions,
        "opinion_count": len(item.opinions),
        "updated_at": item.updated_at,
    }


def round_agent_coverage(case: DiscussionCaseRecord | dict, round_number: int) -> bool:
    item = case if isinstance(case, DiscussionCaseRecord) else DiscussionCaseRecord.model_validate(case)
    present = {opinion.agent_id for opinion in item.opinions if opinion.round == round_number}
    return DISCUSSION_AGENT_IDS.issubset(present)


def round_2_has_substantive_response(case: DiscussionCaseRecord | dict) -> bool:
    item = case if isinstance(case, DiscussionCaseRecord) else DiscussionCaseRecord.model_validate(case)
    round_2_opinions = _latest_round_opinions(item, 2)
    if not DISCUSSION_AGENT_IDS.issubset(round_2_opinions):
        return False
    return all(_opinion_has_substantive_round_2_response(round_2_opinions[agent_id]) for agent_id in DISCUSSION_AGENT_IDS)


def derive_risk_gate(opinions: list[DiscussionOpinion]) -> RiskGate:
    if not opinions:
        return "pending"
    # S1.2: 只有显式的 allow 且没有 reject 才放行
    stances = {op.stance for op in opinions}
    if "rejected" in stances:
        return "reject"
    if any(s in {"limit", "hold", "question"} for s in stances):
        return "limit"
    if "support" in stances or "selected" in stances:
        return "allow"
    return "pending"


def derive_audit_gate(opinions: list[DiscussionOpinion]) -> AuditGate:
    if not opinions:
        return "pending"
    # S1.2: 审计需要所有回合都没有 hold 或 question
    stances = {op.stance for op in opinions}
    if any(s in {"rejected", "hold", "question"} for s in stances):
        return "hold"
    return "clear"


def pick_reason(case: DiscussionCaseRecord, status: str) -> str | None:
    if status == "selected":
        return case.runtime_snapshot.summary or (case.round_2_summary.resolved_points or case.round_1_summary.selected_points or [None])[0]
    if status == "rejected":
        return (case.round_2_summary.remaining_disputes or case.round_1_summary.rejected_points or [case.runtime_snapshot.summary])[0]
    return None


def derive_final_status(case: DiscussionCaseRecord) -> FinalStatus:
    latest_round = 2 if round_agent_coverage(case, 2) else 1
    latest_opinions = _latest_round_opinions(case, latest_round)
    latest_stances = [latest_opinions[agent_id].stance for agent_id in DISCUSSION_AGENT_IDS if agent_id in latest_opinions]
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


def _summary_item(case: DiscussionCaseRecord) -> dict:
    return {
        "case_id": case.case_id,
        "symbol": case.symbol,
        "name": case.name,
        "rank": case.runtime_snapshot.rank,
        "selection_score": case.runtime_snapshot.selection_score,
        "risk_gate": case.risk_gate,
        "audit_gate": case.audit_gate,
        "reason": case.selected_reason or case.rejected_reason or case.runtime_snapshot.summary,
    }


def _collect_reason_points(opinions: list[DiscussionOpinion], stances: set[str]) -> list[str]:
    points: list[str] = []
    for item in opinions:
        if item.stance not in stances:
            continue
        points.extend(item.reasons)
    return points[:12]


def _collect_evidence_gaps(opinions: list[DiscussionOpinion]) -> list[str]:
    gaps: list[str] = []
    for item in opinions:
        gaps.extend(item.evidence_gaps)
        if item.evidence_refs:
            continue
        label = "；".join(item.reasons) if item.reasons else f"{item.agent_id} 未提供证据引用"
        gaps.append(label)
    return gaps[:12]


def _collect_theses(opinions: list[DiscussionOpinion]) -> list[str]:
    return [item.thesis for item in opinions if item.thesis][:12]


def _collect_key_evidence(opinions: list[DiscussionOpinion]) -> list[str]:
    points: list[str] = []
    for item in opinions:
        points.extend(item.key_evidence)
    return points[:12]


def _collect_challenged_points(opinions: list[DiscussionOpinion]) -> list[str]:
    points: list[str] = []
    for item in opinions:
        points.extend(item.challenged_points)
        points.extend(item.questions_to_others)
    return points[:12]


def _collect_revision_notes(opinions: list[DiscussionOpinion]) -> list[str]:
    notes: list[str] = []
    for item in opinions:
        if item.changed:
            previous = item.previous_stance or "unknown"
            reason = "；".join(item.changed_because or item.reasons) or "已调整但未说明原因"
            notes.append(f"{item.agent_id} {previous}->{item.stance}，原因={reason}")
    return notes[:12]


def _collect_challenge_exchange_lines(opinions: list[DiscussionOpinion]) -> list[str]:
    lines: list[str] = []
    for item in opinions:
        agent_display = AGENT_DISPLAY_NAMES.get(item.agent_id, item.agent_id)
        if item.challenged_by:
            challenged_by = "、".join(AGENT_DISPLAY_NAMES.get(agent_id, agent_id) for agent_id in item.challenged_by)
            focus = "；".join(item.challenged_points or item.resolved_questions or item.changed_because or item.reasons) or "已回应质疑"
            lines.append(f"{agent_display} 回应 {challenged_by}：{focus}")
            continue
        if item.questions_to_others:
            lines.append(f"{agent_display} 追问：{'；'.join(item.questions_to_others)}")
    return lines[:12]


def _collect_persuasion_summary(opinions: list[DiscussionOpinion]) -> list[str]:
    lines: list[str] = []
    for item in opinions:
        if item.changed and item.challenged_by:
            reason = "；".join(item.changed_because or item.reasons) or "无"
            challenged_by = "、".join(item.challenged_by)
            lines.append(f"{item.agent_id} 在 {challenged_by} 质疑后改判为 {item.stance}，原因={reason}")
        elif item.resolved_questions:
            lines.append(f"{item.agent_id} 已回应问题：{'；'.join(item.resolved_questions)}")
    return lines[:12]


def _collect_remaining_disputes(opinions: list[DiscussionOpinion]) -> list[str]:
    disputes: list[str] = []
    for item in opinions:
        disputes.extend(item.remaining_disputes)
    if disputes:
        return disputes[:12]
    return _collect_reason_points(opinions, {"question", "watch", "watchlist", "limit", "hold", "rejected"})


def _summary_line(item: dict) -> str:
    reason = item.get("reason") or item.get("headline_reason") or item.get("selected_reason") or item.get("rejected_reason") or ""
    return (
        f"{item['symbol']} {item.get('name') or item['symbol']} | 排名={item.get('rank')} | "
        f"分数={item.get('selection_score')} | 风控={item.get('risk_gate')} 审计={item.get('audit_gate')} | 理由={reason}"
    )


def _controversy_line(case: DiscussionCaseRecord) -> str:
    dispute = (case.round_2_summary.remaining_disputes or case.round_1_summary.questions_for_round_2 or ["暂无显式争议"])[0]
    return f"{case.symbol} {case.name or case.symbol}：{dispute}"


def _round_2_gap_line(case: DiscussionCaseRecord) -> str:
    issue = (case.round_2_summary.remaining_disputes or case.round_1_summary.questions_for_round_2 or ["需要回应前序质疑"])[0]
    return f"{case.symbol} {case.name or case.symbol}：需补充被谁质疑、回应了什么、是否改判或剩余争议。当前焦点={issue}"


def _latest_round_opinions(case: DiscussionCaseRecord, round_number: int) -> dict[str, DiscussionOpinion]:
    latest: dict[str, DiscussionOpinion] = {}
    for opinion in case.opinions:
        if opinion.round != round_number:
            continue
        latest[opinion.agent_id] = opinion
    return latest


def _opinion_has_substantive_round_2_response(opinion: DiscussionOpinion) -> bool:
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
    if any(_is_meaningful_text(item) for item in opinion.key_evidence):
        return True
    if any(_is_meaningful_text(item) for item in opinion.evidence_gaps):
        return True
    if _is_meaningful_text(opinion.thesis):
        return True
    if opinion.previous_stance is not None:
        return True
    if opinion.changed is not None and opinion.challenged_by:
        return True
    return False


def _is_meaningful_text(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return text.lower() not in {"test", "todo", "n/a", "na", "none", "null"}

