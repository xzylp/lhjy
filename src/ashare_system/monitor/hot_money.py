"""游资战法识别 — 常见游资手法模式匹配"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..logging_config import get_logger

logger = get_logger("monitor.hot_money")


class HotMoneyPattern(str, Enum):
    OPEN_HIGH_CLOSE_HIGH = "开高走高"      # 开盘即涨停，全天封板
    OPEN_FLAT_LIMIT_UP = "平开涨停"        # 平开后快速涨停
    PULL_BACK_LIMIT_UP = "回调涨停"        # 盘中回调后再次涨停
    CONTINUOUS_LIMIT = "连板"              # 连续涨停
    DRAGON_TIGER_BUY = "龙虎榜买入"        # 知名游资席位买入
    LARGE_ORDER_PUSH = "大单推升"          # 大单持续推升
    TAIL_MARKET_PULL = "尾盘拉升"          # 尾盘快速拉升
    UNKNOWN = "未知"


@dataclass
class HotMoneySignal:
    symbol: str
    name: str
    pattern: HotMoneyPattern
    confidence: float           # 0-1
    seats: list[str] = field(default_factory=list)
    description: str = ""


class HotMoneyDetector:
    """游资战法识别器"""

    # 知名游资席位关键词
    HOT_SEATS = ["宁波", "东方财富", "华鑫", "方正", "国泰君安上海", "中信证券上海", "招商证券深圳"]

    def detect(self, symbol: str, name: str, ohlcv: dict, dragon_tiger_seats: list[str] | None = None) -> HotMoneySignal | None:
        """检测游资战法"""
        open_p = ohlcv.get("open", 0)
        close_p = ohlcv.get("close", 0)
        high_p = ohlcv.get("high", 0)
        pre_close = ohlcv.get("pre_close", 0)
        volume = ohlcv.get("volume", 0)
        avg_volume = ohlcv.get("avg_volume_20d", volume)

        if pre_close <= 0:
            return None

        limit_up_price = pre_close * 1.099
        is_limit_up = close_p >= limit_up_price
        open_change = (open_p - pre_close) / pre_close if pre_close > 0 else 0
        volume_ratio = volume / max(avg_volume, 1)

        # 龙虎榜游资买入
        if dragon_tiger_seats:
            hot_seats = [s for s in dragon_tiger_seats if any(kw in s for kw in self.HOT_SEATS)]
            if hot_seats and is_limit_up:
                return HotMoneySignal(
                    symbol=symbol, name=name,
                    pattern=HotMoneyPattern.DRAGON_TIGER_BUY,
                    confidence=0.85, seats=hot_seats,
                    description=f"游资席位买入涨停: {', '.join(hot_seats[:2])}",
                )

        # 开高走高
        if open_change >= 0.05 and is_limit_up and volume_ratio >= 2.0:
            return HotMoneySignal(
                symbol=symbol, name=name,
                pattern=HotMoneyPattern.OPEN_HIGH_CLOSE_HIGH,
                confidence=0.75,
                description=f"开高{open_change:.1%}后涨停，量比{volume_ratio:.1f}",
            )

        # 平开涨停
        if abs(open_change) < 0.01 and is_limit_up and volume_ratio >= 1.5:
            return HotMoneySignal(
                symbol=symbol, name=name,
                pattern=HotMoneyPattern.OPEN_FLAT_LIMIT_UP,
                confidence=0.70,
                description=f"平开涨停，量比{volume_ratio:.1f}",
            )

        # 尾盘拉升
        intraday_high_change = (high_p - open_p) / max(open_p, 1)
        if intraday_high_change >= 0.05 and close_p >= high_p * 0.98 and volume_ratio >= 2.0:
            return HotMoneySignal(
                symbol=symbol, name=name,
                pattern=HotMoneyPattern.TAIL_MARKET_PULL,
                confidence=0.60,
                description=f"尾盘拉升{intraday_high_change:.1%}，量比{volume_ratio:.1f}",
            )

        return None

    def batch_detect(self, stocks: list[dict]) -> list[HotMoneySignal]:
        """批量检测"""
        signals: list[HotMoneySignal] = []
        for stock in stocks:
            signal = self.detect(
                symbol=stock.get("symbol", ""),
                name=stock.get("name", ""),
                ohlcv=stock,
                dragon_tiger_seats=stock.get("dragon_tiger_seats"),
            )
            if signal:
                signals.append(signal)
                logger.info("游资战法: %s [%s] %.0f%%", signal.symbol, signal.pattern, signal.confidence * 100)
        return signals
