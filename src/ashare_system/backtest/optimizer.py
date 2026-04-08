"""无限迭代优化器"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from .engine import BacktestEngine, BacktestConfig, BacktestResult
from ..logging_config import get_logger

logger = get_logger("backtest.optimizer")


@dataclass
class OptimizeResult:
    best_params: dict[str, Any]
    best_sharpe: float
    iterations: int
    history: list[dict] = field(default_factory=list)


class GridOptimizer:
    """网格搜索优化器"""

    def __init__(self, engine: BacktestEngine | None = None) -> None:
        self.engine = engine or BacktestEngine()

    def optimize(
        self,
        param_grid: dict[str, list],
        run_fn: Callable[[dict], BacktestResult],
        max_iter: int = 50,
    ) -> OptimizeResult:
        """网格搜索最优参数"""
        import itertools
        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combos = list(itertools.product(*values))[:max_iter]

        best_sharpe = float("-inf")
        best_params: dict[str, Any] = {}
        history: list[dict] = []

        for i, combo in enumerate(combos):
            params = dict(zip(keys, combo))
            try:
                result = run_fn(params)
                sharpe = result.metrics.sharpe_ratio
                history.append({"iter": i, "params": params, "sharpe": sharpe})
                if sharpe > best_sharpe:
                    best_sharpe = sharpe
                    best_params = copy.deepcopy(params)
                    logger.info("优化迭代 %d: 新最优 sharpe=%.3f, params=%s", i, sharpe, params)
            except Exception as e:
                logger.warning("优化迭代 %d 失败: %s", i, e)

        logger.info("优化完成: %d 次迭代, 最优 sharpe=%.3f", len(combos), best_sharpe)
        return OptimizeResult(best_params=best_params, best_sharpe=best_sharpe, iterations=len(combos), history=history)
