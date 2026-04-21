"""学分结算与权重映射。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..discussion.candidate_case import CandidateCase


def determine_weight_bucket(score: float) -> str:
    if score <= 0:
        return "suspended"
    if score < 6:
        return "weak_credit"
    if score < 10:
        return "low_credit"
    if score < 15:
        return "normal"
    return "high_credit"


def determine_weight_value(score: float) -> float:
    if score <= 0:
        return 0.0
    if score < 6:
        return 0.6
    if score < 10:
        return 0.85
    if score < 15:
        return 1.0
    return 1.15


def determine_governance_state(score: float) -> str:
    if score <= 0:
        return "suspended"
    if score < 6:
        return "learning_mode"
    if score < 10:
        return "recovered_low_credit"
    return "normal_mode"


@dataclass
class SettlementSymbolOutcome:
    symbol: str
    next_day_close_pct: float
    note: str = ""


@dataclass
class AgentSettlementResult:
    agent_id: str
    result_score_delta: float
    governance_score_delta: float = 0.0
    sample_count: int = 0
    confidence_tier: str = "low"
    insufficient_sample: bool = False
    cases_evaluated: list[dict] = field(default_factory=list)


class AgentScoreSettlementService:
    """根据次日收盘结果结算 agent 学分。"""

    _DISCUSSION_AGENT_IDS = {"ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"}

    def settle(
        self,
        cases: list[CandidateCase],
        outcomes: dict[str, SettlementSymbolOutcome],
        *,
        min_sample_count: int = 5,
    ) -> list[AgentSettlementResult]:
        sample_count = len(cases)
        confidence_tier = self._resolve_confidence_tier(sample_count)
        if sample_count < min_sample_count:
            return [
                AgentSettlementResult(
                    agent_id=agent_id,
                    result_score_delta=0.0,
                    sample_count=sample_count,
                    confidence_tier=confidence_tier,
                    insufficient_sample=True,
                    cases_evaluated=[],
                )
                for agent_id in sorted(self._DISCUSSION_AGENT_IDS)
            ]
        agent_results: dict[str, AgentSettlementResult] = {}
        for case in cases:
            outcome = outcomes.get(case.symbol)
            if outcome is None:
                continue
            case_context = self._build_case_context(case)
            for opinion in case.opinions:
                if opinion.agent_id not in {"ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"}:
                    continue
                delta = self._score_opinion(
                    opinion.stance,
                    opinion.confidence,
                    outcome.next_day_close_pct,
                    final_status=case_context["final_status"],
                    risk_gate=case_context["risk_gate"],
                    audit_gate=case_context["audit_gate"],
                )
                result = agent_results.setdefault(
                    opinion.agent_id,
                    AgentSettlementResult(
                        agent_id=opinion.agent_id,
                        result_score_delta=0.0,
                        sample_count=sample_count,
                        confidence_tier=confidence_tier,
                        cases_evaluated=[],
                    ),
                )
                result.result_score_delta += delta
                result.cases_evaluated.append(
                    {
                        "symbol": case.symbol,
                        "stance": opinion.stance,
                        "next_day_close_outcome": self._label_outcome(outcome.next_day_close_pct),
                        "delta": round(delta, 2),
                        "final_status": case_context["final_status"],
                        "risk_gate": case_context["risk_gate"],
                        "audit_gate": case_context["audit_gate"],
                    }
                )
        for item in agent_results.values():
            if item.confidence_tier == "low":
                item.result_score_delta *= 0.3
            item.result_score_delta = round(item.result_score_delta, 2)
        return sorted(agent_results.values(), key=lambda item: item.agent_id)

    def settle_discussion(self, cases: list[CandidateCase], *, min_sample_count: int = 5) -> list[AgentSettlementResult]:
        sample_count = len(cases)
        confidence_tier = self._resolve_confidence_tier(sample_count)
        if sample_count < min_sample_count:
            return [
                AgentSettlementResult(
                    agent_id=agent_id,
                    result_score_delta=0.0,
                    governance_score_delta=0.0,
                    sample_count=sample_count,
                    confidence_tier=confidence_tier,
                    insufficient_sample=True,
                    cases_evaluated=[],
                )
                for agent_id in sorted(self._DISCUSSION_AGENT_IDS)
            ]
        agent_results: dict[str, AgentSettlementResult] = {}
        for case in cases:
            case_context = self._build_case_context(case)
            for opinion in case.opinions:
                if opinion.agent_id not in self._DISCUSSION_AGENT_IDS:
                    continue
                result = agent_results.setdefault(
                    opinion.agent_id,
                    AgentSettlementResult(
                        agent_id=opinion.agent_id,
                        result_score_delta=0.0,
                        governance_score_delta=0.0,
                        sample_count=sample_count,
                        confidence_tier=confidence_tier,
                        cases_evaluated=[],
                    ),
                )
                delta = self._score_discussion_opinion(
                    opinion.stance,
                    opinion.confidence,
                    final_status=case_context["final_status"],
                    risk_gate=case_context["risk_gate"],
                    audit_gate=case_context["audit_gate"],
                )
                result.governance_score_delta += delta
                result.cases_evaluated.append(
                    {
                        "symbol": case.symbol,
                        "stance": opinion.stance,
                        "next_day_close_outcome": "discussion_settlement",
                        "delta": round(delta, 2),
                        "final_status": case_context["final_status"],
                        "risk_gate": case_context["risk_gate"],
                        "audit_gate": case_context["audit_gate"],
                    }
                )
        for item in agent_results.values():
            if item.confidence_tier == "low":
                item.governance_score_delta *= 0.3
            item.governance_score_delta = round(item.governance_score_delta, 2)
        return sorted(agent_results.values(), key=lambda item: item.agent_id)

    @staticmethod
    def _resolve_confidence_tier(sample_count: int) -> str:
        if sample_count >= 30:
            return "high"
        if sample_count >= 15:
            return "medium"
        return "low"

    def _score_opinion(
        self,
        stance: str,
        confidence: str,
        next_day_close_pct: float,
        *,
        final_status: str,
        risk_gate: str,
        audit_gate: str,
    ) -> float:
        multiplier = {"high": 1.0, "medium": 0.6, "low": 0.3}.get(confidence, 0.6)
        positive = next_day_close_pct >= 0.02
        negative = next_day_close_pct <= -0.02
        flat = not positive and not negative

        normalized_stance = {
            "selected": "selected",
            "support": "selected",
            "watchlist": "watchlist",
            "watch": "watchlist",
            "hold": "hold",
            "limit": "limit",
            "question": "question",
            "rejected": "rejected",
            "reject": "rejected",
            "oppose": "rejected",
        }.get(stance, stance)

        if normalized_stance == "selected":
            base = 0.6 if positive else (-0.6 if negative else 0.0)
        elif normalized_stance == "rejected":
            base = 0.6 if negative else (-0.6 if positive else 0.0)
        elif normalized_stance in {"watchlist", "hold", "limit", "question"}:
            base = 0.2 if flat else 0.1
        else:
            base = 0.0
        alignment_bonus = self._discussion_alignment_bonus(normalized_stance, final_status, risk_gate, audit_gate)
        governance_multiplier = self._governance_multiplier(final_status, risk_gate, audit_gate)
        return round((base + alignment_bonus) * multiplier * governance_multiplier, 2)

    def _score_discussion_opinion(
        self,
        stance: str,
        confidence: str,
        *,
        final_status: str,
        risk_gate: str,
        audit_gate: str,
    ) -> float:
        multiplier = {"high": 1.0, "medium": 0.6, "low": 0.3}.get(confidence, 0.6)
        normalized_stance = {
            "selected": "selected",
            "support": "selected",
            "watchlist": "watchlist",
            "watch": "watchlist",
            "hold": "hold",
            "limit": "limit",
            "question": "question",
            "rejected": "rejected",
            "reject": "rejected",
            "oppose": "rejected",
        }.get(stance, stance)

        if final_status == "selected" and risk_gate == "allow" and audit_gate == "clear":
            if normalized_stance == "selected":
                base = 0.35
            elif normalized_stance == "rejected":
                base = -0.28
            else:
                base = 0.08
        elif final_status == "rejected" or risk_gate == "reject":
            if normalized_stance == "rejected":
                base = 0.34
            elif normalized_stance == "selected":
                base = -0.3
            else:
                base = 0.06
        else:
            if normalized_stance in {"watchlist", "hold", "limit", "question"}:
                base = 0.18
            elif normalized_stance == "selected":
                base = -0.08
            else:
                base = 0.04
        alignment_bonus = self._discussion_alignment_bonus(normalized_stance, final_status, risk_gate, audit_gate) * 0.6
        governance_multiplier = self._governance_multiplier(final_status, risk_gate, audit_gate)
        return round((base + alignment_bonus) * multiplier * governance_multiplier, 2)

    @staticmethod
    def _build_case_context(case: CandidateCase) -> dict[str, str]:
        return {
            "final_status": str(case.final_status or "watchlist"),
            "risk_gate": str(case.risk_gate or "pending"),
            "audit_gate": str(case.audit_gate or "pending"),
        }

    @staticmethod
    def _governance_multiplier(final_status: str, risk_gate: str, audit_gate: str) -> float:
        if final_status == "selected" and risk_gate == "allow" and audit_gate == "clear":
            return 1.0
        if final_status == "watchlist":
            return 0.85
        if risk_gate == "reject" or audit_gate == "hold" or final_status == "rejected":
            return 0.7
        return 0.8

    @staticmethod
    def _discussion_alignment_bonus(normalized_stance: str, final_status: str, risk_gate: str, audit_gate: str) -> float:
        if final_status == "selected":
            if normalized_stance == "selected":
                return 0.2 if risk_gate == "allow" and audit_gate == "clear" else 0.1
            if normalized_stance == "rejected":
                return -0.15
            return 0.05
        if final_status == "rejected":
            if normalized_stance == "rejected":
                return 0.18
            if normalized_stance == "selected":
                return -0.18
            return 0.02
        if normalized_stance in {"watchlist", "hold", "limit", "question"}:
            return 0.1
        return -0.04

    @staticmethod
    def _label_outcome(next_day_close_pct: float) -> str:
        if next_day_close_pct >= 0.02:
            return "positive"
        if next_day_close_pct <= -0.02:
            return "negative"
        return "flat"
