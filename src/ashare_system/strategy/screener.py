"""选股漏斗 — 基础过滤 → 环境 → 因子 → AI → 集中度控制"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import MarketProfile
from ..infra.filters import get_price_limit_ratio
from ..logging_config import get_logger
from ..runtime_config import RuntimeConfig

logger = get_logger("strategy.screener")

# 宏观过滤阈值
MIN_MARKET_AMOUNT_BILLION = 5000  # 两市最低成交额 (亿)
MAX_LIMIT_DOWN_RATIO = 3.0        # 跌停/涨停比超过此值触发极端弱势


@dataclass
class ScreenerResult:
    passed: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    stage_stats: dict[str, int] = field(default_factory=dict)


@dataclass
class StockInfo:
    """选股所需的个股信息"""
    symbol: str
    is_st: bool = False
    list_days: int = 999
    turnover_rate_5d: float = 1.0
    is_limit_up: bool = False
    is_suspended: bool = False
    industry: str = ""


class StockScreener:
    """五层选股漏斗"""

    def run(
        self,
        candidates: list[str],
        profile: MarketProfile | None = None,
        factor_scores: dict[str, float] | None = None,
        ai_scores: dict[str, float] | None = None,
        stock_info: dict[str, StockInfo] | None = None,
        runtime_config: RuntimeConfig | None = None,
        min_factor_score: float = 0.5,
        min_ai_score: float = 0.6,
        top_n: int = 30,
        max_per_industry: int = 3,
    ) -> ScreenerResult:
        # 运行时配置覆盖默认值
        if runtime_config:
            top_n = runtime_config.screener_pool_size

        result = ScreenerResult()
        pool = list(candidates)

        # 层-1: 买股范围过滤 (动态配置)
        if runtime_config:
            pool = [s for s in pool if runtime_config.scope.is_allowed(s)]
        result.stage_stats["input"] = len(pool)

        # 层0: 基础过滤 (ST/涨停/停牌/次新/流动性)
        pool = self._filter_basic(pool, stock_info, result)
        result.stage_stats["after_basic"] = len(pool)

        # 层1: 环境过滤
        pool = self._filter_environment(pool, profile, result)
        result.stage_stats["after_env"] = len(pool)

        # 层2: 因子过滤
        if factor_scores:
            pool = self._filter_by_score(pool, factor_scores, min_factor_score, result, "factor_low")
        result.stage_stats["after_factor"] = len(pool)

        # 层3: AI 评分过滤
        if ai_scores:
            pool = self._filter_by_score(pool, ai_scores, min_ai_score, result, "ai_low")
        result.stage_stats["after_ai"] = len(pool)

        # 层4: 行业集中度控制 + Top-N
        scores = ai_scores or factor_scores or {}
        pool = sorted(pool, key=lambda s: scores.get(s, 0), reverse=True)
        pool = self._limit_industry_concentration(pool, stock_info, max_per_industry, result)
        pool = pool[:top_n]

        # 热门板块加分排序
        if profile and profile.hot_sectors and stock_info:
            pool = self._boost_hot_sectors(pool, stock_info, profile.hot_sectors, scores)

        result.passed = pool
        result.stage_stats["final"] = len(pool)
        logger.info("选股漏斗: %d → %d", result.stage_stats["input"], len(pool))
        return result

    def _filter_basic(self, pool: list[str], info: dict[str, StockInfo] | None, result: ScreenerResult) -> list[str]:
        """层0: ST排除 + 涨停排除 + 停牌排除 + 上市天数≥60 + 换手率≥0.5%"""
        if not info:
            return pool
        passed = []
        for s in pool:
            si = info.get(s)
            if si is None:
                passed.append(s)
                continue
            if si.is_st:
                result.rejected[s] = "ST股"
            elif si.is_suspended:
                result.rejected[s] = "停牌"
            elif si.is_limit_up:
                result.rejected[s] = "涨停无法买入"
            elif si.list_days < 60:
                result.rejected[s] = f"上市仅{si.list_days}天"
            elif si.turnover_rate_5d < 0.5:
                result.rejected[s] = f"换手率{si.turnover_rate_5d:.1f}%过低"
            else:
                passed.append(s)
        return passed

    def _filter_environment(self, pool: list[str], profile: MarketProfile | None, result: ScreenerResult) -> list[str]:
        """层1: 极端弱势市场禁止买入"""
        if profile is None:
            return pool
        if profile.sentiment_phase == "冰点" and profile.sentiment_score < 15:
            for s in pool:
                result.rejected[s] = "市场冰点，禁止买入"
            logger.warning("市场冰点，全部候选被拒绝")
            return []
        return pool

    def _filter_by_score(self, pool: list[str], scores: dict[str, float], threshold: float, result: ScreenerResult, reason: str) -> list[str]:
        passed = []
        for s in pool:
            if scores.get(s, 0) >= threshold:
                passed.append(s)
            else:
                result.rejected[s] = reason
        return passed

    def _limit_industry_concentration(self, pool: list[str], info: dict[str, StockInfo] | None, max_per: int, result: ScreenerResult) -> list[str]:
        """同行业最多 max_per 只"""
        if not info:
            return pool
        industry_count: dict[str, int] = {}
        passed = []
        for s in pool:
            si = info.get(s)
            ind = si.industry if si else ""
            if ind:
                cnt = industry_count.get(ind, 0)
                if cnt >= max_per:
                    result.rejected[s] = f"行业{ind}已满{max_per}只"
                    continue
                industry_count[ind] = cnt + 1
            passed.append(s)
        return passed

    @staticmethod
    def _boost_hot_sectors(pool: list[str], info: dict[str, StockInfo], hot: list[str], scores: dict[str, float]) -> list[str]:
        """热门板块股票排序靠前"""
        def sort_key(s: str) -> float:
            si = info.get(s)
            base = scores.get(s, 0)
            if si and si.industry in hot:
                return base * 1.15  # 热门板块加权15%
            return base
        return sorted(pool, key=sort_key, reverse=True)
