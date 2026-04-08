"""面向用户与通知的统一讨论摘要构建。"""

from __future__ import annotations

from ..notify.templates import execution_dispatch_summary_lines, execution_precheck_summary_lines


def build_client_brief_payload(
    trade_date: str,
    reply_pack: dict,
    final_brief: dict,
    execution_precheck: dict | None = None,
    execution_dispatch: dict | None = None,
    cycle: dict | None = None,
) -> dict:
    precheck_lines = execution_precheck_summary_lines(execution_precheck)
    dispatch_lines = execution_dispatch_summary_lines(execution_dispatch)
    execution_precheck_items = execution_precheck.get("items", []) if execution_precheck else []
    execution_dispatch_receipts = execution_dispatch.get("receipts", []) if execution_dispatch else []

    lines = list(reply_pack["overview_lines"])
    debate_focus_lines = list(reply_pack.get("debate_focus_lines", []))
    challenge_exchange_lines = list(reply_pack.get("challenge_exchange_lines", []))
    persuasion_summary_lines = list(reply_pack.get("persuasion_summary_lines", []))
    if debate_focus_lines:
        lines.append("争议焦点:")
        lines.extend(debate_focus_lines)
    if challenge_exchange_lines:
        lines.append("关键交锋:")
        lines.extend(challenge_exchange_lines)
    if persuasion_summary_lines:
        lines.append("讨论收敛:")
        lines.extend(persuasion_summary_lines)
    if final_brief["status"] == "ready":
        lines.append("最终推荐:")
        lines.extend(reply_pack["selected_lines"])
    elif reply_pack["watchlist_lines"]:
        lines.append("当前观察池:")
        lines.extend(reply_pack["watchlist_lines"])
    elif reply_pack["rejected_lines"]:
        lines.append("当前淘汰池:")
        lines.extend(reply_pack["rejected_lines"])
    if precheck_lines:
        lines.append("执行预检:")
        lines.extend(precheck_lines)
    if dispatch_lines:
        lines.append("执行回执:")
        lines.extend(dispatch_lines)

    payload = {
        "trade_date": trade_date,
        "status": final_brief["status"],
        "blockers": final_brief.get("blockers", []),
        "selected_count": final_brief.get("selected_count", reply_pack.get("selected_count", 0)),
        "watchlist_count": final_brief.get("watchlist_count", reply_pack.get("watchlist_count", 0)),
        "rejected_count": final_brief.get("rejected_count", reply_pack.get("rejected_count", 0)),
        "selected_display": reply_pack.get("selected_display", []),
        "watchlist_display": reply_pack.get("watchlist_display", []),
        "rejected_display": reply_pack.get("rejected_display", []),
        "overview_lines": reply_pack["overview_lines"],
        "debate_focus_lines": debate_focus_lines,
        "challenge_exchange_lines": challenge_exchange_lines,
        "persuasion_summary_lines": persuasion_summary_lines,
        "selected_lines": reply_pack["selected_lines"],
        "watchlist_lines": reply_pack["watchlist_lines"],
        "rejected_lines": reply_pack["rejected_lines"],
        "execution_precheck_lines": precheck_lines,
        "execution_dispatch_lines": dispatch_lines,
        "execution_precheck_status": (execution_precheck or {}).get("status"),
        "execution_precheck_available": bool(execution_precheck and execution_precheck.get("available", True)),
        "execution_precheck_approved_count": len([item for item in execution_precheck_items if item.get("approved")]),
        "execution_precheck_blocked_count": len([item for item in execution_precheck_items if not item.get("approved")]),
        "execution_dispatch_available": execution_dispatch is not None,
        "execution_dispatch_status": (execution_dispatch or {}).get("status"),
        "execution_dispatch_submitted_count": int((execution_dispatch or {}).get("submitted_count", 0) or 0),
        "execution_dispatch_preview_count": int((execution_dispatch or {}).get("preview_count", 0) or 0),
        "execution_dispatch_blocked_count": int((execution_dispatch or {}).get("blocked_count", 0) or 0),
        "execution_dispatch_receipt_count": len(execution_dispatch_receipts),
        "lines": lines,
        "summary_text": "\n".join(lines),
        "reply_pack": reply_pack,
        "final_brief": final_brief,
        "execution_precheck": execution_precheck,
        "execution_dispatch": execution_dispatch,
    }
    if cycle:
        payload["cycle"] = cycle
    return payload
