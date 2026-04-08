"""每日情绪自动计算"""

from __future__ import annotations

from ..contracts import MarketProfile, SentimentPhase
from ..infra.filters import get_price_limit_ratio
from .indicators import SentimentIndicators, calc_sentiment_score
from .cycle import SentimentCycle
from .position_map import PositionMapper
from .regime import enrich_market_profile
from ..logging_config import get_logger

logger = get_logger("sentiment.calculator")


class SentimentCalculator:
    """每日情绪自动计算器"""

    def __init__(self) -> None:
        self.cycle = SentimentCycle()
        self.mapper = PositionMapper()

    def calc_daily(self, ind: SentimentIndicators) -> MarketProfile:
        """计算当日市场情绪画像"""
        score = calc_sentiment_score(ind)
        phase = self.cycle.determine_phase(score)
        profile = MarketProfile(
            sentiment_phase=phase,
            sentiment_score=score,
            position_ceiling=self.mapper.get_ceiling(phase),
        )
        profile = enrich_market_profile(profile, ind)
        logger.info("情绪计算: %s 得分=%.1f 阶段=%s 仓位上限=%.0f%%", ind.date, score, phase, profile.position_ceiling * 100)
        return profile

    def calc_from_market_data(self, market_adapter, special_fetcher=None) -> MarketProfile | None:
        """从行情数据自动计算情绪 (优先用涨停池数据)"""
        try:
            # 优先使用 SpecialDataFetcher 获取精确涨跌停数据
            if special_fetcher:
                return self._calc_from_special(special_fetcher, market_adapter)

            # 回退: 从快照采样估算
            universe = market_adapter.get_a_share_universe()
            if not universe:
                return None
            snapshots = market_adapter.get_snapshots(universe[:500])

            limit_up = sum(1 for s in snapshots if s.pre_close > 0 and s.last_price >= s.pre_close * (1 + get_price_limit_ratio(s.symbol) - 0.001))
            limit_down = sum(1 for s in snapshots if s.pre_close > 0 and s.last_price <= s.pre_close * (1 - get_price_limit_ratio(s.symbol) + 0.001))
            up_count = sum(1 for s in snapshots if s.last_price > s.pre_close)
            down_count = sum(1 for s in snapshots if s.last_price < s.pre_close)
            total_amount = sum(s.volume for s in snapshots) / 1e8  # 粗略估算成交额(亿)

            ind = SentimentIndicators(
                date="today",
                limit_up_count=limit_up,
                limit_down_count=limit_down,
                up_down_ratio=up_count / max(down_count, 1),
                total_amount_billion=total_amount,
            )
            return self.calc_daily(ind)
        except Exception as e:
            logger.warning("情绪计算失败: %s", e)
            return None

    def _calc_from_special(self, special_fetcher, market_adapter) -> MarketProfile | None:
        """使用 SpecialDataFetcher 获取精确涨跌停数据"""
        try:
            data = special_fetcher.fetch_market_sentiment_data()
            ind = SentimentIndicators(
                date="today",
                limit_up_count=data.get("limit_up_count", 0),
                limit_down_count=data.get("limit_down_count", 0),
                board_fail_rate=data.get("board_fail_rate", 0.0),
                up_down_ratio=data.get("up_down_ratio", 1.0),
                total_amount_billion=data.get("total_amount_billion", 0.0),
            )
            return self.calc_daily(ind)
        except Exception as e:
            logger.warning("SpecialDataFetcher 情绪计算失败: %s", e)
            return None
