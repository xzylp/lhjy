"""技术指标因子 (~50个) — MA/MACD/RSI/ATR/KDJ/BOLL/OBV/VWAP"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..registry import registry


# ── 均线系列 ──────────────────────────────────────────

@registry.register("ma5", "technical", "5日移动均线")
def ma5(df: pd.DataFrame) -> pd.Series:
    return df["close"].rolling(5).mean()


@registry.register("ma10", "technical", "10日移动均线")
def ma10(df: pd.DataFrame) -> pd.Series:
    return df["close"].rolling(10).mean()


@registry.register("ma20", "technical", "20日移动均线")
def ma20(df: pd.DataFrame) -> pd.Series:
    return df["close"].rolling(20).mean()


@registry.register("ma60", "technical", "60日移动均线")
def ma60(df: pd.DataFrame) -> pd.Series:
    return df["close"].rolling(60).mean()


@registry.register("ma5_slope", "technical", "MA5斜率")
def ma5_slope(df: pd.DataFrame) -> pd.Series:
    m = ma5(df)
    return m - m.shift(1)


@registry.register("price_above_ma20", "technical", "收盘价/MA20 - 1")
def price_above_ma20(df: pd.DataFrame) -> pd.Series:
    return df["close"] / ma20(df).replace(0, np.nan) - 1


@registry.register("price_above_ma60", "technical", "收盘价/MA60 - 1")
def price_above_ma60(df: pd.DataFrame) -> pd.Series:
    return df["close"] / ma60(df).replace(0, np.nan) - 1


@registry.register("ma5_above_ma20", "technical", "MA5/MA20 - 1 (金叉/死叉)")
def ma5_above_ma20(df: pd.DataFrame) -> pd.Series:
    return ma5(df) / ma20(df).replace(0, np.nan) - 1


# ── MACD ──────────────────────────────────────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


@registry.register("macd_dif", "technical", "MACD DIF = EMA12 - EMA26")
def macd_dif(df: pd.DataFrame) -> pd.Series:
    return _ema(df["close"], 12) - _ema(df["close"], 26)


@registry.register("macd_dea", "technical", "MACD DEA = EMA9(DIF)")
def macd_dea(df: pd.DataFrame) -> pd.Series:
    return _ema(macd_dif(df), 9)


@registry.register("macd_bar", "technical", "MACD 柱 = 2*(DIF-DEA)")
def macd_bar(df: pd.DataFrame) -> pd.Series:
    return 2 * (macd_dif(df) - macd_dea(df))


@registry.register("macd_cross", "technical", "MACD 金叉/死叉信号")
def macd_cross(df: pd.DataFrame) -> pd.Series:
    dif = macd_dif(df)
    dea = macd_dea(df)
    return np.sign(dif - dea)


# ── RSI (Wilder's) ────────────────────────────────────

def _wilder_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


@registry.register("rsi6", "technical", "RSI 6日 (Wilder's)")
def rsi6(df: pd.DataFrame) -> pd.Series:
    return _wilder_rsi(df["close"], 6)


@registry.register("rsi14", "technical", "RSI 14日 (Wilder's)")
def rsi14(df: pd.DataFrame) -> pd.Series:
    return _wilder_rsi(df["close"], 14)


@registry.register("rsi_overbought", "technical", "RSI14 超买信号 (>70)")
def rsi_overbought(df: pd.DataFrame) -> pd.Series:
    return (rsi14(df) > 70).astype(float)


@registry.register("rsi_oversold", "technical", "RSI14 超卖信号 (<30)")
def rsi_oversold(df: pd.DataFrame) -> pd.Series:
    return (rsi14(df) < 30).astype(float)


# ── ATR ───────────────────────────────────────────────

def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


@registry.register("atr14", "technical", "ATR 14日 (Wilder's)")
def atr14(df: pd.DataFrame) -> pd.Series:
    tr = _true_range(df)
    return tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()


@registry.register("atr_ratio", "technical", "ATR/收盘价 (相对波动率)")
def atr_ratio(df: pd.DataFrame) -> pd.Series:
    return atr14(df) / df["close"].replace(0, np.nan)


# ── 布林带 ────────────────────────────────────────────

@registry.register("boll_upper", "technical", "布林带上轨")
def boll_upper(df: pd.DataFrame) -> pd.Series:
    mid = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()
    return mid + 2 * std


@registry.register("boll_lower", "technical", "布林带下轨")
def boll_lower(df: pd.DataFrame) -> pd.Series:
    mid = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()
    return mid - 2 * std


@registry.register("boll_width", "technical", "布林带宽度 (波动率)")
def boll_width(df: pd.DataFrame) -> pd.Series:
    mid = df["close"].rolling(20).mean()
    return (boll_upper(df) - boll_lower(df)) / mid.replace(0, np.nan)


@registry.register("boll_position", "technical", "价格在布林带的位置 [0,1]")
def boll_position(df: pd.DataFrame) -> pd.Series:
    lo = boll_lower(df)
    hi = boll_upper(df)
    return (df["close"] - lo) / (hi - lo).replace(0, np.nan)


# ── KDJ ───────────────────────────────────────────────

def _kdj(df: pd.DataFrame, n: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    lo_n = df["low"].rolling(n).min()
    hi_n = df["high"].rolling(n).max()
    rsv = (df["close"] - lo_n) / (hi_n - lo_n).replace(0, np.nan) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


@registry.register("kdj_k", "technical", "KDJ K值")
def kdj_k(df: pd.DataFrame) -> pd.Series:
    k, _, _ = _kdj(df)
    return k


@registry.register("kdj_d", "technical", "KDJ D值")
def kdj_d(df: pd.DataFrame) -> pd.Series:
    _, d, _ = _kdj(df)
    return d


@registry.register("kdj_j", "technical", "KDJ J值")
def kdj_j(df: pd.DataFrame) -> pd.Series:
    _, _, j = _kdj(df)
    return j


@registry.register("kdj_cross", "technical", "KDJ 金叉/死叉")
def kdj_cross(df: pd.DataFrame) -> pd.Series:
    k, d, _ = _kdj(df)
    return np.sign(k - d)


# ── OBV ───────────────────────────────────────────────

@registry.register("obv", "technical", "OBV 能量潮")
def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff())
    return (df["volume"] * direction).cumsum()


@registry.register("obv_ma10", "technical", "OBV 10日均线")
def obv_ma10(df: pd.DataFrame) -> pd.Series:
    return obv(df).rolling(10).mean()


@registry.register("obv_diverge", "technical", "OBV与价格背离")
def obv_diverge(df: pd.DataFrame) -> pd.Series:
    price_rank = df["close"].rolling(20).apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1])
    obv_rank = obv(df).rolling(20).apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1])
    return obv_rank - price_rank


# ── VWAP ──────────────────────────────────────────────

@registry.register("vwap", "technical", "VWAP 成交量加权均价")
def vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    return (typical * df["volume"]).rolling(20).sum() / df["volume"].rolling(20).sum().replace(0, np.nan)


@registry.register("price_to_vwap", "technical", "收盘价/VWAP - 1")
def price_to_vwap(df: pd.DataFrame) -> pd.Series:
    return df["close"] / vwap(df).replace(0, np.nan) - 1


# ── 补充技术指标 (经典市场验证) ───────────────────────────

@registry.register("ema_cross_12_26", "technical", "EMA12/EMA26交叉信号")
def ema_cross_12_26(df: pd.DataFrame) -> pd.Series:
    """EMA12 > EMA26 为正，金叉/死叉信号"""
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    return ema12 / ema26.replace(0, np.nan) - 1


@registry.register("williams_r_14", "technical", "Williams %R (14日)")
def williams_r_14(df: pd.DataFrame) -> pd.Series:
    """Williams %R: 0~-100, <-80超卖, >-20超买"""
    hh = df["high"].rolling(14).max()
    ll = df["low"].rolling(14).min()
    denom = hh - ll
    return -100 * (hh - df["close"]) / denom.replace(0, np.nan)


@registry.register("cci_20", "technical", "CCI (20日)")
def cci_20(df: pd.DataFrame) -> pd.Series:
    """CCI: >100超买, <-100超卖"""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sma = tp.rolling(20).mean()
    mad = tp.rolling(20).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - sma) / (0.015 * mad.replace(0, np.nan))


@registry.register("ichimoku_tenkan", "technical", "一目均衡表 转换线 (9日)")
def ichimoku_tenkan(df: pd.DataFrame) -> pd.Series:
    """转换线 = (9日最高 + 9日最低) / 2"""
    return (df["high"].rolling(9).max() + df["low"].rolling(9).min()) / 2


@registry.register("ichimoku_kijun", "technical", "一目均衡表 基准线 (26日)")
def ichimoku_kijun(df: pd.DataFrame) -> pd.Series:
    """基准线 = (26日最高 + 26日最低) / 2"""
    return (df["high"].rolling(26).max() + df["low"].rolling(26).min()) / 2


@registry.register("ichimoku_signal", "technical", "一目均衡表 多空信号")
def ichimoku_signal(df: pd.DataFrame) -> pd.Series:
    """转换线/基准线 - 1，正值看多"""
    tenkan = ichimoku_tenkan(df)
    kijun = ichimoku_kijun(df)
    return tenkan / kijun.replace(0, np.nan) - 1
