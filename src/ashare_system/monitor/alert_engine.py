"""即时预警引擎"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import QuoteSnapshot
from ..infra.filters import get_price_limit_ratio
from ..logging_config import get_logger

logger = get_logger("monitor.alert_engine")

ALERT_CHANGE_THRESHOLD = 0.05   # 5分钟内涨跌超5%触发预警
ALERT_VOLUME_THRESHOLD = 5.0    # 成交量超过5倍均量触发预警


@dataclass
class AlertEvent:
    symbol: str
    alert_type: str   # "price_spike" | "volume_surge" | "limit_up" | "limit_down"
    message: str
    severity: str     # "info" | "warning" | "critical"
    price: float
    change_pct: float


class AlertEngine:
    """即时预警引擎"""

    def __init__(self) -> None:
        self._prev_prices: dict[str, float] = {}
        self._avg_volumes: dict[str, float] = {}

    def check(self, snapshot: QuoteSnapshot) -> list[AlertEvent]:
        alerts: list[AlertEvent] = []
        symbol = snapshot.symbol
        price = snapshot.last_price
        prev = self._prev_prices.get(symbol, price)

        if prev > 0:
            change = (price - prev) / prev
            # 价格异动
            if abs(change) >= ALERT_CHANGE_THRESHOLD:
                severity = "critical" if abs(change) >= 0.09 else "warning"
                alerts.append(AlertEvent(
                    symbol=symbol, alert_type="price_spike",
                    message=f"{symbol} 价格异动 {change:+.1%}",
                    severity=severity, price=price, change_pct=change,
                ))
            # 涨停 (板块感知: 主板10%, 创业板/科创板20%, 北交所30%)
            if snapshot.pre_close > 0:
                limit = get_price_limit_ratio(symbol)
                if price >= snapshot.pre_close * (1 + limit - 0.001):
                    alerts.append(AlertEvent(symbol=symbol, alert_type="limit_up", message=f"{symbol} 涨停", severity="info", price=price, change_pct=change))
                # 跌停
                if price <= snapshot.pre_close * (1 - limit + 0.001):
                    alerts.append(AlertEvent(symbol=symbol, alert_type="limit_down", message=f"{symbol} 跌停", severity="warning", price=price, change_pct=change))

        # 量能异动
        avg_vol = self._avg_volumes.get(symbol, snapshot.volume)
        if avg_vol > 0 and snapshot.volume >= avg_vol * ALERT_VOLUME_THRESHOLD:
            alerts.append(AlertEvent(symbol=symbol, alert_type="volume_surge", message=f"{symbol} 成交量异动 {snapshot.volume/avg_vol:.1f}倍", severity="info", price=price, change_pct=0))

        self._prev_prices[symbol] = price
        self._avg_volumes[symbol] = avg_vol * 0.9 + snapshot.volume * 0.1
        return alerts

    def check_batch(self, snapshots: list[QuoteSnapshot]) -> list[AlertEvent]:
        all_alerts: list[AlertEvent] = []
        for snap in snapshots:
            all_alerts.extend(self.check(snap))
        return all_alerts
