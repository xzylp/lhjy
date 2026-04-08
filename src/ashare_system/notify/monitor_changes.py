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

    def dispatch_latest(self, force: bool = False) -> MonitorChangeDispatchResult:
        summary = self._monitor_state_service.build_change_summary(event_limit=self._event_limit)
        if not summary.get("items"):
            return MonitorChangeDispatchResult(False, "no_changes", summary)
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
