"""量价因子 (~40个) — 换手率/量比/量价背离/资金流向"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..registry import registry


# ── 换手率相关 ──────────────────────────────────────────

@registry.register("turnover_rate", "price_volume", "换手率 = 成交量/流通股本")
def turnover_rate(df: pd.DataFrame) -> pd.Series:
    if "float_shares" not in df.columns or df["float_shares"].eq(0).all():
        return pd.Series(np.nan, index=df.index)
    return df["volume"] / df["float_shares"]


@registry.register("turnover_5d_avg", "price_volume", "5日平均换手率")
def turnover_5d_avg(df: pd.DataFrame) -> pd.Series:
    tr = turnover_rate(df)
    return tr.rolling(5).mean()


@registry.register("turnover_20d_avg", "price_volume", "20日平均换手率")
def turnover_20d_avg(df: pd.DataFrame) -> pd.Series:
    tr = turnover_rate(df)
    return tr.rolling(20).mean()


@registry.register("turnover_ratio_5_20", "price_volume", "5日/20日换手率比值")
def turnover_ratio_5_20(df: pd.DataFrame) -> pd.Series:
    t5 = turnover_5d_avg(df)
    t20 = turnover_20d_avg(df)
    return t5 / t20.replace(0, np.nan)


# ── 量比相关 ──────────────────────────────────────────

@registry.register("volume_ratio", "price_volume", "量比 = 当日成交量/5日均量")
def volume_ratio(df: pd.DataFrame) -> pd.Series:
    avg5 = df["volume"].rolling(5).mean().shift(1)
    return df["volume"] / avg5.replace(0, np.nan)


@registry.register("volume_ratio_10d", "price_volume", "量比 = 当日成交量/10日均量")
def volume_ratio_10d(df: pd.DataFrame) -> pd.Series:
    avg10 = df["volume"].rolling(10).mean().shift(1)
    return df["volume"] / avg10.replace(0, np.nan)


@registry.register("volume_ma5", "price_volume", "5日成交量均值")
def volume_ma5(df: pd.DataFrame) -> pd.Series:
    return df["volume"].rolling(5).mean()


@registry.register("volume_ma20", "price_volume", "20日成交量均值")
def volume_ma20(df: pd.DataFrame) -> pd.Series:
    return df["volume"].rolling(20).mean()


@registry.register("volume_std20", "price_volume", "20日成交量标准差")
def volume_std20(df: pd.DataFrame) -> pd.Series:
    return df["volume"].rolling(20).std()


# ── 量价背离 ──────────────────────────────────────────

@registry.register("price_volume_corr10", "price_volume", "10日量价相关系数")
def price_volume_corr10(df: pd.DataFrame) -> pd.Series:
    return df["close"].rolling(10).corr(df["volume"])


@registry.register("price_volume_corr20", "price_volume", "20日量价相关系数")
def price_volume_corr20(df: pd.DataFrame) -> pd.Series:
    return df["close"].rolling(20).corr(df["volume"])


@registry.register("volume_price_diverge", "price_volume", "量价背离: 价涨量缩为负")
def volume_price_diverge(df: pd.DataFrame) -> pd.Series:
    price_chg = df["close"].pct_change()
    vol_chg = df["volume"].pct_change()
    return price_chg * vol_chg


# ── 资金流向 ──────────────────────────────────────────

@registry.register("money_flow", "price_volume", "资金流向 = 成交额 * 涨跌方向")
def money_flow(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"] - df["close"].shift(1))
    return df["amount"] * direction


@registry.register("money_flow_5d", "price_volume", "5日累计资金流向")
def money_flow_5d(df: pd.DataFrame) -> pd.Series:
    return money_flow(df).rolling(5).sum()


@registry.register("money_flow_10d", "price_volume", "10日累计资金流向")
def money_flow_10d(df: pd.DataFrame) -> pd.Series:
    return money_flow(df).rolling(10).sum()


@registry.register("amount_ma5", "price_volume", "5日成交额均值")
def amount_ma5(df: pd.DataFrame) -> pd.Series:
    return df["amount"].rolling(5).mean()


@registry.register("amount_ma20", "price_volume", "20日成交额均值")
def amount_ma20(df: pd.DataFrame) -> pd.Series:
    return df["amount"].rolling(20).mean()


@registry.register("amount_ratio_5_20", "price_volume", "5日/20日成交额比值")
def amount_ratio_5_20(df: pd.DataFrame) -> pd.Series:
    return amount_ma5(df) / amount_ma20(df).replace(0, np.nan)


# ── 价格振幅 ──────────────────────────────────────────

@registry.register("amplitude", "price_volume", "振幅 = (最高-最低)/昨收")
def amplitude(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return (df["high"] - df["low"]) / prev_close.replace(0, np.nan)


@registry.register("amplitude_5d_avg", "price_volume", "5日平均振幅")
def amplitude_5d_avg(df: pd.DataFrame) -> pd.Series:
    return amplitude(df).rolling(5).mean()


@registry.register("upper_shadow", "price_volume", "上影线比例")
def upper_shadow(df: pd.DataFrame) -> pd.Series:
    body_top = df[["open", "close"]].max(axis=1)
    return (df["high"] - body_top) / df["close"].replace(0, np.nan)


@registry.register("lower_shadow", "price_volume", "下影线比例")
def lower_shadow(df: pd.DataFrame) -> pd.Series:
    body_bottom = df[["open", "close"]].min(axis=1)
    return (body_bottom - df["low"]) / df["close"].replace(0, np.nan)


# ── 价格位置 ──────────────────────────────────────────

@registry.register("price_position_20d", "price_volume", "价格在20日区间的位置 [0,1]")
def price_position_20d(df: pd.DataFrame) -> pd.Series:
    lo = df["low"].rolling(20).min()
    hi = df["high"].rolling(20).max()
    return (df["close"] - lo) / (hi - lo).replace(0, np.nan)


@registry.register("price_position_60d", "price_volume", "价格在60日区间的位置 [0,1]")
def price_position_60d(df: pd.DataFrame) -> pd.Series:
    lo = df["low"].rolling(60).min()
    hi = df["high"].rolling(60).max()
    return (df["close"] - lo) / (hi - lo).replace(0, np.nan)


@registry.register("close_to_high_20d", "price_volume", "收盘价距20日最高价的距离")
def close_to_high_20d(df: pd.DataFrame) -> pd.Series:
    hi = df["high"].rolling(20).max()
    return (df["close"] - hi) / hi.replace(0, np.nan)


@registry.register("close_to_low_20d", "price_volume", "收盘价距20日最低价的距离")
def close_to_low_20d(df: pd.DataFrame) -> pd.Series:
    lo = df["low"].rolling(20).min()
    return (df["close"] - lo) / lo.replace(0, np.nan)
