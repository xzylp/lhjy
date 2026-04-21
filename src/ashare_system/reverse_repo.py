"""逆回购缺口检测与自动回补。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable

from .execution_safety import is_trading_session
from .governance.param_service import ParameterService
from .infra.adapters import ExecutionAdapter
from .infra.audit_store import StateStore
from .notify.dispatcher import MessageDispatcher
from .portfolio import (
    build_test_trading_budget,
    is_reverse_repo_symbol,
    reverse_repo_min_volume,
    reverse_repo_order_amount,
    reverse_repo_volume_for_amount,
    summarize_position_buckets,
)
from .runtime_config import RuntimeConfigManager
from .settings import AppSettings

FINAL_ORDER_STATUSES = {"FILLED", "CANCELLED", "REJECTED"}


@dataclass(frozen=True)
class ReverseRepoInstrument:
    symbol: str
    name: str
    days: int
    market: str


REVERSE_REPO_INSTRUMENTS = (
    ReverseRepoInstrument(symbol="204004.SH", name="沪市4天逆回购", days=4, market="SH"),
    ReverseRepoInstrument(symbol="131809.SZ", name="深市4天逆回购", days=4, market="SZ"),
    ReverseRepoInstrument(symbol="204003.SH", name="沪市3天逆回购", days=3, market="SH"),
    ReverseRepoInstrument(symbol="131800.SZ", name="深市3天逆回购", days=3, market="SZ"),
)


class ReverseRepoService:
    """围绕测试阶段逆回购保留仓的自动管理。"""

    def __init__(
        self,
        settings: AppSettings,
        execution_adapter: ExecutionAdapter,
        market_adapter,
        state_store: StateStore,
        config_mgr: RuntimeConfigManager | None = None,
        parameter_service: ParameterService | None = None,
        dispatcher: MessageDispatcher | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._execution_adapter = execution_adapter
        self._market_adapter = market_adapter
        self._state_store = state_store
        self._config_mgr = config_mgr
        self._parameter_service = parameter_service
        self._dispatcher = dispatcher
        self._now_factory = now_factory or datetime.now

    def inspect(self, account_id: str, auto_submit: bool = False, persist: bool = True) -> dict:
        now = self._now_factory()
        runtime_config = self._config_mgr.get() if self._config_mgr else None
        enabled = bool(getattr(runtime_config, "reverse_repo_auto_repurchase_enabled", True))
        min_term_days = int(getattr(runtime_config, "reverse_repo_min_term_days", 3) or 3)
        max_term_days = int(getattr(runtime_config, "reverse_repo_max_term_days", 4) or 4)
        prefer_longer_term = bool(getattr(runtime_config, "reverse_repo_prefer_longer_term", True))
        session_open = is_trading_session(now)
        live_submit_allowed = (
            auto_submit
            and enabled
            and self._settings.run_mode == "live"
            and self._settings.live_trade_enabled
            and session_open
        )

        try:
            balance = self._execution_adapter.get_balance(account_id)
            positions = self._execution_adapter.get_positions(account_id)
            orders = self._execution_adapter.get_orders(account_id)
        except Exception as exc:
            payload = {
                "account_id": account_id,
                "checked_at": now.isoformat(),
                "status": "error",
                "auto_submit": auto_submit,
                "enabled": enabled,
                "session_open": session_open,
                "error": str(exc),
                "summary_lines": [f"逆回购回补检查失败: {exc}"],
            }
            if persist:
                self._persist(payload)
            return payload

        minimum_total_invested_amount = self._resolve_amount(
            "minimum_total_invested_amount",
            runtime_config,
            default=100000.0,
        )
        reverse_repo_reserved_amount = self._resolve_amount(
            "reverse_repo_reserved_amount",
            runtime_config,
            default=70000.0,
        )
        stock_position_limit_ratio = (
            float(self._parameter_service.get_param_value("equity_position_limit"))
            if self._parameter_service
            else float(getattr(runtime_config, "equity_position_limit", 0.3) or 0.3)
        )

        buckets = summarize_position_buckets(positions)
        budget = build_test_trading_budget(
            float(getattr(balance, "total_asset", 0.0) or 0.0),
            buckets.equity_value,
            buckets.reverse_repo_value,
            minimum_total_invested_amount=minimum_total_invested_amount,
            reverse_repo_reserved_amount=reverse_repo_reserved_amount,
            stock_position_limit_ratio=stock_position_limit_ratio,
        )
        reverse_repo_gap_value = budget.reverse_repo_gap_value
        pending_orders = [
            order.model_dump()
            for order in orders
            if is_reverse_repo_symbol(order.symbol) and order.status not in FINAL_ORDER_STATUSES
        ]
        affordable_amount = min(float(balance.cash), float(reverse_repo_gap_value))
        quotes = self._fetch_quotes([item.symbol for item in REVERSE_REPO_INSTRUMENTS])
        candidate_quotes = self._build_candidate_quotes(
            quotes,
            affordable_amount=affordable_amount,
            min_term_days=min_term_days,
            max_term_days=max_term_days,
            prefer_longer_term=prefer_longer_term,
        )
        selected_candidate = next((item for item in candidate_quotes if item.get("eligible")), None)

        status = "no_gap"
        reason = "reverse_repo_gap_closed"
        submitted_order = None
        dispatch_status = "skipped"
        request_payload = None

        if not enabled:
            status = "disabled"
            reason = "auto_repurchase_disabled"
        elif reverse_repo_gap_value <= 0:
            status = "no_gap"
            reason = "reverse_repo_gap_closed"
        elif pending_orders:
            status = "pending_order_exists"
            reason = "existing_reverse_repo_order_pending"
        elif selected_candidate is None:
            status = "no_candidate"
            reason = "no_affordable_quote_candidate"
        elif auto_submit and not session_open:
            status = "waiting_session"
            reason = "trading_session_closed"
        elif auto_submit and self._settings.run_mode == "live" and not self._settings.live_trade_enabled:
            status = "blocked"
            reason = "live_trade_disabled"
        elif live_submit_allowed:
            try:
                from .contracts import PlaceOrderRequest

                request_payload = {
                    "account_id": account_id,
                    "symbol": selected_candidate["symbol"],
                    "side": "SELL",
                    "quantity": selected_candidate["order_volume"],
                    "price": selected_candidate["last_price"],
                    "request_id": (
                        f"reverse-repo-{now.strftime('%Y%m%d')}-"
                        f"{selected_candidate['symbol']}-{selected_candidate['order_volume']}"
                    ),
                }
                order = self._execution_adapter.place_order(PlaceOrderRequest(**request_payload))
                submitted_order = order.model_dump()
                status = "submitted"
                reason = "reverse_repo_repurchase_submitted"
                dispatch_status = self._dispatch_submit_message(
                    account_id=account_id,
                    candidate=selected_candidate,
                    order=submitted_order,
                    gap_value=reverse_repo_gap_value,
                )
            except Exception as exc:
                status = "submit_failed"
                reason = str(exc)
        else:
            status = "planned"
            reason = "plan_ready"

        payload = {
            "account_id": account_id,
            "checked_at": now.isoformat(),
            "status": status,
            "reason": reason,
            "enabled": enabled,
            "auto_submit": auto_submit,
            "live_submit_allowed": live_submit_allowed,
            "session_open": session_open,
            "run_mode": self._settings.run_mode,
            "live_trade_enabled": self._settings.live_trade_enabled,
            "cash_available": round(float(balance.cash), 2),
            "reverse_repo_value": round(float(buckets.reverse_repo_value), 2),
            "reverse_repo_gap_value": round(float(reverse_repo_gap_value), 2),
            "minimum_total_invested_amount": round(float(minimum_total_invested_amount), 2),
            "reverse_repo_reserved_amount": round(float(reverse_repo_reserved_amount), 2),
            "affordable_amount": round(float(affordable_amount), 2),
            "min_term_days": min_term_days,
            "max_term_days": max_term_days,
            "prefer_longer_term": prefer_longer_term,
            "pending_orders": pending_orders,
            "candidates": candidate_quotes,
            "selected_candidate": selected_candidate,
            "submitted_order": submitted_order,
            "request": request_payload,
            "dispatch_status": dispatch_status,
            "summary_lines": self._build_summary_lines(
                account_id=account_id,
                reverse_repo_gap_value=reverse_repo_gap_value,
                reverse_repo_reserved_amount=reverse_repo_reserved_amount,
                reverse_repo_value=buckets.reverse_repo_value,
                cash=float(balance.cash),
                status=status,
                selected_candidate=selected_candidate,
                pending_orders=pending_orders,
                session_open=session_open,
            ),
        }
        if persist:
            self._persist(payload)
        return payload

    def latest(self) -> dict:
        return self._state_store.get("latest_reverse_repo_repurchase", {}) or {}

    def _resolve_amount(self, param_key: str, runtime_config, default: float) -> float:
        if self._parameter_service:
            for item in self._parameter_service.list_params():
                if item["param_key"] != param_key:
                    continue
                if item.get("current_layer") != "system_defaults":
                    return float(item["current_value"])
                break
        return float(getattr(runtime_config, param_key, default) or default)

    def _fetch_quotes(self, symbols: list[str]) -> dict[str, dict]:
        if not self._market_adapter:
            return {}
        try:
            snapshots = self._market_adapter.get_index_quotes(symbols)
        except Exception:
            return {}
        return {
            item.symbol: {
                "symbol": item.symbol,
                "name": item.name,
                "last_price": float(item.last_price or 0.0),
                "bid_price": float(item.bid_price or 0.0),
                "ask_price": float(item.ask_price or 0.0),
                "volume": float(item.volume or 0.0),
            }
            for item in snapshots
        }

    def _build_candidate_quotes(
        self,
        quotes: dict[str, dict],
        *,
        affordable_amount: float,
        min_term_days: int,
        max_term_days: int,
        prefer_longer_term: bool,
    ) -> list[dict]:
        instruments = [
            item
            for item in REVERSE_REPO_INSTRUMENTS
            if min_term_days <= item.days <= max_term_days
        ]
        instruments = sorted(
            instruments,
            key=lambda item: ((-item.days if prefer_longer_term else item.days), item.market),
        )
        quote_rows: list[dict] = []
        for item in instruments:
            quote = quotes.get(item.symbol, {})
            last_price = float(quote.get("last_price", 0.0) or 0.0)
            order_volume = reverse_repo_volume_for_amount(item.symbol, affordable_amount)
            minimum_volume = reverse_repo_min_volume(item.symbol)
            planned_amount = reverse_repo_order_amount(item.symbol, order_volume) if order_volume > 0 else 0.0
            eligible = last_price > 0 and order_volume >= minimum_volume and planned_amount > 0
            quote_rows.append(
                {
                    **asdict(item),
                    "quote_name": quote.get("name") or item.name,
                    "last_price": round(last_price, 4),
                    "bid_price": round(float(quote.get("bid_price", 0.0) or 0.0), 4),
                    "ask_price": round(float(quote.get("ask_price", 0.0) or 0.0), 4),
                    "quote_volume": round(float(quote.get("volume", 0.0) or 0.0), 2),
                    "order_volume": order_volume,
                    "minimum_order_volume": minimum_volume,
                    "planned_amount": round(planned_amount, 2),
                    "eligible": eligible,
                }
            )
        day_priority = sorted({item["days"] for item in quote_rows}, reverse=prefer_longer_term)
        prioritized: list[dict] = []
        for day in day_priority:
            same_day = [item for item in quote_rows if item["days"] == day]
            prioritized.extend(sorted(same_day, key=lambda item: item["last_price"], reverse=True))
        return prioritized

    def _dispatch_submit_message(self, *, account_id: str, candidate: dict, order: dict, gap_value: float) -> str:
        if not self._dispatcher:
            return "dispatcher_unavailable"
        content = (
            f"账户 {account_id} 逆回购自动回补已提交\n"
            f"标的: {candidate['symbol']} {candidate.get('quote_name') or candidate['name']}\n"
            f"期限: {candidate['days']}天\n"
            f"价格: {candidate['last_price']}\n"
            f"数量: {candidate['order_volume']}\n"
            f"金额: {candidate['planned_amount']}\n"
            f"缺口: {round(gap_value, 2)}\n"
            f"订单: {order.get('order_id')}"
        )
        dispatched = self._dispatcher.dispatch_trade("逆回购自动回补", content, level="info", force=True)
        return "sent" if dispatched else "dispatch_failed"

    @staticmethod
    def _build_summary_lines(
        *,
        account_id: str,
        reverse_repo_gap_value: float,
        reverse_repo_reserved_amount: float,
        reverse_repo_value: float,
        cash: float,
        status: str,
        selected_candidate: dict | None,
        pending_orders: list[dict],
        session_open: bool,
    ) -> list[str]:
        lines = [
            f"账户 {account_id} 逆回购巡检: status={status} session_open={session_open}。",
            f"逆回购保留目标 {round(reverse_repo_reserved_amount, 2)}，当前持有 {round(reverse_repo_value, 2)}，缺口 {round(reverse_repo_gap_value, 2)}，可用现金 {round(cash, 2)}。",
        ]
        if pending_orders:
            lead = pending_orders[0]
            lines.append(
                f"存在未完成逆回购委托 {lead.get('symbol')} order_id={lead.get('order_id')} status={lead.get('status')}，本轮不重复提交。"
            )
        elif selected_candidate:
            lines.append(
                f"候选优先级命中 {selected_candidate['symbol']} {selected_candidate.get('quote_name') or selected_candidate['name']}，"
                f"{selected_candidate['days']}天，价格 {selected_candidate['last_price']}，计划金额 {selected_candidate['planned_amount']}。"
            )
        return lines

    def _persist(self, payload: dict) -> None:
        history = self._state_store.get("reverse_repo_repurchase_history", [])
        history.append(
            {
                "checked_at": payload.get("checked_at"),
                "account_id": payload.get("account_id"),
                "status": payload.get("status"),
                "reason": payload.get("reason"),
                "reverse_repo_gap_value": payload.get("reverse_repo_gap_value", 0.0),
                "selected_symbol": (payload.get("selected_candidate") or {}).get("symbol"),
                "submitted_order_id": (payload.get("submitted_order") or {}).get("order_id"),
            }
        )
        self._state_store.set("latest_reverse_repo_repurchase", payload)
        self._state_store.set("reverse_repo_repurchase_history", history[-100:])
