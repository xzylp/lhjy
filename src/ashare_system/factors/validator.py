"""因子有效性验证 — IC / IR 计算"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass

from ..contracts import FactorValidation
from ..logging_config import get_logger

logger = get_logger("factors.validator")

IC_THRESHOLD = 0.03
IR_THRESHOLD = 0.5


@dataclass
class ICResult:
    factor_name: str
    ic_mean: float
    ic_std: float
    ir: float
    ic_series: list[float]
    is_valid: bool


class FactorValidator:
    """因子 IC/IR 有效性验证"""

    def validate(self, factor_series: pd.Series, forward_returns: pd.Series, factor_name: str = "") -> ICResult:
        """计算单期 IC"""
        aligned = pd.concat([factor_series, forward_returns], axis=1).dropna()
        if len(aligned) < 10:
            return ICResult(factor_name=factor_name, ic_mean=0.0, ic_std=0.0, ir=0.0, ic_series=[], is_valid=False)
        ic = self._spearman_corr(aligned.iloc[:, 0].values, aligned.iloc[:, 1].values)
        ic_val = float(ic) if not np.isnan(ic) else 0.0
        is_valid = abs(ic_val) >= IC_THRESHOLD
        return ICResult(
            factor_name=factor_name,
            ic_mean=ic_val,
            ic_std=0.0,
            ir=ic_val,
            ic_series=[ic_val],
            is_valid=is_valid,
        )

    def validate_rolling(self, factor_panel: pd.DataFrame, returns_panel: pd.DataFrame, factor_name: str = "") -> ICResult:
        """滚动 IC 计算 (多期)"""
        ic_list: list[float] = []
        for date in factor_panel.index:
            if date not in returns_panel.index:
                continue
            f = factor_panel.loc[date].dropna()
            r = returns_panel.loc[date].dropna()
            common = f.index.intersection(r.index)
            if len(common) < 10:
                continue
            ic = self._spearman_corr(f[common].values, r[common].values)
            if not np.isnan(ic):
                ic_list.append(float(ic))
        if not ic_list:
            return ICResult(factor_name=factor_name, ic_mean=0.0, ic_std=0.0, ir=0.0, ic_series=[], is_valid=False)
        ic_mean = float(np.mean(ic_list))
        ic_std = float(np.std(ic_list)) or 1e-9
        ir = ic_mean / ic_std
        return ICResult(
            factor_name=factor_name,
            ic_mean=ic_mean,
            ic_std=ic_std,
            ir=ir,
            ic_series=ic_list,
            is_valid=abs(ic_mean) >= IC_THRESHOLD and abs(ir) >= IR_THRESHOLD,
        )

    @staticmethod
    def _spearman_corr(x: np.ndarray, y: np.ndarray) -> float:
        """不依赖 scipy 的 Spearman 相关系数"""
        n = len(x)
        if n < 2:
            return float("nan")
        rx = np.argsort(np.argsort(x)).astype(float)
        ry = np.argsort(np.argsort(y)).astype(float)
        rx -= rx.mean()
        ry -= ry.mean()
        denom = np.sqrt((rx ** 2).sum() * (ry ** 2).sum())
        if denom == 0:
            return 0.0
        return float(np.dot(rx, ry) / denom)

    def to_contract(self, result: ICResult) -> FactorValidation:
        return FactorValidation(
            factor_name=result.factor_name,
            ic=result.ic_mean,
            ir=result.ir,
            is_valid=result.is_valid,
        )
