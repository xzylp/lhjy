"""Agent 学分状态持久化。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field

from .settlement import determine_governance_state, determine_weight_bucket, determine_weight_value


WeightBucket = Literal["high_credit", "normal", "low_credit", "weak_credit", "suspended"]
GovernanceState = Literal["normal_mode", "learning_mode", "suspended", "recovered_low_credit"]

DEFAULT_AGENT_IDS = (
    "ashare-research",
    "ashare-strategy",
    "ashare-risk",
    "ashare-audit",
)


class AgentCaseEvaluation(BaseModel):
    symbol: str
    stance: str
    next_day_close_outcome: str
    delta: float = 0.0


class AgentLearningUpdate(BaseModel):
    proposal_id: str
    adopted: bool = False
    verified_effective: bool = False
    delta: float = 0.0


class AgentScoreState(BaseModel):
    agent_id: str
    score_date: str
    old_score: float = 10.0
    result_score_delta: float = 0.0
    learning_score_delta: float = 0.0
    governance_score_delta: float = 0.0
    new_score: float = 10.0
    weight_bucket: WeightBucket = "normal"
    weight_value: float = 1.0
    governance_state: GovernanceState = "normal_mode"
    cases_evaluated: list[AgentCaseEvaluation] = Field(default_factory=list)
    learning_updates: list[AgentLearningUpdate] = Field(default_factory=list)
    updated_at: str


class AgentScoreService:
    """学分状态读取与结算记录。"""

    def __init__(self, storage_path: Path, now_factory: Callable[[], datetime] | None = None) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_factory = now_factory or datetime.now

    def ensure_defaults(self, score_date: str | None = None) -> list[AgentScoreState]:
        target_date = score_date or self._now_factory().date().isoformat()
        states = self._ensure_defaults(target_date)
        states = [item for item in states if item.score_date == target_date]
        states.sort(key=lambda item: (-item.new_score, item.agent_id))
        return states

    def list_scores(self, score_date: str | None = None) -> list[AgentScoreState]:
        target_date = score_date or self._now_factory().date().isoformat()
        states = self._ensure_defaults(target_date)
        if score_date:
            states = [item for item in states if item.score_date == score_date]
        else:
            states = [item for item in states if item.score_date == target_date]
        states.sort(key=lambda item: (-item.new_score, item.agent_id))
        return states

    def record_settlement(
        self,
        agent_id: str,
        score_date: str,
        result_score_delta: float = 0.0,
        learning_score_delta: float = 0.0,
        governance_score_delta: float = 0.0,
        cases_evaluated: list[dict] | None = None,
        learning_updates: list[dict] | None = None,
    ) -> AgentScoreState:
        states = self._read_states()
        existing = None
        for item in states:
            if item.agent_id == agent_id and item.score_date == score_date:
                existing = item
                break
        if existing is None:
            existing = AgentScoreState(
                agent_id=agent_id,
                score_date=score_date,
                updated_at=self._now_factory().isoformat(),
            )
            states.append(existing)
        old_score = existing.new_score
        new_score = max(0.0, min(20.0, old_score + result_score_delta + learning_score_delta + governance_score_delta))
        existing.old_score = old_score
        existing.result_score_delta = result_score_delta
        existing.learning_score_delta = learning_score_delta
        existing.governance_score_delta = governance_score_delta
        existing.new_score = new_score
        existing.weight_bucket = determine_weight_bucket(new_score)
        existing.weight_value = determine_weight_value(new_score)
        existing.governance_state = determine_governance_state(new_score)
        existing.cases_evaluated = [AgentCaseEvaluation(**item) for item in (cases_evaluated or [])]
        existing.learning_updates = [AgentLearningUpdate(**item) for item in (learning_updates or [])]
        existing.updated_at = self._now_factory().isoformat()
        self._write_states(states)
        return existing

    def _read_states(self) -> list[AgentScoreState]:
        if not self._storage_path.exists():
            return []
        payload = json.loads(self._storage_path.read_text(encoding="utf-8"))
        return [AgentScoreState(**item) for item in payload.get("states", [])]

    def _ensure_defaults(self, target_date: str) -> list[AgentScoreState]:
        states = self._read_states()
        existing_keys = {(item.agent_id, item.score_date) for item in states}
        mutated = False
        for agent_id in DEFAULT_AGENT_IDS:
            key = (agent_id, target_date)
            if key in existing_keys:
                continue
            states.append(
                AgentScoreState(
                    agent_id=agent_id,
                    score_date=target_date,
                    new_score=10.0,
                    weight_bucket="normal",
                    weight_value=1.0,
                    governance_state="normal_mode",
                    updated_at=self._now_factory().isoformat(),
                )
            )
            mutated = True
        if mutated:
            self._write_states(states)
        return states

    def _write_states(self, states: list[AgentScoreState]) -> None:
        payload = {"states": [item.model_dump() for item in states]}
        self._storage_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
