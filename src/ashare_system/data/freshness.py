"""统一 freshness / staleness 计算。"""

from __future__ import annotations

from datetime import datetime, timedelta

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

