"""消息模板"""

from __future__ import annotations

from ..contracts import MarketProfile

EXECUTION_STATUS_LABELS = {
    "preview": "预演",
    "submitted": "已提交",
    "blocked": "阻断",
    "paper_blocked": "阻断",
    "dispatch_failed": "失败",
    "cancel_submitted": "撤单已提交",
    "cancel_failed": "撤单失败",
    "FILLED": "已成交",
    "PARTIALLY_FILLED": "部分成交",
    "CANCELLED": "已撤单",
    "REJECTED": "已拒绝",
    "PENDING": "待处理",
}

TRADE_SIDE_LABELS = {
    "BUY": "买入",
    "SELL": "卖出",
}

EXECUTION_REASON_LABELS = {
    "preview_only": "仅生成预演回执，未实际报单",
    "dispatch_failed": "报单发送失败",
    "paper_blocked": "当前模式禁止真实报单",
    "execution_adapter_unavailable": "执行适配器不可用",
    "live_mock_fallback_blocked": "实盘模式下禁止 mock 执行",
    "live_not_enabled": "未开启实盘开关",
    "emergency_stop_active": "交易总开关处于暂停状态",
    "live_confirmation_required": "当前未处于允许提交的实盘状态",
    "stale_pending_order": "挂单超时，触发自动撤单",
    "broker timeout": "券商返回超时",
    "balance_unavailable": "账户资金不可用",
    "market_snapshot_fetch_failed": "行情快照抓取失败",
    "market_snapshot_unavailable": "行情快照不可用",
    "market_snapshot_stale": "行情快照已过期",
    "total_position_limit_reached": "总仓位已达上限",
    "budget_below_min_lot": "剩余预算不足一手",
    "trading_session_closed": "当前非交易时段",
    "execution_mode_unknown": "执行模式未识别",
}


def _label_execution_status(status: str | None) -> str:
    if not status:
        return "unknown"
    return EXECUTION_STATUS_LABELS.get(status, status)


def _label_execution_reason(reason: str | None) -> str:
    if not reason:
        return "unknown"
    return EXECUTION_REASON_LABELS.get(reason, reason)


def _label_trade_side(side: str | None) -> str:
    if not side:
        return "unknown"
    return TRADE_SIDE_LABELS.get(side, side)


def trade_executed_template(symbol: str, side: str, price: float, quantity: int, pnl: float | None = None) -> str:
    emoji = "🟢" if side == "BUY" else "🔴"
    side_label = _label_trade_side(side)
    lines = [
        f"{emoji} **交易执行**",
        f"- 标的: {symbol}",
        f"- 方向: {side_label}" + (f" ({side})" if side_label != side else ""),
        f"- 价格: {price:.3f}",
        f"- 数量: {quantity}",
    ]
    if pnl is not None:
        sign = "+" if pnl >= 0 else ""
        lines.append(f"- 盈亏: {sign}{pnl:.2f}")
    return "\n".join(lines)


def execution_order_event_template(
    action: str,
    symbol: str,
    name: str = "",
    account_id: str = "",
    side: str | None = None,
    quantity: int | None = None,
    price: float | None = None,
    order_id: str | None = None,
    status: str | None = None,
    decision_id: str | None = None,
    reason: str | None = None,
) -> str:
    lines = [f"📨 **{action}**"]
    lines.append(f"- 标的: {symbol}{f' {name}' if name else ''}")
    if account_id:
        lines.append(f"- 账户: {account_id}")
    if side:
        side_label = _label_trade_side(side)
        lines.append(f"- 方向: {side_label}" + (f" ({side})" if side_label != side else ""))
    if quantity is not None:
        lines.append(f"- 数量: {quantity}")
    if price is not None:
        lines.append(f"- 价格: {price:.3f}")
    if order_id:
        lines.append(f"- 订单号: {order_id}")
    if status:
        status_label = _label_execution_status(status)
        lines.append(f"- 状态: {status_label}" + (f" ({status})" if status_label != status else ""))
    if decision_id:
        lines.append(f"- 决策ID: {decision_id}")
    if reason:
        reason_label = _label_execution_reason(reason)
        lines.append(f"- 说明: {reason_label}" + (f" ({reason})" if reason_label != reason else ""))
    return "\n".join(lines)


