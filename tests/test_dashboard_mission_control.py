from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ashare_system.app import create_app
from ashare_system.container import (
    get_candidate_case_service,
    get_execution_gateway_state_store,
    get_meeting_state_store,
    get_runtime_state_store,
    reset_container,
)


@contextmanager
def _temporary_env(**overrides: str):
    keys = set(overrides.keys()) | {
        "ASHARE_STORAGE_ROOT",
        "ASHARE_LOGS_DIR",
        "ASHARE_EXECUTION_MODE",
        "ASHARE_MARKET_MODE",
        "ASHARE_RUN_MODE",
        "ASHARE_ACCOUNT_ID",
    }
    previous = {key: os.environ.get(key) for key in keys}
    with tempfile.TemporaryDirectory() as tmp_dir:
        os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
        os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
        os.environ["ASHARE_EXECUTION_MODE"] = "mock"
        os.environ["ASHARE_MARKET_MODE"] = "mock"
        os.environ["ASHARE_RUN_MODE"] = "paper"
        os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
        for key, value in overrides.items():
            os.environ[key] = value
        reset_container()
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            reset_container()


class DashboardMissionControlTests(unittest.TestCase):
    def test_dashboard_mission_control_endpoint_returns_core_sections(self) -> None:
        with _temporary_env():
            get_runtime_state_store().set(
                "latest_runtime_context",
                {
                    "top_picks": [{"symbol": "600519.SH", "selection_score": 92.0, "assigned_playbook": "leadership_breakout"}],
                    "market_profile": {"regime_label": "trend"},
                },
            )
            get_meeting_state_store().set(
                "latest_execution_precheck",
                {
                    "trade_date": "2026-04-21",
                    "approved_count": 1,
                    "blocked_count": 0,
                    "items": [{"symbol": "600519.SH", "approved": True}],
                },
            )
            get_runtime_state_store().set(
                "latest_history_daily_ingest",
                {
                    "trade_date": "2026-04-21",
                    "row_count": 24000,
                    "ingested_symbol_count": 200,
                },
            )
            with TestClient(create_app()) as client:
                payload = client.get("/system/dashboard/mission-control").json()
                self.assertTrue(payload["ok"])
                self.assertIn("market", payload)
                self.assertIn("execution", payload)
                self.assertIn("timeline", payload)
                self.assertIn("feishu_bots", payload)
                self.assertIn("history", payload)
                self.assertEqual(payload["history"]["latest_daily"]["row_count"], 24000)

    def test_dashboard_opportunity_flow_endpoint_returns_bucket_lists(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.get("/system/dashboard/opportunity-flow").json()
                self.assertTrue(payload["ok"])
                self.assertIn("selected", payload)
                self.assertIn("watchlist", payload)
                self.assertIn("rejected", payload)
                self.assertIn("summary_lines", payload)

    def test_execution_mainline_endpoint_returns_unified_trace_view(self) -> None:
        with _temporary_env():
            candidate_case_service = get_candidate_case_service()
            trade_date = "2026-04-21"
            candidate_case_service.sync_from_runtime_report(
                {
                    "job_id": "runtime-mainline-001",
                    "generated_at": f"{trade_date}T09:29:00",
                    "top_picks": [
                        {
                            "symbol": "600519.SH",
                            "name": "贵州茅台",
                            "rank": 1,
                            "selection_score": 98.0,
                            "action": "BUY",
                            "summary": "龙头候选",
                            "resolved_sector": "白酒",
                        }
                    ],
                },
                focus_pool_capacity=10,
                execution_pool_capacity=3,
            )
            get_meeting_state_store().set(
                "latest_execution_precheck",
                {
                    "trade_date": trade_date,
                    "generated_at": f"{trade_date}T09:30:00",
                    "approved_count": 1,
                    "blocked_count": 0,
                    "items": [
                        {
                            "symbol": "600519.SH",
                            "name": "贵州茅台",
                            "approved": True,
                            "primary_blocker_label": "",
                        }
                    ],
                },
            )
            with TestClient(create_app()) as client:
                payload = client.get(f"/system/dashboard/execution-mainline?trade_date={trade_date}").json()
                self.assertTrue(payload["ok"])
                self.assertIn("items", payload)
                self.assertIn("summary_lines", payload)
                self.assertGreaterEqual(payload["count"], 1)
                first = payload["items"][0]
                self.assertIn("candidate_stage", first)
                self.assertIn("precheck_stage", first)
                self.assertIn("intent_stage", first)
                self.assertIn("receipt_stage", first)

    def test_feishu_bots_endpoint_returns_three_roles(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.get("/system/feishu/bots").json()
                self.assertTrue(payload["ok"])
                roles = {item["role"] for item in payload["items"]}
                self.assertEqual(roles, {"main", "supervision", "execution"})

    def test_search_catalog_and_feishu_briefing_include_history_runtime(self) -> None:
        with _temporary_env():
            get_runtime_state_store().set(
                "latest_history_daily_ingest",
                {
                    "trade_date": "2026-04-21",
                    "row_count": 65600,
                    "ingested_symbol_count": 600,
                },
            )
            get_runtime_state_store().set(
                "latest_history_minute_ingest",
                {
                    "trade_date": "2026-04-21",
                    "row_count": 4800,
                    "symbol_count": 20,
                    "count": 240,
                },
            )
            with TestClient(create_app()) as client:
                catalog_payload = client.get("/system/search/catalog").json()
                self.assertTrue(catalog_payload["ok"])
                self.assertIn("capabilities", catalog_payload)
                self.assertIn("latest_history_runtime", catalog_payload)
                self.assertEqual(catalog_payload["latest_history_runtime"]["daily"]["row_count"], 65600)

                briefing_payload = client.get("/system/feishu/briefing").json()
                self.assertTrue(briefing_payload["ok"])
                self.assertIn("history_runtime", briefing_payload)
                self.assertIn("/system/search/catalog", briefing_payload["data_refs"])
                self.assertTrue(any("日线入湖" in line for line in briefing_payload["summary_lines"]))

    def test_client_brief_and_mission_control_use_today_as_display_trade_date_before_refresh(self) -> None:
        class _FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                current = cls.fromisoformat("2026-04-21T09:12:00")
                if tz is not None:
                    return current.astimezone(tz)
                return current

        with _temporary_env():
            candidate_case_service = get_candidate_case_service()
            candidate_case_service.sync_from_runtime_report(
                {
                    "job_id": "runtime-stale-001",
                    "generated_at": "2026-04-20T15:01:00",
                    "top_picks": [
                        {
                            "symbol": "000002.SZ",
                            "name": "万 科Ａ",
                            "rank": 1,
                            "selection_score": 9.6,
                            "action": "BUY",
                            "summary": "隔夜候选仍保留",
                            "resolved_sector": "地产链",
                        }
                    ],
                },
                focus_pool_capacity=10,
                execution_pool_capacity=3,
            )

            with patch("ashare_system.apps.system_api.datetime", _FixedDateTime), patch(
                "ashare_system.supervision_tasks.datetime", _FixedDateTime
            ):
                with TestClient(create_app()) as client:
                    brief_payload = client.get("/system/discussions/client-brief").json()
                    self.assertTrue(brief_payload["ok"])
                    self.assertEqual(brief_payload["trade_date"], "2026-04-21")
                    self.assertEqual(brief_payload["source_trade_date"], "2026-04-20")
                    self.assertTrue(brief_payload["source_trade_date_stale"])
                    self.assertTrue(brief_payload["overview_lines"][0].startswith("交易日 2026-04-21"))
                    self.assertIn("暂沿用 2026-04-20", brief_payload["overview_lines"][1])

                    dashboard_payload = client.get("/system/dashboard/mission-control").json()
                    self.assertTrue(dashboard_payload["ok"])
                    self.assertEqual(dashboard_payload["trade_date"], "2026-04-21")
                    self.assertEqual(dashboard_payload["source_trade_date"], "2026-04-20")
                    self.assertEqual(
                        dashboard_payload["discussion"]["client_brief"]["trade_date"],
                        "2026-04-21",
                    )

                    briefing_payload = client.get("/system/feishu/briefing").json()
                    self.assertTrue(briefing_payload["ok"])
                    self.assertEqual(briefing_payload["trade_date"], "2026-04-21")
                    self.assertEqual(briefing_payload["source_trade_date"], "2026-04-20")
                    self.assertTrue(
                        any("当前运营交易日=2026-04-21" in line for line in briefing_payload["summary_lines"])
                    )

    def test_supervision_executor_uses_receipt_not_stale_precheck_count(self) -> None:
        with _temporary_env():
            trade_date = "2026-04-21"
            get_meeting_state_store().set(
                "latest_execution_precheck",
                {
                    "trade_date": trade_date,
                    "generated_at": f"{trade_date}T10:00:00",
                    "approved_count": 3,
                    "blocked_count": 0,
                    "items": [],
                },
            )
            gateway_store = get_execution_gateway_state_store()
            gateway_store.set("pending_execution_intents", [])
            gateway_store.set(
                "execution_intent_history",
                [
                    {
                        "intent_id": "intent-001",
                        "trade_date": trade_date,
                        "status": "submitted",
                        "symbol": "600519.SH",
                    }
                ],
            )
            gateway_store.set(
                "latest_execution_gateway_receipt",
                {
                    "receipt_id": "receipt-001",
                    "intent_id": "intent-001",
                    "status": "submitted",
                    "reported_at": "2026-04-21T10:17:23",
                    "submitted_at": "2026-04-21T10:17:23",
                },
            )

            with TestClient(create_app()) as client:
                payload = client.get(f"/system/agents/supervision-board?trade_date={trade_date}&overdue_after_seconds=180").json()
                self.assertTrue(payload["ok"])
                executor_item = next(item for item in payload["items"] if item["agent_id"] == "ashare-executor")
                self.assertEqual(executor_item["status"], "working")
                self.assertIn("submitted", executor_item["reasons"][0])

    def test_mission_control_skips_dispatch_not_found_alert_when_receipt_exists(self) -> None:
        with _temporary_env():
            trade_date = "2026-04-21"
            gateway_store = get_execution_gateway_state_store()
            gateway_store.set("pending_execution_intents", [])
            gateway_store.set(
                "execution_intent_history",
                [
                    {
                        "intent_id": "intent-002",
                        "trade_date": trade_date,
                        "status": "submitted",
                        "symbol": "000001.SZ",
                    }
                ],
            )
            gateway_store.set(
                "latest_execution_gateway_receipt",
                {
                    "receipt_id": "receipt-002",
                    "intent_id": "intent-002",
                    "status": "submitted",
                    "reported_at": "2026-04-21T10:20:00",
                    "submitted_at": "2026-04-21T10:20:00",
                },
            )

            with TestClient(create_app()) as client:
                payload = client.get("/system/dashboard/mission-control").json()
                self.assertTrue(payload["ok"])
                titles = {str(item.get("title") or "") for item in list(payload.get("attention_items") or [])}
                self.assertNotIn("执行派发未顺利落地", titles)


if __name__ == "__main__":
    unittest.main()
