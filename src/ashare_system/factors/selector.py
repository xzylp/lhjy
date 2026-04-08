"""Top-N 因子筛选 — 基于 IC 和特征重要性"""

from __future__ import annotations

import pandas as pd
from dataclasses import dataclass

from .validator import FactorValidator, ICResult
from ..logging_config import get_logger

logger = get_logger("factors.selector")


@dataclass
class SelectedFactor:
    name: str
    ic_mean: float
    ir: float
    importance: float = 0.0
    rank: int = 0


class FactorSelector:
    """Top-N 因子筛选器"""

    def __init__(self) -> None:
        self.validator = FactorValidator()

    def select_by_ic(self, factor_panel: pd.DataFrame, returns_panel: pd.DataFrame, top_n: int = 50) -> list[SelectedFactor]:
        """基于 IC 筛选 Top-N 因子"""
        results: list[SelectedFactor] = []
        for col in factor_panel.columns:
            ic_result = self.validator.validate_rolling(
                factor_panel[[col]].T if factor_panel.ndim == 2 else factor_panel,
                returns_panel,
                factor_name=col,
            )
            if ic_result.is_valid:
                results.append(SelectedFactor(
                    name=col,
                    ic_mean=ic_result.ic_mean,
                    ir=ic_result.ir,
                ))
        results.sort(key=lambda x: abs(x.ir), reverse=True)
        for i, r in enumerate(results[:top_n]):
            r.rank = i + 1
        logger.info("因子筛选: %d 个有效因子, 选取 Top-%d", len(results), min(top_n, len(results)))
        return results[:top_n]

    def select_by_importance(self, importance_dict: dict[str, float], top_n: int = 50) -> list[SelectedFactor]:
        """基于 XGBoost 特征重要性筛选"""
        items = sorted(importance_dict.items(), key=lambda x: x[1], reverse=True)
        results = [
            SelectedFactor(name=name, ic_mean=0.0, ir=0.0, importance=imp, rank=i + 1)
            for i, (name, imp) in enumerate(items[:top_n])
        ]
        return results
