"""行为因子 - 涨停/炸板/回封专项最小集。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..registry import registry


def _safe_series(df: pd.DataFrame, column: str, default: float = 0.0) -> pd.Series:
    if column in df.columns:
        return pd.to_numeric(df[column], errors="coerce")
    return pd.Series(default, index=df.index, dtype=float)


@registry.register("board_limit_up_distance", "behavior", "收盘距涨停价的距离")
def board_limit_up_distance(df: pd.DataFrame) -> pd.Series:
    close = _safe_series(df, "close")
    limit_up_price = _safe_series(df, "limit_up_price")
    return close.div(limit_up_price.replace(0, np.nan)).sub(1.0)


@registry.register("board_bomb_risk", "behavior", "炸板风险代理")
def board_bomb_risk(df: pd.DataFrame) -> pd.Series:
    high = _safe_series(df, "high")
    close = _safe_series(df, "close")
    limit_up_price = _safe_series(df, "limit_up_price")
    intraday_hit = high.ge(limit_up_price * 0.998)
    close_away = close.lt(limit_up_price * 0.985)
    return (intraday_hit & close_away).astype(float)


@registry.register("board_reseal_strength", "behavior", "回封强度")
def board_reseal_strength(df: pd.DataFrame) -> pd.Series:
    close = _safe_series(df, "close")
    low = _safe_series(df, "low")
    limit_up_price = _safe_series(df, "limit_up_price")
    drawdown = limit_up_price.sub(low, fill_value=0.0).div(limit_up_price.replace(0, np.nan))
    restore = close.sub(low, fill_value=0.0).div(limit_up_price.replace(0, np.nan))
    return restore.sub(drawdown.fillna(0.0)).fillna(0.0)


@registry.register("board_reseal_persistence", "behavior", "回封后封单持续性代理")
def board_reseal_persistence(df: pd.DataFrame) -> pd.Series:
    close = _safe_series(df, "close")
    limit_up_price = _safe_series(df, "limit_up_price")
    amount = _safe_series(df, "amount")
    amount_ma = amount.rolling(5).mean().replace(0, np.nan)
    near_limit = close.ge(limit_up_price * 0.995).astype(float)
    return near_limit * amount.div(amount_ma).fillna(0.0)


@registry.register("board_limit_up_premium", "behavior", "涨停后次日溢价代理")
def board_limit_up_premium(df: pd.DataFrame) -> pd.Series:
    close = _safe_series(df, "close")
    pre_close = _safe_series(df, "pre_close")
    limit_up_price = _safe_series(df, "limit_up_price")
    close_pct = close.div(pre_close.replace(0, np.nan)).sub(1.0)
    hit_limit = close.ge(limit_up_price * 0.995).astype(float)
    return close_pct.fillna(0.0) * hit_limit
