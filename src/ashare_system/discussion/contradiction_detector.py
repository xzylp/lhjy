"""discussion 矛盾检测器。"""

from __future__ import annotations

from typing import Any

from ..contracts import CaseContradictionSummary, Contradiction

SUPPORT_STANCES = {"support", "selected"}
BLOCKING_STANCES = {"reject", "rejected", "limit", "hold"}
DISCUSSION_AGENT_LABELS = {
    "ashare-research": "研究",
    "ashare-strategy": "策略",
    "ashare-risk": "风控",
    "ashare-audit": "审计",
}


class ContradictionDetector:
    """基于 case 证据包和最新 opinions 发现必须进入 Round 2 的矛盾。"""

    def detect_case_contradictions(
        self,
        *,
        case_id: str,
        opinions: list[Any],
        bull_case: dict[str, Any] | Any | None,
        bear_case: dict[str, Any] | Any | None,
        uncertainty: dict[str, Any] | Any | None,
    ) -> CaseContradictionSummary:
        latest_opinions = self._latest_opinions(opinions)
        bull_payload = self._payload(bull_case)
        bear_payload = self._payload(bear_case)
        uncertainty_payload = self._payload(uncertainty)

        contradictions: list[Contradiction] = []
        contradictions.extend(
            self._detect_pair_conflict(
                case_id=case_id,
                left=latest_opinions.get("ashare-research"),
                right=latest_opinions.get("ashare-risk"),
                contradiction_type="research_support_vs_risk_gate",
                bull_payload=bull_payload,
                bear_payload=bear_payload,
                uncertainty_payload=uncertainty_payload,
            )
        )
        contradictions.extend(
            self._detect_pair_conflict(
                case_id=case_id,
                left=latest_opinions.get("ashare-strategy"),
                right=latest_opinions.get("ashare-audit"),
                contradiction_type="strategy_support_vs_audit_gate",
                bull_payload=bull_payload,
                bear_payload=bear_payload,
                uncertainty_payload=uncertainty_payload,
            )
        )
        contradictions.extend(
            self._detect_case_level_conflict(
                case_id=case_id,
                latest_opinions=latest_opinions,
                bull_payload=bull_payload,
                bear_payload=bear_payload,
                uncertainty_payload=uncertainty_payload,
            )
        )
        summary_lines = self._dedupe(
            [
                self._summary_line(item, bull_payload=bull_payload, bear_payload=bear_payload)
                for item in contradictions
            ]
        )
        must_answer_questions = self._dedupe([item.question for item in contradictions if item.question])
        return CaseContradictionSummary(
            case_id=case_id,
            contradictions=contradictions,
            summary_lines=summary_lines,
            must_answer_questions=must_answer_questions,
        )

    @staticmethod
    def _payload(item: dict[str, Any] | Any | None) -> dict[str, Any]:
        if item is None:
            return {}
        if isinstance(item, dict):
            return dict(item)
        if hasattr(item, "model_dump"):
            dumped = item.model_dump()
            return dict(dumped) if isinstance(dumped, dict) else {}
        return {}

    def _latest_opinions(self, opinions: list[Any]) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for item in opinions:
            payload = self._payload(item)
            agent_id = str(payload.get("agent_id") or "").strip()
            if not agent_id:
                continue
            previous = latest.get(agent_id)
            current_key = (int(payload.get("round") or 0), str(payload.get("recorded_at") or ""))
            previous_key = (
                int(previous.get("round") or 0),
                str(previous.get("recorded_at") or ""),
            ) if previous else (-1, "")
            if current_key >= previous_key:
                latest[agent_id] = payload
        return latest

    def _detect_pair_conflict(
        self,
        *,
        case_id: str,
        left: dict[str, Any] | None,
        right: dict[str, Any] | None,
        contradiction_type: str,
        bull_payload: dict[str, Any],
        bear_payload: dict[str, Any],
        uncertainty_payload: dict[str, Any],
    ) -> list[Contradiction]:
        if not left or not right:
            return []
        left_stance = self._stance(left)
        right_stance = self._stance(right)
        if left_stance not in SUPPORT_STANCES or right_stance not in BLOCKING_STANCES:
            return []
        return [
            Contradiction(
                case_id=case_id,
                between=[str(left.get("agent_id") or ""), str(right.get("agent_id") or "")],
                type=contradiction_type,
                question=self._build_question(
                    contradiction_type=contradiction_type,
                    left=left,
                    right=right,
                    bull_payload=bull_payload,
                    bear_payload=bear_payload,
                    uncertainty_payload=uncertainty_payload,
                ),
                must_resolve_before_round_2=True,
                evidence_refs=self._build_evidence_refs(bull_payload, bear_payload, uncertainty_payload),
            )
        ]

    def _detect_case_level_conflict(
        self,
        *,
        case_id: str,
        latest_opinions: dict[str, dict[str, Any]],
        bull_payload: dict[str, Any],
        bear_payload: dict[str, Any],
        uncertainty_payload: dict[str, Any],
    ) -> list[Contradiction]:
        supporters = [
            payload for payload in latest_opinions.values()
            if self._stance(payload) in SUPPORT_STANCES
        ]
        blockers = [
            payload for payload in latest_opinions.values()
            if self._stance(payload) in BLOCKING_STANCES
        ]
        if not supporters or not blockers:
            return []
        left = supporters[0]
        right = blockers[0]
        return [
            Contradiction(
                case_id=case_id,
                between=[str(left.get("agent_id") or ""), str(right.get("agent_id") or "")],
                type="case_stance_conflict",
                question=self._build_question(
                    contradiction_type="case_stance_conflict",
                    left=left,
                    right=right,
                    bull_payload=bull_payload,
                    bear_payload=bear_payload,
                    uncertainty_payload=uncertainty_payload,
                ),
                must_resolve_before_round_2=True,
                evidence_refs=self._build_evidence_refs(bull_payload, bear_payload, uncertainty_payload),
            )
        ]

    @staticmethod
    def _stance(opinion: dict[str, Any]) -> str:
        return str(opinion.get("stance") or "").strip().lower()

    def _build_question(
        self,
        *,
        contradiction_type: str,
        left: dict[str, Any],
        right: dict[str, Any],
        bull_payload: dict[str, Any],
        bear_payload: dict[str, Any],
        uncertainty_payload: dict[str, Any],
    ) -> str:
        left_label = DISCUSSION_AGENT_LABELS.get(str(left.get("agent_id") or ""), str(left.get("agent_id") or "左侧"))
        right_label = DISCUSSION_AGENT_LABELS.get(str(right.get("agent_id") or ""), str(right.get("agent_id") or "右侧"))
        bull_fact = self._first_evidence(
            bull_payload,
            preferred_key="key_facts",
            fallback_key="thesis",
            fallback_text="多头证据待补充",
        )
        bear_risk = self._first_evidence(
            bear_payload,
            preferred_key="key_risks",
            fallback_key="thesis",
            fallback_text="空头风险待补充",
        )
        unknown = self._first_uncertainty(uncertainty_payload)
        if contradiction_type == "research_support_vs_risk_gate":
            return (
                f"{left_label}支持而{right_label}仍阻断。请说明多头证据“{bull_fact}”为何足以覆盖空头风险“{bear_risk}”，"
                f"以及未知项“{unknown}”若未被消除，是否必须维持阻断立场？"
            )
        if contradiction_type == "strategy_support_vs_audit_gate":
            return (
                f"{left_label}认为可执行，但{right_label}仍不放行。请说明战法依据“{bull_fact}”是否真的成立，"
                f"若风险“{bear_risk}”未被证伪、且未知项“{unknown}”仍存在，当前执行结论是否必须推翻？"
            )
        return (
            f"当前 case 出现明显立场冲突。请围绕多头事实“{bull_fact}”、空头风险“{bear_risk}”与未知项“{unknown}”给出统一判断："
            f"哪条证据一旦失效就必须改判？"
        )

    def _summary_line(
        self,
        contradiction: Contradiction,
        *,
        bull_payload: dict[str, Any],
        bear_payload: dict[str, Any],
    ) -> str:
        bull_fact = self._first_evidence(
            bull_payload,
            preferred_key="key_facts",
            fallback_key="thesis",
            fallback_text="多头证据待补充",
        )
        bear_risk = self._first_evidence(
            bear_payload,
            preferred_key="key_risks",
            fallback_key="thesis",
            fallback_text="空头风险待补充",
        )
        between = " vs ".join(DISCUSSION_AGENT_LABELS.get(item, item) for item in contradiction.between if item)
        return f"{between or contradiction.type} 存在矛盾：多头证据“{bull_fact}”，空头风险“{bear_risk}”，Round 2 必须回应。"

    def _build_evidence_refs(
        self,
        bull_payload: dict[str, Any],
        bear_payload: dict[str, Any],
        uncertainty_payload: dict[str, Any],
    ) -> list[str]:
        refs = [
            f"bull_case:{self._first_evidence(bull_payload, preferred_key='key_facts', fallback_key='thesis', fallback_text='多头证据待补充')}",
            f"bear_case:{self._first_evidence(bear_payload, preferred_key='key_risks', fallback_key='thesis', fallback_text='空头风险待补充')}",
            f"uncertainty:{self._first_uncertainty(uncertainty_payload)}",
        ]
        return self._dedupe(refs)

    def _first_evidence(
        self,
        payload: dict[str, Any],
        *,
        preferred_key: str,
        fallback_key: str,
        fallback_text: str,
    ) -> str:
        items = list(payload.get(preferred_key) or [])
        for item in items:
            text = str(item or "").strip()
            if text:
                return text
        fallback = str(payload.get(fallback_key) or "").strip()
        return fallback or fallback_text

    def _first_uncertainty(self, payload: dict[str, Any]) -> str:
        for key in ("key_unknowns", "data_gaps"):
            items = list(payload.get(key) or [])
            for item in items:
                text = str(item or "").strip()
                if text:
                    return text
        thesis = str(payload.get("thesis") or "").strip()
        return thesis or "当前未知项待补充"

    @staticmethod
    def _dedupe(items: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in items:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    # ── 证据层矛盾检测（v1.0 新增） ──────────────────────

    # 关键词对立表：当两个 agent 的 key_evidence 中同时出现对立词时触发
    EVIDENCE_CONFLICT_PAIRS: list[tuple[str, str]] = [
        ("放量", "缩量"),
        ("突破", "假突破"),
        ("趋势", "震荡"),
        ("加速", "衰竭"),
        ("板块共振", "板块退潮"),
        ("资金流入", "资金流出"),
        ("封板稳固", "炸板"),
        ("高开", "低开"),
        ("超预期", "不及预期"),
    ]

    def detect_evidence_conflicts(
        self,
        case_id: str,
        opinions: list[dict[str, Any]],
    ) -> list[Contradiction]:
        """检测 opinions 之间的证据层矛盾。

        与 stance 矛盾不同，证据矛盾发生在两个 agent 对**同一事实**描述对立：
        例如 research 说 "放量突破"，risk 说 "缩量假突破"，
        即使 stance 都是 watch，逻辑上也是互斥的。

        Args:
            case_id: 讨论 case ID
            opinions: opinion 列表（dict 形式）

        Returns:
            检测到的证据矛盾列表

        TODO:
            1. 引入 embedding 相似度做更精准的语义对立检测
            2. 从 self_evolve 的历史教训中动态扩展对立表
        """
        contradictions: list[Contradiction] = []

        # 收集每个 agent 的 key_evidence
        agent_evidence: dict[str, list[str]] = {}
        for opinion in opinions:
            payload = self._payload(opinion)
            agent_id = str(payload.get("agent_id", ""))
            evidence = list(payload.get("key_evidence", []) or [])
            # 也检查 reasoning / rationale 字段
            reasoning = str(payload.get("reasoning", "") or "")
            rationale = str(payload.get("rationale", "") or "")
            all_text = [str(e) for e in evidence] + ([reasoning] if reasoning else []) + ([rationale] if rationale else [])
            if agent_id and all_text:
                agent_evidence[agent_id] = all_text

        # 两两比对
        agents = list(agent_evidence.keys())
        for i in range(len(agents)):
            for j in range(i + 1, len(agents)):
                a1, a2 = agents[i], agents[j]
                text_a = " ".join(agent_evidence[a1])
                text_b = " ".join(agent_evidence[a2])
                for word_a, word_b in self.EVIDENCE_CONFLICT_PAIRS:
                    a_has_first = word_a in text_a and word_b not in text_a
                    b_has_second = word_b in text_b and word_a not in text_b
                    a_has_second = word_b in text_a and word_a not in text_a
                    b_has_first = word_a in text_b and word_b not in text_b
                    if (a_has_first and b_has_second) or (a_has_second and b_has_first):
                        label_a = DISCUSSION_AGENT_LABELS.get(a1, a1)
                        label_b = DISCUSSION_AGENT_LABELS.get(a2, a2)
                        contradictions.append(Contradiction(
                            case_id=case_id,
                            between=[a1, a2],
                            type="evidence_conflict",
                            question=(
                                f"{label_a}认为「{word_a}」，"
                                f"{label_b}认为「{word_b}」，"
                                f"事实判断互斥，需要补充数据澄清。"
                            ),
                            must_resolve_before_round_2=True,
                            evidence_refs=[word_a, word_b],
                        ))
        return contradictions
