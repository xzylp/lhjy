"""未决订单自动处置服务。"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .infra.adapters import ExecutionAdapter
from .infra.audit_store import StateStore
from .pending_order_inspection import PendingOrderInspectionService

AUTO_CANCEL_ELIGIBLE_STATUSES = {"PENDING", "ACCEPTED", "PARTIAL_FILLED", "UNKNOWN"}


class PendingOrderRemediationService:
    """根据巡检结果对超时未决订单执行保守处置。"""

    def __init__(
        self,
        execution_adapter: ExecutionAdapter,
        state_store: StateStore,
        inspection_service: PendingOrderInspectionService,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._execution_adapter = execution_adapter
        self._state_store = state_store
        self._inspection_service = inspection_service
        self._now_factory = now_factory or datetime.now

    def remediate(
        self,
        account_id: str,
        auto_action: str = "alert_only",
        cancel_after_seconds: int = 900,
        persist: bool = True,
    ) -> dict:
        now = self._now_factory()
        inspection = self._inspection_service.inspect(
            account_id,
            warn_after_seconds=cancel_after_seconds,
            persist=False,
        )
        if inspection.get("status") == "error":
            payload = {
                "account_id": account_id,
                "remediated_at": now.isoformat(),
                "status": "error",
                "auto_action": auto_action,
                "cancel_after_seconds": cancel_after_seconds,
                "inspection": inspection,
                "receipts": [],
                "summary_lines": [f"未决订单处置失败: {inspection.get('error', 'inspection_error')}"],
            }
            if persist:
                self._persist(payload)
            return payload

        stale_items = [item for item in inspection.get("items", []) if item.get("is_stale")]
        receipts: list[dict] = []
        actioned_count = 0
        cancelled_count = 0

        for item in stale_items:
            receipt = {
                "order_id": item.get("order_id"),
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "status": "skipped",
                "reason": "no_action",
                "auto_action": auto_action,
                "evaluated_at": now.isoformat(),
            }
            if auto_action == "alert_only":
                receipt["status"] = "alert_only"
                receipt["reason"] = "stale_pending_order"
                actioned_count += 1
            elif auto_action == "cancel":
                if item.get("status") not in AUTO_CANCEL_ELIGIBLE_STATUSES:
                    receipt["reason"] = "status_not_cancelable"
                else:
                    try:
                        cancelled = self._execution_adapter.cancel_order(account_id, item["order_id"])
                        receipt["status"] = "cancel_submitted"
                        receipt["reason"] = cancelled.status
                        receipt["cancel_result"] = cancelled.model_dump()
                        actioned_count += 1
                        if cancelled.status in {"CANCELLED", "CANCEL_REQUESTED"}:
                            cancelled_count += 1
                    except Exception as exc:
                        receipt["status"] = "cancel_failed"
                        receipt["reason"] = str(exc)
                        actioned_count += 1
            receipts.append(receipt)

        payload = {
            "account_id": account_id,
            "remediated_at": now.isoformat(),
            "status": ("actioned" if receipts else "no_action"),
            "auto_action": auto_action,
            "cancel_after_seconds": cancel_after_seconds,
            "stale_count": len(stale_items),
            "actioned_count": actioned_count,
            "cancelled_count": cancelled_count,
            "inspection": inspection,
            "receipts": receipts,
            "summary_lines": [
                f"未决订单处置: action={auto_action} stale={len(stale_items)} actioned={actioned_count} cancelled={cancelled_count} cancel_after={cancel_after_seconds}s。"
            ],
        }
        for receipt in receipts[:5]:
            payload["summary_lines"].append(
                f"{receipt['symbol']} {receipt['name']} order_id={receipt['order_id']} {receipt['status']} reason={receipt['reason']}。"
            )
        if persist:
            self._persist(payload)
        return payload

    def _persist(self, payload: dict) -> None:
        history = self._state_store.get("pending_order_remediation_history", [])
        history.append(
            {
                "account_id": payload.get("account_id"),
                "remediated_at": payload.get("remediated_at"),
                "status": payload.get("status"),
                "auto_action": payload.get("auto_action"),
                "stale_count": payload.get("stale_count", 0),
                "actioned_count": payload.get("actioned_count", 0),
                "cancelled_count": payload.get("cancelled_count", 0),
            }
        )
        self._state_store.set("latest_pending_order_remediation", payload)
        self._state_store.set("pending_order_remediation_history", history[-50:])
