"""子系统熔断器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..infra.safe_json import atomic_write_json, read_json_with_backup


@dataclass
class CircuitBreakerState:
    subsystem: str
    status: str
    failure_count: int
    opened_at: str | None = None
    half_open_at: str | None = None
    last_error: str = ""


class CircuitBreakerRegistry:
    def __init__(
        self,
        storage_path: Path,
        *,
        failure_threshold: int = 3,
        open_seconds: int = 300,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.failure_threshold = max(int(failure_threshold or 3), 1)
        self.open_seconds = max(int(open_seconds or 300), 1)
        self._now_factory = now_factory or datetime.now

    def check(self, subsystem: str) -> dict[str, Any]:
        payload = self._read_payload()
        entry = dict((payload.get("items") or {}).get(subsystem) or {})
        now = self._now_factory()
        if not entry:
            return {"available": True, "status": "closed"}
        status = str(entry.get("status") or "closed")
        opened_at = self._parse_time(str(entry.get("opened_at") or ""))
        if status == "open" and opened_at is not None and now - opened_at >= timedelta(seconds=self.open_seconds):
            entry["status"] = "half_open"
            entry["half_open_at"] = now.isoformat()
            payload.setdefault("items", {})[subsystem] = entry
            atomic_write_json(self._storage_path, payload)
            return {"available": True, "status": "half_open", "cached_result": entry.get("last_success_payload")}
        if status == "open":
            return {"available": False, "status": "open", "cached_result": entry.get("last_success_payload")}
        return {"available": True, "status": status, "cached_result": entry.get("last_success_payload")}

    def record_success(self, subsystem: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = self._read_payload()
        items = dict(payload.get("items") or {})
        items[subsystem] = {
            "status": "closed",
            "failure_count": 0,
            "last_success_payload": dict(result or {}),
            "updated_at": self._now_factory().isoformat(),
            "last_error": "",
        }
        payload["items"] = items
        atomic_write_json(self._storage_path, payload)
        return dict(items[subsystem])

    def record_failure(self, subsystem: str, error: str, cached_result: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = self._read_payload()
        items = dict(payload.get("items") or {})
        current = dict(items.get(subsystem) or {})
        failure_count = int(current.get("failure_count", 0) or 0) + 1
        status = "open" if failure_count >= self.failure_threshold else "closed"
        now = self._now_factory().isoformat()
        items[subsystem] = {
            **current,
            "status": status,
            "failure_count": failure_count,
            "opened_at": (now if status == "open" else current.get("opened_at")),
            "last_error": str(error or ""),
            "last_success_payload": dict(cached_result or current.get("last_success_payload") or {}),
            "updated_at": now,
        }
        payload["items"] = items
        atomic_write_json(self._storage_path, payload)
        return dict(items[subsystem])

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _read_payload(self) -> dict[str, Any]:
        payload = read_json_with_backup(self._storage_path, default={"items": {}})
        return payload if isinstance(payload, dict) else {"items": {}}