def daily_report_template(date: str, profile: MarketProfile, total_pnl: float, positions: int) -> str:
    phase_emoji = {"冰点": "🧊", "回暖": "🌤", "主升": "🚀", "高潮": "🔥"}.get(profile.sentiment_phase, "📊")
    sign = "+" if total_pnl >= 0 else ""
    return "\n".join([
        f"📋 **日终复盘 {date}**",
        f"- 情绪阶段: {phase_emoji} {profile.sentiment_phase} ({profile.sentiment_score:.0f}分)",
        f"- 仓位上限: {profile.position_ceiling:.0%}",
        f"- 当日盈亏: {sign}{total_pnl:.2f}",
        f"- 持仓数量: {positions}",
        f"- 热点板块: {', '.join(profile.hot_sectors[:3]) or '无'}",
    ])


def risk_alert_template(rule: str, reason: str, symbol: str = "") -> str:
    target = f" [{symbol}]" if symbol else ""
    return f"⚠️ **风控告警{target}**\n- 规则: {rule}\n- 原因: {reason}"


def monitor_change_summary_template(title: str, lines: list[str]) -> str:
    if not lines:
        return title
    return "\n".join([f"📡 **{title}**", *[f"- {line}" for line in lines]])


def live_execution_alert_template(title: str, lines: list[str]) -> str:
    if not lines:
        return title
    return "\n".join([f"🚨 **{title}**", *[f"- {line}" for line in lines]])


def execution_dispatch_notification_template(title: str, lines: list[str]) -> str:
    if not lines:
        return title
    return "\n".join([f"📨 **{title}**", *[f"- {line}" for line in lines]])


def position_watch_notification_template(title: str, lines: list[str], action_suggestions: list[str] | None = None) -> str:
    body = [f"📡 **{title}**"]
    if lines:
        body.extend(f"- {line}" for line in lines)
    suggestions = [str(item).strip() for item in list(action_suggestions or []) if str(item).strip()]
    if suggestions:
        body.append("- 交易动作建议:")
        body.extend(f"  - {item}" for item in suggestions[:6])
    return "\n".join(body)


def latency_alert_notification_template(title: str, alerts: list[dict]) -> str:
    body = [f"⏱️ **{title}**"]
    if not alerts:
        body.append("- 当前没有延迟告警。")
        return "\n".join(body)
    for item in alerts[:6]:
        chain = str(item.get("chain") or "unknown")
        stage = str(item.get("stage") or "unknown")
        elapsed_ms = float(item.get("elapsed_ms", 0.0) or 0.0)
        threshold_ms = float(item.get("threshold_ms", 0.0) or 0.0)
        symbol = str((item.get("metadata") or {}).get("symbol") or "").strip()
        suffix = f" | {symbol}" if symbol else ""
        body.append(f"- {chain} / {stage}{suffix}: {elapsed_ms:.1f}ms > {threshold_ms:.1f}ms")
    return "\n".join(body)


def execution_precheck_summary_lines(execution_precheck: dict | None) -> list[str]:
    if not execution_precheck:
        return []
    lines = list(execution_precheck.get("summary_lines", []))
    minimum_total_invested_amount = execution_precheck.get("minimum_total_invested_amount")
    reverse_repo_reserved_amount = execution_precheck.get("reverse_repo_reserved_amount")
    reverse_repo_value = execution_precheck.get("reverse_repo_value")
    stock_test_budget_amount = execution_precheck.get("stock_test_budget_amount")
    stock_test_budget_remaining = execution_precheck.get("stock_test_budget_remaining")
    if (
        not any("测试基线" in line or "股票测试预算" in line for line in lines)
        and isinstance(minimum_total_invested_amount, (int, float))
    ):
        lines.append(
            f"测试基线 {minimum_total_invested_amount}，逆回购 {reverse_repo_value or 0.0} / {reverse_repo_reserved_amount or 0.0}，"
            f"股票测试预算 {stock_test_budget_remaining or 0.0} / {stock_test_budget_amount or 0.0}。"
        )
    approved = [item for item in execution_precheck.get("items", []) if item.get("approved")]
    blocked = [item for item in execution_precheck.get("items", []) if not item.get("approved")]
    for item in approved[:3]:
        lines.append(
            f"{item['symbol']} {item.get('name') or item['symbol']} 可执行，建议数量 {item.get('proposed_quantity')} 股，金额 {item.get('proposed_value')}，预算 {item.get('budget_value')}。"
        )
    for item in blocked[:3]:
        blockers_raw = item.get("blockers", [])
        blockers = ",".join(_label_execution_reason(blocker) for blocker in blockers_raw) or "unknown"
        next_action_label = item.get("primary_recommended_next_action_label")
        line = f"{item['symbol']} {item.get('name') or item['symbol']} 暂不可执行，原因 {blockers}。"
        if next_action_label:
            line += f" 建议 {next_action_label}。"
        lines.append(line)
    if execution_precheck.get("primary_recommended_next_action_label"):
        lines.append(f"全局建议: {execution_precheck['primary_recommended_next_action_label']}。")
    return lines


