"""v0.9 讨论流程状态机。"""

from __future__ import annotations

from typing import Literal


PoolState = Literal[
    "day_open",
    "base_pool_ready",
    "focus_pool_building",
    "focus_pool_ready",
    "execution_pool_building",
    "execution_pool_ready",
    "execution_pool_blocked",
]

DiscussionState = Literal[
    "idle",
    "round_1_running",
    "round_1_summarized",
    "round_2_running",
    "final_review_ready",
    "final_selection_ready",
    "final_selection_blocked",
]


class DiscussionStateMachine:
    """最小可执行状态机。"""

    @staticmethod
    def needs_round_2(summary: dict | None) -> bool:
        round_coverage = (summary or {}).get("round_coverage", {})
        return int(round_coverage.get("needs_round_2", 0) or 0) > 0

    @staticmethod
    def can_finalize_from_summary(discussion_state: DiscussionState, summary: dict | None) -> bool:
        if discussion_state in {"final_selection_ready", "final_selection_blocked", "final_review_ready"}:
            return True
        if discussion_state != "round_1_summarized":
            return False
        return not DiscussionStateMachine.needs_round_2(summary)

    @staticmethod
    def bootstrap() -> tuple[PoolState, DiscussionState]:
        return "base_pool_ready", "idle"

    @staticmethod
    def start_round_1() -> tuple[PoolState, DiscussionState]:
        return "focus_pool_building", "round_1_running"

    @staticmethod
    def complete_round_1() -> tuple[PoolState, DiscussionState]:
        return "focus_pool_ready", "round_1_summarized"

    @staticmethod
    def start_round_2() -> tuple[PoolState, DiscussionState]:
        return "execution_pool_building", "round_2_running"

    @staticmethod
    def complete_round_2() -> tuple[PoolState, DiscussionState]:
        return "execution_pool_building", "final_review_ready"

    @staticmethod
    def finalize(blocked: bool) -> tuple[PoolState, DiscussionState]:
        if blocked:
            return "execution_pool_blocked", "final_selection_blocked"
        return "execution_pool_ready", "final_selection_ready"
