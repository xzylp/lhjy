from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ashare_system.discussion.candidate_case import CandidateCase, CandidateOpinion, CandidateRuntimeSnapshot
from ashare_system.learning.agent_rating import AgentRatingService
from ashare_system.learning.score_state import AgentScoreService
from ashare_system.learning.settlement import AgentScoreSettlementService, SettlementSymbolOutcome


def _build_case(symbol: str, *, trade_date: str = "2026-04-18") -> CandidateCase:
    return CandidateCase(
        case_id=f"case-{symbol}",
        trade_date=trade_date,
        symbol=symbol,
        runtime_snapshot=CandidateRuntimeSnapshot(rank=1, selection_score=1.0, action="BUY"),
        final_status="selected",
        risk_gate="allow",
        audit_gate="clear",
        opinions=[
            CandidateOpinion(
                round=1,
                agent_id="ashare-strategy",
                stance="selected",
                confidence="high",
                recorded_at="2026-04-18T10:00:00",
            ),
            CandidateOpinion(
                round=1,
                agent_id="ashare-risk",
                stance="selected",
                confidence="medium",
                recorded_at="2026-04-18T10:00:00",
            ),
        ],
        updated_at="2026-04-18T10:00:00",
    )


class SettlementConfidenceTests(unittest.TestCase):
    def test_settlement_service_marks_insufficient_sample(self) -> None:
        service = AgentScoreSettlementService()
        cases = [_build_case("600519.SH"), _build_case("000001.SZ")]
        outcomes = {
            "600519.SH": SettlementSymbolOutcome(symbol="600519.SH", next_day_close_pct=0.03),
            "000001.SZ": SettlementSymbolOutcome(symbol="000001.SZ", next_day_close_pct=-0.01),
        }
        results = service.settle(cases, outcomes, min_sample_count=5)
        self.assertTrue(results)
        self.assertTrue(all(item.insufficient_sample for item in results))
        self.assertTrue(all(item.sample_count == 2 for item in results))
        self.assertTrue(all(item.result_score_delta == 0.0 for item in results))

    def test_agent_rating_applies_confidence_decay_and_same_day_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AgentRatingService(Path(tmp_dir) / "agent_ratings.json")
            low = service.apply_delta("ashare-research", 2.0, confidence_tier="low")
            high = service.apply_delta("ashare-strategy", 2.0, confidence_tier="high")
            self.assertGreater(high.rating - 1000.0, low.rating - 1000.0)

            first = service.apply_delta("ashare-audit", 1.2, confidence_tier="medium")
            second = service.apply_delta("ashare-audit", 1.2, confidence_tier="medium")
            self.assertEqual(first.settled_matches, 1)
            self.assertEqual(second.settled_matches, 1)
            self.assertGreaterEqual(second.rating, first.rating)

    def test_agent_score_service_uses_settlement_key_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AgentScoreService(Path(tmp_dir) / "agent_score_states.json")
            first = service.record_settlement(
                agent_id="ashare-strategy",
                score_date="2026-04-18",
                result_score_delta=1.2,
                settlement_key="disc:abc:2026-04-18",
                confidence_tier="high",
            )
            second = service.record_settlement(
                agent_id="ashare-strategy",
                score_date="2026-04-18",
                result_score_delta=1.2,
                settlement_key="disc:abc:2026-04-18",
                confidence_tier="high",
            )
            self.assertFalse(first.already_applied)
            self.assertTrue(second.already_applied)
            listed = service.list_scores("2026-04-18")[0]
            self.assertEqual(listed.settled_matches, 1)


if __name__ == "__main__":
    unittest.main()
