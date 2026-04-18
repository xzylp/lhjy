"""战法匹配分打分器。"""

from __future__ import annotations

from ..contracts import (
    LeaderRankResult,
    MarketProfile,
    PlaybookContext,
    PlaybookMatchScore,
    SectorProfile,
    StockBehaviorProfile,
)


class PlaybookScorer:
    """为路由后的 playbook context 生成最小可消费的战法匹配分。"""

    def score_contexts(
        self,
        contexts: list[PlaybookContext],
        *,
        market_profile: MarketProfile | None = None,
        sector_profiles: list[SectorProfile] | None = None,
        behavior_profiles: dict[str, StockBehaviorProfile] | None = None,
        leader_ranks: dict[str, LeaderRankResult] | list[LeaderRankResult] | None = None,
    ) -> list[PlaybookContext]:
        sector_map = {
            item.sector_name: item
            for item in (sector_profiles or [])
        }
        behavior_profiles = behavior_profiles or {}
        leader_rank_map = self._normalize_leader_ranks(leader_ranks)
        scored_contexts: list[PlaybookContext] = []
        for context in contexts:
            match_score = self.score_context(
                context,
                market_profile=market_profile,
                sector_profile=sector_map.get(context.sector),
                behavior_profile=behavior_profiles.get(context.symbol),
                leader_rank=leader_rank_map.get(context.symbol),
            )
            scored_contexts.append(context.model_copy(update={"playbook_match_score": match_score}))
        return scored_contexts

    def score_context(
        self,
        context: PlaybookContext,
        *,
        market_profile: MarketProfile | None = None,
        sector_profile: SectorProfile | None = None,
        behavior_profile: StockBehaviorProfile | None = None,
        leader_rank: LeaderRankResult | None = None,
    ) -> PlaybookMatchScore:
        if context.playbook == "leader_chase":
            return self._score_leader_chase(context, behavior_profile=behavior_profile, leader_rank=leader_rank)
        if context.playbook == "divergence_reseal":
            return self._score_divergence_reseal(
                context,
                market_profile=market_profile,
                sector_profile=sector_profile,
                behavior_profile=behavior_profile,
                leader_rank=leader_rank,
            )
        return self._score_sector_reflow_first_board(
            context,
            market_profile=market_profile,
            sector_profile=sector_profile,
            behavior_profile=behavior_profile,
            leader_rank=leader_rank,
        )

    @staticmethod
    def _normalize_leader_ranks(
        leader_ranks: dict[str, LeaderRankResult] | list[LeaderRankResult] | None,
    ) -> dict[str, LeaderRankResult]:
        if leader_ranks is None:
            return {}
        if isinstance(leader_ranks, dict):
            return leader_ranks
        return {item.symbol: item for item in leader_ranks}

    @staticmethod
    def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
        return min(max(value, lower), upper)

    @staticmethod
    def _time_to_minutes(value: str) -> int | None:
        text = str(value or "").strip()
        if not text:
            return None
        if ":" in text:
            parts = text.split(":", maxsplit=1)
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                return int(parts[0]) * 60 + int(parts[1])
            return None
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) == 4:
            return int(digits[:2]) * 60 + int(digits[2:])
        return None

    def _score_leader_chase(
        self,
        context: PlaybookContext,
        *,
        behavior_profile: StockBehaviorProfile | None,
        leader_rank: LeaderRankResult | None,
    ) -> PlaybookMatchScore:
        rank_in_sector = max(context.rank_in_sector or (leader_rank.zt_order_rank if leader_rank else 99), 1)
        seal_ratio = float(leader_rank.seal_ratio if leader_rank is not None else 0.0)
        board_success_rate_20d = float(behavior_profile.board_success_rate_20d if behavior_profile is not None else 0.0)
        first_limit_time = str(leader_rank.first_limit_time if leader_rank is not None else "")
        open_times = int(leader_rank.open_times if leader_rank is not None else 0)

        seal_score = self._clamp(seal_ratio / 0.12)
        rank_score = self._clamp(1.0 - (rank_in_sector - 1) * 0.22)
        board_score = self._clamp(board_success_rate_20d / 0.70)
        first_limit_minutes = self._time_to_minutes(first_limit_time)
        if first_limit_minutes is not None:
            if first_limit_minutes <= 9 * 60 + 45:
                timing_score = 1.0
            elif first_limit_minutes <= 10 * 60:
                timing_score = 0.85
            elif first_limit_minutes <= 10 * 60 + 30:
                timing_score = 0.65
            else:
                timing_score = 0.35
            timing_note = f"首次封板时间 {first_limit_time}"
        else:
            timing_score = self._clamp(1.0 - open_times * 0.25)
            timing_note = f"缺少封板时序，按 open_times={open_times} 代理"

        raw_score = rank_score * 0.30 + seal_score * 0.30 + board_score * 0.25 + timing_score * 0.15
        score = round(raw_score * 100, 2)
        qualified = (
            rank_in_sector <= 2
            and seal_ratio >= 0.08
            and board_success_rate_20d >= 0.45
            and timing_score >= 0.45
        )

        bull_evidence: list[str] = []
        bear_evidence: list[str] = []
        if rank_in_sector <= 2:
            bull_evidence.append(f"板块内前排 rank_in_sector={rank_in_sector}")
        else:
            bear_evidence.append(f"板块内后排 rank_in_sector={rank_in_sector}")
        if seal_ratio >= 0.08:
            bull_evidence.append(f"seal_ratio={seal_ratio:.2f} 达标")
        else:
            bear_evidence.append(f"seal_ratio={seal_ratio:.2f} 偏弱")
        if board_success_rate_20d >= 0.45:
            bull_evidence.append(f"board_success_rate_20d={board_success_rate_20d:.2f} 达标")
        else:
            bear_evidence.append(f"board_success_rate_20d={board_success_rate_20d:.2f} 偏低")
        if timing_score >= 0.65:
            bull_evidence.append(timing_note)
        else:
            bear_evidence.append(timing_note)
        if open_times > 0:
            bear_evidence.append(f"open_times={open_times}，炸板扰动偏高")
        else:
            bull_evidence.append("open_times=0，封板稳定")

        reason = "leader_chase 结构达标" if qualified else (bear_evidence[0] if bear_evidence else "leader_chase 结构不足")
        return PlaybookMatchScore(
            playbook=context.playbook,
            symbol=context.symbol,
            qualified=qualified,
            score=score,
            reason=reason,
            bull_evidence=bull_evidence or ["存在前排龙头特征"],
            bear_evidence=bear_evidence or ["未发现显著负面证据"],
        )

    def _score_divergence_reseal(
        self,
        context: PlaybookContext,
        *,
        market_profile: MarketProfile | None,
        sector_profile: SectorProfile | None,
        behavior_profile: StockBehaviorProfile | None,
        leader_rank: LeaderRankResult | None,
    ) -> PlaybookMatchScore:
        reseal_rate = float(behavior_profile.reseal_rate_20d if behavior_profile is not None else 0.0)
        board_success_rate_20d = float(behavior_profile.board_success_rate_20d if behavior_profile is not None else 0.0)
        bomb_rate_20d = float(behavior_profile.bomb_rate_20d if behavior_profile is not None else 0.0)
        seal_ratio = float(leader_rank.seal_ratio if leader_rank is not None else 0.0)
        sector_strength = float(sector_profile.strength_score if sector_profile is not None else 0.0)
        regime_score = float(market_profile.regime_score if market_profile is not None else context.confidence)

        raw_score = (
            self._clamp(reseal_rate / 0.30) * 0.35
            + self._clamp(board_success_rate_20d / 0.65) * 0.20
            + self._clamp(1.0 - bomb_rate_20d / 0.45) * 0.20
            + self._clamp(seal_ratio / 0.08) * 0.15
            + self._clamp((sector_strength + regime_score) / 2.0) * 0.10
        )
        score = round(raw_score * 100, 2)
        qualified = reseal_rate >= 0.12 and board_success_rate_20d >= 0.30 and bomb_rate_20d <= 0.40

        bull_evidence = [
            f"reseal_rate_20d={reseal_rate:.2f}",
            f"board_success_rate_20d={board_success_rate_20d:.2f}",
            f"seal_ratio={seal_ratio:.2f}",
        ]
        bear_evidence = []
        if bomb_rate_20d > 0.40:
            bear_evidence.append(f"bomb_rate_20d={bomb_rate_20d:.2f} 偏高")
        if reseal_rate < 0.12:
            bear_evidence.append(f"reseal_rate_20d={reseal_rate:.2f} 不足")
        if board_success_rate_20d < 0.30:
            bear_evidence.append(f"board_success_rate_20d={board_success_rate_20d:.2f} 偏低")
        reason = "divergence_reseal 结构达标" if qualified else (bear_evidence[0] if bear_evidence else "divergence_reseal 结构不足")
        return PlaybookMatchScore(
            playbook=context.playbook,
            symbol=context.symbol,
            qualified=qualified,
            score=score,
            reason=reason,
            bull_evidence=bull_evidence,
            bear_evidence=bear_evidence or ["未发现显著负面证据"],
        )

    def _score_sector_reflow_first_board(
        self,
        context: PlaybookContext,
        *,
        market_profile: MarketProfile | None,
        sector_profile: SectorProfile | None,
        behavior_profile: StockBehaviorProfile | None,
        leader_rank: LeaderRankResult | None,
    ) -> PlaybookMatchScore:
        reflow_score = float(sector_profile.reflow_score if sector_profile is not None else 0.0)
        sector_strength = float(sector_profile.strength_score if sector_profile is not None else 0.0)
        board_success_rate_20d = float(behavior_profile.board_success_rate_20d if behavior_profile is not None else 0.0)
        rank_in_sector = max(context.rank_in_sector or (leader_rank.zt_order_rank if leader_rank else 99), 1)
        regime_score = float(market_profile.regime_score if market_profile is not None else context.confidence)

        raw_score = (
            self._clamp(reflow_score / 0.60) * 0.45
            + self._clamp(sector_strength / 0.80) * 0.30
            + self._clamp(board_success_rate_20d / 0.60) * 0.10
            + self._clamp(regime_score) * 0.05
            + self._clamp(1.0 - (rank_in_sector - 1) * 0.20) * 0.10
        )
        score = round(raw_score * 100, 2)
        qualified = reflow_score >= 0.35 and sector_strength >= 0.45 and rank_in_sector <= 3

        bull_evidence = [
            f"reflow_score={reflow_score:.2f}",
            f"sector_strength={sector_strength:.2f}",
            f"rank_in_sector={rank_in_sector}",
        ]
        bear_evidence = []
        if reflow_score < 0.35:
            bear_evidence.append(f"reflow_score={reflow_score:.2f} 不足")
        if sector_strength < 0.45:
            bear_evidence.append(f"sector_strength={sector_strength:.2f} 偏弱")
        if rank_in_sector > 3:
            bear_evidence.append(f"rank_in_sector={rank_in_sector} 偏后")
        reason = (
            "sector_reflow_first_board 结构达标"
            if qualified
            else (bear_evidence[0] if bear_evidence else "sector_reflow_first_board 结构不足")
        )
        return PlaybookMatchScore(
            playbook=context.playbook,
            symbol=context.symbol,
            qualified=qualified,
            score=score,
            reason=reason,
            bull_evidence=bull_evidence,
            bear_evidence=bear_evidence or ["未发现显著负面证据"],
        )
