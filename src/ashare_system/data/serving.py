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

    def get_latest_market_context(self) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_market_context.json")

    def get_latest_event_context(self) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_event_context.json")

    def get_latest_symbol_contexts(self) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_symbol_contexts.json")

    def get_latest_dossier_pack(self) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_dossier_pack.json")

    def get_latest_discussion_context(self) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_discussion_context.json")

    def get_latest_monitor_context(self) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_monitor_context.json")

    def get_latest_runtime_context(self) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_runtime_context.json")

    def get_latest_workspace_context(self) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_workspace_context.json")

    def get_latest_offline_self_improvement_export(self) -> dict[str, Any] | None:
        return self._read_json(self.layout.serving_root / "latest_offline_self_improvement_export.json")

    def get_latest_openclaw_packet(self, packet_type: str) -> dict[str, Any] | None:
        normalized_type = str(packet_type or "").strip()
        if not normalized_type:
            return None
        return self._read_json(self.layout.serving_root / f"latest_{normalized_type}.json")

    def get_dossier(self, trade_date: str, symbol: str) -> dict[str, Any] | None:
        return self._read_json(self.layout.features_dossiers_root / trade_date / f"{symbol.replace('.', '_')}.json")

    def get_symbol_context(self, trade_date: str, symbol: str) -> dict[str, Any] | None:
        return self._read_json(self.layout.features_symbol_context_root / trade_date / f"{symbol.replace('.', '_')}.json")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            return ServingStore._sanitize_json_compatible(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
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
