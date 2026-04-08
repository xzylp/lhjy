"""策略自进化 — 基于回测结果自动调参"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..backtest.metrics import BacktestMetrics
from ..logging_config import get_logger

logger = get_logger("learning.self_evolve")


@dataclass
class EvolveRecord:
    date: str
    target: str          # "strategy_weight" | "factor_weight" | "risk_threshold"
    param_name: str
    old_value: Any
    new_value: Any
    reason: str
    metrics_before: BacktestMetrics | None = None
    metrics_after: BacktestMetrics | None = None
    accepted: bool = False


@dataclass
class EvolveConfig:
    min_sharpe_improvement: float = 0.05   # 夏普提升阈值
    max_drawdown_tolerance: float = 0.15   # 最大回撤容忍
    strategy_weight_step: float = 0.05     # 策略权重调整步长
    factor_ic_decay_threshold: float = 0.5 # IC 衰减超过此比例则剔除因子


class SelfEvolver:
    """策略自进化引擎"""

    def __init__(self) -> None:
        self._history: list[EvolveRecord] = []

    def suggest_strategy_weights(self, strategy_metrics: dict[str, BacktestMetrics]) -> dict[str, float]:
        """根据各策略历史表现建议权重调整"""
        if not strategy_metrics:
            return {}
        sharpes = {name: m.sharpe_ratio for name, m in strategy_metrics.items()}
        total = sum(max(s, 0) for s in sharpes.values()) or 1.0
        weights = {name: max(s, 0) / total for name, s in sharpes.items()}
        logger.info("策略权重建议: %s", {k: f"{v:.2f}" for k, v in weights.items()})
        return weights

    def suggest_factor_pruning(self, factor_ic_history: dict[str, list[float]]) -> list[str]:
        """建议剔除 IC 衰减严重的因子"""
        to_remove: list[str] = []
        for name, ic_list in factor_ic_history.items():
            if len(ic_list) < 4:
                continue
            recent = ic_list[-2:]
            historical = ic_list[:-2]
            if not historical:
                continue
            avg_hist = sum(abs(x) for x in historical) / len(historical)
            avg_recent = sum(abs(x) for x in recent) / len(recent)
            if avg_hist > 0 and avg_recent / avg_hist < (1 - self.EvolveConfig_decay_threshold()):
                to_remove.append(name)
                logger.info("建议剔除因子 %s: IC 衰减 %.1f%%", name, (1 - avg_recent / avg_hist) * 100)
        return to_remove

    def EvolveConfig_decay_threshold(self) -> float:
        return EvolveConfig().factor_ic_decay_threshold

    def suggest_risk_adjustment(self, recent_metrics: BacktestMetrics) -> dict[str, Any]:
        """根据近期绩效建议风控参数调整"""
        suggestions: dict[str, Any] = {}
        if recent_metrics.max_drawdown < -0.12:
            suggestions["max_portfolio_drawdown"] = 0.10
            logger.info("建议降低最大回撤阈值至 10%%（当前回撤 %.1f%%）", recent_metrics.max_drawdown * 100)
        if recent_metrics.win_rate < 0.45:
            suggestions["buy_score_threshold"] = 70.0
            logger.info("建议提高买入评分阈值至 70（当前胜率 %.1f%%）", recent_metrics.win_rate * 100)
        return suggestions

    def apply_and_record(self, target: str, param_name: str, old_val: Any, new_val: Any, reason: str, metrics_before: BacktestMetrics | None = None) -> EvolveRecord:
        record = EvolveRecord(
            date=date.today().isoformat(),
            target=target,
            param_name=param_name,
            old_value=old_val,
            new_value=new_val,
            reason=reason,
            metrics_before=metrics_before,
            accepted=True,
        )
        self._history.append(record)
        logger.info("自进化记录: [%s] %s: %s → %s (%s)", target, param_name, old_val, new_val, reason)
        return record

    def get_history(self, target: str | None = None) -> list[EvolveRecord]:
        if target is None:
            return list(self._history)
        return [r for r in self._history if r.target == target]

    def generate_report(self) -> str:
        lines = [f"# 自进化报告 ({date.today().isoformat()})", f"总调整次数: {len(self._history)}", ""]
        for rec in self._history[-10:]:
            lines.append(f"- [{rec.date}] {rec.target}/{rec.param_name}: {rec.old_value} → {rec.new_value} ({rec.reason})")
        return "\n".join(lines)
