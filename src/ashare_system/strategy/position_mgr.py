"""仓位管理 — 凯利公式 × ATR × 情绪系数"""

from __future__ import annotations

import numpy as np
from dataclasses import dataclass

from ..contracts import PositionPlan, MarketProfile
from ..logging_config import get_logger

logger = get_logger("strategy.position_mgr")

MAX_SINGLE_POSITION = 0.25  # 单票最大仓位


@dataclass
class PositionInput:
    symbol: str
    win_rate: float          # 历史胜率 [0,1]
    profit_loss_ratio: float # 赔率 (平均盈利/平均亏损)
    atr: float               # ATR14
    price: float             # 当前价格
    account_equity: float    # 账户总资产
    portfolio_corr: float = 0.0  # 与现有持仓的相关性惩罚 [0,1]


class PositionManager:
    """凯利公式仓位管理器"""

    def calc(self, inp: PositionInput, profile: MarketProfile | None = None) -> PositionPlan:
        kelly = self._kelly(inp.win_rate, inp.profit_loss_ratio)
        base = kelly * inp.account_equity

        # ATR 波动率调整: 目标风险 = 账户的 1%，标准公式: 仓位 = 风险金额 / ATR * 价格
        target_risk = inp.account_equity * 0.01
        vol_adj = (target_risk / max(inp.atr, 1e-9)) * inp.price

        # 情绪系数
        emotion_ceil = profile.position_ceiling if profile else 0.8

        # 相关性惩罚
        corr_adj = 1.0 - inp.portfolio_corr * 0.5

        # 最终仓位
        raw = min(base, vol_adj) * emotion_ceil * corr_adj
        final_value = min(raw, inp.account_equity * MAX_SINGLE_POSITION)
        final_ratio = final_value / max(inp.account_equity, 1e-9)
        target_shares = int(final_value / max(inp.price, 1e-9) / 100) * 100  # 整手

        return PositionPlan(
            symbol=inp.symbol,
            target_shares=target_shares,
            target_value=final_value,
            kelly_fraction=kelly,
            atr_adjusted=vol_adj,
            emotion_ceiling=emotion_ceil,
            final_ratio=final_ratio,
        )

    @staticmethod
    def _kelly(win_rate: float, profit_loss_ratio: float) -> float:
        """半凯利公式"""
        if profit_loss_ratio <= 0:
            return 0.0
        q = 1.0 - win_rate
        kelly = (win_rate * profit_loss_ratio - q) / profit_loss_ratio
        return max(0.0, min(kelly / 2, 0.25))  # 半凯利，上限25%
