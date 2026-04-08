"""讨论协议对象层。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

DISCUSSION_CONTRACT_VERSION = "quant-discussion-v1"
DISCUSSION_ROUND_2_REQUIRED_FIELDS = [
    "challenged_by",
    "challenged_points",
    "previous_stance",
    "changed",
    "changed_because",
    "resolved_questions",
    "remaining_disputes",
]
DISCUSSION_ROUND_2_REQUIRED_CONTEXT = [
    "controversy_summary_lines",
    "round_2_guidance",
    "substantive_gap_case_ids",
]


class DiscussionProtocolMeta(BaseModel):
    contract_version: str = DISCUSSION_CONTRACT_VERSION
    packet_type: Literal["agent_packets", "meeting_context", "finalize_packet"]
    trade_date: str
    generated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    producer: str = "ashare-system-v2"
    source_endpoint: str = ""
    required_round_2_fields: list[str] = Field(default_factory=lambda: list(DISCUSSION_ROUND_2_REQUIRED_FIELDS))
    required_round_2_context: list[str] = Field(default_factory=lambda: list(DISCUSSION_ROUND_2_REQUIRED_CONTEXT))


class DiscussionAgentPacketsEnvelope(BaseModel):
    protocol: DiscussionProtocolMeta
    generated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    requested_agent_id: str
    status_filter: str | None = None
    case_count: int = 0
    round_coverage: dict[str, Any] = Field(default_factory=dict)
    controversy_summary_lines: list[str] = Field(default_factory=list)
    round_2_guidance: list[str] = Field(default_factory=list)
    shared_context: dict[str, Any] = Field(default_factory=dict)
    shared_context_lines: list[str] = Field(default_factory=list)
    workspace_context: dict[str, Any] = Field(default_factory=dict)
    workspace_summary_lines: list[str] = Field(default_factory=list)
    data_catalog_ref: dict[str, Any] = Field(default_factory=dict)
    preferred_read_order: list[str] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)
    summary_lines: list[str] = Field(default_factory=list)
    summary_text: str = ""
    agent_roles: dict[str, str] = Field(default_factory=dict)


class DiscussionMeetingContextEnvelope(BaseModel):
    protocol: DiscussionProtocolMeta
    generated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    available: bool = True
    resource: str = "discussion_context"
    trade_date: str
    case_count: int = 0
    status: str | None = None
    cycle: dict[str, Any] | None = None
    round_coverage: dict[str, Any] = Field(default_factory=dict)
    disputed_case_ids: list[str] = Field(default_factory=list)
    substantive_gap_case_ids: list[str] = Field(default_factory=list)
    controversy_summary_lines: list[str] = Field(default_factory=list)
    round_2_guidance: list[str] = Field(default_factory=list)
    shared_context: dict[str, Any] = Field(default_factory=dict)
    shared_context_lines: list[str] = Field(default_factory=list)
    data_catalog_ref: dict[str, Any] = Field(default_factory=dict)
    reply_pack: dict[str, Any] = Field(default_factory=dict)
    final_brief: dict[str, Any] = Field(default_factory=dict)
    client_brief: dict[str, Any] = Field(default_factory=dict)
    finalize_packet: dict[str, Any] | None = None
    summary_lines: list[str] = Field(default_factory=list)
    summary_text: str = ""


class DiscussionFinalizePacketEnvelope(BaseModel):
    protocol: DiscussionProtocolMeta
    generated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    trade_date: str
    status: str
    cycle: dict[str, Any] = Field(default_factory=dict)
    selected_case_ids: list[str] = Field(default_factory=list)
    blocked: bool = False
    blockers: list[str] = Field(default_factory=list)
    execution_precheck: dict[str, Any] = Field(default_factory=dict)
    execution_intents: dict[str, Any] = Field(default_factory=dict)
    client_brief: dict[str, Any] = Field(default_factory=dict)
    final_brief: dict[str, Any] = Field(default_factory=dict)
    reply_pack: dict[str, Any] = Field(default_factory=dict)
    shared_context: dict[str, Any] = Field(default_factory=dict)
    summary_lines: list[str] = Field(default_factory=list)
    summary_text: str = ""


def build_agent_packets_envelope(payload: dict[str, Any], *, trade_date: str, requested_agent_id: str | None) -> dict[str, Any]:
    protocol = DiscussionProtocolMeta(
        packet_type="agent_packets",
        trade_date=trade_date,
        source_endpoint="/system/discussions/agent-packets",
    )
    return DiscussionAgentPacketsEnvelope(
        protocol=protocol,
        generated_at=payload.get("generated_at") or protocol.generated_at,
        requested_agent_id=requested_agent_id or "shared",
        status_filter=payload.get("status_filter"),
        case_count=payload.get("case_count", 0),
        round_coverage=payload.get("round_coverage", {}),
        controversy_summary_lines=payload.get("controversy_summary_lines", []),
        round_2_guidance=payload.get("round_2_guidance", []),
        shared_context=payload.get("shared_context", {}),
        shared_context_lines=payload.get("shared_context_lines", []),
        workspace_context=payload.get("workspace_context", {}),
        workspace_summary_lines=payload.get("workspace_summary_lines", []),
        data_catalog_ref=payload.get("data_catalog_ref", {}),
        preferred_read_order=payload.get("preferred_read_order", []),
        items=payload.get("items", []),
        summary_lines=payload.get("summary_lines", []),
        summary_text=payload.get("summary_text", ""),
        agent_roles={
            "ashare-research": "研究面，负责事件、催化和研究证据。",
            "ashare-strategy": "策略面，负责排序、胜出逻辑和战法说明。",
            "ashare-risk": "风控面，负责 allow / limit / reject 与可解除条件。",
            "ashare-audit": "审计面，负责证据闭环和讨论质量。",
        },
    ).model_dump()


def build_meeting_context_envelope(payload: dict[str, Any], *, trade_date: str) -> dict[str, Any]:
    protocol = DiscussionProtocolMeta(
        packet_type="meeting_context",
        trade_date=trade_date,
        source_endpoint="/system/discussions/meeting-context",
    )
    return DiscussionMeetingContextEnvelope(
        protocol=protocol,
        generated_at=payload.get("generated_at") or protocol.generated_at,
        available=payload.get("available", True),
        resource=payload.get("resource", "discussion_context"),
        trade_date=trade_date,
        case_count=payload.get("case_count", 0),
        status=payload.get("status"),
        cycle=payload.get("cycle"),
        round_coverage=payload.get("round_coverage", {}),
        disputed_case_ids=payload.get("disputed_case_ids", []),
        substantive_gap_case_ids=payload.get("substantive_gap_case_ids", []),
        controversy_summary_lines=payload.get("controversy_summary_lines", []),
        round_2_guidance=payload.get("round_2_guidance", []),
        shared_context=payload.get("shared_context", {}),
        shared_context_lines=payload.get("shared_context_lines", []),
        data_catalog_ref=payload.get("data_catalog_ref", {}),
        reply_pack=payload.get("reply_pack", {}),
        final_brief=payload.get("final_brief", {}),
        client_brief=payload.get("client_brief", {}),
        finalize_packet=payload.get("finalize_packet"),
        summary_lines=payload.get("summary_lines", []),
        summary_text=payload.get("summary_text", ""),
    ).model_dump()


def build_finalize_packet_envelope(
    *,
    trade_date: str,
    cycle: dict[str, Any],
    execution_precheck: dict[str, Any],
    execution_intents: dict[str, Any],
    client_brief: dict[str, Any],
    final_brief: dict[str, Any],
    reply_pack: dict[str, Any],
    shared_context: dict[str, Any],
) -> dict[str, Any]:
    blockers = list(dict.fromkeys((cycle or {}).get("blockers", []) or []))
    selected_case_ids = (cycle or {}).get("execution_pool_case_ids", []) or []
    summary_lines = [
        f"交易日 {trade_date} finalize packet：selected={len(selected_case_ids)} blocked={bool(blockers)} intents={execution_intents.get('intent_count', 0)}",
    ]
    summary_lines.extend(client_brief.get("lines", [])[:6])
    protocol = DiscussionProtocolMeta(
        packet_type="finalize_packet",
        trade_date=trade_date,
        source_endpoint="/system/discussions/finalize-packet",
    )
    return DiscussionFinalizePacketEnvelope(
        protocol=protocol,
        generated_at=client_brief.get("generated_at") or final_brief.get("generated_at") or protocol.generated_at,
        trade_date=trade_date,
        status=str(client_brief.get("status") or final_brief.get("status") or "blocked"),
        cycle=cycle or {},
        selected_case_ids=selected_case_ids,
        blocked=bool(blockers) or not selected_case_ids,
        blockers=blockers,
        execution_precheck=execution_precheck,
        execution_intents=execution_intents,
        client_brief=client_brief,
        final_brief=final_brief,
        reply_pack=reply_pack,
        shared_context=shared_context,
        summary_lines=summary_lines,
        summary_text="\n".join(summary_lines),
    ).model_dump()
