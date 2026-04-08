from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ashare_system.apps.research_api import build_router
from ashare_system.infra.audit_store import AuditStore, StateStore
from ashare_system.settings import load_settings


class ResearchApiTests(unittest.TestCase):
    def test_news_event_deduplicates_and_persists_extended_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings = load_settings()
            settings.storage_root = Path(tmp_dir)
            app = FastAPI()
            app.include_router(
                build_router(
                    settings=settings,
                    audit_store=AuditStore(Path(tmp_dir) / "audits.json"),
                    research_state_store=StateStore(Path(tmp_dir) / "research_state.json"),
                )
            )
            client = TestClient(app)

            payload = {
                "symbol": "600519.SH",
                "name": "贵州茅台",
                "title": "白酒板块异动",
                "summary": "盘中出现放量拉升。",
                "sentiment": "positive",
                "source": "manual",
                "source_type": "newswire",
                "severity": "warning",
                "impact_scope": "sector",
                "evidence_url": "https://example.com/news/1",
                "dedupe_key": "news|600519.SH|manual|白酒板块异动|2026-04-05T09:35:00+08:00",
                "tags": ["white-liquor", "intraday"],
                "event_time": "2026-04-05T09:35:00+08:00",
            }

            first = client.post("/research/events/news", json=payload)
            second = client.post("/research/events/news", json=payload)
            summary = client.get("/research/summary").json()

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(summary["news_count"], 1)
            latest = summary["latest_news"][-1]
            self.assertEqual(latest["source_type"], "newswire")
            self.assertEqual(latest["impact_scope"], "sector")
            self.assertEqual(latest["payload"]["tags"], ["white-liquor", "intraday"])
            self.assertIn(latest["staleness_level"], {"fresh", "warm", "stale"})


if __name__ == "__main__":
    unittest.main()
