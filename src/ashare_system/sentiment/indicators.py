"""情绪量化指标"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class SentimentIndicators:
    """每日情绪量化指标"""
    date: str
    limit_up_count: int = 0       # 涨停家数
    limit_down_count: int = 0     # 跌停家数
    board_fail_rate: float = 0.0  # 炸板率
    max_consecutive_up: int = 0   # 最高连板高度
    up_down_ratio: float = 1.0    # 涨跌比
    total_amount_billion: float = 0.0  # 两市成交额 (亿)


def calc_sentiment_score(ind: SentimentIndicators) -> float:
    """
    综合情绪得分 [0, 100]
    权重: 涨停0.25 + 跌停0.20 + 炸板0.15 + 连板0.15 + 涨跌比0.15 + 成交额0.10
    """
    # 涨停得分 (0-100, 以100家为满分)
    limit_up_score = min(ind.limit_up_count / 100, 1.0) * 100

    # 跌停得分 (反向, 跌停越多得分越低)
    limit_down_score = max(0, 1 - ind.limit_down_count / 50) * 100

    # 炸板率得分 (炸板率越低越好)
    board_score = max(0, 1 - ind.board_fail_rate) * 100

    # 连板高度得分 (以10板为满分)
    consec_score = min(ind.max_consecutive_up / 10, 1.0) * 100

    # 涨跌比得分
    ratio_score = min(ind.up_down_ratio / 3, 1.0) * 100

    # 成交额得分 (以1万亿为满分)
    amount_score = min(ind.total_amount_billion / 10000, 1.0) * 100

    score = (
        limit_up_score * 0.25
        + limit_down_score * 0.20
        + board_score * 0.15
        + consec_score * 0.15
        + ratio_score * 0.15
        + amount_score * 0.10
    )
    return round(score, 2)
