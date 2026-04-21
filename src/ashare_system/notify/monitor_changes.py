"""监控池层结构化变化的摘要分发。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..monitor.persistence import MonitorStateService
from .dispatcher import MessageDispatcher
from .templates import monitor_change_summary_template


@dataclass
class MonitorChangeDispatchResult:
    dispatched: bool
    reason: str
    summary: dict


class MonitorChangeNotifier:
    """将 monitor state_change 事件汇总后按策略推送。"""

    def __init__(
        self,
        monitor_state_service: MonitorStateService,
        dispatcher: MessageDispatcher | None = None,
        event_limit: int = 20,
    ) -> None:
        self._monitor_state_service = monitor_state_service
        self._dispatcher = dispatcher
        self._event_limit = event_limit

    @staticmethod
    def _filter_summary(summary: dict) -> dict:
        items = [dict(item) for item in list(summary.get("items") or [])]
        important_alert_types = {"risk_gate_changed", "audit_gate_changed"}
        filtered_items = [
            item
            for item in items
            if str(item.get("alert_type") or "").strip() in important_alert_types
        ]
        if not filtered_items:
            return {
                **summary,
                "items": [],
                "lines": [],
                "count": 0,
                "notify_level": "none",
                "should_notify": False,
                "dispatch_title": "当前无需要飞书推送的池层变化",
            }
        filtered_type_counts: dict[str, int] = {}
        filtered_lines: list[str] = []
        for item in filtered_items:
            alert_type = str(item.get("alert_type") or "").strip()
            filtered_type_counts[alert_type] = filtered_type_counts.get(alert_type, 0) + 1
            message = str(item.get("message") or "").strip()
            label = "风控门变化" if alert_type == "risk_gate_changed" else "审计门变化"
            filtered_lines.append(f"[{label}] {message}")
        notify_level = "warning"
        if filtered_type_counts.get("risk_gate_changed", 0) > 0 and filtered_type_counts.get("audit_gate_changed", 0) > 0:
            notify_level = "critical"
        return {
            **summary,
            "items": filtered_items,
            "lines": filtered_lines,
            "count": len(filtered_items),
            "type_counts": filtered_type_counts,
            "dispatch_title": "风控/审计状态变化提醒",
            "notify_level": notify_level,
            "should_notify": True,
        }

    def dispatch_latest(self, force: bool = False) -> MonitorChangeDispatchResult:
        summary = self._monitor_state_service.build_change_summary(event_limit=self._event_limit)
        summary = self._filter_summary(summary)
        if not summary.get("items"):
            return MonitorChangeDispatchResult(False, "suppressed_non_critical", summary)
        if not self._dispatcher:
            return MonitorChangeDispatchResult(False, "dispatcher_unavailable", summary)
        if not force and not summary.get("should_notify", False):
            return MonitorChangeDispatchResult(False, "policy_skip", summary)

        signature = self._build_signature(summary)
        allowed, reason = self._monitor_state_service.should_dispatch_change_summary(signature, force=force)
        if not allowed:
            return MonitorChangeDispatchResult(False, reason, summary)

        content = monitor_change_summary_template(summary["dispatch_title"], summary["lines"])
        level = summary.get("notify_level", "info")
        dispatched = self._dispatcher.dispatch_monitor_changes(
            summary["dispatch_title"],
            content,
            level=level,
            force=force or level in {"critical", "warning"},
        )
        if dispatched:
            self._monitor_state_service.mark_change_summary_dispatched(signature, summary)
            return MonitorChangeDispatchResult(True, "sent", summary)
        return MonitorChangeDispatchResult(False, "dispatch_failed", summary)

    @staticmethod
    def _build_signature(summary: dict) -> str:
        payload = {
            "dispatch_title": summary.get("dispatch_title"),
            "notify_level": summary.get("notify_level"),
            "event_ids": [item.get("event_id") for item in summary.get("items", [])],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
