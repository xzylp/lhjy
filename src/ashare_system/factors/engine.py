"""因子批量计算引擎 — 支持增量更新"""

from __future__ import annotations

import pandas as pd
from dataclasses import dataclass, field

from .registry import FactorRegistry, registry as global_registry
from .pipeline import FactorPipeline
from ..logging_config import get_logger

logger = get_logger("factors.engine")


@dataclass
class FactorResult:
    factor_name: str
    values: pd.Series
    normalized: pd.Series | None = None


class FactorEngine:
    """因子批量计算引擎"""

    def __init__(self, reg: FactorRegistry | None = None) -> None:
        self.registry = reg or global_registry
        self.pipeline = FactorPipeline()

    def compute_one(self, name: str, df: pd.DataFrame, normalize: bool = True, industry: pd.Series | None = None) -> FactorResult | None:
        """计算单个因子"""
        meta = self.registry.get(name)
        if meta is None:
            logger.warning("因子未注册: %s", name)
            return None
        try:
            values = meta.compute(df)
            normalized = self.pipeline.run(values, industry) if normalize else None
            return FactorResult(factor_name=name, values=values, normalized=normalized)
        except Exception as e:
            logger.warning("因子计算失败 %s: %s", name, e)
            return None

    def compute_all(self, df: pd.DataFrame, normalize: bool = True, industry: pd.Series | None = None) -> dict[str, FactorResult]:
        """批量计算所有注册因子"""
        results: dict[str, FactorResult] = {}
        for meta in self.registry.list_all():
            result = self.compute_one(meta.name, df, normalize, industry)
            if result is not None:
                results[meta.name] = result
        logger.info("因子计算完成: %d/%d 成功", len(results), len(self.registry))
        return results

    def compute_category(self, category: str, df: pd.DataFrame, normalize: bool = True, industry: pd.Series | None = None) -> dict[str, FactorResult]:
        """计算指定类别的因子"""
        results: dict[str, FactorResult] = {}
        for meta in self.registry.list_by_category(category):
            result = self.compute_one(meta.name, df, normalize, industry)
            if result is not None:
                results[meta.name] = result
        return results

    def to_dataframe(self, results: dict[str, FactorResult], use_normalized: bool = True) -> pd.DataFrame:
        """将因子结果转换为 DataFrame (行=股票, 列=因子)"""
        data = {}
        for name, result in results.items():
            series = result.normalized if (use_normalized and result.normalized is not None) else result.values
            data[name] = series
        return pd.DataFrame(data)
