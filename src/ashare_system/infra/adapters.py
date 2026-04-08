"""交易执行适配器 — 基类 + Mock + XtQuant 真实适配器"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from ..contracts import (
    BalanceSnapshot,
    OrderSnapshot,
    PlaceOrderRequest,
    PositionSnapshot,
    TradeSnapshot,
)
from ..logging_config import get_logger
from ..portfolio import is_reverse_repo_symbol, reverse_repo_order_amount
from ..settings import AppSettings

logger = get_logger("execution.adapter")

XTQUANT_CONNECT_RETRY_ATTEMPTS = 3
XTQUANT_CONNECT_RETRY_BACKOFF_SEC = 1.0


class ExecutionAdapter:
    """交易执行适配器基类"""

    def get_balance(self, account_id: str) -> BalanceSnapshot:
        raise NotImplementedError

    def get_positions(self, account_id: str) -> list[PositionSnapshot]:
        raise NotImplementedError

    def get_orders(self, account_id: str) -> list[OrderSnapshot]:
        raise NotImplementedError

    def get_order(self, account_id: str, order_id: str) -> OrderSnapshot:
        for order in self.get_orders(account_id):
            if order.order_id == order_id:
                return order
        raise KeyError(order_id)

    def get_trades(self, account_id: str) -> list[TradeSnapshot]:
        raise NotImplementedError

    def place_order(self, request: PlaceOrderRequest) -> OrderSnapshot:
        raise NotImplementedError

    def cancel_order(self, account_id: str, order_id: str) -> OrderSnapshot:
        raise NotImplementedError


@dataclass
class MockExecutionAdapter(ExecutionAdapter):
    """模拟交易适配器 (dry-run / 测试用)"""
    balances: dict[str, BalanceSnapshot] = field(default_factory=dict)
    positions: dict[str, list[PositionSnapshot]] = field(default_factory=dict)
    orders: dict[str, list[OrderSnapshot]] = field(default_factory=dict)
    trades: dict[str, list[TradeSnapshot]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.balances:
            self.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=1_000_000, cash=600_000)
        if not self.positions:
            self.positions["sim-001"] = [
                PositionSnapshot(account_id="sim-001", symbol="600519.SH", quantity=100, available=100, cost_price=1588.0, last_price=1592.5),
                PositionSnapshot(account_id="sim-001", symbol="000001.SZ", quantity=2000, available=2000, cost_price=10.2, last_price=10.5),
            ]
        self.orders.setdefault("sim-001", [])
        self.trades.setdefault("sim-001", [])

    def get_balance(self, account_id: str) -> BalanceSnapshot:
        return self.balances[account_id]

    def get_positions(self, account_id: str) -> list[PositionSnapshot]:
        return self.positions.get(account_id, [])

    def get_orders(self, account_id: str) -> list[OrderSnapshot]:
        return self.orders.get(account_id, [])

    def get_trades(self, account_id: str) -> list[TradeSnapshot]:
        return self.trades.get(account_id, [])

    def place_order(self, request: PlaceOrderRequest) -> OrderSnapshot:
        order = OrderSnapshot(
            order_id=f"order-{uuid4().hex[:10]}", account_id=request.account_id,
            symbol=request.symbol, side=request.side, quantity=request.quantity,
            price=request.price, status="PENDING",
        )
        self.orders.setdefault(request.account_id, []).append(order)
        return order

    def cancel_order(self, account_id: str, order_id: str) -> OrderSnapshot:
        for i, order in enumerate(self.orders.get(account_id, [])):
            if order.order_id == order_id:
                cancelled = order.model_copy(update={"status": "CANCELLED"})
                self.orders[account_id][i] = cancelled
                return cancelled
        raise KeyError(order_id)


class XtQuantExecutionAdapter(ExecutionAdapter):
    """XtQuant 真实交易适配器"""

    def __init__(self, settings: AppSettings) -> None:
        from .xtquant_runtime import load_xtquant_modules
        self.settings = settings
        self.modules = load_xtquant_modules(str(settings.xtquant.root), str(settings.xtquant.service_root))
        self._trader = None
        self._account = None
        self._request_store = settings.storage_root / "dispatched_requests.json"
        self._dispatched: set[str] = self._load_dispatched()

    def _load_dispatched(self) -> set[str]:
        if not self._request_store.exists():
            return set()
        return set(json.loads(self._request_store.read_text(encoding="utf-8")).get("request_ids", []))

    def _save_dispatched(self) -> None:
        self._request_store.write_text(json.dumps({"request_ids": sorted(self._dispatched)}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _mark_dispatched(self, request_id: str) -> None:
        self._dispatched.add(request_id)
        self._save_dispatched()

    @staticmethod
    def _is_retryable_submission_error(exc: Exception) -> bool:
        text = str(exc).lower()
        transient_signals = (
            "timeout",
            "timed out",
            "temporarily unavailable",
            "connection reset",
            "connection aborted",
            "connection refused",
            "network",
            "socket",
            "connect",
            "超时",
            "连接",
            "网络",
            "断开",
            "繁忙",
            "busy",
        )
        return any(signal in text for signal in transient_signals)

    def _submit_order(self, request: PlaceOrderRequest) -> int | float:
        trader, account = self._ensure_trader()
        c = self.modules["xtconstant"]
        return trader.order_stock(
            account,
            request.symbol,
            c.STOCK_BUY if request.side == "BUY" else c.STOCK_SELL,
            request.quantity,
            c.FIX_PRICE,
            request.price,
            self.settings.strategy_name,
            request.request_id,
        )

    def _submit_order_with_retry(self, request: PlaceOrderRequest) -> int | float:
        attempts = max(int(getattr(self.settings, "execution_submit_retry_attempts", 1) or 1), 1)
        backoff_ms = max(int(getattr(self.settings, "execution_submit_retry_backoff_ms", 0) or 0), 0)
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._submit_order(request)
            except Exception as exc:
                last_exc = exc
                retryable = self._is_retryable_submission_error(exc)
                if attempt >= attempts or not retryable:
                    raise
                logger.warning(
                    "下单提交失败，准备重试 request_id=%s attempt=%d/%d reason=%s",
                    request.request_id,
                    attempt,
                    attempts,
                    exc,
                )
                if backoff_ms > 0:
                    time.sleep(backoff_ms / 1000.0)
        if last_exc:
            raise last_exc
        raise RuntimeError("unreachable order submit state")

    def _connect_trader_with_retry(self, trader) -> int:
        last_result: int | None = None
        for attempt in range(1, XTQUANT_CONNECT_RETRY_ATTEMPTS + 1):
            result = trader.connect()
            if result in {0, -57}:
                return result
            last_result = result
            if attempt >= XTQUANT_CONNECT_RETRY_ATTEMPTS:
                break
            logger.warning(
                "XtQuant connect 失败，准备重试 attempt=%d/%d result=%s",
                attempt,
                XTQUANT_CONNECT_RETRY_ATTEMPTS,
                result,
            )
            time.sleep(XTQUANT_CONNECT_RETRY_BACKOFF_SEC)
        return int(last_result or -1)

    def _ensure_trader(self):
        if self._trader is not None and self._account is not None:
            return self._trader, self._account
        xttrader = self.modules["xttrader"]
        xttype = self.modules["xttype"]
        xtconstant = self.modules["xtconstant"]
        trader = xttrader.XtQuantTrader(str(self.settings.xtquant.userdata), int(self.settings.xtquant.session_id))
        trader.start()
        result = self._connect_trader_with_retry(trader)
        if result != 0:
            # -57 表示需要登录，尝试继续
            if result == -57:
                print("Warning: 需要 QMT 登录，继续尝试...")
            else:
                raise RuntimeError(f"XtQuant connect failed: {result}")
        account = xttype.StockAccount(self.settings.xtquant.account_id, self.settings.xtquant.account_type)
        sub_result = trader.subscribe(account)
        if sub_result != 0 and sub_result != -57:
            print(f"Warning: subscribe 返回 {sub_result}，可能已订阅")
        self._trader, self._account = trader, account
        return trader, account

    def _map_side(self, order_type: int) -> str:
        return "BUY" if order_type == self.modules["xtconstant"].STOCK_BUY else "SELL"

    def _map_status(self, status: int) -> str:
        c = self.modules["xtconstant"]
        if status in (c.ORDER_SUCCEEDED,):
            return "FILLED"
        if status in (c.ORDER_CANCELED, c.ORDER_PARTSUCC_CANCEL):
            return "CANCELLED"
        if status in (c.ORDER_PART_SUCC,):
            return "PARTIAL_FILLED"
        if status in (c.ORDER_REPORTED_CANCEL, c.ORDER_PART_CANCEL):
            return "CANCEL_REQUESTED"
        if status in (c.ORDER_REPORTED,):
            return "ACCEPTED"
        if status in (c.ORDER_JUNK,):
            return "REJECTED"
        return "UNKNOWN"

    def get_balance(self, account_id: str) -> BalanceSnapshot:
        trader, account = self._ensure_trader()
        a = trader.query_stock_asset(account)
        if a is None:
            raise RuntimeError(f"xtquant balance unavailable for account {account_id}")
        resolved_account_id = getattr(a, "account_id", None) or account_id
        return BalanceSnapshot(
            account_id=str(resolved_account_id),
            total_asset=float(getattr(a, "total_asset", 0.0) or 0.0),
            cash=float(getattr(a, "cash", 0.0) or 0.0),
            frozen_cash=float(getattr(a, "frozen_cash", 0.0) or 0.0),
        )

    def get_positions(self, account_id: str) -> list[PositionSnapshot]:
        trader, account = self._ensure_trader()
        return [
            PositionSnapshot(account_id=p.account_id, symbol=p.stock_code, quantity=int(p.volume), available=int(p.can_use_volume), cost_price=float(p.avg_price), last_price=float(p.market_value / p.volume) if p.volume else 0.0)
            for p in (trader.query_stock_positions(account) or [])
        ]

    def get_orders(self, account_id: str) -> list[OrderSnapshot]:
        trader, account = self._ensure_trader()
        return [
            OrderSnapshot(order_id=str(o.order_id), account_id=o.account_id, symbol=o.stock_code, side=self._map_side(o.order_type), quantity=int(o.order_volume), price=float(o.price), status=self._map_status(o.order_status))
            for o in (trader.query_stock_orders(account) or [])
        ]

    def get_trades(self, account_id: str) -> list[TradeSnapshot]:
        trader, account = self._ensure_trader()
        return [
            TradeSnapshot(trade_id=str(t.traded_id), order_id=str(t.order_id), account_id=t.account_id, symbol=t.stock_code, side=self._map_side(t.order_type), quantity=int(t.traded_volume), price=float(t.traded_price))
            for t in (trader.query_stock_trades(account) or [])
        ]

    def place_order(self, request: PlaceOrderRequest) -> OrderSnapshot:
        if request.request_id in self._dispatched:
            raise RuntimeError(f"幂等拦截: {request.request_id} 已发送")
        if request.side == "BUY":
            bal = self.get_balance(request.account_id)
            if bal.cash < request.quantity * request.price:
                raise ValueError(f"资金不足: 需要 {request.quantity * request.price}, 可用 {bal.cash}")
        elif request.side == "SELL":
            if is_reverse_repo_symbol(request.symbol):
                bal = self.get_balance(request.account_id)
                required_cash = reverse_repo_order_amount(request.symbol, request.quantity)
                if bal.cash < required_cash:
                    raise ValueError(f"逆回购资金不足: 需要 {required_cash}, 可用 {bal.cash}")
            else:
                avail = sum(p.available for p in self.get_positions(request.account_id) if p.symbol == request.symbol)
                if avail < request.quantity:
                    raise ValueError(f"持仓不足: 需要 {request.quantity}, 可用 {avail}")
        oid = self._submit_order_with_retry(request)
        if isinstance(oid, (int, float)) and oid < 0:
            raise RuntimeError(f"下单失败: {oid}")
        self._mark_dispatched(request.request_id)
        return OrderSnapshot(order_id=str(oid), account_id=request.account_id, symbol=request.symbol, side=request.side, quantity=request.quantity, price=request.price, status="PENDING")

    def cancel_order(self, account_id: str, order_id: str) -> OrderSnapshot:
        trader, account = self._ensure_trader()
        try:
            existing = self.get_order(account_id, order_id)
        except KeyError:
            existing = None
        r = trader.cancel_order_stock(account, int(order_id))
        if r != 0:
            if existing:
                return existing.model_copy(update={"status": "REJECTED"})
            return OrderSnapshot(
                order_id=str(order_id),
                account_id=account_id,
                symbol="",
                side="SELL",
                quantity=0,
                price=0.0,
                status="REJECTED",
            )
        try:
            refreshed = self.get_order(account_id, order_id)
        except KeyError:
            refreshed = existing
        if refreshed is None:
            return OrderSnapshot(
                order_id=str(order_id),
                account_id=account_id,
                symbol="",
                side="SELL",
                quantity=0,
                price=0.0,
                status="CANCEL_REQUESTED",
            )
        if refreshed.status == "CANCELLED":
            return refreshed
        return refreshed.model_copy(update={"status": "CANCEL_REQUESTED"})


def build_execution_adapter(mode: str, settings: AppSettings) -> ExecutionAdapter:
    if mode == "xtquant":
        try:
            adapter = XtQuantExecutionAdapter(settings)
            adapter.mode = "xtquant"
            return adapter
        except Exception as e:
            if settings.run_mode == "live":
                raise RuntimeError(f"live 模式禁止回退到 mock-fallback: {e}") from e
            print(f"Warning: XtQuant 适配器初始化失败: {e}")
            adapter = MockExecutionAdapter()
            adapter.mode = "mock-fallback"
            return adapter
    adapter = MockExecutionAdapter()
    adapter.mode = "mock"
    return adapter
