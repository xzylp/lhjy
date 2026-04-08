"""系统管理 API"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..backtest.attribution import OFFLINE_BACKTEST_ATTRIBUTION_NOTE
from ..backtest.playbook_runner import OFFLINE_SELF_IMPROVEMENT_NOTE, PlaybookBacktestRunner
from ..account_state import AccountStateService
from ..contracts import (
    ExecutionGatewayClaimInput,
    ExecutionGatewayReceiptInput,
    PlaceOrderRequest,
)
from ..data.archive import DataArchiveStore
from ..data.serving import ServingStore
from ..discussion.candidate_case import CandidateCase, CandidateCaseService, CandidateOpinion
from ..discussion.client_brief import build_client_brief_payload
from ..discussion.discussion_service import DiscussionCycleService
from ..discussion.protocol import (
    build_agent_packets_envelope,
    build_finalize_packet_envelope,
    build_meeting_context_envelope,
)
from ..execution_reconciliation import ExecutionReconciliationService
from ..execution_gateway import (
    EXECUTION_GATEWAY_PENDING_PATH,
    append_execution_intent_history as append_gateway_execution_intent_history,
    build_execution_gateway_intent_packet,
    enqueue_execution_gateway_intent,
    get_execution_intent_history as get_gateway_execution_intent_history,
    get_pending_execution_intents as get_gateway_pending_execution_intents,
    resolve_execution_gateway_state_store,
    save_pending_execution_intents as save_gateway_pending_execution_intents,
)
from ..execution_safety import (
    is_limit_down,
    is_limit_up,
    is_price_deviation_exceeded,
    is_snapshot_fresh,
    is_trading_session,
    snapshot_age_seconds,
)
from ..governance.inspection import (
    build_observation_window_status,
    collect_parameter_hint_inspection,
    is_pending_high_risk_rollback,
)
from ..governance.nl_adjustment import NaturalLanguageAdjustmentInterpreter
from ..governance.param_service import ParameterService, ParamProposalInput
from ..infra.adapters import ExecutionAdapter
from ..infra.audit_store import AuditStore, StateStore
from ..infra.healthcheck import EnvironmentHealthcheck
from ..learning.score_state import AgentScoreService
from ..learning.attribution import TradeAttributionRecord, TradeAttributionService
from ..learning.settlement import AgentScoreSettlementService, SettlementSymbolOutcome
from ..monitor.persistence import (
    MonitorStateService,
    build_execution_bridge_health_client_template,
    build_execution_bridge_health_deployment_contract_sample,
    build_execution_bridge_health_ingress_payload,
    get_execution_bridge_health_latest_descriptor,
)
from ..notify.dispatcher import MessageDispatcher
from ..notify.discussion_finalize import DiscussionFinalizeNotifier
from ..notify.governance_adjustment import GovernanceAdjustmentNotifier
from ..notify.live_execution_alerts import LiveExecutionAlertNotifier
from ..notify.monitor_changes import MonitorChangeNotifier
from ..notify.templates import (
    execution_dispatch_notification_template,
    execution_dispatch_summary_lines,
    execution_order_event_template,
)
from ..pending_order_inspection import PendingOrderInspectionService
from ..pending_order_remediation import PendingOrderRemediationService
from ..portfolio import build_test_trading_budget, summarize_position_buckets
from ..precompute import DossierPrecomputeService
from ..reverse_repo import ReverseRepoService
from ..risk.guard import ExecutionGuard
from ..runtime_config import RuntimeConfigManager
from ..scheduler import run_tail_market_scan
from ..settings import AppSettings
from ..startup_recovery import StartupRecoveryService


def _normalize_confidence(value: Any) -> str:
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 1:
            numeric = numeric / 100.0
        if numeric >= 0.75:
            return "high"
        if numeric >= 0.45:
            return "medium"
        return "low"

    text = str(value or "").strip().lower()
    alias_map = {
        "high": "high",
        "strong": "high",
        "very_high": "high",
        "medium": "medium",
        "mid": "medium",
        "moderate": "medium",
        "neutral": "medium",
        "low": "low",
        "weak": "low",
    }
    if text in alias_map:
        return alias_map[text]
    try:
        return _normalize_confidence(float(text))
    except ValueError as exc:
        raise ValueError(f"unsupported confidence: {value}") from exc


def _normalize_stance(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    alias_map = {
        "selected": "support",
        "select": "support",
        "support": "support",
        "approve": "support",
        "approved": "support",
        "allow": "support",
        "bullish": "support",
        "watchlist": "watch",
        "watch": "watch",
        "neutral": "watch",
        "limit": "limit",
        "conditional": "limit",
        "hold": "hold",
        "question": "question",
        "ask": "question",
        "query": "question",
        "rejected": "rejected",
        "reject": "rejected",
        "oppose": "rejected",
        "against": "rejected",
        "bearish": "rejected",
        "block": "rejected",
    }
    normalized = alias_map.get(text)
    if not normalized:
        raise ValueError(f"unsupported stance: {value}")
    return normalized


def _sanitize_json_compatible(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _sanitize_json_compatible(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_compatible(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_json_compatible(item) for item in value]
    return value


_EXECUTION_BLOCKER_PRIORITY = {
    "emergency_stop_active": 0,
    "balance_unavailable": 1,
    "market_snapshot_fetch_failed": 2,
    "market_price_unavailable": 3,
    "stock_test_budget_reached": 4,
    "total_position_limit_reached": 5,
    "single_position_limit_reached": 6,
    "cash_unavailable": 7,
    "budget_below_min_lot": 8,
    "order_lot_insufficient": 9,
    "risk_gate_reject": 10,
    "audit_gate_hold": 11,
    "trading_session_closed": 12,
    "market_snapshot_unavailable": 13,
    "market_snapshot_stale": 14,
    "limit_up_locked": 15,
    "limit_down_locked": 16,
    "price_deviation_exceeded": 17,
    "cash_exceeded": 18,
    "single_amount_exceeded": 19,
    "guard_reject": 20,
}

_EXECUTION_BLOCKER_LABELS = {
    "emergency_stop_active": "交易总开关已暂停",
    "balance_unavailable": "账户资金不可用",
    "market_snapshot_fetch_failed": "实时行情抓取失败",
    "market_price_unavailable": "行情价格不可用",
    "stock_test_budget_reached": "股票测试预算已用满",
    "total_position_limit_reached": "总仓位上限已占满",
    "single_position_limit_reached": "单票仓位上限已占满",
    "cash_unavailable": "可用现金不足",
    "budget_below_min_lot": "预算不足一手",
    "order_lot_insufficient": "下单股数不足一手",
    "risk_gate_reject": "风控闸门拒绝",
    "audit_gate_hold": "审计闸门未放行",
    "trading_session_closed": "当前非交易时段",
    "market_snapshot_unavailable": "实时行情快照缺失",
    "market_snapshot_stale": "实时行情快照过期",
    "limit_up_locked": "涨停无法买入",
    "limit_down_locked": "跌停锁死",
    "price_deviation_exceeded": "买卖价偏离过大",
    "cash_exceeded": "下单金额超过可用现金",
    "single_amount_exceeded": "下单金额超过单票上限",
    "guard_reject": "执行守卫拒绝",
}

_EXECUTION_NEXT_ACTION_PRIORITY = {
    "resume_trading_switch": 0,
    "repair_account_connection": 1,
    "refresh_market_data": 2,
    "reduce_existing_positions": 3,
    "reduce_symbol_position": 4,
    "review_risk_and_audit": 5,
    "retry_when_market_opens": 6,
    "wait_for_better_price": 7,
    "prepare_cash_or_reduce_budget": 8,
}

_EXECUTION_NEXT_ACTION_LABELS = {
    "resume_trading_switch": "恢复交易总开关后再试",
    "repair_account_connection": "修复账户连接后再试",
    "refresh_market_data": "刷新实时行情后再试",
    "reduce_existing_positions": "先减仓再重试",
    "reduce_symbol_position": "先降低该票仓位再重试",
    "review_risk_and_audit": "先处理风控/审计阻断",
    "retry_when_market_opens": "开盘后重新预检",
    "wait_for_better_price": "等待价格恢复到可成交区间",
    "prepare_cash_or_reduce_budget": "补充现金或降低单笔预算",
}


def _sort_execution_blockers(blockers: list[str]) -> list[str]:
    unique = list(dict.fromkeys(blockers))
    return sorted(unique, key=lambda item: (_EXECUTION_BLOCKER_PRIORITY.get(item, 999), item))


def _pick_execution_degrade_reason(items: list[dict[str, Any]]) -> str:
    blockers = [
        blocker
        for item in items
        for blocker in item.get("blockers", [])
        if blocker
    ]
    ordered = _sort_execution_blockers(blockers)
    return ordered[0] if ordered else "no_approved_candidates"


def _execution_blocker_label(code: str) -> str:
    return _EXECUTION_BLOCKER_LABELS.get(code, code)


def _execution_blocker_next_actions(code: str) -> list[str]:
    mapping = {
        "emergency_stop_active": ["resume_trading_switch"],
        "balance_unavailable": ["repair_account_connection"],
        "market_snapshot_fetch_failed": ["refresh_market_data"],
        "market_snapshot_unavailable": ["refresh_market_data"],
        "market_snapshot_stale": ["refresh_market_data"],
        "market_price_unavailable": ["refresh_market_data"],
        "stock_test_budget_reached": ["reduce_existing_positions"],
        "total_position_limit_reached": ["reduce_existing_positions"],
        "single_position_limit_reached": ["reduce_symbol_position"],
        "cash_unavailable": ["prepare_cash_or_reduce_budget"],
        "budget_below_min_lot": ["prepare_cash_or_reduce_budget"],
        "order_lot_insufficient": ["prepare_cash_or_reduce_budget"],
        "risk_gate_reject": ["review_risk_and_audit"],
        "audit_gate_hold": ["review_risk_and_audit"],
        "guard_reject": ["review_risk_and_audit"],
        "trading_session_closed": ["retry_when_market_opens"],
        "limit_up_locked": ["wait_for_better_price"],
        "limit_down_locked": ["wait_for_better_price"],
        "price_deviation_exceeded": ["wait_for_better_price"],
        "cash_exceeded": ["prepare_cash_or_reduce_budget"],
        "single_amount_exceeded": ["prepare_cash_or_reduce_budget"],
    }
    return mapping.get(code, [])


def _build_execution_next_actions(blockers: list[str]) -> list[dict[str, str]]:
    codes = []
    for blocker in _sort_execution_blockers(blockers):
        codes.extend(_execution_blocker_next_actions(blocker))
    ordered_codes = sorted(
        dict.fromkeys(codes),
        key=lambda item: (_EXECUTION_NEXT_ACTION_PRIORITY.get(item, 999), item),
    )
    return [
        {
            "code": code,
            "label": _EXECUTION_NEXT_ACTION_LABELS.get(code, code),
        }
        for code in ordered_codes
    ]


def _build_execution_next_action_lines(actions: list[dict[str, str]]) -> list[str]:
    if not actions:
        return []
    return [f"建议动作: {'；'.join(action['label'] for action in actions)}。"]


def _clamp_numeric(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def _infer_parameter_hint_value(param_row: dict[str, Any], direction: str) -> int | float | str | bool:
    value_type = param_row.get("value_type")
    current_value = param_row.get("current_value")
    allowed_range = list(param_row.get("allowed_range") or [])
    if value_type == "integer":
        current_int = int(current_value)
        lower = int(allowed_range[0]) if allowed_range else current_int
        upper = int(allowed_range[1]) if allowed_range else current_int
        step = max(1, int(round(abs(current_int) * 0.2)))
        candidate = current_int + step if direction == "increase" else current_int - step
        if candidate == current_int:
            candidate = current_int + (1 if direction == "increase" else -1)
        return int(_clamp_numeric(candidate, lower, upper))
    if value_type in {"number", "percent"}:
        current_float = float(current_value)
        lower = float(allowed_range[0]) if allowed_range else current_float
        upper = float(allowed_range[1]) if allowed_range else current_float
        step = max(abs(current_float) * 0.1, 0.005 if value_type == "percent" else 1.0)
        candidate = current_float + step if direction == "increase" else current_float - step
        if abs(candidate - current_float) < 1e-12:
            candidate = current_float + (0.005 if direction == "increase" else -0.005)
        return round(_clamp_numeric(candidate, lower, upper), 4)
    return current_value


def _build_parameter_hint_approval_policy(param_row: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    scope = str(param_row.get("scope") or "")
    effective_period_default = str(param_row.get("effective_period_default") or "")
    param_key = str(param_row.get("param_key") or "")
    sample_count = int(hint.get("sample_count", 0) or 0)
    auto_approvable = scope in {"monitor", "intraday"} and effective_period_default == "today_session"
    risk_level = "low"
    rationale = "盘中/监控型短周期参数，适合快速试错。"
    if scope == "intraday":
        risk_level = "medium"
        rationale = "盘中交易参数会直接影响快进快出节奏，建议在样本足够时再自动批准。"
    if scope == "risk" or effective_period_default == "until_revoked":
        auto_approvable = False
        risk_level = "high"
        rationale = "风险或长期参数会影响全局仓位与暴露，默认要求人工确认。"
    if param_key in {"sector_exposure_limit", "max_total_position", "equity_position_limit", "max_single_position"}:
        auto_approvable = False
        risk_level = "high"
        rationale = "仓位/暴露上限属于核心风控参数，默认不自动批准。"
    if sample_count < 2 and not auto_approvable:
        rationale += " 当前样本仍偏少。"
    return {
        "auto_approvable": auto_approvable,
        "risk_level": risk_level,
        "required_confirmation": ("optional" if auto_approvable else "manual_review"),
        "recommended_status": ("approved" if auto_approvable else "evaluating"),
        "recommended_effective_period": (
            "today_session" if auto_approvable else (effective_period_default or "next_trading_day")
        ),
        "required_approver": ("ashare-audit" if auto_approvable else "user+ashare-audit"),
        "rationale": rationale,
    }


def _build_parameter_hint_rollback_baseline(
    param_row: dict[str, Any],
    proposed_value: int | float | str | bool,
    hint: dict[str, Any],
) -> dict[str, Any]:
    return {
        "restore_value": param_row.get("current_value"),
        "current_layer": param_row.get("current_layer"),
        "active_event_id": param_row.get("active_event_id"),
        "rollback_trigger": f"若 {hint.get('param_key')} 调整后同类 review_tag 表现继续恶化，则回滚到当前值。",
        "rollback_reason": "保留本次调整前的参数值作为最小回滚锚点。",
        "proposed_value": proposed_value,
    }


def _build_post_rollback_operation_targets(
    rollback_event: dict[str, Any] | None,
    recommended_action: dict[str, Any],
) -> list[dict[str, Any]]:
    if not rollback_event:
        return []
    action = str(recommended_action.get("action") or "")
    source_filters = dict(rollback_event.get("source_filters") or {})
    trade_date = source_filters.get("trade_date")
    score_date = source_filters.get("score_date")
    parent_event_id = str(rollback_event.get("rollback_of_event_id") or rollback_event.get("event_id") or "")
    rollback_event_id = str(rollback_event.get("event_id") or "")
    if action == "manual_release_or_reject" and rollback_event_id:
        shared_payload = {
            **({"trade_date": trade_date} if trade_date else {}),
            **({"score_date": score_date} if score_date else {}),
            "event_ids": [rollback_event_id],
        }
        return [
            {
                "label": "人工放行 rollback",
                "method": "POST",
                "path": "/system/learning/parameter-hints/rollback-approval",
                "payload": {
                    **shared_payload,
                    "action": "release",
                    "approver": "human-audit",
                    "comment": "post_rollback_tracking 建议人工放行",
                },
            },
            {
                "label": "人工驳回 rollback",
                "method": "POST",
                "path": "/system/learning/parameter-hints/rollback-approval",
                "payload": {
                    **shared_payload,
                    "action": "reject",
                    "approver": "human-audit",
                    "comment": "post_rollback_tracking 建议人工驳回",
                },
            },
        ]
    if action in {"continue_observe", "confirm_rollback_effect", "review_rollback_effect"} and parent_event_id:
        return [
            {
                "label": "查看 rollback 跟踪",
                "method": "GET",
                "path": "/system/learning/parameter-hints/effects",
                "query": {
                    **({"trade_date": trade_date} if trade_date else {}),
                    **({"score_date": score_date} if score_date else {}),
                    "event_ids": parent_event_id,
                    "status": "effective",
                },
            }
        ]
    return []


def _build_post_rollback_tracking_summary(
    effect_tracking: dict[str, Any],
    rollback_event: dict[str, Any] | None,
    rollback_report: dict[str, Any] | None,
) -> dict[str, Any]:
    if not rollback_event:
        recommended_action = {
            "action": "continue_observe",
            "priority": "low",
            "reason": "尚未生成 rollback 事件，暂无可跟踪的回滚后样本。",
        }
        return {
            "available": False,
            "tracked": False,
            "followup_status": "not_started",
            "recommended_action": recommended_action,
            "operation_targets": [],
            "summary_lines": ["尚未生成 rollback 事件，暂无二次效果追踪。"],
        }
    approval_ticket = dict(rollback_event.get("approval_ticket") or {})
    rollback_trade_count = int((rollback_report or {}).get("trade_count", 0) or 0)
    observation_window = build_observation_window_status(rollback_event, rollback_trade_count)
    observation_status = str(observation_window.get("status") or "observing")
    pending_high_risk_review = is_pending_high_risk_rollback(rollback_event, approval_ticket)
    if not rollback_report or not rollback_report.get("available"):
        if pending_high_risk_review and observation_status in {"ready_for_review", "overdue"}:
            followup_status = "manual_review_required"
            recommended_action = {
                "action": "manual_release_or_reject",
                "priority": ("high" if observation_status == "overdue" else "medium"),
                "reason": "高风险 rollback 已达到复核窗口，需要人工决定放行还是驳回。",
            }
            summary_lines = ["高风险 rollback 已达到复核窗口，但尚无足够样本，需要人工处理。"]
        else:
            followup_status = "insufficient_samples"
            recommended_action = {
                "action": "continue_observe",
                "priority": "low",
                "reason": "rollback 已记录，但观察窗口内尚未形成足够样本。",
            }
            summary_lines = ["rollback 已记录，但观察窗口内还没有可复核的样本。"]
        return {
            "available": False,
            "tracked": True,
            "rollback_event_id": rollback_event.get("event_id"),
            "rollback_status": rollback_event.get("status"),
            "observation_window": observation_window,
            "followup_status": followup_status,
            "recommended_action": recommended_action,
            "operation_targets": _build_post_rollback_operation_targets(rollback_event, recommended_action),
            "summary_lines": summary_lines,
        }
    rollback_avg = float(rollback_report.get("avg_next_day_close_pct", 0.0) or 0.0)
    rollback_win_rate = float(rollback_report.get("win_rate", 0.0) or 0.0)
    avg_delta = rollback_avg - float(effect_tracking.get("avg_next_day_close_pct", 0.0) or 0.0)
    win_rate_delta = rollback_win_rate - float(effect_tracking.get("win_rate", 0.0) or 0.0)
    recovery_detected = rollback_trade_count >= 1 and (avg_delta > 0 or win_rate_delta > 0)
    if pending_high_risk_review and observation_status in {"ready_for_review", "overdue"}:
        followup_status = "manual_review_required"
        recommended_action = {
            "action": "manual_release_or_reject",
            "priority": ("high" if observation_status == "overdue" else "medium"),
            "reason": "高风险 rollback 已完成观察窗口，需要人工决定放行还是驳回。",
        }
        summary_headline = "高风险 rollback 已达到复核窗口，需要人工确认后续动作。"
    elif observation_status in {"ready_for_review", "overdue"} and recovery_detected:
        followup_status = "recovery_confirmed"
        recommended_action = {
            "action": "confirm_rollback_effect",
            "priority": ("high" if observation_status == "overdue" else "medium"),
            "reason": "rollback 后样本表现已有修复迹象，可以确认回滚有效。",
        }
        summary_headline = "rollback 后同类样本表现已有改善，可以进入确认。"
    elif observation_status in {"ready_for_review", "overdue"}:
        followup_status = "manual_review_required"
        recommended_action = {
            "action": "review_rollback_effect",
            "priority": ("high" if observation_status == "overdue" else "medium"),
            "reason": "rollback 观察窗口已到，但样本未显示稳定改善，建议人工复核。",
        }
        summary_headline = "rollback 后样本尚未形成改善结论，建议人工复核。"
    elif rollback_trade_count <= 0:
        followup_status = "insufficient_samples"
        recommended_action = {
            "action": "continue_observe",
            "priority": "low",
            "reason": "rollback 已开始跟踪，但样本数仍不足以形成结论。",
        }
        summary_headline = "rollback 后样本数仍不足，继续观察。"
    else:
        followup_status = "continue_observe"
        recommended_action = {
            "action": "continue_observe",
            "priority": "low",
            "reason": "rollback 观察窗口尚未完成，继续累计同类样本。",
        }
        summary_headline = "rollback 观察窗口尚未完成，继续观察。"
    manual_review_required = followup_status == "manual_review_required"
    return {
        "available": True,
        "tracked": True,
        "rollback_event_id": rollback_event.get("event_id"),
        "rollback_status": rollback_event.get("status"),
        "trade_count": rollback_trade_count,
        "avg_next_day_close_pct": rollback_avg,
        "win_rate": rollback_win_rate,
        "avg_return_delta": round(avg_delta, 6),
        "win_rate_delta": round(win_rate_delta, 6),
        "recovery_detected": recovery_detected,
        "manual_review_required": manual_review_required,
        "observation_window": observation_window,
        "followup_status": followup_status,
        "recommended_action": recommended_action,
        "operation_targets": _build_post_rollback_operation_targets(rollback_event, recommended_action),
        "summary_lines": [
            summary_headline,
            f"回滚后样本 {rollback_trade_count} 笔，平均次日收益 {rollback_avg:.2%}，胜率 {rollback_win_rate:.1%}。",
        ],
    }


def _build_effect_tracking_summary(
    event: dict[str, Any],
    report: dict[str, Any] | None,
    rollback_event: dict[str, Any] | None = None,
    rollback_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    available = bool(report and report.get("available"))
    trade_count = int((report or {}).get("trade_count", 0) or 0)
    avg_return = float((report or {}).get("avg_next_day_close_pct", 0.0) or 0.0)
    win_rate = float((report or {}).get("win_rate", 0.0) or 0.0)
    observation_window = build_observation_window_status(event, trade_count)
    if not report or not report.get("available"):
        response = {
            "available": False,
            "trade_count": 0,
            "avg_next_day_close_pct": 0.0,
            "win_rate": 0.0,
            "summary_lines": ["当前还没有可用于评估该提案效果的交易样本。"],
            "observation_window": observation_window,
        }
        response["post_rollback_tracking"] = _build_post_rollback_tracking_summary(
            response,
            rollback_event,
            rollback_report,
        )
        return response
    rollback_recommended = trade_count >= 1 and (avg_return < 0 or win_rate < 0.4)
    rollback_baseline = dict(event.get("rollback_baseline") or {})
    rollback_preview = {
        "event_id": event.get("event_id"),
        "param_key": event.get("param_key"),
        "current_value": event.get("new_value"),
        "restore_value": rollback_baseline.get("restore_value"),
        "rollback_trigger": rollback_baseline.get("rollback_trigger"),
        "rollback_reason": (
            "提案生效后同类样本平均收益/胜率仍偏弱，建议回滚到上一个稳定值。"
            if rollback_recommended
            else "当前样本未触发自动回滚判定。"
        ),
    }
    response = {
        "available": available,
        "trade_count": trade_count,
        "avg_next_day_close_pct": avg_return,
        "win_rate": win_rate,
        "summary_lines": list(report.get("summary_lines", [])),
        "rollback_recommended": rollback_recommended,
        "rollback_preview": rollback_preview,
        "observation_window": observation_window,
    }
    response["post_rollback_tracking"] = _build_post_rollback_tracking_summary(
        response,
        rollback_event,
        rollback_report,
    )
    return response


def _build_parameter_observation_window_payload(
    *,
    stage: str,
    observation_window_days: int | None,
    observation_trade_count: int | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"stage": stage}
    if observation_window_days is not None:
        payload["duration_days"] = int(observation_window_days)
    if observation_trade_count is not None:
        payload["expected_trade_count"] = int(observation_trade_count)
    return payload


def _build_parameter_hint_rollback_policy(
    event: dict[str, Any],
    current_param_row: dict[str, Any] | None,
    effect_tracking: dict[str, Any],
) -> dict[str, Any]:
    approval_snapshot = dict(event.get("approval_policy_snapshot") or {})
    current_row = current_param_row or {}
    active_event_match = current_row.get("active_event_id") == event.get("event_id")
    restore_value = (event.get("rollback_baseline") or {}).get("restore_value")
    base_auto_approvable = bool(approval_snapshot.get("auto_approvable"))
    auto_approvable = (
        base_auto_approvable
        and active_event_match
        and restore_value is not None
        and bool(effect_tracking.get("rollback_recommended"))
    )
    rationale_lines = []
    if approval_snapshot.get("rationale"):
        rationale_lines.append(str(approval_snapshot["rationale"]))
    if not effect_tracking.get("rollback_recommended"):
        rationale_lines.append("当前样本尚未触发自动回滚判定。")
    if restore_value is None:
        rationale_lines.append("缺少 restore_value，暂不能直接生成回滚事件。")
    if not active_event_match:
        rationale_lines.append("当前生效参数已不是原提案值，默认不自动回滚历史事件。")
    if auto_approvable:
        rationale_lines.append("该提案仍是当前生效值，且属于低风险参数，可按受控策略直接回滚。")
    recommended_effective_period = (
        "today_session"
        if auto_approvable and current_row.get("scope") in {"monitor", "intraday"}
        else (event.get("effective_period") or current_row.get("effective_period_default") or "next_trading_day")
    )
    return {
        "auto_approvable": auto_approvable,
        "active_event_match": active_event_match,
        "force_required": (not active_event_match or restore_value is None),
        "risk_level": approval_snapshot.get("risk_level") or ("low" if auto_approvable else "high"),
        "required_confirmation": ("optional" if auto_approvable else "manual_review"),
        "recommended_status": ("approved" if auto_approvable else "evaluating"),
        "recommended_effective_period": recommended_effective_period,
        "required_approver": ("ashare-audit" if auto_approvable else "user+ashare-audit"),
        "restore_value_available": restore_value is not None,
        "rationale": " ".join(line for line in rationale_lines if line),
    }


class CandidateOpinionInput(BaseModel):
    round: int = Field(ge=1, le=2)
    agent_id: str
    stance: str
    confidence: str | float = "medium"
    reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    thesis: str = ""
    key_evidence: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    questions_to_others: list[str] = Field(default_factory=list)
    challenged_by: list[str] = Field(default_factory=list)
    challenged_points: list[str] = Field(default_factory=list)
    previous_stance: str | None = None
    changed: bool | None = None
    changed_because: list[str] = Field(default_factory=list)
    resolved_questions: list[str] = Field(default_factory=list)
    remaining_disputes: list[str] = Field(default_factory=list)


class BatchOpinionItem(BaseModel):
    case_id: str
    round: int = Field(ge=1, le=2)
    agent_id: str
    stance: str
    confidence: str | float = "medium"
    reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    thesis: str = ""
    key_evidence: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    questions_to_others: list[str] = Field(default_factory=list)
    challenged_by: list[str] = Field(default_factory=list)
    challenged_points: list[str] = Field(default_factory=list)
    previous_stance: str | None = None
    changed: bool | None = None
    changed_because: list[str] = Field(default_factory=list)
    resolved_questions: list[str] = Field(default_factory=list)
    remaining_disputes: list[str] = Field(default_factory=list)


class BatchOpinionInput(BaseModel):
    items: list[BatchOpinionItem] = Field(default_factory=list)
    auto_rebuild: bool = True


class OpenClawOpinionIngressInput(BaseModel):
    payload: Any = Field(default_factory=dict)
    trade_date: str | None = None
    expected_round: int | None = Field(default=None, ge=1, le=2)
    expected_agent_id: str | None = None
    expected_case_ids: list[str] = Field(default_factory=list)
    case_id_map: dict[str, str] = Field(default_factory=dict)
    default_case_id: str | None = None
    auto_rebuild: bool = True


class ExecutionBridgeHealthIngressInput(BaseModel):
    health: dict[str, Any] = Field(default_factory=dict)
    trigger: str = "windows_gateway"


class SettlementOutcomeInput(BaseModel):
    symbol: str
    next_day_close_pct: float
    note: str = ""
    exit_reason: str = ""
    holding_days: int = 1
    playbook: str = ""
    regime: str = ""


class ScoreSettlementInput(BaseModel):
    trade_date: str
    score_date: str
    outcomes: list[SettlementOutcomeInput] = Field(default_factory=list)


class DiscussionCycleBootstrapInput(BaseModel):
    trade_date: str | None = None


class NaturalLanguageAdjustmentInput(BaseModel):
    instruction: str
    apply: bool = True
    notify: bool = False
    proposed_by: str = "user"
    structured_by: str = "ashare"
    approved_by: str = "ashare-audit"
    status: str = "approved"
    effective_period: str | None = None


class DossierPrecomputeInput(BaseModel):
    trade_date: str | None = None
    symbols: list[str] = Field(default_factory=list)
    source: str = "candidate_pool"
    limit: int = Field(default=30, ge=1, le=50)
    force: bool = False


class ExecutionIntentDispatchInput(BaseModel):
    trade_date: str
    account_id: str | None = None
    intent_ids: list[str] = Field(default_factory=list)
    apply: bool = False


class ParameterHintProposalInput(BaseModel):
    trade_date: str | None = None
    score_date: str | None = None
    review_tag: str | None = None
    exit_context_key: str | None = None
    exit_context_value: str | None = None
    symbol: str | None = None
    reason: str | None = None
    apply: bool = False
    respect_approval_policy: bool = True
    proposed_by: str = "ashare-review"
    structured_by: str = "ashare"
    approved_by: str = "ashare-audit"
    status: str = "approved"
    effective_period: str | None = None
    observation_window_days: int | None = Field(default=None, ge=1, le=10)
    observation_trade_count: int | None = Field(default=None, ge=1, le=20)


class ParameterEffectQueryInput(BaseModel):
    trade_date: str | None = None
    score_date: str | None = None
    event_ids: list[str] = Field(default_factory=list)
    status: str = "effective"


class ParameterRollbackPreviewInput(BaseModel):
    trade_date: str | None = None
    score_date: str | None = None
    event_ids: list[str] = Field(default_factory=list)
    status: str = "effective"


class ParameterRollbackApplyInput(BaseModel):
    trade_date: str | None = None
    score_date: str | None = None
    event_ids: list[str] = Field(default_factory=list)
    status: str = "effective"
    respect_approval_policy: bool = True
    proposed_by: str = "ashare-review"
    structured_by: str = "ashare"
    approved_by: str = "ashare-audit"
    force: bool = False
    proposal_status: str = "approved"
    effective_period: str | None = None
    observation_window_days: int | None = Field(default=None, ge=1, le=10)
    observation_trade_count: int | None = Field(default=None, ge=1, le=20)


class ParameterRollbackApprovalInput(BaseModel):
    event_ids: list[str] = Field(default_factory=list)
    action: str = "approve"
    approver: str = "ashare-audit"
    comment: str = ""
    effective_from: str | None = None


class ParameterHintInspectionRunInput(BaseModel):
    trade_date: str | None = None
    score_date: str | None = None
    statuses: str = "evaluating,approved,effective"
    due_within_days: int = 1
    limit: int = Field(default=50, ge=1, le=200)
def _build_dossier_status(pack: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
    if not pack:
        return {
            "available": False,
            "is_fresh": False,
            "expires_in_seconds": None,
        }

    resolved_now = now or datetime.now()
    expires_at = pack.get("expires_at")
    expires_in_seconds: int | None = None
    is_fresh = False
    if expires_at:
        expires_dt = datetime.fromisoformat(expires_at)
        if expires_dt.tzinfo is not None and resolved_now.tzinfo is None:
            resolved_now = datetime.now(tz=expires_dt.tzinfo)
        remaining = (expires_dt - resolved_now).total_seconds()
        expires_in_seconds = max(int(remaining), 0)
        is_fresh = remaining >= 0

    return {
        "available": True,
        "is_fresh": is_fresh,
        "expires_in_seconds": expires_in_seconds,
        **pack,
    }


def _short_list_text(items: list[str], limit: int = 3, fallback: str = "无") -> str:
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    if not cleaned:
        return fallback
    return "；".join(cleaned[:limit])


def build_router(
    settings: AppSettings,
    config_mgr: RuntimeConfigManager | None = None,
    audit_store: AuditStore | None = None,
    runtime_state_store: StateStore | None = None,
    research_state_store: StateStore | None = None,
    meeting_state_store: StateStore | None = None,
    parameter_service: ParameterService | None = None,
    candidate_case_service: CandidateCaseService | None = None,
    discussion_cycle_service: DiscussionCycleService | None = None,
    agent_score_service: AgentScoreService | None = None,
    monitor_state_service: MonitorStateService | None = None,
    message_dispatcher: MessageDispatcher | None = None,
    market_adapter=None,
    execution_adapter: ExecutionAdapter | None = None,
    dossier_precompute_service: DossierPrecomputeService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/system", tags=["system"])
    reports_dir = settings.logs_dir / "reports"
    runtime_report_path = reports_dir / "runtime_latest.json"
    serving_store = ServingStore(settings.storage_root)
    archive_store = DataArchiveStore(settings.storage_root)
    offline_backtest_runner = PlaybookBacktestRunner()
    settlement_service = AgentScoreSettlementService()
    trade_attribution_service = TradeAttributionService(settings.storage_root / "learning" / "trade_attribution.json")
    adjustment_interpreter = NaturalLanguageAdjustmentInterpreter(parameter_service._registry) if parameter_service else None
    monitor_change_notifier = (
        MonitorChangeNotifier(monitor_state_service, message_dispatcher)
        if monitor_state_service
        else None
    )
    discussion_finalize_notifier = (
        DiscussionFinalizeNotifier(
            candidate_case_service=candidate_case_service,
            discussion_cycle_service=discussion_cycle_service,
            state_store=meeting_state_store,
            dispatcher=message_dispatcher,
        )
        if candidate_case_service and discussion_cycle_service and meeting_state_store
        else None
    )
    governance_adjustment_notifier = (
        GovernanceAdjustmentNotifier(meeting_state_store, message_dispatcher)
        if meeting_state_store
        else None
    )
    live_execution_alert_notifier = (
        LiveExecutionAlertNotifier(
            meeting_state_store,
            message_dispatcher,
            enabled=settings.notify.alerts_enabled,
        )
        if meeting_state_store
        else None
    )
    pending_order_inspection_service = (
        PendingOrderInspectionService(execution_adapter, meeting_state_store)
        if meeting_state_store and execution_adapter
        else None
    )
    pending_order_remediation_service = (
        PendingOrderRemediationService(
            execution_adapter,
            meeting_state_store,
            pending_order_inspection_service,
        )
        if meeting_state_store and execution_adapter and pending_order_inspection_service
        else None
    )
    startup_recovery_service = (
        StartupRecoveryService(execution_adapter, meeting_state_store)
        if meeting_state_store and execution_adapter
        else None
    )
    account_state_service = (
        AccountStateService(
            settings,
            execution_adapter,
            meeting_state_store,
            config_mgr=config_mgr,
            parameter_service=parameter_service,
        )
        if meeting_state_store and execution_adapter
        else None
    )
    execution_reconciliation_service = (
        ExecutionReconciliationService(execution_adapter, meeting_state_store)
        if meeting_state_store and execution_adapter
        else None
    )
    reverse_repo_service = (
        ReverseRepoService(
            settings=settings,
            execution_adapter=execution_adapter,
            market_adapter=market_adapter,
            state_store=runtime_state_store,
            config_mgr=config_mgr,
            parameter_service=parameter_service,
            dispatcher=message_dispatcher,
        )
        if runtime_state_store and execution_adapter and market_adapter
        else None
    )

    def _parse_iso_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None

    def _estimate_holding_days(submitted_at: str | None, reconciled_at: str | None) -> int:
        submitted_dt = _parse_iso_dt(submitted_at)
        reconciled_dt = _parse_iso_dt(reconciled_at)
        if not submitted_dt or not reconciled_dt:
            return 0
        return max((reconciled_dt.date() - submitted_dt.date()).days, 0)

    def _resolve_execution_strategy_context() -> dict[str, Any]:
        runtime_context = serving_store.get_latest_runtime_context() or {}
        dossier_pack = serving_store.get_latest_dossier_pack() or {}
        market_context = serving_store.get_latest_market_context() or {}
        playbook_map = {
            item.get("symbol"): item
            for item in (runtime_context.get("playbook_contexts") or dossier_pack.get("playbook_contexts") or [])
            if item.get("symbol")
        }
        dossier_map = {
            item.get("symbol"): item
            for item in dossier_pack.get("items", [])
            if item.get("symbol")
        }
        regime_value = (
            ((runtime_context.get("market_profile") or {}).get("regime"))
            or market_context.get("regime")
            or "unknown"
        )
        return {
            "runtime_context": runtime_context,
            "dossier_pack": dossier_pack,
            "market_context": market_context,
            "playbook_map": playbook_map,
            "dossier_map": dossier_map,
            "regime_value": regime_value,
        }

    def _sync_reconciliation_attribution(payload: dict) -> dict | None:
        items = payload.get("items", [])
        if not items:
            return None
        execution_context = _resolve_execution_strategy_context()
        playbook_map = execution_context["playbook_map"]
        dossier_map = execution_context["dossier_map"]
        case_map_by_id: dict[str, CandidateCase] = {}
        if candidate_case_service:
            trade_dates = sorted({item.get("trade_date") for item in items if item.get("trade_date")})
            for trade_date in trade_dates:
                for case in candidate_case_service.list_cases(trade_date=trade_date, limit=500):
                    case_map_by_id[case.case_id] = case
        order_journal = {}
        if meeting_state_store:
            order_journal = {
                item.get("order_id"): item
                for item in meeting_state_store.get("execution_order_journal", [])
                if item.get("order_id")
            }
        position_map = {
            item.get("symbol"): item
            for item in payload.get("positions", [])
            if item.get("symbol")
        }
        regime_value = execution_context["regime_value"]
        score_date = (payload.get("reconciled_at") or datetime.now().isoformat())[:10]
        attribution_items: list[TradeAttributionRecord] = []
        for item in items:
            filled_quantity = int(item.get("filled_quantity", 0) or 0)
            if filled_quantity <= 0:
                continue
            order_id = item.get("order_id")
            journal_item = order_journal.get(order_id, {})
            symbol = item.get("symbol")
            if not symbol:
                continue
            decision_id = item.get("decision_id") or journal_item.get("decision_id")
            case = case_map_by_id.get(decision_id) if decision_id else None
            dossier_item = dossier_map.get(symbol) or {}
            playbook_context = playbook_map.get(symbol) or {}
            side = str(item.get("side") or ((journal_item.get("request") or {}).get("side")) or "").upper()
            avg_fill_price = item.get("avg_fill_price")
            position_item = position_map.get(symbol) or {}
            next_day_close_pct = 0.0
            if avg_fill_price and side == "BUY" and position_item.get("last_price") is not None:
                next_day_close_pct = round(
                    (float(position_item.get("last_price")) - float(avg_fill_price)) / max(float(avg_fill_price), 1e-9),
                    6,
                )
            elif avg_fill_price and side == "SELL" and position_item.get("cost_price") is not None:
                next_day_close_pct = round(
                    (float(avg_fill_price) - float(position_item.get("cost_price"))) / max(float(position_item.get("cost_price")), 1e-9),
                    6,
                )
            exit_reason = (
                journal_item.get("exit_reason")
                or ((journal_item.get("request") or {}).get("exit_reason"))
                or ("open_position" if side == "BUY" else "sell_filled")
            )
            trade_date = (
                item.get("trade_date")
                or (case.trade_date if case else None)
                or (payload.get("reconciled_at") or datetime.now().isoformat())[:10]
            )
            attribution_items.append(
                TradeAttributionRecord(
                    trade_date=trade_date,
                    score_date=score_date,
                    symbol=symbol,
                    name=item.get("name") or (case.name if case else dossier_item.get("name", "")),
                    account_id=payload.get("account_id", ""),
                    case_id=(case.case_id if case else decision_id),
                    order_id=order_id,
                    side=side,
                    source="execution_reconciliation",
                    status=item.get("latest_status", ""),
                    playbook=(
                        playbook_context.get("playbook")
                        or journal_item.get("playbook")
                        or ((journal_item.get("request") or {}).get("playbook"))
                        or dossier_item.get("assigned_playbook")
                        or "unassigned"
                    ),
                    regime=(
                        journal_item.get("regime")
                        or ((journal_item.get("request") or {}).get("regime"))
                        or regime_value
                    ),
                    exit_reason=exit_reason,
                    next_day_close_pct=next_day_close_pct,
                    note=f"对账成交回写 {order_id}",
                    selection_score=(case.runtime_snapshot.selection_score if case else dossier_item.get("selection_score")),
                    rank=(case.runtime_snapshot.rank if case else dossier_item.get("rank")),
                    final_status=(case.final_status if case else dossier_item.get("final_status", "")),
                    risk_gate=(case.risk_gate if case else dossier_item.get("risk_gate", "")),
                    audit_gate=(case.audit_gate if case else dossier_item.get("audit_gate", "")),
                    holding_days=_estimate_holding_days(item.get("submitted_at"), payload.get("reconciled_at")),
                    filled_quantity=filled_quantity,
                    filled_value=float(item.get("filled_value", 0.0) or 0.0),
                    avg_fill_price=(float(avg_fill_price) if avg_fill_price is not None else None),
                    submitted_at=item.get("submitted_at"),
                    reconciled_at=payload.get("reconciled_at"),
                    exit_context_snapshot=(journal_item.get("exit_context_snapshot") or {}),
                    review_tags=list(journal_item.get("review_tags") or []),
                    recorded_at=datetime.now().isoformat(),
                )
            )
        if not attribution_items:
            return None
        report = trade_attribution_service.record_outcomes(
            trade_date=attribution_items[-1].trade_date,
            score_date=score_date,
            items=attribution_items,
        )
        return {
            "update_count": len(attribution_items),
            "items": [item.model_dump() for item in attribution_items],
            "report": report.model_dump(),
        }
    execution_guard = ExecutionGuard()

    def _recent_reports() -> list[str]:
        if not reports_dir.exists():
            return []
        files = sorted(reports_dir.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
        return [file.name for file in files[:10]]

    def _latest_dossier_status() -> dict[str, Any]:
        serving_pack = serving_store.get_latest_dossier_pack()
        if serving_pack:
            return {
                **_build_dossier_status(serving_pack),
                "source_layer": "serving",
            }
        if dossier_precompute_service:
            payload = dossier_precompute_service.get_latest_status()
            payload.setdefault("source_layer", "state_store")
            return payload
        return {
            "available": False,
            "is_fresh": False,
            "expires_in_seconds": None,
            "source_layer": "missing",
        }

    def _resolve_shared_context(trade_date: str) -> dict[str, Any]:
        latest_pack = serving_store.get_latest_dossier_pack()
        if latest_pack and latest_pack.get("trade_date") == trade_date:
            return {
                **_build_dossier_status(latest_pack),
                "source_layer": "serving_pack",
                "market_context": latest_pack.get("market_context", {}),
                "event_context": latest_pack.get("event_context", {}),
            }

        market_context = serving_store.get_latest_market_context()
        event_context = serving_store.get_latest_event_context()
        if (
            market_context
            and event_context
            and market_context.get("trade_date") == trade_date
            and event_context.get("trade_date") == trade_date
        ):
            return {
                "available": True,
                "trade_date": trade_date,
                "is_fresh": True,
                "expires_in_seconds": None,
                "source_layer": "serving_context",
                "market_context": market_context,
                "event_context": event_context,
            }

        return {
            "available": False,
            "trade_date": trade_date,
            "is_fresh": False,
            "expires_in_seconds": None,
            "source_layer": "missing",
            "market_context": {},
            "event_context": {},
        }

    def _resolve_case_dossier_payload(trade_date: str, symbol: str) -> dict[str, Any]:
        latest_pack = serving_store.get_latest_dossier_pack()
        if latest_pack and latest_pack.get("trade_date") == trade_date:
            for item in latest_pack.get("items", []):
                if item.get("symbol") == symbol:
                    return {
                        "available": True,
                        "source_layer": "serving_pack",
                        "trade_date": trade_date,
                        "pack_id": latest_pack.get("pack_id"),
                        "generated_at": latest_pack.get("generated_at"),
                        "expires_at": latest_pack.get("expires_at"),
                        "payload": item,
                    }

        dossier_record = serving_store.get_dossier(trade_date, symbol)
        if dossier_record:
            payload = dict(dossier_record.get("payload") or {})
            if not payload.get("symbol_context"):
                symbol_context_record = serving_store.get_symbol_context(trade_date, symbol)
                if symbol_context_record:
                    payload["symbol_context"] = symbol_context_record.get("payload") or {}
            return {
                "available": True,
                "source_layer": "feature_dossier",
                "trade_date": dossier_record.get("trade_date", trade_date),
                "generated_at": dossier_record.get("generated_at"),
                "expires_at": dossier_record.get("expires_at"),
                "staleness_level": dossier_record.get("staleness_level"),
                "payload": payload,
            }

        return {
            "available": False,
            "source_layer": "missing",
            "trade_date": trade_date,
            "payload": {},
        }

    def _build_agent_focus(agent_id: str | None, case: CandidateCase, detail: dict[str, Any], dossier_payload: dict[str, Any]) -> dict[str, Any]:
        resolved_agent = agent_id or "shared"
        dossier = dossier_payload.get("payload") or {}
        symbol_context = dossier.get("symbol_context") or {}
        event_context = dossier.get("event_context") or {}
        research = dossier.get("research") or {}
        market_snapshot = dossier.get("market_snapshot") or {}
        daily_bar = dossier.get("daily_bar") or {}
        market_relative = symbol_context.get("market_relative") or {}
        sector_relative = symbol_context.get("sector_relative") or {}
        discussion = detail.get("discussion") or {}
        latest_opinions = detail.get("latest_opinions") or {}
        rounds = detail.get("rounds") or {}
        freshness_label = dossier_payload.get("staleness_level") or ("fresh" if dossier_payload.get("available") else "missing")

        if resolved_agent == "ashare-research":
            lines = [
                f"研究事件 {event_context.get('event_count', 0)} 条；最近标题：{_short_list_text(research.get('latest_titles', []), fallback='暂无事件标题')}",
                f"板块标签：{_short_list_text(sector_relative.get('sector_tags', []), fallback='暂无板块标签')}",
                f"市场背景：{market_relative.get('benchmark_symbol') or '无基准'}；dossier={freshness_label}",
            ]
        elif resolved_agent == "ashare-strategy":
            lines = [
                f"当前排名 {case.runtime_snapshot.rank}，分数 {case.runtime_snapshot.selection_score}，动作 {case.runtime_snapshot.action}",
                f"盘口/日线：分时涨跌={market_snapshot.get('change_pct')}，日线涨跌={daily_bar.get('change_pct')}，成交量={market_snapshot.get('volume')}",
                f"相对基准：{market_relative.get('benchmark_symbol') or '无'} 强弱={market_relative.get('relative_strength_vs_benchmark')}",
            ]
        elif resolved_agent == "ashare-risk":
            lines = [
                f"当前门控：risk={case.risk_gate} audit={case.audit_gate}，最终状态={case.final_status}",
                f"波动与时效：分时涨跌={market_snapshot.get('change_pct')}，事件数={event_context.get('event_count', 0)}，dossier={freshness_label}",
                f"待解问题：{_short_list_text(discussion.get('questions_for_round_2', []) or discussion.get('remaining_disputes', []), fallback='暂无显式待解问题')}",
            ]
        elif resolved_agent == "ashare-audit":
            latest_opinion_labels = [f"{key}:{value.get('stance')}" for key, value in latest_opinions.items()]
            lines = [
                f"讨论覆盖：Round1={bool(rounds.get('round_1', {}).get('complete'))} Round2={bool(rounds.get('round_2', {}).get('complete'))} 实质回应={bool(rounds.get('round_2', {}).get('substantive_ready'))}，opinions={detail.get('opinion_count', 0)}",
                f"证据缺口：{_short_list_text(discussion.get('evidence_gaps', []), fallback='当前无显式证据缺口')}",
                f"最新意见：{_short_list_text(latest_opinion_labels, fallback='暂无意见')}；dossier={freshness_label}",
            ]
        else:
            lines = [
                f"{case.symbol} {case.name or case.symbol} 排名={case.runtime_snapshot.rank} 分数={case.runtime_snapshot.selection_score}",
                f"事件={event_context.get('event_count', 0)} 风控={case.risk_gate} 审计={case.audit_gate}",
                f"最新理由：{detail.get('headline_reason') or case.runtime_snapshot.summary or '暂无'}",
            ]

        return {
            "agent_id": resolved_agent,
            "summary_lines": lines,
            "key_points": {
                "event_count": event_context.get("event_count", 0),
                "latest_titles": research.get("latest_titles", []),
                "sector_tags": sector_relative.get("sector_tags", []),
                "benchmark_symbol": market_relative.get("benchmark_symbol"),
                "relative_strength_vs_benchmark": market_relative.get("relative_strength_vs_benchmark"),
                "questions_for_round_2": discussion.get("questions_for_round_2", []),
                "remaining_disputes": discussion.get("remaining_disputes", []),
                "evidence_gaps": discussion.get("evidence_gaps", []),
                "round_2_substantive_ready": rounds.get("round_2", {}).get("substantive_ready"),
            },
        }

    def _build_agent_packet(case: CandidateCase, requested_agent_id: str | None = None) -> dict[str, Any]:
        detail = candidate_case_service.build_case_vote_detail(case.case_id) if candidate_case_service else None
        detail = detail or {
            "case_id": case.case_id,
            "symbol": case.symbol,
            "name": case.name,
            "symbol_display": f"{case.symbol} {case.name or case.symbol}",
            "headline_reason": case.selected_reason or case.rejected_reason or case.runtime_snapshot.summary,
            "discussion": {},
            "latest_opinions": {},
            "rounds": {},
            "opinion_count": len(case.opinions),
        }
        dossier_payload = _resolve_case_dossier_payload(case.trade_date, case.symbol)
        dossier = {
            "available": dossier_payload.get("available", False),
            "source_layer": dossier_payload.get("source_layer", "missing"),
            "trade_date": dossier_payload.get("trade_date", case.trade_date),
            "generated_at": dossier_payload.get("generated_at"),
            "expires_at": dossier_payload.get("expires_at"),
            "staleness_level": dossier_payload.get("staleness_level"),
            **(dossier_payload.get("payload") or {}),
        }
        detail["dossier"] = dossier
        detail["agent_focus"] = _build_agent_focus(requested_agent_id, case, detail, dossier_payload)
        return detail

    def _build_shared_context_lines(shared_context: dict[str, Any]) -> list[str]:
        if not shared_context.get("available"):
            return ["共享上下文暂不可用，当前需按角色补读 serving 或原始接口。"]

        market_context = shared_context.get("market_context") or {}
        event_context = shared_context.get("event_context") or {}
        structure = market_context.get("market_structure") or {}
        expires_in = shared_context.get("expires_in_seconds")
        freshness = "fresh" if shared_context.get("is_fresh") else "stale"
        lines = [
            (
                f"shared_context 来源={shared_context.get('source_layer')} trade_date={shared_context.get('trade_date')} "
                f"freshness={freshness} expires_in_seconds={expires_in}"
            ),
            (
                f"市场背景：指数={market_context.get('benchmark_symbol') or market_context.get('index_symbol') or '未标注'} "
                f"成交额={structure.get('total_turnover')} 涨停={structure.get('limit_up_count')} 跌停={structure.get('limit_down_count')}"
            ),
            (
                f"事件背景：event_count={event_context.get('event_count', 0)} "
                f"market_events={len(event_context.get('market_events', []) or [])} "
                f"sector_events={len(event_context.get('sector_events', []) or [])}"
            ),
        ]
        return lines

    def _build_packet_groups(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        if not candidate_case_service:
            return {
                "selected_packets": [],
                "watchlist_packets": [],
                "rejected_packets": [],
            }

        def _packetize(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            packets: list[dict[str, Any]] = []
            for item in items:
                case_id = item.get("case_id")
                if not case_id:
                    continue
                case = candidate_case_service.get_case(case_id)
                if case is None:
                    continue
                packets.append(_build_agent_packet(case))
            return packets

        return {
            "selected_packets": _packetize(payload.get("selected", [])),
            "watchlist_packets": _packetize(payload.get("watchlist", [])),
            "rejected_packets": _packetize(payload.get("rejected", [])),
        }

    def _build_data_catalog_ref() -> dict[str, Any]:
        return {
            "endpoint": "/data/catalog",
            "description": "统一数据目录，列出数据位置、最新时间戳、推荐读取顺序与角色建议。",
        }

    def _build_packet_read_order() -> list[str]:
        return [
            "先读取本次 /system/discussions/agent-packets 返回的 items、shared_context、workspace_context 与 data_catalog_ref。",
            "若需先看全局状态，优先使用 workspace_context.summary_lines 与 workspace_context.runtime_context / discussion_context / monitor_context。",
            "若还要确认数据位置、时间戳和推荐入口，再读取 /data/catalog。",
            "若 packet 证据不足，再按角色补读 /data/market-context/latest、/data/event-context/latest、/data/symbol-contexts/latest、/data/dossiers/latest。",
            "若 serving 层仍不足，再按职责调用内部接口或允许的外部事实源补证，并在 evidence_refs 中标注来源与时间。",
        ]

    def _resolve_workspace_context_for_trade_date(trade_date: str) -> dict[str, Any]:
        payload = _sanitize_json_compatible(serving_store.get_latest_workspace_context() or {})
        if payload.get("trade_date") == trade_date:
            return payload
        return {}

    def _persist_discussion_context(payload: dict[str, Any]) -> dict[str, Any]:
        payload = _sanitize_json_compatible(payload)
        trade_date = payload.get("trade_date")
        if not trade_date:
            return payload
        archive_store.persist_discussion_context(trade_date, payload)
        if meeting_state_store:
            meeting_state_store.set("latest_discussion_context", payload)
            meeting_state_store.set(f"discussion_context:{trade_date}", payload)
        return payload

    def _serialize_cycle_compact(cycle: Any | None) -> dict[str, Any]:
        if cycle is None:
            return {"available": False}
        payload = cycle.model_dump() if hasattr(cycle, "model_dump") else dict(cycle)
        summary_snapshot = payload.pop("summary_snapshot", {}) or {}
        payload["case_count"] = summary_snapshot.get("case_count", len(payload.get("base_pool_case_ids", []) or []))
        payload["selected_count"] = summary_snapshot.get("selected_count", 0)
        payload["watchlist_count"] = summary_snapshot.get("watchlist_count", 0)
        payload["rejected_count"] = summary_snapshot.get("rejected_count", 0)
        payload["risk_gate_counts"] = summary_snapshot.get("risk_gate_counts", {})
        payload["audit_gate_counts"] = summary_snapshot.get("audit_gate_counts", {})
        payload["round_coverage"] = summary_snapshot.get("round_coverage", {})
        payload["controversy_summary_lines"] = summary_snapshot.get("controversy_summary_lines", [])[:5]
        payload["round_2_guidance"] = summary_snapshot.get("round_2_guidance", [])[:5]
        payload["cycle_detail_ref"] = f"/system/discussions/cycles/{payload.get('trade_date')}"
        return _sanitize_json_compatible(payload)

    def _enrich_discussion_payload(
        trade_date: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        shared_context = _resolve_shared_context(trade_date)
        packet_groups = _build_packet_groups(payload)
        enriched = {
            **payload,
            "shared_context": shared_context,
            "shared_context_lines": _build_shared_context_lines(shared_context),
            "data_catalog_ref": _build_data_catalog_ref(),
            **packet_groups,
        }
        alias_pairs = (
            ("selected", "selection"),
            ("selected_count", "selection_count"),
            ("selected_lines", "selection_lines"),
            ("selected_display", "selection_display"),
            ("selected_packets", "selection_packets"),
        )
        for source_key, alias_key in alias_pairs:
            if source_key in enriched and alias_key not in enriched:
                enriched[alias_key] = enriched[source_key]
        return enriched

    def _build_discussion_context_payload(
        trade_date: str,
        *,
        cycle_payload: dict[str, Any] | None = None,
        client_brief_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        discussion_summary = candidate_case_service.build_trade_date_summary(trade_date)
        reply_pack = _enrich_discussion_payload(
            trade_date,
            candidate_case_service.build_reply_pack(
                trade_date,
                selected_limit=5,
                watchlist_limit=10,
                rejected_limit=10,
            ),
        )
        final_brief = _enrich_discussion_payload(
            trade_date,
            candidate_case_service.build_final_brief(
                trade_date,
                selection_limit=3,
            ),
        )
        cycle = cycle_payload or (
            discussion_cycle_service.get_cycle(trade_date).model_dump()
            if discussion_cycle_service and discussion_cycle_service.get_cycle(trade_date)
            else None
        )
        client_brief = client_brief_payload or _build_client_brief(trade_date)
        summary_lines = client_brief.get("lines", []) or final_brief.get("lines", []) or reply_pack.get("overview_lines", [])
        if discussion_summary.get("controversy_summary_lines"):
            summary_lines = [
                *summary_lines,
                "争议焦点:",
                *discussion_summary.get("controversy_summary_lines", []),
            ]
        if discussion_summary.get("round_2_guidance"):
            summary_lines = [
                *summary_lines,
                "二轮要求:",
                *discussion_summary.get("round_2_guidance", []),
            ]
        finalize_packet = build_finalize_packet_envelope(
            trade_date=trade_date,
            cycle=cycle or {},
            execution_precheck=_build_execution_precheck(trade_date, account_id=_resolve_account_id()),
            execution_intents=_build_execution_intents(trade_date, account_id=_resolve_account_id()),
            client_brief=client_brief,
            final_brief=final_brief,
            reply_pack=reply_pack,
            shared_context=reply_pack.get("shared_context") or {},
        )
        payload = {
            "available": True,
            "resource": "discussion_context",
            "trade_date": trade_date,
            "generated_at": datetime.now().isoformat(),
            "case_count": reply_pack.get("case_count", 0),
            "status": final_brief.get("status"),
            "cycle": cycle,
            "round_coverage": discussion_summary.get("round_coverage", {}),
            "disputed_case_ids": discussion_summary.get("disputed_case_ids", []),
            "substantive_gap_case_ids": discussion_summary.get("substantive_gap_case_ids", []),
            "controversy_summary_lines": discussion_summary.get("controversy_summary_lines", []),
            "round_2_guidance": discussion_summary.get("round_2_guidance", []),
            "shared_context": reply_pack.get("shared_context"),
            "shared_context_lines": reply_pack.get("shared_context_lines", []),
            "data_catalog_ref": _build_data_catalog_ref(),
            "reply_pack": reply_pack,
            "final_brief": final_brief,
            "client_brief": client_brief,
            "finalize_packet": finalize_packet,
            "summary_lines": summary_lines,
        }
        payload["summary_text"] = "\n".join(payload["summary_lines"])
        return build_meeting_context_envelope(payload, trade_date=trade_date)

    def _latest_runtime() -> dict:
        if runtime_report_path.exists():
            return json.loads(runtime_report_path.read_text(encoding="utf-8"))
        return (runtime_state_store.get("latest_runtime_report", {}) if runtime_state_store else {}) or {}

    def _research_summary() -> dict:
        if not research_state_store:
            return {"symbols": [], "news_count": 0, "announcement_count": 0, "event_titles": []}
        return research_state_store.get("summary", {"symbols": [], "news_count": 0, "announcement_count": 0, "event_titles": []})

    def _latest_meeting() -> dict:
        if not meeting_state_store:
            return {"available": False}
        meeting = meeting_state_store.get("latest")
        return meeting or {"available": False}

    def _resolve_trade_date(preferred: str | None = None) -> str:
        if preferred:
            return preferred
        if candidate_case_service:
            latest_cases = candidate_case_service.list_cases(limit=1)
            if latest_cases:
                return latest_cases[0].trade_date
        runtime = _latest_runtime()
        generated_at = runtime.get("generated_at")
        if generated_at:
            return datetime.fromisoformat(generated_at).date().isoformat()
        raise ValueError("trade_date is required because no candidate cases are available yet")

    def _persist_discussion_writeback_items(
        items: list[tuple[str, CandidateOpinion]],
        *,
        auto_rebuild: bool,
        audit_message: str,
        audit_payload: dict[str, Any] | None = None,
    ) -> list[CandidateCase]:
        if not candidate_case_service:
            raise ValueError("candidate case service not initialized")
        updated = candidate_case_service.record_opinions_batch(items)
        if auto_rebuild:
            rebuilt_case_ids = list(dict.fromkeys(case_id for case_id, _ in items))
            updated = [candidate_case_service.rebuild_case(case_id) for case_id in rebuilt_case_ids]
        if audit_store:
            case_ids = [case_id for case_id, _ in items]
            payload = {
                "count": len(items),
                "case_count": len(set(case_ids)),
            }
            if audit_payload:
                payload.update(audit_payload)
            audit_store.append(
                category="discussion",
                message=audit_message,
                payload=payload,
            )
        return updated

    def _load_latest_offline_backtest_attribution() -> dict[str, Any]:
        serving_path = serving_store.layout.serving_root / "latest_offline_backtest_attribution.json"
        if serving_path.exists():
            try:
                return _sanitize_json_compatible(json.loads(serving_path.read_text(encoding="utf-8")))
            except Exception:
                pass
        if meeting_state_store:
            payload = meeting_state_store.get("latest_offline_backtest_attribution", {})
            if isinstance(payload, dict):
                return _sanitize_json_compatible(payload)
        return {}

    def _load_latest_offline_backtest_metrics() -> dict[str, Any]:
        serving_path = serving_store.layout.serving_root / "latest_offline_backtest_metrics.json"
        if serving_path.exists():
            try:
                return _sanitize_json_compatible(json.loads(serving_path.read_text(encoding="utf-8")))
            except Exception:
                pass
        if meeting_state_store:
            payload = meeting_state_store.get("latest_offline_backtest_metrics", {})
            if isinstance(payload, dict):
                return _sanitize_json_compatible(payload)
        return {}

    def _normalize_offline_self_improvement_payload(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        archive_manifest = payload.get("archive_ready_manifest")
        latest_descriptor = payload.get("latest_descriptor")
        serving_ready_payload = payload.get("serving_ready_latest_export")
        if isinstance(serving_ready_payload, dict):
            normalized = _sanitize_json_compatible(serving_ready_payload)
            if isinstance(archive_manifest, dict):
                normalized["archive_ready_manifest"] = _sanitize_json_compatible(archive_manifest)
            if isinstance(latest_descriptor, dict):
                normalized["latest_descriptor"] = _sanitize_json_compatible(latest_descriptor)
            return normalized
        export_payload = payload.get("self_improvement_export")
        if isinstance(export_payload, dict):
            normalized = _sanitize_json_compatible(export_payload)
            if isinstance(archive_manifest, dict):
                normalized["archive_ready_manifest"] = _sanitize_json_compatible(archive_manifest)
            if isinstance(latest_descriptor, dict):
                normalized["latest_descriptor"] = _sanitize_json_compatible(latest_descriptor)
            return normalized
        alias_payload = payload.get("export_packet")
        if isinstance(alias_payload, dict):
            normalized = _sanitize_json_compatible(alias_payload)
            if isinstance(archive_manifest, dict):
                normalized["archive_ready_manifest"] = _sanitize_json_compatible(archive_manifest)
            if isinstance(latest_descriptor, dict):
                normalized["latest_descriptor"] = _sanitize_json_compatible(latest_descriptor)
            return normalized
        return _sanitize_json_compatible(payload)

    def _ensure_offline_self_improvement_descriptors(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = _sanitize_json_compatible(payload)
        if not isinstance(normalized, dict) or not normalized:
            return {}
        if not isinstance(normalized.get("archive_ready_manifest"), dict):
            normalized["archive_ready_manifest"] = offline_backtest_runner.build_archive_ready_manifest(normalized)
        if not isinstance(normalized.get("latest_descriptor"), dict):
            normalized["latest_descriptor"] = offline_backtest_runner.build_latest_descriptor(
                normalized,
                archive_manifest=normalized["archive_ready_manifest"],
            )
        latest_descriptor = dict(normalized.get("latest_descriptor") or {})
        if not isinstance(latest_descriptor.get("archive_ref"), dict):
            latest_descriptor["archive_ref"] = offline_backtest_runner.build_archive_ref(
                normalized,
                archive_manifest=normalized["archive_ready_manifest"],
            )
        normalized["latest_descriptor"] = latest_descriptor
        if not isinstance(normalized.get("descriptor_contract_sample"), dict):
            normalized["descriptor_contract_sample"] = offline_backtest_runner.build_descriptor_contract_sample(
                latest_descriptor,
                archive_manifest=normalized["archive_ready_manifest"],
            )
        return normalized

    def _load_latest_offline_self_improvement() -> dict[str, Any]:
        serving_payload = _normalize_offline_self_improvement_payload(
            serving_store.get_latest_offline_self_improvement_export()
        )
        if serving_payload:
            return _ensure_offline_self_improvement_descriptors(serving_payload)
        serving_paths = [
            serving_store.layout.serving_root / "latest_offline_self_improvement_export.json",
            serving_store.layout.serving_root / "latest_offline_self_improvement.json",
        ]
        for serving_path in serving_paths:
            if not serving_path.exists():
                continue
            try:
                payload = _normalize_offline_self_improvement_payload(
                    json.loads(serving_path.read_text(encoding="utf-8"))
                )
            except Exception:
                payload = {}
            if payload:
                return _ensure_offline_self_improvement_descriptors(payload)
        if meeting_state_store:
            for key in ("latest_offline_self_improvement_export", "latest_offline_self_improvement"):
                payload = _normalize_offline_self_improvement_payload(meeting_state_store.get(key, {}))
                if payload:
                    return _ensure_offline_self_improvement_descriptors(payload)
        return {}

    def _load_latest_openclaw_packet(packet_type: str) -> dict[str, Any]:
        serving_payload = serving_store.get_latest_openclaw_packet(packet_type)
        if isinstance(serving_payload, dict):
            return _ensure_openclaw_latest_descriptor(_sanitize_json_compatible(serving_payload))
        if meeting_state_store:
            payload = meeting_state_store.get(f"latest_{packet_type}", {})
            if isinstance(payload, dict):
                return _ensure_openclaw_latest_descriptor(_sanitize_json_compatible(payload))
        return {}

    def _ensure_openclaw_latest_descriptor(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = _sanitize_json_compatible(payload)
        if not isinstance(normalized, dict) or not normalized:
            return {}
        if not isinstance(normalized.get("archive_manifest"), dict):
            packet_type = str(normalized.get("packet_type") or "")
            normalized = _normalize_openclaw_packet_payload(normalized, packet_type) if packet_type else normalized
        if not isinstance(normalized.get("latest_descriptor"), dict):
            if discussion_cycle_service:
                try:
                    normalized["latest_descriptor"] = _sanitize_json_compatible(
                        discussion_cycle_service.build_openclaw_latest_descriptor(normalized)
                    )
                except Exception:
                    pass
        if not isinstance(normalized.get("contract_sample"), dict) and discussion_cycle_service:
            try:
                normalized["contract_sample"] = _sanitize_json_compatible(
                    discussion_cycle_service.build_openclaw_contract_sample(normalized)
                )
            except Exception:
                pass
        return normalized

    def _normalize_openclaw_packet_payload(payload: Any, packet_type: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        packet_candidate = payload.get("packet") if isinstance(payload.get("packet"), dict) else payload
        if str(packet_candidate.get("packet_type") or "") != packet_type:
            return {}
        normalized = _sanitize_json_compatible(packet_candidate)
        if not isinstance(normalized.get("archive_manifest"), dict):
            if not discussion_cycle_service:
                return {}
            try:
                normalized["archive_manifest"] = _sanitize_json_compatible(
                    discussion_cycle_service.build_openclaw_archive_manifest(normalized)
                )
            except Exception:
                return {}
        if not isinstance(normalized.get("latest_descriptor"), dict):
            try:
                normalized["latest_descriptor"] = _sanitize_json_compatible(
                    discussion_cycle_service.build_openclaw_latest_descriptor(normalized)
                )
            except Exception:
                return {}
        return normalized

    def _resolve_openclaw_archive_packet(
        payload: OpenClawOpinionIngressInput,
        *,
        packet_type: str,
    ) -> dict[str, Any]:
        existing_packet = _normalize_openclaw_packet_payload(payload.payload, packet_type)
        if existing_packet:
            if discussion_cycle_service:
                existing_packet["archive_manifest"] = _sanitize_json_compatible(
                    discussion_cycle_service.build_openclaw_archive_manifest(existing_packet)
                )
            return existing_packet
        trade_date = _resolve_trade_date(payload.trade_date)
        if packet_type == "openclaw_replay_packet":
            return discussion_cycle_service.build_openclaw_replay_packet(
                payload.payload,
                trade_date=trade_date,
                expected_round=payload.expected_round,
                expected_agent_id=payload.expected_agent_id,
                expected_case_ids=(payload.expected_case_ids or None),
                case_id_map=(payload.case_id_map or None),
                default_case_id=payload.default_case_id,
            )
        return discussion_cycle_service.build_openclaw_proposal_packet(
            payload.payload,
            trade_date=trade_date,
            expected_round=payload.expected_round,
            expected_agent_id=payload.expected_agent_id,
            expected_case_ids=(payload.expected_case_ids or None),
            case_id_map=(payload.case_id_map or None),
            default_case_id=payload.default_case_id,
        )

    def _build_serving_latest_index() -> dict[str, Any]:
        latest_execution_bridge_health = (
            _sanitize_json_compatible(monitor_state_service.get_latest_execution_bridge_health())
            if monitor_state_service
            else {}
        )
        latest_execution_bridge_trend_summary = (
            _sanitize_json_compatible(monitor_state_service.get_execution_bridge_health_trend_summary())
            if monitor_state_service
            else {}
        )
        offline_self_improvement = _load_latest_offline_self_improvement()
        openclaw_replay_packet = _load_latest_openclaw_packet("openclaw_replay_packet")
        openclaw_proposal_packet = _load_latest_openclaw_packet("openclaw_proposal_packet")
        return {
            "available": bool(
                latest_execution_bridge_health
                or latest_execution_bridge_trend_summary
                or offline_self_improvement
                or openclaw_replay_packet
                or openclaw_proposal_packet
            ),
            "generated_at": datetime.now().isoformat(),
            "items": {
                "execution_bridge_health": {
                    "available": bool(latest_execution_bridge_health),
                    "template_path": "/system/monitor/execution-bridge-health/template",
                    "state_path": "/system/monitor/state",
                    "latest_descriptor": get_execution_bridge_health_latest_descriptor(),
                    "deployment_contract_sample": build_execution_bridge_health_deployment_contract_sample(),
                    "latest_overview": {
                        "overall_status": ((latest_execution_bridge_health.get("health") or {}).get("overall_status") or ""),
                        "source_id": ((latest_execution_bridge_health.get("health") or {}).get("source_id") or ""),
                        "deployment_role": ((latest_execution_bridge_health.get("health") or {}).get("deployment_role") or ""),
                        "bridge_path": ((latest_execution_bridge_health.get("health") or {}).get("bridge_path") or ""),
                        "trend_status": (latest_execution_bridge_trend_summary.get("trend_status") or ""),
                    },
                },
                "offline_self_improvement": {
                    "available": bool(offline_self_improvement),
                    "report_path": "/system/reports/offline-self-improvement",
                    "descriptor_path": "/system/reports/offline-self-improvement-descriptor",
                    "latest_descriptor": offline_self_improvement.get("latest_descriptor", {}),
                    "archive_ref": ((offline_self_improvement.get("latest_descriptor") or {}).get("archive_ref") or {}),
                    "descriptor_contract_sample": offline_self_improvement.get("descriptor_contract_sample", {}),
                },
                "openclaw_replay_packet": {
                    "available": bool(openclaw_replay_packet),
                    "report_path": "/system/reports/openclaw-replay-packet",
                    "latest_descriptor": openclaw_replay_packet.get("latest_descriptor", {}),
                    "contract_sample": openclaw_replay_packet.get("contract_sample", {}),
                },
                "openclaw_proposal_packet": {
                    "available": bool(openclaw_proposal_packet),
                    "report_path": "/system/reports/openclaw-proposal-packet",
                    "latest_descriptor": openclaw_proposal_packet.get("latest_descriptor", {}),
                    "contract_sample": openclaw_proposal_packet.get("contract_sample", {}),
                },
            },
            "summary_lines": [
                "serving latest index 聚合 execution bridge、offline self-improvement、OpenClaw replay/proposal packet。",
                "该索引只服务 Linux/OpenClaw 与 Windows Gateway 的只读契约消费，不新增 live 写口。",
            ],
        }

    def _build_deployment_bootstrap_contracts(account_id: str | None = None) -> dict[str, Any]:
        serving_latest_index = _build_serving_latest_index()
        readiness_payload = _build_readiness(account_id=account_id)
        execution_bridge_deployment_contract_sample = build_execution_bridge_health_deployment_contract_sample()
        return {
            "available": True,
            "generated_at": datetime.now().isoformat(),
            "architecture_boundary": {
                "linux_control_plane": "OpenClaw Gateway + Agent 团队 + ashare-system-v2",
                "windows_execution_plane": "Windows Execution Gateway + QMT / XtQuant",
                "single_live_writer": "Windows Execution Gateway",
                "self_improvement_flow": ["offline", "paper/supervised", "human_review", "deploy"],
            },
            "readiness": readiness_payload,
            "execution_bridge_template": build_execution_bridge_health_client_template(),
            "execution_bridge_deployment_contract_sample": execution_bridge_deployment_contract_sample,
            "deployment_contract_sample": execution_bridge_deployment_contract_sample,
            "serving_latest_index": serving_latest_index,
            "report_paths": {
                "readiness": "/system/readiness",
                "execution_bridge_health_template": "/system/monitor/execution-bridge-health/template",
                "linux_control_plane_startup_checklist": "/system/deployment/linux-control-plane-startup-checklist",
                "serving_latest_index": "/system/reports/serving-latest-index",
                "offline_self_improvement": "/system/reports/offline-self-improvement",
                "offline_self_improvement_descriptor": "/system/reports/offline-self-improvement-descriptor",
                "openclaw_replay_packet": "/system/reports/openclaw-replay-packet",
                "openclaw_proposal_packet": "/system/reports/openclaw-proposal-packet",
                "postclose_master": "/system/reports/postclose-master",
                "postclose_deployment_handoff": "/system/reports/postclose-deployment-handoff",
            },
            "summary_lines": [
                "bootstrap contracts 聚合 readiness、execution bridge template 与 serving latest index。",
                "Linux/OpenClaw 负责研究、编排、归档、review；Windows Execution Gateway 负责唯一执行写口。",
                "所有 replay/proposal/self-improvement 产物继续只服务离线研究，不自动进入 live。",
            ],
        }

    def _build_postclose_master_payload() -> dict[str, Any]:
        reports = _recent_reports()
        latest_runtime = _latest_runtime()
        latest_meeting = _latest_meeting()
        latest_exit_snapshot = (
            _sanitize_json_compatible(monitor_state_service.get_state().get("latest_exit_snapshot"))
            if monitor_state_service
            else None
        )
        latest_exit_snapshot_trend_summary = (
            _sanitize_json_compatible(monitor_state_service.get_state().get("exit_snapshot_trend_summary"))
            if monitor_state_service
            else None
        )
        latest_execution_bridge_health = (
            _sanitize_json_compatible(monitor_state_service.get_state().get("latest_execution_bridge_health"))
            if monitor_state_service
            else None
        )
        latest_execution_bridge_health_trend_summary = (
            _sanitize_json_compatible(monitor_state_service.get_state().get("execution_bridge_health_trend_summary"))
            if monitor_state_service
            else None
        )
        latest_offline_backtest_attribution = _load_latest_offline_backtest_attribution()
        latest_offline_backtest_metrics = _load_latest_offline_backtest_metrics()
        latest_offline_self_improvement = _load_latest_offline_self_improvement()
        latest_openclaw_replay_packet = _load_latest_openclaw_packet("openclaw_replay_packet")
        latest_openclaw_proposal_packet = _load_latest_openclaw_packet("openclaw_proposal_packet")
        latest_offline_self_improvement_descriptor = dict(latest_offline_self_improvement.get("latest_descriptor") or {})
        latest_openclaw_replay_descriptor = dict(latest_openclaw_replay_packet.get("latest_descriptor") or {})
        latest_openclaw_proposal_descriptor = dict(latest_openclaw_proposal_packet.get("latest_descriptor") or {})
        latest_review_board = (
            _sanitize_json_compatible(meeting_state_store.get("latest_review_board"))
            if meeting_state_store
            else None
        )
        return {
            "available": bool(
                reports
                or latest_runtime
                or latest_meeting
                or latest_exit_snapshot
                or latest_exit_snapshot_trend_summary
                or latest_execution_bridge_health
                or latest_execution_bridge_health_trend_summary
                or latest_offline_backtest_attribution
                or latest_offline_backtest_metrics
                or latest_offline_self_improvement
                or latest_openclaw_replay_packet
                or latest_openclaw_proposal_packet
                or latest_offline_self_improvement_descriptor
                or latest_openclaw_replay_descriptor
                or latest_openclaw_proposal_descriptor
                or latest_review_board
            ),
            "reports": reports,
            "latest_runtime": latest_runtime,
            "latest_meeting": latest_meeting,
            "latest_exit_snapshot": latest_exit_snapshot,
            "latest_exit_snapshot_trend_summary": latest_exit_snapshot_trend_summary,
            "latest_execution_bridge_health": latest_execution_bridge_health,
            "latest_execution_bridge_health_trend_summary": latest_execution_bridge_health_trend_summary,
            "latest_offline_backtest_attribution": latest_offline_backtest_attribution,
            "latest_offline_backtest_metrics": latest_offline_backtest_metrics,
            "latest_offline_self_improvement": latest_offline_self_improvement,
            "latest_offline_self_improvement_descriptor": latest_offline_self_improvement_descriptor,
            "latest_openclaw_replay_packet": latest_openclaw_replay_packet,
            "latest_openclaw_proposal_packet": latest_openclaw_proposal_packet,
            "latest_openclaw_replay_descriptor": latest_openclaw_replay_descriptor,
            "latest_openclaw_proposal_descriptor": latest_openclaw_proposal_descriptor,
            "latest_review_board": latest_review_board,
        }

    def _build_postclose_deployment_handoff(
        *,
        trade_date: str | None = None,
        score_date: str | None = None,
        tail_market_source: str = "latest",
        tail_market_limit: int = 20,
        due_within_days: int = 1,
        inspection_limit: int = 50,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        review_board = _build_postclose_review_board(
            trade_date=trade_date,
            score_date=score_date,
            tail_market_source=tail_market_source,
            tail_market_limit=tail_market_limit,
            due_within_days=due_within_days,
            inspection_limit=inspection_limit,
            persist_snapshot=False,
        )
        postclose_master = _build_postclose_master_payload()
        serving_latest_index = _build_serving_latest_index()
        deployment_bootstrap_contracts = _build_deployment_bootstrap_contracts(account_id=account_id)
        control_plane_gateway = dict(
            (review_board.get("sections") or {}).get("control_plane_gateway")
            or _build_control_plane_gateway_summary(trade_date=trade_date)
        )
        return {
            "available": bool(
                review_board.get("available")
                or postclose_master.get("available")
                or serving_latest_index.get("available")
                or deployment_bootstrap_contracts.get("available")
                or control_plane_gateway.get("available")
            ),
            "handoff_scope": "postclose_deployment_handoff",
            "generated_at": datetime.now().isoformat(),
            "report_paths": deployment_bootstrap_contracts.get("report_paths", {}),
            "pending_intent_count": int(control_plane_gateway.get("pending_intent_count", 0) or 0),
            "queued_for_gateway_count": int(control_plane_gateway.get("queued_for_gateway_count", 0) or 0),
            "latest_gateway_source": str(control_plane_gateway.get("latest_gateway_source") or ""),
            "latest_receipt_summary": dict(control_plane_gateway.get("latest_receipt_summary") or {}),
            "discussion_dispatch_queued_for_gateway_count": int(
                ((control_plane_gateway.get("discussion_dispatch") or {}).get("queued_for_gateway_count", 0) or 0)
            ),
            "tail_market_queued_for_gateway_count": int(
                ((control_plane_gateway.get("tail_market") or {}).get("queued_for_gateway_count", 0) or 0)
            ),
            "sections": {
                "review_board": review_board,
                "postclose_master": postclose_master,
                "serving_latest_index": serving_latest_index,
                "deployment_bootstrap_contracts": deployment_bootstrap_contracts,
                "control_plane_gateway": control_plane_gateway,
            },
            "summary_lines": [
                "postclose deployment handoff 聚合 review-board、postclose-master、serving latest index 与 bootstrap contracts。",
                "用于 Linux/OpenClaw 在盘后交接时一次拿全研究总览、latest 引用与部署契约。",
                "该 handoff 只读，不新增 live 写口，也不越过 Windows Execution Gateway。",
                "主控执行: " + str(((control_plane_gateway.get("summary_lines") or ["当前尚无主控执行队列摘要。"])[0])),
            ],
        }

    def _build_linux_control_plane_startup_checklist(account_id: str | None = None) -> dict[str, Any]:
        bootstrap_contracts = _build_deployment_bootstrap_contracts(account_id=account_id)
        serving_latest_index = bootstrap_contracts.get("serving_latest_index", {})
        handoff = _build_postclose_deployment_handoff(account_id=account_id)
        readiness = bootstrap_contracts.get("readiness", {})
        execution_bridge_overview = (
            ((serving_latest_index.get("items") or {}).get("execution_bridge_health") or {}).get("latest_overview") or {}
        )
        offline_self_improvement = ((serving_latest_index.get("items") or {}).get("offline_self_improvement") or {})
        openclaw_replay = ((serving_latest_index.get("items") or {}).get("openclaw_replay_packet") or {})
        openclaw_proposal = ((serving_latest_index.get("items") or {}).get("openclaw_proposal_packet") or {})

        readiness_status = str(readiness.get("status") or "blocked")
        readiness_check_status = "ok" if readiness_status == "ready" else ("warning" if readiness_status == "degraded" else "blocked")
        execution_bridge_status = str(execution_bridge_overview.get("overall_status") or "unknown")
        execution_bridge_check_status = (
            "ok"
            if execution_bridge_status in {"healthy", "ready"}
            else ("warning" if execution_bridge_status in {"degraded", "unknown"} else "blocked")
        )
        offline_descriptor_status = "ok" if offline_self_improvement.get("available") else "warning"
        replay_descriptor_status = "ok" if openclaw_replay.get("available") else "warning"
        proposal_descriptor_status = "ok" if openclaw_proposal.get("available") else "warning"
        handoff_status = "ok" if handoff.get("available") else "warning"

        checks = [
            {
                "name": "readiness",
                "status": readiness_check_status,
                "detail": f"readiness={readiness_status}",
                "path": "/system/readiness",
            },
            {
                "name": "execution_bridge_contract",
                "status": execution_bridge_check_status,
                "detail": (
                    f"overall_status={execution_bridge_status} "
                    f"source_id={execution_bridge_overview.get('source_id', '')} "
                    f"bridge_path={execution_bridge_overview.get('bridge_path', '')}"
                ).strip(),
                "path": "/system/monitor/execution-bridge-health/template",
            },
            {
                "name": "offline_self_improvement_descriptor",
                "status": offline_descriptor_status,
                "detail": (
                    f"available={bool(offline_self_improvement.get('available'))} "
                    f"report={offline_self_improvement.get('descriptor_path', '')}"
                ).strip(),
                "path": "/system/reports/offline-self-improvement-descriptor",
            },
            {
                "name": "openclaw_replay_descriptor",
                "status": replay_descriptor_status,
                "detail": (
                    f"available={bool(openclaw_replay.get('available'))} "
                    f"report={openclaw_replay.get('report_path', '')}"
                ).strip(),
                "path": "/system/reports/openclaw-replay-packet",
            },
            {
                "name": "openclaw_proposal_descriptor",
                "status": proposal_descriptor_status,
                "detail": (
                    f"available={bool(openclaw_proposal.get('available'))} "
                    f"report={openclaw_proposal.get('report_path', '')}"
                ).strip(),
                "path": "/system/reports/openclaw-proposal-packet",
            },
            {
                "name": "postclose_handoff",
                "status": handoff_status,
                "detail": f"available={bool(handoff.get('available'))} handoff_scope={handoff.get('handoff_scope', '')}".strip(),
                "path": "/system/reports/postclose-deployment-handoff",
            },
        ]
        check_statuses = {item["status"] for item in checks}
        overall_status = "ready"
        if "blocked" in check_statuses:
            overall_status = "blocked"
        elif "warning" in check_statuses:
            overall_status = "degraded"
        return {
            "available": True,
            "checklist_scope": "linux_control_plane_startup_checklist",
            "generated_at": datetime.now().isoformat(),
            "status": overall_status,
            "checks": checks,
            "bootstrap_contracts_path": "/system/deployment/bootstrap-contracts",
            "serving_latest_index_path": "/system/reports/serving-latest-index",
            "handoff_path": "/system/reports/postclose-deployment-handoff",
            "summary_lines": [
                f"Linux control plane 启动检查: status={overall_status}。",
                f"readiness={readiness_status} execution_bridge={execution_bridge_status} offline_self_improvement={'yes' if offline_self_improvement.get('available') else 'no'}。",
                "该 checklist 只读，用于 Linux/OpenClaw 启动与切日检查，不新增 live 写口。",
            ],
        }

    def _build_windows_execution_gateway_onboarding_bundle(account_id: str | None = None) -> dict[str, Any]:
        bootstrap_contracts = _build_deployment_bootstrap_contracts(account_id=account_id)
        startup_checklist = _build_linux_control_plane_startup_checklist(account_id=account_id)
        execution_bridge_template = build_execution_bridge_health_client_template()
        deployment_contract_sample = dict(
            bootstrap_contracts.get("execution_bridge_deployment_contract_sample")
            or bootstrap_contracts.get("deployment_contract_sample")
            or {}
        )
        report_paths = dict(bootstrap_contracts.get("report_paths") or {})
        return {
            "available": True,
            "bundle_scope": "windows_execution_gateway_onboarding_bundle",
            "generated_at": datetime.now().isoformat(),
            "architecture_boundary": bootstrap_contracts.get("architecture_boundary", {}),
            "execution_bridge_template": execution_bridge_template,
            "deployment_contract_sample": deployment_contract_sample,
            "source_value_suggestions": execution_bridge_template.get("source_value_suggestions", {}),
            "latest_read_descriptor": execution_bridge_template.get("latest_read_descriptor", {}),
            "worker_entrypoint": {
                "cli": "ashare-execution-gateway-worker",
                "module": "ashare_system.windows_execution_gateway_worker",
                "recommended_once_command": (
                    "ashare-execution-gateway-worker "
                    "--control-plane-base-url http://127.0.0.1:8100 "
                    "--source-id windows-vm-a "
                    "--deployment-role primary_gateway "
                    "--bridge-path \"linux_openclaw -> windows_gateway -> qmt_vm\" "
                    "--executor-mode fail_unconfigured --once"
                ),
                "recommended_xtquant_command": (
                    "ashare-execution-gateway-worker "
                    "--control-plane-base-url http://127.0.0.1:8100 "
                    "--source-id windows-vm-a "
                    "--deployment-role primary_gateway "
                    "--bridge-path \"linux_openclaw -> windows_gateway -> qmt_vm\" "
                    "--executor-mode xtquant --once"
                ),
                "required_env": {
                    "ASHARE_EXECUTION_PLANE": "windows_gateway",
                    "ASHARE_RUN_MODE": "paper|live",
                },
                "notes": [
                    "默认 executor_mode=fail_unconfigured，不会在未接真实执行器时误触 live。",
                    "显式 executor_mode=xtquant 时，worker 会直接创建 XtQuantExecutionAdapter 并调用真实 place_order。",
                    "联调期可改用 noop_success 验证 poll/claim/receipt 协议闭环，但该模式不可替代真实 QMT 执行。",
                ],
            },
            "linux_control_plane": {
                "startup_checklist_status": startup_checklist.get("status", "blocked"),
                "startup_checklist_path": "/system/deployment/linux-control-plane-startup-checklist",
                "bootstrap_contracts_path": "/system/deployment/bootstrap-contracts",
                "handoff_path": "/system/reports/postclose-deployment-handoff",
                "serving_latest_index_path": "/system/reports/serving-latest-index",
                "summary_lines": startup_checklist.get("summary_lines", []),
            },
            "report_paths": report_paths,
            "summary_lines": [
                "Windows Execution Gateway onboarding bundle 聚合 execution bridge template、source 建议与 Linux 控制面只读路径。",
                "Windows 侧只负责执行桥上报与 QMT 连接，不直接承载 OpenClaw 研究与自我进化逻辑。",
                "该 bundle 只读，不新增 live 写口，也不改变 Windows Execution Gateway 的唯一执行写口边界。",
            ],
        }

    def _save_monitor_pool_snapshot(trade_date: str, cycle=None, source: str = "system") -> None:
        if not (monitor_state_service and candidate_case_service):
            return
        focus_case_ids = getattr(cycle, "focus_pool_case_ids", None) if cycle else None
        execution_case_ids = getattr(cycle, "execution_pool_case_ids", None) if cycle else None
        monitor_state_service.save_pool_snapshot(
            trade_date=trade_date,
            pool_snapshot=candidate_case_service.build_pool_snapshot(
                trade_date,
                focus_case_ids=focus_case_ids,
                execution_case_ids=execution_case_ids,
            ),
            source=source,
            discussion_state=(getattr(cycle, "discussion_state", None) if cycle else None),
            pool_state=(getattr(cycle, "pool_state", None) if cycle else None),
        )
        if monitor_change_notifier:
            monitor_change_notifier.dispatch_latest()

    def _resolve_account_id(preferred: str | None = None) -> str:
        if preferred:
            return preferred
        runtime = _latest_runtime()
        if runtime.get("account_id"):
            return str(runtime["account_id"])
        return settings.xtquant.account_id

    def _build_readiness(account_id: str | None = None) -> dict:
        resolved_account_id = _resolve_account_id(account_id)
        health_result = EnvironmentHealthcheck(settings).run()
        checks: list[dict[str, Any]] = []
        for item in health_result.checks:
            normalized = dict(item)
            if settings.run_mode != "live" and item["name"] in {"xtquant_root", "xtquantservice_root"}:
                if item["status"] == "missing":
                    normalized["status"] = "warning"
            checks.append(normalized)

        runtime_config = config_mgr.get() if config_mgr else None
        emergency_stop_active = bool(getattr(runtime_config, "emergency_stop", False))
        trading_halt_reason = str(getattr(runtime_config, "trading_halt_reason", "") or "").strip()
        pending_order_warn_seconds = int(getattr(runtime_config, "pending_order_warn_seconds", 300) or 300)

        checks.append(
            {
                "name": "emergency_stop",
                "status": "blocked" if emergency_stop_active else "ok",
                "detail": (trading_halt_reason or "inactive"),
            }
        )

        latest_account_state = (
            account_state_service.snapshot(resolved_account_id, persist=False)
            if account_state_service
            else None
        )
        account_detail = None
        account_access_ok = False
        account_error = None
        if latest_account_state and latest_account_state.get("status") == "ok":
            account_detail = {
                "account_id": latest_account_state.get("fetched_account_id"),
                "cash": latest_account_state["metrics"]["cash"],
                "total_asset": latest_account_state["metrics"]["total_asset"],
                "position_count": latest_account_state.get("position_count", 0),
                "verified": latest_account_state.get("verified"),
                "config_match": latest_account_state.get("config_match"),
            }
            account_access_ok = bool(latest_account_state.get("verified"))
            if settings.run_mode == "live":
                account_access_ok = account_access_ok and bool(latest_account_state.get("config_match"))
        elif latest_account_state:
            account_error = latest_account_state.get("error")
        checks.append(
            {
                "name": "account_access",
                "status": ("ok" if account_access_ok else "invalid"),
                "detail": (account_error or json.dumps(account_detail, ensure_ascii=False)),
            }
        )

        startup_recovery = (
            meeting_state_store.get("latest_startup_recovery", {})
            if meeting_state_store
            else {}
        ) or {}
        startup_status = startup_recovery.get("status", "missing")
        checks.append(
            {
                "name": "startup_recovery",
                "status": (
                    "ok"
                    if startup_status == "ok"
                    else ("warning" if startup_status == "missing" and settings.run_mode != "live" else "invalid")
                ),
                "detail": startup_status,
            }
        )

        pending_order_inspection = (
            pending_order_inspection_service.inspect(
                resolved_account_id,
                warn_after_seconds=pending_order_warn_seconds,
                persist=False,
            )
            if pending_order_inspection_service
            else {
                "status": "error",
                "error": "pending order inspection service not initialized",
                "pending_count": 0,
                "warning_count": 0,
                "stale_count": 0,
                "summary_lines": ["pending order inspection unavailable"],
            }
        )
        latest_execution_reconciliation = (
            meeting_state_store.get("latest_execution_reconciliation", {})
            if meeting_state_store
            else {}
        ) or {}
        latest_pending_order_remediation = (
            meeting_state_store.get("latest_pending_order_remediation", {})
            if meeting_state_store
            else {}
        ) or {}
        checks.append(
            {
                "name": "account_identity",
                "status": (
                    "ok"
                    if latest_account_state and latest_account_state.get("status") == "ok" and latest_account_state.get("verified")
                    and (latest_account_state.get("config_match") or settings.run_mode != "live")
                    else ("warning" if settings.run_mode != "live" else "invalid")
                ),
                "detail": (
                    latest_account_state.get("summary_lines", ["unavailable"])[0]
                    if latest_account_state
                    else "unavailable"
                ),
            }
        )
        inspection_status = pending_order_inspection.get("status")
        checks.append(
            {
                "name": "pending_order_inspection",
                "status": (
                    "ok"
                    if inspection_status in {"clear", "pending"}
                    else ("warning" if inspection_status == "warning" else "invalid")
                ),
                "detail": (
                    pending_order_inspection.get("error")
                    or f"pending={pending_order_inspection.get('pending_count', 0)} warning={pending_order_inspection.get('warning_count', 0)} stale={pending_order_inspection.get('stale_count', 0)}"
                ),
            }
        )
        remediation_status = latest_pending_order_remediation.get("status", "missing")
        checks.append(
            {
                "name": "pending_order_remediation",
                "status": (
                    "ok"
                    if remediation_status in {"actioned", "no_action"}
                    else ("warning" if remediation_status == "missing" and settings.run_mode != "live" else "invalid")
                ),
                "detail": (
                    remediation_status
                    if remediation_status == "missing"
                    else f"action={latest_pending_order_remediation.get('auto_action')} stale={latest_pending_order_remediation.get('stale_count', 0)} actioned={latest_pending_order_remediation.get('actioned_count', 0)}"
                ),
            }
        )
        reconciliation_status = latest_execution_reconciliation.get("status", "missing")
        checks.append(
            {
                "name": "execution_reconciliation",
                "status": (
                    "ok"
                    if reconciliation_status == "ok"
                    else ("warning" if reconciliation_status == "missing" and settings.run_mode != "live" else "invalid")
                ),
                "detail": (
                    reconciliation_status
                    if reconciliation_status == "missing"
                    else f"matched={latest_execution_reconciliation.get('matched_order_count', 0)} trades={latest_execution_reconciliation.get('trade_count', 0)} orphan_trades={latest_execution_reconciliation.get('orphan_trade_count', 0)}"
                ),
            }
        )

        check_statuses = {item["status"] for item in checks}
        status = "ready"
        if "invalid" in check_statuses or "blocked" in check_statuses:
            status = "blocked"
        elif "warning" in check_statuses:
            status = "degraded"
        return {
            "status": status,
            "run_mode": settings.run_mode,
            "account_id": resolved_account_id,
            "generated_at": datetime.now().isoformat(),
            "checks": checks,
            "account_detail": account_detail,
            "account_state": latest_account_state,
            "startup_recovery": startup_recovery,
            "pending_order_inspection": pending_order_inspection,
            "pending_order_remediation": latest_pending_order_remediation,
            "execution_reconciliation": latest_execution_reconciliation,
            "summary_lines": [
                f"实盘就绪检查: status={status} run_mode={settings.run_mode} account={resolved_account_id}。",
                f"未决订单 pending={pending_order_inspection.get('pending_count', 0)} warning={pending_order_inspection.get('warning_count', 0)} stale={pending_order_inspection.get('stale_count', 0)}。",
            ],
        }

    def _build_execution_precheck(trade_date: str, account_id: str | None = None) -> dict:
        resolved_account_id = _resolve_account_id(account_id)
        cycle = discussion_cycle_service.get_cycle(trade_date) if discussion_cycle_service else None
        case_map = {
            item.case_id: item
            for item in (
                candidate_case_service.list_cases(trade_date=trade_date, limit=500)
                if candidate_case_service
                else []
            )
        }
        execution_case_ids = cycle.execution_pool_case_ids if cycle else []
        balance_error = None
        balance = None
        positions = []
        if execution_adapter:
            try:
                balance = execution_adapter.get_balance(resolved_account_id)
                positions = execution_adapter.get_positions(resolved_account_id)
            except Exception as exc:
                balance_error = str(exc)
        account_state = (
            account_state_service.snapshot(resolved_account_id, persist=True)
            if account_state_service
            else None
        )
        total_asset = float(getattr(balance, "total_asset", 0.0) or 0.0)
        cash = float(getattr(balance, "cash", 0.0) or 0.0)
        runtime_config = config_mgr.get() if config_mgr else None
        now = datetime.now()
        market_snapshot_ttl_seconds = int(
            getattr(getattr(runtime_config, "snapshots", None), "market_snapshot_ttl_seconds", 600) or 600
        )
        max_price_deviation_pct = float(
            getattr(runtime_config, "execution_price_deviation_pct", 0.02) or 0.02
        )
        emergency_stop_active = bool(getattr(runtime_config, "emergency_stop", False))
        trading_halt_reason = str(getattr(runtime_config, "trading_halt_reason", "") or "").strip()
        session_open = is_trading_session(now)
        position_buckets = summarize_position_buckets(positions)
        invested_value = position_buckets.equity_value
        reverse_repo_value = position_buckets.reverse_repo_value
        gross_invested_value = position_buckets.total_value
        current_total_ratio = (invested_value / total_asset) if total_asset > 0 else 0.0
        gross_total_ratio = (gross_invested_value / total_asset) if total_asset > 0 else 0.0
        max_total_position = (
            float(parameter_service.get_param_value("max_total_position"))
            if parameter_service
            else 0.8
        )
        equity_position_limit = (
            float(parameter_service.get_param_value("equity_position_limit"))
            if parameter_service
            else float(getattr(runtime_config, "equity_position_limit", 0.2) or 0.2)
        )
        max_single_position = (
            float(parameter_service.get_param_value("max_single_position"))
            if parameter_service
            else 0.25
        )
        daily_loss_limit = (
            float(parameter_service.get_param_value("daily_loss_limit"))
            if parameter_service
            else 0.05
        )
        max_single_amount = (
            float(parameter_service.get_param_value("max_single_amount"))
            if parameter_service
            else float(getattr(runtime_config, "max_single_amount", 50_000.0) or 50_000.0)
        )
        reverse_repo_target_ratio = (
            float(parameter_service.get_param_value("reverse_repo_target_ratio"))
            if parameter_service
            else float(getattr(runtime_config, "reverse_repo_target_ratio", 0.7) or 0.7)
        )
        minimum_total_invested_amount = (
            float(parameter_service.get_param_value("minimum_total_invested_amount"))
            if parameter_service
            else float(getattr(runtime_config, "minimum_total_invested_amount", 100000.0) or 100000.0)
        )
        reverse_repo_reserved_amount = (
            float(parameter_service.get_param_value("reverse_repo_reserved_amount"))
            if parameter_service
            else float(getattr(runtime_config, "reverse_repo_reserved_amount", 70000.0) or 70000.0)
        )
        trading_budget = build_test_trading_budget(
            invested_value,
            reverse_repo_value,
            minimum_total_invested_amount=minimum_total_invested_amount,
            reverse_repo_reserved_amount=reverse_repo_reserved_amount,
        )
        stock_test_budget_amount = trading_budget.stock_test_budget_amount
        effective_total_position_limit = min(
            max_total_position,
            equity_position_limit,
        )
        reverse_repo_target_value = total_asset * reverse_repo_target_ratio
        reverse_repo_gap_value = trading_budget.reverse_repo_gap_value
        daily_pnl = float(((account_state or {}).get("metrics") or {}).get("daily_pnl", 0.0) or 0.0)

        live_market_quote_error = None
        live_market_quote_map: dict[str, Any] = {}
        live_quote_timestamp = now.isoformat()
        if settings.run_mode == "live" and market_adapter:
            live_symbols = [case_map[case_id].symbol for case_id in execution_case_ids if case_id in case_map]
            if live_symbols:
                try:
                    live_market_quote_map = {
                        item.symbol: item for item in market_adapter.get_snapshots(live_symbols)
                    }
                except Exception as exc:
                    live_market_quote_error = str(exc)
        execution_context = _resolve_execution_strategy_context()
        playbook_map = execution_context["playbook_map"]
        dossier_map = execution_context["dossier_map"]
        regime_value = execution_context["regime_value"]

        items: list[dict[str, Any]] = []
        approved_count = 0
        blocked_count = 0
        for case_id in execution_case_ids:
            case = case_map.get(case_id)
            if case is None:
                continue
            playbook_context = playbook_map.get(case.symbol) or {}
            dossier_item = dossier_map.get(case.symbol) or {}
            resolved_playbook = (
                playbook_context.get("playbook")
                or dossier_item.get("assigned_playbook")
            )
            resolved_sector = (
                playbook_context.get("sector")
                or dossier_item.get("resolved_sector")
                or dossier_item.get("sector")
            )
            runtime_market_snapshot = case.runtime_snapshot.market_snapshot or {}
            latest_price = float(runtime_market_snapshot.get("last_price") or 0.0)
            bid_price = float(runtime_market_snapshot.get("bid_price") or 0.0)
            ask_price = float(runtime_market_snapshot.get("ask_price") or 0.0)
            pre_close = float(runtime_market_snapshot.get("pre_close") or 0.0)
            quote_timestamp = (
                runtime_market_snapshot.get("quote_timestamp")
                or runtime_market_snapshot.get("snapshot_at")
                or runtime_market_snapshot.get("captured_at")
            )
            live_quote = live_market_quote_map.get(case.symbol)
            if live_quote is not None:
                latest_price = float(live_quote.last_price or 0.0)
                bid_price = float(live_quote.bid_price or 0.0)
                ask_price = float(live_quote.ask_price or 0.0)
                pre_close = float(live_quote.pre_close or 0.0)
                quote_timestamp = live_quote_timestamp
            elif latest_price <= 0 and market_adapter and settings.run_mode != "live":
                snapshots = market_adapter.get_snapshots([case.symbol])
                if snapshots:
                    latest_price = float(snapshots[0].last_price or 0.0)
                    bid_price = float(snapshots[0].bid_price or 0.0)
                    ask_price = float(snapshots[0].ask_price or 0.0)
                    pre_close = float(snapshots[0].pre_close or 0.0)
                    quote_timestamp = now.isoformat()
            quote_age_seconds = snapshot_age_seconds(quote_timestamp, now)
            quote_is_fresh = is_snapshot_fresh(quote_timestamp, now, market_snapshot_ttl_seconds)
            current_position = next((item for item in positions if item.symbol == case.symbol), None)
            current_symbol_value = (
                float(current_position.quantity) * float(current_position.last_price)
                if current_position
                else 0.0
            )
            remaining_risk_total_value = max(total_asset * effective_total_position_limit - invested_value, 0.0)
            remaining_test_budget_value = trading_budget.stock_test_budget_remaining
            remaining_total_value = remaining_risk_total_value
            remaining_single_value = max(total_asset * max_single_position - current_symbol_value, 0.0)
            budget_value = min(cash, remaining_total_value, remaining_single_value, max_single_amount)
            proposed_quantity = int(budget_value / max(latest_price, 1e-9) / 100) * 100 if latest_price > 0 else 0
            proposed_value = round(proposed_quantity * latest_price, 2) if latest_price > 0 else 0.0
            blockers: list[str] = []
            if case.risk_gate == "reject":
                blockers.append("risk_gate_reject")
            if case.audit_gate == "hold":
                blockers.append("audit_gate_hold")
            if balance_error:
                blockers.append("balance_unavailable")
            if latest_price <= 0:
                blockers.append("market_price_unavailable")
            if remaining_total_value <= 0:
                blockers.append("total_position_limit_reached")
            if remaining_single_value <= 0:
                blockers.append("single_position_limit_reached")
            if cash <= 0:
                blockers.append("cash_unavailable")
            min_lot_value = round(latest_price * 100, 2) if latest_price > 0 else 0.0
            if latest_price > 0 and budget_value < min_lot_value:
                blockers.append("budget_below_min_lot")
            elif proposed_quantity < 100:
                blockers.append("order_lot_insufficient")
            if proposed_value > cash + 1e-6:
                blockers.append("cash_exceeded")
            if proposed_value > max_single_amount + 1e-6:
                blockers.append("single_amount_exceeded")
            if emergency_stop_active:
                blockers.append("emergency_stop_active")
            if settings.run_mode == "live":
                if not session_open:
                    blockers.append("trading_session_closed")
                if live_market_quote_error:
                    blockers.append("market_snapshot_fetch_failed")
                elif live_quote is None:
                    blockers.append("market_snapshot_unavailable")
                elif not quote_is_fresh:
                    blockers.append("market_snapshot_stale")
                if is_limit_up(case.symbol, latest_price, pre_close):
                    blockers.append("limit_up_locked")
                if is_limit_down(case.symbol, latest_price, pre_close):
                    blockers.append("limit_down_locked")
                if is_price_deviation_exceeded(
                    latest_price,
                    bid_price,
                    ask_price,
                    max_price_deviation_pct,
                ):
                    blockers.append("price_deviation_exceeded")

            approved = False
            guard_reason = "skipped"
            adjusted_request = None
            if not blockers and balance:
                request = PlaceOrderRequest(
                    account_id=resolved_account_id,
                    symbol=case.symbol,
                    side="BUY",
                    quantity=proposed_quantity,
                    price=latest_price,
                    request_id=f"precheck-{trade_date}-{case.symbol}",
                    decision_id=case.case_id,
                    trade_date=trade_date,
                    playbook=resolved_playbook,
                    regime=regime_value,
                )
                approved, guard_reason, adjusted_request = execution_guard.approve(
                    request,
                    balance,
                    daily_pnl=daily_pnl,
                )
                if not approved:
                    blockers.append("guard_reject")
                elif adjusted_request and adjusted_request.quantity != proposed_quantity:
                    proposed_quantity = adjusted_request.quantity
                    proposed_value = round(adjusted_request.quantity * adjusted_request.price, 2)

            blockers = _sort_execution_blockers(blockers)
            primary_blocker = blockers[0] if blockers else None
            next_actions = _build_execution_next_actions(blockers)
            primary_next_action = next_actions[0]["code"] if next_actions else None
            primary_next_action_label = next_actions[0]["label"] if next_actions else None

            item = {
                "case_id": case.case_id,
                "symbol": case.symbol,
                "name": case.name,
                "risk_gate": case.risk_gate,
                "audit_gate": case.audit_gate,
                "final_status": case.final_status,
                "latest_price": latest_price,
                "bid_price": bid_price,
                "ask_price": ask_price,
                "pre_close": pre_close,
                "quote_timestamp": quote_timestamp,
                "snapshot_age_seconds": quote_age_seconds,
                "snapshot_is_fresh": quote_is_fresh,
                "session_open": session_open,
                "max_price_deviation_pct": max_price_deviation_pct,
                "current_position_quantity": (current_position.quantity if current_position else 0),
                "current_position_value": round(current_symbol_value, 2),
                "current_total_ratio": round(current_total_ratio, 4),
                "gross_total_ratio": round(gross_total_ratio, 4),
                "max_total_position": max_total_position,
                "effective_total_position_limit": effective_total_position_limit,
                "equity_position_limit": equity_position_limit,
                "max_single_position": max_single_position,
                "max_single_amount": round(max_single_amount, 2),
                "daily_loss_limit": daily_loss_limit,
                "daily_pnl": round(daily_pnl, 4),
                "equity_invested_value": round(invested_value, 2),
                "gross_invested_value": round(gross_invested_value, 2),
                "reverse_repo_value": round(reverse_repo_value, 2),
                "reverse_repo_target_ratio": round(reverse_repo_target_ratio, 4),
                "reverse_repo_target_value": round(reverse_repo_target_value, 2),
                "reverse_repo_gap_value": round(reverse_repo_gap_value, 2),
                "minimum_total_invested_amount": round(minimum_total_invested_amount, 2),
                "reverse_repo_reserved_amount": round(reverse_repo_reserved_amount, 2),
                "stock_test_budget_amount": round(stock_test_budget_amount, 2),
                "stock_test_budget_remaining": round(remaining_test_budget_value, 2),
                "remaining_total_value": round(remaining_total_value, 2),
                "remaining_risk_total_value": round(remaining_risk_total_value, 2),
                "remaining_single_value": round(remaining_single_value, 2),
                "cash_available": round(cash, 2),
                "budget_value": round(budget_value, 2),
                "min_lot_value": min_lot_value,
                "proposed_quantity": proposed_quantity,
                "proposed_value": proposed_value,
                "approved": approved and not blockers,
                "guard_reason": guard_reason,
                "primary_blocker": primary_blocker,
                "primary_blocker_label": (_execution_blocker_label(primary_blocker) if primary_blocker else None),
                "blockers": blockers,
                "blocker_labels": [_execution_blocker_label(code) for code in blockers],
                "recommended_next_actions": next_actions,
                "primary_recommended_next_action": primary_next_action,
                "primary_recommended_next_action_label": primary_next_action_label,
                "headline_reason": case.selected_reason or case.runtime_snapshot.summary,
                "playbook": resolved_playbook,
                "regime": regime_value,
                "resolved_sector": resolved_sector,
                "playbook_entry_window": playbook_context.get("entry_window"),
            }
            items.append(item)
            if item["approved"]:
                approved_count += 1
            else:
                blocked_count += 1

        status = "ready" if items and approved_count > 0 and not balance_error else "blocked"
        precheck_degrade_reason = None
        if emergency_stop_active:
            precheck_degrade_reason = "emergency_stop_active"
        elif balance_error:
            precheck_degrade_reason = "balance_unavailable"
        elif live_market_quote_error:
            precheck_degrade_reason = "market_snapshot_fetch_failed"
        elif settings.run_mode == "live" and approved_count == 0:
            precheck_degrade_reason = _pick_execution_degrade_reason(items)
        degraded = settings.run_mode == "live" and precheck_degrade_reason is not None
        payload_next_actions = _build_execution_next_actions([item for entry in items for item in entry.get("blockers", [])])
        primary_payload_next_action = payload_next_actions[0]["code"] if payload_next_actions else None
        primary_payload_next_action_label = payload_next_actions[0]["label"] if payload_next_actions else None
        summary_lines = [
            f"账户 {resolved_account_id} 执行预检: 通过 {approved_count}，阻断 {blocked_count}。",
            f"账户资产 total={round(total_asset, 2)} cash={round(cash, 2)} equity={round(invested_value, 2)} repo={round(reverse_repo_value, 2)} gross={round(gross_invested_value, 2)} daily_pnl={daily_pnl:+.2f}。",
            f"测试基线 total>={round(minimum_total_invested_amount, 2)} repo={round(reverse_repo_reserved_amount, 2)} stock_budget={round(stock_test_budget_amount, 2)} remaining={round(trading_budget.stock_test_budget_remaining, 2)}。",
            f"执行约束 equity_position<={effective_total_position_limit:.0%} raw_risk_limit<={max_total_position:.0%} single_position<={max_single_position:.0%} single_amount<={round(max_single_amount, 2)}。",
        ]
        if reverse_repo_value > 0:
            summary_lines.append(
                f"检测到逆回购仓位 {gross_total_ratio - current_total_ratio:.2%}，按类现金仓处理；当前股票测试仓位按 {current_total_ratio:.2%}/{effective_total_position_limit:.2%} 计算。"
            )
        elif reverse_repo_target_ratio > 0:
            summary_lines.append(
                f"逆回购目标占比 {reverse_repo_target_ratio:.0%}，当前缺口约 {round(reverse_repo_gap_value, 2)}。"
            )
        if execution_case_ids:
            summary_lines.append(f"当前执行池 {len(execution_case_ids)} 只。")
        if items:
            blocked_items = [item for item in items if not item.get("approved")]
            if blocked_items:
                lead = blocked_items[0]
                summary_lines.append(
                    f"首个阻断标的 {lead['symbol']} {lead.get('name') or lead['symbol']}：{lead.get('primary_blocker_label') or lead.get('primary_blocker')}"
                    f"，budget={round(lead.get('budget_value', 0.0), 2)}，min_lot={round(lead.get('min_lot_value', 0.0), 2)}。"
                )
                if lead.get("primary_blocker") == "stock_test_budget_reached":
                    summary_lines.append(
                        f"股票测试预算 {round(lead.get('stock_test_budget_amount', 0.0), 2)} 已用尽，当前股票持仓 {round(lead.get('equity_invested_value', 0.0), 2)}，需腾挪股票仓位后再试。"
                    )
                elif lead.get("primary_blocker") == "total_position_limit_reached":
                    summary_lines.append(
                        f"股票测试仓位已达 {lead.get('current_total_ratio', 0.0):.2%}，高于当前上限 {lead.get('effective_total_position_limit', 0.0):.2%}，新增测试预算被压缩为 0。"
                    )
                elif lead.get("primary_blocker") == "single_position_limit_reached":
                    summary_lines.append(
                        f"该标的单票额度剩余 {round(lead.get('remaining_single_value', 0.0), 2)}，不足继续扩仓。"
                    )
                elif lead.get("primary_blocker") in {"budget_below_min_lot", "order_lot_insufficient"}:
                    summary_lines.append(
                        f"按最新价 {round(lead.get('latest_price', 0.0), 3)} 计算，一手约 {round(lead.get('min_lot_value', 0.0), 2)}，当前预算不足形成有效委托。"
                    )
        summary_lines.extend(_build_execution_next_action_lines(payload_next_actions))
        payload = {
            "trade_date": trade_date,
            "account_id": resolved_account_id,
            "status": status,
            "available": bool(items),
            "degraded": degraded,
            "degrade_to": ("blocked" if degraded else None),
            "degrade_reason": precheck_degrade_reason,
            "emergency_stop_active": emergency_stop_active,
            "trading_halt_reason": trading_halt_reason,
            "balance_available": balance is not None,
            "balance_error": balance_error,
            "cycle_state": (cycle.discussion_state if cycle else None),
            "execution_pool_case_ids": execution_case_ids,
            "approved_count": approved_count,
            "blocked_count": blocked_count,
            "equity_invested_value": round(invested_value, 2),
            "gross_invested_value": round(gross_invested_value, 2),
            "reverse_repo_value": round(reverse_repo_value, 2),
            "current_total_ratio": round(current_total_ratio, 4),
            "gross_total_ratio": round(gross_total_ratio, 4),
            "equity_position_limit": round(effective_total_position_limit, 4),
            "risk_total_position_limit": round(max_total_position, 4),
            "reverse_repo_target_ratio": round(reverse_repo_target_ratio, 4),
            "reverse_repo_target_value": round(reverse_repo_target_value, 2),
            "reverse_repo_gap_value": round(reverse_repo_gap_value, 2),
            "minimum_total_invested_amount": round(minimum_total_invested_amount, 2),
            "reverse_repo_reserved_amount": round(reverse_repo_reserved_amount, 2),
            "stock_test_budget_amount": round(stock_test_budget_amount, 2),
            "stock_test_budget_remaining": round(trading_budget.stock_test_budget_remaining, 2),
            "recommended_next_actions": payload_next_actions,
            "primary_recommended_next_action": primary_payload_next_action,
            "primary_recommended_next_action_label": primary_payload_next_action_label,
            "items": items,
            "summary_lines": summary_lines,
        }
        if settings.run_mode == "live":
            if not session_open and execution_case_ids:
                payload["summary_lines"].append("当前为非交易时段，已保留执行池，待开盘后可重新触发预检或委托。")
            if live_market_quote_error:
                payload["summary_lines"].append(f"实时行情抓取失败: {live_market_quote_error}")
            payload["summary_lines"].append(
                f"实盘检查 session_open={session_open} quote_ttl={market_snapshot_ttl_seconds}s deviation_limit={max_price_deviation_pct:.2%}。"
            )
        if emergency_stop_active:
            reason_text = trading_halt_reason or "manual_kill_switch"
            payload["summary_lines"].append(f"交易已暂停: {reason_text}。")
        if balance:
            payload["balance"] = balance.model_dump()
        if account_state:
            payload["account_state"] = account_state
        return payload

    def _dispatch_trade_event(
        title: str,
        *,
        symbol: str,
        name: str = "",
        account_id: str,
        side: str | None = None,
        quantity: int | None = None,
        price: float | None = None,
        order_id: str | None = None,
        status: str | None = None,
        decision_id: str | None = None,
        reason: str | None = None,
        level: str = "info",
    ) -> None:
        if not message_dispatcher:
            return
        content = execution_order_event_template(
            action=title,
            symbol=symbol,
            name=name,
            account_id=account_id,
            side=side,
            quantity=quantity,
            price=price,
            order_id=order_id,
            status=status,
            decision_id=decision_id,
            reason=reason,
        )
        message_dispatcher.dispatch_trade(title, content, level=level, force=True)

    def _dispatch_execution_dispatch_summary(payload: dict) -> dict:
        if not message_dispatcher:
            return {"dispatched": False, "reason": "dispatcher_unavailable"}

        submitted_count = int(payload.get("submitted_count", 0) or 0)
        blocked_count = int(payload.get("blocked_count", 0) or 0)
        status = str(payload.get("status") or "")
        degraded = bool(payload.get("degraded"))
        if submitted_count > 0 and blocked_count == 0 and not degraded:
            return {"dispatched": False, "reason": "submitted_detail_events"}

        title = {
            "preview": "执行预演回执",
            "blocked": "执行阻断回执",
        }.get(status, "执行派发回执")
        level = "warning" if blocked_count > 0 or degraded or status == "blocked" else "info"
        lines = execution_dispatch_summary_lines(payload)
        if not lines:
            return {"dispatched": False, "reason": "empty_summary", "title": title, "level": level}
        content = execution_dispatch_notification_template(title, lines)
        dispatched = message_dispatcher.dispatch_trade(title, content, level=level, force=True)
        return {
            "dispatched": dispatched,
            "reason": "sent" if dispatched else "dispatch_failed",
            "title": title,
            "level": level,
        }

    def _persist_execution_precheck(payload: dict) -> None:
        if not meeting_state_store:
            return
        payload = _sanitize_json_compatible(payload)
        history = meeting_state_store.get("execution_precheck_history", [])
        history.append(
            {
                "trade_date": payload.get("trade_date"),
                "account_id": payload.get("account_id"),
                "status": payload.get("status"),
                "approved_count": payload.get("approved_count", 0),
                "blocked_count": payload.get("blocked_count", 0),
                "generated_at": datetime.now().isoformat(),
            }
        )
        meeting_state_store.set("latest_execution_precheck", payload)
        meeting_state_store.set("execution_precheck_history", history[-30:])

    def _build_execution_intents(trade_date: str, account_id: str | None = None) -> dict:
        precheck = _build_execution_precheck(trade_date, account_id=account_id)
        intents: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for item in precheck.get("items", []):
            if not item.get("approved"):
                blocked.append(
                    {
                        "case_id": item.get("case_id"),
                        "symbol": item.get("symbol"),
                        "name": item.get("name"),
                        "blockers": item.get("blockers", []),
                        "guard_reason": item.get("guard_reason"),
                    }
                )
                continue
            request_id = f"intent-{trade_date}-{item['symbol']}"
            intents.append(
                {
                    "intent_id": request_id,
                    "trade_date": trade_date,
                    "case_id": item["case_id"],
                    "decision_id": item["case_id"],
                    "account_id": precheck["account_id"],
                    "symbol": item["symbol"],
                    "name": item.get("name"),
                    "side": "BUY",
                    "quantity": item["proposed_quantity"],
                    "price": item["latest_price"],
                    "estimated_value": item["proposed_value"],
                    "request": {
                        "account_id": precheck["account_id"],
                        "symbol": item["symbol"],
                        "side": "BUY",
                        "quantity": item["proposed_quantity"],
                        "price": item["latest_price"],
                        "request_id": request_id,
                        "decision_id": item["case_id"],
                        "trade_date": trade_date,
                        "playbook": item.get("playbook"),
                        "regime": item.get("regime"),
                    },
                    "precheck": {
                        "budget_value": item.get("budget_value"),
                        "cash_available": item.get("cash_available"),
                        "remaining_total_value": item.get("remaining_total_value"),
                        "remaining_single_value": item.get("remaining_single_value"),
                        "max_single_amount": item.get("max_single_amount"),
                        "guard_reason": item.get("guard_reason"),
                    },
                    "headline_reason": item.get("headline_reason"),
                    "playbook": item.get("playbook"),
                    "regime": item.get("regime"),
                    "resolved_sector": item.get("resolved_sector"),
                }
            )
        payload = {
            "trade_date": trade_date,
            "account_id": precheck["account_id"],
            "status": "ready" if intents else "blocked",
            "intent_count": len(intents),
            "blocked_count": len(blocked),
            "intents": intents,
            "blocked": blocked,
            "summary_lines": [
                f"执行意图已生成 {len(intents)} 条，阻断 {len(blocked)} 条。"
            ],
            "execution_precheck": precheck,
        }
        return payload

    def _get_pending_execution_intents() -> list[dict[str, Any]]:
        if not meeting_state_store:
            return []
        items = meeting_state_store.get("pending_execution_intents", [])
        if not isinstance(items, list):
            return []
        return [item for item in (_sanitize_json_compatible(it) for it in items) if isinstance(item, dict)]

    def _save_pending_execution_intents(items: list[dict[str, Any]]) -> None:
        if not meeting_state_store:
            return
        sanitized = [item for item in (_sanitize_json_compatible(it) for it in items) if isinstance(item, dict)]
        meeting_state_store.set("pending_execution_intents", sanitized)

    def _append_execution_intent_history(item: dict[str, Any]) -> None:
        if not meeting_state_store:
            return
        history = meeting_state_store.get("execution_intent_history", [])
        if not isinstance(history, list):
            history = []
        history.append(_sanitize_json_compatible(item))
        meeting_state_store.set("execution_intent_history", history[-200:])

    def _get_latest_execution_gateway_receipt() -> dict[str, Any]:
        if not meeting_state_store:
            return {}
        payload = meeting_state_store.get("latest_execution_gateway_receipt", {})
        return payload if isinstance(payload, dict) else {}

    def _append_execution_gateway_receipt(receipt: dict[str, Any]) -> None:
        if not meeting_state_store:
            return
        sanitized = _sanitize_json_compatible(receipt)
        history = meeting_state_store.get("execution_gateway_receipt_history", [])
        if not isinstance(history, list):
            history = []
        history.append(sanitized)
        meeting_state_store.set("latest_execution_gateway_receipt", sanitized)
        meeting_state_store.set("execution_gateway_receipt_history", history[-500:])

    def _find_execution_intent(intent_id: str) -> dict[str, Any]:
        if not intent_id:
            return {}
        for item in _get_pending_execution_intents():
            if str(item.get("intent_id") or "") == intent_id:
                return dict(item)
        if not meeting_state_store:
            return {}
        history = meeting_state_store.get("execution_intent_history", [])
        if not isinstance(history, list):
            return {}
        for item in reversed(history):
            if isinstance(item, dict) and str(item.get("intent_id") or "") == intent_id:
                return _sanitize_json_compatible(item)
        return {}

    def _transition_execution_intent_status(
        intent: dict[str, Any],
        next_status: str,
        *,
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current_status = str(intent.get("status") or "approved")
        allowed: dict[str, set[str]] = {
            "approved": {"claimed"},
            "claimed": {"submitted", "failed", "expired"},
            "submitted": {"partial_filled", "filled", "canceled", "rejected", "failed"},
            "partial_filled": {"filled", "canceled", "failed"},
            "filled": set(),
            "canceled": set(),
            "rejected": set(),
            "failed": set(),
            "expired": set(),
        }
        if next_status == current_status:
            return _sanitize_json_compatible(intent)
        if next_status not in allowed.get(current_status, set()):
            raise ValueError(f"invalid execution intent transition: {current_status} -> {next_status}")
        updated = dict(intent)
        updated["status"] = next_status
        if receipt:
            updated["latest_receipt"] = _sanitize_json_compatible(receipt)
        summary_lines = list(updated.get("summary_lines") or [])
        if receipt and receipt.get("summary_lines"):
            summary_lines = list(receipt.get("summary_lines") or [])
        elif next_status == "claimed":
            claim = dict(updated.get("claim") or {})
            summary_lines = [f"执行意图已被 {claim.get('gateway_source_id', '') or 'gateway'} 认领。"]
        updated["summary_lines"] = summary_lines
        return _sanitize_json_compatible(updated)

    def _claim_execution_intent(
        intent_id: str,
        gateway_source_id: str,
        deployment_role: str,
        bridge_path: str,
        claimed_at: str,
    ) -> tuple[bool, dict[str, Any], str]:
        pending = _get_pending_execution_intents()
        for index, item in enumerate(pending):
            if str(item.get("intent_id") or "") != intent_id:
                continue
            current_claim = dict(item.get("claim") or {})
            if str(item.get("status") or "approved") == "claimed":
                if str(current_claim.get("gateway_source_id") or "") == gateway_source_id:
                    return True, _sanitize_json_compatible(item), ""
                return False, _sanitize_json_compatible(item), "intent already claimed by another gateway"
            if str(item.get("status") or "approved") != "approved":
                return False, _sanitize_json_compatible(item), f"intent status {item.get('status') or 'unknown'} cannot be claimed"
            updated = dict(item)
            updated["claim"] = {
                "gateway_source_id": gateway_source_id,
                "deployment_role": deployment_role,
                "bridge_path": bridge_path,
                "claimed_at": claimed_at,
            }
            updated = _transition_execution_intent_status(updated, "claimed")
            pending[index] = updated
            _save_pending_execution_intents(pending)
            _append_execution_intent_history(updated)
            return True, updated, ""
        return False, {}, "intent not found"

    def _store_execution_gateway_receipt(payload: ExecutionGatewayReceiptInput) -> dict[str, Any]:
        receipt = _sanitize_json_compatible(payload.model_dump())
        latest = _get_latest_execution_gateway_receipt()
        if str(latest.get("receipt_id") or "") == payload.receipt_id:
            return latest
        history = meeting_state_store.get("execution_gateway_receipt_history", []) if meeting_state_store else []
        if isinstance(history, list):
            for item in reversed(history):
                if isinstance(item, dict) and str(item.get("receipt_id") or "") == payload.receipt_id:
                    return _sanitize_json_compatible(item)
        pending = _get_pending_execution_intents()
        for index, item in enumerate(pending):
            if str(item.get("intent_id") or "") != payload.intent_id:
                continue
            pending[index] = _transition_execution_intent_status(item, payload.status, receipt=receipt)
            _save_pending_execution_intents(pending)
            _append_execution_intent_history(pending[index])
            break
        _append_execution_gateway_receipt(receipt)
        return receipt

    def _persist_execution_intents(payload: dict) -> None:
        if not meeting_state_store:
            return
        payload = _sanitize_json_compatible(payload)
        history = meeting_state_store.get("execution_intent_history", [])
        history.append(
            {
                "trade_date": payload.get("trade_date"),
                "account_id": payload.get("account_id"),
                "status": payload.get("status"),
                "intent_count": payload.get("intent_count", 0),
                "blocked_count": payload.get("blocked_count", 0),
                "generated_at": datetime.now().isoformat(),
            }
        )
        meeting_state_store.set("latest_execution_intents", payload)
        meeting_state_store.set("execution_intent_history", history[-30:])

    def _get_execution_gateway_state_store() -> StateStore | None:
        return resolve_execution_gateway_state_store(meeting_state_store, runtime_state_store)

    def _get_pending_execution_intents() -> list[dict[str, Any]]:
        return get_gateway_pending_execution_intents(_get_execution_gateway_state_store())

    def _save_pending_execution_intents(items: list[dict[str, Any]]) -> None:
        save_gateway_pending_execution_intents(_get_execution_gateway_state_store(), items)

    def _get_execution_intent_history() -> list[dict[str, Any]]:
        return get_gateway_execution_intent_history(_get_execution_gateway_state_store())

    def _append_execution_intent_history(item: dict[str, Any]) -> None:
        append_gateway_execution_intent_history(_get_execution_gateway_state_store(), item)

    def _get_latest_execution_gateway_receipt() -> dict[str, Any]:
        state_store = _get_execution_gateway_state_store()
        if not state_store:
            return {}
        latest = state_store.get("latest_execution_gateway_receipt", {})
        return _sanitize_json_compatible(latest) if isinstance(latest, dict) else {}

    def _get_execution_gateway_receipt_history() -> list[dict[str, Any]]:
        state_store = _get_execution_gateway_state_store()
        if not state_store:
            return []
        history = state_store.get("execution_gateway_receipt_history", [])
        if not isinstance(history, list):
            return []
        return [_sanitize_json_compatible(item) for item in history if isinstance(item, dict)]

    def _append_execution_gateway_receipt(item: dict[str, Any]) -> None:
        state_store = _get_execution_gateway_state_store()
        if not state_store:
            return
        receipt = _sanitize_json_compatible(item)
        history = _get_execution_gateway_receipt_history()
        history.append(receipt)
        state_store.set("latest_execution_gateway_receipt", receipt)
        state_store.set("execution_gateway_receipt_history", history[-500:])

    def _find_execution_intent(intent_id: str) -> tuple[dict[str, Any], int | None]:
        pending = _get_pending_execution_intents()
        for index, item in enumerate(pending):
            if str(item.get("intent_id") or "") == intent_id:
                return item, index
        history = _get_execution_intent_history()
        for item in reversed(history):
            if str(item.get("intent_id") or "") == intent_id:
                return item, None
        return {}, None

    def _claim_execution_intent(payload: ExecutionGatewayClaimInput) -> tuple[bool, str, dict[str, Any], str]:
        pending = _get_pending_execution_intents()
        for index, item in enumerate(pending):
            if str(item.get("intent_id") or "") != payload.intent_id:
                continue
            current_status = str(item.get("status") or "approved")
            claim = dict(item.get("claim") or {})
            claimed_by = str(claim.get("gateway_source_id") or "")
            if current_status == "claimed":
                if claimed_by == payload.gateway_source_id:
                    return True, "claimed", item, ""
                return False, "conflict", item, "intent already claimed by another gateway"
            if current_status != "approved":
                return False, "invalid_status", item, f"intent status {current_status} cannot be claimed"
            updated = dict(item)
            updated["status"] = "claimed"
            updated["claim"] = {
                "gateway_source_id": payload.gateway_source_id,
                "deployment_role": payload.deployment_role,
                "bridge_path": payload.bridge_path,
                "claimed_at": payload.claimed_at,
            }
            pending[index] = updated
            _save_pending_execution_intents(pending)
            _append_execution_intent_history(updated)
            return True, "claimed", updated, ""
        return False, "not_found", {}, "intent not found"

    def _store_execution_gateway_receipt(payload: ExecutionGatewayReceiptInput) -> tuple[bool, bool, dict[str, Any], str]:
        for item in _get_execution_gateway_receipt_history():
            if str(item.get("receipt_id") or "") == payload.receipt_id:
                return True, False, item, ""

        pending = _get_pending_execution_intents()
        matched_index: int | None = None
        matched_intent: dict[str, Any] = {}
        for index, item in enumerate(pending):
            if str(item.get("intent_id") or "") == payload.intent_id:
                matched_index = index
                matched_intent = dict(item)
                break
        if not matched_intent:
            matched_intent, _ = _find_execution_intent(payload.intent_id)
        if not matched_intent:
            return False, False, {}, "intent not found"

        current_status = str(matched_intent.get("status") or "approved")
        next_status = payload.status
        allowed_next = {
            "claimed": {"submitted", "failed"},
            "submitted": {"partial_filled", "filled", "canceled", "rejected", "failed"},
            "partial_filled": {"filled", "canceled", "failed"},
            "approved": set(),
            "filled": set(),
            "canceled": set(),
            "rejected": set(),
            "failed": set(),
            "expired": set(),
        }
        if next_status != current_status and next_status not in allowed_next.get(current_status, set()):
            return False, False, {}, f"intent status {current_status} cannot transition to {next_status}"

        updated_intent = dict(matched_intent)
        updated_intent["status"] = next_status
        updated_intent["latest_receipt_id"] = payload.receipt_id
        updated_intent["latest_gateway_source_id"] = payload.gateway_source_id
        updated_intent["latest_reported_at"] = payload.reported_at
        if matched_index is not None:
            terminal_statuses = {"filled", "canceled", "rejected", "failed", "expired"}
            if next_status in terminal_statuses:
                pending.pop(matched_index)
            else:
                pending[matched_index] = updated_intent
            _save_pending_execution_intents(pending)
        _append_execution_intent_history(updated_intent)

        receipt = {
            "receipt_scope": "execution_gateway_receipt",
            "receipt_id": payload.receipt_id,
            "intent_id": payload.intent_id,
            "intent_version": payload.intent_version,
            "gateway_source_id": payload.gateway_source_id,
            "deployment_role": payload.deployment_role,
            "bridge_path": payload.bridge_path,
            "reported_at": payload.reported_at,
            "submitted_at": payload.submitted_at,
            "status": payload.status,
            "broker_order_id": payload.broker_order_id,
            "broker_session_id": payload.broker_session_id,
            "exchange_order_id": payload.exchange_order_id,
            "error_code": payload.error_code,
            "error_message": payload.error_message,
            "order": payload.order,
            "fills": payload.fills,
            "latency_ms": payload.latency_ms,
            "raw_payload": payload.raw_payload,
            "summary_lines": list(payload.summary_lines or []),
        }
        _append_execution_gateway_receipt(receipt)
        return True, True, receipt, ""

    def _build_execution_gateway_intent_packet(intent: dict[str, Any]) -> dict[str, Any]:
        return _sanitize_json_compatible(
            build_execution_gateway_intent_packet(
                intent,
                run_mode=str(settings.run_mode),
                approval_source="discussion_execution_dispatch",
                summary_lines=["讨论执行意图已批准，等待 Windows Execution Gateway 拉取。"],
            )
        )

    def _enqueue_execution_gateway_intent(intent: dict[str, Any]) -> dict[str, Any]:
        return _sanitize_json_compatible(
            enqueue_execution_gateway_intent(
                _get_execution_gateway_state_store(),
                intent,
                run_mode=str(settings.run_mode),
                approval_source="discussion_execution_dispatch",
                summary_lines=["讨论执行意图已批准，等待 Windows Execution Gateway 拉取。"],
            )
        )

    def _build_execution_dispatch_receipts(
        trade_date: str,
        account_id: str | None = None,
        intent_ids: list[str] | None = None,
        apply: bool = False,
    ) -> dict:
        intents_payload = _build_execution_intents(trade_date, account_id=account_id)
        selected_intents = intents_payload.get("intents", [])
        if intent_ids:
            allowed_ids = set(intent_ids)
            selected_intents = [item for item in selected_intents if item["intent_id"] in allowed_ids]

        receipts: list[dict[str, Any]] = []
        submitted_count = 0
        queued_count = 0
        blocked_count = 0
        preview_count = 0
        execution_plane = str(getattr(settings, "execution_plane", "local_xtquant") or "local_xtquant")
        gateway_pull_path = EXECUTION_GATEWAY_PENDING_PATH
        execution_mode = getattr(execution_adapter, "mode", settings.execution_mode) if execution_adapter else settings.execution_mode
        runtime_config = config_mgr.get() if config_mgr else None
        emergency_stop_active = bool(getattr(runtime_config, "emergency_stop", False))
        trading_halt_reason = str(getattr(runtime_config, "trading_halt_reason", "") or "").strip()
        submit_block_reason = None
        if apply:
            if emergency_stop_active:
                submit_block_reason = "emergency_stop_active"
            elif execution_plane == "windows_gateway":
                if not _get_execution_gateway_state_store():
                    submit_block_reason = "execution_gateway_state_unavailable"
                elif settings.run_mode == "live" and not settings.live_trade_enabled:
                    submit_block_reason = "live_not_enabled"
            else:
                if not execution_adapter:
                    submit_block_reason = "execution_adapter_unavailable"
                elif execution_mode.startswith("mock"):
                    if settings.run_mode == "live":
                        submit_block_reason = "live_mock_fallback_blocked"
                elif execution_mode == "xtquant":
                    if settings.run_mode != "live":
                        submit_block_reason = "live_confirmation_required"
                    elif not settings.live_trade_enabled:
                        submit_block_reason = "live_not_enabled"
                else:
                    submit_block_reason = "execution_adapter_unavailable"

        for intent in selected_intents:
            receipt = {
                "intent_id": intent["intent_id"],
                "case_id": intent["case_id"],
                "decision_id": intent["decision_id"],
                "symbol": intent["symbol"],
                "name": intent.get("name"),
                "status": "preview",
                "apply": apply,
                "reason": "preview_only",
                "processed_at": datetime.now().isoformat(),
                "request": intent["request"],
                "estimated_value": intent.get("estimated_value"),
                "execution_plane": execution_plane,
            }
            if not apply:
                preview_count += 1
                receipts.append(receipt)
                continue
            if submit_block_reason:
                receipt["status"] = "paper_blocked"
                receipt["reason"] = submit_block_reason
                blocked_count += 1
                receipts.append(receipt)
                continue
            try:
                if execution_plane == "windows_gateway":
                    queued_packet = _enqueue_execution_gateway_intent(intent)
                    receipt["status"] = "queued_for_gateway"
                    receipt["reason"] = "forward_to_windows_execution_gateway"
                    receipt["queued_at"] = queued_packet["approved_at"]
                    receipt["gateway_pull_path"] = gateway_pull_path
                    receipt["gateway_intent"] = queued_packet
                    queued_count += 1
                else:
                    order = execution_adapter.place_order(PlaceOrderRequest(**intent["request"]))
                    receipt["status"] = "submitted"
                    receipt["reason"] = "sent"
                    receipt["submitted_at"] = datetime.now().isoformat()
                    receipt["order"] = order.model_dump()
                    _dispatch_trade_event(
                        "自动下单",
                        symbol=intent["symbol"],
                        name=intent.get("name") or "",
                        account_id=intent["request"]["account_id"],
                        side=intent["request"].get("side"),
                        quantity=intent["request"].get("quantity"),
                        price=intent["request"].get("price"),
                        order_id=order.order_id,
                        status=order.status,
                        decision_id=intent.get("decision_id"),
                        reason=intent.get("headline_reason"),
                    )
                    submitted_count += 1
            except Exception as exc:
                receipt["status"] = "dispatch_failed"
                receipt["reason"] = str(exc)
                blocked_count += 1
            receipts.append(receipt)

        blocked_from_intents = [
            {
                "intent_id": None,
                "case_id": item.get("case_id"),
                "decision_id": item.get("case_id"),
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "status": "blocked",
                "apply": apply,
                "reason": ",".join(item.get("blockers", [])) or "blocked",
                "request": None,
                "estimated_value": 0.0,
            }
            for item in intents_payload.get("blocked", [])
        ]

        dispatch_degrade_reason = None
        if apply:
            if submit_block_reason:
                dispatch_degrade_reason = submit_block_reason
            else:
                failed_receipt = next((item for item in receipts if item.get("status") == "dispatch_failed"), None)
                if failed_receipt:
                    dispatch_degrade_reason = "dispatch_failed"
                elif blocked_from_intents:
                    dispatch_degrade_reason = blocked_from_intents[0].get("reason") or "blocked"
        degraded = apply and dispatch_degrade_reason is not None

        payload = {
            "trade_date": trade_date,
            "account_id": intents_payload.get("account_id"),
            "apply": apply,
            "run_mode": settings.run_mode,
            "execution_plane": execution_plane,
            "execution_mode": execution_mode,
            "degraded": degraded,
            "degrade_to": ("blocked" if degraded else None),
            "degrade_reason": dispatch_degrade_reason,
            "emergency_stop_active": emergency_stop_active,
            "trading_halt_reason": trading_halt_reason,
            "status": (
                "submitted"
                if submitted_count > 0
                else (
                    "queued_for_gateway"
                    if queued_count > 0
                    else ("preview" if preview_count > 0 and blocked_count == 0 else "blocked")
                )
            ),
            "submitted_count": submitted_count,
            "queued_count": queued_count,
            "preview_count": preview_count,
            "blocked_count": blocked_count + len(blocked_from_intents),
            "receipts": receipts + blocked_from_intents,
            "execution_intents": intents_payload,
            "summary_lines": [
                (
                    "执行派发结果: "
                    f"queued={queued_count} submitted={submitted_count} preview={preview_count} "
                    f"blocked={blocked_count + len(blocked_from_intents)}."
                )
            ],
        }
        if execution_plane == "windows_gateway":
            payload["gateway_pull_path"] = gateway_pull_path
            if queued_count > 0:
                payload["summary_lines"].append("当前为 windows_gateway 执行平面，Linux 控制面仅入队 intent，等待 Gateway 拉取。")
        if submit_block_reason:
            payload["summary_lines"].append(
                f"当前为 {settings.run_mode}/{execution_plane}/{execution_mode}，原因 {submit_block_reason}。"
            )
        if emergency_stop_active:
            payload["summary_lines"].append(f"交易暂停说明: {trading_halt_reason or 'manual_kill_switch'}。")
        return payload

    def _persist_execution_dispatch(payload: dict) -> None:
        if not meeting_state_store:
            return
        trade_date = payload.get("trade_date")
        history = meeting_state_store.get("execution_dispatch_history", [])
        history.append(
            {
                "trade_date": trade_date,
                "account_id": payload.get("account_id"),
                "status": payload.get("status"),
                "submitted_count": payload.get("submitted_count", 0),
                "preview_count": payload.get("preview_count", 0),
                "blocked_count": payload.get("blocked_count", 0),
                "generated_at": datetime.now().isoformat(),
            }
        )
        order_journal = meeting_state_store.get("execution_order_journal", [])
        for receipt in payload.get("receipts", []):
            if receipt.get("status") != "submitted":
                continue
            order = receipt.get("order") or {}
            request = receipt.get("request") or {}
            order_id = order.get("order_id")
            if not order_id:
                continue
            order_journal.append(
                {
                    "trade_date": trade_date,
                    "account_id": payload.get("account_id"),
                    "order_id": order_id,
                    "symbol": receipt.get("symbol"),
                    "name": receipt.get("name"),
                    "decision_id": receipt.get("decision_id"),
                    "side": request.get("side"),
                    "submitted_at": receipt.get("submitted_at") or receipt.get("processed_at"),
                    "playbook": request.get("playbook"),
                    "regime": request.get("regime"),
                    "request": request,
                }
            )
        meeting_state_store.set("latest_execution_dispatch", payload)
        if trade_date:
            meeting_state_store.set(f"execution_dispatch:{trade_date}", payload)
        meeting_state_store.set("execution_dispatch_history", history[-30:])
        if order_journal:
            meeting_state_store.set("execution_order_journal", order_journal[-200:])

    def _get_execution_dispatch_payload(trade_date: str | None = None) -> dict | None:
        if not meeting_state_store:
            return None
        if trade_date:
            payload = meeting_state_store.get(f"execution_dispatch:{trade_date}")
            if payload:
                return payload
        payload = meeting_state_store.get("latest_execution_dispatch")
        if trade_date and payload and payload.get("trade_date") != trade_date:
            return None
        return payload

    def _extract_gateway_source_from_intents(items: list[dict[str, Any]]) -> str:
        for item in reversed(items):
            latest_source = str(item.get("latest_gateway_source_id") or "")
            if latest_source:
                return latest_source
            claim = dict(item.get("claim") or {})
            claim_source = str(claim.get("gateway_source_id") or "")
            if claim_source:
                return claim_source
        return ""

    def _build_latest_receipt_summary() -> dict[str, Any]:
        receipt = _get_latest_execution_gateway_receipt()
        if not receipt:
            return {
                "available": False,
                "receipt_id": "",
                "intent_id": "",
                "status": "not_found",
                "gateway_source_id": "",
                "deployment_role": "",
                "bridge_path": "",
                "reported_at": "",
                "submitted_at": "",
                "symbol": "",
                "side": "",
                "quantity": 0,
                "broker_order_id": "",
                "fill_count": 0,
                "error_code": "",
                "error_message": "",
                "summary_lines": ["当前尚无 execution gateway latest receipt。"],
            }
        order = dict(receipt.get("order") or {})
        fills = list(receipt.get("fills") or [])
        summary_lines = [str(line) for line in list(receipt.get("summary_lines") or []) if str(line)]
        if not summary_lines:
            summary_lines = [
                (
                    f"latest receipt {receipt.get('receipt_id') or '-'} "
                    f"status={receipt.get('status') or 'unknown'} "
                    f"source={receipt.get('gateway_source_id') or 'unknown'}。"
                )
            ]
        return {
            "available": True,
            "receipt_id": str(receipt.get("receipt_id") or ""),
            "intent_id": str(receipt.get("intent_id") or ""),
            "status": str(receipt.get("status") or "unknown"),
            "gateway_source_id": str(receipt.get("gateway_source_id") or ""),
            "deployment_role": str(receipt.get("deployment_role") or ""),
            "bridge_path": str(receipt.get("bridge_path") or ""),
            "reported_at": str(receipt.get("reported_at") or ""),
            "submitted_at": str(receipt.get("submitted_at") or ""),
            "symbol": str(order.get("symbol") or ""),
            "side": str(order.get("side") or ""),
            "quantity": int(order.get("quantity") or 0),
            "broker_order_id": str(receipt.get("broker_order_id") or ""),
            "fill_count": len(fills),
            "error_code": str(receipt.get("error_code") or ""),
            "error_message": str(receipt.get("error_message") or ""),
            "summary_lines": summary_lines,
        }

    def _build_gateway_queue_source_summary(
        payload: dict[str, Any] | None,
        *,
        scope: str,
        items_key: str,
    ) -> dict[str, Any]:
        normalized = dict(payload or {})
        queued_items = [
            dict(item)
            for item in list(normalized.get(items_key) or [])
            if isinstance(item, dict) and str(item.get("status") or "") == "queued_for_gateway"
        ]
        latest_item = queued_items[-1] if queued_items else {}
        latest_gateway_intent = dict(latest_item.get("gateway_intent") or {})
        return {
            "scope": scope,
            "available": bool(normalized),
            "trade_date": str(normalized.get("trade_date") or ""),
            "status": str(normalized.get("status") or ("not_found" if not normalized else "unknown")),
            "queued_for_gateway_count": int(normalized.get("queued_count", 0) or 0),
            "latest_intent_id": str(
                latest_gateway_intent.get("intent_id")
                or latest_item.get("intent_id")
                or ""
            ),
            "latest_approval_source": str(latest_gateway_intent.get("approval_source") or ""),
            "summary_lines": [str(line) for line in list(normalized.get("summary_lines") or []) if str(line)],
        }

    def _build_control_plane_gateway_summary(
        *,
        trade_date: str | None = None,
        latest_dispatch_payload: dict[str, Any] | None = None,
        latest_tail_market_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pending_items = _get_pending_execution_intents()
        pending_intent_count = len(pending_items)
        queued_for_gateway_count = sum(
            1 for item in pending_items if str(item.get("status") or "approved") == "approved"
        )
        latest_receipt_summary = _build_latest_receipt_summary()
        dispatch_payload = latest_dispatch_payload if latest_dispatch_payload is not None else _get_execution_dispatch_payload(trade_date)
        tail_market_payload = (
            latest_tail_market_payload
            if latest_tail_market_payload is not None
            else (meeting_state_store.get("latest_tail_market_scan") if meeting_state_store else None)
        )
        discussion_dispatch = _build_gateway_queue_source_summary(
            dispatch_payload,
            scope="discussion_dispatch",
            items_key="receipts",
        )
        tail_market = _build_gateway_queue_source_summary(
            tail_market_payload,
            scope="tail_market",
            items_key="items",
        )
        latest_gateway_source = str(latest_receipt_summary.get("gateway_source_id") or "")
        if not latest_gateway_source and monitor_state_service:
            latest_health = dict((monitor_state_service.get_latest_execution_bridge_health() or {}).get("health") or {})
            latest_gateway_source = str(latest_health.get("source_id") or "")
        if not latest_gateway_source:
            latest_gateway_source = _extract_gateway_source_from_intents(pending_items)
        summary_lines = [
            (
                f"主控执行队列 pending={pending_intent_count}，"
                f"queued_for_gateway={queued_for_gateway_count}，"
                f"latest_gateway_source={latest_gateway_source or 'unknown'}。"
            ),
            (
                f"discussion dispatch queued={discussion_dispatch.get('queued_for_gateway_count', 0)}；"
                f"tail-market queued={tail_market.get('queued_for_gateway_count', 0)}。"
            ),
        ]
        if latest_receipt_summary.get("available"):
            summary_lines.append(
                (
                    f"latest receipt {latest_receipt_summary.get('receipt_id') or '-'} "
                    f"status={latest_receipt_summary.get('status') or 'unknown'} "
                    f"source={latest_receipt_summary.get('gateway_source_id') or 'unknown'}。"
                )
            )
        return {
            "available": bool(
                pending_intent_count
                or latest_receipt_summary.get("available")
                or discussion_dispatch.get("available")
                or tail_market.get("available")
            ),
            "pending_intent_count": pending_intent_count,
            "queued_for_gateway_count": queued_for_gateway_count,
            "latest_gateway_source": latest_gateway_source,
            "latest_receipt_summary": latest_receipt_summary,
            "discussion_dispatch": discussion_dispatch,
            "tail_market": tail_market,
            "summary_lines": summary_lines,
        }

    def _attach_control_plane_gateway_summary(
        payload: dict[str, Any],
        *,
        trade_date: str | None = None,
        latest_dispatch_payload: dict[str, Any] | None = None,
        latest_tail_market_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_payload = _sanitize_json_compatible(payload)
        gateway_summary = _build_control_plane_gateway_summary(
            trade_date=trade_date,
            latest_dispatch_payload=latest_dispatch_payload,
            latest_tail_market_payload=latest_tail_market_payload,
        )
        normalized_payload["control_plane_gateway_summary"] = gateway_summary
        normalized_payload["pending_intent_count"] = int(gateway_summary.get("pending_intent_count", 0) or 0)
        normalized_payload["queued_for_gateway_count"] = int(gateway_summary.get("queued_for_gateway_count", 0) or 0)
        normalized_payload["latest_gateway_source"] = str(gateway_summary.get("latest_gateway_source") or "")
        normalized_payload["latest_receipt_summary"] = dict(gateway_summary.get("latest_receipt_summary") or {})
        normalized_payload["discussion_dispatch_queued_for_gateway_count"] = int(
            ((gateway_summary.get("discussion_dispatch") or {}).get("queued_for_gateway_count", 0) or 0)
        )
        normalized_payload["tail_market_queued_for_gateway_count"] = int(
            ((gateway_summary.get("tail_market") or {}).get("queued_for_gateway_count", 0) or 0)
        )
        summary_lines = [str(line) for line in list(normalized_payload.get("summary_lines") or []) if str(line)]
        for line in list(gateway_summary.get("summary_lines") or [])[:2]:
            prefixed = "主控执行: " + str(line)
            if prefixed not in summary_lines:
                summary_lines.append(prefixed)
        normalized_payload["summary_lines"] = summary_lines
        return normalized_payload

    def _build_client_brief(
        trade_date: str,
        selection_limit: int = 3,
        watchlist_limit: int = 5,
        rejected_limit: int = 5,
    ) -> dict:
        reply_pack = _enrich_discussion_payload(
            trade_date,
            candidate_case_service.build_reply_pack(
                trade_date,
                selected_limit=selection_limit,
                watchlist_limit=watchlist_limit,
                rejected_limit=rejected_limit,
            ),
        )
        final_brief = _enrich_discussion_payload(
            trade_date,
            candidate_case_service.build_final_brief(
                trade_date,
                selection_limit=selection_limit,
            ),
        )
        cycle = discussion_cycle_service.get_cycle(trade_date) if discussion_cycle_service else None
        execution_precheck = _build_execution_precheck(trade_date)
        execution_dispatch = _get_execution_dispatch_payload(trade_date)
        payload = build_client_brief_payload(
            trade_date=trade_date,
            reply_pack=reply_pack,
            final_brief=final_brief,
            execution_precheck=execution_precheck,
            execution_dispatch=execution_dispatch,
            cycle=(cycle.model_dump() if cycle else None),
        )
        return _enrich_discussion_payload(trade_date, payload)

    @router.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": settings.app_name,
            "version": "0.2.0",
            "mode": settings.run_mode,
            "environment": settings.environment,
        }

    @router.get("/home")
    async def home():
        workspace_context = serving_store.get_latest_workspace_context() or {}
        runtime = _latest_runtime()
        research = _research_summary()
        return {
            "service": settings.app_name,
            "version": "0.2.0",
            "mode": settings.run_mode,
            "environment": settings.environment,
            "timestamp": datetime.now().isoformat(),
            "modules": {
                "execution": settings.execution_mode,
                "market": settings.market_mode,
                "alerts": settings.notify.alerts_enabled,
            },
            "runtime": {
                "latest_job_id": runtime.get("job_id"),
                "latest_generated_at": runtime.get("generated_at"),
                "decision_count": len(runtime.get("top_picks", [])),
            },
            "research": {
                "news_count": research.get("news_count", 0),
                "announcement_count": research.get("announcement_count", 0),
            },
            "workspace_context": workspace_context,
        }

    @router.get("/overview")
    async def overview():
        workspace_context = serving_store.get_latest_workspace_context()
        runtime = _latest_runtime()
        research = _research_summary()
        return {
            "service": settings.app_name,
            "mode": settings.run_mode,
            "timestamp": datetime.now().isoformat(),
            "status": "operational",
            "runtime_status": "ready" if runtime else "idle",
            "research_status": "ready" if research.get("news_count", 0) or research.get("announcement_count", 0) else "idle",
            "latest_reports": _recent_reports(),
            "workspace_context": workspace_context,
        }

    @router.get("/workspace-context")
    async def get_workspace_context():
        payload = _sanitize_json_compatible(serving_store.get_latest_workspace_context())
        return payload or {"available": False, "resource": "workspace_context"}

    @router.get("/operations/components")
    async def operations_components():
        runtime = _latest_runtime()
        return {
            "components": [
                {"name": "api_stack", "status": "ok", "detail": f"http://{settings.service.host}:{settings.service.port}"},
                {"name": "market_adapter", "status": settings.market_mode},
                {"name": "execution_adapter", "status": settings.execution_mode},
                {"name": "scheduler", "status": "managed_externally"},
                {"name": "runtime_report", "status": "ready" if runtime else "idle"},
            ],
            "timestamp": datetime.now().isoformat(),
        }

    @router.get("/healthcheck")
    async def healthcheck():
        result = EnvironmentHealthcheck(settings).run()
        return {"ok": result.ok, "checks": result.checks}

    @router.get("/readiness")
    async def readiness(account_id: str | None = None):
        payload = _build_readiness(account_id=account_id)
        return {"ok": payload["status"] != "blocked", **payload}

    @router.get("/deployment/bootstrap-contracts")
    async def deployment_bootstrap_contracts(account_id: str | None = None):
        payload = _build_deployment_bootstrap_contracts(account_id=account_id)
        if not isinstance(payload.get("execution_bridge_deployment_contract_sample"), dict):
            payload["execution_bridge_deployment_contract_sample"] = _sanitize_json_compatible(
                payload.get("deployment_contract_sample") or build_execution_bridge_health_deployment_contract_sample()
            )
        return {"ok": payload["readiness"]["status"] != "blocked", **payload}

    @router.get("/deployment/linux-control-plane-startup-checklist")
    async def linux_control_plane_startup_checklist(account_id: str | None = None):
        payload = _build_linux_control_plane_startup_checklist(account_id=account_id)
        return {"ok": payload["status"] != "blocked", **payload}

    @router.get("/deployment/windows-execution-gateway-onboarding-bundle")
    async def windows_execution_gateway_onboarding_bundle(account_id: str | None = None):
        payload = _build_windows_execution_gateway_onboarding_bundle(account_id=account_id)
        return {"ok": True, **payload}

    @router.get("/account-state")
    async def account_state(account_id: str | None = None):
        if not account_state_service:
            return {"ok": False, "error": "account state service not initialized"}
        resolved_account_id = _resolve_account_id(account_id)
        payload = account_state_service.snapshot(resolved_account_id, persist=True)
        return {"ok": payload.get("status") in {"ok", "queued_for_gateway"}, **payload}

    @router.post("/reverse-repo/check")
    async def reverse_repo_check(account_id: str | None = None, auto_submit: bool = False):
        if not reverse_repo_service:
            return {"ok": False, "error": "reverse repo service not initialized"}
        resolved_account_id = _resolve_account_id(account_id)
        payload = reverse_repo_service.inspect(resolved_account_id, auto_submit=auto_submit, persist=True)
        return {"ok": payload.get("status") != "error", **payload}

    @router.get("/reverse-repo/latest")
    async def reverse_repo_latest():
        if not reverse_repo_service:
            return {"ok": False, "error": "reverse repo service not initialized"}
        return {"ok": True, "item": reverse_repo_service.latest()}

    @router.get("/settings")
    async def get_settings():
        return {
            "app_name": settings.app_name,
            "run_mode": settings.run_mode,
            "environment": settings.environment,
            "execution_mode": settings.execution_mode,
            "market_mode": settings.market_mode,
            "workspace": str(settings.workspace),
            "storage_root": str(settings.storage_root),
            "logs_dir": str(settings.logs_dir),
        }

    @router.get("/params")
    async def list_params(as_of: str | None = None):
        if not parameter_service:
            return {"items": [], "count": 0}
        items = parameter_service.list_params(as_of=as_of)
        return {"items": items, "count": len(items), "as_of": as_of or datetime.now().date().isoformat()}

    @router.post("/params/proposals")
    async def create_param_proposal(payload: ParamProposalInput):
        if not parameter_service:
            return {"ok": False, "error": "parameter service not initialized"}
        try:
            event = parameter_service.propose_change(payload)
            if audit_store:
                audit_store.append(
                    category="governance",
                    message=f"参数提案已记录: {event.param_key}",
                    payload=event.model_dump(),
                )
            return {"ok": True, "event": event.model_dump()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.get("/params/proposals")
    async def list_param_proposals(status: str | None = None):
        if not parameter_service:
            return {"items": [], "count": 0}
        items = [event.model_dump() for event in parameter_service.list_proposals(status=status)]
        return {"items": items, "count": len(items), "status": status}

    @router.post("/adjustments/natural-language")
    async def adjust_from_natural_language(payload: NaturalLanguageAdjustmentInput):
        if not parameter_service or not adjustment_interpreter:
            return {"ok": False, "error": "parameter service not initialized"}
        parsed = adjustment_interpreter.interpret(payload.instruction)
        if not parsed.matched:
            return {
                "ok": False,
                "instruction": payload.instruction,
                "matched_count": 0,
                "unmatched": parsed.unmatched,
            }

        preview_items = [item.model_dump() for item in parsed.matched]
        preview_lines = [
            f"{item['param_key']} -> {item['new_value']}"
            + (f" (config: {item['config_key']} -> {item['config_value']})" if item.get("config_key") else "")
            for item in preview_items
        ]
        preview_reply_lines = [
            "本次仅预览，尚未落地。",
            *preview_lines,
        ]
        if not payload.apply:
            notify_result = (
                governance_adjustment_notifier.dispatch(
                    instruction=payload.instruction,
                    applied=False,
                    summary_lines=preview_lines,
                    matched_items=preview_items,
                )
                if payload.notify and governance_adjustment_notifier
                else None
            )
            return {
                "ok": True,
                "applied": False,
                "instruction": payload.instruction,
                "matched_count": len(preview_items),
                "items": preview_items,
                "summary_lines": preview_lines,
                "status": "preview",
                "reply_lines": preview_reply_lines,
                "inferred_effective_period": parsed.inferred_effective_period,
                "notification": (
                    {"dispatched": notify_result.dispatched, "reason": notify_result.reason}
                    if notify_result
                    else {"dispatched": False, "reason": "disabled"}
                ),
            }

        proposal_events = []
        config_updates: dict[str, int | float | str | bool] = {}
        for item in parsed.matched:
            event = parameter_service.propose_change(
                ParamProposalInput(
                    param_key=item.param_key,
                    new_value=item.new_value,
                    effective_period=(payload.effective_period or item.effective_period),
                    proposed_by=payload.proposed_by,
                    structured_by=payload.structured_by,
                    approved_by=payload.approved_by,
                    reason=f"自然语言调整: {payload.instruction}",
                    status=payload.status,
                )
            )
            proposal_events.append(event.model_dump())
            if item.config_key:
                config_updates[item.config_key] = item.config_value

        updated_config = None
        if config_mgr and config_updates:
            updated_config = config_mgr.update(**config_updates).model_dump()
        applied_lines = [
            f"{item['param_key']} 已调整为 {item['new_value']}"
            + (f"，配置 {item['config_key']} 同步为 {item['config_value']}" if item.get("config_key") else "")
            for item in preview_items
        ]
        notify_result = (
            governance_adjustment_notifier.dispatch(
                instruction=payload.instruction,
                applied=True,
                summary_lines=applied_lines,
                matched_items=preview_items,
            )
            if payload.notify and governance_adjustment_notifier
            else None
        )
        applied_reply_lines = [
            "本次调整已生效。",
            *applied_lines,
            (
                f"通知状态: {notify_result.reason if notify_result else 'disabled'}"
                if payload.notify
                else "通知状态: disabled"
            ),
        ]
        if audit_store:
            audit_store.append(
                category="governance",
                message="自然语言参数调整已处理",
                payload={
                    "instruction": payload.instruction,
                    "apply": payload.apply,
                    "notify": payload.notify,
                    "matched_count": len(preview_items),
                    "config_update_count": len(config_updates),
                    "notify_dispatched": (notify_result.dispatched if notify_result else False),
                },
            )
        return {
            "ok": True,
            "applied": True,
            "instruction": payload.instruction,
            "matched_count": len(preview_items),
            "items": preview_items,
            "summary_lines": applied_lines,
            "status": "effective",
            "reply_lines": applied_reply_lines,
            "proposal_events": proposal_events,
            "config_updates": config_updates,
            "config": updated_config,
            "inferred_effective_period": parsed.inferred_effective_period,
            "notification": (
                {"dispatched": notify_result.dispatched, "reason": notify_result.reason}
                if notify_result
                else {"dispatched": False, "reason": "disabled"}
            ),
        }

    @router.post("/precompute/dossiers")
    async def precompute_dossiers(payload: DossierPrecomputeInput):
        if not dossier_precompute_service:
            return {"ok": False, "error": "dossier precompute service not initialized"}
        try:
            pack = dossier_precompute_service.precompute(
                trade_date=payload.trade_date,
                symbols=payload.symbols,
                source=payload.source,
                limit=payload.limit,
                force=payload.force,
            )
            if audit_store:
                audit_store.append(
                    category="precompute",
                    message="候选 dossier 预计算完成",
                    payload={
                        "trade_date": pack["trade_date"],
                        "source": pack["source"],
                        "symbol_count": pack["symbol_count"],
                        "reused": pack["reused"],
                    },
                )
            return {"ok": True, **pack}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.get("/precompute/dossiers/latest")
    async def get_latest_precomputed_dossier():
        return _latest_dossier_status()

    @router.get("/monitoring/cadence")
    async def get_monitoring_cadence(trade_date: str | None = None):
        polling_status = monitor_state_service.get_polling_status() if monitor_state_service else {}
        dossier = _latest_dossier_status()
        resolved_trade_date = trade_date
        if not resolved_trade_date and dossier.get("trade_date"):
            resolved_trade_date = dossier.get("trade_date")
        if not resolved_trade_date and candidate_case_service:
            latest_cases = candidate_case_service.list_cases(limit=1)
            if latest_cases:
                resolved_trade_date = latest_cases[0].trade_date

        cycle_payload = None
        if resolved_trade_date and discussion_cycle_service:
            cycle = discussion_cycle_service.get_cycle(resolved_trade_date)
            if cycle:
                cycle_payload = {
                    "trade_date": resolved_trade_date,
                    "cycle_id": cycle.cycle_id,
                    "discussion_state": cycle.discussion_state,
                    "pool_state": cycle.pool_state,
                    "round_2_target_case_ids": cycle.round_2_target_case_ids,
                    "execution_pool_case_ids": cycle.execution_pool_case_ids,
                    "blockers": cycle.blockers,
                    "updated_at": cycle.updated_at,
                }

        summary_lines: list[str] = []
        if dossier.get("available"):
            freshness_text = "fresh" if dossier.get("is_fresh") else "stale"
            summary_lines.append(
                f"dossier={freshness_text} source={dossier.get('source_layer')} trade_date={dossier.get('trade_date')} expires_in={dossier.get('expires_in_seconds')}"
            )
        else:
            summary_lines.append("dossier=missing")
        for layer in ("candidate", "focus", "execution"):
            item = polling_status.get(layer)
            if not item:
                continue
            state = "due" if item.get("due_now") else "cooldown"
            summary_lines.append(
                f"{layer}_poll={state} interval={item.get('interval_seconds')} last={item.get('last_polled_at')}"
            )
        if cycle_payload:
            summary_lines.append(
                f"cycle={cycle_payload['discussion_state']} pool={cycle_payload['pool_state']}"
            )

        return {
            "trade_date": resolved_trade_date,
            "polling_status": polling_status,
            "dossier": dossier,
            "cycle": cycle_payload,
            "summary_lines": summary_lines,
        }

    @router.post("/monitor/execution-bridge-health")
    async def record_execution_bridge_health(payload: ExecutionBridgeHealthIngressInput):
        if not monitor_state_service:
            return {"ok": False, "error": "monitor state service not initialized"}
        try:
            normalized_payload = build_execution_bridge_health_ingress_payload(
                payload.health,
                trigger=payload.trigger,
            )
            normalized_health = dict(normalized_payload["health"])
            if normalized_health.get("overall_status") == "unknown":
                gateway_snapshot = dict(normalized_health.get("windows_execution_gateway") or {})
                qmt_snapshot = dict(normalized_health.get("qmt_vm") or {})
                gateway_status = str(
                    gateway_snapshot.get("status")
                    or ("healthy" if normalized_health.get("gateway_online") else "down" if normalized_health.get("last_error") else "")
                )
                qmt_status = str(
                    qmt_snapshot.get("status")
                    or ("healthy" if normalized_health.get("qmt_connected") else "down" if normalized_health.get("last_error") else "")
                )
                derived_status = monitor_state_service._derive_execution_bridge_overall_status(gateway_status, qmt_status)
                normalized_health["overall_status"] = "" if derived_status == "unknown" else derived_status
            latest = monitor_state_service.save_execution_bridge_health(
                normalized_health,
                trigger=str(normalized_payload["trigger"]),
            )
            trend_summary = monitor_state_service.get_execution_bridge_health_trend_summary()
            if audit_store:
                health = dict(latest.get("health") or {})
                audit_store.append(
                    category="monitor",
                    message="execution bridge health 已写入",
                    payload={
                        "trigger": normalized_payload["trigger"],
                        "overall_status": health.get("overall_status", "unknown"),
                        "source_id": health.get("source_id", ""),
                        "deployment_role": health.get("deployment_role", ""),
                        "bridge_path": health.get("bridge_path", ""),
                        "attention_component_keys": health.get("attention_component_keys", []),
                        "gateway_online": health.get("gateway_online", False),
                        "qmt_connected": health.get("qmt_connected", False),
                    },
                )
            return {
                "ok": True,
                "latest_execution_bridge_health": latest,
                "trend_summary": trend_summary,
                "summary_lines": list((latest.get("health") or {}).get("summary_lines") or []),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.get("/monitor/execution-bridge-health/template")
    async def execution_bridge_health_template():
        template = build_execution_bridge_health_client_template()
        return {
            "ok": True,
            "template": template,
            "latest_descriptor": get_execution_bridge_health_latest_descriptor(),
            "deployment_contract_sample": build_execution_bridge_health_deployment_contract_sample(),
        }

    @router.get("/execution/gateway/intents/pending")
    async def gateway_pending_execution_intents(
        gateway_source_id: str | None = None,
        deployment_role: str | None = None,
        account_id: str | None = None,
        limit: int = 20,
    ):
        del gateway_source_id, deployment_role
        approved_items = [
            item
            for item in _get_pending_execution_intents()
            if str(item.get("status") or "") == "approved"
            and (not account_id or str(item.get("account_id") or "") == account_id)
        ]
        normalized_limit = max(1, min(limit, 100))
        items = approved_items[:normalized_limit]
        return {
            "ok": True,
            "available": bool(items),
            "items": items,
            "count": len(items),
            "summary_lines": [f"当前有 {len(items)} 条待执行 intent。"],
        }

    @router.post("/execution/gateway/intents/claim")
    async def claim_gateway_execution_intent(payload: ExecutionGatewayClaimInput):
        ok, claim_status, intent, reason = _claim_execution_intent(payload)
        return {
            "ok": ok,
            "claim_status": claim_status,
            "intent": intent,
            "reason": reason,
        }

    @router.post("/execution/gateway/receipts")
    async def record_gateway_execution_receipt(payload: ExecutionGatewayReceiptInput):
        ok, stored, latest_receipt, reason = _store_execution_gateway_receipt(payload)
        return {
            "ok": ok,
            "stored": stored,
            "latest_receipt": latest_receipt,
            "reason": reason,
            "summary_lines": ["receipt 已写入并更新 latest。"] if ok else [reason],
        }

    @router.get("/execution/gateway/receipts/latest")
    async def latest_gateway_execution_receipt():
        receipt = _get_latest_execution_gateway_receipt()
        return {
            "ok": True,
            "available": bool(receipt),
            "receipt": receipt,
        }

    @router.get("/execution/gateway/intents/{intent_id}")
    async def gateway_execution_intent_detail(intent_id: str):
        intent, _ = _find_execution_intent(intent_id)
        return {
            "ok": True,
            "available": bool(intent),
            "intent": intent,
        }

    @router.get("/cases")
    async def list_cases(trade_date: str | None = None, status: str | None = None, limit: int = 50):
        if not candidate_case_service:
            return {"items": [], "count": 0}
        items = [case.model_dump() for case in candidate_case_service.list_cases(trade_date=trade_date, status=status, limit=limit)]
        return {"items": items, "count": len(items), "trade_date": trade_date, "status": status}

    @router.get("/cases/{case_id}")
    async def get_case(case_id: str):
        if not candidate_case_service:
            return {"available": False}
        case = candidate_case_service.get_case(case_id)
        return case.model_dump() if case else {"available": False, "case_id": case_id}

    @router.get("/cases/{case_id}/vote-detail")
    async def get_case_vote_detail(case_id: str):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        payload = candidate_case_service.build_case_vote_detail(case_id)
        if not payload:
            return {"ok": False, "available": False, "case_id": case_id}
        return {"ok": True, "case": payload}

    @router.post("/cases/{case_id}/opinions")
    async def record_case_opinion(case_id: str, payload: CandidateOpinionInput):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        try:
            updated = candidate_case_service.record_opinion(
                case_id,
                    CandidateOpinion(
                        round=payload.round,
                        agent_id=payload.agent_id,
                        stance=_normalize_stance(payload.stance),
                        confidence=_normalize_confidence(payload.confidence),
                        reasons=payload.reasons,
                        evidence_refs=payload.evidence_refs,
                        thesis=payload.thesis,
                        key_evidence=payload.key_evidence,
                        evidence_gaps=payload.evidence_gaps,
                        questions_to_others=payload.questions_to_others,
                        challenged_by=payload.challenged_by,
                        challenged_points=payload.challenged_points,
                        previous_stance=(_normalize_stance(payload.previous_stance) if payload.previous_stance else None),
                        changed=payload.changed,
                        changed_because=payload.changed_because,
                        resolved_questions=payload.resolved_questions,
                        remaining_disputes=payload.remaining_disputes,
                        recorded_at=datetime.now().isoformat(),
                ),
            )
            if audit_store:
                audit_store.append(
                    category="discussion",
                    message=f"候选 case 观点已写入: {case_id}",
                    payload={"case_id": case_id, "agent_id": payload.agent_id, "round": payload.round},
                )
            return {"ok": True, "case": updated.model_dump()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/discussions/opinions/batch")
    async def record_batch_opinions(payload: BatchOpinionInput):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        try:
            writeback_items = [
                (
                    item.case_id,
                    CandidateOpinion(
                        round=item.round,
                        agent_id=item.agent_id,
                        stance=_normalize_stance(item.stance),
                        confidence=_normalize_confidence(item.confidence),
                        reasons=item.reasons,
                        evidence_refs=item.evidence_refs,
                        thesis=item.thesis,
                        key_evidence=item.key_evidence,
                        evidence_gaps=item.evidence_gaps,
                        questions_to_others=item.questions_to_others,
                        challenged_by=item.challenged_by,
                        challenged_points=item.challenged_points,
                        previous_stance=(_normalize_stance(item.previous_stance) if item.previous_stance else None),
                        changed=item.changed,
                        changed_because=item.changed_because,
                        resolved_questions=item.resolved_questions,
                        remaining_disputes=item.remaining_disputes,
                        recorded_at=datetime.now().isoformat(),
                    ),
                )
                for item in payload.items
            ]
            updated = _persist_discussion_writeback_items(
                writeback_items,
                auto_rebuild=payload.auto_rebuild,
                audit_message="批量写入候选 case 观点",
                audit_payload={"input": "manual_batch"},
            )
            return {"ok": True, "items": [item.model_dump() for item in updated], "count": len(updated)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/discussions/opinions/openclaw-ingress")
    async def record_openclaw_opinions(payload: OpenClawOpinionIngressInput):
        if not discussion_cycle_service:
            return {"ok": False, "error": "discussion cycle service not initialized"}
        try:
            trade_date = _resolve_trade_date(payload.trade_date)
            response = discussion_cycle_service.write_openclaw_opinions(
                payload.payload,
                trade_date=trade_date,
                expected_round=payload.expected_round,
                expected_agent_id=payload.expected_agent_id,
                expected_case_ids=(payload.expected_case_ids or None),
                auto_rebuild=payload.auto_rebuild,
                case_id_map=(payload.case_id_map or None),
                default_case_id=payload.default_case_id,
            )
            if response.get("ok") and audit_store:
                audit_store.append(
                    category="discussion",
                    message="写入 OpenClaw 讨论观点",
                    payload={
                        "input": "openclaw_ingress",
                        "trade_date": trade_date,
                        "expected_round": payload.expected_round,
                        "expected_agent_id": payload.expected_agent_id,
                        "count": response.get("written_count", 0),
                        "case_count": len(
                            {item.get("case_id") for item in response.get("items", []) if item.get("case_id")}
                        ),
                    },
                )
            return response
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/discussions/opinions/openclaw-preview")
    async def preview_openclaw_opinions(payload: OpenClawOpinionIngressInput):
        if not discussion_cycle_service:
            return {"ok": False, "error": "discussion cycle service not initialized"}
        try:
            trade_date = _resolve_trade_date(payload.trade_date)
            return discussion_cycle_service.adapt_openclaw_opinion_payload(
                payload.payload,
                trade_date=trade_date,
                expected_round=payload.expected_round,
                expected_agent_id=payload.expected_agent_id,
                expected_case_ids=(payload.expected_case_ids or None),
                case_id_map=(payload.case_id_map or None),
                default_case_id=payload.default_case_id,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/discussions/opinions/openclaw-replay-packet")
    async def build_openclaw_replay_packet(payload: OpenClawOpinionIngressInput):
        if not discussion_cycle_service:
            return {"ok": False, "error": "discussion cycle service not initialized"}
        try:
            trade_date = _resolve_trade_date(payload.trade_date)
            return discussion_cycle_service.build_openclaw_replay_packet(
                payload.payload,
                trade_date=trade_date,
                expected_round=payload.expected_round,
                expected_agent_id=payload.expected_agent_id,
                expected_case_ids=(payload.expected_case_ids or None),
                case_id_map=(payload.case_id_map or None),
                default_case_id=payload.default_case_id,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/discussions/opinions/openclaw-proposal-packet")
    async def build_openclaw_proposal_packet(payload: OpenClawOpinionIngressInput):
        if not discussion_cycle_service:
            return {"ok": False, "error": "discussion cycle service not initialized"}
        try:
            trade_date = _resolve_trade_date(payload.trade_date)
            return discussion_cycle_service.build_openclaw_proposal_packet(
                payload.payload,
                trade_date=trade_date,
                expected_round=payload.expected_round,
                expected_agent_id=payload.expected_agent_id,
                expected_case_ids=(payload.expected_case_ids or None),
                case_id_map=(payload.case_id_map or None),
                default_case_id=payload.default_case_id,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/discussions/opinions/openclaw-replay-packet/archive")
    async def archive_openclaw_replay_packet(payload: OpenClawOpinionIngressInput):
        if not discussion_cycle_service:
            return {"ok": False, "error": "discussion cycle service not initialized"}
        try:
            packet = _resolve_openclaw_archive_packet(payload, packet_type="openclaw_replay_packet")
            persisted = archive_store.persist_openclaw_packet(packet)
            if meeting_state_store:
                meeting_state_store.set("latest_openclaw_replay_packet", persisted)
            if audit_store:
                audit_store.append(
                    category="discussion",
                    message="OpenClaw replay packet 已归档",
                    payload={
                        "packet_type": persisted.get("packet_type"),
                        "packet_id": persisted.get("packet_id"),
                        "trade_date": persisted.get("trade_date"),
                        "archive_path": ((persisted.get("archive_manifest") or {}).get("archive_path") or ""),
                    },
                )
            return {"ok": True, "packet": persisted}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/discussions/opinions/openclaw-proposal-packet/archive")
    async def archive_openclaw_proposal_packet(payload: OpenClawOpinionIngressInput):
        if not discussion_cycle_service:
            return {"ok": False, "error": "discussion cycle service not initialized"}
        try:
            packet = _resolve_openclaw_archive_packet(payload, packet_type="openclaw_proposal_packet")
            persisted = archive_store.persist_openclaw_packet(packet)
            if meeting_state_store:
                meeting_state_store.set("latest_openclaw_proposal_packet", persisted)
            if audit_store:
                audit_store.append(
                    category="discussion",
                    message="OpenClaw proposal packet 已归档",
                    payload={
                        "packet_type": persisted.get("packet_type"),
                        "packet_id": persisted.get("packet_id"),
                        "trade_date": persisted.get("trade_date"),
                        "archive_path": ((persisted.get("archive_manifest") or {}).get("archive_path") or ""),
                    },
                )
            return {"ok": True, "packet": persisted}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/cases/{case_id}/rebuild")
    async def rebuild_case(case_id: str):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        try:
            case = candidate_case_service.rebuild_case(case_id)
            if audit_store:
                audit_store.append(
                    category="discussion",
                    message=f"候选 case 汇总已重建: {case_id}",
                    payload={"case_id": case_id, "final_status": case.final_status},
                )
            return {"ok": True, "case": case.model_dump()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/cases/rebuild")
    async def rebuild_cases(trade_date: str | None = None):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        items = [case.model_dump() for case in candidate_case_service.rebuild_cases(trade_date=trade_date)]
        if audit_store:
            audit_store.append(
                category="discussion",
                message="批量重建候选 case 汇总",
                payload={"trade_date": trade_date, "count": len(items)},
            )
        return {"ok": True, "items": items, "count": len(items), "trade_date": trade_date}

    @router.get("/discussions/summary")
    async def get_discussion_summary(trade_date: str):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        return {"ok": True, **candidate_case_service.build_trade_date_summary(trade_date)}

    @router.get("/discussions/reason-board")
    async def get_discussion_reason_board(trade_date: str):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        payload = candidate_case_service.build_reason_board(trade_date)
        cycle = discussion_cycle_service.get_cycle(trade_date) if discussion_cycle_service else None
        if cycle:
            payload["cycle"] = {
                "cycle_id": cycle.cycle_id,
                "discussion_state": cycle.discussion_state,
                "pool_state": cycle.pool_state,
                "round_2_target_case_ids": cycle.round_2_target_case_ids,
                "execution_pool_case_ids": cycle.execution_pool_case_ids,
                "blockers": cycle.blockers,
                "updated_at": cycle.updated_at,
            }
        return {"ok": True, **payload}

    @router.get("/discussions/agent-packets")
    async def get_discussion_agent_packets(
        trade_date: str,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        supported_agent_ids = {None, "ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"}
        if agent_id not in supported_agent_ids:
            return {"ok": False, "error": f"unsupported agent_id: {agent_id}"}
        cases = candidate_case_service.list_cases(trade_date=trade_date, status=status, limit=limit)
        shared_context = _resolve_shared_context(trade_date)
        workspace_context = _resolve_workspace_context_for_trade_date(trade_date)
        discussion_summary = candidate_case_service.build_trade_date_summary(trade_date)
        items = [_build_agent_packet(case, requested_agent_id=agent_id) for case in cases]
        summary_lines = [
            f"交易日 {trade_date} 统一 dossier 包：{len(items)} 只候选，agent={agent_id or 'shared'}，shared_context={shared_context.get('source_layer')}",
        ]
        if workspace_context:
            summary_lines.append(
                f"workspace status={workspace_context.get('status')} runtime={'yes' if workspace_context.get('runtime_context') else 'no'} discussion={'yes' if workspace_context.get('discussion_context') else 'no'} monitor={'yes' if workspace_context.get('monitor_context') else 'no'}"
            )
        if items:
            summary_lines.append(
                "候选：" + "；".join(item.get("symbol_display", item["symbol"]) for item in items[: min(5, len(items))])
            )
        if discussion_summary.get("controversy_summary_lines"):
            summary_lines.append("争议焦点：" + "；".join(discussion_summary.get("controversy_summary_lines", [])[:3]))
        if discussion_summary.get("round_2_guidance"):
            summary_lines.append("二轮要求：" + "；".join(discussion_summary.get("round_2_guidance", [])[:3]))
        return {
            "ok": True,
            "trade_date": trade_date,
            **build_agent_packets_envelope(
                {
                    "status_filter": status,
                    "case_count": len(items),
                    "round_coverage": discussion_summary.get("round_coverage", {}),
                    "controversy_summary_lines": discussion_summary.get("controversy_summary_lines", []),
                    "round_2_guidance": discussion_summary.get("round_2_guidance", []),
                    "shared_context": shared_context,
                    "shared_context_lines": _build_shared_context_lines(shared_context),
                    "workspace_context": workspace_context,
                    "workspace_summary_lines": workspace_context.get("summary_lines", []) if workspace_context else [],
                    "data_catalog_ref": _build_data_catalog_ref(),
                    "preferred_read_order": _build_packet_read_order(),
                    "items": items,
                    "summary_lines": summary_lines,
                    "summary_text": "\n".join(summary_lines),
                },
                trade_date=trade_date,
                requested_agent_id=agent_id,
            ),
        }

    @router.get("/discussions/vote-board")
    async def get_discussion_vote_board(trade_date: str, status: str | None = None, limit: int = 100):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        payload = candidate_case_service.build_vote_board(trade_date, status=status, limit=limit)
        cycle = discussion_cycle_service.get_cycle(trade_date) if discussion_cycle_service else None
        if cycle:
            payload["cycle"] = {
                "cycle_id": cycle.cycle_id,
                "discussion_state": cycle.discussion_state,
                "pool_state": cycle.pool_state,
                "round_2_target_case_ids": cycle.round_2_target_case_ids,
                "execution_pool_case_ids": cycle.execution_pool_case_ids,
                "blockers": cycle.blockers,
                "updated_at": cycle.updated_at,
            }
        return {"ok": True, **payload}

    @router.get("/discussions/reply-pack")
    async def get_discussion_reply_pack(
        trade_date: str,
        selected_limit: int = 3,
        watchlist_limit: int = 5,
        rejected_limit: int = 5,
    ):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        payload = _enrich_discussion_payload(
            trade_date,
            candidate_case_service.build_reply_pack(
                trade_date,
                selected_limit=selected_limit,
                watchlist_limit=watchlist_limit,
                rejected_limit=rejected_limit,
            ),
        )
        cycle = discussion_cycle_service.get_cycle(trade_date) if discussion_cycle_service else None
        if cycle:
            payload["cycle"] = {
                "cycle_id": cycle.cycle_id,
                "discussion_state": cycle.discussion_state,
                "pool_state": cycle.pool_state,
                "round_2_target_case_ids": cycle.round_2_target_case_ids,
                "execution_pool_case_ids": cycle.execution_pool_case_ids,
                "blockers": cycle.blockers,
                "updated_at": cycle.updated_at,
            }
        return {"ok": True, **payload}

    @router.get("/discussions/final-brief")
    async def get_discussion_final_brief(trade_date: str, selection_limit: int = 3):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        payload = _enrich_discussion_payload(
            trade_date,
            candidate_case_service.build_final_brief(
                trade_date,
                selection_limit=selection_limit,
            ),
        )
        cycle = discussion_cycle_service.get_cycle(trade_date) if discussion_cycle_service else None
        if cycle:
            payload["cycle"] = {
                "cycle_id": cycle.cycle_id,
                "discussion_state": cycle.discussion_state,
                "pool_state": cycle.pool_state,
                "round_2_target_case_ids": cycle.round_2_target_case_ids,
                "execution_pool_case_ids": cycle.execution_pool_case_ids,
                "blockers": cycle.blockers,
                "updated_at": cycle.updated_at,
            }
        return {"ok": True, **payload}

    @router.get("/discussions/client-brief")
    async def get_discussion_client_brief(
        trade_date: str,
        selection_limit: int = 3,
        watchlist_limit: int = 5,
        rejected_limit: int = 5,
    ):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        payload = _build_client_brief(
            trade_date,
            selection_limit=selection_limit,
            watchlist_limit=watchlist_limit,
            rejected_limit=rejected_limit,
        )
        return {"ok": True, **payload}

    @router.get("/discussions/meeting-context")
    async def get_discussion_meeting_context(trade_date: str | None = None):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        if trade_date and meeting_state_store:
            stored = _sanitize_json_compatible(meeting_state_store.get(f"discussion_context:{trade_date}"))
            if stored:
                return {"ok": True, **stored}
        if not trade_date and meeting_state_store:
            stored = _sanitize_json_compatible(meeting_state_store.get("latest_discussion_context"))
            if stored:
                return {"ok": True, **stored}
        if not trade_date:
            latest = _sanitize_json_compatible(serving_store.get_latest_discussion_context())
            if latest:
                return {"ok": True, **latest}
            return {"ok": True, "available": False, "resource": "discussion_context"}
        payload = _build_discussion_context_payload(trade_date)
        _persist_discussion_context(payload)
        return {"ok": True, **_sanitize_json_compatible(payload)}

    @router.get("/discussions/finalize-packet")
    async def get_discussion_finalize_packet(trade_date: str):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        meeting_context = _build_discussion_context_payload(trade_date)
        return {
            "ok": True,
            "trade_date": trade_date,
            **_sanitize_json_compatible(meeting_context.get("finalize_packet") or {}),
        }

    @router.get("/discussions/execution-precheck")
    async def get_discussion_execution_precheck(trade_date: str, account_id: str | None = None):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        return {"ok": True, **_build_execution_precheck(trade_date, account_id=account_id)}

    @router.get("/discussions/execution-intents")
    async def get_discussion_execution_intents(trade_date: str, account_id: str | None = None):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        return {"ok": True, **_build_execution_intents(trade_date, account_id=account_id)}

    @router.post("/discussions/execution-intents/dispatch")
    async def dispatch_discussion_execution_intents(payload: ExecutionIntentDispatchInput):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        result = _build_execution_dispatch_receipts(
            trade_date=payload.trade_date,
            account_id=payload.account_id,
            intent_ids=payload.intent_ids,
            apply=payload.apply,
        )
        _persist_execution_dispatch(result)
        summary_dispatch_result = _dispatch_execution_dispatch_summary(result)
        alert_result = (
            live_execution_alert_notifier.dispatch_dispatch(result)
            if settings.run_mode == "live" and live_execution_alert_notifier
            else None
        )
        if audit_store:
            audit_store.append(
                category="execution",
                message=f"执行意图已派发: {payload.trade_date}",
                payload={
                    "trade_date": payload.trade_date,
                    "apply": payload.apply,
                    "submitted_count": result.get("submitted_count", 0),
                    "preview_count": result.get("preview_count", 0),
                    "blocked_count": result.get("blocked_count", 0),
                    "status": result.get("status"),
                    "summary_dispatched": summary_dispatch_result.get("dispatched", False),
                    "summary_reason": summary_dispatch_result.get("reason"),
                    "alert_dispatched": (alert_result.dispatched if alert_result else False),
                    "alert_reason": (alert_result.reason if alert_result else "disabled"),
                },
            )
        return {
            "ok": True,
            **result,
            "summary_notification": summary_dispatch_result,
            "notification": (
                {
                    "dispatched": alert_result.dispatched,
                    "reason": alert_result.reason,
                    "level": alert_result.payload.get("level"),
                    "title": alert_result.payload.get("title"),
                }
                if alert_result
                else {"dispatched": False, "reason": "disabled"}
            ),
        }

    @router.get("/discussions/execution-dispatch/latest")
    async def get_latest_execution_dispatch(trade_date: str | None = None):
        if not meeting_state_store:
            return {"ok": False, "error": "meeting state store not initialized"}
        if trade_date:
            payload = meeting_state_store.get(f"execution_dispatch:{trade_date}")
            if payload:
                normalized_payload = _attach_control_plane_gateway_summary(
                    payload,
                    trade_date=trade_date,
                    latest_dispatch_payload=payload,
                )
                return {"ok": True, **normalized_payload}
            not_found_payload = {
                "ok": True,
                "available": False,
                "trade_date": trade_date,
                "status": "not_found",
                "summary_lines": [f"{trade_date} 尚无执行派发回执。"],
            }
            return _attach_control_plane_gateway_summary(not_found_payload, trade_date=trade_date)
        payload = meeting_state_store.get("latest_execution_dispatch")
        if not payload:
            not_found_payload = {
                "ok": True,
                "available": False,
                "status": "not_found",
                "summary_lines": ["当前尚无执行派发回执。"],
            }
            return _attach_control_plane_gateway_summary(not_found_payload)
        normalized_payload = _attach_control_plane_gateway_summary(
            payload,
            trade_date=str(payload.get("trade_date") or "") or None,
            latest_dispatch_payload=payload,
        )
        return {"ok": True, **normalized_payload}

    @router.get("/execution/gateway/intents/pending")
    async def list_pending_gateway_intents(
        gateway_source_id: str | None = None,
        deployment_role: str | None = None,
        account_id: str | None = None,
        limit: int = 20,
    ):
        items = _get_pending_execution_intents()
        if account_id:
            items = [item for item in items if str(item.get("account_id") or "") == account_id]
        items = [item for item in items if str(item.get("status") or "approved") == "approved"]
        capped_limit = max(1, min(limit, 100))
        items = items[:capped_limit]
        return {
            "ok": True,
            "available": bool(items),
            "gateway_source_id": gateway_source_id or "",
            "deployment_role": deployment_role or "",
            "count": len(items),
            "items": items,
            "summary_lines": [f"当前有 {len(items)} 条待 Windows Gateway 拉取的执行意图。"],
        }

    @router.post("/execution/gateway/intents/claim")
    async def claim_gateway_intent(payload: ExecutionGatewayClaimInput):
        ok, intent, reason = _claim_execution_intent(
            payload.intent_id,
            payload.gateway_source_id,
            payload.deployment_role,
            payload.bridge_path,
            payload.claimed_at,
        )
        return {
            "ok": ok,
            "claim_status": "claimed" if ok else ("conflict" if intent else "not_found"),
            "intent": intent,
            "reason": reason,
        }

    @router.post("/execution/gateway/receipts")
    async def record_execution_gateway_receipt(payload: ExecutionGatewayReceiptInput):
        try:
            latest_receipt = _store_execution_gateway_receipt(payload)
        except ValueError as exc:
            return {"ok": False, "stored": False, "error": str(exc)}
        return {
            "ok": True,
            "stored": True,
            "latest_receipt": latest_receipt,
            "summary_lines": ["execution gateway receipt 已写入并更新 latest。"],
        }

    @router.get("/execution/gateway/receipts/latest")
    async def get_latest_execution_gateway_receipt():
        receipt = _get_latest_execution_gateway_receipt()
        return {
            "ok": True,
            "available": bool(receipt),
            "receipt": receipt,
            "summary_lines": ["当前尚无 execution gateway receipt。"] if not receipt else list(receipt.get("summary_lines") or []),
        }

    @router.get("/execution/gateway/intents/{intent_id}")
    async def get_execution_gateway_intent(intent_id: str):
        intent = _find_execution_intent(intent_id)
        return {
            "ok": True,
            "available": bool(intent),
            "intent": intent,
            "summary_lines": [f"{intent_id} 未找到。"] if not intent else list(intent.get("summary_lines") or []),
        }

    @router.get("/discussions/execution-orders/inspection")
    async def inspect_pending_execution_orders(account_id: str | None = None):
        if not pending_order_inspection_service:
            return {"ok": False, "error": "pending order inspection service not initialized"}
        resolved_account_id = _resolve_account_id(account_id)
        runtime_config = config_mgr.get() if config_mgr else None
        warn_after_seconds = int(getattr(runtime_config, "pending_order_warn_seconds", 300) or 300)
        payload = pending_order_inspection_service.inspect(
            resolved_account_id,
            warn_after_seconds=warn_after_seconds,
            persist=True,
        )
        if (
            settings.run_mode == "live"
            and settings.notify.alerts_enabled
            and message_dispatcher
            and payload.get("status") in {"warning", "error"}
        ):
            message_dispatcher.dispatch_alert("\n".join(payload.get("summary_lines", [])))
        return {"ok": True, **payload}

    @router.post("/discussions/execution-orders/remediation/run")
    async def run_pending_order_remediation(account_id: str | None = None):
        if not pending_order_remediation_service:
            return {"ok": False, "error": "pending order remediation service not initialized"}
        resolved_account_id = _resolve_account_id(account_id)
        runtime_config = config_mgr.get() if config_mgr else None
        auto_action = str(getattr(runtime_config, "pending_order_auto_action", "alert_only") or "alert_only")
        cancel_after_seconds = int(getattr(runtime_config, "pending_order_cancel_after_seconds", 900) or 900)
        payload = pending_order_remediation_service.remediate(
            resolved_account_id,
            auto_action=auto_action,
            cancel_after_seconds=cancel_after_seconds,
            persist=True,
        )
        for receipt in payload.get("receipts", []):
            if receipt.get("status") == "cancel_submitted":
                cancel_result = receipt.get("cancel_result") or {}
                _dispatch_trade_event(
                    "自动撤单",
                    symbol=receipt.get("symbol") or cancel_result.get("symbol") or "",
                    name=receipt.get("name") or "",
                    account_id=resolved_account_id,
                    side=cancel_result.get("side"),
                    quantity=cancel_result.get("quantity"),
                    price=cancel_result.get("price"),
                    order_id=receipt.get("order_id"),
                    status=cancel_result.get("status") or receipt.get("status"),
                    decision_id=receipt.get("decision_id"),
                    reason=receipt.get("reason"),
                    level="warning",
                )
            elif receipt.get("status") == "cancel_failed":
                _dispatch_trade_event(
                    "自动撤单失败",
                    symbol=receipt.get("symbol") or "",
                    name=receipt.get("name") or "",
                    account_id=resolved_account_id,
                    order_id=receipt.get("order_id"),
                    status=receipt.get("status"),
                    decision_id=receipt.get("decision_id"),
                    reason=receipt.get("reason"),
                    level="warning",
                )
        if audit_store:
            audit_store.append(
                category="execution",
                message="执行未决订单处置",
                payload={
                    "account_id": resolved_account_id,
                    "status": payload.get("status"),
                    "auto_action": auto_action,
                    "stale_count": payload.get("stale_count", 0),
                    "actioned_count": payload.get("actioned_count", 0),
                    "cancelled_count": payload.get("cancelled_count", 0),
                },
            )
        if (
            settings.run_mode == "live"
            and settings.notify.alerts_enabled
            and message_dispatcher
            and payload.get("actioned_count", 0) > 0
        ):
            message_dispatcher.dispatch_alert("\n".join(payload.get("summary_lines", [])))
        return {"ok": True, **payload}

    @router.get("/discussions/execution-orders/remediation/latest")
    async def get_latest_pending_order_remediation():
        if not meeting_state_store:
            return {"ok": False, "error": "meeting state store not initialized"}
        payload = meeting_state_store.get("latest_pending_order_remediation")
        if not payload:
            return {
                "ok": True,
                "available": False,
                "status": "not_found",
                "summary_lines": ["当前尚无未决订单处置记录。"],
            }
        return {"ok": True, **payload}

    @router.post("/startup-recovery/run")
    async def run_startup_recovery(account_id: str | None = None):
        if not startup_recovery_service:
            return {"ok": False, "error": "startup recovery service not initialized"}
        resolved_account_id = _resolve_account_id(account_id)
        payload = startup_recovery_service.recover(resolved_account_id, persist=True)
        if audit_store:
            audit_store.append(
                category="execution",
                message="执行启动恢复扫描",
                payload={
                    "account_id": resolved_account_id,
                    "status": payload.get("status"),
                    "order_count": payload.get("order_count", 0),
                    "pending_count": payload.get("pending_count", 0),
                    "orphan_count": payload.get("orphan_count", 0),
                    "updated_count": payload.get("updated_count", 0),
                },
            )
        return {"ok": payload.get("status") in {"ok", "queued_for_gateway"}, **payload}

    @router.get("/startup-recovery/latest")
    async def get_latest_startup_recovery():
        if not meeting_state_store:
            return {"ok": False, "error": "meeting state store not initialized"}
        payload = meeting_state_store.get("latest_startup_recovery")
        if not payload:
            return {
                "ok": True,
                "available": False,
                "status": "not_found",
                "summary_lines": ["当前尚无启动恢复记录。"],
            }
        return {"ok": payload.get("status") in {"ok", "queued_for_gateway"}, **payload}

    @router.post("/execution-reconciliation/run")
    async def run_execution_reconciliation(account_id: str | None = None):
        if not execution_reconciliation_service:
            return {"ok": False, "error": "execution reconciliation service not initialized"}
        resolved_account_id = _resolve_account_id(account_id)
        payload = execution_reconciliation_service.reconcile(resolved_account_id, persist=True)
        attribution_payload = _sync_reconciliation_attribution(payload)
        if attribution_payload:
            payload["attribution"] = attribution_payload
            payload["summary_lines"] = list(payload.get("summary_lines", [])) + [
                f"归因回写 {attribution_payload['update_count']} 条，来源 execution_reconciliation。"
            ]
            if meeting_state_store:
                meeting_state_store.set("latest_execution_reconciliation", payload)
        if audit_store:
            audit_store.append(
                category="execution",
                message="执行成交与账户对账",
                payload={
                    "account_id": resolved_account_id,
                    "status": payload.get("status"),
                    "matched_order_count": payload.get("matched_order_count", 0),
                    "filled_order_count": payload.get("filled_order_count", 0),
                    "trade_count": payload.get("trade_count", 0),
                    "orphan_trade_count": payload.get("orphan_trade_count", 0),
                    "position_count": payload.get("position_count", 0),
                    "attribution_update_count": (
                        (payload.get("attribution") or {}).get("update_count", 0)
                    ),
                },
            )
        return {"ok": payload.get("status") in {"ok", "queued_for_gateway"}, **payload}

    @router.get("/execution-reconciliation/latest")
    async def get_latest_execution_reconciliation():
        if not meeting_state_store:
            return {"ok": False, "error": "meeting state store not initialized"}
        payload = meeting_state_store.get("latest_execution_reconciliation")
        if not payload:
            return {
                "ok": True,
                "available": False,
                "status": "not_found",
                "summary_lines": ["当前尚无执行对账记录。"],
            }
        return {"ok": payload.get("status") == "ok", **payload}

    @router.post("/tail-market/run")
    async def run_tail_market(account_id: str | None = None):
        if not execution_adapter or not market_adapter or not meeting_state_store:
            return {"ok": False, "error": "tail market services not initialized"}
        resolved_account_id = _resolve_account_id(account_id)
        payload = run_tail_market_scan(
            settings=settings,
            market=market_adapter,
            execution_adapter=execution_adapter,
            meeting_state_store=meeting_state_store,
            runtime_state_store=runtime_state_store,
            candidate_case_service=candidate_case_service,
            dispatcher=message_dispatcher,
            runtime_context=(
                serving_store.get_latest_runtime_context()
                or (runtime_state_store.get("latest_runtime_context", {}) if runtime_state_store else {})
                or {}
            ),
            discussion_context=(
                serving_store.get_latest_discussion_context()
                or meeting_state_store.get("latest_discussion_context", {})
                or {}
            ),
            account_id=resolved_account_id,
        )
        if audit_store:
            audit_store.append(
                category="risk",
                message="手动触发尾盘卖出扫描",
                payload={
                    "account_id": resolved_account_id,
                    "status": payload.get("status"),
                    "position_count": payload.get("position_count", 0),
                    "signal_count": payload.get("signal_count", 0),
                    "submitted_count": payload.get("submitted_count", 0),
                    "preview_count": payload.get("preview_count", 0),
                    "error_count": payload.get("error_count", 0),
                },
            )
        return {"ok": payload.get("status") in {"ok", "queued_for_gateway"}, **payload}

    @router.get("/tail-market/latest")
    async def get_latest_tail_market_scan():
        if not meeting_state_store:
            return {"ok": False, "error": "meeting state store not initialized"}
        payload = meeting_state_store.get("latest_tail_market_scan")
        if not payload:
            not_found_payload = {
                "ok": True,
                "available": False,
                "status": "not_found",
                "summary_lines": ["当前尚无尾盘卖出扫描记录。"],
            }
            attached = _attach_control_plane_gateway_summary(not_found_payload)
            return {"ok": True, **attached}
        attached = _attach_control_plane_gateway_summary(
            payload,
            trade_date=str(payload.get("trade_date") or "") or None,
            latest_tail_market_payload=payload,
        )
        return {"ok": payload.get("status") in {"ok", "queued_for_gateway"}, **attached}

    @router.get("/tail-market/history")
    async def get_tail_market_scan_history(limit: int = 20):
        if not meeting_state_store:
            return {"ok": False, "error": "meeting state store not initialized"}
        history = meeting_state_store.get("tail_market_history", [])
        normalized_limit = max(min(int(limit or 20), 100), 1)
        items = list(reversed(history[-normalized_limit:]))
        return {
            "ok": True,
            "count": len(items),
            "items": items,
        }

    def _build_tail_market_review_summary(
        *,
        scan_payloads: list[dict[str, Any]],
        symbol: str | None = None,
        exit_reason: str | None = None,
        review_tag: str | None = None,
    ) -> dict[str, Any]:
        matched_items: list[dict[str, Any]] = []
        for payload in scan_payloads:
            for item in list(payload.get("items") or []):
                if not item.get("exit_reason"):
                    continue
                item_symbol = str(item.get("symbol") or "")
                item_reason = str(item.get("exit_reason") or "")
                item_tags = [str(tag) for tag in list(item.get("review_tags") or [])]
                if symbol and item_symbol != symbol:
                    continue
                if exit_reason and item_reason != exit_reason:
                    continue
                if review_tag and review_tag not in item_tags:
                    continue
                matched_items.append(
                    {
                        "trade_date": payload.get("trade_date"),
                        "scanned_at": payload.get("scanned_at"),
                        "account_id": payload.get("account_id"),
                        **item,
                    }
                )

        def _group_count(key_name: str) -> list[dict[str, Any]]:
            buckets: dict[str, int] = {}
            for item in matched_items:
                key = str(item.get(key_name) or "")
                if not key:
                    continue
                buckets[key] = buckets.get(key, 0) + 1
            return [
                {"key": key, "count": count}
                for key, count in sorted(buckets.items(), key=lambda pair: (-pair[1], pair[0]))
            ]

        review_tag_buckets: dict[str, int] = {}
        for item in matched_items:
            for tag in list(item.get("review_tags") or []):
                normalized_tag = str(tag or "")
                if not normalized_tag:
                    continue
                review_tag_buckets[normalized_tag] = review_tag_buckets.get(normalized_tag, 0) + 1
        by_review_tag = [
            {"key": key, "count": count}
            for key, count in sorted(review_tag_buckets.items(), key=lambda pair: (-pair[1], pair[0]))
        ]

        summary_lines = [
            f"tail-market review 命中 {len(matched_items)} 条退出扫描项。"
        ]
        if by_review_tag:
            summary_lines.append(
                "主要 review_tags: " + "；".join(f"{item['key']}({item['count']})" for item in by_review_tag[:5])
            )
        if _group_count("exit_reason"):
            reason_top = _group_count("exit_reason")
            summary_lines.append(
                "主要退出原因: " + "；".join(f"{item['key']}({item['count']})" for item in reason_top[:5])
            )

        return {
            "ok": True,
            "available": bool(matched_items),
            "filters": {
                "symbol": symbol,
                "exit_reason": exit_reason,
                "review_tag": review_tag,
            },
            "count": len(matched_items),
            "items": matched_items,
            "by_symbol": _group_count("symbol"),
            "by_exit_reason": _group_count("exit_reason"),
            "by_review_tag": by_review_tag,
            "summary_lines": summary_lines,
        }

    def _build_governance_review_summary(
        *,
        trade_date: str | None = None,
        score_date: str | None = None,
        due_within_days: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        inspection = _collect_parameter_hint_inspection(
            trade_date=trade_date,
            score_date=score_date,
            statuses="evaluating,approved,effective",
            due_within_days=due_within_days,
            limit=limit,
        )
        action_items = []
        for item in list(inspection.get("high_priority_action_items") or [])[:5]:
            recommended_action = dict(item.get("recommended_action") or {})
            action_items.append(
                {
                    "source": "governance",
                    "event_id": item.get("event_id"),
                    "param_key": item.get("param_key"),
                    "event_type": item.get("event_type"),
                    "priority": recommended_action.get("priority") or "medium",
                    "title": (
                        f"参数 {item.get('param_key')} 待处理"
                        if item.get("param_key")
                        else "参数治理待处理项"
                    ),
                    "reason": recommended_action.get("reason"),
                    "recommended_action": recommended_action,
                    "operation_targets": list(item.get("operation_targets") or []),
                }
            )
        return {
            "available": bool(inspection.get("inspected_count", 0)),
            "inspected_count": inspection.get("inspected_count", 0),
            "action_item_count": inspection.get("action_item_count", 0),
            "high_priority_action_item_count": inspection.get("high_priority_action_item_count", 0),
            "pending_high_risk_rollback_count": inspection.get("pending_high_risk_rollback_count", 0),
            "observation_overdue_count": inspection.get("observation_overdue_count", 0),
            "recommended_action_counts": inspection.get("recommended_action_counts", {}),
            "action_items": action_items,
            "summary_lines": list(inspection.get("summary_lines") or []),
            "inspection": inspection,
        }

    def _build_tail_market_review_section(
        *,
        source: str = "latest",
        limit: int = 20,
    ) -> dict[str, Any]:
        normalized_source = str(source or "latest").lower()
        if normalized_source not in {"latest", "history"}:
            normalized_source = "latest"
        if normalized_source == "latest":
            latest = meeting_state_store.get("latest_tail_market_scan") if meeting_state_store else None
            payloads = [latest] if latest else []
        else:
            history = meeting_state_store.get("tail_market_history", []) if meeting_state_store else []
            normalized_limit = max(min(int(limit or 20), 100), 1)
            payloads = list(reversed(history[-normalized_limit:]))
        review = _build_tail_market_review_summary(
            scan_payloads=[item for item in payloads if item],
        )
        action_items = []
        for item in list(review.get("items") or [])[:5]:
            action_items.append(
                {
                    "source": "tail_market",
                    "symbol": item.get("symbol"),
                    "priority": "medium",
                    "title": f"{item.get('symbol')} 尾盘退出 {item.get('exit_reason')}",
                    "reason": "尾盘自动退出扫描已命中，建议纳入盘后 review。",
                    "review_tags": list(item.get("review_tags") or []),
                    "operation_targets": [
                        {
                            "label": "查看 tail-market review",
                            "method": "GET",
                            "path": "/system/tail-market/review",
                            "query": {
                                "source": "history",
                                "symbol": item.get("symbol"),
                                "exit_reason": item.get("exit_reason"),
                            },
                        }
                    ],
                }
            )
        return {
            "available": bool(review.get("available")),
            "source": normalized_source,
            "count": review.get("count", 0),
            "by_symbol": review.get("by_symbol", []),
            "by_exit_reason": review.get("by_exit_reason", []),
            "by_review_tag": review.get("by_review_tag", []),
            "summary_lines": list(review.get("summary_lines") or []),
            "action_items": action_items,
            "review": review,
        }

    def _build_discussion_review_summary(trade_date: str | None = None) -> dict[str, Any]:
        context = None
        resolved_trade_date = trade_date
        if resolved_trade_date:
            context = _build_discussion_context_payload(resolved_trade_date)
        else:
            if meeting_state_store:
                context = _sanitize_json_compatible(meeting_state_store.get("latest_discussion_context"))
            if not context:
                context = _sanitize_json_compatible(serving_store.get_latest_discussion_context())
            resolved_trade_date = str((context or {}).get("trade_date") or "") or None
            if resolved_trade_date and not (context or {}).get("finalize_packet"):
                context = _build_discussion_context_payload(resolved_trade_date)
        if not context:
            return {
                "available": False,
                "trade_date": resolved_trade_date,
                "status": "not_found",
                "discussion_state": None,
                "blocked": False,
                "blocked_count": 0,
                "approved_count": 0,
                "selected_count": 0,
                "action_items": [],
                "summary_lines": ["当前尚无可用于盘后 review 的 discussion 收口结果。"],
            }
        client_brief = dict(context.get("client_brief") or {})
        final_brief = dict(context.get("final_brief") or {})
        reply_pack = dict(context.get("reply_pack") or {})
        finalize_packet = dict(context.get("finalize_packet") or {})
        execution_precheck = dict(
            finalize_packet.get("execution_precheck")
            or client_brief.get("execution_precheck")
            or {}
        )
        cycle = dict(context.get("cycle") or {})
        status = str(
            client_brief.get("status")
            or final_brief.get("status")
            or finalize_packet.get("status")
            or context.get("status")
            or "unknown"
        )
        blocked = bool(finalize_packet.get("blocked"))
        blocked_count = int(execution_precheck.get("blocked_count", 0) or 0)
        approved_count = int(execution_precheck.get("approved_count", 0) or 0)
        selected_count = int(final_brief.get("selected_count", 0) or reply_pack.get("selected_count", 0) or 0)
        action_items = []
        if blocked or blocked_count > 0 or status == "blocked":
            action_items.append(
                {
                    "source": "discussion",
                    "trade_date": resolved_trade_date,
                    "priority": "high",
                    "title": "讨论收口仍存在执行阻断",
                    "reason": (
                        f"discussion 状态 {status}，执行阻断 {blocked_count}，建议先复核 finalize packet 与 execution precheck。"
                    ),
                    "operation_targets": [
                        {
                            "label": "查看 finalize packet",
                            "method": "GET",
                            "path": "/system/discussions/finalize-packet",
                            "query": {"trade_date": resolved_trade_date},
                        },
                        {
                            "label": "查看 execution precheck",
                            "method": "GET",
                            "path": "/system/discussions/execution-precheck",
                            "query": {"trade_date": resolved_trade_date},
                        },
                    ],
                }
            )
        summary_lines = [
            f"discussion 状态 {status}，入选 {selected_count}，执行通过 {approved_count}，执行阻断 {blocked_count}。"
        ]
        if client_brief.get("lines"):
            summary_lines.extend(list(client_brief.get("lines") or [])[:2])
        elif finalize_packet.get("summary_lines"):
            summary_lines.extend(list(finalize_packet.get("summary_lines") or [])[:2])
        return {
            "available": True,
            "trade_date": resolved_trade_date,
            "status": status,
            "discussion_state": cycle.get("discussion_state"),
            "blocked": blocked,
            "blocked_count": blocked_count,
            "approved_count": approved_count,
            "selected_count": selected_count,
            "action_items": action_items,
            "summary_lines": summary_lines,
            "finalize_packet": finalize_packet,
            "client_brief": client_brief,
        }

    def _build_offline_backtest_review_summary() -> dict[str, Any]:
        report = _load_latest_offline_backtest_attribution()
        if not report or not report.get("available"):
            return {
                "available": False,
                "trade_count": 0,
                "selected_weakest_bucket": {},
                "selected_compare_view": {},
                "action_items": [],
                "summary_lines": ["当前尚无可用于盘后 review 的离线回测 attribution 结果。"],
            }
        overview = dict(report.get("overview") or {})
        selected_weakest_bucket = dict(report.get("selected_weakest_bucket") or {})
        selected_compare_view = dict(report.get("selected_compare_view") or {})
        weakest_buckets = list(report.get("weakest_buckets") or [])
        action_items = []
        weakest_avg_return = float(selected_weakest_bucket.get("avg_return_pct", 0.0) or 0.0)
        if selected_weakest_bucket and weakest_avg_return < 0:
            action_items.append(
                {
                    "source": "offline_backtest",
                    "priority": "medium",
                    "title": "离线回测最弱分桶仍为负收益",
                    "reason": (
                        f"{selected_weakest_bucket.get('dimension')}:{selected_weakest_bucket.get('key')} "
                        f"平均收益 {weakest_avg_return:.2%}，建议复核战法/市场状态适配。"
                    ),
                    "operation_targets": [
                        {
                            "label": "查看离线回测 attribution",
                            "method": "GET",
                            "path": "/system/reports/offline-backtest-attribution",
                        }
                    ],
                }
            )
        summary_lines = list(report.get("summary_lines") or [])
        if not summary_lines:
            summary_lines = [
                f"离线回测样本 {overview.get('trade_count', 0)} 笔，平均收益 {overview.get('avg_return_pct', 0.0):.2%}。"
            ]
        return {
            "available": True,
            "trade_count": int(overview.get("trade_count", 0) or 0),
            "overview": overview,
            "selected_weakest_bucket": selected_weakest_bucket,
            "selected_compare_view": selected_compare_view,
            "weakest_buckets": weakest_buckets,
            "compare_views": dict(report.get("compare_views") or {}),
            "action_items": action_items,
            "summary_lines": summary_lines,
        }

    def _build_exit_monitor_review_summary() -> dict[str, Any]:
        if not monitor_state_service:
            return {
                "available": False,
                "signal_count": 0,
                "snapshot": {},
                "trend_summary": {},
                "action_items": [],
                "summary_lines": ["当前尚无可用于盘后 review 的 exit monitor 快照。"],
            }
        state = monitor_state_service.get_state()
        latest_payload = dict(state.get("latest_exit_snapshot") or {})
        snapshot = dict(latest_payload.get("snapshot") or {})
        trend_summary = dict(state.get("exit_snapshot_trend_summary") or {})
        if not snapshot:
            return {
                "available": False,
                "signal_count": 0,
                "snapshot": {},
                "trend_summary": trend_summary,
                "action_items": [],
                "summary_lines": ["当前尚无可用于盘后 review 的 exit monitor 快照。"],
            }
        signal_count = int(snapshot.get("signal_count", 0) or 0)
        action_items = []
        if signal_count > 0:
            action_items.append(
                {
                    "source": "exit_monitor",
                    "priority": "medium",
                    "title": "exit monitor 存在待复核信号",
                    "reason": f"最近一次监控命中 {signal_count} 条退出信号，建议复核原因分布与标的明细。",
                    "operation_targets": [
                        {
                            "label": "查看 monitor state",
                            "method": "GET",
                            "path": "/monitor/state",
                        }
                    ],
                }
            )
        return {
            "available": True,
            "generated_at": latest_payload.get("generated_at"),
            "trigger": latest_payload.get("trigger"),
            "signal_count": signal_count,
            "snapshot": snapshot,
            "trend_summary": trend_summary,
            "action_items": action_items,
            "summary_lines": list(snapshot.get("summary_lines") or ["当前无退出监控信号。"]),
        }

    def _build_execution_bridge_health_review_summary() -> dict[str, Any]:
        if not monitor_state_service:
            return {
                "available": False,
                "overall_status": "unknown",
                "attention_count": 0,
                "health": {},
                "trend_summary": {},
                "action_items": [],
                "summary_lines": ["当前尚无可用于盘后 review 的 execution bridge health 快照。"],
            }
        latest_payload = monitor_state_service.get_latest_execution_bridge_health()
        health = dict(latest_payload.get("health") or {})
        trend_summary = monitor_state_service.get_execution_bridge_health_trend_summary()
        has_snapshot = bool(latest_payload.get("health_id") or latest_payload.get("generated_at"))
        if not has_snapshot and not trend_summary.get("available"):
            return {
                "available": False,
                "overall_status": "unknown",
                "attention_count": 0,
                "health": {},
                "trend_summary": trend_summary,
                "action_items": [],
                "summary_lines": ["当前尚无可用于盘后 review 的 execution bridge health 快照。"],
            }
        overall_status = str(health.get("overall_status") or "unknown")
        attention_components = [str(item) for item in list(health.get("attention_components") or []) if str(item)]
        latest_reported_at = str(health.get("reported_at") or "")
        latest_source_id = str(health.get("source_id") or "")
        latest_deployment_role = str(health.get("deployment_role") or "")
        latest_bridge_path = str(health.get("bridge_path") or "")
        action_items = []
        if overall_status in {"degraded", "down"} or attention_components:
            action_items.append(
                {
                    "source": "execution_bridge_health",
                    "priority": "high" if overall_status == "down" else "medium",
                    "title": "Windows 执行桥健康状态需要复核",
                    "reason": (
                        f"当前 overall_status={overall_status}，"
                        f"关注组件 {', '.join(attention_components) if attention_components else '无'}。"
                        + (f" 来源 {latest_source_id}。" if latest_source_id else "")
                    ),
                    "operation_targets": [
                        {
                            "label": "查看 monitor state",
                            "method": "GET",
                            "path": "/monitor/state",
                        }
                    ],
                }
            )
        summary_lines = list(health.get("summary_lines") or [])
        if not summary_lines:
            summary_lines = [f"execution bridge 最新状态 {overall_status}。"]
        if latest_source_id or latest_bridge_path or latest_deployment_role:
            summary_lines.append(
                "来源: "
                + (latest_source_id or "unknown")
                + (f" | 角色={latest_deployment_role}" if latest_deployment_role else "")
                + (f" | 桥路={latest_bridge_path}" if latest_bridge_path else "")
                + (f" | reported_at={latest_reported_at}" if latest_reported_at else "")
            )
        return {
            "available": True,
            "generated_at": latest_payload.get("generated_at"),
            "trigger": latest_payload.get("trigger"),
            "overall_status": overall_status,
            "attention_count": len(attention_components),
            "attention_components": attention_components,
            "reported_at": latest_reported_at,
            "source_id": latest_source_id,
            "deployment_role": latest_deployment_role,
            "bridge_path": latest_bridge_path,
            "health": health,
            "trend_summary": trend_summary,
            "action_items": action_items,
            "summary_lines": summary_lines,
        }

    def _build_offline_backtest_metrics_review_summary() -> dict[str, Any]:
        payload = _load_latest_offline_backtest_metrics()
        if not payload:
            return {
                "available": False,
                "overview": {},
                "top_playbook": {},
                "weak_regime": {},
                "action_items": [],
                "summary_lines": ["当前尚无可用于盘后 review 的 offline backtest metrics。"],
            }
        overview = dict(payload.get("overview") or {})
        by_playbook = list(payload.get("by_playbook_metrics") or [])
        by_regime = list(payload.get("by_regime_metrics") or [])
        by_exit_reason = list(payload.get("by_exit_reason_metrics") or [])
        top_playbook = by_playbook[0] if by_playbook else {}
        weak_regime = min(by_regime, key=lambda item: item.get("avg_return_pct", 0.0)) if by_regime else {}
        action_items = []
        if weak_regime and float(weak_regime.get("avg_return_pct", 0.0) or 0.0) < 0:
            action_items.append(
                {
                    "source": "offline_backtest_metrics",
                    "priority": "medium",
                    "title": "离线回测 regime 指标出现负收益桶",
                    "reason": (
                        f"{weak_regime.get('key')} 平均收益 {float(weak_regime.get('avg_return_pct', 0.0) or 0.0):.2%}，"
                        "建议复核市场状态适配。"
                    ),
                    "operation_targets": [
                        {
                            "label": "查看 offline backtest metrics",
                            "method": "GET",
                            "path": "/system/reports/offline-backtest-metrics",
                        }
                    ],
                }
            )
        summary_lines = [
            f"离线回测 metrics 共 {int(overview.get('total_trades', 0) or 0)} 笔，胜率 {float(overview.get('win_rate', 0.0) or 0.0):.1%}。"
        ]
        if top_playbook:
            summary_lines.append(
                f"战法样本最多的是 {top_playbook.get('key')}，共 {top_playbook.get('trade_count', 0)} 笔。"
            )
        if weak_regime:
            summary_lines.append(
                f"regime 最弱桶 {weak_regime.get('key')}，平均收益 {float(weak_regime.get('avg_return_pct', 0.0) or 0.0):.2%}。"
            )
        return {
            "available": True,
            "overview": overview,
            "top_playbook": top_playbook,
            "weak_regime": weak_regime,
            "by_playbook_metrics": by_playbook,
            "by_regime_metrics": by_regime,
            "by_exit_reason_metrics": by_exit_reason,
            "action_items": action_items,
            "summary_lines": summary_lines,
        }

    def _build_postclose_review_board(
        *,
        trade_date: str | None = None,
        score_date: str | None = None,
        tail_market_source: str = "latest",
        tail_market_limit: int = 20,
        due_within_days: int = 1,
        inspection_limit: int = 50,
        persist_snapshot: bool = True,
    ) -> dict[str, Any]:
        governance = _build_governance_review_summary(
            trade_date=trade_date,
            score_date=score_date,
            due_within_days=due_within_days,
            limit=inspection_limit,
        )
        tail_market = _build_tail_market_review_section(
            source=tail_market_source,
            limit=tail_market_limit,
        )
        discussion = _build_discussion_review_summary(trade_date=trade_date)
        offline_backtest = _build_offline_backtest_review_summary()
        exit_monitor = _build_exit_monitor_review_summary()
        execution_bridge_health = _build_execution_bridge_health_review_summary()
        offline_backtest_metrics = _build_offline_backtest_metrics_review_summary()
        resolved_trade_date = (
            trade_date
            or discussion.get("trade_date")
            or (
                (
                    ((tail_market.get("review") or {}).get("items") or [{}])[0].get("trade_date")
                    if (tail_market.get("count", 0) or 0) > 0
                    else None
                )
            )
        )
        control_plane_gateway = _build_control_plane_gateway_summary(
            trade_date=resolved_trade_date,
            latest_tail_market_payload=(meeting_state_store.get("latest_tail_market_scan") if meeting_state_store else None),
        )
        action_items = [
            *(list(governance.get("action_items") or [])[:5]),
            *(list(tail_market.get("action_items") or [])[:5]),
            *(list(discussion.get("action_items") or [])[:3]),
            *(list(offline_backtest.get("action_items") or [])[:3]),
            *(list(exit_monitor.get("action_items") or [])[:3]),
            *(list(execution_bridge_health.get("action_items") or [])[:3]),
            *(list(offline_backtest_metrics.get("action_items") or [])[:3]),
        ]
        summary_lines = [
            "盘后 review board 已汇总参数治理、tail-market、discussion、offline backtest、exit monitor、execution bridge health 与 metrics 七类重点事项。",
            (
                f"治理高优先级 {governance.get('high_priority_action_item_count', 0)} 项；"
                f"tail-market 命中 {tail_market.get('count', 0)} 项；"
                f"discussion 状态 {discussion.get('status') or 'unknown'}；"
                f"离线回测样本 {offline_backtest.get('trade_count', 0)} 笔；"
                f"exit monitor 信号 {exit_monitor.get('signal_count', 0)} 条；"
                f"执行桥状态 {execution_bridge_health.get('overall_status') or 'unknown'}；"
                f"metrics 样本 {int((offline_backtest_metrics.get('overview') or {}).get('total_trades', 0) or 0)} 笔；"
                f"主控 pending intent {control_plane_gateway.get('pending_intent_count', 0)} 条。"
            ),
        ]
        if governance.get("summary_lines"):
            summary_lines.append("治理: " + str((governance.get("summary_lines") or [""])[0]))
        if tail_market.get("summary_lines"):
            summary_lines.append("尾盘: " + str((tail_market.get("summary_lines") or [""])[0]))
        if discussion.get("summary_lines"):
            summary_lines.append("讨论: " + str((discussion.get("summary_lines") or [""])[0]))
        if offline_backtest.get("summary_lines"):
            summary_lines.append("回测: " + str((offline_backtest.get("summary_lines") or [""])[0]))
        if exit_monitor.get("summary_lines"):
            summary_lines.append("监控: " + str((exit_monitor.get("summary_lines") or [""])[0]))
        if (exit_monitor.get("trend_summary") or {}).get("summary_lines"):
            summary_lines.append("监控趋势: " + str(((exit_monitor.get("trend_summary") or {}).get("summary_lines") or [""])[0]))
        if execution_bridge_health.get("summary_lines"):
            summary_lines.append("执行桥: " + str((execution_bridge_health.get("summary_lines") or [""])[0]))
        if (execution_bridge_health.get("trend_summary") or {}).get("summary_lines"):
            summary_lines.append(
                "执行桥趋势: " + str(((execution_bridge_health.get("trend_summary") or {}).get("summary_lines") or [""])[0])
            )
        if offline_backtest_metrics.get("summary_lines"):
            summary_lines.append("指标: " + str((offline_backtest_metrics.get("summary_lines") or [""])[0]))
        if control_plane_gateway.get("summary_lines"):
            summary_lines.append("主控执行: " + str((control_plane_gateway.get("summary_lines") or [""])[0]))
            if len(list(control_plane_gateway.get("summary_lines") or [])) > 1:
                summary_lines.append("主控执行队列: " + str((control_plane_gateway.get("summary_lines") or ["", ""])[1]))
        payload = {
            "ok": True,
            "available": bool(
                governance.get("available")
                or tail_market.get("available")
                or discussion.get("available")
                or offline_backtest.get("available")
                or exit_monitor.get("available")
                or execution_bridge_health.get("available")
                or offline_backtest_metrics.get("available")
                or control_plane_gateway.get("available")
            ),
            "trade_date": resolved_trade_date,
            "generated_at": datetime.now().isoformat(),
            "pending_intent_count": int(control_plane_gateway.get("pending_intent_count", 0) or 0),
            "queued_for_gateway_count": int(control_plane_gateway.get("queued_for_gateway_count", 0) or 0),
            "latest_gateway_source": str(control_plane_gateway.get("latest_gateway_source") or ""),
            "latest_receipt_summary": dict(control_plane_gateway.get("latest_receipt_summary") or {}),
            "discussion_dispatch_queued_for_gateway_count": int(
                ((control_plane_gateway.get("discussion_dispatch") or {}).get("queued_for_gateway_count", 0) or 0)
            ),
            "tail_market_queued_for_gateway_count": int(
                ((control_plane_gateway.get("tail_market") or {}).get("queued_for_gateway_count", 0) or 0)
            ),
            "counts": {
                "governance_action_item_count": governance.get("action_item_count", 0),
                "governance_high_priority_action_item_count": governance.get("high_priority_action_item_count", 0),
                "tail_market_count": tail_market.get("count", 0),
                "discussion_blocked_count": discussion.get("blocked_count", 0),
                "discussion_selected_count": discussion.get("selected_count", 0),
                "offline_backtest_trade_count": offline_backtest.get("trade_count", 0),
                "exit_monitor_signal_count": exit_monitor.get("signal_count", 0),
                "execution_bridge_attention_count": execution_bridge_health.get("attention_count", 0),
                "offline_backtest_metrics_trade_count": int((offline_backtest_metrics.get("overview") or {}).get("total_trades", 0) or 0),
                "pending_intent_count": int(control_plane_gateway.get("pending_intent_count", 0) or 0),
                "queued_for_gateway_count": int(control_plane_gateway.get("queued_for_gateway_count", 0) or 0),
                "discussion_dispatch_queued_for_gateway_count": int(
                    ((control_plane_gateway.get("discussion_dispatch") or {}).get("queued_for_gateway_count", 0) or 0)
                ),
                "tail_market_queued_for_gateway_count": int(
                    ((control_plane_gateway.get("tail_market") or {}).get("queued_for_gateway_count", 0) or 0)
                ),
                "total_action_item_count": len(action_items),
            },
            "sections": {
                "governance": governance,
                "tail_market": tail_market,
                "discussion": discussion,
                "offline_backtest": offline_backtest,
                "exit_monitor": exit_monitor,
                "execution_bridge_health": execution_bridge_health,
                "offline_backtest_metrics": offline_backtest_metrics,
                "control_plane_gateway": control_plane_gateway,
            },
            "action_items": action_items,
            "summary_lines": summary_lines,
        }
        if persist_snapshot and meeting_state_store:
            meeting_state_store.set("latest_review_board", _sanitize_json_compatible(payload))
        return payload

    @router.get("/tail-market/review")
    async def review_tail_market_scans(
        source: str = "history",
        limit: int = 20,
        symbol: str | None = None,
        exit_reason: str | None = None,
        review_tag: str | None = None,
    ):
        if not meeting_state_store:
            return {"ok": False, "error": "meeting state store not initialized"}
        normalized_source = str(source or "history").lower()
        if normalized_source not in {"latest", "history"}:
            return {"ok": False, "error": f"unsupported source: {source}"}
        if normalized_source == "latest":
            latest = meeting_state_store.get("latest_tail_market_scan")
            payloads = [latest] if latest else []
        else:
            history = meeting_state_store.get("tail_market_history", [])
            normalized_limit = max(min(int(limit or 20), 100), 1)
            payloads = list(reversed(history[-normalized_limit:]))
        return _build_tail_market_review_summary(
            scan_payloads=[item for item in payloads if item],
            symbol=symbol,
            exit_reason=exit_reason,
            review_tag=review_tag,
        )

    @router.get("/reports/review-board")
    async def review_board(
        trade_date: str | None = None,
        score_date: str | None = None,
        tail_market_source: str = "latest",
        tail_market_limit: int = 20,
        due_within_days: int = 1,
        inspection_limit: int = 50,
    ):
        return _build_postclose_review_board(
            trade_date=trade_date,
            score_date=score_date,
            tail_market_source=tail_market_source,
            tail_market_limit=tail_market_limit,
            due_within_days=due_within_days,
            inspection_limit=inspection_limit,
        )

    @router.get("/discussions/cycles")
    async def list_discussion_cycles():
        if not discussion_cycle_service:
            return {"items": [], "count": 0}
        items = [cycle.model_dump() for cycle in discussion_cycle_service.list_cycles()]
        return {"items": items, "count": len(items)}

    @router.get("/discussions/cycles/{trade_date}")
    async def get_discussion_cycle(trade_date: str):
        if not discussion_cycle_service:
            return {"available": False}
        cycle = discussion_cycle_service.get_cycle(trade_date)
        return cycle.model_dump() if cycle else {"available": False, "trade_date": trade_date}

    @router.post("/discussions/cycles/bootstrap")
    async def bootstrap_discussion_cycle(payload: DiscussionCycleBootstrapInput):
        if not discussion_cycle_service:
            return {"ok": False, "error": "discussion cycle service not initialized"}
        try:
            trade_date = _resolve_trade_date(payload.trade_date)
            cycle = discussion_cycle_service.bootstrap_cycle(trade_date)
            if audit_store:
                audit_store.append(
                    category="discussion",
                    message=f"讨论周期已初始化: {trade_date}",
                    payload={"trade_date": trade_date, "cycle_id": cycle.cycle_id},
                )
            _save_monitor_pool_snapshot(trade_date, cycle, source="discussion_bootstrap")
            _persist_discussion_context(_build_discussion_context_payload(trade_date, cycle_payload=cycle.model_dump()))
            return {"ok": True, "cycle": _serialize_cycle_compact(cycle)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/discussions/cycles/{trade_date}/rounds/{round_number}/start")
    async def start_discussion_round(trade_date: str, round_number: int):
        if not discussion_cycle_service:
            return {"ok": False, "error": "discussion cycle service not initialized"}
        try:
            cycle = discussion_cycle_service.start_round(trade_date, round_number)
            _save_monitor_pool_snapshot(trade_date, cycle, source=f"discussion_round_{round_number}_start")
            if audit_store:
                audit_store.append(
                    category="discussion",
                    message=f"讨论轮次已启动: {trade_date} round {round_number}",
                    payload={"trade_date": trade_date, "round_number": round_number},
                )
            _persist_discussion_context(_build_discussion_context_payload(trade_date, cycle_payload=cycle.model_dump()))
            return {"ok": True, "cycle": _serialize_cycle_compact(cycle)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/discussions/cycles/{trade_date}/refresh")
    async def refresh_discussion_cycle(trade_date: str):
        if not discussion_cycle_service:
            return {"ok": False, "error": "discussion cycle service not initialized"}
        try:
            existing_cycle = discussion_cycle_service.get_cycle(trade_date)
            focus_gate_required = bool(
                existing_cycle
                and existing_cycle.discussion_state in {"round_1_running", "round_1_summarized"}
            )
            cadence_gate = (
                monitor_state_service.mark_poll_if_due("focus", trigger="discussion_refresh")
                if monitor_state_service and focus_gate_required
                else {
                    "triggered": True,
                    "layer": "focus",
                    "trigger": "discussion_refresh",
                    "bypassed": not focus_gate_required,
                }
            )
            if focus_gate_required and not cadence_gate.get("triggered"):
                return {
                    "ok": True,
                    "cycle": (
                        _serialize_cycle_compact(existing_cycle)
                        if existing_cycle
                        else {"available": False, "trade_date": trade_date}
                    ),
                    "refresh_skipped": True,
                    "cadence_gate": cadence_gate,
                }
            cycle = discussion_cycle_service.refresh_cycle(trade_date)
            _save_monitor_pool_snapshot(trade_date, cycle, source="discussion_refresh")
            _persist_discussion_context(_build_discussion_context_payload(trade_date, cycle_payload=cycle.model_dump()))
            return {
                "ok": True,
                "cycle": _serialize_cycle_compact(cycle),
                "refresh_skipped": False,
                "cadence_gate": cadence_gate,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/discussions/cycles/{trade_date}/finalize")
    async def finalize_discussion_cycle(trade_date: str):
        if not discussion_cycle_service:
            return {"ok": False, "error": "discussion cycle service not initialized"}
        try:
            cycle_for_finalize = discussion_cycle_service.refresh_cycle(trade_date)
            if not discussion_cycle_service.can_finalize(cycle_for_finalize) and cycle_for_finalize.discussion_state not in {
                "final_selection_ready",
                "final_selection_blocked",
            }:
                return {
                    "ok": True,
                    "cycle": _serialize_cycle_compact(cycle_for_finalize),
                    "notification": {"dispatched": False, "reason": "discussion_not_ready"},
                    "finalize_skipped": True,
                    "cadence_gate": {
                        "triggered": False,
                        "layer": "execution",
                        "trigger": "discussion_finalize",
                        "reason": "discussion_not_ready",
                    },
                }
            execution_gate_required = not (
                cycle_for_finalize
                and cycle_for_finalize.discussion_state in {"final_selection_ready", "final_selection_blocked"}
            )
            cadence_gate = (
                monitor_state_service.mark_poll_if_due("execution", trigger="discussion_finalize")
                if monitor_state_service and execution_gate_required
                else {"triggered": True, "layer": "execution", "trigger": "discussion_finalize"}
            )
            if execution_gate_required and not cadence_gate.get("triggered"):
                return {
                    "ok": True,
                    "cycle": (
                        _serialize_cycle_compact(cycle_for_finalize)
                        if cycle_for_finalize
                        else {"available": False, "trade_date": trade_date}
                    ),
                    "notification": {"dispatched": False, "reason": "execution_poll_skip"},
                    "finalize_skipped": True,
                    "cadence_gate": cadence_gate,
                }
            cycle = discussion_cycle_service.finalize_cycle(trade_date)
            _save_monitor_pool_snapshot(trade_date, cycle, source="discussion_finalize")
            execution_precheck = _build_execution_precheck(
                trade_date,
                account_id=_resolve_account_id(),
            )
            _persist_execution_precheck(execution_precheck)
            execution_alert_result = (
                live_execution_alert_notifier.dispatch_precheck(execution_precheck)
                if settings.run_mode == "live" and live_execution_alert_notifier
                else None
            )
            execution_intents = _build_execution_intents(
                trade_date,
                account_id=execution_precheck["account_id"],
            )
            _persist_execution_intents(execution_intents)
            client_brief = _build_client_brief(trade_date)
            _persist_discussion_context(
                _build_discussion_context_payload(
                    trade_date,
                    cycle_payload=cycle.model_dump(),
                    client_brief_payload=client_brief,
                )
            )
            dispatch_result = (
                discussion_finalize_notifier.dispatch(trade_date)
                if discussion_finalize_notifier
                else None
            )
            if audit_store:
                audit_store.append(
                    category="discussion",
                    message=f"讨论周期已终审: {trade_date}",
                    payload={
                        "trade_date": trade_date,
                        "discussion_state": cycle.discussion_state,
                        "execution_pool_case_count": len(cycle.execution_pool_case_ids),
                        "notify_dispatched": (dispatch_result.dispatched if dispatch_result else False),
                        "notify_reason": (dispatch_result.reason if dispatch_result else "not_configured"),
                        "execution_alert_dispatched": (execution_alert_result.dispatched if execution_alert_result else False),
                        "execution_alert_reason": (execution_alert_result.reason if execution_alert_result else "disabled"),
                    },
                )
            return _sanitize_json_compatible({
                "ok": True,
                "cycle": _serialize_cycle_compact(cycle),
                "finalize_skipped": False,
                "cadence_gate": cadence_gate,
                "execution_precheck": execution_precheck,
                "execution_intents": execution_intents,
                "client_brief": client_brief,
                "finalize_packet": (
                    (_sanitize_json_compatible(meeting_state_store.get(f"discussion_context:{trade_date}")) or {}).get("finalize_packet")
                    if meeting_state_store
                    else None
                ),
                "execution_alert_notification": (
                    {
                        "dispatched": execution_alert_result.dispatched,
                        "reason": execution_alert_result.reason,
                        "level": execution_alert_result.payload.get("level"),
                        "title": execution_alert_result.payload.get("title"),
                    }
                    if execution_alert_result
                    else {"dispatched": False, "reason": "disabled"}
                ),
                "notification": (
                    {
                        "dispatched": dispatch_result.dispatched,
                        "reason": dispatch_result.reason,
                        "status": dispatch_result.payload.get("status"),
                    }
                    if dispatch_result
                    else {"dispatched": False, "reason": "not_configured"}
                ),
            })
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.get("/agent-scores")
    async def list_agent_scores(score_date: str | None = None):
        if not agent_score_service:
            return {"items": [], "count": 0}
        items = [state.model_dump() for state in agent_score_service.list_scores(score_date=score_date)]
        return {"items": items, "count": len(items), "score_date": score_date or datetime.now().date().isoformat()}

    @router.post("/agent-scores/settlements")
    async def settle_agent_scores(payload: ScoreSettlementInput):
        if not agent_score_service or not candidate_case_service:
            return {"ok": False, "error": "score services not initialized"}
        cases = candidate_case_service.list_cases(trade_date=payload.trade_date, limit=500)
        case_map = {item.symbol: item for item in cases}
        runtime_context = serving_store.get_latest_runtime_context() or {}
        dossier_pack = serving_store.get_latest_dossier_pack() or {}
        market_context = serving_store.get_latest_market_context() or {}
        playbook_map = {
            item.get("symbol"): item
            for item in (runtime_context.get("playbook_contexts") or dossier_pack.get("playbook_contexts") or [])
            if item.get("symbol")
        }
        dossier_map = {
            item.get("symbol"): item
            for item in dossier_pack.get("items", [])
            if item.get("symbol")
        }
        outcome_map = {
            item.symbol: SettlementSymbolOutcome(
                symbol=item.symbol,
                next_day_close_pct=item.next_day_close_pct,
                note=item.note,
            )
            for item in payload.outcomes
        }
        results = settlement_service.settle(cases, outcome_map)
        persisted = []
        for item in results:
            state = agent_score_service.record_settlement(
                agent_id=item.agent_id,
                score_date=payload.score_date,
                result_score_delta=item.result_score_delta,
                cases_evaluated=item.cases_evaluated,
            )
            persisted.append(state.model_dump())
        attribution_items = []
        runtime_regime = ((runtime_context.get("market_profile") or {}).get("regime") or market_context.get("regime") or "unknown")
        for outcome in payload.outcomes:
            case = case_map.get(outcome.symbol)
            dossier_item = dossier_map.get(outcome.symbol) or {}
            playbook_context = playbook_map.get(outcome.symbol) or {}
            attribution_items.append(
                TradeAttributionRecord(
                    trade_date=payload.trade_date,
                    score_date=payload.score_date,
                    symbol=outcome.symbol,
                    name=(case.name if case else dossier_item.get("name", "")),
                    case_id=(case.case_id if case else None),
                    playbook=(
                        outcome.playbook
                        or playbook_context.get("playbook")
                        or dossier_item.get("assigned_playbook")
                        or "unassigned"
                    ),
                    regime=outcome.regime or runtime_regime,
                    exit_reason=outcome.exit_reason or "next_day_close",
                    next_day_close_pct=outcome.next_day_close_pct,
                    note=outcome.note,
                    selection_score=(case.runtime_snapshot.selection_score if case else dossier_item.get("selection_score")),
                    rank=(case.runtime_snapshot.rank if case else dossier_item.get("rank")),
                    final_status=(case.final_status if case else dossier_item.get("final_status", "")),
                    risk_gate=(case.risk_gate if case else dossier_item.get("risk_gate", "")),
                    audit_gate=(case.audit_gate if case else dossier_item.get("audit_gate", "")),
                    holding_days=outcome.holding_days,
                    recorded_at=datetime.now().isoformat(),
                )
            )
        attribution_report = trade_attribution_service.record_outcomes(
            trade_date=payload.trade_date,
            score_date=payload.score_date,
            items=attribution_items,
        )
        if audit_store:
            audit_store.append(
                category="learning",
                message="agent 学分结算完成",
                payload={
                    "trade_date": payload.trade_date,
                    "score_date": payload.score_date,
                    "agent_count": len(persisted),
                    "attribution_trade_count": attribution_report.trade_count,
                },
            )
        return {
            "ok": True,
            "items": persisted,
            "count": len(persisted),
            "trade_date": payload.trade_date,
            "score_date": payload.score_date,
            "attribution": attribution_report.model_dump(),
        }

    @router.get("/learning/attribution")
    async def learning_attribution(
        trade_date: str | None = None,
        score_date: str | None = None,
        symbol: str | None = None,
        reason: str | None = None,
        review_tag: str | None = None,
        exit_context_key: str | None = None,
        exit_context_value: str | None = None,
    ):
        report = trade_attribution_service.build_report(
            trade_date=trade_date,
            score_date=score_date,
            symbol=symbol,
            reason=reason,
            review_tag=review_tag,
            exit_context_key=exit_context_key,
            exit_context_value=exit_context_value,
        )
        return report.model_dump()

    @router.get("/learning/trade-review")
    async def learning_trade_review(
        trade_date: str | None = None,
        score_date: str | None = None,
        symbol: str | None = None,
        reason: str | None = None,
        review_tag: str | None = None,
        exit_context_key: str | None = None,
        exit_context_value: str | None = None,
    ):
        report = trade_attribution_service.build_report(
            trade_date=trade_date,
            score_date=score_date,
            symbol=symbol,
            reason=reason,
            review_tag=review_tag,
            exit_context_key=exit_context_key,
            exit_context_value=exit_context_value,
        )
        return {
            "available": report.available,
            "trade_date": report.trade_date,
            "score_date": report.score_date,
            "generated_at": report.generated_at,
            "filters": report.filters,
            "review_summary": report.review_summary,
            "review_tag_summary": report.review_tag_summary,
            "parameter_hints": report.parameter_hints,
            "summary_lines": report.summary_lines,
            "by_symbol": report.by_symbol,
            "by_reason": report.by_reason,
            "by_playbook": report.by_playbook,
            "by_regime": report.by_regime,
            "by_exit_reason": report.by_exit_reason,
        }

    @router.get("/reports/offline-backtest-attribution")
    async def offline_backtest_attribution_report():
        payload = _load_latest_offline_backtest_attribution()
        return {
            "available": bool(payload.get("available")),
            "attribution_scope": payload.get("attribution_scope", "offline_backtest"),
            "semantics_note": payload.get("semantics_note", OFFLINE_BACKTEST_ATTRIBUTION_NOTE),
            "overview": payload.get("overview", {}),
            "weakest_buckets": payload.get("weakest_buckets", []),
            "compare_views": payload.get("compare_views", {}),
            "selected_weakest_bucket": payload.get("selected_weakest_bucket", {}),
            "selected_compare_view": payload.get("selected_compare_view", {}),
            "summary_lines": payload.get("summary_lines", []),
        }

    @router.get("/reports/offline-backtest-metrics")
    async def offline_backtest_metrics_report():
        payload = _load_latest_offline_backtest_metrics()
        export_payload = dict(payload.get("export_payload") or {})
        overview = dict(payload.get("overview") or export_payload.get("overview") or {})
        return {
            "available": bool(payload),
            "metrics_scope": payload.get("metrics_scope", "offline_backtest_metrics"),
            "semantics_note": payload.get(
                "semantics_note",
                "这是 offline_backtest metrics，用于离线回测绩效拆分，不代表线上真实成交后的事实归因。",
            ),
            "overview": overview,
            "by_playbook_metrics": payload.get("by_playbook_metrics", export_payload.get("by_playbook", [])),
            "by_regime_metrics": payload.get("by_regime_metrics", export_payload.get("by_regime", [])),
            "by_exit_reason_metrics": payload.get("by_exit_reason_metrics", export_payload.get("by_exit_reason", [])),
            "win_rate_by_playbook": payload.get("win_rate_by_playbook", []),
            "avg_return_by_regime": payload.get("avg_return_by_regime", []),
            "exit_reason_distribution": payload.get("exit_reason_distribution", []),
            "calmar_by_playbook": payload.get("calmar_by_playbook", []),
            "export_payload": export_payload,
        }

    @router.get("/reports/serving-latest-index")
    async def serving_latest_index():
        return _build_serving_latest_index()

    @router.get("/reports/offline-self-improvement")
    async def offline_self_improvement_report():
        payload = _load_latest_offline_self_improvement()
        latest_descriptor = dict(payload.get("latest_descriptor") or {})
        return {
            "available": bool(payload.get("available")),
            "packet_scope": payload.get("packet_scope", "offline_self_improvement_export"),
            "semantics_note": payload.get("semantics_note", OFFLINE_SELF_IMPROVEMENT_NOTE),
            "live_execution_allowed": bool(payload.get("live_execution_allowed", False)),
            "serving_ready": bool(payload.get("serving_ready", False)),
            "artifact_name": payload.get("artifact_name", "latest_offline_self_improvement_export.json"),
            "generated_at": payload.get("generated_at"),
            "consumers": payload.get("consumers", []),
            "filters": payload.get("filters", {}),
            "input_source": payload.get("input_source", "trade_list"),
            "summary_lines": payload.get("summary_lines", []),
            "archive_ready_manifest": payload.get("archive_ready_manifest", {}),
            "latest_descriptor": latest_descriptor,
            "archive_ref": latest_descriptor.get("archive_ref", {}),
            "descriptor_contract_sample": payload.get("descriptor_contract_sample", {}),
            "proposal_packet": payload.get("proposal_packet", {}),
            "attribution": payload.get("attribution", {}),
            "metrics": payload.get("metrics", {}),
        }

    @router.get("/reports/offline-self-improvement-descriptor")
    async def offline_self_improvement_descriptor_report():
        payload = _load_latest_offline_self_improvement()
        latest_descriptor = dict(payload.get("latest_descriptor") or {})
        return {
            "available": bool(latest_descriptor),
            "descriptor": latest_descriptor,
            "archive_ref": latest_descriptor.get("archive_ref", {}),
            "descriptor_contract_sample": payload.get("descriptor_contract_sample", {}),
        }

    @router.post("/reports/offline-self-improvement/persist")
    async def persist_offline_self_improvement_report(payload: dict[str, Any]):
        normalized = _normalize_offline_self_improvement_payload(payload)
        if not normalized:
            return {"ok": False, "error": "offline self-improvement payload is empty"}
        archive_manifest = payload.get("archive_ready_manifest")
        if isinstance(archive_manifest, dict):
            normalized["archive_ready_manifest"] = _sanitize_json_compatible(archive_manifest)
        elif not isinstance(normalized.get("archive_ready_manifest"), dict):
            normalized["archive_ready_manifest"] = offline_backtest_runner.build_archive_ready_manifest(normalized)
        latest_descriptor = payload.get("latest_descriptor")
        if isinstance(latest_descriptor, dict):
            normalized["latest_descriptor"] = _sanitize_json_compatible(latest_descriptor)
        normalized = _ensure_offline_self_improvement_descriptors(normalized)
        persisted = archive_store.persist_offline_self_improvement_export(normalized)
        if meeting_state_store:
            meeting_state_store.set("latest_offline_self_improvement_export", persisted)
        if audit_store:
            audit_store.append(
                category="backtest",
                message="offline self-improvement export 已归档",
                payload={
                    "packet_scope": persisted.get("packet_scope"),
                    "generated_at": persisted.get("generated_at"),
                    "artifact_name": persisted.get("artifact_name"),
                    "relative_archive_path": ((persisted.get("archive_ready_manifest") or {}).get("relative_archive_path") or ""),
                },
            )
        return {"ok": True, "payload": persisted}

    @router.get("/reports/openclaw-replay-packet")
    async def openclaw_replay_packet_report():
        payload = _load_latest_openclaw_packet("openclaw_replay_packet")
        latest_descriptor = dict(payload.get("latest_descriptor") or {})
        return {
            "available": bool(payload),
            "packet_type": payload.get("packet_type", "openclaw_replay_packet"),
            "packet_id": payload.get("packet_id", ""),
            "research_track": payload.get("research_track", ""),
            "offline_only": bool(payload.get("offline_only", True)),
            "live_trigger": bool(payload.get("live_trigger", False)),
            "generated_at": payload.get("generated_at"),
            "archive_manifest": payload.get("archive_manifest", {}),
            "latest_descriptor": latest_descriptor,
            "contract_sample": payload.get("contract_sample", {}),
            "source_refs": payload.get("source_refs", []),
            "archive_tags": payload.get("archive_tags", []),
            "summary_snapshot": payload.get("summary_snapshot", {}),
            "writeback_preview": payload.get("writeback_preview", {}),
            "replay_packet": payload.get("replay_packet", {}),
        }

    @router.get("/reports/openclaw-proposal-packet")
    async def openclaw_proposal_packet_report():
        payload = _load_latest_openclaw_packet("openclaw_proposal_packet")
        latest_descriptor = dict(payload.get("latest_descriptor") or {})
        return {
            "available": bool(payload),
            "packet_type": payload.get("packet_type", "openclaw_proposal_packet"),
            "packet_id": payload.get("packet_id", ""),
            "research_track": payload.get("research_track", ""),
            "offline_only": bool(payload.get("offline_only", True)),
            "live_trigger": bool(payload.get("live_trigger", False)),
            "generated_at": payload.get("generated_at"),
            "archive_manifest": payload.get("archive_manifest", {}),
            "latest_descriptor": latest_descriptor,
            "contract_sample": payload.get("contract_sample", {}),
            "source_refs": payload.get("source_refs", []),
            "archive_tags": payload.get("archive_tags", []),
            "summary_snapshot": payload.get("summary_snapshot", {}),
            "writeback_preview": payload.get("writeback_preview", {}),
            "proposal_packet": payload.get("proposal_packet", {}),
        }

    @router.post("/learning/parameter-hints/proposals")
    async def create_parameter_hint_proposals(payload: ParameterHintProposalInput):
        if not parameter_service:
            return {"ok": False, "error": "parameter service not initialized"}
        report = trade_attribution_service.build_report(
            trade_date=payload.trade_date,
            score_date=payload.score_date,
            symbol=payload.symbol,
            reason=payload.reason,
            review_tag=payload.review_tag,
            exit_context_key=payload.exit_context_key,
            exit_context_value=payload.exit_context_value,
        )
        hint_items = list(report.parameter_hints or [])
        if not hint_items:
            return {
                "ok": True,
                "applied": False,
                "matched_count": 0,
                "filters": report.filters,
                "items": [],
                "proposal_events": [],
                "summary_lines": ["当前筛选条件下没有可生成的参数建议。"],
            }

        param_rows = {item["param_key"]: item for item in parameter_service.list_params()}
        preview_items: list[dict[str, Any]] = []
        proposal_events: list[dict[str, Any]] = []
        effective_event_count = 0
        pending_review_event_count = 0
        for hint in hint_items:
            param_key = str(hint.get("param_key") or "")
            param_row = param_rows.get(param_key)
            if not param_row:
                continue
            proposed_value = _infer_parameter_hint_value(param_row, str(hint.get("direction") or "decrease"))
            approval_policy = _build_parameter_hint_approval_policy(param_row, hint)
            preview_items.append(
                {
                    "param_key": param_key,
                    "direction": hint.get("direction"),
                    "sample_count": int(hint.get("sample_count", 0) or 0),
                    "reason": hint.get("reason", ""),
                    "scope": param_row.get("scope"),
                    "value_type": param_row.get("value_type"),
                    "current_value": param_row.get("current_value"),
                    "proposed_value": proposed_value,
                    "allowed_range": param_row.get("allowed_range"),
                    "approval_policy": approval_policy,
                    "rollback_baseline": _build_parameter_hint_rollback_baseline(param_row, proposed_value, hint),
                }
            )
            if not payload.apply:
                continue
            proposal_status = payload.status
            proposal_effective_period = payload.effective_period
            proposal_approved_by = payload.approved_by
            if payload.respect_approval_policy:
                proposal_status = str(approval_policy.get("recommended_status") or proposal_status)
                proposal_effective_period = (
                    str(approval_policy.get("recommended_effective_period") or proposal_effective_period)
                    if approval_policy.get("recommended_effective_period") is not None
                    else proposal_effective_period
                )
                if not approval_policy.get("auto_approvable"):
                    proposal_approved_by = None
            event = parameter_service.propose_change(
                ParamProposalInput(
                    param_key=param_key,
                    new_value=proposed_value,
                    effective_period=proposal_effective_period,
                    proposed_by=payload.proposed_by,
                    structured_by=payload.structured_by,
                    approved_by=proposal_approved_by,
                    reason=f"trade_review 参数建议: {hint.get('reason', '')}",
                    status=proposal_status,
                    source_filters=report.filters,
                    approval_policy_snapshot=approval_policy,
                    rollback_baseline=_build_parameter_hint_rollback_baseline(param_row, proposed_value, hint),
                    observation_window=_build_parameter_observation_window_payload(
                        stage="proposal_observation",
                        observation_window_days=payload.observation_window_days,
                        observation_trade_count=payload.observation_trade_count,
                    ),
                )
            )
            proposal_events.append(event.model_dump())
            if event.status in {"approved", "effective"}:
                effective_event_count += 1
            else:
                pending_review_event_count += 1

        applied = payload.apply and effective_event_count > 0
        auto_approvable_count = sum(
            1 for item in preview_items if (item.get("approval_policy") or {}).get("auto_approvable")
        )
        manual_review_count = len(preview_items) - auto_approvable_count
        if audit_store:
            audit_store.append(
                category="governance",
                message="trade review 参数建议已生成",
                payload={
                    "apply": payload.apply,
                    "matched_count": len(preview_items),
                    "proposal_event_count": len(proposal_events),
                    "auto_approvable_count": auto_approvable_count,
                    "manual_review_count": manual_review_count,
                    "effective_event_count": effective_event_count,
                    "pending_review_event_count": pending_review_event_count,
                    "respect_approval_policy": payload.respect_approval_policy,
                    "filters": report.filters,
                },
            )
        summary_lines = [
            f"基于 {len(preview_items)} 条参数建议生成 {'正式提案' if payload.apply else '预览'}。",
            f"审批基线: auto={auto_approvable_count} manual={manual_review_count}。",
        ]
        if payload.apply:
            summary_lines.append(
                f"提案结果: effective={effective_event_count} pending_review={pending_review_event_count}。"
            )
        if preview_items:
            summary_lines.extend(
                [
                    f"{item['param_key']}: {item['current_value']} -> {item['proposed_value']} ({item['direction']})"
                    for item in preview_items[:5]
                ]
            )
        return {
            "ok": True,
            "applied": applied,
            "matched_count": len(preview_items),
            "approval_baseline": {
                "auto_approvable_count": auto_approvable_count,
                "manual_review_count": manual_review_count,
            },
            "execution_summary": {
                "effective_event_count": effective_event_count,
                "pending_review_event_count": pending_review_event_count,
                "respect_approval_policy": payload.respect_approval_policy,
            },
            "filters": report.filters,
            "items": preview_items,
            "proposal_events": proposal_events,
            "summary_lines": summary_lines,
        }

    @router.get("/learning/parameter-hints/effects")
    async def list_parameter_hint_effects(
        trade_date: str | None = None,
        score_date: str | None = None,
        event_ids: str | None = None,
        status: str = "effective",
    ):
        if not parameter_service:
            return {"ok": False, "error": "parameter service not initialized"}
        selected_ids = {item.strip() for item in str(event_ids or "").split(",") if item.strip()}
        all_events = [item.model_dump() for item in parameter_service.list_proposals()]
        rollback_events_by_parent: dict[str, list[dict[str, Any]]] = {}
        for event in all_events:
            rollback_of_event_id = str(event.get("rollback_of_event_id") or "")
            if rollback_of_event_id:
                rollback_events_by_parent.setdefault(rollback_of_event_id, []).append(event)
        events = [item.model_dump() for item in parameter_service.list_proposals(status=status)]
        filtered_events = [
            item
            for item in events
            if (not selected_ids or item.get("event_id") in selected_ids)
        ]
        items: list[dict[str, Any]] = []
        for event in filtered_events:
            source_filters = dict(event.get("source_filters") or {})
            effective_trade_date = trade_date or source_filters.get("trade_date")
            effective_score_date = score_date or source_filters.get("score_date")
            report = trade_attribution_service.build_report(
                trade_date=effective_trade_date,
                score_date=effective_score_date,
                review_tag=source_filters.get("review_tag"),
                exit_context_key=source_filters.get("exit_context_key"),
                exit_context_value=source_filters.get("exit_context_value"),
            ).model_dump()
            rollback_event = None
            rollback_report = None
            if event.get("event_type") != "param_rollback":
                related_rollbacks = rollback_events_by_parent.get(str(event.get("event_id") or ""), [])
                rollback_event = related_rollbacks[0] if related_rollbacks else None
                if rollback_event:
                    rollback_filters = dict(rollback_event.get("source_filters") or {})
                    rollback_trade_date = trade_date or rollback_filters.get("trade_date")
                    rollback_score_date = score_date or rollback_filters.get("score_date")
                    rollback_report = trade_attribution_service.build_report(
                        trade_date=rollback_trade_date,
                        score_date=rollback_score_date,
                        review_tag=rollback_filters.get("review_tag"),
                        exit_context_key=rollback_filters.get("exit_context_key"),
                        exit_context_value=rollback_filters.get("exit_context_value"),
                    ).model_dump()
            items.append(
                {
                    "event": event,
                    "filters": {
                        **source_filters,
                        **({"trade_date": effective_trade_date} if effective_trade_date else {}),
                        **({"score_date": effective_score_date} if effective_score_date else {}),
                    },
                    "related_rollback_count": len(rollback_events_by_parent.get(str(event.get("event_id") or ""), [])),
                    "latest_rollback_event": rollback_event,
                    "effect_tracking": _build_effect_tracking_summary(
                        event,
                        report,
                        rollback_event=rollback_event,
                        rollback_report=rollback_report,
                    ),
                }
            )
        rollback_recommended_count = sum(
            1 for item in items if (item.get("effect_tracking") or {}).get("rollback_recommended")
        )
        return {
            "ok": True,
            "status": status,
            "count": len(items),
            "rollback_recommended_count": rollback_recommended_count,
            "items": items,
            "summary_lines": [
                f"已评估 {len(items)} 条参数提案，建议回滚 {rollback_recommended_count} 条。"
            ],
        }

    @router.post("/learning/parameter-hints/rollback-preview")
    async def preview_parameter_hint_rollbacks(payload: ParameterRollbackPreviewInput):
        if not parameter_service:
            return {"ok": False, "error": "parameter service not initialized"}
        selected_ids = set(payload.event_ids)
        events = [item.model_dump() for item in parameter_service.list_proposals(status=payload.status)]
        param_rows = {item["param_key"]: item for item in parameter_service.list_params()}
        filtered_events = [
            item
            for item in events
            if (not selected_ids or item.get("event_id") in selected_ids)
        ]
        preview_items: list[dict[str, Any]] = []
        for event in filtered_events:
            source_filters = dict(event.get("source_filters") or {})
            effective_trade_date = payload.trade_date or source_filters.get("trade_date")
            effective_score_date = payload.score_date or source_filters.get("score_date")
            report = trade_attribution_service.build_report(
                trade_date=effective_trade_date,
                score_date=effective_score_date,
                review_tag=source_filters.get("review_tag"),
                exit_context_key=source_filters.get("exit_context_key"),
                exit_context_value=source_filters.get("exit_context_value"),
            ).model_dump()
            effect_tracking = _build_effect_tracking_summary(event, report)
            current_param_row = param_rows.get(str(event.get("param_key") or ""))
            if not effect_tracking.get("rollback_recommended"):
                continue
            preview_items.append(
                {
                    "event_id": event.get("event_id"),
                    "param_key": event.get("param_key"),
                    "current_value": event.get("new_value"),
                    "restore_value": (event.get("rollback_baseline") or {}).get("restore_value"),
                    "effect_tracking": effect_tracking,
                    "current_param_state": current_param_row or {},
                    "rollback_policy": _build_parameter_hint_rollback_policy(
                        event,
                        current_param_row,
                        effect_tracking,
                    ),
                    "approval_policy_snapshot": event.get("approval_policy_snapshot") or {},
                    "observation_window": event.get("observation_window") or {},
                    "source_filters": source_filters,
                }
            )
        return {
            "ok": True,
            "status": payload.status,
            "count": len(preview_items),
            "items": preview_items,
            "summary_lines": [
                f"已生成 {len(preview_items)} 条参数回滚预览。"
            ],
        }

    @router.post("/learning/parameter-hints/rollback-apply")
    async def apply_parameter_hint_rollbacks(payload: ParameterRollbackApplyInput):
        if not parameter_service:
            return {"ok": False, "error": "parameter service not initialized"}
        selected_ids = set(payload.event_ids)
        events = [item.model_dump() for item in parameter_service.list_proposals(status=payload.status)]
        filtered_events = [
            item
            for item in events
            if (not selected_ids or item.get("event_id") in selected_ids)
        ]
        param_rows = {item["param_key"]: item for item in parameter_service.list_params()}
        execution_items: list[dict[str, Any]] = []
        rollback_events: list[dict[str, Any]] = []
        effective_event_count = 0
        pending_review_event_count = 0
        skipped_count = 0
        for event in filtered_events:
            source_filters = dict(event.get("source_filters") or {})
            effective_trade_date = payload.trade_date or source_filters.get("trade_date")
            effective_score_date = payload.score_date or source_filters.get("score_date")
            report = trade_attribution_service.build_report(
                trade_date=effective_trade_date,
                score_date=effective_score_date,
                review_tag=source_filters.get("review_tag"),
                exit_context_key=source_filters.get("exit_context_key"),
                exit_context_value=source_filters.get("exit_context_value"),
            ).model_dump()
            effect_tracking = _build_effect_tracking_summary(event, report)
            current_param_row = param_rows.get(str(event.get("param_key") or ""))
            rollback_policy = _build_parameter_hint_rollback_policy(
                event,
                current_param_row,
                effect_tracking,
            )
            restore_value = (event.get("rollback_baseline") or {}).get("restore_value")
            skip_reasons: list[str] = []
            if restore_value is None:
                skip_reasons.append("missing_restore_value")
            if not effect_tracking.get("rollback_recommended") and not payload.force:
                skip_reasons.append("rollback_not_recommended")
            if not rollback_policy.get("active_event_match") and not payload.force:
                skip_reasons.append("active_event_mismatch")

            item_payload = {
                "event_id": event.get("event_id"),
                "param_key": event.get("param_key"),
                "current_value": event.get("new_value"),
                "restore_value": restore_value,
                "effect_tracking": effect_tracking,
                "current_param_state": current_param_row or {},
                "rollback_policy": rollback_policy,
                "source_filters": source_filters,
                "skip_reasons": skip_reasons,
            }
            execution_items.append(item_payload)
            if skip_reasons:
                skipped_count += 1
                continue

            proposal_status = payload.proposal_status
            proposal_effective_period = payload.effective_period
            proposal_approved_by = payload.approved_by
            if payload.respect_approval_policy:
                proposal_status = str(rollback_policy.get("recommended_status") or proposal_status)
                proposal_effective_period = (
                    str(rollback_policy.get("recommended_effective_period") or proposal_effective_period)
                    if rollback_policy.get("recommended_effective_period") is not None
                    else proposal_effective_period
                )
                if not rollback_policy.get("auto_approvable"):
                    proposal_approved_by = None
            rollback_event = parameter_service.propose_change(
                ParamProposalInput(
                    event_type="param_rollback",
                    rollback_of_event_id=str(event.get("event_id") or ""),
                    param_key=str(event.get("param_key") or ""),
                    new_value=restore_value,
                    effective_period=proposal_effective_period,
                    proposed_by=payload.proposed_by,
                    structured_by=payload.structured_by,
                    approved_by=proposal_approved_by,
                    reason=(
                        f"parameter rollback: {event.get('event_id')} -> {restore_value}; "
                        f"{(effect_tracking.get('rollback_preview') or {}).get('rollback_reason', '')}"
                    ),
                    status=proposal_status,
                    source_filters={
                        **source_filters,
                        "rollback_of_event_id": event.get("event_id"),
                    },
                    approval_policy_snapshot=rollback_policy,
                    rollback_baseline={
                        "restore_value": (current_param_row or {}).get("current_value"),
                        "current_layer": (current_param_row or {}).get("current_layer"),
                        "active_event_id": (current_param_row or {}).get("active_event_id"),
                        "rollback_trigger": "若本次回滚后同类样本仍未改善，则需要人工复核参数定义与策略假设。",
                        "rollback_reason": "当前事件为回滚事件，保留回滚前的生效值作为新的反向锚点。",
                        "proposed_value": (current_param_row or {}).get("current_value"),
                    },
                    observation_window=_build_parameter_observation_window_payload(
                        stage="rollback_followup",
                        observation_window_days=payload.observation_window_days,
                        observation_trade_count=payload.observation_trade_count,
                    ),
                    approval_ticket={
                        "required": rollback_policy.get("required_confirmation") == "manual_review",
                        "state": ("pending" if rollback_policy.get("required_confirmation") == "manual_review" else "not_required"),
                        "risk_level": rollback_policy.get("risk_level"),
                        "required_approver": rollback_policy.get("required_approver"),
                    },
                )
            )
            rollback_events.append(rollback_event.model_dump())
            item_payload["rollback_event"] = rollback_event.model_dump()
            if rollback_event.status in {"approved", "effective"}:
                effective_event_count += 1
            else:
                pending_review_event_count += 1

        applied = effective_event_count > 0
        if audit_store:
            audit_store.append(
                category="governance",
                message="parameter rollback 已执行",
                payload={
                    "selected_event_count": len(filtered_events),
                    "rollback_event_count": len(rollback_events),
                    "effective_event_count": effective_event_count,
                    "pending_review_event_count": pending_review_event_count,
                    "skipped_count": skipped_count,
                    "respect_approval_policy": payload.respect_approval_policy,
                    "force": payload.force,
                    "event_ids": list(selected_ids),
                },
            )
        summary_lines = [
            f"已处理 {len(filtered_events)} 条回滚候选，生成 {len(rollback_events)} 条 rollback event。",
            f"执行结果: effective={effective_event_count} pending_review={pending_review_event_count} skipped={skipped_count}。",
        ]
        return {
            "ok": True,
            "applied": applied,
            "count": len(execution_items),
            "items": execution_items,
            "rollback_events": rollback_events,
            "execution_summary": {
                "effective_event_count": effective_event_count,
                "pending_review_event_count": pending_review_event_count,
                "skipped_count": skipped_count,
                "respect_approval_policy": payload.respect_approval_policy,
                "force": payload.force,
            },
            "summary_lines": summary_lines,
        }

    @router.post("/learning/parameter-hints/rollback-approval")
    async def approve_parameter_hint_rollbacks(payload: ParameterRollbackApprovalInput):
        if not parameter_service:
            return {"ok": False, "error": "parameter service not initialized"}
        if payload.action not in {"approve", "release", "reject"}:
            return {"ok": False, "error": f"unsupported action: {payload.action}"}
        reviewed_items: list[dict[str, Any]] = []
        skipped_items: list[dict[str, Any]] = []
        for event_id in payload.event_ids:
            event = parameter_service.get_event(event_id)
            if not event:
                skipped_items.append({"event_id": event_id, "reason": "event_not_found"})
                continue
            if event.event_type != "param_rollback":
                skipped_items.append({"event_id": event_id, "reason": "not_rollback_event"})
                continue
            reviewed = parameter_service.review_event(
                event_id,
                action=payload.action,
                approver=payload.approver,
                comment=payload.comment,
                effective_from=payload.effective_from,
            )
            reviewed_items.append(reviewed.model_dump())
        if audit_store:
            audit_store.append(
                category="governance",
                message="parameter rollback 审批已执行",
                payload={
                    "action": payload.action,
                    "reviewed_count": len(reviewed_items),
                    "skipped_count": len(skipped_items),
                    "event_ids": payload.event_ids,
                    "approver": payload.approver,
                },
            )
        return {
            "ok": True,
            "action": payload.action,
            "count": len(reviewed_items),
            "items": reviewed_items,
            "skipped": skipped_items,
            "summary_lines": [
                f"已处理 {len(reviewed_items)} 条 rollback 审批动作，跳过 {len(skipped_items)} 条。",
            ],
        }

    def _collect_parameter_hint_inspection(
        *,
        trade_date: str | None,
        score_date: str | None,
        statuses: str,
        due_within_days: int,
        limit: int,
    ) -> dict[str, Any]:
        return collect_parameter_hint_inspection(
            parameter_service=parameter_service,
            trade_attribution_service=trade_attribution_service,
            trade_date=trade_date,
            score_date=score_date,
            statuses=statuses,
            due_within_days=due_within_days,
            limit=limit,
        )

    @router.get("/learning/parameter-hints/inspection")
    async def inspect_parameter_hint_rollbacks(
        trade_date: str | None = None,
        score_date: str | None = None,
        statuses: str = "evaluating,approved,effective",
        due_within_days: int = 1,
        limit: int = 50,
    ):
        if not parameter_service:
            return {"ok": False, "error": "parameter service not initialized"}
        return _collect_parameter_hint_inspection(
            trade_date=trade_date,
            score_date=score_date,
            statuses=statuses,
            due_within_days=due_within_days,
            limit=limit,
        )

    @router.post("/learning/parameter-hints/inspection/run")
    async def run_parameter_hint_rollbacks_inspection(payload: ParameterHintInspectionRunInput):
        if not parameter_service:
            return {"ok": False, "error": "parameter service not initialized"}
        result = _collect_parameter_hint_inspection(
            trade_date=payload.trade_date,
            score_date=payload.score_date,
            statuses=payload.statuses,
            due_within_days=payload.due_within_days,
            limit=payload.limit,
        )
        if audit_store:
            audit_store.append(
                category="governance",
                message="parameter inspection 已执行",
                payload={
                    "inspected_count": result.get("inspected_count", 0),
                    "pending_high_risk_rollback_count": result.get("pending_high_risk_rollback_count", 0),
                    "observation_near_due_count": result.get("observation_near_due_count", 0),
                    "observation_overdue_count": result.get("observation_overdue_count", 0),
                    "action_item_count": result.get("action_item_count", 0),
                    "high_priority_action_item_count": result.get("high_priority_action_item_count", 0),
                    "recommended_action_counts": result.get("recommended_action_counts", {}),
                    "statuses": result.get("statuses", []),
                    "due_within_days": payload.due_within_days,
                    "limit": payload.limit,
                    "trade_date": payload.trade_date,
                    "score_date": payload.score_date,
                },
            )
        return {
            **result,
            "executed": True,
            "summary_lines": [*list(result.get("summary_lines", [])), "巡检结果已写入审计记录。"],
        }

    @router.get("/audits")
    async def list_audits(limit: int = 20):
        records = audit_store.recent(limit) if audit_store else []
        return {"records": [record.model_dump() for record in records], "count": len(records)}

    @router.get("/audits/by-decision")
    async def audits_by_decision(decision_id: str):
        records = audit_store.recent(100) if audit_store else []
        matched = [record.model_dump() for record in records if record.payload.get("decision_id") == decision_id]
        return {"decision_id": decision_id, "records": matched, "count": len(matched)}

    @router.get("/audits/by-experiment")
    async def audits_by_experiment(experiment_id: str):
        records = audit_store.recent(100) if audit_store else []
        matched = [record.model_dump() for record in records if record.payload.get("experiment_id") == experiment_id]
        return {"experiment_id": experiment_id, "records": matched, "count": len(matched)}

    @router.get("/research/summary")
    async def research_summary():
        return _research_summary()

    @router.get("/reports/runtime")
    async def runtime_report():
        runtime = _latest_runtime()
        return runtime or {"available": False, "message": "暂无运行时报告"}

    @router.get("/reports/postclose-deployment-handoff")
    async def postclose_deployment_handoff(
        trade_date: str | None = None,
        score_date: str | None = None,
        tail_market_source: str = "latest",
        tail_market_limit: int = 20,
        due_within_days: int = 1,
        inspection_limit: int = 50,
        account_id: str | None = None,
    ):
        return _build_postclose_deployment_handoff(
            trade_date=trade_date,
            score_date=score_date,
            tail_market_source=tail_market_source,
            tail_market_limit=tail_market_limit,
            due_within_days=due_within_days,
            inspection_limit=inspection_limit,
            account_id=account_id,
        )

    @router.get("/reports/postclose-master")
    async def postclose_master():
        return _build_postclose_master_payload()

    @router.get("/reports/postclose-master-template")
    async def postclose_master_template():
        return {
            "title": "盘后总报告模板",
            "sections": ["市场情绪", "运行结果", "研究摘要", "风控结论", "执行回顾", "审计备注"],
        }

    @router.post("/meetings/record")
    async def record_meeting(payload: dict):
        if not meeting_state_store:
            return {"ok": False, "error": "meeting store not initialized"}
        meeting = {
            "meeting_id": payload.get("meeting_id") or f"meeting-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "title": payload.get("title", "未命名会议"),
            "summary": payload.get("summary", ""),
            "participants": payload.get("participants", []),
            "decisions": payload.get("decisions", []),
            "recorded_at": datetime.now().isoformat(),
        }
        history = meeting_state_store.get("history", [])
        history.append(meeting)
        meeting_state_store.set("history", history[-30:])
        meeting_state_store.set("latest", meeting)
        if audit_store:
            audit_store.append(category="meeting", message=f"会议纪要已记录: {meeting['title']}", payload=meeting)
        return {"ok": True, "meeting": meeting}

    @router.get("/meetings/latest")
    async def latest_meeting():
        return _latest_meeting()

    @router.get("/config")
    async def get_runtime_config():
        if not config_mgr:
            return {"error": "config manager not initialized"}
        return config_mgr.get().model_dump()

    @router.post("/config")
    async def update_runtime_config(updates: dict):
        if not config_mgr:
            return {"error": "config manager not initialized"}
        try:
            new_config = config_mgr.update(**updates)
            return {"ok": True, "config": new_config.model_dump()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    return router
