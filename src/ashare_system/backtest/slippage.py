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

    COMMISSION_RATE = 0.0003    # 佣金 0.03%，最低5元
    STAMP_DUTY_RATE = 0.0005     # 印花税 0.05% (仅卖出, 2023年8月下调)
    TRANSFER_FEE_RATE = 0.00002 # 过户费 0.002%
    SLIPPAGE_RATE = 0.001       # 滑点 0.1%

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
