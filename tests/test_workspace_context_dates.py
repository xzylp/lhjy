from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ashare_system.data.archive import DataArchiveStore
from ashare_system.data.serving import ServingStore


class WorkspaceContextTradeDateTests(unittest.TestCase):
    def test_market_and_event_context_backfill_trade_date_and_refresh_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_root = Path(temp_dir)
            archive_store = DataArchiveStore(storage_root)
            serving_store = ServingStore(storage_root)

            archive_store.persist_runtime_context(
                "2026-04-19",
                {
                    "resource": "runtime_context",
                    "trade_date": "2026-04-19",
                    "generated_at": "2026-04-19T11:55:44",
                    "job_id": "runtime-old",
                },
            )

            archive_store.persist_market_context(
                "2026-04-20",
                {
                    "resource": "market_context",
                    "generated_at": "2026-04-20T07:30:07",
                    "summary_lines": ["上证指数 0.50%"],
                },
            )
            archive_store.persist_event_context(
                "2026-04-20",
                {
                    "resource": "event_context",
                    "generated_at": "2026-04-20T07:30:08",
                    "summary_lines": ["盘前新闻同步完成"],
                },
            )

            latest_market_context = serving_store.get_latest_market_context()
            latest_event_context = serving_store.get_latest_event_context()
            latest_workspace_context = serving_store.get_latest_workspace_context()

            self.assertIsNotNone(latest_market_context)
            self.assertIsNotNone(latest_event_context)
            self.assertIsNotNone(latest_workspace_context)
            self.assertEqual(latest_market_context["trade_date"], "2026-04-20")
            self.assertEqual(latest_event_context["trade_date"], "2026-04-20")
            self.assertEqual(latest_workspace_context["trade_date"], "2026-04-20")
            self.assertEqual(latest_workspace_context["market_context"]["trade_date"], "2026-04-20")
            self.assertEqual(latest_workspace_context["event_context"]["trade_date"], "2026-04-20")
            self.assertIsNone(latest_workspace_context.get("runtime_context"))


if __name__ == "__main__":
    unittest.main()
