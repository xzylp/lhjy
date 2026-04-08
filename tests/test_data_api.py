from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ashare_system.apps.data_api import build_router
from ashare_system.settings import load_settings


class DataApiTests(unittest.TestCase):
    def test_data_api_reads_latest_serving_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            settings = load_settings()
            settings.storage_root = root

            serving_dir = root / "serving"
            feature_dossiers_dir = root / "features" / "dossiers" / "2026-04-05"
            feature_symbol_dir = root / "features" / "symbol_context" / "2026-04-05"
            serving_dir.mkdir(parents=True, exist_ok=True)
            feature_dossiers_dir.mkdir(parents=True, exist_ok=True)
            feature_symbol_dir.mkdir(parents=True, exist_ok=True)

            latest_pack = {
                "pack_id": "dossier-1",
                "trade_date": "2026-04-05",
                "symbol_count": 1,
            }
            latest_symbol_contexts = {
                "trade_date": "2026-04-05",
                "symbol_count": 1,
                "items": [{"symbol": "600519.SH", "name": "贵州茅台"}],
            }
            dossier = {
                "trade_date": "2026-04-05",
                "symbol": "600519.SH",
                "payload": {"symbol": "600519.SH", "name": "贵州茅台"},
            }
            symbol_context = {
                "trade_date": "2026-04-05",
                "symbol": "600519.SH",
                "payload": {"symbol": "600519.SH", "name": "贵州茅台"},
            }
            (serving_dir / "latest_dossier_pack.json").write_text(json.dumps(latest_pack, ensure_ascii=False), encoding="utf-8")
            (serving_dir / "latest_symbol_contexts.json").write_text(json.dumps(latest_symbol_contexts, ensure_ascii=False), encoding="utf-8")
            (feature_dossiers_dir / "600519_SH.json").write_text(json.dumps(dossier, ensure_ascii=False), encoding="utf-8")
            (feature_symbol_dir / "600519_SH.json").write_text(json.dumps(symbol_context, ensure_ascii=False), encoding="utf-8")

            app = FastAPI()
            app.include_router(build_router(settings))
            client = TestClient(app)

            latest_dossiers = client.get("/data/dossiers/latest").json()
            latest_discussion_context = client.get("/data/discussion-context/latest").json()
            latest_monitor_context = client.get("/data/monitor-context/latest").json()
            latest_runtime_context = client.get("/data/runtime-context/latest").json()
            latest_workspace_context = client.get("/data/workspace-context/latest").json()
            latest_symbols = client.get("/data/symbol-contexts/latest").json()
            single_dossier = client.get("/data/dossiers/2026-04-05/600519.SH").json()
            single_symbol_context = client.get("/data/symbol-contexts/2026-04-05/600519.SH").json()
            catalog = client.get("/data/catalog").json()

            self.assertEqual(latest_dossiers["pack_id"], "dossier-1")
            self.assertFalse(latest_discussion_context["available"])
            self.assertFalse(latest_monitor_context["available"])
            self.assertFalse(latest_runtime_context["available"])
            self.assertFalse(latest_workspace_context["available"])
            self.assertEqual(latest_symbols["items"][0]["symbol"], "600519.SH")
            self.assertEqual(single_dossier["symbol"], "600519.SH")
            self.assertEqual(single_symbol_context["symbol"], "600519.SH")
            self.assertEqual(catalog["status"], "ok")
            self.assertEqual(catalog["catalog_version"], "v1")
            self.assertIn("preferred_read_order", catalog)
            self.assertGreaterEqual(len(catalog["resources"]), 8)
            self.assertEqual(catalog["resources"][0]["resource"], "market_context")
            self.assertTrue(any(item["resource"] == "discussion_context" for item in catalog["resources"]))
            self.assertTrue(any(item["resource"] == "monitor_context" for item in catalog["resources"]))
            self.assertTrue(any(item["resource"] == "runtime_context" for item in catalog["resources"]))
            self.assertTrue(any(item["resource"] == "workspace_context" for item in catalog["resources"]))
            self.assertIn("features_dossiers_root", catalog["storage_domains"])
            self.assertIn("features_monitor_context_root", catalog["storage_domains"])
            self.assertIn("features_runtime_context_root", catalog["storage_domains"])
            self.assertIn("features_workspace_context_root", catalog["storage_domains"])
            self.assertIn("roles", catalog["agent_usage"])


if __name__ == "__main__":
    unittest.main()
