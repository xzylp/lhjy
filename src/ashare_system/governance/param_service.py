"""动态参数服务。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, Field

from ..logging_config import get_logger
from .param_registry import EffectivePeriod, ParameterDefinition, ParameterRegistry
from .param_store import ParamChangeEvent, ParameterEventStore

logger = get_logger("governance.params")


class ParamProposalInput(BaseModel):
    event_type: str = "param_change"
    rollback_of_event_id: str | None = None
    param_key: str
    new_value: int | float | str | bool
    effective_period: EffectivePeriod | None = None
    proposed_by: str = "user"
    structured_by: str = "ashare"
    evaluated_by: list[str] = Field(default_factory=list)
    approved_by: str | None = None
    written_by: str | None = None
    reason: str = ""
    status: str = "proposed"
    source_layer: str = "agent_adjusted_params"
    effective_from: str | None = None
    effective_to: str | None = None
    source_filters: dict = Field(default_factory=dict)
    approval_policy_snapshot: dict = Field(default_factory=dict)
    rollback_baseline: dict = Field(default_factory=dict)
    observation_window: dict = Field(default_factory=dict)
    approval_ticket: dict = Field(default_factory=dict)


class ParameterService:
    """参数读取、提案与生效解析。"""

    def __init__(
        self,
        registry: ParameterRegistry,
        store: ParameterEventStore,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._now_factory = now_factory or datetime.now

    def list_params(self, as_of: str | None = None) -> list[dict]:
        reference_date = self._parse_date(as_of) if as_of else self._now_factory().date()
        active = self._active_events(reference_date)
        rows: list[dict] = []
        for definition in self._registry.list():
            event = active.get(definition.param_key)
            rows.append(
                {
                    "param_key": definition.param_key,
                    "scope": definition.scope,
                    "value_type": definition.value_type,
                    "default_value": definition.default_value,
                    "current_value": event.new_value if event else definition.default_value,
                    "allowed_range": definition.allowed_range,
                    "current_layer": event.source_layer if event else "system_defaults",
                    "effective_period_default": definition.effective_period_default,
                    "active_event_id": event.event_id if event else None,
                    "notes": definition.notes,
                }
            )
        return rows

    def list_proposals(self, status: str | None = None) -> list[ParamChangeEvent]:
        events = self._store.list_events()
        if status:
            events = [item for item in events if item.status == status]
        return list(reversed(events))

    def get_event(self, event_id: str) -> ParamChangeEvent | None:
        for item in self._store.list_events():
            if item.event_id == event_id:
                return item
        return None

    def get_param_value(self, param_key: str, as_of: str | None = None) -> int | float | str | bool:
        for item in self.list_params(as_of=as_of):
            if item["param_key"] == param_key:
                return item["current_value"]
        definition = self._registry.get(param_key)
        if not definition:
            raise KeyError(f"unknown parameter: {param_key}")
        return definition.default_value

    def propose_change(self, payload: ParamProposalInput) -> ParamChangeEvent:
        definition = self._require_definition(payload.param_key)
        self._validate_new_value(definition, payload.new_value)
        effective_period = payload.effective_period or definition.effective_period_default
        now = self._now_factory()
        old_value = self.get_param_value(payload.param_key)
        effective_from = payload.effective_from or self._default_effective_from(effective_period, now)
        status = self._resolve_status(payload.status, payload.approved_by, effective_from, now)
        observation_window = self._normalize_observation_window(
            payload.observation_window,
            event_type=payload.event_type,
            effective_from=effective_from,
            effective_to=payload.effective_to,
            approval_policy_snapshot=payload.approval_policy_snapshot,
        )
        approval_ticket = self._normalize_approval_ticket(
            payload.approval_ticket,
            status=status,
            approved_by=payload.approved_by,
            approval_policy_snapshot=payload.approval_policy_snapshot,
            now=now,
        )
        event = ParamChangeEvent(
            event_id=f"param-{now.strftime('%Y%m%d')}-{payload.param_key}-{uuid4().hex[:6]}",
            event_type=payload.event_type,
            rollback_of_event_id=payload.rollback_of_event_id,
            param_key=payload.param_key,
            scope=definition.scope,
            old_value=old_value,
            new_value=payload.new_value,
            allowed_range=definition.allowed_range,
            effective_period=effective_period,
            effective_from=effective_from,
            effective_to=payload.effective_to,
            source_layer=payload.source_layer,
            proposed_by=payload.proposed_by,
            structured_by=payload.structured_by,
            evaluated_by=payload.evaluated_by,
            approved_by=payload.approved_by,
            written_by=payload.written_by or payload.structured_by,
            reason=payload.reason,
            source_filters=payload.source_filters,
            approval_policy_snapshot=payload.approval_policy_snapshot,
            rollback_baseline=payload.rollback_baseline,
            observation_window=observation_window,
            approval_ticket=approval_ticket,
            status=status,
            created_at=now.isoformat(),
        )
        logger.info("记录参数提案: %s -> %s (%s)", payload.param_key, payload.new_value, status)
        return self._store.append(event)

    def review_event(
        self,
        event_id: str,
        *,
        action: str,
        approver: str,
        comment: str = "",
        effective_from: str | None = None,
    ) -> ParamChangeEvent:
        if action not in {"approve", "release", "reject"}:
            raise ValueError(f"unsupported review action: {action}")
        now = self._now_factory()
        events = self._store.list_events()
        for index, event in enumerate(events):
            if event.event_id != event_id:
                continue
            approval_ticket = dict(event.approval_ticket or {})
            comments = list(approval_ticket.get("comments") or [])
            if comment:
                comments.append(
                    {
                        "action": action,
                        "by": approver,
                        "comment": comment,
                        "at": now.isoformat(),
                    }
                )
            if approver and approver not in event.evaluated_by:
                event.evaluated_by.append(approver)
            approval_ticket.update(
                {
                    "required": bool(
                        approval_ticket.get("required")
                        or (event.approval_policy_snapshot or {}).get("required_confirmation") == "manual_review"
                    ),
                    "risk_level": approval_ticket.get("risk_level")
                    or (event.approval_policy_snapshot or {}).get("risk_level")
                    or "medium",
                    "required_approver": approval_ticket.get("required_approver")
                    or (event.approval_policy_snapshot or {}).get("required_approver")
                    or approver,
                    "comments": comments[-10:],
                    "reviewed_by": approver,
                    "reviewed_at": now.isoformat(),
                }
            )
            if action == "reject":
                event.status = "rejected"
                approval_ticket["state"] = "rejected"
            elif action == "release":
                event.effective_from = effective_from or now.date().isoformat()
                event.approved_by = approver
                event.status = "effective"
                approval_ticket["state"] = "released"
                approval_ticket["released_by"] = approver
                approval_ticket["released_at"] = now.isoformat()
            else:
                event.effective_from = effective_from or event.effective_from or self._default_effective_from(
                    event.effective_period,
                    now,
                )
                event.approved_by = approver
                event.status = self._resolve_status("approved", approver, event.effective_from, now)
                approval_ticket["state"] = "released" if event.status == "effective" else "approved"
                if event.status == "effective":
                    approval_ticket["released_by"] = approver
                    approval_ticket["released_at"] = now.isoformat()
            event.approval_ticket = approval_ticket
            events[index] = event
            self._store.save_events(events)
            logger.info("参数事件审批更新: %s -> %s", event_id, event.status)
            return event
        raise KeyError(f"unknown event: {event_id}")

    def _require_definition(self, param_key: str) -> ParameterDefinition:
        definition = self._registry.get(param_key)
        if definition is None:
            raise ValueError(f"unknown parameter: {param_key}")
        return definition

    def _active_events(self, reference_date) -> dict[str, ParamChangeEvent]:
        active: dict[str, ParamChangeEvent] = {}
        for event in self._store.list_events():
            if event.status not in {"approved", "effective"}:
                continue
            if not event.effective_from:
                continue
            start_date = self._parse_date(event.effective_from)
            if start_date > reference_date:
                continue
            if event.effective_period == "today_session" and start_date != reference_date:
                continue
            if event.effective_to and self._parse_date(event.effective_to) < reference_date:
                continue
            active[event.param_key] = event
        return active

    def _validate_new_value(self, definition: ParameterDefinition, value: int | float | str | bool) -> None:
        if definition.value_type == "integer" and not isinstance(value, int):
            raise ValueError(f"{definition.param_key} expects integer")
        if definition.value_type in {"number", "percent"} and not isinstance(value, (int, float)):
            raise ValueError(f"{definition.param_key} expects numeric value")
        if definition.value_type == "time" and not isinstance(value, str):
            raise ValueError(f"{definition.param_key} expects time string")
        if definition.value_type == "boolean" and not isinstance(value, bool):
            raise ValueError(f"{definition.param_key} expects boolean")

        allowed = definition.allowed_range
        if not allowed:
            return
        if definition.value_type in {"integer", "number", "percent"}:
            lower, upper = float(allowed[0]), float(allowed[1])
            numeric_value = float(value)
            if numeric_value < lower or numeric_value > upper:
                raise ValueError(f"{definition.param_key} out of allowed range")
            return
        if definition.value_type == "time":
            lower, upper = str(allowed[0]), str(allowed[1])
            if str(value) < lower or str(value) > upper:
                raise ValueError(f"{definition.param_key} out of allowed range")
            return
        if value not in allowed:
            raise ValueError(f"{definition.param_key} out of allowed range")

    def _resolve_status(self, requested_status: str, approved_by: str | None, effective_from: str, now: datetime) -> str:
        if requested_status not in {"proposed", "evaluating", "approved", "effective", "rejected"}:
            raise ValueError(f"unsupported status: {requested_status}")
        if requested_status in {"approved", "effective"} and not approved_by:
            raise ValueError("approved_by is required when proposal is approved")
        if requested_status == "effective":
            return "effective"
        if requested_status == "approved":
            effective_date = self._parse_date(effective_from)
            return "effective" if effective_date <= now.date() else "approved"
        return requested_status

    def _default_effective_from(self, effective_period: EffectivePeriod, now: datetime) -> str:
        today = now.date()
        if effective_period in {"today_session", "until_revoked"}:
            return today.isoformat()
        return self._next_weekday(today).isoformat()

    @staticmethod
    def _parse_date(value: str):
        return datetime.fromisoformat(value).date()

    @staticmethod
    def _next_weekday(value):
        probe = value + timedelta(days=1)
        while probe.weekday() >= 5:
            probe += timedelta(days=1)
        return probe

    def _normalize_observation_window(
        self,
        payload_window: dict,
        *,
        event_type: str,
        effective_from: str,
        effective_to: str | None,
        approval_policy_snapshot: dict,
    ) -> dict:
        window = dict(payload_window or {})
        start_date = str(window.get("start_date") or effective_from)
        start = self._parse_date(start_date)
        risk_level = str(approval_policy_snapshot.get("risk_level") or "medium")
        duration_days = int(window.get("duration_days") or (3 if risk_level == "high" else 2))
        expected_trade_count = int(window.get("expected_trade_count") or (2 if event_type == "param_rollback" else 1))
        end_date = str(window.get("end_date") or effective_to or self._shift_weekdays(start, max(duration_days - 1, 0)).isoformat())
        return {
            "stage": str(
                window.get("stage")
                or ("rollback_followup" if event_type == "param_rollback" else "proposal_observation")
            ),
            "start_date": start_date,
            "end_date": end_date,
            "duration_days": duration_days,
            "expected_trade_count": expected_trade_count,
            "review_after_date": str(window.get("review_after_date") or end_date),
            "risk_level": risk_level,
            "summary": str(
                window.get("summary")
                or ("回滚后继续观察同类样本是否修复。" if event_type == "param_rollback" else "提案生效后观察同类样本表现。")
            ),
        }

    @staticmethod
    def _normalize_approval_ticket(
        payload_ticket: dict,
        *,
        status: str,
        approved_by: str | None,
        approval_policy_snapshot: dict,
        now: datetime,
    ) -> dict:
        ticket = dict(payload_ticket or {})
        required = bool(
            ticket.get("required")
            or approval_policy_snapshot.get("required_confirmation") == "manual_review"
        )
        state = str(ticket.get("state") or ("pending" if required else "not_required"))
        if status == "rejected":
            state = "rejected"
        elif status == "effective":
            state = "released"
        elif status == "approved":
            state = "approved"
        normalized = {
            "required": required,
            "state": state,
            "risk_level": ticket.get("risk_level") or approval_policy_snapshot.get("risk_level") or "medium",
            "required_approver": ticket.get("required_approver")
            or approval_policy_snapshot.get("required_approver")
            or ("ashare-audit" if not required else "user+ashare-audit"),
            "comments": list(ticket.get("comments") or []),
        }
        if approved_by:
            normalized["reviewed_by"] = approved_by
            normalized["reviewed_at"] = now.isoformat()
        if status == "effective" and approved_by:
            normalized["released_by"] = approved_by
            normalized["released_at"] = now.isoformat()
        return normalized

    def _shift_weekdays(self, value, days: int):
        probe = value
        for _ in range(days):
            probe = self._next_weekday(probe)
        return probe
