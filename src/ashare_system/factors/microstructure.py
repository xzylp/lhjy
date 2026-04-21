"""盘口与分时微观结构工具。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..contracts import OrderBookSnapshot


@dataclass
class MicrostructureSignal:
    score: float
    evidence: list[str]
    details: dict[str, Any]


def order_book_imbalance(snapshot: OrderBookSnapshot, depth: int = 5) -> MicrostructureSignal:
    bids = list(snapshot.bids or [])[: max(depth, 1)]
    asks = list(snapshot.asks or [])[: max(depth, 1)]
    bid_volume = sum(float(level.volume or 0.0) for level in bids)
    ask_volume = sum(float(level.volume or 0.0) for level in asks)
    total = bid_volume + ask_volume
    imbalance = ((bid_volume - ask_volume) / total) if total > 0 else 0.0
    score = max(min(imbalance, 1.0), -1.0)
    return MicrostructureSignal(
        score=round(score, 4),
        evidence=[
            f"盘口前{len(bids)}档买量={bid_volume:.0f}",
            f"盘口前{len(asks)}档卖量={ask_volume:.0f}",
            f"盘口失衡度={imbalance:.2%}",
        ],
        details={"bid_volume": bid_volume, "ask_volume": ask_volume, "imbalance": imbalance},
    )


def large_order_flow(snapshot: OrderBookSnapshot) -> MicrostructureSignal:
    large_buy = float(snapshot.large_buy_volume or 0.0)
    large_sell = float(snapshot.large_sell_volume or 0.0)
    fallback_buy = float(snapshot.buy_volume or 0.0)
    fallback_sell = float(snapshot.sell_volume or 0.0)
    if large_buy <= 0 and large_sell <= 0:
        large_buy = fallback_buy * 0.35
        large_sell = fallback_sell * 0.35
    total = large_buy + large_sell
    flow = ((large_buy - large_sell) / total) if total > 0 else 0.0
    score = max(min(flow, 1.0), -1.0)
    return MicrostructureSignal(
        score=round(score, 4),
        evidence=[
            f"大单买量={large_buy:.0f}",
            f"大单卖量={large_sell:.0f}",
            f"大单流向={flow:.2%}",
        ],
        details={"large_buy_volume": large_buy, "large_sell_volume": large_sell, "flow": flow},
    )
