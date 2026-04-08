"""市场状态机：基于日级情绪与涨停统计输出交易 regime。"""

from __future__ import annotations

from ..contracts import MarketProfile, PlaybookName, RegimeName
from .indicators import SentimentIndicators

REGIME_SCORE = {
    "chaos": 0.10,
    "defensive": 0.35,
    "rotation": 0.65,
    "trend": 0.90,
}


def classify_regime(
    zt_count: int,
    board_fail_rate: float,
    seal_rate: float,
    max_consecutive: int,
    up_down_ratio: float,
    prev_day_premium: float = 0.0,
    theme_concentration: float = 1.0,
) -> tuple[RegimeName, list[PlaybookName], list[str]]:
    """基于关键统计指标识别交易 regime。"""

    risk_flags: list[str] = []

    if zt_count < 15 or up_down_ratio < 0.5:
        return "chaos", [], ["涨停数不足", "市场极弱"]

    if board_fail_rate > 0.40 and prev_day_premium < -0.02:
        return "defensive", [], ["高炸板率", "昨日涨停溢价转弱"]

    if zt_count > 40 and seal_rate > 0.70 and max_consecutive >= 5:
        return "trend", ["leader_chase", "divergence_reseal"], []

    if 20 <= zt_count <= 40 and theme_concentration < 0.60:
        return "rotation", ["sector_reflow_first_board", "divergence_reseal"], []

    if board_fail_rate > 0.30:
        risk_flags.append("炸板率偏高")
    if prev_day_premium < 0:
        risk_flags.append("昨日涨停溢价偏弱")
    return "defensive", [], risk_flags


def enrich_market_profile(
    profile: MarketProfile,
    indicators: SentimentIndicators,
    *,
    prev_day_premium: float = 0.0,
    theme_concentration: float = 1.0,
) -> MarketProfile:
    """在已有情绪画像上补充 regime 相关字段。"""

    seal_rate = max(0.0, min(1.0, 1.0 - indicators.board_fail_rate))
    regime, allowed_playbooks, risk_flags = classify_regime(
        zt_count=indicators.limit_up_count,
        board_fail_rate=indicators.board_fail_rate,
        seal_rate=seal_rate,
        max_consecutive=indicators.max_consecutive_up,
        up_down_ratio=indicators.up_down_ratio,
        prev_day_premium=prev_day_premium,
        theme_concentration=theme_concentration,
    )
    hot_sectors = list(profile.hot_sectors)
    if theme_concentration > 0 and regime in {"trend", "rotation"} and not hot_sectors:
        hot_sectors = ["待补充板块映射"]
    return profile.model_copy(
        update={
            "regime": regime,
            "regime_score": REGIME_SCORE[regime],
            "allowed_playbooks": allowed_playbooks,
            "market_risk_flags": risk_flags,
            "hot_sectors": hot_sectors,
        }
    )
