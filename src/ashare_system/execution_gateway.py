"""Windows Execution Gateway 执行意图辅助函数。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .contracts import ExecutionIntentPacket
from .infra.audit_store import StateStore

EXECUTION_GATEWAY_PENDING_PATH = "/system/execution/gateway/intents/pending"


def resolve_execution_gateway_state_store(
    primary: StateStore | None,
    fallback: StateStore | None = None,
) -> StateStore | None:
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
