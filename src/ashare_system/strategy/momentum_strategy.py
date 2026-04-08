"""动量策略 — A股趋势跟踪，适合主升浪行情"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..contracts import Signal, MarketProfile
from ..logging_config import get_logger

logger = get_logger("strategy.momentum")


class MomentumStrategy:
    """动量策略: 追踪强势股，顺势而为"""

    name = "momentum"
    weight = 0.35

    # 因子权重
    WEIGHTS = {
        "return_20d": 0.25,
        "return_60d": 0.15,
        "sharpe_20d": 0.20,
        "volume_ratio_5d": 0.15,
        "rsi14": 0.15,
        "price_above_ma20": 0.10,
    }

    def score(self, symbol: str, df: pd.DataFrame, profile: MarketProfile | None = None) -> float:
        """评分 [0, 100]"""
        if df.empty or len(df) < 60:
            return 0.0
        last = df.iloc[-1]
        s = 0.0
        total_w = 0.0

        # 20日收益率 → 归一化到 [0,100]
        if "return_20d" in df.columns:
            r20 = float(last.get("return_20d", 0))
            s += self.WEIGHTS["return_20d"] * self._norm_return(r20)
            total_w += self.WEIGHTS["return_20d"]

        # 60日收益率
        if "return_60d" in df.columns:
            r60 = float(last.get("return_60d", 0))
            s += self.WEIGHTS["return_60d"] * self._norm_return(r60)
            total_w += self.WEIGHTS["return_60d"]

        # 20日夏普
        if "sharpe_20d" in df.columns:
            sharpe = float(last.get("sharpe_20d", 0))
            s += self.WEIGHTS["sharpe_20d"] * min(max(sharpe * 20 + 50, 0), 100)
            total_w += self.WEIGHTS["sharpe_20d"]

        # 量比
        if "volume_ratio_5d" in df.columns:
            vr = float(last.get("volume_ratio_5d", 1))
            s += self.WEIGHTS["volume_ratio_5d"] * min(vr * 30, 100)
            total_w += self.WEIGHTS["volume_ratio_5d"]

        # RSI: 50-70区间最佳 (强势但未超买)
        rsi = self._get_rsi(df)
        if rsi is not None:
            rsi_score = 100 - abs(rsi - 60) * 2.5  # 60分最优
            s += self.WEIGHTS["rsi14"] * max(rsi_score, 0)
            total_w += self.WEIGHTS["rsi14"]

        # 站上MA20
        close = float(df["close"].iloc[-1])
        ma20 = float(df["close"].rolling(20).mean().iloc[-1]) if len(df) >= 20 else close
        if close > ma20:
            s += self.WEIGHTS["price_above_ma20"] * 80
            total_w += self.WEIGHTS["price_above_ma20"]

        return s / max(total_w, 1e-9)

    def signal(self, symbol: str, df: pd.DataFrame, profile: MarketProfile | None = None) -> Signal:
        sc = self.score(symbol, df, profile)
        rsi = self._get_rsi(df)

        # 冰点期不做动量
        if profile and profile.sentiment_phase == "冰点":
            return Signal(symbol=symbol, action="HOLD", strength=0, confidence=0, source_strategy=self.name)

        if sc > 70 and (rsi is None or rsi < 80):
            return Signal(symbol=symbol, action="BUY", strength=sc, confidence=sc / 100, source_strategy=self.name)
        if sc < 30 or (rsi is not None and rsi > 85):
            return Signal(symbol=symbol, action="SELL", strength=sc, confidence=0.7, source_strategy=self.name)
        return Signal(symbol=symbol, action="HOLD", strength=sc, confidence=0.5, source_strategy=self.name)

    def _get_rsi(self, df: pd.DataFrame) -> float | None:
        if "rsi14" in df.columns:
            v = df["rsi14"].iloc[-1]
            return float(v) if not pd.isna(v) else None
        return None

    @staticmethod
    def _norm_return(r: float) -> float:
        """收益率归一化到 [0, 100]，20%收益=100分"""
        return min(max(r * 500 + 50, 0), 100)
