"""情绪四阶段框架"""

from __future__ import annotations

from ..contracts import SentimentPhase
from ..logging_config import get_logger

logger = get_logger("sentiment.cycle")

# 阶段分界线
PHASE_THRESHOLDS = {
    "冰点": (0, 25),
    "回暖": (25, 50),
    "主升": (50, 75),
    "高潮": (75, 100),
}


class SentimentCycle:
    """情绪四阶段框架: 冰点 → 回暖 → 主升 → 高潮"""

    def determine_phase(self, score: float) -> SentimentPhase:
        """根据情绪得分判定阶段"""
        if score < 25:
            return "冰点"
        elif score < 50:
            return "回暖"
        elif score < 75:
            return "主升"
        else:
            return "高潮"

    def get_phase_range(self, phase: SentimentPhase) -> tuple[float, float]:
        return PHASE_THRESHOLDS.get(phase, (0, 100))

    def is_transition_zone(self, score: float, margin: float = 3.0) -> bool:
        """是否处于阶段切换边界区域"""
        boundaries = [25, 50, 75]
        return any(abs(score - b) <= margin for b in boundaries)

    def describe(self, phase: SentimentPhase) -> str:
        descriptions = {
            "冰点": "市场极度悲观，赚钱效应极差，大多数股票下跌",
            "回暖": "市场底部回升，赚钱效应开始恢复，部分板块活跃",
            "主升": "市场赚钱效应扩散，多数板块上涨，连板效应明显",
            "高潮": "市场过热，追高风险极大，随时可能反转",
        }
        return descriptions.get(phase, "")
