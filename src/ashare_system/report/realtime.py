"""实时交易报告"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .generator import ReportGenerator
from ..logging_config import get_logger

logger = get_logger("report.realtime")


@dataclass
class RealtimeSnapshot:
    timestamp: str
    total_asset: float
    cash: float
    unrealized_pnl: float
    realized_pnl: float
    positions: list[dict] = field(default_factory=list)
    recent_trades: list[dict] = field(default_factory=list)


class RealtimeReporter:
    """实时交易报告 (每30分钟/1小时)"""

    def __init__(self, generator: ReportGenerator | None = None) -> None:
        self.gen = generator or ReportGenerator()

    def generate(self, snapshot: RealtimeSnapshot) -> str:
        lines = [
            f"📊 **实时快照 {snapshot.timestamp}**",
            f"- 总资产: {snapshot.total_asset:,.2f}",
            f"- 可用资金: {snapshot.cash:,.2f}",
            f"- 浮动盈亏: {snapshot.unrealized_pnl:+,.2f}",
            f"- 已实现盈亏: {snapshot.realized_pnl:+,.2f}",
            f"- 持仓数量: {len(snapshot.positions)}",
        ]
        if snapshot.recent_trades:
            lines.append(f"- 最近交易: {len(snapshot.recent_trades)} 笔")
        content = "\n".join(lines)
        logger.info("实时报告生成: %s", snapshot.timestamp)
        return content
