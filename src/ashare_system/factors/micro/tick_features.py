"""Tick 级特征因子"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..registry import registry


@registry.register("tick_volume_imbalance", "micro", "Tick 成交量不平衡 (买量-卖量)/(买量+卖量)")
def tick_volume_imbalance(df: pd.DataFrame) -> pd.Series:
    """基于 Tick 数据的成交量不平衡因子"""
    if "buy_volume" in df.columns and "sell_volume" in df.columns:
        total = df["buy_volume"] + df["sell_volume"]
        return (df["buy_volume"] - df["sell_volume"]) / total.replace(0, np.nan)
    # 用价格方向代理
    direction = np.sign(df["close"] - df["close"].shift(1))
    return (df["volume"] * direction).rolling(5).sum() / df["volume"].rolling(5).sum().replace(0, np.nan)


@registry.register("tick_trade_count", "micro", "单位时间成交笔数 (活跃度)")
def tick_trade_count(df: pd.DataFrame) -> pd.Series:
    if "trade_count" in df.columns:
        return df["trade_count"]
    # 用成交额/均价代理
    avg_price = (df["high"] + df["low"]) / 2
    return df["amount"] / avg_price.replace(0, np.nan) / 100


@registry.register("tick_avg_trade_size", "micro", "平均每笔成交量")
def tick_avg_trade_size(df: pd.DataFrame) -> pd.Series:
    if "trade_count" in df.columns:
        return df["volume"] / df["trade_count"].replace(0, np.nan)
    return df["volume"].rolling(5).mean()


@registry.register("tick_price_impact", "micro", "价格冲击系数 (Amihud 非流动性)")
def tick_price_impact(df: pd.DataFrame) -> pd.Series:
    """Amihud 非流动性: |收益率| / 成交额"""
    ret = df["close"].pct_change().abs()
    return ret / df["amount"].replace(0, np.nan) * 1e6


@registry.register("tick_price_impact_5d", "micro", "5日平均价格冲击系数")
def tick_price_impact_5d(df: pd.DataFrame) -> pd.Series:
    return tick_price_impact(df).rolling(5).mean()


@registry.register("tick_spread_proxy", "micro", "买卖价差代理 (高低价差/收盘价)")
def tick_spread_proxy(df: pd.DataFrame) -> pd.Series:
    return (df["high"] - df["low"]) / df["close"].replace(0, np.nan)


@registry.register("tick_spread_proxy_5d", "micro", "5日平均买卖价差代理")
def tick_spread_proxy_5d(df: pd.DataFrame) -> pd.Series:
    return tick_spread_proxy(df).rolling(5).mean()


@registry.register("tick_large_order_ratio", "micro", "大单占比 (成交额/均量成交额)")
def tick_large_order_ratio(df: pd.DataFrame) -> pd.Series:
    """大单占比: 当日成交额 / 20日均成交额"""
    avg_amount = df["amount"].rolling(20).mean()
    return df["amount"] / avg_amount.replace(0, np.nan)


@registry.register("tick_momentum_intraday", "micro", "日内动量 (收盘/开盘 - 1)")
def tick_momentum_intraday(df: pd.DataFrame) -> pd.Series:
    return df["close"] / df["open"].replace(0, np.nan) - 1


@registry.register("tick_overnight_gap", "micro", "隔夜跳空 (开盘/昨收 - 1)")
def tick_overnight_gap(df: pd.DataFrame) -> pd.Series:
    return df["open"] / df["close"].shift(1).replace(0, np.nan) - 1


# ── 微观结构因子 (市场验证) ──────────────────────────────

@registry.register("kyle_lambda", "micro", "Kyle's Lambda (价格冲击)")
def kyle_lambda(df: pd.DataFrame) -> pd.Series:
    """价格冲击 = |收益率| / sqrt(成交量)，衡量流动性"""
    ret = df["close"].pct_change().abs()
    vol_sqrt = np.sqrt(df["volume"].replace(0, np.nan))
    return ret / vol_sqrt


@registry.register("kyle_lambda_20d", "micro", "Kyle's Lambda 20日均值")
def kyle_lambda_20d(df: pd.DataFrame) -> pd.Series:
    return kyle_lambda(df).rolling(20).mean()


@registry.register("large_order_flow_proxy", "micro", "大单净流入代理")
def large_order_flow_proxy(df: pd.DataFrame) -> pd.Series:
    """用量价关系代理大单: 放量上涨=大单买入，放量下跌=大单卖出"""
    direction = np.sign(df["close"] - df["open"])
    vol_ratio = df["volume"] / df["volume"].rolling(20).mean().replace(0, np.nan)
    # 放量(>1.5倍均量)时方向有意义
    flow = direction * (vol_ratio - 1).clip(lower=0)
    return flow.rolling(5).sum()
