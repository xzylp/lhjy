"""滑点 + 手续费模型"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TradeCost:
    commission: float    # 佣金
    stamp_duty: float    # 印花税 (卖出)
    transfer_fee: float  # 过户费
    slippage: float      # 滑点成本
    total: float         # 总成本


class CostModel:
    """A股交易成本模型"""

    def __init__(
        self,
        *,
        commission_rate: float = 0.0003,
        stamp_duty_rate: float = 0.0005,
        transfer_fee_rate: float = 0.00002,
        slippage_rate: float = 0.001,
    ) -> None:
        self.COMMISSION_RATE = commission_rate
        self.STAMP_DUTY_RATE = stamp_duty_rate
        self.TRANSFER_FEE_RATE = transfer_fee_rate
        self.SLIPPAGE_RATE = slippage_rate

    def calc(self, price: float, quantity: int, side: str) -> TradeCost:
        value = price * quantity
        commission = max(value * self.COMMISSION_RATE, 5.0)
        stamp_duty = value * self.STAMP_DUTY_RATE if side == "SELL" else 0.0
        transfer_fee = value * self.TRANSFER_FEE_RATE
        slippage = value * self.SLIPPAGE_RATE
        total = commission + stamp_duty + transfer_fee + slippage
        return TradeCost(
            commission=commission,
            stamp_duty=stamp_duty,
            transfer_fee=transfer_fee,
            slippage=slippage,
            total=total,
        )

    def effective_price(self, price: float, side: str) -> float:
        """考虑滑点后的实际成交价"""
        if side == "BUY":
            return price * (1 + self.SLIPPAGE_RATE)
        return price * (1 - self.SLIPPAGE_RATE)
