"""退出监控器。

定位:
- 作为 market_watcher 下游消费 `QuoteSnapshot + ExitContext`
- 只输出 signal/check 结果，不直接下单
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..contracts import ExitContext, QuoteSnapshot


_SEVERITY_PRIORITY = {
    "IMMEDIATE": 0,
    "HIGH": 1,
    "NORMAL": 2,
}


@dataclass
class ExitMonitorSignal:
    symbol: str
    signal_type: str
    reason: str
    severity: str
    message: str
    suggested_action: str = "SELL_REVIEW"
    score: float = 0.0
    tags: list[str] = field(default_factory=list)
    evidence: dict | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["evidence"] = dict(self.evidence or {})
        payload["tags"] = list(dict.fromkeys(self.tags))
        return payload


class ExitMonitor:
    """最小退出监控实现。

    当前只做信号输出，后续由调度或执行层决定是否落地为卖出动作。
    """

    def __init__(
        self,
        *,
        soft_stop_loss_pct: float = 0.03,
        sector_trend_threshold: float = -0.015,
        negative_alert_threshold: int = 2,
        underperform_bar_threshold: int = 3,
        reseal_rebound_threshold: float = 0.02,
    ) -> None:
        self.soft_stop_loss_pct = soft_stop_loss_pct
        self.sector_trend_threshold = sector_trend_threshold
        self.negative_alert_threshold = negative_alert_threshold
        self.underperform_bar_threshold = underperform_bar_threshold
        self.reseal_rebound_threshold = reseal_rebound_threshold

    def check(
        self,
        quote: QuoteSnapshot,
        context: ExitContext,
        board_snapshot: dict | None = None,
    ) -> list[ExitMonitorSignal]:
        signals: list[ExitMonitorSignal] = []
        board = dict(board_snapshot or {})
        soft_stop = float(context.exit_params.get("soft_stop_loss_pct", self.soft_stop_loss_pct))

        if context.is_bomb and context.rebound_from_low_pct < self.reseal_rebound_threshold:
            signals.append(
                ExitMonitorSignal(
                    symbol=quote.symbol,
                    signal_type="bomb_exit",
                    reason="board_break",
                    severity="IMMEDIATE",
                    message="炸板后未出现有效回封，需立即复核退出。",
                    score=0.95,
                    tags=["board_break", "micro_rebound_failed"],
                    evidence={
                        "is_bomb": context.is_bomb,
                        "rebound_from_low_pct": context.rebound_from_low_pct,
                        "reseal_count": board.get("reseal_count", 0),
                    },
                )
            )

        sector_trend_weak = (
            context.sector_retreat
            or context.sector_relative_trend_5m <= self.sector_trend_threshold
            or context.sector_underperform_bars_5m >= self.underperform_bar_threshold
        )
        if sector_trend_weak:
            tags = ["sector_retreat"]
            if context.sector_underperform_bars_5m >= self.underperform_bar_threshold:
                tags.append("sector_relative_trend_weak")
            signals.append(
                ExitMonitorSignal(
                    symbol=quote.symbol,
                    signal_type="sector_retreat_exit",
                    reason="sector_retreat",
                    severity="HIGH",
                    message="板块联动转弱，需提高退出优先级。",
                    score=0.80,
                    tags=tags,
                    evidence={
                        "sector_retreat": context.sector_retreat,
                        "sector_relative_trend_5m": context.sector_relative_trend_5m,
                        "sector_underperform_bars_5m": context.sector_underperform_bars_5m,
                    },
                )
            )

        if context.negative_alert_count >= self.negative_alert_threshold:
            signals.append(
                ExitMonitorSignal(
                    symbol=quote.symbol,
                    signal_type="negative_alert_exit",
                    reason="time_stop",
                    severity="HIGH",
                    message="负反馈预警累计过多，需收紧持仓。",
                    score=0.72,
                    tags=["negative_alert"],
                    evidence={"negative_alert_count": context.negative_alert_count},
                )
            )

        if (
            context.intraday_drawdown_pct <= -abs(soft_stop)
            or context.intraday_change_pct <= -abs(soft_stop)
        ):
            signals.append(
                ExitMonitorSignal(
                    symbol=quote.symbol,
                    signal_type="soft_stop_loss_exit",
                    reason="time_stop",
                    severity="HIGH",
                    message="盘中回撤触发软止损阈值，建议退出复核。",
                    score=0.78,
                    tags=["intraday_fade"],
                    evidence={
                        "intraday_drawdown_pct": context.intraday_drawdown_pct,
                        "intraday_change_pct": context.intraday_change_pct,
                        "soft_stop_loss_pct": soft_stop,
                    },
                )
            )

        return sorted(signals, key=lambda item: (_SEVERITY_PRIORITY.get(item.severity, 99), -item.score))

    @staticmethod
    def _bucket_counts(values: list[str], *, priority: dict[str, int] | None = None) -> list[dict]:
        counts: dict[str, int] = {}
        for value in values:
            if not value:
                continue
            counts[value] = counts.get(value, 0) + 1
        return [
            {"key": key, "count": count}
            for key, count in sorted(
                counts.items(),
                key=lambda item: (-item[1], (priority or {}).get(item[0], 99), item[0]),
            )
        ]

    @classmethod
    def _build_summary_lines(
        cls,
        *,
        count: int,
        symbols: list[str],
        by_symbol: list[dict],
        by_reason: list[dict],
        by_severity: list[dict],
        by_tag: list[dict],
    ) -> list[str]:
        if count == 0:
            return ["当前无退出监控信号。"]
        lines = [f"退出监控共 {count} 条，涉及 {len(symbols)} 个标的。"]
        if by_symbol:
            lines.append("主要标的: " + "；".join(f"{item['key']}({item['count']})" for item in by_symbol[:3]))
        if by_reason:
            lines.append("主要原因: " + "；".join(f"{item['key']}({item['count']})" for item in by_reason[:4]))
        if by_severity:
            lines.append("严重级别: " + "；".join(f"{item['key']}({item['count']})" for item in by_severity))
        if by_tag:
            lines.append("主要标签: " + "；".join(f"{item['key']}({item['count']})" for item in by_tag[:4]))
        return lines

    def check_batch(
        self,
        snapshots: list[QuoteSnapshot],
        contexts: dict[str, ExitContext],
        board_snapshots: dict[str, dict] | None = None,
    ) -> list[ExitMonitorSignal]:
        board_map = board_snapshots or {}
        results: list[ExitMonitorSignal] = []
        for quote in snapshots:
            context = contexts.get(quote.symbol)
            if context is None:
                continue
            results.extend(self.check(quote, context, board_map.get(quote.symbol)))
        return results

    def summarize(self, signals: list[ExitMonitorSignal]) -> dict:
        items = [item.to_dict() for item in signals]
        by_symbol = self._bucket_counts([item.symbol for item in signals])
        by_reason = self._bucket_counts([item.reason for item in signals])
        by_severity = self._bucket_counts([item.severity for item in signals], priority=_SEVERITY_PRIORITY)
        by_type = self._bucket_counts([item.signal_type for item in signals])
        by_tag = self._bucket_counts([tag for item in signals for tag in item.tags])
        symbols = sorted({item.symbol for item in signals})
        return {
            "count": len(signals),
            "symbols": symbols,
            "by_symbol": by_symbol,
            "by_reason": by_reason,
            "by_severity": by_severity,
            "by_type": by_type,
            "by_tag": by_tag,
            "summary_lines": self._build_summary_lines(
                count=len(signals),
                symbols=symbols,
                by_symbol=by_symbol,
                by_reason=by_reason,
                by_severity=by_severity,
                by_tag=by_tag,
            ),
            "items": items,
        }
