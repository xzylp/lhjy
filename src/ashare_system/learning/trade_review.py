"""交易复盘分析"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..logging_config import get_logger

logger = get_logger("learning.trade_review")


@dataclass
class TradeAnalysis:
    trade_id: str
    symbol: str
    side: str
    pnl: float
    pnl_pct: float
    holding_days: int
    win: bool
    factors_at_entry: dict = field(default_factory=dict)
    exit_reason: str = ""
    lessons: list[str] = field(default_factory=list)


@dataclass
class ReviewSummary:
    total_trades: int
    win_rate: float
    avg_pnl: float
    best_factors: list[str] = field(default_factory=list)
    worst_factors: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


class TradeReviewer:
    """交易复盘分析器"""

    def analyze_trade(self, trade: dict) -> TradeAnalysis:
        pnl = trade.get("pnl", 0.0)
        entry_price = trade.get("entry_price", 1.0)
        pnl_pct = pnl / max(entry_price * trade.get("quantity", 1), 1e-9)
        analysis = TradeAnalysis(
            trade_id=trade.get("trade_id", ""),
            symbol=trade.get("symbol", ""),
            side=trade.get("side", "BUY"),
            pnl=pnl,
            pnl_pct=pnl_pct,
            holding_days=trade.get("holding_days", 0),
            win=pnl > 0,
            factors_at_entry=trade.get("factors", {}),
            exit_reason=trade.get("exit_reason", ""),
        )
        if not analysis.win:
            analysis.lessons.append(f"亏损 {pnl_pct:.1%}，检查入场时机和止损设置")
        return analysis

    def summarize(self, trades: list[dict]) -> ReviewSummary:
        if not trades:
            return ReviewSummary(total_trades=0, win_rate=0.0, avg_pnl=0.0)
        analyses = [self.analyze_trade(t) for t in trades]
        wins = [a for a in analyses if a.win]
        win_rate = len(wins) / len(analyses)
        avg_pnl = sum(a.pnl for a in analyses) / len(analyses)
        suggestions = []
        if win_rate < 0.5:
            suggestions.append(f"胜率 {win_rate:.1%} 偏低，建议提高入场标准")
        if avg_pnl < 0:
            suggestions.append("平均盈亏为负，建议优化止损止盈比例")
        logger.info("复盘汇总: %d 笔交易, 胜率=%.1%%, 均盈亏=%.2f", len(trades), win_rate * 100, avg_pnl)
        return ReviewSummary(total_trades=len(trades), win_rate=win_rate, avg_pnl=avg_pnl, suggestions=suggestions)
