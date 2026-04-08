"""产业链因子 — 上下游供需"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..registry import registry


@registry.register("upstream_price_change", "chain", "上游原材料价格变化")
def upstream_price_change(df: pd.DataFrame) -> pd.Series:
    if "upstream_price" in df.columns:
        return df["upstream_price"].pct_change(20)
    return pd.Series(np.nan, index=df.index)


@registry.register("downstream_demand_index", "chain", "下游需求指数")
def downstream_demand_index(df: pd.DataFrame) -> pd.Series:
    if "downstream_demand" in df.columns:
        return df["downstream_demand"]
    return pd.Series(np.nan, index=df.index)
