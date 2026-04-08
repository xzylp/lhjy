"""行为金融因子 — 羊群效应 + CSAD"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..registry import registry


@registry.register("csad", "behavior", "CSAD 截面绝对偏差 (羊群效应)")
def csad(df: pd.DataFrame) -> pd.Series:
    """CSAD = |个股收益 - 市场收益| 的滚动均值，越低羊群效应越强"""
    ret = df["close"].pct_change()
    if "market_return" in df.columns:
        mkt = df["market_return"]
    else:
        mkt = ret.rolling(20).mean()  # 无市场数据时用自身均值代理
    return (ret - mkt).abs().rolling(20).mean()


@registry.register("herd_corr_market", "behavior", "与市场的相关性 (羊群代理)")
def herd_corr_market(df: pd.DataFrame) -> pd.Series:
    """与市场收益的滚动相关性，越高越跟随大盘"""
    ret = df["close"].pct_change()
    if "market_return" in df.columns:
        return ret.rolling(20).corr(df["market_return"])
    return pd.Series(np.nan, index=df.index)


@registry.register("contrarian_signal", "behavior", "反转信号 (超跌+缩量)")
def contrarian_signal(df: pd.DataFrame) -> pd.Series:
    """超跌反转: 20日跌幅>15% 且 近5日量缩至20日均量50%以下"""
    ret_20 = df["close"].pct_change(20)
    vol_ratio = df["volume"].rolling(5).mean() / df["volume"].rolling(20).mean().replace(0, np.nan)
    # 超跌 + 缩量 = 恐慌释放完毕，反转概率高
    signal = ((ret_20 < -0.15) & (vol_ratio < 0.5)).astype(float)
    return signal


@registry.register("overreaction_reversal", "behavior", "过度反应反转 (2σ偏离)")
def overreaction_reversal(df: pd.DataFrame) -> pd.Series:
    """5日收益超过60日2倍标准差时，取反作为反转信号"""
    ret_5 = df["close"].pct_change(5)
    std_60 = df["close"].pct_change().rolling(60).std()
    threshold = 2 * std_60
    # 超涨→看空，超跌→看多
    signal = pd.Series(0.0, index=df.index)
    signal[ret_5 > threshold] = -1.0
    signal[ret_5 < -threshold] = 1.0
    return signal
