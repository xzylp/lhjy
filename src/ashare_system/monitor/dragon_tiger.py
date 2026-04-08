"""龙虎榜分析器"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..logging_config import get_logger

logger = get_logger("monitor.dragon_tiger")


@dataclass
class DragonTigerRecord:
    symbol: str
    date: str
    buy_seats: list[str] = field(default_factory=list)
    sell_seats: list[str] = field(default_factory=list)
    net_buy: float = 0.0
    reason: str = ""


# 知名游资席位特征关键词
HOT_MONEY_SEATS = ["宁波", "东方财富", "华鑫", "方正", "国泰君安上海", "中信证券上海"]


class DragonTigerAnalyzer:
    """龙虎榜分析器"""

    def analyze(self, records: list[DragonTigerRecord]) -> dict:
        """分析龙虎榜数据"""
        hot_money_buys: list[str] = []
        hot_money_sells: list[str] = []

        for rec in records:
            buy_hot = any(any(kw in seat for kw in HOT_MONEY_SEATS) for seat in rec.buy_seats)
            sell_hot = any(any(kw in seat for kw in HOT_MONEY_SEATS) for seat in rec.sell_seats)
            if buy_hot and rec.net_buy > 0:
                hot_money_buys.append(rec.symbol)
            if sell_hot and rec.net_buy < 0:
                hot_money_sells.append(rec.symbol)

        return {
            "hot_money_buy": hot_money_buys,
            "hot_money_sell": hot_money_sells,
            "total_records": len(records),
        }

    def is_hot_money_seat(self, seat_name: str) -> bool:
        return any(kw in seat_name for kw in HOT_MONEY_SEATS)
