"""风控规则引擎 — 6大规则"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..contracts import BalanceSnapshot, PositionSnapshot, MarketProfile
from ..logging_config import get_logger

logger = get_logger("risk.rules")


class RuleResult(str, Enum):
    PASS = "pass"
    REJECT = "reject"
    LIMIT = "limit"


@dataclass
class RuleCheck:
    rule: str
    result: RuleResult
    reason: str = ""
    adjusted_value: float | None = None


class RiskRules:
    """6大风控规则引擎"""

    MAX_SINGLE_POSITION = 0.25   # 单票最大仓位
    HARD_STOP_LOSS = -0.05       # 硬性止损线
    MAX_PORTFOLIO_DRAWDOWN = 0.15 # 组合最大回撤
    MAX_DAILY_LOSS = 0.08        # 单日最大亏损
    MAX_CONSECUTIVE_LOSS = 3     # 连续亏损次数上限

    def check_single_position(self, buy_value: float, account_equity: float) -> RuleCheck:
        ratio = buy_value / max(account_equity, 1e-9)
        if ratio > self.MAX_SINGLE_POSITION:
            adjusted = account_equity * self.MAX_SINGLE_POSITION
            return RuleCheck(rule="single_position", result=RuleResult.LIMIT, reason=f"单票仓位 {ratio:.1%} 超限", adjusted_value=adjusted)
        return RuleCheck(rule="single_position", result=RuleResult.PASS)

    def check_stop_loss(self, position: PositionSnapshot) -> RuleCheck:
        if position.cost_price <= 0:
            return RuleCheck(rule="stop_loss", result=RuleResult.PASS)
        pnl_pct = (position.last_price - position.cost_price) / position.cost_price
        if pnl_pct <= self.HARD_STOP_LOSS:
            return RuleCheck(rule="stop_loss", result=RuleResult.REJECT, reason=f"亏损 {pnl_pct:.1%} 触发硬性止损")
        return RuleCheck(rule="stop_loss", result=RuleResult.PASS)

    def check_portfolio_drawdown(self, current_equity: float, peak_equity: float) -> RuleCheck:
        if peak_equity <= 0:
            return RuleCheck(rule="portfolio_drawdown", result=RuleResult.PASS)
        drawdown = (current_equity - peak_equity) / peak_equity
        if drawdown <= -self.MAX_PORTFOLIO_DRAWDOWN:
            return RuleCheck(rule="portfolio_drawdown", result=RuleResult.REJECT, reason=f"组合回撤 {drawdown:.1%} 触发熔断")
        return RuleCheck(rule="portfolio_drawdown", result=RuleResult.PASS)

    def check_daily_loss(self, daily_pnl: float, account_equity: float) -> RuleCheck:
        daily_loss_pct = daily_pnl / max(account_equity, 1e-9)
        if daily_loss_pct <= -self.MAX_DAILY_LOSS:
            return RuleCheck(rule="daily_loss", result=RuleResult.REJECT, reason=f"单日亏损 {daily_loss_pct:.1%} 超限")
        return RuleCheck(rule="daily_loss", result=RuleResult.PASS)

    def check_consecutive_loss(self, consecutive_losses: int) -> RuleCheck:
        if consecutive_losses >= self.MAX_CONSECUTIVE_LOSS:
            return RuleCheck(rule="consecutive_loss", result=RuleResult.REJECT, reason=f"连续亏损 {consecutive_losses} 次，强制空仓")
        return RuleCheck(rule="consecutive_loss", result=RuleResult.PASS)

    def check_emotion_shield(self, buy_value: float, account_equity: float, profile: MarketProfile) -> RuleCheck:
        ratio = buy_value / max(account_equity, 1e-9)
        ceil = profile.position_ceiling
        if ratio > ceil:
            adjusted = account_equity * ceil
            return RuleCheck(rule="emotion_shield", result=RuleResult.LIMIT, reason=f"情绪{profile.sentiment_phase}，仓位上限{ceil:.0%}", adjusted_value=adjusted)
        return RuleCheck(rule="emotion_shield", result=RuleResult.PASS)

    def check_all_buy(self, buy_value: float, balance: BalanceSnapshot, profile: MarketProfile | None = None, consecutive_losses: int = 0, daily_pnl: float = 0.0) -> list[RuleCheck]:
        checks = [
            self.check_single_position(buy_value, balance.total_asset),
            self.check_daily_loss(daily_pnl, balance.total_asset),
            self.check_consecutive_loss(consecutive_losses),
        ]
        if profile:
            checks.append(self.check_emotion_shield(buy_value, balance.total_asset, profile))
        return checks
