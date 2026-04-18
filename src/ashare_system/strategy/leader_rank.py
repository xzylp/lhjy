"""板块内相对龙头排序。"""

from __future__ import annotations

from ..contracts import LeaderRankResult, StockBehaviorProfile


class LeaderRanker:
    """根据板块前排特征和历史股性做相对排序。"""

    def rank(
        self,
        candidates: list[str],
        sector_data: dict[str, dict],
        profiles: dict[str, StockBehaviorProfile] | None = None,
    ) -> list[LeaderRankResult]:
        profiles = profiles or {}
        results: list[LeaderRankResult] = []
        for symbol in candidates:
            sector_meta = sector_data.get(symbol, {})
            profile = profiles.get(symbol)
            score = self._calc_leader_score(sector_meta, profile)
            zt_order_rank = int(sector_meta.get("zt_order_rank", 99) or 99)
            results.append(
                LeaderRankResult(
                    symbol=symbol,
                    sector=str(sector_meta.get("sector", "")),
                    leader_score=score,
                    zt_order_rank=zt_order_rank,
                    seal_ratio=round(float(sector_meta.get("seal_ratio", 0.0) or 0.0), 4),
                    first_limit_time=str(sector_meta.get("first_limit_time", "") or ""),
                    open_times=int(sector_meta.get("open_times", 0) or 0),
                    is_core_leader=score >= 0.7,
                )
            )
        return sorted(results, key=lambda item: (item.leader_score, -item.zt_order_rank), reverse=True)

    def to_map(
        self,
        candidates: list[str],
        sector_data: dict[str, dict],
        profiles: dict[str, StockBehaviorProfile] | None = None,
    ) -> dict[str, LeaderRankResult]:
        return {item.symbol: item for item in self.rank(candidates, sector_data, profiles)}

    @staticmethod
    def _calc_leader_score(sector_meta: dict, profile: StockBehaviorProfile | None) -> float:
        zt_rank_score = max(0.0, 1 - float(sector_meta.get("zt_order_rank", 10)) * 0.1)
        seal_score = min(float(sector_meta.get("seal_ratio", 0.0)) * 10, 1.0)
        diffusion_score = min(float(sector_meta.get("diffusion_count", 0.0)) / 10, 1.0)
        liq_score = min(float(sector_meta.get("turnover_rate", 0.0)) / 10, 1.0)
        hist_score = profile.board_success_rate_20d if profile is not None else 0.5
        leader_bonus = profile.leader_frequency_30d if profile is not None else 0.0
        raw = (
            zt_rank_score * 0.30
            + seal_score * 0.25
            + diffusion_score * 0.20
            + hist_score * 0.15
            + liq_score * 0.05
            + leader_bonus * 0.05
        )
        return round(min(max(raw, 0.0), 1.0), 4)
