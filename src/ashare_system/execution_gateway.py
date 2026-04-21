"""Windows Execution Gateway 执行意图辅助函数。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from .contracts import ExecutionIntentPacket
from .infra.audit_store import StateStore

EXECUTION_GATEWAY_PENDING_PATH = "/system/execution/gateway/intents/pending"
TERMINAL_INTENT_STATUSES = {"filled", "canceled", "rejected", "failed", "expired"}
INTENT_TRANSITIONS: dict[str, set[str]] = {
    "approved": {"claimed", "expired"},
    "claimed": {"submitted", "partial_filled", "filled", "canceled", "rejected", "failed", "expired"},
    "submitted": {"partial_filled", "filled", "canceled", "rejected", "failed", "expired"},
    "partial_filled": {"filled", "canceled", "failed", "expired"},
    "filled": set(),
    "canceled": set(),
    "rejected": set(),
    "failed": set(),
    "expired": set(),
}
ORDER_STATUS_TO_INTENT_STATUS = {
    "PENDING": "submitted",
    "ACCEPTED": "submitted",
    "PARTIAL_FILLED": "partial_filled",
    "FILLED": "filled",
    "CANCEL_REQUESTED": "submitted",
    "CANCELLED": "canceled",
    "REJECTED": "rejected",
    "UNKNOWN": "submitted",
}


def resolve_execution_gateway_state_store(
    primary: StateStore | None,
    fallback: StateStore | None = None,
) -> StateStore | None:
    """优先使用独立 execution_gateway_state，再回退旧状态文件。"""
    return primary or fallback


def build_execution_gateway_intent_packet(
    intent: Mapping[str, Any],
    *,
    run_mode: str,
    approval_source: str,
    approved_by: str = "linux_control_plane",
    approved_at: str | None = None,
    execution_plane: str = "windows_gateway",
    summary_lines: list[str] | None = None,
) -> dict[str, Any]:
    request_payload = _sanitize_mapping(intent.get("request"))
    timestamp = approved_at or datetime.now().isoformat()
    trade_date = str(intent.get("trade_date") or request_payload.get("trade_date") or datetime.now().date().isoformat())
    account_id = str(intent.get("account_id") or request_payload.get("account_id") or "")
    symbol = str(intent.get("symbol") or request_payload.get("symbol") or "")
    side = str(intent.get("side") or request_payload.get("side") or "BUY").upper()
    quantity = int(intent.get("quantity") or request_payload.get("quantity") or 0)
    price = intent.get("price", request_payload.get("price"))

    strategy_context = {
        "playbook": intent.get("playbook") or request_payload.get("playbook"),
        "regime": intent.get("regime") or request_payload.get("regime"),
        "resolved_sector": intent.get("resolved_sector"),
    }
    strategy_context.update(_sanitize_mapping(intent.get("strategy_context")))

    risk_context = {
        "estimated_value": intent.get("estimated_value"),
        "precheck": _sanitize_mapping(intent.get("precheck")),
    }
    risk_context.update(_sanitize_mapping(intent.get("risk_context")))

    discussion_context = {
        "case_id": intent.get("case_id"),
        "decision_id": intent.get("decision_id"),
        "name": intent.get("name"),
        "headline_reason": intent.get("headline_reason"),
    }
    discussion_context.update(_sanitize_mapping(intent.get("discussion_context")))

    packet = ExecutionIntentPacket(
        intent_id=str(intent.get("intent_id") or request_payload.get("request_id") or ""),
        generated_at=timestamp,
        trade_date=trade_date,
        account_id=account_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=(float(price) if price is not None else None),
        run_mode=str(run_mode),
        execution_plane=str(execution_plane or "windows_gateway"),
        approval_source=approval_source,
        approved_by=str(approved_by or "linux_control_plane"),
        approved_at=timestamp,
        idempotency_key=str(
            intent.get("idempotency_key")
            or f"{account_id}-{trade_date}-{symbol}-{side}-{price}-{quantity}"
        ),
        live_execution_allowed=bool(intent.get("live_execution_allowed", False)),
        offline_only=bool(intent.get("offline_only", False)),
        trace_id=str(intent.get("trace_id") or request_payload.get("trace_id") or "").strip() or None,
        status=str(intent.get("status") or "approved"),
        request=request_payload,
        strategy_context=strategy_context,
        risk_context=risk_context,
        discussion_context=discussion_context,
        claim=_sanitize_mapping(intent.get("claim")),
        summary_lines=list(summary_lines or intent.get("summary_lines") or []),
    ).model_dump()
    return _sanitize_mapping(packet)


def enqueue_execution_gateway_intent(
    state_store: StateStore | None,
    intent: Mapping[str, Any],
    *,
    run_mode: str,
    approval_source: str,
    approved_by: str = "linux_control_plane",
    approved_at: str | None = None,
    execution_plane: str = "windows_gateway",
    summary_lines: list[str] | None = None,
) -> dict[str, Any]:
    if state_store is None:
        raise ValueError("execution_gateway_state_unavailable")
    packet = build_execution_gateway_intent_packet(
        intent,
        run_mode=run_mode,
        approval_source=approval_source,
        approved_by=approved_by,
        approved_at=approved_at,
        execution_plane=execution_plane,
        summary_lines=summary_lines,
    )
    pending = get_pending_execution_intents(state_store)
    for index, item in enumerate(pending):
        if str(item.get("intent_id") or "") != packet["intent_id"]:
            continue
        pending[index] = packet
        save_pending_execution_intents(state_store, pending)
        append_execution_intent_history(state_store, packet)
        return packet
    pending.append(packet)
    save_pending_execution_intents(state_store, pending)
    append_execution_intent_history(state_store, packet)
    return packet


def get_pending_execution_intents(state_store: StateStore | None) -> list[dict[str, Any]]:
    if not state_store:
        return []
    items = state_store.get("pending_execution_intents", [])
    if not isinstance(items, list):
        return []
    return [_sanitize_mapping(item) for item in items if isinstance(item, dict)]


def save_pending_execution_intents(state_store: StateStore | None, items: list[dict[str, Any]]) -> None:
    if not state_store:
        return
    state_store.set("pending_execution_intents", [_sanitize_mapping(item) for item in items if isinstance(item, dict)])


def get_execution_intent_history(state_store: StateStore | None) -> list[dict[str, Any]]:
    if not state_store:
        return []
    history = state_store.get("execution_intent_history", [])
    if not isinstance(history, list):
        return []
    return [_sanitize_mapping(item) for item in history if isinstance(item, dict)]


def append_execution_intent_history(state_store: StateStore | None, item: dict[str, Any]) -> None:
    if not state_store:
        return
    history = get_execution_intent_history(state_store)
    history.append(_sanitize_mapping(item))
    state_store.set("execution_intent_history", history[-200:])


def get_execution_gateway_receipt_history(state_store: StateStore | None) -> list[dict[str, Any]]:
    if not state_store:
        return []
    history = state_store.get("execution_gateway_receipt_history", [])
    if not isinstance(history, list):
        return []
    return [_sanitize_mapping(item) for item in history if isinstance(item, dict)]


def append_execution_gateway_receipt(state_store: StateStore | None, receipt: Mapping[str, Any]) -> dict[str, Any]:
    if not state_store:
        return {}
    sanitized = _sanitize_mapping(receipt)
    history = get_execution_gateway_receipt_history(state_store)
    history.append(sanitized)
    state_store.set("latest_execution_gateway_receipt", sanitized)
    state_store.set("execution_gateway_receipt_history", history[-500:])
    return sanitized


def get_receipt_audit_trail(state_store: StateStore | None) -> list[dict[str, Any]]:
    if not state_store:
        return []
    items = state_store.get("receipt_audit_trail", [])
    if not isinstance(items, list):
        return []
    return [_sanitize_mapping(item) for item in items if isinstance(item, dict)]


def append_receipt_audit_event(
    state_store: StateStore | None,
    *,
    intent_id: str,
    previous_status: str,
    next_status: str,
    reason: str,
    reported_at: str | None = None,
    receipt_id: str | None = None,
    broker_order_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not state_store:
        return {}
    event = {
        "intent_id": str(intent_id or "").strip(),
        "previous_status": str(previous_status or "").strip(),
        "next_status": str(next_status or "").strip(),
        "reason": str(reason or "").strip(),
        "reported_at": str(reported_at or datetime.now().isoformat()),
        "receipt_id": str(receipt_id or "").strip(),
        "broker_order_id": str(broker_order_id or "").strip(),
        "metadata": _sanitize_mapping(metadata),
    }
    history = get_receipt_audit_trail(state_store)
    history.append(event)
    state_store.set("receipt_audit_trail", history[-1000:])
    return event


def transition_execution_intent(
    state_store: StateStore | None,
    intent: Mapping[str, Any],
    next_status: str,
    *,
    reason: str,
    receipt: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = _sanitize_mapping(intent)
    current_status = str(normalized.get("status") or "approved").strip().lower() or "approved"
    target_status = str(next_status or current_status).strip().lower() or current_status
    if target_status == current_status:
        return normalized
    allowed = INTENT_TRANSITIONS.get(current_status, set())
    if target_status not in allowed:
        raise ValueError(f"invalid execution intent transition: {current_status} -> {target_status}")
    updated = dict(normalized)
    updated["status"] = target_status
    if receipt:
        sanitized_receipt = _sanitize_mapping(receipt)
        updated["latest_receipt"] = sanitized_receipt
        updated["latest_receipt_id"] = str(sanitized_receipt.get("receipt_id") or "")
        updated["latest_gateway_source_id"] = str(sanitized_receipt.get("gateway_source_id") or "")
        updated["latest_reported_at"] = str(
            sanitized_receipt.get("reported_at")
            or sanitized_receipt.get("submitted_at")
            or datetime.now().isoformat()
        )
        broker_order_id = str(
            sanitized_receipt.get("broker_order_id")
            or ((sanitized_receipt.get("order") or {}).get("order_id"))
            or ""
        ).strip()
        if broker_order_id:
            updated["broker_order_id"] = broker_order_id
    append_receipt_audit_event(
        state_store,
        intent_id=str(updated.get("intent_id") or ""),
        previous_status=current_status,
        next_status=target_status,
        reason=reason,
        reported_at=str(updated.get("latest_reported_at") or datetime.now().isoformat()),
        receipt_id=str(updated.get("latest_receipt_id") or ""),
        broker_order_id=str(updated.get("broker_order_id") or ""),
        metadata=metadata,
    )
    return _sanitize_mapping(updated)


def map_order_status_to_intent_status(order_status: Any) -> str:
    return ORDER_STATUS_TO_INTENT_STATUS.get(str(order_status or "").strip().upper(), "submitted")


def retry_stale_claimed_intents(
    state_store: StateStore | None,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = 300,
) -> dict[str, Any]:
    if not state_store:
        return {"checked_count": 0, "retry_count": 0, "items": []}
    current_time = now or datetime.now()
    pending = get_pending_execution_intents(state_store)
    checked_count = 0
    retry_items: list[dict[str, Any]] = []
    mutated = False
    for index, item in enumerate(pending):
        if str(item.get("status") or "").strip().lower() != "claimed":
            continue
        checked_count += 1
        claim = dict(item.get("claim") or {})
        claimed_at_text = str(claim.get("claimed_at") or item.get("latest_reported_at") or "").strip()
        claimed_at = _parse_iso_datetime(claimed_at_text)
        if claimed_at is None:
            continue
        normalized_now, normalized_claimed = _normalize_datetime_pair(current_time, claimed_at)
        if normalized_now - normalized_claimed < timedelta(seconds=max(int(stale_after_seconds), 1)):
            continue
        reset_item = dict(item)
        previous_status = str(reset_item.get("status") or "claimed")
        reset_item["status"] = "approved"
        reset_item["claim_retry_count"] = int(reset_item.get("claim_retry_count", 0) or 0) + 1
        reset_item["claim_retry_at"] = current_time.isoformat()
        reset_item["claim_retry_reason"] = f"claimed_stale>{stale_after_seconds}s"
        reset_item["claim"] = {}
        append_receipt_audit_event(
            state_store,
            intent_id=str(reset_item.get("intent_id") or ""),
            previous_status=previous_status,
            next_status="approved",
            reason="claimed_intent_retry",
            reported_at=current_time.isoformat(),
            metadata={
                "stale_after_seconds": stale_after_seconds,
                "claimed_at": claimed_at_text,
                "claim_retry_count": reset_item["claim_retry_count"],
            },
        )
        pending[index] = _sanitize_mapping(reset_item)
        retry_items.append(
            {
                "intent_id": reset_item.get("intent_id"),
                "claimed_at": claimed_at_text,
                "retry_count": reset_item["claim_retry_count"],
            }
        )
        mutated = True
    if mutated:
        save_pending_execution_intents(state_store, pending)
        for item in retry_items:
            matched = next(
                (candidate for candidate in pending if str(candidate.get("intent_id") or "") == str(item.get("intent_id") or "")),
                None,
            )
            if matched:
                append_execution_intent_history(state_store, matched)
    return {
        "checked_count": checked_count,
        "retry_count": len(retry_items),
        "items": retry_items,
    }


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalize_datetime_pair(left: datetime, right: datetime) -> tuple[datetime, datetime]:
    if (left.tzinfo is None) == (right.tzinfo is None):
        return left, right
    if left.tzinfo is not None:
        return left.replace(tzinfo=None), right
    return left, right.replace(tzinfo=None)


def _sanitize_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _sanitize_value(item) for key, item in value.items()}


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item) for item in value]
    return value
