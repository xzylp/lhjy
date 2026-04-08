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
    cases_evaluated: list[dict] = field(default_factory=list)


class AgentScoreSettlementService:
    """根据次日收盘结果结算 agent 学分。"""

    def settle(self, cases: list[CandidateCase], outcomes: dict[str, SettlementSymbolOutcome]) -> list[AgentSettlementResult]:
        agent_results: dict[str, AgentSettlementResult] = {}
        for case in cases:
            outcome = outcomes.get(case.symbol)
            if outcome is None:
                continue
            for opinion in case.opinions:
                if opinion.agent_id not in {"ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"}:
                    continue
                delta = self._score_opinion(opinion.stance, opinion.confidence, outcome.next_day_close_pct)
                result = agent_results.setdefault(
                    opinion.agent_id,
                    AgentSettlementResult(agent_id=opinion.agent_id, result_score_delta=0.0, cases_evaluated=[]),
                )
                result.result_score_delta += delta
                result.cases_evaluated.append(
                    {
                        "symbol": case.symbol,
                        "stance": opinion.stance,
                        "next_day_close_outcome": self._label_outcome(outcome.next_day_close_pct),
                        "delta": round(delta, 2),
                    }
                )
        for item in agent_results.values():
            item.result_score_delta = round(item.result_score_delta, 2)
        return sorted(agent_results.values(), key=lambda item: item.agent_id)

    def _score_opinion(self, stance: str, confidence: str, next_day_close_pct: float) -> float:
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
        return round(base * multiplier, 2)

    @staticmethod
    def _label_outcome(next_day_close_pct: float) -> str:
        if next_day_close_pct >= 0.02:
            return "positive"
        if next_day_close_pct <= -0.02:
            return "negative"
        return "flat"
