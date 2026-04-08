"""策略优化报告"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..backtest.metrics import BacktestMetrics
from .generator import ReportGenerator
from ..logging_config import get_logger

logger = get_logger("report.strategy_report")


@dataclass
class StrategyReportData:
    strategy_name: str
    metrics: BacktestMetrics
    factor_importance: dict[str, float] = field(default_factory=dict)
    suggestions: list[str] = field(default_factory=list)
    period: str = ""


class StrategyReporter:
    """策略优化报告生成器"""

    def __init__(self, generator: ReportGenerator | None = None) -> None:
        self.gen = generator or ReportGenerator()

    def generate(self, data: StrategyReportData) -> str:
        m = data.metrics
        top_factors = sorted(data.factor_importance.items(), key=lambda x: x[1], reverse=True)[:5]
        lines = [
            f"# 策略优化报告: {data.strategy_name}",
            f"**回测周期:** {data.period}",
            "",
            "## 绩效指标",
            f"- 总收益: {m.total_return:+.1%}",
            f"- 年化收益: {m.annual_return:+.1%}",
            f"- 夏普比率: {m.sharpe_ratio:.2f}",
            f"- 最大回撤: {m.max_drawdown:.1%}",
            f"- 卡尔玛比率: {m.calmar_ratio:.2f}",
            f"- 胜率: {m.win_rate:.1%}",
            f"- 盈亏比: {m.profit_loss_ratio:.2f}",
            f"- 总交易: {m.total_trades}",
            "",
            "## Top 5 因子",
        ]
        for name, imp in top_factors:
            lines.append(f"- {name}: {imp:.4f}")
        if data.suggestions:
            lines.append("\n## 优化建议")
            for s in data.suggestions:
                lines.append(f"- {s}")
        content = "\n".join(lines)
        filename = self.gen.timestamp_filename(f"strategy_{data.strategy_name}")
        self.gen.save(content, filename)
        return content
