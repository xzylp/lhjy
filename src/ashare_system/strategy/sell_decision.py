"""卖出决策引擎 — ATR止损 + 移动止损 + 时间止损"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..contracts import ExitContext, PositionSnapshot, QuoteSnapshot, SectorProfile
from ..logging_config import get_logger
from .exit_engine import ExitEngine

logger = get_logger("strategy.sell_decision")


class SellReason(str, Enum):
    ENTRY_FAILURE = "entry_failure"
    BOARD_BREAK = "board_break"
    SECTOR_RETREAT = "sector_retreat"
    NO_SEAL_ON_SURGE = "no_seal_on_surge"
    INITIAL_STOP = "initial_stop"
    TRAILING_STOP = "trailing_stop"
    TIME_STOP = "time_stop"
    TAKE_PROFIT_1 = "take_profit_1"
    TAKE_PROFIT_2 = "take_profit_2"
    RISK_GUARD = "risk_guard"


@dataclass
class SellSignal:
    symbol: str
    reason: SellReason
    sell_ratio: float  # 卖出比例 [0,1]
    current_price: float
    stop_price: float


@dataclass
class PositionState:
    symbol: str
    entry_price: float
    atr: float
    holding_days: int       # 交易日，非日历日
    current_price: float
    trailing_stop: float = 0.0
    tp1_triggered: bool = False
    tp2_triggered: bool = False


class SellDecisionEngine:
    """三层止损 + 分批止盈"""

    ATR_STOP_MULT = 2.0    # 初始止损 = 入场价 - 2*ATR
    ATR_TP1_MULT = 3.0     # 第一止盈 = 入场价 + 3*ATR
    ATR_TP2_MULT = 5.0     # 第二止盈 = 入场价 + 5*ATR
    ATR_TRAIL_MULT = 1.5   # 移动止损回撤 = 1.5*ATR
    TIME_STOP_DAYS = 3     # 持仓N天无盈利触发评估

    def __init__(self) -> None:
        self.exit_engine = ExitEngine()

    def evaluate(self, state: PositionState) -> SellSignal | None:
        price = state.current_price
        entry = state.entry_price
        atr = state.atr

        # 初始止损
        initial_stop = entry - self.ATR_STOP_MULT * atr
        if price <= initial_stop:
            return SellSignal(symbol=state.symbol, reason=SellReason.INITIAL_STOP, sell_ratio=1.0, current_price=price, stop_price=initial_stop)

        # 移动止损
        if state.trailing_stop > 0 and price <= state.trailing_stop:
            return SellSignal(symbol=state.symbol, reason=SellReason.TRAILING_STOP, sell_ratio=1.0, current_price=price, stop_price=state.trailing_stop)

        # 第一止盈 (减仓50%)
        tp1 = entry + self.ATR_TP1_MULT * atr
        if price >= tp1 and not state.tp1_triggered:
            return SellSignal(symbol=state.symbol, reason=SellReason.TAKE_PROFIT_1, sell_ratio=0.5, current_price=price, stop_price=tp1)

        # 第二止盈 (再减30%)
        tp2 = entry + self.ATR_TP2_MULT * atr
        if price >= tp2 and not state.tp2_triggered:
            return SellSignal(symbol=state.symbol, reason=SellReason.TAKE_PROFIT_2, sell_ratio=0.3, current_price=price, stop_price=tp2)

        # 时间止损
        if state.holding_days >= self.TIME_STOP_DAYS and price <= entry:
            return SellSignal(symbol=state.symbol, reason=SellReason.TIME_STOP, sell_ratio=1.0, current_price=price, stop_price=entry)

        return None

    def evaluate_with_context(
        self,
        state: PositionState,
        position: PositionSnapshot,
        ctx: ExitContext | None = None,
        quote: QuoteSnapshot | None = None,
        sector: SectorProfile | None = None,
    ) -> SellSignal | None:
        if ctx is not None and quote is not None:
            exit_signal = self.exit_engine.check(position, ctx, quote, sector)
            if exit_signal is not None:
                return SellSignal(
                    symbol=exit_signal.symbol,
                    reason=self._map_reason(exit_signal.reason),
                    sell_ratio=exit_signal.sell_ratio,
                    current_price=exit_signal.current_price,
                    stop_price=exit_signal.reference_price,
                )
        return self.evaluate(state)

    def update_trailing_stop(self, state: PositionState) -> float:
        """更新移动止损位"""
        price = state.current_price
        atr = state.atr
        new_trail = price - self.ATR_TRAIL_MULT * atr
        return max(state.trailing_stop, new_trail)

    @staticmethod
    def _map_reason(reason: str) -> SellReason:
        mapping = {
            "entry_failure": SellReason.ENTRY_FAILURE,
            "board_break": SellReason.BOARD_BREAK,
            "sector_retreat": SellReason.SECTOR_RETREAT,
            "no_seal_on_surge": SellReason.NO_SEAL_ON_SURGE,
            "time_stop": SellReason.TIME_STOP,
        }
        return mapping.get(reason, SellReason.RISK_GUARD)
