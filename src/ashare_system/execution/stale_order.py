"""挂单治理与过期 intent 收口。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from ..execution_gateway import (
    TERMINAL_INTENT_STATUSES,
    append_execution_intent_history,
    get_pending_execution_intents,
    map_order_status_to_intent_status,
    retry_stale_claimed_intents,
    save_pending_execution_intents,
    transition_execution_intent,
)
from ..infra.adapters import ExecutionAdapter
from ..infra.audit_store import AuditStore, StateStore


def cleanup(
    state_store: StateStore | None,
    execution_adapter: ExecutionAdapter | None,
    *,
    account_id: str,
    audit_store: AuditStore | None = None,
    now_factory: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    now = (now_factory or datetime.now)()
    if state_store is None:
        return {
            "ok": False,
            "status": "skipped",
            "reason": "execution_gateway_state_unavailable",
            "generated_at": now.isoformat(),
        }

    pending = get_pending_execution_intents(state_store)
    retry_payload = retry_stale_claimed_intents(state_store, now=now, stale_after_seconds=300)
    pending = get_pending_execution_intents(state_store)
    order_map: dict[str, Any] = {}
    order_error = ""
    if execution_adapter is not None and str(account_id or "").strip():
        try:
            orders = list(execution_adapter.get_orders(account_id) or [])
            order_map = {str(item.order_id): item for item in orders if str(item.order_id).strip()}
        except Exception as exc:
            order_error = str(exc)

    kept: list[dict[str, Any]] = []
    closed_count = 0
    stale_count = 0
    orphaned_count = 0
    mutated = False

    for item in pending:
        current = dict(item)
        status = str(current.get("status") or "approved").strip().lower() or "approved"
        broker_order_id = str(
            current.get("broker_order_id")
            or ((current.get("latest_receipt") or {}).get("broker_order_id"))
            or ""
        ).strip()
        generated_at = _parse_iso_datetime(
            str(current.get("latest_reported_at") or current.get("generated_at") or "")
        )
        trade_date = str(current.get("trade_date") or "").strip()

        matched_order = order_map.get(broker_order_id) if broker_order_id else None
        if matched_order is not None:
            target_status = map_order_status_to_intent_status(getattr(matched_order, "status", "UNKNOWN"))
            if target_status != status:
                current = transition_execution_intent(
                    state_store,
                    current,
                    target_status,
                    reason="stale_order_cleanup_from_broker",
                    metadata={"broker_order_id": broker_order_id, "broker_status": getattr(matched_order, "status", "")},
                )
                mutated = True
            if str(current.get("status") or "").strip().lower() in TERMINAL_INTENT_STATUSES:
                append_execution_intent_history(state_store, current)
                closed_count += 1
                mutated = True
                continue
            kept.append(current)
            continue

        age_expired = False
        if generated_at is not None:
            left, right = _normalize_datetime_pair(now, generated_at)
            age_expired = (left - right) >= timedelta(hours=24)
        elif trade_date and trade_date < now.date().isoformat():
            age_expired = True

        if broker_order_id and status in {"submitted", "partial_filled", "claimed"} and age_expired:
            current = transition_execution_intent(
                state_store,
                current,
                "expired",
                reason="orphaned_broker_order_missing",
                metadata={"broker_order_id": broker_order_id},
            )
            append_execution_intent_history(state_store, current)
            orphaned_count += 1
            mutated = True
            continue

        if status in {"approved", "claimed", "submitted", "partial_filled"} and age_expired:
            current = transition_execution_intent(
                state_store,
                current,
                "expired",
                reason="stale_pending_intent",
                metadata={"trade_date": trade_date},
            )
            append_execution_intent_history(state_store, current)
            stale_count += 1
            mutated = True
            continue

        kept.append(current)

    if mutated:
        save_pending_execution_intents(state_store, kept)

    payload = {
        "ok": True,
        "status": "ok" if not order_error else "degraded",
        "generated_at": now.isoformat(),
        "account_id": account_id,
        "checked_count": len(pending),
        "kept_count": len(kept),
        "closed_count": closed_count,
        "stale_count": stale_count,
        "orphaned_count": orphaned_count,
        "claim_retry_count": int(retry_payload.get("retry_count", 0) or 0),
        "order_error": order_error,
        "summary_lines": [
            (
                f"挂单治理完成: checked={len(pending)} kept={len(kept)} "
                f"closed={closed_count} stale={stale_count} orphaned={orphaned_count} claim_retry={retry_payload.get('retry_count', 0)}。"
            )
        ],
    }
    if order_error:
        payload["summary_lines"].append(f"券商订单侧查询失败: {order_error}")
    if audit_store is not None:
        audit_store.append("execution", "执行挂单治理完成", payload)
    state_store.set("latest_pending_order_cleanup", payload)
    return payload


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

