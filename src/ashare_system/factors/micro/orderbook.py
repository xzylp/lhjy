"""微观结构因子 — 盘口深度/委比"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..registry import registry


@registry.register("bid_ask_spread", "micro", "买卖价差 (ask-bid)/mid")
def bid_ask_spread(df: pd.DataFrame) -> pd.Series:
    if "ask_price" in df.columns and "bid_price" in df.columns:
        mid = (df["ask_price"] + df["bid_price"]) / 2
        return (df["ask_price"] - df["bid_price"]) / mid.replace(0, np.nan)
    return pd.Series(np.nan, index=df.index)


@registry.register("order_imbalance", "micro", "委比 = (买量-卖量)/(买量+卖量)")
def order_imbalance(df: pd.DataFrame) -> pd.Series:
    if "bid_volume" in df.columns and "ask_volume" in df.columns:
        total = df["bid_volume"] + df["ask_volume"]
        return (df["bid_volume"] - df["ask_volume"]) / total.replace(0, np.nan)
    return pd.Series(np.nan, index=df.index)


@registry.register("tick_direction", "micro", "Tick方向 (uptick/downtick)")
def tick_direction(df: pd.DataFrame) -> pd.Series:
    return np.sign(df["close"].diff())


@registry.register("tick_direction_5d", "micro", "5日Tick方向累计")
def tick_direction_5d(df: pd.DataFrame) -> pd.Series:
    return tick_direction(df).rolling(5).sum()
