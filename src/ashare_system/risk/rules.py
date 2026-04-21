"""风控规则引擎。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Any

from ..contracts import BalanceSnapshot, PositionSnapshot, MarketProfile
from ..logging_config import get_logger
from ..runtime_config import RuntimeConfig

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


@dataclass
class RiskThresholds:
    max_single_position: float = 0.25
    hard_stop_loss: float = -0.05
    max_portfolio_drawdown: float = 0.15
    max_daily_loss: float = 0.08
    max_consecutive_loss: int = 3
    max_single_loss_pct: float = 0.05
    high_volatility_threshold: float = 0.035
    volatility_position_scale: float = 0.6
    max_position_correlation: float = 0.7
    min_daily_turnover_amount: float = 10_000_000.0

    @classmethod
    def from_runtime_config(cls, config: RuntimeConfig) -> "RiskThresholds":
        return cls(
            max_single_position=float(getattr(config, "max_single_position", 0.25) or 0.25),
            max_daily_loss=float(getattr(config, "daily_loss_limit", 0.08) or 0.08),
            max_single_loss_pct=float(getattr(config, "max_single_loss_pct", 0.05) or 0.05),
            high_volatility_threshold=float(getattr(config, "high_volatility_threshold", 0.035) or 0.035),
            volatility_position_scale=float(getattr(config, "volatility_position_scale", 0.6) or 0.6),
            max_position_correlation=float(getattr(config, "max_position_correlation", 0.7) or 0.7),
            min_daily_turnover_amount=float(getattr(config, "min_daily_turnover_amount", 10_000_000.0) or 10_000_000.0),
        )


class RiskRules:
    """扩展风控规则引擎。"""

    def __init__(self, thresholds: RiskThresholds | None = None) -> None:
        self.thresholds = thresholds or RiskThresholds()

    def check_single_position(self, buy_value: float, account_equity: float) -> RuleCheck:
        ratio = buy_value / max(account_equity, 1e-9)
        if ratio > self.thresholds.max_single_position:
            adjusted = account_equity * self.thresholds.max_single_position
            return RuleCheck(
                rule="single_position",
                result=RuleResult.LIMIT,
                reason=f"单票仓位 {ratio:.1%} 超限",
                adjusted_value=adjusted,
            )
        return RuleCheck(rule="single_position", result=RuleResult.PASS)

    def check_stop_loss(self, position: PositionSnapshot) -> RuleCheck:
        if position.cost_price <= 0:
            return RuleCheck(rule="stop_loss", result=RuleResult.PASS)
        pnl_pct = (position.last_price - position.cost_price) / position.cost_price
        if pnl_pct <= self.thresholds.hard_stop_loss:
            return RuleCheck(rule="stop_loss", result=RuleResult.REJECT, reason=f"亏损 {pnl_pct:.1%} 触发硬性止损")
        return RuleCheck(rule="stop_loss", result=RuleResult.PASS)

    def check_single_loss_risk(self, pnl_pct: float | None) -> RuleCheck:
        if pnl_pct is None:
            return RuleCheck(rule="single_loss_risk", result=RuleResult.PASS)
        if pnl_pct <= -abs(self.thresholds.max_single_loss_pct):
            return RuleCheck(
                rule="single_loss_risk",
                result=RuleResult.REJECT,
                reason=f"单票浮亏 {pnl_pct:.1%} 触发止损阈值",
            )
        return RuleCheck(rule="single_loss_risk", result=RuleResult.PASS)

    def check_portfolio_drawdown(self, current_equity: float, peak_equity: float) -> RuleCheck:
        if peak_equity <= 0:
            return RuleCheck(rule="portfolio_drawdown", result=RuleResult.PASS)
        drawdown = (current_equity - peak_equity) / peak_equity
        if drawdown <= -self.thresholds.max_portfolio_drawdown:
            return RuleCheck(rule="portfolio_drawdown", result=RuleResult.REJECT, reason=f"组合回撤 {drawdown:.1%} 触发熔断")
        return RuleCheck(rule="portfolio_drawdown", result=RuleResult.PASS)

    def check_daily_loss(self, daily_pnl: float, account_equity: float) -> RuleCheck:
        daily_loss_pct = daily_pnl / max(account_equity, 1e-9)
        if daily_loss_pct <= -self.thresholds.max_daily_loss:
            return RuleCheck(rule="daily_loss", result=RuleResult.REJECT, reason=f"单日亏损 {daily_loss_pct:.1%} 超限")
        return RuleCheck(rule="daily_loss", result=RuleResult.PASS)

    def check_consecutive_loss(self, consecutive_losses: int) -> RuleCheck:
        if consecutive_losses >= self.thresholds.max_consecutive_loss:
            return RuleCheck(rule="consecutive_loss", result=RuleResult.REJECT, reason=f"连续亏损 {consecutive_losses} 次，强制空仓")
        return RuleCheck(rule="consecutive_loss", result=RuleResult.PASS)

    def check_emotion_shield(self, buy_value: float, account_equity: float, profile: MarketProfile) -> RuleCheck:
        ratio = buy_value / max(account_equity, 1e-9)
        ceil = profile.position_ceiling
        if ratio > ceil:
            adjusted = account_equity * ceil
            return RuleCheck(rule="emotion_shield", result=RuleResult.LIMIT, reason=f"情绪{profile.sentiment_phase}，仓位上限{ceil:.0%}", adjusted_value=adjusted)
        return RuleCheck(rule="emotion_shield", result=RuleResult.PASS)

    def check_volatility_scaling(self, buy_value: float, account_equity: float, realized_volatility: float | None) -> RuleCheck:
        if realized_volatility is None or realized_volatility < self.thresholds.high_volatility_threshold:
            return RuleCheck(rule="volatility_scaling", result=RuleResult.PASS)
        adjusted = account_equity * self.thresholds.max_single_position * self.thresholds.volatility_position_scale
        return RuleCheck(
            rule="volatility_scaling",
            result=RuleResult.LIMIT,
            reason=f"波动率 {realized_volatility:.2%} 过高，压缩单票仓位",
            adjusted_value=min(adjusted, buy_value),
        )

    def check_position_correlation(self, correlation: float | None) -> RuleCheck:
        if correlation is None or math.isnan(correlation):
            return RuleCheck(rule="position_correlation", result=RuleResult.PASS)
        if correlation > self.thresholds.max_position_correlation:
            return RuleCheck(
                rule="position_correlation",
                result=RuleResult.REJECT,
                reason=f"与现有持仓相关性 {correlation:.2f} 超过阈值 {self.thresholds.max_position_correlation:.2f}",
            )
        return RuleCheck(rule="position_correlation", result=RuleResult.PASS)

    def check_liquidity_risk(self, daily_turnover_amount: float | None) -> RuleCheck:
        if daily_turnover_amount is None:
            return RuleCheck(rule="liquidity_risk", result=RuleResult.PASS)
        if daily_turnover_amount < self.thresholds.min_daily_turnover_amount:
            return RuleCheck(
                rule="liquidity_risk",
                result=RuleResult.REJECT,
                reason=f"日成交额 {daily_turnover_amount:.0f} 低于阈值 {self.thresholds.min_daily_turnover_amount:.0f}",
            )
        return RuleCheck(rule="liquidity_risk", result=RuleResult.PASS)

    def check_all_buy(
        self,
        buy_value: float,
        balance: BalanceSnapshot,
        profile: MarketProfile | None = None,
        consecutive_losses: int = 0,
        daily_pnl: float = 0.0,
        *,
        realized_volatility: float | None = None,
        position_correlation: float | None = None,
        daily_turnover_amount: float | None = None,
        single_position_pnl_pct: float | None = None,
    ) -> list[RuleCheck]:
        checks = [
            self.check_single_position(buy_value, balance.total_asset),
            self.check_daily_loss(daily_pnl, balance.total_asset),
            self.check_consecutive_loss(consecutive_losses),
            self.check_single_loss_risk(single_position_pnl_pct),
            self.check_volatility_scaling(buy_value, balance.total_asset, realized_volatility),
            self.check_position_correlation(position_correlation),
            self.check_liquidity_risk(daily_turnover_amount),
        ]
        if profile:
            checks.append(self.check_emotion_shield(buy_value, balance.total_asset, profile))
        return checks
