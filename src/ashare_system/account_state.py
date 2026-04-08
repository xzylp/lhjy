"""账户真实性校验与日内状态快照。"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .governance.param_service import ParameterService
from .infra.adapters import ExecutionAdapter
from .infra.audit_store import StateStore
from .portfolio import build_test_trading_budget, summarize_position_buckets
from .runtime_config import RuntimeConfigManager
from .settings import AppSettings


class AccountStateService:
    """构建账户状态快照，并维护当日基线。"""

    def __init__(
        self,
        settings: AppSettings,
        execution_adapter: ExecutionAdapter,
        state_store: StateStore,
        config_mgr: RuntimeConfigManager | None = None,
        parameter_service: ParameterService | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._execution_adapter = execution_adapter
        self._state_store = state_store
        self._config_mgr = config_mgr
        self._parameter_service = parameter_service
        self._now_factory = now_factory or datetime.now

    def snapshot(self, account_id: str, persist: bool = True) -> dict:
        now = self._now_factory()
        trade_date = now.date().isoformat()
        try:
            balance = self._execution_adapter.get_balance(account_id)
            positions = self._execution_adapter.get_positions(account_id)
            trades = self._execution_adapter.get_trades(account_id)
        except Exception as exc:
            payload = {
                "account_id": account_id,
                "trade_date": trade_date,
                "captured_at": now.isoformat(),
                "status": "error",
                "verified": False,
                "error": str(exc),
                "summary_lines": [f"账户状态获取失败: {exc}"],
            }
            if persist:
                self._persist(payload)
            return payload

        baselines = self._state_store.get("account_session_baselines", {})
        baseline_key = f"{trade_date}:{account_id}"
        baseline = baselines.get(baseline_key)
        if baseline is None:
            baseline = {
                "trade_date": trade_date,
                "account_id": account_id,
                "baseline_total_asset": balance.total_asset,
                "baseline_cash": balance.cash,
                "captured_at": now.isoformat(),
            }
            baselines[baseline_key] = baseline
            self._state_store.set("account_session_baselines", baselines)

        total_asset = float(balance.total_asset)
        cash = float(balance.cash)
        runtime_config = self._config_mgr.get() if self._config_mgr else None
        equity_position_limit = (
            float(self._parameter_service.get_param_value("equity_position_limit"))
            if self._parameter_service
            else float(getattr(runtime_config, "equity_position_limit", 0.2) or 0.2)
        )
        reverse_repo_target_ratio = (
            float(self._parameter_service.get_param_value("reverse_repo_target_ratio"))
            if self._parameter_service
            else float(getattr(runtime_config, "reverse_repo_target_ratio", 0.7) or 0.7)
        )
        minimum_total_invested_amount = (
            float(self._parameter_service.get_param_value("minimum_total_invested_amount"))
            if self._parameter_service
            else float(getattr(runtime_config, "minimum_total_invested_amount", 100000.0) or 100000.0)
        )
        reverse_repo_reserved_amount = (
            float(self._parameter_service.get_param_value("reverse_repo_reserved_amount"))
            if self._parameter_service
            else float(getattr(runtime_config, "reverse_repo_reserved_amount", 70000.0) or 70000.0)
        )
        buckets = summarize_position_buckets(positions)
        invested_value = buckets.equity_value
        reverse_repo_value = buckets.reverse_repo_value
        gross_invested_value = buckets.total_value
        trading_budget = build_test_trading_budget(
            invested_value,
            reverse_repo_value,
            minimum_total_invested_amount=minimum_total_invested_amount,
            reverse_repo_reserved_amount=reverse_repo_reserved_amount,
        )
        unrealized_pnl = sum((float(item.last_price) - float(item.cost_price)) * float(item.quantity) for item in positions)
        daily_pnl = round(total_asset - float(baseline["baseline_total_asset"]), 4)
        realized_pnl_estimate = round(daily_pnl - unrealized_pnl, 4)
        current_total_ratio = invested_value / max(total_asset, 1e-9)
        reverse_repo_ratio = reverse_repo_value / max(total_asset, 1e-9)
        gross_total_ratio = gross_invested_value / max(total_asset, 1e-9)
        reverse_repo_target_value = total_asset * reverse_repo_target_ratio
        reverse_repo_gap_value = trading_budget.reverse_repo_gap_value
        available_test_trade_value = trading_budget.stock_test_budget_remaining
        config_account_id = str(self._settings.xtquant.account_id)
        fetched_account_id = str(balance.account_id)
        verified = fetched_account_id == str(account_id)
        config_match = fetched_account_id == config_account_id

        payload = {
            "account_id": account_id,
            "trade_date": trade_date,
            "captured_at": now.isoformat(),
            "status": "ok",
            "verified": verified,
            "config_match": config_match,
            "configured_account_id": config_account_id,
            "fetched_account_id": fetched_account_id,
            "configured_account_type": str(self._settings.xtquant.account_type),
            "balance": balance.model_dump(),
            "position_count": len(positions),
            "trade_count": len(trades),
            "positions": [item.model_dump() for item in positions],
            "equity_positions": [item.model_dump() for item in buckets.equity_positions],
            "reverse_repo_positions": [item.model_dump() for item in buckets.reverse_repo_positions],
            "baseline": baseline,
            "metrics": {
                "total_asset": round(total_asset, 4),
                "cash": round(cash, 4),
                "invested_value": round(invested_value, 4),
                "gross_invested_value": round(gross_invested_value, 4),
                "reverse_repo_value": round(reverse_repo_value, 4),
                "current_total_ratio": round(current_total_ratio, 6),
                "gross_total_ratio": round(gross_total_ratio, 6),
                "reverse_repo_ratio": round(reverse_repo_ratio, 6),
                "equity_position_limit": round(equity_position_limit, 6),
                "reverse_repo_target_ratio": round(reverse_repo_target_ratio, 6),
                "reverse_repo_target_value": round(reverse_repo_target_value, 4),
                "reverse_repo_gap_value": round(reverse_repo_gap_value, 4),
                "minimum_total_invested_amount": round(minimum_total_invested_amount, 4),
                "reverse_repo_reserved_amount": round(reverse_repo_reserved_amount, 4),
                "stock_test_budget_amount": round(trading_budget.stock_test_budget_amount, 4),
                "available_test_trade_value": round(available_test_trade_value, 4),
                "daily_pnl": daily_pnl,
                "unrealized_pnl": round(unrealized_pnl, 4),
                "realized_pnl_estimate": realized_pnl_estimate,
            },
            "summary_lines": [
                f"账户状态: account={account_id} fetched={fetched_account_id} verified={verified} config_match={config_match}。",
                f"资产 total={round(total_asset, 2)} cash={round(cash, 2)} equity={round(invested_value, 2)} repo={round(reverse_repo_value, 2)} gross={round(gross_invested_value, 2)} daily_pnl={daily_pnl:+.2f}。",
                f"测试基线 total>={round(minimum_total_invested_amount, 2)}，repo 保留 {round(reverse_repo_reserved_amount, 2)}，股票测试预算 {round(trading_budget.stock_test_budget_amount, 2)}，当前剩余 {round(available_test_trade_value, 2)}。",
            ],
        }
        if reverse_repo_value > 0:
            payload["summary_lines"].append("检测到逆回购持仓，按类现金保留仓处理，不占用股票测试仓位。")
        elif reverse_repo_gap_value > 0:
            payload["summary_lines"].append(
                f"当前逆回购低于保留金额，若到期或空仓，建议优先回补约 {round(reverse_repo_gap_value, 2)}。"
            )
        if persist:
            self._persist(payload)
        return payload

    def _persist(self, payload: dict) -> None:
        history = self._state_store.get("account_state_history", [])
        history.append(
            {
                "account_id": payload.get("account_id"),
                "trade_date": payload.get("trade_date"),
                "captured_at": payload.get("captured_at"),
                "status": payload.get("status"),
                "verified": payload.get("verified"),
                "config_match": payload.get("config_match"),
                "daily_pnl": (payload.get("metrics") or {}).get("daily_pnl"),
                "position_count": payload.get("position_count", 0),
                "trade_count": payload.get("trade_count", 0),
            }
        )
        self._state_store.set("latest_account_state", payload)
        self._state_store.set("account_state_history", history[-100:])
