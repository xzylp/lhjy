"""执行成交与账户对账服务。"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .infra.adapters import ExecutionAdapter
from .infra.audit_store import StateStore


class ExecutionReconciliationService:
    """汇总订单、成交、余额、持仓，修复本地执行台账。"""

    def __init__(
        self,
        execution_adapter: ExecutionAdapter,
        state_store: StateStore,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._execution_adapter = execution_adapter
        self._state_store = state_store
        self._now_factory = now_factory or datetime.now

    def reconcile(self, account_id: str, persist: bool = True) -> dict:
        now = self._now_factory()
        try:
            balance = self._execution_adapter.get_balance(account_id)
            positions = self._execution_adapter.get_positions(account_id)
            orders = self._execution_adapter.get_orders(account_id)
            trades = self._execution_adapter.get_trades(account_id)
        except Exception as exc:
            payload = {
                "account_id": account_id,
                "reconciled_at": now.isoformat(),
                "status": "error",
                "error": str(exc),
                "matched_order_count": 0,
                "filled_order_count": 0,
                "orphan_trade_count": 0,
                "position_count": 0,
                "trade_count": 0,
                "summary_lines": [f"执行对账失败: {exc}"],
            }
            if persist:
                self._persist(payload)
            return payload

        order_map = {order.order_id: order for order in orders}
        trade_groups: dict[str, list] = {}
        for trade in trades:
            trade_groups.setdefault(trade.order_id, []).append(trade)

        journal = self._state_store.get("execution_order_journal", [])
        matched_order_count = 0
        filled_order_count = 0
        updated_count = 0
        reconciliation_items: list[dict] = []

        for record in journal:
            order_id = record.get("order_id")
            if not order_id:
                continue
            order = order_map.get(order_id)
            if order is not None:
                matched_order_count += 1
            grouped_trades = trade_groups.get(order_id, [])
            filled_quantity = sum(int(item.quantity) for item in grouped_trades)
            filled_value = sum(float(item.quantity) * float(item.price) for item in grouped_trades)
            avg_fill_price = round(filled_value / filled_quantity, 4) if filled_quantity > 0 else None
            previous_status = record.get("latest_status")
            new_status = order.status if order is not None else previous_status
            if previous_status != new_status or record.get("filled_quantity") != filled_quantity:
                updated_count += 1
            record["latest_status"] = new_status
            record["last_reconciled_at"] = now.isoformat()
            record["filled_quantity"] = filled_quantity
            record["filled_value"] = round(filled_value, 4)
            record["avg_fill_price"] = avg_fill_price
            record["trade_count"] = len(grouped_trades)
            record["last_trade_at"] = (now.isoformat() if grouped_trades else record.get("last_trade_at"))
            if filled_quantity > 0:
                filled_order_count += 1
            reconciliation_items.append(
                {
                    "order_id": order_id,
                    "symbol": record.get("symbol"),
                    "name": record.get("name") or record.get("symbol"),
                    "decision_id": record.get("decision_id"),
                    "trade_date": record.get("trade_date"),
                    "submitted_at": record.get("submitted_at"),
                    "side": (
                        (order.side if order is not None else None)
                        or ((record.get("request") or {}).get("side"))
                    ),
                    "latest_status": new_status,
                    "filled_quantity": filled_quantity,
                    "filled_value": round(filled_value, 4),
                    "avg_fill_price": avg_fill_price,
                    "trade_count": len(grouped_trades),
                }
            )

        trade_journal = self._state_store.get("execution_trade_journal", [])
        known_trade_ids = {item.get("trade_id") for item in trade_journal}
        orphan_trade_count = 0
        for trade in trades:
            if trade.trade_id in known_trade_ids:
                continue
            trade_journal.append(
                {
                    "trade_id": trade.trade_id,
                    "order_id": trade.order_id,
                    "account_id": trade.account_id,
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "quantity": trade.quantity,
                    "price": trade.price,
                    "reconciled_at": now.isoformat(),
                    "known_order": trade.order_id in order_map,
                }
            )
            known_trade_ids.add(trade.trade_id)
            if trade.order_id not in {item.get("order_id") for item in journal}:
                orphan_trade_count += 1

        payload = {
            "account_id": account_id,
            "reconciled_at": now.isoformat(),
            "status": "ok",
            "matched_order_count": matched_order_count,
            "filled_order_count": filled_order_count,
            "orphan_trade_count": orphan_trade_count,
            "position_count": len(positions),
            "trade_count": len(trades),
            "updated_count": updated_count,
            "balance": balance.model_dump(),
            "positions": [item.model_dump() for item in positions],
            "items": reconciliation_items,
            "summary_lines": [
                f"执行对账完成: matched_orders={matched_order_count} filled_orders={filled_order_count} trades={len(trades)} orphan_trades={orphan_trade_count} positions={len(positions)}。",
                f"账户资产 total={round(balance.total_asset, 2)} cash={round(balance.cash, 2)}。",
            ],
        }
        if persist:
            self._state_store.set("execution_order_journal", journal[-200:])
            self._state_store.set("execution_trade_journal", trade_journal[-500:])
            self._persist(payload)
        return payload

    def _persist(self, payload: dict) -> None:
        history = self._state_store.get("execution_reconciliation_history", [])
        history.append(
            {
                "account_id": payload.get("account_id"),
                "reconciled_at": payload.get("reconciled_at"),
                "status": payload.get("status"),
                "matched_order_count": payload.get("matched_order_count", 0),
                "filled_order_count": payload.get("filled_order_count", 0),
                "orphan_trade_count": payload.get("orphan_trade_count", 0),
                "position_count": payload.get("position_count", 0),
                "trade_count": payload.get("trade_count", 0),
            }
        )
        self._state_store.set("latest_execution_reconciliation", payload)
        self._state_store.set("execution_reconciliation_history", history[-50:])
