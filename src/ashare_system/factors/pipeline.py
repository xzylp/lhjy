"""因子标准化 Pipeline — Winsorize + Z-Score + 中性化"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..logging_config import get_logger

logger = get_logger("factors.pipeline")


class FactorPipeline:
    """三步标准化 pipeline"""

    @staticmethod
    def mad(series: pd.Series, n: float = 3.0) -> pd.Series:
        """中位数绝对偏差去极值 (Median Absolute Deviation)
        比 Winsorize 更稳健，是 A 股量化常用的去极值方法。
        """
        median = series.median()
        ad = (series - median).abs()
        mad = ad.median()
        threshold = n * 1.4826 * mad # 1.4826 是为了与正态分布标准差对齐的常数
        return series.clip(median - threshold, median + threshold)

    def run(self, series: pd.Series, industry: pd.Series | None = None, method: str = "mad", use_rank: bool = False) -> pd.Series:
        """完整 pipeline: 去极值 → 标准化 → 中性化 → [排名标准化]"""
        if method == "mad":
            s = self.mad(series)
        else:
            s = self.winsorize(series)
        s = self.zscore(s)
        if industry is not None:
            s = self.neutralize(s, industry)
        
        if use_rank:
            s = self.rank_normalize(s)
        return s

    @staticmethod
    def winsorize(series: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
        """截尾: 将 1%/99% 分位以外的值截断到边界"""
        lo = series.quantile(lower)
        hi = series.quantile(upper)
        return series.clip(lo, hi)

    @staticmethod
    def zscore(series: pd.Series) -> pd.Series:
        """Z-Score 标准化: 均值0, 标准差1"""
        std = series.std()
        if std == 0:
            return pd.Series(0.0, index=series.index)
        return (series - series.mean()) / std

    @staticmethod
    def neutralize(factor: pd.Series, industry: pd.Series) -> pd.Series:
        """行业中性化: 去除行业效应，保留个股 alpha"""
        dummies = pd.get_dummies(industry, prefix="ind")
        # 对齐索引
        dummies = dummies.reindex(factor.index).fillna(0)
        if dummies.empty or dummies.shape[1] == 0:
            return factor
        # OLS 回归残差
        X = dummies.values.astype(float)
        y = factor.values.astype(float)
        mask = ~np.isnan(y)
        if mask.sum() < 2:
            return factor
        try:
            coef, *_ = np.linalg.lstsq(X[mask], y[mask], rcond=None)
            fitted = X @ coef
            residuals = y - fitted
            return pd.Series(residuals, index=factor.index)
        except Exception:
            return factor

    @staticmethod
    def rank_normalize(series: pd.Series) -> pd.Series:
        """排名标准化: 转换为 [0, 1] 均匀分布"""
        return series.rank(pct=True)
