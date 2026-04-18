"""serving 层只读访问。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .storage import ensure_storage_layout


class ServingStore:
    """统一读取 serving / feature 层最新产物。"""

    def __init__(self, storage_root: Path) -> None:
        self.layout = ensure_storage_layout(storage_root)

    def get_latest_market_context(self, as_of_time: str | None = None) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_market_context.json", as_of_time=as_of_time)

    def get_latest_event_context(self, as_of_time: str | None = None) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_event_context.json", as_of_time=as_of_time)

    def get_latest_symbol_contexts(self, as_of_time: str | None = None) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_symbol_contexts.json", as_of_time=as_of_time)

    def get_latest_dossier_pack(self, as_of_time: str | None = None) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_dossier_pack.json", as_of_time=as_of_time)

    def get_latest_discussion_context(self, as_of_time: str | None = None) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_discussion_context.json", as_of_time=as_of_time)

    def get_latest_monitor_context(self, as_of_time: str | None = None) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_monitor_context.json", as_of_time=as_of_time)

    def get_latest_runtime_context(self, as_of_time: str | None = None) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_runtime_context.json", as_of_time=as_of_time)

    def get_latest_workspace_context(self, as_of_time: str | None = None) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_workspace_context.json", as_of_time=as_of_time)

    def get_latest_offline_self_improvement_export(self, as_of_time: str | None = None) -> dict[str, Any] | None:
        return self._read_json(
            self.layout.serving_root / "latest_offline_self_improvement_export.json",
            as_of_time=as_of_time,
        )

    def get_latest_openclaw_packet(self, packet_type: str, as_of_time: str | None = None) -> dict[str, Any] | None:
        normalized_type = str(packet_type or "").strip()
        if not normalized_type:
            return None
        return self._read_json(self.layout.serving_root / f"latest_{normalized_type}.json", as_of_time=as_of_time)

    def get_dossier(self, trade_date: str, symbol: str, as_of_time: str | None = None) -> dict[str, Any] | None:
        return self._read_json(
            self.layout.features_dossiers_root / trade_date / f"{symbol.replace('.', '_')}.json",
            as_of_time=as_of_time,
        )

    def get_symbol_context(self, trade_date: str, symbol: str, as_of_time: str | None = None) -> dict[str, Any] | None:
        return self._read_json(
            self.layout.features_symbol_context_root / trade_date / f"{symbol.replace('.', '_')}.json",
            as_of_time=as_of_time,
        )

    @staticmethod
    def _read_json(path: Path, *, as_of_time: str | None = None) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = ServingStore._sanitize_json_compatible(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None
        if not ServingStore._is_available_as_of(payload, as_of_time):
            return None
        return payload

    @staticmethod
    def _is_available_as_of(payload: Any, as_of_time: str | None) -> bool:
        if not as_of_time or not isinstance(payload, dict):
            return True
        try:
            cutoff = ServingStore._parse_iso_datetime(as_of_time)
        except ValueError:
            return True
        if cutoff is None:
            return True
        candidates = [
            payload.get("generated_at"),
            payload.get("snapshot_at"),
            payload.get("trade_time"),
            payload.get("source_at"),
        ]
        for value in candidates:
            current = ServingStore._parse_iso_datetime(value)
            if current is not None:
                return current <= cutoff
        return True

    @staticmethod
    def _parse_iso_datetime(value: Any) -> Any:
        text = str(value or "").strip()
        if not text:
            return None
        normalized = text.replace(" ", "T")
        try:
            from datetime import datetime

            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    @staticmethod
    def _sanitize_json_compatible(value: Any) -> Any:
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, dict):
            return {key: ServingStore._sanitize_json_compatible(item) for key, item in value.items()}
        if isinstance(value, list):
            return [ServingStore._sanitize_json_compatible(item) for item in value]
        if isinstance(value, tuple):
            return [ServingStore._sanitize_json_compatible(item) for item in value]
        return value
