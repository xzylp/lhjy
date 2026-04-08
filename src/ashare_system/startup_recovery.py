"""服务启动后的执行状态恢复。"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .infra.adapters import ExecutionAdapter
from .infra.audit_store import StateStore

FINAL_ORDER_STATUSES = {"FILLED", "CANCELLED", "REJECTED"}


class StartupRecoveryService:
    """启动时扫描券商订单并修复本地执行台账。"""

    def __init__(
        self,
        execution_adapter: ExecutionAdapter,
        state_store: StateStore,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._execution_adapter = execution_adapter
        self._state_store = state_store
        self._now_factory = now_factory or datetime.now

    def recover(self, account_id: str, persist: bool = True) -> dict:
        now = self._now_factory()
        try:
            orders = self._execution_adapter.get_orders(account_id)
        except Exception as exc:
            payload = {
                "account_id": account_id,
                "recovered_at": now.isoformat(),
                "status": "error",
                "order_count": 0,
                "pending_count": 0,
                "resolved_count": 0,
                "orphan_count": 0,
                "updated_count": 0,
                "error": str(exc),
                "summary_lines": [f"启动恢复失败: {exc}"],
            }
            if persist:
                self._persist(payload)
            return payload

        journal = self._state_store.get("execution_order_journal", [])
        journal_by_order_id = {item.get("order_id"): item for item in journal if item.get("order_id")}
        updated_count = 0
        orphan_count = 0
        pending_count = 0
        resolved_count = 0

        for order in orders:
            record = journal_by_order_id.get(order.order_id)
            if record is None:
                record = {
                    "order_id": order.order_id,
                    "account_id": account_id,
                    "symbol": order.symbol,
                    "name": order.symbol,
                    "decision_id": None,
                    "trade_date": None,
                    "submitted_at": None,
                    "source": "broker_scan",
                }
                journal.append(record)
                journal_by_order_id[order.order_id] = record
                orphan_count += 1
            previous_status = record.get("latest_status")
            record["latest_status"] = order.status
            record["last_checked_at"] = now.isoformat()
            if order.status in FINAL_ORDER_STATUSES:
                record["resolved_at"] = now.isoformat()
                resolved_count += 1
            else:
                pending_count += 1
            if previous_status != order.status:
                updated_count += 1

        payload = {
            "account_id": account_id,
            "recovered_at": now.isoformat(),
            "status": "ok",
            "order_count": len(orders),
            "pending_count": pending_count,
            "resolved_count": resolved_count,
            "orphan_count": orphan_count,
            "updated_count": updated_count,
            "summary_lines": [
                f"启动恢复完成: orders={len(orders)} pending={pending_count} resolved={resolved_count} orphan={orphan_count} updated={updated_count}。"
            ],
        }
        if persist:
            self._state_store.set("execution_order_journal", journal[-200:])
            self._persist(payload)
        return payload

    def _persist(self, payload: dict) -> None:
        history = self._state_store.get("startup_recovery_history", [])
        history.append(
            {
                "account_id": payload.get("account_id"),
                "recovered_at": payload.get("recovered_at"),
                "status": payload.get("status"),
                "order_count": payload.get("order_count", 0),
                "pending_count": payload.get("pending_count", 0),
                "orphan_count": payload.get("orphan_count", 0),
                "updated_count": payload.get("updated_count", 0),
            }
        )
        self._state_store.set("latest_startup_recovery", payload)
        self._state_store.set("startup_recovery_history", history[-50:])
