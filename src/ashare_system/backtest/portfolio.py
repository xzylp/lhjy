"""组合回测"""

from __future__ import annotations

import pandas as pd
from dataclasses import dataclass, field

from .engine import BacktestEngine, BacktestConfig
from .metrics import BacktestMetrics, MetricsCalculator
from ..logging_config import get_logger

logger = get_logger("backtest.portfolio")


@dataclass
class PortfolioConfig:
    initial_cash: float = 1_000_000.0
    strategy_weights: dict[str, float] = field(default_factory=dict)
    rebalance_freq: int = 20  # 再平衡频率 (交易日)


@dataclass
class PortfolioResult:
    combined_metrics: BacktestMetrics
    strategy_metrics: dict[str, BacktestMetrics] = field(default_factory=dict)
    equity_curve: pd.Series = field(default_factory=pd.Series)


class PortfolioBacktest:
    """多策略组合回测"""

    def __init__(self, config: PortfolioConfig | None = None) -> None:
        self.config = config or PortfolioConfig()
        self.calc = MetricsCalculator()

    def run(self, strategy_results: dict[str, tuple[pd.Series, list[dict]]]) -> PortfolioResult:
        """
        strategy_results: {strategy_name: (equity_curve, trades)}
        """
        if not strategy_results:
            return PortfolioResult(combined_metrics=BacktestMetrics())

        weights = self.config.strategy_weights
        total_weight = sum(weights.get(name, 1.0) for name in strategy_results)

        # 加权合并净值曲线
        combined: pd.Series | None = None
        strategy_metrics: dict[str, BacktestMetrics] = {}
        all_trades: list[dict] = []

        for name, (equity, trades) in strategy_results.items():
            w = weights.get(name, 1.0) / total_weight
            normalized = equity / equity.iloc[0] if not equity.empty and equity.iloc[0] != 0 else equity
            combined = normalized * w if combined is None else combined + normalized * w
            strategy_metrics[name] = self.calc.calc(equity, trades)
            all_trades.extend(trades)

        if combined is None:
            return PortfolioResult(combined_metrics=BacktestMetrics())

        equity_curve = combined * self.config.initial_cash
        combined_metrics = self.calc.calc(equity_curve, all_trades)
        logger.info("组合回测完成: 夏普=%.2f, 最大回撤=%.1f%%", combined_metrics.sharpe_ratio, combined_metrics.max_drawdown * 100)
        return PortfolioResult(combined_metrics=combined_metrics, strategy_metrics=strategy_metrics, equity_curve=equity_curve)
