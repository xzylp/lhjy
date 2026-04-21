"""治理参数快照与回滚。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ..infra.safe_json import atomic_write_json, read_json_with_backup


class ParameterSnapshotService:
    def __init__(self, storage_path: Path, now_factory: Callable[[], datetime] | None = None) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_factory = now_factory or datetime.now

    def create_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        store = self._read_payload()
        items = list(store.get("items") or [])
        snapshot = {
            "snapshot_id": f"snap-{self._now_factory().strftime('%Y%m%d')}-{uuid4().hex[:8]}",
            "created_at": self._now_factory().isoformat(),
            **dict(payload or {}),
        }
        items.append(snapshot)
        cutoff = self._now_factory() - timedelta(days=30)
        items = [
            item
            for item in items
            if datetime.fromisoformat(str(item.get("created_at") or self._now_factory().isoformat())) >= cutoff
        ]
        store["items"] = items[-200:]
        store["latest_snapshot"] = snapshot
        atomic_write_json(self._storage_path, store)
        return snapshot

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        store = self._read_payload()
        for item in reversed(list(store.get("items") or [])):
            if str(item.get("snapshot_id") or "") == str(snapshot_id or ""):
                return dict(item)
        return {}

    def list_snapshots(self, limit: int = 20) -> list[dict[str, Any]]:
        store = self._read_payload()
        items = list(store.get("items") or [])
        return list(reversed(items[-max(int(limit or 20), 1) :]))

    def _read_payload(self) -> dict[str, Any]:
        payload = read_json_with_backup(self._storage_path, default={"items": []})
        return payload if isinstance(payload, dict) else {"items": []}
