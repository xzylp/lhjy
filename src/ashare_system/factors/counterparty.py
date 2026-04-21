"""对手盘行为分析。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass
class TraderTypeClassifier:
    """基于分时收益、成交量与冲击成本粗分对手盘类型。"""

    large_order_ratio_threshold: float = 1.8
    impact_threshold: float = 0.002

    def classify(self, bars: pd.DataFrame) -> dict[str, Any]:
        if bars.empty or "close" not in bars.columns or "volume" not in bars.columns:
            return {"trader_type": "unknown", "confidence": 0.0, "evidence": ["缺少分时成交数据"]}
        frame = bars.copy()
        frame["ret"] = frame["close"].pct_change().fillna(0.0)
        frame["trade_value"] = frame.get("amount", frame["close"] * frame["volume"])
        avg_trade_value = float(frame["trade_value"].mean() or 0.0)
        large_ratio = (
            float(frame["trade_value"].quantile(0.9) / max(avg_trade_value, 1e-9))
            if avg_trade_value > 0
            else 0.0
        )
        impact = float(frame["ret"].abs().mean() or 0.0)
        if large_ratio >= self.large_order_ratio_threshold and impact <= self.impact_threshold:
            return {
                "trader_type": "institutional",
                "confidence": min(0.95, 0.55 + (large_ratio - self.large_order_ratio_threshold) * 0.2),
                "evidence": [f"大单占比={large_ratio:.2f}", f"冲击成本={impact:.4f}"],
            }
        if impact > self.impact_threshold * 1.5:
            return {
                "trader_type": "retail_dominant",
                "confidence": min(0.9, 0.5 + impact / max(self.impact_threshold, 1e-9) * 0.1),
                "evidence": [f"波动冲击偏大={impact:.4f}", f"大单占比={large_ratio:.2f}"],
            }
        return {
            "trader_type": "balanced",
            "confidence": 0.55,
            "evidence": [f"大单占比={large_ratio:.2f}", f"冲击成本={impact:.4f}"],
        }


def counterparty_strength_from_bars(bars: pd.DataFrame) -> dict[str, Any]:
    classifier = TraderTypeClassifier()
    classified = classifier.classify(bars)
    trader_type = classified.get("trader_type")
    if trader_type == "institutional":
        score = 0.8
    elif trader_type == "balanced":
        score = 0.25
    elif trader_type == "retail_dominant":
        score = -0.45
    else:
        score = 0.0
    return {
        "score": score,
        "trader_type": trader_type,
        "confidence": float(classified.get("confidence", 0.0) or 0.0),
        "evidence": list(classified.get("evidence") or []),
    }
