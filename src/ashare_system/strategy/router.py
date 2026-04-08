"""战法路由：根据市场状态与板块生命周期分配 playbook。"""

from __future__ import annotations

from ..contracts import LeaderRankResult, MarketProfile, PlaybookContext, PlaybookName, SectorProfile, StockBehaviorProfile

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

            behavior_profile = behavior_profiles.get(symbol)
            leader_rank = leader_rank_map.get(symbol)
            results.append(
                PlaybookContext(
                    playbook=playbook,
                    symbol=symbol,
                    sector=sector,
                    entry_window=ENTRY_WINDOWS[playbook],
                    confidence=self._calc_confidence(sector_profile, profile, behavior_profile, leader_rank),
                    rank_in_sector=self._resolve_rank(symbol, sector_profile, leader_rank),
                    leader_score=leader_rank.leader_score if leader_rank is not None else 0.0,
                    style_tag=behavior_profile.style_tag if behavior_profile is not None else "",
                    exit_params=EXIT_PARAMS[playbook],
                )
            )

        return sorted(results, key=lambda item: (item.confidence, item.leader_score, -item.rank_in_sector), reverse=True)

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
