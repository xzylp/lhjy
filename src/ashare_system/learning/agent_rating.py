"""Agent Elo 评分服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import math
from typing import Any

from ..infra.safe_json import atomic_write_json, read_json_with_backup


@dataclass
class AgentRatingSnapshot:
    agent_id: str
    rating: float
    tier: str
    apprentice: bool
    settled_matches: int
    updated_at: str
    last_updated_date: str
    daily_delta_accumulator: float = 0.0
    daily_update_count: int = 0


class AgentRatingService:
    def __init__(self, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)

    def ensure_agent(self, agent_id: str) -> AgentRatingSnapshot:
        payload = self._read()
        items = payload.setdefault("items", {})
        current = dict(items.get(agent_id) or {})
        if current:
            return self._to_snapshot(agent_id, current)
        snapshot = AgentRatingSnapshot(
            agent_id=agent_id,
            rating=1000.0,
            tier="apprentice",
            apprentice=True,
            settled_matches=0,
            updated_at=datetime.now().isoformat(),
            last_updated_date="",
        )
        items[agent_id] = snapshot.__dict__
        self._write(payload)
        return snapshot

    def apply_delta(self, agent_id: str, delta: float, confidence_tier: str = "medium") -> AgentRatingSnapshot:
        payload = self._read()
        items = payload.setdefault("items", {})
        current = dict(items.get(agent_id) or self.ensure_agent(agent_id).__dict__)
        current_rating = float(current.get("rating", 1000.0) or 1000.0)
        settled_matches = int(current.get("settled_matches", 0) or 0)
        now = datetime.now()
        today = now.date().isoformat()
        previous_update_date = str(current.get("last_updated_date") or "")
        current_pending_delta = float(current.get("daily_delta_accumulator", 0.0) or 0.0)
        current_pending_count = int(current.get("daily_update_count", 0) or 0)
        raw_anchor_rating = current.get("day_anchor_rating")
        day_anchor_rating = current_rating if raw_anchor_rating is None else float(raw_anchor_rating)
        raw_anchor_matches = current.get("day_anchor_matches")
        day_anchor_matches = settled_matches if raw_anchor_matches is None else int(raw_anchor_matches)
        current_scale_sum = float(current.get("daily_confidence_scale_sum", 0.0) or 0.0)

        if previous_update_date != today:
            day_anchor_rating = current_rating
            day_anchor_matches = settled_matches
            current_pending_delta = 0.0
            current_pending_count = 0
            current_scale_sum = 0.0

        confidence_scale = self._resolve_confidence_scale(confidence_tier)
        merged_delta = current_pending_delta + float(delta or 0.0)
        merged_count = current_pending_count + 1
        merged_scale_sum = current_scale_sum + confidence_scale
        merged_confidence = self._resolve_confidence_tier_from_scale(merged_scale_sum / max(merged_count, 1))

        actual = 0.5 + math.tanh(merged_delta) * 0.5
        expected = 1.0 / (1.0 + 10 ** ((1000.0 - day_anchor_rating) / 400.0))
        base_k = 16.0 if day_anchor_matches >= 20 else 24.0
        k_factor = base_k * self._resolve_confidence_scale(merged_confidence)
        next_rating = day_anchor_rating + k_factor * (actual - expected)
        settled_matches = day_anchor_matches + 1
        if settled_matches < 30:
            next_rating = min(next_rating, 1080.0)
        tier = self._resolve_tier(next_rating, settled_matches)
        snapshot = AgentRatingSnapshot(
            agent_id=agent_id,
            rating=round(next_rating, 2),
            tier=tier,
            apprentice=settled_matches < 12,
            settled_matches=settled_matches,
            updated_at=now.isoformat(),
            last_updated_date=today,
            daily_delta_accumulator=round(merged_delta, 4),
            daily_update_count=merged_count,
        )
        snapshot_payload = {
            **snapshot.__dict__,
            "day_anchor_rating": round(day_anchor_rating, 4),
            "day_anchor_matches": day_anchor_matches,
            "daily_confidence_scale_sum": round(merged_scale_sum, 4),
        }
        items[agent_id] = snapshot_payload
        self._write(payload)
        return snapshot

    def apply_pairwise_round(
        self,
        round_scores: dict[str, float],
        *,
        confidence_tier: str = "medium",
    ) -> dict[str, AgentRatingSnapshot]:
        normalized_scores = {
            str(agent_id).strip(): float(score or 0.0)
            for agent_id, score in dict(round_scores or {}).items()
            if str(agent_id).strip()
        }
        if len(normalized_scores) < 2:
            return {
                agent_id: self.apply_delta(agent_id, score, confidence_tier=confidence_tier)
                for agent_id, score in normalized_scores.items()
            }
        if len({round(score, 8) for score in normalized_scores.values()}) <= 1:
            return {agent_id: self.ensure_agent(agent_id) for agent_id in normalized_scores}

        payload = self._read()
        items = payload.setdefault("items", {})
        confidence_scale = self._resolve_confidence_scale(confidence_tier)
        current_states: dict[str, dict[str, Any]] = {}
        rating_map: dict[str, float] = {}
        settled_match_map: dict[str, int] = {}
        for agent_id in normalized_scores:
            current = dict(items.get(agent_id) or self.ensure_agent(agent_id).__dict__)
            current_states[agent_id] = current
            rating_map[agent_id] = float(current.get("rating", 1000.0) or 1000.0)
            settled_match_map[agent_id] = int(current.get("settled_matches", 0) or 0)

        delta_map = {agent_id: 0.0 for agent_id in normalized_scores}
        pair_count_map = {agent_id: 0 for agent_id in normalized_scores}
        agent_ids = sorted(normalized_scores)
        for index, left_id in enumerate(agent_ids):
            for right_id in agent_ids[index + 1 :]:
                left_score = normalized_scores[left_id]
                right_score = normalized_scores[right_id]
                if left_score > right_score:
                    actual_left = 1.0
                elif left_score < right_score:
                    actual_left = 0.0
                else:
                    actual_left = 0.5
                expected_left = 1.0 / (1.0 + 10 ** ((rating_map[right_id] - rating_map[left_id]) / 400.0))
                delta_map[left_id] += actual_left - expected_left
                delta_map[right_id] += (1.0 - actual_left) - (1.0 - expected_left)
                pair_count_map[left_id] += 1
                pair_count_map[right_id] += 1

        now = datetime.now()
        today = now.date().isoformat()
        snapshots: dict[str, AgentRatingSnapshot] = {}
        for agent_id in agent_ids:
            pair_count = max(pair_count_map.get(agent_id, 0), 1)
            settled_matches = settled_match_map.get(agent_id, 0)
            base_k = 16.0 if settled_matches >= 20 else 24.0
            k_factor = base_k * confidence_scale / pair_count
            next_rating = rating_map[agent_id] + k_factor * delta_map[agent_id]
            next_settled_matches = settled_matches + 1
            if next_settled_matches < 30:
                next_rating = min(next_rating, 1080.0)
            snapshot = AgentRatingSnapshot(
                agent_id=agent_id,
                rating=round(next_rating, 2),
                tier=self._resolve_tier(next_rating, next_settled_matches),
                apprentice=next_settled_matches < 12,
                settled_matches=next_settled_matches,
                updated_at=now.isoformat(),
                last_updated_date=today,
                daily_delta_accumulator=0.0,
                daily_update_count=0,
            )
            items[agent_id] = {
                **snapshot.__dict__,
                "day_anchor_rating": round(snapshot.rating, 4),
                "day_anchor_matches": next_settled_matches,
                "daily_confidence_scale_sum": 0.0,
            }
            snapshots[agent_id] = snapshot
        self._write(payload)
        return snapshots

    def list(self) -> list[dict[str, Any]]:
        payload = self._read()
        items = [self._to_snapshot(agent_id, entry).__dict__ for agent_id, entry in dict(payload.get("items") or {}).items()]
        items.sort(key=lambda item: (-float(item.get("rating", 0.0) or 0.0), item.get("agent_id")))
        return items

    def _resolve_tier(self, rating: float, settled_matches: int) -> str:
        if settled_matches < 12:
            return "apprentice"
        if rating >= 1120:
            return "elite"
        if rating >= 1040:
            return "solid"
        if rating >= 960:
            return "normal"
        return "probation"

    @staticmethod
    def _resolve_confidence_scale(confidence_tier: str) -> float:
        return {"low": 0.3, "medium": 0.6, "high": 1.0}.get(str(confidence_tier or "medium").strip().lower(), 0.6)

    @classmethod
    def _resolve_confidence_tier_from_scale(cls, scale: float) -> str:
        if scale >= 0.85:
            return "high"
        if scale <= 0.4:
            return "low"
        return "medium"

    def _to_snapshot(self, agent_id: str, payload: dict[str, Any]) -> AgentRatingSnapshot:
        return AgentRatingSnapshot(
            agent_id=agent_id,
            rating=float(payload.get("rating", 1000.0) or 1000.0),
            tier=str(payload.get("tier") or "apprentice"),
            apprentice=bool(payload.get("apprentice", True)),
            settled_matches=int(payload.get("settled_matches", 0) or 0),
            updated_at=str(payload.get("updated_at") or datetime.now().isoformat()),
            last_updated_date=str(payload.get("last_updated_date") or ""),
            daily_delta_accumulator=float(payload.get("daily_delta_accumulator", 0.0) or 0.0),
            daily_update_count=int(payload.get("daily_update_count", 0) or 0),
        )

    def _read(self) -> dict[str, Any]:
        payload = read_json_with_backup(self._storage_path, default={"items": {}})
        return payload if isinstance(payload, dict) else {"items": {}}

    def _write(self, payload: dict[str, Any]) -> None:
        atomic_write_json(self._storage_path, payload)
