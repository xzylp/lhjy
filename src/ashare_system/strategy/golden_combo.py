"""黄金策略组合 — 多策略加权融合"""

from __future__ import annotations

import pandas as pd

from ..contracts import MarketProfile, Signal
from .registry import StrategyRegistry, strategy_registry as global_registry
from ..logging_config import get_logger

logger = get_logger("strategy.golden_combo")


class GoldenCombo:
    """黄金策略组合: 多策略加权融合输出综合信号"""

    def __init__(self, registry: StrategyRegistry | None = None) -> None:
        self.registry = registry or global_registry

    def score(self, symbol: str, df: pd.DataFrame, profile: MarketProfile | None = None) -> float:
        """输出综合评分 [0, 100]"""
        return self.registry.combo_score(symbol, df, profile)

    def signal(self, symbol: str, df: pd.DataFrame, profile: MarketProfile | None = None) -> Signal:
        """输出综合信号"""
        score = self.score(symbol, df, profile)
        if score >= 70:
            action = "BUY"
        elif score <= 30:
            action = "SELL"
        else:
            action = "HOLD"
        return Signal(
            symbol=symbol,
            action=action,
            strength=score,
            confidence=min(score / 100, 1.0),
            source_strategy="golden_combo",
        )

    def batch_score(self, symbols: list[str], df_map: dict[str, pd.DataFrame], profile: MarketProfile | None = None) -> dict[str, float]:
        """批量评分"""
        return {s: self.score(s, df_map.get(s, pd.DataFrame()), profile) for s in symbols}
