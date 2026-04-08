"""数据清洗 pipeline — 过滤 → 修复 → 标准化"""

from __future__ import annotations

from ..contracts import BarSnapshot
from ..infra.filters import get_price_limit_ratio
from ..logging_config import get_logger
from .contracts import CleanedBar, StockMeta

logger = get_logger("data.cleaner")


class DataCleaner:
    """三阶段数据清洗 pipeline"""

    def clean(self, bars: list[BarSnapshot], meta: dict[str, StockMeta] | None = None) -> list[CleanedBar]:
        meta = meta or {}
        # 按 symbol + trade_time 排序，确保 pre_close 追踪正确
        sorted_bars = sorted(bars, key=lambda b: (b.symbol, b.trade_time))
        stage1 = self._filter(sorted_bars, meta)
        stage2 = self._repair(stage1)
        return stage2

    # ── 阶段 1: 过滤 ──

    def _filter(self, bars: list[BarSnapshot], meta: dict[str, StockMeta]) -> list[CleanedBar]:
        """剔除不可用数据: 停牌、次新、ST、K线不足"""
        result = []
        prev_close_map: dict[str, float] = {}
        for bar in bars:
            m = meta.get(bar.symbol)
            if self._is_suspended(bar):
                prev_close_map[bar.symbol] = bar.close
                continue
            if m and m.is_st:
                continue
            if m and m.list_days < 20:
                continue
            # 计算涨跌幅: 优先用 bar.pre_close，其次用前一根K线的 close
            ref_price = bar.pre_close if bar.pre_close > 0 else prev_close_map.get(bar.symbol, 0.0)
            change_pct = self._calc_change_pct(bar.close, ref_price)
            is_st = m.is_st if m else False
            limit_ratio = get_price_limit_ratio(bar.symbol, is_st=is_st) * 100
            cleaned = CleanedBar(
                bar=bar,
                quality=self._default_quality(),
                adjusted_close=bar.close,
                change_pct=change_pct,
                is_suspended=False,
                is_st=is_st,
                is_limit_up=change_pct >= limit_ratio - 0.1 if ref_price > 0 else False,
                is_limit_down=change_pct <= -(limit_ratio - 0.1) if ref_price > 0 else False,
            )
            result.append(cleaned)
            prev_close_map[bar.symbol] = bar.close
        logger.info("过滤: %d → %d 条", len(bars), len(result))
        return result

    # ── 阶段 2: 修复 ──

    def _repair(self, bars: list[CleanedBar]) -> list[CleanedBar]:
        """修复数据缺陷: 异常值检测"""
        repaired = []
        for bar in bars:
            limit_ratio = get_price_limit_ratio(bar.bar.symbol, is_st=bar.is_st) * 100
            if abs(bar.change_pct) > limit_ratio + 1 and not bar.is_limit_up and not bar.is_limit_down:
                logger.warning("异常涨跌幅 %s: %.2f%% (限制%.0f%%)", bar.bar.symbol, bar.change_pct, limit_ratio)
            repaired.append(bar)
        return repaired

    # ── 工具方法 ──

    @staticmethod
    def _is_suspended(bar: BarSnapshot) -> bool:
        return bar.volume == 0 and bar.open == bar.close == bar.high == bar.low

    @staticmethod
    def _calc_change_pct(close: float, pre_close: float) -> float:
        """涨跌幅 = (收盘价 - 前收盘价) / 前收盘价 * 100"""
        if pre_close <= 0:
            return 0.0
        return (close - pre_close) / pre_close * 100

    @staticmethod
    def _default_quality():
        from ..contracts import DataQuality
        return DataQuality(source="real", completeness=1.0)
