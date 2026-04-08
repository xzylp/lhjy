"""K线数据获取与管理"""

from __future__ import annotations

from ..contracts import BarSnapshot, DataQuality
from ..logging_config import get_logger
from .contracts import FetchResult

logger = get_logger("data.kline")


class KlineManager:
    """K线数据管理器 — 支持多周期"""

    def __init__(self, market_adapter) -> None:
        self.market = market_adapter

    def get_daily(self, symbols: list[str], count: int = 1) -> FetchResult:
        return self._fetch(symbols, "1d", count=count)

    def get_minute(self, symbols: list[str], period: str = "1m", count: int = 1) -> FetchResult:
        if period not in ("1m", "5m", "15m", "60m"):
            logger.warning("不支持的分钟周期: %s, 使用 1m", period)
            period = "1m"
        return self._fetch(symbols, period, count=count)

    def _fetch(self, symbols: list[str], period: str, count: int = 1) -> FetchResult:
        try:
            bars = self.market.get_bars(symbols, period, count=count)
            return FetchResult(
                symbols=symbols,
                bars=bars,
                quality=DataQuality(source="real", completeness=len(bars) / max(len(symbols), 1)),
            )
        except Exception as e:
            logger.warning("K线获取失败 (%s): %s", period, e)
            return FetchResult(
                symbols=symbols,
                quality=DataQuality(source="unavailable"),
                errors=[str(e)],
            )

    def sync_history(self, symbols: list[str], period: str = "1d", start_time: str = "") -> dict:
        """同步历史数据到本地"""
        try:
            return self.market.sync_history(symbols, period, start_time)
        except Exception as e:
            logger.warning("历史数据同步失败: %s", e)
            return {"accepted_symbols": [], "error": str(e)}
