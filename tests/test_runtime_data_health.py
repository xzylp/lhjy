from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ashare_system.app import create_app
from ashare_system.container import reset_container
from ashare_system.scheduler import PRE_MARKET_TASKS


class RuntimeDataHealthTests(unittest.TestCase):
    def test_runtime_exposes_data_health_and_kline_cache_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = str(Path(tmp_dir) / "storage")
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            reset_container()
            try:
                with TestClient(create_app()) as client:
                    health = client.get("/runtime/data/health")
                    self.assertEqual(health.status_code, 200)
                    self.assertIn("gateway_health", health.json())
                    self.assertIn("kline_freshness", health.json())
                    self.assertIn("universe_coverage", health.json())

                    stats = client.get("/runtime/cache/kline/stats")
                    self.assertEqual(stats.status_code, 200)
                    self.assertIn("hit_rate", stats.json())
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_scheduler_includes_data_freshness_check_task(self) -> None:
        handlers = {task.handler for task in PRE_MARKET_TASKS}
        self.assertIn("data.freshness:check", handlers)


if __name__ == "__main__":
    unittest.main()
