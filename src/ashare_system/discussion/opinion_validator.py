"""discussion opinion 校验与归一化 helper。"""

from __future__ import annotations

from typing import Any

from .contracts import (
    DISCUSSION_AGENT_IDS,
    ROUND_2_SUBSTANTIVE_FIELDS,
    STANCE_ALIAS_MAP,
    DiscussionOpinion,
    OpinionBatchValidationResult,
    OpinionValidationIssue,
    OpinionStance,
)


def normalize_opinion_stance(value: str | None) -> OpinionStance | str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return STANCE_ALIAS_MAP.get(text, text)


def is_meaningful_text(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return text.lower() not in {"test", "todo", "n/a", "na", "none", "null"}


def has_meaningful_items(items: list[str]) -> bool:
    return any(is_meaningful_text(item) for item in items)


def has_substantive_round_2_response(opinion: DiscussionOpinion) -> bool:
    for field_name in ROUND_2_SUBSTANTIVE_FIELDS:
        value = getattr(opinion, field_name, None)
        if isinstance(value, list) and has_meaningful_items([str(item) for item in value]):
            return True
        if isinstance(value, str) and is_meaningful_text(value):
            return True
        if field_name == "previous_stance" and value is not None:
            return True
        if field_name == "changed" and value is not None and opinion.challenged_by:
            return True
    return False


def validate_opinion_payload(
    payload: dict[str, Any] | DiscussionOpinion,
    *,
    expected_round: int | None = None,
    expected_agent_id: str | None = None,
) -> tuple[DiscussionOpinion | None, list[OpinionValidationIssue]]:
    issues: list[OpinionValidationIssue] = []
    raw = payload.model_dump() if isinstance(payload, DiscussionOpinion) else dict(payload)
    raw["stance"] = normalize_opinion_stance(raw.get("stance"))
    previous_stance = raw.get("previous_stance")
    if previous_stance:
        raw["previous_stance"] = normalize_opinion_stance(previous_stance)

    opinion: DiscussionOpinion | None = None
    try:
        opinion = DiscussionOpinion.model_validate(raw)
    except Exception as exc:
        issues.append(
            OpinionValidationIssue(
                level="error",
                code="invalid_schema",
                message=str(exc),
                field="payload",
                case_id=str(raw.get("case_id") or "") or None,
                agent_id=str(raw.get("agent_id") or "") or None,
                round=int(raw["round"]) if raw.get("round") else None,
            )
        )
        return None, issues

    if expected_round is not None and opinion.round != expected_round:
        issues.append(
            OpinionValidationIssue(
                level="error",
                code="round_mismatch",
                message=f"expected round={expected_round}, got {opinion.round}",
                field="round",
                case_id=opinion.case_id or None,
                agent_id=opinion.agent_id,
                round=opinion.round,
            )
        )
    if expected_agent_id is not None and opinion.agent_id != expected_agent_id:
        issues.append(
            OpinionValidationIssue(
                level="error",
                code="agent_mismatch",
                message=f"expected agent_id={expected_agent_id}, got {opinion.agent_id}",
                field="agent_id",
                case_id=opinion.case_id or None,
                agent_id=opinion.agent_id,
                round=opinion.round,
            )
        )
    if opinion.agent_id not in DISCUSSION_AGENT_IDS:
        issues.append(
            OpinionValidationIssue(
                level="warning",
                code="unknown_discussion_agent",
                message=f"agent_id={opinion.agent_id} 不在当前 discussion 主持代理列表内",
                field="agent_id",
                case_id=opinion.case_id or None,
                agent_id=opinion.agent_id,
                round=opinion.round,
            )
        )
    if not has_meaningful_items(opinion.reasons):
        issues.append(
            OpinionValidationIssue(
                level="error",
                code="reasons_missing",
                message="reasons 至少需要 1 条有效文本",
                field="reasons",
                case_id=opinion.case_id or None,
                agent_id=opinion.agent_id,
                round=opinion.round,
            )
        )
    if not has_meaningful_items(opinion.evidence_refs):
        issues.append(
            OpinionValidationIssue(
                level="warning",
                code="evidence_refs_missing",
                message="evidence_refs 为空，当前主链会把该意见视为证据较弱",
                field="evidence_refs",
                case_id=opinion.case_id or None,
                agent_id=opinion.agent_id,
                round=opinion.round,
            )
        )
    if opinion.round == 2 and not has_substantive_round_2_response(opinion):
        issues.append(
            OpinionValidationIssue(
                level="error",
                code="round_2_not_substantive",
                message="Round 2 opinion 未包含当前实现要求的实质回应字段",
                field="round",
                case_id=opinion.case_id or None,
                agent_id=opinion.agent_id,
                round=opinion.round,
            )
        )
    return opinion, issues


def validate_opinion_batch(
    payloads: list[dict[str, Any] | DiscussionOpinion],
    *,
    expected_round: int | None = None,
    expected_agent_id: str | None = None,
    expected_case_ids: list[str] | None = None,
) -> OpinionBatchValidationResult:
    normalized_items: list[DiscussionOpinion] = []
    issues: list[OpinionValidationIssue] = []
    duplicate_keys: list[str] = []
    substantive_round_2_case_ids: list[str] = []
    seen_keys: set[str] = set()

    for raw in payloads:
        opinion, item_issues = validate_opinion_payload(
            raw,
            expected_round=expected_round,
            expected_agent_id=expected_agent_id,
        )
        issues.extend(item_issues)
        if opinion is None:
            continue
        key = f"{opinion.case_id}:{opinion.round}:{opinion.agent_id}"
        if key in seen_keys:
            duplicate_keys.append(key)
            issues.append(
                OpinionValidationIssue(
                    level="error",
                    code="duplicate_case_round_agent",
                    message=f"重复 opinion 键: {key}",
                    field="case_id",
                    case_id=opinion.case_id or None,
                    agent_id=opinion.agent_id,
                    round=opinion.round,
                )
            )
        seen_keys.add(key)
        normalized_items.append(opinion)
        if opinion.round == 2 and has_substantive_round_2_response(opinion):
            substantive_round_2_case_ids.append(opinion.case_id)

    covered_case_ids = list(dict.fromkeys(item.case_id for item in normalized_items if item.case_id))
    expected_case_id_list = list(dict.fromkeys(expected_case_ids or []))
    missing_case_ids = [case_id for case_id in expected_case_id_list if case_id not in covered_case_ids]
    for case_id in missing_case_ids:
        issues.append(
            OpinionValidationIssue(
                level="error",
                code="missing_case_coverage",
                message=f"缺少 case_id={case_id} 的 opinion",
                field="case_id",
                case_id=case_id,
                agent_id=expected_agent_id,
                round=expected_round,
            )
        )

    error_count = sum(1 for item in issues if item.level == "error")
    summary_lines = [
        f"opinions={len(payloads)} normalized={len(normalized_items)} errors={error_count} warnings={len(issues) - error_count}",
    ]
    if missing_case_ids:
        summary_lines.append("缺少覆盖: " + "、".join(missing_case_ids))
    if duplicate_keys:
        summary_lines.append("重复键: " + "、".join(duplicate_keys))

    return OpinionBatchValidationResult(
        ok=error_count == 0,
        normalized_items=normalized_items,
        issues=issues,
        covered_case_ids=covered_case_ids,
        missing_case_ids=missing_case_ids,
        duplicate_keys=duplicate_keys,
        substantive_round_2_case_ids=list(dict.fromkeys(substantive_round_2_case_ids)),
        summary_lines=summary_lines,
    )

