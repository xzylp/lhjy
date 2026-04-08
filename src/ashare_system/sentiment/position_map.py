"""情绪周期 → 仓位映射"""

from __future__ import annotations

from ..contracts import MarketProfile, SentimentPhase

PHASE_POSITION_MAP: dict[str, dict] = {
    "冰点": {
        "ceiling": 0.20,
        "floor": 0.0,
        "allow_chase": False,
        "allow_board": False,
        "strategy": "仅低吸超跌，禁止追高打板",
    },
    "回暖": {
        "ceiling": 0.60,
        "floor": 0.10,
        "allow_chase": True,
        "allow_board": False,
        "strategy": "低吸+半仓试错，禁止满仓",
    },
    "主升": {
        "ceiling": 0.80,
        "floor": 0.30,
        "allow_chase": True,
        "allow_board": True,
        "strategy": "追强+打板+加仓，积极进攻",
    },
    "高潮": {
        "ceiling": 0.30,
        "floor": 0.0,
        "allow_chase": False,
        "allow_board": False,
        "strategy": "减仓兑现利润，禁止追高加仓",
    },
}


class PositionMapper:
    """情绪阶段 → 仓位映射"""

    def get_ceiling(self, phase: SentimentPhase) -> float:
        return PHASE_POSITION_MAP.get(phase, {}).get("ceiling", 0.6)

    def get_floor(self, phase: SentimentPhase) -> float:
        return PHASE_POSITION_MAP.get(phase, {}).get("floor", 0.0)

    def get_strategy_hint(self, phase: SentimentPhase) -> str:
        return PHASE_POSITION_MAP.get(phase, {}).get("strategy", "")

    def apply_to_profile(self, profile: MarketProfile) -> MarketProfile:
        ceiling = self.get_ceiling(profile.sentiment_phase)
        return profile.model_copy(update={"position_ceiling": ceiling})
