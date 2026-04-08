"""资金曲线生成"""

from __future__ import annotations

import pandas as pd
from dataclasses import dataclass, field


@dataclass
class CurveData:
    equity: pd.Series = field(default_factory=pd.Series)
    drawdown: pd.Series = field(default_factory=pd.Series)
    daily_returns: pd.Series = field(default_factory=pd.Series)


class CurveBuilder:
    """资金曲线构建器"""

    def build(self, initial_cash: float, trade_log: list[dict]) -> CurveData:
        """从交易日志构建资金曲线"""
        if not trade_log:
            return CurveData(
                equity=pd.Series([initial_cash]),
                drawdown=pd.Series([0.0]),
                daily_returns=pd.Series([0.0]),
            )

        dates = sorted({t["date"] for t in trade_log})
        equity_map: dict[str, float] = {}
        cash = initial_cash
        for date in dates:
            day_trades = [t for t in trade_log if t["date"] == date]
            for t in day_trades:
                cash += t.get("pnl", 0)
            equity_map[date] = cash

        equity = pd.Series(equity_map)
        peak = equity.cummax()
        drawdown = (equity - peak) / peak
        daily_returns = equity.pct_change().fillna(0)

        return CurveData(equity=equity, drawdown=drawdown, daily_returns=daily_returns)

    def to_normalized(self, equity: pd.Series) -> pd.Series:
        """归一化净值曲线 (初始=1)"""
        if equity.empty or equity.iloc[0] == 0:
            return equity
        return equity / equity.iloc[0]
