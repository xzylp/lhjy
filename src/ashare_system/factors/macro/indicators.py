"""宏观因子 — PMI/利率/信用利差/社融 + 月度→日度广播"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..registry import registry


def _broadcast_monthly(series: pd.Series) -> pd.Series:
    """将月度数据前向填充到日度频率"""
    return series.ffill()


@registry.register("macro_rate_spread", "macro", "利差 (10年-1年国债)")
def macro_rate_spread(df: pd.DataFrame) -> pd.Series:
    if "rate_10y" in df.columns and "rate_1y" in df.columns:
        return _broadcast_monthly(df["rate_10y"]) - _broadcast_monthly(df["rate_1y"])
    return pd.Series(np.nan, index=df.index)


@registry.register("macro_pmi", "macro", "制造业PMI")
def macro_pmi(df: pd.DataFrame) -> pd.Series:
    if "pmi" in df.columns:
        return _broadcast_monthly(df["pmi"])
    return pd.Series(np.nan, index=df.index)


@registry.register("pmi_momentum", "macro", "PMI动量 (环比变化)")
def pmi_momentum(df: pd.DataFrame) -> pd.Series:
    """PMI月度环比变化，>0经济扩张加速"""
    if "pmi" in df.columns:
        return _broadcast_monthly(df["pmi"]).diff()
    return pd.Series(np.nan, index=df.index)


@registry.register("macro_usd_cny", "macro", "美元/人民币汇率")
def macro_usd_cny(df: pd.DataFrame) -> pd.Series:
    if "usd_cny" in df.columns:
        return _broadcast_monthly(df["usd_cny"])
    return pd.Series(np.nan, index=df.index)


@registry.register("macro_m2_yoy", "macro", "M2同比增速")
def macro_m2_yoy(df: pd.DataFrame) -> pd.Series:
    if "m2_yoy" in df.columns:
        return _broadcast_monthly(df["m2_yoy"])
    return pd.Series(np.nan, index=df.index)


@registry.register("yield_spread_10y_2y", "macro", "期限利差 (10Y-2Y)")
def yield_spread_10y_2y(df: pd.DataFrame) -> pd.Series:
    """国债10年-2年利差，收窄预示经济放缓"""
    if "rate_10y" in df.columns and "rate_2y" in df.columns:
        return _broadcast_monthly(df["rate_10y"]) - _broadcast_monthly(df["rate_2y"])
    return pd.Series(np.nan, index=df.index)


@registry.register("credit_spread", "macro", "信用利差 (企业债-国债)")
def credit_spread(df: pd.DataFrame) -> pd.Series:
    """信用利差扩大=风险偏好下降，利空股市"""
    if "corp_bond_yield" in df.columns and "gov_bond_yield" in df.columns:
        return _broadcast_monthly(df["corp_bond_yield"]) - _broadcast_monthly(df["gov_bond_yield"])
    return pd.Series(np.nan, index=df.index)


@registry.register("social_financing_yoy", "macro", "社融增速")
def social_financing_yoy(df: pd.DataFrame) -> pd.Series:
    """社会融资规模增速，领先经济周期"""
    if "social_financing_yoy" in df.columns:
        return _broadcast_monthly(df["social_financing_yoy"])
    return pd.Series(np.nan, index=df.index)
