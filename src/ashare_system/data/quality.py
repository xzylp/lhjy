"""行情数据质量校验。"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..contracts import BarSnapshot


@dataclass
class QualityAlert:
    symbol: str
    trade_time: str
    level: str
    issue: str
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class BarQualityChecker:
    """对 K 线做轻量级质量校验，不阻断主流程。"""

    def validate_bars(self, bars: list[BarSnapshot]) -> tuple[list[BarSnapshot], list[QualityAlert]]:
        cleaned: list[BarSnapshot] = []
        alerts: list[QualityAlert] = []
        ordered = sorted(
            bars,
            key=lambda item: (str(item.symbol), str(item.trade_time)),
        )
        for bar in ordered:
            if float(bar.close or 0.0) <= 0.0:
                alerts.append(
                    QualityAlert(
                        symbol=bar.symbol,
                        trade_time=bar.trade_time,
                        level="error",
                        issue="zero_price",
                        detail="close<=0，已剔除",
                    )
                )
                continue
            if float(bar.pre_close or 0.0) > 0:
                gap = abs(float(bar.close or 0.0) / float(bar.pre_close or 1.0) - 1.0)
                if gap > 0.11:
                    alerts.append(
                        QualityAlert(
                            symbol=bar.symbol,
                            trade_time=bar.trade_time,
                            level="warning",
                            issue="abnormal_gap",
                            detail=f"close/pre_close 偏离 {gap:.2%}",
                        )
                    )
            if float(bar.volume or 0.0) <= 0.0 and abs(float(bar.close or 0.0) - float(bar.pre_close or 0.0)) > 1e-9:
                alerts.append(
                    QualityAlert(
                        symbol=bar.symbol,
                        trade_time=bar.trade_time,
                        level="warning",
                        issue="suspended_day",
                        detail="volume=0 但价格变动，疑似停牌/脏数据",
                    )
                )
            cleaned.append(bar)
        return cleaned, alerts
