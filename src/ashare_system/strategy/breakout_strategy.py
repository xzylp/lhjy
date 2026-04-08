"""放量突破策略 — 量价齐升突破，适合主升浪初期"""

from __future__ import annotations

import pandas as pd

from ..contracts import Signal, MarketProfile
from ..logging_config import get_logger

logger = get_logger("strategy.breakout")


class BreakoutStrategy:
    """放量突破策略: 捕捉量价齐升的突破行情"""

    name = "breakout"
    weight = 0.35

    def score(self, symbol: str, df: pd.DataFrame, profile: MarketProfile | None = None) -> float:
        """评分 [0, 100]"""
        if df.empty or len(df) < 20:
            return 0.0
        last = df.iloc[-1]
        close = df["close"]
        vol = df["volume"]
        s = 0.0
        count = 0

        # 量比 > 2 (显著放量)
        avg_vol = float(vol.iloc[-20:].mean()) if len(df) >= 20 else float(vol.mean())
        cur_vol = float(vol.iloc[-1])
        vol_ratio = cur_vol / max(avg_vol, 1)
        if vol_ratio > 1.5:
            s += min(vol_ratio * 25, 100)  # 2倍量=50分, 4倍量=100分
        count += 1

        # 创20日新高
        if len(df) >= 20:
            hi_20 = float(df["high"].iloc[-20:].max())
            if float(close.iloc[-1]) >= hi_20 * 0.99:
                s += 90
        count += 1

        # MACD金叉 (DIF > DEA 且 DIF 上穿)
        if "macd_dif" in df.columns and "macd_dea" in df.columns:
            dif = float(last.get("macd_dif", 0))
            dea = float(last.get("macd_dea", 0))
            if dif > dea and dif > 0:
                s += 80
            elif dif > dea:
                s += 50
        count += 1

        # 站上MA20
        if len(df) >= 20:
            ma20 = float(close.rolling(20).mean().iloc[-1])
            if float(close.iloc[-1]) > ma20:
                s += 70
        count += 1

        # MA20 向上 (趋势确认)
        if len(df) >= 25:
            ma20_now = float(close.rolling(20).mean().iloc[-1])
            ma20_5ago = float(close.rolling(20).mean().iloc[-5])
            if ma20_now > ma20_5ago:
                s += 60
        count += 1

        return s / max(count, 1)

    def signal(self, symbol: str, df: pd.DataFrame, profile: MarketProfile | None = None) -> Signal:
        sc = self.score(symbol, df, profile)

        # 高潮期不追突破 (容易接盘)
        if profile and profile.sentiment_phase == "高潮":
            return Signal(symbol=symbol, action="HOLD", strength=0, confidence=0, source_strategy=self.name)

        if sc > 70:
            return Signal(symbol=symbol, action="BUY", strength=sc, confidence=sc / 100, source_strategy=self.name)

        # 跌破MA20 + 缩量 = 突破失败
        if len(df) >= 20:
            close = float(df["close"].iloc[-1])
            ma20 = float(df["close"].rolling(20).mean().iloc[-1])
            if close < ma20 * 0.97:
                return Signal(symbol=symbol, action="SELL", strength=30, confidence=0.7, source_strategy=self.name)

        return Signal(symbol=symbol, action="HOLD", strength=sc, confidence=0.5, source_strategy=self.name)
