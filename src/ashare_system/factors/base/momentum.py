"""动量反转因子 (~30个) — N日涨幅/相对强度/反转"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..registry import registry


# ── 动量因子 ──────────────────────────────────────────

@registry.register("return_1d", "momentum", "1日收益率")
def return_1d(df: pd.DataFrame) -> pd.Series:
    return df["close"].pct_change(1)


@registry.register("return_5d", "momentum", "5日收益率")
def return_5d(df: pd.DataFrame) -> pd.Series:
    return df["close"].pct_change(5)


@registry.register("return_10d", "momentum", "10日收益率")
def return_10d(df: pd.DataFrame) -> pd.Series:
    return df["close"].pct_change(10)


@registry.register("return_20d", "momentum", "20日收益率")
def return_20d(df: pd.DataFrame) -> pd.Series:
    return df["close"].pct_change(20)


@registry.register("return_60d", "momentum", "60日收益率")
def return_60d(df: pd.DataFrame) -> pd.Series:
    return df["close"].pct_change(60)


@registry.register("return_120d", "momentum", "120日收益率")
def return_120d(df: pd.DataFrame) -> pd.Series:
    return df["close"].pct_change(120)


# ── 动量加速度 ────────────────────────────────────────

@registry.register("momentum_accel_5_20", "momentum", "动量加速度: 5日收益 - 20日收益")
def momentum_accel_5_20(df: pd.DataFrame) -> pd.Series:
    return return_5d(df) - return_20d(df)


@registry.register("momentum_accel_20_60", "momentum", "动量加速度: 20日收益 - 60日收益")
def momentum_accel_20_60(df: pd.DataFrame) -> pd.Series:
    return return_20d(df) - return_60d(df)


# ── 反转因子 ──────────────────────────────────────────

@registry.register("reversal_1d", "momentum", "1日反转 (负动量)")
def reversal_1d(df: pd.DataFrame) -> pd.Series:
    return -return_1d(df)


@registry.register("reversal_5d", "momentum", "5日反转")
def reversal_5d(df: pd.DataFrame) -> pd.Series:
    return -return_5d(df)


# ── 相对强度 ──────────────────────────────────────────

@registry.register("return_std_20d", "momentum", "20日收益率标准差 (波动率)")
def return_std_20d(df: pd.DataFrame) -> pd.Series:
    return df["close"].pct_change().rolling(20).std()


@registry.register("return_std_60d", "momentum", "60日收益率标准差")
def return_std_60d(df: pd.DataFrame) -> pd.Series:
    return df["close"].pct_change().rolling(60).std()


@registry.register("sharpe_20d", "momentum", "20日夏普比率 (简化)")
def sharpe_20d(df: pd.DataFrame) -> pd.Series:
    r = df["close"].pct_change()
    return r.rolling(20).mean() / r.rolling(20).std().replace(0, np.nan)


@registry.register("sharpe_60d", "momentum", "60日夏普比率 (简化)")
def sharpe_60d(df: pd.DataFrame) -> pd.Series:
    r = df["close"].pct_change()
    return r.rolling(60).mean() / r.rolling(60).std().replace(0, np.nan)


# ── 最大回撤 ──────────────────────────────────────────

@registry.register("max_drawdown_20d", "momentum", "20日最大回撤")
def max_drawdown_20d(df: pd.DataFrame) -> pd.Series:
    def _mdd(x: pd.Series) -> float:
        peak = x.cummax()
        dd = (x - peak) / peak.replace(0, np.nan)
        return float(dd.min()) if len(dd) > 0 else 0.0
    return df["close"].rolling(20).apply(_mdd, raw=False)


# ── 连涨连跌 ──────────────────────────────────────────

@registry.register("up_days_5d", "momentum", "5日内上涨天数")
def up_days_5d(df: pd.DataFrame) -> pd.Series:
    return (df["close"].pct_change() > 0).rolling(5).sum()


@registry.register("down_days_5d", "momentum", "5日内下跌天数")
def down_days_5d(df: pd.DataFrame) -> pd.Series:
    return (df["close"].pct_change() < 0).rolling(5).sum()


@registry.register("consecutive_up", "momentum", "连续上涨天数")
def consecutive_up(df: pd.DataFrame) -> pd.Series:
    up = (df["close"].pct_change() > 0).astype(int)
    result = []
    count = 0
    for v in up:
        count = count + 1 if v else 0
        result.append(count)
    return pd.Series(result, index=df.index)


@registry.register("consecutive_down", "momentum", "连续下跌天数")
def consecutive_down(df: pd.DataFrame) -> pd.Series:
    down = (df["close"].pct_change() < 0).astype(int)
    result = []
    count = 0
    for v in down:
        count = count + 1 if v else 0
        result.append(count)
    return pd.Series(result, index=df.index)


# ── 高低点突破 ────────────────────────────────────────

@registry.register("new_high_20d", "momentum", "创20日新高信号")
def new_high_20d(df: pd.DataFrame) -> pd.Series:
    return (df["close"] >= df["high"].rolling(20).max().shift(1)).astype(float)


@registry.register("new_low_20d", "momentum", "创20日新低信号")
def new_low_20d(df: pd.DataFrame) -> pd.Series:
    return (df["close"] <= df["low"].rolling(20).min().shift(1)).astype(float)


@registry.register("distance_to_52w_high", "momentum", "距52周最高价的距离")
def distance_to_52w_high(df: pd.DataFrame) -> pd.Series:
    hi = df["high"].rolling(252).max()
    return (df["close"] - hi) / hi.replace(0, np.nan)


# ── 高级动量因子 (A股市场验证) ────────────────────────────

@registry.register("industry_relative_momentum", "momentum", "行业相对动量 (20日)")
def industry_relative_momentum(df: pd.DataFrame) -> pd.Series:
    """个股20日收益 - 行业平均20日收益，需要 industry_mean_return_20d 列"""
    ret = df["close"].pct_change(20)
    if "industry_mean_return_20d" in df.columns:
        return ret - df["industry_mean_return_20d"]
    return ret  # 无行业数据时退化为绝对动量


@registry.register("beta_adjusted_momentum", "momentum", "Beta调整动量 (20日)")
def beta_adjusted_momentum(df: pd.DataFrame) -> pd.Series:
    """个股收益 - beta * 市场收益，剥离系统性风险后的超额动量"""
    ret = df["close"].pct_change(20)
    if "market_return_20d" not in df.columns:
        return ret
    mkt = df["market_return_20d"]
    # 滚动60日计算beta
    stock_ret = df["close"].pct_change()
    mkt_ret = df["market_return_20d"].diff() if mkt.std() > 0 else stock_ret * 0
    cov = stock_ret.rolling(60).cov(mkt_ret)
    var = mkt_ret.rolling(60).var()
    beta = cov / var.replace(0, np.nan)
    beta = beta.clip(-3, 3).fillna(1.0)
    return ret - beta * mkt


@registry.register("momentum_quality", "momentum", "动量质量 (收益/波动率)")
def momentum_quality(df: pd.DataFrame) -> pd.Series:
    """高质量动量: 收益率高且波动率低的股票，比纯动量更稳健"""
    ret_20 = df["close"].pct_change(20)
    vol_20 = df["close"].pct_change().rolling(20).std()
    return ret_20 / vol_20.replace(0, np.nan)
