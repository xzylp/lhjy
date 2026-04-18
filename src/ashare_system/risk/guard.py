"""执行守卫 — 拦截层"""

from __future__ import annotations

from ..contracts import PlaceOrderRequest, BalanceSnapshot, MarketProfile
from ..logging_config import get_logger
from ..runtime_config import RuntimeConfig
from .rules import RiskRules, RiskThresholds, RuleResult

logger = get_logger("risk.guard")


class ExecutionGuard:
    """执行守卫: 在下单前进行风控拦截"""

    def __init__(self, rules: RiskRules | None = None) -> None:
        self.rules = rules or RiskRules()

    @classmethod
    def from_runtime_config(cls, config: RuntimeConfig) -> "ExecutionGuard":
        return cls(rules=RiskRules(RiskThresholds.from_runtime_config(config)))

    def approve(
        self,
        request: PlaceOrderRequest,
        balance: BalanceSnapshot,
        profile: MarketProfile | None = None,
        consecutive_losses: int = 0,
        daily_pnl: float = 0.0,
    ) -> tuple[bool, str, PlaceOrderRequest]:
        """
        审批下单请求。
        返回: (approved, reason, adjusted_request)
        """
        if request.side == "SELL":
            return True, "卖出直接放行", request

        buy_value = request.quantity * request.price
        checks = self.rules.check_all_buy(buy_value, balance, profile, consecutive_losses, daily_pnl)

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

        return True, "通过", request
