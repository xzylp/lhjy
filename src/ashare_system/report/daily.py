"""日终复盘报告"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..contracts import MarketProfile
from .generator import ReportGenerator
from ..logging_config import get_logger

logger = get_logger("report.daily")


@dataclass
class DailyReportData:
    date: str
    profile: MarketProfile
    total_pnl: float = 0.0
    total_return_pct: float = 0.0
    positions: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    top_gainers: list[str] = field(default_factory=list)
    top_losers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


DAILY_TEMPLATE = """# 日终复盘报告 {date}

## 市场情绪
- 阶段: {phase} ({score:.0f}分)
- 仓位上限: {ceiling:.0%}
- 热点板块: {sectors}

## 交易汇总
- 当日盈亏: {pnl:+.2f}
- 当日收益率: {return_pct:+.2f}%
- 交易笔数: {trade_count}
- 持仓数量: {position_count}

## 备注
{notes}
"""


class DailyReporter:
    """日终复盘报告生成器"""

    def __init__(self, generator: ReportGenerator | None = None, dispatcher=None) -> None:
        self.gen = generator or ReportGenerator()
        self.dispatcher = dispatcher  # MessageDispatcher | None

    def generate(self, data: DailyReportData) -> str:
        content = DAILY_TEMPLATE.format(
            date=data.date,
            phase=data.profile.sentiment_phase,
            score=data.profile.sentiment_score,
            ceiling=data.profile.position_ceiling,
            sectors=", ".join(data.profile.hot_sectors[:5]) or "无",
            pnl=data.total_pnl,
            return_pct=data.total_return_pct * 100,
            trade_count=len(data.trades),
            position_count=len(data.positions),
            notes="\n".join(f"- {n}" for n in data.notes) or "无",
        )
        filename = self.gen.timestamp_filename(f"daily_{data.date}")
        self.gen.save(content, filename)
        logger.info("日终报告生成: %s", filename)

        # 推送飞书
        if self.dispatcher:
            try:
                self.dispatcher.dispatch_report(f"日终复盘 {data.date}", content)
            except Exception as e:
                logger.warning("飞书推送失败: %s", e)

        return content
