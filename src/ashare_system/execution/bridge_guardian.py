"""执行桥健康守护。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

import httpx

from ..infra.audit_store import StateStore
from ..monitor.persistence import MonitorStateService
from ..notify.dispatcher import MessageDispatcher
from ..settings import AppSettings


def check(
    settings: AppSettings,
    runtime_state_store: StateStore | None,
    monitor_state_service: MonitorStateService | None,
    dispatcher: MessageDispatcher | None = None,
    *,
    now_factory: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    now = (now_factory or datetime.now)()
    latest_payload = monitor_state_service.get_latest_execution_bridge_health() if monitor_state_service else {}
    health = dict((latest_payload or {}).get("health") or {})
    reported_at_text = str(health.get("reported_at") or "").strip()
    reported_at = _parse_iso_datetime(reported_at_text)
    age_seconds = None
    if reported_at is not None:
        left, right = _normalize_datetime_pair(now, reported_at)
        age_seconds = max((left - right).total_seconds(), 0.0)
    status = "healthy"
    blocked = False
    probe: dict[str, Any] = {}
    summary_lines = []
    if age_seconds is None:
        status = "warning"
        summary_lines.append("执行桥健康上报缺失。")
    elif age_seconds > 300:
        status = "bridge_stale"
        blocked = True
        summary_lines.append(f"执行桥健康已超时 {round(age_seconds, 1)}s，进入阻断态。")
        probe = _probe_go_platform_health(settings)
        if probe.get("ok"):
            blocked = False
            status = "degraded"
            summary_lines.append("go_platform /health 可达，桥接改为降级告警。")
        else:
            summary_lines.append(f"go_platform /health 失败: {probe.get('detail') or 'unknown'}")
    elif age_seconds > 120:
        status = "warning"
        summary_lines.append(f"执行桥健康已延迟 {round(age_seconds, 1)}s，需尽快检查 Windows Gateway。")
    else:
        summary_lines.append(f"执行桥健康正常，最近上报延迟 {round(age_seconds, 1)}s。")

    payload = {
        "status": status,
        "blocked": blocked,
        "generated_at": now.isoformat(),
        "reported_at": reported_at_text,
        "age_seconds": age_seconds,
        "probe": probe,
        "summary_lines": summary_lines,
    }
    if runtime_state_store is not None:
        runtime_state_store.set("latest_bridge_guardian", payload)
        runtime_state_store.set("bridge_dispatch_blocked", blocked)
    if dispatcher is not None and status in {"warning", "bridge_stale"}:
        try:
            dispatcher.dispatch_alert("\n".join(["执行桥健康守护告警", *summary_lines]))
        except Exception:
            pass
    return payload


def _probe_go_platform_health(settings: AppSettings) -> dict[str, Any]:
    if not bool(getattr(settings.go_platform, "enabled", False)):
        return {"ok": False, "detail": "go_platform_disabled"}
    base_url = str(getattr(settings.go_platform, "base_url", "") or "").strip().rstrip("/")
    if not base_url:
        return {"ok": False, "detail": "go_platform_base_url_missing"}
    try:
        with httpx.Client(timeout=min(max(float(settings.go_platform.timeout_sec or 2.0), 1.0), 5.0)) as client:
            response = client.get(f"{base_url}/health")
        return {"ok": response.status_code == 200, "status_code": response.status_code, "detail": response.text[:160]}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalize_datetime_pair(left: datetime, right: datetime) -> tuple[datetime, datetime]:
    if (left.tzinfo is None) == (right.tzinfo is None):
        return left, right
    if left.tzinfo is not None:
        return left.replace(tzinfo=None), right
    return left, right.replace(tzinfo=None)

