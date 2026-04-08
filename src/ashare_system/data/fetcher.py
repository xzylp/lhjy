"""统一数据获取 — 失败返回空，绝不注入 Mock 数据"""

from __future__ import annotations

from pathlib import Path

from ..contracts import BarSnapshot, DataQuality, QuoteSnapshot
from ..logging_config import get_logger
from ..settings import AppSettings
from .cache import DataCache
from .cleaner import DataCleaner
from .contracts import CleanedBar, FetchResult, StockMeta
from .validator import DataValidator

logger = get_logger("data.fetcher")


class DataFetcher:
    """统一数据获取入口"""

    def __init__(self, market_adapter) -> None:
        self.market = market_adapter

    def fetch_snapshots(self, symbols: list[str]) -> list[QuoteSnapshot]:
        try:
            return self.market.get_snapshots(symbols)
        except Exception as e:
            logger.warning("快照获取失败: %s", e)
            return []

    def fetch_bars(self, symbols: list[str], period: str = "1d", count: int = 1) -> FetchResult:
        try:
            bars = self.market.get_bars(symbols, period, count=count)
            quality = DataQuality(source="real", completeness=1.0)
            return FetchResult(symbols=symbols, bars=bars, quality=quality)
        except Exception as e:
            logger.warning("K线获取失败 (period=%s): %s", period, e)
            return FetchResult(symbols=symbols, quality=DataQuality(source="unavailable"), errors=[str(e)])

    def fetch_universe(self, scope: str = "main-board") -> list[str]:
        try:
            if scope == "a-share":
                return self.market.get_a_share_universe()
            return self.market.get_main_board_universe()
        except Exception as e:
            logger.warning("股票池获取失败: %s", e)
            return []

    def fetch_sectors(self) -> list[str]:
        try:
            return self.market.get_sectors()
        except Exception as e:
            logger.warning("板块列表获取失败: %s", e)
            return []

    def fetch_sector_symbols(self, sector_name: str) -> list[str]:
        try:
            return self.market.get_sector_symbols(sector_name)
        except Exception as e:
            logger.warning("板块成分股获取失败 (%s): %s", sector_name, e)
            return []

    def fetch_index_quotes(self, symbols: list[str]) -> list[QuoteSnapshot]:
        try:
            return self.market.get_index_quotes(symbols)
        except Exception as e:
            logger.warning("指数行情获取失败: %s", e)
            return []


class DataPipeline:
    """端到端数据管道: fetch → cache → clean → validate"""

    def __init__(self, fetcher: DataFetcher, cache_dir: Path | None = None) -> None:
        self.fetcher = fetcher
        self.cache = DataCache(cache_dir) if cache_dir else None
        self.cleaner = DataCleaner()
        self.validator = DataValidator()

    def get_daily_bars(
        self,
        symbols: list[str],
        meta: dict[str, StockMeta] | None = None,
        count: int = 1,
    ) -> list[CleanedBar]:
        """完整管道: 获取日线 → 缓存 → 清洗 → 验证"""
        all_bars: list[BarSnapshot] = []
        uncached: list[str] = []
        requested_count = max(int(count or 1), 1)

        # 1. 检查缓存
        if self.cache:
            for sym in symbols:
                cached = self.cache.get_bars(sym, "1d")
                if cached and len(cached) >= requested_count:
                    all_bars.extend(cached[-requested_count:])
                else:
                    uncached.append(sym)
        else:
            uncached = list(symbols)

        # 2. 获取未缓存的数据
        if uncached:
            result = self.fetcher.fetch_bars(uncached, "1d", count=requested_count)
            all_bars.extend(result.bars)
            # 写入缓存
            if self.cache and result.bars:
                by_sym: dict[str, list[BarSnapshot]] = {}
                for bar in result.bars:
                    by_sym.setdefault(bar.symbol, []).append(bar)
                for sym, bars in by_sym.items():
                    self.cache.put_bars(sym, "1d", bars)

        # 3. 验证
        issues = self.validator.validate_bars(all_bars)
        if issues:
            logger.warning("数据验证问题: %d 条", len(issues))

        # 4. 清洗
        cleaned = self.cleaner.clean(all_bars, meta)
        logger.info("数据管道: %d symbols → %d bars → %d cleaned", len(symbols), len(all_bars), len(cleaned))
        return cleaned
