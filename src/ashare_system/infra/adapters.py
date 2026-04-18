"""交易执行适配器 — 基类 + Mock + XtQuant / Windows Proxy 真实适配器"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

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
from .go_client import GoPlatformClient

logger = get_logger("execution.adapter")

XTQUANT_CONNECT_RETRY_ATTEMPTS = 3
XTQUANT_CONNECT_RETRY_BACKOFF_SEC = 1.0


def _sanitize_gateway_detail(detail: Any) -> str:
    text = str(detail or "").strip()
    if not text:
        return text
    if "http://127.0.0.1:18792" in text or "127.0.0.1:18792" in text:
        return (
            "Windows 18791 网关已收到请求，但其内部交易桥 127.0.0.1:18792 当前不可用；"
            f"原始错误: {text}"
        )
    return text


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


class WindowsProxyExecutionAdapter(ExecutionAdapter):
    """通过 Windows 侧 HTTP 交易桥访问 QMT。"""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._base_url = str(settings.windows_gateway.base_url or "").rstrip("/")
        if not self._base_url:
            raise RuntimeError("ASHARE_WINDOWS_GATEWAY_BASE_URL 未配置")
        self._timeout_sec = float(settings.windows_gateway.timeout_sec or 10.0)
        self._token = self._load_token()
        if not self._token:
            raise RuntimeError("Windows Gateway token 未配置，请设置 ASHARE_WINDOWS_GATEWAY_TOKEN 或 TOKEN_FILE")
        self._client = httpx.Client(timeout=self._timeout_sec, trust_env=False)

    def _timeout_for_path(self, method: str, path: str) -> float:
        if method == "GET" and path in {"/qmt/account/asset", "/qmt/account/positions"}:
            return min(self._timeout_sec, 8.0)
        if method == "GET" and path in {"/qmt/account/orders", "/qmt/account/trades"}:
            return min(self._timeout_sec, 12.0)
        if method == "POST" and path == "/qmt/trade/order":
            return min(self._timeout_sec, 8.0)
        return self._timeout_sec

    @staticmethod
    def _gateway_error_prefix(path: str, status_code: int) -> str:
        if status_code == 429:
            return "windows_gateway_overloaded"
        if status_code == 401:
            return "windows_gateway_auth_failed"
        if status_code == 404:
            return "windows_gateway_not_found"
        if status_code == 408:
            return "windows_gateway_timeout"
        if 500 <= status_code <= 599:
            return "windows_gateway_upstream_error"
        return "windows_gateway_http_error"

    def _load_token(self) -> str:
        explicit = str(self.settings.windows_gateway.token or "").strip()
        if explicit:
            return explicit
        token_file = str(self.settings.windows_gateway.token_file or "").strip()
        if not token_file:
            return ""
        path = Path(token_file)
        if not path.exists():
            raise RuntimeError(f"Windows Gateway token 文件不存在: {path}")
        return path.read_text(encoding="utf-8").strip()

    def _headers(self) -> dict[str, str]:
        return {"X-Ashare-Token": self._token}

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        request_timeout = timeout or self._timeout_for_path(method, path)
        try:
            response = self._client.request(
                method,
                f"{self._base_url}{path}",
                headers=self._headers(),
                params=params,
                json=json_payload,
                timeout=request_timeout,
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"windows_gateway_timeout: {path} | elapsed>{request_timeout:.1f}s") from exc
        except httpx.ConnectError as exc:
            raise RuntimeError(f"windows_gateway_unavailable: {path} | {exc}") from exc
        if response.status_code >= 400:
            detail = ""
            try:
                raw_payload = response.json()
                if isinstance(raw_payload, dict):
                    detail = str(
                        raw_payload.get("last_error")
                        or raw_payload.get("message")
                        or raw_payload.get("error")
                        or raw_payload.get("detail")
                        or ""
                    )
            except Exception:
                detail = response.text[:200]
            prefix = self._gateway_error_prefix(path, response.status_code)
            raise RuntimeError(f"{prefix}: {path} | {_sanitize_gateway_detail(detail)}".strip())
        payload = dict(response.json())
        if not payload.get("ok", False):
            error = (
                payload.get("last_error")
                or payload.get("message")
                or payload.get("error")
                or payload.get("detail")
                or "windows_gateway_request_failed"
            )
            error_text = _sanitize_gateway_detail(error)
            if "invalid token" in error_text.lower():
                raise RuntimeError(f"windows_gateway_auth_failed: {path} | {error_text}")
            raise RuntimeError(f"windows_gateway_request_failed: {path} | {error_text}")
        return payload

    @staticmethod
    def _normalize_side(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"buy", "b", "23"}:
            return "BUY"
        if text in {"sell", "s", "24"}:
            return "SELL"
        return "BUY"

    @staticmethod
    def _normalize_status(value: Any) -> str:
        mapping = {
            "48": "PENDING",
            "49": "PENDING",
            "50": "ACCEPTED",
            "51": "CANCEL_REQUESTED",
            "52": "CANCEL_REQUESTED",
            "53": "CANCEL_REQUESTED",
            "54": "CANCELLED",
            "55": "PARTIAL_FILLED",
            "56": "FILLED",
            "57": "REJECTED",
            "pending": "PENDING",
            "accepted": "ACCEPTED",
            "partial_filled": "PARTIAL_FILLED",
            "filled": "FILLED",
            "cancel_requested": "CANCEL_REQUESTED",
            "cancelled": "CANCELLED",
            "canceled": "CANCELLED",
            "rejected": "REJECTED",
        }
        return mapping.get(str(value or "").strip().lower(), "UNKNOWN")

    def get_balance(self, account_id: str) -> BalanceSnapshot:
        payload = self._request_json("GET", "/qmt/account/asset")
        asset = dict(payload.get("asset") or {})
        resolved_account_id = str(payload.get("account_id") or asset.get("account_id") or account_id)
        return BalanceSnapshot(
            account_id=resolved_account_id,
            total_asset=float(asset.get("total_asset", 0.0) or 0.0),
            cash=float(asset.get("cash", 0.0) or 0.0),
            frozen_cash=float(asset.get("frozen_cash", 0.0) or 0.0),
        )

    def get_positions(self, account_id: str) -> list[PositionSnapshot]:
        payload = self._request_json("GET", "/qmt/account/positions")
        positions = []
        for item in list(payload.get("positions") or []):
            positions.append(
                PositionSnapshot(
                    account_id=str(payload.get("account_id") or item.get("account_id") or account_id),
                    symbol=str(item.get("stock_code") or item.get("symbol") or ""),
                    quantity=int(item.get("volume", 0) or 0),
                    available=int(item.get("can_use_volume", 0) or 0),
                    cost_price=float(item.get("open_price", item.get("avg_price", 0.0)) or 0.0),
                    last_price=float(
                        item.get("last_price")
                        or (
                            float(item.get("market_value", 0.0) or 0.0) / max(int(item.get("volume", 0) or 0), 1)
                        )
                    ),
                )
            )
        return positions

    def get_orders(self, account_id: str) -> list[OrderSnapshot]:
        payload = self._request_json("GET", "/qmt/account/orders")
        orders = []
        for item in list(payload.get("orders") or []):
            orders.append(
                OrderSnapshot(
                    order_id=str(item.get("order_id") or ""),
                    account_id=str(payload.get("account_id") or item.get("account_id") or account_id),
                    symbol=str(item.get("stock_code") or item.get("symbol") or ""),
                    side=self._normalize_side(item.get("side") or item.get("order_type")),
                    quantity=int(item.get("order_volume", item.get("quantity", 0)) or 0),
                    price=float(item.get("price", 0.0) or 0.0),
                    status=self._normalize_status(item.get("status") or item.get("order_status")),
                )
            )
        return orders

    def get_order(self, account_id: str, order_id: str) -> OrderSnapshot:
        payload = self._request_json("POST", "/qmt/trade/order_status", json_payload={"order_id": int(order_id)})
        item = dict(payload.get("order") or {})
        if not item:
            raise KeyError(order_id)
        return OrderSnapshot(
            order_id=str(payload.get("order_id") or item.get("order_id") or order_id),
            account_id=str(payload.get("account_id") or item.get("account_id") or account_id),
            symbol=str(item.get("stock_code") or item.get("symbol") or ""),
            side=self._normalize_side(item.get("side") or item.get("order_type")),
            quantity=int(item.get("order_volume", item.get("quantity", 0)) or 0),
            price=float(item.get("price", 0.0) or 0.0),
            status=self._normalize_status(item.get("status") or item.get("order_status")),
        )

    def get_trades(self, account_id: str) -> list[TradeSnapshot]:
        payload = self._request_json("GET", "/qmt/account/trades")
        trades = []
        for item in list(payload.get("trades") or []):
            trades.append(
                TradeSnapshot(
                    trade_id=str(item.get("traded_id") or item.get("trade_id") or item.get("order_id") or ""),
                    order_id=str(item.get("order_id") or ""),
                    account_id=str(payload.get("account_id") or item.get("account_id") or account_id),
                    symbol=str(item.get("stock_code") or item.get("symbol") or ""),
                    side=self._normalize_side(item.get("side") or item.get("order_type")),
                    quantity=int(item.get("traded_volume", item.get("quantity", 0)) or 0),
                    price=float(item.get("traded_price", item.get("price", 0.0)) or 0.0),
                )
            )
        return trades

    def place_order(self, request: PlaceOrderRequest) -> OrderSnapshot:
        payload = self._request_json(
            "POST",
            "/qmt/trade/order",
            json_payload={
                "stock_code": request.symbol,
                "side": request.side.lower(),
                "quantity": request.quantity,
                "price": request.price,
                "price_type": "fix",
                "strategy_name": self.settings.strategy_name,
                "remark": request.request_id,
            },
        )
        order_id = str(payload.get("order_id") or "")
        if not order_id:
            raise RuntimeError("Windows Gateway 下单成功但未返回 order_id")
        try:
            return self.get_order(request.account_id, order_id)
        except Exception:
            return OrderSnapshot(
                order_id=order_id,
                account_id=request.account_id,
                symbol=request.symbol,
                side=request.side,
                quantity=request.quantity,
                price=request.price,
                status="PENDING",
            )

    def cancel_order(self, account_id: str, order_id: str) -> OrderSnapshot:
        raise RuntimeError("Windows Gateway 当前未暴露撤单接口")


class GoPlatformExecutionAdapter(WindowsProxyExecutionAdapter):
    """通过 Linux 本地 Go 并发数据平台访问执行。"""

    def __init__(self, settings: AppSettings) -> None:
        super().__init__(settings)
        self._go_client = GoPlatformClient(settings)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """覆盖请求逻辑，优先使用 Go 平台，失败时对读请求 fallback 到 Windows Gateway"""
        try:
            if method == "GET":
                return self._go_client.get_json(path, params=params, timeout=timeout)
            response = self._go_client.request(method, path, params=params, json=json_payload, timeout=timeout)
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError(f"go_platform_invalid_payload: {path}")
            return payload
        except Exception as exc:
            if method == "GET":
                logger.warning(f"go_platform_exec_fallback | GET {path} | error={exc} | falling back to windows_proxy")
                try:
                    result = super()._request_json(
                        method,
                        path,
                        params=params,
                        json_payload=json_payload,
                        timeout=timeout,
                    )
                    if isinstance(result, dict):
                        result["_fallback"] = True
                        result["_fallback_reason"] = str(exc)
                    return result
                except Exception as fallback_exc:
                    logger.error(f"go_platform_exec_fallback_failed | GET {path} | error={fallback_exc}")
                    raise
            
            logger.error(f"go_platform_exec_error | {method} {path} | error={exc}")
            raise


def build_execution_adapter(mode: str, settings: AppSettings) -> ExecutionAdapter:
    if mode == "go_platform":
        adapter = GoPlatformExecutionAdapter(settings)
        adapter.mode = "go_platform"
        return adapter
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
    if mode == "windows_proxy":
        adapter = WindowsProxyExecutionAdapter(settings)
        adapter.mode = "windows_proxy"
        return adapter
    adapter = MockExecutionAdapter()
    adapter.mode = "mock"
    return adapter
