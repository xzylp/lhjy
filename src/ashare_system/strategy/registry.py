"""策略注册表 — 插件化注册与管理"""

from __future__ import annotations

from typing import Protocol
import pandas as pd

from ..contracts import Signal, MarketProfile
from ..logging_config import get_logger

logger = get_logger("strategy.registry")


class Strategy(Protocol):
    name: str
    weight: float

    def score(self, symbol: str, df: pd.DataFrame, profile: MarketProfile | None) -> float:
        ...

    def signal(self, symbol: str, df: pd.DataFrame, profile: MarketProfile | None) -> Signal:
        ...


class StrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[str, Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        self._strategies[strategy.name] = strategy
        logger.info("策略注册: %s (weight=%.2f)", strategy.name, strategy.weight)

    def unregister(self, name: str) -> None:
        self._strategies.pop(name, None)

    def get(self, name: str) -> Strategy | None:
        return self._strategies.get(name)

    def list_active(self) -> list[Strategy]:
        return list(self._strategies.values())

    def combo_score(self, symbol: str, df: pd.DataFrame, profile: MarketProfile | None = None) -> float:
        """加权组合评分 [0, 100]"""
        strategies = self.list_active()
        if not strategies:
            return 0.0
        total_weight = sum(s.weight for s in strategies)
        if total_weight == 0:
            return 0.0
        weighted_sum = sum(s.score(symbol, df, profile) * s.weight for s in strategies)
        return weighted_sum / total_weight


# 全局单例
strategy_registry = StrategyRegistry()
