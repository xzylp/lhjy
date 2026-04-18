"""discussion 证据包构建器。"""

from __future__ import annotations

from ..contracts import (
    BearCase,
    BullCase,
    CaseEvidence,
    LeaderRankResult,
    MarketProfile,
    PlaybookContext,
    PlaybookMatchScore,
    SectorProfile,
    StockBehaviorProfile,
    UncertaintyProfile,
)


class EvidenceBuilder:
    """把现有评分事实收口成 discussion 可直接消费的证据包。"""

    def build_case_evidence(
        self,
        *,
        symbol: str,
        playbook_context: PlaybookContext | dict | None = None,
        playbook_match_score: PlaybookMatchScore | dict | None = None,
        behavior_profile: StockBehaviorProfile | dict | None = None,
        sector_profile: SectorProfile | dict | None = None,
        market_profile: MarketProfile | dict | None = None,
        leader_rank: LeaderRankResult | dict | None = None,
    ) -> CaseEvidence:
        context_payload = self._payload(playbook_context)
        match_payload = self._payload(playbook_match_score or context_payload.get("playbook_match_score"))
        behavior_payload = self._payload(behavior_profile)
        sector_payload = self._payload(sector_profile)
        market_payload = self._payload(market_profile)
        leader_payload = self._payload(leader_rank)

        playbook = self._text(match_payload.get("playbook")) or self._text(context_payload.get("playbook"))
        reason = self._text(match_payload.get("reason")) or f"{playbook or '当前标的'} 证据待复核"
        bull_facts = self._build_bull_facts(
            match_payload=match_payload,
            context_payload=context_payload,
            behavior_payload=behavior_payload,
            market_payload=market_payload,
            leader_payload=leader_payload,
        )
        bear_risks = self._build_bear_risks(
            match_payload=match_payload,
            context_payload=context_payload,
            behavior_payload=behavior_payload,
            sector_payload=sector_payload,
            leader_payload=leader_payload,
        )
        data_gaps = self._collect_data_gaps(
            match_payload=match_payload,
            behavior_payload=behavior_payload,
            sector_payload=sector_payload,
            market_payload=market_payload,
            leader_payload=leader_payload,
        )
        unknowns = self._build_key_unknowns(
            reason=reason,
            data_gaps=data_gaps,
            market_payload=market_payload,
            sector_payload=sector_payload,
        )

        bull_case = BullCase(
            thesis=self._build_bull_thesis(playbook=playbook, reason=reason, match_payload=match_payload),
            key_facts=bull_facts,
            data_gaps=data_gaps,
        )
        bear_case = BearCase(
            thesis=self._build_bear_thesis(playbook=playbook, bear_risks=bear_risks),
            key_risks=bear_risks,
            data_gaps=data_gaps,
        )
        uncertainty = UncertaintyProfile(
            thesis="当前结论基于已有评分事实，盘中仍需验证关键证据是否持续成立。",
            key_unknowns=unknowns,
            data_gaps=data_gaps,
        )
        return CaseEvidence(
            symbol=symbol,
            playbook=playbook,
            reason=reason,
            bull_case=bull_case,
            bear_case=bear_case,
            uncertainty=uncertainty,
        )

    @staticmethod
    def _payload(item) -> dict:
        if item is None:
            return {}
        if isinstance(item, dict):
            return dict(item)
        if hasattr(item, "model_dump"):
            return item.model_dump()
        return {}

    @staticmethod
    def _text(value) -> str:
        return str(value or "").strip()

    @staticmethod
    def _float(value) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _int(value, default: int = 0) -> int:
        try:
            return int(value or default)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _dedupe(items: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in items:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    def _build_bull_facts(
        self,
        *,
        match_payload: dict,
        context_payload: dict,
        behavior_payload: dict,
        market_payload: dict,
        leader_payload: dict,
    ) -> list[str]:
        facts = [self._text(item) for item in list(match_payload.get("bull_evidence") or [])]
        rank_in_sector = self._int(context_payload.get("rank_in_sector"), default=99)
        leader_score = self._float(context_payload.get("leader_score"))
        style_tag = self._text(context_payload.get("style_tag") or behavior_payload.get("style_tag"))
        board_success_rate = self._float(behavior_payload.get("board_success_rate_20d"))
        seal_ratio = self._float(leader_payload.get("seal_ratio"))
        sentiment_phase = self._text(market_payload.get("sentiment_phase"))

        if rank_in_sector < 99:
            facts.append(f"板块排位 rank_in_sector={rank_in_sector}")
        if leader_score > 0:
            facts.append(f"leader_score={leader_score:.2f}")
        if style_tag:
            facts.append(f"style_tag={style_tag}")
        if board_success_rate > 0:
            facts.append(f"board_success_rate_20d={board_success_rate:.2f}")
        if seal_ratio > 0:
            facts.append(f"seal_ratio={seal_ratio:.2f}")
        if sentiment_phase:
            facts.append(f"市场阶段={sentiment_phase}")
        return self._dedupe(facts) or [self._text(match_payload.get("reason")) or "当前存在可跟踪的多头结构"]

    def _build_bear_risks(
        self,
        *,
        match_payload: dict,
        context_payload: dict,
        behavior_payload: dict,
        sector_payload: dict,
        leader_payload: dict,
    ) -> list[str]:
        risks = [self._text(item) for item in list(match_payload.get("bear_evidence") or [])]
        bomb_rate = self._float(behavior_payload.get("bomb_rate_20d"))
        rank_in_sector = self._int(context_payload.get("rank_in_sector"), default=99)
        sector_life_cycle = self._text(sector_payload.get("life_cycle"))
        seal_ratio = self._float(leader_payload.get("seal_ratio"))

        if bomb_rate > 0:
            risks.append(f"bomb_rate_20d={bomb_rate:.2f}")
        if rank_in_sector > 2 and rank_in_sector < 99:
            risks.append(f"板块排位偏后 rank_in_sector={rank_in_sector}")
        if sector_life_cycle == "retreat":
            risks.append("板块生命周期处于 retreat，承接需二次确认")
        if 0 < seal_ratio < 0.08:
            risks.append(f"seal_ratio={seal_ratio:.2f} 偏弱")
        return self._dedupe(risks) or ["当前空头证据不足，但需警惕结构走弱"]

    def _collect_data_gaps(
        self,
        *,
        match_payload: dict,
        behavior_payload: dict,
        sector_payload: dict,
        market_payload: dict,
        leader_payload: dict,
    ) -> list[str]:
        gaps: list[str] = []
        if not match_payload.get("bull_evidence"):
            gaps.append("bull_evidence 缺失，需补充多头事实来源")
        if not match_payload.get("bear_evidence"):
            gaps.append("bear_evidence 缺失，需补充反证或风险事实")
        if not self._text(match_payload.get("reason")):
            gaps.append("playbook_match_score.reason 缺失，当前结论解释力不足")
        if "board_success_rate_20d" not in behavior_payload:
            gaps.append("board_success_rate_20d 缺失，无法确认历史封板成功率")
        if "bomb_rate_20d" not in behavior_payload:
            gaps.append("bomb_rate_20d 缺失，无法确认炸板扰动")
        if "seal_ratio" not in leader_payload:
            gaps.append("seal_ratio 缺失，无法确认封板强度")
        if not sector_payload:
            gaps.append("sector_profile 缺失，无法确认板块生命周期")
        if not market_payload:
            gaps.append("market_profile 缺失，无法确认市场阶段")
        return self._dedupe(gaps)

    def _build_key_unknowns(
        self,
        *,
        reason: str,
        data_gaps: list[str],
        market_payload: dict,
        sector_payload: dict,
    ) -> list[str]:
        unknowns = list(data_gaps)
        if self._text(market_payload.get("sentiment_phase")):
            unknowns.append(f"若市场阶段从 {market_payload.get('sentiment_phase')} 快速转弱，当前结论需要重估")
        else:
            unknowns.append("市场阶段未确认，盘中情绪切换可能改变当前结论")
        if self._text(sector_payload.get("life_cycle")):
            unknowns.append(f"若板块生命周期 {sector_payload.get('life_cycle')} 判断失真，战法适配结论需要回看")
        unknowns.append(f"当前主结论依赖：{reason}")
        return self._dedupe(unknowns) or ["当前证据不足，需等待更多盘中验证"]

    def _build_bull_thesis(self, *, playbook: str, reason: str, match_payload: dict) -> str:
        if bool(match_payload.get("qualified", False)):
            return f"{playbook or '该标的'} 多头结构当前达标，核心依据是：{reason}"
        return f"{playbook or '该标的'} 仍存在可交易多头线索，但尚未完全确认：{reason}"

    @staticmethod
    def _build_bear_thesis(*, playbook: str, bear_risks: list[str]) -> str:
        headline = bear_risks[0] if bear_risks else "当前仍有未解风险"
        return f"{playbook or '该标的'} 的主要反证是：{headline}"
