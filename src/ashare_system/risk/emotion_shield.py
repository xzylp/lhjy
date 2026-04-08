"""情绪保护机制"""

from __future__ import annotations

from ..contracts import MarketProfile
from ..logging_config import get_logger

logger = get_logger("risk.emotion_shield")

PHASE_CONFIG = {
    "冰点":  {"ceiling": 0.20, "allow_chase": False, "allow_board": False},
    "回暖":  {"ceiling": 0.60, "allow_chase": True,  "allow_board": False},
    "主升":  {"ceiling": 0.80, "allow_chase": True,  "allow_board": True},
    "高潮":  {"ceiling": 0.30, "allow_chase": False, "allow_board": False},
}


class EmotionShield:
    """情绪保护: 根据情绪阶段限制操作"""

    def get_position_ceiling(self, profile: MarketProfile) -> float:
        cfg = PHASE_CONFIG.get(profile.sentiment_phase, {"ceiling": 0.6})
        phase_ceiling = cfg["ceiling"]
        # MarketProfile 的默认值是 0.6；若调用方未显式覆盖，则回退到情绪阶段配置。
        if profile.position_ceiling == 0.6 and phase_ceiling != 0.6:
            return phase_ceiling
        return min(phase_ceiling, profile.position_ceiling)

    def can_chase_high(self, profile: MarketProfile) -> bool:
        cfg = PHASE_CONFIG.get(profile.sentiment_phase, {"allow_chase": True})
        return cfg["allow_chase"]

    def can_buy_limit_up(self, profile: MarketProfile) -> bool:
        cfg = PHASE_CONFIG.get(profile.sentiment_phase, {"allow_board": False})
        return cfg["allow_board"]

    def get_allowed_playbooks(self, profile: MarketProfile) -> list[str]:
        """结合情绪阶段和 regime 输出最终允许战法。"""

        if profile.sentiment_phase == "冰点":
            return []
        if profile.sentiment_phase == "高潮" and profile.regime == "trend":
            return [item for item in profile.allowed_playbooks if item == "divergence_reseal"]
        return list(profile.allowed_playbooks)

    def get_advice(self, profile: MarketProfile) -> str:
        phase = profile.sentiment_phase
        cfg = PHASE_CONFIG.get(phase, {})
        ceil = cfg.get("ceiling", 0.6)
        chase = "允许" if cfg.get("allow_chase") else "禁止"
        board = "允许" if cfg.get("allow_board") else "禁止"
        playbooks = self.get_allowed_playbooks(profile)
        playbook_text = ",".join(playbooks) if playbooks else "无"
        return f"情绪{phase}: 仓位上限{ceil:.0%}, 追高{chase}, 打板{board}, 战法={playbook_text}"
