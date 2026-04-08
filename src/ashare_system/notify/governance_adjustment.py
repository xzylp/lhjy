"""自然语言治理调整的摘要分发。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..infra.audit_store import StateStore
from .dispatcher import MessageDispatcher
from .templates import governance_adjustment_template


@dataclass
class GovernanceAdjustmentDispatchResult:
    dispatched: bool
    reason: str


class GovernanceAdjustmentNotifier:
    def __init__(self, state_store: StateStore, dispatcher: MessageDispatcher | None = None) -> None:
        self._state_store = state_store
        self._dispatcher = dispatcher

    def dispatch(
        self,
        instruction: str,
        applied: bool,
        summary_lines: list[str],
        matched_items: list[dict],
        force: bool = False,
    ) -> GovernanceAdjustmentDispatchResult:
        if not self._dispatcher:
            return GovernanceAdjustmentDispatchResult(False, "dispatcher_unavailable")
        signature = self._build_signature(instruction, applied, matched_items)
        latest = self._state_store.get("last_governance_adjustment_dispatch")
        if not force and latest and latest.get("signature") == signature:
            return GovernanceAdjustmentDispatchResult(False, "duplicate")
        title = "自然语言调参已生效" if applied else "自然语言调参预览"
        content = governance_adjustment_template(title, instruction, summary_lines)
        level = "info" if applied else "warning"
        ok = self._dispatcher.dispatch_governance_update(title, content, level=level, force=True)
        if not ok:
            return GovernanceAdjustmentDispatchResult(False, "dispatch_failed")
        history = self._state_store.get("governance_adjustment_dispatch_history", [])
        record = {
            "signature": signature,
            "instruction": instruction,
            "applied": applied,
            "item_count": len(matched_items),
        }
        history.append(record)
        self._state_store.set("last_governance_adjustment_dispatch", record)
        self._state_store.set("governance_adjustment_dispatch_history", history[-50:])
        return GovernanceAdjustmentDispatchResult(True, "sent")

    @staticmethod
    def _build_signature(instruction: str, applied: bool, matched_items: list[dict]) -> str:
        payload = {
            "instruction": instruction,
            "applied": applied,
            "items": [
                {
                    "param_key": item.get("param_key"),
                    "new_value": item.get("new_value"),
                    "config_key": item.get("config_key"),
                    "config_value": item.get("config_value"),
                }
                for item in matched_items
            ],
        }
        return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
