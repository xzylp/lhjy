"""Agent 催办任务编排：按时间阶段、状态与市场变化生成自然语言任务。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, time
from typing import Any

from .infra.audit_store import StateStore

TASK_DISPATCH_CONTROL_KEY_PREFIX = "agent_task_dispatch_control:"


def task_dispatch_control_key(trade_date: str | None) -> str:
    return f"{TASK_DISPATCH_CONTROL_KEY_PREFIX}{trade_date or 'unknown'}"


def _parse_iso_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _align_datetimes(left: datetime | None, right: datetime | None) -> tuple[datetime | None, datetime | None]:
    if left is None or right is None:
        return left, right
    if left.tzinfo is None and right.tzinfo is not None:
        left = left.replace(tzinfo=right.tzinfo)
    elif left.tzinfo is not None and right.tzinfo is None:
        right = right.replace(tzinfo=left.tzinfo)
    return left, right


def _seconds_since(value: str | None, *, now: datetime) -> int | None:
    parsed = _parse_iso_datetime(value)
    if parsed is None:
        return None
    current = now.astimezone(parsed.tzinfo) if parsed.tzinfo and now.tzinfo else now
    current, parsed = _align_datetimes(current, parsed)
    if current is None or parsed is None:
        return None
    return max(int((current - parsed).total_seconds()), 0)


def _load_dispatch_state(store: StateStore | None, trade_date: str | None) -> dict[str, Any]:
    if not store:
        return {}
    payload = store.get(task_dispatch_control_key(trade_date), {})
    return dict(payload) if isinstance(payload, dict) else {}


def _save_dispatch_state(store: StateStore | None, trade_date: str | None, payload: dict[str, Any]) -> None:
    if not store:
        return
    store.set(task_dispatch_control_key(trade_date), payload)


def _derive_completion_tags(task_payload: dict[str, Any]) -> list[str]:
    text_parts = [
        str(task_payload.get("agent_id") or ""),
        str(task_payload.get("task_reason") or ""),
        str(task_payload.get("task_prompt") or ""),
        " ".join(str(item) for item in list(task_payload.get("expected_outputs") or [])),
    ]
    text = " ".join(part for part in text_parts if part).lower()
    tags: list[str] = []

    def _add(tag: str) -> None:
        if tag not in tags:
            tags.append(tag)

    if "研究" in text or "机会票" in text or "热点" in text or "催化" in text:
        _add("research_output")
    if "策略" in text or "调参" in text or "runtime" in text or "打法" in text:
        _add("strategy_output")
    if "夜间沙盘" in text or "沙盘" in text:
        _add("nightly_sandbox")
    if "次日预案" in text or "预案" in text:
        _add("next_day_plan")
    if "审计" in text or "纪要" in text or "证据链" in text or "复盘" in text:
        _add("audit_record")
    if "风控" in text or "风险" in text or "阻断" in text or "放行" in text:
        _add("risk_review")
    if "执行" in text or "回执" in text or "报单" in text or "撤单" in text or "对账" in text:
        _add("execution_feedback")
    if "运行底座" in text or "样本" in text or "刷新" in text:
        _add("runtime_refresh")
    if "cycle" in text or "收敛" in text or "续轮" in text or "finalize" in text or "总协调" in text:
        _add("coordination")
    if "round" in text or "观点" in text or "质询" in text:
        _add("discussion_progress")
    if "nightly_sandbox" in tags and "strategy_output" in tags:
        tags.remove("strategy_output")
    if "next_day_plan" in tags and "audit_record" in tags:
        tags.remove("audit_record")
    return tags


def _activity_matches_completion_tags(item: dict[str, Any], completion_tags: list[str]) -> bool:
    if not completion_tags:
        return True
    activity_label = str(item.get("activity_label") or "")
    reasons = " ".join(str(reason) for reason in list(item.get("reasons") or []))
    signal_sources = " ".join(str(signal.get("source") or "") for signal in list(item.get("activity_signals") or []))
    text = " ".join([activity_label, reasons, signal_sources]).lower()
    covered = int(item.get("covered_case_count", 0) or 0)
    expected = int(item.get("expected_case_count", 0) or 0)
    status = str(item.get("status") or "").strip()
    negative_hints = ("未形成", "尚未", "仍未", "未产出", "缺少", "没有")

    for tag in completion_tags:
        if tag == "research_output" and any(keyword in text for keyword in ["研究", "事件", "催化", "热点", "dossier", "research_summary"]):
            return True
        if tag == "strategy_output" and any(keyword in text for keyword in ["策略", "调参", "提案", "param_proposals", "proposal", "override", "compose", "replan", "重编排"]):
            return True
        if tag == "nightly_sandbox" and "夜间沙盘" in text and not any(hint in text for hint in negative_hints):
            return True
        if tag == "next_day_plan" and any(keyword in text for keyword in ["次日预案", "预案", "纪要"]) and not any(hint in text for hint in negative_hints):
            return True
        if tag == "audit_record" and any(keyword in text for keyword in ["审计", "纪要", "review_board", "replay", "复盘"]):
            return True
        if tag == "risk_review" and any(keyword in text for keyword in ["风控", "执行预检", "reconciliation", "tail_market_scan", "风险"]):
            return True
        if tag == "execution_feedback" and any(keyword in text for keyword in ["执行", "dispatch", "对账", "回执", "报单", "撤单"]):
            return True
        if tag == "runtime_refresh" and any(keyword in text for keyword in ["运行事实产出", "runtime", "monitor_context", "runtime_report"]):
            return True
        if tag == "coordination" and status == "working" and any(keyword in text for keyword in ["讨论态", "round", "cycle", "总协调"]):
            return True
        if tag == "discussion_progress" and expected > 0 and covered >= expected and status not in {"needs_work", "overdue"}:
            return True
    return False


def record_agent_task_dispatch(
    store: StateStore | None,
    trade_date: str | None,
    *,
    agent_id: str,
    dispatch_key: str,
    task_payload: dict[str, Any],
    sent_at: str | None = None,
) -> dict[str, Any]:
    state = _load_dispatch_state(store, trade_date)
    agents = dict(state.get("agents") or {})
    history = list(state.get("history") or [])
    event = {
        "agent_id": agent_id,
        "dispatch_key": dispatch_key,
        "phase": task_payload.get("phase_code"),
        "reason": task_payload.get("task_reason"),
        "sent_at": sent_at or datetime.now().isoformat(),
        "task_prompt": task_payload.get("task_prompt"),
        "task_mode": task_payload.get("task_mode"),
        "expected_outputs": list(task_payload.get("expected_outputs") or []),
        "completion_tags": _derive_completion_tags(task_payload),
    }
    agents[agent_id] = event
    history.append(event)
    state["agents"] = agents
    state["history"] = history[-200:]
    _save_dispatch_state(store, trade_date, state)
    return event


def list_recent_agent_task_dispatches(store: StateStore | None, trade_date: str | None) -> list[dict[str, Any]]:
    state = _load_dispatch_state(store, trade_date)
    return [dict(item) for item in list(state.get("history") or [])]


def record_agent_task_completion(
    store: StateStore | None,
    trade_date: str | None,
    *,
    agent_id: str,
    completion_type: str,
    completion_payload: dict[str, Any] | None = None,
    completed_at: str | None = None,
) -> dict[str, Any]:
    state = _load_dispatch_state(store, trade_date)
    agents = dict(state.get("agents") or {})
    current = dict(agents.get(agent_id) or {})
    event = {
        **current,
        "agent_id": agent_id,
        "completion_type": completion_type,
        "completion_payload": dict(completion_payload or {}),
        "completed_at": completed_at or datetime.now().isoformat(),
    }
    if event.get("dispatched_at") is None and event.get("sent_at") is not None:
        event["dispatched_at"] = event.get("sent_at")
    agents[agent_id] = event
    history = list(state.get("history") or [])
    history.append(
        {
            "agent_id": agent_id,
            "dispatch_key": event.get("dispatch_key"),
            "phase": event.get("phase"),
            "completion_type": completion_type,
            "completed_at": event.get("completed_at"),
        }
    )
    state["agents"] = agents
    state["history"] = history[-200:]
    _save_dispatch_state(store, trade_date, state)
    return event


def sync_agent_task_completion_from_activity(
    store: StateStore | None,
    trade_date: str | None,
    *,
    items: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    if not store:
        return {}
    current = now or datetime.now()
    state = _load_dispatch_state(store, trade_date)
    agents = dict(state.get("agents") or {})
    updated: dict[str, dict[str, Any]] = {}
    changed = False
    history = list(state.get("history") or [])
    for item in items:
        agent_id = str(item.get("agent_id") or "").strip()
        if not agent_id:
            continue
        existing = dict(agents.get(agent_id) or {})
        sent_at = str(existing.get("sent_at") or existing.get("dispatched_at") or "").strip() or None
        last_active_at = str(item.get("last_active_at") or "").strip() or None
        if not sent_at or not last_active_at:
            continue
        sent_dt = _parse_iso_datetime(sent_at)
        active_dt = _parse_iso_datetime(last_active_at)
        sent_dt, active_dt = _align_datetimes(sent_dt, active_dt)
        if sent_dt is None or active_dt is None or active_dt < sent_dt:
            continue
        completed_at = str(existing.get("completed_at") or "").strip() or None
        completed_dt = _parse_iso_datetime(completed_at)
        completed_dt, active_dt = _align_datetimes(completed_dt, active_dt)
        if completed_dt is not None and completed_dt >= active_dt:
            updated[agent_id] = existing
            continue
        status = str(item.get("status") or "").strip()
        if status in {"needs_work", "overdue"}:
            continue
        completion_tags = [str(tag) for tag in list(existing.get("completion_tags") or []) if str(tag).strip()]
        if not _activity_matches_completion_tags(item, completion_tags):
            continue
        completion_event = {
            **existing,
            "agent_id": agent_id,
            "completion_type": "activity_observed",
            "completion_payload": {
                "activity_label": item.get("activity_label"),
                "last_active_at": last_active_at,
                "status": status,
                "completion_tags": completion_tags,
            },
            "completed_at": active_dt.isoformat(),
        }
        agents[agent_id] = completion_event
        updated[agent_id] = completion_event
        history.append(
            {
                "agent_id": agent_id,
                "dispatch_key": completion_event.get("dispatch_key"),
                "phase": completion_event.get("phase"),
                "completion_type": "activity_observed",
                "completed_at": completion_event.get("completed_at"),
            }
        )
        changed = True
    if changed:
        state["agents"] = agents
        state["history"] = history[-200:]
        _save_dispatch_state(store, trade_date, state)
    return updated


def resolve_market_phase(trade_date: str | None, *, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now()
    current_trade_date = current.date().isoformat()
    same_trade_date = bool(trade_date and trade_date == current_trade_date)
    current_time = current.time()

    if not same_trade_date:
        return {
            "code": "off_trade_date",
            "label": "非当日值班",
            "dispatch_interval_seconds": 3600,
            "active_agents": ("ashare", "ashare-research", "ashare-strategy", "ashare-audit"),
            "prompt_hint": "当前不是当日盘中，更适合做复盘、学习、沙盘和次日预案。",
        }
    if current_time < time(9, 15):
        return {
            "code": "pre_market",
            "label": "盘前预热",
            "dispatch_interval_seconds": 900,
            "active_agents": ("ashare", "ashare-research", "ashare-strategy", "ashare-risk"),
            "prompt_hint": "盘前先看消息、板块、竞价预期和空仓填仓机会，不要急着下结论。",
        }
    if current_time < time(9, 30):
        return {
            "code": "auction",
            "label": "集合竞价",
            "dispatch_interval_seconds": 180,
            "active_agents": ("ashare", "ashare-research", "ashare-strategy", "ashare-risk", "ashare-runtime"),
            "prompt_hint": "集合竞价讲究快与准，盯竞价强弱、封单质量、主题发酵和撤单异动。",
        }
    if current_time < time(11, 30):
        return {
            "code": "morning_session",
            "label": "上午盘中",
            "dispatch_interval_seconds": 420,
            "active_agents": ("ashare", "ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit", "ashare-executor"),
            "prompt_hint": "盘中发现机会优先，先事实后判断，策略可自主调参后再调用 runtime。",
        }
    if current_time < time(13, 0):
        return {
            "code": "midday",
            "label": "午间整理",
            "dispatch_interval_seconds": 1800,
            "active_agents": ("ashare", "ashare-research", "ashare-strategy", "ashare-audit"),
            "prompt_hint": "午间重点是收敛上午事实、更新假设、准备下午打法。",
        }
    if current_time < time(14, 30):
        return {
            "code": "afternoon_session",
            "label": "下午盘中",
            "dispatch_interval_seconds": 420,
            "active_agents": ("ashare", "ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit", "ashare-executor"),
            "prompt_hint": "下午盘更看持续性、分歧转一致和持仓去弱留强。",
        }
    if current_time < time(15, 0):
        return {
            "code": "tail_session",
            "label": "尾盘收口",
            "dispatch_interval_seconds": 180,
            "active_agents": ("ashare", "ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit", "ashare-executor"),
            "prompt_hint": "尾盘重点是换仓、尾盘潜伏、做 T 收口和次日预案。",
        }
    if current_time < time(18, 0):
        return {
            "code": "post_close",
            "label": "盘后复盘",
            "dispatch_interval_seconds": 1800,
            "active_agents": ("ashare", "ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"),
            "prompt_hint": "盘后先复盘今天事实、盈亏归因、误判原因，再形成学习结论。",
        }
    return {
        "code": "night_review",
        "label": "夜间学习",
        "dispatch_interval_seconds": 3600,
        "active_agents": ("ashare", "ashare-research", "ashare-strategy", "ashare-audit"),
        "prompt_hint": "夜间重在学习、回测、沙盘、提示词修正和新战法沉淀。",
    }


def _build_dispatch_key(agent_id: str, item: dict[str, Any], phase_code: str, context_markers: dict[str, Any]) -> str:
    payload = {
        "agent_id": agent_id,
        "status": item.get("status"),
        "phase_code": phase_code,
        "context_markers": context_markers,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _build_common_context(
    phase: dict[str, Any],
    *,
    cycle_state: str | None,
    round_number: int,
    market_event_recent: bool,
    execution_pending: bool,
    execution_status: str,
) -> str:
    parts = [f"当前阶段是{phase['label']}"]
    if cycle_state:
        parts.append(f"讨论态={cycle_state}")
    if round_number > 0:
        parts.append(f"轮次={round_number}")
    if market_event_recent:
        parts.append("刚出现新的市场变化或高优先级事件")
    if execution_pending:
        parts.append(f"执行侧当前有待跟进状态({execution_status or 'pending'})")
    return "，".join(parts) + "。"


def _build_quality_gap_hint(item: dict[str, Any]) -> str:
    expected = int(item.get("expected_case_count", 0) or 0)
    covered = int(item.get("covered_case_count", 0) or 0)
    if expected > 0 and covered < expected:
        return f"本轮主线当前只覆盖了 {covered}/{expected}。"
    return ""


def _build_quality_instruction(agent_id: str, item: dict[str, Any]) -> str:
    quality_state = str(item.get("quality_state") or "").strip()
    quality_reason = str(item.get("quality_reason") or "").strip()
    if not quality_state and not quality_reason:
        return ""

    gap_hint = _build_quality_gap_hint(item)
    if quality_state == "blocked":
        return (
            f"当前推进质量已阻塞主线。{quality_reason}{gap_hint}"
            " 请先把最缺的正式产物补出来，再继续衍生性分析。"
        )
    if quality_state == "low":
        if int(item.get("activity_signal_count", 0) or 0) > 0 or str(item.get("last_active_at") or "").strip():
            return (
                f"当前推进质量偏低。{quality_reason}{gap_hint}"
                " 不要只停留在内部动作、调参或思考过程，必须把可消费结论写回主线。"
            )
        return (
            f"当前推进质量偏低。{quality_reason}{gap_hint}"
            " 请直接补齐该阶段最缺的正式材料。"
        )
    if quality_state == "partial":
        return f"当前推进只完成了一部分。{quality_reason}{gap_hint} 请优先补齐缺口后再扩展。"
    if quality_state == "good":
        return f"当前推进质量正常。{quality_reason}"
    return quality_reason


def _build_task_reason_fragments(item: dict[str, Any]) -> list[str]:
    fragments: list[str] = []
    quality_state = str(item.get("quality_state") or "").strip()
    quality_reason = str(item.get("quality_reason") or "").strip()
    gap_hint = _build_quality_gap_hint(item)
    if quality_state:
        fragments.append(f"推进质量={quality_state}")
    if gap_hint:
        fragments.append(gap_hint.removesuffix("。"))
    elif quality_reason and quality_state in {"low", "partial", "blocked"}:
        fragments.append(quality_reason.removesuffix("。"))
    return fragments


def _task_priority_key(task: dict[str, Any]) -> tuple[int, int, int, int, int, str]:
    status = str(task.get("status") or "").strip()
    quality_state = str(task.get("quality_state") or "").strip()
    market_response_state = str(task.get("market_response_state") or "").strip()
    task_mode = str(task.get("task_mode") or "").strip()
    expected = int(task.get("expected_case_count", 0) or 0)
    covered = int(task.get("covered_case_count", 0) or 0)
    gap = max(expected - covered, 0)

    status_rank = {
        "overdue": 0,
        "needs_work": 1,
        "working": 2,
        "standby": 3,
    }.get(status, 4)
    quality_rank = {
        "blocked": 0,
        "low": 1,
        "partial": 2,
        "observe": 3,
        "good": 4,
    }.get(quality_state, 5)
    response_rank = {
        "lagging": 0,
        "needs_output": 1,
        "aligned": 2,
    }.get(market_response_state, 3)
    mode_rank = {
        "follow_up": 1,
    }.get(task_mode, 0)
    agent_id = str(task.get("agent_id") or "")
    return (status_rank, quality_rank, response_rank, -gap, -mode_rank, agent_id)


def _sort_group_by_task_priority(
    source_items: list[dict[str, Any]],
    task_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    ordered: list[tuple[tuple[int, int, int, int, str], dict[str, Any]]] = []
    for raw_item in source_items:
        item = dict(raw_item)
        agent_id = str(item.get("agent_id") or "")
        task = dict(task_map.get(agent_id) or {})
        priority_payload = {**item, **task}
        ordered.append((_task_priority_key(priority_payload), item))
    ordered.sort(key=lambda row: row[0])
    return [item for _, item in ordered]


def _build_position_aware_action_reason(
    agent_id: str,
    phase: dict[str, Any],
    position_context: dict[str, Any] | None,
) -> str:
    context = dict(position_context or {})
    position_limit = float(context.get("equity_position_limit", 0.0) or 0.0)
    position_ratio = float(context.get("current_total_ratio", 0.0) or 0.0)
    position_count = int(context.get("position_count", 0) or 0)
    available_budget = float(context.get("available_test_trade_value", 0.0) or 0.0)
    stock_budget = float(context.get("stock_test_budget_amount", 0.0) or 0.0)
    if position_limit <= 0:
        return ""

    has_room_to_add = available_budget >= 10000.0 and position_ratio + 0.03 < position_limit
    near_full_position = position_ratio >= max(position_limit - 0.03, position_limit * 0.85)
    phase_code = str(phase.get("code") or "")

    if agent_id == "ashare-strategy":
        if phase_code in {"morning_session", "afternoon_session", "tail_session"} and has_room_to_add:
            return (
                f"当前股票仓位约 {position_ratio:.1%}/{position_limit:.1%}，可用预算约 {available_budget:.0f}/{stock_budget:.0f}，"
                " 还有仓位空间，优先判断补仓、扩新机会还是重组 runtime。"
            )
        if phase_code in {"morning_session", "afternoon_session", "tail_session"} and near_full_position and position_count > 0:
            return (
                f"当前股票仓位约 {position_ratio:.1%}/{position_limit:.1%}，持仓数={position_count}，"
                " 已接近满仓，优先做替换仓位和去弱留强，不要再按补仓逻辑思考。"
            )
    if agent_id == "ashare-research":
        if phase_code in {"morning_session", "afternoon_session", "tail_session"} and has_room_to_add:
            return (
                f"当前仍有可用仓位，预算约 {available_budget:.0f}/{stock_budget:.0f}，"
                " 研究侧应优先补新机会和替代方向，别只围着已有票打转。"
            )
        if phase_code in {"morning_session", "afternoon_session", "tail_session"} and near_full_position and position_count > 0:
            return (
                f"当前仓位接近上限 {position_ratio:.1%}/{position_limit:.1%}，"
                " 研究侧应优先验证哪些持仓该被替换、哪些新方向值得顶替进来。"
            )
    if agent_id == "ashare-risk" and position_count > 0:
        return (
            f"当前持仓数={position_count}，股票仓位约 {position_ratio:.1%}/{position_limit:.1%}，"
            " 风控侧应优先盯持仓波动、回撤、换仓边界和隔夜风险。"
        )
    if agent_id == "ashare-executor" and position_count > 0:
        return (
            f"当前持仓数={position_count}，执行侧应优先看做T、撤改单、换仓预演和尾盘动作准备。"
        )
    if agent_id == "ashare" and has_room_to_add:
        return (
            f"当前股票仓位约 {position_ratio:.1%}/{position_limit:.1%}，仍有仓位空间，"
            " 总协调应优先推动继续找机会，不要让团队空转。"
        )
    if agent_id == "ashare" and near_full_position and position_count > 0:
        return (
            f"当前股票仓位约 {position_ratio:.1%}/{position_limit:.1%}，已接近满仓，"
            " 总协调应优先推动替换仓位、持仓复核和收口决策。"
        )
    return ""


def _build_event_aware_action_reason(
    agent_id: str,
    phase: dict[str, Any],
    *,
    market_event_recent: bool,
    latest_market_change_at: str | None,
    execution_pending: bool,
    execution_status: str,
    execution_summary: dict[str, Any] | None,
) -> str:
    reasons: list[str] = []
    phase_label = str(phase.get("label") or "").strip()
    execution_summary = dict(execution_summary or {})
    submitted_count = int(execution_summary.get("submitted_count", 0) or 0)
    preview_count = int(execution_summary.get("preview_count", 0) or 0)
    blocked_count = int(execution_summary.get("blocked_count", 0) or 0)
    latest_execution_reconciliation = dict(execution_summary.get("latest_execution_reconciliation") or {})
    real_trade_observed = (
        str(latest_execution_reconciliation.get("status") or "").strip() == "ok"
        and int(latest_execution_reconciliation.get("trade_count", 0) or 0) > 0
    )

    if market_event_recent and latest_market_change_at and agent_id in {"ashare", "ashare-research", "ashare-strategy", "ashare-risk", "ashare-runtime"}:
        role_hint = {
            "ashare": "总协调应立即重新分工，别按旧节奏推进。",
            "ashare-research": "研究侧应优先把新异动、新热点和消息催化落成正式判断。",
            "ashare-strategy": "策略侧应优先判断是否切换打法、改参数或重组 runtime。",
            "ashare-risk": "风控侧应优先确认异动后的放行/阻断和仓位边界。",
            "ashare-runtime": "runtime 应优先把异动后的底座变化和样本刷新结果回写给上游。",
        }.get(agent_id, "应优先响应新的市场变化。")
        reasons.append(f"{phase_label}刚出现新的市场变化({latest_market_change_at})，{role_hint}")

    if execution_pending and agent_id in {"ashare", "ashare-risk", "ashare-audit", "ashare-executor"}:
        role_hint = {
            "ashare": "总协调应把执行状态接回主线，决定继续推进还是收口。",
            "ashare-risk": "风控侧应优先复核执行前后风险，不要等回执堆积。",
            "ashare-audit": "审计侧应优先核对执行与讨论是否一致，并更新纪要。",
            "ashare-executor": "执行侧应优先补齐预演、回执或阻断结果，避免执行链悬空。",
        }.get(agent_id, "应优先处理执行链状态。")
        reasons.append(
            f"执行链当前处于 {execution_status or 'pending'}，submitted={submitted_count} preview={preview_count} blocked={blocked_count}，{role_hint}"
        )

    if real_trade_observed and agent_id in {"ashare", "ashare-strategy", "ashare-risk", "ashare-audit"}:
        role_hint = {
            "ashare": "总协调应优先推动学习归因和次日主线收口。",
            "ashare-strategy": "策略侧应优先做打法归因和参数调整。",
            "ashare-risk": "风控侧应优先做成交后仓位与风险复核。",
            "ashare-audit": "审计侧应优先收口真实成交复盘和纪要。",
        }.get(agent_id, "应优先处理真实成交后的收口。")
        reasons.append("已出现真实成交/对账结果，" + role_hint)

    return " ".join(reasons).strip()


def _extract_activity_text(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("activity_label") or ""),
        " ".join(str(reason) for reason in list(item.get("reasons") or [])),
        " ".join(
            str(signal.get("label") or signal.get("source") or "")
            for signal in list(item.get("activity_signals") or [])
            if isinstance(signal, dict)
        ),
    ]
    return " ".join(part for part in parts if part).lower()


def _detect_market_response_outputs(item: dict[str, Any]) -> list[str]:
    text = _extract_activity_text(item)
    outputs: list[str] = []

    def _add(label: str, keywords: tuple[str, ...]) -> None:
        if label not in outputs and any(keyword in text for keyword in keywords):
            outputs.append(label)

    _add("新提案", ("提案", "机会票", "opportunity", "新增方向", "新票", "候选"))
    _add("新compose", ("compose", "runtime/jobs/compose", "compose-from-brief", "策略草案", "编排草案", "重编排", "replan"))
    _add("持仓动作判断", ("做t", "换仓", "减仓", "加仓", "持仓", "替换", "仓位"))
    _add("热点催化判断", ("研究", "热点", "催化", "事件", "news", "dossier"))
    _add("风险边界", ("风控", "风险", "放行", "阻断", "precheck"))
    _add("执行反馈", ("执行", "回执", "报单", "撤单", "dispatch", "成交", "对账", "preview"))
    _add("纪要复盘", ("纪要", "复盘", "review board", "审计"))
    _add("盘后学习", ("夜间沙盘", "sandbox", "次日预案", "参数建议"))
    _add("底座刷新", ("runtime", "样本", "刷新", "事实底座", "monitor_context"))
    return outputs


def _resolve_response_lane(
    phase: dict[str, Any],
    *,
    position_context: dict[str, Any] | None,
    execution_pending: bool,
) -> str:
    phase_code = str(phase.get("code") or "")
    position_count = int((position_context or {}).get("position_count", 0) or 0)
    if phase_code in {"pre_market", "auction"}:
        return "盘前机会发现"
    if phase_code in {"morning_session", "afternoon_session"} and position_count > 0:
        return "持仓做T/换仓"
    if phase_code == "tail_session" and position_count > 0:
        return "尾盘持仓收口"
    if phase_code == "tail_session":
        return "尾盘机会收口"
    if phase_code in {"post_close", "night_review"}:
        return "盘后学习"
    if execution_pending:
        return "执行反馈收口"
    return "盘中机会响应"


def _expected_market_response_outputs(
    agent_id: str,
    phase: dict[str, Any],
    *,
    position_context: dict[str, Any] | None,
    execution_pending: bool,
    market_event_recent: bool,
) -> list[str]:
    phase_code = str(phase.get("code") or "")
    position_count = int((position_context or {}).get("position_count", 0) or 0)
    has_position = position_count > 0
    outputs: list[str] = []

    if agent_id == "ashare":
        outputs = ["分工调整", "主线动作判断"]
        if phase_code in {"post_close", "night_review"}:
            outputs.append("学习归因结论")
    elif agent_id == "ashare-research":
        if phase_code in {"post_close", "night_review"}:
            outputs = ["纪要复盘", "次日关注方向"]
        else:
            outputs = ["新提案", "热点催化判断"]
            if has_position:
                outputs.append("替代方向判断")
    elif agent_id == "ashare-strategy":
        if phase_code in {"post_close", "night_review"}:
            outputs = ["盘后学习", "次日策略预案"]
        elif has_position and phase_code in {"morning_session", "afternoon_session", "tail_session"}:
            outputs = ["持仓动作判断", "做T/换仓方案"]
            if market_event_recent:
                outputs.insert(0, "新compose")
        elif phase_code in {"pre_market", "auction"}:
            outputs = ["compose草案", "竞价应对方案"]
        else:
            outputs = ["新compose", "打法切换结论"]
    elif agent_id == "ashare-risk":
        outputs = ["风险边界", "放行/阻断结论"]
        if has_position or execution_pending or phase_code in {"post_close", "night_review"}:
            outputs.append("成交后风险复核")
    elif agent_id == "ashare-audit":
        outputs = ["纪要复盘", "证据复核"]
    elif agent_id == "ashare-executor":
        outputs = ["执行反馈"]
        if has_position:
            outputs.append("做T/换仓预演")
    elif agent_id == "ashare-runtime":
        outputs = ["底座刷新", "样本变化摘要"]
    return list(dict.fromkeys(outputs))


def _append_market_response_instruction(
    prompt: str,
    *,
    response_lane: str,
    response_targets: list[str],
    response_gap: list[str],
    market_event_recent: bool,
) -> str:
    if not response_targets and not response_gap:
        return prompt
    if "市场响应产物" in prompt and "主导链路" in prompt:
        return prompt
    parts = [f"当前主导链路={response_lane}。"]
    if response_targets:
        parts.append(
            "不要只汇报是否调用了哪些工具，必须形成市场响应产物："
            + " / ".join(response_targets[:3])
            + "。"
        )
    if market_event_recent and response_gap:
        parts.append("最近市场已变化，本窗口仍缺：" + " / ".join(response_gap[:3]) + "。")
    return prompt.rstrip() + " " + " ".join(parts)


def _task_prompt_for_agent(
    agent_id: str,
    item: dict[str, Any],
    *,
    phase: dict[str, Any],
    cycle_state: str | None,
    round_number: int,
    market_event_recent: bool,
    execution_pending: bool,
    execution_status: str,
    position_context: dict[str, Any] | None = None,
    response_lane: str = "",
    response_targets: list[str] | None = None,
    response_gap: list[str] | None = None,
) -> tuple[str, list[str]]:
    status = str(item.get("status") or "standby")
    expected_case_count = int(item.get("expected_case_count", 0) or 0)
    common = _build_common_context(
        phase,
        cycle_state=cycle_state,
        round_number=round_number,
        market_event_recent=market_event_recent,
        execution_pending=execution_pending,
        execution_status=execution_status,
    )
    prompts: dict[str, tuple[str, list[str]]] = {
        "ashare": (
            f"{common}你是总协调。现在不要只看状态栏，要主动判断谁该先干活、谁该被质询、谁该补材料。"
            " 先拉齐研究/策略/风控/审计分工，再决定是否推进 round、是否收敛提案、是否需要升级催办。",
            ["分工结论", "当前推进动作", "需要质询或升级的对象"],
        ),
        "ashare-research": (
            f"{common}你负责市场事实和机会发现。请结合热点、异动、新闻、板块强弱、个股势能，"
            " 输出当前最值得盯的方向和机会票；若市场变了，就用新事实推翻旧结论，不要机械复读昨天的票。",
            ["市场变化摘要", "机会方向/个股", "支撑这些判断的事实证据"],
        ),
        "ashare-strategy": (
            f"{common}你负责打法编排。请根据市场风格自主决定是盘中发现、龙头、低位挖掘、超跌反弹、尾盘潜伏、做 T 还是替换持仓。"
            " 如有必要，自己组织参数、权重和战法组合后再调用 runtime；不要把 runtime 当固定筛选器。",
            ["本轮策略假设", "参数/权重调整", "是否需要调用 runtime 及理由"],
        ),
        "ashare-risk": (
            f"{common}你负责风控闸门。请盯住仓位、单票上限、回撤、黑名单、题材过热、执行条件和消息面雷区。"
            " 如果现在该谨慎、减仓、阻断或只允许预演，直接讲清楚，不要做和稀泥式通过。",
            ["风险判断", "阻断或放行理由", "需要团队立刻修正的点"],
        ),
        "ashare-audit": (
            f"{common}你负责证据和复核。请检查提案是不是拿得出事实链、有没有反证、有没有偷换概念、有没有忽略执行和风控约束。"
            " 若讨论老化或纪要空转，要明确指出卡点并要求补材料。",
            ["质询点", "证据缺口", "纪要/复盘更新建议"],
        ),
        "ashare-executor": (
            f"{common}你负责执行反馈。请盯住预演、真实报单、撤单、回执、持仓与做 T 机会。"
            " 如果没有正式执行条件，就继续预演并把阻断原因讲明白；不要越权交易。",
            ["执行状态", "阻断或回执", "需要上游补齐的执行前提"],
        ),
        "ashare-runtime": (
            f"{common}你是运行底座，不负责拍脑袋选股。请刷新候选样本、市场事实和监控上下文，供研究与策略消费。"
            " 只有当市场环境变化或参数组合变化时才有必要重跑；刷新后要明确哪些事实更新了。",
            ["更新后的事实底座", "刷新范围", "可供上游消费的变化点"],
        ),
    }
    prompt, outputs = prompts.get(
        agent_id,
        (
            f"{common}请根据当前阶段补齐你负责的工作，并给出结构化结论。",
            ["工作结论", "关键事实", "下一步建议"],
        ),
    )
    if agent_id == "ashare" and cycle_state in {None, "idle"}:
        prompt += " 若候选池、focus pool 或 execution pool 已就绪但 cycle 仍 idle，默认先推动 bootstrap/启动 round 1，再安排后续质询。"
        outputs = ["分工结论", "是否启动/推进 round 1", "需要质询或升级的对象"]
    if agent_id == "ashare-strategy" and (cycle_state in {None, "idle"} or expected_case_count <= 0):
        prompt += (
            " 当前还没有稳定的主线编排时，默认先产出一份 compose 草案：先理解市场，再决定是否需要 runtime。"
            " 写清市场假设、playbooks、factors、weights、约束、采用理由、放弃项，再决定是否调用 /runtime/jobs/compose-from-brief。"
        )
        outputs = ["市场假设", "工具选择理由与放弃项", "playbooks/factors/weights/约束", "是否调用 compose-from-brief 及理由", "调用后的下一步动作"]
    if agent_id == "ashare-research":
        agent_proposed_count = int(((item.get("autonomy_metrics") or {}).get("agent_proposed_count") or 0))
        if agent_proposed_count > 0:
            prompt += (
                f" 当前已有 {agent_proposed_count} 只 agent_proposed 自提票进入讨论主线。"
                " 你不能只报热点摘要，还要补齐这些非默认机会票的市场逻辑、证据和风险点。"
            )
            outputs = list(dict.fromkeys([*outputs, "自提机会票补证"]))
    if agent_id == "ashare-strategy":
        autonomy_metrics = dict(item.get("autonomy_metrics") or {})
        if bool(autonomy_metrics.get("retry_required")):
            prompt += (
                " 上一轮 compose 已被系统判定为失败后待重编排。"
                " 现在必须直接进入第二轮 compose：先说明原市场假设为何失效，再重写假设、切换 playbooks/factors/constraints，"
                " 并基于 auto_replan 给出的方向继续组织下一轮 compose。"
            )
            outputs = list(
                dict.fromkeys(
                    [*outputs, "第二轮 compose 草案", "假设修正说明", "重编排原因", "下一轮 compose 请求"]
                )
            )
    if status == "overdue":
        prompt += " 你当前已经超时，必须优先回应该阶段最紧急的任务。"
    elif status == "needs_work":
        prompt += " 你当前还没有足够的新产出，必须补齐本阶段该有的工作痕迹。"
    quality_instruction = _build_quality_instruction(agent_id, item)
    if quality_instruction:
        prompt += " " + quality_instruction
    prompt = _append_market_response_instruction(
        prompt,
        response_lane=response_lane,
        response_targets=list(response_targets or []),
        response_gap=list(response_gap or []),
        market_event_recent=market_event_recent,
    )
    merged_outputs = list(dict.fromkeys([*outputs, *(response_targets or [])]))
    return prompt, merged_outputs


def _build_follow_up_overrides(
    *,
    tasks: list[dict[str, Any]],
    items: list[dict[str, Any]],
    last_agents: dict[str, dict[str, Any]],
    phase: dict[str, Any],
    cycle_state: str | None,
    round_number: int,
    execution_summary: dict[str, Any],
    execution_pending: bool,
    execution_status: str,
    position_context: dict[str, Any] | None,
    interval_seconds: int,
    now: datetime,
) -> dict[str, dict[str, Any]]:
    item_map = {str(item.get("agent_id") or ""): item for item in items}
    task_map = {str(task.get("agent_id") or ""): task for task in tasks}
    attention_ids = {
        str(item.get("agent_id") or "")
        for item in items
        if str(item.get("status") or "") in {"needs_work", "overdue"}
    }
    completion_window = max(interval_seconds * 2, 900)
    position_context = dict(position_context or {})
    position_ratio = float(position_context.get("current_total_ratio", 0.0) or 0.0)
    position_limit = float(position_context.get("equity_position_limit", 0.0) or 0.0)
    position_count = int(position_context.get("position_count", 0) or 0)
    available_budget = float(position_context.get("available_test_trade_value", 0.0) or 0.0)
    stock_budget = float(position_context.get("stock_test_budget_amount", 0.0) or 0.0)
    has_room_to_add = available_budget >= 10000.0 and position_limit > 0 and position_ratio + 0.03 < position_limit
    near_full_position = position_limit > 0 and position_ratio >= max(position_limit - 0.03, position_limit * 0.85)
    submitted_count = int(execution_summary.get("submitted_count", 0) or 0)
    preview_count = int(execution_summary.get("preview_count", 0) or 0)
    blocked_count = int(execution_summary.get("blocked_count", 0) or 0)
    latest_execution_reconciliation = dict(execution_summary.get("latest_execution_reconciliation") or {})
    reconciliation_status = str(
        execution_summary.get("reconciliation_status") or latest_execution_reconciliation.get("status") or ""
    ).strip()
    trade_count = int(execution_summary.get("trade_count", latest_execution_reconciliation.get("trade_count", 0)) or 0)
    matched_order_count = int(
        execution_summary.get("matched_order_count", latest_execution_reconciliation.get("matched_order_count", 0)) or 0
    )
    reconciliation_summary_lines = [
        str(line)
        for line in list(
            execution_summary.get("reconciliation_summary_lines")
            or latest_execution_reconciliation.get("summary_lines")
            or []
        )
        if str(line)
    ]
    latest_review_board_summary_lines = [
        str(line)
        for line in list(execution_summary.get("latest_review_board_summary_lines") or [])
        if str(line)
    ]
    latest_nightly_sandbox = dict(execution_summary.get("latest_nightly_sandbox") or {})
    nightly_sandbox_summary_lines = [
        str(line)
        for line in list(latest_nightly_sandbox.get("summary_lines") or [])
        if str(line)
    ]

    def _completed_recent(agent_id: str) -> bool:
        event = dict(last_agents.get(agent_id) or {})
        completed_at = str(event.get("completed_at") or "").strip() or None
        if not completed_at:
            return False
        age = _seconds_since(completed_at, now=now)
        return age is not None and age <= completion_window

    follow_up: dict[str, dict[str, Any]] = {}
    discussion_agents = ("ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit")
    discussion_ready = all(_completed_recent(agent_id) for agent_id in discussion_agents)

    if (
        cycle_state in {None, "idle"}
        and "ashare" in item_map
        and "ashare" in task_map
        and "ashare" not in attention_ids
    ):
        prompt = (
            f"当前阶段是{phase['label']}，讨论态={cycle_state or 'idle'}。"
            " 当前主线不该继续空转。请你先判断候选池是否已具备启动条件，"
            " 若已具备则立即推动 bootstrap/round 1，并明确谁先交研究、谁先给策略编排、谁负责风控质询。"
        )
        follow_up["ashare"] = {
            "task_prompt": prompt,
            "task_reason": "discussion cycle 仍 idle；总协调应主动启动 round 1 而不是等待",
            "expected_outputs": ["是否启动 round 1", "启动条件/阻断", "首轮分工"],
            "dispatch_recommended": True,
            "task_mode": "follow_up",
        }

    strategy_task = task_map.get("ashare-strategy") or {}
    strategy_item = item_map.get("ashare-strategy") or {}
    if (
        phase.get("code") in {"auction", "morning_session", "afternoon_session", "tail_session"}
        and "ashare-strategy" in item_map
        and "ashare-strategy" not in attention_ids
        and (
            cycle_state in {None, "idle"}
            or int(strategy_task.get("expected_case_count", strategy_item.get("expected_case_count", 0)) or 0) <= 0
        )
    ):
        prompt = (
            f"当前阶段是{phase['label']}，讨论态={cycle_state or 'idle'}。"
            " 现在不要只做口头策略判断。请先产出一份可执行 compose 草案，"
            " 至少包含市场假设、playbooks、factors、weights、约束、工具选择理由、放弃项，必要时自己组织参数，"
            " 并明确是否调用 /runtime/jobs/compose-from-brief 进行验证。"
        )
        follow_up["ashare-strategy"] = {
            "task_prompt": prompt,
            "task_reason": "当前还没有稳定的策略主线；策略侧应先产出 compose 草案",
            "expected_outputs": ["compose 草案", "市场假设", "工具选择理由与放弃项", "是否调用 compose-from-brief", "调用后的下一步动作"],
            "dispatch_recommended": True,
            "task_mode": "follow_up",
        }

    if (
        discussion_ready
        and "ashare" in item_map
        and "ashare" in task_map
        and "ashare" not in attention_ids
        and cycle_state in {"round_1_running", "round_2_running", "round_running", "round_1_summarized", "round_summarized", "final_review_ready"}
    ):
        prompt = (
            f"当前阶段是{phase['label']}，讨论态={cycle_state}，轮次={round_number}。"
            " 研究、策略、风控、审计四个岗位都已经交了新材料。"
            " 现在不要继续等人，立即刷新 discussion cycle，判断是续轮、收敛、进入 final review，还是可以准备 finalize。"
        )
        follow_up["ashare"] = {
            "task_prompt": prompt,
            "task_reason": "四岗材料已齐；应由总协调推进 round 收敛或续轮",
            "expected_outputs": ["cycle 推进结论", "是否续轮/收敛/finalize", "仍需补材料的对象"],
            "dispatch_recommended": True,
            "task_mode": "follow_up",
        }

    if (
        phase.get("code") in {"morning_session", "afternoon_session", "tail_session"}
        and has_room_to_add
        and "ashare-strategy" in item_map
        and "ashare-strategy" in task_map
        and "ashare-strategy" not in attention_ids
    ):
        prompt = (
            f"当前阶段是{phase['label']}，当前股票仓位约 {position_ratio:.1%}/{position_limit:.1%}，"
            f"剩余测试预算约 {available_budget:.0f}/{stock_budget:.0f}。"
            " 仓位还没打满，不要空等讨论结束。请继续结合市场强弱决定是补仓、换股还是继续观察，并明确是否需要重组 runtime 参数再跑一轮。"
        )
        follow_up["ashare-strategy"] = {
            "task_prompt": prompt,
            "task_reason": "当前仓位未打满；需要继续找机会或组织替换方案",
            "expected_outputs": ["补仓/换股判断", "是否重跑 runtime", "优先级最高的下一手动作"],
            "dispatch_recommended": True,
            "task_mode": "follow_up",
        }

    if (
        phase.get("code") in {"morning_session", "afternoon_session", "tail_session"}
        and has_room_to_add
        and "ashare-research" in item_map
        and "ashare-research" in task_map
        and "ashare-research" not in attention_ids
    ):
        prompt = (
            f"当前阶段是{phase['label']}，当前还有可用仓位。"
            " 请优先补充新的热点、异动、消息催化和候补机会，不要只围着已有票打转；如果市场风格变了，要直接给出替代方向。"
        )
        follow_up["ashare-research"] = {
            "task_prompt": prompt,
            "task_reason": "当前仓位未打满；研究侧应继续补充新机会",
            "expected_outputs": ["新增方向/个股", "最新催化", "对现有结论的替代建议"],
            "dispatch_recommended": True,
            "task_mode": "follow_up",
        }

    if (
        phase.get("code") in {"morning_session", "afternoon_session", "tail_session"}
        and near_full_position
        and position_count > 0
        and "ashare-strategy" in item_map
        and "ashare-strategy" in task_map
        and "ashare-strategy" not in attention_ids
    ):
        prompt = (
            f"当前阶段是{phase['label']}，当前股票仓位约 {position_ratio:.1%}/{position_limit:.1%}，持仓数={position_count}。"
            " 仓位已经接近打满，不要再按补仓逻辑思考。请改做替换仓位评估：哪些票该腾挪，哪些新机会值得顶替进来，替换后整体胜率和风险是否更优。"
        )
        follow_up["ashare-strategy"] = {
            "task_prompt": prompt,
            "task_reason": "仓位接近打满；策略侧应转入替换仓位与去弱留强",
            "expected_outputs": ["替换候选", "应腾挪的持仓", "替换前后收益/风险比较"],
            "dispatch_recommended": True,
            "task_mode": "follow_up",
        }

    if (
        phase.get("code") in {"morning_session", "afternoon_session", "tail_session"}
        and position_count > 0
        and "ashare-risk" in item_map
        and "ashare-risk" in task_map
        and "ashare-risk" not in attention_ids
    ):
        prompt = (
            f"当前阶段是{phase['label']}，当前持仓数={position_count}，股票仓位约 {position_ratio:.1%}。"
            " 已经有持仓在场，就不要只看新票。请盯住持仓股异动、回撤、炸板、消息雷区和是否应减仓/做 T/换股。"
        )
        follow_up["ashare-risk"] = {
            "task_prompt": prompt,
            "task_reason": "当前已有持仓；风控侧需要盯持仓和换仓风险",
            "expected_outputs": ["持仓风险扫描", "是否减仓/做T/换股", "需要立刻提醒的风险点"],
            "dispatch_recommended": True,
            "task_mode": "follow_up",
        }

    if (
        phase.get("code") in {"morning_session", "afternoon_session", "tail_session"}
        and position_count > 0
        and "ashare-executor" in item_map
        and "ashare-executor" not in attention_ids
    ):
        prompt = (
            f"当前阶段是{phase['label']}，当前持仓数={position_count}。"
            " 请围绕持仓管理看执行层机会：是否有做 T 条件、是否需要撤单/改单、是否该准备尾盘换仓或执行预演。"
        )
        follow_up["ashare-executor"] = {
            "task_prompt": prompt,
            "task_reason": "当前已有持仓；执行侧需要盯做T、换仓和执行准备",
            "expected_outputs": ["执行层机会", "做T/换仓预演", "执行阻断或回执"],
            "dispatch_recommended": True,
            "task_mode": "follow_up",
        }

    if (
        execution_pending
        and _completed_recent("ashare-executor")
        and "ashare-audit" in item_map
        and "ashare-audit" not in attention_ids
    ):
        prompt = (
            f"当前阶段是{phase['label']}，执行侧状态={execution_status or 'pending'}。"
            " 执行岗位刚交了新回执或预演结果。请你立即复核执行与讨论是否一致，补齐纪要、风控口径和后续动作建议。"
        )
        follow_up["ashare-audit"] = {
            "task_prompt": prompt,
            "task_reason": "执行侧刚有新结果；需要审计复核并更新纪要",
            "expected_outputs": ["执行复核结论", "纪要更新", "是否需升级提醒或改口径"],
            "dispatch_recommended": True,
            "task_mode": "follow_up",
        }

    if (
        submitted_count > 0
        and "ashare-risk" in item_map
        and "ashare-risk" in task_map
        and "ashare-risk" not in attention_ids
    ):
        prompt = (
            f"当前阶段是{phase['label']}，执行侧已提交 {submitted_count} 笔。"
            " 请立刻复核真实提交后的仓位变化、集中度、单票风险、潜在追高和回撤风险，必要时给出减仓、替换或冻结后续执行建议。"
        )
        follow_up["ashare-risk"] = {
            "task_prompt": prompt,
            "task_reason": "执行侧已有真实提交；风控侧需要复核提交后风险",
            "expected_outputs": ["提交后风险复核", "是否冻结后续执行", "减仓/替换建议"],
            "dispatch_recommended": True,
            "task_mode": "follow_up",
        }

    if (
        (submitted_count > 0 or preview_count > 0 or blocked_count > 0)
        and "ashare" in item_map
        and "ashare" in task_map
        and "ashare" not in attention_ids
    ):
        prompt = (
            f"当前阶段是{phase['label']}，执行侧 submitted={submitted_count} preview={preview_count} blocked={blocked_count}。"
            " 现在总协调需要把执行结果接回主线，判断是否继续推进、暂停、复核，还是转入盘后学习/复盘。"
        )
        follow_up["ashare"] = {
            "task_prompt": prompt,
            "task_reason": "执行链已有新结果；总协调应决定后续推进或收口",
            "expected_outputs": ["执行后主线判断", "继续推进/暂停/复核决定", "需要谁继续工作"],
            "dispatch_recommended": True,
            "task_mode": "follow_up",
        }

    if (
        phase.get("code") in {"post_close", "night_review"}
        and _completed_recent("ashare-research")
        and _completed_recent("ashare-audit")
        and "ashare-strategy" in item_map
        and "ashare-strategy" in task_map
        and "ashare-strategy" not in attention_ids
    ):
        prompt = (
            f"当前阶段是{phase['label']}。研究和审计的新产物已经落地。"
            " 请你基于今天的市场事实、执行反馈和复盘结论，组织夜间沙盘、参数建议和次日打法预案，不要只停留在复述今天发生了什么。"
        )
        follow_up["ashare-strategy"] = {
            "task_prompt": prompt,
            "task_reason": "盘后材料已齐；需要推进夜间沙盘和次日打法",
            "expected_outputs": ["夜间沙盘方向", "参数建议", "次日策略预案"],
            "dispatch_recommended": True,
            "task_mode": "follow_up",
        }

    real_trade_observed = reconciliation_status == "ok" and (trade_count > 0 or matched_order_count > 0)
    review_board_ready = bool(latest_review_board_summary_lines)
    nightly_sandbox_ready = bool(nightly_sandbox_summary_lines)
    if (
        phase.get("code") in {"post_close", "night_review"}
        and review_board_ready
        and not nightly_sandbox_ready
        and "ashare-strategy" in item_map
        and "ashare-strategy" in task_map
        and "ashare-strategy" not in attention_ids
    ):
        review_hint = latest_review_board_summary_lines[0]
        follow_up["ashare-strategy"] = {
            "task_prompt": (
                f"当前阶段是{phase['label']}。{review_hint}"
                " 复盘看板已经出来，但夜间沙盘还没形成。请把盘后事实、风控结论和执行反馈转成次日打法推演，"
                " 明确核心假设、优先战法、参数方向和放弃项。"
            ),
            "task_reason": "盘后 review board 已落地；策略侧应继续推进夜间沙盘与次日预案",
            "expected_outputs": ["夜间沙盘推演", "次日核心假设", "战法/参数优先级"],
            "dispatch_recommended": True,
            "task_mode": "follow_up",
        }

    if (
        phase.get("code") == "night_review"
        and nightly_sandbox_ready
        and "ashare-audit" in item_map
        and "ashare-audit" in task_map
        and "ashare-audit" not in attention_ids
    ):
        sandbox_hint = nightly_sandbox_summary_lines[0]
        follow_up["ashare-audit"] = {
            "task_prompt": (
                f"当前阶段是{phase['label']}。{sandbox_hint}"
                " 夜间沙盘已经产出，请你检查推演是否真正基于当天事实、真实成交与风险边界，"
                " 把次日预案、证据缺口和禁止动作整理成正式纪要。"
            ),
            "task_reason": "夜间沙盘已落地；审计侧应收口次日预案纪要",
            "expected_outputs": ["次日预案纪要", "证据缺口", "禁止动作与边界"],
            "dispatch_recommended": True,
            "task_mode": "follow_up",
        }

    if phase.get("code") in {"post_close", "night_review"} and real_trade_observed:
        reconciliation_hint = (
            reconciliation_summary_lines[0]
            if reconciliation_summary_lines
            else f"真实成交/对账已落地，matched={matched_order_count} trades={trade_count}。"
        )
        review_hint = (
            latest_review_board_summary_lines[0]
            if latest_review_board_summary_lines
            else "当前还缺正式 review board 摘要，请结合真实成交补齐治理口径。"
        )
        sandbox_hint = (
            nightly_sandbox_summary_lines[0]
            if nightly_sandbox_summary_lines
            else "夜间沙盘尚未产出，请按真实成交结果重建次日打法与参数假设。"
        )

        if (
            "ashare-audit" in item_map
            and "ashare-audit" in task_map
            and "ashare-audit" not in attention_ids
        ):
            follow_up["ashare-audit"] = {
                "task_prompt": (
                    f"当前阶段是{phase['label']}。{reconciliation_hint}"
                    f" {review_hint}"
                    " 请把真实成交、执行回执、讨论结论和盘后纪要收口成同一份复盘口径，明确哪些判断被市场验证，哪些需要追责或修正。"
                ),
                "task_reason": "已出现真实成交；审计侧需要收口纪要与成交复盘",
                "expected_outputs": ["真实成交复盘", "纪要收口", "需要追责或补证据的点"],
                "dispatch_recommended": True,
                "task_mode": "follow_up",
            }

        if (
            "ashare-risk" in item_map
            and "ashare-risk" in task_map
            and "ashare-risk" not in attention_ids
        ):
            follow_up["ashare-risk"] = {
                "task_prompt": (
                    f"当前阶段是{phase['label']}。{reconciliation_hint}"
                    " 请按真实成交后的持仓、集中度、可用仓位、回撤暴露和尾部风险重新复核，"
                    " 判断是否需要降风险、替换仓位、冻结某类打法，或调整明日风险边界。"
                ),
                "task_reason": "已出现真实成交；风控侧需要成交后风险与仓位复核",
                "expected_outputs": ["成交后风险复核", "仓位/集中度判断", "次日风险边界建议"],
                "dispatch_recommended": True,
                "task_mode": "follow_up",
            }

        if (
            "ashare-strategy" in item_map
            and "ashare-strategy" in task_map
            and "ashare-strategy" not in attention_ids
        ):
            follow_up["ashare-strategy"] = {
                "task_prompt": (
                    f"当前阶段是{phase['label']}。{reconciliation_hint}"
                    f" {sandbox_hint}"
                    " 请基于真实成交效果复盘本轮打法、参数、权重和 runtime 组织方式，"
                    " 说明哪些该强化、哪些该降权、哪些只适合保留在观察池。"
                ),
                "task_reason": "已出现真实成交；策略侧需要归因打法与参数调整",
                "expected_outputs": ["打法归因", "参数/权重调整建议", "次日 runtime 组织建议"],
                "dispatch_recommended": True,
                "task_mode": "follow_up",
            }

        if (
            "ashare" in item_map
            and "ashare" in task_map
            and "ashare" not in attention_ids
        ):
            follow_up["ashare"] = {
                "task_prompt": (
                    f"当前阶段是{phase['label']}。{reconciliation_hint}"
                    " 现在由你决定是否转入正式学习归因与收口：谁的判断被验证、谁要补材料、谁该调低权重，"
                    " 以及明天开盘前要先检查哪些风险或机会。"
                ),
                "task_reason": "已出现真实成交；总协调应推动学习归因与次日主线",
                "expected_outputs": ["学习归因结论", "团队调整动作", "次日开盘前检查清单"],
                "dispatch_recommended": True,
                "task_mode": "follow_up",
            }

    return follow_up


def build_agent_task_plan(
    supervision_payload: dict[str, Any],
    *,
    execution_summary: dict[str, Any] | None = None,
    position_context: dict[str, Any] | None = None,
    latest_market_change_at: str | None = None,
    meeting_state_store: StateStore | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now()
    trade_date = str(supervision_payload.get("trade_date") or "").strip() or None
    phase = resolve_market_phase(trade_date, now=current)
    position_context = dict(position_context or {})
    items = [dict(item) for item in list(supervision_payload.get("items") or [])]
    item_map = {str(item.get("agent_id") or ""): item for item in items}
    cycle_state = str(supervision_payload.get("cycle_state") or "").strip() or None
    round_number = int(supervision_payload.get("round") or 0)
    execution_summary = dict(execution_summary or {})
    execution_status = str(execution_summary.get("dispatch_status") or execution_summary.get("status") or "").strip()
    execution_pending = bool(int(execution_summary.get("intent_count", 0) or 0) > 0 or execution_status in {"queued", "preview", "submitted"})
    market_change_age = _seconds_since(latest_market_change_at, now=current)
    market_event_recent = market_change_age is not None and market_change_age <= 1800
    active_agents = set(phase.get("active_agents") or ())
    sync_agent_task_completion_from_activity(
        meeting_state_store,
        trade_date,
        items=items,
        now=current,
    )
    dispatch_state = _load_dispatch_state(meeting_state_store, trade_date)
    last_agents = dict(dispatch_state.get("agents") or {})
    interval_seconds = int(phase.get("dispatch_interval_seconds") or 900)
    context_markers = {
        "phase_code": phase.get("code"),
        "cycle_state": cycle_state,
        "round": round_number,
        "market_event_recent": market_event_recent,
        "execution_pending": execution_pending,
        "execution_status": execution_status,
        "progress_blockers": list(supervision_payload.get("progress_blockers") or []),
    }

    tasks: list[dict[str, Any]] = []
    for item in items:
        agent_id = str(item.get("agent_id") or "").strip()
        if not agent_id:
            continue
        response_lane = _resolve_response_lane(
            phase,
            position_context=position_context,
            execution_pending=execution_pending,
        )
        response_targets = _expected_market_response_outputs(
            agent_id,
            phase,
            position_context=position_context,
            execution_pending=execution_pending,
            market_event_recent=market_event_recent,
        )
        response_outputs = _detect_market_response_outputs(item)
        response_gap = [label for label in response_targets if label not in response_outputs]
        response_lagging = bool(
            market_event_recent
            and response_gap
            and market_change_age is not None
            and market_change_age >= 300
            and agent_id in {"ashare", "ashare-research", "ashare-strategy", "ashare-risk", "ashare-runtime"}
        )
        market_response_state = "lagging" if response_lagging else ("needs_output" if response_gap else "aligned")
        is_attention = str(item.get("status") or "") in {"needs_work", "overdue"} or response_lagging
        if agent_id not in active_agents and not is_attention:
            continue
        if agent_id == "ashare-runtime" and not is_attention and not market_event_recent:
            continue
        if agent_id == "ashare-executor" and not (is_attention or execution_pending):
            continue

        task_prompt, expected_outputs = _task_prompt_for_agent(
            agent_id,
            item,
            phase=phase,
            cycle_state=cycle_state,
            round_number=round_number,
            market_event_recent=market_event_recent,
            execution_pending=execution_pending,
            execution_status=execution_status,
            position_context=position_context,
            response_lane=response_lane,
            response_targets=response_targets,
            response_gap=response_gap,
        )
        task_reason_parts = [f"阶段={phase.get('label')}"]
        if is_attention:
            task_reason_parts.append(f"状态={item.get('status')}")
        task_reason_parts.extend(_build_task_reason_fragments(item))
        task_reason_parts.append(f"主导链路={response_lane}")
        if response_lagging:
            task_reason_parts.append("市场响应迟滞")
        elif response_gap:
            task_reason_parts.append("待补市场响应产物")
        if response_gap:
            task_reason_parts.append("缺口=" + "/".join(response_gap[:3]))
        if market_event_recent and agent_id in {"ashare", "ashare-research", "ashare-strategy", "ashare-risk"}:
            task_reason_parts.append("市场发生新变化")
        if execution_pending and agent_id in {"ashare", "ashare-risk", "ashare-audit", "ashare-executor"}:
            task_reason_parts.append(f"执行待跟进={execution_status or 'pending'}")
        if cycle_state and agent_id in {"ashare", "ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"}:
            task_reason_parts.append(f"讨论态={cycle_state}")
        dispatch_key = _build_dispatch_key(agent_id, item, str(phase.get("code") or ""), context_markers)
        last_event = dict(last_agents.get(agent_id) or {})
        last_key = str(last_event.get("dispatch_key") or "")
        last_sent_at = str(last_event.get("sent_at") or "").strip() or None
        last_age = _seconds_since(last_sent_at, now=current)
        completed_at = str(last_event.get("completed_at") or "").strip() or None
        completed_age = _seconds_since(completed_at, now=current)
        dispatch_recommended = (
            is_attention
            or last_key != dispatch_key
            or last_age is None
            or last_age >= interval_seconds
        )
        if completed_at and not is_attention and last_key == dispatch_key and completed_age is not None and completed_age < interval_seconds:
            dispatch_recommended = False
        task = {
            "agent_id": agent_id,
            "status": item.get("status"),
            "quality_state": item.get("quality_state"),
            "quality_reason": item.get("quality_reason"),
            "response_lane": response_lane,
            "market_response_state": market_response_state,
            "market_response_outputs": response_outputs,
            "market_response_targets": response_targets,
            "market_response_gap": response_gap,
            "covered_case_count": item.get("covered_case_count"),
            "expected_case_count": item.get("expected_case_count"),
            "phase_code": phase.get("code"),
            "phase_label": phase.get("label"),
            "task_prompt": task_prompt,
            "task_reason": "；".join(task_reason_parts),
            "expected_outputs": expected_outputs,
            "dispatch_key": dispatch_key,
            "dispatch_recommended": dispatch_recommended,
            "last_dispatched_at": last_sent_at,
            "last_completed_at": completed_at,
            "dispatch_interval_seconds": interval_seconds,
        }
        base_action_reason = str(item.get("supervision_action_reason") or "").strip()
        position_action_reason = _build_position_aware_action_reason(agent_id, phase, position_context)
        event_action_reason = _build_event_aware_action_reason(
            agent_id,
            phase,
            market_event_recent=market_event_recent,
            latest_market_change_at=latest_market_change_at,
            execution_pending=execution_pending,
            execution_status=execution_status,
            execution_summary=execution_summary,
        )
        action_reason_parts = [part for part in [base_action_reason, position_action_reason, event_action_reason] if str(part).strip()]
        if response_lagging:
            action_reason_parts.append("市场已变化但当前仍未形成对口产物，按真实迟滞处理。")
        if action_reason_parts:
            task["supervision_action_reason"] = " ".join(action_reason_parts)
        tasks.append(task)

    follow_up_overrides = _build_follow_up_overrides(
        tasks=tasks,
        items=items,
        last_agents=last_agents,
        phase=phase,
        cycle_state=cycle_state,
        round_number=round_number,
        execution_summary=execution_summary,
        execution_pending=execution_pending,
        execution_status=execution_status,
        position_context=position_context,
        interval_seconds=interval_seconds,
        now=current,
    )
    for agent_id, override in follow_up_overrides.items():
        existing_task = next((task for task in tasks if str(task.get("agent_id") or "") == agent_id), None)
        if existing_task is None:
            response_lane = _resolve_response_lane(
                phase,
                position_context=position_context,
                execution_pending=execution_pending,
            )
            base_item = item_map.get(agent_id) or {}
            response_targets = _expected_market_response_outputs(
                agent_id,
                phase,
                position_context=position_context,
                execution_pending=execution_pending,
                market_event_recent=market_event_recent,
            )
            response_outputs = _detect_market_response_outputs(base_item)
            response_gap = [label for label in response_targets if label not in response_outputs]
            tasks.append(
                {
                    "agent_id": agent_id,
                    "status": (item_map.get(agent_id) or {}).get("status"),
                    "quality_state": (item_map.get(agent_id) or {}).get("quality_state"),
                    "quality_reason": (item_map.get(agent_id) or {}).get("quality_reason"),
                    "response_lane": response_lane,
                    "market_response_state": "lagging" if market_event_recent and response_gap else ("needs_output" if response_gap else "aligned"),
                    "market_response_outputs": response_outputs,
                    "market_response_targets": response_targets,
                    "market_response_gap": response_gap,
                    "covered_case_count": (item_map.get(agent_id) or {}).get("covered_case_count"),
                    "expected_case_count": (item_map.get(agent_id) or {}).get("expected_case_count"),
                    "phase_code": phase.get("code"),
                    "phase_label": phase.get("label"),
                    "dispatch_key": hashlib.sha1(
                        json.dumps(
                            {
                                "agent_id": agent_id,
                                "phase_code": phase.get("code"),
                                "task_mode": override.get("task_mode"),
                                "task_reason": override.get("task_reason"),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ).encode("utf-8")
                    ).hexdigest(),
                    "last_dispatched_at": None,
                    "last_completed_at": None,
                    "dispatch_interval_seconds": interval_seconds,
                    **override,
                }
            )
            continue
        existing_task.update(override)

    for task in tasks:
        agent_id = str(task.get("agent_id") or "")
        response_lane = str(task.get("response_lane") or _resolve_response_lane(
            phase,
            position_context=position_context,
            execution_pending=execution_pending,
        ))
        response_targets = [
            str(item)
            for item in list(
                task.get("market_response_targets")
                or _expected_market_response_outputs(
                    agent_id,
                    phase,
                    position_context=position_context,
                    execution_pending=execution_pending,
                    market_event_recent=market_event_recent,
                )
            )
            if str(item).strip()
        ]
        base_item = item_map.get(agent_id) or {}
        response_outputs = [
            str(item)
            for item in list(task.get("market_response_outputs") or _detect_market_response_outputs(base_item))
            if str(item).strip()
        ]
        response_gap = [label for label in response_targets if label not in response_outputs]
        market_response_state = str(task.get("market_response_state") or "").strip()
        if not market_response_state:
            market_response_state = "lagging" if market_event_recent and response_gap else ("needs_output" if response_gap else "aligned")
        task["response_lane"] = response_lane
        task["market_response_targets"] = response_targets
        task["market_response_outputs"] = response_outputs
        task["market_response_gap"] = response_gap
        task["market_response_state"] = market_response_state
        task["task_prompt"] = _append_market_response_instruction(
            str(task.get("task_prompt") or ""),
            response_lane=response_lane,
            response_targets=response_targets,
            response_gap=response_gap,
            market_event_recent=market_event_recent,
        )
        task["expected_outputs"] = list(
            dict.fromkeys(
                [str(item) for item in list(task.get("expected_outputs") or []) if str(item).strip()]
                + response_targets
            )
        )
        if market_response_state == "lagging" and "市场响应迟滞" not in str(task.get("task_reason") or ""):
            task["task_reason"] = str(task.get("task_reason") or "").rstrip("；") + "；市场响应迟滞"
            task["dispatch_recommended"] = True

        dispatch_key = str(task.get("dispatch_key") or "").strip()
        last_event = dict(last_agents.get(agent_id) or {})
        last_key = str(last_event.get("dispatch_key") or "")
        last_sent_at = str(last_event.get("sent_at") or "").strip() or None
        last_age = _seconds_since(last_sent_at, now=current)
        completed_at = str(last_event.get("completed_at") or "").strip() or None
        completed_age = _seconds_since(completed_at, now=current)
        latest_activity_at = str((item_map.get(agent_id) or {}).get("last_active_at") or "").strip() or None
        latest_activity_age = _seconds_since(latest_activity_at, now=current)
        latest_activity_after_dispatch = (
            bool(latest_activity_at and last_sent_at)
            and latest_activity_age is not None
            and last_age is not None
            and latest_activity_age < last_age
        )
        is_attention = str(task.get("status") or "") in {"needs_work", "overdue"} or market_response_state == "lagging"
        dispatch_recommended = (
            is_attention
            or last_key != dispatch_key
            or last_age is None
            or last_age >= interval_seconds
            or (completed_at is None and latest_activity_after_dispatch)
        )
        if completed_at and not is_attention and last_key == dispatch_key and completed_age is not None and completed_age < interval_seconds:
            dispatch_recommended = False
        task["dispatch_recommended"] = dispatch_recommended
        task["last_dispatched_at"] = last_sent_at
        task["last_completed_at"] = completed_at
        task["dispatch_interval_seconds"] = interval_seconds

    tasks.sort(key=_task_priority_key)
    recommended_tasks = [task for task in tasks if task.get("dispatch_recommended")]
    summary_lines = [
        f"当前催办阶段={phase.get('label')} recommended={len(recommended_tasks)}",
        str(phase.get("prompt_hint") or ""),
    ]
    quality_summary_lines = [
        str(line).strip()
        for line in list(supervision_payload.get("quality_summary_lines") or [])
        if str(line).strip()
    ]
    progress_blockers = [
        str(line).strip()
        for line in list(supervision_payload.get("progress_blockers") or [])
        if str(line).strip()
    ]
    if quality_summary_lines:
        summary_lines.append("推进质量摘要=" + "；".join(quality_summary_lines[:2]))
    if progress_blockers:
        summary_lines.append("当前主线卡点=" + "；".join(progress_blockers[:2]))
    if market_event_recent:
        summary_lines.append(f"最近市场变化={latest_market_change_at}")
    lagging_agents = [str(task.get("agent_id") or "") for task in tasks if str(task.get("market_response_state") or "") == "lagging"]
    if lagging_agents:
        summary_lines.append("市场响应迟滞=" + "、".join(lagging_agents[:4]))
    if execution_pending:
        summary_lines.append(f"执行待跟进状态={execution_status or 'pending'}")
    if follow_up_overrides:
        summary_lines.append(f"续发任务={len(follow_up_overrides)}")

    task_map = {str(task.get("agent_id") or ""): task for task in tasks}

    def _enrich_group(source_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched_group: list[dict[str, Any]] = []
        for raw_item in source_items:
            enriched = dict(raw_item)
            mapped_task = task_map.get(str(enriched.get("agent_id") or ""))
            if mapped_task:
                enriched.update(
                    {
                        "task_prompt": mapped_task.get("task_prompt"),
                        "task_reason": mapped_task.get("task_reason"),
                        "expected_outputs": mapped_task.get("expected_outputs"),
                        "response_lane": mapped_task.get("response_lane"),
                        "market_response_state": mapped_task.get("market_response_state"),
                        "market_response_outputs": mapped_task.get("market_response_outputs"),
                        "market_response_targets": mapped_task.get("market_response_targets"),
                        "market_response_gap": mapped_task.get("market_response_gap"),
                        "dispatch_recommended": mapped_task.get("dispatch_recommended"),
                        "phase_label": mapped_task.get("phase_label"),
                        "phase_code": mapped_task.get("phase_code"),
                        "dispatch_key": mapped_task.get("dispatch_key"),
                        "dispatch_interval_seconds": mapped_task.get("dispatch_interval_seconds"),
                        "last_dispatched_at": mapped_task.get("last_dispatched_at"),
                        "last_completed_at": mapped_task.get("last_completed_at"),
                        "supervision_action_reason": mapped_task.get("supervision_action_reason"),
                    }
                )
            enriched_group.append(enriched)
        return enriched_group

    enriched_items = _enrich_group(items)
    attention_items = _enrich_group(
        _sort_group_by_task_priority(
            [dict(item) for item in list(supervision_payload.get("attention_items") or [])],
            task_map,
        )
    )
    notify_items = _enrich_group(
        _sort_group_by_task_priority(
            [dict(item) for item in list(supervision_payload.get("notify_items") or [])],
            task_map,
        )
    )

    return {
        "phase": phase,
        "summary_lines": [line for line in summary_lines if line],
        "tasks": tasks,
        "recommended_tasks": recommended_tasks,
        "items": enriched_items,
        "attention_items": attention_items,
        "notify_items": notify_items,
    }
