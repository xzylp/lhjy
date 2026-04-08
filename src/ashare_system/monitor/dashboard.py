"""监控仪表盘 — 终端文本可视化"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..contracts import MarketProfile
from ..logging_config import get_logger

logger = get_logger("monitor.dashboard")


@dataclass
class DashboardSnapshot:
    timestamp: str
    profile: MarketProfile | None = None
    limit_up_count: int = 0
    limit_down_count: int = 0
    total_asset: float = 0.0
    cash: float = 0.0
    unrealized_pnl: float = 0.0
    position_count: int = 0
    today_trades: int = 0
    alerts: list[str] = field(default_factory=list)
    hot_sectors: list[str] = field(default_factory=list)
    top_gainers: list[str] = field(default_factory=list)


class Dashboard:
    """终端监控仪表盘"""

    def render(self, snap: DashboardSnapshot) -> str:
        """渲染文本仪表盘"""
        phase = snap.profile.sentiment_phase if snap.profile else "未知"
        score = snap.profile.sentiment_score if snap.profile else 0
        ceiling = snap.profile.position_ceiling if snap.profile else 0
        pnl_sign = "+" if snap.unrealized_pnl >= 0 else ""
        lines = [
            "=" * 60,
            f"  ashare-system-v2 监控仪表盘  {snap.timestamp}",
            "=" * 60,
            f"  市场情绪: {phase} ({score:.0f}分)  仓位上限: {ceiling:.0%}",
            f"  涨停: {snap.limit_up_count}  跌停: {snap.limit_down_count}",
            "-" * 60,
            f"  总资产: {snap.total_asset:>12,.2f}  可用: {snap.cash:>12,.2f}",
            f"  浮动盈亏: {pnl_sign}{snap.unrealized_pnl:>10,.2f}  持仓: {snap.position_count}只  今日交易: {snap.today_trades}笔",
            "-" * 60,
        ]
        if snap.hot_sectors:
            lines.append(f"  热点板块: {' | '.join(snap.hot_sectors[:5])}")
        if snap.top_gainers:
            lines.append(f"  涨幅前列: {' '.join(snap.top_gainers[:5])}")
        if snap.alerts:
            lines.append("-" * 60)
            lines.append("  ⚠️  告警:")
            for alert in snap.alerts[-3:]:
                lines.append(f"    • {alert}")
        lines.append("=" * 60)
        return "\n".join(lines)

    def print(self, snap: DashboardSnapshot) -> None:
        print(self.render(snap))

    def build_snapshot(self, market_adapter=None, execution_adapter=None, profile: MarketProfile | None = None, account_id: str = "sim-001") -> DashboardSnapshot:
        """从适配器构建快照"""
        snap = DashboardSnapshot(timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), profile=profile)
        if execution_adapter:
            try:
                bal = execution_adapter.get_balance(account_id)
                positions = execution_adapter.get_positions(account_id)
                snap.total_asset = bal.total_asset
                snap.cash = bal.cash
                snap.position_count = len(positions)
                snap.unrealized_pnl = sum((p.last_price - p.cost_price) * p.quantity for p in positions)
            except Exception as e:
                logger.warning("仪表盘获取账户数据失败: %s", e)
        if profile:
            snap.hot_sectors = profile.hot_sectors[:5]
        return snap
