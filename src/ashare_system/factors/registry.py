"""因子注册表 — 声明式注册 + 自动发现"""

from __future__ import annotations

from typing import Callable
import pandas as pd

from ..logging_config import get_logger

logger = get_logger("factors.registry")

# 因子函数签名: (df: pd.DataFrame) -> pd.Series
FactorFn = Callable[[pd.DataFrame], pd.Series]


class FactorMeta:
    def __init__(self, name: str, category: str, fn: FactorFn, description: str = "") -> None:
        self.name = name
        self.category = category
        self.fn = fn
        self.description = description

    def compute(self, df: pd.DataFrame) -> pd.Series:
        return self.fn(df)


class FactorRegistry:
    """全局因子注册表"""

    def __init__(self) -> None:
        self._factors: dict[str, FactorMeta] = {}

    def register(self, name: str, category: str, description: str = ""):
        """装饰器: 注册因子函数"""
        def decorator(fn: FactorFn) -> FactorFn:
            self._factors[name] = FactorMeta(name, category, fn, description)
            return fn
        return decorator

    def get(self, name: str) -> FactorMeta | None:
        return self._factors.get(name)

    def list_by_category(self, category: str) -> list[FactorMeta]:
        return [f for f in self._factors.values() if f.category == category]

    def list_all(self) -> list[FactorMeta]:
        return list(self._factors.values())

    def names(self) -> list[str]:
        return list(self._factors.keys())

    def __len__(self) -> int:
        return len(self._factors)


# 全局单例
registry = FactorRegistry()
