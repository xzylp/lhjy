"""未决订单巡检服务。"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .infra.adapters import ExecutionAdapter
from .infra.audit_store import StateStore

PENDING_ORDER_STATUSES = {"PENDING", "ACCEPTED", "PARTIAL_FILLED", "CANCEL_REQUESTED", "UNKNOWN"}


class PendingOrderInspectionService:
    """检查未决订单并输出结构化巡检结果。"""

    def __init__(
        self,
        execution_adapter: ExecutionAdapter,
        state_store: StateStore,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._execution_adapter = execution_adapter
        self._state_store = state_store
        self._now_factory = now_factory or datetime.now

    def inspect(self, account_id: str, warn_after_seconds: int = 300, persist: bool = True) -> dict:
        now = self._now_factory()
        try:
            orders = self._execution_adapter.get_orders(account_id)
        except Exception as exc:
            payload = {
                "account_id": account_id,
                "inspected_at": now.isoformat(),
                "warn_after_seconds": warn_after_seconds,
                "status": "error",
                "pending_count": 0,
                "warning_count": 0,
                "stale_count": 0,
                "error": str(exc),
                "items": [],
                "summary_lines": [f"订单巡检失败: {exc}"],
            }
            if persist:
                self._persist(payload)
            return payload

        journal = self._state_store.get("execution_order_journal", [])
        journal_by_order_id = {item.get("order_id"): item for item in journal if item.get("order_id")}

        items: list[dict] = []
        warning_count = 0
        stale_count = 0
        for order in orders:
            if order.status not in PENDING_ORDER_STATUSES:
                continue
            journal_item = journal_by_order_id.get(order.order_id, {})
            submitted_at = journal_item.get("submitted_at")
            age_seconds = self._age_seconds(submitted_at, now)
            is_stale = age_seconds is not None and age_seconds >= warn_after_seconds
            needs_attention = is_stale or order.status in {"CANCEL_REQUESTED", "UNKNOWN"}
            if needs_attention:
                warning_count += 1
            if is_stale:
                stale_count += 1
            items.append(
                {
                    "order_id": order.order_id,
                    "account_id": order.account_id,
                    "symbol": order.symbol,
                    "name": journal_item.get("name") or order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "price": order.price,
                    "status": order.status,
                    "decision_id": journal_item.get("decision_id"),
                    "trade_date": journal_item.get("trade_date"),
                    "submitted_at": submitted_at,
                    "age_seconds": age_seconds,
                    "is_stale": is_stale,
                    "needs_attention": needs_attention,
                }
            )

        status = "clear"
        if items:
            status = "warning" if warning_count > 0 else "pending"
        payload = {
            "account_id": account_id,
            "inspected_at": now.isoformat(),
            "warn_after_seconds": warn_after_seconds,
            "status": status,
            "pending_count": len(items),
            "warning_count": warning_count,
            "stale_count": stale_count,
            "items": items,
            "summary_lines": [
                f"订单巡检: pending={len(items)} warning={warning_count} stale={stale_count} warn_after={warn_after_seconds}s。"
            ],
        }
        for item in items[:5]:
            age_text = f"{round(item['age_seconds'], 1)}s" if item["age_seconds"] is not None else "unknown"
            payload["summary_lines"].append(
                f"{item['symbol']} {item['name']} order_id={item['order_id']} status={item['status']} age={age_text}。"
            )
        if persist:
            self._persist(payload)
        return payload

    def _persist(self, payload: dict) -> None:
        history = self._state_store.get("pending_order_inspection_history", [])
        history.append(
            {
                "account_id": payload.get("account_id"),
                "inspected_at": payload.get("inspected_at"),
                "status": payload.get("status"),
                "pending_count": payload.get("pending_count", 0),
                "warning_count": payload.get("warning_count", 0),
                "stale_count": payload.get("stale_count", 0),
            }
        )
        self._state_store.set("latest_pending_order_inspection", payload)
        self._state_store.set("pending_order_inspection_history", history[-50:])

    @staticmethod
    def _age_seconds(submitted_at: str | None, now: datetime) -> float | None:
        if not submitted_at:
            return None
        try:
            created_at = datetime.fromisoformat(submitted_at)
        except ValueError:
            return None
        return max((now - created_at).total_seconds(), 0.0)
