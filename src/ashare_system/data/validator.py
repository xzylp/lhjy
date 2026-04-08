"""数据完整性验证"""

from __future__ import annotations

from ..contracts import BarSnapshot
from ..logging_config import get_logger

logger = get_logger("data.validator")


class DataValidator:
    """数据完整性和一致性校验"""

    def validate_bars(self, bars: list[BarSnapshot]) -> list[str]:
        """校验 K 线数据，返回问题列表"""
        issues: list[str] = []
        for bar in bars:
            issues.extend(self._validate_single(bar))
        if issues:
            logger.warning("数据校验发现 %d 个问题", len(issues))
        return issues

    def _validate_single(self, bar: BarSnapshot) -> list[str]:
        issues: list[str] = []
        if bar.high < bar.low:
            issues.append(f"{bar.symbol}: high({bar.high}) < low({bar.low})")
        if bar.close > bar.high or bar.close < bar.low:
            issues.append(f"{bar.symbol}: close({bar.close}) 超出 high/low 范围")
        if bar.open > bar.high or bar.open < bar.low:
            issues.append(f"{bar.symbol}: open({bar.open}) 超出 high/low 范围")
        if bar.volume < 0:
            issues.append(f"{bar.symbol}: volume({bar.volume}) 为负")
        if bar.amount < 0:
            issues.append(f"{bar.symbol}: amount({bar.amount}) 为负")
        return issues

    def check_completeness(self, symbols: list[str], bars: list[BarSnapshot]) -> float:
        """检查数据完整度 (0-1)"""
        if not symbols:
            return 1.0
        received = {bar.symbol for bar in bars}
        return len(received) / len(symbols)
