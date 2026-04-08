"""紧急预案 — 熔断/强平"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import PositionSnapshot
from ..logging_config import get_logger

logger = get_logger("risk.emergency")


@dataclass
class EmergencyAction:
    action_type: str  # "circuit_break" | "force_close" | "reduce_position"
    symbols: list[str]
    reason: str


class EmergencyHandler:
    """紧急预案处理器"""

    def circuit_break(self, reason: str) -> EmergencyAction:
        """触发熔断: 停止所有新买入"""
        logger.critical("熔断触发: %s", reason)
        return EmergencyAction(action_type="circuit_break", symbols=[], reason=reason)

    def force_close_all(self, positions: list[PositionSnapshot], reason: str) -> EmergencyAction:
        """强制平仓所有持仓"""
        symbols = [p.symbol for p in positions if p.available > 0]
        logger.critical("强制平仓 %d 只: %s", len(symbols), reason)
        return EmergencyAction(action_type="force_close", symbols=symbols, reason=reason)

    def reduce_position(self, positions: list[PositionSnapshot], ratio: float, reason: str) -> EmergencyAction:
        """按比例减仓"""
        symbols = [p.symbol for p in positions if p.available > 0]
        logger.warning("减仓 %.0f%% (%d只): %s", ratio * 100, len(symbols), reason)
        return EmergencyAction(action_type="reduce_position", symbols=symbols, reason=reason)

    def evaluate_drawdown(self, current_equity: float, peak_equity: float, positions: list[PositionSnapshot]) -> EmergencyAction | None:
        """根据回撤自动触发预案"""
        if peak_equity <= 0:
            return None
        dd = (current_equity - peak_equity) / peak_equity
        if dd <= -0.15:
            return self.force_close_all(positions, f"组合回撤 {dd:.1%} 触发强平")
        if dd <= -0.10:
            return self.reduce_position(positions, 0.5, f"组合回撤 {dd:.1%} 触发减仓50%")
        return None
