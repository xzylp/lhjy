"""统一 freshness / staleness 计算。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx

from .contracts import FreshnessMeta, StalenessLevel


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    if len(normalized) == 8 and normalized.isdigit():
        normalized = f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:8]}T00:00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def classify_staleness(
    source_at: str | None,
    *,
    now: datetime | None = None,
    fresh_seconds: int = 300,
    warm_seconds: int | None = None,
) -> StalenessLevel:
    parsed = parse_dt(source_at)
    if parsed is None:
        return "missing"
    current = now or datetime.now(parsed.tzinfo)
    age_seconds = max((current - parsed).total_seconds(), 0.0)
    resolved_warm = warm_seconds if warm_seconds is not None else max(fresh_seconds * 10, fresh_seconds)
    if age_seconds <= fresh_seconds:
        return "fresh"
    if age_seconds <= resolved_warm:
        return "warm"
    return "stale"


def build_freshness_meta(
    *,
    source_at: str | None,
    fetched_at: str | None,
    generated_at: str | None,
    fresh_seconds: int,
    warm_seconds: int | None = None,
    expiry_seconds: int | None = None,
    now: datetime | None = None,
) -> FreshnessMeta:
    base_time = parse_dt(source_at) or parse_dt(generated_at) or parse_dt(fetched_at) or (now or datetime.now())
    current = now or datetime.now(base_time.tzinfo)
    staleness = classify_staleness(
        source_at or generated_at or fetched_at,
        now=current,
        fresh_seconds=fresh_seconds,
        warm_seconds=warm_seconds,
    )
    resolved_expiry = expiry_seconds if expiry_seconds is not None else fresh_seconds
    expires_at = (base_time + timedelta(seconds=resolved_expiry)).isoformat()
    return FreshnessMeta(
        source_at=source_at,
        fetched_at=fetched_at,
        generated_at=generated_at,
        expires_at=expires_at,
        staleness_level=staleness,
    )


# ── 数据时效性分级（v1.0 新增） ────────────────────────────
# 与 contracts.DataFreshnessLevel 对应

def tag_freshness(fetched_at: str | None, now: datetime | None = None) -> str:
    """为数据打上时效性标签。

    Returns:
        "REALTIME" (<30s), "NEAR_REALTIME" (<5min),
        "DELAYED" (<15min), "STALE" (>15min)

    用途：Agent 在讨论中可以看到数据时效性，
    避免用 STALE 数据做出高置信度判断。
    """
    parsed = parse_dt(fetched_at)
    if parsed is None:
        return "STALE"
    current = now or datetime.now(parsed.tzinfo)
    age_seconds = max((current - parsed).total_seconds(), 0.0)
    if age_seconds <= 30:
        return "REALTIME"
    if age_seconds <= 300:
        return "NEAR_REALTIME"
    if age_seconds <= 900:
        return "DELAYED"
    return "STALE"


class DataFreshnessMonitor:
    def __init__(self, market_adapter: Any, now_factory=None) -> None:
        self._market_adapter = market_adapter
        self._now_factory = now_factory or datetime.now

    @staticmethod
    def _expected_latest_daily_trade_date(now: datetime) -> datetime.date:
        current_date = now.date()
        if now.weekday() >= 5:
            days_back = now.weekday() - 4
            return (now - timedelta(days=days_back)).date()
        current_minutes = now.hour * 60 + now.minute
        if current_minutes < 15 * 60:
            days_back = 1
            expected = now - timedelta(days=days_back)
            while expected.weekday() >= 5:
                days_back += 1
                expected = now - timedelta(days=days_back)
            return expected.date()
        return current_date

    def check_gateway_health(self) -> dict[str, Any]:
        base_url = str(getattr(self._market_adapter, "_base_url", "") or "").strip()
        if not base_url:
            return {
                "available": True,
                "status": "not_applicable",
                "adapter_type": self._market_adapter.__class__.__name__,
                "latency_ms": None,
            }
        started_at = datetime.now()
        try:
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                response = client.get(f"{base_url}/health")
            latency_ms = max(int((datetime.now() - started_at).total_seconds() * 1000), 0)
            return {
                "available": True,
                "status": "healthy" if response.status_code < 400 else "degraded",
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "base_url": base_url,
            }
        except Exception as exc:
            return {
                "available": False,
                "status": "unreachable",
                "latency_ms": None,
                "base_url": base_url,
                "error": str(exc),
            }

    def check_kline_freshness(self, symbols: list[str]) -> dict[str, Any]:
        resolved_symbols = [str(item).strip() for item in list(symbols or []) if str(item).strip()]
        try:
            if not resolved_symbols:
                resolved_symbols = list((self._market_adapter.get_main_board_universe() or [])[:5])
        except Exception as exc:
            return {
                "available": False,
                "sample_symbol_count": 0,
                "bar_count": 0,
                "latest_trade_time": "",
                "lag_hours": None,
                "status": "degraded",
                "error": str(exc),
            }
        try:
            bars = list(self._market_adapter.get_bars(resolved_symbols[:5], period="1d", count=1) or [])
        except Exception as exc:
            return {
                "available": False,
                "sample_symbol_count": len(resolved_symbols[:5]),
                "bar_count": 0,
                "latest_trade_time": "",
                "lag_hours": None,
                "status": "degraded",
                "error": str(exc),
            }
        latest_time = max((str(item.trade_time) for item in bars if str(item.trade_time).strip()), default="")
        parsed = parse_dt(latest_time)
        lag_hours = None
        stale = True
        expected_trade_date = None
        if parsed is not None:
            now = self._now_factory()
            if parsed.tzinfo is None:
                current = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
            else:
                current = now.astimezone(parsed.tzinfo) if getattr(now, "tzinfo", None) else now.replace(tzinfo=parsed.tzinfo)
            lag_hours = round(max((current - parsed).total_seconds(), 0.0) / 3600.0, 4)
            expected_trade_date = self._expected_latest_daily_trade_date(current).isoformat()
            stale = parsed.date() < self._expected_latest_daily_trade_date(current)
        return {
            "available": bool(bars),
            "sample_symbol_count": len(resolved_symbols[:5]),
            "bar_count": len(bars),
            "latest_trade_time": latest_time,
            "lag_hours": lag_hours,
            "expected_trade_date": expected_trade_date,
            "status": "stale" if stale else "fresh",
        }

    def check_universe_coverage(self) -> dict[str, Any]:
        try:
            universe = list(self._market_adapter.get_a_share_universe() or [])
        except Exception as exc:
            return {"available": False, "status": "degraded", "count": 0, "error": str(exc)}
        count = len(universe)
        return {
            "available": True,
            "status": "healthy" if count > 3000 else "degraded",
            "count": count,
            "expected_threshold": 3000,
        }