def execution_dispatch_summary_lines(execution_dispatch: dict | None) -> list[str]:
    if not execution_dispatch:
        return []
    status = execution_dispatch.get("status") or "unknown"
    status_label = _label_execution_status(status)
    submitted_count = int(execution_dispatch.get("submitted_count", 0) or 0)
    preview_count = int(execution_dispatch.get("preview_count", 0) or 0)
    blocked_count = int(execution_dispatch.get("blocked_count", 0) or 0)

    lines = [
        f"执行派发状态 {status_label}，提交 {submitted_count}，预演 {preview_count}，阻断 {blocked_count}。"
    ]
    for line in execution_dispatch.get("summary_lines", []):
        if line not in lines:
            lines.append(line)
    for receipt in execution_dispatch.get("receipts", [])[:5]:
        symbol = receipt.get("symbol") or "-"
        name = receipt.get("name") or symbol
        receipt_status = receipt.get("status") or "unknown"
        receipt_status_label = _label_execution_status(receipt_status)
        reason = receipt.get("reason") or "unknown"
        reason_label = _label_execution_reason(reason)
        request = receipt.get("request") or {}
        quantity = request.get("quantity")
        price = request.get("price")
        order = receipt.get("order") or {}
        order_id = order.get("order_id")
        line = f"{symbol} {name} | {receipt_status_label}"
        if quantity is not None and price is not None:
            line += f" | 数量 {quantity} | 价格 {price}"
        line += f"，原因 {reason_label}"
        if reason_label != reason:
            line += f" ({reason})"
        if order_id:
            line += f"，order_id {order_id}"
        lines.append(line)
    return lines


def discussion_client_brief_template(
    trade_date: str,
    overview_lines: list[str],
    debate_focus_lines: list[str],
    challenge_exchange_lines: list[str],
    persuasion_summary_lines: list[str],
    selected_lines: list[str],
    watchlist_lines: list[str],
    rejected_lines: list[str],
    execution_precheck_lines: list[str] | None = None,
    execution_dispatch_lines: list[str] | None = None,
) -> str:
    lines = [f"🧭 **选股讨论简报 {trade_date}**"]
    lines.extend(f"- {line}" for line in overview_lines)
    if debate_focus_lines:
        lines.append("- 争议焦点:")
        lines.extend(f"  - {line}" for line in debate_focus_lines)
    if challenge_exchange_lines:
        lines.append("- 关键交锋:")
        lines.extend(f"  - {line}" for line in challenge_exchange_lines)
    if persuasion_summary_lines:
        lines.append("- 讨论收敛:")
        lines.extend(f"  - {line}" for line in persuasion_summary_lines)
    if selected_lines:
        lines.append("- 入选:")
        lines.extend(f"  - {line}" for line in selected_lines)
    if watchlist_lines:
        lines.append("- 观察:")
        lines.extend(f"  - {line}" for line in watchlist_lines)
    if rejected_lines:
        lines.append("- 淘汰:")
        lines.extend(f"  - {line}" for line in rejected_lines)
    if execution_precheck_lines:
        lines.append("- 执行预检:")
        lines.extend(f"  - {line}" for line in execution_precheck_lines)
    if execution_dispatch_lines:
        lines.append("- 执行回执:")
        lines.extend(f"  - {line}" for line in execution_dispatch_lines)
    return "\n".join(lines)


def discussion_reply_pack_template(
    trade_date: str,
    overview_lines: list[str],
    debate_focus_lines: list[str],
    challenge_exchange_lines: list[str],
    persuasion_summary_lines: list[str],
    selected_lines: list[str],
    watchlist_lines: list[str],
    rejected_lines: list[str],
    execution_precheck_lines: list[str] | None = None,
) -> str:
    return discussion_client_brief_template(
        trade_date,
        overview_lines,
        debate_focus_lines,
        challenge_exchange_lines,
        persuasion_summary_lines,
        selected_lines,
        watchlist_lines,
        rejected_lines,
        execution_precheck_lines=execution_precheck_lines,
        execution_dispatch_lines=None,
    )


def governance_adjustment_template(title: str, instruction: str, summary_lines: list[str]) -> str:
    lines = [f"🛠 **{title}**", f"- 指令: {instruction}"]
    lines.extend(f"- {line}" for line in summary_lines)
    return "\n".join(lines)


