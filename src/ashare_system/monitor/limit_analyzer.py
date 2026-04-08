"""涨停数据分析"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import QuoteSnapshot
from ..infra.filters import get_price_limit_ratio
from ..logging_config import get_logger

logger = get_logger("monitor.limit_analyzer")


@dataclass
class LimitStats:
    date: str
    limit_up_count: int = 0
    limit_down_count: int = 0
    limit_up_symbols: list[str] = field(default_factory=list)
    limit_down_symbols: list[str] = field(default_factory=list)
    board_fail_symbols: list[str] = field(default_factory=list)
    max_consecutive: int = 0


class LimitAnalyzer:
    """涨停数据分析器"""

    def analyze(self, snapshots: list[QuoteSnapshot], date: str = "") -> LimitStats:
        stats = LimitStats(date=date)
        for snap in snapshots:
            if snap.pre_close <= 0:
                continue
            limit = get_price_limit_ratio(snap.symbol)
            ratio = snap.last_price / snap.pre_close
            if ratio >= 1 + limit - 0.001:
                stats.limit_up_count += 1
                stats.limit_up_symbols.append(snap.symbol)
            elif ratio <= 1 - limit + 0.001:
                stats.limit_down_count += 1
                stats.limit_down_symbols.append(snap.symbol)
        logger.info("涨停分析: 涨停%d 跌停%d", stats.limit_up_count, stats.limit_down_count)
        return stats

    def calc_board_fail_rate(self, ever_limit_up: list[str], current_limit_up: list[str]) -> float:
        """炸板率 = 曾涨停但未封住 / 曾涨停总数"""
        if not ever_limit_up:
            return 0.0
        failed = [s for s in ever_limit_up if s not in current_limit_up]
        return len(failed) / len(ever_limit_up)
