"""战法路由：根据市场状态与板块生命周期分配 playbook。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from ..contracts import (
    LeaderRankResult,
    MarketProfile,
    PlaybookContext,
    PlaybookName,
    PlaybookOverrideSnapshot,
    SectorProfile,
    StockBehaviorProfile,
)
from ..settings import load_settings
from .playbook_scorer import PlaybookScorer

EXIT_PARAMS: dict[PlaybookName, dict] = {
    "leader_chase": {
        "max_hold_minutes": 240,
        "open_failure_minutes": 5,
        "time_stop": "14:50",
        "win_rate": 0.58,
        "pl_ratio": 2.0,
        "atr_pct": 0.015,
    },
    "divergence_reseal": {
        "max_hold_minutes": 480,
        "open_failure_minutes": 30,
        "time_stop": "14:50",
        "win_rate": 0.55,
        "pl_ratio": 1.8,
        "atr_pct": 0.02,
    },
    "sector_reflow_first_board": {
        "max_hold_minutes": 240,
        "open_failure_minutes": 10,
        "time_stop": "14:50",
        "win_rate": 0.52,
        "pl_ratio": 1.6,
        "atr_pct": 0.02,
    },
}

ENTRY_WINDOWS: dict[PlaybookName, str] = {
    "leader_chase": "09:30-10:00",
    "divergence_reseal": "10:00-14:00",
    "sector_reflow_first_board": "09:45-10:30",
}

ROUTE_TABLE: dict[tuple[str, str], PlaybookName] = {
    ("trend", "ferment"): "leader_chase",
    ("trend", "climax"): "divergence_reseal",
    ("rotation", "start"): "sector_reflow_first_board",
    ("rotation", "ferment"): "sector_reflow_first_board",
    ("rotation", "climax"): "divergence_reseal",
}


class StrategyRouter:
    def __init__(self, storage_root: Path | None = None) -> None:
        self.playbook_scorer = PlaybookScorer()
        resolved_storage_root = Path(storage_root) if storage_root is not None else self._default_storage_root()
        self._playbook_override_path = resolved_storage_root / "learning" / "playbook_overrides.json"

    def route(
        self,
        profile: MarketProfile,
        sector_profiles: list[SectorProfile],
        candidates: list[str],
        stock_info: dict,
        behavior_profiles: dict[str, StockBehaviorProfile] | None = None,
        leader_ranks: dict[str, LeaderRankResult] | list[LeaderRankResult] | None = None,
    ) -> list[PlaybookContext]:
        if not profile.allowed_playbooks:
            return []

        behavior_profiles = behavior_profiles or {}
        leader_rank_map = self._normalize_leader_ranks(leader_ranks)
        active_overrides = self._load_active_playbook_overrides()
        results: list[PlaybookContext] = []
        for symbol in candidates:
            sector = self._get_sector(symbol, stock_info)
            if not sector:
                continue
            sector_profile = self._find_sector_profile(sector, sector_profiles)
            if sector_profile is None:
                continue

            playbook = ROUTE_TABLE.get((profile.regime, sector_profile.life_cycle))
            if playbook is None or playbook not in profile.allowed_playbooks:
                continue
            playbook_override = active_overrides.get(playbook, {})
            if str(playbook_override.get("status") or "") == "suspend":
                continue

            behavior_profile = behavior_profiles.get(symbol)
            leader_rank = leader_rank_map.get(symbol)
            rank_in_sector = self._resolve_rank(symbol, sector_profile, leader_rank)
            if playbook == "leader_chase" and rank_in_sector > 2:
                continue
            results.append(
                PlaybookContext(
                    playbook=playbook,
                    symbol=symbol,
                    sector=sector,
                    entry_window=ENTRY_WINDOWS[playbook],
                    confidence=self._calc_confidence(sector_profile, profile, behavior_profile, leader_rank),
                    rank_in_sector=rank_in_sector,
                    leader_score=leader_rank.leader_score if leader_rank is not None else 0.0,
                    style_tag=behavior_profile.style_tag if behavior_profile is not None else "",
                    exit_params=EXIT_PARAMS[playbook],
                )
            )

        results = self.playbook_scorer.score_contexts(
            results,
            market_profile=profile,
            sector_profiles=sector_profiles,
            behavior_profiles=behavior_profiles,
            leader_ranks=leader_rank_map,
        )
        if any(item.playbook_match_score is not None for item in results):
            return sorted(
                results,
                key=lambda item: (
                    float(item.playbook_match_score.score if item.playbook_match_score is not None else -1.0)
                    + self._override_sort_bonus(item.playbook, active_overrides),
                    item.confidence,
                    item.leader_score,
                    -item.rank_in_sector,
                ),
                reverse=True,
            )
        return sorted(
            results,
            key=lambda item: (
                item.confidence + self._override_sort_bonus(item.playbook, active_overrides),
                item.leader_score,
                -item.rank_in_sector,
            ),
            reverse=True,
        )

    @staticmethod
    def _get_sector(symbol: str, stock_info: dict) -> str:
        info = stock_info.get(symbol, {})
        if isinstance(info, str):
            return info
        return str(info.get("sector", ""))

    @staticmethod
    def _find_sector_profile(sector: str, sector_profiles: list[SectorProfile]) -> SectorProfile | None:
        for profile in sector_profiles:
            if profile.sector_name == sector:
                return profile
        return None

    @staticmethod
    def _get_sector_rank(symbol: str, sector_profile: SectorProfile) -> int:
        if symbol in sector_profile.leader_symbols:
            return sector_profile.leader_symbols.index(symbol) + 1
        return len(sector_profile.leader_symbols) + 1

    @staticmethod
    def _calc_confidence(
        sector_profile: SectorProfile,
        profile: MarketProfile,
        behavior_profile: StockBehaviorProfile | None = None,
        leader_rank: LeaderRankResult | None = None,
    ) -> float:
        raw = sector_profile.strength_score * 0.6 + profile.regime_score * 0.4
        if sector_profile.life_cycle == "ferment":
            raw += 0.05
        if sector_profile.life_cycle == "retreat":
            raw -= 0.20
        if behavior_profile is not None:
            raw += behavior_profile.board_success_rate_20d * 0.08
            raw -= behavior_profile.bomb_rate_20d * 0.05
        if leader_rank is not None:
            raw += leader_rank.leader_score * 0.12
        return round(min(max(raw, 0.0), 1.0), 3)

    @staticmethod
    def _normalize_leader_ranks(
        leader_ranks: dict[str, LeaderRankResult] | list[LeaderRankResult] | None,
    ) -> dict[str, LeaderRankResult]:
        if leader_ranks is None:
            return {}
        if isinstance(leader_ranks, dict):
            return leader_ranks
        return {item.symbol: item for item in leader_ranks}

    def _resolve_rank(
        self,
        symbol: str,
        sector_profile: SectorProfile,
        leader_rank: LeaderRankResult | None,
    ) -> int:
        if leader_rank is not None and leader_rank.zt_order_rank > 0:
            return leader_rank.zt_order_rank
        return self._get_sector_rank(symbol, sector_profile)

    @staticmethod
    def _default_storage_root() -> Path:
        try:
            return load_settings().storage_root
        except Exception:
            return Path(".ashare_state")

    @staticmethod
    def _payload(item: Any) -> dict[str, Any]:
        if item is None:
            return {}
        if isinstance(item, dict):
            return dict(item)
        if hasattr(item, "model_dump"):
            dumped = item.model_dump()
            return dict(dumped) if isinstance(dumped, dict) else {}
        return {}

    def _load_active_playbook_overrides(self) -> dict[str, dict[str, Any]]:
        if not self._playbook_override_path.exists():
            return {}
        try:
            raw_payload = json.loads(self._playbook_override_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        try:
            snapshot = PlaybookOverrideSnapshot.model_validate(raw_payload)
            payload = snapshot.model_dump()
        except Exception:
            payload = self._payload(raw_payload)
        today = date.today().isoformat()
        active: dict[str, dict[str, Any]] = {}
        for item in list(payload.get("overrides") or []):
            override_payload = self._payload(item)
            playbook = str(override_payload.get("playbook") or "").strip()
            status = str(override_payload.get("status") or "").strip()
            expires_on = str(override_payload.get("expires_on") or "").strip()
            if not playbook or status not in {"suspend", "boost"}:
                continue
            if expires_on and expires_on < today:
                continue
            active[playbook] = override_payload
        return active

    @staticmethod
    def _override_sort_bonus(
        playbook: PlaybookName,
        active_overrides: dict[str, dict[str, Any]],
    ) -> float:
        override_payload = active_overrides.get(str(playbook), {})
        if str(override_payload.get("status") or "") == "boost":
            return 0.03
        return 0.0
