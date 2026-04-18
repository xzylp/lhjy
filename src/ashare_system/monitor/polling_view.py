"""监控轮询状态展示层辅助。"""

from __future__ import annotations

from copy import deepcopy


def _is_discussion_round_active(discussion_state: str) -> bool:
    normalized = str(discussion_state or "").strip().lower()
    return normalized.startswith("round_") and normalized.endswith("_running")


def decorate_polling_status_for_display(
    polling_status: dict[str, dict] | None,
    *,
    cycle: dict | None = None,
) -> dict[str, dict]:
    """在不改变底层轮询计时语义的前提下，补充更贴近业务态的展示口径。"""

    result = deepcopy(polling_status or {})
    cycle_payload = dict(cycle or {})
    discussion_state = str(cycle_payload.get("discussion_state") or "")
    execution_pool_case_ids = list(cycle_payload.get("execution_pool_case_ids") or [])
    blockers = [str(item) for item in list(cycle_payload.get("blockers") or []) if str(item)]

    for layer, item in result.items():
        raw_due_now = bool(item.get("due_now"))
        last_trigger = str(item.get("last_trigger") or "")
        suppressed_due_reason = ""

        item["raw_due_now"] = raw_due_now
        item["suppressed_due_reason"] = ""

        if layer == "focus" and raw_due_now and _is_discussion_round_active(discussion_state):
            item["due_now"] = False
            suppressed_due_reason = f"discussion_active:{discussion_state}"
        elif (
            layer == "execution"
            and raw_due_now
            and execution_pool_case_ids
            and not blockers
            and last_trigger == "execution_intents_read"
        ):
            item["due_now"] = False
            suppressed_due_reason = "execution_intents_ready"

        if suppressed_due_reason:
            item["suppressed_due_reason"] = suppressed_due_reason
            item["display_state"] = "active"
        else:
            item["display_state"] = "due" if bool(item.get("due_now")) else "cooldown"

    return result
