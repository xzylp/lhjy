"""执行桥 receipt 与券商订单侧对账。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from ..execution_gateway import (
    append_receipt_audit_event,
    get_execution_gateway_receipt_history,
    map_order_status_to_intent_status,
)
from ..execution_reconciliation import ExecutionReconciliationService
from ..infra.adapters import ExecutionAdapter
from ..infra.audit_store import StateStore


def run(
    execution_adapter: ExecutionAdapter,
    meeting_state_store: StateStore,
    execution_gateway_state_store: StateStore | None,
    *,
    account_id: str,
    now_factory: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    now = (now_factory or datetime.now)()
    base_service = ExecutionReconciliationService(execution_adapter, meeting_state_store, now_factory=now_factory)
    base_payload = base_service.reconcile(account_id, persist=True)

    receipt_history = get_execution_gateway_receipt_history(execution_gateway_state_store)
    latest_receipt = (
        dict(execution_gateway_state_store.get("latest_execution_gateway_receipt", {}) or {})
        if execution_gateway_state_store is not None
        else {}
    )
    try:
        orders = list(execution_adapter.get_orders(account_id) or [])
        order_map = {str(item.order_id): item for item in orders if str(item.order_id).strip()}
    except Exception as exc:
        payload = {
            **base_payload,
            "bridge_reconciliation_status": "error",
            "bridge_reconciliation_error": str(exc),
            "matched": 0,
            "unmatched_linux": len(receipt_history),
            "unmatched_qmt": 0,
            "status_mismatch": 0,
        }
        meeting_state_store.set("latest_execution_bridge_reconciliation", payload)
        return payload

    updated_history: list[dict[str, Any]] = []
    receipt_order_ids = {
        str(item.get("broker_order_id") or "").strip()
        for item in receipt_history
        if str(item.get("broker_order_id") or "").strip()
    }
    matched = 0
    unmatched_linux: list[dict[str, Any]] = []
    status_mismatch: list[dict[str, Any]] = []

    for item in receipt_history:
        receipt = dict(item)
        broker_order_id = str(receipt.get("broker_order_id") or "").strip()
        order = order_map.get(broker_order_id) if broker_order_id else None
        if order is None:
            unmatched_linux.append(
                {
                    "receipt_id": receipt.get("receipt_id"),
                    "intent_id": receipt.get("intent_id"),
                    "broker_order_id": broker_order_id,
                }
            )
            updated_history.append(receipt)
            continue
        matched += 1
        target_status = map_order_status_to_intent_status(getattr(order, "status", "UNKNOWN"))
        current_status = str(receipt.get("status") or "").strip().lower()
        if target_status != current_status:
            status_mismatch.append(
                {
                    "receipt_id": receipt.get("receipt_id"),
                    "intent_id": receipt.get("intent_id"),
                    "broker_order_id": broker_order_id,
                    "receipt_status": current_status,
                    "broker_status": getattr(order, "status", "UNKNOWN"),
                    "resolved_status": target_status,
                }
            )
            receipt["status"] = target_status
            receipt["reconciled_at"] = now.isoformat()
            if execution_gateway_state_store is not None:
                append_receipt_audit_event(
                    execution_gateway_state_store,
                    intent_id=str(receipt.get("intent_id") or ""),
                    previous_status=current_status,
                    next_status=target_status,
                    reason="execution_reconciliation_status_fix",
                    reported_at=now.isoformat(),
                    receipt_id=str(receipt.get("receipt_id") or ""),
                    broker_order_id=broker_order_id,
                    metadata={"broker_status": getattr(order, "status", "UNKNOWN")},
                )
        updated_history.append(receipt)

    unmatched_qmt = [
        {
            "order_id": str(item.order_id),
            "symbol": item.symbol,
            "status": item.status,
        }
        for item in orders
        if str(item.order_id) not in receipt_order_ids
    ]

    if execution_gateway_state_store is not None:
        execution_gateway_state_store.set("execution_gateway_receipt_history", updated_history[-500:])
        if latest_receipt:
            latest_id = str(latest_receipt.get("receipt_id") or "")
            for item in reversed(updated_history):
                if str(item.get("receipt_id") or "") == latest_id:
                    execution_gateway_state_store.set("latest_execution_gateway_receipt", item)
                    break

    bridge_payload = {
        "generated_at": now.isoformat(),
        "account_id": account_id,
        "matched": matched,
        "unmatched_linux": unmatched_linux,
        "unmatched_qmt": unmatched_qmt,
        "status_mismatch": status_mismatch,
        "summary_lines": [
            (
                f"执行桥对账完成: matched={matched} "
                f"unmatched_linux={len(unmatched_linux)} unmatched_qmt={len(unmatched_qmt)} "
                f"status_mismatch={len(status_mismatch)}。"
            )
        ],
    }
    payload = {**base_payload, "bridge_reconciliation_status": "ok", **bridge_payload}
    meeting_state_store.set("latest_execution_bridge_reconciliation", payload)
    return payload
