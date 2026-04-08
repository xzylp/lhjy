"""参数事件持久化。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


ParamEventStatus = Literal["proposed", "evaluating", "approved", "rejected", "effective", "expired", "revoked"]


class ParamChangeEvent(BaseModel):
    event_id: str
    event_type: str = "param_change"
    rollback_of_event_id: str | None = None
    param_key: str
    scope: str
    old_value: int | float | str | bool
    new_value: int | float | str | bool
    allowed_range: list[int | float | str] = Field(default_factory=list)
    effective_period: str
    effective_from: str | None = None
    effective_to: str | None = None
    source_layer: str = "agent_adjusted_params"
    proposed_by: str = "user"
    structured_by: str = "ashare"
    evaluated_by: list[str] = Field(default_factory=list)
    approved_by: str | None = None
    written_by: str | None = None
    reason: str = ""
    source_filters: dict = Field(default_factory=dict)
    approval_policy_snapshot: dict = Field(default_factory=dict)
    rollback_baseline: dict = Field(default_factory=dict)
    observation_window: dict = Field(default_factory=dict)
    approval_ticket: dict = Field(default_factory=dict)
    status: ParamEventStatus = "proposed"
    created_at: str


class ParameterEventStore:
    """参数变更事件文件存储。"""

    def __init__(self, storage_path: Path) -> None:
        self.storage_path = storage_path
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)

    def list_events(self) -> list[ParamChangeEvent]:
        if not self.storage_path.exists():
            return []
        payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
        return [ParamChangeEvent(**item) for item in payload.get("events", [])]

    def append(self, event: ParamChangeEvent) -> ParamChangeEvent:
        events = self.list_events()
        events.append(event)
        self.save_events(events)
        return event

    def save_events(self, events: list[ParamChangeEvent]) -> None:
        payload = {"events": [item.model_dump() for item in events]}
        self.storage_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
