"""情绪拐点识别"""

from __future__ import annotations

from dataclasses import dataclass

from .indicators import SentimentIndicators
from ..contracts import SentimentPhase
from ..logging_config import get_logger

logger = get_logger("sentiment.turning_point")


@dataclass
class TurningSignal:
    detected: bool
    from_phase: SentimentPhase
    to_phase: SentimentPhase
    reason: str
    confidence: float


class TurningPointDetector:
    """情绪拐点检测器"""

    def detect(self, current: SentimentIndicators, history: list[SentimentIndicators], current_phase: SentimentPhase) -> TurningSignal:
        """检测是否发生阶段切换"""
        if len(history) < 2:
            return TurningSignal(detected=False, from_phase=current_phase, to_phase=current_phase, reason="历史数据不足", confidence=0.0)

        prev = history[-1]
        prev2 = history[-2]

        # 冰点→回暖: 连续2日涨停>30且跌停<5
        if current_phase == "冰点":
            if (current.limit_up_count > 30 and current.limit_down_count < 5 and prev.limit_up_count > 30 and prev.limit_down_count < 5):
                return TurningSignal(detected=True, from_phase="冰点", to_phase="回暖", reason="连续2日涨停>30且跌停<5", confidence=0.75)

        # 回暖→主升: 连板高度≥5且炸板率<30%
        if current_phase == "回暖":
            if current.max_consecutive_up >= 5 and current.board_fail_rate < 0.3:
                return TurningSignal(detected=True, from_phase="回暖", to_phase="主升", reason=f"连板高度{current.max_consecutive_up}且炸板率{current.board_fail_rate:.0%}", confidence=0.80)

        # 主升→高潮: 涨停>100或成交额>1.5万亿
        if current_phase == "主升":
            if current.limit_up_count > 100 or current.total_amount_billion > 15000:
                return TurningSignal(detected=True, from_phase="主升", to_phase="高潮", reason=f"涨停{current.limit_up_count}家或成交额{current.total_amount_billion:.0f}亿", confidence=0.70)

        # 高潮→冰点: 跌停>涨停×3
        if current_phase == "高潮":
            if current.limit_down_count > current.limit_up_count * 3:
                return TurningSignal(detected=True, from_phase="高潮", to_phase="冰点", reason=f"跌停{current.limit_down_count}>>涨停{current.limit_up_count}", confidence=0.85)

        return TurningSignal(detected=False, from_phase=current_phase, to_phase=current_phase, reason="无拐点信号", confidence=0.0)
