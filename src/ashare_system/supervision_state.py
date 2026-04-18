"""Agent 监督状态: 催办签名、确认回写、通知抑制"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from .infra.audit_store import StateStore

SUPERVISION_CONTROL_KEY_PREFIX = "agent_supervision_control:"


def supervision_control_key(trade_date: str | None) -> str:
    return f"{SUPERVISION_CONTROL_KEY_PREFIX}{trade_date or 'unknown'}"


def _parse_iso_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _seconds_since(value: str | None, *, now: datetime) -> int | None:
    parsed = _parse_iso_datetime(value)
    if parsed is None:
        return None
    current = now.astimezone(parsed.tzinfo) if parsed.tzinfo and now.tzinfo else now
    return max(int((current - parsed).total_seconds()), 0)


def build_supervision_attention_signature(payload: dict[str, Any]) -> str:
    normalized_items = []
    for item in sorted(payload.get("attention_items", []), key=lambda row: str(row.get("agent_id") or "")):
        normalized_items.append(
            {
                "agent_id": item.get("agent_id"),
                "status": item.get("status"),
                "covered_case_count": item.get("covered_case_count"),
                "expected_case_count": item.get("expected_case_count"),
            }
        )
    envelope = {
        "trade_date": payload.get("trade_date"),
        "cycle_state": payload.get("cycle_state"),
        "round": payload.get("round"),
        "attention_items": normalized_items,
    }
    raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _load_control_state(store: StateStore | None, trade_date: str | None) -> dict[str, Any]:
    if not store:
        return {}
    payload = store.get(supervision_control_key(trade_date), {})
    return dict(payload) if isinstance(payload, dict) else {}


def _save_control_state(store: StateStore | None, trade_date: str | None, payload: dict[str, Any]) -> None:
    if not store:
        return
    store.set(supervision_control_key(trade_date), payload)


def _annotate_quality(item: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(item)
    status = str(enriched.get("status") or "").strip()
    covered = int(enriched.get("covered_case_count", 0) or 0)
    expected = int(enriched.get("expected_case_count", 0) or 0)
    activity_signal_count = int(enriched.get("activity_signal_count", 0) or 0)
    has_recent_activity = bool(activity_signal_count or str(enriched.get("last_active_at") or "").strip())

    quality_state = "observe"
    quality_reason = "当前以值班观察为主。"

    if status == "overdue":
        quality_state = "blocked"
        quality_reason = "当前岗位已超时，主线推进出现卡点。"
    elif status == "needs_work":
        quality_state = "low"
        if expected > 0 and covered < expected:
            if has_recent_activity:
                quality_reason = "有活动痕迹，但还没推进到本轮主线产物。"
            else:
                quality_reason = "本轮主线产物仍未补齐。"
        else:
            quality_reason = "当前缺少可确认的新产出。"
    elif expected > 0:
        if covered >= expected:
            quality_state = "good"
            quality_reason = "本轮主线材料已覆盖齐。"
        elif covered > 0:
            quality_state = "partial"
            quality_reason = "本轮主线有推进，但材料仍未补齐。"
        elif has_recent_activity:
            quality_state = "low"
            quality_reason = "有活动痕迹，但还没形成主线可消费产物。"
        else:
            quality_state = "low"
            quality_reason = "当前既无活动痕迹，也无主线覆盖。"
    elif status == "working":
        if has_recent_activity:
            quality_state = "good"
            quality_reason = "当前有新的活动痕迹或产物。"
        else:
            quality_state = "observe"
            quality_reason = "当前处于值班状态，尚未看到明确新产物。"

    enriched["quality_state"] = quality_state
    enriched["quality_reason"] = quality_reason
    return enriched


def _build_quality_summary(items: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    state_counts = {"good": 0, "partial": 0, "low": 0, "blocked": 0, "observe": 0}
    blockers: list[str] = []
    for item in items:
        quality_state = str(item.get("quality_state") or "observe")
        state_counts[quality_state] = state_counts.get(quality_state, 0) + 1
        agent_id = str(item.get("agent_id") or "").strip() or "-"
        status = str(item.get("status") or "").strip()
        covered = int(item.get("covered_case_count", 0) or 0)
        expected = int(item.get("expected_case_count", 0) or 0)
        quality_reason = str(item.get("quality_reason") or "").strip()
        if expected > 0 and covered < expected and status in {"needs_work", "overdue"}:
            blockers.append(
                f"{agent_id} 本轮仍缺 {max(expected - covered, 0)} 份主线材料"
                + ("，已有活动但未写回观点。" if str(item.get("last_active_at") or "").strip() else "。")
            )
            continue
        if quality_state == "blocked":
            blockers.append(f"{agent_id} 已超时阻塞：{quality_reason}")
        elif quality_state == "low" and "主线" in quality_reason:
            blockers.append(f"{agent_id} 推进质量偏低：{quality_reason}")

    summary_lines = [
        "推进质量: "
        f"good={state_counts.get('good', 0)} partial={state_counts.get('partial', 0)} "
        f"low={state_counts.get('low', 0)} blocked={state_counts.get('blocked', 0)} observe={state_counts.get('observe', 0)}。"
    ]
    if blockers:
        summary_lines.append("主线卡点: " + "；".join(blockers[:3]))
    if any("已有活动但未写回观点" in line for line in blockers):
        summary_lines.append("已发现“有活动但未推进主线”的情况，应把动作及时沉淀为讨论/执行可消费产物。")
    return summary_lines, blockers


def _build_supervision_action_reason(
    item: dict[str, Any],
    *,
    escalate_notification: bool,
    repeated_age_seconds: int | None,
) -> str:
    agent_id = str(item.get("agent_id") or "").strip() or "当前岗位"
    phase_label = str(item.get("phase_label") or "").strip()
    quality_reason = str(item.get("quality_reason") or "").strip()
    task_reason = str(item.get("task_reason") or "").strip()
    expected = int(item.get("expected_case_count", 0) or 0)
    covered = int(item.get("covered_case_count", 0) or 0)
    gap = max(expected - covered, 0)
    has_activity_without_writeback = bool(
        str(item.get("last_active_at") or "").strip()
        and expected > 0
        and gap > 0
    )

    reasons: list[str] = []
    if escalate_notification and repeated_age_seconds is not None:
        reasons.append(f"同一阻塞已持续 {repeated_age_seconds} 秒且重复催办未解除")
    if gap > 0:
        reasons.append(f"仍缺 {gap} 份主线材料")
    if has_activity_without_writeback:
        reasons.append("已有活动但没有写回主线产物")
    if quality_reason:
        reasons.append(quality_reason.removesuffix("。"))
    elif task_reason:
        reasons.append(task_reason.removesuffix("。"))
    if not reasons:
        reasons.append("当前阶段仍缺关键产出")

    unique_reasons = "；".join(dict.fromkeys(reasons))
    role_message_map = {
        "ashare-research": "请先把市场事实、热点变化和机会票写成正式研究结论，别停留在内部观察。",
        "ashare-strategy": "请先把打法、参数和 runtime 组织结论写回正式提案，别只做内部调参。",
        "ashare-risk": "请先把阻断/放行判断和风险边界落成正式口径，别只停留在提醒层。",
        "ashare-audit": "请先把证据链、反证和纪要结论补齐，别让主线停在口头判断。",
        "ashare-executor": "请先把执行预演、回执或阻断结果写清，别让执行链悬空。",
        "ashare-runtime": "请先把刷新后的事实底座和变化点回写给上游消费，别只做静默刷新。",
        "ashare": "请先把分工、推进判断和下一步决策写回主线，别让团队继续空转。",
    }
    phase_role_message_map = {
        "ashare-research": {
            "盘前预热": "盘前先收口隔夜消息、预期差和潜在方向。",
            "集合竞价": "竞价阶段重点讲清封单、撤单、强弱分层和第一批机会票。",
            "上午盘中": "盘中先把新热点、异动和承接变化落成正式机会判断。",
            "午间整理": "午间先把上午变化收口成结构化结论，给下午打法提供事实底座。",
            "下午盘中": "下午先补持续性和分歧转一致证据，别让机会判断停留在上午。",
            "尾盘收口": "尾盘先把尾盘潜伏、换仓线索和次日延续性研究结论补齐。",
            "盘后复盘": "盘后先把今天的事实、误判点和有效催化沉淀成复盘材料。",
            "夜间学习": "夜间先把研究结论转成可复用知识和次日预案输入。",
        },
        "ashare-strategy": {
            "盘前预热": "盘前先把主打法、候选战法和仓位组织思路定下来。",
            "集合竞价": "竞价阶段先决定是否切换打法、是否要快速组织 runtime 参数。",
            "上午盘中": "盘中先把打法判断、参数调整和 runtime 组织结论写回主线。",
            "午间整理": "午间先根据上午事实重排下午策略，不要沿用失效打法。",
            "下午盘中": "下午先判断是继续进攻、转防守、做T还是换仓，并写清依据。",
            "尾盘收口": "尾盘先给出尾盘潜伏、去弱留强和次日衔接打法。",
            "盘后复盘": "盘后先把打法归因、参数得失和次日预案沉淀下来。",
            "夜间学习": "夜间先完成沙盘、参数修正和新战法假设。",
        },
        "ashare-risk": {
            "盘前预热": "盘前先确认当日风险边界、禁区和特殊风险项。",
            "集合竞价": "竞价阶段先判断哪些方向该放行、哪些该直接拦住。",
            "上午盘中": "盘中先把阻断/放行和仓位边界写清，别让团队带着模糊风险往前冲。",
            "午间整理": "午间先复核上午暴露出来的仓位和题材风险，给下午定边界。",
            "下午盘中": "下午先盯持仓回撤、追高风险和换仓边界，别等尾盘再补。",
            "尾盘收口": "尾盘先确认换仓、做T和隔夜持仓的风险口径。",
            "盘后复盘": "盘后先把风险命中、漏判点和次日边界整理清楚。",
            "夜间学习": "夜间先把风险复盘转成可执行边界和约束建议。",
        },
        "ashare-audit": {
            "盘前预热": "盘前先明确今天什么证据算成立、什么情况必须补证。",
            "集合竞价": "竞价阶段先检查早盘判断是不是有证据支撑，别让情绪先跑。",
            "上午盘中": "盘中先把证据链、反证和纪要结论补齐，别让主线停在口头判断。",
            "午间整理": "午间先收口上午争议点，明确下午还要补哪些证据。",
            "下午盘中": "下午先盯判断有没有被市场证伪，并及时写回纪要。",
            "尾盘收口": "尾盘先把日内结论、换仓依据和尾盘动作收成正式纪要。",
            "盘后复盘": "盘后先把复盘口径、追责点和证据缺口整理完整。",
            "夜间学习": "夜间先把次日预案、禁止动作和证据缺口写进正式纪要。",
        },
        "ashare-executor": {
            "上午盘中": "盘中先把预演、执行条件和阻断结果写清，别让执行链断档。",
            "下午盘中": "下午先盯做T、撤改单和换仓准备，动作结果要及时回写。",
            "尾盘收口": "尾盘先把尾盘执行、换仓预演和未完成动作收口。",
        },
        "ashare-runtime": {
            "集合竞价": "竞价阶段先把竞价强弱、样本刷新和变化点明确给上游。",
            "上午盘中": "盘中先把底座变化和新样本刷新结果回写，别只在后台更新。",
            "下午盘中": "下午先补下午的新事实底座，供策略判断是否切换。",
            "尾盘收口": "尾盘先把尾盘扫描和次日预热需要的底座材料准备好。",
        },
        "ashare": {
            "盘前预热": "盘前先把今天的分工、关注方向和推进顺序讲清楚。",
            "集合竞价": "竞价阶段先决定谁盯竞价、谁收消息、谁准备风控口径。",
            "上午盘中": "盘中先把人手拉齐，别让团队各做各的却没人推进主线。",
            "午间整理": "午间先统一上午结论和下午分工，防止下午重复空转。",
            "下午盘中": "下午先判断是继续推进、收缩战线还是准备尾盘动作。",
            "尾盘收口": "尾盘先决定收口、换仓、做T和盘后交接顺序。",
            "盘后复盘": "盘后先推动复盘、归因和次日预案形成闭环。",
            "夜间学习": "夜间先推动学习、沙盘和次日准备收口。",
        },
    }
    phase_hint = f"{phase_label}阶段，" if phase_label else ""
    role_hint = role_message_map.get(agent_id, "请先把该阶段最缺的正式产物补出来。")
    phase_role_hint = (
        phase_role_message_map.get(agent_id, {}).get(phase_label, "").strip()
        if phase_label
        else ""
    )
    suffix = role_hint if not phase_role_hint else f"{phase_role_hint} {role_hint}"
    return f"{agent_id} 现在必须优先处理：{phase_hint}{unique_reasons}。{suffix}"


def annotate_supervision_payload(payload: dict[str, Any], meeting_state_store: StateStore | None) -> dict[str, Any]:
    enriched = dict(payload)
    signature = build_supervision_attention_signature(enriched)
    control_state = _load_control_state(meeting_state_store, enriched.get("trade_date"))
    now = datetime.now()
    ack_records = list(control_state.get("acks") or [])
    ack_map: dict[str, dict[str, Any]] = {}
    for record in ack_records:
        if str(record.get("signature") or "") != signature:
            continue
        agent_id = str(record.get("agent_id") or "").strip()
        if not agent_id:
            continue
        previous = ack_map.get(agent_id)
        if previous is None or str(record.get("acked_at") or "") >= str(previous.get("acked_at") or ""):
            ack_map[agent_id] = dict(record)

    item_map: dict[str, dict[str, Any]] = {}
    items = []
    for item in list(enriched.get("items") or []):
        quality_item = _annotate_quality(dict(item))
        items.append(quality_item)
        agent_id = str(quality_item.get("agent_id") or "").strip()
        if agent_id:
            item_map[agent_id] = quality_item

    attention_items = []
    acknowledged_items = []
    notify_items = []
    for item in list(enriched.get("attention_items") or []):
        agent_id = str(item.get("agent_id") or "").strip()
        ack = ack_map.get(agent_id)
        base_item = item_map.get(agent_id, {})
        enriched_item = {
            **base_item,
            **item,
            "acknowledged": bool(ack),
            "acknowledged_at": (ack or {}).get("acked_at"),
            "acknowledged_by": (ack or {}).get("actor"),
            "ack_note": (ack or {}).get("note"),
        }
        attention_items.append(enriched_item)
        if ack:
            acknowledged_items.append(enriched_item)
        else:
            notify_items.append(enriched_item)

    summary_lines = list(enriched.get("summary_lines") or [])
    quality_summary_lines, progress_blockers = _build_quality_summary(items)
    for line in quality_summary_lines:
        if line not in summary_lines:
            summary_lines.append(line)
    progress_line = f"已确认 {len(acknowledged_items)} 项，待催办 {len(notify_items)} 项。"
    if progress_line not in summary_lines:
        summary_lines.append(progress_line)

    last_notification = dict(control_state.get("last_notification") or {})
    last_signature = str(last_notification.get("signature") or "").strip()
    last_level = str(last_notification.get("level") or "").strip()
    repeated_same_attention = bool(last_signature and last_signature == signature)
    last_sent_at = str(last_notification.get("sent_at") or "").strip() or None
    repeated_age_seconds = _seconds_since(last_sent_at, now=now)
    escalate_notification = bool(
        repeated_same_attention
        and any(str(item.get("status") or "") == "overdue" for item in notify_items)
        and last_level in {"warning", "critical"}
        and repeated_age_seconds is not None
        and repeated_age_seconds >= 180
    )
    notification_level = "critical" if escalate_notification else (
        "warning" if any(str(item.get("status") or "") == "overdue" for item in notify_items) else "info"
    )
    notification_title = "Agent 升级催办" if escalate_notification else "Agent 自动催办"

    notify_items_enriched = []
    for item in notify_items:
        tier = "observe"
        tier_reason = "当前仅记录状态，无需主动催办。"
        action_reason = ""
        status = str(item.get("status") or "").strip()
        if status == "needs_work":
            tier = "remind"
            tier_reason = "已有任务但缺少本阶段新产出，应发普通催办。"
        elif status == "overdue":
            if escalate_notification:
                tier = "escalate"
                tier_reason = "同一阻塞已重复催办仍未解除，应升级派工并要求立即回应。"
            else:
                tier = "urgent"
                tier_reason = "已超过当前阶段响应窗口，应优先处理。"
        if tier in {"remind", "urgent", "escalate"}:
            action_reason = _build_supervision_action_reason(
                item,
                escalate_notification=escalate_notification and status == "overdue",
                repeated_age_seconds=repeated_age_seconds,
            )
        notify_items_enriched.append(
            {
                **item,
                "supervision_tier": tier,
                "supervision_tier_reason": tier_reason,
                "supervision_action_reason": action_reason,
            }
        )
    notify_items = notify_items_enriched

    if repeated_same_attention and repeated_age_seconds is not None:
        repeat_line = f"同一关注签名已持续 {repeated_age_seconds} 秒。"
        if repeat_line not in summary_lines:
            summary_lines.append(repeat_line)
    if escalate_notification:
        escalation_line = "当前存在重复未解除的 overdue 项，已升级催办级别。"
        if escalation_line not in summary_lines:
            summary_lines.append(escalation_line)
        lead_escalation_reason = next(
            (
                str(item.get("supervision_action_reason") or "").strip()
                for item in notify_items
                if str(item.get("supervision_tier") or "") == "escalate"
                and str(item.get("supervision_action_reason") or "").strip()
            ),
            "",
        )
        if lead_escalation_reason and lead_escalation_reason not in summary_lines:
            summary_lines.append("升级原因: " + lead_escalation_reason)

    enriched["attention_signature"] = signature
    enriched["items"] = items
    enriched["attention_items"] = attention_items
    enriched["acknowledged_items"] = acknowledged_items
    enriched["notify_items"] = notify_items
    enriched["notify_recommended"] = bool(notify_items)
    enriched["summary_lines"] = summary_lines
    enriched["quality_summary_lines"] = quality_summary_lines
    enriched["progress_blockers"] = progress_blockers
    enriched["last_notification"] = control_state.get("last_notification")
    enriched["notification_title"] = notification_title
    enriched["notification_level"] = notification_level
    enriched["escalated"] = escalate_notification
    return enriched


def record_supervision_notification(
    meeting_state_store: StateStore | None,
    trade_date: str | None,
    *,
    signature: str,
    level: str,
    item_count: int,
) -> dict[str, Any]:
    control_state = _load_control_state(meeting_state_store, trade_date)
    last_notification = {
        "signature": signature,
        "level": level,
        "item_count": item_count,
        "sent_at": datetime.now().isoformat(),
    }
    control_state["last_notification"] = last_notification
    _save_control_state(meeting_state_store, trade_date, control_state)
    return last_notification


def record_supervision_ack(
    meeting_state_store: StateStore | None,
    trade_date: str | None,
    *,
    signature: str,
    agent_id: str,
    actor: str,
    note: str = "",
) -> dict[str, Any]:
    control_state = _load_control_state(meeting_state_store, trade_date)
    ack_record = {
        "agent_id": agent_id,
        "signature": signature,
        "actor": actor,
        "note": note,
        "acked_at": datetime.now().isoformat(),
    }
    history = [
        item
        for item in list(control_state.get("acks") or [])
        if not (
            str(item.get("agent_id") or "") == agent_id
            and str(item.get("signature") or "") == signature
        )
    ]
    history.append(ack_record)
    control_state["acks"] = history[-200:]
    _save_control_state(meeting_state_store, trade_date, control_state)
    return ack_record
