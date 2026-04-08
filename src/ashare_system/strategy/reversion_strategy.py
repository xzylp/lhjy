"""均值回归策略 — 超跌反弹，适合回暖/主升期"""

from __future__ import annotations

import pandas as pd

from ..contracts import Signal, MarketProfile
from ..logging_config import get_logger

logger = get_logger("strategy.reversion")


class ReversionStrategy:
    """均值回归策略: 捕捉超跌反弹机会"""

    name = "reversion"
    weight = 0.30

    def score(self, symbol: str, df: pd.DataFrame, profile: MarketProfile | None = None) -> float:
        """评分 [0, 100]"""
        if df.empty or len(df) < 20:
            return 0.0

        # 冰点期均值回归失效，直接0分
        if profile and profile.sentiment_phase == "冰点":
            return 0.0

        last = df.iloc[-1]
        s = 0.0
        count = 0

        # 布林带位置 < 0.2 (接近下轨)
        if "boll_position" in df.columns:
            bp = float(last.get("boll_position", 0.5))
            if bp < 0.3:
                s += (0.3 - bp) / 0.3 * 100  # 越低分越高
            count += 1

        # RSI < 30 (超卖)
        if "rsi14" in df.columns:
            rsi = float(last.get("rsi14", 50))
            if rsi < 35:
                s += (35 - rsi) / 35 * 100
            count += 1

        # 量价背离 (价跌量缩 = 抛压衰竭)
        close = df["close"]
        vol = df["volume"]
        if len(df) >= 10:
            price_chg = float(close.iloc[-1] / close.iloc[-5] - 1)
            vol_chg = float(vol.iloc[-5:].mean() / vol.iloc[-20:-5].mean()) if len(df) >= 20 else 1.0
            if price_chg < -0.05 and vol_chg < 0.7:  # 价跌5%+量缩30%
                s += 80
            count += 1

        # 价格位置 (20日区间低位)
        if len(df) >= 20:
            hi = float(df["high"].iloc[-20:].max())
            lo = float(df["low"].iloc[-20:].min())
            pos = (float(close.iloc[-1]) - lo) / max(hi - lo, 1e-9)
            if pos < 0.2:
                s += (0.2 - pos) / 0.2 * 100
            count += 1

        # 距MA20偏离度 (负偏离越大越超跌)
        if len(df) >= 20:
            ma20 = float(close.rolling(20).mean().iloc[-1])
            deviation = float(close.iloc[-1]) / max(ma20, 1e-9) - 1
            if deviation < -0.08:
                s += min(abs(deviation) * 500, 100)
            count += 1

        return s / max(count, 1)

    def signal(self, symbol: str, df: pd.DataFrame, profile: MarketProfile | None = None) -> Signal:
        sc = self.score(symbol, df, profile)

        if sc > 65:
            return Signal(symbol=symbol, action="BUY", strength=sc, confidence=sc / 100, source_strategy=self.name)

        # 反弹到MA20上方则卖出
        if len(df) >= 20:
            close = float(df["close"].iloc[-1])
            ma20 = float(df["close"].rolling(20).mean().iloc[-1])
            if close > ma20 * 1.02:
                return Signal(symbol=symbol, action="SELL", strength=40, confidence=0.6, source_strategy=self.name)

        return Signal(symbol=symbol, action="HOLD", strength=sc, confidence=0.5, source_strategy=self.name)
