"""实盘前执行安全规则。"""

from __future__ import annotations

from datetime import datetime


def is_trading_session(moment: datetime) -> bool:
    if moment.weekday() >= 5:
        return False
    current = moment.hour * 60 + moment.minute
    morning_open = 9 * 60 + 30
    morning_close = 11 * 60 + 30
    afternoon_open = 13 * 60
    afternoon_close = 15 * 60
    return (morning_open <= current <= morning_close) or (afternoon_open <= current <= afternoon_close)


def board_limit_pct(symbol: str) -> float:
    code = symbol.split(".", 1)[0]
    if code.startswith(("300", "301", "688", "689")):
        return 0.2
    if code.startswith(("43", "83", "87", "88", "92")):
        return 0.3
    return 0.1


def is_limit_up(symbol: str, last_price: float, pre_close: float) -> bool:
    if pre_close <= 0 or last_price <= 0:
        return False
    limit = board_limit_pct(symbol)
    return last_price >= pre_close * (1 + limit - 0.001)


def is_limit_down(symbol: str, last_price: float, pre_close: float) -> bool:
    if pre_close <= 0 or last_price <= 0:
        return False
    limit = board_limit_pct(symbol)
    return last_price <= pre_close * (1 - limit + 0.001)


def snapshot_age_seconds(snapshot_at: str | None, now: datetime) -> float | None:
    if not snapshot_at:
        return None
    try:
        recorded_at = datetime.fromisoformat(snapshot_at)
    except ValueError:
        return None
    return max((now - recorded_at).total_seconds(), 0.0)


def is_snapshot_fresh(snapshot_at: str | None, now: datetime, max_age_seconds: int) -> bool:
    age = snapshot_age_seconds(snapshot_at, now)
    return age is not None and age <= max_age_seconds


def is_price_deviation_exceeded(
    latest_price: float,
    bid_price: float,
    ask_price: float,
    max_deviation_pct: float,
) -> bool:
    reference = ask_price if ask_price > 0 else (bid_price if bid_price > 0 else latest_price)
    if latest_price <= 0 or reference <= 0:
        return False
    deviation = abs(latest_price - reference) / reference
    return deviation > max_deviation_pct
