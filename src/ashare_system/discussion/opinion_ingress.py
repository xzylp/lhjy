"""OpenClaw discussion opinion ingress adapter。"""

from __future__ import annotations

from typing import Any

from .candidate_case import CandidateOpinion
from .contracts import DiscussionOpinion
from .opinion_validator import validate_opinion_batch

OPINION_CONTAINER_KEYS: tuple[str, ...] = ("opinions", "items", "results")
OPINION_NESTED_KEYS: tuple[str, ...] = ("output", "data", "payload", "result")
OPINION_LIKE_KEYS: frozenset[str] = frozenset(
    {
        "case_id",
        "agent_id",
        "stance",
        "confidence",
        "reasons",
        "evidence_refs",
        "thesis",
        "key_evidence",
        "evidence_gaps",
        "questions_to_others",
        "challenged_by",
        "challenged_points",
        "previous_stance",
        "changed",
        "changed_because",
        "resolved_questions",
        "remaining_disputes",
        "recorded_at",
    }
)


def extract_opinion_items(payload: Any) -> list[dict[str, Any]]:
    """把外部 agent 返回 payload 解成 opinion dict 列表。

    当前只支持以下输入形态：
    - 直接 list[dict]
    - 单条 opinion dict
    - {"opinions": [...]}
    - {"items": [...]}
    - {"results": [...]}
    - {"output": ...} / {"data": ...} / {"payload": ...} / {"result": ...}
    """

    if payload is None:
        return []
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    if _is_opinion_like(payload):
        return [dict(payload)]
    for key in OPINION_CONTAINER_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
    for key in OPINION_NESTED_KEYS:
        value = payload.get(key)
        items = extract_opinion_items(value)
        if items:
            return items
    return []


def normalize_openclaw_opinion_payloads(
    payload: Any,
    *,
    expected_round: int | None = None,
    expected_agent_id: str | None = None,
    case_id_map: dict[str, str] | None = None,
    default_case_id: str | None = None,
) -> list[dict[str, Any]]:
    items = extract_opinion_items(payload)
    normalized: list[dict[str, Any]] = []
    for item in items:
        current = dict(item)
        if expected_round is not None and current.get("round") is None:
            current["round"] = expected_round
        if expected_agent_id is not None and not current.get("agent_id"):
            current["agent_id"] = expected_agent_id
        if not current.get("case_id"):
            mapped_case_id = _resolve_case_id(current, case_id_map=case_id_map, default_case_id=default_case_id)
            if mapped_case_id:
                current["case_id"] = mapped_case_id
        normalized.append(current)
    return normalized


def adapt_openclaw_opinion_payload(
    payload: Any,
    *,
    expected_round: int | None = None,
    expected_agent_id: str | None = None,
    expected_case_ids: list[str] | None = None,
    case_id_map: dict[str, str] | None = None,
    default_case_id: str | None = None,
) -> dict[str, Any]:
    normalized_payloads = normalize_openclaw_opinion_payloads(
        payload,
        expected_round=expected_round,
        expected_agent_id=expected_agent_id,
        case_id_map=case_id_map,
        default_case_id=default_case_id,
    )
    validation = validate_opinion_batch(
        normalized_payloads,
        expected_round=expected_round,
        expected_agent_id=expected_agent_id,
        expected_case_ids=expected_case_ids,
    )
    writeback_items = [
        (item.case_id, CandidateOpinion(**_candidate_opinion_payload(item)))
        for item in validation.normalized_items
        if item.case_id
    ]
    return {
        "ok": validation.ok,
        "raw_count": len(normalized_payloads),
        "normalized_payloads": normalized_payloads,
        "normalized_items": [item.model_dump() for item in validation.normalized_items],
        "issues": [item.model_dump() for item in validation.issues],
        "summary_lines": validation.summary_lines,
        "covered_case_ids": validation.covered_case_ids,
        "missing_case_ids": validation.missing_case_ids,
        "duplicate_keys": validation.duplicate_keys,
        "substantive_round_2_case_ids": validation.substantive_round_2_case_ids,
        "writeback_items": writeback_items,
    }


def _resolve_case_id(
    payload: dict[str, Any],
    *,
    case_id_map: dict[str, str] | None = None,
    default_case_id: str | None = None,
) -> str | None:
    symbol = str(payload.get("symbol") or "").strip()
    if case_id_map and symbol and symbol in case_id_map:
        return case_id_map[symbol]
    if default_case_id:
        return default_case_id
    return None


def _candidate_opinion_payload(item: DiscussionOpinion) -> dict[str, Any]:
    payload = item.model_dump()
    payload.pop("case_id", None)
    return payload


def _is_opinion_like(payload: dict[str, Any]) -> bool:
    return bool(OPINION_LIKE_KEYS.intersection(payload.keys()))

