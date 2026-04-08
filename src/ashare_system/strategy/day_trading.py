"""日内做T系统 — T+0 高抛低吸信号"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import PositionSnapshot, QuoteSnapshot
from ..logging_config import get_logger

logger = get_logger("strategy.day_trading")


@dataclass
class DayTradingSignal:
    symbol: str
    action: str  # "HIGH_SELL" | "LOW_BUY" | "HOLD"
    price: float
    quantity: int
    reason: str


class DayTradingEngine:
    """日内做T引擎 (T+0 高抛低吸)"""

    SELL_THRESHOLD = 0.02   # 日内涨幅超过2%考虑高抛
    BUY_THRESHOLD = -0.015  # 日内跌幅超过1.5%考虑低吸

    def evaluate(self, position: PositionSnapshot, quote: QuoteSnapshot, vwap: float, open_price: float = 0.0) -> DayTradingSignal:
        """评估做T机会"""
        price = quote.last_price
        # T+1 检查: 可用股数不足则不操作
        if position.available < 100:
            return DayTradingSignal(symbol=position.symbol, action="HOLD", price=price, quantity=0, reason="T+1限制，无可用股份")

        # 日内收益率 (基于今日开盘价)
        ref_price = open_price if open_price > 0 else position.cost_price
        intraday_return = (price - ref_price) / max(ref_price, 1e-9)

        # 高抛: 价格显著高于VWAP且有浮盈
        if price > vwap * 1.015 and intraday_return > self.SELL_THRESHOLD:
            qty = max(position.available // 2 // 100 * 100, 100)  # 卖出一半，整手，最少100股
            if qty <= position.available:
                return DayTradingSignal(
                    symbol=position.symbol, action="HIGH_SELL",
                    price=price, quantity=qty, reason=f"高于VWAP {(price/vwap-1)*100:.1f}%",
                )

        # 低吸: 价格显著低于VWAP
        if price < vwap * 0.985 and intraday_return < self.BUY_THRESHOLD:
            return DayTradingSignal(
                symbol=position.symbol, action="LOW_BUY",
                price=price, quantity=100, reason=f"低于VWAP {(price/vwap-1)*100:.1f}%",
            )

        return DayTradingSignal(symbol=position.symbol, action="HOLD", price=price, quantity=0, reason="无做T机会")
