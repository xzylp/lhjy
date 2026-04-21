from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ashare_system.infra.audit_store import StateStore
from ashare_system.precompute import DossierPrecomputeService
from ashare_system.settings import load_settings


class _DummyMarketAdapter:
    def get_snapshots(self, symbols):  # pragma: no cover - not used in this test
        return []


class PrecomputeTradeDateTests(unittest.TestCase):
    def test_resolve_trade_date_uses_current_day_when_latest_runtime_report_is_previous_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_env = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_WORKSPACE",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = str(Path(tmp_dir) / "storage")
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_WORKSPACE"] = tmp_dir

            try:
                settings = load_settings()
                runtime_state = StateStore(Path(tmp_dir) / "runtime_state.json")
                runtime_state.set(
                    "latest_runtime_report",
                    {
                        "generated_at": "2026-04-19T11:55:44.945147",
                    },
                )
                research_state = StateStore(Path(tmp_dir) / "research_state.json")

                service = DossierPrecomputeService(
                    settings=settings,
                    market_adapter=_DummyMarketAdapter(),
                    research_state_store=research_state,
                    runtime_state_store=runtime_state,
                    now_factory=lambda: datetime(2026, 4, 20, 9, 0, 0),
                )

                self.assertEqual(service._resolve_trade_date(), "2026-04-20")
                self.assertEqual(service._resolve_trade_date("2026-04-19T08:00:00"), "2026-04-19")
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
