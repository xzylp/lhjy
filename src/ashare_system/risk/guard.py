"""执行守卫 — 拦截层"""

from __future__ import annotations

from ..contracts import PlaceOrderRequest, BalanceSnapshot, MarketProfile
from ..logging_config import get_logger
from ..runtime_config import RuntimeConfig
from .portfolio_risk import PortfolioPositionContext, PortfolioRiskChecker, PortfolioRiskResult
from .position_sizing import HistoricalTradeStats, PositionSizer, PositionSizingContext, PositionSizingResult
from .rules import RiskRules, RiskThresholds, RuleResult

logger = get_logger("risk.guard")


class ExecutionGuard:
    """执行守卫: 在下单前进行风控拦截"""

    def __init__(
        self,
        rules: RiskRules | None = None,
        *,
        position_sizer: PositionSizer | None = None,
        portfolio_checker: PortfolioRiskChecker | None = None,
    ) -> None:
        self.rules = rules or RiskRules()
        self.position_sizer = position_sizer or PositionSizer()
        self.portfolio_checker = portfolio_checker or PortfolioRiskChecker()
        self.last_position_sizing: PositionSizingResult | None = None
        self.last_portfolio_risk: PortfolioRiskResult | None = None

    @classmethod
    def from_runtime_config(cls, config: RuntimeConfig) -> "ExecutionGuard":
        return cls(
            rules=RiskRules(RiskThresholds.from_runtime_config(config)),
            position_sizer=PositionSizer(
                default_mode=str(getattr(config, "position_sizing_mode", "half_kelly") or "half_kelly"),  # type: ignore[arg-type]
                max_single_position=float(getattr(config, "max_single_position", 0.25) or 0.25),
                fixed_pct=float(getattr(config, "position_pct", 0.10) or 0.10),
                target_volatility=float(getattr(config, "target_position_volatility", 0.02) or 0.02),
            ),
            portfolio_checker=PortfolioRiskChecker(
                sector_concentration_limit=float(getattr(config, "portfolio_sector_concentration_limit", 0.40) or 0.40),
                daily_new_exposure_limit=float(getattr(config, "daily_new_exposure_limit", 0.30) or 0.30),
            ),
        )

    def approve(
        self,
        request: PlaceOrderRequest,
        balance: BalanceSnapshot,
        profile: MarketProfile | None = None,
        consecutive_losses: int = 0,
        daily_pnl: float = 0.0,
        *,
        realized_volatility: float | None = None,
        position_correlation: float | None = None,
        daily_turnover_amount: float | None = None,
        single_position_pnl_pct: float | None = None,
        existing_positions: list[PortfolioPositionContext] | None = None,
        candidate_sector: str | None = None,
        candidate_beta: float | None = None,
        regime_label: str | None = None,
        daily_new_exposure: float = 0.0,
        historical_trade_stats: HistoricalTradeStats | None = None,
        sizing_mode: str | None = None,
        open_slots: int = 1,
    ) -> tuple[bool, str, PlaceOrderRequest]:
        """
        审批下单请求。
        返回: (approved, reason, adjusted_request)
        """
        self.last_position_sizing = None
        self.last_portfolio_risk = None
        thresholds = getattr(self.rules, "thresholds", None)
        if not isinstance(thresholds, RiskThresholds):
            thresholds = RiskThresholds()
        if request.side == "SELL":
            return True, "卖出直接放行", request

        if self.position_sizer is not None:
            sizing_result = self.position_sizer.calculate(
                context=PositionSizingContext(
                    total_equity=float(balance.total_asset or 0.0),
                    cash_available=float(balance.cash or 0.0),
                    price=float(request.price or 0.0),
                    open_slots=max(int(open_slots or 1), 1),
                    max_single_position=thresholds.max_single_position,
                    max_single_amount=None,
                    target_volatility=float(getattr(self.position_sizer, "target_volatility", 0.02) or 0.02),
                ),
                stats=historical_trade_stats,
                mode=(str(sizing_mode).strip() if sizing_mode else None),  # type: ignore[arg-type]
            )
            self.last_position_sizing = sizing_result
            if sizing_result.quantity < 100:
                return False, sizing_result.reason or "动态仓位金额不足一手", request
            if sizing_result.quantity != request.quantity:
                request = request.model_copy(update={"quantity": int(sizing_result.quantity)})

        buy_value = request.quantity * request.price
        checks = self.rules.check_all_buy(
            buy_value,
            balance,
            profile,
            consecutive_losses,
            daily_pnl,
            realized_volatility=realized_volatility,
            position_correlation=position_correlation,
            daily_turnover_amount=daily_turnover_amount,
            single_position_pnl_pct=single_position_pnl_pct,
        )

        for check in checks:
            if check.result == RuleResult.REJECT:
                logger.warning("风控拦截 [%s]: %s", check.rule, check.reason)
                return False, check.reason, request

            if check.result == RuleResult.LIMIT and check.adjusted_value is not None:
                new_qty = int(check.adjusted_value / max(request.price, 1e-9) / 100) * 100
                if new_qty < 100:
                    return False, f"调整后数量不足100股: {check.reason}", request
                request = request.model_copy(update={"quantity": new_qty})
                logger.info("风控调整 [%s]: 数量 → %d", check.rule, new_qty)

        if self.portfolio_checker is not None:
            portfolio_result = self.portfolio_checker.check(
                total_equity=float(balance.total_asset or 0.0),
                existing_positions=existing_positions,
                buy_value=float(request.quantity or 0) * float(request.price or 0.0),
                candidate_symbol=request.symbol,
                candidate_sector=str(candidate_sector or ""),
                candidate_beta=candidate_beta,
                regime_label=regime_label,
                daily_new_exposure=float(daily_new_exposure or 0.0),
            )
            self.last_portfolio_risk = portfolio_result
            if not portfolio_result.approved:
                logger.warning("组合风控拦截 [%s]: %s", request.symbol, portfolio_result.reason)
                return False, portfolio_result.reason, request

        return True, "通过", request