def feishu_briefing_template(title: str, summary_lines: list[str], data_refs: list[str] | None = None) -> str:
    lines = [f"📮 **{title}**"]
    lines.extend(f"- {line}" for line in summary_lines if line)
    refs = [str(item).strip() for item in (data_refs or []) if str(item).strip()]
    if refs:
        lines.append("- 数据入口:")
        lines.extend(f"  - {item}" for item in refs)
    return "\n".join(lines)


def feishu_answer_template(
    question: str,
    answer_lines: list[str],
    data_refs: list[str] | None = None,
    trade_advice: dict | None = None,
) -> str:
    lines = [f"💬 **飞书问答**", f"- 问题: {question}"]
    lines.extend(f"- {line}" for line in answer_lines if line)
    advice = dict(trade_advice or {})
    if advice:
        lines.append("- 交易台建议卡:")
        if advice.get("recommendation_level") or advice.get("stance"):
            lines.append(
                f"  - 建议级别: {advice.get('recommendation_level') or '-'} | 立场: {advice.get('stance') or '-'}"
            )
        if advice.get("summary"):
            lines.append(f"  - 结论: {advice.get('summary')}")
        trigger_conditions = [str(item).strip() for item in list(advice.get("trigger_conditions") or []) if str(item).strip()]
        if trigger_conditions:
            lines.append("  - 触发条件: " + "；".join(trigger_conditions[:3]))
        risk_notes = [str(item).strip() for item in list(advice.get("risk_notes") or []) if str(item).strip()]
        if risk_notes:
            lines.append("  - 风险提示: " + "；".join(risk_notes[:3]))
    refs = [str(item).strip() for item in (data_refs or []) if str(item).strip()]
    if refs:
        lines.append("- 相关入口:")
        lines.extend(f"  - {item}" for item in refs)
    return "\n".join(lines)


def agent_supervision_template(title: str, summary_lines: list[str], items: list[dict] | None = None) -> str:
    def _compact_task_prompt(text: str) -> str:
        normalized = " ".join(str(text or "").split())
        if len(normalized) <= 180:
            return normalized
        return normalized[:177] + "..."

    lines = [f"🧷 **{title}**"]
    seen_summary: set[str] = set()
    for line in summary_lines:
        text = str(line or "").strip()
        if not text or text in seen_summary:
            continue
        seen_summary.add(text)
        lines.append(f"- {text}")
    for item in (items or [])[:8]:
        agent_id = str(item.get("agent_id") or "-")
        status = str(item.get("status") or "-")
        phase_label = str(item.get("phase_label") or "").strip()
        task_mode = str(item.get("task_mode") or "").strip()
        last_active_at = str(item.get("last_active_at") or "").strip()
        reasons = item.get("reasons") or []
        reason_text = "；".join(str(reason).strip() for reason in reasons[:2] if str(reason).strip())
        suffix = f" | last_active={last_active_at}" if last_active_at else ""
        header = f"- {agent_id}: {status}"
        if phase_label:
            header += f" | 阶段={phase_label}"
        if task_mode:
            header += f" | 模式={task_mode}"
        quality_state = str(item.get("quality_state") or "").strip()
        if quality_state:
            header += f" | 质量={quality_state}"
        header += suffix
        if reason_text:
            header += f" | {reason_text}"
        lines.append(header)
        quality_reason = str(item.get("quality_reason") or "").strip()
        if quality_reason:
            lines.append(f"  - 推进质量: {quality_reason}")
        task_reason = str(item.get("task_reason") or "").strip()
        if task_reason:
            lines.append(f"  - 派工缘由: {task_reason}")
        supervision_tier = str(item.get("supervision_tier") or "").strip()
        supervision_tier_reason = str(item.get("supervision_tier_reason") or "").strip()
        if supervision_tier:
            tier_line = f"  - 催办等级: {supervision_tier}"
            if supervision_tier_reason:
                tier_line += f" | {supervision_tier_reason}"
            lines.append(tier_line)
        supervision_action_reason = str(item.get("supervision_action_reason") or "").strip()
        if supervision_action_reason:
            lines.append(f"  - 优先原因: {supervision_action_reason}")
        task_prompt = str(item.get("task_prompt") or "").strip()
        if task_prompt:
            lines.append(f"  - 当前任务: {_compact_task_prompt(task_prompt)}")
        expected_outputs = [str(output).strip() for output in list(item.get("expected_outputs") or []) if str(output).strip()]
        if expected_outputs:
            lines.append(f"  - 预期产物: {'；'.join(expected_outputs[:3])}")
        last_completed_at = str(item.get("last_completed_at") or "").strip()
        if last_completed_at:
            lines.append(f"  - 最近完成: {last_completed_at}")
    return "\n".join(lines)
