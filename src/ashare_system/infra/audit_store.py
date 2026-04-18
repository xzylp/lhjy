"""审计日志存储 + 通用状态持久化"""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from ..contracts import AuditRecord
from ..logging_config import get_logger

logger = get_logger("audit")


def _lock_path_for(storage_path: Path) -> Path:
    return storage_path.with_name(f"{storage_path.name}.lock")


@contextmanager
def _file_lock(storage_path: Path):
    lock_path = _lock_path_for(storage_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load_json_payload(storage_path: Path, default):
    if not storage_path.exists():
        return default
    raw = storage_path.read_text(encoding="utf-8")
    if not raw.strip():
        return default
    return json.loads(raw)


def _atomic_write_json(storage_path: Path, payload) -> None:
    tmp_path = storage_path.with_name(f".{storage_path.name}.{uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(storage_path)


# ── 通用状态存储 ──────────────────────────────────────────

@dataclass
class StateStore:
    """通用 JSON 状态存储"""
    storage_path: Path
    data: dict = field(default_factory=dict)
    _cached_mtime_ns: int | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        with _file_lock(self.storage_path):
            self.data = _load_json_payload(self.storage_path, {})
            self._cached_mtime_ns = self._stat_mtime_ns()

    def _save(self) -> None:
        with _file_lock(self.storage_path):
            latest = _load_json_payload(self.storage_path, {})
            if isinstance(latest, dict):
                latest.update(self.data)
                self.data = latest
            _atomic_write_json(self.storage_path, self.data)
            self._cached_mtime_ns = self._stat_mtime_ns()

    def _stat_mtime_ns(self) -> int | None:
        try:
            return self.storage_path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def _ensure_loaded(self) -> None:
        current_mtime_ns = self._stat_mtime_ns()
        if self.data and self._cached_mtime_ns == current_mtime_ns:
            return
        self.data = _load_json_payload(self.storage_path, {})
        self._cached_mtime_ns = current_mtime_ns

    def get(self, key: str, default=None):
        with _file_lock(self.storage_path):
            self._ensure_loaded()
            return self.data.get(key, default)

    def get_many(self, keys: list[str]) -> dict[str, object]:
        with _file_lock(self.storage_path):
            self._ensure_loaded()
            return {key: self.data.get(key) for key in keys}

    def set(self, key: str, value) -> None:
        with _file_lock(self.storage_path):
            self._ensure_loaded()
            self.data[key] = value
            _atomic_write_json(self.storage_path, self.data)
            self._cached_mtime_ns = self._stat_mtime_ns()

    def delete(self, key: str) -> None:
        with _file_lock(self.storage_path):
            self._ensure_loaded()
            self.data.pop(key, None)
            _atomic_write_json(self.storage_path, self.data)
            self._cached_mtime_ns = self._stat_mtime_ns()


# ── 审计日志 ──────────────────────────────────────────────

@dataclass
class AuditStore:
    storage_path: Path
    records: list[AuditRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        with _file_lock(self.storage_path):
            payload = _load_json_payload(self.storage_path, {"records": []})
            self.records = [AuditRecord(**item) for item in payload.get("records", [])]

    def _save(self) -> None:
        payload = {"records": [r.model_dump() for r in self.records]}
        with _file_lock(self.storage_path):
            _atomic_write_json(self.storage_path, payload)

    def append(self, category: str, message: str, payload: dict | None = None) -> AuditRecord:
        record = AuditRecord(audit_id=f"audit-{uuid4().hex[:10]}", category=category, message=message, payload=payload or {})
        with _file_lock(self.storage_path):
            latest_payload = _load_json_payload(self.storage_path, {"records": []})
            self.records = [AuditRecord(**item) for item in latest_payload.get("records", [])]
            self.records.append(record)
            _atomic_write_json(
                self.storage_path,
                {"records": [item.model_dump() for item in self.records]},
            )
        logger.info("[%s] %s", category, message)
        return record

    def recent(self, limit: int = 20) -> list[AuditRecord]:
        with _file_lock(self.storage_path):
            payload = _load_json_payload(self.storage_path, {"records": []})
            self.records = [AuditRecord(**item) for item in payload.get("records", [])]
            return list(reversed(self.records[-limit:]))
