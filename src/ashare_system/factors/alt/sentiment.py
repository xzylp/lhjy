"""另类数据因子 — 舆情/搜索热度"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..registry import registry


@registry.register("news_sentiment_score", "alt", "新闻情感得分")
def news_sentiment_score(df: pd.DataFrame) -> pd.Series:
    if "news_sentiment" in df.columns:
        return df["news_sentiment"]
    return pd.Series(np.nan, index=df.index)


@registry.register("search_heat", "alt", "搜索热度指数")
def search_heat(df: pd.DataFrame) -> pd.Series:
    if "search_index" in df.columns:
        return df["search_index"]
    return pd.Series(np.nan, index=df.index)


@registry.register("social_mention_count", "alt", "社交媒体提及次数")
def social_mention_count(df: pd.DataFrame) -> pd.Series:
    if "social_mentions" in df.columns:
        return df["social_mentions"]
    return pd.Series(np.nan, index=df.index)
