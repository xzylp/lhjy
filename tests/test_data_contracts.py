from __future__ import annotations

import unittest

from ashare_system.data.contracts import (
    DossierRecord,
    EventRecord,
    IndexSnapshotRecord,
    MarketBarRecord,
    MarketSnapshotRecord,
    MarketStructureSnapshotRecord,
)


class DataContractTests(unittest.TestCase):
    def test_market_snapshot_record_carries_freshness_metadata(self) -> None:
        record = MarketSnapshotRecord(
            symbol="600519.SH",
            name="贵州茅台",
            snapshot_at="2026-04-05T09:35:00+08:00",
            last_price=1500.0,
            bid_price=1499.8,
            ask_price=1500.2,
            source_at="2026-04-05T09:35:00+08:00",
            fetched_at="2026-04-05T09:35:01+08:00",
            generated_at="2026-04-05T09:35:02+08:00",
            expires_at="2026-04-05T09:36:00+08:00",
            staleness_level="fresh",
        )
        self.assertEqual(record.symbol, "600519.SH")
        self.assertEqual(record.staleness_level, "fresh")
        self.assertEqual(record.name, "贵州茅台")

    def test_event_record_supports_scope_and_payload(self) -> None:
        record = EventRecord(
            event_id="evt-1",
            symbol="600519.SH",
            name="贵州茅台",
            source="manual",
            source_type="announcement",
            category="announcement",
            title="年度报告披露",
            summary="披露年度经营数据。",
            event_at="2026-04-05T08:00:00+08:00",
            impact_scope="symbol",
            payload={"importance": "high"},
            staleness_level="warm",
        )
        self.assertEqual(record.impact_scope, "symbol")
        self.assertEqual(record.payload["importance"], "high")
        self.assertEqual(record.staleness_level, "warm")

    def test_history_and_context_records_cover_market_and_dossier(self) -> None:
        bar = MarketBarRecord(
            symbol="000001.SZ",
            period="1d",
            trade_time="2026-04-03",
            open=10.0,
            high=10.5,
            low=9.9,
            close=10.2,
            source_at="2026-04-03T15:00:00+08:00",
        )
        index_snapshot = IndexSnapshotRecord(
            index_symbol="000001.SH",
            index_name="上证指数",
            snapshot_at="2026-04-05T10:00:00+08:00",
            last_price=3100.0,
        )
        structure_snapshot = MarketStructureSnapshotRecord(
            snapshot_at="2026-04-05T10:00:00+08:00",
            limit_up_count=45,
            turnover_total=820000000000.0,
        )
        dossier = DossierRecord(
            trade_date="2026-04-05",
            symbol="000001.SZ",
            name="平安银行",
            signature="2026-04-05:candidate_pool:000001.SZ",
            payload={"market_context": {}, "event_context": {}},
            staleness_level="fresh",
        )
        self.assertEqual(bar.period, "1d")
        self.assertEqual(index_snapshot.index_name, "上证指数")
        self.assertEqual(structure_snapshot.limit_up_count, 45)
        self.assertEqual(dossier.payload["market_context"], {})


if __name__ == "__main__":
    unittest.main()
