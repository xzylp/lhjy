from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ashare_system.contracts import BarSnapshot, QuoteSnapshot
from ashare_system.data.archive import DataArchiveStore


class DataArchiveStoreTests(unittest.TestCase):
    def test_persist_symbol_snapshot_and_bars_write_partition_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = DataArchiveStore(Path(tmp_dir))
            snapshots = [
                QuoteSnapshot(
                    symbol="600519.SH",
                    name="贵州茅台",
                    last_price=1500.0,
                    bid_price=1499.8,
                    ask_price=1500.2,
                    volume=1000.0,
                    pre_close=1490.0,
                )
            ]
            bars = [
                BarSnapshot(
                    symbol="600519.SH",
                    period="1d",
                    open=1490.0,
                    high=1510.0,
                    low=1485.0,
                    close=1500.0,
                    volume=1000.0,
                    amount=1500000.0,
                    trade_time="2026-04-05",
                    pre_close=1480.0,
                )
            ]
            generated_at = "2026-04-05T09:35:00+08:00"

            store.persist_symbol_snapshots(snapshots, generated_at=generated_at)
            store.persist_market_bars(bars, generated_at=generated_at, name_map={"600519.SH": "贵州茅台"})

            snapshot_file = Path(tmp_dir) / "normalized" / "market" / "symbol" / "snapshot" / "2026-04-05.jsonl"
            bars_file = Path(tmp_dir) / "normalized" / "market" / "symbol" / "1d" / "2026-04-05.jsonl"

            self.assertTrue(snapshot_file.exists())
            self.assertTrue(bars_file.exists())

            snapshot_rows = [json.loads(line) for line in snapshot_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            bar_rows = [json.loads(line) for line in bars_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(snapshot_rows[0]["symbol"], "600519.SH")
            self.assertEqual(bar_rows[0]["name"], "贵州茅台")

    def test_persist_dossier_pack_writes_feature_and_serving_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = DataArchiveStore(Path(tmp_dir))
            pack = {
                "pack_id": "dossier-20260405093500",
                "trade_date": "2026-04-05",
                "generated_at": "2026-04-05T09:35:00+08:00",
                "expires_at": "2026-04-05T09:40:00+08:00",
                "signature": "2026-04-05:candidate_pool:600519.SH",
                "items": [
                    {
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "rank": 1,
                        "selection_score": 88.0,
                    }
                ],
            }

            records = store.persist_dossier_pack(pack)

            feature_file = Path(tmp_dir) / "features" / "dossiers" / "2026-04-05" / "600519_SH.json"
            serving_file = Path(tmp_dir) / "serving" / "latest_dossier_pack.json"
            self.assertEqual(len(records), 1)
            self.assertTrue(feature_file.exists())
            self.assertTrue(serving_file.exists())

    def test_persist_symbol_contexts_writes_feature_and_serving_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = DataArchiveStore(Path(tmp_dir))

            records = store.persist_symbol_contexts(
                "2026-04-05",
                [
                    {
                        "trade_date": "2026-04-05",
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "generated_at": "2026-04-05T09:35:00+08:00",
                        "event_summary": {"total_related_event_count": 3},
                    }
                ],
                generated_at="2026-04-05T09:35:00+08:00",
                signature="2026-04-05:candidate_pool:600519.SH",
            )

            feature_file = Path(tmp_dir) / "features" / "symbol_context" / "2026-04-05" / "600519_SH.json"
            serving_file = Path(tmp_dir) / "serving" / "latest_symbol_contexts.json"

            self.assertEqual(len(records), 1)
            self.assertTrue(feature_file.exists())
            self.assertTrue(serving_file.exists())

            payload = json.loads(serving_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["symbol_count"], 1)
            self.assertEqual(payload["items"][0]["symbol"], "600519.SH")


if __name__ == "__main__":
    unittest.main()
