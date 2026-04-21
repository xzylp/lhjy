from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ashare_system.data.catalog_service import CatalogService
from ashare_system.data.control_db import ControlPlaneDB
from ashare_system.data.document_index import DocumentIndexService
from ashare_system.data.history_ingest import HistoryIngestService
from ashare_system.data.history_store import HistoryStore
from ashare_system.infra.audit_store import StateStore
from ashare_system.infra.market_adapter import MockMarketDataAdapter


class ControlPlaneDataLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = ControlPlaneDB(self.root / "db" / "control_plane.sqlite3")
        self.catalog = CatalogService(self.db)
        self.catalog.ensure_default_catalog()
        self.index = DocumentIndexService(self.db, self.catalog)
        self.history_store = HistoryStore(self.root, self.db, self.catalog)
        self.research_state_store = StateStore(self.root / "research_state.json")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_document_index_supports_workspace_search(self) -> None:
        doc_path = self.root / "docs" / "sample_note.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("# 整改记录\n\n这是一份用于验证全文检索的整改说明。", encoding="utf-8")

        indexed = self.index.index_markdown_file(doc_path)
        self.assertTrue(indexed)

        results = self.index.search("整改", limit=5)
        self.assertEqual(len(results), 1)
        self.assertIn("整改记录", results[0]["title"])
        self.assertTrue(self.db.fts_enabled)

    def test_history_ingest_persists_daily_bars_and_behavior_profiles(self) -> None:
        self.research_state_store.set(
            "latest_stock_behavior_profiles",
            {
                "trade_date": "2026-04-21",
                "generated_at": "2026-04-21T16:00:00",
                "source_counts": {"runtime": 1},
                "items": [
                    {
                        "symbol": "600519.SH",
                        "source": "runtime",
                        "profile_trade_date": "2026-04-21",
                        "profile": {
                            "symbol": "600519.SH",
                            "board_success_rate_20d": 0.62,
                            "bomb_rate_20d": 0.12,
                            "next_day_premium_20d": 1.8,
                            "reseal_rate_20d": 0.24,
                            "optimal_hold_days": 2,
                            "style_tag": "leader",
                            "avg_sector_rank_30d": 2.6,
                            "leader_frequency_30d": 0.41,
                        },
                    }
                ],
            },
        )
        service = HistoryIngestService(
            market_adapter=MockMarketDataAdapter(),
            history_store=self.history_store,
            catalog_service=self.catalog,
            research_state_store=self.research_state_store,
        )

        bars_result = service.ingest_daily_bars(
            symbols=["600519.SH", "000001.SZ"],
            trade_date="2026-04-21",
            count=3,
        )
        self.assertEqual(bars_result["trade_date"], "2026-04-21")
        self.assertEqual(bars_result["partition_count"], 3)
        self.assertEqual(len(bars_result["trade_dates"]), 3)
        self.assertTrue(Path(bars_result["latest_path"]).exists())
        self.assertIn(bars_result["partitions"][0]["file_format"], {"jsonl", "parquet"})

        profile_result = service.sync_behavior_profiles()
        self.assertEqual(profile_result["trade_date"], "2026-04-21")
        summary = self.history_store.get_stock_summary("600519.SH")
        self.assertIsNotNone(summary)
        self.assertIn("封板率", str(summary["summary"]))
        bars_payload = self.history_store.read_bars(symbols=["600519.SH"], period="1d", limit=2)
        self.assertEqual(len(bars_payload["by_symbol"]["600519.SH"]), 2)

        catalog_snapshot = self.catalog.build_health_snapshot()
        dataset_names = {item["dataset_name"] for item in catalog_snapshot["datasets"]}
        self.assertIn("bars_1d", dataset_names)
        self.assertIn("stock_behavior_profiles", dataset_names)

    def test_history_store_uses_distinct_files_for_same_trade_date_batches(self) -> None:
        first = self.history_store.write_records(
            dataset_name="bars_1d",
            trade_date="2026-04-21",
            period="1d",
            records=[
                {"symbol": "000001.SZ", "trade_time": "2026-04-21 00:00:00", "close": 10.0},
                {"symbol": "000002.SZ", "trade_time": "2026-04-21 00:00:00", "close": 11.0},
            ],
            source="batch_1",
        )
        second = self.history_store.write_records(
            dataset_name="bars_1d",
            trade_date="2026-04-21",
            period="1d",
            records=[
                {"symbol": "000004.SZ", "trade_time": "2026-04-21 00:00:00", "close": 12.0},
                {"symbol": "000006.SZ", "trade_time": "2026-04-21 00:00:00", "close": 13.0},
            ],
            source="batch_2",
        )
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(Path(first["path"]).exists())
        self.assertTrue(Path(second["path"]).exists())
        partitions = self.catalog.list_partitions("bars_1d", limit=10)
        paths = {item["path"] for item in partitions}
        self.assertIn(first["path"], paths)
        self.assertIn(second["path"], paths)

    def test_history_context_summarizes_daily_and_behavior_data(self) -> None:
        service = HistoryIngestService(
            market_adapter=MockMarketDataAdapter(),
            history_store=self.history_store,
            catalog_service=self.catalog,
            research_state_store=self.research_state_store,
        )
        service.ingest_daily_bars(
            symbols=["000001.SZ"],
            trade_date="2026-04-21",
            count=5,
        )
        self.research_state_store.set(
            "latest_stock_behavior_profiles",
            {
                "trade_date": "2026-04-21",
                "generated_at": "2026-04-21T16:00:00",
                "source_counts": {"runtime": 1},
                "items": [
                    {
                        "symbol": "000001.SZ",
                        "source": "runtime",
                        "profile_trade_date": "2026-04-21",
                        "profile": {
                            "symbol": "000001.SZ",
                            "board_success_rate_20d": 0.32,
                            "bomb_rate_20d": 0.08,
                            "next_day_premium_20d": 0.9,
                            "reseal_rate_20d": 0.15,
                            "optimal_hold_days": 2,
                            "style_tag": "trend",
                            "avg_sector_rank_30d": 6.2,
                            "leader_frequency_30d": 0.18,
                        },
                    }
                ],
            },
        )
        service.sync_behavior_profiles()
        payload = self.history_store.build_history_context(symbols=["000001.SZ"], trade_date="2026-04-21")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["symbol_count"], 1)
        self.assertIn("000001.SZ 历史摘要", payload["summary_lines"][0])


if __name__ == "__main__":
    unittest.main()
