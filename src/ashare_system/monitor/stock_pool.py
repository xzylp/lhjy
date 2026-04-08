"""预选股池生成"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..logging_config import get_logger

logger = get_logger("monitor.stock_pool")


@dataclass
class StockPool:
    date: str
    symbols: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    source: str = "screener"


class StockPoolManager:
    """预选股池管理器"""

    def __init__(self) -> None:
        self._pool: StockPool | None = None

    def update(self, symbols: list[str], scores: dict[str, float] | None = None, names: dict[str, str] | None = None) -> StockPool:
        today = date.today().isoformat()
        self._pool = StockPool(date=today, symbols=symbols, scores=scores or {}, names=names or {})
        logger.info("股池更新: %d 只候选股 (%s)", len(symbols), today)
        return self._pool

    def get(self) -> StockPool | None:
        return self._pool

    def get_top_n(self, n: int = 10) -> list[str]:
        if self._pool is None:
            return []
        if self._pool.scores:
            sorted_symbols = sorted(self._pool.symbols, key=lambda s: self._pool.scores.get(s, 0), reverse=True)
            return sorted_symbols[:n]
        return self._pool.symbols[:n]
