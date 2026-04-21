"""仓位 sizing 模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


PositionSizingMode = Literal["fixed_pct", "half_kelly", "volatility_target"]


@dataclass
class HistoricalTradeStats:
    sample_count: int = 0
    win_rate: float = 0.0
    payoff_ratio: float = 0.0
    realized_volatility: float | None = None
    avg_return_pct: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionSizingContext:
    total_equity: float
    cash_available: float
    price: float
    open_slots: int = 1
    base_position_pct: float = 0.10
    max_single_position: float = 0.25
    max_single_amount: float | None = None
    target_volatility: float = 0.02


@dataclass
class PositionSizingResult:
    mode: PositionSizingMode
    allocation_value: float
    quantity: int
    fraction_of_equity: float
    downgraded_to: PositionSizingMode | None = None
    reason: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "allocation_value": round(float(self.allocation_value or 0.0), 4),
            "quantity": int(self.quantity or 0),
            "fraction_of_equity": round(float(self.fraction_of_equity or 0.0), 6),
            "downgraded_to": self.downgraded_to,
            "reason": self.reason,
            "diagnostics": dict(self.diagnostics or {}),
        }


class PositionSizer:
    """根据历史胜率/赔率/波动率动态估算下单金额。"""

    def __init__(
        self,
        *,
        default_mode: PositionSizingMode = "half_kelly",
        min_history_trades: int = 20,
        max_single_position: float = 0.25,
        fixed_pct: float = 0.10,
        target_volatility: float = 0.02,
    ) -> None:
        self.default_mode = default_mode
        self.min_history_trades = max(int(min_history_trades or 20), 1)
        self.max_single_position = max(float(max_single_position or 0.25), 0.0)
        self.fixed_pct = max(float(fixed_pct or 0.10), 0.0)
        self.target_volatility = max(float(target_volatility or 0.02), 0.0001)

    def calculate(
        self,
        *,
        context: PositionSizingContext,
        stats: HistoricalTradeStats | None = None,
        mode: PositionSizingMode | None = None,
    ) -> PositionSizingResult:
        resolved_mode = mode or self.default_mode
        base_allocation = min(
            float(context.total_equity or 0.0) * float(context.base_position_pct or self.fixed_pct),
            float(context.cash_available or 0.0) / max(int(context.open_slots or 1), 1),
        )
        absolute_cap = min(
            float(context.total_equity or 0.0) * min(float(context.max_single_position or self.max_single_position), self.max_single_position),
            float(context.cash_available or 0.0),
        )
        if context.max_single_amount is not None:
            absolute_cap = min(absolute_cap, float(context.max_single_amount or 0.0))
        base_allocation = max(min(base_allocation, absolute_cap), 0.0)

        allocation = base_allocation
        downgraded_to: PositionSizingMode | None = None
        reason = ""
        diagnostics: dict[str, Any] = {
            "base_allocation": round(base_allocation, 4),
            "absolute_cap": round(absolute_cap, 4),
        }

        if resolved_mode == "half_kelly":
            if stats is None or stats.sample_count < self.min_history_trades or float(stats.payoff_ratio or 0.0) <= 0:
                downgraded_to = "fixed_pct"
                reason = "历史样本不足，half_kelly 自动降级到 fixed_pct"
                allocation = base_allocation
            else:
                win_rate = max(min(float(stats.win_rate or 0.0), 1.0), 0.0)
                payoff_ratio = max(float(stats.payoff_ratio or 0.0), 1e-6)
                raw_fraction = win_rate - (1.0 - win_rate) / payoff_ratio
                half_kelly_fraction = max(min(0.5 * raw_fraction, self.max_single_position), 0.0)
                allocation = min(float(context.total_equity or 0.0) * half_kelly_fraction, absolute_cap)
                diagnostics.update(
                    {
                        "win_rate": round(win_rate, 6),
                        "payoff_ratio": round(payoff_ratio, 6),
                        "raw_kelly_fraction": round(raw_fraction, 6),
                        "half_kelly_fraction": round(half_kelly_fraction, 6),
                        "sample_count": int(stats.sample_count or 0),
                    }
                )
        elif resolved_mode == "volatility_target":
            stock_vol = float((stats.realized_volatility if stats else None) or 0.0)
            if stock_vol <= 0:
                downgraded_to = "fixed_pct"
                reason = "缺少有效波动率，volatility_target 自动降级到 fixed_pct"
                allocation = base_allocation
            else:
                target_vol = float(context.target_volatility or self.target_volatility)
                scale = max(min(target_vol / stock_vol, 2.0), 0.25)
                allocation = min(base_allocation * scale, absolute_cap)
                diagnostics.update(
                    {
                        "stock_volatility": round(stock_vol, 6),
                        "target_volatility": round(target_vol, 6),
                        "volatility_scale": round(scale, 6),
                    }
                )
        else:
            resolved_mode = "fixed_pct"
            allocation = base_allocation

        price = float(context.price or 0.0)
        quantity = int(allocation / max(price, 1e-9) / 100) * 100 if price > 0 else 0
        lot_allocation = float(quantity) * price if quantity > 0 else 0.0
        fraction = lot_allocation / max(float(context.total_equity or 0.0), 1e-9) if context.total_equity else 0.0
        if quantity <= 0 and not reason:
            reason = "估算金额不足一手"
        diagnostics["lot_allocation"] = round(lot_allocation, 4)
        diagnostics["cash_available"] = round(float(context.cash_available or 0.0), 4)
        diagnostics["price"] = round(price, 4)
        return PositionSizingResult(
            mode=resolved_mode,
            allocation_value=lot_allocation,
            quantity=quantity,
            fraction_of_equity=fraction,
            downgraded_to=downgraded_to,
            reason=reason,
            diagnostics=diagnostics,
        )

    @staticmethod
    def derive_trade_stats(
        trade_records: list[dict[str, Any]] | None,
        *,
        playbook: str | None = None,
        symbol: str | None = None,
        max_samples: int = 50,
    ) -> HistoricalTradeStats:
        filtered: list[dict[str, Any]] = []
        for item in list(trade_records or []):
            if playbook and str(item.get("playbook") or "") != str(playbook):
                continue
            if symbol and str(item.get("symbol") or "") != str(symbol):
                continue
            filtered.append(dict(item))
        filtered = filtered[-max_samples:]
        if not filtered:
            return HistoricalTradeStats()
        returns: list[float] = []
        for item in filtered:
            raw = item.get("holding_return_pct")
            if raw is None:
                raw = item.get("next_day_close_pct")
            if raw is None:
                raw = item.get("return_pct")
            returns.append(float(raw or 0.0))
        wins = [value for value in returns if value > 0]
        losses = [abs(value) for value in returns if value < 0]
        payoff_ratio = (sum(wins) / len(wins)) / max(sum(losses) / len(losses), 1e-6) if wins and losses else (1.5 if wins else 0.0)
        mean_return = sum(returns) / max(len(returns), 1)
        realized_volatility = None
        if len(returns) >= 2:
            variance = sum((value - mean_return) ** 2 for value in returns) / max(len(returns) - 1, 1)
            realized_volatility = variance ** 0.5
        return HistoricalTradeStats(
            sample_count=len(filtered),
            win_rate=len(wins) / max(len(filtered), 1),
            payoff_ratio=payoff_ratio,
            realized_volatility=realized_volatility,
            avg_return_pct=mean_return,
            metadata={
                "playbook": playbook,
                "symbol": symbol,
                "max_samples": max_samples,
            },
        )
