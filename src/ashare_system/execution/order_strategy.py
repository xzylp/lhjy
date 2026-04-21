"""下单策略决策。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..contracts import QuoteSnapshot


OrderScenario = Literal["urgent_exit", "normal_buy", "opportunistic_buy"]


@dataclass
class OrderExecutionPlan:
    scenario: OrderScenario
    order_type: str
    time_in_force: str
    price: float
    chase_after_seconds: int | None = None
    urgency_tag: str = "normal"
    summary_lines: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "price": round(float(self.price or 0.0), 4),
            "chase_after_seconds": self.chase_after_seconds,
            "urgency_tag": self.urgency_tag,
            "summary_lines": list(self.summary_lines or []),
            "diagnostics": dict(self.diagnostics or {}),
        }


class OrderStrategyResolver:
    """根据紧迫度与盘口选择报单价格策略。"""

    def __init__(self, *, default_tick_size: float = 0.01) -> None:
        self.default_tick_size = max(float(default_tick_size or 0.01), 0.001)

    def resolve(
        self,
        *,
        side: str,
        quote: QuoteSnapshot | None = None,
        scenario: OrderScenario | None = None,
        signal_price: float | None = None,
    ) -> OrderExecutionPlan:
        resolved_side = str(side or "BUY").upper()
        last_price = float((quote.last_price if quote else None) or signal_price or 0.0)
        bid_price = float((quote.bid_price if quote else None) or last_price or 0.0)
        ask_price = float((quote.ask_price if quote else None) or last_price or 0.0)
        tick = self._resolve_tick_size(max(last_price, bid_price, ask_price, 0.0))

        if scenario is None:
            scenario = "urgent_exit" if resolved_side == "SELL" else "normal_buy"

        if scenario == "urgent_exit":
            price = bid_price if resolved_side == "SELL" else ask_price
            return OrderExecutionPlan(
                scenario="urgent_exit",
                order_type="opponent_best",
                time_in_force="day",
                price=max(price, last_price, tick),
                urgency_tag="immediate",
                summary_lines=["紧急退出采用对手价优先成交。"],
                diagnostics={"bid_price": bid_price, "ask_price": ask_price, "tick_size": tick},
            )
        if scenario == "opportunistic_buy":
            midpoint = (bid_price + ask_price) / 2 if bid_price > 0 and ask_price > 0 else max(last_price, bid_price, ask_price, 0.0)
            price = max(round(midpoint / tick) * tick, tick)
            return OrderExecutionPlan(
                scenario="opportunistic_buy",
                order_type="limit",
                time_in_force="day",
                price=price,
                chase_after_seconds=180,
                urgency_tag="patient",
                summary_lines=["机会型买入采用中间价挂单，3 分钟未成交再追价。"],
                diagnostics={"bid_price": bid_price, "ask_price": ask_price, "midpoint": midpoint, "tick_size": tick},
            )
        price = ask_price if ask_price > 0 else max(last_price, tick)
        price = price + tick
        return OrderExecutionPlan(
            scenario="normal_buy",
            order_type="limit",
            time_in_force="day",
            price=round(price, 4),
            urgency_tag="normal",
            summary_lines=["常规买入采用 ask1 + 1 tick 限价。"],
            diagnostics={"bid_price": bid_price, "ask_price": ask_price, "tick_size": tick},
        )

    def _resolve_tick_size(self, reference_price: float) -> float:
        if reference_price <= 0:
            return self.default_tick_size
        return 0.001 if reference_price < 1 else self.default_tick_size
