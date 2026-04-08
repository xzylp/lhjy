"""strategy/runtime 对外 dossier API 测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ashare_system.app import create_app
from ashare_system.container import reset_container


class StrategyRuntimeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        os.environ["ASHARE_STORAGE_ROOT"] = str(root / "state")
        os.environ["ASHARE_LOGS_DIR"] = str(root / "logs")
        os.environ["ASHARE_WORKSPACE"] = str(root)
        os.environ["ASHARE_MARKET_MODE"] = "mock"
        os.environ["ASHARE_EXECUTION_MODE"] = "mock"
        os.environ["ASHARE_RUN_MODE"] = "paper"
        os.environ["ASHARE_LIVE_ENABLE"] = "false"
        reset_container()
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        reset_container()
        self._tmpdir.cleanup()

    def _prepare_runtime_pipeline(self) -> dict:
        response = self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("generated_at", payload)
        return payload

    def test_strategy_context_latest_returns_candidate_digest(self) -> None:
        runtime_payload = self._prepare_runtime_pipeline()

        response = self.client.get("/strategy/context/latest")
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["trade_date"], runtime_payload["generated_at"][:10])
        self.assertIn("regime", payload)
        self.assertIn(payload["regime"]["value"], {"trend", "rotation", "defensive", "chaos"})
        self.assertIn("active_strategies", payload)
        self.assertGreaterEqual(len(payload["active_strategies"]), 1)
        self.assertIn("candidate_count", payload)
        self.assertGreaterEqual(payload["candidate_count"], 1)
        self.assertIn("candidates", payload)
        self.assertGreaterEqual(len(payload["candidates"]), 1)
        self.assertIn("symbol", payload["candidates"][0])
        self.assertIn("sector_tags", payload["candidates"][0])
        self.assertIn("playbook", payload["candidates"][0])
        self.assertIn("resolved_sector", payload["candidates"][0])
        self.assertIn("playbook_contexts", payload)
        self.assertGreaterEqual(len(payload["playbook_contexts"]), 1)
        self.assertIn("behavior_profiles", payload)
        self.assertGreaterEqual(len(payload["behavior_profiles"]), 1)
        self.assertIn("sector_profiles", payload)
        self.assertGreaterEqual(len(payload["sector_profiles"]), 1)
        self.assertIn("gaps", payload)
        self.assertIn("runtime_context_ref", payload)
        self.assertIn("dossier_ref", payload)
        self.assertNotIn("market_regime_not_persisted", payload["gaps"])
        self.assertNotIn("playbook_assignment_not_persisted", payload["gaps"])

    def test_strategy_candidates_latest_returns_hot_sector_summary(self) -> None:
        self._prepare_runtime_pipeline()

        response = self.client.get("/strategy/candidates/latest")
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["available"])
        self.assertIn("items", payload)
        self.assertGreaterEqual(len(payload["items"]), 1)
        self.assertIn("hot_sectors", payload)
        self.assertGreaterEqual(len(payload["hot_sectors"]), 1)
        self.assertIn("sector_profiles", payload)
        self.assertGreaterEqual(len(payload["sector_profiles"]), 1)
        self.assertIn("playbook_contexts", payload)
        self.assertGreaterEqual(len(payload["playbook_contexts"]), 1)
        self.assertIn("behavior_profiles", payload)
        self.assertGreaterEqual(len(payload["behavior_profiles"]), 1)
        self.assertIn("regime", payload)
        self.assertIn("allowed_playbooks", payload)
        self.assertIn("gaps", payload)
        self.assertIn("style_tag", payload["items"][0])
        self.assertIn("optimal_hold_days", payload["items"][0])

    def test_runtime_context_and_dossier_endpoints_return_serving_payloads(self) -> None:
        runtime_payload = self._prepare_runtime_pipeline()
        first_symbol = runtime_payload["top_picks"][0]["symbol"]
        trade_date = runtime_payload["generated_at"][:10]

        runtime_context = self.client.get("/runtime/context/latest").json()
        self.assertTrue(runtime_context["available"])
        self.assertEqual(runtime_context["resource"], "runtime_context")
        self.assertEqual(runtime_context["trade_date"], trade_date)
        self.assertIn("market_profile", runtime_context)
        self.assertIn("regime", runtime_context["market_profile"])
        self.assertIn("source", runtime_context["market_profile"])
        self.assertIn("playbook_contexts", runtime_context)
        self.assertGreaterEqual(len(runtime_context["playbook_contexts"]), 1)
        self.assertIn("behavior_profiles", runtime_context)
        self.assertGreaterEqual(len(runtime_context["behavior_profiles"]), 1)
        self.assertIn("sector_profiles", runtime_context)
        self.assertGreaterEqual(len(runtime_context["sector_profiles"]), 1)

        dossier_pack = self.client.get("/runtime/dossiers/latest").json()
        self.assertTrue(dossier_pack["available"])
        self.assertEqual(dossier_pack["resource"], "runtime_dossiers")
        self.assertEqual(dossier_pack["trade_date"], trade_date)
        self.assertGreaterEqual(dossier_pack["symbol_count"], 1)
        self.assertIn("runtime_context", dossier_pack)
        self.assertIn("items", dossier_pack)
        self.assertGreaterEqual(len(dossier_pack["items"]), 1)
        self.assertIn("behavior_profiles", dossier_pack)
        self.assertGreaterEqual(len(dossier_pack["behavior_profiles"]), 1)

        dossier = self.client.get(f"/runtime/dossiers/{trade_date}/{first_symbol}").json()
        self.assertTrue(dossier["available"])
        self.assertEqual(dossier["trade_date"], trade_date)
        self.assertEqual(dossier["symbol"], first_symbol)
        self.assertIn("payload", dossier)
        self.assertIn("symbol_context", dossier["payload"])
        self.assertIn("playbook_context", dossier["payload"])
        self.assertIn("behavior_profile", dossier["payload"])
        self.assertIn("behavior_profile", dossier["payload"]["symbol_context"])
        self.assertIn("market_context", dossier["payload"])
        self.assertIn("regime", dossier["payload"]["market_context"])


if __name__ == "__main__":
    unittest.main()
