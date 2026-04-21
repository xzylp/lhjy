"""Agent 学分状态持久化。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field

from .agent_rating import AgentRatingService, AgentRatingSnapshot
from ..infra.safe_json import atomic_write_json, read_json_with_backup
from ..logging_config import get_logger
from .settlement import determine_governance_state, determine_weight_bucket, determine_weight_value


WeightBucket = Literal["high_credit", "normal", "low_credit", "weak_credit", "suspended"]
GovernanceState = Literal["normal_mode", "learning_mode", "suspended", "recovered_low_credit"]

DEFAULT_AGENT_IDS = (
    "ashare-research",
    "ashare-strategy",
    "ashare-risk",
    "ashare-audit",
)

logger = get_logger("learning.score_state")


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
    elo_rating: float = 1000.0
    rating_tier: str = "apprentice"
    apprentice: bool = True
    settled_matches: int = 0
    applied_settlement_keys: list[str] = Field(default_factory=list)
    already_applied: bool = False
    cases_evaluated: list[AgentCaseEvaluation] = Field(default_factory=list)
    learning_updates: list[AgentLearningUpdate] = Field(default_factory=list)
    updated_at: str


class AgentScoreService:
    """学分状态读取与结算记录。"""

    def __init__(self, storage_path: Path, now_factory: Callable[[], datetime] | None = None) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_factory = now_factory or datetime.now
        self._rating_service = AgentRatingService(storage_path.with_name("agent_ratings.json"))

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
        settlement_key: str | None = None,
        confidence_tier: str = "medium",
        rating_snapshot_override: AgentRatingSnapshot | None = None,
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
        if settlement_key and settlement_key in existing.applied_settlement_keys:
            existing.already_applied = True
            return existing
        old_score = existing.new_score
        new_score = max(0.0, min(20.0, old_score + result_score_delta + learning_score_delta + governance_score_delta))
        existing.old_score = old_score
        existing.result_score_delta = result_score_delta
        existing.learning_score_delta = learning_score_delta
        existing.governance_score_delta = governance_score_delta
        existing.new_score = new_score
        rating_snapshot = rating_snapshot_override or self._rating_service.apply_delta(
            agent_id,
            result_score_delta + learning_score_delta + governance_score_delta,
            confidence_tier=confidence_tier,
        )
        elo_multiplier = 0.9
        if rating_snapshot.rating >= 1120:
            elo_multiplier = 1.15
        elif rating_snapshot.rating >= 1040:
            elo_multiplier = 1.05
        elif rating_snapshot.rating < 960:
            elo_multiplier = 0.8
        existing.weight_bucket = determine_weight_bucket(new_score)
        existing.weight_value = round(determine_weight_value(new_score) * elo_multiplier, 4)
        existing.governance_state = determine_governance_state(new_score)
        existing.elo_rating = rating_snapshot.rating
        existing.rating_tier = rating_snapshot.tier
        existing.apprentice = rating_snapshot.apprentice
        existing.settled_matches = rating_snapshot.settled_matches
        if settlement_key and settlement_key not in existing.applied_settlement_keys:
            existing.applied_settlement_keys.append(settlement_key)
            existing.applied_settlement_keys = existing.applied_settlement_keys[-200:]
        existing.already_applied = False
        existing.cases_evaluated = [AgentCaseEvaluation(**item) for item in (cases_evaluated or [])]
        existing.learning_updates = [AgentLearningUpdate(**item) for item in (learning_updates or [])]
        existing.updated_at = self._now_factory().isoformat()
        self._write_states(states)
        return existing

    def _read_states(self) -> list[AgentScoreState]:
        payload = read_json_with_backup(self._storage_path, default={"states": []})
        if not isinstance(payload, dict):
            return []
        return [AgentScoreState(**item) for item in payload.get("states", [])]

    def _ensure_defaults(self, target_date: str) -> list[AgentScoreState]:
        states = self._read_states()
        existing_keys = {(item.agent_id, item.score_date) for item in states}
        mutated = False
        for agent_id in DEFAULT_AGENT_IDS:
            key = (agent_id, target_date)
            if key in existing_keys:
                continue
            rating_snapshot = self._rating_service.ensure_agent(agent_id)
            states.append(
                AgentScoreState(
                    agent_id=agent_id,
                    score_date=target_date,
                    new_score=10.0,
                    weight_bucket="normal",
                    weight_value=1.0,
                    governance_state="normal_mode",
                    elo_rating=rating_snapshot.rating,
                    rating_tier=rating_snapshot.tier,
                    apprentice=rating_snapshot.apprentice,
                    settled_matches=rating_snapshot.settled_matches,
                    updated_at=self._now_factory().isoformat(),
                )
            )
            mutated = True
        if mutated:
            self._write_states(states)
        return states

    def _write_states(self, states: list[AgentScoreState]) -> None:
        payload = {"states": [item.model_dump() for item in states]}
        atomic_write_json(self._storage_path, payload)

    # ── 自进化扩展方法 ──────────────────────────────────────

    def read_states(self, target_date: str | None = None) -> list[AgentScoreState]:
        """兼容旧调用名，返回指定日期的 score states。"""
        return self.list_scores(target_date)

    def export_weights(self, target_date: str | None = None) -> dict[str, float]:
        """导出所有 Agent 的当日 weight_value。

        Returns:
            {agent_id: weight_value}，如 {"ashare-risk": 0.6, "ashare-research": 1.0, ...}

        用途：传给 finalizer.build_finalize_bundle(agent_weights=...) 实现加权投票。
        """
        states = self.read_states(target_date)
        return {
            state.agent_id: state.weight_value
            for state in states
        }

    def export_profiles(self, target_date: str | None = None) -> dict[str, dict[str, float | str | bool]]:
        states = self.read_states(target_date)
        return {
            state.agent_id: {
                "weight_value": state.weight_value,
                "new_score": state.new_score,
                "governance_score": round(float(state.new_score or 0.0) / 20.0, 4),
                "elo_rating": state.elo_rating,
                "rating_tier": state.rating_tier,
                "governance_state": state.governance_state,
                "apprentice": state.apprentice,
            }
            for state in states
        }

    def run_daily_settlement(
        self,
        *,
        settlement_results: list[dict] | None = None,
        trade_date: str | None = None,
    ) -> list[dict]:
        """每日盘后统一结算入口。

        协调调用 settlement → record_settlement → 更新 score_state。

        Args:
            settlement_results: settlement.py:run_daily() 的输出
            trade_date: 交易日

        Returns:
            更新后的所有 agent score state dicts

        TODO:
            1. 调用 settlement.run_daily(attribution_report, cases, outcomes)
            2. 遍历 settlement_results，调用 record_settlement()
            3. 通过 EventBus 发布 SETTLEMENT_COMPLETE 事件
        """
        target_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        settlement_results = settlement_results or []
        normalized_results: list[dict] = []
        for result in settlement_results:
            agent_id = str(result.get("agent_id", "")).strip()
            if not agent_id:
                continue
            normalized_results.append(dict(result))

        pairwise_rating_snapshots: dict[str, AgentRatingSnapshot] = {}
        if len(normalized_results) >= 2:
            round_scores = {
                str(result.get("agent_id") or "").strip(): (
                    float(result.get("result_score_delta", result.get("credit_delta", 0.0)) or 0.0)
                    + float(result.get("learning_score_delta", 0.0) or 0.0)
                    + float(result.get("governance_score_delta", 0.0) or 0.0)
                )
                for result in normalized_results
                if str(result.get("agent_id") or "").strip()
            }
            if len({round(float(score or 0.0), 8) for score in round_scores.values()}) > 1:
                dominant_confidence = max(
                    (
                        str(result.get("confidence_tier") or "medium")
                        for result in normalized_results
                    ),
                    key=lambda item: {"low": 0, "medium": 1, "high": 2}.get(item, 1),
                    default="medium",
                )
                pairwise_rating_snapshots = self._rating_service.apply_pairwise_round(
                    round_scores,
                    confidence_tier=dominant_confidence,
                )

        for result in normalized_results:
            agent_id = str(result.get("agent_id", ""))
            if not agent_id:
                continue
            result_score_delta = float(result.get("result_score_delta", result.get("credit_delta", 0.0)) or 0.0)
            learning_score_delta = float(result.get("learning_score_delta", 0.0) or 0.0)
            governance_score_delta = float(result.get("governance_score_delta", 0.0) or 0.0)
            rating_snapshot_override = pairwise_rating_snapshots.get(agent_id)
            if (
                result_score_delta == 0.0
                and learning_score_delta == 0.0
                and governance_score_delta == 0.0
                and rating_snapshot_override is None
                and not str(result.get("settlement_key") or "").strip()
            ):
                continue
            self.record_settlement(
                agent_id=agent_id,
                score_date=target_date,
                result_score_delta=result_score_delta,
                learning_score_delta=learning_score_delta,
                governance_score_delta=governance_score_delta,
                cases_evaluated=list(result.get("cases_evaluated") or []),
                learning_updates=list(result.get("learning_updates") or []),
                settlement_key=str(result.get("settlement_key") or "") or None,
                confidence_tier=str(result.get("confidence_tier") or "medium"),
                rating_snapshot_override=rating_snapshot_override,
            )

        updated_states = self.read_states(target_date)
        logger.info("每日学分结算完成: trade_date=%s agents=%d", target_date, len(updated_states))
        return [state.model_dump() for state in updated_states]
