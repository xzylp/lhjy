"""discussion 最终收敛 helper。"""

from __future__ import annotations

from typing import Iterable

from .client_brief import build_client_brief_payload
from .contracts import DiscussionCaseRecord, DiscussionCycleSnapshot, DiscussionFinalizeBundle
from .protocol import build_finalize_packet_envelope
from .round_summarizer import build_reason_board


def build_reply_pack(
    *,
    trade_date: str,
    reason_board: dict,
    selected_limit: int = 3,
    watchlist_limit: int = 5,
    rejected_limit: int = 5,
) -> dict:
    selected = reason_board["selected"][:selected_limit]
    watchlist = reason_board["watchlist"][:watchlist_limit]
    rejected = reason_board["rejected"][:rejected_limit]
    overview_lines = [
        f"交易日 {trade_date}，候选 {reason_board['case_count']} 只，入选 {reason_board['selected_count']} 只，观察 {reason_board['watchlist_count']} 只，淘汰 {reason_board['rejected_count']} 只。"
    ]
    if selected:
        overview_lines.append(f"当前优先执行池关注 {', '.join(item['name'] or item['symbol'] for item in selected)}。")
    elif watchlist:
        overview_lines.append("当前没有满足执行条件的标的，重点观察池仍需继续收敛。")
    else:
        overview_lines.append("当前没有可执行标的，也没有足够的观察池结论。")
    debate_focus_lines = _build_debate_focus_lines(reason_board)
    persuasion_summary_lines = _build_persuasion_summary_lines(reason_board)
    return {
        "trade_date": trade_date,
        "case_count": reason_board["case_count"],
        "selected_count": reason_board["selected_count"],
        "selection_count": reason_board["selected_count"],
        "watchlist_count": reason_board["watchlist_count"],
        "rejected_count": reason_board["rejected_count"],
        "overview_lines": overview_lines,
        "debate_focus_lines": debate_focus_lines,
        "challenge_exchange_lines": _build_challenge_exchange_lines(reason_board),
        "persuasion_summary_lines": persuasion_summary_lines,
        "selected_lines": [_reply_line(item) for item in selected],
        "selection_lines": [_reply_line(item) for item in selected],
        "watchlist_lines": [_reply_line(item) for item in watchlist],
        "rejected_lines": [_reply_line(item) for item in rejected],
        "selected_display": [_display_item(item) for item in selected],
        "selection_display": [_display_item(item) for item in selected],
        "watchlist_display": [_display_item(item) for item in watchlist],
        "rejected_display": [_display_item(item) for item in rejected],
        "selected": selected,
        "selection": selected,
        "watchlist": watchlist,
        "rejected": rejected,
    }


def build_final_brief(*, trade_date: str, reply_pack: dict) -> dict:
    selected = reply_pack["selected"]
    watchlist = reply_pack["watchlist"]
    rejected = reply_pack["rejected"]
    status = "ready" if selected else "blocked"
    blockers: list[str] = []
    if not selected:
        blockers.append("no_selected_candidates")
    lines = list(reply_pack["overview_lines"])
    if reply_pack.get("debate_focus_lines"):
        lines.append("争议焦点:")
        lines.extend(reply_pack["debate_focus_lines"])
    if reply_pack.get("persuasion_summary_lines"):
        lines.append("讨论收敛:")
        lines.extend(reply_pack["persuasion_summary_lines"])
    if selected:
        lines.append("最终推荐:")
        lines.extend(reply_pack["selected_lines"])
    elif watchlist:
        lines.append("当前观察池:")
        lines.extend(reply_pack["watchlist_lines"])
    else:
        lines.append("当前淘汰池:")
        lines.extend(reply_pack["rejected_lines"])
    return {
        "trade_date": trade_date,
        "status": status,
        "blockers": blockers,
        "selected_count": reply_pack["selected_count"],
        "selection_count": reply_pack["selected_count"],
        "watchlist_count": reply_pack["watchlist_count"],
        "rejected_count": reply_pack["rejected_count"],
        "selected": selected,
        "selection": selected,
        "watchlist": watchlist,
        "rejected": rejected,
        "selected_lines": reply_pack["selected_lines"],
        "selection_lines": reply_pack["selected_lines"],
        "selected_display": [_display_item(item) for item in selected],
        "selection_display": [_display_item(item) for item in selected],
        "watchlist_display": [_display_item(item) for item in watchlist],
        "rejected_display": [_display_item(item) for item in rejected],
        "debate_focus_lines": reply_pack.get("debate_focus_lines", []),
        "persuasion_summary_lines": reply_pack.get("persuasion_summary_lines", []),
        "lines": lines,
        "summary_text": "\n".join(lines),
    }


