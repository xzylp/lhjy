"""实盘执行异常告警与降级通知。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..infra.audit_store import StateStore
from .dispatcher import MessageDispatcher
from .templates import live_execution_alert_template
from .templates import _label_execution_reason, _label_execution_status

PRECHECK_ALERT_BLOCKERS = {
    "balance_unavailable",
    "market_snapshot_fetch_failed",
    "market_snapshot_unavailable",
    "market_snapshot_stale",
    "emergency_stop_active",
}

DISPATCH_ALERT_REASONS = {
    "execution_adapter_unavailable",
    "live_mock_fallback_blocked",
    "live_not_enabled",
    "emergency_stop_active",
}


@dataclass
class LiveExecutionAlertDispatchResult:
    dispatched: bool
    reason: str
    payload: dict


class LiveExecutionAlertNotifier:
    """实盘执行阶段异常摘要告警。"""

    def __init__(
        self,
        state_store: StateStore,
        dispatcher: MessageDispatcher | None = None,
        enabled: bool = True,
    ) -> None:
        self._state_store = state_store
        self._dispatcher = dispatcher
        self._enabled = enabled

    def dispatch_precheck(self, precheck: dict, force: bool = False) -> LiveExecutionAlertDispatchResult:
        summary = self._build_precheck_summary(precheck)
        if not summary["should_notify"]:
            return LiveExecutionAlertDispatchResult(False, "policy_skip", summary)
        return self._dispatch("precheck", summary, force=force)

    def dispatch_dispatch(self, execution_dispatch: dict, force: bool = False) -> LiveExecutionAlertDispatchResult:
        summary = self._build_dispatch_summary(execution_dispatch)
        if not summary["should_notify"]:
            return LiveExecutionAlertDispatchResult(False, "policy_skip", summary)
        return self._dispatch("dispatch", summary, force=force)

    def _dispatch(self, kind: str, summary: dict, force: bool = False) -> LiveExecutionAlertDispatchResult:
        if not self._enabled:
            return LiveExecutionAlertDispatchResult(False, "disabled", summary)
        if not self._dispatcher:
            return LiveExecutionAlertDispatchResult(False, "dispatcher_unavailable", summary)

        signature = self._build_signature(kind, summary)
        latest = self._state_store.get(f"last_live_execution_alert:{kind}")
        if not force and latest and latest.get("signature") == signature:
            return LiveExecutionAlertDispatchResult(False, "duplicate", summary)

        content = live_execution_alert_template(summary["title"], summary["lines"])
        dispatched = self._dispatcher.dispatch_live_execution_alert(
            summary["title"],
            content,
            level=summary["level"],
            force=True,
        )
        if not dispatched:
            return LiveExecutionAlertDispatchResult(False, "dispatch_failed", summary)

        record = {
            "kind": kind,
            "signature": signature,
            "title": summary["title"],
            "level": summary["level"],
            "trade_date": summary.get("trade_date"),
            "status": summary.get("status"),
            "reasons": summary.get("reasons", []),
        }
        history = self._state_store.get("live_execution_alert_history", [])
        history.append(record)
        self._state_store.set(f"last_live_execution_alert:{kind}", record)
        self._state_store.set("live_execution_alert_history", history[-50:])
        return LiveExecutionAlertDispatchResult(True, "sent", summary)

    @staticmethod
    def _build_signature(kind: str, summary: dict) -> str:
        raw = json.dumps(
            {
                "kind": kind,
                "title": summary.get("title"),
                "level": summary.get("level"),
                "trade_date": summary.get("trade_date"),
                "status": summary.get("status"),
                "reasons": summary.get("reasons", []),
                "items": summary.get("items", []),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _build_precheck_summary(precheck: dict) -> dict:
        trade_date = precheck.get("trade_date")
        blocked_items = [item for item in precheck.get("items", []) if not item.get("approved")]
        reasons = sorted(
            {
                blocker
                for item in blocked_items
                for blocker in item.get("blockers", [])
                if blocker in PRECHECK_ALERT_BLOCKERS
            }
        )
        if precheck.get("balance_error"):
            reasons.append("balance_error")
        reasons = list(dict.fromkeys(reasons))
        level = "critical" if any(reason in {"balance_error", "emergency_stop_active"} for reason in reasons) else "warning"
        lines = [
            f"交易日 {trade_date} 实盘预检异常，状态 {precheck.get('status')}，通过 {precheck.get('approved_count', 0)}，阻断 {precheck.get('blocked_count', 0)}。",
        ]
        minimum_total_invested_amount = precheck.get("minimum_total_invested_amount")
        if isinstance(minimum_total_invested_amount, (int, float)):
            lines.append(
                f"测试基线 {minimum_total_invested_amount}，逆回购 {precheck.get('reverse_repo_value', 0.0)} / {precheck.get('reverse_repo_reserved_amount', 0.0)}，"
                f"股票测试预算 {precheck.get('stock_test_budget_remaining', 0.0)} / {precheck.get('stock_test_budget_amount', 0.0)}。"
            )
        if precheck.get("degraded"):
            lines.append(
                f"已降级到 {precheck.get('degrade_to')}，原因 {precheck.get('degrade_reason') or 'unknown'}。"
            )
        if precheck.get("primary_recommended_next_action_label"):
            lines.append(f"建议动作: {precheck['primary_recommended_next_action_label']}。")
        if precheck.get("balance_error"):
            lines.append(f"账户查询失败: {precheck['balance_error']}")
        for item in blocked_items[:3]:
            item_reasons = [reason for reason in item.get("blockers", []) if reason in PRECHECK_ALERT_BLOCKERS]
            if not item_reasons:
                continue
            line = f"{item.get('symbol')} {item.get('name') or item.get('symbol')} 阻断: {','.join(_label_execution_reason(reason) for reason in item_reasons)}。"
            if item.get("primary_recommended_next_action_label"):
                line += f" 建议 {item['primary_recommended_next_action_label']}。"
            lines.append(line)
        return {
            "kind": "precheck",
            "should_notify": bool(reasons),
            "title": "实盘预检异常",
            "level": level,
            "trade_date": trade_date,
            "status": precheck.get("status"),
            "reasons": reasons,
            "items": [
                {
                    "symbol": item.get("symbol"),
                    "name": item.get("name"),
                    "blockers": [reason for reason in item.get("blockers", []) if reason in PRECHECK_ALERT_BLOCKERS],
                }
                for item in blocked_items
                if any(reason in PRECHECK_ALERT_BLOCKERS for reason in item.get("blockers", []))
            ],
            "lines": lines,
        }

    @staticmethod
    def _build_dispatch_summary(execution_dispatch: dict) -> dict:
        trade_date = execution_dispatch.get("trade_date")
        receipts = execution_dispatch.get("receipts", [])
        bad_receipts = [
            item
            for item in receipts
            if item.get("status") == "dispatch_failed"
            or item.get("reason") in DISPATCH_ALERT_REASONS
        ]
        reasons = list(
            dict.fromkeys(
                [item.get("reason") for item in bad_receipts if item.get("reason")]
            )
        )
        level = "critical" if any(item.get("status") == "dispatch_failed" for item in bad_receipts) else "warning"
        dispatch_status = execution_dispatch.get("status")
        lines = [
            f"交易日 {trade_date} 实盘派发异常，状态 {_label_execution_status(dispatch_status)}，提交 {execution_dispatch.get('submitted_count', 0)}，阻断 {execution_dispatch.get('blocked_count', 0)}。",
        ]
        if execution_dispatch.get("degraded"):
            lines.append(
                f"已降级到 {_label_execution_status(execution_dispatch.get('degrade_to'))}，原因 {_label_execution_reason(execution_dispatch.get('degrade_reason') or 'unknown')}。"
            )
        for item in bad_receipts[:3]:
            lines.append(
                f"{item.get('symbol')} {item.get('name') or item.get('symbol')} {_label_execution_status(item.get('status'))}，原因 {_label_execution_reason(item.get('reason'))}。"
            )
        return {
            "kind": "dispatch",
            "should_notify": bool(bad_receipts),
            "title": "实盘派发异常",
            "level": level,
            "trade_date": trade_date,
            "status": execution_dispatch.get("status"),
            "reasons": reasons,
            "items": [
                {
                    "symbol": item.get("symbol"),
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "reason": item.get("reason"),
                }
                for item in bad_receipts
            ],
            "lines": lines,
        }
