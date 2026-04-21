from __future__ import annotations

import unittest

from ashare_system.hermes.model_router import HermesModelRouter
from ashare_system.settings import HermesSettings


class HermesModelRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = HermesModelRouter(HermesSettings())

    def test_high_risk_execution_routes_to_guard_model(self) -> None:
        result = self.router.resolve(role="execution_operator", task_kind="execution", risk_level="high")
        self.assertEqual(result["slot"]["id"], "execution-guard")
        self.assertEqual(result["slot"]["provider_id"], "compat-gpt54")

    def test_fast_watch_routes_to_fastlane(self) -> None:
        result = self.router.resolve(role="cron_intraday_watch", task_kind="watch", risk_level="low", prefer_fast=True)
        self.assertEqual(result["slot"]["id"], "ops-fastlane")
        self.assertEqual(result["slot"]["provider_id"], "minimax")

    def test_deep_strategy_routes_to_research_slot(self) -> None:
        result = self.router.resolve(role="strategy_analyst", task_kind="strategy", risk_level="medium", require_deep_reasoning=True)
        self.assertEqual(result["slot"]["id"], "research-deep-dive")
        self.assertEqual(result["slot"]["provider_id"], "compat-gpt54")


if __name__ == "__main__":
    unittest.main()
