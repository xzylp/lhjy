"""参数治理巡检复用逻辑。"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _parse_effect_date(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def build_observation_window_status(
    event: dict[str, Any],
    trade_count: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    window = dict(event.get("observation_window") or {})
    now_date = (now or datetime.now()).date()
    start_raw = (
        window.get("start_date")
        or event.get("effective_from")
        or str(event.get("created_at") or "")[:10]
        or now_date.isoformat()
    )
    end_raw = window.get("end_date")
    start_dt = _parse_effect_date(start_raw)
    end_dt = _parse_effect_date(end_raw)
    expected_trade_count = int(window.get("expected_trade_count", 1) or 1)
    if start_dt and now_date < start_dt.date():
        status = "scheduled"
    elif trade_count >= expected_trade_count:
        status = "ready_for_review"
    elif end_dt and now_date > end_dt.date():
        status = "overdue"
    else:
        status = "observing"
    days_remaining = None if not end_dt else (end_dt.date() - now_date).days
    return {
        **window,
        "start_date": start_dt.date().isoformat() if start_dt else str(start_raw),
        "end_date": end_dt.date().isoformat() if end_dt else end_raw,
        "expected_trade_count": expected_trade_count,
        "trade_count": trade_count,
        "status": status,
        "days_remaining": days_remaining,
    }


def is_pending_high_risk_rollback(event: dict[str, Any], approval_ticket: dict[str, Any]) -> bool:
    if event.get("event_type") != "param_rollback":
        return False
    if str((approval_ticket or {}).get("risk_level") or "").lower() != "high":
        return False
    if not bool((approval_ticket or {}).get("required")):
        return False
    ticket_state = str((approval_ticket or {}).get("state") or "pending").lower()
    if ticket_state not in {"pending", "approved"}:
        return False
    return str(event.get("status") or "").lower() in {"evaluating", "approved"}


def _build_recommended_action(
    *,
    event: dict[str, Any],
    report: dict[str, Any],
    observation_window: dict[str, Any],
    approval_ticket: dict[str, Any],
) -> dict[str, Any]:
    event_type = str(event.get("event_type") or "param_change")
    observation_status = str(observation_window.get("status") or "observing")
    avg_return = float(report.get("avg_next_day_close_pct", 0.0) or 0.0) if report.get("available") else 0.0
    win_rate = float(report.get("win_rate", 0.0) or 0.0) if report.get("available") else 0.0
    if is_pending_high_risk_rollback(event, approval_ticket):
        if observation_status in {"ready_for_review", "overdue"}:
            return {
                "action": "manual_release_or_reject",
                "priority": ("high" if observation_status == "overdue" else "medium"),
                "reason": "高风险 rollback 已达到人工复核窗口，应决定放行还是驳回。",
            }
        return {
            "action": "continue_observe",
            "priority": "medium",
            "reason": "高风险 rollback 仍在观察窗口内，暂不建议提前放行。",
        }
    if event_type == "param_rollback":
        if observation_status in {"ready_for_review", "overdue"}:
            recovery_detected = avg_return > 0 or win_rate >= 0.4
            return {
                "action": ("confirm_rollback_effect" if recovery_detected else "review_rollback_effect"),
                "priority": ("high" if observation_status == "overdue" else "medium"),
                "reason": (
                    "rollback 后样本已有修复迹象，可以进入确认。"
                    if recovery_detected
                    else "rollback 观察窗口已到，但样本未显示明显修复，建议人工复核。"
                ),
            }
        return {
            "action": "continue_observe",
            "priority": "low",
            "reason": "rollback 观察窗口尚未完成，继续跟踪同类样本表现。",
        }
    if observation_status in {"ready_for_review", "overdue"}:
        degraded = avg_return < 0 or win_rate < 0.4
        return {
            "action": ("consider_rollback_preview" if degraded else "review_and_keep"),
            "priority": ("high" if observation_status == "overdue" and degraded else "medium"),
            "reason": (
                "提案生效后同类样本表现仍弱，建议先看 rollback preview。"
                if degraded
                else "提案观察窗口已到，当前样本未见明显恶化，可复核后继续保留。"
            ),
        }
    return {
        "action": "continue_observe",
        "priority": "low",
        "reason": "观察窗口尚未到期，继续累计样本。",
    }


def _build_operation_targets(
    *,
    event: dict[str, Any],
    effective_trade_date: str | None,
    effective_score_date: str | None,
    source_filters: dict[str, Any],
    recommended_action: dict[str, Any],
) -> list[dict[str, Any]]:
    event_id = str(event.get("event_id") or "")
    action = str(recommended_action.get("action") or "")
    shared_payload = {
        **({"trade_date": effective_trade_date} if effective_trade_date else {}),
        **({"score_date": effective_score_date} if effective_score_date else {}),
        "event_ids": [event_id],
    }
    if action == "manual_release_or_reject":
        return [
            {
                "label": "人工放行 rollback",
                "method": "POST",
                "path": "/system/learning/parameter-hints/rollback-approval",
                "payload": {
                    **shared_payload,
                    "action": "release",
                    "approver": "human-audit",
                    "comment": "inspection 建议人工放行",
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
                    "comment": "inspection 建议人工驳回",
                },
            },
        ]
    if action == "consider_rollback_preview":
        return [
            {
                "label": "生成 rollback 预览",
                "method": "POST",
                "path": "/system/learning/parameter-hints/rollback-preview",
                "payload": shared_payload,
            }
        ]
    if action in {"review_and_keep", "confirm_rollback_effect", "review_rollback_effect"}:
        return [
            {
                "label": "查看效果跟踪",
                "method": "GET",
                "path": "/system/learning/parameter-hints/effects",
                "query": {
                    **({"trade_date": effective_trade_date} if effective_trade_date else {}),
                    **({"score_date": effective_score_date} if effective_score_date else {}),
                    "event_ids": event_id,
                    "status": "effective",
                },
            }
        ]
    if action == "continue_observe" and source_filters:
        return [
            {
                "label": "继续巡检同类样本",
                "method": "GET",
                "path": "/system/learning/parameter-hints/inspection",
                "query": {
                    **({"trade_date": effective_trade_date} if effective_trade_date else {}),
                    **({"score_date": effective_score_date} if effective_score_date else {}),
                    "statuses": "evaluating,approved,effective",
                    "due_within_days": 1,
                },
            }
        ]
    return []


def _is_high_priority_action_item(item: dict[str, Any]) -> bool:
    recommended_action = dict(item.get("recommended_action") or {})
    action = str(recommended_action.get("action") or "")
    priority = str(recommended_action.get("priority") or "low")
    if action == "continue_observe":
        return False
    return priority in {"medium", "high"}


def collect_parameter_hint_inspection(
    *,
    parameter_service: Any,
    trade_attribution_service: Any,
    trade_date: str | None,
    score_date: str | None,
    statuses: str,
    due_within_days: int,
    limit: int,
) -> dict[str, Any]:
    selected_statuses = {
        item.strip().lower()
        for item in str(statuses or "").split(",")
        if item.strip()
    }
    all_events = [item.model_dump() for item in parameter_service.list_proposals()]
    filtered_events = [
        item
        for item in all_events
        if (not selected_statuses or str(item.get("status") or "").lower() in selected_statuses)
    ]
    pending_high_risk_rollbacks: list[dict[str, Any]] = []
    observation_window_alerts: list[dict[str, Any]] = []
    recommended_actions: list[dict[str, Any]] = []
    action_items: list[dict[str, Any]] = []
    high_priority_action_items: list[dict[str, Any]] = []
    inspected_count = 0
    near_due_count = 0
    overdue_count = 0
    for event in filtered_events[: max(limit, 1)]:
        source_filters = dict(event.get("source_filters") or {})
        effective_trade_date = trade_date or source_filters.get("trade_date")
        effective_score_date = score_date or source_filters.get("score_date")
        report = trade_attribution_service.build_report(
            trade_date=effective_trade_date,
            score_date=effective_score_date,
            review_tag=source_filters.get("review_tag"),
            exit_context_key=source_filters.get("exit_context_key"),
            exit_context_value=source_filters.get("exit_context_value"),
            symbol=source_filters.get("symbol"),
            reason=source_filters.get("reason"),
        ).model_dump()
        trade_count = int(report.get("trade_count", 0) or 0) if report.get("available") else 0
        observation_window = build_observation_window_status(event, trade_count)
        approval_ticket = dict(event.get("approval_ticket") or {})
        recommended_action = _build_recommended_action(
            event=event,
            report=report,
            observation_window=observation_window,
            approval_ticket=approval_ticket,
        )
        operation_targets = _build_operation_targets(
            event=event,
            effective_trade_date=effective_trade_date,
            effective_score_date=effective_score_date,
            source_filters=source_filters,
            recommended_action=recommended_action,
        )
        inspected_count += 1
        if is_pending_high_risk_rollback(event, approval_ticket):
            pending_high_risk_rollbacks.append(
                {
                    "event_id": event.get("event_id"),
                    "event_type": event.get("event_type"),
                    "status": event.get("status"),
                    "param_key": event.get("param_key"),
                    "new_value": event.get("new_value"),
                    "rollback_of_event_id": event.get("rollback_of_event_id"),
                    "approval_ticket": approval_ticket,
                    "observation_window": observation_window,
                    "recommended_action": recommended_action,
                    "operation_targets": operation_targets,
                    "filters": {
                        **source_filters,
                        **({"trade_date": effective_trade_date} if effective_trade_date else {}),
                        **({"score_date": effective_score_date} if effective_score_date else {}),
                    },
                }
            )
        alert_level: str | None = None
        if observation_window.get("status") == "overdue":
            alert_level = "overdue"
            overdue_count += 1
        else:
            days_remaining = observation_window.get("days_remaining")
            if (
                isinstance(days_remaining, int)
                and days_remaining >= 0
                and days_remaining <= due_within_days
            ):
                alert_level = "near_due"
                near_due_count += 1
        if alert_level:
            observation_window_alerts.append(
                {
                    "event_id": event.get("event_id"),
                    "event_type": event.get("event_type"),
                    "status": event.get("status"),
                    "param_key": event.get("param_key"),
                    "alert_level": alert_level,
                    "observation_window": observation_window,
                    "approval_ticket": approval_ticket,
                    "recommended_action": recommended_action,
                    "operation_targets": operation_targets,
                }
            )
        recommended_actions.append(
            {
                "event_id": event.get("event_id"),
                "event_type": event.get("event_type"),
                "status": event.get("status"),
                "param_key": event.get("param_key"),
                "observation_window": observation_window,
                "approval_ticket": approval_ticket,
                "recommended_action": recommended_action,
                "operation_targets": operation_targets,
            }
        )
        if str(recommended_action.get("priority") or "low") in {"medium", "high"}:
            action_item = {
                "event_id": event.get("event_id"),
                "event_type": event.get("event_type"),
                "status": event.get("status"),
                "param_key": event.get("param_key"),
                "filters": {
                    **source_filters,
                    **({"trade_date": effective_trade_date} if effective_trade_date else {}),
                    **({"score_date": effective_score_date} if effective_score_date else {}),
                },
                "observation_window": observation_window,
                "approval_ticket": approval_ticket,
                "recommended_action": recommended_action,
                "operation_targets": operation_targets,
            }
            action_items.append(action_item)
            if _is_high_priority_action_item(action_item):
                high_priority_action_items.append(action_item)
    pending_high_risk_rollbacks = pending_high_risk_rollbacks[: max(limit, 1)]
    observation_window_alerts = observation_window_alerts[: max(limit, 1)]
    recommended_actions = recommended_actions[: max(limit, 1)]
    action_items = action_items[: max(limit, 1)]
    high_priority_action_items = high_priority_action_items[: max(limit, 1)]
    recommended_action_counts: dict[str, int] = {}
    for item in recommended_actions:
        action = str((item.get("recommended_action") or {}).get("action") or "unknown")
        recommended_action_counts[action] = recommended_action_counts.get(action, 0) + 1
    return {
        "ok": True,
        "inspected_count": inspected_count,
        "statuses": sorted(selected_statuses),
        "due_within_days": due_within_days,
        "pending_high_risk_rollback_count": len(pending_high_risk_rollbacks),
        "observation_near_due_count": near_due_count,
        "observation_overdue_count": overdue_count,
        "pending_high_risk_rollbacks": pending_high_risk_rollbacks,
        "observation_window_alerts": observation_window_alerts,
        "recommended_actions": recommended_actions,
        "recommended_action_counts": recommended_action_counts,
        "action_items": action_items,
        "action_item_count": len(action_items),
        "high_priority_action_items": high_priority_action_items,
        "high_priority_action_item_count": len(high_priority_action_items),
        "summary_lines": [
            (
                f"巡检 {inspected_count} 条参数事件，高风险待审回滚 "
                f"{len(pending_high_risk_rollbacks)} 条。"
            ),
            f"观察窗口预警：near_due={near_due_count} overdue={overdue_count}。",
            f"待处理动作项：{len(action_items)} 条。",
            f"高优先级待办：{len(high_priority_action_items)} 条。",
            "建议动作: "
            + (
                " / ".join(f"{action}={count}" for action, count in sorted(recommended_action_counts.items()))
                if recommended_action_counts
                else "none"
            ),
        ],
    }
