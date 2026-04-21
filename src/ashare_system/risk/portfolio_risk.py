"""组合级风险约束与效率统计。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PortfolioPositionContext:
    symbol: str
    market_value: float
    sector: str = ""
    beta: float = 1.0


@dataclass
class PortfolioRiskResult:
    approved: bool
    reason: str = ""
    blockers: list[str] = field(default_factory=list)
    sector_concentration: dict[str, float] = field(default_factory=dict)
    portfolio_beta: float = 0.0
    position_count: int = 0
    max_position_count: int = 0
    daily_new_exposure_ratio: float = 0.0
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "blockers": list(self.blockers or []),
            "sector_concentration": {key: round(float(value or 0.0), 6) for key, value in dict(self.sector_concentration or {}).items()},
            "portfolio_beta": round(float(self.portfolio_beta or 0.0), 6),
            "position_count": int(self.position_count or 0),
            "max_position_count": int(self.max_position_count or 0),
            "daily_new_exposure_ratio": round(float(self.daily_new_exposure_ratio or 0.0), 6),
            "diagnostics": dict(self.diagnostics or {}),
        }


class PortfolioRiskChecker:
    """组合级风险检查器。"""

    def __init__(
        self,
        *,
        sector_concentration_limit: float = 0.40,
        beta_limit: float = 1.30,
        low_beta_threshold: float = 1.00,
        daily_new_exposure_limit: float = 0.30,
    ) -> None:
        self.sector_concentration_limit = max(float(sector_concentration_limit or 0.40), 0.0)
        self.beta_limit = max(float(beta_limit or 1.30), 0.0)
        self.low_beta_threshold = max(float(low_beta_threshold or 1.00), 0.0)
        self.daily_new_exposure_limit = max(float(daily_new_exposure_limit or 0.30), 0.0)

    def check(
        self,
        *,
        total_equity: float,
        existing_positions: list[PortfolioPositionContext] | None,
        buy_value: float,
        candidate_symbol: str,
        candidate_sector: str = "",
        candidate_beta: float | None = None,
        regime_label: str | None = None,
        daily_new_exposure: float = 0.0,
    ) -> PortfolioRiskResult:
        positions = list(existing_positions or [])
        blockers: list[str] = []
        max_position_count = self.max_positions_for_regime(regime_label)
        candidate_sector = str(candidate_sector or "").strip() or "unknown"
        candidate_beta = float(candidate_beta if candidate_beta is not None else 1.0)

        current_sector = self._sector_concentration(positions, total_equity)
        next_sector_value = current_sector.get(candidate_sector, 0.0) + float(buy_value or 0.0) / max(float(total_equity or 0.0), 1e-9)
        if next_sector_value > self.sector_concentration_limit + 1e-9:
            blockers.append("sector_concentration_exceeded")

        active_symbols = {item.symbol for item in positions if float(item.market_value or 0.0) > 0}
        if candidate_symbol not in active_symbols and len(active_symbols) >= max_position_count:
            blockers.append("max_position_count_exceeded")

        current_beta = self._portfolio_beta(positions)
        next_positions = positions + [
            PortfolioPositionContext(
                symbol=candidate_symbol,
                market_value=float(buy_value or 0.0),
                sector=candidate_sector,
                beta=candidate_beta,
            )
        ]
        next_beta = self._portfolio_beta(next_positions)
        if current_beta > self.beta_limit and candidate_beta > self.low_beta_threshold:
            blockers.append("high_beta_buy_blocked")

        next_new_exposure_ratio = (float(daily_new_exposure or 0.0) + float(buy_value or 0.0)) / max(float(total_equity or 0.0), 1e-9)
        if next_new_exposure_ratio > self.daily_new_exposure_limit + 1e-9:
            blockers.append("daily_new_exposure_exceeded")

        diagnostics = {
            "candidate_sector_ratio_after_buy": round(next_sector_value, 6),
            "current_portfolio_beta": round(current_beta, 6),
            "portfolio_beta_after_buy": round(next_beta, 6),
            "candidate_beta": round(candidate_beta, 6),
        }
        reason_map = {
            "sector_concentration_exceeded": f"板块 {candidate_sector} 持仓占比将超过 {self.sector_concentration_limit:.0%}",
            "max_position_count_exceeded": f"当前市场阶段最多允许持有 {max_position_count} 只股票",
            "high_beta_buy_blocked": "当前组合 Beta 已偏高，仅允许继续买入低 Beta 标的",
            "daily_new_exposure_exceeded": f"单日新增敞口将超过 {self.daily_new_exposure_limit:.0%}",
        }
        reason = "通过"
        if blockers:
            reason = "；".join(reason_map.get(code, code) for code in blockers)
        return PortfolioRiskResult(
            approved=not blockers,
            reason=reason,
            blockers=blockers,
            sector_concentration=current_sector,
            portfolio_beta=next_beta,
            position_count=len(active_symbols),
            max_position_count=max_position_count,
            daily_new_exposure_ratio=max(next_new_exposure_ratio, 0.0),
            diagnostics=diagnostics,
        )

    def build_efficiency_snapshot(
        self,
        *,
        total_equity: float,
        cash_available: float,
        existing_positions: list[PortfolioPositionContext] | None,
        regime_label: str | None = None,
        daily_new_exposure: float = 0.0,
        reverse_repo_value: float = 0.0,
    ) -> dict[str, Any]:
        positions = list(existing_positions or [])
        invested_equity = sum(max(float(item.market_value or 0.0), 0.0) for item in positions)
        sector_concentration = self._sector_concentration(positions, total_equity)
        current_position_ratio = invested_equity / max(float(total_equity or 0.0), 1e-9)
        risk_budget_used = min(
            max(
                current_position_ratio,
                max(sector_concentration.values()) if sector_concentration else 0.0,
                float(daily_new_exposure or 0.0) / max(float(total_equity or 0.0), 1e-9),
            ),
            1.0,
        )
        return {
            "total_equity": round(float(total_equity or 0.0), 4),
            "invested_equity": round(float(invested_equity or 0.0), 4),
            "cash_ratio": round(float(cash_available or 0.0) / max(float(total_equity or 0.0), 1e-9), 6),
            "reverse_repo_coverage": round(float(reverse_repo_value or 0.0) / max(float(total_equity or 0.0), 1e-9), 6),
            "risk_budget_used": round(risk_budget_used, 6),
            "risk_budget_remaining": round(max(1.0 - risk_budget_used, 0.0), 6),
            "sector_concentration": {key: round(float(value or 0.0), 6) for key, value in sector_concentration.items()},
            "portfolio_beta": round(self._portfolio_beta(positions), 6),
            "position_count": len([item for item in positions if float(item.market_value or 0.0) > 0]),
            "max_position_count_for_regime": self.max_positions_for_regime(regime_label),
            "daily_new_exposure_ratio": round(float(daily_new_exposure or 0.0) / max(float(total_equity or 0.0), 1e-9), 6),
        }

    @staticmethod
    def max_positions_for_regime(regime_label: str | None) -> int:
        regime = str(regime_label or "").strip().lower()
        if regime == "strong_rotation":
            return 6
        if regime in {"weak_defense", "panic_sell"}:
            return 2
        if regime in {"sector_breakout", "index_rebound"}:
            return 4
        return 3

    @staticmethod
    def _sector_concentration(
        positions: list[PortfolioPositionContext],
        total_equity: float,
    ) -> dict[str, float]:
        buckets: dict[str, float] = {}
        for item in positions:
            sector = str(item.sector or "").strip() or "unknown"
            buckets[sector] = buckets.get(sector, 0.0) + float(item.market_value or 0.0)
        return {
            key: value / max(float(total_equity or 0.0), 1e-9)
            for key, value in buckets.items()
            if value > 0
        }

    @staticmethod
    def _portfolio_beta(positions: list[PortfolioPositionContext]) -> float:
        total_value = sum(max(float(item.market_value or 0.0), 0.0) for item in positions)
        if total_value <= 0:
            return 0.0
        return sum(max(float(item.market_value or 0.0), 0.0) * float(item.beta or 1.0) for item in positions) / total_value
