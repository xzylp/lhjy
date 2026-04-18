"""基于 attribution 的最小自动治理。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..contracts import PlaybookOverride, PlaybookOverrideSnapshot
from .attribution import TradeAttributionReport


class AutoGovernance:
    """把 by_playbook 归因结果收口成次日可消费的 override 快照。"""

    def build_override_snapshot(
        self,
        *,
        report: TradeAttributionReport | dict[str, Any],
        previous_snapshot: PlaybookOverrideSnapshot | dict[str, Any] | None = None,
    ) -> PlaybookOverrideSnapshot:
        report_payload = self._payload(report)
        previous_payload = self._payload(previous_snapshot)
        trade_date = str(report_payload.get("trade_date") or report_payload.get("score_date") or "")
        generated_at = str(report_payload.get("generated_at") or datetime.now().isoformat())
        previous_streaks = {
            str(key): int(value or 0)
            for key, value in dict(previous_payload.get("streaks") or {}).items()
            if str(key).strip()
        }

        overrides: list[PlaybookOverride] = []
        next_streaks: dict[str, int] = {}
        for item in list(report_payload.get("by_playbook") or []):
            payload = self._payload(item)
            playbook = str(payload.get("key") or "").strip()
            if not playbook or playbook == "unassigned":
                continue
            signal = self._classify_signal(payload)
            previous_streak = previous_streaks.get(playbook, 0)
            streak = self._next_streak(previous_streak, signal)
            next_streaks[playbook] = streak
            status = self._resolve_status(streak)
            if status is None:
                continue
            overrides.append(
                PlaybookOverride(
                    playbook=playbook,
                    status=status,
                    reason=self._build_reason(playbook=playbook, payload=payload, signal=signal, streak=streak),
                    source="trade_attribution.by_playbook",
                    trade_date=trade_date,
                    expires_on=self._default_expiry(trade_date),
                    streak=streak,
                )
            )

        summary_lines = self._build_summary_lines(overrides, next_streaks)
        return PlaybookOverrideSnapshot(
            trade_date=trade_date,
            generated_at=generated_at,
            source="auto_governance",
            overrides=overrides,
            streaks=next_streaks,
            summary_lines=summary_lines,
        )

    @staticmethod
    def _payload(item: TradeAttributionReport | PlaybookOverrideSnapshot | dict[str, Any] | None) -> dict[str, Any]:
        if item is None:
            return {}
        if isinstance(item, dict):
            return dict(item)
        if hasattr(item, "model_dump"):
            dumped = item.model_dump()
            return dict(dumped) if isinstance(dumped, dict) else {}
        return {}

    @staticmethod
    def _classify_signal(payload: dict[str, Any]) -> str:
        trade_count = int(payload.get("trade_count", 0) or 0)
        avg_return = float(payload.get("avg_next_day_close_pct", 0.0) or 0.0)
        win_rate = float(payload.get("win_rate", 0.0) or 0.0)
        if trade_count >= 2 and (avg_return <= -0.01 or win_rate <= 0.34):
            return "weak"
        if trade_count >= 2 and avg_return >= 0.015 and win_rate >= 0.60:
            return "strong"
        return "neutral"

    @staticmethod
    def _next_streak(previous_streak: int, signal: str) -> int:
        if signal == "strong":
            return previous_streak + 1 if previous_streak > 0 else 1
        if signal == "weak":
            return previous_streak - 1 if previous_streak < 0 else -1
        return 0

    @staticmethod
    def _resolve_status(streak: int) -> str | None:
        if streak <= -2:
            return "suspend"
        if streak >= 2:
            return "boost"
        return None

    @staticmethod
    def _build_reason(*, playbook: str, payload: dict[str, Any], signal: str, streak: int) -> str:
        trade_count = int(payload.get("trade_count", 0) or 0)
        avg_return = float(payload.get("avg_next_day_close_pct", 0.0) or 0.0)
        win_rate = float(payload.get("win_rate", 0.0) or 0.0)
        if signal == "weak":
            return (
                f"attribution 触发弱势治理：{playbook} 连续 {abs(streak)} 次偏弱，"
                f"样本 {trade_count}，平均收益 {avg_return:.2%}，胜率 {win_rate:.1%}。"
            )
        return (
            f"attribution 触发强势治理：{playbook} 连续 {streak} 次偏强，"
            f"样本 {trade_count}，平均收益 {avg_return:.2%}，胜率 {win_rate:.1%}。"
        )

    @staticmethod
    def _default_expiry(trade_date: str) -> str:
        try:
            return (datetime.fromisoformat(trade_date) + timedelta(days=1)).date().isoformat()
        except ValueError:
            return ""

    @staticmethod
    def _build_summary_lines(overrides: list[PlaybookOverride], streaks: dict[str, int]) -> list[str]:
        if overrides:
            return [
                f"{item.playbook} -> {item.status}，streak={item.streak}，expires_on={item.expires_on}。"
                for item in overrides
            ]
        if streaks:
            return [
                "本次 attribution 已更新 playbook streak，但尚未达到 suspend/boost 触发阈值。"
            ]
        return ["当前 attribution 尚未形成可执行的 playbook override。"]

    # ── 自进化扩展方法 ──────────────────────────────────────

    def build_agent_lesson_patches(
        self,
        *,
        report: TradeAttributionReport | dict[str, Any],
        score_states: list[dict[str, Any]] | None = None,
    ) -> dict[str, list[str]]:
        """根据归因报告 + 学分状态，为每个 Agent 生成需要注入 prompt 的教训文本。

        例如：ashare-risk 连续 3 天对 momentum playbook 给出 allow，
        但该 playbook 连续亏损 → 生成：
        "近三日动量战法连亏，你此前放行了这些票，下次遇到类似情况应优先 limit 而非 allow。"

        Args:
            report: 归因报告（TradeAttributionReport 或 dict）
            score_states: AgentScoreState.model_dump() 列表

        Returns:
            {agent_id: [lesson_text, ...]}

        TODO:
            1. 实现基于 by_playbook 的失败模式检测
            2. 实现基于 by_exit_reason 的退出模式教训
            3. 关联 score_states 中 governance_state == 'learning_mode' 的 agent
            4. 对 learning_mode 的 agent 生成更严格的教训
        """
        report_payload = self._payload(report)
        score_states = score_states or []

        # 构建 agent → governance_state 映射
        agent_governance: dict[str, str] = {}
        for state in score_states:
            agent_id = str(state.get("agent_id", ""))
            gov_state = str(state.get("governance_state", "normal_mode"))
            if agent_id:
                agent_governance[agent_id] = gov_state

        lessons: dict[str, list[str]] = {
            "ashare-research": [],
            "ashare-strategy": [],
            "ashare-risk": [],
            "ashare-audit": [],
        }

        # 分析 by_playbook 中的弱势战法
        for item in list(report_payload.get("by_playbook") or []):
            payload = self._payload(item)
            playbook = str(payload.get("key", ""))
            avg_return = float(payload.get("avg_next_day_close_pct", 0.0) or 0.0)
            win_rate = float(payload.get("win_rate", 0.0) or 0.0)
            trade_count = int(payload.get("trade_count", 0) or 0)

            if trade_count >= 2 and (avg_return <= -0.01 or win_rate <= 0.34):
                # 弱势战法 → 所有 agent 都应该注意
                lesson_text = (
                    f"近期 {playbook} 战法表现偏弱"
                    f"（{trade_count}笔，胜率{win_rate:.0%}，均收{avg_return:.2%}），"
                    f"遇到类似模式应提高警惕。"
                )
                # risk 和 audit 应该更积极拦截
                lessons["ashare-risk"].append(
                    f"{lesson_text}下次遇到{playbook}应优先考虑 limit 或 reject。"
                )
                lessons["ashare-audit"].append(
                    f"{lesson_text}复核时重点关注{playbook}类提案的数据支撑力度。"
                )

        # 分析 by_exit_reason 中的高频退出原因
        for item in list(report_payload.get("by_exit_reason") or []):
            payload = self._payload(item)
            exit_reason = str(payload.get("key", ""))
            exit_count = int(payload.get("trade_count", 0) or 0)

            if exit_count >= 3 and exit_reason in {"entry_failure", "board_break"}:
                lessons["ashare-strategy"].append(
                    f"近期 {exit_reason} 退出频繁（{exit_count}笔），"
                    f"选股时应加强入场条件筛选，避免弱势票入池。"
                )
                lessons["ashare-research"].append(
                    f"近期 {exit_reason} 频繁出现（{exit_count}笔），"
                    f"研究面应补充更严格的板块强度验证。"
                )

        # 对 learning_mode 的 agent 追加额外警示
        for agent_id, gov_state in agent_governance.items():
            if gov_state == "learning_mode" and agent_id in lessons:
                lessons[agent_id].append(
                    "当前处于学分低位（learning_mode），你的投票权重已被降低。"
                    "请更加审慎地给出判断，用高质量回应逐步恢复信用。"
                )

        # 过滤空列表
        return {k: v for k, v in lessons.items() if v}

