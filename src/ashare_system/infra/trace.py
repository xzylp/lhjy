"""交易主链 trace。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..infra.safe_json import atomic_write_json, read_json_with_backup


class TradeTraceService:
    def __init__(self, storage_path: Path, now_factory: Callable[[], datetime] | None = None) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_factory = now_factory or datetime.now

    def append_event(
        self,
        *,
        trace_id: str,
        stage: str,
        payload: dict[str, Any] | None = None,
        trade_date: str | None = None,
    ) -> dict[str, Any]:
        if not str(trace_id or "").strip():
            return {}
        data = self._read_payload()
        traces = dict(data.get("traces") or {})
        trace = dict(traces.get(trace_id) or {"trace_id": trace_id, "events": []})
        event = {
            "stage": stage,
            "recorded_at": self._now_factory().isoformat(),
            "payload": dict(payload or {}),
        }
        trace["trade_date"] = trade_date or trace.get("trade_date") or str(event["recorded_at"])[:10]
        trace["updated_at"] = event["recorded_at"]
        events = list(trace.get("events") or [])
        events.append(event)
        trace["events"] = events[-300:]
        traces[trace_id] = trace
        data["traces"] = traces
        data["latest_trace_id"] = trace_id
        atomic_write_json(self._storage_path, data)
        return trace

    def get_trace(self, trace_id: str) -> dict[str, Any]:
        traces = dict(self._read_payload().get("traces") or {})
        trace = dict(traces.get(trace_id) or {})
        if not trace:
            return {}
        trace["event_count"] = len(list(trace.get("events") or []))
        return trace

    def latest_trace_id(self) -> str | None:
        payload = self._read_payload()
        trace_id = str(payload.get("latest_trace_id") or "").strip()
        return trace_id or None

    def _read_payload(self) -> dict[str, Any]:
        payload = read_json_with_backup(self._storage_path, default={"traces": {}})
        return payload if isinstance(payload, dict) else {"traces": {}}
