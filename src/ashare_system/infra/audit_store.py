"""审计日志存储 + 通用状态持久化"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from ..contracts import AuditRecord
from ..logging_config import get_logger

logger = get_logger("audit")


# ── 通用状态存储 ──────────────────────────────────────────

@dataclass
class StateStore:
    """通用 JSON 状态存储"""
    storage_path: Path
    data: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self.storage_path.exists():
            self.data = {}
            return
        self.data = json.loads(self.storage_path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self.storage_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, key: str, default=None):
        self._load()
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self._save()

    def delete(self, key: str) -> None:
        self.data.pop(key, None)
        self._save()


# ── 审计日志 ──────────────────────────────────────────────

@dataclass
class AuditStore:
    storage_path: Path
    records: list[AuditRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self.storage_path.exists():
            self.records = []
            return
        payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
        self.records = [AuditRecord(**item) for item in payload.get("records", [])]

    def _save(self) -> None:
        payload = {"records": [r.model_dump() for r in self.records]}
        self.storage_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def append(self, category: str, message: str, payload: dict | None = None) -> AuditRecord:
        record = AuditRecord(audit_id=f"audit-{uuid4().hex[:10]}", category=category, message=message, payload=payload or {})
        self.records.append(record)
        self._save()
        logger.info("[%s] %s", category, message)
        return record

    def recent(self, limit: int = 20) -> list[AuditRecord]:
        self._load()
        return list(reversed(self.records[-limit:]))
