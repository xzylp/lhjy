"""交易执行 API"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ..contracts import (
    BalanceSnapshot,
    CancelOrderRequest,
    OrderSnapshot,
    PlaceOrderRequest,
    PositionSnapshot,
    TradeSnapshot,
)
from ..infra.adapters import ExecutionAdapter
from ..infra.audit_store import StateStore
from ..notify.dispatcher import MessageDispatcher
from ..notify.templates import execution_order_event_template


def build_router(
    adapter: ExecutionAdapter,
    mode: str = "mock",
    dispatcher: MessageDispatcher | None = None,
    meeting_state_store: StateStore | None = None,
    execution_plane: str = "local_xtquant",
) -> APIRouter:
    router = APIRouter(prefix="/execution", tags=["execution"])

    def _lookup_order_name(order_id: str | None) -> tuple[str | None, str | None]:
        if not meeting_state_store or not order_id:
            return None, None
        journal = meeting_state_store.get("execution_order_journal", [])
        for item in reversed(journal):
            if item.get("order_id") == order_id:
                return item.get("name"), item.get("decision_id")
        return None, None

    def _dispatch_execution_event(
        title: str,
        *,
        symbol: str,
        name: str = "",
        account_id: str,
        side: str | None = None,
        quantity: int | None = None,
        price: float | None = None,
        order_id: str | None = None,
        status: str | None = None,
        decision_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        if not dispatcher:
            return
        content = execution_order_event_template(
            action=title,
            symbol=symbol,
            name=name,
            account_id=account_id,
            side=side,
            quantity=quantity,
            price=price,
            order_id=order_id,
            status=status,
            decision_id=decision_id,
            reason=reason,
        )
        dispatcher.dispatch_trade(title, content, level="info", force=True)

    def _append_order_journal(request: PlaceOrderRequest, order: OrderSnapshot, submitted_at: str) -> None:
        if not meeting_state_store:
            return
        journal = meeting_state_store.get("execution_order_journal", [])
        request_payload = request.model_dump()
        for item in reversed(journal):
            if item.get("order_id") != order.order_id:
                continue
            item.update(
                {
                    "trade_date": request.trade_date or item.get("trade_date") or datetime.now().date().isoformat(),
                    "account_id": request.account_id,
                    "symbol": request.symbol,
                    "decision_id": request.decision_id,
                    "submitted_at": submitted_at,
                    "playbook": request.playbook,
                    "regime": request.regime,
                    "exit_reason": request.exit_reason,
                    "request": request_payload,
                }
            )
            meeting_state_store.set("execution_order_journal", journal[-200:])
            return
        journal.append(
            {
                "trade_date": request.trade_date or datetime.now().date().isoformat(),
                "account_id": request.account_id,
                "order_id": order.order_id,
                "symbol": request.symbol,
                "name": request.symbol,
                "decision_id": request.decision_id,
                "submitted_at": submitted_at,
                "playbook": request.playbook,
                "regime": request.regime,
                "exit_reason": request.exit_reason,
                "request": request_payload,
                "source": "execution_api",
            }
        )
        meeting_state_store.set("execution_order_journal", journal[-200:])

    @router.get("/health")
    async def health():
        return {"status": "ok", "mode": mode}

    @router.get("/balance/{account_id}", response_model=BalanceSnapshot)
    async def get_balance(account_id: str):
        try:
            return adapter.get_balance(account_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"账户不存在: {account_id}")
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))

    @router.get("/positions/{account_id}", response_model=list[PositionSnapshot])
    async def get_positions(account_id: str):
        return adapter.get_positions(account_id)

    @router.get("/orders/{account_id}", response_model=list[OrderSnapshot])
    async def get_orders(account_id: str):
        return adapter.get_orders(account_id)

    @router.get("/orders/{account_id}/{order_id}", response_model=OrderSnapshot)
    async def get_order(account_id: str, order_id: str):
        try:
            return adapter.get_order(account_id, order_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"订单不存在: {order_id}")

    @router.get("/trades/{account_id}", response_model=list[TradeSnapshot])
    async def get_trades(account_id: str):
        return adapter.get_trades(account_id)

    @router.post("/orders")
    async def place_order(request: PlaceOrderRequest):
        if execution_plane == "windows_gateway":
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "status": "blocked",
                    "reason": "execution_plane_windows_gateway_requires_gateway_dispatch",
                    "execution_plane": execution_plane,
                    "dispatch_path": "/system/discussions/execution-intents/dispatch",
                    "summary_lines": ["当前执行平面为 windows_gateway，/execution/orders 不允许直接下单。"],
                },
            )
        try:
            order = adapter.place_order(request)
            submitted_at = datetime.now().isoformat()
            _dispatch_execution_event(
                "交易下单",
                symbol=request.symbol,
                account_id=request.account_id,
                side=request.side,
                quantity=request.quantity,
                price=request.price,
                order_id=order.order_id,
                status=order.status,
                decision_id=request.decision_id,
                reason=request.exit_reason,
            )
            _append_order_journal(request, order, submitted_at)
            return order
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.delete("/orders", response_model=OrderSnapshot)
    async def cancel_order(request: CancelOrderRequest):
        try:
            existing = None
            try:
                existing = adapter.get_order(request.account_id, request.order_id)
            except KeyError:
                existing = None
            cancelled = adapter.cancel_order(request.account_id, request.order_id)
            name, decision_id = _lookup_order_name(request.order_id)
            if meeting_state_store:
                journal = meeting_state_store.get("execution_order_journal", [])
                for item in journal:
                    if item.get("order_id") == request.order_id:
                        item["latest_status"] = cancelled.status
                        item["cancelled_at"] = datetime.now().isoformat()
                        break
                meeting_state_store.set("execution_order_journal", journal[-200:])
            _dispatch_execution_event(
                "交易撤单",
                symbol=(cancelled.symbol or (existing.symbol if existing else "")),
                name=name or "",
                account_id=request.account_id,
                side=(cancelled.side if cancelled.quantity > 0 else (existing.side if existing else None)),
                quantity=(cancelled.quantity if cancelled.quantity > 0 else (existing.quantity if existing else None)),
                price=(cancelled.price if cancelled.price > 0 else (existing.price if existing else None)),
                order_id=request.order_id,
                status=cancelled.status,
                decision_id=decision_id,
            )
            return cancelled
        except KeyError:
            raise HTTPException(status_code=404, detail=f"订单不存在: {request.order_id}")

    return router
