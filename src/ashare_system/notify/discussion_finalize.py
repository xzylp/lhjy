"""讨论终审完成后的摘要分发。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..discussion.candidate_case import CandidateCaseService
from ..discussion.client_brief import build_client_brief_payload
from ..discussion.discussion_service import DiscussionCycleService
from ..infra.audit_store import StateStore
from .dispatcher import MessageDispatcher
from .templates import (
    discussion_client_brief_template,
)


@dataclass
class DiscussionFinalizeDispatchResult:
    dispatched: bool
    reason: str
    payload: dict


class DiscussionFinalizeNotifier:
    """在 discussion finalize 后分发最终推荐或阻断摘要。"""

    def __init__(
        self,
        candidate_case_service: CandidateCaseService,
        discussion_cycle_service: DiscussionCycleService,
        state_store: StateStore,
        dispatcher: MessageDispatcher | None = None,
    ) -> None:
        self._candidate_case_service = candidate_case_service
        self._discussion_cycle_service = discussion_cycle_service
        self._state_store = state_store
        self._dispatcher = dispatcher

    def dispatch(self, trade_date: str, force: bool = False) -> DiscussionFinalizeDispatchResult:
        reply_pack = self._candidate_case_service.build_reply_pack(trade_date)
        brief = self._candidate_case_service.build_final_brief(trade_date)
        cycle = self._discussion_cycle_service.get_cycle(trade_date)
        execution_precheck = self._state_store.get("latest_execution_precheck")
        execution_dispatch = self._state_store.get(f"execution_dispatch:{trade_date}")
        if not execution_dispatch:
            execution_dispatch = self._state_store.get("latest_execution_dispatch")
        if execution_precheck and execution_precheck.get("trade_date") != trade_date:
            execution_precheck = None
        if execution_dispatch and execution_dispatch.get("trade_date") != trade_date:
            execution_dispatch = None
        if cycle:
            brief["cycle"] = cycle.model_dump()
        if execution_precheck:
            brief["execution_precheck"] = execution_precheck
        if execution_dispatch:
            brief["execution_dispatch"] = execution_dispatch
        client_brief = build_client_brief_payload(
            trade_date=trade_date,
            reply_pack=reply_pack,
            final_brief=brief,
            execution_precheck=execution_precheck,
            execution_dispatch=execution_dispatch,
            cycle=(cycle.model_dump() if cycle else None),
        )
        result_payload = {
            "status": client_brief["status"],
            "selected_count": client_brief["selected_count"],
            "watchlist_count": client_brief["watchlist_count"],
            "rejected_count": client_brief["rejected_count"],
            "blockers": client_brief.get("blockers", []),
            "client_brief": client_brief,
            "final_brief": brief,
        }
        if not self._dispatcher:
            return DiscussionFinalizeDispatchResult(False, "dispatcher_unavailable", result_payload)

        signature = self._build_signature(
            trade_date,
            client_brief,
            cycle.model_dump() if cycle else None,
            execution_precheck,
            execution_dispatch,
        )
        latest = self._state_store.get("last_discussion_finalize_dispatch")
        if not force and latest and latest.get("signature") == signature:
            return DiscussionFinalizeDispatchResult(False, "duplicate", result_payload)

        title = "最终推荐已生成" if client_brief["status"] == "ready" else "最终推荐被阻断"
        content = discussion_client_brief_template(
            trade_date,
            client_brief["overview_lines"],
            client_brief.get("debate_focus_lines", []),
            client_brief.get("challenge_exchange_lines", []),
            client_brief.get("persuasion_summary_lines", []),
            client_brief["selected_lines"],
            client_brief["watchlist_lines"],
            client_brief["rejected_lines"],
            client_brief["execution_precheck_lines"],
            client_brief["execution_dispatch_lines"],
        )
        level = "info" if client_brief["status"] == "ready" else "warning"
        dispatched = self._dispatcher.dispatch_discussion_summary(title, content, level=level, force=True)
        if not dispatched:
            return DiscussionFinalizeDispatchResult(False, "dispatch_failed", result_payload)

        record = {
            "signature": signature,
            "trade_date": trade_date,
            "status": client_brief["status"],
            "selected_count": client_brief["selected_count"],
            "watchlist_count": client_brief["watchlist_count"],
            "rejected_count": client_brief["rejected_count"],
            "cycle_state": (cycle.discussion_state if cycle else None),
            "finalized_at": (cycle.finalized_at if cycle else None),
            "execution_precheck_status": (execution_precheck or {}).get("status"),
            "execution_precheck_approved_count": (execution_precheck or {}).get("approved_count"),
            "execution_dispatch_status": (execution_dispatch or {}).get("status"),
            "execution_dispatch_submitted_count": (execution_dispatch or {}).get("submitted_count"),
            "execution_dispatch_preview_count": (execution_dispatch or {}).get("preview_count"),
        }
        history = self._state_store.get("discussion_finalize_dispatch_history", [])
        history.append(record)
        self._state_store.set("last_discussion_finalize_dispatch", record)
        self._state_store.set("discussion_finalize_dispatch_history", history[-50:])
        return DiscussionFinalizeDispatchResult(True, "sent", result_payload)

    @staticmethod
    def _build_signature(
        trade_date: str,
        client_brief: dict,
        cycle: dict | None,
        execution_precheck: dict | None,
        execution_dispatch: dict | None,
    ) -> str:
        final_brief = client_brief.get("final_brief", {})
        payload = {
            "trade_date": trade_date,
            "status": client_brief.get("status"),
            "selected_symbols": [item.get("symbol") for item in final_brief.get("selected", [])],
            "watchlist_symbols": [item.get("symbol") for item in final_brief.get("watchlist", [])],
            "rejected_symbols": [item.get("symbol") for item in final_brief.get("rejected", [])],
            "blockers": client_brief.get("blockers", []),
            "cycle_state": (cycle or {}).get("discussion_state"),
            "finalized_at": (cycle or {}).get("finalized_at"),
            "execution_precheck_status": (execution_precheck or {}).get("status"),
            "execution_precheck_items": [
                {
                    "symbol": item.get("symbol"),
                    "approved": item.get("approved"),
                    "proposed_quantity": item.get("proposed_quantity"),
                    "blockers": item.get("blockers", []),
                }
                for item in (execution_precheck or {}).get("items", [])
            ],
            "execution_dispatch_status": (execution_dispatch or {}).get("status"),
            "execution_dispatch_receipts": [
                {
                    "symbol": item.get("symbol"),
                    "status": item.get("status"),
                    "reason": item.get("reason"),
                }
                for item in (execution_dispatch or {}).get("receipts", [])
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