def build_finalize_bundle(
    *,
    trade_date: str,
    cases: Iterable[DiscussionCaseRecord | dict],
    cycle: DiscussionCycleSnapshot | dict | None = None,
    execution_precheck: dict | None = None,
    execution_dispatch: dict | None = None,
    selected_limit: int = 3,
    watchlist_limit: int = 5,
    rejected_limit: int = 5,
    include_client_brief: bool = True,
) -> DiscussionFinalizeBundle:
    cycle_payload = cycle.model_dump() if isinstance(cycle, DiscussionCycleSnapshot) else dict(cycle or {})
    reason_board = build_reason_board(cases, trade_date)
    reply_pack = build_reply_pack(
        trade_date=trade_date,
        reason_board=reason_board,
        selected_limit=selected_limit,
        watchlist_limit=watchlist_limit,
        rejected_limit=rejected_limit,
    )
    final_brief = build_final_brief(trade_date=trade_date, reply_pack=reply_pack)
    if not cycle_payload.get("execution_pool_case_ids"):
        cycle_payload["execution_pool_case_ids"] = [item["case_id"] for item in reply_pack.get("selected", [])]
    if "blockers" not in cycle_payload:
        cycle_payload["blockers"] = list(final_brief.get("blockers", []))
    client_brief = (
        build_client_brief_payload(
            trade_date=trade_date,
            reply_pack=reply_pack,
            final_brief=final_brief,
            execution_precheck=execution_precheck,
            execution_dispatch=execution_dispatch,
            cycle=cycle_payload or None,
        )
        if include_client_brief
        else None
    )
    finalize_packet = build_finalize_packet_envelope(
        trade_date=trade_date,
        cycle=cycle_payload,
        execution_precheck=execution_precheck or {},
        execution_intents=_build_execution_intents(reply_pack, execution_dispatch),
        client_brief=client_brief or {
            "trade_date": trade_date,
            "status": final_brief["status"],
            "blockers": final_brief["blockers"],
            "lines": final_brief["lines"],
        },
        final_brief=final_brief,
        reply_pack=reply_pack,
        shared_context={
            "reason_board_counts": {
                "selected": reason_board["selected_count"],
                "watchlist": reason_board["watchlist_count"],
                "rejected": reason_board["rejected_count"],
            }
        },
    )
    return DiscussionFinalizeBundle(
        trade_date=trade_date,
        reason_board=reason_board,
        reply_pack=reply_pack,
        final_brief=final_brief,
        client_brief=client_brief,
        finalize_packet=finalize_packet,
    )


def _build_execution_intents(reply_pack: dict, execution_dispatch: dict | None) -> dict:
    selected = reply_pack.get("selected", [])
    submitted_count = int((execution_dispatch or {}).get("submitted_count", 0) or 0)
    preview_count = int((execution_dispatch or {}).get("preview_count", 0) or 0)
    blocked_count = int((execution_dispatch or {}).get("blocked_count", 0) or 0)
    return {
        "intent_count": len(selected),
        "selected_symbols": [item["symbol"] for item in selected],
        "submitted_count": submitted_count,
        "preview_count": preview_count,
        "blocked_count": blocked_count,
    }


def _reply_line(item: dict) -> str:
    gate = f"风控={item['risk_gate']} 审计={item['audit_gate']}"
    score = f"分数={item['selection_score']}"
    reason = item.get("headline_reason") or item.get("selected_reason") or item.get("rejected_reason") or ""
    return f"{item['symbol']} {item['name'] or item['symbol']} | 排名={item['rank']} | {score} | {gate} | 理由={reason}"


def _display_item(item: dict) -> str:
    return f"{item['symbol']} {item.get('name') or item['symbol']}"


def _build_debate_focus_lines(board: dict) -> list[str]:
    lines: list[str] = []
    for group in ("selected", "watchlist", "rejected"):
        for item in board.get(group, []):
            discussion = item.get("discussion") or {}
            disputes = discussion.get("remaining_disputes") or discussion.get("questions_for_round_2") or []
            if not disputes:
                continue
            lines.append(f"{item['symbol']} {item.get('name') or item['symbol']}：{disputes[0]}")
    return lines[:8]


def _build_challenge_exchange_lines(board: dict) -> list[str]:
    lines: list[str] = []
    for group in ("selected", "watchlist", "rejected"):
        for item in board.get(group, []):
            discussion = item.get("discussion") or {}
            for entry in discussion.get("challenge_exchange_lines") or []:
                lines.append(f"{item['symbol']} {item.get('name') or item['symbol']}：{entry}")
    return lines[:8]


def _build_persuasion_summary_lines(board: dict) -> list[str]:
    lines: list[str] = []
    for group in ("selected", "watchlist", "rejected"):
        for item in board.get(group, []):
            discussion = item.get("discussion") or {}
            persuasion = discussion.get("persuasion_summary") or discussion.get("revision_notes") or []
            for entry in persuasion:
                lines.append(f"{item['symbol']} {item.get('name') or item['symbol']}：{entry}")
    return lines[:8]
