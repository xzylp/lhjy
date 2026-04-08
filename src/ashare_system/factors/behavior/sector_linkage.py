"""行为因子 - 板块联动最小集。

输入约定:
- 不依赖逐笔或盘口，只使用聚合后的板块/市场列。
- 缺失列时尽量回退到 0 或 NaN 安全计算，保证最小可用。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..registry import registry


def _safe_series(df: pd.DataFrame, column: str, default: float = 0.0) -> pd.Series:
    if column in df.columns:
        return pd.to_numeric(df[column], errors="coerce")
    return pd.Series(default, index=df.index, dtype=float)


@registry.register("sector_relative_return_5d", "behavior", "板块相对大盘 5 日强度")
def sector_relative_return_5d(df: pd.DataFrame) -> pd.Series:
    sector_ret = _safe_series(df, "sector_close", _safe_series(df, "close")).pct_change(5)
    market_ret = _safe_series(df, "market_close", _safe_series(df, "close")).pct_change(5)
    return sector_ret.sub(market_ret, fill_value=0.0)


@registry.register("sector_relative_volume_5d", "behavior", "板块成交额/成交量放大程度")
def sector_relative_volume_5d(df: pd.DataFrame) -> pd.Series:
    short = _safe_series(df, "sector_volume", _safe_series(df, "volume")).rolling(5).mean()
    long = _safe_series(df, "sector_volume", _safe_series(df, "volume")).rolling(20).mean()
    return short.div(long.replace(0, np.nan))


@registry.register("sector_breadth_thrust", "behavior", "板块涨跌家数扩散力度")
def sector_breadth_thrust(df: pd.DataFrame) -> pd.Series:
    advancers = _safe_series(df, "sector_advancers")
    decliners = _safe_series(df, "sector_decliners")
    total = advancers.add(decliners, fill_value=0.0).replace(0, np.nan)
    return advancers.sub(decliners, fill_value=0.0).div(total)


@registry.register("sector_limit_up_ratio", "behavior", "板块涨停占比")
def sector_limit_up_ratio(df: pd.DataFrame) -> pd.Series:
    zt_count = _safe_series(df, "sector_limit_up_count")
    constituents = _safe_series(df, "sector_constituent_count")
    return zt_count.div(constituents.replace(0, np.nan))


@registry.register("sector_reseal_ratio", "behavior", "板块回封占炸板比例")
def sector_reseal_ratio(df: pd.DataFrame) -> pd.Series:
    reseal = _safe_series(df, "sector_reseal_count")
    bombs = _safe_series(df, "sector_bomb_count")
    base = reseal.add(bombs, fill_value=0.0).replace(0, np.nan)
    return reseal.div(base)


@registry.register("sector_leader_premium_5d", "behavior", "龙头相对板块 5 日溢价")
def sector_leader_premium_5d(df: pd.DataFrame) -> pd.Series:
    leader_ret = _safe_series(df, "leader_close", _safe_series(df, "close")).pct_change(5)
    sector_ret = _safe_series(df, "sector_close", _safe_series(df, "close")).pct_change(5)
    return leader_ret.sub(sector_ret, fill_value=0.0)


@registry.register("sector_linkage_heat", "behavior", "板块联动热度综合分")
def sector_linkage_heat(df: pd.DataFrame) -> pd.Series:
    breadth = sector_breadth_thrust(df).fillna(0.0)
    zt_ratio = sector_limit_up_ratio(df).fillna(0.0)
    vol_ratio = sector_relative_volume_5d(df).fillna(0.0)
    relative_ret = sector_relative_return_5d(df).fillna(0.0)
    return (
        breadth * 0.30
        + zt_ratio * 0.25
        + relative_ret * 0.25
        + vol_ratio.clip(lower=0.0, upper=3.0).sub(1.0) * 0.20
    )
