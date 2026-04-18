"""v1.0 讨论流程状态机 — 支持动态轮次。

相对 v0.9 的变化：
- DiscussionState 从 round_1/round_2 硬编码改为 round_running/round_summarized 泛化
- 新增 can_continue_discussion() 支持 Round 3+
- 保留 start_round_1/complete_round_1 等兼容方法
"""

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
    "round_running",          # 泛化：Round N (N >= 3) 进行中
    "round_summarized",       # 泛化：Round N (N >= 3) 汇总完成
    "final_review_ready",
    "final_selection_ready",
    "final_selection_blocked",
]

# 默认最大讨论轮次
DEFAULT_MAX_ROUNDS = 3


class DiscussionStateMachine:
    """支持动态轮次的讨论状态机。"""

    @staticmethod
    def needs_round_2(summary: dict | None) -> bool:
        round_coverage = (summary or {}).get("round_coverage", {})
        return int(round_coverage.get("needs_round_2", 0) or 0) > 0

    @staticmethod
    def can_continue_discussion(
        summary: dict | None,
        current_round: int,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
    ) -> bool:
        """判断是否应该继续讨论（进入下一轮）。

        条件：
        - 当前轮次 < max_rounds
        - summary 中存在 remaining_disputes > 0 或存在 all_inquiry_targets

        Args:
            summary: 当前轮次的汇总结果
            current_round: 当前轮次号（1, 2, 3, ...）
            max_rounds: 最大允许轮次

        Returns:
            True 表示应进入下一轮，False 表示应该 finalize
        """
        if current_round >= max_rounds:
            return False
        if summary is None:
            return False
        # 检查是否还有未解决的争议
        remaining_disputes = summary.get("remaining_disputes", [])
        if isinstance(remaining_disputes, list) and len(remaining_disputes) > 0:
            return True
        if isinstance(remaining_disputes, int) and remaining_disputes > 0:
            return True
        # S1.2: 检查是否还有未回应的质询目标
        all_inquiry_targets = summary.get("all_inquiry_targets", [])
        if all_inquiry_targets:
            return True
        # 兼容 round_coverage 格式
        round_coverage = summary.get("round_coverage", {})
        if current_round == 1:
            return int(round_coverage.get("needs_round_2", 0) or 0) > 0
        return False

    @staticmethod
    def can_finalize_from_summary(discussion_state: DiscussionState, summary: dict | None) -> bool:
        if discussion_state in {"final_selection_ready", "final_selection_blocked", "final_review_ready"}:
            return True
        if discussion_state == "round_1_summarized":
            return not DiscussionStateMachine.needs_round_2(summary)
        if discussion_state == "round_summarized":
            return True  # 泛化状态下已经过了 can_continue 检查
        return False

    # ── 固定轮次兼容方法（保留 v0.9 接口） ──

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

    # ── 泛化轮次方法 ──

    @staticmethod
    def start_round(n: int) -> tuple[PoolState, DiscussionState]:
        """启动第 N 轮讨论。

        Round 1/2 使用固定状态名（向后兼容），Round 3+ 使用泛化状态名。
        """
        if n == 1:
            return DiscussionStateMachine.start_round_1()
        if n == 2:
            return DiscussionStateMachine.start_round_2()
        return "execution_pool_building", "round_running"

    @staticmethod
    def complete_round(n: int) -> tuple[PoolState, DiscussionState]:
        """完成第 N 轮讨论。"""
        if n == 1:
            return DiscussionStateMachine.complete_round_1()
        if n == 2:
            return DiscussionStateMachine.complete_round_2()
        return "execution_pool_building", "round_summarized"

    @staticmethod
    def finalize(blocked: bool) -> tuple[PoolState, DiscussionState]:
        if blocked:
            return "execution_pool_blocked", "final_selection_blocked"
        return "execution_pool_ready", "final_selection_ready"
