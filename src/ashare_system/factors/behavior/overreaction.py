"""行为金融因子 — 过度反应"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..registry import registry


@registry.register("overreaction_5d", "behavior", "5日过度反应: 大涨后反转")
def overreaction_5d(df: pd.DataFrame) -> pd.Series:
    """大涨后短期反转信号"""
    r5 = df["close"].pct_change(5)
    return -r5.where(r5.abs() > r5.rolling(60).std() * 2, 0)


@registry.register("overreaction_20d", "behavior", "20日过度反应")
def overreaction_20d(df: pd.DataFrame) -> pd.Series:
    r20 = df["close"].pct_change(20)
    return -r20.where(r20.abs() > r20.rolling(120).std() * 2, 0)
