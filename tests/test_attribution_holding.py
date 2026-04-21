from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ashare_system.learning.attribution import TradeAttributionRecord, TradeAttributionService


class AttributionHoldingTests(unittest.TestCase):
    def test_backfill_holding_outcomes_updates_report_metric(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = TradeAttributionService(Path(tmp_dir) / "trade_attribution.json")
            service.record_outcomes(
                trade_date="2026-04-18",
                score_date="2026-04-19",
                items=[
                    TradeAttributionRecord(
                        trade_date="2026-04-18",
                        score_date="2026-04-19",
                        symbol="600519.SH",
                        playbook="trend_acceleration",
                        regime="trend",
                        next_day_close_pct=-0.03,
                        recorded_at="2026-04-19T16:00:00",
                    )
                ],
            )
            report_before = service.build_report(trade_date="2026-04-18", score_date="2026-04-19", use_holding_return=False)
            self.assertLess(report_before.avg_next_day_close_pct, 0.0)

            report_after = service.backfill_holding_outcomes(
                [
                    {
                        "trade_date": "2026-04-18",
                        "score_date": "2026-04-19",
                        "symbol": "600519.SH",
                        "holding_return_pct": 0.08,
                        "max_drawdown_during_hold": -0.04,
                        "exit_price": 108.0,
                    }
                ]
            )
            self.assertGreater(report_after.avg_next_day_close_pct, 0.0)
            self.assertEqual(report_after.win_rate, 1.0)
            self.assertEqual(report_after.items[0].exit_price, 108.0)


if __name__ == "__main__":
    unittest.main()
