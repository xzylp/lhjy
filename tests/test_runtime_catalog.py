from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ashare_system.app import create_app
from ashare_system.container import reset_container


class RuntimeCatalogTests(unittest.TestCase):
    def test_runtime_factor_and_playbook_catalog_endpoints_expose_seed_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                with TestClient(create_app()) as client:
                    factors_payload = client.get("/runtime/factors").json()
                    self.assertGreaterEqual(len(factors_payload["items"]), 76)
                    self.assertLessEqual(len(factors_payload["items"]), 90)
                    factor_ids = {item["id"] for item in factors_payload["items"]}
                    self.assertIn("momentum_slope", factor_ids)
                    self.assertIn("sector_heat_score", factor_ids)
                    self.assertIn("portfolio_fit_score", factor_ids)
                    self.assertTrue(all("effectiveness_status" in item for item in factors_payload["items"]))

                    playbooks_payload = client.get("/runtime/playbooks").json()
                    self.assertGreaterEqual(len(playbooks_payload["items"]), 18)
                    self.assertLessEqual(len(playbooks_payload["items"]), 24)
                    playbook_ids = {item["id"] for item in playbooks_payload["items"]}
                    self.assertIn("sector_resonance", playbook_ids)
                    self.assertIn("trend_acceleration", playbook_ids)
                    self.assertIn("position_trim_redeploy", playbook_ids)

                    capabilities_payload = client.get("/runtime/capabilities").json()
                    self.assertIn("factor_effectiveness", capabilities_payload)
                    factor_effectiveness = capabilities_payload["factor_effectiveness"]
                    self.assertIn(factor_effectiveness.get("status", "ready"), {"ready", "not_ready"})

                    ingest_payload = client.post(
                        "/runtime/history/ingest/daily",
                        params={"symbols": "000001.SZ,600519.SH", "trade_date": "2026-04-21", "count": 4},
                    ).json()
                    self.assertGreaterEqual(ingest_payload.get("partition_count", 0), 4)

                    bars_payload = client.get(
                        "/runtime/history/bars",
                        params={"symbol": "000001.SZ", "period": "1d", "limit": 2},
                    ).json()
                    self.assertTrue(bars_payload["ok"])
                    self.assertEqual(len(bars_payload["by_symbol"]["000001.SZ"]), 2)

                    latest_payload = client.get(
                        "/runtime/history/bars/latest",
                        params={"symbols": "000001.SZ,600519.SH", "period": "1d"},
                    ).json()
                    self.assertEqual(latest_payload["count"], 2)

                    symbol_search_payload = client.get(
                        "/runtime/history/search-symbol",
                        params={"q": "平安", "limit": 5},
                    ).json()
                    self.assertTrue(symbol_search_payload["ok"])
                    self.assertGreaterEqual(symbol_search_payload["count"], 1)

                    compose_payload = client.post(
                        "/runtime/jobs/compose-from-brief",
                        json={
                            "objective": "测试历史上下文注入",
                            "market_hypothesis": "关注银行股日内机会",
                            "symbol_pool": ["000001.SZ"],
                            "playbooks": ["sector_resonance"],
                            "factors": ["momentum_slope"],
                        },
                    ).json()
                    self.assertTrue(compose_payload["ok"])
                    history_context = compose_payload["compose_request"]["market_context"].get("history_context", {})
                    self.assertTrue(history_context.get("available"))
                    self.assertEqual(history_context.get("symbol_count"), 1)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()


if __name__ == "__main__":
    unittest.main()
