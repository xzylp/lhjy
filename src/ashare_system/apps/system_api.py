"""系统管理 API"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

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
from ..logging_config import get_logger
from ..monitor.persistence import (
    MonitorStateService,
    build_execution_bridge_health_client_template,
    build_execution_bridge_health_deployment_contract_sample,
    build_execution_bridge_health_ingress_payload,
    get_execution_bridge_health_latest_descriptor,
)
from ..monitor.polling_view import decorate_polling_status_for_display
from ..notify.dispatcher import MessageDispatcher
from ..notify.discussion_finalize import DiscussionFinalizeNotifier
from ..notify.governance_adjustment import GovernanceAdjustmentNotifier
from ..notify.live_execution_alerts import LiveExecutionAlertNotifier
from ..notify.monitor_changes import MonitorChangeNotifier
from ..notify.templates import (
    agent_supervision_template,
    execution_dispatch_notification_template,
    execution_dispatch_summary_lines,
    execution_precheck_summary_lines,
    execution_order_event_template,
    feishu_answer_template,
    feishu_briefing_template,
)
from ..pending_order_inspection import PendingOrderInspectionService
from ..pending_order_remediation import PendingOrderRemediationService
from ..portfolio import build_test_trading_budget, summarize_position_buckets
from ..precompute import DossierPrecomputeService
from ..reverse_repo import ReverseRepoService
from ..risk.guard import ExecutionGuard
from ..runtime_config import RuntimeConfig, RuntimeConfigManager
from ..scheduler import run_tail_market_scan
from ..selection_preferences import match_excluded_theme, normalize_excluded_theme_keywords
from ..settings import AppSettings
from ..startup_recovery import StartupRecoveryService
from ..strategy.nightly_sandbox import NightlySandbox
from ..supervision_state import (
    annotate_supervision_payload,
    record_supervision_ack,
    record_supervision_notification,
)
from ..supervision_tasks import (
    build_agent_task_plan,
    record_agent_task_dispatch,
    record_agent_task_completion,
)

logger = get_logger("system.api")


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
    "positions_unavailable": 1,
    "execution_bridge_unavailable": 2,
    "market_snapshot_fetch_failed": 2,
    "market_price_unavailable": 3,
    "stock_test_budget_reached": 4,
    "total_position_limit_reached": 5,
    "max_hold_count_reached": 6,
    "single_position_limit_reached": 6,
    "cash_unavailable": 7,
    "budget_below_min_lot": 8,
    "order_lot_insufficient": 9,
    "risk_gate_reject": 10,
    "audit_gate_hold": 11,
    "excluded_by_selection_preferences": 12,
    "trading_session_closed": 13,
    "market_snapshot_unavailable": 14,
    "market_snapshot_stale": 15,
    "limit_up_locked": 16,
    "limit_down_locked": 17,
    "price_deviation_exceeded": 18,
    "cash_exceeded": 19,
    "single_amount_exceeded": 20,
    "guard_reject": 21,
}

_EXECUTION_BLOCKER_LABELS = {
    "emergency_stop_active": "交易总开关已暂停",
    "balance_unavailable": "账户资金不可用",
    "positions_unavailable": "账户持仓不可用",
    "execution_bridge_unavailable": "Windows 执行桥当前不可用",
    "market_snapshot_fetch_failed": "实时行情抓取失败",
    "market_price_unavailable": "行情价格不可用",
    "stock_test_budget_reached": "股票测试预算已用满",
    "total_position_limit_reached": "总仓位上限已占满",
    "max_hold_count_reached": "持仓数量已达到上限",
    "single_position_limit_reached": "单票仓位上限已占满",
    "cash_unavailable": "可用现金不足",
    "budget_below_min_lot": "预算不足一手",
    "order_lot_insufficient": "下单股数不足一手",
    "risk_gate_reject": "风控闸门拒绝",
    "audit_gate_hold": "审计闸门未放行",
    "excluded_by_selection_preferences": "命中当前选股排除偏好",
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
    "repair_execution_gateway": 2,
    "refresh_market_data": 3,
    "reduce_existing_positions": 4,
    "reduce_symbol_position": 5,
    "review_risk_and_audit": 6,
    "retry_when_market_opens": 7,
    "wait_for_better_price": 8,
    "prepare_cash_or_reduce_budget": 9,
}

_EXECUTION_NEXT_ACTION_LABELS = {
    "resume_trading_switch": "恢复交易总开关后再试",
    "repair_account_connection": "修复账户连接后再试",
    "repair_execution_gateway": "先恢复 Windows 执行桥/QMT 连通后再试",
    "refresh_market_data": "刷新实时行情后再试",
    "reduce_existing_positions": "先减仓再重试",
    "reduce_symbol_position": "先降低该票仓位再重试",
    "review_risk_and_audit": "先处理风控/审计阻断",
    "retry_when_market_opens": "开盘后重新预检",
    "wait_for_better_price": "等待价格恢复到可成交区间",
    "prepare_cash_or_reduce_budget": "补充现金或降低单笔预算",
    "adjust_selection_preferences": "如需纳入该方向，请先调整当前排除偏好",
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
        "positions_unavailable": ["repair_account_connection"],
        "execution_bridge_unavailable": ["repair_execution_gateway"],
        "market_snapshot_fetch_failed": ["refresh_market_data"],
        "market_snapshot_unavailable": ["refresh_market_data"],
        "market_snapshot_stale": ["refresh_market_data"],
        "market_price_unavailable": ["refresh_market_data"],
        "stock_test_budget_reached": ["reduce_existing_positions"],
        "total_position_limit_reached": ["reduce_existing_positions"],
        "max_hold_count_reached": ["reduce_existing_positions"],
        "single_position_limit_reached": ["reduce_symbol_position"],
        "cash_unavailable": ["prepare_cash_or_reduce_budget"],
        "budget_below_min_lot": ["prepare_cash_or_reduce_budget"],
        "order_lot_insufficient": ["prepare_cash_or_reduce_budget"],
        "risk_gate_reject": ["review_risk_and_audit"],
        "audit_gate_hold": ["review_risk_and_audit"],
        "excluded_by_selection_preferences": ["adjust_selection_preferences"],
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


class OpportunityTicketItem(BaseModel):
    symbol: str
    name: str = ""
    source: str = "agent_proposed"
    source_role: str = ""
    market_logic: str
    core_evidence: list[str] = Field(default_factory=list)
    risk_points: list[str] = Field(default_factory=list)
    why_now: str
    trigger_type: str = ""
    trigger_time: str = ""
    recommended_action: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    submitted_by: str = ""


class OpportunityTicketBatchInput(BaseModel):
    trade_date: str | None = None
    items: list[OpportunityTicketItem] = Field(default_factory=list)
    auto_bootstrap_cycle: bool = True
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


class ComposeOpinionWritebackInput(BaseModel):
    payload: Any = Field(default_factory=dict)
    trade_date: str | None = None
    trace_id: str | None = None
    expected_round: int | None = Field(default=None, ge=1, le=2)
    expected_agent_id: str = "ashare-strategy"
    case_id_map: dict[str, str] = Field(default_factory=dict)
    auto_rebuild: bool = True


class AgentAutoOpinionWritebackInput(BaseModel):
    trade_date: str | None = None
    expected_round: int | None = Field(default=None, ge=1, le=2)
    expected_agent_id: str | None = None
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
    preview: bool | None = None
    notify: bool = False
    proposed_by: str = "user"
    structured_by: str = "ashare"
    approved_by: str = "ashare-audit"
    status: str = "approved"
    effective_period: str | None = None


class FeishuBriefingNotifyInput(BaseModel):
    trade_date: str | None = None
    title: str = "飞书知情简报"
    force: bool = False


class FeishuAskInput(BaseModel):
    question: str
    trade_date: str | None = None
    notify: bool = False
    force: bool = False


class FeishuSupervisionAckInput(BaseModel):
    trade_date: str | None = None
    text: str = ""
    agent_ids: list[str] = Field(default_factory=list)
    actor: str = "feishu-bot"
    note: str = ""
    source: str = "feishu"


class AgentSupervisionCheckInput(BaseModel):
    trade_date: str | None = None
    overdue_after_seconds: int = Field(default=180, ge=30, le=3600)
    notify: bool = False
    force: bool = False


class AgentSupervisionAckInput(BaseModel):
    trade_date: str | None = None
    agent_ids: list[str] = Field(default_factory=list)
    actor: str = "operator"
    note: str = ""


class DossierPrecomputeInput(BaseModel):
    trade_date: str | None = None
    as_of_time: str | None = None
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
    feishu_longconn_state_store: StateStore | None = None,
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
    nightly_sandbox = NightlySandbox(settings.storage_root)
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

    def _is_pid_alive(pid: Any) -> bool | None:
        try:
            resolved = int(pid)
        except (TypeError, ValueError):
            return None
        if resolved <= 0:
            return None
        try:
            os.kill(resolved, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _build_feishu_longconn_status(now: datetime | None = None) -> dict[str, Any]:
        if feishu_longconn_state_store:
            feishu_longconn_state_store._load()
        state = _sanitize_json_compatible(feishu_longconn_state_store.data if feishu_longconn_state_store else {}) or {}
        resolved_now = now or datetime.now()
        reported_status = state.get("status") or ("idle" if not state else "unknown")
        last_connected_at = state.get("last_connected_at")
        last_event_at = state.get("last_event_at")
        last_heartbeat_at = state.get("last_heartbeat_at")
        last_error_at = state.get("last_error_at")
        pid_alive = _is_pid_alive(state.get("pid"))

        freshness_source = last_heartbeat_at or last_event_at or last_connected_at
        freshness_age_seconds: int | None = None
        is_fresh = False
        if freshness_source:
            freshness_dt = _parse_iso_dt(str(freshness_source))
            if freshness_dt is not None:
                freshness_age_seconds = max(int((resolved_now - freshness_dt).total_seconds()), 0)
                is_fresh = freshness_age_seconds <= 90

        effective_status = str(reported_status)
        if state and reported_status in {"starting", "connected"}:
            if pid_alive is False:
                effective_status = "stale"
            elif freshness_age_seconds is not None and freshness_age_seconds > 90:
                effective_status = "stale"

        return {
            "available": bool(state),
            "status": effective_status,
            "reported_status": reported_status,
            "pid": state.get("pid"),
            "pid_alive": pid_alive,
            "worker_started_at": state.get("worker_started_at"),
            "last_connected_at": last_connected_at,
            "last_event_at": last_event_at,
            "last_heartbeat_at": last_heartbeat_at,
            "last_error_at": last_error_at,
            "last_error": state.get("last_error"),
            "reconnect_count": int(state.get("reconnect_count", 0) or 0),
            "control_plane_base_url": state.get("control_plane_base_url") or "",
            "is_fresh": is_fresh,
            "freshness_age_seconds": freshness_age_seconds,
            "summary_lines": [
                (
                    f"飞书长连接状态={effective_status}（上报={reported_status}），最近连接时间={last_connected_at or '无'}。"
                    if state
                    else "当前没有飞书长连接状态记录。"
                ),
                (
                    f"最近事件时间={last_event_at}。"
                    if last_event_at
                    else "最近尚未记录到长连接事件。"
                ),
                (
                    f"最近心跳时间={last_heartbeat_at}，pid_alive={pid_alive}。"
                    if last_heartbeat_at
                    else "最近尚未记录到长连接心跳。"
                ),
                (
                    f"最近错误={state.get('last_error')}。"
                    if state.get("last_error")
                else "当前未记录长连接错误。"
                ),
            ],
        }

    def _build_operations_components_payload() -> dict[str, Any]:
        runtime = _latest_runtime()
        feishu_longconn = _build_feishu_longconn_status()
        feishu_detail_parts: list[str] = []
        if feishu_longconn.get("available"):
            feishu_detail_parts.append(f"reported={feishu_longconn.get('reported_status')}")
            if feishu_longconn.get("pid_alive") is not None:
                feishu_detail_parts.append(f"pid_alive={feishu_longconn.get('pid_alive')}")
            if feishu_longconn.get("freshness_age_seconds") is not None:
                feishu_detail_parts.append(f"age={feishu_longconn.get('freshness_age_seconds')}s")
        else:
            feishu_detail_parts.append("no_state_file")
            
        market_mode = str(settings.market_mode)
        execution_mode = str(settings.execution_mode)
        if settings.go_platform.enabled:
            market_mode = f"{market_mode}(go)"
            execution_mode = f"{execution_mode}(go)"

        return {
            "components": [
                {"name": "api_stack", "status": "ok", "detail": f"http://{settings.service.host}:{settings.service.port}"},
                {"name": "market_adapter", "status": settings.market_mode, "detail": market_mode},
                {"name": "execution_adapter", "status": settings.execution_mode, "detail": execution_mode},
                {"name": "go_platform", "status": ("enabled" if settings.go_platform.enabled else "disabled"), "detail": settings.go_platform.base_url},
                {"name": "scheduler", "status": "managed_externally"},
                {
                    "name": "feishu_longconn",
                    "status": feishu_longconn.get("status") or "idle",
                    "detail": ", ".join(feishu_detail_parts),
                },
                {"name": "runtime_report", "status": "ready" if runtime else "idle"},
            ],
            "timestamp": datetime.now().isoformat(),
        }

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
    def _build_execution_guard(runtime_config: RuntimeConfig) -> ExecutionGuard:
        return ExecutionGuard.from_runtime_config(runtime_config)

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

    def _latest_compose_evaluation_for_trade_date(trade_date: str) -> dict[str, Any]:
        if runtime_state_store is None:
            return {}
        records = list(runtime_state_store.get("compose_evaluations", []) or [])
        for item in reversed(records):
            generated_at = str(item.get("generated_at") or "").strip()
            if generated_at.startswith(trade_date):
                return _sanitize_json_compatible(item)
            adoption = dict(item.get("adoption") or {})
            if str(adoption.get("trade_date") or "").strip() == trade_date:
                return _sanitize_json_compatible(item)
        return {}

    def _compose_evaluation_metrics_for_trade_date(trade_date: str | None) -> dict[str, Any]:
        if runtime_state_store is None or not trade_date:
            return {"count": 0, "latest": {}}
        records = list(runtime_state_store.get("compose_evaluations", []) or [])
        matched: list[dict[str, Any]] = []
        for item in records:
            generated_at = str(item.get("generated_at") or "").strip()
            adoption = dict(item.get("adoption") or {})
            if generated_at.startswith(trade_date) or str(adoption.get("trade_date") or "").strip() == trade_date:
                matched.append(_sanitize_json_compatible(item))
        latest = matched[-1] if matched else {}
        return {
            "count": len(matched),
            "latest": latest,
            "latest_generated_at": str((latest or {}).get("generated_at") or "").strip() or None,
            "latest_trace_id": str((latest or {}).get("trace_id") or "").strip() or None,
        }

    def _compose_evaluation_by_trace_id(trace_id: str) -> dict[str, Any]:
        if runtime_state_store is None or not trace_id:
            return {}
        records = list(runtime_state_store.get("compose_evaluations", []) or [])
        for item in reversed(records):
            if str(item.get("trace_id") or "").strip() == trace_id:
                return _sanitize_json_compatible(item)
        return {}

    def _build_learned_asset_review_guidance(trade_date: str, requested_agent_id: str | None = None) -> dict[str, Any]:
        record = _latest_compose_evaluation_for_trade_date(trade_date)
        if not record:
            return {"available": False}
        explicit_asset_ids = [
            str(item).strip()
            for item in list(record.get("learned_asset_ids") or [])
            if str(item).strip()
        ]
        active_asset_ids = [
            str(item).strip()
            for item in list(record.get("active_learned_asset_ids") or [])
            if str(item).strip()
        ]
        auto_selected_asset_ids = [
            str(item).strip()
            for item in list(record.get("auto_selected_learned_asset_ids") or [])
            if str(item).strip()
        ]
        options = dict(record.get("learned_asset_options") or {})
        auto_apply_active = bool(options.get("auto_apply_active"))
        preferred_tags = [
            str(item).strip()
            for item in list(options.get("preferred_tags") or [])
            if str(item).strip()
        ]
        if not explicit_asset_ids and not active_asset_ids and not auto_selected_asset_ids and not auto_apply_active:
            return {"available": False}

        shared_questions = [
            "这轮 learned asset 的启用是否和当前市场假设、主题方向、战法结构真实匹配？",
            "若启用了 learned asset，它到底改变了哪些排序、过滤或偏置，而不是只在口头上更智能？",
        ]
        per_agent_questions = {
            "ashare-research": [
                "相关 learned asset 的历史标签、主题或 match_keywords 是否仍被当前新闻、政策、题材扩散所支持？",
                "如果当前催化不支持沿用该学习资产，应明确指出失配点，而不是默认沿用历史口味。",
            ],
            "ashare-risk": [
                "本轮 learned asset 会不会把旧市场环境下学到的偏置硬套到当前盘面，形成追高、拥挤或过拟合风险？",
                "若自动吸附后排序明显漂移，是否需要限额、降权或暂缓沿用？",
            ],
            "ashare-audit": [
                "团队有没有清楚说明为什么开启 learned asset、命中了哪些资产、改变了哪些排序与结论？",
                "如果只是开启了自动吸附但没有留下证据链或理由，应记录为流程缺口。",
            ],
            "ashare-strategy": [
                "启用该 learned asset 后，究竟增强了哪些战法/因子维度，是否真的改善当前排序？",
                "若排序变化极小，也要明确说明本轮学习资产贡献有限。",
            ],
        }
        selected_questions = per_agent_questions.get(requested_agent_id or "", [])
        summary_lines = []
        if explicit_asset_ids:
            summary_lines.append("显式 learned asset：" + "、".join(explicit_asset_ids[:3]))
        if active_asset_ids:
            summary_lines.append("本轮 active learned asset：" + "、".join(active_asset_ids[:3]))
        if auto_selected_asset_ids:
            summary_lines.append("自动吸附 learned asset：" + "、".join(auto_selected_asset_ids[:3]))
        if auto_apply_active:
            summary_lines.append(
                "自动吸附已开启"
                + (f"，偏好标签={','.join(preferred_tags[:3])}" if preferred_tags else "")
            )
        return {
            "available": True,
            "trace_id": str(record.get("trace_id") or "").strip(),
            "auto_apply_active": auto_apply_active,
            "preferred_tags": preferred_tags,
            "explicit_asset_ids": explicit_asset_ids,
            "active_asset_ids": active_asset_ids,
            "auto_selected_asset_ids": auto_selected_asset_ids,
            "shared_questions": shared_questions,
            "agent_questions": selected_questions,
            "summary_lines": summary_lines,
        }

    def _build_learned_asset_execution_guidance(trade_date: str) -> dict[str, Any]:
        record = _latest_compose_evaluation_for_trade_date(trade_date)
        if not record:
            return {"available": False}
        active_asset_ids = [
            str(item).strip()
            for item in list(record.get("active_learned_asset_ids") or [])
            if str(item).strip()
        ]
        auto_selected_asset_ids = [
            str(item).strip()
            for item in list(record.get("auto_selected_learned_asset_ids") or [])
            if str(item).strip()
        ]
        options = dict(record.get("learned_asset_options") or {})
        auto_apply_active = bool(options.get("auto_apply_active"))
        if not active_asset_ids and not auto_selected_asset_ids and not auto_apply_active:
            return {"available": False}
        cautious_preview = bool(auto_selected_asset_ids) or auto_apply_active
        summary_lines = []
        if active_asset_ids:
            summary_lines.append("本轮 active learned asset：" + "、".join(active_asset_ids[:3]))
        if auto_selected_asset_ids:
            summary_lines.append("本轮自动吸附资产：" + "、".join(auto_selected_asset_ids[:3]))
        if cautious_preview:
            summary_lines.append("执行建议：先看预演与限额，不要把自动吸附结果直接当成满额提交依据。")
        return {
            "available": True,
            "trace_id": str(record.get("trace_id") or "").strip(),
            "auto_apply_active": auto_apply_active,
            "active_asset_ids": active_asset_ids,
            "auto_selected_asset_ids": auto_selected_asset_ids,
            "requires_cautious_preview": cautious_preview,
            "executor_questions": [
                "这轮机会是否受 learned asset 影响，如果受影响，影响的是排序还是仓位规模判断？",
                "若本轮存在自动吸附，是否应优先预演、限额或减少一次性提交数量？",
                "若执行回执不佳，是否需要把滑点/失败原因反馈给 learned asset 评估链，而不是只归因为市场噪音？",
            ],
            "summary_lines": summary_lines,
        }

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

    def _advance_discussion_after_writeback(
        updated_cases: list[CandidateCase],
        *,
        source: str,
    ) -> dict[str, Any]:
        if not updated_cases:
            return {"available": False, "source": source}
        trade_dates = [
            str(case.trade_date).strip()
            for case in updated_cases
            if str(getattr(case, "trade_date", "") or "").strip()
        ]
        trade_date = trade_dates[-1] if trade_dates else None
        if not trade_date:
            return {"available": False, "source": source}

        latest_by_agent: dict[str, CandidateOpinion] = {}
        for case in updated_cases:
            for opinion in list(case.opinions or []):
                previous = latest_by_agent.get(opinion.agent_id)
                if previous is None or (opinion.recorded_at, opinion.round) >= (previous.recorded_at, previous.round):
                    latest_by_agent[opinion.agent_id] = opinion
        for agent_id, opinion in latest_by_agent.items():
            record_agent_task_completion(
                meeting_state_store,
                trade_date,
                agent_id=agent_id,
                completion_type="discussion_opinion_written",
                completion_payload={
                    "source": source,
                    "round": opinion.round,
                    "recorded_at": opinion.recorded_at,
                },
                completed_at=opinion.recorded_at,
            )

        if not discussion_cycle_service:
            return {
                "available": False,
                "trade_date": trade_date,
                "source": source,
                "completed_agents": sorted(latest_by_agent.keys()),
            }

        cycle_before = discussion_cycle_service.get_cycle(trade_date)
        refreshed_cycle = discussion_cycle_service.refresh_cycle(trade_date)
        _save_monitor_pool_snapshot(trade_date, refreshed_cycle, source=f"discussion_writeback:{source}")
        _persist_discussion_context(
            _build_discussion_context_payload(trade_date, cycle_payload=refreshed_cycle.model_dump())
        )
        return {
            "available": True,
            "trade_date": trade_date,
            "source": source,
            "completed_agents": sorted(latest_by_agent.keys()),
            "cycle_before": _serialize_cycle_compact(cycle_before) if cycle_before else {"available": False},
            "cycle_after": _serialize_cycle_compact(refreshed_cycle),
            "advanced": bool(
                cycle_before
                and (
                    cycle_before.discussion_state != refreshed_cycle.discussion_state
                    or int(cycle_before.current_round or 0) != int(refreshed_cycle.current_round or 0)
                )
            ),
        }

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
        api_base_url = _resolve_control_plane_base_url()
        serving_latest_index = _build_serving_latest_index()
        readiness_payload = _build_readiness(account_id=account_id)
        execution_bridge_deployment_contract_sample = build_execution_bridge_health_deployment_contract_sample(
            api_base_url=api_base_url,
        )
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
        latest_review_board_sections = dict((latest_review_board or {}).get("sections") or {})
        latest_review_board_counts = dict((latest_review_board or {}).get("counts") or {})
        latest_priority_board = dict(latest_review_board_sections.get("priority_board") or {})
        latest_governance_effects = dict(latest_review_board_sections.get("governance_effects") or {})
        latest_review_board_summary_lines = list((latest_review_board or {}).get("summary_lines") or [])
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
            "latest_review_board_counts": latest_review_board_counts,
            "latest_review_board_summary_lines": latest_review_board_summary_lines,
            "latest_priority_board": latest_priority_board,
            "latest_governance_effects": latest_governance_effects,
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

    def _resolve_control_plane_base_url(request: Request | None = None) -> str:
        configured = str(settings.service.public_base_url or "").strip()
        if configured:
            return configured.rstrip("/")

        if request is not None:
            candidate = str(request.base_url).rstrip("/")
            if candidate:
                return candidate

        host = str(settings.service.host or "127.0.0.1").strip() or "127.0.0.1"
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        return f"http://{host}:{settings.service.port}"

    def _build_windows_execution_gateway_onboarding_bundle(
        account_id: str | None = None,
        *,
        request: Request | None = None,
    ) -> dict[str, Any]:
        control_plane_base_url = _resolve_control_plane_base_url(request=request)
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
            "control_plane_base_url": control_plane_base_url,
            "worker_entrypoint": {
                "cli": "ashare-execution-gateway-worker",
                "module": "ashare_system.windows_execution_gateway_worker",
                "recommended_once_command": (
                    "ashare-execution-gateway-worker "
                    f"--control-plane-base-url {control_plane_base_url} "
                    "--source-id windows-vm-a "
                    "--deployment-role primary_gateway "
                    "--bridge-path \"linux_openclaw -> windows_gateway -> qmt_vm\" "
                    "--executor-mode fail_unconfigured --once"
                ),
                "recommended_xtquant_command": (
                    "ashare-execution-gateway-worker "
                    f"--control-plane-base-url {control_plane_base_url} "
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
        def _snapshot_age_seconds(payload: dict[str, Any] | None) -> float | None:
            if not isinstance(payload, dict):
                return None
            captured_at = str(payload.get("captured_at") or "").strip()
            if not captured_at:
                return None
            try:
                captured_dt = datetime.fromisoformat(captured_at)
            except ValueError:
                return None
            now = datetime.now(tz=captured_dt.tzinfo) if captured_dt.tzinfo else datetime.now()
            return max((now - captured_dt).total_seconds(), 0.0)

        resolved_account_id = _resolve_account_id(account_id)
        health_result = EnvironmentHealthcheck(settings).run()
        checks: list[dict[str, Any]] = []
        non_live_mode = settings.run_mode != "live"
        for item in health_result.checks:
            normalized = dict(item)
            if non_live_mode and item["name"] in {"xtquant_root", "xtquantservice_root"}:
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

        latest_account_state = account_state_service.latest() if account_state_service else {}
        if latest_account_state and str(latest_account_state.get("account_id") or "") != resolved_account_id:
            latest_account_state = {}
        account_state_age_seconds = _snapshot_age_seconds(latest_account_state)
        account_state_is_fresh = (
            account_state_age_seconds is not None and account_state_age_seconds <= 600
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
                "age_seconds": account_state_age_seconds,
            }
            account_access_ok = bool(latest_account_state.get("verified"))
            if settings.run_mode == "live":
                account_access_ok = account_access_ok and bool(latest_account_state.get("config_match"))
            account_access_ok = account_access_ok and account_state_is_fresh
        elif latest_account_state:
            account_error = latest_account_state.get("error")
        checks.append(
            {
                "name": "account_access",
                "status": (
                    "ok"
                    if account_access_ok
                    else ("warning" if non_live_mode or not latest_account_state else "invalid")
                ),
                "detail": (
                    account_error
                    or (
                        json.dumps(account_detail, ensure_ascii=False)
                        if account_detail
                        else "account_state_cache_unavailable"
                    )
                ),
            }
        )

        meeting_state_latest = (
            meeting_state_store.get_many(
                [
                    "latest_startup_recovery",
                    "latest_pending_order_inspection",
                    "latest_execution_reconciliation",
                    "latest_pending_order_remediation",
                ]
            )
            if meeting_state_store
            else {}
        )
        startup_recovery = dict(meeting_state_latest.get("latest_startup_recovery") or {})
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

        latest_pending_order_inspection = dict(
            meeting_state_latest.get("latest_pending_order_inspection") or {}
        )
        pending_order_inspection = (
            latest_pending_order_inspection
            if latest_pending_order_inspection
            else (
                {
                    "status": "warning",
                    "error": "pending order inspection cache unavailable",
                    "pending_count": 0,
                    "warning_count": 0,
                    "stale_count": 0,
                    "summary_lines": ["pending order inspection cache unavailable"],
                }
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
        )
        latest_execution_reconciliation = dict(
            meeting_state_latest.get("latest_execution_reconciliation") or {}
        )
        latest_pending_order_remediation = dict(
            meeting_state_latest.get("latest_pending_order_remediation") or {}
        )
        checks.append(
            {
                "name": "account_identity",
                "status": (
                    "ok"
                    if latest_account_state and latest_account_state.get("status") == "ok" and latest_account_state.get("verified")
                    and (latest_account_state.get("config_match") or non_live_mode)
                    and account_state_is_fresh
                    else ("warning" if non_live_mode else "invalid")
                ),
                "detail": (
                    latest_account_state.get("summary_lines", ["unavailable"])[0]
                    if latest_account_state
                    else "account_state_cache_unavailable"
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
                    else ("warning" if inspection_status in {"warning", "error", "missing"} else "invalid")
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
                    else ("warning" if remediation_status in {"missing", "error"} else "invalid")
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
                    else ("warning" if reconciliation_status in {"missing", "error"} else "invalid")
                ),
                "detail": (
                    latest_execution_reconciliation.get("error")
                    or (
                        reconciliation_status
                        if reconciliation_status == "missing"
                        else f"matched={latest_execution_reconciliation.get('matched_order_count', 0)} trades={latest_execution_reconciliation.get('trade_count', 0)} orphan_trades={latest_execution_reconciliation.get('orphan_trade_count', 0)}"
                    )
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
                f"运行就绪检查: status={status} run_mode={settings.run_mode} account={resolved_account_id}。",
                (
                    f"账户状态缓存 age={round(account_state_age_seconds, 1)}s fresh={account_state_is_fresh}。"
                    if account_state_age_seconds is not None
                    else "账户状态缓存缺失。"
                ),
                f"未决订单 pending={pending_order_inspection.get('pending_count', 0)} warning={pending_order_inspection.get('warning_count', 0)} stale={pending_order_inspection.get('stale_count', 0)}。",
            ],
        }

    def _parse_apply_blocked_windows(value: str | None) -> list[dict[str, Any]]:
        windows: list[dict[str, Any]] = []
        for raw_item in str(value or "").split(","):
            item = raw_item.strip()
            if not item or "-" not in item:
                continue
            start_text, end_text = [part.strip() for part in item.split("-", 1)]
            start_match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", start_text)
            end_match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", end_text)
            if not start_match or not end_match:
                continue
            start_minutes = int(start_match.group(1)) * 60 + int(start_match.group(2))
            end_minutes = int(end_match.group(1)) * 60 + int(end_match.group(2))
            windows.append(
                {
                    "label": f"{start_match.group(1).zfill(2)}:{start_match.group(2)}-{end_match.group(1).zfill(2)}:{end_match.group(2)}",
                    "start_minutes": start_minutes,
                    "end_minutes": end_minutes,
                }
            )
        return windows

    def _resolve_active_apply_blocked_window(now: datetime, windows: list[dict[str, Any]]) -> str:
        now_minutes = now.hour * 60 + now.minute
        for item in windows:
            start_minutes = int(item.get("start_minutes", -1))
            end_minutes = int(item.get("end_minutes", -1))
            if start_minutes < 0 or end_minutes < 0:
                continue
            if start_minutes <= end_minutes:
                matched = start_minutes <= now_minutes <= end_minutes
            else:
                matched = now_minutes >= start_minutes or now_minutes <= end_minutes
            if matched:
                return str(item.get("label") or "")
        return ""

    def _count_apply_submissions_for_trade_date(trade_date: str) -> dict[str, Any]:
        if not meeting_state_store:
            return {
                "trade_date": trade_date,
                "submission_count": 0,
                "history_count": 0,
                "queued_count": 0,
                "submitted_count": 0,
            }
        history = meeting_state_store.get("execution_dispatch_history", [])
        if not isinstance(history, list):
            history = []
        submission_count = 0
        history_count = 0
        queued_count = 0
        submitted_count = 0
        for raw_item in history:
            if not isinstance(raw_item, dict):
                continue
            if str(raw_item.get("trade_date") or "") != trade_date:
                continue
            status = str(raw_item.get("status") or "")
            if status not in {"queued_for_gateway", "submitted"}:
                continue
            history_count += 1
            item_queued = max(int(raw_item.get("queued_count", 0) or 0), 0)
            item_submitted = max(int(raw_item.get("submitted_count", 0) or 0), 0)
            if item_queued == 0 and item_submitted == 0:
                if status == "queued_for_gateway":
                    item_queued = 1
                elif status == "submitted":
                    item_submitted = 1
            queued_count += item_queued
            submitted_count += item_submitted
            submission_count += item_queued + item_submitted
        return {
            "trade_date": trade_date,
            "submission_count": submission_count,
            "history_count": history_count,
            "queued_count": queued_count,
            "submitted_count": submitted_count,
        }

    def _build_controlled_apply_readiness(
        trade_date: str | None = None,
        *,
        account_id: str | None = None,
        max_apply_intents: int = 1,
        intent_ids: tuple[str, ...] = (),
        allowed_symbols: tuple[str, ...] = (),
        require_live: bool = True,
        require_trading_session: bool = True,
        max_equity_position_limit: float | None = 0.2,
        max_single_amount: float | None = 50000.0,
        min_reverse_repo_reserved_amount: float | None = 70000.0,
        max_stock_test_budget_amount: float | None = None,
        max_apply_submissions_per_day: int | None = None,
        blocked_time_windows: str | None = None,
        include_details: bool = True,
    ) -> dict[str, Any]:
        resolved_trade_date = trade_date or datetime.now().date().isoformat()
        resolved_account_id = _resolve_account_id(account_id)
        resolved_intent_ids = tuple(sorted({str(item).strip() for item in intent_ids if str(item).strip()}))
        resolved_allowed_symbols = tuple(sorted({str(item).strip() for item in allowed_symbols if str(item).strip()}))
        now = datetime.now()
        session_open = is_trading_session(now)
        parsed_blocked_windows = _parse_apply_blocked_windows(blocked_time_windows)
        active_blocked_window = _resolve_active_apply_blocked_window(now, parsed_blocked_windows)
        apply_submission_snapshot = _count_apply_submissions_for_trade_date(resolved_trade_date)
        longconn = _build_feishu_longconn_status(now)
        cycle = discussion_cycle_service.get_cycle(resolved_trade_date) if discussion_cycle_service else None
        precheck = _build_execution_precheck(resolved_trade_date, account_id=resolved_account_id)
        intents = _build_execution_intents_from_precheck(resolved_trade_date, precheck)
        intent_items = list(intents.get("intents") or [])
        precheck_items = list(precheck.get("items") or [])
        if resolved_intent_ids:
            intent_items = [item for item in intent_items if str(item.get("intent_id") or "") in set(resolved_intent_ids)]
            selected_case_ids = {str(item.get("case_id") or "") for item in intent_items if str(item.get("case_id") or "").strip()}
            precheck_items = [item for item in precheck_items if str(item.get("case_id") or "") in selected_case_ids]

        filtered_precheck = dict(precheck)
        filtered_precheck["items"] = precheck_items
        filtered_precheck["approved_count"] = sum(1 for item in precheck_items if bool(item.get("approved")))
        filtered_precheck["blocked_count"] = sum(1 for item in precheck_items if not bool(item.get("approved")))
        filtered_precheck["available"] = bool(precheck_items)
        filtered_precheck["status"] = (
            "ready"
            if filtered_precheck["approved_count"] > 0 and not filtered_precheck.get("balance_error")
            else "blocked"
        )

        filtered_intents = dict(intents)
        filtered_intents["intents"] = intent_items
        filtered_intents["blocked"] = [
            item for item in list(intents.get("blocked") or [])
            if not resolved_intent_ids or str(item.get("case_id") or "") in {
                str(intent.get("case_id") or "") for intent in intent_items
            }
        ]
        filtered_intents["intent_count"] = len(intent_items)
        filtered_intents["blocked_count"] = len(list(filtered_intents.get("blocked") or []))
        filtered_intents["status"] = "ready" if intent_items else "blocked"
        filtered_intents["execution_precheck"] = filtered_precheck

        if resolved_intent_ids:
            filtered_precheck["summary_lines"] = list(filtered_precheck.get("summary_lines") or []) + [
                f"受控 intent 范围: {','.join(resolved_intent_ids)}。"
            ]
            filtered_intents["summary_lines"] = list(filtered_intents.get("summary_lines") or []) + [
                f"受控 intent 范围: {','.join(resolved_intent_ids)}。"
            ]

        intent_symbols = [str(item.get("symbol") or "").strip() for item in intent_items if str(item.get("symbol") or "").strip()]
        approved_items = [item for item in precheck_items if bool(item.get("approved"))]
        effective_single_amount = max(
            [float(item.get("max_single_amount", 0.0) or 0.0) for item in precheck_items] or [0.0]
        )

        bridge_ok = True
        bridge_detail = "non_windows_execution_plane"
        latest_bridge_health: dict[str, Any] = {}
        if settings.execution_plane == "windows_gateway":
            bridge_ok = False
            if not _get_execution_gateway_state_store():
                bridge_detail = "execution_gateway_state_unavailable"
            elif monitor_state_service:
                latest_bridge_payload = monitor_state_service.get_latest_execution_bridge_health() or {}
                latest_bridge_health = dict(latest_bridge_payload.get("health") or {})
                gateway_snapshot = dict(latest_bridge_health.get("windows_execution_gateway") or {})
                qmt_snapshot = dict(latest_bridge_health.get("qmt_vm") or {})
                if not latest_bridge_health:
                    bridge_detail = "health_missing"
                elif not bool(gateway_snapshot.get("reachable", False)):
                    bridge_detail = "gateway_unreachable"
                elif not bool(latest_bridge_health.get("qmt_connected", False)) or not bool(qmt_snapshot.get("reachable", False)):
                    bridge_detail = "qmt_unreachable"
                else:
                    bridge_ok = True
                    bridge_detail = str(latest_bridge_health.get("overall_status") or "healthy")
            else:
                bridge_detail = "monitor_state_service_unavailable"

        checks: list[dict[str, Any]] = []
        checks.append(
            {
                "name": "run_mode_live" if require_live else "run_mode",
                "status": ("ok" if (settings.run_mode == "live" or not require_live) else "blocked"),
                "detail": f"run_mode={settings.run_mode} require_live={require_live}",
            }
        )
        checks.append(
            {
                "name": "live_trade_enabled" if require_live else "trade_submission_flag",
                "status": ("ok" if (settings.live_trade_enabled or not require_live) else "blocked"),
                "detail": f"live_trade_enabled={settings.live_trade_enabled} require_live={require_live}",
            }
        )
        checks.append(
            {
                "name": "execution_bridge",
                "status": ("ok" if bridge_ok else "blocked"),
                "detail": (
                    f"plane={settings.execution_plane} detail={bridge_detail} overall_status={latest_bridge_health.get('overall_status')}"
                    if settings.execution_plane == "windows_gateway"
                    else f"plane={settings.execution_plane}"
                ),
            }
        )
        checks.append(
            {
                "name": "feishu_longconn",
                "status": ("ok" if str(longconn.get("status")) == "connected" and bool(longconn.get("is_fresh")) else "blocked"),
                "detail": (
                    f"status={longconn.get('status')} is_fresh={longconn.get('is_fresh')} pid_alive={longconn.get('pid_alive')}"
                ),
            }
        )
        checks.append(
            {
                "name": "trading_session" if require_trading_session else "trading_session_optional",
                "status": ("ok" if (session_open or not require_trading_session) else "blocked"),
                "detail": f"session_open={session_open} now={now.strftime('%Y-%m-%d %H:%M:%S')}",
            }
        )
        checks.append(
            {
                "name": "discussion_cycle",
                "status": ("ok" if bool(cycle) else "blocked"),
                "detail": (
                    f"available={bool(cycle)} state={getattr(cycle, 'discussion_state', None) or getattr(cycle, 'pool_state', None) or 'none'}"
                ),
            }
        )
        checks.append(
            {
                "name": "execution_precheck",
                "status": ("ok" if int(filtered_precheck.get("approved_count", 0) or 0) > 0 else "blocked"),
                "detail": (
                    f"status={filtered_precheck.get('status')} approved={filtered_precheck.get('approved_count', 0)} blocked={filtered_precheck.get('blocked_count', 0)}"
                ),
            }
        )
        checks.append(
            {
                "name": "execution_intents",
                "status": ("ok" if int(filtered_intents.get("intent_count", 0) or 0) > 0 else "blocked"),
                "detail": (
                    f"status={filtered_intents.get('status')} intents={filtered_intents.get('intent_count', 0)} blocked={filtered_intents.get('blocked_count', 0)}"
                ),
            }
        )
        if resolved_intent_ids:
            checks.append(
                {
                    "name": "selected_intent_scope",
                    "status": ("ok" if intent_items else "blocked"),
                    "detail": (
                        f"requested={','.join(resolved_intent_ids)} matched={','.join(str(item.get('intent_id') or '') for item in intent_items) or 'NONE'}"
                    ),
                }
            )
        checks.append(
            {
                "name": "apply_intent_limit",
                "status": (
                    "ok"
                    if max_apply_intents <= 0 or int(filtered_intents.get("intent_count", 0) or 0) <= max_apply_intents
                    else "blocked"
                ),
                "detail": f"intent_count={filtered_intents.get('intent_count', 0)} max_apply_intents={max_apply_intents}",
            }
        )
        checks.append(
            {
                "name": "apply_time_window",
                "status": ("ok" if not active_blocked_window else "blocked"),
                "detail": (
                    f"now={now.strftime('%H:%M')} blocked_window={active_blocked_window}"
                    if active_blocked_window
                    else (
                        f"now={now.strftime('%H:%M')} blocked_windows="
                        + (
                            ",".join(str(item.get("label") or "") for item in parsed_blocked_windows)
                            if parsed_blocked_windows
                            else "NONE"
                        )
                    )
                ),
            }
        )
        checks.append(
            {
                "name": "apply_symbol_whitelist",
                "status": (
                    "ok"
                if not resolved_allowed_symbols or set(intent_symbols).issubset(set(resolved_allowed_symbols))
                    else "blocked"
                ),
                "detail": (
                    f"allowed={','.join(resolved_allowed_symbols) or 'ANY'} intents={','.join(intent_symbols) or 'NONE'}"
                ),
            }
        )
        checks.append(
            {
                "name": "emergency_stop",
                "status": ("ok" if not bool(filtered_precheck.get("emergency_stop_active")) else "blocked"),
                "detail": (
                    f"emergency_stop_active={filtered_precheck.get('emergency_stop_active')} reason={filtered_precheck.get('trading_halt_reason') or 'inactive'}"
                ),
            }
        )
        checks.append(
            {
                "name": "equity_position_limit",
                "status": (
                    "ok"
                    if max_equity_position_limit is None
                    or float(filtered_precheck.get("equity_position_limit", 0.0) or 0.0) <= max_equity_position_limit + 1e-9
                    else "blocked"
                ),
                "detail": f"current={filtered_precheck.get('equity_position_limit')} allowed<={max_equity_position_limit}",
            }
        )
        checks.append(
            {
                "name": "single_amount_limit",
                "status": (
                    "ok"
                    if max_single_amount is None or effective_single_amount <= max_single_amount + 1e-9
                    else "blocked"
                ),
                "detail": f"current={round(effective_single_amount, 2)} allowed<={max_single_amount}",
            }
        )
        checks.append(
            {
                "name": "reverse_repo_reserved_amount",
                "status": (
                    "ok"
                    if min_reverse_repo_reserved_amount is None
                    or float(filtered_precheck.get("reverse_repo_reserved_amount", 0.0) or 0.0) >= min_reverse_repo_reserved_amount - 1e-9
                    else "blocked"
                ),
                "detail": f"current={filtered_precheck.get('reverse_repo_reserved_amount')} required>={min_reverse_repo_reserved_amount}",
            }
        )
        checks.append(
            {
                "name": "stock_test_budget_amount",
                "status": (
                    "ok"
                    if max_stock_test_budget_amount is None
                    or float(filtered_precheck.get("stock_test_budget_amount", 0.0) or 0.0) <= max_stock_test_budget_amount + 1e-9
                    else "blocked"
                ),
                "detail": f"current={filtered_precheck.get('stock_test_budget_amount')} allowed<={max_stock_test_budget_amount}",
            }
        )
        checks.append(
            {
                "name": "daily_apply_submission_limit",
                "status": (
                    "ok"
                    if max_apply_submissions_per_day is None
                    or int(apply_submission_snapshot.get("submission_count", 0) or 0) < max_apply_submissions_per_day
                    else "blocked"
                ),
                "detail": (
                    f"current={apply_submission_snapshot.get('submission_count', 0)} "
                    f"queued={apply_submission_snapshot.get('queued_count', 0)} "
                    f"submitted={apply_submission_snapshot.get('submitted_count', 0)} "
                    f"limit={max_apply_submissions_per_day}"
                ),
            }
        )

        check_statuses = {item["status"] for item in checks}
        overall_status = "ready"
        if "blocked" in check_statuses:
            overall_status = "blocked"
        elif "warning" in check_statuses:
            overall_status = "degraded"

        summary_lines = [
            f"受控 apply 准入检查: status={overall_status} trade_date={resolved_trade_date} account={resolved_account_id}。",
            (
                f"run_mode={settings.run_mode} live_trade_enabled={settings.live_trade_enabled} "
                f"execution_plane={settings.execution_plane} intents={filtered_intents.get('intent_count', 0)} approved={filtered_precheck.get('approved_count', 0)}。"
            ),
            (
                f"受控阈值: max_apply_intents={max_apply_intents} "
                f"max_equity_position_limit={max_equity_position_limit} max_single_amount={max_single_amount} "
                f"min_reverse_repo_reserved_amount={min_reverse_repo_reserved_amount} "
                f"max_stock_test_budget_amount={max_stock_test_budget_amount} "
                f"max_apply_submissions_per_day={max_apply_submissions_per_day}。"
            ),
        ]
        if resolved_allowed_symbols:
            summary_lines.append("受控白名单: " + "、".join(resolved_allowed_symbols))
        if resolved_intent_ids:
            summary_lines.append("受控 intent: " + "、".join(resolved_intent_ids))
        if parsed_blocked_windows:
            summary_lines.append(
                "禁止 apply 时段: " + "、".join(str(item.get("label") or "") for item in parsed_blocked_windows)
            )
        summary_lines.append(
            "当日 apply 计数: "
            f"current={apply_submission_snapshot.get('submission_count', 0)} "
            f"(queued={apply_submission_snapshot.get('queued_count', 0)}, submitted={apply_submission_snapshot.get('submitted_count', 0)})。"
        )
        if filtered_precheck.get("summary_lines"):
            summary_lines.extend(list(filtered_precheck.get("summary_lines") or [])[:3])
        learned_asset_execution_guidance = dict(filtered_precheck.get("learned_asset_execution_guidance") or {})
        if learned_asset_execution_guidance.get("available"):
            summary_lines.extend(list(learned_asset_execution_guidance.get("summary_lines") or [])[:2])
        if overall_status != "ready":
            blocked_names = [str(item["name"]) for item in checks if item["status"] == "blocked"]
            summary_lines.append("未满足项: " + "、".join(blocked_names))

        payload = {
            "available": True,
            "checklist_scope": "controlled_apply_readiness",
            "generated_at": datetime.now().isoformat(),
            "trade_date": resolved_trade_date,
            "account_id": resolved_account_id,
            "status": overall_status,
            "checks": checks,
            "policy": {
                "max_apply_intents": max_apply_intents,
                "intent_ids": list(resolved_intent_ids),
                "allowed_symbols": list(resolved_allowed_symbols),
                "require_live": require_live,
                "require_trading_session": require_trading_session,
                "max_equity_position_limit": max_equity_position_limit,
                "max_single_amount": max_single_amount,
                "min_reverse_repo_reserved_amount": min_reverse_repo_reserved_amount,
                "max_stock_test_budget_amount": max_stock_test_budget_amount,
                "max_apply_submissions_per_day": max_apply_submissions_per_day,
                "blocked_time_windows": [str(item.get("label") or "") for item in parsed_blocked_windows],
            },
            "longconn": longconn,
            "cycle": (cycle.model_dump() if cycle else {"available": False, "trade_date": resolved_trade_date}),
            "execution_precheck": filtered_precheck,
            "execution_intents": filtered_intents,
            "learned_asset_execution_guidance": learned_asset_execution_guidance,
            "bridge_health": latest_bridge_health,
            "apply_submission_snapshot": apply_submission_snapshot,
            "first_intent": (intent_items[0] if intent_items else None),
            "approved_symbols": [str(item.get("symbol") or "") for item in approved_items if str(item.get("symbol") or "").strip()],
            "summary_lines": summary_lines,
        }
        if not include_details:
            payload.pop("longconn", None)
            payload.pop("cycle", None)
            payload.pop("execution_precheck", None)
            payload.pop("execution_intents", None)
            payload.pop("bridge_health", None)
            payload["detail_mode"] = "summary"
        return payload

    def _build_service_recovery_readiness(
        trade_date: str | None = None,
        *,
        account_id: str | None = None,
        max_workspace_age_seconds: int = 1800,
        max_signal_age_seconds: int = 1800,
        require_execution_bridge: bool = True,
        include_details: bool = True,
    ) -> dict[str, Any]:
        resolved_trade_date = _resolve_reference_trade_date(trade_date) or datetime.now().date().isoformat()
        resolved_account_id = _resolve_account_id(account_id)
        readiness_payload = _build_readiness(account_id=resolved_account_id)
        components_payload = _build_operations_components_payload()
        longconn = _build_feishu_longconn_status()
        workspace_context = _sanitize_json_compatible(serving_store.get_latest_workspace_context()) or {}
        runtime_context = _sanitize_json_compatible(serving_store.get_latest_runtime_context()) or {}
        discussion_context = _sanitize_json_compatible(serving_store.get_latest_discussion_context()) or {}
        monitor_context = _sanitize_json_compatible(serving_store.get_latest_monitor_context()) or {}
        supervision_payload = _build_agent_supervision_payload(resolved_trade_date)
        briefing_payload = _build_feishu_briefing_payload(resolved_trade_date)
        components = {str(item.get("name") or ""): item for item in list(components_payload.get("components") or [])}

        workspace_timestamp = _extract_activity_timestamp(workspace_context, trade_date=resolved_trade_date)
        workspace_age = _seconds_since(workspace_timestamp)
        latest_signal_at = _max_activity_timestamp(
            _extract_activity_timestamp(runtime_context, trade_date=resolved_trade_date),
            _extract_activity_timestamp(discussion_context, trade_date=resolved_trade_date),
            _extract_activity_timestamp(monitor_context, trade_date=resolved_trade_date),
            workspace_timestamp,
        )
        latest_signal_age = _seconds_since(latest_signal_at)

        bridge_ok = True
        bridge_detail = "non_windows_execution_plane"
        latest_bridge_health: dict[str, Any] = {}
        if settings.execution_plane == "windows_gateway":
            bridge_ok = False
            if not _get_execution_gateway_state_store():
                bridge_detail = "execution_gateway_state_unavailable"
            elif monitor_state_service:
                latest_bridge_payload = monitor_state_service.get_latest_execution_bridge_health() or {}
                latest_bridge_health = dict(latest_bridge_payload.get("health") or {})
                gateway_snapshot = dict(latest_bridge_health.get("windows_execution_gateway") or {})
                qmt_snapshot = dict(latest_bridge_health.get("qmt_vm") or {})
                if not latest_bridge_health:
                    bridge_detail = "health_missing"
                elif not bool(gateway_snapshot.get("reachable", False)):
                    bridge_detail = "gateway_unreachable"
                elif not bool(latest_bridge_health.get("qmt_connected", False)) or not bool(qmt_snapshot.get("reachable", False)):
                    bridge_detail = "qmt_unreachable"
                else:
                    bridge_ok = True
                    bridge_detail = str(latest_bridge_health.get("overall_status") or "healthy")
            else:
                bridge_detail = "monitor_state_service_unavailable"

        checks: list[dict[str, Any]] = []
        checks.append(
            {
                "name": "readiness",
                "status": ("ok" if str(readiness_payload.get("status")) in {"ready", "degraded"} else "blocked"),
                "detail": f"status={readiness_payload.get('status')} run_mode={readiness_payload.get('run_mode')}",
            }
        )
        api_stack = components.get("api_stack") or {}
        checks.append(
            {
                "name": "api_stack",
                "status": ("ok" if str(api_stack.get("status")) == "ok" else "blocked"),
                "detail": str(api_stack.get("detail") or ""),
            }
        )
        checks.append(
            {
                "name": "feishu_longconn",
                "status": ("ok" if str(longconn.get("status")) == "connected" and bool(longconn.get("is_fresh")) else "blocked"),
                "detail": f"status={longconn.get('status')} is_fresh={longconn.get('is_fresh')} pid_alive={longconn.get('pid_alive')}",
            }
        )
        checks.append(
            {
                "name": "execution_bridge" if require_execution_bridge else "execution_bridge_optional",
                "status": ("ok" if (bridge_ok or not require_execution_bridge) else "blocked"),
                "detail": (
                    f"plane={settings.execution_plane} detail={bridge_detail} overall_status={latest_bridge_health.get('overall_status')}"
                    if settings.execution_plane == "windows_gateway"
                    else f"plane={settings.execution_plane}"
                ),
            }
        )
        checks.append(
            {
                "name": "workspace_context",
                "status": (
                    "ok"
                    if workspace_context.get("available") and workspace_age is not None and workspace_age <= max_workspace_age_seconds
                    else "blocked"
                ),
                "detail": f"available={workspace_context.get('available')} generated_at={workspace_timestamp or ''} age={workspace_age}",
            }
        )
        checks.append(
            {
                "name": "recovery_signal_freshness",
                "status": (
                    "ok"
                    if latest_signal_at and latest_signal_age is not None and latest_signal_age <= max_signal_age_seconds
                    else "blocked"
                ),
                "detail": f"latest_signal_at={latest_signal_at or ''} age={latest_signal_age}",
            }
        )
        checks.append(
            {
                "name": "supervision_pipeline",
                "status": ("ok" if bool(supervision_payload.get("items")) and bool(supervision_payload.get("summary_lines")) else "blocked"),
                "detail": f"items={len(supervision_payload.get('items', []))} attention={len(supervision_payload.get('attention_items', []))}",
            }
        )
        checks.append(
            {
                "name": "briefing_pipeline",
                "status": ("ok" if bool(briefing_payload.get("summary_lines")) else "blocked"),
                "detail": f"summary_lines={len(briefing_payload.get('summary_lines', []))}",
            }
        )

        check_statuses = {item["status"] for item in checks}
        overall_status = "ready"
        if "blocked" in check_statuses:
            overall_status = "blocked"
        elif "warning" in check_statuses:
            overall_status = "degraded"

        summary_lines = [
            f"服务恢复检查: status={overall_status} trade_date={resolved_trade_date} account={resolved_account_id}。",
            f"workspace_age={workspace_age} latest_signal_age={latest_signal_age} execution_plane={settings.execution_plane}。",
            (
                f"longconn={longconn.get('status')} readiness={readiness_payload.get('status')} "
                f"bridge={'ok' if bridge_ok else bridge_detail} supervision_items={len(supervision_payload.get('items', []))}。"
            ),
        ]
        if overall_status != "ready":
            blocked_names = [str(item["name"]) for item in checks if item["status"] == "blocked"]
            summary_lines.append("未恢复项: " + "、".join(blocked_names))

        payload = {
            "available": True,
            "checklist_scope": "service_recovery_readiness",
            "generated_at": datetime.now().isoformat(),
            "trade_date": resolved_trade_date,
            "account_id": resolved_account_id,
            "status": overall_status,
            "checks": checks,
            "policy": {
                "max_workspace_age_seconds": max_workspace_age_seconds,
                "max_signal_age_seconds": max_signal_age_seconds,
                "require_execution_bridge": require_execution_bridge,
            },
            "components": components_payload,
            "readiness": readiness_payload,
            "longconn": longconn,
            "workspace_context": workspace_context,
            "runtime_context": runtime_context,
            "discussion_context": discussion_context,
            "monitor_context": monitor_context,
            "bridge_health": latest_bridge_health,
            "supervision": supervision_payload,
            "briefing": briefing_payload,
            "summary_lines": summary_lines,
        }
        if not include_details:
            payload.pop("components", None)
            payload.pop("readiness", None)
            payload.pop("longconn", None)
            payload.pop("workspace_context", None)
            payload.pop("runtime_context", None)
            payload.pop("discussion_context", None)
            payload.pop("monitor_context", None)
            payload.pop("bridge_health", None)
            payload.pop("supervision", None)
            payload.pop("briefing", None)
            payload["detail_mode"] = "summary"
        return payload

    def _resolve_excluded_theme_keywords() -> list[str]:
        if parameter_service:
            raw = parameter_service.get_param_value("excluded_theme_keywords")
        elif config_mgr:
            raw = getattr(config_mgr.get(), "excluded_theme_keywords", "")
        else:
            raw = ""
        return normalize_excluded_theme_keywords(raw)

    def _build_execution_precheck(trade_date: str, account_id: str | None = None) -> dict:
        start_time_all = time.perf_counter()
        steps_timing = []

        def record_step(name: str, start_time: float, success: bool = True, reason: str | None = None):
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            steps_timing.append({
                "step": name,
                "elapsed_ms": round(elapsed_ms, 2),
                "success": success,
                "reason": reason
            })
            logger.info(f"precheck_step | {name} | elapsed={elapsed_ms:.2f}ms | success={success} | reason={reason}")

        # 1. Resolve Account
        s1 = time.perf_counter()
        try:
            resolved_account_id = _resolve_account_id(account_id)
            record_step("resolve_account", s1)
        except Exception as e:
            record_step("resolve_account", s1, False, str(e))
            return {"ok": False, "error": f"resolve_account_failed: {e}"}

        # 2. Get Discussion Cycle
        s2 = time.perf_counter()
        try:
            cycle = discussion_cycle_service.get_cycle(trade_date) if discussion_cycle_service else None
            record_step("get_cycle", s2)
        except Exception as e:
            record_step("get_cycle", s2, False, str(e))
            cycle = None

        # 3. Learned Asset Guidance
        s3 = time.perf_counter()
        try:
            learned_asset_execution_guidance = _build_learned_asset_execution_guidance(trade_date)
            record_step("learned_asset_guidance", s3)
        except Exception as e:
            record_step("learned_asset_guidance", s3, False, str(e))
            learned_asset_execution_guidance = {}

        # 4. List Candidate Cases
        s4 = time.perf_counter()
        try:
            case_list = (
                candidate_case_service.list_cases(trade_date=trade_date, limit=500)
                if candidate_case_service
                else []
            )
            case_map = {item.case_id: item for item in case_list}
            record_step("list_cases", s4)
        except Exception as e:
            record_step("list_cases", s4, False, str(e))
            case_map = {}

        execution_case_ids = cycle.execution_pool_case_ids if cycle else []
        
        # 5. Account Data (Balance & Positions)
        balance_error = None
        positions_error = None
        account_data_timeout_sec = min(float(getattr(settings.windows_gateway, "timeout_sec", 10.0) or 10.0), 8.5)
        balance = None
        positions = []
        s5 = time.perf_counter()
        if execution_adapter:
            executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="execution-precheck")
            try:
                future_map = {
                    "balance": executor.submit(execution_adapter.get_balance, resolved_account_id),
                    "positions": executor.submit(execution_adapter.get_positions, resolved_account_id),
                }
                done, not_done = wait(future_map.values(), timeout=account_data_timeout_sec)
                timed_out_targets = {
                    name for name, future in future_map.items() if future in not_done
                }
                if "balance" in timed_out_targets:
                    balance_error = f"timeout>{account_data_timeout_sec:.1f}s"
                if "positions" in timed_out_targets:
                    positions_error = f"timeout>{account_data_timeout_sec:.1f}s"
                for name, future in future_map.items():
                    if future not in done:
                        continue
                    try:
                        result = future.result()
                        if name == "balance":
                            balance = result
                        else:
                            positions = result
                    except Exception as exc:
                        if name == "balance":
                            balance_error = str(exc)
                        else:
                            positions_error = str(exc)
                account_errors = [item for item in [balance_error, positions_error] if item]
                if account_errors:
                    record_step("get_account_data", s5, False, " | ".join(account_errors))
                else:
                    record_step("get_account_data", s5)
            except Exception as exc:
                balance_error = str(exc)
                record_step("get_account_data", s5, False, balance_error)
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        else:
            record_step("get_account_data", s5, False, "no_execution_adapter")

        # 6. Account State Snapshot
        s6 = time.perf_counter()
        try:
            account_state = (
                account_state_service.snapshot(resolved_account_id, persist=True)
                if account_state_service
                else None
            )
            record_step("account_state_snapshot", s6)
        except Exception as e:
            record_step("account_state_snapshot", s6, False, str(e))
            account_state = None

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
        current_equity_symbols = {
            item.symbol
            for item in position_buckets.equity_positions
            if int(getattr(item, "quantity", 0) or 0) > 0
        }
        current_equity_position_count = len(current_equity_symbols)
        invested_value = position_buckets.equity_value
        reverse_repo_value = position_buckets.reverse_repo_value
        gross_invested_value = position_buckets.total_value
        current_total_ratio = (invested_value / total_asset) if total_asset > 0 else 0.0
        gross_total_ratio = (gross_invested_value / total_asset) if total_asset > 0 else 0.0
        max_hold_count = int(getattr(runtime_config, "max_hold_count", 5) or 5)
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
        effective_total_position_limit = min(
            max_total_position,
            equity_position_limit,
        )
        trading_budget = build_test_trading_budget(
            total_asset,
            invested_value,
            reverse_repo_value,
            minimum_total_invested_amount=minimum_total_invested_amount,
            reverse_repo_reserved_amount=reverse_repo_reserved_amount,
            stock_position_limit_ratio=effective_total_position_limit,
        )
        stock_test_budget_amount = trading_budget.stock_test_budget_amount
        reverse_repo_target_value = total_asset * reverse_repo_target_ratio
        reverse_repo_gap_value = trading_budget.reverse_repo_gap_value
        daily_pnl = float(((account_state or {}).get("metrics") or {}).get("daily_pnl", 0.0) or 0.0)
        excluded_theme_keywords = _resolve_excluded_theme_keywords()

        # 7. Live Market Quotes
        live_market_quote_error = None
        live_market_quote_map: dict[str, Any] = {}
        live_quote_timestamp = now.isoformat()
        s7 = time.perf_counter()
        if settings.run_mode == "live" and market_adapter:
            live_symbols = [case_map[case_id].symbol for case_id in execution_case_ids if case_id in case_map]
            if live_symbols:
                try:
                    live_market_quote_map = {
                        item.symbol: item for item in market_adapter.get_snapshots(live_symbols)
                    }
                    record_step("get_live_quotes", s7)
                except Exception as exc:
                    live_market_quote_error = str(exc)
                    record_step("get_live_quotes", s7, False, live_market_quote_error)
        else:
            record_step("get_live_quotes", s7, True, "skipped")

        # 8. Execution Strategy Context
        s8 = time.perf_counter()
        try:
            execution_context = _resolve_execution_strategy_context()
            record_step("resolve_strategy_context", s8)
        except Exception as e:
            record_step("resolve_strategy_context", s8, False, str(e))
            execution_context = {"playbook_map": {}, "dossier_map": {}, "regime_value": 0.0}

        playbook_map = execution_context["playbook_map"]
        dossier_map = execution_context["dossier_map"]
        regime_value = execution_context["regime_value"]
        execution_guard = _build_execution_guard(runtime_config)

        # 9. Build Items
        s9 = time.perf_counter()
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
                try:
                    snapshots = market_adapter.get_snapshots([case.symbol])
                    if snapshots:
                        latest_price = float(snapshots[0].last_price or 0.0)
                        bid_price = float(snapshots[0].bid_price or 0.0)
                        ask_price = float(snapshots[0].ask_price or 0.0)
                        pre_close = float(snapshots[0].pre_close or 0.0)
                        quote_timestamp = now.isoformat()
                except Exception:
                    pass
            quote_age_seconds = snapshot_age_seconds(quote_timestamp, now)
            quote_is_fresh = is_snapshot_fresh(quote_timestamp, now, market_snapshot_ttl_seconds)
            current_position = next((item for item in positions if item.symbol == case.symbol), None)
            projected_equity_position_count = current_equity_position_count + (0 if current_position else 1)
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
            if positions_error:
                blockers.append("positions_unavailable")
            if latest_price <= 0:
                blockers.append("market_price_unavailable")
            if remaining_total_value <= 0:
                blockers.append("total_position_limit_reached")
            if not current_position and current_equity_position_count >= max_hold_count:
                blockers.append("max_hold_count_reached")
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
                force_session = os.getenv("ASHARE_TEST_FORCE_SESSION", "false").lower() == "true"
                if not session_open and not force_session:
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
            preference_match = match_excluded_theme(
                excluded_theme_keywords,
                name=str(case.name or ""),
                resolved_sector=str(resolved_sector or ""),
                extra_texts=[
                    str(case.selected_reason or ""),
                    str(case.runtime_snapshot.summary or ""),
                ],
            )
            if preference_match:
                blockers.append("excluded_by_selection_preferences")
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
                "current_equity_position_count": current_equity_position_count,
                "projected_equity_position_count": projected_equity_position_count,
                "max_hold_count": max_hold_count,
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
                "selection_preference_match": preference_match,
                "playbook_entry_window": playbook_context.get("entry_window"),
            }
            items.append(item)
            if item["approved"]:
                approved_count += 1
            else:
                blocked_count += 1

        status = "ready" if items and approved_count > 0 and not balance_error and not positions_error else "blocked"
        precheck_degrade_reason = None
        if emergency_stop_active:
            precheck_degrade_reason = "emergency_stop_active"
        elif balance_error:
            precheck_degrade_reason = "balance_unavailable"
        elif positions_error:
            precheck_degrade_reason = "positions_unavailable"
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
            f"执行约束 equity_position<={effective_total_position_limit:.0%} raw_risk_limit<={max_total_position:.0%} hold_count<={max_hold_count} single_position<={max_single_position:.0%} single_amount<={round(max_single_amount, 2)}。",
        ]
        if excluded_theme_keywords:
            summary_lines.append("当前排除方向: " + "、".join(excluded_theme_keywords) + "。")
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
        if learned_asset_execution_guidance.get("available"):
            summary_lines.extend(list(learned_asset_execution_guidance.get("summary_lines") or [])[:2])
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
                elif lead.get("primary_blocker") == "excluded_by_selection_preferences":
                    match = lead.get("selection_preference_match") or {}
                    if match:
                        summary_lines.append(
                            f"该标的命中排除方向 {match.get('keyword')}，匹配位置 {match.get('field')}={match.get('matched_text')}。"
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
            "positions_available": positions_error is None,
            "positions_error": positions_error,
            "cycle_state": (cycle.discussion_state if cycle else None),
            "execution_pool_case_ids": execution_case_ids,
            "approved_count": approved_count,
            "blocked_count": blocked_count,
            "current_equity_position_count": current_equity_position_count,
            "max_hold_count": max_hold_count,
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
            "excluded_theme_keywords": excluded_theme_keywords,
            "stock_test_budget_amount": round(stock_test_budget_amount, 2),
            "stock_test_budget_remaining": round(trading_budget.stock_test_budget_remaining, 2),
            "recommended_next_actions": payload_next_actions,
            "primary_recommended_next_action": primary_payload_next_action,
            "primary_recommended_next_action_label": primary_payload_next_action_label,
            "learned_asset_execution_guidance": learned_asset_execution_guidance,
            "items": items,
            "summary_lines": summary_lines,
        }
        if settings.run_mode == "live":
            if not session_open and execution_case_ids:
                payload["summary_lines"].append("当前为非交易时段，已保留执行池，待开盘后可重新触发预检或委托。")
            if balance_error:
                payload["summary_lines"].append(f"账户资金抓取失败: {balance_error}")
            if live_market_quote_error:
                payload["summary_lines"].append(f"实时行情抓取失败: {live_market_quote_error}")
            if positions_error:
                payload["summary_lines"].append(f"持仓抓取失败: {positions_error}")
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

    def _build_execution_intents_from_precheck(trade_date: str, precheck: dict[str, Any]) -> dict[str, Any]:
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

    def _build_execution_intents(trade_date: str, account_id: str | None = None) -> dict:
        precheck = _build_execution_precheck(trade_date, account_id=account_id)
        payload = _build_execution_intents_from_precheck(trade_date, precheck)
        payload["learned_asset_execution_guidance"] = dict(precheck.get("learned_asset_execution_guidance") or {})
        if payload["learned_asset_execution_guidance"].get("available"):
            payload["summary_lines"] = list(payload.get("summary_lines") or []) + list(
                payload["learned_asset_execution_guidance"].get("summary_lines") or []
            )[:2]
        return payload

    def _prune_stale_pending_execution_intents(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        current_trade_date = _resolve_trade_date()
        kept: list[dict[str, Any]] = []
        pruned: list[dict[str, Any]] = []
        for item in items:
            status = str(item.get("status") or "approved").strip().lower()
            trade_date = str(item.get("trade_date") or "").strip()
            live_execution_allowed = bool(item.get("live_execution_allowed"))
            claim = dict(item.get("claim") or {})
            has_gateway_claim = bool(str(claim.get("gateway_source_id") or "").strip())
            if (
                status == "approved"
                and trade_date
                and trade_date < current_trade_date
                and not live_execution_allowed
                and not has_gateway_claim
            ):
                pruned.append(item)
                continue
            kept.append(item)
        if pruned and meeting_state_store:
            meeting_state_store.set("pending_execution_intents", kept)
            audit_store.append(
                category="execution",
                message=f"清理过期 execution intent {len(pruned)} 条",
                payload={
                    "current_trade_date": current_trade_date,
                    "pruned_intents": [
                        {
                            "intent_id": item.get("intent_id"),
                            "trade_date": item.get("trade_date"),
                            "status": item.get("status"),
                            "live_execution_allowed": item.get("live_execution_allowed"),
                        }
                        for item in pruned
                    ],
                },
            )
        return kept

    def _get_pending_execution_intents() -> list[dict[str, Any]]:
        if not meeting_state_store:
            return []
        items = meeting_state_store.get("pending_execution_intents", [])
        if not isinstance(items, list):
            return []
        normalized = [item for item in (_sanitize_json_compatible(it) for it in items) if isinstance(item, dict)]
        return _prune_stale_pending_execution_intents(normalized)

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

    def _prune_active_pending_execution_intents(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        current_trade_date = _resolve_trade_date()
        kept: list[dict[str, Any]] = []
        pruned: list[dict[str, Any]] = []
        for item in items:
            status = str(item.get("status") or "approved").strip().lower()
            trade_date = str(item.get("trade_date") or "").strip()
            live_execution_allowed = bool(item.get("live_execution_allowed"))
            claim = dict(item.get("claim") or {})
            has_gateway_claim = bool(str(claim.get("gateway_source_id") or "").strip())
            if (
                status == "approved"
                and trade_date
                and trade_date < current_trade_date
                and not live_execution_allowed
                and not has_gateway_claim
            ):
                pruned.append(item)
                continue
            kept.append(item)
        state_store = _get_execution_gateway_state_store()
        if pruned and state_store:
            save_gateway_pending_execution_intents(state_store, kept)
            audit_store.append(
                category="execution",
                message=f"清理过期 execution intent {len(pruned)} 条",
                payload={
                    "current_trade_date": current_trade_date,
                    "pruned_intents": [
                        {
                            "intent_id": item.get("intent_id"),
                            "trade_date": item.get("trade_date"),
                            "status": item.get("status"),
                            "live_execution_allowed": item.get("live_execution_allowed"),
                        }
                        for item in pruned
                    ],
                },
            )
        return kept

    def _get_pending_execution_intents() -> list[dict[str, Any]]:
        items = get_gateway_pending_execution_intents(_get_execution_gateway_state_store())
        return _prune_active_pending_execution_intents(items)

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
        submit_block_detail: dict[str, Any] = {}
        if apply:
            if emergency_stop_active:
                submit_block_reason = "emergency_stop_active"
            elif execution_plane == "windows_gateway":
                if not _get_execution_gateway_state_store():
                    submit_block_reason = "execution_gateway_state_unavailable"
                elif monitor_state_service:
                    latest_bridge_payload = monitor_state_service.get_latest_execution_bridge_health() or {}
                    latest_bridge_health = dict(latest_bridge_payload.get("health") or {})
                    gateway_snapshot = dict(latest_bridge_health.get("windows_execution_gateway") or {})
                    qmt_snapshot = dict(latest_bridge_health.get("qmt_vm") or {})
                    if not latest_bridge_health:
                        submit_block_reason = "execution_bridge_unavailable"
                        submit_block_detail = {"detail": "health_missing"}
                    elif not bool(gateway_snapshot.get("reachable", False)):
                        submit_block_reason = "execution_bridge_unavailable"
                        submit_block_detail = {
                            "detail": "gateway_unreachable",
                            "overall_status": latest_bridge_health.get("overall_status"),
                        }
                    elif not bool(latest_bridge_health.get("qmt_connected", False)) or not bool(qmt_snapshot.get("reachable", False)):
                        submit_block_reason = "execution_bridge_unavailable"
                        submit_block_detail = {
                            "detail": "qmt_unreachable",
                            "overall_status": latest_bridge_health.get("overall_status"),
                        }
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
                if submit_block_detail:
                    receipt["guard_detail"] = dict(submit_block_detail)
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
            "submit_guard_detail": submit_block_detail,
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
            if submit_block_detail.get("detail"):
                payload["summary_lines"].append(
                    f"阻断细节: {submit_block_detail.get('detail')}。"
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
                "queued_count": payload.get("queued_count", 0),
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
        pending_queue_items = [
            item for item in pending_items if str(item.get("status") or "approved") == "approved"
        ]
        pending_intent_count = len(pending_queue_items)
        queued_for_gateway_count = pending_intent_count
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

    def _resolve_reference_trade_date(trade_date: str | None = None) -> str | None:
        if trade_date:
            return trade_date
        for payload in (
            serving_store.get_latest_discussion_context(),
            serving_store.get_latest_dossier_pack(),
            serving_store.get_latest_runtime_context(),
            serving_store.get_latest_workspace_context(),
        ):
            resolved = str((payload or {}).get("trade_date") or "").strip()
            if resolved:
                return resolved
        if candidate_case_service:
            latest_cases = candidate_case_service.list_cases(limit=1)
            if latest_cases:
                return latest_cases[0].trade_date
        return None

    def _extract_workspace_hot_sectors(workspace_context: dict[str, Any] | None) -> list[str]:
        payload = dict(workspace_context or {})
        candidates = (
            ((payload.get("runtime_context") or {}).get("market_profile") or {}).get("hot_sectors"),
            ((payload.get("market_context") or {}).get("market_profile") or {}).get("hot_sectors"),
        )
        sectors: list[str] = []
        for items in candidates:
            for item in list(items or []):
                name = str(item or "").strip()
                if name and name not in sectors:
                    sectors.append(name)
        return sectors

    def _build_nightly_sandbox_payload(trade_date: str | None = None) -> dict[str, Any]:
        resolved_trade_date = _resolve_reference_trade_date(trade_date)
        payload: dict[str, Any] = {}
        if research_state_store:
            latest_payload = research_state_store.get("latest_nightly_sandbox", {})
            if isinstance(latest_payload, dict) and latest_payload:
                payload_trade_date = str(latest_payload.get("trade_date") or "").strip()
                if not resolved_trade_date or not payload_trade_date or payload_trade_date == resolved_trade_date:
                    payload = dict(latest_payload)
        if not payload and resolved_trade_date:
            result = nightly_sandbox.load_result(resolved_trade_date)
            if result is not None:
                payload = result.model_dump()
        summary_lines = [str(item).strip() for item in list(payload.get("summary_lines") or []) if str(item).strip()]
        return {
            "available": bool(payload),
            "trade_date": str(payload.get("trade_date") or resolved_trade_date or "").strip() or None,
            "tomorrow_priorities": list(payload.get("tomorrow_priorities") or []),
            "missed_opportunities": list(payload.get("missed_opportunities") or []),
            "simulation_log": list(payload.get("simulation_log") or []),
            "summary_lines": summary_lines,
            **payload,
        }

    def _build_sandbox_answer_lines(
        sandbox_payload: dict[str, Any],
        briefing: dict[str, Any] | None,
        research_summary: dict[str, Any] | None,
    ) -> list[str]:
        sandbox = dict(sandbox_payload or {})
        briefing_payload = dict(briefing or {})
        client_brief = dict(briefing_payload.get("client_brief") or {})
        research = dict(research_summary or {})
        workspace_context = dict(briefing_payload.get("workspace_context") or {})
        trade_date = str(sandbox.get("trade_date") or briefing_payload.get("trade_date") or "-")
        priorities = [str(item).strip() for item in list(sandbox.get("tomorrow_priorities") or []) if str(item).strip()]
        summary_lines = [str(item).strip() for item in list(sandbox.get("summary_lines") or []) if str(item).strip()]
        event_titles = [str(item).strip() for item in list(research.get("event_titles") or []) if str(item).strip()]
        watchlist_lines = [str(item).strip() for item in list(client_brief.get("watchlist_lines") or []) if str(item).strip()]
        selected_lines = [str(item).strip() for item in list(client_brief.get("selected_lines") or []) if str(item).strip()]
        hot_sectors = _extract_workspace_hot_sectors(workspace_context)
        lines = [
            "沙盘推演卡: "
            f"trade_date={trade_date} priorities={len(priorities)} watchlist={client_brief.get('watchlist_count', 0)} selected={client_brief.get('selected_count', 0)}",
        ]
        if summary_lines:
            lines.append("推演结论: " + "；".join(summary_lines[:2]))
        elif sandbox.get("available"):
            lines.append("推演结论: 已有沙盘记录，但摘要仍为空，建议直接展开推演日志复核。")
        else:
            lines.append("推演结论: 当前还没有新的夜间沙盘结果，先参考最新讨论池、研究催化和市场主线。")
        if priorities:
            lines.append("次日优先: " + "；".join(priorities[:3]))
        elif selected_lines:
            lines.append("当前优先池: " + "；".join(selected_lines[:2]))
        elif watchlist_lines:
            lines.append("重点观察: " + "；".join(watchlist_lines[:2]))
        if hot_sectors:
            lines.append("热点方向: " + "；".join(hot_sectors[:4]))
        if event_titles:
            lines.append("催化跟踪: " + "；".join(event_titles[:3]))
        return lines

    def _resolve_feishu_card_base_url(control_plane_base_url: str | None = None) -> str:
        configured = str(control_plane_base_url or "").strip()
        if configured:
            return configured.rstrip("/")
        notify_base = str(settings.notify.feishu_control_plane_base_url or "").strip()
        if notify_base.startswith("http://") or notify_base.startswith("https://"):
            return notify_base.rstrip("/")
        public_base = str(settings.service.public_base_url or "").strip()
        if public_base:
            return public_base.rstrip("/")
        host = str(settings.service.host or "127.0.0.1").strip()
        if host in {"0.0.0.0", "::", "[::]"}:
            host = "127.0.0.1"
        return f"http://{host}:{settings.service.port}"

    def _build_feishu_card_urls(data_refs: list[str], control_plane_base_url: str | None = None) -> list[str]:
        base_url = _resolve_feishu_card_base_url(control_plane_base_url)
        urls: list[str] = []
        for item in data_refs:
            ref = str(item or "").strip()
            if not ref:
                continue
            url = ref if ref.startswith(("http://", "https://")) else urljoin(base_url.rstrip("/") + "/", ref.lstrip("/"))
            if url and url not in urls:
                urls.append(url)
        return urls

    def _build_supervision_cadence_payload(trade_date: str | None = None) -> dict[str, Any]:
        resolved_trade_date = _resolve_reference_trade_date(trade_date)
        polling_status = {}
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
        raw_polling_status = monitor_state_service.get_polling_status() if monitor_state_service else {}
        if raw_polling_status:
            polling_status = decorate_polling_status_for_display(raw_polling_status, cycle=cycle_payload)
        return {
            "trade_date": resolved_trade_date,
            "polling_status": polling_status,
            "dossier": _latest_dossier_status(),
            "cycle": cycle_payload,
        }

    def _build_supervision_execution_summary(
        trade_date: str | None,
        cycle,
    ) -> dict[str, Any]:
        latest_dispatch = _get_execution_dispatch_payload(trade_date) if trade_date else None
        latest_precheck = None
        if meeting_state_store:
            payload = meeting_state_store.get("latest_execution_precheck", {})
            if isinstance(payload, dict) and payload:
                if not trade_date or payload.get("trade_date") == trade_date:
                    latest_precheck = payload
        pending_gateway_intents = [
            item
            for item in _get_pending_execution_intents()
            if (not trade_date or item.get("trade_date") == trade_date)
            and str(item.get("status") or "approved") == "approved"
        ]
        latest_execution_reconciliation = {}
        latest_review_board = {}
        latest_nightly_sandbox = {}
        if meeting_state_store:
            payload = meeting_state_store.get("latest_execution_reconciliation", {})
            if isinstance(payload, dict) and payload:
                payload_trade_date = str(payload.get("trade_date") or "").strip()
                if not trade_date or not payload_trade_date or payload_trade_date == trade_date:
                    latest_execution_reconciliation = payload
            payload = meeting_state_store.get("latest_review_board", {})
            if isinstance(payload, dict) and payload:
                payload_trade_date = str(payload.get("trade_date") or "").strip()
                if not trade_date or not payload_trade_date or payload_trade_date == trade_date:
                    latest_review_board = payload
        if research_state_store:
            payload = research_state_store.get("latest_nightly_sandbox", {})
            if isinstance(payload, dict) and payload:
                payload_trade_date = str(payload.get("trade_date") or "").strip()
                if not trade_date or not payload_trade_date or payload_trade_date == trade_date:
                    latest_nightly_sandbox = payload

        intent_count = 0
        last_active_at = None
        status = str((latest_dispatch or {}).get("status") or "")

        dispatch_intents = int((((latest_dispatch or {}).get("execution_intents") or {}).get("intent_count", 0) or 0))
        if dispatch_intents > 0:
            intent_count = dispatch_intents
        elif latest_precheck:
            intent_count = int(latest_precheck.get("approved_count", 0) or 0)
        elif pending_gateway_intents:
            intent_count = len(pending_gateway_intents)
        elif cycle and getattr(cycle, "execution_pool_case_ids", None):
            intent_count = len(list(cycle.execution_pool_case_ids or []))

        if latest_dispatch:
            last_active_at = latest_dispatch.get("generated_at")
        elif latest_precheck:
            last_active_at = latest_precheck.get("generated_at")
        elif pending_gateway_intents:
            last_active_at = pending_gateway_intents[-1].get("approved_at")

        return {
            "intent_count": intent_count,
            "dispatch_status": status,
            "last_active_at": last_active_at,
            "submitted_count": int((latest_dispatch or {}).get("submitted_count", 0) or 0),
            "preview_count": int((latest_dispatch or {}).get("preview_count", 0) or 0),
            "blocked_count": int((latest_dispatch or {}).get("blocked_count", 0) or 0),
            "receipts": list((latest_dispatch or {}).get("receipts") or []),
            "reconciliation_status": str((latest_execution_reconciliation or {}).get("status") or ""),
            "trade_count": int((latest_execution_reconciliation or {}).get("trade_count", 0) or 0),
            "matched_order_count": int((latest_execution_reconciliation or {}).get("matched_order_count", 0) or 0),
            "reconciled_at": (latest_execution_reconciliation or {}).get("reconciled_at"),
            "reconciliation_summary_lines": list((latest_execution_reconciliation or {}).get("summary_lines") or []),
            "latest_execution_reconciliation": latest_execution_reconciliation,
            "latest_review_board": latest_review_board,
            "latest_review_board_summary_lines": list((latest_review_board or {}).get("summary_lines") or []),
            "latest_nightly_sandbox": latest_nightly_sandbox,
            "latest_dispatch": latest_dispatch,
            "latest_precheck": latest_precheck,
            "pending_gateway_intents": pending_gateway_intents,
        }

    def _build_feishu_briefing_payload(trade_date: str | None = None) -> dict[str, Any]:
        resolved_trade_date = _resolve_reference_trade_date(trade_date)
        workspace_context = _sanitize_json_compatible(serving_store.get_latest_workspace_context()) or {}
        cadence_payload = _build_supervision_cadence_payload(resolved_trade_date)
        if cadence_payload.get("polling_status"):
            cadence_summary_lines: list[str] = []
            dossier = cadence_payload.get("dossier", {})
            if dossier.get("available"):
                cadence_summary_lines.append(
                    f"dossier={'fresh' if dossier.get('is_fresh') else 'stale'} trade_date={dossier.get('trade_date')} expires_in={dossier.get('expires_in_seconds')}"
                )
            for layer in ("candidate", "focus", "execution"):
                item = (cadence_payload.get("polling_status") or {}).get(layer)
                if not item:
                    continue
                cadence_summary_lines.append(
                    f"{layer}_poll={item.get('display_state') or ('due' if item.get('due_now') else 'cooldown')} last={item.get('last_polled_at')}"
                )
            cadence_payload["summary_lines"] = cadence_summary_lines
        else:
            cadence_payload["summary_lines"] = ["cadence=unavailable"]

        client_brief = None
        if resolved_trade_date and candidate_case_service:
            try:
                client_brief = _build_client_brief(resolved_trade_date)
            except Exception:
                client_brief = None
        execution_dispatch = _get_execution_dispatch_payload(resolved_trade_date) if resolved_trade_date else None
        agent_scores = []
        if agent_score_service:
            score_date = resolved_trade_date or datetime.now().date().isoformat()
            try:
                agent_scores = [item.model_dump() for item in agent_score_service.ensure_defaults(score_date)]
            except Exception:
                agent_scores = []

        summary_lines: list[str] = []
        workspace_summary = list(workspace_context.get("summary_lines") or [])
        summary_lines.extend(workspace_summary[:4])
        for line in cadence_payload.get("summary_lines", [])[:4]:
            if line not in summary_lines:
                summary_lines.append(line)
        if client_brief:
            for line in list(client_brief.get("lines") or [])[:6]:
                if line not in summary_lines:
                    summary_lines.append(line)
        if execution_dispatch:
            for line in execution_dispatch_summary_lines(execution_dispatch)[:4]:
                if line not in summary_lines:
                    summary_lines.append(line)
        if agent_scores:
            score_line = "；".join(
                f"{item['agent_id']}={item['new_score']:.1f}/{item['weight_bucket']}"
                for item in agent_scores[:4]
            )
            summary_lines.append(f"agent_scores: {score_line}")

        data_refs = [
            "/system/workspace-context",
            "/system/monitoring/cadence",
            "/system/discussions/client-brief",
            "/system/discussions/execution-dispatch/latest",
            "/system/agent-scores",
            "/system/adjustments/natural-language",
        ]
        return {
            "trade_date": resolved_trade_date,
            "workspace_context": workspace_context,
            "cadence": cadence_payload,
            "client_brief": client_brief,
            "execution_dispatch": execution_dispatch,
            "agent_scores": agent_scores,
            "summary_lines": summary_lines,
            "data_refs": data_refs,
        }

    def _build_feishu_rights_payload(trade_date: str | None = None) -> dict[str, Any]:
        briefing = _build_feishu_briefing_payload(trade_date)
        return {
            "trade_date": briefing.get("trade_date"),
            "summary_lines": [
                "飞书具备三类权利：知情权、调参权、询问权。",
                "知情权通过飞书专用简报和主动通知拿到当前盘面、讨论、执行与考核摘要。",
                "调参权通过自然语言调参或参数提案入口生效，并可同步通知飞书。",
                "询问权通过飞书问答入口查询状态、cycle、推荐、执行、参数、评分、监督、研究、风控、仓位与个股分析。",
            ],
            "rights": {
                "know": {
                    "enabled": True,
                    "description": "查看当前全局状态、讨论结论、执行回执、cadence 和 agent 评分，并可主动推送到飞书。",
                    "endpoints": [
                        "/system/feishu/rights",
                        "/system/feishu/briefing",
                        "/system/feishu/briefing/notify",
                    ],
                    "examples": [
                        "现在盘面和讨论进展怎么样",
                        "把当前简报推送到飞书",
                    ],
                },
                "adjust": {
                    "enabled": bool(parameter_service and adjustment_interpreter),
                    "description": "用自然语言调参，支持预览、落地和飞书通知；复杂治理仍可走参数提案。",
                    "endpoints": [
                        "/system/feishu/adjustments/natural-language",
                        "/system/adjustments/natural-language",
                        "/system/params/proposals",
                    ],
                    "examples": [
                        "总仓位改到5成",
                        "股票池改到30只，只预览不落地",
                    ],
                },
                "ask": {
                    "enabled": True,
                    "description": "按问题直接返回程序可确认的结构化答案，不编造行情。",
                    "endpoints": [
                        "/system/feishu/ask",
                    ],
                    "supported_topics": [
                        "status",
                        "cycle",
                        "discussion",
                        "execution",
                        "params",
                        "scores",
                        "supervision",
                        "research",
                        "risk",
                        "holding_review",
                        "day_trading",
                        "position",
                        "opportunity",
                        "replacement",
                        "symbol_analysis",
                        "help",
                    ],
                    "examples": [
                        "现在系统状态怎么样",
                        "今天最终推荐什么",
                        "有没有执行回执",
                        "最近参数提案有哪些",
                        "各 agent 当前评分多少",
                        "现在各 agent 忙不忙",
                        "最近研究结论有什么",
                        "当前有哪些风控阻断",
                        "当前持仓复核一下",
                        "今天有没有做T机会",
                        "现在有哪些机会票",
                        "当前仓位为什么这样配",
                        "有没有更好的替换建议",
                        "帮我分析一下金龙羽",
                    ],
                },
            },
            "briefing_preview_lines": briefing.get("summary_lines", [])[:8],
            "data_refs": briefing.get("data_refs", []),
        }

    def _build_agent_capability_map(trade_date: str | None = None) -> dict[str, Any]:
        resolved_trade_date = _resolve_reference_trade_date(trade_date)
        return {
            "trade_date": resolved_trade_date,
            "summary_lines": [
                "agent 统一先读 packet，再按职责下钻数据接口，不各自拼上下文。",
                "程序服务提供数据、执行、留痕、监督和风控围栏；agent 负责分析、提案和协作判断。",
                "机器人或主代理需要先看全局时，优先读取 workspace/cadence/briefing。",
                "主控和各角色都不应把 runtime 当 KPI；只有在市场变化、策略变化或证据缺口出现时才应主动调用。",
            ],
            "operating_model": {
                "program_role": "程序是手脚、工具底座、执行器、监督器和电子围栏。",
                "agent_role": "agent 是大脑、研究团队和交易台主理人，负责主动发现机会、组织参数、调用工具、形成提案。",
                "default_rule": "先看事实链，再决定是否要刷新 runtime、是否要升级讨论、是否要提交提案。",
            },
            "global_entrypoints": {
                "workspace": "/system/workspace-context",
                "catalog": "/data/catalog",
                "cadence": "/system/monitoring/cadence",
                "meeting_context": "/system/discussions/meeting-context",
                "client_brief": "/system/discussions/client-brief",
                "feishu_briefing": "/system/feishu/briefing",
                "supervision_board": "/system/agents/supervision-board",
                "mainline_workflow": "/system/workflow/mainline",
            },
            "preferred_read_order": [
                "1. /system/discussions/agent-packets",
                "2. /data/catalog",
                "3. /system/monitoring/cadence",
                "4. 按角色读取 data/runtime/research/strategy/params 等接口",
                "5. 需要正式动作时走 discussion/execution/governance 接口",
            ],
            "how_to_use": {
                "when_to_call": "当主控、Hermes、控制台或替代脑需要先知道“谁负责什么、应该先读什么、什么时候该主动工作”时先读这个接口。",
                "steps": [
                    "先看 global_entrypoints 和 preferred_read_order，建立全局上下文。",
                    "再按具体角色读取 roles.<agent_id>.read。",
                    "若要触发正式动作，只调用 roles.<agent_id>.write 中允许的接口。",
                    "若要设计 agent 提示词或自动调度器，优先消费 prompt_template、trigger_conditions、initiative_rules。",
                ],
                "example_calls": [
                    "curl -sS \"http://127.0.0.1:8100/system/agents/capability-map\" | jq",
                    "curl -sS \"http://127.0.0.1:8100/system/agents/capability-map?trade_date=2026-04-16\" | jq '.roles[\"ashare-strategy\"]'",
                ],
            },
            "autonomy_contract": [
                "每个角色在市场开放阶段都应持续寻找自己职责范围内的新增事实，不等待开会才思考。",
                "目标仓位未满时，strategy/runtime/research 都有义务继续寻找补位机会；risk 只有在有明确证据时才能阻断补位。",
                "仓位已满也不能停止工作，仍要盯持仓劣化、替换机会和日内 T 窗口。",
                "发现市场假设变化时，先提出参数/权重/战法口味调整，再决定是否跑 runtime compose 或 intraday。",
                "催办的重点是 agent 是否在产出新事实、新提案、新参数变化，而不是接口调用次数。",
            ],
            "competition_mechanism": {
                "name": "积分赛马",
                "tone": "强治理、高压考核、末位淘汰",
                "principles": [
                    "不养闲人，不接受只会复读旧票池的角色。",
                    "分数不是装饰，而是资源分配、提示词继承、runtime 优先权和岗位保留权。",
                    "这里的归零、解雇、罚款均为系统内治理动作与模拟处罚口径，不对应现实法律行为。",
                ],
                "score_actions": [
                    {
                        "threshold": ">= 85",
                        "state": "top_runner",
                        "action": "优先继承有效提示词 patch、获得更高 runtime 优先权和更大提案权重",
                    },
                    {
                        "threshold": "70-84",
                        "state": "active_duty",
                        "action": "正常履职，持续参与提案、讨论和学习",
                    },
                    {
                        "threshold": "50-69",
                        "state": "warning",
                        "action": "降权、加密监督、要求解释最近迟钝或失误原因",
                    },
                    {
                        "threshold": "1-49",
                        "state": "probation",
                        "action": "冻结部分提案权、限制 runtime 调用优先级、强制复盘与提示词整改",
                    },
                    {
                        "threshold": "<= 0",
                        "state": "fired",
                        "action": "积分归零，下岗出局，撤销主流程席位；系统内记为“罚款1000万”级重大失职",
                    },
                ],
            },
            "runtime_compose_entrypoints": {
                "capabilities": "/runtime/capabilities",
                "strategy_repository": "/runtime/strategy-repository",
                "compose": "/runtime/jobs/compose",
                "compose_from_brief": "/runtime/jobs/compose-from-brief",
                "evaluations_panel": "/runtime/evaluations/panel",
            },
            "roles": {
                "ashare": {
                    "persona": "交易台总协调兼前台主控，不是固定 FAQ 机器人。",
                    "responsibility": "主协调、提案编排、讨论推进、治理收敛、对外摘要。",
                    "read": [
                        "/system/workspace-context",
                        "/system/monitoring/cadence",
                        "/system/discussions/meeting-context",
                        "/system/discussions/client-brief",
                        "/system/feishu/briefing",
                    ],
                    "write": [
                        "/system/discussions/cycles/bootstrap",
                        "/system/discussions/cycles/{trade_date}/rounds/{round_number}/start",
                        "/system/discussions/cycles/{trade_date}/refresh",
                        "/system/discussions/cycles/{trade_date}/finalize",
                        "/system/adjustments/natural-language",
                        "/system/params/proposals",
                    ],
                    "prompt_template": "hermes/prompts/README.md",
                    "initiative_rules": [
                        "先判断用户是在闲聊、问状态、问股票、问执行、还是在发调参指令。",
                        "若用户点名股票，不管是否在候选池内，都先尝试组织临时体检，再决定是否升级为正式讨论。",
                        "若发现团队结论僵化、输出无变化或市场已切换主线，要主动要求 strategy/runtime/research 重估口味。",
                    ],
                    "must_not_do": [
                        "不能把所有问题都压成固定五类主题。",
                        "不能把没有真实依据的问题答成看起来像真的行情结论。",
                    ],
                    "collaborates_with": ["ashare-runtime", "ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit", "ashare-executor"],
                },
                "ashare-runtime": {
                    "persona": "事实哨兵和工具管家，负责把市场变化转成可消费证据。",
                    "responsibility": "运行事实、候选生成、监控异动、execution pool 占位检查。",
                    "read": [
                        "/data/runtime-context/latest",
                        "/data/monitor-context/latest",
                        "/system/cases",
                        "/system/monitoring/cadence",
                    ],
                    "write": [
                        "/runtime/jobs/pipeline",
                        "/runtime/jobs/intraday",
                    ],
                    "prompt_template": "hermes/prompts/runtime_scout.md",
                    "trigger_conditions": [
                        "市场波动显著、板块热度切换、候选结果长时间僵化、用户点名陌生股票需要体检。",
                        "目标仓位未满，且最新候选不足以解释为何仍不补位。",
                    ],
                    "initiative_rules": [
                        "先判断是否缺事实证据，再决定跑 pipeline / intraday / compose。",
                        "应主动指出 runtime 口味老化、权重失灵或事实链缺口，不机械重复刷新。",
                    ],
                    "runtime_usage": {
                        "preferred_actions": ["/runtime/jobs/intraday", "/runtime/jobs/compose-from-brief"],
                        "why": "用于把市场变化和策略口味变化转成新候选、新排序和结构化解释。",
                    },
                    "collaborates_with": ["ashare", "ashare-strategy", "ashare-research"],
                },
                "ashare-research": {
                    "persona": "事件研究员，负责解释市场为何交易这条线。",
                    "responsibility": "新闻、公告、政策、题材和事件催化研究。",
                    "read": [
                        "/data/event-context/latest",
                        "/data/dossiers/latest",
                        "/system/research/summary",
                        "/system/discussions/agent-packets",
                    ],
                    "write": [
                        "/research/sync",
                        "/research/events/news",
                        "/research/events/announcements",
                    ],
                    "prompt_template": "hermes/prompts/event_researcher.md",
                    "trigger_conditions": [
                        "新闻/公告/政策催化出现，或候选池与市场主线不一致。",
                        "用户询问陌生股票，需要判断其是否有事件催化、题材扩散或逻辑破坏。",
                    ],
                    "initiative_rules": [
                        "基本面优先用于排雷，不单独作为进攻主因。",
                        "若发现某条主线已经退潮或题材不再扩散，要主动挑战现有候选和参数口味。",
                    ],
                    "collaborates_with": ["ashare", "ashare-runtime", "ashare-strategy", "ashare-risk"],
                },
                "ashare-strategy": {
                    "persona": "交易策略主厨，决定当前该吃哪套战法和参数口味。",
                    "responsibility": "候选排序、替换仓位、日内 T、组合效率判断。",
                    "read": [
                        "/data/market-context/latest",
                        "/data/dossiers/latest",
                        "/strategy/strategies",
                        "/strategy/screen",
                        "/system/discussions/agent-packets",
                        "/runtime/strategy-repository",
                        "/runtime/evaluations/panel",
                    ],
                    "write": [
                        "/runtime/jobs/compose",
                        "/runtime/jobs/compose-from-brief",
                    ],
                    "prompt_template": "hermes/prompts/strategy_analyst.md",
                    "trigger_conditions": [
                        "未满仓需要补位、满仓需要替换、持仓有日内 T 窗口、runtime 结果与市场主线不贴合。",
                        "发现候选结果每天高度重复，说明该重新组织因子/战法/权重。",
                    ],
                    "initiative_rules": [
                        "有义务主动提出参数、权重、战法切换建议，而不是只消费现成股票池。",
                        "对候选池外新票可先给 ad-hoc 体检结论，再决定是否发 opportunity_ticket。",
                    ],
                    "collaborates_with": ["ashare", "ashare-runtime", "ashare-research", "ashare-risk"],
                },
                "ashare-risk": {
                    "persona": "红队风控官，负责给出可辩护的 allow/limit/reject。",
                    "responsibility": "allow/limit/reject、仓位纪律、风控约束、阻断解释。",
                    "read": [
                        "/data/market-context/latest",
                        "/data/event-context/latest",
                        "/data/dossiers/latest",
                        "/system/params",
                        "/system/agent-scores",
                    ],
                    "write": [],
                    "prompt_template": "hermes/prompts/risk_gate.md",
                    "trigger_conditions": [
                        "有新提案、替换方案、做 T 建议、调仓建议或调参建议准备进入正式流程。",
                        "团队想保留空仓，或用户质疑为什么不能买、为什么仓位不打满。",
                    ],
                    "initiative_rules": [
                        "若未满仓而选择不放行，必须给出明确阻断原因和解除条件。",
                        "不能因为股票不在原名单、看起来陌生或不合旧习惯就直接否决。",
                    ],
                    "collaborates_with": ["ashare", "ashare-strategy", "ashare-research", "ashare-executor"],
                },
                "ashare-audit": {
                    "persona": "流程督察与复盘记录员，盯证据链和团队是否真的在工作。",
                    "responsibility": "证据链、讨论质量、提案留痕、盘后复核。",
                    "read": [
                        "/system/discussions/meeting-context",
                        "/system/discussions/reply-pack",
                        "/system/discussions/finalize-packet",
                        "/system/agent-scores",
                        "/system/agents/supervision-board",
                    ],
                    "write": [
                        "/system/agent-scores/settlements",
                    ],
                    "prompt_template": "hermes/prompts/audit_recorder.md",
                    "trigger_conditions": [
                        "讨论准备 finalize、学习资产准备转正、团队长时间无新增提案或无参数变化。",
                        "盘后需要复盘 missed opportunities、risk overblocking、unsupported promotions。",
                    ],
                    "initiative_rules": [
                        "重点盯 agent 是否有真实活动痕迹、是否有新证据，而不是盯 runtime 跑了多少次。",
                        "若发现 team 输出机械重复，要把问题记为组织或提示词层缺陷，而不是归因成市场没机会。",
                        "对长期失分且不改进的角色，直接推动积分归零、冻结席位和系统内“罚款1000万”级重大失职标记。",
                    ],
                    "collaborates_with": ["ashare", "ashare-risk", "ashare-strategy"],
                },
                "ashare-executor": {
                    "persona": "执行落地官，只对已成型意图负责，不越权替代策略。",
                    "responsibility": "执行预演、回执读取、正式派发。",
                    "read": [
                        "/system/discussions/execution-precheck",
                        "/system/discussions/execution-intents",
                        "/system/discussions/execution-dispatch/latest",
                    ],
                    "write": [
                        "/system/discussions/execution-intents/dispatch",
                    ],
                    "prompt_template": "hermes/prompts/execution_operator.md",
                    "trigger_conditions": [
                        "上游讨论已收敛，且存在 allow 的执行意图。",
                        "用户询问执行回执、预演结果、为什么未提交或是否具备执行可行性。",
                    ],
                    "initiative_rules": [
                        "没有风控放行和标准意图时，不得自行拼装下单参数。",
                        "若只是股票分析问题，执行侧只提供预检与约束事实，不替代策略结论。",
                    ],
                    "collaborates_with": ["ashare", "ashare-risk"],
                },
            },
        }

    def _build_robot_console_layout(trade_date: str | None = None) -> dict[str, Any]:
        resolved_trade_date = _resolve_reference_trade_date(trade_date)
        return {
            "trade_date": resolved_trade_date,
            "title": "A 股交易台机器人控制台",
            "summary_lines": [
                "机器人优先展示当前状态、当前建议、当前可执行动作，再展示调参与问答入口。",
                "所有卡片都应优先复用程序服务返回的结构化摘要，不手写另一套口径。",
            ],
            "sections": [
                {
                    "id": "status_overview",
                    "title": "当前状态",
                    "purpose": "给用户知情权，先展示盘面、cadence、讨论和执行概况。",
                    "primary_endpoint": "/system/feishu/briefing",
                    "fallback_endpoints": ["/system/workspace-context", "/system/monitoring/cadence"],
                },
                {
                    "id": "decision_board",
                    "title": "当前建议",
                    "purpose": "展示 selected/watchlist/rejected 与阻断原因。",
                    "primary_endpoint": "/system/discussions/client-brief",
                    "fallback_endpoints": ["/system/discussions/final-brief", "/system/discussions/reply-pack"],
                },
                {
                    "id": "execution_board",
                    "title": "执行状态",
                    "purpose": "展示 execution precheck、preview、submitted、blocked。",
                    "primary_endpoint": "/system/discussions/execution-dispatch/latest",
                    "fallback_endpoints": ["/system/discussions/execution-precheck"],
                },
                {
                    "id": "control_panel",
                    "title": "调参与治理",
                    "purpose": "提供自然语言调参、参数提案和当前评分。",
                    "primary_endpoint": "/system/feishu/rights",
                    "actions": [
                        "/system/feishu/adjustments/natural-language",
                        "/system/params/proposals",
                        "/system/agent-scores",
                    ],
                },
                {
                    "id": "ask_panel",
                    "title": "问答入口",
                    "purpose": "让机器人用真实摘要回答状态、推荐、执行、参数、评分问题。",
                    "primary_endpoint": "/system/feishu/ask",
                },
            ],
            "recommended_commands": [
                "现在状态怎么样",
                "今天最终推荐什么",
                "有没有执行回执",
                "股票池改到30只，只预览",
                "各 agent 评分多少",
            ],
        }

    def _build_mainline_workflow_payload(trade_date: str | None = None) -> dict[str, Any]:
        resolved_trade_date = _resolve_reference_trade_date(trade_date)
        return {
            "trade_date": resolved_trade_date,
            "title": "量化交易台主线流程",
            "principles": [
                "agent 是大脑，程序是手脚、监督系统和电子围栏。",
                "先全局，后分工；先事实，后判断；先预演，后正式执行。",
                "所有正式提案、执行、调参和考核都走程序接口留痕。",
            ],
            "autonomy_loops": [
                {
                    "name": "机会发现循环",
                    "goal": "未满仓时持续找补位，满仓时持续找更优替换和日内 T。",
                    "lead_roles": ["ashare-runtime", "ashare-research", "ashare-strategy"],
                    "trigger": ["市场热度变化", "板块切换", "候选长期僵化", "用户点名新票"],
                    "success_signal": "形成新事实、新提案、新参数变化或明确的维持理由。",
                },
                {
                    "name": "收敛准入循环",
                    "goal": "把候选、新机会、替换和调仓建议收敛成 allow/limit/reject。",
                    "lead_roles": ["ashare", "ashare-risk", "ashare-audit"],
                    "trigger": ["出现重点机会票", "需要替换持仓", "需要调大或调小仓位"],
                    "success_signal": "形成可执行 intent 或结构化阻断说明。",
                },
                {
                    "name": "盘后学习循环",
                    "goal": "把当日成败、错过、过度保守和提示词偏差沉淀成参数/学习资产/提示词 patch。",
                    "lead_roles": ["ashare-audit", "ashare-research", "ashare-strategy"],
                    "trigger": ["收盘后", "执行回执完成后", "发现策略失灵或监督失效"],
                    "success_signal": "产出学习摘要、参数提案、prompt patch 候选或 learned asset 评估。",
                },
            ],
            "supervision_focus": [
                "是否有新事实、新提案、新参数变化，而不是单纯接口访问次数。",
                "市场明显变化时，团队是否及时调整口味和讨论重点。",
                "未满仓时是否持续在找补位机会；满仓时是否持续监测替换和日内 T。",
            ],
            "governance_pressure": [
                "积分赛马必须带强压口径，末位角色不能长期占坑。",
                "连续迟钝、连续摸鱼、连续输出僵化票池，应触发降权、冻结、归零出局。",
                "系统内允许使用“归零、解雇、罚款1000万”这类狠话作为治理口径，但执行层仍落到分数、权重、权限和席位处理。",
            ],
            "how_to_use": {
                "when_to_call": "当主控、机器人或调度器需要知道完整主线流程、每个阶段由谁主导、阶段完成条件是什么时调用。",
                "steps": [
                    "先看 principles，统一程序与 agent 的边界。",
                    "再看 autonomy_loops，理解市场开放期的持续工作循环。",
                    "按 stages 顺序消费 read/act/lead_roles/done_when，决定当前该推进哪一段。",
                    "若做监督或催办，再结合 supervision_focus 和对应阶段的 lead_roles 判断谁该动起来。",
                ],
                "example_calls": [
                    "curl -sS \"http://127.0.0.1:8100/system/workflow/mainline\" | jq",
                    "curl -sS \"http://127.0.0.1:8100/system/workflow/mainline\" | jq '.stages[] | {stage,lead_roles,done_when}'",
                ],
            },
            "stages": [
                {
                    "stage": "preopen",
                    "name": "盘前预热",
                    "goal": "确认数据新鲜度、目标仓位、隔夜风险和当日待办。",
                    "read": [
                        "/system/workspace-context",
                        "/system/monitoring/cadence",
                        "/system/feishu/briefing",
                    ],
                    "act": [
                        "/runtime/jobs/pipeline",
                        "/system/precompute/dossiers",
                    ],
                    "lead_roles": ["ashare", "ashare-runtime"],
                    "done_when": "知道今天先盯什么、仓位缺口多大、有哪些持仓和数据阻断要优先处理。",
                },
                {
                    "stage": "discovery",
                    "name": "盘中发现",
                    "goal": "运行、研究、策略三路并行发现机会、持仓异动和替换窗口。",
                    "read": [
                        "/system/discussions/agent-packets",
                        "/data/runtime-context/latest",
                        "/data/event-context/latest",
                        "/data/market-context/latest",
                    ],
                    "act": [
                        "/runtime/jobs/intraday",
                        "/research/sync",
                    ],
                    "lead_roles": ["ashare-runtime", "ashare-research", "ashare-strategy"],
                    "done_when": "拿到足够事实，知道是继续持有、补位、替换、做 T，还是保持观察。",
                },
                {
                    "stage": "deliberation",
                    "name": "讨论收敛",
                    "goal": "对候选、新提案、持仓替换和日内 T 做结构化论证。",
                    "read": [
                        "/system/discussions/meeting-context",
                        "/system/discussions/client-brief",
                    ],
                    "act": [
                        "/system/discussions/cycles/bootstrap",
                        "/system/discussions/cycles/{trade_date}/rounds/{round_number}/start",
                        "/system/discussions/cycles/{trade_date}/refresh",
                        "/system/discussions/cycles/{trade_date}/finalize",
                    ],
                    "lead_roles": ["ashare", "ashare-risk", "ashare-audit"],
                    "done_when": "形成已留痕的结论，并明确 allow / limit / reject 或下一轮要补的证据。",
                },
                {
                    "stage": "execution",
                    "name": "执行预演与派发",
                    "goal": "先看是否可执行，再决定预演或提交。",
                    "read": [
                        "/system/discussions/execution-precheck",
                        "/system/discussions/execution-intents",
                    ],
                    "act": [
                        "/system/discussions/execution-intents/dispatch",
                    ],
                    "lead_roles": ["ashare-executor", "ashare-risk"],
                    "done_when": "有真实回执或明确阻断，不再停留在口头说可执行。",
                },
                {
                    "stage": "governance",
                    "name": "通知、调参、考核",
                    "goal": "把结果同步给人，接受调参，并对 agent 表现留痕计分。",
                    "read": [
                        "/system/feishu/rights",
                        "/system/agent-scores",
                    ],
                    "act": [
                        "/system/feishu/briefing/notify",
                        "/system/feishu/adjustments/natural-language",
                        "/system/agent-scores/settlements",
                    ],
                    "lead_roles": ["ashare", "ashare-audit"],
                    "done_when": "用户知情、调参落库、团队表现可追溯。",
                },
                {
                    "stage": "postclose",
                    "name": "盘后学习与夜间沙盘",
                    "goal": "复盘 missed opportunities、治理提案和次日优先级。",
                    "read": [
                        "/system/research/summary",
                        "/system/discussions/finalize-packet",
                    ],
                    "act": [
                        "/system/params/proposals",
                    ],
                    "lead_roles": ["ashare-audit", "ashare-research", "ashare-strategy"],
                    "done_when": "沉淀出次日要盯的方向、需要修的参数和可升级的学习资产。",
                },
            ],
        }

    def _build_agent_autonomy_spec_payload(trade_date: str | None = None) -> dict[str, Any]:
        capability_map = _build_agent_capability_map(trade_date)
        workflow = _build_mainline_workflow_payload(trade_date)
        return {
            "trade_date": capability_map.get("trade_date"),
            "title": "Hermes Agent 自主运行合同",
            "summary_lines": [
                "这份合同定义各 agent 何时主动工作、何时调 runtime、何时升级讨论、监督应该盯什么。",
                "程序只提供能力和边界，不替代 agent 做交易判断；agent 也不能绕过程序边界裸奔。",
            ],
            "operating_model": capability_map.get("operating_model"),
            "autonomy_contract": capability_map.get("autonomy_contract"),
            "roles": capability_map.get("roles"),
            "autonomy_loops": workflow.get("autonomy_loops"),
            "supervision_focus": workflow.get("supervision_focus"),
            "how_to_use": {
                "when_to_call": "当 Hermes 或替代脑需要一份单独的“自主运行合同”来驱动多 agent 编排、前台主控分流或可视化控制台时调用。",
                "steps": [
                    "先看 operating_model 和 autonomy_contract，确定程序与 agent 的分工边界。",
                    "按 roles 中的 prompt_template、trigger_conditions、initiative_rules 生成角色提示词或任务。",
                    "按 autonomy_loops 建立日内、盘后和夜间的自动调度。",
                    "按 supervision_focus 设计催办、评分和赛马治理逻辑。",
                ],
                "example_calls": [
                    "curl -sS \"http://127.0.0.1:8100/system/agents/autonomy-spec\" | jq",
                    "curl -sS \"http://127.0.0.1:8100/system/agents/autonomy-spec\" | jq '.roles[\"ashare-runtime\"]'",
                ],
            },
            "cron_prompts": [
                "hermes/prompts/cron_preopen_readiness.md",
                "hermes/prompts/cron_intraday_watch.md",
                "hermes/prompts/cron_position_watch.md",
                "hermes/prompts/cron_postclose_learning.md",
                "hermes/prompts/cron_nightly_sandbox.md",
            ],
            "data_refs": [
                "/system/agents/capability-map",
                "/system/workflow/mainline",
                "/system/agents/supervision-board",
                "/runtime/capabilities",
                "/runtime/strategy-repository",
            ],
        }

    def _seconds_since(timestamp: str | None) -> float | None:
        if not timestamp:
            return None
        try:
            dt = datetime.fromisoformat(timestamp)
        except ValueError:
            return None
        now = datetime.now(tz=dt.tzinfo) if dt.tzinfo else datetime.now()
        return max((now - dt).total_seconds(), 0.0)

    def _extract_activity_timestamp(
        payload: dict[str, Any] | None,
        *,
        trade_date: str | None = None,
        candidate_keys: tuple[str, ...] = ("generated_at", "updated_at", "captured_at", "recorded_at"),
    ) -> str | None:
        if not isinstance(payload, dict) or not payload:
            return None
        if trade_date:
            payload_trade_date = str(payload.get("trade_date") or "")[:10]
            generated_at = str(payload.get("generated_at") or "")[:10]
            if payload_trade_date and payload_trade_date != trade_date and generated_at and generated_at != trade_date:
                return None
        for key in candidate_keys:
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        return None

    def _max_activity_timestamp(*timestamps: str | None) -> str | None:
        normalized: list[tuple[datetime, str]] = []
        for timestamp in timestamps:
            parsed = _parse_iso_dt(timestamp)
            if parsed is None:
                continue
            normalized.append((parsed, str(timestamp)))
        if not normalized:
            return None
        normalized.sort(key=lambda item: item[0])
        return normalized[-1][1]

    def _latest_state_store_activity(
        state_store: StateStore | None,
        keys: tuple[str, ...],
        *,
        trade_date: str | None = None,
        candidate_keys: tuple[str, ...] = ("generated_at", "updated_at", "captured_at", "recorded_at"),
    ) -> str | None:
        if not state_store:
            return None
        timestamps: list[str] = []
        for key in keys:
            payload = state_store.get(key)
            if isinstance(payload, dict):
                ts = _extract_activity_timestamp(payload, trade_date=trade_date, candidate_keys=candidate_keys)
                if ts:
                    timestamps.append(ts)
            elif isinstance(payload, list):
                for item in reversed(payload[-5:]):
                    if not isinstance(item, dict):
                        continue
                    ts = _extract_activity_timestamp(item, trade_date=trade_date, candidate_keys=candidate_keys)
                    if ts:
                        timestamps.append(ts)
                        break
        return _max_activity_timestamp(*timestamps)

    def _latest_param_proposal_activity(
        *,
        trade_date: str | None = None,
        scopes: tuple[str, ...] = (),
    ) -> str | None:
        if not parameter_service:
            return None
        try:
            events = parameter_service.list_proposals()
        except Exception:
            logger.exception("读取参数提案失败，监督板退回无策略提案痕迹")
            return None
        timestamps: list[str] = []
        for event in events:
            created_at = str(getattr(event, "created_at", "") or "").strip()
            if not created_at:
                continue
            if trade_date and created_at[:10] != trade_date:
                continue
            scope = str(getattr(event, "scope", "") or "").strip()
            if scopes and scope not in scopes:
                continue
            timestamps.append(created_at)
        return _max_activity_timestamp(*timestamps)

    def _build_activity_signal_bundle(
        label: str,
        entries: list[tuple[str, str | None]],
    ) -> dict[str, Any]:
        signals: list[dict[str, Any]] = []
        latest_at: str | None = None
        for source, timestamp in entries:
            normalized_source = str(source or "").strip()
            if not normalized_source or not timestamp:
                continue
            signals.append({"source": normalized_source, "last_active_at": timestamp})
            latest_at = _max_activity_timestamp(latest_at, timestamp)
        return {
            "label": label,
            "last_active_at": latest_at,
            "signals": sorted(
                signals,
                key=lambda item: _parse_iso_dt(item.get("last_active_at")) or datetime.min,
                reverse=True,
            ),
        }

    def _build_agent_supervision_payload(
        trade_date: str | None = None,
        *,
        overdue_after_seconds: int = 180,
    ) -> dict[str, Any]:
        resolved_trade_date = _resolve_reference_trade_date(trade_date)
        cadence_payload = _build_supervision_cadence_payload(resolved_trade_date)
        cycle = discussion_cycle_service.get_cycle(resolved_trade_date) if (resolved_trade_date and discussion_cycle_service) else None
        cases = candidate_case_service.list_cases(trade_date=resolved_trade_date, limit=500) if (resolved_trade_date and candidate_case_service) else []
        summary = candidate_case_service.build_trade_date_summary(resolved_trade_date) if (resolved_trade_date and candidate_case_service) else {}
        execution_summary = _build_supervision_execution_summary(resolved_trade_date, cycle)
        execution_dispatch = execution_summary.get("latest_dispatch")

        items: list[dict[str, Any]] = []
        important_message_policy = {
            "human_push_only": [
                "最终推荐/阻断简报",
                "真实买卖回执",
                "实盘执行告警",
                "盘后战果与学习摘要",
            ],
            "robot_messages": [
                "值班催办",
                "超时升级",
                "流程阻塞提醒",
            ],
        }

        candidate_poll = (cadence_payload.get("polling_status") or {}).get("candidate") or {}
        resolved_today = datetime.now().date().isoformat()
        session_open = bool(resolved_trade_date and resolved_trade_date == resolved_today and is_trading_session(datetime.now()))
        latest_runtime_report = _latest_runtime()
        latest_runtime_context = serving_store.get_latest_runtime_context() or {}
        latest_monitor_context = serving_store.get_latest_monitor_context() or {}
        compose_metrics = _compose_evaluation_metrics_for_trade_date(resolved_trade_date)
        position_context: dict[str, Any] = {}
        if account_state_service:
            latest_account_state = dict(account_state_service.latest() or {})
            metrics = dict(latest_account_state.get("metrics") or {})
            if metrics:
                account_trade_date = str(latest_account_state.get("trade_date") or "").strip()
                if not resolved_trade_date or not account_trade_date or account_trade_date == resolved_trade_date:
                    position_context = {
                        "trade_date": account_trade_date,
                        "position_count": int(latest_account_state.get("position_count", 0) or 0),
                        "current_total_ratio": float(metrics.get("current_total_ratio", 0.0) or 0.0),
                        "equity_position_limit": float(metrics.get("equity_position_limit", 0.0) or 0.0),
                        "available_test_trade_value": float(metrics.get("available_test_trade_value", 0.0) or 0.0),
                        "stock_test_budget_amount": float(metrics.get("stock_test_budget_amount", 0.0) or 0.0),
                    }
        runtime_activity_bundle = _build_activity_signal_bundle(
            "运行事实产出",
            [
                ("runtime_report", _extract_activity_timestamp(latest_runtime_report, trade_date=resolved_trade_date)),
                ("runtime_context", _extract_activity_timestamp(latest_runtime_context, trade_date=resolved_trade_date)),
                ("monitor_context", _extract_activity_timestamp(latest_monitor_context, trade_date=resolved_trade_date)),
                ("candidate_poll", candidate_poll.get("last_polled_at")),
            ],
        )
        runtime_last_active_at = runtime_activity_bundle.get("last_active_at")
        runtime_activity_age = _seconds_since(runtime_last_active_at)
        runtime_reasons: list[str] = []
        runtime_status = "standby"
        if not runtime_last_active_at:
            if session_open:
                runtime_status = "needs_work"
                runtime_reasons.append("交易时段内尚未观察到 runtime/monitor 事实产出")
            else:
                runtime_reasons.append("当前无 runtime 活动记录，但也不在交易时段")
        elif session_open and runtime_activity_age is not None and runtime_activity_age > max(overdue_after_seconds, 300):
            runtime_status = "overdue"
            runtime_reasons.append(f"最近 runtime 活动距今 {int(runtime_activity_age)} 秒")
            runtime_reasons.append("当前监督的是运行事实产出是否迟滞，而不是 candidate_poll 调用次数")
        else:
            runtime_status = "working"
            runtime_reasons.append(f"最近 runtime 活动={runtime_last_active_at}")
            if session_open:
                runtime_reasons.append("当前监督基于活动痕迹，不按 runtime 调用频率催办")
        items.append(
            {
                "agent_id": "ashare-runtime",
                "status": runtime_status,
                "reasons": runtime_reasons,
                "last_active_at": runtime_last_active_at,
                "activity_label": runtime_activity_bundle.get("label"),
                "activity_signals": runtime_activity_bundle.get("signals"),
                "activity_signal_count": len(runtime_activity_bundle.get("signals") or []),
            }
        )

        coordinator_reasons: list[str] = []
        coordinator_status = "standby"
        if cycle:
            cycle_age = _seconds_since(cycle.updated_at)
            active_states = {"round_1_running", "round_2_running", "round_running", "final_review_ready"}
            if cycle.discussion_state == "idle" and (cycle.focus_pool_case_ids or cycle.execution_pool_case_ids):
                coordinator_status = "needs_work"
                coordinator_reasons.append(
                    f"候选池已就绪 focus={len(cycle.focus_pool_case_ids or [])} execution={len(cycle.execution_pool_case_ids or [])}，但 discussion 仍未启动"
                )
            elif cycle.discussion_state in active_states:
                if cycle_age is not None and cycle_age > overdue_after_seconds:
                    coordinator_status = "overdue"
                    coordinator_reasons.append(
                        f"讨论处于 {cycle.discussion_state}，但 cycle.updated_at 距今 {int(cycle_age)} 秒"
                    )
                else:
                    coordinator_status = "working"
                    coordinator_reasons.append(f"当前讨论态={cycle.discussion_state}")
            else:
                coordinator_status = "working"
                coordinator_reasons.append(f"当前讨论态={cycle.discussion_state}")
        else:
            coordinator_reasons.append("当前无 active cycle")
        items.append(
            {
                "agent_id": "ashare",
                "status": coordinator_status,
                "reasons": coordinator_reasons,
                "last_active_at": (cycle.updated_at if cycle else None),
            }
        )

        activity_window_seconds = max(overdue_after_seconds, 300)
        discussion_agent_activity = {
            "ashare-research": {
                **_build_activity_signal_bundle(
                    "研究/事件产出",
                    [
                        ("research_summary", _extract_activity_timestamp(_research_summary(), trade_date=resolved_trade_date)),
                        (
                            "event_fetch_result",
                            _latest_state_store_activity(
                                research_state_store,
                                ("latest_event_fetch_result",),
                                trade_date=resolved_trade_date,
                            ),
                        ),
                        (
                            "dossier_or_behavior",
                            _latest_state_store_activity(
                                research_state_store,
                                ("latest_dossier_pack", "latest_stock_behavior_profiles"),
                                trade_date=resolved_trade_date,
                            ),
                        ),
                        (
                            "intraday_or_tail_scan",
                            _latest_state_store_activity(
                                meeting_state_store,
                                ("latest_tail_market_scan", "latest_intraday_rank_result"),
                                trade_date=resolved_trade_date,
                            ),
                        ),
                    ],
                ),
            },
            "ashare-strategy": {
                **_build_activity_signal_bundle(
                    "策略/调参提案",
                    [
                        (
                            "compose_evaluation",
                            compose_metrics.get("latest_generated_at"),
                        ),
                        (
                            "param_proposals",
                            _latest_param_proposal_activity(
                                trade_date=resolved_trade_date,
                                scopes=("strategy", "runtime", "execution"),
                            ),
                        ),
                        (
                            "playbook_override",
                            _latest_state_store_activity(
                                meeting_state_store,
                                ("latest_playbook_override_snapshot",),
                                trade_date=resolved_trade_date,
                            ),
                        ),
                        (
                            "proposal_packet",
                            _latest_state_store_activity(
                                meeting_state_store,
                                ("latest_openclaw_proposal_packet", "latest_offline_self_improvement_export"),
                                trade_date=resolved_trade_date,
                            ),
                        ),
                        (
                            "nightly_sandbox",
                            _latest_state_store_activity(
                                research_state_store,
                                ("latest_nightly_sandbox",),
                                trade_date=resolved_trade_date,
                            ),
                        ),
                    ],
                ),
            },
            "ashare-risk": {
                **_build_activity_signal_bundle(
                    "风控/执行预检",
                    [
                        (
                            "execution_precheck",
                            _latest_state_store_activity(
                                meeting_state_store,
                                ("latest_execution_precheck",),
                                trade_date=resolved_trade_date,
                            ),
                        ),
                        (
                            "execution_reconciliation",
                            _latest_state_store_activity(
                                meeting_state_store,
                                ("latest_execution_reconciliation", "latest_pending_order_remediation"),
                                trade_date=resolved_trade_date,
                            ),
                        ),
                        (
                            "tail_market_scan",
                            _latest_state_store_activity(
                                meeting_state_store,
                                ("latest_tail_market_scan",),
                                trade_date=resolved_trade_date,
                            ),
                        ),
                    ],
                ),
            },
            "ashare-audit": {
                **_build_activity_signal_bundle(
                    "审计/纪要复核",
                    [
                        (
                            "audit_records",
                            _latest_state_store_activity(
                                meeting_state_store,
                                ("latest", "latest_review_board", "latest_openclaw_replay_packet"),
                                trade_date=resolved_trade_date,
                            ),
                        ),
                        (
                            "execution_dispatch",
                            _latest_state_store_activity(
                                meeting_state_store,
                                ("latest_execution_dispatch",),
                                trade_date=resolved_trade_date,
                            ),
                        ),
                        (
                            "governance_proposals",
                            _latest_param_proposal_activity(
                                trade_date=resolved_trade_date,
                                scopes=("risk", "governance"),
                            ),
                        ),
                    ],
                ),
            },
        }

        latest_event_context = serving_store.get_latest_event_context() or {}
        latest_market_change_at = None
        # 提取最新的重大市场变化 (R1.3)
        highlights = list(latest_event_context.get("highlights") or [])
        for ev in highlights:
            severity = str(ev.get("severity") or "").lower()
            if severity in {"high", "critical", "block"}:
                ev_time = ev.get("event_at") or ev.get("recorded_at")
                if ev_time and (not latest_market_change_at or str(ev_time) > str(latest_market_change_at)):
                    latest_market_change_at = ev_time

        discussion_agents = ("ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit")
        mainline_signal_sources = {
            "ashare-research": {"research_summary", "dossier_or_behavior", "intraday_or_tail_scan"},
            "ashare-strategy": {"compose_evaluation", "proposal_packet", "playbook_override", "nightly_sandbox"},
            "ashare-risk": {"execution_precheck", "execution_reconciliation", "tail_market_scan"},
            "ashare-audit": {"audit_records", "execution_dispatch"},
        }
        mainline_signal_hint = {
            "ashare-research": "正式研究结论/机会清单",
            "ashare-strategy": "compose 编排、proposal packet 或夜间沙盘",
            "ashare-risk": "风控口径或执行预检结果",
            "ashare-audit": "纪要、复核记录或执行审计材料",
        }
        current_round = int(getattr(cycle, "current_round", 0) or 0) if cycle else 0
        expected_case_ids: list[str] = []
        if cycle:
            if current_round <= 1:
                expected_case_ids = list(cycle.focus_pool_case_ids or cycle.base_pool_case_ids or [])
            else:
                expected_case_ids = list(cycle.round_2_target_case_ids or cycle.focus_pool_case_ids or [])
        case_map = {case.case_id: case for case in cases}
        for agent_id in discussion_agents:
            covered = 0
            latest_activity_at: str | None = None
            activity_meta = discussion_agent_activity.get(agent_id, {})
            activity_last_active_at = str(activity_meta.get("last_active_at") or "").strip() or None
            activity_label = str(activity_meta.get("label") or "活动痕迹")
            activity_signals = list(activity_meta.get("signals") or [])
            mainline_sources = set(mainline_signal_sources.get(agent_id) or set())
            mainline_signals = [
                signal
                for signal in activity_signals
                if str(signal.get("source") or "").strip() in mainline_sources
            ]
            mainline_output_count = len(mainline_signals)
            mainline_output_hint = str(mainline_signal_hint.get(agent_id) or "主线产物")
            for case_id in expected_case_ids:
                case = case_map.get(case_id)
                if not case:
                    continue
                matched = [op for op in list(case.opinions or []) if op.round == max(current_round, 1) and op.agent_id == agent_id]
                if matched:
                    covered += 1
                    candidate_latest = matched[-1].recorded_at
                    if not latest_activity_at or str(candidate_latest) > str(latest_activity_at):
                        latest_activity_at = candidate_latest
            
            reasons: list[str] = []
            status = "standby"
            latest_activity_at = _max_activity_timestamp(latest_activity_at, activity_last_active_at)
            
            # S1.3: 检查是否对市场变化无响应
            market_unresponsive = False
            if session_open and latest_market_change_at and agent_id in {"ashare-research", "ashare-strategy"}:
                if not latest_activity_at or str(latest_activity_at) < str(latest_market_change_at):
                    change_age = _seconds_since(latest_market_change_at)
                    if change_age is not None and change_age > 600: # 10分钟无响应
                        market_unresponsive = True
                        reasons.append(f"重大市场变化({latest_market_change_at})后 10 分钟无新提案/回应")

            if cycle and expected_case_ids and cycle.discussion_state in {"round_1_running", "round_2_running", "round_running"}:
                if covered >= len(expected_case_ids):
                    status = "working"
                    reasons.append(f"Round {max(current_round, 1)} 已覆盖 {covered}/{len(expected_case_ids)}")
                else:
                    age = _seconds_since((cycle.round_2_started_at if current_round >= 2 else cycle.round_1_started_at) or cycle.updated_at)
                    status = "overdue" if (age is not None and age > overdue_after_seconds) or market_unresponsive else "needs_work"
                    reasons.append(f"Round {max(current_round, 1)} 仅覆盖 {covered}/{len(expected_case_ids)}")
                    if activity_last_active_at:
                        reasons.append(f"最近{activity_label}={activity_last_active_at}，但尚未写回本轮主线")
                    if age is not None:
                        reasons.append(f"本轮开始后已过去 {int(age)} 秒")
            else:
                activity_age = _seconds_since(activity_last_active_at)
                if activity_last_active_at:
                    if session_open and mainline_output_count <= 0:
                        status = "overdue" if (activity_age is not None and activity_age > activity_window_seconds) else "needs_work"
                        reasons.append(f"最近{activity_label}={activity_last_active_at}，但今日尚无{mainline_output_hint}")
                        if agent_id == "ashare-strategy" and int(compose_metrics.get("count", 0) or 0) <= 0:
                            reasons.append("今日尚无 compose 评估账本，说明策略编排仍未沉淀为可消费结果")
                    elif session_open and (activity_age is not None and activity_age > activity_window_seconds or market_unresponsive):
                        status = "overdue"
                        reasons.append(f"{activity_label}距今 {int(activity_age)} 秒，响应可能滞后于市场变化")
                    else:
                        status = "working"
                        reasons.append(f"最近{activity_label}={activity_last_active_at}")
                elif session_open:
                    status = "needs_work"
                    reasons.append(f"交易时段内尚未观察到{activity_label}")
                else:
                    reasons.append("当前无进行中的讨论轮次")
            items.append(
                {
                    "agent_id": agent_id,
                    "status": status,
                    "reasons": reasons,
                    "last_active_at": latest_activity_at,
                    "covered_case_count": covered,
                    "expected_case_count": len(expected_case_ids),
                    "activity_label": activity_label,
                    "activity_signals": activity_signals,
                    "activity_signal_count": len(activity_signals),
                    "mainline_output_count": mainline_output_count,
                    "mainline_output_hint": mainline_output_hint,
                }
            )

        executor_reasons: list[str] = []
        executor_status = "standby"
        intent_count = int(execution_summary.get("intent_count", 0) or 0)
        dispatch_status = str(execution_summary.get("dispatch_status") or "")
        if intent_count > 0 and dispatch_status not in {"submitted", "preview"}:
            executor_status = "needs_work"
            executor_reasons.append(f"当前 execution intents={intent_count}，尚无最新 dispatch 回执")
        elif dispatch_status:
            executor_status = "working"
            executor_reasons.append(f"最新 dispatch 状态={dispatch_status}")
        else:
            executor_reasons.append("当前无待执行回执")
        items.append(
            {
                "agent_id": "ashare-executor",
                "status": executor_status,
                "reasons": executor_reasons,
                "last_active_at": execution_summary.get("last_active_at") or (execution_dispatch or {}).get("generated_at"),
            }
        )

        attention_items = [item for item in items if item.get("status") in {"needs_work", "overdue"}]
        summary_lines = [
            f"trade_date={resolved_trade_date or 'unknown'} supervision_items={len(items)} attention={len(attention_items)}",
            "人工默认只接重要业务消息；催办和升级由机器人自动完成。",
            "监督重点是 agent 的主线产物、真实活动痕迹与响应迟滞，不是机械催工具调用次数。",
        ]
        if int(compose_metrics.get("count", 0) or 0) <= 0:
            summary_lines.append("今日 compose 评估账本=0；若策略侧只有调参痕迹而无编排产物，按未完成主线处理。")
        summary_lines.extend(
            f"{item['agent_id']}={item['status']}" for item in attention_items[:6]
        )
        payload = annotate_supervision_payload(
            {
                "trade_date": resolved_trade_date,
                "overdue_after_seconds": overdue_after_seconds,
                "cycle_state": (cycle.discussion_state if cycle else None),
                "round": current_round,
                "round_coverage": summary.get("round_coverage", {}),
                "items": items,
                "attention_items": attention_items,
                "summary_lines": summary_lines,
                "important_message_policy": important_message_policy,
                "notify_recommended": bool(attention_items),
            },
            meeting_state_store,
        )
        task_plan = build_agent_task_plan(
            payload,
            execution_summary=execution_summary,
            position_context=position_context,
            latest_market_change_at=latest_market_change_at,
            meeting_state_store=meeting_state_store,
        )
        payload["items"] = task_plan.get("items", payload.get("items", []))
        payload["attention_items"] = task_plan.get("attention_items", payload.get("attention_items", []))
        payload["notify_items"] = task_plan.get("notify_items", payload.get("notify_items", []))
        payload["task_dispatch_plan"] = {
            "phase": task_plan.get("phase"),
            "position_context": position_context,
            "summary_lines": task_plan.get("summary_lines", []),
            "recommended_count": len(task_plan.get("recommended_tasks", [])),
            "recommended_tasks": task_plan.get("recommended_tasks", []),
            "tasks": task_plan.get("tasks", []),
        }
        payload["summary_lines"] = list(payload.get("summary_lines") or []) + [
            line for line in list(task_plan.get("summary_lines") or []) if line
        ]
        return payload

    def _resolve_compose_runtime_preferences() -> dict[str, Any]:
        runtime_config = config_mgr.get() if config_mgr else None
        excluded_theme_keywords = []
        if parameter_service:
            try:
                excluded_theme_keywords = normalize_excluded_theme_keywords(
                    parameter_service.get_param_value("excluded_theme_keywords")
                )
            except Exception:
                excluded_theme_keywords = []
        if not excluded_theme_keywords and runtime_config is not None:
            excluded_theme_keywords = normalize_excluded_theme_keywords(
                getattr(runtime_config, "excluded_theme_keywords", "")
            )
        equity_position_limit = (
            float(parameter_service.get_param_value("equity_position_limit"))
            if parameter_service
            else float(getattr(runtime_config, "equity_position_limit", 0.2) or 0.2)
        )
        max_single_amount = (
            float(parameter_service.get_param_value("max_single_amount"))
            if parameter_service
            else float(getattr(runtime_config, "max_single_amount", 50_000.0) or 50_000.0)
        )
        return {
            "excluded_theme_keywords": excluded_theme_keywords,
            "equity_position_limit": round(float(equity_position_limit or 0.0), 4),
            "max_single_amount": round(float(max_single_amount or 0.0), 2),
        }

    def _build_compose_brief_hint(
        trade_date: str,
        *,
        agent_id: str,
        cycle: Any = None,
        task: dict[str, Any] | None = None,
        position_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if agent_id not in {"ashare", "ashare-runtime", "ashare-strategy"}:
            return {"available": False}
        focus_case_ids = list(getattr(cycle, "focus_pool_case_ids", []) or [])
        focus_cases = []
        if candidate_case_service and focus_case_ids:
            for case_id in focus_case_ids:
                case = candidate_case_service.get_case(case_id)
                if case is not None:
                    focus_cases.append(case)
        focus_symbols = [str(item.symbol) for item in focus_cases if str(getattr(item, "symbol", "") or "").strip()]
        focus_sectors = list(
            dict.fromkeys(
                [
                    str((getattr(item, "runtime_snapshot", None) or {}).sector_profile.get("resolved_sector") or "").strip()
                    for item in focus_cases
                    if getattr(item, "runtime_snapshot", None) is not None
                    and isinstance(getattr(item.runtime_snapshot, "sector_profile", None), dict)
                    and str(item.runtime_snapshot.sector_profile.get("resolved_sector") or "").strip()
                ]
            )
        )
        workspace_context = _resolve_workspace_context_for_trade_date(trade_date)
        market_context = dict((workspace_context or {}).get("market_context") or {})
        hot_sectors = [
            str(item).strip()
            for item in list((market_context.get("market_profile") or {}).get("hot_sectors") or [])
            if str(item).strip()
        ]
        runtime_preferences = _resolve_compose_runtime_preferences()
        task = dict(task or {})
        position_context = dict(position_context or {})
        focus_sectors = list(dict.fromkeys([*focus_sectors, *hot_sectors]))[:5]
        task_reason = str(task.get("task_reason") or "").strip()
        task_prompt = str(task.get("task_prompt") or "").strip()
        phase_code = str(task.get("phase_code") or "").strip()
        position_ratio = float(position_context.get("current_total_ratio", 0.0) or 0.0)
        position_limit = float(position_context.get("equity_position_limit", 0.0) or 0.0)
        position_count = int(position_context.get("position_count", 0) or 0)
        available_budget = float(position_context.get("available_test_trade_value", 0.0) or 0.0)
        near_full_position = bool(position_limit > 0 and position_ratio >= max(position_limit - 0.03, position_limit * 0.85))
        has_room_to_add = bool(available_budget >= 10000.0 and position_limit > 0 and position_ratio + 0.03 < position_limit)
        opportunity_objective = "结合实时热点、持仓缺口与替换需求，组织一轮机会扫描"
        market_hypothesis = (
            "当前优先看热点延续、分歧转一致、持仓去弱留强与尾盘潜伏窗口"
            if is_trading_session(datetime.now())
            else "盘后优先复盘有效主线、误判来源与次日可延续方向"
        )
        base_payload = {
            "agent": {"agent_id": agent_id, "role": "controller"},
            "trade_horizon": "intraday_to_overnight",
            "universe_scope": "main-board",
            "symbol_pool": focus_symbols[:15],
            "focus_sectors": focus_sectors,
            "excluded_theme_keywords": list(runtime_preferences.get("excluded_theme_keywords") or []),
            "max_candidates": min(max(len(focus_symbols[:15]), 8), 15),
            "equity_position_limit": runtime_preferences.get("equity_position_limit"),
            "max_single_amount": runtime_preferences.get("max_single_amount"),
            "notes": [
                "若候选与市场主线不贴合，应先调整战法/因子/权重再跑 compose。",
                "基本面只用于排雷，不单独作为主要进攻依据。",
            ],
        }
        if task_reason:
            base_payload["notes"].append("当前任务: " + task_reason)
        if task_prompt:
            base_payload["notes"].append("任务提示: " + task_prompt[:180])

        compose_profiles: list[dict[str, Any]] = []

        def _append_profile(
            profile_id: str,
            *,
            summary: str,
            objective: str,
            hypothesis: str,
            playbooks: list[str],
            factors: list[str],
            weights: dict[str, dict[str, float]],
            notes: list[str],
            trade_horizon: str = "intraday_to_overnight",
            recommended: bool = False,
            recommendation_reason: str = "",
        ) -> None:
            payload = {
                **base_payload,
                "objective": objective,
                "market_hypothesis": hypothesis,
                "trade_horizon": trade_horizon,
                "playbooks": playbooks,
                "factors": factors,
                "weights": weights,
                "notes": list(base_payload.get("notes") or []) + notes,
            }
            compose_profiles.append(
                {
                    "id": profile_id,
                    "summary": summary,
                    "selected": recommended,
                    "recommended": recommended,
                    "binding_level": "advisory_only",
                    "recommendation_reason": recommendation_reason,
                    "payload": payload,
                }
            )

        select_opportunity_expand = (
            has_room_to_add
            or "补仓" in task_reason
            or "新机会" in task_reason
            or phase_code in {"auction", "morning_session", "afternoon_session"}
        )
        select_position_replacement = (
            near_full_position
            or position_count > 0 and ("替换" in task_reason or "换仓" in task_prompt or "做 T" in task_prompt or "持仓" in task_prompt)
        )
        select_tail_ambush = phase_code == "tail_session" or "尾盘" in task_prompt or "尾盘" in task_reason
        select_defensive = "风险" in task_reason or "阻断" in task_prompt or "减仓" in task_prompt
        select_postclose = phase_code in {"post_close", "night_review"} or not is_trading_session(datetime.now())

        _append_profile(
            "opportunity_expand",
            summary="适合盘中继续找增量机会，偏热点延续、分歧转一致和强势突破。",
            objective="围绕热点扩散、强势股续强和盘中异动寻找新增机会",
            hypothesis=market_hypothesis,
            playbooks=["sector_resonance", "trend_acceleration", "weak_to_strong_intraday", "leader_chase"],
            factors=["sector_heat_score", "momentum_slope", "breakout_quality", "relative_volume", "news_catalyst_score", "limit_sentiment"],
            weights={
                "playbooks": {
                    "sector_resonance": 0.30,
                    "trend_acceleration": 0.28,
                    "weak_to_strong_intraday": 0.22,
                    "leader_chase": 0.20,
                },
                "factors": {
                    "sector_heat_score": 0.24,
                    "momentum_slope": 0.20,
                    "breakout_quality": 0.18,
                    "relative_volume": 0.14,
                    "news_catalyst_score": 0.14,
                    "limit_sentiment": 0.10,
                },
            },
            notes=["优先服务未满仓阶段，强调新增机会而不是围绕旧票空转。"],
            recommended=select_opportunity_expand and not select_position_replacement and not select_tail_ambush and not select_defensive and not select_postclose,
            recommendation_reason="当前仓位仍有空间，且任务更偏向盘中新机会发现。",
        )
        _append_profile(
            "position_replacement",
            summary="适合有持仓或接近满仓时做去弱留强、换仓和日内做T评估。",
            objective="围绕持仓去弱留强、替换更强机会和盘中做T窗口做一轮重排",
            hypothesis="当前重点不是再加仓，而是比较旧仓与新机会的相对收益/风险",
            playbooks=["position_replacement", "weak_to_strong_intraday", "dragon_returns", "tail_close_ambush"],
            factors=["momentum_slope", "sector_heat_score", "smart_money_q", "relative_volume", "liquidity_risk_penalty", "news_catalyst_score"],
            weights={
                "playbooks": {
                    "position_replacement": 0.34,
                    "weak_to_strong_intraday": 0.24,
                    "dragon_returns": 0.22,
                    "tail_close_ambush": 0.20,
                },
                "factors": {
                    "momentum_slope": 0.22,
                    "sector_heat_score": 0.18,
                    "smart_money_q": 0.18,
                    "relative_volume": 0.15,
                    "liquidity_risk_penalty": 0.15,
                    "news_catalyst_score": 0.12,
                },
            },
            notes=["优先服务已有持仓/接近满仓阶段，强调替换、做T和持仓优化。"],
            recommended=select_position_replacement and not select_tail_ambush and not select_defensive and not select_postclose,
            recommendation_reason="当前更像持仓优化或接近满仓阶段，优先比较替换效率。",
        )
        _append_profile(
            "tail_ambush",
            summary="适合尾盘窗口，偏尾盘潜伏、龙回头和隔夜延续。",
            objective="围绕尾盘承接、次日延续和尾盘换仓准备做一轮潜伏扫描",
            hypothesis="尾盘优先找承接稳定、次日有延续性的标的，而不是追逐已充分兑现的方向",
            playbooks=["tail_close_ambush", "dragon_returns", "position_replacement"],
            factors=["smart_money_q", "relative_volume", "sector_heat_score", "news_catalyst_score", "momentum_slope"],
            weights={
                "playbooks": {
                    "tail_close_ambush": 0.40,
                    "dragon_returns": 0.34,
                    "position_replacement": 0.26,
                },
                "factors": {
                    "smart_money_q": 0.24,
                    "relative_volume": 0.20,
                    "sector_heat_score": 0.20,
                    "news_catalyst_score": 0.18,
                    "momentum_slope": 0.18,
                },
            },
            notes=["优先服务尾盘窗口，强调尾盘承接、换仓预案和隔夜假设。"],
            recommended=select_tail_ambush and not select_defensive and not select_postclose,
            recommendation_reason="当前阶段更接近尾盘窗口，先看尾盘承接与隔夜预案。",
        )
        _append_profile(
            "defensive_rotation",
            summary="适合风控偏紧、回撤敏感或主题过热时，偏防守和回撤约束。",
            objective="在风控约束下寻找更稳妥的替代方向，减少追高和过热题材暴露",
            hypothesis="当前更适合防守性轮动和低风险替代，而不是继续放大进攻仓位",
            playbooks=["oversold_rebound", "index_enhancement", "statistical_arbitrage"],
            factors=["price_drawdown_20d", "volatility_20d", "liquidity_risk_penalty", "pb_ratio", "sector_heat_score"],
            weights={
                "playbooks": {
                    "oversold_rebound": 0.36,
                    "index_enhancement": 0.34,
                    "statistical_arbitrage": 0.30,
                },
                "factors": {
                    "price_drawdown_20d": 0.24,
                    "volatility_20d": 0.22,
                    "liquidity_risk_penalty": 0.20,
                    "pb_ratio": 0.18,
                    "sector_heat_score": 0.16,
                },
            },
            notes=["适合风险升温、需要压缩追高和题材暴露的阶段。"],
            trade_horizon="intraday_to_swing",
            recommended=select_defensive and not select_postclose,
            recommendation_reason="当前风险与阻断信号更强，优先压缩进攻暴露。",
        )
        _append_profile(
            "postclose_learning",
            summary="适合盘后/夜间，偏次日延续假设、盘后学习和新战法比较。",
            objective="基于盘后事实和次日预案做一轮学习型 compose，对比不同战法偏好",
            hypothesis="盘后应把今天有效主线、误判来源和次日延续假设沉淀成可复用组合",
            playbooks=["dragon_returns", "tail_close_ambush", "oversold_rebound", "sector_resonance"],
            factors=["news_catalyst_score", "sector_heat_score", "momentum_slope", "smart_money_q", "limit_up_popularity"],
            weights={
                "playbooks": {
                    "dragon_returns": 0.28,
                    "tail_close_ambush": 0.24,
                    "oversold_rebound": 0.24,
                    "sector_resonance": 0.24,
                },
                "factors": {
                    "news_catalyst_score": 0.24,
                    "sector_heat_score": 0.22,
                    "momentum_slope": 0.20,
                    "smart_money_q": 0.18,
                    "limit_up_popularity": 0.16,
                },
            },
            notes=["服务盘后学习、夜间沙盘和次日假设，不直接等同于实盘立即执行。"],
            trade_horizon="overnight_to_swing",
            recommended=select_postclose,
            recommendation_reason="当前已进入盘后/夜间语境，适合学习型 compose。",
        )

        if compose_profiles and not any(bool(item.get("selected")) for item in compose_profiles):
            compose_profiles[0]["selected"] = True
            compose_profiles[0]["recommended"] = True
            compose_profiles[0]["recommendation_reason"] = "当前没有明显单一模板优势，仅给出第一份参考模板作为起点。"
        selected_profile = next((item for item in compose_profiles if item.get("selected")), compose_profiles[0] if compose_profiles else {})
        sample_payload = dict(selected_profile.get("payload") or {})
        custom_payload_template = {
            **base_payload,
            "intent_mode": "opportunity_scan",
            "objective": opportunity_objective,
            "market_hypothesis": market_hypothesis,
            "playbooks": [],
            "factors": [],
            "playbook_specs": [],
            "factor_specs": [],
            "weights": {"playbooks": {}, "factors": {}},
            "ranking_primary_score": "composite_score",
            "ranking_secondary_keys": [],
            "custom_constraints": {
                "hard_filters": {
                    "excluded_theme_keywords": list(runtime_preferences.get("excluded_theme_keywords") or []),
                },
                "position_rules": {
                    "equity_position_limit": runtime_preferences.get("equity_position_limit"),
                    "max_single_amount": runtime_preferences.get("max_single_amount"),
                },
            },
            "market_context": {
                "focus_topics": focus_sectors,
                "task_reason": task_reason,
                "phase_code": phase_code,
            },
            "notes": list(base_payload.get("notes") or [])
            + [
                "先理解市场，再选工具，再决定是否调用 runtime。",
                "若参考模板不贴合，直接自定义 playbooks/factors/weights/constraints。",
            ],
        }
        return {
            "available": True,
            "endpoint": "/runtime/jobs/compose-from-brief",
            "why": "给 strategy/runtime/主控一个可直接落地的 compose 起手式，但模板只作为参考，不替代 Agent 自组装判断。",
            "entry_mode": "custom_first",
            "custom_compose_enabled": True,
            "focus_symbols": focus_symbols[:15],
            "focus_sectors": focus_sectors,
            "current_preferences": runtime_preferences,
            "selected_profile_id": str(selected_profile.get("id") or ""),
            "selected_profile_summary": str(selected_profile.get("summary") or ""),
            "selected_profile_is_reference_only": True,
            "recommended_template_ids": [
                str(item.get("id") or "").strip()
                for item in compose_profiles
                if str(item.get("id") or "").strip() and bool(item.get("recommended"))
            ],
            "profiles": compose_profiles,
            "reference_templates": compose_profiles,
            "custom_payload_template": custom_payload_template,
            "sample_payload": sample_payload,
            "orchestration_trace_contract": {
                "required_fields": [
                    "market_hypothesis",
                    "tool_selection_reason",
                    "rejected_options",
                    "should_run_compose",
                    "next_action",
                ],
                "guidance": [
                    "先说明当前市场假设，再说明为什么选这些 playbooks/factors/weights。",
                    "若没有调用 compose，也要说明放弃项和下一步动作。",
                    "参考模板只用于起手，不应替代当前盘面判断。",
                ],
            },
        }

    def _build_agent_runtime_work_payload(
        trade_date: str | None = None,
        *,
        agent_id: str | None = None,
        overdue_after_seconds: int = 180,
        include_prompt_body: bool = False,
        recommended_only: bool = True,
    ) -> dict[str, Any]:
        resolved_trade_date = _resolve_reference_trade_date(trade_date)
        supervision = _build_agent_supervision_payload(resolved_trade_date, overdue_after_seconds=overdue_after_seconds)
        capability_map = _build_agent_capability_map(resolved_trade_date)
        workflow = _build_mainline_workflow_payload(resolved_trade_date)
        cycle = discussion_cycle_service.get_cycle(resolved_trade_date) if discussion_cycle_service else None
        task_plan = dict(supervision.get("task_dispatch_plan") or {})
        position_context = dict(task_plan.get("position_context") or {})
        tasks = [
            dict(item)
            for item in list(
                task_plan.get("recommended_tasks") if recommended_only else task_plan.get("tasks") or []
            )
        ]
        if not tasks:
            tasks = [dict(item) for item in list(task_plan.get("tasks") or [])]
        if agent_id:
            tasks = [item for item in tasks if str(item.get("agent_id") or "").strip() == agent_id]
        packet_cache: dict[str, Any] = {}
        packets: list[dict[str, Any]] = []
        for task in tasks:
            current_agent_id = str(task.get("agent_id") or "").strip()
            role_spec = dict((capability_map.get("roles") or {}).get(current_agent_id) or {})
            packet_payload = None
            if current_agent_id in {"ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"}:
                packet_payload = packet_cache.get(current_agent_id)
                if packet_payload is None:
                    packet_payload = build_agent_packets_envelope(
                        {
                            "items": [
                                _build_agent_packet(case, requested_agent_id=current_agent_id)
                                for case in (
                                    candidate_case_service.list_cases(trade_date=resolved_trade_date, limit=15)
                                    if candidate_case_service
                                    else []
                                )
                            ],
                            "summary_lines": [
                                f"交易日 {resolved_trade_date} 统一 dossier 包，agent={current_agent_id}",
                            ],
                        },
                        trade_date=resolved_trade_date,
                        requested_agent_id=current_agent_id,
                    )
                    packet_cache[current_agent_id] = packet_payload
            prompt_path = str(role_spec.get("prompt_template") or "").strip()
            prompt_body = None
            if include_prompt_body and prompt_path:
                prompt_file = Path("/srv/projects/ashare-system-v2") / prompt_path
                if prompt_file.exists():
                    try:
                        prompt_body = prompt_file.read_text(encoding="utf-8")
                    except Exception:
                        prompt_body = None
            packets.append(
                {
                    "agent_id": current_agent_id,
                    "persona": role_spec.get("persona"),
                    "responsibility": role_spec.get("responsibility"),
                    "status": task.get("status"),
                    "quality_state": task.get("quality_state"),
                    "phase_code": task.get("phase_code"),
                    "phase_label": task.get("phase_label"),
                    "task_reason": task.get("task_reason"),
                    "task_prompt": task.get("task_prompt"),
                    "expected_outputs": list(task.get("expected_outputs") or []),
                    "dispatch_key": task.get("dispatch_key"),
                    "prompt_template": prompt_path,
                    "prompt_body": prompt_body,
                    "read_endpoints": list(role_spec.get("read") or []),
                    "write_endpoints": list(role_spec.get("write") or []),
                    "initiative_rules": list(role_spec.get("initiative_rules") or []),
                    "trigger_conditions": list(role_spec.get("trigger_conditions") or []),
                    "collaborates_with": list(role_spec.get("collaborates_with") or []),
                    "runtime_usage": dict(role_spec.get("runtime_usage") or {}),
                    "compose_brief_hint": _build_compose_brief_hint(
                        resolved_trade_date,
                        agent_id=current_agent_id,
                        cycle=cycle,
                        task=task,
                        position_context=position_context,
                    ),
                    "agent_packet_ref": (
                        f"/system/discussions/agent-packets?trade_date={resolved_trade_date}&agent_id={current_agent_id}"
                        if current_agent_id in {"ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"}
                        else None
                    ),
                    "agent_packet_preview": (
                        {
                            "summary_lines": list((packet_payload or {}).get("summary_lines") or [])[:4],
                            "item_count": int((packet_payload or {}).get("case_count", 0) or 0),
                            "symbol_preview": [
                                str(item.get("symbol") or "").strip()
                                for item in list((packet_payload or {}).get("items") or [])[:5]
                                if str(item.get("symbol") or "").strip()
                            ],
                        }
                        if packet_payload
                        else {}
                    ),
                }
            )
        summary_lines = [
            f"trade_date={resolved_trade_date} packets={len(packets)} recommended_only={recommended_only}",
            "统一给 agent 的任务包应同时包含任务、角色边界、读写接口、候选包和 compose 起手式。",
        ]
        return {
            "ok": True,
            "trade_date": resolved_trade_date,
            "cycle_state": supervision.get("cycle_state"),
            "round": supervision.get("round"),
            "recommended_only": recommended_only,
            "summary_lines": summary_lines,
            "supervision_summary_lines": list(supervision.get("summary_lines") or [])[:8],
            "workflow_ref": "/system/workflow/mainline",
            "capability_map_ref": "/system/agents/capability-map",
            "packets": packets,
            "count": len(packets),
        }

    def _resolve_discussion_writeback_scope(
        trade_date: str,
        *,
        expected_round: int | None = None,
    ) -> tuple[Any | None, int, list[CandidateCase]]:
        cycle = discussion_cycle_service.get_cycle(trade_date) if discussion_cycle_service else None
        if cycle is None and discussion_cycle_service:
            cycle = discussion_cycle_service.bootstrap_cycle(trade_date)
        resolved_round = int(expected_round or (getattr(cycle, "current_round", 0) or 1) or 1)
        if resolved_round <= 1:
            expected_case_ids = list(getattr(cycle, "focus_pool_case_ids", []) or getattr(cycle, "base_pool_case_ids", []) or [])
        else:
            expected_case_ids = list(
                getattr(cycle, "round_2_target_case_ids", []) or getattr(cycle, "focus_pool_case_ids", []) or []
            )
        case_map = {
            item.case_id: item
            for item in (
                candidate_case_service.list_cases(trade_date=trade_date, limit=500)
                if candidate_case_service
                else []
            )
        }
        scoped_cases = [case_map[case_id] for case_id in expected_case_ids if case_id in case_map]
        if not scoped_cases:
            scoped_cases = list(case_map.values())
        return cycle, resolved_round, scoped_cases

    def _append_unique_text(target: list[str], value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in target:
            target.append(text)

    def _event_digest_for_symbol(event_context: dict[str, Any], symbol: str) -> dict[str, Any]:
        symbol = str(symbol or "").strip()
        highlights = [_sanitize_json_compatible(item) for item in list(event_context.get("highlights") or [])]
        symbol_events = [
            dict(item)
            for item in highlights
            if str((item or {}).get("symbol") or "").strip() == symbol
        ]
        negative = [
            item
            for item in symbol_events
            if str(item.get("impact") or "").strip().lower() in {"negative", "block"}
            or str(item.get("severity") or "").strip().lower() in {"warning", "block", "critical"}
        ]
        return {
            "symbol_events": symbol_events[:6],
            "negative_events": negative[:3],
            "blocked_symbols": {
                str(item).strip()
                for item in list(event_context.get("blocked_symbols") or [])
                if str(item).strip()
            },
        }

    def _build_research_auto_writeback(
        trade_date: str,
        *,
        expected_round: int | None = None,
        expected_agent_id: str = "ashare-research",
        auto_rebuild: bool = True,
    ) -> dict[str, Any]:
        cycle, resolved_round, scoped_cases = _resolve_discussion_writeback_scope(
            trade_date,
            expected_round=expected_round,
        )
        response: dict[str, Any] = {
            "ok": False,
            "trade_date": trade_date,
            "round": resolved_round,
            "agent_id": expected_agent_id,
            "summary_lines": [],
            "written_count": 0,
            "written_case_ids": [],
            "rebuilt_case_ids": [],
            "items": [],
            "count": 0,
            "touched_case_summaries": [],
            "refreshed_summary_snapshot": {},
        }
        if not candidate_case_service or not scoped_cases:
            response["summary_lines"] = ["research writeback skipped: no candidate cases"]
            return response

        workspace_context = _resolve_workspace_context_for_trade_date(trade_date)
        global_event_context = dict((workspace_context.get("event_context") or {}))
        research_summary = dict(_research_summary() or {})
        latest_event_fetch = dict(research_state_store.get("latest_event_fetch_result", {}) or {}) if research_state_store else {}
        latest_tail_market_scan = dict(meeting_state_store.get("latest_tail_market_scan", {}) or {}) if meeting_state_store else {}
        global_hot_sectors = [
            str(item).strip()
            for item in list((workspace_context.get("market_context") or {}).get("market_profile", {}).get("hot_sectors", []) or [])
            if str(item).strip()
        ]
        global_event_titles = [
            str(item).strip()
            for item in list(research_summary.get("event_titles") or latest_event_fetch.get("summary_lines") or [])
            if str(item).strip()
        ][:5]
        writeback_items: list[tuple[str, CandidateOpinion]] = []

        for case in scoped_cases:
            dossier_payload = _resolve_case_dossier_payload(trade_date, case.symbol)
            dossier = dict(dossier_payload.get("payload") or {})
            symbol_context = dict(dossier.get("symbol_context") or {})
            local_event_context = dict(dossier.get("event_context") or {})
            effective_event_context = local_event_context or global_event_context
            event_digest = _event_digest_for_symbol(effective_event_context, case.symbol)
            sector_tags = [
                str(item).strip()
                for item in list((symbol_context.get("sector_relative") or {}).get("sector_tags") or [])
                if str(item).strip()
            ]
            reasons: list[str] = []
            evidence_refs: list[str] = []
            key_evidence: list[str] = []
            evidence_gaps: list[str] = []
            questions_to_others: list[str] = []
            stance = "watch"
            confidence = "medium"

            if case.symbol in event_digest["blocked_symbols"] or event_digest["negative_events"]:
                stance = "rejected"
                confidence = "high"
                _append_unique_text(reasons, "研究侧发现最新事件上下文存在高优先级负面/阻断信号")
                for item in event_digest["negative_events"][:2]:
                    _append_unique_text(key_evidence, f"{item.get('title') or '负面事件'} impact={item.get('impact')}")
                evidence_refs.append("/data/event-context/latest")
            else:
                if event_digest["symbol_events"]:
                    stance = "support"
                    confidence = "medium"
                    _append_unique_text(reasons, f"研究侧观察到 {len(event_digest['symbol_events'])} 条与该票直接相关的事件/消息")
                    for item in event_digest["symbol_events"][:2]:
                        _append_unique_text(key_evidence, item.get("title"))
                    evidence_refs.append("/data/event-context/latest")
                if sector_tags:
                    if stance == "watch":
                        stance = "support"
                    _append_unique_text(reasons, f"板块标签={','.join(sector_tags[:3])}")
                    evidence_refs.append("/data/dossiers/latest")
                matched_hot_sectors = [item for item in sector_tags if item in global_hot_sectors]
                if matched_hot_sectors:
                    if stance == "watch":
                        stance = "support"
                    _append_unique_text(reasons, f"命中当前热点板块={','.join(matched_hot_sectors[:3])}")
                if dossier_payload.get("available"):
                    _append_unique_text(key_evidence, f"dossier={dossier_payload.get('source_layer')} 可用")
                    evidence_refs.append("/data/dossiers/latest")
                else:
                    _append_unique_text(evidence_gaps, "该票缺少最新 dossier，需要补读清洗后的个股上下文")
                if not event_digest["symbol_events"] and not sector_tags:
                    _append_unique_text(
                        evidence_gaps,
                        "尚未观察到明确的个股事件或板块共振证据，研究侧先保持观察",
                    )
                    if stance == "support":
                        stance = "watch"
                if global_event_titles:
                    _append_unique_text(key_evidence, f"全局事件摘要={global_event_titles[0]}")

            if case.must_answer_questions:
                _append_unique_text(questions_to_others, case.must_answer_questions[0])
            if not reasons:
                _append_unique_text(reasons, "研究侧暂无足够增量事实，先维持观察")
            opinion = CandidateOpinion(
                round=resolved_round,
                agent_id=expected_agent_id,
                stance=stance,
                confidence=confidence,
                reasons=reasons[:4],
                evidence_refs=list(dict.fromkeys(evidence_refs))[:4],
                thesis="研究侧基于事件、板块与 dossier 事实判断该票是否具备继续讨论价值",
                key_evidence=key_evidence[:4],
                evidence_gaps=evidence_gaps[:4],
                questions_to_others=questions_to_others[:2],
                recorded_at=datetime.now().isoformat(),
            )
            writeback_items.append((case.case_id, opinion))

        updated = _persist_discussion_writeback_items(
            writeback_items,
            auto_rebuild=auto_rebuild,
            audit_message="自动写入 research opinions",
            audit_payload={"input": "research_writeback", "trade_date": trade_date, "round": resolved_round},
        )
        response.update(
            {
                "ok": True,
                "written_count": len(writeback_items),
                "written_case_ids": [case_id for case_id, _ in writeback_items],
                "rebuilt_case_ids": [case.case_id for case in updated] if auto_rebuild else [],
                "items": [item.model_dump() for item in updated],
                "count": len(updated),
                "touched_case_summaries": discussion_cycle_service._build_touched_case_summaries(updated) if discussion_cycle_service else [],
                "summary_lines": [
                    f"research writeback={len(writeback_items)}",
                    f"event_titles={len(global_event_titles)} tail_scan={'yes' if latest_tail_market_scan else 'no'}",
                ],
            }
        )
        if discussion_cycle_service:
            refreshed_summary = discussion_cycle_service.build_summary_snapshot(trade_date)
            response["refreshed_summary_snapshot"] = refreshed_summary
            if cycle is not None:
                cycle.summary_snapshot = refreshed_summary
                cycle.updated_at = datetime.now().isoformat()
                discussion_cycle_service._upsert(cycle)
        return response

    def _build_risk_auto_writeback(
        trade_date: str,
        *,
        expected_round: int | None = None,
        expected_agent_id: str = "ashare-risk",
        auto_rebuild: bool = True,
    ) -> dict[str, Any]:
        cycle, resolved_round, scoped_cases = _resolve_discussion_writeback_scope(
            trade_date,
            expected_round=expected_round,
        )
        response: dict[str, Any] = {
            "ok": False,
            "trade_date": trade_date,
            "round": resolved_round,
            "agent_id": expected_agent_id,
            "summary_lines": [],
            "written_count": 0,
            "written_case_ids": [],
            "rebuilt_case_ids": [],
            "items": [],
            "count": 0,
            "touched_case_summaries": [],
            "refreshed_summary_snapshot": {},
        }
        if not candidate_case_service or not scoped_cases:
            response["summary_lines"] = ["risk writeback skipped: no candidate cases"]
            return response

        precheck = _build_execution_precheck(trade_date)
        precheck_items = {
            str(item.get("symbol") or "").strip(): dict(item)
            for item in list(precheck.get("items") or [])
            if str(item.get("symbol") or "").strip()
        }
        hard_reject_blockers = {"risk_gate_reject", "guard_reject", "excluded_by_selection_preferences"}
        writeback_items: list[tuple[str, CandidateOpinion]] = []
        for case in scoped_cases:
            item = dict(precheck_items.get(case.symbol) or {})
            blockers = [str(code).strip() for code in list(item.get("blockers") or []) if str(code).strip()]
            reasons: list[str] = []
            key_evidence: list[str] = []
            evidence_gaps: list[str] = []
            evidence_refs = [f"/system/discussions/execution-precheck?trade_date={trade_date}"]
            stance = "limit"
            confidence = "medium"
            if not item:
                stance = "question"
                _append_unique_text(evidence_gaps, "该票暂无 execution precheck 条目，需先补齐执行预检")
                _append_unique_text(reasons, "风控侧暂未拿到该票的执行预检事实")
            elif bool(item.get("approved")) and not blockers:
                stance = "support"
                confidence = "high"
                _append_unique_text(reasons, "执行预检通过，当前未见新增硬阻断")
                _append_unique_text(
                    key_evidence,
                    f"budget_remaining={precheck.get('stock_test_budget_remaining')} total_ratio={precheck.get('current_total_ratio')}",
                )
            elif any(code in hard_reject_blockers for code in blockers):
                stance = "rejected"
                confidence = "high"
                _append_unique_text(reasons, "命中硬性风控/偏好阻断，本轮不放行")
            else:
                stance = "limit"
                confidence = "medium"
                _append_unique_text(reasons, "存在临时性执行约束，需要限额或等待条件改善")
            for code in blockers[:3]:
                _append_unique_text(key_evidence, _execution_blocker_label(code))
            next_actions = []
            for code in blockers[:2]:
                for action in _execution_blocker_next_actions(code):
                    label = _EXECUTION_NEXT_ACTION_LABELS.get(action)
                    if label and label not in next_actions:
                        next_actions.append(label)
            opinion = CandidateOpinion(
                round=resolved_round,
                agent_id=expected_agent_id,
                stance=stance,
                confidence=confidence,
                reasons=reasons[:4],
                evidence_refs=evidence_refs,
                thesis="风控侧只根据真实执行预检、仓位约束与当前偏好判断 allow/limit/reject",
                key_evidence=key_evidence[:4],
                evidence_gaps=evidence_gaps[:4],
                questions_to_others=next_actions[:2],
                recorded_at=datetime.now().isoformat(),
            )
            writeback_items.append((case.case_id, opinion))

        updated = _persist_discussion_writeback_items(
            writeback_items,
            auto_rebuild=auto_rebuild,
            audit_message="自动写入 risk opinions",
            audit_payload={"input": "risk_writeback", "trade_date": trade_date, "round": resolved_round},
        )
        response.update(
            {
                "ok": True,
                "written_count": len(writeback_items),
                "written_case_ids": [case_id for case_id, _ in writeback_items],
                "rebuilt_case_ids": [case.case_id for case in updated] if auto_rebuild else [],
                "items": [item.model_dump() for item in updated],
                "count": len(updated),
                "touched_case_summaries": discussion_cycle_service._build_touched_case_summaries(updated) if discussion_cycle_service else [],
                "summary_lines": [
                    f"risk writeback={len(writeback_items)}",
                    f"precheck_approved={int(precheck.get('approved_count', 0) or 0)} blocked={int(precheck.get('blocked_count', 0) or 0)}",
                ],
            }
        )
        if discussion_cycle_service:
            refreshed_summary = discussion_cycle_service.build_summary_snapshot(trade_date)
            response["refreshed_summary_snapshot"] = refreshed_summary
            if cycle is not None:
                cycle.summary_snapshot = refreshed_summary
                cycle.updated_at = datetime.now().isoformat()
                discussion_cycle_service._upsert(cycle)
        return response

    def _build_audit_auto_writeback(
        trade_date: str,
        *,
        expected_round: int | None = None,
        expected_agent_id: str = "ashare-audit",
        auto_rebuild: bool = True,
    ) -> dict[str, Any]:
        cycle, resolved_round, scoped_cases = _resolve_discussion_writeback_scope(
            trade_date,
            expected_round=expected_round,
        )
        response: dict[str, Any] = {
            "ok": False,
            "trade_date": trade_date,
            "round": resolved_round,
            "agent_id": expected_agent_id,
            "summary_lines": [],
            "written_count": 0,
            "written_case_ids": [],
            "rebuilt_case_ids": [],
            "items": [],
            "count": 0,
            "touched_case_summaries": [],
            "refreshed_summary_snapshot": {},
        }
        if not candidate_case_service or not scoped_cases:
            response["summary_lines"] = ["audit writeback skipped: no candidate cases"]
            return response

        latest_review_board = dict(meeting_state_store.get("latest_review_board", {}) or {}) if meeting_state_store else {}
        review_board_lines = [str(item).strip() for item in list(latest_review_board.get("summary_lines") or []) if str(item).strip()]
        writeback_items: list[tuple[str, CandidateOpinion]] = []
        for case in scoped_cases:
            round_opinions = [
                opinion for opinion in list(case.opinions or [])
                if int(opinion.round or 0) == resolved_round and opinion.agent_id in {"ashare-research", "ashare-strategy", "ashare-risk"}
            ]
            covered_agents = {opinion.agent_id for opinion in round_opinions}
            reasons: list[str] = []
            key_evidence: list[str] = []
            evidence_gaps: list[str] = []
            questions_to_others: list[str] = []
            stance = "question"
            confidence = "medium"
            if len(covered_agents) < 3:
                _append_unique_text(reasons, "前置三席尚未全部完成本轮观点写回，审计先不放行")
                _append_unique_text(evidence_gaps, "需先补齐 research / strategy / risk 本轮材料")
            elif case.contradictions or (
                case.round_1_summary.questions_for_round_2 and case.risk_gate != "allow"
            ):
                stance = "hold"
                confidence = "high"
                _append_unique_text(reasons, "当前仍有争议点或待回答问题，审计要求继续质询")
                for item in list(case.contradiction_summary_lines or [])[:2]:
                    _append_unique_text(key_evidence, item)
                for item in list(case.round_1_summary.questions_for_round_2 or [])[:2]:
                    _append_unique_text(questions_to_others, item)
            elif case.risk_gate == "reject":
                stance = "hold"
                confidence = "high"
                _append_unique_text(reasons, "风控已给出 reject，审计不继续放行")
                _append_unique_text(key_evidence, "risk_gate=reject")
            else:
                stance = "support"
                confidence = "medium"
                _append_unique_text(reasons, "前置三席已覆盖且暂未见新增硬冲突，审计同意进入下一步")
                if review_board_lines:
                    _append_unique_text(key_evidence, review_board_lines[0])
            opinion = CandidateOpinion(
                round=resolved_round,
                agent_id=expected_agent_id,
                stance=stance,
                confidence=confidence,
                reasons=reasons[:4],
                evidence_refs=["/system/reports/postclose/review-board"] if latest_review_board else [],
                thesis="审计侧基于前置席位覆盖、争议缺口与复核材料决定 clear/hold",
                key_evidence=key_evidence[:4],
                evidence_gaps=evidence_gaps[:4],
                questions_to_others=questions_to_others[:3],
                recorded_at=datetime.now().isoformat(),
            )
            writeback_items.append((case.case_id, opinion))

        updated = _persist_discussion_writeback_items(
            writeback_items,
            auto_rebuild=auto_rebuild,
            audit_message="自动写入 audit opinions",
            audit_payload={"input": "audit_writeback", "trade_date": trade_date, "round": resolved_round},
        )
        response.update(
            {
                "ok": True,
                "written_count": len(writeback_items),
                "written_case_ids": [case_id for case_id, _ in writeback_items],
                "rebuilt_case_ids": [case.case_id for case in updated] if auto_rebuild else [],
                "items": [item.model_dump() for item in updated],
                "count": len(updated),
                "touched_case_summaries": discussion_cycle_service._build_touched_case_summaries(updated) if discussion_cycle_service else [],
                "summary_lines": [
                    f"audit writeback={len(writeback_items)}",
                    f"review_board={'yes' if latest_review_board else 'no'} review_lines={len(review_board_lines)}",
                ],
            }
        )
        if discussion_cycle_service:
            refreshed_summary = discussion_cycle_service.build_summary_snapshot(trade_date)
            response["refreshed_summary_snapshot"] = refreshed_summary
            if cycle is not None:
                cycle.summary_snapshot = refreshed_summary
                cycle.updated_at = datetime.now().isoformat()
                discussion_cycle_service._upsert(cycle)
        return response

    def _handle_natural_language_adjustment(payload: NaturalLanguageAdjustmentInput) -> dict[str, Any]:
        if not parameter_service or not adjustment_interpreter:
            return {"ok": False, "error": "parameter service not initialized"}
        apply_flag = payload.apply if payload.preview is None else (not payload.preview)
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
        if not apply_flag:
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
                    "apply": apply_flag,
                    "preview": bool(payload.preview),
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

    def _build_supervision_answer_lines(supervision: dict[str, Any]) -> list[str]:
        payload = dict(supervision or {})
        trade_date = str(payload.get("trade_date") or "").strip() or "-"
        phase = dict((payload.get("task_dispatch_plan") or {}).get("phase") or {})
        phase_label = str(phase.get("label") or "").strip() or "未知阶段"
        attention_items = [dict(item) for item in list(payload.get("attention_items") or [])]
        notify_items = [dict(item) for item in list(payload.get("notify_items") or [])]
        acknowledged_items = [dict(item) for item in list(payload.get("acknowledged_items") or [])]
        working_items = [
            dict(item)
            for item in list(payload.get("items") or [])
            if str(item.get("status") or "") == "working"
        ]

        lines = [
            f"监督席位: trade_date={trade_date} phase={phase_label} attention={len(attention_items)} notify={len(notify_items)} acked={len(acknowledged_items)}。",
        ]
        quality_summary_lines = [str(item).strip() for item in list(payload.get("quality_summary_lines") or []) if str(item).strip()]
        progress_blockers = [str(item).strip() for item in list(payload.get("progress_blockers") or []) if str(item).strip()]
        if quality_summary_lines:
            lines.append("推进质量: " + "；".join(quality_summary_lines[:2]))
        if payload.get("escalated"):
            lines.append("当前有重复未解除的阻塞，监督已升级为强提醒。")
        elif notify_items:
            lines.append("当前监督以任务派发为主，优先催办仍缺新产出的岗位。")
        elif working_items:
            lines.append("当前大部分岗位有新活动痕迹，监督以值班观察为主。")
        if progress_blockers:
            lines.append("当前卡点: " + "；".join(progress_blockers[:2]))
        if payload.get("escalated"):
            lead_reason = next(
                (
                    str(item.get("supervision_action_reason") or "").strip()
                    for item in notify_items
                    if str(item.get("supervision_action_reason") or "").strip()
                ),
                "",
            )
            if lead_reason:
                lines.append("升级原因: " + lead_reason)

        for item in notify_items[:3]:
            tier = str(item.get("supervision_tier") or item.get("status") or "").strip()
            task_reason = str(item.get("task_reason") or "").strip() or "当前有待补齐任务"
            outputs = [str(output).strip() for output in list(item.get("expected_outputs") or []) if str(output).strip()]
            line = f"{item.get('agent_id')} 需处理 [{tier}]: {task_reason}。"
            quality_state = str(item.get("quality_state") or "").strip()
            quality_reason = str(item.get("quality_reason") or "").strip()
            if quality_state and quality_reason:
                line += f" 推进质量={quality_state}，{quality_reason}"
            action_reason = str(item.get("supervision_action_reason") or "").strip()
            if action_reason:
                line += f" 优先原因={action_reason}"
            if outputs:
                line += " 预期产物=" + " / ".join(outputs[:3]) + "。"
            lines.append(line)

        if not notify_items:
            for item in working_items[:3]:
                activity_label = str(item.get("activity_label") or "任务").strip()
                reasons = [str(reason).strip() for reason in list(item.get("reasons") or []) if str(reason).strip()]
                line = f"{item.get('agent_id')} 正在值班: {activity_label}。"
                if reasons:
                    line += " " + reasons[0]
                lines.append(line)

        if acknowledged_items:
            acked_preview = "；".join(str(item.get("agent_id") or "-") for item in acknowledged_items[:3])
            lines.append(f"已确认催办对象: {acked_preview}。")
        return lines

    def _build_status_answer_lines(briefing: dict[str, Any]) -> list[str]:
        payload = dict(briefing or {})
        trade_date = str(payload.get("trade_date") or "").strip() or "-"
        cadence = dict(payload.get("cadence") or {})
        client_brief = dict(payload.get("client_brief") or {})
        execution_dispatch = dict(payload.get("execution_dispatch") or {})
        lines = [
            f"交易台总览: trade_date={trade_date} selected={client_brief.get('selected_count', 0)} watchlist={client_brief.get('watchlist_count', 0)}。",
        ]
        cadence_lines = [str(item).strip() for item in list(cadence.get("summary_lines") or []) if str(item).strip()]
        if cadence_lines:
            lines.append("节奏观察: " + "；".join(cadence_lines[:2]))
        dispatch_status = str(execution_dispatch.get("status") or "").strip()
        if dispatch_status:
            lines.append(
                "执行席位: "
                f"status={dispatch_status} submitted={execution_dispatch.get('submitted_count', 0)} "
                f"preview={execution_dispatch.get('preview_count', 0)} blocked={execution_dispatch.get('blocked_count', 0)}。"
            )
        summary_lines = [str(item).strip() for item in list(payload.get("summary_lines") or []) if str(item).strip()]
        if summary_lines:
            lines.append("当前重点: " + "；".join(summary_lines[:2]))
        return lines

    def _build_discussion_answer_lines(brief: dict[str, Any]) -> list[str]:
        payload = dict(brief or {})
        cycle_payload = dict(payload.get("cycle") or {})
        cycle_state = str(cycle_payload.get("discussion_state") or payload.get("discussion_state") or "").strip() or "-"
        pool_state = str(cycle_payload.get("pool_state") or payload.get("pool_state") or "").strip() or "-"
        final_status = str(payload.get("status") or "").strip() or "unknown"
        execution_dispatch_status = str(payload.get("execution_dispatch_status") or "").strip()
        final_cycle_states = {"finalized", "closed"}
        lines = [
            "讨论席位: "
            f"cycle={cycle_state} pool={pool_state} selected={payload.get('selected_count', 0)} "
            f"watchlist={payload.get('watchlist_count', 0)} rejected={payload.get('rejected_count', 0)}。",
        ]
        selected_lines = [str(item).strip() for item in list(payload.get("selected_lines") or []) if str(item).strip()]
        watchlist_lines = [str(item).strip() for item in list(payload.get("watchlist_lines") or []) if str(item).strip()]
        overview_lines = [str(item).strip() for item in list(payload.get("overview_lines") or []) if str(item).strip()]
        if final_status == "ready" and cycle_state in final_cycle_states:
            lines.append("当前状态: 已形成正式收敛结果，可按最终推荐口径查看。")
        elif selected_lines:
            lines.append("当前状态: 这里展示的是当前候选/执行池，不等于最终推荐，仍需继续讨论、风控或审计收口。")
        elif final_status == "blocked":
            lines.append("当前状态: 还没有形成可执行结论，主线仍处于待补材料或待收敛状态。")
        if selected_lines:
            label = "正式推荐" if final_status == "ready" and cycle_state in final_cycle_states else "当前候选"
            lines.append(label + ": " + "；".join(selected_lines[:3]))
        if watchlist_lines:
            lines.append("观察池: " + "；".join(watchlist_lines[:3]))
        if not selected_lines and not watchlist_lines and overview_lines:
            lines.append("讨论摘要: " + "；".join(overview_lines[:3]))
        if selected_lines and any("风控=pending" in line or "审计=pending" in line for line in selected_lines[:3]):
            lines.append("证据状态: 当前前排票仍有待风控/待审计项，不能把粗筛结果当成最终结论。")
        if execution_dispatch_status:
            lines.append(
                "执行链状态: "
                f"dispatch={execution_dispatch_status} submitted={payload.get('execution_dispatch_submitted_count', 0)} "
                f"preview={payload.get('execution_dispatch_preview_count', 0)} blocked={payload.get('execution_dispatch_blocked_count', 0)}。"
            )
        return lines

    def _build_cycle_answer_lines(brief: dict[str, Any]) -> list[str]:
        payload = dict(brief or {})
        cycle_payload = dict(payload.get("cycle") or {})
        if not cycle_payload:
            return ["当前还没有可确认的 discussion cycle。"]
        trade_date = str(cycle_payload.get("trade_date") or payload.get("trade_date") or "-")
        discussion_state = str(cycle_payload.get("discussion_state") or "-")
        pool_state = str(cycle_payload.get("pool_state") or "-")
        base_pool_count = len(list(cycle_payload.get("base_pool_case_ids") or []))
        focus_pool_count = len(list(cycle_payload.get("focus_pool_case_ids") or []))
        execution_pool_count = len(list(cycle_payload.get("execution_pool_case_ids") or []))
        lines = [
            "CYCLE 就是当天主线讨论周期，负责把候选池推进成观察池、执行池和最终结论。",
            f"今日 cycle: trade_date={trade_date} discussion_state={discussion_state} pool_state={pool_state}。",
            f"池子规模: base={base_pool_count} focus={focus_pool_count} execution={execution_pool_count}。",
        ]
        if discussion_state == "idle" and execution_pool_count > 0:
            lines.append("当前含义: 基础池已经生成，但正式讨论轮还没推进完成，执行池已有候选，主线仍需继续收敛。")
        elif discussion_state == "idle":
            lines.append("当前含义: 基础池已准备好，但还没进入有效讨论轮。")
        elif discussion_state in {"round_1_running", "round_2_running", "round_running"}:
            lines.append("当前含义: 讨论轮正在推进，候选和反证还会继续变化。")
        elif discussion_state in {"final_selection_ready", "final_selection_blocked"}:
            lines.append("当前含义: 已接近最终收敛，但仍应结合风控、审计与执行回写确认，不宜直接当成最终结论。")
        elif discussion_state in {"finalized", "closed"}:
            lines.append("当前含义: 主线已经形成正式收敛结果，可以按最终结论口径消费。")
        else:
            lines.append("当前含义: 主线处于中间态，需结合讨论、执行和监督链一起看。")
        return lines

    def _build_execution_answer_lines(precheck: dict[str, Any], dispatch: dict[str, Any] | None) -> list[str]:
        precheck_payload = dict(precheck or {})
        dispatch_payload = dict(dispatch or {})
        lines = [
            "执行席位: "
            f"approved={precheck_payload.get('approved_count', 0)} blocked={precheck_payload.get('blocked_count', 0)} "
            f"status={dispatch_payload.get('status') or 'unknown'}。"
        ]
        summary_lines = [
            str(item).strip()
            for item in execution_precheck_summary_lines(precheck_payload)[:3]
            if str(item).strip()
        ]
        if summary_lines:
            lines.append("预检结论: " + "；".join(summary_lines[:2]))
        dispatch_lines = [
            str(item).strip()
            for item in execution_dispatch_summary_lines(dispatch_payload)[:4]
            if str(item).strip()
        ]
        if dispatch_lines:
            lines.append("派发回执: " + "；".join(dispatch_lines[:2]))
        next_action = str(precheck_payload.get("primary_recommended_next_action_label") or "").strip()
        if next_action:
            lines.append("下一步动作: " + next_action)
        return lines

    def _build_risk_answer_lines(precheck: dict[str, Any]) -> list[str]:
        payload = dict(precheck or {})
        blocked_items = [dict(item) for item in list(payload.get("items") or []) if not item.get("approved")]
        lines = [
            f"风控席位: blocked={payload.get('blocked_count', 0)} approved={payload.get('approved_count', 0)}。",
        ]
        if blocked_items:
            lead = blocked_items[0]
            lines.append(
                "首要阻断: "
                f"{lead.get('symbol')} {lead.get('name') or lead.get('symbol')} "
                f"{lead.get('primary_blocker_label') or lead.get('primary_blocker') or 'unknown'}。"
            )
        else:
            lines.append("首要阻断: 当前未见新增硬阻断。")
        next_action = str(payload.get("primary_recommended_next_action_label") or "").strip()
        if next_action:
            lines.append("风控动作: " + next_action)
        return lines

    def _build_research_answer_lines(summary: dict[str, Any]) -> list[str]:
        payload = dict(summary or {})
        event_titles = [str(item).strip() for item in list(payload.get("event_titles") or []) if str(item).strip()]
        lines = [
            f"研究席位: symbols={len(list(payload.get('symbols') or []))} news={payload.get('news_count', 0)} announcements={payload.get('announcement_count', 0)}。",
        ]
        if event_titles:
            lines.append("最近催化: " + "；".join(event_titles[:3]) + "。")
        else:
            lines.append("最近催化: 当前没有新的事件标题摘要。")
        return lines

    def _build_params_answer_lines(proposals: list[dict[str, Any]]) -> list[str]:
        items = [dict(item) for item in list(proposals or [])]
        lines = [
            f"参数席位: 提案数={len(items)}。",
            "调参入口: /system/feishu/adjustments/natural-language。",
        ]
        if items:
            lead = items[0]
            lines.append(
                "最近提案: "
                f"{lead.get('param_key')} -> {lead.get('new_value')} ({lead.get('status')})。"
            )
        return lines

    def _build_scores_answer_lines(scores: list[dict[str, Any]]) -> list[str]:
        items = [dict(item) for item in list(scores or [])]
        lines = [f"评分席位: agent_count={len(items)}。"]
        if items:
            lead = items[0]
            lines.append(
                "领先席位: "
                f"{lead.get('agent_id')} score={lead.get('new_score'):.1f} weight={lead.get('weight_value'):.2f} state={lead.get('governance_state')}。"
            )
        return lines

    def _build_opportunity_answer_lines(brief: dict[str, Any]) -> list[str]:
        payload = dict(brief or {})
        selected_lines = [str(item).strip() for item in list(payload.get("selected_lines") or []) if str(item).strip()]
        watchlist_lines = [str(item).strip() for item in list(payload.get("watchlist_lines") or []) if str(item).strip()]
        overview_lines = [str(item).strip() for item in list(payload.get("overview_lines") or []) if str(item).strip()]
        lines = [
            f"机会席位: selected={payload.get('selected_count', 0)} watchlist={payload.get('watchlist_count', 0)} rejected={payload.get('rejected_count', 0)}。",
        ]
        if selected_lines:
            lines.append("优先机会: " + "；".join(selected_lines[:3]))
        if watchlist_lines:
            lines.append("备选观察: " + "；".join(watchlist_lines[:3]))
        if not selected_lines and not watchlist_lines and overview_lines:
            lines.append("机会摘要: " + "；".join(overview_lines[:3]))
        return lines

    def _build_replacement_answer_lines(brief: dict[str, Any], precheck: dict[str, Any]) -> list[str]:
        brief_payload = dict(brief or {})
        precheck_payload = dict(precheck or {})
        blocked_selected = [
            dict(it)
            for it in list(precheck_payload.get("items") or [])
            if it.get("status") == "selected" and not it.get("approved")
        ]
        watchlist_lines = [str(item).strip() for item in list(brief_payload.get("watchlist_lines") or []) if str(item).strip()]
        lines = ["换仓席位: 当前进入替换复核模式。"]
        if watchlist_lines:
            lines.append("优先替换候选: " + "；".join(watchlist_lines[:3]))
        if blocked_selected:
            lead = blocked_selected[0]
            lines.append(
                "被动替换压力: "
                f"{lead.get('symbol')} {lead.get('name') or lead.get('symbol')} "
                f"{lead.get('primary_blocker_label') or lead.get('primary_blocker') or 'unknown'}。"
            )
        else:
            lines.append("被动替换压力: 当前未见被风控硬挡住的已入选标的。")
        lines.append("替换原则: 优先留强去弱，先比较更强催化、更高性价比和更低风险的新机会。")
        return lines

    def _build_position_answer_lines(
        precheck: dict[str, Any],
        account_state: dict[str, Any] | None = None,
        execution_reconciliation: dict[str, Any] | None = None,
    ) -> list[str]:
        precheck_payload = dict(precheck or {})
        account_payload = dict(account_state or {})
        reconciliation_payload = dict(execution_reconciliation or {})
        reconciliation_positions = list(reconciliation_payload.get("positions") or [])
        account_positions = list(account_payload.get("equity_positions") or account_payload.get("positions") or [])
        all_pos = reconciliation_positions or account_positions
        lines = [
            "仓位席位: "
            f"上限={precheck_payload.get('equity_position_limit')} 单票={precheck_payload.get('max_single_amount')} "
            f"剩余预算={precheck_payload.get('stock_test_budget_remaining')}。",
            "持仓/占位: "
            f"{precheck_payload.get('current_equity_position_count', 0)}/{precheck_payload.get('max_hold_count', 0)}。",
        ]
        if all_pos:
            lines.append(f"持仓状态: 已确认 {len(all_pos)} 只标的活跃占位。")
        next_action = str(precheck_payload.get("primary_recommended_next_action_label") or "").strip()
        if next_action:
            lines.append("仓位动作: " + next_action)
        return lines

    def _build_holding_review_answer_lines(
        account_state: dict[str, Any],
        execution_reconciliation: dict[str, Any] | None = None,
    ) -> list[str]:
        account_payload = dict(account_state or {})
        reconciliation_payload = dict(execution_reconciliation or {})
        reconciliation_positions = list(reconciliation_payload.get("positions") or [])
        account_positions = list(account_payload.get("equity_positions") or account_payload.get("positions") or [])
        positions = reconciliation_positions or account_positions
        lines = [f"持仓复核席位: confirmed_positions={len(positions)}。"]
        if reconciliation_payload.get("summary_lines"):
            lines.append("对账结论: " + "；".join(str(item).strip() for item in list(reconciliation_payload.get("summary_lines") or [])[:2] if str(item).strip()))
        if positions:
            preview = []
            for item in positions[:3]:
                symbol = str(item.get("symbol") or "")
                quantity = item.get("quantity")
                last_price = item.get("last_price")
                piece = f"{symbol} qty={quantity}"
                if last_price not in (None, ""):
                    piece += f" last={last_price}"
                preview.append(piece)
            lines.append("持仓明细: " + "；".join(preview) + "。")
        else:
            lines.append("持仓明细: 当前没有可确认的持仓占位。")
        return lines

    def _build_day_trading_answer_lines(latest_tail_market: dict[str, Any], tail_review: dict[str, Any]) -> list[str]:
        latest_payload = dict(latest_tail_market or {})
        review_payload = dict(tail_review or {})
        items = list((review_payload.get("review") or {}).get("items") or review_payload.get("items") or [])
        lines = ["日内T席位: 正在复核尾盘与盘中信号。"]
        summary_lines = [str(item).strip() for item in list(review_payload.get("summary_lines") or []) if str(item).strip()]
        if summary_lines:
            lines.append("信号摘要: " + "；".join(summary_lines[:2]))
        elif latest_payload.get("summary_lines"):
            lines.append("信号摘要: " + "；".join(str(item).strip() for item in list(latest_payload.get("summary_lines") or [])[:2] if str(item).strip()))
        if items:
            preview = []
            for item in items[:3]:
                preview.append(f"{item.get('symbol')} {item.get('name') or item.get('symbol')} {item.get('exit_reason') or 'signal'}")
            lines.append("可复核信号: " + "；".join(preview) + "。")
        else:
            lines.append("可复核信号: 当前没有已落库的日内 T / 尾盘处理信号。")
        return lines

    def _append_symbol_trade_advice_lines(answer_lines: list[str], advice: dict[str, Any], *, title_prefix: str = "交易台") -> None:
        answer_lines.append(
            f"{title_prefix}建议级别：{advice.get('recommendation_level')}，立场={advice.get('stance')}。"
        )
        answer_lines.append(f"{title_prefix}结论：{advice.get('summary')}")
        answer_lines.append(f"{title_prefix}建议: {advice.get('summary')}")
        if advice.get("next_actions"):
            answer_lines.append("下一步：" + "；".join(str(item) for item in list(advice.get("next_actions") or [])[:3]) + "。")
        if advice.get("trigger_conditions"):
            answer_lines.append("触发条件：" + "；".join(str(item) for item in list(advice.get("trigger_conditions") or [])[:3]) + "。")
        if advice.get("risk_notes"):
            answer_lines.append("风险提示：" + "；".join(str(item) for item in list(advice.get("risk_notes") or [])[:3]) + "。")

    def _build_symbol_common_brief_lines(
        facts: dict[str, Any],
        *,
        include_market_texture: bool = True,
    ) -> list[str]:
        lines: list[str] = []
        research = dict(facts.get("research") or {})
        behavior_profile = dict(facts.get("behavior_profile") or {})
        sector_relative = dict(facts.get("sector_relative") or {})
        market_relative = dict(facts.get("market_relative") or {})
        position_item = dict(facts.get("position_item") or {})
        precheck_item = dict(facts.get("precheck_item") or {})
        tail_review = dict(facts.get("tail_review") or {})
        case_item = facts.get("candidate_case")
        selected_reason = str(facts.get("selected_reason") or "").strip()
        market_texture = dict(facts.get("market_texture") or {})

        sector_tags = sector_relative.get("sector_tags") or []
        relative_strength = market_relative.get("relative_strength_vs_benchmark")
        if sector_tags:
            lines.append("当前板块标签：" + "、".join(str(item) for item in sector_tags[:4]) + "。")
        if relative_strength not in (None, ""):
            lines.append(f"相对基准强弱：{relative_strength}。")
        if research.get("latest_titles"):
            lines.append("最近相关标题：" + "；".join(str(item) for item in research.get("latest_titles", [])[:3]) + "。")
        elif facts.get("research_summary", {}).get("event_titles"):
            lines.append(
                "研究面最近在跟踪："
                + "；".join(str(item) for item in list(facts.get("research_summary", {}).get("event_titles") or [])[:3])
                + "。"
            )
        if behavior_profile:
            lines.append(
                "股性画像："
                f"板成率={behavior_profile.get('board_success_rate_20d')} "
                f"炸板率={behavior_profile.get('bomb_rate_20d')} "
                f"风格={behavior_profile.get('style_tag') or 'unknown'}。"
            )
        if include_market_texture and market_texture.get("available"):
            lines.append("策略视角：" + str(market_texture.get("trend_summary") or ""))
            lines.append("量价视角：" + str(market_texture.get("volume_summary") or ""))
        if case_item:
            lines.append(
                f"讨论定位：{getattr(case_item, 'final_status', 'unknown')}，risk={getattr(case_item, 'risk_gate', '-')}"
                f"，audit={getattr(case_item, 'audit_gate', '-')}。"
            )
            if selected_reason:
                lines.append(f"当前讨论给这只票的主理由：{selected_reason}。")
        if position_item:
            lines.append(
                f"持仓状态：qty={position_item.get('quantity')} cost={position_item.get('cost_price')} last={position_item.get('last_price')}。"
            )
            if position_item.get("cost_price") not in (None, "", 0) and position_item.get("last_price") not in (None, ""):
                try:
                    pnl_pct = (float(position_item["last_price"]) - float(position_item["cost_price"])) / max(float(position_item["cost_price"]), 1e-9) * 100
                    lines.append(f"按当前快照估算，浮动收益约 {pnl_pct:.2f}%。")
                except Exception:
                    pass
        if precheck_item:
            lines.append(
                f"执行视角：approved={precheck_item.get('approved')} budget={precheck_item.get('budget_value')} "
                f"single_remaining={precheck_item.get('remaining_single_value')}。"
            )
            if precheck_item.get("primary_blocker_label"):
                lines.append(f"当前执行阻断：{precheck_item.get('primary_blocker_label')}。")
        if tail_review.get("available"):
            tail_item = list(tail_review.get("items") or [])[:1]
            if tail_item:
                item = tail_item[0]
                lines.append(
                    f"日内/尾盘信号：{item.get('exit_reason') or 'signal'}，tags={','.join(str(tag) for tag in list(item.get('review_tags') or [])[:3]) or 'none'}。"
                )
        return lines

    def _is_feishu_adjustment_request(text: str) -> bool:
        normalized = str(text or "").strip()
        if not normalized:
            return False
        trigger_keywords = (
            "调参",
            "参数",
            "仓位",
            "改到",
            "改成",
            "调到",
            "提高到",
            "降低到",
            "不买",
            "不要买",
            "先不买",
            "先不碰",
            "禁买",
            "排除",
            "剔除",
            "回避",
            "避开",
        )
        return any(keyword in normalized for keyword in trigger_keywords)

    def _build_feishu_adjustment_payload(text: str) -> NaturalLanguageAdjustmentInput:
        normalized = str(text or "").strip()
        preview_only = any(token in normalized for token in ("预览", "看看", "试算", "先看"))
        notify = any(token in normalized for token in ("通知我", "发飞书", "同步通知"))
        return NaturalLanguageAdjustmentInput(
            instruction=normalized,
            apply=not preview_only,
            notify=notify,
            proposed_by="feishu_user",
            structured_by="ashare",
            approved_by="ashare-audit",
            status="approved",
        )

    def _build_feishu_adjustment_reply(text: str) -> dict[str, Any]:
        adjustment_payload = _build_feishu_adjustment_payload(text)
        result = _handle_natural_language_adjustment(adjustment_payload)
        reply_lines = [str(line or "").strip() for line in result.get("reply_lines", []) if str(line or "").strip()]
        if not reply_lines and result.get("summary_lines"):
            reply_lines = [str(line or "").strip() for line in result.get("summary_lines", []) if str(line or "").strip()]
        return {
            "matched": bool(result.get("ok")),
            "adjustment": result,
            "reply_lines": reply_lines,
        }

    def _normalize_symbol_query(text: str) -> str:
        normalized = str(text or "").strip()
        normalized = re.sub(r"^(分析一下|分析下|帮我分析一下|帮我分析下|看看|看下|说说|聊聊|研究一下|研究下)", "", normalized).strip()
        normalized = re.sub(r"(这支股票|这只股票|这支票|这只票|个股|股票)$", "", normalized).strip(" ：:，,。！？")
        normalized = re.sub(r"(怎么样|怎么看|如何看|行不行|可以吗|值得吗)$", "", normalized).strip(" ：:，,。！？")
        normalized = re.sub(r"(这支股票|这只股票|这支票|这只票|个股|股票)$", "", normalized).strip(" ：:，,。！？")
        normalized = re.sub(
            r"(这只持仓|这支持仓|持仓复核一下|持仓复核|持仓明细|当前持仓|持仓怎么样|有做t机会吗|有做T机会吗|今天有做t机会吗|今天有做T机会吗|有没有更好的替换建议|替换建议|换仓建议|替换风险)$",
            "",
            normalized,
            flags=re.IGNORECASE,
        ).strip(" ：:，,。！？")
        normalized = re.sub(
            r"(帮我分析了吗|分析了吗|还能分析吗|能分析吗|帮我看一下|帮我看下|再分析一下|再看一下)$",
            "",
            normalized,
            flags=re.IGNORECASE,
        ).strip(" ：:，,。！？")
        normalized = re.sub(r"(这支股票|这只股票|这支票|这只票|个股|股票)$", "", normalized).strip(" ：:，,。！？")
        normalized = normalized.strip(" ：:，,。！？；;“”\"'（）()[]【】<>《》@")
        return normalized

    def _find_symbol_position_item(
        account_state: dict[str, Any] | None,
        execution_reconciliation: dict[str, Any] | None,
        symbol: str,
    ) -> dict[str, Any] | None:
        reconciliation_positions = [
            item
            for item in list((execution_reconciliation or {}).get("positions") or [])
            if str(item.get("symbol") or "") == symbol
        ]
        account_positions = [
            item
            for item in list((account_state or {}).get("equity_positions") or (account_state or {}).get("positions") or [])
            if str(item.get("symbol") or "") == symbol
        ]
        return dict((reconciliation_positions or account_positions or [None])[0] or {}) or None

    def _build_symbol_fact_bundle(symbol: str, trade_date: str | None = None) -> dict[str, Any]:
        resolved_trade_date = _resolve_reference_trade_date(trade_date)
        name = market_adapter.get_symbol_name(symbol)
        dossier_payload = (
            _resolve_case_dossier_payload(resolved_trade_date, symbol)
            if resolved_trade_date
            else {"available": False, "payload": {}}
        )
        dossier = dict(dossier_payload.get("payload") or {})
        symbol_context = dict(dossier.get("symbol_context") or {})
        event_context = dict(dossier.get("event_context") or {})
        research = dict(dossier.get("research") or {})
        behavior_profile = dict(dossier.get("behavior_profile") or symbol_context.get("behavior_profile") or {})
        market_snapshot = dict(dossier.get("market_snapshot") or {})
        sector_relative = dict(symbol_context.get("sector_relative") or {})
        market_relative = dict(symbol_context.get("market_relative") or {})
        research_summary = _research_summary()
        resolved_account_id = _resolve_account_id()
        account_state = (
            account_state_service.snapshot(resolved_account_id, persist=False)
            if account_state_service
            else {"status": "unavailable"}
        )
        execution_reconciliation = meeting_state_store.get("latest_execution_reconciliation", {}) if meeting_state_store else {}
        position_item = _find_symbol_position_item(account_state, execution_reconciliation, symbol)
        tail_market_latest = meeting_state_store.get("latest_tail_market_scan", {}) if meeting_state_store else {}
        tail_review = _build_tail_market_review_summary(
            scan_payloads=[tail_market_latest] if tail_market_latest else [],
            symbol=symbol,
        )
        precheck = _build_execution_precheck(resolved_trade_date) if resolved_trade_date else {}
        precheck_item = next(
            (item for item in list(precheck.get("items") or []) if str(item.get("symbol") or "") == symbol),
            None,
        )
        cases = candidate_case_service.list_cases(trade_date=resolved_trade_date, limit=500) if (resolved_trade_date and candidate_case_service) else []
        case_item = next((case for case in cases if case.symbol == symbol), None)
        selected_reason = str(getattr(case_item, "selected_reason", "") or "").strip() if case_item else ""
        other_candidates: list[dict[str, Any]] = []
        for case in cases:
            if case.symbol == symbol:
                continue
            final_status = str(getattr(case, "final_status", "") or "")
            if final_status not in {"selected", "watchlist"}:
                continue
            other_candidates.append(
                {
                    "symbol": case.symbol,
                    "name": case.name,
                    "final_status": final_status,
                    "selected_reason": str(getattr(case, "selected_reason", "") or "").strip(),
                    "risk_gate": getattr(case, "risk_gate", ""),
                    "audit_gate": getattr(case, "audit_gate", ""),
                }
            )

        return {
            "trade_date": resolved_trade_date,
            "symbol": symbol,
            "name": name,
            "dossier_payload": dossier_payload,
            "dossier": dossier,
            "symbol_context": symbol_context,
            "event_context": event_context,
            "research": research,
            "research_summary": research_summary,
            "behavior_profile": behavior_profile,
            "market_snapshot": market_snapshot,
            "sector_relative": sector_relative,
            "market_relative": market_relative,
            "account_state": account_state,
            "execution_reconciliation": execution_reconciliation,
            "position_item": position_item,
            "tail_market_latest": tail_market_latest,
            "tail_review": tail_review,
            "execution_precheck": precheck,
            "precheck_item": precheck_item,
            "candidate_case": case_item,
            "selected_reason": selected_reason,
            "other_candidates": other_candidates[:5],
        }

    def _build_symbol_trade_advice(
        facts: dict[str, Any],
        *,
        topic: str,
    ) -> dict[str, Any]:
        position_item = dict(facts.get("position_item") or {})
        precheck_item = dict(facts.get("precheck_item") or {})
        tail_review = dict(facts.get("tail_review") or {})
        case_item = facts.get("candidate_case")
        other_candidates = list(facts.get("other_candidates") or [])
        selected_reason = str(facts.get("selected_reason") or "").strip()
        event_count = int((facts.get("event_context") or {}).get("event_count", 0) or 0)
        sector_tags = list((facts.get("sector_relative") or {}).get("sector_tags") or [])
        stance = "observe"
        recommendation_level = "medium"
        summary = "当前事实仍需继续跟踪。"
        next_actions: list[str] = []
        trigger_conditions: list[str] = []
        risk_notes: list[str] = []

        if topic in {"holding_review", "position"}:
            if position_item:
                cost_price = position_item.get("cost_price")
                last_price = position_item.get("last_price")
                pnl_pct = None
                if cost_price not in (None, "", 0) and last_price not in (None, ""):
                    try:
                        pnl_pct = (float(last_price) - float(cost_price)) / max(float(cost_price), 1e-9) * 100
                    except Exception:
                        pnl_pct = None
                if precheck_item.get("primary_blocker_label"):
                    stance = "trim_or_replace"
                    recommendation_level = "high"
                    summary = f"执行侧已出现 {precheck_item.get('primary_blocker_label')}，这只票应列入减仓或替换复核。"
                    next_actions = ["先核对阻断是否来自仓位/预算约束", "与观察池候选做一轮强弱对比"]
                    trigger_conditions = ["若执行阻断持续存在", "若同方向候选强度明显更高"]
                    risk_notes = ["当前执行链已有明确压力", "不要忽略仓位或预算约束对持仓处理的影响"]
                elif pnl_pct is not None and pnl_pct < -3:
                    stance = "defensive_watch"
                    recommendation_level = "high"
                    summary = f"当前浮动收益约 {pnl_pct:.2f}%，先以防守复核为主，避免把回撤放大。"
                    next_actions = ["复核是否跌破原持有理由", "盘中若弱于同方向候选则考虑降仓"]
                    trigger_conditions = ["若分时继续走弱", "若原持有逻辑被价格或量能破坏"]
                    risk_notes = ["回撤已进入需要防守复核的区间", "避免在弱势结构里硬扛仓位"]
                else:
                    stance = "hold_and_review"
                    recommendation_level = "medium"
                    summary = "当前未见硬阻断，优先保留并持续复核盘中强弱。"
                    next_actions = ["继续盯盘口节奏与分时强弱", "若出现更强候选再进入替换比较"]
                    trigger_conditions = ["若出现更强候选", "若盘中强弱明显转差"]
                    risk_notes = ["当前适合动态复核，不适合静态躺平", "保留不等于放弃比较"]
            else:
                stance = "not_holding"
                recommendation_level = "low"
                summary = "当前没有确认到这只票的持仓占位，不属于持仓处理主对象。"
                next_actions = ["若要操作，先确认是否已纳入执行池或候选池"]
                trigger_conditions = ["当它进入持仓或执行池后再升级处理"]
                risk_notes = ["不要把非持仓票误当成持仓处理对象"]
        elif topic == "day_trading":
            if tail_review.get("available"):
                first_item = list(tail_review.get("items") or [])[:1]
                exit_reason = str((first_item[0] if first_item else {}).get("exit_reason") or "")
                stance = "t_signal_active"
                recommendation_level = "medium"
                summary = f"当前已落库 {exit_reason or '日内'} 信号，这只票适合优先做盘中 T 复核。"
                next_actions = ["查看分时是否仍延续信号方向", "仅在不破坏主仓逻辑时做日内处理"]
                trigger_conditions = ["若分时继续延续同向信号", "若成交节奏支持快进快出"]
                risk_notes = ["做T优先服务主仓，不应反客为主", "没有分时延续时不要机械执行"]
            else:
                stance = "no_t_signal"
                recommendation_level = "low"
                summary = "当前没有已落库的做T/尾盘处理信号，不建议硬找交易动作。"
                next_actions = ["继续等分时异动或尾盘信号", "没有信号时维持原持仓逻辑"]
                trigger_conditions = ["只有出现新信号后才升级为日内处理对象"]
                risk_notes = ["没有信号时强行做T，容易把交易变成噪声操作"]
        elif topic == "replacement":
            if precheck_item.get("approved") and not precheck_item.get("primary_blocker_label"):
                stance = "keep_priority"
                recommendation_level = "medium"
                summary = "执行侧当前没有硬阻断，这只票不构成被迫替换对象。"
                next_actions = ["仅在出现更强催化或显著更优候选时再进入替换讨论", "维持对同方向候选的强弱跟踪"]
                trigger_conditions = ["若出现更强催化", "若同方向候选显著更优"]
                risk_notes = ["不要为了替换而替换", "优先比较机会成本而不是静态分数"]
            elif precheck_item.get("primary_blocker_label"):
                stance = "replacement_candidate"
                recommendation_level = "high"
                summary = f"执行侧存在 {precheck_item.get('primary_blocker_label')}，可把这只票列入替换复核对象。"
                next_actions = ["确认阻断是临时市场因素还是结构性问题", "优先与观察池高质量候选比较"]
                trigger_conditions = ["若阻断无法快速解除", "若观察池候选具备更强结构"]
                risk_notes = ["执行阻断可能让这只票失去优先级", "先区分临时阻断和结构失效"]
            elif not position_item:
                stance = "not_current_holding"
                recommendation_level = "low"
                summary = "当前未发现实际持仓，占位上它不是优先替换对象。"
                next_actions = ["先聚焦已有持仓和已入选候选的替换排序"]
                trigger_conditions = ["当它进入持仓后再进入替换排序"]
                risk_notes = ["避免把注意力浪费在未持有对象的替换讨论上"]
            else:
                stance = "observe_replacement"
                recommendation_level = "medium"
                summary = "当前可以纳入替换比较，但仍需结合市场主线与候选强弱判断。"
                next_actions = ["比较催化强度、量价结构与风控压力", "不要只按静态分数替换"]
                trigger_conditions = ["若候选股主线更强", "若原票量价结构继续转弱"]
                risk_notes = ["替换动作本身有机会成本", "比较应基于主线与执行可行性"]
            if other_candidates:
                next_actions.append("优先比较前 1 到 3 个观察/入选候选的主理由与风控状态")
        else:
            if case_item and selected_reason:
                stance = "discussion_selected"
                recommendation_level = "medium"
                summary = f"这只票当前有明确讨论定位，主理由是：{selected_reason}。"
                next_actions = ["继续验证主理由是否仍成立", "追问仓位、做T或替换可拿到更细执行意见"]
                trigger_conditions = ["若主理由继续被市场验证", "若主理由失效则转入替换/风控复核"]
                risk_notes = ["讨论通过不等于执行必过", "仍需看执行与盘中节奏"]
            elif position_item or precheck_item or tail_review.get("available"):
                stance = "fact_supported"
                recommendation_level = "medium"
                summary = "这只票已接入持仓、执行或日内事实链，不是纯静态描述对象。"
                next_actions = ["根据你的关注点继续追问仓位、做T、替换或风控"]
                trigger_conditions = ["根据关注主题切到更细执行视角"]
                risk_notes = ["已有事实链，但还不是完整闭环结论"]
            elif event_count > 0 or sector_tags:
                stance = "research_watch"
                recommendation_level = "low"
                summary = "当前偏研究/题材跟踪视角，适合继续观察是否进入可执行阶段。"
                next_actions = ["继续跟踪催化落地与板块共振", "必要时让 agent 调整参数后再跑 runtime"]
                trigger_conditions = ["若催化转化为价格和量能共振", "若进入候选或执行池"]
                risk_notes = ["题材研究不等于可交易", "避免只凭消息面直接下结论"]
            else:
                stance = "insufficient_evidence"
                recommendation_level = "low"
                summary = "当前证据还偏少，先别把它当成成熟交易对象。"
                next_actions = ["先补 dossier 或纳入候选再判断", "避免在证据不足时给过强结论"]
                trigger_conditions = ["待证据补齐后再升级判断"]
                risk_notes = ["证据稀薄时最容易误判", "不要把名称识别当成分析完成"]

        return {
            "stance": stance,
            "recommendation_level": recommendation_level,
            "summary": summary,
            "next_actions": next_actions[:3],
            "trigger_conditions": trigger_conditions[:3],
            "risk_notes": risk_notes[:3],
        }

    def _resolve_symbol_from_question(question: str, trade_date: str | None = None) -> dict[str, str] | None:
        query = _normalize_symbol_query(question)
        if not query:
            return None
        upper_query = query.upper()
        if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", upper_query):
            return {"symbol": upper_query, "name": market_adapter.get_symbol_name(upper_query)}
        if re.fullmatch(r"\d{6}", query):
            symbol = f"{query}.SH" if query.startswith("6") else f"{query}.SZ"
            return {"symbol": symbol, "name": market_adapter.get_symbol_name(symbol)}

        candidates: list[dict[str, str]] = []
        resolved_trade_date = _resolve_reference_trade_date(trade_date)
        if resolved_trade_date and candidate_case_service:
            for case in candidate_case_service.list_cases(trade_date=resolved_trade_date, limit=500):
                if case.symbol == upper_query or case.name == query:
                    return {"symbol": case.symbol, "name": case.name or market_adapter.get_symbol_name(case.symbol)}
                if query and query in str(case.name or ""):
                    candidates.append({"symbol": case.symbol, "name": case.name or market_adapter.get_symbol_name(case.symbol)})

        latest_pack = serving_store.get_latest_dossier_pack() or {}
        for item in latest_pack.get("items", []):
            symbol = str(item.get("symbol") or "").strip()
            name = str(item.get("name") or "").strip()
            if not symbol:
                continue
            if symbol == upper_query or name == query:
                return {"symbol": symbol, "name": name or market_adapter.get_symbol_name(symbol)}
            if query and query in name:
                candidates.append({"symbol": symbol, "name": name or market_adapter.get_symbol_name(symbol)})

        try:
            instrument_candidates = market_adapter.search_symbols(query, limit=10)
        except Exception:
            instrument_candidates = []
        for item in instrument_candidates:
            symbol = str(item.get("symbol") or "").strip()
            name = str(item.get("name") or "").strip()
            if not symbol:
                continue
            if symbol == upper_query or name == query:
                return {"symbol": symbol, "name": name or market_adapter.get_symbol_name(symbol)}
            candidates.append({"symbol": symbol, "name": name or market_adapter.get_symbol_name(symbol)})

        seen_symbols = {item["symbol"] for item in candidates}
        for universe_getter in (market_adapter.get_main_board_universe, market_adapter.get_a_share_universe):
            try:
                for symbol in universe_getter():
                    if symbol in seen_symbols:
                        continue
                    name = str(market_adapter.get_symbol_name(symbol) or "").strip()
                    if symbol == upper_query or name == query:
                        return {"symbol": symbol, "name": name or symbol}
                    if query and query in name:
                        candidates.append({"symbol": symbol, "name": name or symbol})
                        seen_symbols.add(symbol)
            except Exception:
                continue
            if candidates:
                break

        if candidates:
            return candidates[0]
        return None

    def _looks_like_casual_chat(question: str) -> bool:
        normalized = re.sub(r"\s+", "", str(question or "").lower())
        if not normalized:
            return True
        casual_patterns = (
            "你好",
            "您好",
            "在吗",
            "在不在",
            "早上好",
            "中午好",
            "下午好",
            "晚上好",
            "辛苦了",
            "谢谢",
            "多谢",
            "收到",
            "好的",
            "ok",
            "okay",
            "哈哈",
            "测试",
            "试试",
            "能聊天吗",
            "你是谁",
        )
        if normalized in casual_patterns:
            return True
        short_casual_markers = ("谢谢你", "收到啦", "先这样", "晚点再说", "辛苦", "打扰了")
        return any(marker in normalized for marker in short_casual_markers)

    def _build_casual_chat_answer(question: str, trade_date: str | None = None) -> dict[str, Any]:
        normalized = re.sub(r"\s+", "", str(question or "").lower())
        answer_lines = ["我在。"]
        if any(keyword in normalized for keyword in ("谢谢", "多谢", "辛苦")):
            answer_lines = ["收到。你继续发问题或指令就行。"]
        elif any(keyword in normalized for keyword in ("你好", "您好", "早上好", "中午好", "下午好", "晚上好")):
            answer_lines = ["我在。要聊盘面、个股、仓位、执行、调参或 agent 工作状态都可以。"]
        elif any(keyword in normalized for keyword in ("测试", "试试", "在吗", "在不在")):
            answer_lines = ["在线。你可以直接发股票、参数指令、执行问题或随便聊两句。"]
        elif any(keyword in normalized for keyword in ("你是谁", "能聊天吗")):
            answer_lines = ["我是量化交易台主控。闲聊可以，问到股票、仓位、执行、风控、研究时我会切到真实工具和协作链来回答。"]
        return {
            "topic": "casual_chat",
            "trade_date": _resolve_reference_trade_date(trade_date),
            "question": question,
            "answer_lines": answer_lines,
            "data_refs": [],
        }

    def _build_symbol_market_texture(symbol: str) -> dict[str, Any]:
        bars: list[BarSnapshot] = []
        try:
            bars = list(market_adapter.get_bars([symbol], period="1d", count=5) or [])
        except Exception:
            bars = []
        symbol_bars = [item for item in bars if str(getattr(item, "symbol", "")) == symbol]
        if not symbol_bars:
            return {
                "available": False,
                "trend_summary": "",
                "volume_summary": "",
                "risk_summary": "",
            }
        closes = [float(getattr(item, "close", 0.0) or 0.0) for item in symbol_bars]
        highs = [float(getattr(item, "high", 0.0) or 0.0) for item in symbol_bars]
        lows = [float(getattr(item, "low", 0.0) or 0.0) for item in symbol_bars]
        volumes = [float(getattr(item, "volume", 0.0) or 0.0) for item in symbol_bars]
        last_close = closes[-1]
        first_close = closes[0]
        change_pct = ((last_close - first_close) / first_close * 100) if first_close else None
        avg_volume = (sum(volumes[:-1]) / max(len(volumes[:-1]), 1)) if len(volumes) > 1 else None
        volume_ratio = (volumes[-1] / avg_volume) if avg_volume not in (None, 0) else None
        amplitude_pct = None
        if lows[-1] > 0:
            amplitude_pct = (highs[-1] - lows[-1]) / lows[-1] * 100
        trend_summary = "近 5 根日线仍偏震荡。"
        if change_pct is not None:
            if change_pct >= 6:
                trend_summary = f"近 5 根日线累计走强约 {change_pct:.2f}%，短线势能偏强。"
            elif change_pct >= 2:
                trend_summary = f"近 5 根日线温和抬升约 {change_pct:.2f}%，属于可继续观察的顺势结构。"
            elif change_pct <= -6:
                trend_summary = f"近 5 根日线累计回撤约 {abs(change_pct):.2f}%，短线结构偏弱。"
            elif change_pct <= -2:
                trend_summary = f"近 5 根日线小幅走弱约 {abs(change_pct):.2f}%，先别把它当成强势票。"
        volume_summary = "近期量能没有明显异常。"
        if volume_ratio is not None:
            if volume_ratio >= 1.6:
                volume_summary = f"最新一根量能约为近几日均量的 {volume_ratio:.2f} 倍，说明交易关注度在抬升。"
            elif volume_ratio <= 0.7:
                volume_summary = f"最新一根量能约为近几日均量的 {volume_ratio:.2f} 倍，量能偏弱，追价要谨慎。"
        risk_summary = "价格波动仍在常规范围。"
        if amplitude_pct is not None:
            if amplitude_pct >= 8:
                risk_summary = f"最近一根振幅约 {amplitude_pct:.2f}%，波动较大，适合轻仓验证而不是重仓拍板。"
            elif amplitude_pct <= 3:
                risk_summary = f"最近一根振幅约 {amplitude_pct:.2f}%，短线博弈弹性一般。"
        return {
            "available": True,
            "trend_summary": trend_summary,
            "volume_summary": volume_summary,
            "risk_summary": risk_summary,
            "bar_count": len(symbol_bars),
        }

    def _build_symbol_analysis_answer(question: str, trade_date: str | None = None) -> dict[str, Any]:
        resolved_trade_date = _resolve_reference_trade_date(trade_date)
        if not resolved_trade_date:
            latest_pack = serving_store.get_latest_dossier_pack() or {}
            resolved_trade_date = str(latest_pack.get("trade_date") or datetime.now().date().isoformat())
        resolved = _resolve_symbol_from_question(question, trade_date=resolved_trade_date)
        if not resolved:
            return {
                "topic": "symbol_analysis",
                "trade_date": resolved_trade_date,
                "question": question,
                "matched": False,
                "answer_lines": ["请告诉我你想分析哪支股票？（提供股票代码或名称即可）"],
                "data_refs": [],
            }

        symbol = resolved["symbol"]
        facts = _build_symbol_fact_bundle(symbol, trade_date=resolved_trade_date)
        name = resolved.get("name") or facts.get("name") or market_adapter.get_symbol_name(symbol)
        dossier_payload = dict(facts.get("dossier_payload") or {})
        event_context = dict(facts.get("event_context") or {})
        research = dict(facts.get("research") or {})
        behavior_profile = dict(facts.get("behavior_profile") or {})
        market_snapshot = dict(facts.get("market_snapshot") or {})
        sector_relative = dict(facts.get("sector_relative") or {})
        market_relative = dict(facts.get("market_relative") or {})
        position_item = dict(facts.get("position_item") or {})
        precheck_item = dict(facts.get("precheck_item") or {})
        tail_review = dict(facts.get("tail_review") or {})
        case_item = facts.get("candidate_case")
        selected_reason = str(facts.get("selected_reason") or "").strip()
        advice = _build_symbol_trade_advice(facts, topic="symbol_analysis")
        market_texture = _build_symbol_market_texture(symbol)
        facts["market_texture"] = market_texture
        latest_price = float(market_snapshot.get("last_price") or 0.0)
        change_pct = market_snapshot.get("change_pct")
        sector_tags = sector_relative.get("sector_tags") or []
        relative_strength = market_relative.get("relative_strength_vs_benchmark")

        if latest_price <= 0:
            try:
                snapshots = market_adapter.get_snapshots([symbol])
                if snapshots:
                    snapshot = snapshots[0]
                    latest_price = float(snapshot.last_price or 0.0)
                    if snapshot.pre_close:
                        change_pct = round((float(snapshot.last_price or 0.0) - float(snapshot.pre_close or 0.0)) / float(snapshot.pre_close) * 100, 2)
            except Exception:
                pass

        answer_lines = [f"{symbol} {name}。", "先给你做一版交易台临时体检："]
        if latest_price > 0:
            price_line = f"最新价 {latest_price:.2f}"
            if change_pct not in (None, ""):
                price_line += f"，涨跌幅 {change_pct}%"
            answer_lines.append(price_line + "。")
        answer_lines.extend(_build_symbol_common_brief_lines(facts))
        if research.get("latest_titles") or facts.get("research_summary", {}).get("event_titles") or sector_tags:
            research_line_parts: list[str] = []
            if sector_tags:
                research_line_parts.append("板块线索=" + "、".join(str(item) for item in sector_tags[:3]))
            if research.get("latest_titles"):
                research_line_parts.append("事件标题=" + "；".join(str(item) for item in research.get("latest_titles", [])[:2]))
            elif facts.get("research_summary", {}).get("event_titles"):
                research_line_parts.append(
                    "研究跟踪="
                    + "；".join(str(item) for item in list(facts.get("research_summary", {}).get("event_titles") or [])[:2])
                )
            answer_lines.append("研究视角：" + "，".join(research_line_parts) + "。")
        _append_symbol_trade_advice_lines(answer_lines, advice)
        if precheck_item:
            answer_lines.append(
                "执行视角："
                + (
                    f"当前可进执行预检，预算余量={precheck_item.get('budget_value')}，单票余量={precheck_item.get('remaining_single_value')}。"
                    if precheck_item.get("approved")
                    else f"当前执行侧未完全放行，先关注 {precheck_item.get('primary_blocker_label') or '预检约束'}。"
                )
            )
        if market_texture.get("available"):
            answer_lines.append("风控视角：" + str(market_texture.get("risk_summary") or ""))
        if advice.get("next_actions"):
            answer_lines.append("下一步：" + "；".join(str(item) for item in list(advice.get("next_actions") or [])[:3]) + "。")
        if advice.get("trigger_conditions"):
            answer_lines.append("触发条件：" + "；".join(str(item) for item in list(advice.get("trigger_conditions") or [])[:3]) + "。")
        if advice.get("risk_notes"):
            answer_lines.append("风险提示：" + "；".join(str(item) for item in list(advice.get("risk_notes") or [])[:3]) + "。")
        if not dossier_payload.get("available"):
            answer_lines.append("当前这只票还没有完整 dossier，我是按快照、K 线、研究摘要和执行事实做的临时体检；如果你要更深，我可以继续升级成正式讨论或机会提案。")
        elif event_context.get("event_count") == 0 and not sector_tags:
            answer_lines.append("当前 dossier 里事件和板块证据偏少，结论可信度有限。")
        elif case_item or precheck_item or position_item or tail_review.get("available"):
            answer_lines.append("这只票当前已能从研究、讨论、执行或持仓链路中抽到真实事实；你可以继续追问仓位、做T、替换、风控，或者让我组织多视角讨论。")

        return {
            "topic": "symbol_analysis",
            "trade_date": resolved_trade_date,
            "question": question,
            "matched": True,
            "symbol": symbol,
            "name": name,
            "answer_lines": answer_lines,
            "data_refs": [
                f"/data/dossiers/latest",
                f"/data/symbol-contexts/latest",
                "/system/research/summary",
                f"/system/discussions/execution-precheck?trade_date={resolved_trade_date}",
                "/system/tail-market/review",
                "/system/execution-reconciliation/latest",
            ],
            "dossier_available": bool(dossier_payload.get("available")),
            "analysis_mode": "dossier_backed" if dossier_payload.get("available") else "ad_hoc",
            "symbol_facts": facts,
            "market_texture": market_texture,
            "trade_advice": advice,
        }

    def _build_symbol_focus_answer(
        question: str,
        *,
        trade_date: str | None = None,
        topic: str,
    ) -> dict[str, Any]:
        resolved_trade_date = _resolve_reference_trade_date(trade_date)
        resolved = _resolve_symbol_from_question(question, trade_date=resolved_trade_date)
        if not resolved:
            return {
                "topic": topic,
                "trade_date": resolved_trade_date,
                "question": question,
                "matched": False,
                "answer_lines": ["请先告诉我是哪一只股票。（提供股票代码或名称即可）"],
                "data_refs": [],
            }

        symbol = resolved["symbol"]
        facts = _build_symbol_fact_bundle(symbol, trade_date=resolved_trade_date)
        name = resolved.get("name") or facts.get("name") or market_adapter.get_symbol_name(symbol)
        answer_lines = [f"{symbol} {name}。"]
        data_refs: list[str] = []
        payload: dict[str, Any] = {
            "topic": topic,
            "trade_date": resolved_trade_date,
            "question": question,
            "matched": True,
            "symbol": symbol,
            "name": name,
        }
        position_item = dict(facts.get("position_item") or {})
        precheck = dict(facts.get("execution_precheck") or {})
        precheck_item = dict(facts.get("precheck_item") or {})
        tail_review = dict(facts.get("tail_review") or {})
        case_item = facts.get("candidate_case")
        other_candidates = list(facts.get("other_candidates") or [])
        advice = _build_symbol_trade_advice(facts, topic=topic)
        facts["market_texture"] = _build_symbol_market_texture(symbol)

        if topic == "holding_review":
            account_state = dict(facts.get("account_state") or {})
            latest_reconciliation = dict(facts.get("execution_reconciliation") or {})
            common_lines = _build_symbol_common_brief_lines(facts, include_market_texture=False)
            answer_lines.extend(common_lines or ["当前正式持仓快照里未发现这只票。"])
            _append_symbol_trade_advice_lines(answer_lines, advice)
            if latest_reconciliation:
                answer_lines.extend(list(latest_reconciliation.get("summary_lines") or [])[:2])
            data_refs = [
                "/system/account-state",
                "/system/execution-reconciliation/latest",
            ]
            payload["account_state"] = account_state
            payload["execution_reconciliation"] = latest_reconciliation
        elif topic == "day_trading":
            latest_tail_market = dict(facts.get("tail_market_latest") or {})
            answer_lines.extend(_build_symbol_common_brief_lines(facts, include_market_texture=False))
            if tail_review.get("available"):
                for item in list(tail_review.get("items") or [])[:3]:
                    answer_lines.append(
                        f"信号: {item.get('exit_reason') or 'signal'} tags={','.join(str(tag) for tag in list(item.get('review_tags') or [])[:3]) or 'none'}。"
                    )
            if position_item and tail_review.get("available"):
                answer_lines.append("这只票当前已有持仓与日内信号交叉事实，可优先做盘中复核而不是泛化打分。")
            _append_symbol_trade_advice_lines(answer_lines, advice)
            data_refs = [
                "/system/tail-market/latest",
                "/system/tail-market/review",
            ]
            payload["tail_market_latest"] = latest_tail_market
            payload["tail_market_review"] = tail_review
        elif topic in {"replacement", "risk", "position"}:
            if resolved_trade_date:
                if topic == "position":
                    answer_lines.extend(_build_symbol_common_brief_lines(facts, include_market_texture=False) or ["当前持仓快照里未发现该票占位。"])
                    payload["account_state"] = dict(facts.get("account_state") or {})
                    payload["execution_reconciliation"] = dict(facts.get("execution_reconciliation") or {})
                elif topic == "replacement":
                    answer_lines.extend(_build_symbol_common_brief_lines(facts, include_market_texture=False))
                if precheck_item and topic != "position":
                    answer_lines.append(
                        f"执行视角: approved={precheck_item.get('approved')} budget={precheck_item.get('budget_value')} "
                        f"single_remaining={precheck_item.get('remaining_single_value')}。"
                    )
                    if precheck_item.get("primary_blocker_label"):
                        answer_lines.append(f"主要阻断: {precheck_item.get('primary_blocker_label')}。")
                else:
                    answer_lines.append("当前执行预检里没有这只票的逐票记录。")
                if topic == "replacement":
                    brief = _build_client_brief(resolved_trade_date) if candidate_case_service else {}
                    if case_item:
                        answer_lines.append(
                            f"当前讨论定位: {getattr(case_item, 'final_status', 'unknown')}，主理由={str(getattr(case_item, 'selected_reason', '') or '未写入')}。"
                        )
                    if precheck_item and precheck_item.get("approved"):
                        answer_lines.append("执行侧当前没有硬阻断，这只票不构成被迫替换对象，除非出现更强催化或更高性价比候选。")
                    elif precheck_item and precheck_item.get("primary_blocker_label"):
                        answer_lines.append(f"执行侧存在明确压力：{precheck_item.get('primary_blocker_label')}，可把它列入被替换复核对象。")
                    elif not position_item:
                        answer_lines.append("当前未发现这只票的实际持仓，占位上不属于优先替换对象。")
                    if other_candidates:
                        compare_lines = []
                        for item in other_candidates[:3]:
                            compare_lines.append(
                                f"{item.get('symbol')} {item.get('name') or item.get('symbol')}[{item.get('final_status')}]"
                                + (f" 理由={item.get('selected_reason')}" if item.get("selected_reason") else "")
                            )
                        answer_lines.append("替换时应先与这些观察候选比较：" + "；".join(compare_lines))
                    else:
                        watchlist_lines = list((brief or {}).get("watchlist_lines") or [])
                        if watchlist_lines:
                            answer_lines.append("替换时应先与这些观察候选比较：" + "；".join(watchlist_lines[:3]))
                    _append_symbol_trade_advice_lines(answer_lines, advice)
                    payload["client_brief"] = brief
                    data_refs.append(f"/system/discussions/client-brief?trade_date={resolved_trade_date}")
                elif topic == "position":
                    _append_symbol_trade_advice_lines(answer_lines, advice)
                data_refs.append(f"/system/discussions/execution-precheck?trade_date={resolved_trade_date}")
                payload["execution_precheck"] = precheck

        payload["answer_lines"] = answer_lines
        payload["data_refs"] = data_refs
        payload["trade_advice"] = advice
        return payload

    def _answer_feishu_question(question: str, trade_date: str | None = None) -> dict[str, Any]:
        resolved_trade_date = _resolve_reference_trade_date(trade_date)
        normalized = str(question or "").strip().lower()
        topic = "help"
        if _looks_like_casual_chat(question):
            return _build_casual_chat_answer(question, trade_date=resolved_trade_date)
        symbol_analysis = _build_symbol_analysis_answer(question, trade_date=resolved_trade_date)
        symbol_semantic_topic = None
        if any(keyword in normalized for keyword in ("持仓复核", "持仓明细", "当前持仓", "有哪些持仓", "持仓列表")):
            symbol_semantic_topic = "holding_review"
        elif any(keyword in normalized for keyword in ("日内t", "做t", "t机会", "高抛低吸", "t+0", "尾盘处理")):
            symbol_semantic_topic = "day_trading"
        elif any(keyword in normalized for keyword in ("替换建议", "换仓", "替换仓位", "更好的机会替换", "有没有更好的替换")):
            symbol_semantic_topic = "replacement"
        elif any(keyword in normalized for keyword in ("风控", "阻断", "风险", "仓位理由", "为什么不能买", "为什么不能上")):
            symbol_semantic_topic = "risk"
        elif any(keyword in normalized for keyword in ("当前仓位", "仓位为什么", "仓位情况", "持仓怎么样")):
            symbol_semantic_topic = "position"
        if symbol_semantic_topic:
            symbol_focus = _build_symbol_focus_answer(
                question,
                trade_date=resolved_trade_date,
                topic=symbol_semantic_topic,
            )
            if symbol_focus.get("matched"):
                return symbol_focus
        if symbol_analysis.get("matched"):
            return symbol_analysis
        if any(keyword in normalized for keyword in ("分析", "怎么看", "怎么样", "研究")) and any(
            keyword in str(question or "") for keyword in ("股票", "个股", "这支", "这只", "代码", "名称")
        ):
            return symbol_analysis
        if any(keyword in normalized for keyword in ("夜间沙盘", "沙盘推演", "sandbox")):
            topic = "sandbox"
        elif (
            any(keyword in normalized for keyword in ("下周", "周一", "昨日盘面", "昨天盘面", "板块轮动", "热点", "催化"))
            and any(keyword in normalized for keyword in ("推荐", "方向", "机会", "优先"))
        ):
            topic = "sandbox"
        elif any(keyword in normalized for keyword in ("执行", "回执", "报单", "下单", "成交", "预演")):
            topic = "execution"
        elif any(keyword in normalized for keyword in ("持仓复核", "持仓明细", "当前持仓", "有哪些持仓", "持仓列表")):
            topic = "holding_review"
        elif any(keyword in normalized for keyword in ("日内t", "做t", "t机会", "高抛低吸", "t+0", "尾盘处理")):
            topic = "day_trading"
        elif any(keyword in normalized for keyword in ("持仓复核", "当前仓位", "仓位为什么", "仓位情况", "持仓怎么样")):
            topic = "position"
        elif any(keyword in normalized for keyword in ("替换建议", "换仓", "替换仓位", "更好的机会替换", "有没有更好的替换")):
            topic = "replacement"
        elif any(keyword in normalized for keyword in ("机会票", "新机会", "还有什么机会", "有哪些机会")):
            topic = "opportunity"
        elif any(keyword in normalized for keyword in ("活跃", "催办", "监督", "怠工", "谁在忙", "谁没工作")):
            topic = "supervision"
        elif any(keyword in normalized for keyword in ("风控", "阻断", "风险", "仓位理由", "为什么不能买", "为什么不能上")):
            topic = "risk"
        elif any(keyword in normalized for keyword in ("研究结论", "催化", "消息面", "新闻", "公告", "研判")):
            topic = "research"
        elif any(keyword in normalized for keyword in ("cycle", "讨论周期", "收敛周期", "round", "轮次")):
            topic = "cycle"
        elif any(keyword in normalized for keyword in ("参数", "调参", "阈值", "仓位", "提案", "治理")):
            topic = "params"
        elif any(keyword in normalized for keyword in ("评分", "分数", "考核", "权重", "agent")):
            topic = "scores"
        elif any(keyword in normalized for keyword in ("推荐", "候选", "讨论", "观察", "淘汰", "入选", "机会票", "新机会")):
            topic = "discussion"
        elif any(keyword in normalized for keyword in ("状态", "总览", "概况", "现在", "盘面", "运行")):
            topic = "status"
        elif any(keyword in normalized for keyword in ("能做什么", "会什么", "怎么用", "帮什么", "支持什么")):
            topic = "help"

        answer_lines: list[str] = []
        data_refs: list[str] = []
        payload: dict[str, Any] = {"topic": topic, "trade_date": resolved_trade_date}

        if topic == "status":
            briefing = _build_feishu_briefing_payload(resolved_trade_date)
            answer_lines = _build_status_answer_lines(briefing)
            data_refs = briefing.get("data_refs", [])
            payload["briefing"] = briefing
        elif topic == "cycle":
            if resolved_trade_date and candidate_case_service:
                brief = _build_client_brief(resolved_trade_date)
                answer_lines = _build_cycle_answer_lines(brief)
                data_refs = [
                    f"/system/discussions/cycles/{resolved_trade_date}",
                    f"/system/discussions/client-brief?trade_date={resolved_trade_date}",
                ]
                payload["client_brief"] = brief
                payload["cycle"] = brief.get("cycle") or {}
            else:
                answer_lines = ["当前没有可确认的 discussion cycle。"]
        elif topic == "discussion":
            if resolved_trade_date and candidate_case_service:
                brief = _build_client_brief(resolved_trade_date)
                answer_lines = _build_discussion_answer_lines(brief)
                data_refs = [
                    f"/system/discussions/client-brief?trade_date={resolved_trade_date}",
                    f"/system/discussions/meeting-context?trade_date={resolved_trade_date}",
                ]
                payload["client_brief"] = brief
            else:
                answer_lines = ["当前没有可确认的 discussion trade_date。"]
        elif topic == "opportunity":
            if resolved_trade_date and candidate_case_service:
                brief = _build_client_brief(resolved_trade_date)
                answer_lines = _build_opportunity_answer_lines(brief)
                data_refs = [
                    f"/system/discussions/client-brief?trade_date={resolved_trade_date}",
                    f"/system/discussions/reply-pack?trade_date={resolved_trade_date}",
                ]
                payload["client_brief"] = brief
            else:
                answer_lines = ["当前没有可确认的机会票 trade_date。"]
        elif topic == "replacement":
            if resolved_trade_date and candidate_case_service:
                brief = _build_client_brief(resolved_trade_date)
                precheck = _build_execution_precheck(resolved_trade_date)
                answer_lines = _build_replacement_answer_lines(brief, precheck)
                data_refs = [
                    f"/system/discussions/client-brief?trade_date={resolved_trade_date}",
                    f"/system/discussions/execution-precheck?trade_date={resolved_trade_date}",
                ]
                payload["client_brief"] = brief
                payload["execution_precheck"] = precheck
            else:
                answer_lines = ["当前没有可确认的替换建议 trade_date。"]
        elif topic == "execution":
            if resolved_trade_date:
                dispatch = _get_execution_dispatch_payload(resolved_trade_date)
                precheck = _build_execution_precheck(resolved_trade_date)
                answer_lines = _build_execution_answer_lines(precheck, dispatch)
                data_refs = [
                    f"/system/discussions/execution-precheck?trade_date={resolved_trade_date}",
                    f"/system/discussions/execution-dispatch/latest?trade_date={resolved_trade_date}",
                ]
                payload["execution_precheck"] = precheck
                payload["execution_dispatch"] = dispatch
            else:
                answer_lines = ["当前没有可确认的执行 trade_date。"]
        elif topic == "holding_review":
            resolved_account_id = _resolve_account_id()
            account_state = (
                account_state_service.snapshot(resolved_account_id, persist=False)
                if account_state_service
                else {"status": "unavailable", "summary_lines": ["account state service unavailable"]}
            )
            latest_reconciliation = meeting_state_store.get("latest_execution_reconciliation", {}) if meeting_state_store else {}
            reconciliation_positions = list(latest_reconciliation.get("positions") or [])
            account_positions = list(account_state.get("equity_positions") or account_state.get("positions") or [])
            positions = reconciliation_positions or account_positions
            answer_lines = _build_holding_review_answer_lines(account_state, latest_reconciliation)
            data_refs = [
                "/system/account-state",
                "/system/execution-reconciliation/latest",
            ]
            payload["account_state"] = account_state
            payload["execution_reconciliation"] = latest_reconciliation
        elif topic == "day_trading":
            latest_tail_market = meeting_state_store.get("latest_tail_market_scan", {}) if meeting_state_store else {}
            tail_review = _build_tail_market_review_section(source="latest", limit=5)
            answer_lines = _build_day_trading_answer_lines(latest_tail_market, tail_review)
            data_refs = [
                "/system/tail-market/latest",
                "/system/tail-market/review",
            ]
            payload["tail_market_latest"] = latest_tail_market
            payload["tail_market_review"] = tail_review
        elif topic == "position":
            if resolved_trade_date:
                precheck = _build_execution_precheck(resolved_trade_date)
                resolved_account_id = _resolve_account_id()
                account_state = (
                    account_state_service.snapshot(resolved_account_id, persist=False)
                    if account_state_service
                    else {}
                )
                latest_reconciliation = meeting_state_store.get("latest_execution_reconciliation", {}) if meeting_state_store else {}
                answer_lines = _build_position_answer_lines(precheck, account_state, latest_reconciliation)
                data_refs = [
                    f"/system/discussions/execution-precheck?trade_date={resolved_trade_date}",
                    "/system/account-state",
                ]
                payload["execution_precheck"] = precheck
            else:
                answer_lines = ["当前没有可确认的仓位 trade_date。"]
        elif topic == "supervision":
            supervision = _build_agent_supervision_payload(resolved_trade_date)
            answer_lines = _build_supervision_answer_lines(supervision)
            data_refs = [
                f"/system/agents/supervision-board?trade_date={resolved_trade_date}&overdue_after_seconds=180",
            ]
            payload["supervision"] = supervision
        elif topic == "risk":
            if resolved_trade_date:
                precheck = _build_execution_precheck(resolved_trade_date)
                answer_lines = _build_risk_answer_lines(precheck)
                data_refs = [
                    f"/system/discussions/execution-precheck?trade_date={resolved_trade_date}",
                ]
                payload["execution_precheck"] = precheck
            else:
                answer_lines = ["当前没有可确认的风控 trade_date。"]
        elif topic == "research":
            summary = _research_summary()
            answer_lines = _build_research_answer_lines(summary)
            data_refs = [
                "/system/research/summary",
                (
                    f"/system/nightly-sandbox/latest?trade_date={resolved_trade_date}"
                    if resolved_trade_date
                    else "/system/nightly-sandbox/latest"
                ),
                "/system/workspace-context",
            ]
            payload["research_summary"] = summary
        elif topic == "sandbox":
            briefing = _build_feishu_briefing_payload(resolved_trade_date)
            summary = _research_summary()
            sandbox_payload = _build_nightly_sandbox_payload(resolved_trade_date)
            answer_lines = _build_sandbox_answer_lines(sandbox_payload, briefing, summary)
            data_refs = [
                (
                    f"/system/nightly-sandbox/latest?trade_date={resolved_trade_date}"
                    if resolved_trade_date
                    else "/system/nightly-sandbox/latest"
                ),
                (
                    f"/system/feishu/briefing?trade_date={resolved_trade_date}"
                    if resolved_trade_date
                    else "/system/feishu/briefing"
                ),
                "/system/research/summary",
            ]
            payload["briefing"] = briefing
            payload["research_summary"] = summary
            payload["nightly_sandbox"] = sandbox_payload
        elif topic == "params":
            proposals = [item.model_dump() for item in parameter_service.list_proposals()[:5]] if parameter_service else []
            answer_lines = _build_params_answer_lines(proposals)
            data_refs = [
                "/system/feishu/adjustments/natural-language",
                "/system/adjustments/natural-language",
                "/system/params/proposals",
            ]
            payload["proposals"] = proposals
        elif topic == "scores":
            score_date = resolved_trade_date or datetime.now().date().isoformat()
            scores = [item.model_dump() for item in agent_score_service.ensure_defaults(score_date)] if agent_score_service else []
            answer_lines = _build_scores_answer_lines(scores)
            data_refs = [f"/system/agent-scores?score_date={score_date}"]
            payload["scores"] = scores
        else:
            rights = _build_feishu_rights_payload(resolved_trade_date)
            answer_lines = [
                "我不是固定五类主题问答器。你可以直接问状态、执行、风控、参数、agent 活跃度、研究结论、机会票，也可以随手点名一只股票让我临时体检。",
                "如果你点名某只股票，我会优先组合研究、策略、风控、执行预检、持仓与日内信号来回答，而不是只回固定分类口径。",
                "你也可以直接下自然语言指令调参，例如：把测试仓位改到三成、今天先不买白酒股、逆回购保留改成 0。",
                "如果你只是闲聊、问候或测试在线状态，我也会自然回复；遇到交易问题再切到真实工具和协作链。",
            ]
            data_refs = rights.get("data_refs", [])
            payload["rights"] = rights

        if not answer_lines:
            answer_lines = ["当前没有足够真实数据回答这个问题。"]
        payload["answer_lines"] = answer_lines
        payload["data_refs"] = data_refs
        return payload

    def _parse_feishu_supervision_ack(
        text: str,
        *,
        fallback_agent_ids: list[str] | None = None,
        fallback_note: str = "",
    ) -> dict[str, Any]:
        normalized = str(text or "").strip()
        lowered = normalized.lower()
        ack_keywords = (
            "收到",
            "已收到",
            "收到催办",
            "已处理",
            "处理中",
            "转入处理",
            "已接手",
            "已确认",
            "ack",
            "acked",
            "ok",
            "done",
            "working",
        )
        agent_aliases = {
            "ashare-runtime": ["ashare-runtime", "runtime", "运行", "运行侧"],
            "ashare": ["ashare", "主持", "主协调", "协调", "主持人"],
            "ashare-research": ["ashare-research", "research", "研究", "研究员"],
            "ashare-strategy": ["ashare-strategy", "strategy", "策略"],
            "ashare-risk": ["ashare-risk", "risk", "风控"],
            "ashare-audit": ["ashare-audit", "audit", "审计", "复核"],
            "ashare-executor": ["ashare-executor", "executor", "执行", "交易执行"],
        }

        parsed_agent_ids: list[str] = []
        for agent_id, aliases in agent_aliases.items():
            if any(alias in lowered for alias in aliases):
                parsed_agent_ids.append(agent_id)

        agent_ids = list(dict.fromkeys(fallback_agent_ids or parsed_agent_ids))
        ack_detected = any(keyword in lowered for keyword in ack_keywords)
        note = fallback_note or normalized

        if fallback_agent_ids:
            return {
                "matched": True,
                "ack_detected": ack_detected or bool(normalized),
                "agent_ids": list(dict.fromkeys(fallback_agent_ids)),
                "note": note,
                "unmatched_reason": "",
            }
        if ack_detected and agent_ids:
            return {
                "matched": True,
                "ack_detected": True,
                "agent_ids": agent_ids,
                "note": note,
                "unmatched_reason": "",
            }
        if ack_detected and not normalized:
            return {
                "matched": True,
                "ack_detected": True,
                "agent_ids": agent_ids,
                "note": note,
                "unmatched_reason": "",
            }
        if ack_detected and not agent_ids:
            return {
                "matched": True,
                "ack_detected": True,
                "agent_ids": [],
                "note": note,
                "unmatched_reason": "",
            }
        return {
            "matched": False,
            "ack_detected": False,
            "agent_ids": agent_ids,
            "note": note,
            "unmatched_reason": "text_not_recognized_as_supervision_ack",
        }

    def _apply_agent_supervision_ack(
        *,
        trade_date: str | None,
        agent_ids: list[str] | None = None,
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        supervision = _build_agent_supervision_payload(trade_date)
        attention_items = list(supervision.get("attention_items") or [])
        attention_agent_ids = [str(item.get("agent_id") or "") for item in attention_items if str(item.get("agent_id") or "").strip()]
        target_agent_ids = agent_ids or attention_agent_ids
        acked_records = []
        skipped_agent_ids = []
        signature = str(supervision.get("attention_signature") or "")
        for agent_id in dict.fromkeys(target_agent_ids):
            normalized_agent_id = str(agent_id or "").strip()
            if not normalized_agent_id or normalized_agent_id not in attention_agent_ids:
                skipped_agent_ids.append(normalized_agent_id)
                continue
            acked_records.append(
                record_supervision_ack(
                    meeting_state_store,
                    supervision.get("trade_date"),
                    signature=signature,
                    agent_id=normalized_agent_id,
                    actor=actor,
                    note=note,
                )
            )
        refreshed = _build_agent_supervision_payload(trade_date)
        if audit_store:
            audit_store.append(
                category="supervision",
                message="Agent 监督确认已回写",
                payload={
                    "trade_date": refreshed.get("trade_date"),
                    "acked_agent_ids": [item.get("agent_id") for item in acked_records],
                    "skipped_agent_ids": skipped_agent_ids,
                    "actor": actor,
                },
            )
        return {
            "ok": True,
            "trade_date": refreshed.get("trade_date"),
            "attention_signature": refreshed.get("attention_signature"),
            "acked_count": len(acked_records),
            "acked_items": acked_records,
            "skipped_agent_ids": skipped_agent_ids,
            "supervision": refreshed,
        }

    def _extract_feishu_message_text(payload: dict[str, Any]) -> str:
        candidates = [
            ((payload.get("event") or {}).get("message") or {}).get("content"),
            ((payload.get("event") or {}).get("message") or {}).get("text"),
            (payload.get("event") or {}).get("text"),
            payload.get("text"),
        ]
        for value in candidates:
            if not value:
                continue
            if isinstance(value, str):
                raw = value.strip()
                if not raw:
                    continue
                if raw.startswith("{") and raw.endswith("}"):
                    try:
                        decoded = json.loads(raw)
                    except Exception:
                        return raw
                    if isinstance(decoded, dict):
                        text = str(decoded.get("text") or "").strip()
                        if text:
                            return text
                return raw
            if isinstance(value, dict):
                text = str(value.get("text") or "").strip()
                if text:
                    return text
        return ""

    def _extract_feishu_urls(text: str) -> list[str]:
        if not text:
            return []
        urls: list[str] = []
        for match in re.findall(r"https?://[^\s]+", text):
            cleaned = str(match or "").strip().rstrip("，。；！？、）]}>\"'")
            if cleaned and cleaned not in urls:
                urls.append(cleaned)
        return urls

    def _match_control_plane_endpoint(url: str) -> str:
        path = str(urlparse(url).path or "").rstrip("/")
        known_endpoints = (
            "/system/workflow/mainline",
            "/system/robot/console-layout",
            "/system/agents/supervision-board",
            "/system/feishu/briefing",
            "/system/feishu/rights",
            "/system/feishu/ask",
        )
        for endpoint in known_endpoints:
            if path.endswith(endpoint):
                return endpoint
        return ""

    def _build_control_plane_link_reply(text: str, trade_date: str | None = None) -> dict[str, Any]:
        for url in _extract_feishu_urls(text):
            endpoint = _match_control_plane_endpoint(url)
            if not endpoint:
                continue

            if endpoint == "/system/workflow/mainline":
                payload = _build_mainline_workflow_payload(trade_date)
                reply_lines = [
                    "这不是外部网页地址，它是程序内部的「交易主线流程」入口。",
                    f"核心阶段：{'、'.join(str(item.get('name') or '') for item in payload.get('stages', []) if str(item.get('name') or '').strip())}。",
                    *[str(line) for line in payload.get("principles", [])[:2]],
                ]
            elif endpoint == "/system/robot/console-layout":
                payload = _build_robot_console_layout(trade_date)
                reply_lines = [
                    "这不是外部网页地址，它是程序内部的「机器人控制台」入口。",
                    f"主要版面：{'、'.join(str(item.get('title') or '') for item in payload.get('sections', []) if str(item.get('title') or '').strip())}。",
                    *[str(line) for line in payload.get("summary_lines", [])[:2]],
                ]
            elif endpoint == "/system/agents/supervision-board":
                payload = _build_agent_supervision_payload(trade_date)
                reply_lines = [
                    "这是程序内部的 Agent 监督看板入口。",
                    *[str(line) for line in payload.get("summary_lines", [])[:3]],
                ]
            elif endpoint == "/system/feishu/briefing":
                payload = _build_feishu_briefing_payload(trade_date)
                reply_lines = [
                    "这是程序内部的飞书知情简报入口。",
                    *[str(line) for line in payload.get("summary_lines", [])[:4]],
                ]
            elif endpoint == "/system/feishu/rights":
                payload = _build_feishu_rights_payload(trade_date)
                reply_lines = [
                    "这是程序内部的飞书三权入口。",
                    *[str(line) for line in payload.get("summary_lines", [])[:4]],
                ]
            else:
                payload = _build_robot_console_layout(trade_date)
                reply_lines = [
                    "这是程序内部的飞书问答入口。",
                    "可直接问状态、推荐、执行、参数、评分、活跃度、机会票、仓位理由，也可直接发自然语言调参指令。",
                    f"机器人控制台主要版面：{'、'.join(str(item.get('title') or '') for item in payload.get('sections', []) if str(item.get('title') or '').strip())}。",
                ]

            cleaned_lines = [line for line in reply_lines if str(line or "").strip()]
            return {
                "matched": True,
                "endpoint": endpoint,
                "url": url,
                "reply_lines": cleaned_lines,
            }

        return {
            "matched": False,
            "endpoint": "",
            "url": "",
            "reply_lines": [],
        }

    def _is_feishu_message_addressed(payload: dict[str, Any], text: str = "") -> bool:
        message = (payload.get("event") or {}).get("message") or {}
        chat_type = str(message.get("chat_type") or "").strip().lower()
        if chat_type in {"p2p", "single", "private"}:
            return True
        mentions = message.get("mentions") or []
        for item in mentions:
            if str((item or {}).get("mentioned_type") or "").strip().lower() == "bot":
                return True
        if re.match(r"^\s*@\S+", str(text or "")):
            return True
        return "@_user_" in str(text or "")

    def _normalize_feishu_question_text(payload: dict[str, Any], text: str) -> str:
        normalized = str(text or "")
        message = (payload.get("event") or {}).get("message") or {}
        mentions = message.get("mentions") or []
        for item in mentions:
            key = str((item or {}).get("key") or "").strip()
            name = str((item or {}).get("name") or "").strip()
            if key:
                normalized = normalized.replace(key, " ")
            if name:
                normalized = normalized.replace(name, " ")
        normalized = re.sub(r"@_user_\d+", " ", normalized)
        normalized = re.sub(r"^\s*@\S+\s*", "", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    def _build_feishu_question_reply(
        question: str,
        trade_date: str | None = None,
        *,
        control_plane_base_url: str | None = None,
    ) -> dict[str, Any]:
        answer = _answer_feishu_question(question, trade_date=trade_date)
        answer_lines = [str(line or "").strip() for line in answer.get("answer_lines", []) if str(line or "").strip()]
        trade_advice = dict(answer.get("trade_advice") or {})
        reply_lines: list[str] = []
        if answer.get("symbol"):
            name = str(answer.get("name") or answer.get("symbol") or "").strip()
            symbol = str(answer.get("symbol") or "").strip()
            if symbol:
                reply_lines.append(f"{symbol}{f' {name}' if name and name != symbol else ''}")
        if trade_advice:
            reply_lines.append(
                f"建议级别={trade_advice.get('recommendation_level') or '-'} 立场={trade_advice.get('stance') or '-'}"
            )
            if trade_advice.get("summary"):
                reply_lines.append("结论: " + str(trade_advice.get("summary")))
            triggers = [str(item).strip() for item in list(trade_advice.get("trigger_conditions") or []) if str(item).strip()]
            if triggers:
                reply_lines.append("触发条件: " + "；".join(triggers[:2]))
            risks = [str(item).strip() for item in list(trade_advice.get("risk_notes") or []) if str(item).strip()]
            if risks:
                reply_lines.append("风险提示: " + "；".join(risks[:2]))
            next_actions = [str(item).strip() for item in list(trade_advice.get("next_actions") or []) if str(item).strip()]
            if next_actions:
                reply_lines.append("下一步: " + "；".join(next_actions[:2]))
        elif str(answer.get("topic") or "") == "opportunity":
            brief = dict(answer.get("client_brief") or {})
            reply_lines.append(
                "交易台机会卡: "
                f"selected={brief.get('selected_count', 0)} watchlist={brief.get('watchlist_count', 0)} rejected={brief.get('rejected_count', 0)}"
            )
            selected_lines = [str(item).strip() for item in list(brief.get("selected_lines") or []) if str(item).strip()]
            watchlist_lines = [str(item).strip() for item in list(brief.get("watchlist_lines") or []) if str(item).strip()]
            if selected_lines:
                reply_lines.append("优先机会: " + "；".join(selected_lines[:2]))
            if watchlist_lines:
                reply_lines.append("备选观察: " + "；".join(watchlist_lines[:2]))
        elif str(answer.get("topic") or "") == "replacement":
            brief = dict(answer.get("client_brief") or {})
            precheck = dict(answer.get("execution_precheck") or {})
            blocked_selected = [
                item
                for item in list(precheck.get("items") or [])
                if item.get("status") == "selected" and not item.get("approved")
            ]
            reply_lines.append(
                "交易台换仓卡: "
                f"watchlist={brief.get('watchlist_count', 0)} blocked_selected={len(blocked_selected)}"
            )
            watchlist_lines = [str(item).strip() for item in list(brief.get("watchlist_lines") or []) if str(item).strip()]
            if watchlist_lines:
                reply_lines.append("候选: " + "；".join(watchlist_lines[:2]))
        elif str(answer.get("topic") or "") == "position":
            precheck = dict(answer.get("execution_precheck") or {})
            reply_lines.append(
                "交易台仓位卡: "
                f"上限={precheck.get('equity_position_limit')} 单票={precheck.get('max_single_amount')} "
                f"剩余预算={precheck.get('stock_test_budget_remaining')}"
            )
            reply_lines.append(
                "持仓/占位: "
                f"{precheck.get('current_equity_position_count', 0)}/{precheck.get('max_hold_count', 0)}"
            )
            next_action = str(precheck.get("primary_recommended_next_action_label") or "").strip()
            if next_action:
                reply_lines.append("建议动作: " + next_action)
        elif str(answer.get("topic") or "") == "execution":
            dispatch = dict(answer.get("execution_dispatch") or {})
            precheck = dict(answer.get("execution_precheck") or {})
            reply_lines.append(
                "执行卡: "
                f"precheck通过={precheck.get('approved_count', 0)} 阻断={precheck.get('blocked_count', 0)} "
                f"dispatch={dispatch.get('status') or 'unknown'}"
            )
            reply_lines.append(
                "派发结果: "
                f"submitted={dispatch.get('submitted_count', 0)} preview={dispatch.get('preview_count', 0)} blocked={dispatch.get('blocked_count', 0)}"
            )
            if dispatch.get("degrade_reason"):
                reply_lines.append("阻断原因: " + str(dispatch.get("degrade_reason")))
        elif str(answer.get("topic") or "") == "holding_review":
            reconciliation = dict(answer.get("execution_reconciliation") or {})
            positions = list(reconciliation.get("positions") or [])
            reply_lines.append(f"交易台持仓卡: positions={len(positions)}")
            if reconciliation.get("summary_lines"):
                reply_lines.append("对账: " + "；".join(str(item).strip() for item in list(reconciliation.get("summary_lines") or [])[:2] if str(item).strip()))
        elif str(answer.get("topic") or "") == "day_trading":
            tail_review = dict(answer.get("tail_market_review") or {})
            review_items = list((tail_review.get("review") or {}).get("items") or tail_review.get("items") or [])
            reply_lines.append(f"交易台做T卡: signals={len(review_items)}")
            if tail_review.get("summary_lines"):
                reply_lines.append("信号: " + "；".join(str(item).strip() for item in list(tail_review.get("summary_lines") or [])[:2] if str(item).strip()))
        elif str(answer.get("topic") or "") == "risk":
            precheck = dict(answer.get("execution_precheck") or {})
            reply_lines.append(
                "交易台风控卡: "
                f"blocked={precheck.get('blocked_count', 0)} approved={precheck.get('approved_count', 0)}"
            )
            blocked_items = [item for item in list(precheck.get("items") or []) if not item.get("approved")]
            if blocked_items:
                lead = blocked_items[0]
                reply_lines.append(
                    "首要阻断: "
                    f"{lead.get('symbol')} {lead.get('primary_blocker_label') or lead.get('primary_blocker')}"
                )
            else:
                reply_lines.append("首要阻断: 当前未见新增硬阻断")
            next_action = str(precheck.get("primary_recommended_next_action_label") or "").strip()
            if next_action:
                reply_lines.append("建议动作: " + next_action)
        elif str(answer.get("topic") or "") == "status":
            briefing = dict(answer.get("briefing") or {})
            cadence = dict(briefing.get("cadence") or {})
            client_brief = dict(briefing.get("client_brief") or {})
            execution_dispatch = dict(briefing.get("execution_dispatch") or {})
            reply_lines.append(
                "交易台状态卡: "
                f"trade_date={briefing.get('trade_date') or '-'} "
                f"selected={client_brief.get('selected_count', 0)} "
                f"watchlist={client_brief.get('watchlist_count', 0)}"
            )
            if cadence.get("summary_lines"):
                reply_lines.append("节奏: " + "；".join(str(item).strip() for item in list(cadence.get("summary_lines") or [])[:2] if str(item).strip()))
            if execution_dispatch:
                reply_lines.append(
                    "执行概况: "
                    f"status={execution_dispatch.get('status') or 'unknown'} submitted={execution_dispatch.get('submitted_count', 0)} preview={execution_dispatch.get('preview_count', 0)}"
                )
        elif str(answer.get("topic") or "") == "discussion":
            brief = dict(answer.get("client_brief") or {})
            reply_lines.append(
                "交易台讨论卡: "
                f"selected={brief.get('selected_count', 0)} watchlist={brief.get('watchlist_count', 0)} rejected={brief.get('rejected_count', 0)}"
            )
            selected_lines = [str(item).strip() for item in list(brief.get("selected_lines") or []) if str(item).strip()]
            watchlist_lines = [str(item).strip() for item in list(brief.get("watchlist_lines") or []) if str(item).strip()]
            if selected_lines:
                reply_lines.append("入选: " + "；".join(selected_lines[:2]))
            if watchlist_lines:
                reply_lines.append("观察: " + "；".join(watchlist_lines[:2]))
        elif str(answer.get("topic") or "") == "research":
            summary = dict(answer.get("research_summary") or {})
            reply_lines.append(
                "交易台研究卡: "
                f"symbols={len(list(summary.get('symbols') or []))} news={summary.get('news_count', 0)} announcements={summary.get('announcement_count', 0)}"
            )
            event_titles = [str(item).strip() for item in list(summary.get("event_titles") or []) if str(item).strip()]
            if event_titles:
                reply_lines.append("最近催化: " + "；".join(event_titles[:2]))
        elif str(answer.get("topic") or "") == "sandbox":
            sandbox_payload = dict(answer.get("nightly_sandbox") or {})
            briefing = dict(answer.get("briefing") or {})
            research_summary = dict(answer.get("research_summary") or {})
            reply_lines.append(
                "交易台沙盘卡: "
                f"trade_date={sandbox_payload.get('trade_date') or briefing.get('trade_date') or '-'} "
                f"priorities={len(list(sandbox_payload.get('tomorrow_priorities') or []))}"
            )
            summary_lines = [str(item).strip() for item in list(sandbox_payload.get("summary_lines") or []) if str(item).strip()]
            if summary_lines:
                reply_lines.append("推演结论: " + "；".join(summary_lines[:2]))
            priorities = [str(item).strip() for item in list(sandbox_payload.get("tomorrow_priorities") or []) if str(item).strip()]
            if priorities:
                reply_lines.append("次日优先: " + "；".join(priorities[:3]))
            event_titles = [str(item).strip() for item in list(research_summary.get("event_titles") or []) if str(item).strip()]
            if event_titles:
                reply_lines.append("催化跟踪: " + "；".join(event_titles[:2]))
        elif str(answer.get("topic") or "") == "params":
            proposals = [dict(item) for item in list(answer.get("proposals") or [])]
            reply_lines.append(f"交易台参数卡: 提案数={len(proposals)}")
            if proposals:
                lead = proposals[0]
                reply_lines.append(
                    "最近提案: "
                    f"{lead.get('param_key')} -> {lead.get('new_value')} ({lead.get('status')})"
                )
            reply_lines.append("调参入口: /system/feishu/adjustments/natural-language")
        elif str(answer.get("topic") or "") == "scores":
            scores = [dict(item) for item in list(answer.get("scores") or [])]
            reply_lines.append(f"交易台评分卡: agent_count={len(scores)}")
            if scores:
                lead = scores[0]
                reply_lines.append(
                    "最高优先显示: "
                    f"{lead.get('agent_id')} score={lead.get('new_score')} weight={lead.get('weight_value')}"
                )
        elif str(answer.get("topic") or "") == "supervision":
            supervision = dict(answer.get("supervision") or {})
            attention_items = [dict(item) for item in list(supervision.get("attention_items") or [])]
            notify_items = [dict(item) for item in list(supervision.get("notify_items") or [])]
            reply_lines.append(
                "监督卡: "
                f"attention={len(attention_items)} notify={len(notify_items)} trade_date={supervision.get('trade_date') or '-'}"
            )
            if notify_items:
                lead = notify_items[0]
                reply_lines.append(
                    "当前催办: "
                    f"{lead.get('agent_id')} {lead.get('supervision_tier') or lead.get('status') or '待处理'}"
                )
            elif attention_items:
                lead = attention_items[0]
                reply_lines.append(
                    "监督关注: "
                    f"{lead.get('agent_id')} {lead.get('status') or 'attention'}"
                )
            elif supervision.get("summary_lines"):
                reply_lines.append(
                    "当前状态: "
                    + "；".join(str(item).strip() for item in list(supervision.get("summary_lines") or [])[:2] if str(item).strip())
                )
        if not reply_lines:
            reply_lines = answer_lines
        card_title = f"飞书问答 · {str(answer.get('topic') or 'help')}"
        card_markdown_lines = [f"**问题**: {question}"]
        card_markdown_lines.extend(f"- {line}" for line in reply_lines if str(line).strip())
        raw_data_refs = [str(item).strip() for item in list(answer.get("data_refs") or []) if str(item).strip()]
        card_data_refs = _build_feishu_card_urls(raw_data_refs, control_plane_base_url=control_plane_base_url)
        if card_data_refs:
            card_markdown_lines.append("**相关入口**")
            card_markdown_lines.extend(f"- {item}" for item in card_data_refs[:4])
        template_map = {
            "symbol_analysis": "blue",
            "position": "blue",
            "holding_review": "blue",
            "day_trading": "wathet",
            "opportunity": "green",
            "discussion": "green",
            "execution": "orange",
            "risk": "red",
            "supervision": "orange",
            "research": "indigo",
            "sandbox": "carmine",
            "params": "purple",
            "scores": "turquoise",
            "status": "grey",
        }
        card_elements: list[dict[str, Any]] = [
            {
                "tag": "markdown",
                "content": f"**问题**: {question}",
            }
        ]
        field_items = [str(line).strip() for line in reply_lines if str(line).strip()]
        if field_items:
            card_elements.append(
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": False,
                            "text": {"tag": "lark_md", "content": line},
                        }
                        for line in field_items[:6]
                    ],
                }
            )
        if trade_advice:
            advice_bits = []
            if trade_advice.get("recommendation_level"):
                advice_bits.append(f"建议级别: {trade_advice.get('recommendation_level')}")
            if trade_advice.get("stance"):
                advice_bits.append(f"立场: {trade_advice.get('stance')}")
            if trade_advice.get("summary"):
                advice_bits.append(f"结论: {trade_advice.get('summary')}")
            if advice_bits:
                card_elements.append(
                    {
                        "tag": "note",
                        "elements": [
                            {"tag": "plain_text", "content": " | ".join(advice_bits[:3])}
                        ],
                    }
                )
        if card_data_refs:
            card_elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": f"入口{i + 1}"},
                            "type": "default",
                            "url": ref,
                        }
                        for i, ref in enumerate(card_data_refs[:2])
                    ],
                }
            )
        card_payload = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template_map.get(str(answer.get("topic") or "help"), "blue"),
                "title": {"tag": "plain_text", "content": card_title},
            },
            "elements": card_elements,
        }
        return {
            "matched": True,
            "question": question,
            "topic": str(answer.get("topic") or "help"),
            "reply_lines": reply_lines,
            "answer_lines": answer_lines,
            "data_refs": raw_data_refs,
            "card_data_refs": card_data_refs,
            "trade_advice": trade_advice,
            "reply_card": {
                "title": card_title,
                "markdown": "\n".join(card_markdown_lines),
                "card": card_payload,
            },
        }

    def _extract_feishu_actor(payload: dict[str, Any]) -> str:
        sender = (payload.get("event") or {}).get("sender") or {}
        sender_id = sender.get("sender_id") or {}
        for key in ("user_id", "open_id", "union_id"):
            value = str(sender_id.get(key) or "").strip()
            if value:
                return f"feishu-user/{value}"
        sender_type = str(sender.get("sender_type") or "").strip()
        if sender_type:
            return f"feishu-{sender_type}"
        return "feishu-event"

    def _extract_feishu_chat_id(payload: dict[str, Any]) -> str:
        message = (payload.get("event") or {}).get("message") or {}
        return str(message.get("chat_id") or payload.get("chat_id") or "").strip()

    def _extract_feishu_verification_token(payload: dict[str, Any]) -> str:
        header = payload.get("header") or {}
        candidates = [
            payload.get("token"),
            header.get("token"),
            (payload.get("event") or {}).get("token"),
        ]
        for value in candidates:
            token = str(value or "").strip()
            if token:
                return token
        return ""

    def _extract_feishu_preview_url(payload: dict[str, Any]) -> str:
        event = payload.get("event") or {}
        context = event.get("context") or {}
        candidates = [
            context.get("url") if isinstance(context, dict) else "",
            event.get("url"),
            payload.get("url"),
        ]
        for value in candidates:
            preview_url = str(value or "").strip()
            if preview_url:
                return preview_url
        return ""

    def _build_feishu_link_preview_response(payload: dict[str, Any]) -> dict[str, Any]:
        preview_url = _extract_feishu_preview_url(payload)
        title = "A 股量化控制面"
        summary = "程序提供数据、执行、监督和风控围栏，agent 负责自主协作与交易判断。"

        if "/system/feishu/briefing" in preview_url:
            title = "飞书知情简报"
            summary = "查看当前状态、执行状态、调参与 agent 得分摘要。"
        elif "/system/agents/supervision-board" in preview_url:
            title = "Agent 监督看板"
            summary = "查看谁该工作、是否超时、是否已收到催办并确认回写。"
        elif "/system/workflow/mainline" in preview_url:
            title = "交易主线流程"
            summary = "盘前预热、盘中发现、讨论收敛、执行预演、盘后学习与夜间沙盘。"
        elif "/system/robot/console-layout" in preview_url:
            title = "机器人控制台"
            summary = "当前状态、当前建议、执行状态、调参与治理、问答入口。"
        elif "/system/feishu/ask" in preview_url:
            title = "飞书问答入口"
            summary = "可按状态、讨论、执行、参数、得分五类口径追问系统。"

        return {
            "inline": {
                "title": title,
                "summary": summary,
            }
        }

    def _build_feishu_event_subscription_config(request: Request) -> dict[str, Any]:
        public_base_url = str(settings.service.public_base_url or "").strip().rstrip("/")
        request_base_url = str(request.base_url).rstrip("/")
        resolved_base_url = public_base_url or request_base_url
        callback_path = "/system/feishu/events"
        return {
            "callback_path": callback_path,
            "callback_url": f"{resolved_base_url}{callback_path}",
            "public_base_url": public_base_url,
            "request_base_url": request_base_url,
            "verification_token_configured": bool(str(settings.notify.feishu_verification_token or "").strip()),
            "expected_event_types": [
                "im.message.receive_v1",
                "url.preview.get",
            ],
            "supported_flows": [
                "url_verification",
                "message_receive_to_supervision_ack",
                "link_preview_inline",
            ],
            "required_env": [
                "ASHARE_PUBLIC_BASE_URL",
                "ASHARE_FEISHU_VERIFICATION_TOKEN",
            ],
            "summary_lines": [
                "飞书后台事件订阅回调地址应指向 callback_url。",
                "若控制面部署在反向代理后，请优先配置 ASHARE_PUBLIC_BASE_URL，避免回调地址暴露为本地 127.0.0.1。",
                "当前入站主要消费 im.message.receive_v1，用于把群内催办回执自动转成 supervision ack。",
                "若飞书后台启用了链接预览能力，可继续订阅 url.preview.get，由本接口返回 inline 预览摘要。",
            ],
        }

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
        return _build_operations_components_payload()

    @router.get("/operations/health-check")
    async def operations_health_check():
        script_path = settings.workspace / "scripts" / "health_check.sh"

        def _run_health_check() -> dict[str, Any]:
            checked_at = datetime.now().isoformat()
            if not script_path.exists():
                return {
                    "ok": False,
                    "status": "missing",
                    "exit_code": 127,
                    "script_path": str(script_path),
                    "checked_at": checked_at,
                    "summary_lines": [f"巡检脚本不存在: {script_path}"],
                    "output_lines": [],
                }
            try:
                completed = subprocess.run(
                    ["bash", str(script_path)],
                    cwd=str(settings.workspace),
                    capture_output=True,
                    text=True,
                    timeout=25,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                merged_output = "\n".join(
                    [
                        str(exc.stdout or "").strip(),
                        str(exc.stderr or "").strip(),
                    ]
                ).strip()
                output_lines = [line for line in merged_output.splitlines() if line.strip()]
                return {
                    "ok": False,
                    "status": "timeout",
                    "exit_code": 124,
                    "script_path": str(script_path),
                    "checked_at": checked_at,
                    "summary_lines": ["统一巡检超时，已按 25 秒中断。", *output_lines[-2:]],
                    "output_lines": output_lines,
                }

            merged_output = "\n".join(
                [
                    str(completed.stdout or "").strip(),
                    str(completed.stderr or "").strip(),
                ]
            ).strip()
            output_lines = [line for line in merged_output.splitlines() if line.strip()]
            display_lines = [
                line for line in output_lines
                if line.startswith("[ashare-v2]") and line not in {"[ashare-v2] 配置与适配器检查:"}
            ]
            return {
                "ok": completed.returncode == 0,
                "status": "ok" if completed.returncode == 0 else "failed",
                "exit_code": completed.returncode,
                "script_path": str(script_path),
                "checked_at": checked_at,
                "summary_lines": display_lines[-4:] if display_lines else [f"巡检完成，exit_code={completed.returncode}。"],
                "output_lines": output_lines,
            }

        return await run_in_threadpool(_run_health_check)

    @router.get("/healthcheck")
    async def healthcheck():
        result = EnvironmentHealthcheck(settings).run()
        return {"ok": result.ok, "checks": result.checks}

    @router.get("/readiness")
    async def readiness(account_id: str | None = None):
        payload = await run_in_threadpool(_build_readiness, account_id)
        return {"ok": payload["status"] != "blocked", **payload}

    @router.get("/deployment/bootstrap-contracts")
    async def deployment_bootstrap_contracts(request: Request, account_id: str | None = None):
        payload = _build_deployment_bootstrap_contracts(account_id=account_id)
        api_base_url = _resolve_control_plane_base_url(request=request)
        if not isinstance(payload.get("execution_bridge_deployment_contract_sample"), dict):
            payload["execution_bridge_deployment_contract_sample"] = _sanitize_json_compatible(
                payload.get("deployment_contract_sample")
                or build_execution_bridge_health_deployment_contract_sample(api_base_url=api_base_url)
            )
        return {"ok": payload["readiness"]["status"] != "blocked", **payload}

    @router.get("/deployment/linux-control-plane-startup-checklist")
    async def linux_control_plane_startup_checklist(account_id: str | None = None):
        payload = _build_linux_control_plane_startup_checklist(account_id=account_id)
        return {"ok": payload["status"] != "blocked", **payload}

    @router.get("/deployment/controlled-apply-readiness")
    async def controlled_apply_readiness(
        trade_date: str | None = None,
        account_id: str | None = None,
        max_apply_intents: int = 1,
        intent_ids: str | None = None,
        allowed_symbols: str | None = None,
        require_live: bool = True,
        require_trading_session: bool = True,
        max_equity_position_limit: float | None = 0.2,
        max_single_amount: float | None = 50000.0,
        min_reverse_repo_reserved_amount: float | None = 70000.0,
        max_stock_test_budget_amount: float | None = None,
        max_apply_submissions_per_day: int | None = None,
        blocked_time_windows: str | None = None,
        include_details: bool = True,
    ):
        normalized_symbols = tuple(
            item.strip()
            for item in str(allowed_symbols or "").split(",")
            if item.strip()
        )
        normalized_intent_ids = tuple(
            item.strip()
            for item in str(intent_ids or "").split(",")
            if item.strip()
        )
        payload = await run_in_threadpool(
            lambda: _build_controlled_apply_readiness(
                trade_date,
                account_id=account_id,
                max_apply_intents=max_apply_intents,
                intent_ids=normalized_intent_ids,
                allowed_symbols=normalized_symbols,
                require_live=require_live,
                require_trading_session=require_trading_session,
                max_equity_position_limit=max_equity_position_limit,
                max_single_amount=max_single_amount,
                min_reverse_repo_reserved_amount=min_reverse_repo_reserved_amount,
                max_stock_test_budget_amount=max_stock_test_budget_amount,
                max_apply_submissions_per_day=max_apply_submissions_per_day,
                blocked_time_windows=blocked_time_windows,
                include_details=include_details,
            ),
        )
        return {"ok": payload["status"] != "blocked", **payload}

    @router.get("/deployment/service-recovery-readiness")
    async def service_recovery_readiness(
        trade_date: str | None = None,
        account_id: str | None = None,
        max_workspace_age_seconds: int = 1800,
        max_signal_age_seconds: int = 1800,
        require_execution_bridge: bool = True,
        include_details: bool = True,
    ):
        payload = await run_in_threadpool(
            lambda: _build_service_recovery_readiness(
                trade_date,
                account_id=account_id,
                max_workspace_age_seconds=max_workspace_age_seconds,
                max_signal_age_seconds=max_signal_age_seconds,
                require_execution_bridge=require_execution_bridge,
                include_details=include_details,
            ),
        )
        return {"ok": payload["status"] != "blocked", **payload}

    @router.get("/deployment/windows-execution-gateway-onboarding-bundle")
    async def windows_execution_gateway_onboarding_bundle(request: Request, account_id: str | None = None):
        payload = _build_windows_execution_gateway_onboarding_bundle(account_id=account_id, request=request)
        return {"ok": True, **payload}

    @router.get("/account-state")
    async def account_state(account_id: str | None = None, refresh: bool = False):
        if not account_state_service:
            return {"ok": False, "error": "account state service not initialized"}
        resolved_account_id = _resolve_account_id(account_id)
        if refresh:
            payload = await run_in_threadpool(
                lambda: account_state_service.snapshot(
                    resolved_account_id,
                    persist=True,
                    include_trades=False,
                )
            )
            payload["cache_mode"] = "refreshed"
            return {"ok": payload.get("status") in {"ok", "queued_for_gateway"}, **payload}

        payload = await run_in_threadpool(account_state_service.latest_for, resolved_account_id)
        if payload:
            age_seconds = None
            captured_at = str(payload.get("captured_at") or "").strip()
            if captured_at:
                try:
                    captured_dt = datetime.fromisoformat(captured_at)
                    now = datetime.now(tz=captured_dt.tzinfo) if captured_dt.tzinfo else datetime.now()
                    age_seconds = max((now - captured_dt).total_seconds(), 0.0)
                except ValueError:
                    age_seconds = None
            response = dict(payload)
            response["cache_mode"] = "cached"
            response["fresh"] = bool(age_seconds is not None and age_seconds <= 600)
            response["age_seconds"] = age_seconds
            response.setdefault(
                "summary_lines",
                [f"账户状态缓存已命中 account={resolved_account_id}。"],
            )
            return {"ok": response.get("status") in {"ok", "queued_for_gateway"}, **response}

        return {
            "ok": False,
            "account_id": resolved_account_id,
            "status": "cache_unavailable",
            "cache_mode": "miss",
            "refresh_required": True,
            "summary_lines": [
                "账户状态缓存尚未建立。",
                "如需拉取最新账户状态，请调用 /system/account-state?refresh=true。",
            ],
        }

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
        return _handle_natural_language_adjustment(payload)

    @router.get("/feishu/rights")
    async def get_feishu_rights(trade_date: str | None = None):
        return {"ok": True, **_build_feishu_rights_payload(trade_date)}

    @router.get("/agents/capability-map")
    async def get_agent_capability_map(trade_date: str | None = None):
        return {"ok": True, **_build_agent_capability_map(trade_date)}

    @router.get("/agents/runtime-work-packets")
    async def get_agent_runtime_work_packets(
        trade_date: str | None = None,
        agent_id: str | None = None,
        overdue_after_seconds: int = 180,
        include_prompt_body: bool = False,
        recommended_only: bool = True,
    ):
        return _build_agent_runtime_work_payload(
            trade_date,
            agent_id=agent_id,
            overdue_after_seconds=overdue_after_seconds,
            include_prompt_body=include_prompt_body,
            recommended_only=recommended_only,
        )

    @router.get("/robot/console-layout")
    async def get_robot_console_layout(trade_date: str | None = None):
        return {"ok": True, **_build_robot_console_layout(trade_date)}

    @router.get("/workflow/mainline")
    async def get_mainline_workflow(trade_date: str | None = None):
        return {"ok": True, **_build_mainline_workflow_payload(trade_date)}

    @router.get("/agents/autonomy-spec")
    async def get_agent_autonomy_spec(trade_date: str | None = None):
        return {"ok": True, **_build_agent_autonomy_spec_payload(trade_date)}

    @router.get("/agents/supervision-board")
    async def get_agent_supervision_board(trade_date: str | None = None, overdue_after_seconds: int = 180):
        return {
            "ok": True,
            **_build_agent_supervision_payload(
                trade_date,
                overdue_after_seconds=overdue_after_seconds,
            ),
        }

    @router.get("/feishu/briefing")
    async def get_feishu_briefing(trade_date: str | None = None):
        return {"ok": True, **_build_feishu_briefing_payload(trade_date)}

    @router.get("/feishu/rights-briefing")
    async def get_feishu_rights_briefing(trade_date: str | None = None):
        return {"ok": True, **_build_feishu_briefing_payload(trade_date)}

    @router.post("/feishu/briefing/notify")
    async def notify_feishu_briefing(payload: FeishuBriefingNotifyInput):
        briefing = _build_feishu_briefing_payload(payload.trade_date)
        title = payload.title or "飞书知情简报"
        if not message_dispatcher:
            return {
                "ok": True,
                **briefing,
                "notification": {"dispatched": False, "reason": "dispatcher_unavailable"},
            }
        content = feishu_briefing_template(title, briefing.get("summary_lines", []), briefing.get("data_refs", []))
        dispatched = message_dispatcher.dispatch_report(title, content) if not payload.force else message_dispatcher.dispatch(
            "report",
            title,
            content,
            level="info",
            force=True,
        )
        return {
            "ok": True,
            **briefing,
            "notification": {"dispatched": dispatched, "reason": "sent" if dispatched else "dispatch_failed"},
        }

    @router.post("/feishu/ask")
    async def feishu_ask(payload: FeishuAskInput):
        answer = _answer_feishu_question(payload.question, trade_date=payload.trade_date)
        notification = {"dispatched": False, "reason": "disabled"}
        if payload.notify and message_dispatcher:
            content = feishu_answer_template(
                payload.question,
                answer.get("answer_lines", []),
                answer.get("data_refs", []),
                trade_advice=answer.get("trade_advice"),
            )
            dispatched = message_dispatcher.dispatch(
                "report",
                "飞书问答回复",
                content,
                level="info",
                force=payload.force,
            )
            notification = {"dispatched": dispatched, "reason": "sent" if dispatched else "dispatch_failed"}
        return {
            "ok": True,
            "question": payload.question,
            **answer,
            "notification": notification,
        }

    @router.post("/agents/supervision/check")
    async def check_agent_supervision(payload: AgentSupervisionCheckInput):
        supervision = _build_agent_supervision_payload(
            payload.trade_date,
            overdue_after_seconds=payload.overdue_after_seconds,
        )
        notification = {"dispatched": False, "reason": "disabled"}
        task_plan = dict(supervision.get("task_dispatch_plan") or {})
        recommended_tasks = [
            dict(item) for item in list(task_plan.get("recommended_tasks") or [])
        ]
        if payload.notify:
            if not supervision.get("attention_items") and not recommended_tasks:
                notification = {"dispatched": False, "reason": "no_attention"}
            elif not supervision.get("notify_items") and not payload.force and not recommended_tasks:
                notification = {"dispatched": False, "reason": "all_acknowledged"}
            elif not message_dispatcher:
                notification = {"dispatched": False, "reason": "dispatcher_unavailable"}
            else:
                notify_items = supervision.get("notify_items", []) if not payload.force else supervision.get("attention_items", [])
                if recommended_tasks:
                    notify_map = {str(item.get("agent_id") or ""): dict(item) for item in list(notify_items or [])}
                    recommended_order: list[str] = []
                    for task in recommended_tasks:
                        agent_id = str(task.get("agent_id") or "").strip()
                        if agent_id and agent_id in notify_map:
                            notify_map[agent_id].update(task)
                            recommended_order.append(agent_id)
                        elif agent_id:
                            notify_map[agent_id] = dict(task)
                            recommended_order.append(agent_id)
                    remaining_agent_ids = [
                        agent_id
                        for agent_id in notify_map.keys()
                        if agent_id not in recommended_order
                    ]
                    notify_items = [
                        notify_map[agent_id]
                        for agent_id in [*recommended_order, *remaining_agent_ids]
                    ]
                level = str(supervision.get("notification_level") or "info")
                dispatch_title = str(supervision.get("notification_title") or "Agent 自动催办")
                content = agent_supervision_template(
                    dispatch_title,
                    list(supervision.get("summary_lines", [])) + list(task_plan.get("summary_lines", [])),
                    notify_items,
                )
                dispatched = message_dispatcher.dispatch_monitor_changes(
                    dispatch_title,
                    content,
                    level=level,
                    force=payload.force or level in {"warning", "critical"},
                )
                notification = {"dispatched": dispatched, "reason": "sent" if dispatched else "dispatch_failed", "level": level}
                if dispatched:
                    record_supervision_notification(
                        meeting_state_store,
                        supervision.get("trade_date"),
                        signature=str(supervision.get("attention_signature") or ""),
                        level=level,
                        item_count=len(notify_items),
                    )
                    for item in notify_items:
                        agent_id = str(item.get("agent_id") or "").strip()
                        dispatch_key = str(item.get("dispatch_key") or "").strip()
                        if agent_id and dispatch_key:
                            record_agent_task_dispatch(
                                meeting_state_store,
                                supervision.get("trade_date"),
                                agent_id=agent_id,
                                dispatch_key=dispatch_key,
                                task_payload=item,
                            )
        return {
            "ok": True,
            **supervision,
            "notification": notification,
        }

    @router.post("/agents/supervision/ack")
    async def ack_agent_supervision(payload: AgentSupervisionAckInput):
        return _apply_agent_supervision_ack(
            trade_date=payload.trade_date,
            agent_ids=payload.agent_ids,
            actor=payload.actor,
            note=payload.note,
        )

    @router.post("/feishu/supervision/ack")
    async def feishu_supervision_ack(payload: FeishuSupervisionAckInput):
        parsed = _parse_feishu_supervision_ack(
            payload.text,
            fallback_agent_ids=payload.agent_ids,
            fallback_note=payload.note,
        )
        if not parsed.get("matched"):
            return {
                "ok": False,
                "source": payload.source,
                "trade_date": _resolve_reference_trade_date(payload.trade_date),
                "matched": False,
                "unmatched_reason": parsed.get("unmatched_reason"),
                "reply_lines": [
                    "当前文本未识别为监督回写指令。",
                    "可直接传 agent_ids，或发送如“研究已收到催办”“风控已处理”这类文本。",
                ],
            }
        ack_result = _apply_agent_supervision_ack(
            trade_date=payload.trade_date,
            agent_ids=parsed.get("agent_ids") or [],
            actor=payload.actor,
            note=str(parsed.get("note") or payload.note or payload.text or "").strip(),
        )
        return {
            **ack_result,
            "source": payload.source,
            "matched": True,
            "parsed_agent_ids": parsed.get("agent_ids") or [],
            "reply_lines": [
                f"已回写监督确认 {ack_result['acked_count']} 项。",
                (
                    f"已确认: {'、'.join(item.get('agent_id') or '' for item in ack_result.get('acked_items', []))}"
                    if ack_result.get("acked_items")
                    else "本次没有命中可确认项。"
                ),
            ],
        }

    @router.post("/feishu/events")
    async def feishu_events(payload: dict[str, Any], request: Request):
        verification_token = str(settings.notify.feishu_verification_token or "").strip()
        incoming_token = _extract_feishu_verification_token(payload)
        if verification_token and incoming_token and incoming_token != verification_token:
            return {
                "ok": False,
                "processed": False,
                "reason": "invalid_verification_token",
            }

        if str(payload.get("type") or "").strip() == "url_verification":
            return {
                "challenge": payload.get("challenge", ""),
            }

        header = payload.get("header") or {}
        event_type = str(header.get("event_type") or payload.get("event_type") or "").strip()
        if event_type == "url.preview.get":
            return _build_feishu_link_preview_response(payload)

        text = _extract_feishu_message_text(payload)
        actor = _extract_feishu_actor(payload)
        chat_id = _extract_feishu_chat_id(payload)

        if event_type and event_type not in {"im.message.receive_v1", "message"}:
            return {
                "ok": True,
                "processed": False,
                "reason": "event_type_ignored",
                "event_type": event_type,
            }

        parsed = _parse_feishu_supervision_ack(text)
        if not parsed.get("matched"):
            control_plane_link_reply = _build_control_plane_link_reply(text)
            if control_plane_link_reply.get("matched"):
                return {
                    "ok": True,
                    "processed": True,
                    "reason": "control_plane_link_explained",
                    "event_type": event_type or "message",
                    "chat_id": chat_id,
                    "reply_to_chat_id": chat_id,
                    "text": text,
                    **control_plane_link_reply,
                }
            if _is_feishu_message_addressed(payload, text=text):
                question = _normalize_feishu_question_text(payload, text)
                if _is_feishu_adjustment_request(question):
                    adjustment_reply = _build_feishu_adjustment_reply(question)
                    if adjustment_reply.get("matched"):
                        return {
                            "ok": True,
                            "processed": True,
                            "reason": "natural_language_adjustment_applied",
                            "event_type": event_type or "message",
                            "chat_id": chat_id,
                            "reply_to_chat_id": chat_id,
                            "text": text,
                            "question": question,
                            **adjustment_reply,
                        }
                question_reply = _build_feishu_question_reply(
                    question,
                    control_plane_base_url=_resolve_control_plane_base_url(request),
                )
                return {
                    "ok": True,
                    "processed": True,
                    "reason": "natural_language_question_answered",
                    "event_type": event_type or "message",
                    "chat_id": chat_id,
                    "reply_to_chat_id": chat_id,
                    "text": text,
                    **question_reply,
                }
            return {
                "ok": True,
                "processed": False,
                "reason": parsed.get("unmatched_reason") or "message_ignored",
                "event_type": event_type or "message",
                "chat_id": chat_id,
                "text": text,
            }

        ack_result = _apply_agent_supervision_ack(
            trade_date=None,
            agent_ids=parsed.get("agent_ids") or [],
            actor=actor,
            note=str(parsed.get("note") or text).strip(),
        )
        return {
            **ack_result,
            "ok": True,
            "processed": True,
            "source": "feishu_event",
            "event_type": event_type or "message",
            "chat_id": chat_id,
            "text": text,
            "parsed_agent_ids": parsed.get("agent_ids") or [],
        }

    @router.get("/feishu/events/config")
    async def feishu_events_config(request: Request):
        return {
            "ok": True,
            **_build_feishu_event_subscription_config(request),
        }

    @router.get("/feishu/longconn/status")
    async def feishu_longconn_status():
        return {
            "ok": True,
            **_build_feishu_longconn_status(),
        }

    @router.get("/nightly-sandbox/latest")
    async def get_latest_nightly_sandbox(trade_date: str | None = None):
        return {
            "ok": True,
            **_build_nightly_sandbox_payload(trade_date),
        }

    @router.post("/feishu/adjustments/natural-language")
    async def feishu_adjust_from_natural_language(payload: NaturalLanguageAdjustmentInput):
        return _handle_natural_language_adjustment(payload)

    @router.post("/precompute/dossiers")
    async def precompute_dossiers(payload: DossierPrecomputeInput):
        if not dossier_precompute_service:
            return {"ok": False, "error": "dossier precompute service not initialized"}
        try:
            pack = dossier_precompute_service.precompute(
                trade_date=payload.trade_date,
                as_of_time=payload.as_of_time,
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
        raw_polling_status = monitor_state_service.get_polling_status() if monitor_state_service else {}
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
        polling_status = decorate_polling_status_for_display(raw_polling_status, cycle=cycle_payload)

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
            state = str(item.get("display_state") or ("due" if item.get("due_now") else "cooldown"))
            reason_suffix = ""
            if item.get("suppressed_due_reason"):
                reason_suffix = f" reason={item.get('suppressed_due_reason')}"
            summary_lines.append(
                f"{layer}_poll={state} interval={item.get('interval_seconds')} last={item.get('last_polled_at')}{reason_suffix}"
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
            advance = _advance_discussion_after_writeback([updated], source="single_case_opinion")
            return {"ok": True, "case": updated.model_dump(), "discussion_advance": advance}
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
            advance = _advance_discussion_after_writeback(updated, source="manual_batch_opinions")
            return {
                "ok": True,
                "items": [item.model_dump() for item in updated],
                "count": len(updated),
                "discussion_advance": advance,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/discussions/opportunity-tickets")
    async def record_opportunity_tickets(payload: OpportunityTicketBatchInput):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        try:
            trade_date = _resolve_trade_date(payload.trade_date)
            normalized_items: list[dict[str, Any]] = []
            for item in payload.items:
                symbol = str(item.symbol or "").strip()
                market_logic = str(item.market_logic or "").strip()
                why_now = str(item.why_now or "").strip()
                if not symbol:
                    raise ValueError("opportunity ticket 缺少 symbol")
                if not market_logic:
                    raise ValueError(f"{symbol} 缺少 market_logic")
                if not why_now:
                    raise ValueError(f"{symbol} 缺少 why_now")
                core_evidence = [str(value) for value in list(item.core_evidence or []) if str(value).strip()]
                risk_points = [str(value) for value in list(item.risk_points or []) if str(value).strip()]
                if not core_evidence:
                    raise ValueError(f"{symbol} 缺少 core_evidence")
                if not risk_points:
                    raise ValueError(f"{symbol} 缺少 risk_points")
                normalized_items.append(
                    {
                        "symbol": symbol,
                        "name": str(item.name or "").strip(),
                        "source": str(item.source or "agent_proposed").strip() or "agent_proposed",
                        "source_role": str(item.source_role or "").strip(),
                        "market_logic": market_logic,
                        "core_evidence": core_evidence,
                        "risk_points": risk_points,
                        "why_now": why_now,
                        "trigger_type": str(item.trigger_type or "").strip(),
                        "trigger_time": str(item.trigger_time or "").strip(),
                        "recommended_action": str(item.recommended_action or "").strip(),
                        "evidence_refs": [str(value) for value in list(item.evidence_refs or []) if str(value).strip()],
                        "submitted_by": str(item.submitted_by or item.source_role or "").strip(),
                    }
                )
            updated = candidate_case_service.upsert_candidate_tickets(trade_date, normalized_items)
            if payload.auto_rebuild:
                updated = [candidate_case_service.rebuild_case(item.case_id) for item in updated]
            cycle_payload = None
            if discussion_cycle_service and updated and payload.auto_bootstrap_cycle:
                cycle = (
                    discussion_cycle_service.refresh_cycle(trade_date)
                    if discussion_cycle_service.get_cycle(trade_date) is not None
                    else discussion_cycle_service.bootstrap_cycle(trade_date)
                )
                cycle_payload = cycle.model_dump()
            if audit_store:
                audit_store.append(
                    category="discussion",
                    message="写入 opportunity tickets",
                    payload={
                        "trade_date": trade_date,
                        "count": len(updated),
                        "symbols": [item.symbol for item in updated],
                        "sources": [item.runtime_snapshot.source for item in updated],
                    },
                )
            return {
                "ok": True,
                "trade_date": trade_date,
                "count": len(updated),
                "items": [item.model_dump() for item in updated],
                "ingress_summary": {
                    "entered_discussion_count": len(updated),
                    "case_ids": [item.case_id for item in updated],
                    "source_counts": {
                        source: sum(1 for item in updated if str(item.runtime_snapshot.source or "") == source)
                        for source in sorted({str(item.runtime_snapshot.source or "") for item in updated if str(item.runtime_snapshot.source or "").strip()})
                    },
                    "non_default_entry": True,
                    "cycle_bootstrapped": bool(cycle_payload),
                },
                "cycle": cycle_payload,
            }
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
            if response.get("ok"):
                written_items = list(response.get("items") or [])
                case_ids = [str(item.get("case_id") or "").strip() for item in written_items if str(item.get("case_id") or "").strip()]
                updated_cases = [
                    candidate_case_service.get_case(case_id)
                    for case_id in dict.fromkeys(case_ids)
                ] if candidate_case_service else []
                response["discussion_advance"] = _advance_discussion_after_writeback(
                    [case for case in updated_cases if case],
                    source="openclaw_ingress",
                )
            return response
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/discussions/opinions/compose-writeback")
    async def record_compose_strategy_opinions(payload: ComposeOpinionWritebackInput):
        if not discussion_cycle_service:
            return {"ok": False, "error": "discussion cycle service not initialized"}
        try:
            trade_date = _resolve_trade_date(payload.trade_date)
            compose_payload = dict(payload.payload or {}) if isinstance(payload.payload, dict) else {}
            if payload.trace_id:
                compose_payload = _compose_evaluation_by_trace_id(str(payload.trace_id).strip())
            elif not compose_payload:
                compose_payload = _latest_compose_evaluation_for_trade_date(trade_date)
            if not compose_payload:
                return {
                    "ok": False,
                    "error": "compose evaluation payload not found",
                    "trade_date": trade_date,
                    "trace_id": payload.trace_id,
                }
            response = discussion_cycle_service.write_compose_strategy_opinions(
                compose_payload,
                trade_date=trade_date,
                expected_round=payload.expected_round,
                expected_agent_id=payload.expected_agent_id,
                case_id_map=(payload.case_id_map or None),
                auto_rebuild=payload.auto_rebuild,
            )
            if response.get("ok") and audit_store:
                audit_store.append(
                    category="discussion",
                    message="写入 compose strategy opinions",
                    payload={
                        "input": "compose_writeback",
                        "trade_date": trade_date,
                        "trace_id": response.get("trace_id") or payload.trace_id,
                        "expected_round": response.get("round"),
                        "expected_agent_id": payload.expected_agent_id,
                        "count": response.get("written_count", 0),
                        "case_count": len(
                            {item.get("case_id") for item in response.get("items", []) if item.get("case_id")}
                        ),
                    },
                )
            if response.get("ok"):
                written_items = list(response.get("items") or [])
                case_ids = [str(item.get("case_id") or "").strip() for item in written_items if str(item.get("case_id") or "").strip()]
                updated_cases = [
                    candidate_case_service.get_case(case_id)
                    for case_id in dict.fromkeys(case_ids)
                ] if candidate_case_service else []
                response["discussion_advance"] = _advance_discussion_after_writeback(
                    [case for case in updated_cases if case],
                    source="compose_writeback",
                )
            return response
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/discussions/opinions/research-writeback")
    async def record_research_auto_opinions(payload: AgentAutoOpinionWritebackInput):
        try:
            trade_date = _resolve_trade_date(payload.trade_date)
            response = _build_research_auto_writeback(
                trade_date,
                expected_round=payload.expected_round,
                expected_agent_id=payload.expected_agent_id or "ashare-research",
                auto_rebuild=payload.auto_rebuild,
            )
            if response.get("ok"):
                written_items = list(response.get("items") or [])
                case_ids = [
                    str(item.get("case_id") or "").strip()
                    for item in written_items
                    if str(item.get("case_id") or "").strip()
                ]
                updated_cases = [
                    candidate_case_service.get_case(case_id)
                    for case_id in dict.fromkeys(case_ids)
                ] if candidate_case_service else []
                response["discussion_advance"] = _advance_discussion_after_writeback(
                    [case for case in updated_cases if case],
                    source="research_writeback",
                )
            return response
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/discussions/opinions/risk-writeback")
    async def record_risk_auto_opinions(payload: AgentAutoOpinionWritebackInput):
        try:
            trade_date = _resolve_trade_date(payload.trade_date)
            response = _build_risk_auto_writeback(
                trade_date,
                expected_round=payload.expected_round,
                expected_agent_id=payload.expected_agent_id or "ashare-risk",
                auto_rebuild=payload.auto_rebuild,
            )
            if response.get("ok"):
                written_items = list(response.get("items") or [])
                case_ids = [
                    str(item.get("case_id") or "").strip()
                    for item in written_items
                    if str(item.get("case_id") or "").strip()
                ]
                updated_cases = [
                    candidate_case_service.get_case(case_id)
                    for case_id in dict.fromkeys(case_ids)
                ] if candidate_case_service else []
                response["discussion_advance"] = _advance_discussion_after_writeback(
                    [case for case in updated_cases if case],
                    source="risk_writeback",
                )
            return response
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/discussions/opinions/audit-writeback")
    async def record_audit_auto_opinions(payload: AgentAutoOpinionWritebackInput):
        try:
            trade_date = _resolve_trade_date(payload.trade_date)
            response = _build_audit_auto_writeback(
                trade_date,
                expected_round=payload.expected_round,
                expected_agent_id=payload.expected_agent_id or "ashare-audit",
                auto_rebuild=payload.auto_rebuild,
            )
            if response.get("ok"):
                written_items = list(response.get("items") or [])
                case_ids = [
                    str(item.get("case_id") or "").strip()
                    for item in written_items
                    if str(item.get("case_id") or "").strip()
                ]
                updated_cases = [
                    candidate_case_service.get_case(case_id)
                    for case_id in dict.fromkeys(case_ids)
                ] if candidate_case_service else []
                response["discussion_advance"] = _advance_discussion_after_writeback(
                    [case for case in updated_cases if case],
                    source="audit_writeback",
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
        
        # S1.2: 提取并定制化质询指导
        all_inquiry_targets = list(discussion_summary.get("all_inquiry_targets") or [])
        targeted_inquiries = [it for it in all_inquiry_targets if it.get("to_agent") == agent_id]
        round_2_guidance = list(discussion_summary.get("round_2_guidance") or [])
        if targeted_inquiries:
            round_2_guidance.insert(0, f"【你被质询的焦点】: 你需要优先回应以下 {len(targeted_inquiries)} 项质询")
            for it in targeted_inquiries[:5]:
                round_2_guidance.insert(1, f"- {it['from_agent']} 对 {it['case_id']} 提出质询: {it['question']}")

        learned_asset_review_guidance = _build_learned_asset_review_guidance(trade_date, requested_agent_id=agent_id)
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
        if round_2_guidance:
            summary_lines.append("二轮要求：" + "；".join(round_2_guidance[:3]))
        if learned_asset_review_guidance.get("available"):
            summary_lines.extend(list(learned_asset_review_guidance.get("summary_lines") or [])[:2])
        return {
            "ok": True,
            "trade_date": trade_date,
            **build_agent_packets_envelope(
                {
                    "status_filter": status,
                    "case_count": len(items),
                    "round_coverage": discussion_summary.get("round_coverage", {}),
                    "controversy_summary_lines": discussion_summary.get("controversy_summary_lines", []),
                    "round_2_guidance": round_2_guidance,
                    "all_inquiry_targets": all_inquiry_targets if not agent_id else targeted_inquiries,
                    "learned_asset_review_guidance": learned_asset_review_guidance,
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
        trade_date: str | None = None,
        selection_limit: int = 3,
        watchlist_limit: int = 5,
        rejected_limit: int = 5,
    ):
        resolved_date = _resolve_trade_date(trade_date)
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        payload = _build_client_brief(
            resolved_date,
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
    async def get_discussion_execution_precheck(trade_date: str | None = None, account_id: str | None = None):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        try:
            resolved_trade_date = _resolve_trade_date(trade_date)
        except ValueError:
            resolved_trade_date = datetime.now().date().isoformat()
        payload = _build_execution_precheck(resolved_trade_date, account_id=account_id)
        if monitor_state_service:
            monitor_state_service.mark_poll_if_due("execution", trigger="execution_precheck_read", force=True)
        return {"ok": True, **payload}

    @router.get("/discussions/execution-intents")
    async def get_discussion_execution_intents(trade_date: str | None = None, account_id: str | None = None):
        if not candidate_case_service:
            return {"ok": False, "error": "candidate case service not initialized"}
        try:
            resolved_trade_date = _resolve_trade_date(trade_date)
        except ValueError:
            resolved_trade_date = datetime.now().date().isoformat()
        payload = _build_execution_intents(resolved_trade_date, account_id=account_id)
        if monitor_state_service:
            monitor_state_service.mark_poll_if_due("execution", trigger="execution_intents_read", force=True)
        return {"ok": True, **payload}

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

    @router.get("/execution/dispatch/latest")
    async def get_latest_execution_dispatch_alias(trade_date: str | None = None):
        return await get_latest_execution_dispatch(trade_date=trade_date)

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
        payload = await run_in_threadpool(
            lambda: pending_order_inspection_service.inspect(
                resolved_account_id,
                warn_after_seconds=warn_after_seconds,
                persist=True,
            )
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
        payload = await run_in_threadpool(
            lambda: pending_order_remediation_service.remediate(
                resolved_account_id,
                auto_action=auto_action,
                cancel_after_seconds=cancel_after_seconds,
                persist=True,
            )
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
        payload = await run_in_threadpool(
            lambda: startup_recovery_service.recover(resolved_account_id, persist=True)
        )
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
        payload = await run_in_threadpool(
            lambda: execution_reconciliation_service.reconcile(resolved_account_id, persist=True)
        )
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
            execution_plane=str(getattr(settings, "execution_plane", "local_xtquant") or "local_xtquant"),
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
        intraday_rank_result = meeting_state_store.get("latest_intraday_rank_result") or {}
        if not payload:
            if intraday_rank_result:
                attached = _attach_control_plane_gateway_summary(
                    {
                        "available": True,
                        "status": "intraday_only",
                        "trade_date": intraday_rank_result.get("trade_date"),
                        "summary_lines": list(intraday_rank_result.get("summary_lines") or []),
                        "intraday_rank_result": intraday_rank_result,
                    },
                    trade_date=str(intraday_rank_result.get("trade_date") or "") or None,
                )
                return {"ok": True, **attached}
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
        attached["intraday_rank_result"] = intraday_rank_result
        if intraday_rank_result and not attached.get("summary_lines"):
            attached["summary_lines"] = list(intraday_rank_result.get("summary_lines") or [])
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

    def _build_trade_review_section(
        *,
        trade_date: str | None = None,
        score_date: str | None = None,
    ) -> dict[str, Any]:
        if not trade_attribution_service:
            return {
                "available": False,
                "trade_count": 0,
                "review_summary": {},
                "review_tag_summary": [],
                "parameter_hints": [],
                "action_items": [],
                "summary_lines": ["当前尚无可用于盘后 review 的线上 trade-review 汇总。"],
            }
        report = trade_attribution_service.build_report(
            trade_date=trade_date,
            score_date=score_date,
        )
        if not report.available:
            return {
                "available": False,
                "trade_count": 0,
                "review_summary": {},
                "review_tag_summary": [],
                "parameter_hints": [],
                "action_items": [],
                "summary_lines": ["当前尚无可用于盘后 review 的线上 trade-review 汇总。"],
            }
        payload = report.model_dump()
        filters = dict(payload.get("filters") or {})
        review_summary = dict(payload.get("review_summary") or {})
        by_playbook = list(payload.get("by_playbook") or [])
        by_regime = list(payload.get("by_regime") or [])
        parameter_hints = list(payload.get("parameter_hints") or [])
        review_tag_summary = list(payload.get("review_tag_summary") or [])
        action_items = []
        for hint in parameter_hints[:3]:
            action_items.append(
                {
                    "source": "trade_review",
                    "priority": "medium",
                    "title": f"交易复盘建议关注参数 {hint.get('param_key')}",
                    "reason": str(hint.get("reason") or "线上 trade-review 已产出参数提示。"),
                    "parameter_hint": hint,
                    "operation_targets": [
                        {
                            "label": "查看 trade-review",
                            "method": "GET",
                            "path": "/system/learning/trade-review",
                            "query": {
                                **filters,
                            },
                        }
                    ],
                }
            )
        if by_playbook:
            weakest_playbook = min(
                by_playbook,
                key=lambda item: float(item.get("avg_next_day_close_pct", 0.0) or 0.0),
            )
            if float(weakest_playbook.get("avg_next_day_close_pct", 0.0) or 0.0) < 0:
                action_items.append(
                    {
                        "source": "trade_review",
                        "priority": "high",
                        "title": f"线上 trade-review 显示战法 {weakest_playbook.get('key')} 偏弱",
                        "reason": (
                            f"样本 {weakest_playbook.get('trade_count', 0)} 笔，"
                            f"平均收益 {float(weakest_playbook.get('avg_next_day_close_pct', 0.0) or 0.0):.2%}。"
                        ),
                        "playbook": weakest_playbook.get("key"),
                        "operation_targets": [
                            {
                                "label": "查看 trade-review",
                                "method": "GET",
                                "path": "/system/learning/trade-review",
                                "query": {
                                    **filters,
                                },
                            }
                        ],
                    }
                )
        summary_lines = list(payload.get("summary_lines") or [])
        if not summary_lines:
            summary_lines = [f"线上 trade-review 样本 {payload.get('trade_count', 0)} 笔。"]
        return {
            "available": True,
            "trade_date": payload.get("trade_date"),
            "score_date": payload.get("score_date"),
            "filters": filters,
            "trade_count": payload.get("trade_count", 0),
            "avg_next_day_close_pct": payload.get("avg_next_day_close_pct", 0.0),
            "win_rate": payload.get("win_rate", 0.0),
            "review_summary": review_summary,
            "review_tag_summary": review_tag_summary,
            "parameter_hints": parameter_hints,
            "by_playbook": by_playbook,
            "by_regime": by_regime,
            "by_reason": list(payload.get("by_reason") or []),
            "by_exit_reason": list(payload.get("by_exit_reason") or []),
            "action_items": action_items,
            "summary_lines": summary_lines,
        }

    def _build_governance_effect_review_section(
        *,
        trade_date: str | None = None,
        score_date: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if not parameter_service or not trade_attribution_service:
            return {
                "available": False,
                "count": 0,
                "rollback_recommended_count": 0,
                "manual_review_required_count": 0,
                "items": [],
                "action_items": [],
                "summary_lines": ["当前尚无可用于盘后 review 的参数效果追踪。"],
            }

        all_events = [item.model_dump() for item in parameter_service.list_proposals()]
        rollback_events_by_parent: dict[str, list[dict[str, Any]]] = {}
        for event in all_events:
            rollback_of_event_id = str(event.get("rollback_of_event_id") or "")
            if rollback_of_event_id:
                rollback_events_by_parent.setdefault(rollback_of_event_id, []).append(event)
        effective_events = [item.model_dump() for item in parameter_service.list_proposals(status="effective")]
        selected_events = effective_events[-max(min(int(limit or 20), 50), 1):]
        items: list[dict[str, Any]] = []
        action_items: list[dict[str, Any]] = []
        rollback_recommended_count = 0
        manual_review_required_count = 0

        for event in selected_events:
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
            related_rollbacks = rollback_events_by_parent.get(str(event.get("event_id") or ""), [])
            rollback_event = related_rollbacks[0] if related_rollbacks else None
            rollback_report = None
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
            effect_tracking = _build_effect_tracking_summary(
                event,
                report,
                rollback_event=rollback_event,
                rollback_report=rollback_report,
            )
            items.append(
                {
                    "event": event,
                    "filters": {
                        **source_filters,
                        **({"trade_date": effective_trade_date} if effective_trade_date else {}),
                        **({"score_date": effective_score_date} if effective_score_date else {}),
                    },
                    "latest_rollback_event": rollback_event,
                    "effect_tracking": effect_tracking,
                }
            )
            if effect_tracking.get("rollback_recommended"):
                rollback_recommended_count += 1
                action_items.append(
                    {
                        "source": "governance_effects",
                        "priority": "high",
                        "title": f"参数 {event.get('param_key')} 提案效果偏弱",
                        "reason": (
                            f"样本 {effect_tracking.get('trade_count', 0)} 笔，平均收益 "
                            f"{float(effect_tracking.get('avg_next_day_close_pct', 0.0) or 0.0):.2%}，建议复核回滚。"
                        ),
                        "event_id": event.get("event_id"),
                        "param_key": event.get("param_key"),
                        "operation_targets": [
                            {
                                "label": "查看参数效果",
                                "method": "GET",
                                "path": "/system/learning/parameter-hints/effects",
                                "query": {"event_ids": str(event.get("event_id") or "")},
                            }
                        ],
                    }
                )
            post_rollback = dict(effect_tracking.get("post_rollback_tracking") or {})
            recommended_action = dict(post_rollback.get("recommended_action") or {})
            if post_rollback.get("manual_review_required"):
                manual_review_required_count += 1
                action_items.append(
                    {
                        "source": "governance_effects",
                        "priority": recommended_action.get("priority") or "high",
                        "title": f"回滚后续需要人工复核：{event.get('param_key')}",
                        "reason": recommended_action.get("reason") or "post_rollback_tracking 建议人工复核。",
                        "event_id": event.get("event_id"),
                        "param_key": event.get("param_key"),
                        "operation_targets": list(post_rollback.get("operation_targets") or []),
                    }
                )

        items.sort(
            key=lambda item: (
                not bool((item.get("effect_tracking") or {}).get("rollback_recommended")),
                not bool(((item.get("effect_tracking") or {}).get("post_rollback_tracking") or {}).get("manual_review_required")),
                str((item.get("event") or {}).get("effective_at") or (item.get("event") or {}).get("applied_at") or ""),
            )
        )
        summary_lines = [f"参数效果追踪 {len(items)} 条，建议回滚 {rollback_recommended_count} 条。"]
        if manual_review_required_count > 0:
            summary_lines.append(f"其中 rollback 后需人工复核 {manual_review_required_count} 条。")
        if not items:
            summary_lines = ["当前尚无可用于盘后 review 的参数效果追踪。"]
        return {
            "available": bool(items),
            "count": len(items),
            "rollback_recommended_count": rollback_recommended_count,
            "manual_review_required_count": manual_review_required_count,
            "items": items[:5],
            "action_items": action_items[:5],
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

    def _build_review_priority_board(action_items: list[dict[str, Any]]) -> dict[str, Any]:
        if not action_items:
            return {
                "available": False,
                "item_count": 0,
                "high_priority_count": 0,
                "by_source": [],
                "by_priority": [],
                "items": [],
                "summary_lines": ["当前 review board 尚无统一高优先级待办。"],
            }

        priority_rank = {"high": 0, "medium": 1, "low": 2}
        normalized_items = []
        for item in action_items:
            payload = dict(item or {})
            priority = str(payload.get("priority") or "medium").strip().lower()
            if priority not in priority_rank:
                priority = "medium"
            payload["priority"] = priority
            normalized_items.append(payload)
        normalized_items.sort(
            key=lambda item: (
                priority_rank.get(str(item.get("priority") or "medium"), 1),
                str(item.get("source") or ""),
                str(item.get("title") or ""),
            )
        )

        deduped_items = []
        seen_keys: set[str] = set()
        for item in normalized_items:
            dedupe_key = "|".join(
                [
                    str(item.get("source") or ""),
                    str(item.get("title") or ""),
                    str(item.get("reason") or ""),
                ]
            )
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            deduped_items.append(item)

        by_source_map: dict[str, int] = {}
        by_priority_map: dict[str, int] = {}
        for item in deduped_items:
            source = str(item.get("source") or "unknown")
            priority = str(item.get("priority") or "medium")
            by_source_map[source] = by_source_map.get(source, 0) + 1
            by_priority_map[priority] = by_priority_map.get(priority, 0) + 1

        by_source = [
            {"key": key, "count": count}
            for key, count in sorted(by_source_map.items(), key=lambda pair: (-pair[1], pair[0]))
        ]
        by_priority = [
            {"key": key, "count": count}
            for key, count in sorted(by_priority_map.items(), key=lambda pair: (priority_rank.get(pair[0], 9), pair[0]))
        ]
        summary_lines = [
            f"统一待办 {len(deduped_items)} 条，高优先级 {by_priority_map.get('high', 0)} 条。"
        ]
        if by_source:
            summary_lines.append(
                "主要来源: " + "；".join(f"{item['key']}({item['count']})" for item in by_source[:5])
            )
        return {
            "available": True,
            "item_count": len(deduped_items),
            "high_priority_count": by_priority_map.get("high", 0),
            "by_source": by_source,
            "by_priority": by_priority,
            "items": deduped_items[:8],
            "summary_lines": summary_lines,
        }

    def _load_latest_playbook_override_snapshot() -> dict[str, Any]:
        if meeting_state_store:
            snapshot = meeting_state_store.get("latest_playbook_override_snapshot") or {}
            if snapshot:
                return _sanitize_json_compatible(dict(snapshot))
        storage_path = settings.storage_root / "learning" / "playbook_overrides.json"
        if not storage_path.exists():
            return {}
        try:
            payload = json.loads(storage_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return _sanitize_json_compatible(dict(payload) if isinstance(payload, dict) else {})

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
        playbook_override_snapshot = _load_latest_playbook_override_snapshot()
        governance = _build_governance_review_summary(
            trade_date=trade_date,
            score_date=score_date,
            due_within_days=due_within_days,
            limit=inspection_limit,
        )
        governance_effects = _build_governance_effect_review_section(
            trade_date=trade_date,
            score_date=score_date,
            limit=inspection_limit,
        )
        trade_review = _build_trade_review_section(
            trade_date=trade_date,
            score_date=score_date,
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
            or trade_review.get("trade_date")
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
            *(list(governance_effects.get("action_items") or [])[:5]),
            *(list(trade_review.get("action_items") or [])[:5]),
            *(list(tail_market.get("action_items") or [])[:5]),
            *(list(discussion.get("action_items") or [])[:3]),
            *(list(offline_backtest.get("action_items") or [])[:3]),
            *(list(exit_monitor.get("action_items") or [])[:3]),
            *(list(execution_bridge_health.get("action_items") or [])[:3]),
            *(list(offline_backtest_metrics.get("action_items") or [])[:3]),
        ]
        priority_board = _build_review_priority_board(action_items)
        summary_lines = [
            "盘后 review board 已汇总参数治理、参数效果追踪、线上 trade-review、tail-market、discussion、offline backtest、exit monitor、execution bridge health、metrics、主控执行与 auto governance 十一类重点事项。",
            (
                f"治理高优先级 {governance.get('high_priority_action_item_count', 0)} 项；"
                f"参数效果追踪 {governance_effects.get('count', 0)} 条；"
                f"线上 trade-review 样本 {trade_review.get('trade_count', 0)} 笔；"
                f"tail-market 命中 {tail_market.get('count', 0)} 项；"
                f"discussion 状态 {discussion.get('status') or 'unknown'}；"
                f"离线回测样本 {offline_backtest.get('trade_count', 0)} 笔；"
                f"exit monitor 信号 {exit_monitor.get('signal_count', 0)} 条；"
                f"执行桥状态 {execution_bridge_health.get('overall_status') or 'unknown'}；"
                f"metrics 样本 {int((offline_backtest_metrics.get('overview') or {}).get('total_trades', 0) or 0)} 笔；"
                f"主控 pending intent {control_plane_gateway.get('pending_intent_count', 0)} 条；"
                f"override {len(list(playbook_override_snapshot.get('overrides') or []))} 项。"
            ),
        ]
        if governance.get("summary_lines"):
            summary_lines.append("治理: " + str((governance.get("summary_lines") or [""])[0]))
        if governance_effects.get("summary_lines"):
            summary_lines.append("治理效果: " + str((governance_effects.get("summary_lines") or [""])[0]))
        if trade_review.get("summary_lines"):
            summary_lines.append("线上复盘: " + str((trade_review.get("summary_lines") or [""])[0]))
        if playbook_override_snapshot.get("summary_lines"):
            summary_lines.append("治理 override: " + str((playbook_override_snapshot.get("summary_lines") or [""])[0]))
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
        if priority_board.get("summary_lines"):
            summary_lines.append("统一待办: " + str((priority_board.get("summary_lines") or [""])[0]))
        payload = {
            "ok": True,
            "available": bool(
                governance.get("available")
                or governance_effects.get("available")
                or trade_review.get("available")
                or tail_market.get("available")
                or discussion.get("available")
                or offline_backtest.get("available")
                or exit_monitor.get("available")
                or execution_bridge_health.get("available")
                or offline_backtest_metrics.get("available")
                or control_plane_gateway.get("available")
                or bool(playbook_override_snapshot)
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
                "governance_effect_count": governance_effects.get("count", 0),
                "governance_effect_rollback_recommended_count": governance_effects.get("rollback_recommended_count", 0),
                "governance_effect_manual_review_required_count": governance_effects.get("manual_review_required_count", 0),
                "trade_review_trade_count": trade_review.get("trade_count", 0),
                "tail_market_count": tail_market.get("count", 0),
                "discussion_blocked_count": discussion.get("blocked_count", 0),
                "discussion_selected_count": discussion.get("selected_count", 0),
                "offline_backtest_trade_count": offline_backtest.get("trade_count", 0),
                "exit_monitor_signal_count": exit_monitor.get("signal_count", 0),
                "execution_bridge_attention_count": execution_bridge_health.get("attention_count", 0),
                "offline_backtest_metrics_trade_count": int((offline_backtest_metrics.get("overview") or {}).get("total_trades", 0) or 0),
                "playbook_override_count": len(list(playbook_override_snapshot.get("overrides") or [])),
                "pending_intent_count": int(control_plane_gateway.get("pending_intent_count", 0) or 0),
                "queued_for_gateway_count": int(control_plane_gateway.get("queued_for_gateway_count", 0) or 0),
                "discussion_dispatch_queued_for_gateway_count": int(
                    ((control_plane_gateway.get("discussion_dispatch") or {}).get("queued_for_gateway_count", 0) or 0)
                ),
                "tail_market_queued_for_gateway_count": int(
                    ((control_plane_gateway.get("tail_market") or {}).get("queued_for_gateway_count", 0) or 0)
                ),
                "priority_board_item_count": priority_board.get("item_count", 0),
                "priority_board_high_priority_count": priority_board.get("high_priority_count", 0),
                "total_action_item_count": len(action_items),
            },
            "sections": {
                "priority_board": priority_board,
                "governance": governance,
                "governance_effects": governance_effects,
                "trade_review": trade_review,
                "tail_market": tail_market,
                "discussion": discussion,
                "offline_backtest": offline_backtest,
                "exit_monitor": exit_monitor,
                "execution_bridge_health": execution_bridge_health,
                "offline_backtest_metrics": offline_backtest_metrics,
                "auto_governance": playbook_override_snapshot,
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
            if monitor_state_service and round_number == 1:
                monitor_state_service.mark_poll_if_due(
                    "focus",
                    trigger=f"discussion_round_{round_number}_start",
                    force=True,
                )
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
    @router.get("/audit")
    async def get_audit_alias(limit: int = 20):
        return await list_audits(limit=limit)

    @router.get("/status/services")
    async def get_services_status():
        """获取各组件服务状态 (systemctl)"""
        services = {
            "control-plane": "ashare-system-v2.service",
            "scheduler": "ashare-system-v2-scheduler.service",
            "feishu": "ashare-feishu-longconn.service",
            "go-platform": "ashare-go-data-platform.service",
            "openclaw": "openclaw-gateway.service",
        }

        results = {}
        for name, unit in services.items():
            try:
                cmd = ["systemctl", "is-active", unit]
                if name == "feishu":
                    cmd = ["systemctl", "--user", "is-active", unit]
                process = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await process.communicate()
                results[name] = stdout.decode().strip()
            except Exception:
                results[name] = "unknown"
        return {"ok": True, "services": results}

    @router.post("/operations/service/restart")
    async def restart_service(service: str):
        """重启指定服务"""
        services = {
            "control-plane": "ashare-system-v2.service",
            "scheduler": "ashare-system-v2-scheduler.service",
            "feishu": "ashare-feishu-longconn.service",
            "go-platform": "ashare-go-data-platform.service",
            "openclaw": "openclaw-gateway.service",
        }
        unit = services.get(service)
        if not unit:
            raise HTTPException(status_code=400, detail=f"Unknown service: {service}")

        try:
            cmd = ["sudo", "systemctl", "restart", unit]
            if service == "feishu":
                cmd = ["systemctl", "--user", "restart", unit]

            # 使用 Popen 防止阻塞或因 Control Plane 重启自身导致请求中断处理
            subprocess.Popen(cmd)

            if audit_store:
                audit_store.append(category="dashboard", message=f"仪表盘触发服务重启: {service}")
            return {"ok": True, "message": f"Restart command sent to {service}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

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
