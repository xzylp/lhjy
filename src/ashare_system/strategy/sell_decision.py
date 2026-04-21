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
    DAY_TRADING_HIGH_SELL = "day_trading_high_sell"


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
    """三层止损 + 分批止盈（v1.0: playbook-aware + regime-aware）"""

    # 默认参数（向后兼容，playbook 为空时使用）
    ATR_STOP_MULT = 2.0
    ATR_TP1_MULT = 3.0
    ATR_TP2_MULT = 5.0
    ATR_TRAIL_MULT = 1.5
    TIME_STOP_DAYS = 3

    # 各 playbook 的专属退出参数
    PLAYBOOK_EXIT_PARAMS: dict[str, dict[str, float]] = {
        "leader_chase": {
            "atr_stop_mult": 1.5,       # 龙头票容错低，初始止损收紧
            "atr_tp1_mult": 2.5,        # 龙头票第一波止盈来得快
            "atr_tp2_mult": 5.0,        # 但第二波可能走趋势
            "trail_mult": 1.0,          # 移动止损紧跟
            "time_stop_days": 2,        # T+1 不封板就跑
        },
        "divergence_reseal": {
            "atr_stop_mult": 2.0,
            "atr_tp1_mult": 3.5,
            "atr_tp2_mult": 6.0,
            "trail_mult": 1.5,
            "time_stop_days": 3,
        },
        "sector_reflow_first_board": {
            "atr_stop_mult": 2.5,       # 首板票波动大，止损稍宽
            "atr_tp1_mult": 3.0,
            "atr_tp2_mult": 4.5,
            "trail_mult": 2.0,
            "time_stop_days": 2,
        },
    }

    # Regime 修正系数
    REGIME_MODIFIERS: dict[str, dict[str, float]] = {
        "chaos": {"atr_stop_mult": 0.8, "atr_tp2_mult": 0.8},    # 止损收紧
        "trend": {"atr_tp2_mult": 1.3, "trail_mult": 0.8},        # 止盈放宽，移动止损跟紧
        "defensive": {"atr_stop_mult": 0.9},
        "rotation": {},
    }

    def __init__(self) -> None:
        self.exit_engine = ExitEngine()

    def _resolve_params(
        self,
        playbook: str = "",
        regime: str = "",
        param_overrides: dict[str, float] | None = None,
    ) -> dict[str, float]:
        """根据 playbook 和 regime 解析最终的退出参数。"""
        base = self.PLAYBOOK_EXIT_PARAMS.get(playbook, {})
        params = {
            "atr_stop_mult": float(base.get("atr_stop_mult", self.ATR_STOP_MULT)),
            "atr_tp1_mult": float(base.get("atr_tp1_mult", self.ATR_TP1_MULT)),
            "atr_tp2_mult": float(base.get("atr_tp2_mult", self.ATR_TP2_MULT)),
            "trail_mult": float(base.get("trail_mult", self.ATR_TRAIL_MULT)),
            "time_stop_days": float(base.get("time_stop_days", self.TIME_STOP_DAYS)),
        }
        # 应用 regime 修正
        modifiers = self.REGIME_MODIFIERS.get(regime, {})
        for key, factor in modifiers.items():
            if key in params:
                params[key] = params[key] * factor
        for key, value in dict(param_overrides or {}).items():
            if key in params:
                params[key] = float(value)
        return params

    def evaluate(
        self,
        state: PositionState,
        playbook: str = "",
        regime: str = "",
        param_overrides: dict[str, float] | None = None,
    ) -> SellSignal | None:
        """评估卖出信号（支持 playbook-aware 和 regime-aware 参数）。"""
        price = state.current_price
        entry = state.entry_price
        atr = state.atr
        p = self._resolve_params(playbook, regime, param_overrides=param_overrides)

        # 初始止损
        initial_stop = entry - p["atr_stop_mult"] * atr
        if price <= initial_stop:
            return SellSignal(symbol=state.symbol, reason=SellReason.INITIAL_STOP, sell_ratio=1.0, current_price=price, stop_price=initial_stop)

        # 移动止损
        if state.trailing_stop > 0 and price <= state.trailing_stop:
            return SellSignal(symbol=state.symbol, reason=SellReason.TRAILING_STOP, sell_ratio=1.0, current_price=price, stop_price=state.trailing_stop)

        # 第一止盈 (减仓50%)
        tp1 = entry + p["atr_tp1_mult"] * atr
        if price >= tp1 and not state.tp1_triggered:
            return SellSignal(symbol=state.symbol, reason=SellReason.TAKE_PROFIT_1, sell_ratio=0.5, current_price=price, stop_price=tp1)

        # 第二止盈 (再减30%)
        tp2 = entry + p["atr_tp2_mult"] * atr
        if price >= tp2 and not state.tp2_triggered:
            return SellSignal(symbol=state.symbol, reason=SellReason.TAKE_PROFIT_2, sell_ratio=0.3, current_price=price, stop_price=tp2)

        # 时间止损
        time_stop_days = int(p["time_stop_days"])
        if state.holding_days >= time_stop_days and price <= entry:
            return SellSignal(symbol=state.symbol, reason=SellReason.TIME_STOP, sell_ratio=1.0, current_price=price, stop_price=entry)

        return None

    def evaluate_with_context(
        self,
        state: PositionState,
        position: PositionSnapshot,
        ctx: ExitContext | None = None,
        quote: QuoteSnapshot | None = None,
        sector: SectorProfile | None = None,
        playbook: str = "",
        regime: str = "",
        param_overrides: dict[str, float] | None = None,
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
        return self.evaluate(
            state,
            playbook=playbook,
            regime=regime,
            param_overrides=param_overrides,
        )

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
