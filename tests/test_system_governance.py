"""System governance 最小闭环测试。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

import httpx
import pandas as pd

from ashare_system.apps.system_api import _build_post_rollback_tracking_summary
from ashare_system.app import create_app
from ashare_system.backtest.attribution import OfflineBacktestAttributionService
from ashare_system.backtest.metrics import MetricsCalculator
from ashare_system.backtest.playbook_runner import PlaybookBacktestRunner
from ashare_system.data.archive import DataArchiveStore
from ashare_system.contracts import BalanceSnapshot, ExecutionIntentPacket, PositionSnapshot, TradeSnapshot
from ashare_system.container import (
    get_execution_adapter,
    get_market_adapter,
    get_monitor_state_service,
    get_meeting_state_store,
    get_runtime_state_store,
    get_settings,
    reset_container,
)
from ashare_system.learning.attribution import TradeAttributionRecord, TradeAttributionService
from ashare_system.monitor.persistence import build_execution_bridge_health_ingress_payload
from ashare_system.portfolio import build_test_trading_budget
from ashare_system.scheduler import run_tail_market_scan
from ashare_system.windows_execution_gateway_worker import ExecutionGatewayWorker, GatewayWorkerConfig, _build_submitter


class ASGISyncClient:
    """基于 ASGITransport 的同步测试客户端。

    当前环境中的 starlette TestClient 首个请求会卡住，这里直接驱动 ASGI app，
    同时手动进入/退出 lifespan，保持接口级验证覆盖。
    """

    def __init__(self, app) -> None:
        self._app = app
        self._lifespan = app.router.lifespan_context(app)
        asyncio.run(self._lifespan.__aenter__())

    def close(self) -> None:
        asyncio.run(self._lifespan.__aexit__(None, None, None))

    def request(self, method: str, url: str, **kwargs):
        async def _send():
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(_send())

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)


class WorkerASGIClient:
    """把 ASGI 同步客户端适配给 gateway worker。"""

    def __init__(self, sync_client: ASGISyncClient) -> None:
        self._sync_client = sync_client

    def get(self, url: str, **kwargs):
        return self._sync_client.get(self._to_path(url), **kwargs)

    def post(self, url: str, **kwargs):
        return self._sync_client.post(self._to_path(url), **kwargs)

    def close(self) -> None:
        return None

    @staticmethod
    def _to_path(url: str) -> str:
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return path


class SystemGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        os.environ["ASHARE_STORAGE_ROOT"] = str(root / "state")
        os.environ["ASHARE_LOGS_DIR"] = str(root / "logs")
        os.environ["ASHARE_WORKSPACE"] = str(root)
        os.environ["ASHARE_MARKET_MODE"] = "mock"
        os.environ["ASHARE_EXECUTION_MODE"] = "mock"
        os.environ["ASHARE_EXECUTION_PLANE"] = "local_xtquant"
        os.environ["ASHARE_RUN_MODE"] = "paper"
        os.environ["ASHARE_LIVE_ENABLE"] = "false"
        reset_container()
        self.client = ASGISyncClient(create_app())

    def tearDown(self) -> None:
        self.client.close()
        reset_container()
        self._tmpdir.cleanup()

    def _reload_app(self) -> None:
        self.client.close()
        reset_container()
        self.client = ASGISyncClient(create_app())

    def _build_gateway_worker(
        self,
        *,
        source_id: str = "windows-vm-a",
        deployment_role: str = "primary_gateway",
        bridge_path: str = "linux_openclaw -> windows_gateway -> qmt_vm",
        executor_mode: str = "noop_success",
    ) -> ExecutionGatewayWorker:
        return ExecutionGatewayWorker(
            GatewayWorkerConfig(
                control_plane_base_url="http://testserver",
                source_id=source_id,
                deployment_role=deployment_role,
                bridge_path=bridge_path,
            ),
            submitter=_build_submitter(executor_mode),
            client=WorkerASGIClient(self.client),
            now_factory=lambda: datetime(2026, 4, 8, 15, 1, 2),
        )

    def _store_gateway_intent(
        self,
        *,
        intent_id: str = "intent-20260408-0001",
        status: str = "approved",
        account_id: str = "sim-001",
        symbol: str = "600519.SH",
        side: str = "BUY",
    ) -> dict:
        meeting_state = get_meeting_state_store()
        payload = ExecutionIntentPacket(
            intent_id=intent_id,
            generated_at="2026-04-08T09:30:58+08:00",
            trade_date="2026-04-08",
            account_id=account_id,
            symbol=symbol,
            side=side,
            quantity=100,
            price=1688.0,
            run_mode="paper",
            execution_plane="windows_gateway",
            approval_source="system_governance_test",
            approved_by="tester",
            approved_at="2026-04-08T09:30:58+08:00",
            idempotency_key=f"{account_id}-2026-04-08-{symbol}-{side}-1688.0-100",
            status=status,
            request={
                "account_id": account_id,
                "symbol": symbol,
                "side": side,
                "quantity": 100,
                "price": 1688.0,
                "request_id": f"{intent_id}-request",
            },
            summary_lines=["已批准，等待 Windows Gateway 拉取。"],
        ).model_dump()
        meeting_state.set("pending_execution_intents", [payload])
        meeting_state.set("execution_intent_history", [payload])
        return payload

    def test_post_rollback_tracking_marks_recovery_confirmed(self) -> None:
        tracked = _build_post_rollback_tracking_summary(
            {
                "avg_next_day_close_pct": -0.02,
                "win_rate": 0.3,
            },
            {
                "event_id": "rollback-1",
                "rollback_of_event_id": "proposal-1",
                "status": "effective",
                "source_filters": {"trade_date": "2026-04-06", "score_date": "2026-04-07"},
                "observation_window": {
                    "stage": "rollback_followup",
                    "start_date": "2026-04-01",
                    "end_date": "2026-04-06",
                    "expected_trade_count": 1,
                },
            },
            {
                "available": True,
                "trade_count": 2,
                "avg_next_day_close_pct": 0.01,
                "win_rate": 0.6,
            },
        )
        self.assertTrue(tracked["tracked"])
        self.assertTrue(tracked["recovery_detected"])
        self.assertEqual(tracked["followup_status"], "recovery_confirmed")
        self.assertEqual(tracked["recommended_action"]["action"], "confirm_rollback_effect")
        self.assertEqual(tracked["operation_targets"][0]["path"], "/system/learning/parameter-hints/effects")
        self.assertEqual(tracked["operation_targets"][0]["query"]["event_ids"], "proposal-1")

    def test_post_rollback_tracking_marks_high_risk_manual_review(self) -> None:
        tracked = _build_post_rollback_tracking_summary(
            {
                "avg_next_day_close_pct": -0.01,
                "win_rate": 0.35,
            },
            {
                "event_id": "rollback-high-risk-1",
                "event_type": "param_rollback",
                "rollback_of_event_id": "proposal-high-risk-1",
                "status": "evaluating",
                "approval_ticket": {
                    "required": True,
                    "state": "pending",
                    "risk_level": "high",
                },
                "source_filters": {"trade_date": "2026-04-06", "score_date": "2026-04-07"},
                "observation_window": {
                    "stage": "rollback_followup",
                    "start_date": "2026-04-01",
                    "end_date": "2026-04-06",
                    "expected_trade_count": 1,
                },
            },
            None,
        )
        self.assertTrue(tracked["tracked"])
        self.assertEqual(tracked["followup_status"], "manual_review_required")
        self.assertEqual(tracked["recommended_action"]["action"], "manual_release_or_reject")
        self.assertEqual(
            tracked["operation_targets"][0]["path"],
            "/system/learning/parameter-hints/rollback-approval",
        )
        self.assertEqual(
            tracked["operation_targets"][0]["payload"]["event_ids"],
            ["rollback-high-risk-1"],
        )

    def _prepare_finalized_discussion(self) -> tuple[str, dict]:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        cases = self.client.get("/system/cases").json()["items"]
        trade_date = cases[0]["trade_date"]

        self.client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date})
        self.client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start")

        round_1_items = []
        agent_reasons = {
            "ashare-research": "研究支持入池",
            "ashare-strategy": "策略排序靠前",
            "ashare-risk": "风险可控",
            "ashare-audit": "证据链完整",
        }
        for index, case in enumerate(cases):
            for agent_id, reason in agent_reasons.items():
                stance = "support"
                reasons = [reason]
                if index == 0 and agent_id == "ashare-risk":
                    stance = "limit"
                    reasons = ["仓位需要二轮确认"]
                if index == 0 and agent_id == "ashare-audit":
                    stance = "question"
                    reasons = ["补充第二轮说明"]
                round_1_items.append(
                    {
                        "case_id": case["case_id"],
                        "round": 1,
                        "agent_id": agent_id,
                        "stance": stance,
                        "confidence": "high" if agent_id in {"ashare-research", "ashare-strategy"} else "medium",
                        "reasons": reasons,
                        "evidence_refs": [f"{agent_id}:round1:{case['symbol']}"],
                    }
                )

        self.client.post(
            "/system/discussions/opinions/batch",
            json={"auto_rebuild": True, "items": round_1_items},
        )
        self.client.post(f"/system/discussions/cycles/{trade_date}/refresh")
        self.client.post(f"/system/discussions/cycles/{trade_date}/rounds/2/start")

        round_2_items = [
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-research",
                "stance": "support",
                "confidence": "high",
                "reasons": ["第二轮补充行业催化"],
                "evidence_refs": ["research:round2"],
                "thesis": "催化已明确，继续支持入选",
                "key_evidence": ["新增行业催化公告", "催化与候选主题一致"],
                "challenged_by": ["ashare-audit"],
                "challenged_points": ["缺少第二轮补充说明"],
                "previous_stance": "support",
                "changed": False,
                "resolved_questions": ["行业催化真实性已补证"],
            },
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-strategy",
                "stance": "support",
                "confidence": "high",
                "reasons": ["二轮后仍在前列"],
                "evidence_refs": ["strategy:round2"],
                "thesis": "在争议票中仍是最优胜出者",
                "key_evidence": ["候选排序仍居前列"],
                "challenged_by": ["ashare-risk", "ashare-audit"],
                "challenged_points": ["仓位限制", "证据链不足"],
                "previous_stance": "watch",
                "changed": True,
                "changed_because": ["研究已补到催化", "审计问题已得到回应"],
                "resolved_questions": ["排序胜出逻辑已补充"],
            },
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-risk",
                "stance": "support",
                "confidence": "medium",
                "reasons": ["风险条件已澄清"],
                "evidence_refs": ["risk:round2"],
                "thesis": "在限制仓位条件下可放行",
                "key_evidence": ["波动约束已说明"],
                "challenged_by": ["ashare-strategy"],
                "previous_stance": "limit",
                "changed": True,
                "changed_because": ["执行仓位条件明确", "波动风险已可控"],
                "resolved_questions": ["仓位限制条件已明确"],
            },
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-audit",
                "stance": "support",
                "confidence": "medium",
                "reasons": ["审计复核通过"],
                "evidence_refs": ["audit:round2"],
                "thesis": "关键证据缺口已关闭",
                "key_evidence": ["第二轮补充说明完整"],
                "challenged_by": ["ashare-research"],
                "previous_stance": "question",
                "changed": True,
                "changed_because": ["研究和策略已回应核心质疑"],
                "resolved_questions": ["证据链已补齐"],
            },
        ]
        self.client.post(
            "/system/discussions/opinions/batch",
            json={"auto_rebuild": True, "items": round_2_items},
        )
        self.client.post(f"/system/discussions/cycles/{trade_date}/refresh")

        finalize = self.client.post(f"/system/discussions/cycles/{trade_date}/finalize")
        self.assertEqual(finalize.status_code, 200)
        finalize_payload = finalize.json()
        self.assertTrue(finalize_payload["ok"])
        self.assertEqual(finalize_payload["cycle"]["discussion_state"], "final_selection_ready")
        return trade_date, finalize_payload

    def _seed_pending_gateway_intent(
        self,
        *,
        intent_id: str = "intent-20260408-0001",
        status: str = "approved",
        trade_date: str = "2026-04-08",
        account_id: str = "sim-001",
    ) -> dict:
        packet = ExecutionIntentPacket(
            intent_id=intent_id,
            generated_at="2026-04-08T09:31:00+08:00",
            trade_date=trade_date,
            account_id=account_id,
            symbol="600519.SH",
            side="BUY",
            price=1688.0,
            quantity=100,
            run_mode="paper",
            execution_plane="windows_gateway",
            approval_source="system_governance_test",
            approved_by="tester",
            approved_at="2026-04-08T09:30:58+08:00",
            idempotency_key=f"{account_id}-{trade_date}-600519.SH-BUY-1688.0-100",
            status=status,
            request={
                "account_id": account_id,
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "price": 1688.0,
                "request_id": "req-intent-1",
                "trade_date": trade_date,
            },
            summary_lines=["已批准，等待 Windows Gateway 拉取。"],
        ).model_dump()
        meeting_state = get_meeting_state_store()
        meeting_state.set("pending_execution_intents", [packet])
        meeting_state.set("execution_intent_history", [packet])
        return packet

    def test_param_proposal_can_be_effective_immediately(self) -> None:
        response = self.client.post(
            "/system/params/proposals",
            json={
                "param_key": "focus_pool_capacity",
                "new_value": 6,
                "effective_period": "until_revoked",
                "status": "approved",
                "proposed_by": "user",
                "structured_by": "ashare",
                "approved_by": "ashare-audit",
                "reason": "缩小重点观察池",
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["event"]["status"], "effective")

        params = self.client.get("/system/params").json()["items"]
        focus = next(item for item in params if item["param_key"] == "focus_pool_capacity")
        self.assertEqual(focus["current_value"], 6)
        self.assertEqual(focus["current_layer"], "agent_adjusted_params")

    def test_runtime_config_supports_nested_watch_and_snapshot_updates(self) -> None:
        response = self.client.get("/system/config")
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["screener_pool_size"], 30)
        self.assertEqual(payload["watch"]["candidate_poll_seconds"], 300)
        self.assertEqual(payload["snapshots"]["dossier_retention_trading_days"], 5)

        updated = self.client.post(
            "/system/config",
            json={
                "watch.execution_poll_seconds": 20,
                "snapshots.archive_retention_trading_days": 15,
            },
        )
        updated_payload = updated.json()
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated_payload["ok"])
        self.assertEqual(updated_payload["config"]["watch"]["execution_poll_seconds"], 20)
        self.assertEqual(updated_payload["config"]["snapshots"]["archive_retention_trading_days"], 15)

    def test_natural_language_adjustment_preview_does_not_apply(self) -> None:
        response = self.client.post(
            "/system/adjustments/natural-language",
            json={
                "instruction": "把股票池改到30只，心跳改成5分钟，总仓位改成8成",
                "apply": False,
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["matched_count"], 3)
        self.assertIn("summary_lines", payload)
        self.assertEqual(payload["status"], "preview")
        self.assertIn("reply_lines", payload)
        self.assertIn("本次仅预览", payload["reply_lines"][0])
        self.assertEqual(payload["notification"]["reason"], "disabled")
        current_config = self.client.get("/system/config").json()
        self.assertEqual(current_config["watch"]["heartbeat_save_seconds"], 600)
        params = {item["param_key"]: item for item in self.client.get("/system/params").json()["items"]}
        self.assertEqual(params["max_total_position"]["current_value"], 0.8)

    def test_natural_language_adjustment_applies_params_and_config(self) -> None:
        response = self.client.post(
            "/system/adjustments/natural-language",
            json={
                "instruction": "今天把候选池改到28只，观察池改到12只，心跳改成5分钟，归档时长改成15天",
                "apply": True,
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["applied"])
        self.assertEqual(payload["matched_count"], 4)
        self.assertEqual(payload["inferred_effective_period"], "today_session")
        self.assertIn("summary_lines", payload)
        self.assertEqual(payload["status"], "effective")
        self.assertIn("reply_lines", payload)
        self.assertIn("本次调整已生效", payload["reply_lines"][0])
        self.assertEqual(payload["notification"]["reason"], "disabled")
        self.assertEqual(payload["config_updates"]["screener_pool_size"], 28)
        self.assertEqual(payload["config_updates"]["watch.heartbeat_save_seconds"], 300)
        self.assertEqual(payload["config_updates"]["snapshots.archive_retention_trading_days"], 15)

        config = self.client.get("/system/config").json()
        self.assertEqual(config["screener_pool_size"], 28)
        self.assertEqual(config["watch"]["heartbeat_save_seconds"], 300)
        self.assertEqual(config["snapshots"]["archive_retention_trading_days"], 15)

        params = {item["param_key"]: item for item in self.client.get("/system/params").json()["items"]}
        self.assertEqual(params["base_pool_capacity"]["current_value"], 28)
        self.assertEqual(params["focus_pool_capacity"]["current_value"], 12)
        self.assertEqual(params["monitor_heartbeat_save_seconds"]["current_value"], 300)
        self.assertEqual(params["archive_retention_trading_days"]["current_value"], 15)

    def test_natural_language_adjustment_applies_single_amount_limit(self) -> None:
        response = self.client.post(
            "/system/adjustments/natural-language",
            json={
                "instruction": "把单票金额上限改成2万",
                "apply": True,
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["applied"])
        self.assertEqual(payload["matched_count"], 1)
        self.assertEqual(payload["config_updates"]["max_single_amount"], 20000.0)

        config = self.client.get("/system/config").json()
        self.assertEqual(config["max_single_amount"], 20000.0)

        params = {item["param_key"]: item for item in self.client.get("/system/params").json()["items"]}
        self.assertEqual(params["max_single_amount"]["current_value"], 20000.0)

    def test_natural_language_adjustment_can_request_notification(self) -> None:
        os.environ["ASHARE_FEISHU_APP_ID"] = ""
        os.environ["ASHARE_FEISHU_APP_SECRET"] = ""
        os.environ["ASHARE_FEISHU_CHAT_ID"] = ""
        self.client.close()
        reset_container()
        self.client = ASGISyncClient(create_app())

        response = self.client.post(
            "/system/adjustments/natural-language",
            json={
                "instruction": "把心跳改成5分钟",
                "apply": False,
                "notify": True,
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["applied"])
        self.assertIn("notification", payload)
        self.assertEqual(payload["status"], "preview")
        self.assertIn("reply_lines", payload)
        self.assertIn(payload["notification"]["reason"], {"dispatch_failed", "dispatcher_unavailable", "sent"})

    def test_precompute_dossiers_can_reuse_fresh_pack(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        first = self.client.post(
            "/system/precompute/dossiers",
            json={"source": "candidate_pool", "limit": 4, "force": True},
        ).json()
        second = self.client.post("/system/precompute/dossiers", json={"source": "candidate_pool", "limit": 4}).json()

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["trade_date"], second["trade_date"])
        self.assertEqual(first["symbol_count"], second["symbol_count"])

    def test_governance_params_include_monitor_heartbeat_controls(self) -> None:
        params = self.client.get("/system/params").json()["items"]
        items = {item["param_key"]: item for item in params}
        self.assertIn("monitor_heartbeat_save_seconds", items)
        self.assertIn("monitor_auction_heartbeat_save_seconds", items)
        self.assertIn("dossier_retention_trading_days", items)
        self.assertEqual(items["monitor_heartbeat_save_seconds"]["current_value"], 600)
        self.assertEqual(items["dossier_retention_trading_days"]["current_value"], 5)

    def test_runtime_pipeline_syncs_candidate_cases(self) -> None:
        runtime = self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        runtime_payload = runtime.json()
        self.assertEqual(runtime.status_code, 200)
        self.assertEqual(len(runtime_payload["top_picks"]), 4)
        self.assertTrue(runtime_payload["top_picks"][0]["name"])

        cases_response = self.client.get("/system/cases")
        cases_payload = cases_response.json()
        self.assertEqual(cases_response.status_code, 200)
        self.assertEqual(cases_payload["count"], 4)

        first_case = cases_payload["items"][0]
        runtime_name_map = {item["symbol"]: item["name"] for item in runtime_payload["top_picks"]}
        self.assertEqual(first_case["name"], runtime_name_map[first_case["symbol"]])
        self.assertIn(first_case["final_status"], {"selected", "watchlist", "rejected"})
        self.assertTrue(first_case["pool_membership"]["base_pool"])
        self.assertEqual(first_case["runtime_snapshot"]["runtime_report_ref"], runtime_payload["job_id"])
        runtime_context = self.client.get("/data/runtime-context/latest").json()
        self.assertTrue(runtime_context["available"])
        self.assertEqual(runtime_context["job_id"], runtime_payload["job_id"])
        self.assertEqual(runtime_context["decision_count"], len(runtime_payload["top_picks"]))
        self.assertEqual(runtime_context["selected_symbols"][0], runtime_payload["top_picks"][0]["symbol"])
        self.assertIn("pool", runtime_context)
        self.assertIn("summary", runtime_context)
        self.assertEqual(runtime_context["summary"]["buy_count"], runtime_payload["summary"]["buy_count"])
        workspace_context = self.client.get("/data/workspace-context/latest").json()
        self.assertTrue(workspace_context["available"])
        self.assertEqual(workspace_context["trade_date"], runtime_context["trade_date"])
        self.assertIn("runtime_context", workspace_context)
        self.assertEqual(workspace_context["runtime_context"]["job_id"], runtime_payload["job_id"])
        self.assertIn("summary_lines", workspace_context)

        opinion = self.client.post(
            f"/system/cases/{first_case['case_id']}/opinions",
            json={
                "round": 1,
                "agent_id": "ashare-research",
                "stance": "support",
                "confidence": "high",
                "reasons": ["公告与情绪共振"],
                "evidence_refs": ["research-summary:test"],
            },
        )
        opinion_payload = opinion.json()
        self.assertEqual(opinion.status_code, 200)
        self.assertTrue(opinion_payload["ok"])
        self.assertEqual(len(opinion_payload["case"]["opinions"]), 1)

        dossier = self.client.get("/system/precompute/dossiers/latest").json()
        self.assertTrue(dossier["available"])
        self.assertTrue(dossier["is_fresh"])
        self.assertIsNotNone(dossier["expires_in_seconds"])
        self.assertGreaterEqual(dossier["expires_in_seconds"], 0)
        self.assertEqual(dossier["trade_date"], first_case["trade_date"])
        self.assertGreaterEqual(dossier["symbol_count"], 4)
        self.assertTrue(dossier["items"][0]["name"])

        cadence = self.client.get("/system/monitoring/cadence").json()
        self.assertEqual(cadence["trade_date"], first_case["trade_date"])
        self.assertIn("candidate", cadence["polling_status"])
        self.assertFalse(cadence["polling_status"]["candidate"]["due_now"])
        self.assertTrue(cadence["dossier"]["available"])
        self.assertTrue(cadence["dossier"]["is_fresh"])
        self.assertIn("summary_lines", cadence)

    def test_runtime_pipeline_uses_runtime_default_candidate_pool_size(self) -> None:
        updated = self.client.post("/system/config", json={"screener_pool_size": 3})
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.json()["ok"])

        runtime = self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "auto_trade": False, "account_id": "sim-001"},
        )
        payload = runtime.json()
        self.assertEqual(runtime.status_code, 200)
        self.assertEqual(len(payload["top_picks"]), 3)

        cases_payload = self.client.get("/system/cases").json()
        self.assertEqual(cases_payload["count"], 3)

    def test_serving_layer_is_used_when_dossier_state_store_is_empty(self) -> None:
        serving_dir = Path(os.environ["ASHARE_STORAGE_ROOT"]) / "serving"
        serving_dir.mkdir(parents=True, exist_ok=True)
        latest_pack = {
            "pack_id": "dossier-serving-1",
            "trade_date": "2026-04-05",
            "source": "candidate_pool",
            "symbol_count": 2,
            "generated_at": "2026-04-05T09:30:00+08:00",
            "expires_at": "2099-04-05T09:40:00+08:00",
            "items": [
                {
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "selection_score": 92.0,
                    "final_status": "watchlist",
                    "risk_gate": "pending",
                    "audit_gate": "pending",
                },
                {
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "selection_score": 88.0,
                    "final_status": "watchlist",
                    "risk_gate": "pending",
                    "audit_gate": "pending",
                },
            ],
        }
        (serving_dir / "latest_dossier_pack.json").write_text(
            json.dumps(latest_pack, ensure_ascii=False),
            encoding="utf-8",
        )

        dossier = self.client.get("/system/precompute/dossiers/latest").json()
        self.assertTrue(dossier["available"])
        self.assertEqual(dossier["source_layer"], "serving")
        self.assertEqual(dossier["pack_id"], "dossier-serving-1")
        self.assertEqual(dossier["trade_date"], "2026-04-05")

        cadence = self.client.get("/system/monitoring/cadence").json()
        self.assertEqual(cadence["trade_date"], "2026-04-05")
        self.assertEqual(cadence["dossier"]["source_layer"], "serving")
        self.assertTrue(any("source=serving" in line for line in cadence["summary_lines"]))

        pool = self.client.get("/monitor/pool", params={"top_n": 2}).json()
        self.assertEqual(pool["source"], "serving_dossier")
        self.assertEqual(pool["items"][0]["name"], "贵州茅台")
        self.assertEqual(pool["total"], 2)

    def test_monitor_pool_prefers_serving_discussion_context_when_available(self) -> None:
        serving_dir = Path(os.environ["ASHARE_STORAGE_ROOT"]) / "serving"
        serving_dir.mkdir(parents=True, exist_ok=True)
        latest_discussion_context = {
            "available": True,
            "resource": "discussion_context",
            "trade_date": "2026-04-05",
            "case_count": 2,
            "cycle": {"discussion_state": "final_selection_ready"},
            "reply_pack": {
                "selected": [
                    {
                        "case_id": "case-20260405-600519-SH",
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "selection_score": 95.0,
                        "final_status": "selected",
                        "risk_gate": "allow",
                        "audit_gate": "clear",
                    }
                ],
                "watchlist": [
                    {
                        "case_id": "case-20260405-000001-SZ",
                        "symbol": "000001.SZ",
                        "name": "平安银行",
                        "selection_score": 88.0,
                        "final_status": "watchlist",
                        "risk_gate": "limit",
                        "audit_gate": "hold",
                    }
                ],
                "rejected": [],
            },
        }
        (serving_dir / "latest_discussion_context.json").write_text(
            json.dumps(latest_discussion_context, ensure_ascii=False),
            encoding="utf-8",
        )

        pool = self.client.get("/monitor/pool", params={"top_n": 2}).json()
        self.assertEqual(pool["source"], "serving_discussion_context")
        self.assertEqual(pool["discussion_state"], "final_selection_ready")
        self.assertEqual(pool["items"][0]["name"], "贵州茅台")
        self.assertEqual(pool["items"][1]["name"], "平安银行")

        latest_discussion = self.client.get("/monitor/discussion/latest").json()
        self.assertTrue(latest_discussion["available"])
        self.assertEqual(latest_discussion["trade_date"], "2026-04-05")
        self.assertEqual(latest_discussion["reply_pack"]["selected"][0]["symbol"], "600519.SH")

    def test_monitor_pool_and_state_endpoints_include_names(self) -> None:
        runtime = self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        payload = runtime.json()
        self.assertEqual(runtime.status_code, 200)

        pool = self.client.get("/monitor/pool", params={"top_n": 2})
        pool_payload = pool.json()
        self.assertEqual(pool.status_code, 200)
        self.assertEqual(len(pool_payload["items"]), 2)
        self.assertTrue(pool_payload["items"][0]["name"])

        state = self.client.get("/monitor/state")
        state_payload = state.json()
        self.assertEqual(state.status_code, 200)
        self.assertIn("latest_heartbeat", state_payload)
        self.assertIn("recent_events", state_payload)
        self.assertIn("latest_pool_snapshot", state_payload)
        self.assertIsNone(state_payload["latest_heartbeat"])
        self.assertEqual(state_payload["latest_pool_snapshot"]["counts"]["candidate_pool"], 4)
        self.assertTrue(state_payload["latest_pool_snapshot"]["candidate_pool"][0]["name"])

        layers = self.client.get("/monitor/pool/layers")
        layers_payload = layers.json()
        self.assertEqual(layers.status_code, 200)
        self.assertEqual(layers_payload["counts"]["candidate_pool"], 4)
        self.assertIn("focus_pool", layers_payload)

        monitor_context = self.client.get("/data/monitor-context/latest").json()
        self.assertTrue(monitor_context["available"])
        self.assertEqual(monitor_context["trade_date"], state_payload["latest_pool_snapshot"]["trade_date"])
        self.assertIn("latest_pool_snapshot", monitor_context)
        self.assertIn("polling_status", monitor_context)
        self.assertEqual(
            monitor_context["latest_pool_snapshot"]["counts"]["candidate_pool"],
            state_payload["latest_pool_snapshot"]["counts"]["candidate_pool"],
        )

    def test_execution_bridge_health_ingress_endpoint_persists_linux_windows_bridge_snapshot(self) -> None:
        ingress_payload = build_execution_bridge_health_ingress_payload(
            {
                "reported_at": "2026-04-08T10:05:00+08:00",
                "source_id": "windows-vm-qmt-02",
                "deployment_role": "windows_execution_gateway",
                "bridge_path": "linux_openclaw->windows_gateway->qmt_vm",
                "gateway_online": True,
                "qmt_connected": False,
                "session_fresh_seconds": 88,
                "last_error": "qmt vm heartbeat stale",
                "windows_execution_gateway": {
                    "status": "healthy",
                    "reachable": True,
                    "latency_ms": 11.5,
                    "detail": "linux openclaw bridge ok",
                    "tags": ["linux_openclaw_bridge"],
                },
                "qmt_vm": {
                    "status": "degraded",
                    "reachable": False,
                    "latency_ms": 176.0,
                    "staleness_seconds": 88.0,
                    "error_count": 3,
                    "detail": "windows vm heartbeat stale",
                    "tags": ["windows_vm", "qmt_bridge"],
                },
            },
            trigger="windows_execution_gateway",
        )
        response = self.client.post(
            "/system/monitor/execution-bridge-health",
            json=ingress_payload,
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["latest_execution_bridge_health"]["trigger"], "windows_execution_gateway")
        self.assertEqual(payload["latest_execution_bridge_health"]["health"]["overall_status"], "degraded")
        self.assertEqual(payload["latest_execution_bridge_health"]["health"]["source_id"], "windows-vm-qmt-02")
        self.assertEqual(
            payload["latest_execution_bridge_health"]["health"]["bridge_path"],
            "linux_openclaw->windows_gateway->qmt_vm",
        )
        self.assertEqual(
            payload["latest_execution_bridge_health"]["health"]["attention_component_keys"],
            ["qmt_vm"],
        )
        self.assertTrue(payload["trend_summary"]["available"])
        self.assertEqual(payload["trend_summary"]["latest_source_id"], "windows-vm-qmt-02")

        state_payload = self.client.get("/monitor/state").json()
        self.assertEqual(state_payload["latest_execution_bridge_health"]["trigger"], "windows_execution_gateway")
        self.assertEqual(state_payload["latest_execution_bridge_health"]["health"]["overall_status"], "degraded")
        self.assertEqual(
            state_payload["latest_execution_bridge_health"]["health"]["deployment_role"],
            "windows_execution_gateway",
        )
        self.assertEqual(
            state_payload["latest_execution_bridge_health"]["health"]["component_health"][1]["key"],
            "qmt_vm",
        )
        self.assertEqual(
            state_payload["execution_bridge_health_trend_summary"]["health_trend_snapshot"]["latest_source_id"],
            "windows-vm-qmt-02",
        )
        self.assertEqual(
            state_payload["execution_bridge_health_trend_summary"]["health_trend_snapshot"]["latest_qmt_vm_status"],
            "degraded",
        )

    def test_execution_bridge_health_template_endpoint_exposes_client_contract(self) -> None:
        response = self.client.get("/system/monitor/execution-bridge-health/template")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["template"]["method"], "POST")
        self.assertEqual(payload["template"]["path"], "/system/monitor/execution-bridge-health")
        self.assertEqual(payload["template"]["minimal_request_body"]["trigger"], "windows_gateway")
        self.assertEqual(
            payload["template"]["minimal_request_body"]["health"]["bridge_path"],
            "linux_openclaw -> windows_gateway -> qmt_vm",
        )
        self.assertEqual(
            payload["latest_descriptor"]["latest_execution_bridge_health"]["recommended_fields"]["source_id"],
            "latest_execution_bridge_health.health.source_id",
        )
        self.assertEqual(
            payload["template"]["source_value_suggestions"]["windows_gateway"]["deployment_role"],
            "primary_gateway",
        )
        self.assertEqual(
            payload["deployment_contract_sample"]["request_samples"]["windows_gateway_primary_post_body"]["health"][
                "source_id"
            ],
            "windows-vm-a",
        )
        self.assertEqual(
            payload["deployment_contract_sample"]["read_samples"]["linux_latest_read_example"]["recommended_fields"][
                "bridge_path"
            ],
            "latest_execution_bridge_health.health.bridge_path",
        )

    def test_gateway_pending_intents_endpoint_returns_approved_items(self) -> None:
        stored = self._store_gateway_intent()

        response = self.client.get("/system/execution/gateway/intents/pending")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["available"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["intent_id"], stored["intent_id"])
        self.assertEqual(payload["items"][0]["status"], "approved")
        self.assertEqual(payload["items"][0]["execution_plane"], "windows_gateway")

    def test_gateway_claim_endpoint_marks_intent_claimed_once(self) -> None:
        stored = self._store_gateway_intent()

        response = self.client.post(
            "/system/execution/gateway/intents/claim",
            json={
                "intent_id": stored["intent_id"],
                "gateway_source_id": "windows-vm-a",
                "deployment_role": "primary_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                "claimed_at": "2026-04-08T09:31:05+08:00",
            },
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["claim_status"], "claimed")
        self.assertEqual(payload["intent"]["status"], "claimed")
        self.assertEqual(payload["intent"]["claim"]["gateway_source_id"], "windows-vm-a")

    def test_gateway_claim_endpoint_is_idempotent_for_same_gateway(self) -> None:
        stored = self._store_gateway_intent()
        claim_payload = {
            "intent_id": stored["intent_id"],
            "gateway_source_id": "windows-vm-a",
            "deployment_role": "primary_gateway",
            "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
            "claimed_at": "2026-04-08T09:31:05+08:00",
        }

        first = self.client.post("/system/execution/gateway/intents/claim", json=claim_payload).json()
        second = self.client.post("/system/execution/gateway/intents/claim", json=claim_payload).json()

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(second["claim_status"], "claimed")
        self.assertEqual(second["intent"]["claim"]["gateway_source_id"], "windows-vm-a")

    def test_gateway_claim_endpoint_rejects_conflicting_gateway(self) -> None:
        stored = self._store_gateway_intent()
        self.client.post(
            "/system/execution/gateway/intents/claim",
            json={
                "intent_id": stored["intent_id"],
                "gateway_source_id": "windows-vm-a",
                "deployment_role": "primary_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                "claimed_at": "2026-04-08T09:31:05+08:00",
            },
        )

        response = self.client.post(
            "/system/execution/gateway/intents/claim",
            json={
                "intent_id": stored["intent_id"],
                "gateway_source_id": "windows-vm-b",
                "deployment_role": "backup_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway_backup -> qmt_vm",
                "claimed_at": "2026-04-08T09:31:07+08:00",
            },
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["claim_status"], "conflict")
        self.assertEqual(payload["intent"]["claim"]["gateway_source_id"], "windows-vm-a")

    def test_gateway_receipt_endpoint_persists_submitted_receipt(self) -> None:
        stored = self._store_gateway_intent(status="claimed")
        meeting_state = get_meeting_state_store()
        pending = meeting_state.get("pending_execution_intents", [])
        pending[0]["claim"] = {
            "gateway_source_id": "windows-vm-a",
            "deployment_role": "primary_gateway",
            "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
            "claimed_at": "2026-04-08T09:31:05+08:00",
        }
        meeting_state.set("pending_execution_intents", pending)
        meeting_state.set("execution_intent_history", pending)

        response = self.client.post(
            "/system/execution/gateway/receipts",
            json={
                "receipt_id": "receipt-20260408-0001",
                "intent_id": stored["intent_id"],
                "intent_version": "v1",
                "gateway_source_id": "windows-vm-a",
                "deployment_role": "primary_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                "reported_at": "2026-04-08T09:31:08+08:00",
                "submitted_at": "2026-04-08T09:31:07+08:00",
                "status": "submitted",
                "broker_order_id": "qmt-123456",
                "order": {
                    "symbol": "600519.SH",
                    "side": "BUY",
                    "price": 1688.0,
                    "quantity": 100,
                },
                "summary_lines": ["订单已由 Windows Gateway 提交到 QMT。"],
            },
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["stored"])
        self.assertEqual(payload["latest_receipt"]["receipt_id"], "receipt-20260408-0001")

        latest = self.client.get("/system/execution/gateway/receipts/latest").json()
        self.assertTrue(latest["available"])
        self.assertEqual(latest["receipt"]["status"], "submitted")

        detail = self.client.get(f"/system/execution/gateway/intents/{stored['intent_id']}").json()
        self.assertTrue(detail["available"])
        self.assertEqual(detail["intent"]["status"], "submitted")

    def test_gateway_receipt_endpoint_is_idempotent_for_same_receipt_id(self) -> None:
        stored = self._store_gateway_intent(status="claimed")
        meeting_state = get_meeting_state_store()
        pending = meeting_state.get("pending_execution_intents", [])
        pending[0]["claim"] = {
            "gateway_source_id": "windows-vm-a",
            "deployment_role": "primary_gateway",
            "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
            "claimed_at": "2026-04-08T09:31:05+08:00",
        }
        meeting_state.set("pending_execution_intents", pending)
        meeting_state.set("execution_intent_history", pending)
        receipt_payload = {
            "receipt_id": "receipt-20260408-0002",
            "intent_id": stored["intent_id"],
            "intent_version": "v1",
            "gateway_source_id": "windows-vm-a",
            "deployment_role": "primary_gateway",
            "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
            "reported_at": "2026-04-08T09:31:08+08:00",
            "submitted_at": "2026-04-08T09:31:07+08:00",
            "status": "submitted",
            "broker_order_id": "qmt-999999",
            "order": {"symbol": "600519.SH", "side": "BUY", "price": 1688.0, "quantity": 100},
        }

        first = self.client.post("/system/execution/gateway/receipts", json=receipt_payload).json()
        second = self.client.post("/system/execution/gateway/receipts", json=receipt_payload).json()

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertFalse(second["stored"])
        self.assertEqual(second["latest_receipt"]["receipt_id"], "receipt-20260408-0002")

    def test_gateway_receipt_endpoint_allows_claimed_intent_to_transition_to_failed(self) -> None:
        stored = self._store_gateway_intent(status="claimed")
        meeting_state = get_meeting_state_store()
        pending = meeting_state.get("pending_execution_intents", [])
        pending[0]["claim"] = {
            "gateway_source_id": "windows-vm-a",
            "deployment_role": "primary_gateway",
            "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
            "claimed_at": "2026-04-08T09:31:05+08:00",
        }
        meeting_state.set("pending_execution_intents", pending)
        meeting_state.set("execution_intent_history", pending)

        response = self.client.post(
            "/system/execution/gateway/receipts",
            json={
                "receipt_id": "receipt-20260408-failed-0001",
                "intent_id": stored["intent_id"],
                "intent_version": "v1",
                "gateway_source_id": "windows-vm-a",
                "deployment_role": "primary_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                "reported_at": "2026-04-08T09:31:08+08:00",
                "submitted_at": "",
                "status": "failed",
                "error_code": "gateway_submit_failed",
                "error_message": "XtQuant connect failed: -1",
                "summary_lines": ["测试失败回执已写入。"],
            },
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["stored"])
        self.assertEqual(payload["latest_receipt"]["receipt_id"], "receipt-20260408-failed-0001")
        self.assertEqual(payload["latest_receipt"]["status"], "failed")
        self.assertEqual(payload["latest_receipt"]["error_code"], "gateway_submit_failed")

        latest_receipt = self.client.get("/system/execution/gateway/receipts/latest").json()
        self.assertTrue(latest_receipt["available"])
        self.assertEqual(latest_receipt["receipt"]["status"], "failed")
        self.assertEqual(latest_receipt["receipt"]["intent_id"], stored["intent_id"])

        latest_intent = self.client.get(f"/system/execution/gateway/intents/{stored['intent_id']}").json()
        self.assertTrue(latest_intent["available"])
        self.assertEqual(latest_intent["intent"]["status"], "failed")
        self.assertEqual(latest_intent["intent"]["latest_receipt_id"], "receipt-20260408-failed-0001")

        pending_after = meeting_state.get("pending_execution_intents", [])
        self.assertEqual(pending_after, [])

    def test_discussion_gateway_noop_worker_end_to_end_updates_latest_receipt(self) -> None:
        os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
        self._reload_app()

        trade_date, finalize_payload = self._prepare_finalized_discussion()
        first_intent = finalize_payload["execution_intents"]["intents"][0]

        dispatch = self.client.post(
            "/system/discussions/execution-intents/dispatch",
            json={
                "trade_date": trade_date,
                "account_id": "sim-001",
                "intent_ids": [first_intent["intent_id"]],
                "apply": True,
            },
        ).json()
        self.assertTrue(dispatch["ok"])
        self.assertEqual(dispatch["status"], "queued_for_gateway")

        worker = self._build_gateway_worker()
        try:
            worker_payload = worker.run_once()
        finally:
            worker.close()

        self.assertTrue(worker_payload["ok"])
        self.assertEqual(worker_payload["polled_count"], 1)
        self.assertEqual(worker_payload["claimed_count"], 1)
        self.assertEqual(worker_payload["stored_count"], 1)
        self.assertEqual(worker_payload["results"][0]["phase"], "receipt")
        self.assertEqual(worker_payload["results"][0]["receipt_status"], "submitted")

        latest_dispatch = self.client.get(
            "/system/discussions/execution-dispatch/latest",
            params={"trade_date": trade_date},
        ).json()
        self.assertTrue(latest_dispatch["ok"])
        self.assertEqual(latest_dispatch["status"], "queued_for_gateway")

        latest_receipt = self.client.get("/system/execution/gateway/receipts/latest").json()
        self.assertTrue(latest_receipt["ok"])
        self.assertTrue(latest_receipt["available"])
        self.assertEqual(latest_receipt["receipt"]["intent_id"], first_intent["intent_id"])
        self.assertEqual(latest_receipt["receipt"]["status"], "submitted")
        self.assertIn("noop_success", "\n".join(latest_receipt["receipt"].get("summary_lines", [])))

        latest_intent = self.client.get(f"/system/execution/gateway/intents/{first_intent['intent_id']}").json()
        self.assertTrue(latest_intent["available"])
        self.assertEqual(latest_intent["intent"]["status"], "submitted")

    def test_tail_market_gateway_noop_worker_end_to_end_updates_receipt(self) -> None:
        os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
        self._reload_app()
        settings = get_settings()
        runtime_context = {
            "available": True,
            "resource": "runtime_context",
            "trade_date": "2026-04-06",
            "generated_at": "2026-04-06T14:45:00",
            "market_profile": {
                "sentiment_phase": "主升",
                "regime": "trend",
                "allowed_playbooks": ["leader_chase"],
            },
            "sector_profiles": [
                {
                    "sector_name": "白酒",
                    "life_cycle": "retreat",
                    "strength_score": 0.8,
                }
            ],
            "playbook_contexts": [
                {
                    "playbook": "leader_chase",
                    "symbol": "600519.SH",
                    "sector": "白酒",
                    "entry_window": "09:30-10:00",
                    "confidence": 0.91,
                    "rank_in_sector": 1,
                    "leader_score": 0.94,
                    "style_tag": "leader",
                    "exit_params": {
                        "atr_pct": 0.015,
                        "open_failure_minutes": 5,
                        "max_hold_minutes": 240,
                        "time_stop": "14:50",
                    },
                }
            ],
        }
        DataArchiveStore(settings.storage_root).persist_runtime_context("2026-04-06", runtime_context)
        get_runtime_state_store().set("latest_runtime_context", runtime_context)

        adapter = get_execution_adapter()
        adapter.positions["sim-001"] = [
            PositionSnapshot(
                account_id="sim-001",
                symbol="600519.SH",
                quantity=100,
                available=100,
                cost_price=1588.0,
                last_price=1592.5,
            )
        ]
        meeting_state = get_meeting_state_store()
        meeting_state.set(
            "execution_order_journal",
            [
                {
                    "trade_date": "2026-04-06",
                    "account_id": "sim-001",
                    "order_id": "order-buy-1",
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "decision_id": "case-600519",
                    "submitted_at": "2026-04-06T09:35:00",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "request": {
                        "side": "BUY",
                        "playbook": "leader_chase",
                        "regime": "trend",
                    },
                    "latest_status": "FILLED",
                }
            ],
        )

        tail_market = self.client.post("/system/tail-market/run", params={"account_id": "sim-001"}).json()
        self.assertTrue(tail_market["ok"])
        self.assertEqual(tail_market["status"], "queued_for_gateway")
        queued = next(item for item in tail_market["items"] if item["status"] == "queued_for_gateway")

        worker = self._build_gateway_worker()
        try:
            worker_payload = worker.run_once()
        finally:
            worker.close()

        self.assertTrue(worker_payload["ok"])
        self.assertEqual(worker_payload["polled_count"], 1)
        self.assertEqual(worker_payload["results"][0]["phase"], "receipt")
        self.assertEqual(worker_payload["results"][0]["receipt_status"], "submitted")

        latest_receipt = self.client.get("/system/execution/gateway/receipts/latest").json()
        self.assertTrue(latest_receipt["available"])
        self.assertEqual(latest_receipt["receipt"]["intent_id"], queued["gateway_intent"]["intent_id"])
        self.assertEqual(latest_receipt["receipt"]["status"], "submitted")

        latest_intent = self.client.get(
            f"/system/execution/gateway/intents/{queued['gateway_intent']['intent_id']}"
        ).json()
        self.assertTrue(latest_intent["available"])
        self.assertEqual(latest_intent["intent"]["status"], "submitted")
        self.assertEqual(latest_intent["intent"]["approval_source"], "tail_market_scan")

    def test_deployment_bootstrap_contracts_aggregate_readiness_and_templates(self) -> None:
        response = self.client.get("/system/deployment/bootstrap-contracts")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["available"])
        self.assertIn("linux_control_plane", payload["architecture_boundary"])
        self.assertEqual(
            payload["execution_bridge_template"]["path"],
            "/system/monitor/execution-bridge-health",
        )
        self.assertEqual(
            payload["report_paths"]["serving_latest_index"],
            "/system/reports/serving-latest-index",
        )
        self.assertEqual(
            payload["deployment_contract_sample"]["source_value_samples"]["windows_gateway_backup"]["deployment_role"],
            "backup_gateway",
        )
        self.assertIn("summary_lines", payload["serving_latest_index"])

    def test_linux_control_plane_startup_checklist_summarizes_bootstrap_and_latest_state(self) -> None:
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 15, 35, 0))
        result = runner.run(
            [
                {
                    "trade_id": "check-1",
                    "symbol": "600519.SH",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "exit_reason": "time_stop",
                    "return_pct": -0.02,
                    "trade_date": "2026-04-07",
                },
                {
                    "trade_id": "check-2",
                    "symbol": "000001.SZ",
                    "playbook": "low_absorb",
                    "regime": "range",
                    "exit_reason": "board_break",
                    "return_pct": 0.01,
                    "trade_date": "2026-04-07",
                },
            ]
        )
        self.client.post("/system/reports/offline-self-improvement/persist", json=result.model_dump())
        self.client.post(
            "/system/monitor/execution-bridge-health",
            json=build_execution_bridge_health_ingress_payload(
                {
                    "source_id": "windows-vm-a",
                    "deployment_role": "primary_gateway",
                    "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                    "gateway_online": True,
                    "qmt_connected": True,
                    "windows_execution_gateway": {"status": "healthy", "reachable": True},
                    "qmt_vm": {"status": "healthy", "reachable": True},
                }
            ),
        )
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 2, "auto_trade": False, "account_id": "sim-001"},
        )
        case = self.client.get("/system/cases").json()["items"][0]
        packet_request = {
            "trade_date": case["trade_date"],
            "expected_round": 1,
            "expected_agent_id": "ashare-research",
            "expected_case_ids": [case["case_id"]],
            "payload": {
                "output": {
                    "items": [
                        {
                            "symbol": case["symbol"],
                            "stance": "selected",
                            "reasons": ["研究支持"],
                            "evidence_refs": ["openclaw:startup:1"],
                        }
                    ]
                }
            },
        }
        replay_packet = self.client.post("/system/discussions/opinions/openclaw-replay-packet", json=packet_request).json()
        proposal_packet = self.client.post("/system/discussions/opinions/openclaw-proposal-packet", json=packet_request).json()
        self.client.post("/system/discussions/opinions/openclaw-replay-packet/archive", json={"payload": replay_packet})
        self.client.post("/system/discussions/opinions/openclaw-proposal-packet/archive", json={"payload": proposal_packet})

        response = self.client.get("/system/deployment/linux-control-plane-startup-checklist")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["checklist_scope"], "linux_control_plane_startup_checklist")
        self.assertIn(payload["status"], {"ready", "degraded"})
        check_map = {item["name"]: item for item in payload["checks"]}
        self.assertEqual(check_map["readiness"]["path"], "/system/readiness")
        self.assertEqual(
            check_map["execution_bridge_contract"]["path"],
            "/system/monitor/execution-bridge-health/template",
        )
        self.assertEqual(
            check_map["offline_self_improvement_descriptor"]["path"],
            "/system/reports/offline-self-improvement-descriptor",
        )
        self.assertEqual(
            check_map["postclose_handoff"]["path"],
            "/system/reports/postclose-deployment-handoff",
        )
        self.assertTrue(payload["summary_lines"])

    def test_windows_execution_gateway_onboarding_bundle_exposes_template_and_linux_paths(self) -> None:
        response = self.client.get("/system/deployment/windows-execution-gateway-onboarding-bundle")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["bundle_scope"], "windows_execution_gateway_onboarding_bundle")
        self.assertEqual(
            payload["execution_bridge_template"]["path"],
            "/system/monitor/execution-bridge-health",
        )
        self.assertEqual(
            payload["source_value_suggestions"]["windows_gateway"]["deployment_role"],
            "primary_gateway",
        )
        self.assertEqual(
            payload["worker_entrypoint"]["cli"],
            "ashare-execution-gateway-worker",
        )
        self.assertIn(
            "--executor-mode xtquant",
            payload["worker_entrypoint"]["recommended_xtquant_command"],
        )
        self.assertEqual(
            payload["worker_entrypoint"]["required_env"]["ASHARE_EXECUTION_PLANE"],
            "windows_gateway",
        )
        self.assertEqual(
            payload["linux_control_plane"]["startup_checklist_path"],
            "/system/deployment/linux-control-plane-startup-checklist",
        )
        self.assertEqual(
            payload["report_paths"]["postclose_deployment_handoff"],
            "/system/reports/postclose-deployment-handoff",
        )
        self.assertEqual(
            payload["deployment_contract_sample"]["http_samples"]["windows_gateway_post"]["path"],
            "/system/monitor/execution-bridge-health",
        )
        self.assertEqual(
            payload["deployment_contract_sample"]["request_samples"]["windows_gateway_backup_post_body"]["health"][
                "deployment_role"
            ],
            "backup_gateway",
        )
        self.assertTrue(payload["summary_lines"])

    def test_discussion_refresh_respects_focus_poll_gate(self) -> None:
        self.client.post("/system/config", json={"watch.focus_poll_seconds": 60})
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        trade_date = self.client.get("/system/cases").json()["items"][0]["trade_date"]
        self.client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date})
        self.client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start")

        first = self.client.post(f"/system/discussions/cycles/{trade_date}/refresh")
        first_payload = first.json()
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first_payload["ok"])
        self.assertFalse(first_payload["refresh_skipped"])
        self.assertTrue(first_payload["cadence_gate"]["triggered"])

        second = self.client.post(f"/system/discussions/cycles/{trade_date}/refresh")
        second_payload = second.json()
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second_payload["ok"])
        self.assertTrue(second_payload["refresh_skipped"])
        self.assertFalse(second_payload["cadence_gate"]["triggered"])
        self.assertEqual(second_payload["cadence_gate"]["layer"], "focus")

    def test_discussion_api_normalizes_numeric_confidence_and_stance_aliases(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        first_case = self.client.get("/system/cases").json()["items"][0]

        opinion = self.client.post(
            f"/system/cases/{first_case['case_id']}/opinions",
            json={
                "round": 1,
                "agent_id": "ashare-research",
                "stance": "watchlist",
                "confidence": 0.81,
                "reasons": ["研究层先放观察池"],
                "evidence_refs": ["research:alias"],
            },
        )
        payload = opinion.json()
        self.assertEqual(opinion.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["case"]["opinions"][0]["stance"], "watch")
        self.assertEqual(payload["case"]["opinions"][0]["confidence"], "high")

    def test_discussion_cycle_updates_monitor_pool_layers(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        cases = self.client.get("/system/cases").json()["items"]
        trade_date = cases[0]["trade_date"]

        bootstrap = self.client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date})
        self.assertEqual(bootstrap.status_code, 200)

        state = self.client.get("/monitor/state").json()
        latest_pool = state["latest_pool_snapshot"]
        self.assertEqual(latest_pool["trade_date"], trade_date)
        self.assertEqual(latest_pool["source"], "discussion_bootstrap")
        self.assertEqual(latest_pool["counts"]["focus_pool"], 4)

        summary = self.client.get("/monitor/changes/summary")
        summary_payload = summary.json()
        self.assertEqual(summary.status_code, 200)
        self.assertIn("title", summary_payload)
        self.assertIn("summary_text", summary_payload)

    def test_case_rebuild_generates_round_summary_and_gates(self) -> None:
        runtime = self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        case_id = self.client.get("/system/cases").json()["items"][0]["case_id"]

        self.client.post(
            f"/system/cases/{case_id}/opinions",
            json={
                "round": 1,
                "agent_id": "ashare-research",
                "stance": "support",
                "confidence": "high",
                "reasons": ["基本面与情绪支撑"],
                "evidence_refs": ["research-summary:1"],
            },
        )
        self.client.post(
            f"/system/cases/{case_id}/opinions",
            json={
                "round": 1,
                "agent_id": "ashare-risk",
                "stance": "limit",
                "confidence": "medium",
                "reasons": ["仓位需要限制"],
                "evidence_refs": ["risk-check:1"],
            },
        )
        self.client.post(
            f"/system/cases/{case_id}/opinions",
            json={
                "round": 1,
                "agent_id": "ashare-audit",
                "stance": "question",
                "confidence": "medium",
                "reasons": ["需要补充第二轮解释"],
                "evidence_refs": [],
            },
        )
        self.client.post(
            f"/system/cases/{case_id}/opinions",
            json={
                "round": 2,
                "agent_id": "ashare-audit",
                "stance": "support",
                "confidence": "medium",
                "reasons": ["第二轮已补充清楚"],
                "evidence_refs": ["audit-round2:1"],
            },
        )

        rebuilt = self.client.post(f"/system/cases/{case_id}/rebuild")
        payload = rebuilt.json()
        self.assertEqual(rebuilt.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("基本面与情绪支撑", payload["case"]["round_1_summary"]["support_points"])
        self.assertIn("仓位需要限制", payload["case"]["round_1_summary"]["oppose_points"])
        self.assertEqual(payload["case"]["risk_gate"], "limit")
        self.assertEqual(payload["case"]["audit_gate"], "clear")

    def test_agent_score_settlement_updates_scores(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        cases = self.client.get("/system/cases").json()["items"]
        first_case = cases[0]
        symbol = first_case["symbol"]
        case_id = first_case["case_id"]

        self.client.post(
            f"/system/cases/{case_id}/opinions",
            json={
                "round": 1,
                "agent_id": "ashare-research",
                "stance": "support",
                "confidence": "high",
                "reasons": ["研究明确看多"],
                "evidence_refs": ["research:bullish"],
            },
        )
        self.client.post(
            f"/system/cases/{case_id}/opinions",
            json={
                "round": 1,
                "agent_id": "ashare-risk",
                "stance": "limit",
                "confidence": "medium",
                "reasons": ["只能轻仓"],
                "evidence_refs": ["risk:light"],
            },
        )
        self.client.post(f"/system/cases/{case_id}/rebuild")

        settlement = self.client.post(
            "/system/agent-scores/settlements",
            json={
                "trade_date": first_case["trade_date"],
                "score_date": "2026-04-07",
                "outcomes": [
                    {
                        "symbol": symbol,
                        "next_day_close_pct": 0.035,
                        "note": "次日上涨",
                        "exit_reason": "time_stop",
                        "holding_days": 2,
                    }
                ],
            },
        )
        payload = settlement.json()
        self.assertEqual(settlement.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["count"], 2)
        self.assertIn("attribution", payload)
        self.assertTrue(payload["attribution"]["available"])
        self.assertEqual(payload["attribution"]["trade_count"], 1)
        self.assertEqual(payload["attribution"]["items"][0]["symbol"], symbol)
        self.assertEqual(payload["attribution"]["items"][0]["exit_reason"], "time_stop")
        self.assertGreaterEqual(len(payload["attribution"]["by_playbook"]), 1)

        scores = self.client.get("/system/agent-scores", params={"score_date": "2026-04-07"}).json()["items"]
        research = next(item for item in scores if item["agent_id"] == "ashare-research")
        self.assertGreater(research["new_score"], 10.0)

        attribution = self.client.get(
            "/system/learning/attribution",
            params={"trade_date": first_case["trade_date"], "score_date": "2026-04-07"},
        ).json()
        self.assertTrue(attribution["available"])
        self.assertEqual(attribution["trade_count"], 1)
        self.assertEqual(attribution["items"][0]["symbol"], symbol)
        self.assertIn("by_regime", attribution)
        self.assertGreaterEqual(len(attribution["by_regime"]), 1)

        review = self.client.get(
            "/system/learning/trade-review",
            params={"trade_date": first_case["trade_date"], "score_date": "2026-04-07"},
        ).json()
        self.assertTrue(review["available"])
        self.assertIn("review_summary", review)
        self.assertIn("summary_lines", review)
        self.assertGreaterEqual(len(review["summary_lines"]), 1)

    def test_batch_discussion_api_builds_trade_date_summary(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        cases = self.client.get("/system/cases").json()["items"]
        trade_date = cases[0]["trade_date"]

        batch = self.client.post(
            "/system/discussions/opinions/batch",
            json={
                "auto_rebuild": True,
                "items": [
                    {
                        "case_id": cases[0]["case_id"],
                        "round": 1,
                        "agent_id": "ashare-research",
                        "stance": "support",
                        "confidence": "high",
                        "reasons": ["研究支持入选"],
                        "evidence_refs": ["research:1"],
                    },
                    {
                        "case_id": cases[0]["case_id"],
                        "round": 1,
                        "agent_id": "ashare-strategy",
                        "stance": "support",
                        "confidence": "high",
                        "reasons": ["策略排序靠前"],
                        "evidence_refs": ["strategy:1"],
                    },
                    {
                        "case_id": cases[0]["case_id"],
                        "round": 1,
                        "agent_id": "ashare-risk",
                        "stance": "limit",
                        "confidence": "medium",
                        "reasons": ["控制单票仓位"],
                        "evidence_refs": ["risk:1"],
                    },
                    {
                        "case_id": cases[0]["case_id"],
                        "round": 1,
                        "agent_id": "ashare-audit",
                        "stance": "question",
                        "confidence": "medium",
                        "reasons": ["第二轮补证据"],
                        "evidence_refs": [],
                    },
                    {
                        "case_id": cases[1]["case_id"],
                        "round": 1,
                        "agent_id": "ashare-research",
                        "stance": "oppose",
                        "confidence": "medium",
                        "reasons": ["事件驱动不足"],
                        "evidence_refs": ["research:2"],
                    },
                ],
            },
        )
        batch_payload = batch.json()
        self.assertEqual(batch.status_code, 200)
        self.assertTrue(batch_payload["ok"])
        self.assertGreaterEqual(batch_payload["count"], 2)

        summary = self.client.get("/system/discussions/summary", params={"trade_date": trade_date})
        summary_payload = summary.json()
        self.assertEqual(summary.status_code, 200)
        self.assertTrue(summary_payload["ok"])
        self.assertEqual(summary_payload["trade_date"], trade_date)
        self.assertEqual(summary_payload["case_count"], 4)
        self.assertGreaterEqual(summary_payload["round_coverage"]["needs_round_2"], 1)
        self.assertGreaterEqual(summary_payload["audit_gate_counts"]["hold"], 1)
        self.assertIn("candidate_pool_lines", summary_payload)
        self.assertIn("summary_text", summary_payload)
        self.assertTrue(any(" " in line for line in summary_payload["candidate_pool_lines"]))

    def test_discussion_reason_board_returns_grouped_reasons_and_support(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        cases = self.client.get("/system/cases").json()["items"]
        trade_date = cases[0]["trade_date"]
        target = cases[0]

        self.client.post(
            "/system/discussions/opinions/batch",
            json={
                "auto_rebuild": True,
                "items": [
                    {
                        "case_id": target["case_id"],
                        "round": 1,
                        "agent_id": "ashare-research",
                        "stance": "support",
                        "confidence": "high",
                        "reasons": ["研究确认景气度提升"],
                        "evidence_refs": ["research:景气度"],
                    },
                    {
                        "case_id": target["case_id"],
                        "round": 1,
                        "agent_id": "ashare-strategy",
                        "stance": "support",
                        "confidence": "high",
                        "reasons": ["策略排序进入前列"],
                        "evidence_refs": ["strategy:排序"],
                    },
                    {
                        "case_id": target["case_id"],
                        "round": 1,
                        "agent_id": "ashare-risk",
                        "stance": "limit",
                        "confidence": "medium",
                        "reasons": ["风险建议轻仓观察"],
                        "evidence_refs": ["risk:轻仓"],
                    },
                    {
                        "case_id": target["case_id"],
                        "round": 2,
                        "agent_id": "ashare-audit",
                        "stance": "support",
                        "confidence": "medium",
                        "reasons": ["审计确认理由充分"],
                        "evidence_refs": ["audit:通过"],
                    },
                ],
            },
        )

        board = self.client.get("/system/discussions/reason-board", params={"trade_date": trade_date})
        payload = board.json()
        self.assertEqual(board.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["trade_date"], trade_date)
        self.assertEqual(payload["case_count"], 4)

        grouped = payload["selected"] + payload["watchlist"] + payload["rejected"]
        item = next(entry for entry in grouped if entry["case_id"] == target["case_id"])
        self.assertEqual(item["symbol"], target["symbol"])
        self.assertTrue(item["name"])
        self.assertIn("summary", item["runtime_snapshot"])
        self.assertIn("score_breakdown", item["runtime_snapshot"])
        self.assertIn("support_points", item["discussion"])
        self.assertIn("oppose_points", item["discussion"])
        self.assertIn("research确认景气度提升".replace("research", "研究"), "".join(item["discussion"]["support_points"]))
        self.assertIn("ashare-research", item["latest_opinions"])
        self.assertIn("ashare-strategy", item["latest_opinions"])
        self.assertGreaterEqual(item["opinion_count"], 4)

    def test_discussion_agent_packets_returns_shared_dossier_for_specialists(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        cases = self.client.get("/system/cases").json()["items"]
        trade_date = cases[0]["trade_date"]

        packets = self.client.get(
            "/system/discussions/agent-packets",
            params={"trade_date": trade_date, "agent_id": "ashare-risk", "limit": 2},
        )
        payload = packets.json()
        self.assertEqual(packets.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["trade_date"], trade_date)
        self.assertEqual(payload["requested_agent_id"], "ashare-risk")
        self.assertEqual(payload["case_count"], 2)
        self.assertIn("shared_context", payload)
        self.assertIn("shared_context_lines", payload)
        self.assertIn("workspace_context", payload)
        self.assertIn("workspace_summary_lines", payload)
        self.assertIn("data_catalog_ref", payload)
        self.assertIn("preferred_read_order", payload)
        self.assertIn("protocol", payload)
        self.assertEqual(payload["protocol"]["packet_type"], "agent_packets")
        self.assertEqual(payload["protocol"]["contract_version"], "quant-discussion-v1")
        self.assertIn("round_coverage", payload)
        self.assertIn("controversy_summary_lines", payload)
        self.assertIn("round_2_guidance", payload)
        self.assertGreaterEqual(len(payload["preferred_read_order"]), 3)
        self.assertIn(payload["shared_context"]["source_layer"], {"serving_pack", "serving_context", "missing"})
        self.assertIn("summary_lines", payload)

        item = payload["items"][0]
        self.assertTrue(item["name"])
        self.assertIn(item["name"], item["symbol_display"])
        self.assertIn("dossier", item)
        self.assertTrue(item["dossier"]["available"])
        self.assertIn(item["dossier"]["source_layer"], {"serving_pack", "feature_dossier"})
        self.assertIn("symbol_context", item["dossier"])
        self.assertIn("event_context", item["dossier"])
        self.assertIn("agent_focus", item)
        self.assertEqual(item["agent_focus"]["agent_id"], "ashare-risk")
        self.assertGreaterEqual(len(item["agent_focus"]["summary_lines"]), 1)
        self.assertIn("questions_for_round_2", item["agent_focus"]["key_points"])
        self.assertIn("remaining_disputes", item["agent_focus"]["key_points"])
        self.assertIn("round_2_substantive_ready", item["agent_focus"]["key_points"])

    def test_discussion_vote_board_returns_round_details_and_names(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        cases = self.client.get("/system/cases").json()["items"]
        trade_date = cases[0]["trade_date"]
        target = cases[0]

        self.client.post(
            "/system/discussions/opinions/batch",
            json={
                "auto_rebuild": True,
                "items": [
                    {
                        "case_id": target["case_id"],
                        "round": 1,
                        "agent_id": "ashare-research",
                        "stance": "support",
                        "confidence": "high",
                        "reasons": ["研究确认景气度修复"],
                        "evidence_refs": ["research:round1"],
                    },
                    {
                        "case_id": target["case_id"],
                        "round": 1,
                        "agent_id": "ashare-risk",
                        "stance": "limit",
                        "confidence": "medium",
                        "reasons": ["风控建议轻仓试错"],
                        "evidence_refs": ["risk:round1"],
                    },
                    {
                        "case_id": target["case_id"],
                        "round": 2,
                        "agent_id": "ashare-audit",
                        "stance": "support",
                        "confidence": "medium",
                        "reasons": ["审计确认补证完成"],
                        "evidence_refs": ["audit:round2"],
                    },
                ],
            },
        )

        vote_board = self.client.get(
            "/system/discussions/vote-board",
            params={"trade_date": trade_date, "limit": 10},
        )
        payload = vote_board.json()
        self.assertEqual(vote_board.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["trade_date"], trade_date)
        self.assertIn("summary_lines", payload)
        self.assertGreaterEqual(payload["case_count"], 1)

        item = next(entry for entry in payload["items"] if entry["case_id"] == target["case_id"])
        self.assertEqual(item["symbol"], target["symbol"])
        self.assertTrue(item["name"])
        self.assertIn(target["symbol"], item["symbol_display"])
        self.assertIn(item["name"], item["symbol_display"])
        self.assertIn("headline_line", item)
        self.assertIn("状态=", item["headline_line"])
        self.assertIn("rounds", item)
        self.assertIn("round_1", item["rounds"])
        self.assertIn("round_2", item["rounds"])
        self.assertIn("discussion_digest_lines", item)
        self.assertTrue(any("研究(" in line for line in item["rounds"]["round_1"]["lines"]))
        self.assertTrue(any("风控(" in line for line in item["rounds"]["round_1"]["lines"]))
        self.assertTrue(any("审计(" in line for line in item["rounds"]["round_2"]["lines"]))
        self.assertTrue(any(opinion["agent_display"] == "研究" for opinion in item["opinion_timeline"]))

    def test_case_vote_detail_returns_per_agent_reasons(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        target = self.client.get("/system/cases").json()["items"][0]

        self.client.post(
            f"/system/cases/{target['case_id']}/opinions",
            json={
                "round": 1,
                "agent_id": "ashare-strategy",
                "stance": "support",
                "confidence": "high",
                "reasons": ["策略排序保持前二"],
                "evidence_refs": ["strategy:single"],
            },
        )
        self.client.post(
            f"/system/cases/{target['case_id']}/opinions",
            json={
                "round": 1,
                "agent_id": "ashare-audit",
                "stance": "question",
                "confidence": "medium",
                "reasons": ["审计要求补充成交量证据"],
                "evidence_refs": ["audit:single"],
            },
        )
        self.client.post(f"/system/cases/{target['case_id']}/rebuild")

        detail = self.client.get(f"/system/cases/{target['case_id']}/vote-detail")
        payload = detail.json()
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(payload["ok"])
        case = payload["case"]
        self.assertEqual(case["case_id"], target["case_id"])
        self.assertEqual(case["symbol"], target["symbol"])
        self.assertTrue(case["name"])
        self.assertIn("round_1", case["rounds"])
        self.assertEqual(len(case["rounds"]["round_1"]["items"]), 2)
        self.assertTrue(any(item["agent_display"] == "策略" for item in case["rounds"]["round_1"]["items"]))
        self.assertTrue(any(item["agent_display"] == "审计" for item in case["rounds"]["round_1"]["items"]))
        self.assertTrue(any("成交量证据" in line for line in case["rounds"]["round_1"]["lines"]))
        self.assertIn("discussion_digest", case)
        self.assertIn("discussion_digest_lines", case)
        self.assertIn("latest_opinions", case)
        self.assertIn("ashare-audit", case["latest_opinions"])
        self.assertEqual(case["latest_opinions"]["ashare-audit"]["stance"], "question")

    def test_discussion_reply_pack_returns_reply_ready_lines(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        cases = self.client.get("/system/cases").json()["items"]
        trade_date = cases[0]["trade_date"]
        target = cases[0]

        self.client.post(
            "/system/discussions/opinions/batch",
            json={
                "auto_rebuild": True,
                "items": [
                    {
                        "case_id": target["case_id"],
                        "round": 1,
                        "agent_id": "ashare-research",
                        "stance": "support",
                        "confidence": "high",
                        "reasons": ["研究侧支持继续跟踪"],
                        "evidence_refs": ["research:1"],
                    },
                    {
                        "case_id": target["case_id"],
                        "round": 1,
                        "agent_id": "ashare-strategy",
                        "stance": "support",
                        "confidence": "high",
                        "reasons": ["策略排序仍在前列"],
                        "evidence_refs": ["strategy:1"],
                    },
                    {
                        "case_id": target["case_id"],
                        "round": 1,
                        "agent_id": "ashare-risk",
                        "stance": "allow",
                        "confidence": "medium",
                        "reasons": ["风控允许进入执行池"],
                        "evidence_refs": ["risk:1"],
                    },
                    {
                        "case_id": target["case_id"],
                        "round": 1,
                        "agent_id": "ashare-audit",
                        "stance": "support",
                        "confidence": "medium",
                        "reasons": ["审计确认结论可用"],
                        "evidence_refs": ["audit:1"],
                    },
                ],
            },
        )

        reply_pack = self.client.get(
            "/system/discussions/reply-pack",
            params={"trade_date": trade_date, "selected_limit": 2, "watchlist_limit": 2, "rejected_limit": 2},
        )
        payload = reply_pack.json()
        self.assertEqual(reply_pack.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["trade_date"], trade_date)
        self.assertIn("overview_lines", payload)
        self.assertGreaterEqual(len(payload["overview_lines"]), 1)
        self.assertIn("selected_lines", payload)
        self.assertIsInstance(payload["selected_lines"], list)
        self.assertIn("selected_display", payload)
        self.assertIsInstance(payload["selected_display"], list)
        self.assertIn("shared_context", payload)
        self.assertIn("shared_context_lines", payload)
        self.assertIn("data_catalog_ref", payload)
        self.assertIn("selected_packets", payload)
        self.assertIn("watchlist_packets", payload)
        self.assertIn("rejected_packets", payload)
        combined_lines = payload["selected_lines"] + payload["watchlist_lines"] + payload["rejected_lines"]
        self.assertTrue(any(target["symbol"] in line for line in combined_lines))
        self.assertTrue(any("风控=" in line and "审计=" in line for line in combined_lines))
        combined_display = payload["selected_display"] + payload["watchlist_display"] + payload["rejected_display"]
        self.assertTrue(any(target["symbol"] in line and " " in line for line in combined_display))
        combined_packets = payload["selected_packets"] + payload["watchlist_packets"] + payload["rejected_packets"]
        self.assertTrue(any(packet["symbol"] == target["symbol"] for packet in combined_packets))

    def test_discussion_final_brief_returns_short_answer_payload(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        cases = self.client.get("/system/cases").json()["items"]
        trade_date = cases[0]["trade_date"]
        target = cases[0]

        self.client.post(
            "/system/discussions/opinions/batch",
            json={
                "auto_rebuild": True,
                "items": [
                    {
                        "case_id": target["case_id"],
                        "round": 1,
                        "agent_id": "ashare-research",
                        "stance": "support",
                        "confidence": "high",
                        "reasons": ["研究确认可继续跟踪"],
                        "evidence_refs": ["research:1"],
                    },
                    {
                        "case_id": target["case_id"],
                        "round": 1,
                        "agent_id": "ashare-strategy",
                        "stance": "support",
                        "confidence": "high",
                        "reasons": ["策略确认排位靠前"],
                        "evidence_refs": ["strategy:1"],
                    },
                    {
                        "case_id": target["case_id"],
                        "round": 1,
                        "agent_id": "ashare-risk",
                        "stance": "allow",
                        "confidence": "medium",
                        "reasons": ["风控通过"],
                        "evidence_refs": ["risk:1"],
                    },
                    {
                        "case_id": target["case_id"],
                        "round": 1,
                        "agent_id": "ashare-audit",
                        "stance": "support",
                        "confidence": "medium",
                        "reasons": ["审计通过"],
                        "evidence_refs": ["audit:1"],
                    },
                ],
            },
        )

        brief = self.client.get(
            "/system/discussions/final-brief",
            params={"trade_date": trade_date, "selection_limit": 2},
        )
        payload = brief.json()
        self.assertEqual(brief.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertIn(payload["status"], {"ready", "blocked"})
        self.assertIn("lines", payload)
        self.assertGreaterEqual(len(payload["lines"]), 1)
        self.assertIn("summary_text", payload)
        self.assertIn("selection_display", payload)
        self.assertIn("shared_context", payload)
        self.assertIn("shared_context_lines", payload)
        self.assertIn("data_catalog_ref", payload)
        self.assertIn("selection_packets", payload)
        self.assertIn("watchlist_packets", payload)
        self.assertIn("rejected_packets", payload)
        self.assertTrue(any(target["symbol"] in line for line in payload["lines"]))
        self.assertTrue(all(" " in line for line in payload["selection_display"]))
        grouped_packets = payload["selection_packets"] + payload["watchlist_packets"] + payload["rejected_packets"]
        self.assertTrue(any(packet["symbol"] == target["symbol"] for packet in grouped_packets))

    def test_batch_discussion_api_normalizes_alias_payloads(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        cases = self.client.get("/system/cases").json()["items"]

        batch = self.client.post(
            "/system/discussions/opinions/batch",
            json={
                "auto_rebuild": True,
                "items": [
                    {
                        "case_id": cases[0]["case_id"],
                        "round": 1,
                        "agent_id": "ashare-risk",
                        "stance": "allow",
                        "confidence": 0.92,
                        "reasons": ["风险侧放行"],
                        "evidence_refs": ["risk:alias"],
                    },
                    {
                        "case_id": cases[0]["case_id"],
                        "round": 1,
                        "agent_id": "ashare-audit",
                        "stance": "approved",
                        "confidence": 0.48,
                        "reasons": ["审计认可当前证据"],
                        "evidence_refs": ["audit:alias"],
                    },
                ],
            },
        )
        payload = batch.json()
        self.assertEqual(batch.status_code, 200)
        self.assertTrue(payload["ok"])

        case = next(item for item in payload["items"] if item["case_id"] == cases[0]["case_id"])
        stances = [item["stance"] for item in case["opinions"]]
        confidences = [item["confidence"] for item in case["opinions"]]
        self.assertIn("support", stances)
        self.assertIn("high", confidences)
        self.assertIn("medium", confidences)

    def test_agent_scores_are_seeded_by_default(self) -> None:
        response = self.client.get("/system/agent-scores")
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["count"], 4)
        agent_ids = {item["agent_id"] for item in payload["items"]}
        self.assertEqual(agent_ids, {"ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"})
        for item in payload["items"]:
            self.assertEqual(item["new_score"], 10.0)
            self.assertEqual(item["weight_bucket"], "normal")

    def test_discussion_cycle_supports_two_round_flow_and_finalize(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        cases = self.client.get("/system/cases").json()["items"]
        trade_date = cases[0]["trade_date"]

        bootstrap = self.client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date})
        bootstrap_payload = bootstrap.json()
        self.assertEqual(bootstrap.status_code, 200)
        self.assertTrue(bootstrap_payload["ok"])
        self.assertEqual(bootstrap_payload["cycle"]["discussion_state"], "idle")
        self.assertEqual(bootstrap_payload["cycle"]["pool_state"], "base_pool_ready")

        round_1 = self.client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start")
        round_1_payload = round_1.json()
        self.assertEqual(round_1.status_code, 200)
        self.assertTrue(round_1_payload["ok"])
        self.assertEqual(round_1_payload["cycle"]["discussion_state"], "round_1_running")

        round_1_items = []
        agent_reasons = {
            "ashare-research": "研究支持入池",
            "ashare-strategy": "策略排序靠前",
            "ashare-risk": "风险可控",
            "ashare-audit": "证据链完整",
        }
        for index, case in enumerate(cases):
            for agent_id, reason in agent_reasons.items():
                stance = "support"
                reasons = [reason]
                if index == 0 and agent_id == "ashare-risk":
                    stance = "limit"
                    reasons = ["仓位需要二轮确认"]
                if index == 0 and agent_id == "ashare-audit":
                    stance = "question"
                    reasons = ["补充第二轮说明"]
                round_1_items.append(
                    {
                        "case_id": case["case_id"],
                        "round": 1,
                        "agent_id": agent_id,
                        "stance": stance,
                        "confidence": "high" if agent_id in {"ashare-research", "ashare-strategy"} else "medium",
                        "reasons": reasons,
                        "evidence_refs": [f"{agent_id}:round1:{case['symbol']}"],
                    }
                )

        batch_1 = self.client.post(
            "/system/discussions/opinions/batch",
            json={"auto_rebuild": True, "items": round_1_items},
        )
        batch_1_payload = batch_1.json()
        self.assertEqual(batch_1.status_code, 200)
        self.assertTrue(batch_1_payload["ok"])

        refresh_1 = self.client.post(f"/system/discussions/cycles/{trade_date}/refresh")
        refresh_1_payload = refresh_1.json()
        self.assertEqual(refresh_1.status_code, 200)
        self.assertTrue(refresh_1_payload["ok"])
        self.assertEqual(refresh_1_payload["cycle"]["discussion_state"], "round_1_summarized")
        self.assertEqual(refresh_1_payload["cycle"]["pool_state"], "focus_pool_ready")

        round_2 = self.client.post(f"/system/discussions/cycles/{trade_date}/rounds/2/start")
        round_2_payload = round_2.json()
        self.assertEqual(round_2.status_code, 200)
        self.assertTrue(round_2_payload["ok"])
        self.assertIn(cases[0]["case_id"], round_2_payload["cycle"]["round_2_target_case_ids"])

        round_2_items = [
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-research",
                "stance": "support",
                "confidence": "high",
                "reasons": ["第二轮补充行业催化"],
                "evidence_refs": ["research:round2"],
                "challenged_by": ["ashare-audit"],
                "resolved_questions": ["行业催化已补证"],
            },
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-strategy",
                "stance": "support",
                "confidence": "high",
                "reasons": ["二轮后仍在前列"],
                "evidence_refs": ["strategy:round2"],
                "challenged_by": ["ashare-risk"],
                "resolved_questions": ["排序优势已再次确认"],
            },
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-risk",
                "stance": "support",
                "confidence": "medium",
                "reasons": ["风险条件已澄清"],
                "evidence_refs": ["risk:round2"],
                "previous_stance": "limit",
                "changed": True,
                "changed_because": ["仓位限制条件已明确"],
            },
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-audit",
                "stance": "support",
                "confidence": "medium",
                "reasons": ["审计复核通过"],
                "evidence_refs": ["audit:round2"],
                "previous_stance": "question",
                "resolved_questions": ["证据链补齐"],
            },
        ]
        batch_2 = self.client.post(
            "/system/discussions/opinions/batch",
            json={"auto_rebuild": True, "items": round_2_items},
        )
        batch_2_payload = batch_2.json()
        self.assertEqual(batch_2.status_code, 200)
        self.assertTrue(batch_2_payload["ok"])

        refresh_2 = self.client.post(f"/system/discussions/cycles/{trade_date}/refresh")
        refresh_2_payload = refresh_2.json()
        self.assertEqual(refresh_2.status_code, 200)
        self.assertTrue(refresh_2_payload["ok"])
        self.assertEqual(refresh_2_payload["cycle"]["discussion_state"], "final_review_ready")
        self.assertEqual(refresh_2_payload["cycle"]["pool_state"], "execution_pool_building")

        finalize = self.client.post(f"/system/discussions/cycles/{trade_date}/finalize")
        finalize_payload = finalize.json()
        self.assertEqual(finalize.status_code, 200)
        self.assertTrue(finalize_payload["ok"])
        self.assertEqual(finalize_payload["cycle"]["discussion_state"], "final_selection_ready")
        self.assertEqual(finalize_payload["cycle"]["pool_state"], "execution_pool_ready")
        self.assertGreater(len(finalize_payload["cycle"]["execution_pool_case_ids"]), 0)
        self.assertIn("execution_precheck", finalize_payload)
        self.assertEqual(finalize_payload["execution_precheck"]["trade_date"], trade_date)
        self.assertTrue(finalize_payload["execution_precheck"]["available"])
        self.assertGreater(len(finalize_payload["execution_precheck"]["items"]), 0)
        self.assertIn("approved", finalize_payload["execution_precheck"]["items"][0])
        self.assertIn("execution_intents", finalize_payload)
        self.assertGreaterEqual(finalize_payload["execution_intents"]["intent_count"], 1)
        self.assertIn("request", finalize_payload["execution_intents"]["intents"][0])
        self.assertIn("client_brief", finalize_payload)
        self.assertIn("finalize_packet", finalize_payload)
        self.assertEqual(finalize_payload["finalize_packet"]["protocol"]["packet_type"], "finalize_packet")
        self.assertEqual(finalize_payload["client_brief"]["trade_date"], trade_date)
        self.assertIn(finalize_payload["client_brief"]["status"], {"ready", "blocked"})
        self.assertIn("lines", finalize_payload["client_brief"])
        self.assertGreaterEqual(len(finalize_payload["client_brief"]["lines"]), 1)
        meeting_context = self.client.get("/system/discussions/meeting-context", params={"trade_date": trade_date}).json()
        self.assertTrue(meeting_context["ok"])
        self.assertTrue(meeting_context["available"])
        self.assertEqual(meeting_context["trade_date"], trade_date)
        self.assertIn("protocol", meeting_context)
        self.assertEqual(meeting_context["protocol"]["packet_type"], "meeting_context")
        self.assertIn("reply_pack", meeting_context)
        self.assertIn("final_brief", meeting_context)
        self.assertIn("client_brief", meeting_context)
        self.assertIn("finalize_packet", meeting_context)
        self.assertIn("shared_context", meeting_context)
        self.assertIn("controversy_summary_lines", meeting_context)
        self.assertIn("round_2_guidance", meeting_context)
        self.assertIn("challenge_exchange_lines", meeting_context["client_brief"])
        self.assertIn("summary_lines", meeting_context)
        finalize_packet = self.client.get("/system/discussions/finalize-packet", params={"trade_date": trade_date}).json()
        self.assertTrue(finalize_packet["ok"])
        self.assertEqual(finalize_packet["trade_date"], trade_date)
        self.assertEqual(finalize_packet["protocol"]["packet_type"], "finalize_packet")
        self.assertIn("execution_precheck", finalize_packet)
        self.assertIn("execution_intents", finalize_packet)
        self.assertIn("client_brief", finalize_packet)
        discussion_context = self.client.get("/data/discussion-context/latest").json()
        self.assertTrue(discussion_context["available"])
        self.assertEqual(discussion_context["trade_date"], trade_date)
        self.assertIn("controversy_summary_lines", discussion_context)
        self.assertIn("round_2_guidance", discussion_context)
        monitor_discussion = self.client.get("/monitor/discussion/latest").json()
        self.assertTrue(monitor_discussion["available"])
        self.assertEqual(monitor_discussion["trade_date"], trade_date)
        workspace_context = self.client.get("/system/workspace-context").json()
        self.assertTrue(workspace_context["available"])
        self.assertEqual(workspace_context["trade_date"], trade_date)
        self.assertIn("discussion_context", workspace_context)
        self.assertIn("monitor_context", workspace_context)
        self.assertIn("runtime_context", workspace_context)
        self.assertEqual(workspace_context["discussion_context"]["trade_date"], trade_date)
        self.assertEqual(
            finalize_payload["execution_intents"]["intents"][0]["request"]["decision_id"],
            finalize_payload["execution_intents"]["intents"][0]["case_id"],
        )
        first_precheck_item = finalize_payload["execution_precheck"]["items"][0]
        self.assertLessEqual(first_precheck_item["proposed_value"], first_precheck_item["cash_available"])
        self.assertLessEqual(first_precheck_item["proposed_value"], first_precheck_item["max_single_amount"])

        precheck = self.client.get("/system/discussions/execution-precheck", params={"trade_date": trade_date})
        precheck_payload = precheck.json()
        self.assertEqual(precheck.status_code, 200)
        self.assertTrue(precheck_payload["ok"])
        self.assertEqual(precheck_payload["trade_date"], trade_date)
        self.assertIn("summary_lines", precheck_payload)
        self.assertIn("cash=", precheck_payload["summary_lines"][1])
        self.assertGreaterEqual(precheck_payload["approved_count"], 1)

        intents = self.client.get("/system/discussions/execution-intents", params={"trade_date": trade_date})
        intents_payload = intents.json()
        self.assertEqual(intents.status_code, 200)
        self.assertTrue(intents_payload["ok"])
        self.assertEqual(intents_payload["trade_date"], trade_date)
        self.assertGreaterEqual(intents_payload["intent_count"], 1)
        self.assertIn("summary_lines", intents_payload)
        first_intent = intents_payload["intents"][0]
        self.assertEqual(first_intent["request"]["trade_date"], trade_date)
        self.assertEqual(first_intent["request"]["playbook"], first_intent["playbook"])
        self.assertEqual(first_intent["request"]["regime"], first_intent["regime"])
        self.assertTrue(first_intent["request"]["playbook"])
        self.assertIn(first_intent["request"]["regime"], {"trend", "rotation", "defensive", "chaos"})

        cycle_detail = self.client.get(f"/system/discussions/cycles/{trade_date}")
        cycle_detail_payload = cycle_detail.json()
        self.assertEqual(cycle_detail.status_code, 200)
        self.assertEqual(cycle_detail_payload["discussion_state"], "final_selection_ready")
        self.assertEqual(cycle_detail_payload["trade_date"], trade_date)

    def test_round_2_requires_substantive_response_before_completion(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        cases = self.client.get("/system/cases").json()["items"]
        trade_date = cases[0]["trade_date"]

        self.client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date})
        self.client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start")

        round_1_items = []
        for index, case in enumerate(cases):
            for agent_id in ("ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"):
                stance = "support"
                reasons = [f"{agent_id} round1 pass"]
                if index == 0 and agent_id == "ashare-risk":
                    stance = "limit"
                    reasons = ["仓位需要二轮确认"]
                if index == 0 and agent_id == "ashare-audit":
                    stance = "question"
                    reasons = ["需要补充第二轮回应"]
                round_1_items.append(
                    {
                        "case_id": case["case_id"],
                        "round": 1,
                        "agent_id": agent_id,
                        "stance": stance,
                        "confidence": "high" if agent_id in {"ashare-research", "ashare-strategy"} else "medium",
                        "reasons": reasons,
                        "evidence_refs": [f"{agent_id}:round1:{case['symbol']}"],
                    }
                )

        self.client.post("/system/discussions/opinions/batch", json={"auto_rebuild": True, "items": round_1_items})
        self.client.post(f"/system/discussions/cycles/{trade_date}/refresh")
        self.client.post(f"/system/discussions/cycles/{trade_date}/rounds/2/start")

        shallow_round_2_items = [
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-research",
                "stance": "support",
                "confidence": "high",
                "reasons": ["research round2 shallow"],
                "evidence_refs": ["research:round2"],
            },
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-strategy",
                "stance": "support",
                "confidence": "high",
                "reasons": ["strategy round2 shallow"],
                "evidence_refs": ["strategy:round2"],
            },
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-risk",
                "stance": "support",
                "confidence": "medium",
                "reasons": ["risk round2 shallow"],
                "evidence_refs": ["risk:round2"],
            },
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-audit",
                "stance": "support",
                "confidence": "medium",
                "reasons": ["audit round2 shallow"],
                "evidence_refs": ["audit:round2"],
            },
        ]
        self.client.post("/system/discussions/opinions/batch", json={"auto_rebuild": True, "items": shallow_round_2_items})

        refresh = self.client.post(f"/system/discussions/cycles/{trade_date}/refresh")
        refresh_payload = refresh.json()
        self.assertEqual(refresh.status_code, 200)
        self.assertTrue(refresh_payload["ok"])
        self.assertEqual(refresh_payload["cycle"]["discussion_state"], "round_2_running")

        summary = self.client.get("/system/discussions/summary", params={"trade_date": trade_date})
        summary_payload = summary.json()
        self.assertEqual(summary.status_code, 200)
        self.assertTrue(summary_payload["ok"])
        self.assertEqual(summary_payload["round_coverage"]["round_2_ready"], 1)
        self.assertEqual(summary_payload["round_coverage"]["round_2_substantive_ready"], 0)
        self.assertIn(cases[0]["case_id"], summary_payload["substantive_gap_case_ids"])
        self.assertTrue(any("二轮待实质回应" in line for line in summary_payload["summary_lines"]))

    def test_finalize_skips_when_round_2_discussion_is_not_ready(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        cases = self.client.get("/system/cases").json()["items"]
        trade_date = cases[0]["trade_date"]

        self.client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date})
        self.client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start")
        self.client.post(
            "/system/discussions/opinions/batch",
            json={
                "auto_rebuild": True,
                "items": [
                    {
                        "case_id": case["case_id"],
                        "round": 1,
                        "agent_id": agent_id,
                        "stance": (
                            "limit"
                            if case["case_id"] == cases[0]["case_id"] and agent_id == "ashare-risk"
                            else "question"
                            if case["case_id"] == cases[0]["case_id"] and agent_id == "ashare-audit"
                            else "support"
                        ),
                        "confidence": "medium",
                        "reasons": [f"{agent_id} round1"],
                        "evidence_refs": [f"{agent_id}:round1:{case['symbol']}"],
                    }
                    for case in cases
                    for agent_id in ("ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit")
                ],
            },
        )
        self.client.post(f"/system/discussions/cycles/{trade_date}/refresh")
        self.client.post(f"/system/discussions/cycles/{trade_date}/rounds/2/start")

        finalize = self.client.post(f"/system/discussions/cycles/{trade_date}/finalize")
        finalize_payload = finalize.json()
        self.assertEqual(finalize.status_code, 200)
        self.assertTrue(finalize_payload["ok"])
        self.assertTrue(finalize_payload["finalize_skipped"])
        self.assertEqual(finalize_payload["notification"]["reason"], "discussion_not_ready")
        self.assertEqual(finalize_payload["cycle"]["discussion_state"], "round_2_running")
        self.assertEqual(finalize_payload["cadence_gate"]["reason"], "discussion_not_ready")

    def test_live_execution_precheck_adds_market_safety_blockers_and_quote_fields(self) -> None:
        os.environ["ASHARE_RUN_MODE"] = "live"
        os.environ["ASHARE_MARKET_MODE"] = "mock"
        os.environ["ASHARE_EXECUTION_MODE"] = "mock"
        os.environ["ASHARE_LIVE_ENABLE"] = "true"
        self.client.close()
        reset_container()
        self.client = ASGISyncClient(create_app())

        trade_date, _ = self._prepare_finalized_discussion()
        self.client.post("/system/config", json={"execution_price_deviation_pct": 0.02})

        with patch("ashare_system.apps.system_api.is_trading_session", return_value=False):
            response = self.client.get("/system/discussions/execution-precheck", params={"trade_date": trade_date})

        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["trade_date"], trade_date)
        self.assertIn("实盘检查", payload["summary_lines"][-1])
        self.assertGreater(len(payload["items"]), 0)

        first = payload["items"][0]
        self.assertFalse(first["session_open"])
        self.assertIn("trading_session_closed", first["blockers"])
        self.assertIsNotNone(first["quote_timestamp"])
        self.assertEqual(first["max_price_deviation_pct"], 0.02)
        self.assertIn("snapshot_age_seconds", first)
        self.assertIn("snapshot_is_fresh", first)
        self.assertIn("bid_price", first)
        self.assertIn("ask_price", first)
        self.assertIn("pre_close", first)

    def test_discussion_finalize_respects_execution_poll_gate(self) -> None:
        self.client.post("/system/config", json={"watch.execution_poll_seconds": 60})
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 4, "auto_trade": False, "account_id": "sim-001"},
        )
        cases = self.client.get("/system/cases").json()["items"]
        trade_date = cases[0]["trade_date"]

        self.client.post("/system/discussions/cycles/bootstrap", json={"trade_date": trade_date})
        self.client.post(f"/system/discussions/cycles/{trade_date}/rounds/1/start")

        round_1_items = []
        for case in cases:
            for agent_id in ("ashare-research", "ashare-strategy", "ashare-risk", "ashare-audit"):
                round_1_items.append(
                    {
                        "case_id": case["case_id"],
                        "round": 1,
                        "agent_id": agent_id,
                        "stance": "support",
                        "confidence": "high" if agent_id != "ashare-audit" else "medium",
                        "reasons": [f"{agent_id} round1 pass"],
                        "evidence_refs": [f"{agent_id}:round1:{case['symbol']}"],
                    }
                )
        self.client.post("/system/discussions/opinions/batch", json={"auto_rebuild": True, "items": round_1_items})
        self.client.post(f"/system/discussions/cycles/{trade_date}/refresh")
        self.client.post(f"/system/discussions/cycles/{trade_date}/rounds/2/start")

        round_2_items = [
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-research",
                "stance": "support",
                "confidence": "high",
                "reasons": ["research round2 pass"],
                "evidence_refs": ["research:round2"],
                "challenged_by": ["ashare-audit"],
                "resolved_questions": ["研究补证已完成"],
            },
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-strategy",
                "stance": "support",
                "confidence": "high",
                "reasons": ["strategy round2 pass"],
                "evidence_refs": ["strategy:round2"],
                "challenged_by": ["ashare-risk"],
                "resolved_questions": ["排序逻辑已补充"],
            },
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-risk",
                "stance": "support",
                "confidence": "medium",
                "reasons": ["risk round2 pass"],
                "evidence_refs": ["risk:round2"],
                "previous_stance": "limit",
                "changed": True,
                "changed_because": ["风险条件已澄清"],
            },
            {
                "case_id": cases[0]["case_id"],
                "round": 2,
                "agent_id": "ashare-audit",
                "stance": "support",
                "confidence": "medium",
                "reasons": ["audit round2 pass"],
                "evidence_refs": ["audit:round2"],
                "previous_stance": "question",
                "resolved_questions": ["审计问题已关闭"],
            },
        ]
        self.client.post("/system/discussions/opinions/batch", json={"auto_rebuild": True, "items": round_2_items})
        self.client.post(f"/system/discussions/cycles/{trade_date}/refresh")

        first = self.client.post(f"/system/discussions/cycles/{trade_date}/finalize")
        first_payload = first.json()
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first_payload["ok"])
        self.assertFalse(first_payload["finalize_skipped"])
        self.assertTrue(first_payload["cadence_gate"]["triggered"])
        self.assertEqual(first_payload["cycle"]["discussion_state"], "final_selection_ready")

        second = self.client.post(f"/system/discussions/cycles/{trade_date}/finalize")
        second_payload = second.json()
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second_payload["ok"])
        self.assertFalse(second_payload["finalize_skipped"])
        self.assertEqual(second_payload["cycle"]["discussion_state"], "final_selection_ready")

    def test_execution_intent_dispatch_preview_returns_standard_receipts(self) -> None:
        trade_date, finalize_payload = self._prepare_finalized_discussion()
        first_intent = finalize_payload["execution_intents"]["intents"][0]

        response = self.client.post(
            "/system/discussions/execution-intents/dispatch",
            json={
                "trade_date": trade_date,
                "account_id": "sim-001",
                "intent_ids": [first_intent["intent_id"]],
                "apply": False,
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "preview")
        self.assertEqual(payload["preview_count"], 1)
        self.assertEqual(payload["submitted_count"], 0)
        preview_receipts = [item for item in payload["receipts"] if item["status"] == "preview"]
        self.assertEqual(len(preview_receipts), 1)
        self.assertEqual(preview_receipts[0]["intent_id"], first_intent["intent_id"])
        self.assertEqual(preview_receipts[0]["request"]["decision_id"], first_intent["case_id"])
        self.assertIn("execution_intents", payload)
        self.assertIn("summary_lines", payload)
        self.assertIn("summary_notification", payload)
        self.assertIn(payload["summary_notification"]["reason"], {"sent", "dispatch_failed", "dispatcher_unavailable"})
        self.assertEqual(payload["summary_notification"]["title"], "执行预演回执")

    def test_execution_intent_dispatch_preview_pushes_summary_notification(self) -> None:
        trade_date, finalize_payload = self._prepare_finalized_discussion()
        first_intent = finalize_payload["execution_intents"]["intents"][0]
        captured: list[dict] = []

        def fake_dispatch(_self, title: str, content: str, level: str = "info", force: bool = True) -> bool:
            captured.append(
                {
                    "title": title,
                    "content": content,
                    "level": level,
                    "force": force,
                }
            )
            return True

        with patch("ashare_system.notify.dispatcher.MessageDispatcher.dispatch_trade", new=fake_dispatch):
            response = self.client.post(
                "/system/discussions/execution-intents/dispatch",
                json={
                    "trade_date": trade_date,
                    "account_id": "sim-001",
                    "intent_ids": [first_intent["intent_id"]],
                    "apply": False,
                },
            )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary_notification"]["reason"], "sent")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["title"], "执行预演回执")
        self.assertEqual(captured[0]["level"], "info")
        self.assertIn("预演", captured[0]["content"])
        self.assertIn(first_intent["symbol"], captured[0]["content"])

    def test_execution_intent_dispatch_apply_submits_mock_orders(self) -> None:
        trade_date, finalize_payload = self._prepare_finalized_discussion()
        intent_ids = [item["intent_id"] for item in finalize_payload["execution_intents"]["intents"]]

        response = self.client.post(
            "/system/discussions/execution-intents/dispatch",
            json={
                "trade_date": trade_date,
                "account_id": "sim-001",
                "intent_ids": intent_ids,
                "apply": True,
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "submitted")
        self.assertGreaterEqual(payload["submitted_count"], 1)
        submitted = [item for item in payload["receipts"] if item["status"] == "submitted"]
        self.assertGreaterEqual(len(submitted), 1)
        self.assertEqual(submitted[0]["reason"], "sent")
        self.assertIn("order", submitted[0])
        self.assertEqual(submitted[0]["order"]["status"], "PENDING")
        self.assertTrue(submitted[0]["request"]["playbook"])
        self.assertIn(submitted[0]["request"]["regime"], {"trend", "rotation", "defensive", "chaos"})

        orders = self.client.get("/execution/orders/sim-001").json()
        self.assertGreaterEqual(len(orders), payload["submitted_count"])
        meeting_state = get_meeting_state_store()
        journal = meeting_state.get("execution_order_journal", [])
        journal_item = next(item for item in journal if item.get("order_id") == submitted[0]["order"]["order_id"])
        self.assertEqual(journal_item["side"], "BUY")
        self.assertEqual(journal_item["playbook"], submitted[0]["request"]["playbook"])
        self.assertEqual(journal_item["regime"], submitted[0]["request"]["regime"])
        self.assertEqual(journal_item["trade_date"], trade_date)

    def test_execution_intent_dispatch_apply_pushes_trade_notification(self) -> None:
        trade_date, finalize_payload = self._prepare_finalized_discussion()
        first_intent = finalize_payload["execution_intents"]["intents"][0]
        captured: list[dict] = []

        def fake_dispatch(_self, title: str, content: str, level: str = "info", force: bool = True) -> bool:
            captured.append(
                {
                    "title": title,
                    "content": content,
                    "level": level,
                    "force": force,
                }
            )
            return True

        with patch("ashare_system.notify.dispatcher.MessageDispatcher.dispatch_trade", new=fake_dispatch):
            response = self.client.post(
                "/system/discussions/execution-intents/dispatch",
                json={
                    "trade_date": trade_date,
                    "account_id": "sim-001",
                    "intent_ids": [first_intent["intent_id"]],
                    "apply": True,
                },
            )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["submitted_count"], 1)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["title"], "自动下单")
        self.assertTrue(captured[0]["force"])
        self.assertIn(first_intent["symbol"], captured[0]["content"])
        self.assertIn(first_intent["name"], captured[0]["content"])

    def test_execution_intent_dispatch_apply_enqueues_gateway_intents_on_windows_plane(self) -> None:
        os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
        self._reload_app()

        trade_date, finalize_payload = self._prepare_finalized_discussion()
        first_intent = finalize_payload["execution_intents"]["intents"][0]
        captured: list[dict] = []

        def fake_dispatch(_self, title: str, content: str, level: str = "info", force: bool = True) -> bool:
            captured.append(
                {
                    "title": title,
                    "content": content,
                    "level": level,
                    "force": force,
                }
            )
            return True

        with patch("ashare_system.notify.dispatcher.MessageDispatcher.dispatch_trade", new=fake_dispatch):
            response = self.client.post(
                "/system/discussions/execution-intents/dispatch",
                json={
                    "trade_date": trade_date,
                    "account_id": "sim-001",
                    "intent_ids": [first_intent["intent_id"]],
                    "apply": True,
                },
            )

        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "queued_for_gateway")
        self.assertEqual(payload["execution_plane"], "windows_gateway")
        self.assertEqual(payload["gateway_pull_path"], "/system/execution/gateway/intents/pending")
        self.assertEqual(payload["submitted_count"], 0)
        self.assertEqual(payload["queued_count"], 1)

        queued = [item for item in payload["receipts"] if item["status"] == "queued_for_gateway"]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["reason"], "forward_to_windows_execution_gateway")
        self.assertEqual(queued[0]["execution_plane"], "windows_gateway")
        self.assertEqual(queued[0]["gateway_pull_path"], "/system/execution/gateway/intents/pending")
        self.assertEqual(queued[0]["gateway_intent"]["intent_id"], first_intent["intent_id"])
        self.assertEqual(queued[0]["gateway_intent"]["status"], "approved")
        self.assertEqual(queued[0]["gateway_intent"]["execution_plane"], "windows_gateway")
        self.assertFalse(any(item["title"] == "自动下单" for item in captured))

        meeting_state = get_meeting_state_store()
        pending = meeting_state.get("pending_execution_intents", [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["intent_id"], first_intent["intent_id"])
        self.assertEqual(pending[0]["status"], "approved")
        self.assertEqual(pending[0]["execution_plane"], "windows_gateway")
        self.assertEqual(pending[0]["approval_source"], "discussion_execution_dispatch")

        orders = self.client.get("/execution/orders/sim-001").json()
        self.assertEqual(orders, [])

    def test_execution_dispatch_latest_endpoint_returns_persisted_receipt(self) -> None:
        trade_date, finalize_payload = self._prepare_finalized_discussion()
        first_intent = finalize_payload["execution_intents"]["intents"][0]

        dispatch = self.client.post(
            "/system/discussions/execution-intents/dispatch",
            json={
                "trade_date": trade_date,
                "account_id": "sim-001",
                "intent_ids": [first_intent["intent_id"]],
                "apply": False,
            },
        )
        dispatch_payload = dispatch.json()
        self.assertEqual(dispatch.status_code, 200)
        self.assertTrue(dispatch_payload["ok"])

        latest = self.client.get("/system/discussions/execution-dispatch/latest")
        latest_payload = latest.json()
        self.assertEqual(latest.status_code, 200)
        self.assertTrue(latest_payload["ok"])
        self.assertEqual(latest_payload["trade_date"], trade_date)
        self.assertEqual(latest_payload["receipts"][0]["intent_id"], first_intent["intent_id"])

        by_date = self.client.get(
            "/system/discussions/execution-dispatch/latest",
            params={"trade_date": trade_date},
        )
        by_date_payload = by_date.json()
        self.assertEqual(by_date.status_code, 200)
        self.assertTrue(by_date_payload["ok"])
        self.assertEqual(by_date_payload["trade_date"], trade_date)
        self.assertEqual(by_date_payload["status"], "preview")

    def test_execution_dispatch_latest_endpoint_includes_control_plane_gateway_summary(self) -> None:
        os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
        self._reload_app()

        trade_date, finalize_payload = self._prepare_finalized_discussion()
        first_intent = finalize_payload["execution_intents"]["intents"][0]
        dispatch = self.client.post(
            "/system/discussions/execution-intents/dispatch",
            json={
                "trade_date": trade_date,
                "account_id": "sim-001",
                "intent_ids": [first_intent["intent_id"]],
                "apply": True,
            },
        ).json()
        self.assertTrue(dispatch["ok"])
        self.assertEqual(dispatch["status"], "queued_for_gateway")

        claim = self.client.post(
            "/system/execution/gateway/intents/claim",
            json={
                "intent_id": first_intent["intent_id"],
                "gateway_source_id": "windows-vm-a",
                "deployment_role": "primary_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                "claimed_at": "2026-04-08T09:31:05+08:00",
            },
        ).json()
        self.assertTrue(claim["ok"])

        receipt = self.client.post(
            "/system/execution/gateway/receipts",
            json={
                "receipt_id": "receipt-dispatch-latest-1",
                "intent_id": first_intent["intent_id"],
                "intent_version": "v1",
                "gateway_source_id": "windows-vm-a",
                "deployment_role": "primary_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                "reported_at": "2026-04-08T09:31:08+08:00",
                "submitted_at": "2026-04-08T09:31:07+08:00",
                "status": "submitted",
                "broker_order_id": "qmt-dispatch-1",
                "order": {
                    "symbol": first_intent["symbol"],
                    "side": first_intent["request"]["side"],
                    "price": first_intent["request"]["price"],
                    "quantity": first_intent["request"]["quantity"],
                },
                "fills": [],
                "latency_ms": 120.0,
                "raw_payload": {"upstream": "xtquant"},
                "summary_lines": ["讨论执行意图已由 Windows Gateway 提交。"],
            },
        ).json()
        self.assertTrue(receipt["ok"])

        latest_payload = self.client.get("/system/discussions/execution-dispatch/latest").json()
        self.assertTrue(latest_payload["ok"])
        self.assertEqual(latest_payload["pending_intent_count"], 1)
        self.assertEqual(latest_payload["queued_for_gateway_count"], 0)
        self.assertEqual(latest_payload["discussion_dispatch_queued_for_gateway_count"], 1)
        self.assertEqual(latest_payload["tail_market_queued_for_gateway_count"], 0)
        self.assertEqual(latest_payload["latest_gateway_source"], "windows-vm-a")
        self.assertEqual(latest_payload["latest_receipt_summary"]["receipt_id"], "receipt-dispatch-latest-1")
        self.assertEqual(latest_payload["latest_receipt_summary"]["status"], "submitted")
        self.assertEqual(
            latest_payload["control_plane_gateway_summary"]["latest_receipt_summary"]["gateway_source_id"],
            "windows-vm-a",
        )

    def test_gateway_pending_intents_endpoint_returns_approved_items(self) -> None:
        seeded = self._seed_pending_gateway_intent()

        response = self.client.get(
            "/system/execution/gateway/intents/pending",
            params={"gateway_source_id": "windows-vm-a", "deployment_role": "primary_gateway"},
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["available"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["intent_id"], seeded["intent_id"])
        self.assertEqual(payload["items"][0]["status"], "approved")
        self.assertEqual(payload["items"][0]["execution_plane"], "windows_gateway")

    def test_gateway_claim_endpoint_marks_intent_claimed_once(self) -> None:
        seeded = self._seed_pending_gateway_intent()

        response = self.client.post(
            "/system/execution/gateway/intents/claim",
            json={
                "intent_id": seeded["intent_id"],
                "gateway_source_id": "windows-vm-a",
                "deployment_role": "primary_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                "claimed_at": "2026-04-08T09:31:05+08:00",
            },
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["claim_status"], "claimed")
        self.assertEqual(payload["intent"]["status"], "claimed")
        self.assertEqual(payload["intent"]["claim"]["gateway_source_id"], "windows-vm-a")

        meeting_state = get_meeting_state_store()
        pending = meeting_state.get("pending_execution_intents", [])
        self.assertEqual(pending[0]["status"], "claimed")

    def test_gateway_receipt_endpoint_persists_submitted_receipt(self) -> None:
        seeded = self._seed_pending_gateway_intent(status="claimed")
        meeting_state = get_meeting_state_store()
        pending = meeting_state.get("pending_execution_intents", [])
        pending[0]["claim"] = {
            "gateway_source_id": "windows-vm-a",
            "deployment_role": "primary_gateway",
            "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
            "claimed_at": "2026-04-08T09:31:05+08:00",
        }
        meeting_state.set("pending_execution_intents", pending)

        response = self.client.post(
            "/system/execution/gateway/receipts",
            json={
                "receipt_id": "receipt-20260408-0001",
                "intent_id": seeded["intent_id"],
                "intent_version": "v1",
                "gateway_source_id": "windows-vm-a",
                "deployment_role": "primary_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                "reported_at": "2026-04-08T09:31:08+08:00",
                "submitted_at": "2026-04-08T09:31:07+08:00",
                "status": "submitted",
                "broker_order_id": "qmt-123456",
                "order": {
                    "symbol": "600519.SH",
                    "side": "BUY",
                    "price": 1688.0,
                    "quantity": 100,
                },
                "fills": [],
                "latency_ms": 182.0,
                "raw_payload": {"upstream": "xtquant"},
                "summary_lines": ["订单已由 Windows Gateway 提交到 QMT。"],
            },
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["stored"])
        self.assertEqual(payload["latest_receipt"]["receipt_id"], "receipt-20260408-0001")

        latest = self.client.get("/system/execution/gateway/receipts/latest").json()
        self.assertTrue(latest["available"])
        self.assertEqual(latest["receipt"]["intent_id"], seeded["intent_id"])

        detail = self.client.get(f"/system/execution/gateway/intents/{seeded['intent_id']}").json()
        self.assertTrue(detail["available"])
        self.assertEqual(detail["intent"]["status"], "submitted")

    def test_pending_order_inspection_endpoint_reports_submitted_orders(self) -> None:
        trade_date, finalize_payload = self._prepare_finalized_discussion()
        first_intent = finalize_payload["execution_intents"]["intents"][0]

        dispatch = self.client.post(
            "/system/discussions/execution-intents/dispatch",
            json={
                "trade_date": trade_date,
                "account_id": "sim-001",
                "intent_ids": [first_intent["intent_id"]],
                "apply": True,
            },
        )
        dispatch_payload = dispatch.json()
        self.assertEqual(dispatch.status_code, 200)
        self.assertTrue(dispatch_payload["ok"])
        submitted = [item for item in dispatch_payload["receipts"] if item["status"] == "submitted"]
        self.assertGreaterEqual(len(submitted), 1)

        inspection = self.client.get(
            "/system/discussions/execution-orders/inspection",
            params={"account_id": "sim-001"},
        )
        inspection_payload = inspection.json()
        self.assertEqual(inspection.status_code, 200)
        self.assertTrue(inspection_payload["ok"])
        self.assertIn(inspection_payload["status"], {"pending", "warning"})
        self.assertGreaterEqual(inspection_payload["pending_count"], 1)
        self.assertIn("summary_lines", inspection_payload)
        self.assertTrue(any(item["order_id"] == submitted[0]["order"]["order_id"] for item in inspection_payload["items"]))

    def test_readiness_endpoint_includes_recovery_and_pending_order_checks(self) -> None:
        response = self.client.get("/system/readiness", params={"account_id": "sim-001"})
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertIn(payload["status"], {"ready", "degraded"})
        self.assertEqual(payload["account_id"], "sim-001")
        self.assertIn("checks", payload)
        check_names = {item["name"] for item in payload["checks"]}
        self.assertIn("account_access", check_names)
        self.assertIn("account_identity", check_names)
        self.assertIn("startup_recovery", check_names)
        self.assertIn("pending_order_inspection", check_names)
        self.assertIn("startup_recovery", payload)
        self.assertIn("pending_order_inspection", payload)
        self.assertIn("account_state", payload)

    def test_account_state_endpoint_returns_verified_metrics(self) -> None:
        response = self.client.get("/system/account-state", params={"account_id": "sim-001"})
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["account_id"], "sim-001")
        self.assertTrue(payload["verified"])
        self.assertIn("metrics", payload)
        self.assertIn("daily_pnl", payload["metrics"])

    def test_account_state_treats_reverse_repo_as_cash_equivalent(self) -> None:
        adapter = get_execution_adapter()
        adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=1_000_000, cash=200_000)
        adapter.positions["sim-001"] = [
            PositionSnapshot(
                account_id="sim-001",
                symbol="204001.SH",
                quantity=8000,
                available=8000,
                cost_price=100.0,
                last_price=100.0,
            )
        ]

        response = self.client.get("/system/account-state", params={"account_id": "sim-001"})
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["metrics"]["invested_value"], 0.0)
        self.assertEqual(payload["metrics"]["reverse_repo_value"], 800000.0)
        self.assertEqual(payload["metrics"]["current_total_ratio"], 0.0)
        self.assertEqual(payload["metrics"]["reverse_repo_ratio"], 0.8)
        self.assertTrue(any("不占用股票测试仓位" in line for line in payload["summary_lines"]))

    def test_test_trading_budget_keeps_reverse_repo_reserved_amount_outside_stock_budget(self) -> None:
        budget = build_test_trading_budget(
            equity_value=0.0,
            reverse_repo_value=80000.0,
            minimum_total_invested_amount=100000.0,
            reverse_repo_reserved_amount=70000.0,
        )
        self.assertEqual(budget.stock_test_budget_amount, 30000.0)
        self.assertEqual(budget.stock_test_budget_remaining, 30000.0)
        self.assertEqual(budget.reverse_repo_gap_value, 0.0)

    def test_startup_recovery_run_endpoint_persists_latest_payload(self) -> None:
        response = self.client.post("/system/startup-recovery/run", params={"account_id": "sim-001"})
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["account_id"], "sim-001")
        self.assertEqual(payload["status"], "ok")

        latest = self.client.get("/system/startup-recovery/latest")
        latest_payload = latest.json()
        self.assertEqual(latest.status_code, 200)
        self.assertTrue(latest_payload["ok"])
        self.assertEqual(latest_payload["status"], "ok")
        self.assertEqual(latest_payload["account_id"], "sim-001")

    def test_execution_reconciliation_endpoint_persists_trade_and_updates_readiness(self) -> None:
        trade_date, finalize_payload = self._prepare_finalized_discussion()
        first_intent = finalize_payload["execution_intents"]["intents"][0]
        dispatch = self.client.post(
            "/system/discussions/execution-intents/dispatch",
            json={
                "trade_date": trade_date,
                "account_id": "sim-001",
                "intent_ids": [first_intent["intent_id"]],
                "apply": True,
            },
        )
        dispatch_payload = dispatch.json()
        self.assertEqual(dispatch.status_code, 200)
        self.assertTrue(dispatch_payload["ok"])
        submitted = [item for item in dispatch_payload["receipts"] if item["status"] == "submitted"]
        self.assertGreaterEqual(len(submitted), 1)

        adapter = get_execution_adapter()
        adapter.trades["sim-001"] = []
        adapter.trades["sim-001"].append(
            TradeSnapshot(
                trade_id="trade-sys-1",
                order_id=submitted[0]["order"]["order_id"],
                account_id="sim-001",
                symbol=submitted[0]["symbol"],
                side="BUY",
                quantity=submitted[0]["request"]["quantity"],
                price=submitted[0]["request"]["price"],
            )
        )

        reconcile = self.client.post("/system/execution-reconciliation/run", params={"account_id": "sim-001"})
        reconcile_payload = reconcile.json()
        self.assertEqual(reconcile.status_code, 200)
        self.assertTrue(reconcile_payload["ok"])
        self.assertEqual(reconcile_payload["matched_order_count"], 1)
        self.assertEqual(reconcile_payload["filled_order_count"], 1)
        self.assertEqual(reconcile_payload["trade_count"], 1)
        self.assertIn("attribution", reconcile_payload)
        self.assertEqual(reconcile_payload["attribution"]["update_count"], 1)
        self.assertEqual(reconcile_payload["attribution"]["items"][0]["source"], "execution_reconciliation")
        self.assertEqual(reconcile_payload["attribution"]["items"][0]["side"], "BUY")
        self.assertEqual(reconcile_payload["attribution"]["items"][0]["filled_quantity"], submitted[0]["request"]["quantity"])

        latest = self.client.get("/system/execution-reconciliation/latest")
        latest_payload = latest.json()
        self.assertEqual(latest.status_code, 200)
        self.assertTrue(latest_payload["ok"])
        self.assertEqual(latest_payload["trade_count"], 1)
        self.assertIn("attribution", latest_payload)
        self.assertEqual(latest_payload["attribution"]["update_count"], 1)

        attribution = self.client.get(
            "/system/learning/attribution",
            params={"trade_date": trade_date, "score_date": latest_payload["reconciled_at"][:10]},
        ).json()
        self.assertTrue(attribution["available"])
        self.assertGreaterEqual(attribution["trade_count"], 1)
        first_attr = next(item for item in attribution["items"] if item["symbol"] == submitted[0]["symbol"])
        self.assertEqual(first_attr["source"], "execution_reconciliation")
        self.assertEqual(first_attr["order_id"], submitted[0]["order"]["order_id"])
        self.assertEqual(first_attr["exit_reason"], "open_position")

        readiness = self.client.get("/system/readiness", params={"account_id": "sim-001"}).json()
        check_names = {item["name"] for item in readiness["checks"]}
        self.assertIn("execution_reconciliation", check_names)
        self.assertIn("execution_reconciliation", readiness)
        precheck = self.client.get("/system/discussions/execution-precheck", params={"trade_date": trade_date}).json()
        self.assertIn("account_state", precheck)
        self.assertIn("daily_pnl", precheck["items"][0])

    def test_manual_sell_order_reconciliation_backfills_exit_reason_and_playbook(self) -> None:
        response = self.client.post(
            "/execution/orders",
            json={
                "account_id": "sim-001",
                "symbol": "600519.SH",
                "side": "SELL",
                "quantity": 100,
                "price": 1590.0,
                "request_id": "req-manual-sell-1",
                "trade_date": "2026-04-06",
                "playbook": "leader_chase",
                "regime": "trend",
                "exit_reason": "sector_retreat",
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        order_id = payload["order_id"]

        adapter = get_execution_adapter()
        adapter.trades["sim-001"] = [
            TradeSnapshot(
                trade_id="trade-sell-1",
                order_id=order_id,
                account_id="sim-001",
                symbol="600519.SH",
                side="SELL",
                quantity=100,
                price=1590.0,
            )
        ]

        reconcile = self.client.post("/system/execution-reconciliation/run", params={"account_id": "sim-001"})
        reconcile_payload = reconcile.json()
        self.assertEqual(reconcile.status_code, 200)
        self.assertTrue(reconcile_payload["ok"])
        self.assertIn("attribution", reconcile_payload)
        self.assertEqual(reconcile_payload["attribution"]["update_count"], 1)
        attr_item = reconcile_payload["attribution"]["items"][0]
        self.assertEqual(attr_item["source"], "execution_reconciliation")
        self.assertEqual(attr_item["side"], "SELL")
        self.assertEqual(attr_item["exit_reason"], "sector_retreat")
        self.assertEqual(attr_item["playbook"], "leader_chase")
        self.assertEqual(attr_item["regime"], "trend")

        attribution = self.client.get(
            "/system/learning/attribution",
            params={"trade_date": "2026-04-06", "score_date": reconcile_payload["reconciled_at"][:10]},
        ).json()
        self.assertTrue(attribution["available"])
        sell_item = next(item for item in attribution["items"] if item["order_id"] == order_id)
        self.assertEqual(sell_item["exit_reason"], "sector_retreat")
        self.assertEqual(sell_item["playbook"], "leader_chase")
        self.assertEqual(sell_item["regime"], "trend")

    def test_execution_orders_endpoint_blocks_direct_submit_on_windows_gateway_plane(self) -> None:
        os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
        self._reload_app()

        response = self.client.post(
            "/execution/orders",
            json={
                "account_id": "sim-001",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "price": 1688.0,
                "request_id": "req-windows-gateway-block",
                "trade_date": "2026-04-08",
                "playbook": "leader_chase",
                "regime": "trend",
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 409)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["reason"], "execution_plane_windows_gateway_requires_gateway_dispatch")
        self.assertEqual(payload["execution_plane"], "windows_gateway")
        self.assertEqual(payload["dispatch_path"], "/system/discussions/execution-intents/dispatch")

        orders = self.client.get("/execution/orders/sim-001").json()
        self.assertEqual(orders, [])
        meeting_state = get_meeting_state_store()
        self.assertEqual(meeting_state.get("pending_execution_intents", []), [])

    def test_tail_market_auto_sell_reconciliation_backfills_exit_reason_and_playbook(self) -> None:
        settings = get_settings()
        runtime_context = {
            "available": True,
            "resource": "runtime_context",
            "trade_date": "2026-04-06",
            "generated_at": "2026-04-06T14:45:00",
            "market_profile": {
                "sentiment_phase": "主升",
                "regime": "trend",
                "allowed_playbooks": ["leader_chase"],
            },
            "sector_profiles": [
                {
                    "sector_name": "白酒",
                    "life_cycle": "retreat",
                    "strength_score": 0.8,
                }
            ],
            "playbook_contexts": [
                {
                    "playbook": "leader_chase",
                    "symbol": "600519.SH",
                    "sector": "白酒",
                    "entry_window": "09:30-10:00",
                    "confidence": 0.91,
                    "rank_in_sector": 1,
                    "leader_score": 0.94,
                    "style_tag": "leader",
                    "exit_params": {
                        "atr_pct": 0.015,
                        "open_failure_minutes": 5,
                        "max_hold_minutes": 240,
                        "time_stop": "14:50",
                    },
                }
            ],
        }
        DataArchiveStore(settings.storage_root).persist_runtime_context("2026-04-06", runtime_context)
        runtime_state = get_runtime_state_store()
        runtime_state.set("latest_runtime_context", runtime_context)

        adapter = get_execution_adapter()
        adapter.positions["sim-001"] = [
            PositionSnapshot(
                account_id="sim-001",
                symbol="600519.SH",
                quantity=100,
                available=100,
                cost_price=1588.0,
                last_price=1592.5,
            )
        ]
        meeting_state = get_meeting_state_store()
        meeting_state.set(
            "execution_order_journal",
            [
                {
                    "trade_date": "2026-04-06",
                    "account_id": "sim-001",
                    "order_id": "order-buy-1",
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "decision_id": "case-600519",
                    "submitted_at": "2026-04-06T09:35:00",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "request": {
                        "side": "BUY",
                        "playbook": "leader_chase",
                        "regime": "trend",
                    },
                    "latest_status": "FILLED",
                }
            ],
        )

        payload = run_tail_market_scan(
            settings=settings,
            market=get_market_adapter(),
            execution_adapter=adapter,
            meeting_state_store=meeting_state,
            runtime_state_store=runtime_state,
            now_factory=lambda: datetime(2026, 4, 6, 14, 50, 0),
        )
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["submitted_count"], 1)
        submitted = next(item for item in payload["items"] if item["status"] == "submitted")
        self.assertEqual(submitted["exit_reason"], "sector_retreat")
        order_id = submitted["order_id"]

        adapter.trades["sim-001"] = [
            TradeSnapshot(
                trade_id="trade-auto-sell-1",
                order_id=order_id,
                account_id="sim-001",
                symbol="600519.SH",
                side="SELL",
                quantity=100,
                price=9.99,
            )
        ]

        reconcile = self.client.post("/system/execution-reconciliation/run", params={"account_id": "sim-001"})
        reconcile_payload = reconcile.json()
        self.assertEqual(reconcile.status_code, 200)
        self.assertTrue(reconcile_payload["ok"])
        self.assertIn("attribution", reconcile_payload)
        self.assertEqual(reconcile_payload["attribution"]["update_count"], 1)
        attr_item = reconcile_payload["attribution"]["items"][0]
        self.assertEqual(attr_item["source"], "execution_reconciliation")
        self.assertEqual(attr_item["side"], "SELL")
        self.assertEqual(attr_item["exit_reason"], "sector_retreat")
        self.assertEqual(attr_item["playbook"], "leader_chase")
        self.assertEqual(attr_item["regime"], "trend")
        self.assertEqual(attr_item["exit_context_snapshot"]["sector_retreat"], True)
        self.assertIn("sector_retreat", attr_item["review_tags"])

        attribution = self.client.get(
            "/system/learning/attribution",
            params={"trade_date": "2026-04-06", "score_date": reconcile_payload["reconciled_at"][:10]},
        ).json()
        self.assertTrue(attribution["available"])
        sell_item = next(item for item in attribution["items"] if item["order_id"] == order_id)
        self.assertEqual(sell_item["exit_reason"], "sector_retreat")
        self.assertEqual(sell_item["playbook"], "leader_chase")
        self.assertEqual(sell_item["regime"], "trend")
        self.assertEqual(sell_item["exit_context_snapshot"]["sector_retreat"], True)
        self.assertIn("exit_tag_counts", attribution["review_summary"])

        filtered_attr = self.client.get(
            "/system/learning/attribution",
            params={
                "trade_date": "2026-04-06",
                "score_date": reconcile_payload["reconciled_at"][:10],
                "review_tag": "sector_retreat",
                "exit_context_key": "sector_retreat",
                "exit_context_value": "true",
            },
        ).json()
        self.assertTrue(filtered_attr["available"])
        self.assertEqual(filtered_attr["trade_count"], 1)
        self.assertEqual(filtered_attr["filters"]["review_tag"], "sector_retreat")
        self.assertEqual(filtered_attr["filters"]["exit_context_key"], "sector_retreat")

        filtered_review = self.client.get(
            "/system/learning/trade-review",
            params={
                "trade_date": "2026-04-06",
                "score_date": reconcile_payload["reconciled_at"][:10],
                "review_tag": "sector_retreat",
                "exit_context_key": "sector_retreat",
                "exit_context_value": "true",
            },
        ).json()
        self.assertTrue(filtered_review["available"])
        self.assertEqual(filtered_review["filters"]["review_tag"], "sector_retreat")
        self.assertGreaterEqual(len(filtered_review["parameter_hints"]), 1)
        param_keys = {item["param_key"] for item in filtered_review["parameter_hints"]}
        self.assertIn("sector_exposure_limit", param_keys)

        preview_proposals = self.client.post(
            "/system/learning/parameter-hints/proposals",
            json={
                "trade_date": "2026-04-06",
                "score_date": reconcile_payload["reconciled_at"][:10],
                "review_tag": "sector_retreat",
                "exit_context_key": "sector_retreat",
                "exit_context_value": "true",
                "apply": False,
            },
        ).json()
        self.assertTrue(preview_proposals["ok"])
        self.assertFalse(preview_proposals["applied"])
        self.assertGreaterEqual(preview_proposals["matched_count"], 1)
        preview_keys = {item["param_key"] for item in preview_proposals["items"]}
        self.assertIn("sector_exposure_limit", preview_keys)
        self.assertIn("approval_baseline", preview_proposals)
        self.assertGreaterEqual(preview_proposals["approval_baseline"]["manual_review_count"], 1)
        sector_exposure_preview = next(
            item for item in preview_proposals["items"] if item["param_key"] == "sector_exposure_limit"
        )
        self.assertFalse(sector_exposure_preview["approval_policy"]["auto_approvable"])
        self.assertEqual(sector_exposure_preview["approval_policy"]["required_confirmation"], "manual_review")
        self.assertEqual(
            sector_exposure_preview["rollback_baseline"]["restore_value"],
            sector_exposure_preview["current_value"],
        )

        applied_proposals = self.client.post(
            "/system/learning/parameter-hints/proposals",
            json={
                "trade_date": "2026-04-06",
                "score_date": reconcile_payload["reconciled_at"][:10],
                "review_tag": "sector_retreat",
                "exit_context_key": "sector_retreat",
                "exit_context_value": "true",
                "apply": True,
                "effective_period": "until_revoked",
                "status": "approved",
            },
        ).json()
        self.assertTrue(applied_proposals["ok"])
        self.assertFalse(applied_proposals["applied"])
        self.assertGreaterEqual(len(applied_proposals["proposal_events"]), 1)
        applied_keys = {item["param_key"] for item in applied_proposals["items"]}
        self.assertIn("sector_exposure_limit", applied_keys)
        self.assertGreaterEqual(applied_proposals["approval_baseline"]["manual_review_count"], 1)
        self.assertEqual(applied_proposals["execution_summary"]["effective_event_count"], 0)
        self.assertGreaterEqual(applied_proposals["execution_summary"]["pending_review_event_count"], 1)
        proposal_statuses = {item["status"] for item in applied_proposals["proposal_events"]}
        self.assertEqual(proposal_statuses, {"evaluating"})

        params = {item["param_key"]: item for item in self.client.get("/system/params").json()["items"]}
        self.assertEqual(params["sector_exposure_limit"]["current_value"], 0.4)

    def test_tail_market_endpoint_queues_gateway_intent_on_windows_gateway_plane(self) -> None:
        os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
        self._reload_app()
        settings = get_settings()
        runtime_context = {
            "available": True,
            "resource": "runtime_context",
            "trade_date": "2026-04-06",
            "generated_at": "2026-04-06T14:45:00",
            "market_profile": {
                "sentiment_phase": "主升",
                "regime": "trend",
                "allowed_playbooks": ["leader_chase"],
            },
            "sector_profiles": [
                {
                    "sector_name": "白酒",
                    "life_cycle": "retreat",
                    "strength_score": 0.8,
                }
            ],
            "playbook_contexts": [
                {
                    "playbook": "leader_chase",
                    "symbol": "600519.SH",
                    "sector": "白酒",
                    "entry_window": "09:30-10:00",
                    "confidence": 0.91,
                    "rank_in_sector": 1,
                    "leader_score": 0.94,
                    "style_tag": "leader",
                    "exit_params": {
                        "atr_pct": 0.015,
                        "open_failure_minutes": 5,
                        "max_hold_minutes": 240,
                        "time_stop": "14:50",
                    },
                }
            ],
        }
        DataArchiveStore(settings.storage_root).persist_runtime_context("2026-04-06", runtime_context)
        get_runtime_state_store().set("latest_runtime_context", runtime_context)

        adapter = get_execution_adapter()
        adapter.positions["sim-001"] = [
            PositionSnapshot(
                account_id="sim-001",
                symbol="600519.SH",
                quantity=100,
                available=100,
                cost_price=1588.0,
                last_price=1592.5,
            )
        ]
        meeting_state = get_meeting_state_store()
        meeting_state.set(
            "execution_order_journal",
            [
                {
                    "trade_date": "2026-04-06",
                    "account_id": "sim-001",
                    "order_id": "order-buy-1",
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "decision_id": "case-600519",
                    "submitted_at": "2026-04-06T09:35:00",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "request": {
                        "side": "BUY",
                        "playbook": "leader_chase",
                        "regime": "trend",
                    },
                    "latest_status": "FILLED",
                }
            ],
        )

        response = self.client.post("/system/tail-market/run", params={"account_id": "sim-001"})
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "queued_for_gateway")
        self.assertEqual(payload["execution_plane"], "windows_gateway")
        self.assertEqual(payload["submitted_count"], 0)
        self.assertEqual(payload["queued_count"], 1)
        self.assertEqual(payload["preview_count"], 0)
        self.assertEqual(payload["gateway_pull_path"], "/system/execution/gateway/intents/pending")

        queued = next(item for item in payload["items"] if item["status"] == "queued_for_gateway")
        self.assertEqual(queued["gateway_pull_path"], "/system/execution/gateway/intents/pending")
        self.assertEqual(queued["gateway_intent"]["approval_source"], "tail_market_scan")
        self.assertEqual(queued["gateway_intent"]["status"], "approved")
        self.assertEqual(queued["gateway_intent"]["execution_plane"], "windows_gateway")
        self.assertEqual(queued["gateway_intent"]["request"]["side"], "SELL")
        self.assertEqual(queued["gateway_intent"]["discussion_context"]["trigger_source"], "tail_market_scan")

        pending = get_meeting_state_store().get("pending_execution_intents", [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["intent_id"], queued["gateway_intent"]["intent_id"])
        self.assertEqual(pending[0]["approval_source"], "tail_market_scan")

        orders = self.client.get("/execution/orders/sim-001").json()
        self.assertEqual(orders, [])

    def test_tail_market_latest_endpoint_includes_control_plane_gateway_summary(self) -> None:
        meeting_state = get_meeting_state_store()
        self._store_gateway_intent(intent_id="intent-tail-latest-1", side="SELL")
        meeting_state.set(
            "latest_execution_dispatch",
            {
                "trade_date": "2026-04-08",
                "status": "queued_for_gateway",
                "queued_count": 2,
                "summary_lines": ["讨论执行已入队。"],
                "receipts": [
                    {
                        "status": "queued_for_gateway",
                        "intent_id": "intent-dispatch-latest-1",
                        "gateway_intent": {
                            "intent_id": "intent-dispatch-latest-1",
                            "approval_source": "discussion_execution_dispatch",
                        },
                    }
                ],
            },
        )
        meeting_state.set(
            "latest_execution_gateway_receipt",
            {
                "receipt_id": "receipt-tail-latest-1",
                "intent_id": "intent-prev-1",
                "gateway_source_id": "windows-vm-b",
                "deployment_role": "backup_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway_backup -> qmt_vm",
                "reported_at": "2026-04-08T14:49:58+08:00",
                "submitted_at": "2026-04-08T14:49:55+08:00",
                "status": "submitted",
                "broker_order_id": "qmt-tail-1",
                "order": {
                    "symbol": "600519.SH",
                    "side": "SELL",
                    "price": 1590.0,
                    "quantity": 100,
                },
                "fills": [],
                "summary_lines": ["上一笔 Gateway receipt 已写入。"],
            },
        )
        meeting_state.set(
            "latest_tail_market_scan",
            {
                "status": "queued_for_gateway",
                "trade_date": "2026-04-08",
                "account_id": "sim-001",
                "execution_plane": "windows_gateway",
                "submitted_count": 0,
                "queued_count": 1,
                "preview_count": 0,
                "summary_lines": ["尾盘扫描已入队。"],
                "items": [
                    {
                        "status": "queued_for_gateway",
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "exit_reason": "sector_retreat",
                        "review_tags": ["leader_style"],
                        "gateway_intent": {
                            "intent_id": "intent-tail-latest-1",
                            "approval_source": "tail_market_scan",
                        },
                    }
                ],
            },
        )

        latest_payload = self.client.get("/system/tail-market/latest").json()
        self.assertTrue(latest_payload["ok"])
        self.assertEqual(latest_payload["pending_intent_count"], 1)
        self.assertEqual(latest_payload["queued_for_gateway_count"], 1)
        self.assertEqual(latest_payload["discussion_dispatch_queued_for_gateway_count"], 2)
        self.assertEqual(latest_payload["tail_market_queued_for_gateway_count"], 1)
        self.assertEqual(latest_payload["latest_gateway_source"], "windows-vm-b")
        self.assertEqual(latest_payload["latest_receipt_summary"]["receipt_id"], "receipt-tail-latest-1")
        self.assertEqual(
            latest_payload["control_plane_gateway_summary"]["tail_market"]["latest_approval_source"],
            "tail_market_scan",
        )

    def test_parameter_hint_proposals_auto_approve_monitor_params(self) -> None:
        settings = get_settings()
        service = TradeAttributionService(settings.storage_root / "learning" / "trade_attribution.json")
        service.record_outcomes(
            trade_date="2026-04-06",
            score_date="2026-04-07",
            items=[
                TradeAttributionRecord(
                    trade_date="2026-04-06",
                    score_date="2026-04-07",
                    symbol="600519.SH",
                    name="贵州茅台",
                    account_id="sim-001",
                    order_id="order-auto-approve-1",
                    side="SELL",
                    source="execution_reconciliation",
                    status="FILLED",
                    playbook="leader_chase",
                    regime="trend",
                    exit_reason="time_stop",
                    next_day_close_pct=-0.012,
                    holding_days=1,
                    exit_context_snapshot={
                        "sector_relative_trend_5m": -0.018,
                        "sector_underperform_bars_5m": 3,
                    },
                    review_tags=["sector_relative_trend_weak", "leader_style"],
                    recorded_at=datetime.now().isoformat(),
                )
            ],
        )

        response = self.client.post(
            "/system/learning/parameter-hints/proposals",
            json={
                "trade_date": "2026-04-06",
                "score_date": "2026-04-07",
                "review_tag": "sector_relative_trend_weak",
                "apply": True,
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["applied"])
        self.assertGreaterEqual(payload["approval_baseline"]["auto_approvable_count"], 1)
        self.assertEqual(payload["execution_summary"]["pending_review_event_count"], 0)
        self.assertGreaterEqual(payload["execution_summary"]["effective_event_count"], 1)
        proposal_statuses = {item["status"] for item in payload["proposal_events"]}
        self.assertTrue(proposal_statuses.issubset({"approved", "effective"}))
        self.assertTrue(all(item["observation_window"]["stage"] == "proposal_observation" for item in payload["proposal_events"]))
        event_ids = [item["event_id"] for item in payload["proposal_events"]]

        params = {item["param_key"]: item for item in self.client.get("/system/params").json()["items"]}
        self.assertLess(params["execution_poll_seconds"]["current_value"], 30)

        effects = self.client.get(
            "/system/learning/parameter-hints/effects",
            params={"trade_date": "2026-04-06", "score_date": "2026-04-07", "event_ids": ",".join(event_ids)},
        ).json()
        self.assertTrue(effects["ok"])
        self.assertEqual(effects["count"], len(event_ids))
        self.assertGreaterEqual(effects["rollback_recommended_count"], 1)
        first_effect = effects["items"][0]
        self.assertTrue(first_effect["effect_tracking"]["available"])
        self.assertTrue(first_effect["effect_tracking"]["rollback_recommended"])
        self.assertEqual(first_effect["filters"]["review_tag"], "sector_relative_trend_weak")
        self.assertIn(first_effect["effect_tracking"]["observation_window"]["status"], {"ready_for_review", "observing"})

        rollback_preview = self.client.post(
            "/system/learning/parameter-hints/rollback-preview",
            json={"trade_date": "2026-04-06", "score_date": "2026-04-07", "event_ids": event_ids},
        ).json()
        self.assertTrue(rollback_preview["ok"])
        self.assertGreaterEqual(rollback_preview["count"], 1)
        self.assertTrue(rollback_preview["items"][0]["rollback_policy"]["auto_approvable"])
        self.assertEqual(
            rollback_preview["items"][0]["restore_value"],
            rollback_preview["items"][0]["effect_tracking"]["rollback_preview"]["restore_value"],
        )

        rollback_apply = self.client.post(
            "/system/learning/parameter-hints/rollback-apply",
            json={"trade_date": "2026-04-06", "score_date": "2026-04-07", "event_ids": event_ids},
        ).json()
        self.assertTrue(rollback_apply["ok"])
        self.assertTrue(rollback_apply["applied"])
        self.assertGreaterEqual(rollback_apply["execution_summary"]["effective_event_count"], 1)
        rollback_event_types = {item["event_type"] for item in rollback_apply["rollback_events"]}
        self.assertEqual(rollback_event_types, {"param_rollback"})
        self.assertTrue(all(item["rollback_of_event_id"] for item in rollback_apply["rollback_events"]))
        self.assertTrue(
            all(item["observation_window"]["stage"] == "rollback_followup" for item in rollback_apply["rollback_events"])
        )

        params_after_rollback = {item["param_key"]: item for item in self.client.get("/system/params").json()["items"]}
        self.assertEqual(params_after_rollback["execution_poll_seconds"]["current_value"], 30)

        effects_after_rollback = self.client.get(
            "/system/learning/parameter-hints/effects",
            params={"trade_date": "2026-04-06", "score_date": "2026-04-07", "event_ids": ",".join(event_ids)},
        ).json()
        self.assertTrue(effects_after_rollback["ok"])
        tracked_effect = effects_after_rollback["items"][0]["effect_tracking"]["post_rollback_tracking"]
        self.assertTrue(tracked_effect["tracked"])
        self.assertIn("rollback_event_id", tracked_effect)
        self.assertEqual(tracked_effect["observation_window"]["stage"], "rollback_followup")
        self.assertEqual(tracked_effect["followup_status"], "continue_observe")
        self.assertEqual(tracked_effect["recommended_action"]["action"], "continue_observe")
        self.assertEqual(
            tracked_effect["operation_targets"][0]["path"],
            "/system/learning/parameter-hints/effects",
        )

        rollback_apply_again = self.client.post(
            "/system/learning/parameter-hints/rollback-apply",
            json={"trade_date": "2026-04-06", "score_date": "2026-04-07", "event_ids": event_ids},
        ).json()
        self.assertTrue(rollback_apply_again["ok"])
        self.assertFalse(rollback_apply_again["applied"])
        self.assertGreaterEqual(rollback_apply_again["execution_summary"]["skipped_count"], 1)
        self.assertIn("active_event_mismatch", rollback_apply_again["items"][0]["skip_reasons"])

    def test_high_risk_rollback_requires_manual_release(self) -> None:
        settings = get_settings()
        service = TradeAttributionService(settings.storage_root / "learning" / "trade_attribution.json")
        service.record_outcomes(
            trade_date="2026-04-06",
            score_date="2026-04-07",
            items=[
                TradeAttributionRecord(
                    trade_date="2026-04-06",
                    score_date="2026-04-07",
                    symbol="600519.SH",
                    name="贵州茅台",
                    account_id="sim-001",
                    order_id="order-high-risk-1",
                    side="SELL",
                    source="execution_reconciliation",
                    status="FILLED",
                    playbook="leader_chase",
                    regime="trend",
                    exit_reason="sector_retreat",
                    next_day_close_pct=-0.031,
                    holding_days=2,
                    exit_context_snapshot={"sector_retreat": True},
                    review_tags=["sector_retreat", "negative_alert"],
                    recorded_at=datetime.now().isoformat(),
                )
            ],
        )

        proposal_payload = self.client.post(
            "/system/learning/parameter-hints/proposals",
            json={
                "trade_date": "2026-04-06",
                "score_date": "2026-04-07",
                "review_tag": "sector_retreat",
                "apply": True,
                "respect_approval_policy": False,
                "effective_period": "until_revoked",
                "status": "approved",
            },
        ).json()
        self.assertTrue(proposal_payload["ok"])
        self.assertTrue(proposal_payload["applied"])
        risk_event = next(
            item for item in proposal_payload["proposal_events"] if item["param_key"] == "sector_exposure_limit"
        )

        rollback_preview = self.client.post(
            "/system/learning/parameter-hints/rollback-preview",
            json={"trade_date": "2026-04-06", "score_date": "2026-04-07", "event_ids": [risk_event["event_id"]]},
        ).json()
        self.assertTrue(rollback_preview["ok"])
        self.assertEqual(rollback_preview["count"], 1)
        self.assertFalse(rollback_preview["items"][0]["rollback_policy"]["auto_approvable"])
        self.assertEqual(rollback_preview["items"][0]["rollback_policy"]["required_confirmation"], "manual_review")

        rollback_apply = self.client.post(
            "/system/learning/parameter-hints/rollback-apply",
            json={
                "trade_date": "2026-04-06",
                "score_date": "2026-04-07",
                "event_ids": [risk_event["event_id"]],
                "observation_window_days": 4,
                "observation_trade_count": 2,
            },
        ).json()
        self.assertTrue(rollback_apply["ok"])
        self.assertFalse(rollback_apply["applied"])
        self.assertEqual(rollback_apply["execution_summary"]["pending_review_event_count"], 1)
        rollback_event = rollback_apply["rollback_events"][0]
        self.assertEqual(rollback_event["status"], "evaluating")
        self.assertTrue(rollback_event["approval_ticket"]["required"])
        self.assertEqual(rollback_event["approval_ticket"]["state"], "pending")
        self.assertEqual(rollback_event["observation_window"]["duration_days"], 4)

        effects_before_release = self.client.get(
            "/system/learning/parameter-hints/effects",
            params={
                "trade_date": "2026-04-06",
                "score_date": "2026-04-07",
                "event_ids": risk_event["event_id"],
            },
        ).json()
        self.assertTrue(effects_before_release["ok"])
        tracked_effect = effects_before_release["items"][0]["effect_tracking"]["post_rollback_tracking"]
        self.assertEqual(tracked_effect["followup_status"], "continue_observe")
        self.assertEqual(tracked_effect["recommended_action"]["action"], "continue_observe")
        self.assertEqual(
            tracked_effect["operation_targets"][0]["path"],
            "/system/learning/parameter-hints/effects",
        )

        approval = self.client.post(
            "/system/learning/parameter-hints/rollback-approval",
            json={
                "event_ids": [rollback_event["event_id"]],
                "action": "release",
                "approver": "human-audit",
                "comment": "高风险 rollback 人工放行",
            },
        ).json()
        self.assertTrue(approval["ok"])
        self.assertEqual(approval["count"], 1)
        approved_item = approval["items"][0]
        self.assertEqual(approved_item["status"], "effective")
        self.assertEqual(approved_item["approval_ticket"]["state"], "released")
        self.assertEqual(approved_item["approval_ticket"]["released_by"], "human-audit")

        params = {item["param_key"]: item for item in self.client.get("/system/params").json()["items"]}
        self.assertEqual(params["sector_exposure_limit"]["current_value"], 0.4)

    def test_learning_review_views_support_symbol_and_reason(self) -> None:
        settings = get_settings()
        service = TradeAttributionService(settings.storage_root / "learning" / "trade_attribution.json")
        service.record_outcomes(
            trade_date="2026-04-06",
            score_date="2026-04-07",
            items=[
                TradeAttributionRecord(
                    trade_date="2026-04-06",
                    score_date="2026-04-07",
                    symbol="600519.SH",
                    name="贵州茅台",
                    account_id="sim-001",
                    order_id="review-view-1",
                    side="SELL",
                    source="execution_reconciliation",
                    status="FILLED",
                    playbook="leader_chase",
                    regime="trend",
                    exit_reason="sector_retreat",
                    next_day_close_pct=-0.018,
                    review_tags=["sector_retreat", "leader_style"],
                    recorded_at=datetime.now().isoformat(),
                ),
                TradeAttributionRecord(
                    trade_date="2026-04-06",
                    score_date="2026-04-07",
                    symbol="300750.SZ",
                    name="宁德时代",
                    account_id="sim-001",
                    order_id="review-view-2",
                    side="SELL",
                    source="execution_reconciliation",
                    status="FILLED",
                    playbook="trend_follow",
                    regime="rotation",
                    exit_reason="time_stop",
                    next_day_close_pct=0.026,
                    review_tags=["intraday_fade"],
                    recorded_at=datetime.now().isoformat(),
                ),
            ],
        )

        attribution = self.client.get(
            "/system/learning/attribution",
            params={"trade_date": "2026-04-06", "score_date": "2026-04-07", "symbol": "600519.SH"},
        ).json()
        self.assertTrue(attribution["available"])
        self.assertEqual(attribution["filters"]["symbol"], "600519.SH")
        self.assertEqual(attribution["trade_count"], 1)
        self.assertEqual(attribution["by_symbol"][0]["key"], "600519.SH")

        trade_review = self.client.get(
            "/system/learning/trade-review",
            params={"trade_date": "2026-04-06", "score_date": "2026-04-07", "reason": "sector_retreat"},
        ).json()
        self.assertTrue(trade_review["available"])
        self.assertEqual(trade_review["filters"]["reason"], "sector_retreat")
        self.assertTrue(any(item["key"] == "sector_retreat" for item in trade_review["by_reason"]))
        self.assertTrue(any(line.startswith("主要复盘原因") for line in trade_review["summary_lines"]))

    def test_parameter_hint_inspection_lists_pending_high_risk_and_window_alerts(self) -> None:
        settings = get_settings()
        service = TradeAttributionService(settings.storage_root / "learning" / "trade_attribution.json")
        service.record_outcomes(
            trade_date="2026-04-06",
            score_date="2026-04-07",
            items=[
                TradeAttributionRecord(
                    trade_date="2026-04-06",
                    score_date="2026-04-07",
                    symbol="600519.SH",
                    name="贵州茅台",
                    account_id="sim-001",
                    order_id="inspect-high-risk-1",
                    side="SELL",
                    source="execution_reconciliation",
                    status="FILLED",
                    playbook="leader_chase",
                    regime="trend",
                    exit_reason="sector_retreat",
                    next_day_close_pct=-0.028,
                    holding_days=2,
                    exit_context_snapshot={"sector_retreat": True},
                    review_tags=["sector_retreat", "negative_alert"],
                    recorded_at=datetime.now().isoformat(),
                )
            ],
        )

        proposal_payload = self.client.post(
            "/system/learning/parameter-hints/proposals",
            json={
                "trade_date": "2026-04-06",
                "score_date": "2026-04-07",
                "review_tag": "sector_retreat",
                "apply": True,
                "respect_approval_policy": False,
                "effective_period": "until_revoked",
                "status": "approved",
            },
        ).json()
        self.assertTrue(proposal_payload["ok"])
        risk_event = next(
            item for item in proposal_payload["proposal_events"] if item["param_key"] == "sector_exposure_limit"
        )

        rollback_apply = self.client.post(
            "/system/learning/parameter-hints/rollback-apply",
            json={
                "trade_date": "2026-04-06",
                "score_date": "2026-04-07",
                "event_ids": [risk_event["event_id"]],
                "observation_window_days": 2,
                "observation_trade_count": 2,
            },
        ).json()
        self.assertTrue(rollback_apply["ok"])
        self.assertEqual(rollback_apply["execution_summary"]["pending_review_event_count"], 1)

        inspection = self.client.get(
            "/system/learning/parameter-hints/inspection",
            params={
                "trade_date": "2026-04-06",
                "score_date": "2026-04-07",
                "statuses": "evaluating,approved,effective",
                "due_within_days": 5,
            },
        ).json()
        self.assertTrue(inspection["ok"])
        self.assertGreaterEqual(inspection["pending_high_risk_rollback_count"], 1)
        pending_item = inspection["pending_high_risk_rollbacks"][0]
        self.assertEqual(pending_item["event_type"], "param_rollback")
        self.assertEqual(pending_item["approval_ticket"]["state"], "pending")
        self.assertEqual(str(pending_item["approval_ticket"]["risk_level"]).lower(), "high")
        self.assertEqual(pending_item["recommended_action"]["action"], "continue_observe")
        self.assertEqual(pending_item["operation_targets"][0]["path"], "/system/learning/parameter-hints/inspection")
        self.assertGreaterEqual(inspection["recommended_action_counts"]["continue_observe"], 1)
        self.assertGreaterEqual(inspection["action_item_count"], 1)
        self.assertIn("high_priority_action_items", inspection)
        self.assertTrue(
            all(item["recommended_action"]["action"] != "continue_observe" for item in inspection["high_priority_action_items"])
        )
        self.assertGreaterEqual(len(inspection["observation_window_alerts"]), 1)
        self.assertIn(inspection["observation_window_alerts"][0]["alert_level"], {"near_due", "overdue"})

    def test_parameter_hint_inspection_run_records_audit(self) -> None:
        settings = get_settings()
        service = TradeAttributionService(settings.storage_root / "learning" / "trade_attribution.json")
        service.record_outcomes(
            trade_date="2026-04-06",
            score_date="2026-04-07",
            items=[
                TradeAttributionRecord(
                    trade_date="2026-04-06",
                    score_date="2026-04-07",
                    symbol="600519.SH",
                    name="贵州茅台",
                    account_id="sim-001",
                    order_id="inspect-run-1",
                    side="SELL",
                    source="execution_reconciliation",
                    status="FILLED",
                    playbook="leader_chase",
                    regime="trend",
                    exit_reason="sector_retreat",
                    next_day_close_pct=-0.031,
                    holding_days=2,
                    exit_context_snapshot={"sector_retreat": True},
                    review_tags=["sector_retreat", "negative_alert"],
                    recorded_at=datetime.now().isoformat(),
                )
            ],
        )

        proposal_payload = self.client.post(
            "/system/learning/parameter-hints/proposals",
            json={
                "trade_date": "2026-04-06",
                "score_date": "2026-04-07",
                "review_tag": "sector_retreat",
                "apply": True,
                "respect_approval_policy": False,
                "effective_period": "until_revoked",
                "status": "approved",
            },
        ).json()
        risk_event = next(
            item for item in proposal_payload["proposal_events"] if item["param_key"] == "sector_exposure_limit"
        )
        self.client.post(
            "/system/learning/parameter-hints/rollback-apply",
            json={
                "trade_date": "2026-04-06",
                "score_date": "2026-04-07",
                "event_ids": [risk_event["event_id"]],
                "observation_window_days": 2,
                "observation_trade_count": 2,
            },
        )

        inspection = self.client.post(
            "/system/learning/parameter-hints/inspection/run",
            json={
                "trade_date": "2026-04-06",
                "score_date": "2026-04-07",
                "statuses": "evaluating,approved,effective",
                "due_within_days": 5,
                "limit": 20,
            },
        ).json()
        self.assertTrue(inspection["ok"])
        self.assertTrue(inspection["executed"])
        self.assertTrue(any("巡检结果已写入审计记录" in line for line in inspection["summary_lines"]))
        self.assertIn("recommended_action_counts", inspection)
        self.assertIn("action_item_count", inspection)
        self.assertIn("high_priority_action_item_count", inspection)

        audits = self.client.get("/system/audits", params={"limit": 20}).json()
        self.assertGreaterEqual(audits["count"], 1)
        audit_item = next(item for item in audits["records"] if item["message"] == "parameter inspection 已执行")
        self.assertEqual(audit_item["category"], "governance")
        self.assertGreaterEqual(audit_item["payload"]["pending_high_risk_rollback_count"], 1)
        self.assertGreaterEqual(audit_item["payload"]["action_item_count"], 1)
        self.assertIn("high_priority_action_item_count", audit_item["payload"])
        self.assertIn("recommended_action_counts", audit_item["payload"])

    def test_parameter_hint_inspection_recommends_rollback_preview_for_negative_effective_event(self) -> None:
        settings = get_settings()
        service = TradeAttributionService(settings.storage_root / "learning" / "trade_attribution.json")
        service.record_outcomes(
            trade_date="2026-04-06",
            score_date="2026-04-07",
            items=[
                TradeAttributionRecord(
                    trade_date="2026-04-06",
                    score_date="2026-04-07",
                    symbol="600519.SH",
                    name="贵州茅台",
                    account_id="sim-001",
                    order_id="inspect-negative-effect-1",
                    side="SELL",
                    source="execution_reconciliation",
                    status="FILLED",
                    playbook="leader_chase",
                    regime="trend",
                    exit_reason="time_stop",
                    next_day_close_pct=-0.012,
                    holding_days=1,
                    exit_context_snapshot={
                        "sector_relative_trend_5m": -0.018,
                        "sector_underperform_bars_5m": 3,
                    },
                    review_tags=["sector_relative_trend_weak", "leader_style"],
                    recorded_at=datetime.now().isoformat(),
                )
            ],
        )

        proposal_payload = self.client.post(
            "/system/learning/parameter-hints/proposals",
            json={
                "trade_date": "2026-04-06",
                "score_date": "2026-04-07",
                "review_tag": "sector_relative_trend_weak",
                "apply": True,
            },
        ).json()
        self.assertTrue(proposal_payload["ok"])

        inspection = self.client.get(
            "/system/learning/parameter-hints/inspection",
            params={
                "trade_date": "2026-04-06",
                "score_date": "2026-04-07",
                "statuses": "effective",
                "due_within_days": 5,
            },
        ).json()
        self.assertTrue(inspection["ok"])
        rollback_preview_item = next(
            item
            for item in inspection["recommended_actions"]
            if item["param_key"] == "execution_poll_seconds"
        )
        self.assertEqual(rollback_preview_item["recommended_action"]["action"], "consider_rollback_preview")
        self.assertEqual(
            rollback_preview_item["operation_targets"][0]["path"],
            "/system/learning/parameter-hints/rollback-preview",
        )
        self.assertGreaterEqual(inspection["recommended_action_counts"]["consider_rollback_preview"], 1)
        high_priority_item = next(
            item
            for item in inspection["high_priority_action_items"]
            if item["param_key"] == "execution_poll_seconds"
        )
        self.assertEqual(high_priority_item["recommended_action"]["action"], "consider_rollback_preview")

    def test_tail_market_endpoints_expose_latest_and_history(self) -> None:
        settings = get_settings()
        runtime_context = {
            "available": True,
            "resource": "runtime_context",
            "trade_date": "2026-04-06",
            "generated_at": "2026-04-06T14:45:00",
            "market_profile": {
                "sentiment_phase": "主升",
                "regime": "trend",
                "allowed_playbooks": ["leader_chase"],
            },
            "sector_profiles": [
                {
                    "sector_name": "白酒",
                    "life_cycle": "retreat",
                    "strength_score": 0.8,
                }
            ],
            "playbook_contexts": [
                {
                    "playbook": "leader_chase",
                    "symbol": "600519.SH",
                    "sector": "白酒",
                    "entry_window": "09:30-10:00",
                    "confidence": 0.91,
                    "rank_in_sector": 1,
                    "leader_score": 0.94,
                    "style_tag": "leader",
                    "exit_params": {
                        "atr_pct": 0.015,
                        "open_failure_minutes": 5,
                        "max_hold_minutes": 240,
                        "time_stop": "14:50",
                    },
                }
            ],
        }
        DataArchiveStore(settings.storage_root).persist_runtime_context("2026-04-06", runtime_context)
        get_runtime_state_store().set("latest_runtime_context", runtime_context)
        adapter = get_execution_adapter()
        adapter.positions["sim-001"] = [
            PositionSnapshot(
                account_id="sim-001",
                symbol="600519.SH",
                quantity=100,
                available=100,
                cost_price=1588.0,
                last_price=1592.5,
            )
        ]
        meeting_state = get_meeting_state_store()
        meeting_state.set(
            "execution_order_journal",
            [
                {
                    "trade_date": "2026-04-06",
                    "account_id": "sim-001",
                    "order_id": "order-buy-1",
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "decision_id": "case-600519",
                    "submitted_at": "2026-04-06T09:35:00",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "request": {
                        "side": "BUY",
                        "playbook": "leader_chase",
                        "regime": "trend",
                    },
                    "latest_status": "FILLED",
                }
            ],
        )

        run_response = self.client.post("/system/tail-market/run", params={"account_id": "sim-001"})
        run_payload = run_response.json()
        self.assertEqual(run_response.status_code, 200)
        self.assertTrue(run_payload["ok"])
        self.assertEqual(run_payload["account_id"], "sim-001")
        self.assertEqual(run_payload["submitted_count"], 1)

        latest = self.client.get("/system/tail-market/latest")
        latest_payload = latest.json()
        self.assertEqual(latest.status_code, 200)
        self.assertTrue(latest_payload["ok"])
        self.assertEqual(latest_payload["account_id"], "sim-001")
        self.assertEqual(latest_payload["submitted_count"], 1)

        history = self.client.get("/system/tail-market/history", params={"limit": 5})
        history_payload = history.json()
        self.assertEqual(history.status_code, 200)
        self.assertTrue(history_payload["ok"])
        self.assertGreaterEqual(history_payload["count"], 1)
        self.assertEqual(history_payload["items"][0]["account_id"], "sim-001")

        review = self.client.get(
            "/system/tail-market/review",
            params={"source": "latest", "review_tag": "leader_style"},
        )
        review_payload = review.json()
        self.assertEqual(review.status_code, 200)
        self.assertTrue(review_payload["ok"])
        self.assertTrue(review_payload["available"])
        self.assertGreaterEqual(review_payload["count"], 1)
        self.assertEqual(review_payload["items"][0]["symbol"], "600519.SH")
        self.assertTrue(any(item["key"] == "leader_style" for item in review_payload["by_review_tag"]))
        self.assertTrue(
            any(item["key"] == review_payload["items"][0]["exit_reason"] for item in review_payload["by_exit_reason"])
        )

    def test_review_board_aggregates_governance_tail_market_and_discussion(self) -> None:
        trade_date, _ = self._prepare_finalized_discussion()
        meeting_state = get_meeting_state_store()
        meeting_state.set(
            "latest_tail_market_scan",
            {
                "status": "ok",
                "trade_date": trade_date,
                "account_id": "sim-001",
                "scanned_at": f"{trade_date}T14:50:00",
                "position_count": 1,
                "signal_count": 1,
                "submitted_count": 0,
                "preview_count": 1,
                "error_count": 0,
                "summary_lines": ["尾盘卖出扫描完成: positions=1 signals=1 submitted=0 preview=1 errors=0."],
                "items": [
                    {
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "status": "preview",
                        "exit_reason": "sector_retreat",
                        "review_tags": ["sector_retreat", "leader_style"],
                    }
                ],
            },
        )

        settings = get_settings()
        service = TradeAttributionService(settings.storage_root / "learning" / "trade_attribution.json")
        service.record_outcomes(
            trade_date=trade_date,
            score_date=trade_date,
            items=[
                TradeAttributionRecord(
                    trade_date=trade_date,
                    score_date=trade_date,
                    symbol="600519.SH",
                    name="贵州茅台",
                    account_id="sim-001",
                    order_id="review-board-1",
                    side="SELL",
                    source="execution_reconciliation",
                    status="FILLED",
                    playbook="leader_chase",
                    regime="trend",
                    exit_reason="time_stop",
                    next_day_close_pct=-0.012,
                    holding_days=1,
                    exit_context_snapshot={
                        "sector_relative_trend_5m": -0.018,
                        "sector_underperform_bars_5m": 3,
                    },
                    review_tags=["sector_relative_trend_weak", "leader_style"],
                    recorded_at=datetime.now().isoformat(),
                )
            ],
        )
        proposal_payload = self.client.post(
            "/system/learning/parameter-hints/proposals",
            json={
                "trade_date": trade_date,
                "score_date": trade_date,
                "review_tag": "sector_relative_trend_weak",
                "apply": True,
            },
        ).json()
        self.assertTrue(proposal_payload["ok"])
        offline_report = OfflineBacktestAttributionService(
            now_factory=lambda: datetime(2026, 4, 7, 15, 30, 0)
        ).build_report(
            [
                {
                    "trade_id": "rb-1",
                    "symbol": "600519.SH",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "exit_reason": "time_stop",
                    "return_pct": -0.012,
                    "trade_date": trade_date,
                },
                {
                    "trade_id": "rb-2",
                    "symbol": "300750.SZ",
                    "playbook": "low_absorb",
                    "regime": "range",
                    "exit_reason": "board_break",
                    "return_pct": 0.018,
                    "trade_date": trade_date,
                },
            ],
            weakest_bucket_dimension="playbook",
            compare_view_dimension="regime",
        )
        serving_dir = settings.storage_root / "serving"
        serving_dir.mkdir(parents=True, exist_ok=True)
        (serving_dir / "latest_offline_backtest_attribution.json").write_text(
            json.dumps(offline_report.model_dump(), ensure_ascii=False),
            encoding="utf-8",
        )
        metrics = MetricsCalculator().calc(
            pd.Series(dtype=float),
            [
                {
                    "symbol": "600519.SH",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "exit_reason": "time_stop",
                    "return_pct": -0.012,
                },
                {
                    "symbol": "300750.SZ",
                    "playbook": "low_absorb",
                    "regime": "range",
                    "exit_reason": "board_break",
                    "return_pct": 0.018,
                },
            ],
        )
        (serving_dir / "latest_offline_backtest_metrics.json").write_text(
            json.dumps(
                {
                    "metrics_scope": metrics.metrics_scope,
                    "semantics_note": metrics.semantics_note,
                    "overview": metrics.export_payload["overview"],
                    "by_playbook_metrics": metrics.by_playbook_metrics,
                    "by_regime_metrics": metrics.by_regime_metrics,
                    "by_exit_reason_metrics": metrics.by_exit_reason_metrics,
                    "win_rate_by_playbook": metrics.win_rate_by_playbook,
                    "avg_return_by_regime": metrics.avg_return_by_regime,
                    "exit_reason_distribution": metrics.exit_reason_distribution,
                    "calmar_by_playbook": metrics.calmar_by_playbook,
                    "export_payload": metrics.export_payload,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monitor_state = get_monitor_state_service()
        monitor_state.save_exit_snapshot(
            {
                "version": "v1",
                "checked_at": 1712476200.0,
                "signal_count": 2,
                "watched_symbols": ["600519.SH", "300750.SZ"],
                "by_symbol": [{"key": "600519.SH", "count": 1}, {"key": "300750.SZ", "count": 1}],
                "by_reason": [{"key": "sector_retreat", "count": 1}, {"key": "time_stop", "count": 1}],
                "by_severity": [{"key": "warning", "count": 2}],
                "by_tag": [{"key": "sector_retreat", "count": 1}, {"key": "intraday_fade", "count": 1}],
                "summary_lines": ["退出监控命中 2 条信号。"],
                "items": [
                    {"symbol": "600519.SH", "reason": "sector_retreat", "severity": "warning"},
                    {"symbol": "300750.SZ", "reason": "time_stop", "severity": "warning"},
                ],
            }
        )
        monitor_state.save_execution_bridge_health(
            {
                "version": "v1",
                "checked_at": 1712476260.0,
                "reported_at": f"{trade_date}T14:59:30",
                "source_id": "windows-vm-qmt-01",
                "deployment_role": "windows_execution_gateway",
                "bridge_path": "linux_openclaw->windows_gateway->qmt_vm",
                "overall_status": "degraded",
                "gateway_online": True,
                "qmt_connected": False,
                "account_id": "sim-001",
                "session_fresh_seconds": 95,
                "attention_components": ["QMT VM"],
                "attention_component_keys": ["qmt_vm"],
                "last_error": "qmt reconnect timeout",
                "summary_lines": ["执行桥最新状态 degraded，需关注 QMT VM。"],
                "windows_execution_gateway": {
                    "status": "healthy",
                    "reachable": True,
                    "latency_ms": 12.0,
                    "staleness_seconds": 2.0,
                    "error_count": 0,
                    "success_count": 8,
                    "last_ok_at": f"{trade_date}T14:58:00",
                    "last_error_at": "",
                    "detail": "gateway ok",
                    "tags": ["linux_openclaw_bridge"],
                },
                "qmt_vm": {
                    "status": "degraded",
                    "reachable": False,
                    "latency_ms": 180.0,
                    "staleness_seconds": 95.0,
                    "error_count": 2,
                    "success_count": 3,
                    "last_ok_at": f"{trade_date}T14:55:00",
                    "last_error_at": f"{trade_date}T14:59:00",
                    "detail": "windows vm heartbeat stale",
                    "tags": ["windows_vm", "qmt_bridge"],
                },
            }
        )
        self_improvement_runner = PlaybookBacktestRunner(
            now_factory=lambda: datetime(2026, 4, 7, 15, 35, 0)
        )
        self_improvement_result = self_improvement_runner.run(
            [
                {
                    "trade_id": "rb-1",
                    "symbol": "600519.SH",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "exit_reason": "time_stop",
                    "return_pct": -0.012,
                    "trade_date": trade_date,
                },
                {
                    "trade_id": "rb-2",
                    "symbol": "300750.SZ",
                    "playbook": "low_absorb",
                    "regime": "range",
                    "exit_reason": "board_break",
                    "return_pct": 0.018,
                    "trade_date": trade_date,
                },
            ]
        )
        self_improvement_runner.write_latest_offline_self_improvement_export(
            self_improvement_result,
            serving_dir,
        )

        response = self.client.get(
            "/system/reports/review-board",
            params={
                "trade_date": trade_date,
                "score_date": trade_date,
                "due_within_days": 5,
                "tail_market_source": "latest",
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["available"])
        self.assertEqual(payload["trade_date"], trade_date)
        self.assertIn("sections", payload)
        self.assertGreaterEqual(payload["counts"]["governance_high_priority_action_item_count"], 1)
        self.assertEqual(payload["counts"]["tail_market_count"], 1)
        self.assertTrue(payload["sections"]["discussion"]["available"])
        self.assertTrue(payload["sections"]["offline_backtest"]["available"])
        self.assertTrue(payload["sections"]["exit_monitor"]["available"])
        self.assertTrue(payload["sections"]["execution_bridge_health"]["available"])
        self.assertTrue(payload["sections"]["offline_backtest_metrics"]["available"])
        self.assertEqual(payload["sections"]["tail_market"]["count"], 1)
        self.assertEqual(payload["counts"]["offline_backtest_trade_count"], 2)
        self.assertEqual(payload["counts"]["exit_monitor_signal_count"], 2)
        self.assertEqual(payload["counts"]["execution_bridge_attention_count"], 1)
        self.assertEqual(payload["counts"]["offline_backtest_metrics_trade_count"], 2)
        action_sources = {item["source"] for item in payload["action_items"]}
        self.assertIn("governance", action_sources)
        self.assertIn("tail_market", action_sources)
        self.assertIn("offline_backtest", action_sources)
        self.assertIn("exit_monitor", action_sources)
        self.assertIn("execution_bridge_health", action_sources)
        self.assertIn("offline_backtest_metrics", action_sources)
        self.assertTrue(payload["sections"]["exit_monitor"]["trend_summary"]["available"])
        self.assertEqual(payload["sections"]["execution_bridge_health"]["overall_status"], "degraded")
        self.assertEqual(payload["sections"]["execution_bridge_health"]["source_id"], "windows-vm-qmt-01")
        self.assertEqual(payload["sections"]["execution_bridge_health"]["deployment_role"], "windows_execution_gateway")
        self.assertEqual(
            payload["sections"]["execution_bridge_health"]["bridge_path"],
            "linux_openclaw->windows_gateway->qmt_vm",
        )
        self.assertTrue(payload["sections"]["execution_bridge_health"]["trend_summary"]["available"])
        self.assertEqual(
            payload["sections"]["execution_bridge_health"]["trend_summary"]["latest_source_id"],
            "windows-vm-qmt-01",
        )
        self.assertTrue(any(line.startswith("盘后 review board") for line in payload["summary_lines"]))

        postclose = self.client.get("/system/reports/postclose-master").json()
        self.assertIn("latest_review_board", postclose)
        self.assertIn("latest_exit_snapshot", postclose)
        self.assertIn("latest_exit_snapshot_trend_summary", postclose)
        self.assertIn("latest_execution_bridge_health", postclose)
        self.assertIn("latest_execution_bridge_health_trend_summary", postclose)
        self.assertIn("latest_offline_backtest_attribution", postclose)
        self.assertIn("latest_offline_backtest_metrics", postclose)
        self.assertIn("latest_offline_self_improvement", postclose)
        self.assertEqual(postclose["latest_exit_snapshot"]["snapshot"]["signal_count"], 2)
        self.assertTrue(postclose["latest_exit_snapshot_trend_summary"]["available"])
        self.assertEqual(postclose["latest_execution_bridge_health"]["health"]["overall_status"], "degraded")
        self.assertEqual(postclose["latest_execution_bridge_health"]["health"]["source_id"], "windows-vm-qmt-01")
        self.assertEqual(
            postclose["latest_execution_bridge_health_trend_summary"]["latest_bridge_path"],
            "linux_openclaw->windows_gateway->qmt_vm",
        )
        self.assertTrue(postclose["latest_execution_bridge_health_trend_summary"]["available"])
        self.assertTrue(postclose["latest_offline_backtest_attribution"]["available"])
        self.assertEqual(postclose["latest_offline_backtest_metrics"]["overview"]["total_trades"], 2)
        self.assertFalse(postclose["latest_offline_self_improvement"]["live_execution_allowed"])
        self.assertEqual(postclose["latest_review_board"]["counts"]["tail_market_count"], 1)

    def test_openclaw_opinion_ingress_endpoint_writes_batch(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 2, "auto_trade": False, "account_id": "sim-001"},
        )
        case = self.client.get("/system/cases").json()["items"][0]

        response = self.client.post(
            "/system/discussions/opinions/openclaw-ingress",
            json={
                "trade_date": case["trade_date"],
                "expected_round": 1,
                "expected_agent_id": "ashare-research",
                "expected_case_ids": [case["case_id"]],
                "auto_rebuild": True,
                "payload": {
                    "output": {
                        "items": [
                            {
                                "symbol": case["symbol"],
                                "stance": "selected",
                                "reasons": ["研究支持"],
                                "evidence_refs": ["openclaw:research:1"],
                            }
                        ]
                    }
                },
            },
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["trade_date"], case["trade_date"])
        self.assertEqual(payload["written_count"], 1)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["normalized_items"][0]["stance"], "support")
        self.assertTrue(payload["refresh_summary"])
        self.assertEqual(payload["refreshed_summary_snapshot"]["selected_count"], 0)
        self.assertEqual(payload["touched_case_summaries"][0]["case_id"], case["case_id"])

        updated_case = self.client.get(f"/system/cases/{case['case_id']}").json()
        self.assertEqual(updated_case["opinions"][0]["agent_id"], "ashare-research")
        self.assertEqual(updated_case["opinions"][0]["stance"], "support")

    def test_openclaw_opinion_preview_endpoint_normalizes_without_writeback(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 2, "auto_trade": False, "account_id": "sim-001"},
        )
        case = self.client.get("/system/cases").json()["items"][0]

        response = self.client.post(
            "/system/discussions/opinions/openclaw-preview",
            json={
                "trade_date": case["trade_date"],
                "expected_round": 1,
                "expected_agent_id": "ashare-research",
                "expected_case_ids": [case["case_id"]],
                "payload": {
                    "output": {
                        "items": [
                            {
                                "symbol": case["symbol"],
                                "stance": "selected",
                                "reasons": ["研究支持"],
                                "evidence_refs": ["openclaw:preview:1"],
                            }
                        ]
                    }
                },
            },
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["trade_date"], case["trade_date"])
        self.assertEqual(payload["normalized_items"][0]["stance"], "support")
        self.assertEqual(payload["case_id_map"][case["symbol"]], case["case_id"])
        self.assertIsNone(payload["default_case_id"])

        unchanged_case = self.client.get(f"/system/cases/{case['case_id']}").json()
        self.assertEqual(unchanged_case["opinions"], [])

    def test_openclaw_replay_packet_endpoint_builds_offline_packet_without_writeback(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 2, "auto_trade": False, "account_id": "sim-001"},
        )
        case = self.client.get("/system/cases").json()["items"][0]

        response = self.client.post(
            "/system/discussions/opinions/openclaw-replay-packet",
            json={
                "trade_date": case["trade_date"],
                "expected_round": 1,
                "expected_agent_id": "ashare-research",
                "expected_case_ids": [case["case_id"]],
                "payload": {
                    "output": {
                        "items": [
                            {
                                "symbol": case["symbol"],
                                "stance": "selected",
                                "reasons": ["研究支持"],
                                "evidence_refs": ["openclaw:replay:1"],
                            }
                        ]
                    }
                },
            },
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["packet_type"], "openclaw_replay_packet")
        self.assertTrue(payload["offline_only"])
        self.assertFalse(payload["live_trigger"])
        self.assertTrue(payload["packet_id"].startswith(f"openclaw_replay_packet-{case['trade_date'].replace('-', '')}-"))
        self.assertEqual(payload["research_track"], "post_close_replay")
        self.assertTrue(any(item["kind"] == "discussion_cycle" for item in payload["source_refs"]))
        self.assertIn("research_track:post_close_replay", payload["archive_tags"])
        self.assertEqual(payload["writeback_preview"]["count"], 1)
        self.assertEqual(payload["preview"]["normalized_items"][0]["stance"], "support")

        unchanged_case = self.client.get(f"/system/cases/{case['case_id']}").json()
        self.assertEqual(unchanged_case["opinions"], [])

    def test_openclaw_packet_archive_endpoints_persist_latest_packets(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 2, "auto_trade": False, "account_id": "sim-001"},
        )
        case = self.client.get("/system/cases").json()["items"][0]
        packet_request = {
            "trade_date": case["trade_date"],
            "expected_round": 1,
            "expected_agent_id": "ashare-research",
            "expected_case_ids": [case["case_id"]],
            "payload": {
                "output": {
                    "items": [
                        {
                            "symbol": case["symbol"],
                            "stance": "selected",
                            "reasons": ["研究支持"],
                            "evidence_refs": ["openclaw:archive:1"],
                        }
                    ]
                }
            },
        }
        replay_packet = self.client.post(
            "/system/discussions/opinions/openclaw-replay-packet",
            json=packet_request,
        ).json()
        proposal_packet = self.client.post(
            "/system/discussions/opinions/openclaw-proposal-packet",
            json=packet_request,
        ).json()

        replay_response = self.client.post(
            "/system/discussions/opinions/openclaw-replay-packet/archive",
            json={"payload": replay_packet},
        )
        replay_payload = replay_response.json()
        self.assertEqual(replay_response.status_code, 200)
        self.assertTrue(replay_payload["ok"])
        self.assertEqual(replay_payload["packet"]["packet_type"], "openclaw_replay_packet")
        self.assertEqual(
            replay_payload["packet"]["archive_manifest"]["artifact_name"],
            f"{replay_payload['packet']['packet_id']}.json",
        )

        proposal_response = self.client.post(
            "/system/discussions/opinions/openclaw-proposal-packet/archive",
            json={"payload": proposal_packet},
        )
        proposal_payload = proposal_response.json()
        self.assertEqual(proposal_response.status_code, 200)
        self.assertTrue(proposal_payload["ok"])
        self.assertEqual(proposal_payload["packet"]["packet_type"], "openclaw_proposal_packet")
        self.assertEqual(
            proposal_payload["packet"]["archive_manifest"]["artifact_name"],
            f"{proposal_payload['packet']['packet_id']}.json",
        )

        postclose = self.client.get("/system/reports/postclose-master").json()
        self.assertEqual(
            postclose["latest_openclaw_replay_packet"]["packet_id"],
            replay_payload["packet"]["packet_id"],
        )
        self.assertEqual(
            postclose["latest_openclaw_proposal_packet"]["packet_id"],
            proposal_payload["packet"]["packet_id"],
        )
        self.assertTrue(postclose["latest_openclaw_replay_packet"]["offline_only"])
        self.assertFalse(postclose["latest_openclaw_proposal_packet"]["live_trigger"])
        self.assertEqual(
            postclose["latest_openclaw_replay_packet"]["archive_manifest"]["archive_path"],
            replay_payload["packet"]["archive_manifest"]["archive_path"],
        )
        self.assertEqual(
            postclose["latest_openclaw_proposal_packet"]["archive_manifest"]["archive_path"],
            proposal_payload["packet"]["archive_manifest"]["archive_path"],
        )
        self.assertEqual(
            postclose["latest_openclaw_replay_descriptor"]["archive_path"],
            replay_payload["packet"]["latest_descriptor"]["archive_path"],
        )
        self.assertEqual(
            postclose["latest_openclaw_proposal_descriptor"]["archive_path"],
            proposal_payload["packet"]["latest_descriptor"]["archive_path"],
        )

        storage_root = Path(os.environ["ASHARE_STORAGE_ROOT"])
        replay_archive_path = storage_root / "features" / replay_payload["packet"]["archive_manifest"]["archive_path"]
        proposal_archive_path = storage_root / "features" / proposal_payload["packet"]["archive_manifest"]["archive_path"]
        self.assertTrue(replay_archive_path.exists())
        self.assertTrue(proposal_archive_path.exists())
        for alias in replay_payload["packet"]["archive_manifest"]["latest_aliases"]:
            self.assertTrue((storage_root / "features" / alias).exists())
        for alias in proposal_payload["packet"]["archive_manifest"]["latest_aliases"]:
            self.assertTrue((storage_root / "features" / alias).exists())

        replay_report = self.client.get("/system/reports/openclaw-replay-packet").json()
        proposal_report = self.client.get("/system/reports/openclaw-proposal-packet").json()
        self.assertTrue(replay_report["available"])
        self.assertTrue(proposal_report["available"])
        self.assertEqual(
            replay_report["latest_descriptor"]["packet_id"],
            replay_payload["packet"]["packet_id"],
        )
        self.assertEqual(
            proposal_report["latest_descriptor"]["packet_id"],
            proposal_payload["packet"]["packet_id"],
        )
        self.assertEqual(
            replay_report["latest_descriptor"]["latest_aliases"],
            replay_payload["packet"]["archive_manifest"]["latest_aliases"],
        )
        self.assertEqual(
            replay_report["contract_sample"]["linux_openclaw_samples"]["archive_reference"]["archive_path"],
            replay_payload["packet"]["latest_descriptor"]["archive_path"],
        )
        self.assertEqual(
            proposal_report["contract_sample"]["proposal_descriptor_minimal"]["research_track"],
            "self_evolution_research",
        )

    def test_openclaw_proposal_packet_endpoint_builds_research_only_packet(self) -> None:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 2, "auto_trade": False, "account_id": "sim-001"},
        )
        case = self.client.get("/system/cases").json()["items"][0]

        response = self.client.post(
            "/system/discussions/opinions/openclaw-proposal-packet",
            json={
                "trade_date": case["trade_date"],
                "expected_round": 1,
                "expected_agent_id": "ashare-research",
                "expected_case_ids": [case["case_id"]],
                "payload": {
                    "output": {
                        "items": [
                            {
                                "symbol": case["symbol"],
                                "stance": "selected",
                                "reasons": ["研究支持"],
                                "evidence_refs": ["openclaw:proposal:1"],
                            }
                        ]
                    }
                },
            },
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["packet_type"], "openclaw_proposal_packet")
        self.assertTrue(payload["offline_only"])
        self.assertFalse(payload["live_trigger"])
        self.assertTrue(payload["packet_id"].startswith(f"openclaw_proposal_packet-{case['trade_date'].replace('-', '')}-"))
        self.assertEqual(payload["research_track"], "self_evolution_research")
        self.assertTrue(any(item["kind"] == "openclaw_preview" for item in payload["source_refs"]))
        self.assertIn("research_track:self_evolution_research", payload["archive_tags"])
        self.assertEqual(payload["proposal_packet"]["mode"], "self_evolution_research")
        self.assertEqual(payload["proposal_packet"]["writeback_candidates"][0]["case_id"], case["case_id"])

        unchanged_case = self.client.get(f"/system/cases/{case['case_id']}").json()
        self.assertEqual(unchanged_case["opinions"], [])

    def test_serving_latest_index_aggregates_latest_descriptors(self) -> None:
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 15, 35, 0))
        result = runner.run(
            [
                {
                    "trade_id": "index-1",
                    "symbol": "600519.SH",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "exit_reason": "time_stop",
                    "return_pct": -0.02,
                    "trade_date": "2026-04-07",
                },
                {
                    "trade_id": "index-2",
                    "symbol": "000001.SZ",
                    "playbook": "low_absorb",
                    "regime": "range",
                    "exit_reason": "board_break",
                    "return_pct": 0.01,
                    "trade_date": "2026-04-07",
                },
            ]
        )
        self.client.post("/system/reports/offline-self-improvement/persist", json=result.model_dump())

        self.client.post(
            "/system/monitor/execution-bridge-health",
            json=build_execution_bridge_health_ingress_payload(
                {
                    "source_id": "windows-vm-a",
                    "deployment_role": "primary_gateway",
                    "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                    "gateway_online": True,
                    "qmt_connected": True,
                }
            ),
        )

        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 2, "auto_trade": False, "account_id": "sim-001"},
        )
        case = self.client.get("/system/cases").json()["items"][0]
        packet_request = {
            "trade_date": case["trade_date"],
            "expected_round": 1,
            "expected_agent_id": "ashare-research",
            "expected_case_ids": [case["case_id"]],
            "payload": {
                "output": {
                    "items": [
                        {
                            "symbol": case["symbol"],
                            "stance": "selected",
                            "reasons": ["研究支持"],
                            "evidence_refs": ["openclaw:index:1"],
                        }
                    ]
                }
            },
        }
        replay_packet = self.client.post("/system/discussions/opinions/openclaw-replay-packet", json=packet_request).json()
        proposal_packet = self.client.post("/system/discussions/opinions/openclaw-proposal-packet", json=packet_request).json()
        self.client.post("/system/discussions/opinions/openclaw-replay-packet/archive", json={"payload": replay_packet})
        self.client.post("/system/discussions/opinions/openclaw-proposal-packet/archive", json={"payload": proposal_packet})

        replay_report = self.client.get("/system/reports/openclaw-replay-packet").json()
        proposal_report = self.client.get("/system/reports/openclaw-proposal-packet").json()

        response = self.client.get("/system/reports/serving-latest-index")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(
            payload["items"]["execution_bridge_health"]["template_path"],
            "/system/monitor/execution-bridge-health/template",
        )
        self.assertEqual(
            payload["items"]["offline_self_improvement"]["latest_descriptor"]["guardrails"]["research_track"],
            "self_evolution_research",
        )
        self.assertEqual(
            payload["items"]["offline_self_improvement"]["descriptor_contract_sample"]["contract_scope"],
            "offline_self_improvement_descriptor_contract_sample",
        )
        self.assertEqual(
            payload["items"]["openclaw_replay_packet"]["latest_descriptor"]["packet_type"],
            "openclaw_replay_packet",
        )
        self.assertEqual(
            replay_report["contract_sample"]["replay_descriptor_minimal"]["packet_type"],
            "openclaw_replay_packet",
        )
        self.assertEqual(
            payload["items"]["openclaw_replay_packet"]["contract_sample"]["linux_openclaw_samples"]["latest_pull"][
                "expect_descriptor_fields"
            ][0],
            "packet_type",
        )
        self.assertEqual(
            payload["items"]["openclaw_proposal_packet"]["latest_descriptor"]["packet_type"],
            "openclaw_proposal_packet",
        )
        self.assertEqual(
            proposal_report["contract_sample"]["proposal_descriptor_minimal"]["packet_type"],
            "openclaw_proposal_packet",
        )
        self.assertFalse(payload["items"]["openclaw_proposal_packet"]["contract_sample"]["live_trigger"])
        self.assertEqual(
            payload["items"]["execution_bridge_health"]["deployment_contract_sample"]["http_samples"][
                "windows_gateway_post"
            ]["path"],
            "/system/monitor/execution-bridge-health",
        )

    def test_postclose_deployment_handoff_aggregates_review_postclose_and_bootstrap(self) -> None:
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 15, 35, 0))
        result = runner.run(
            [
                {
                    "trade_id": "handoff-1",
                    "symbol": "600519.SH",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "exit_reason": "time_stop",
                    "return_pct": -0.02,
                    "trade_date": "2026-04-07",
                },
                {
                    "trade_id": "handoff-2",
                    "symbol": "000001.SZ",
                    "playbook": "low_absorb",
                    "regime": "range",
                    "exit_reason": "board_break",
                    "return_pct": 0.01,
                    "trade_date": "2026-04-07",
                },
            ]
        )
        self.client.post("/system/reports/offline-self-improvement/persist", json=result.model_dump())
        self.client.post(
            "/system/monitor/execution-bridge-health",
            json=build_execution_bridge_health_ingress_payload(
                {
                    "source_id": "windows-vm-a",
                    "deployment_role": "primary_gateway",
                    "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                    "gateway_online": True,
                    "qmt_connected": True,
                }
            ),
        )

        self.client.post(
            "/runtime/jobs/pipeline",
            json={"universe_scope": "main-board", "max_candidates": 2, "auto_trade": False, "account_id": "sim-001"},
        )
        case = self.client.get("/system/cases").json()["items"][0]
        packet_request = {
            "trade_date": case["trade_date"],
            "expected_round": 1,
            "expected_agent_id": "ashare-research",
            "expected_case_ids": [case["case_id"]],
            "payload": {
                "output": {
                    "items": [
                        {
                            "symbol": case["symbol"],
                            "stance": "selected",
                            "reasons": ["研究支持"],
                            "evidence_refs": ["openclaw:handoff:1"],
                        }
                    ]
                }
            },
        }
        replay_packet = self.client.post("/system/discussions/opinions/openclaw-replay-packet", json=packet_request).json()
        proposal_packet = self.client.post("/system/discussions/opinions/openclaw-proposal-packet", json=packet_request).json()
        self.client.post("/system/discussions/opinions/openclaw-replay-packet/archive", json={"payload": replay_packet})
        self.client.post("/system/discussions/opinions/openclaw-proposal-packet/archive", json={"payload": proposal_packet})

        response = self.client.get("/system/reports/postclose-deployment-handoff")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["handoff_scope"], "postclose_deployment_handoff")
        self.assertIn("review_board", payload["sections"])
        self.assertIn("postclose_master", payload["sections"])
        self.assertIn("serving_latest_index", payload["sections"])
        self.assertIn("deployment_bootstrap_contracts", payload["sections"])
        self.assertEqual(
            payload["report_paths"]["postclose_master"],
            "/system/reports/postclose-master",
        )
        self.assertEqual(
            payload["sections"]["deployment_bootstrap_contracts"]["report_paths"]["serving_latest_index"],
            "/system/reports/serving-latest-index",
        )
        self.assertEqual(
            payload["sections"]["serving_latest_index"]["items"]["openclaw_replay_packet"]["latest_descriptor"]["packet_type"],
            "openclaw_replay_packet",
        )
        self.assertTrue(payload["sections"]["postclose_master"]["latest_offline_self_improvement_descriptor"])
        self.assertEqual(
            payload["sections"]["postclose_master"]["latest_offline_self_improvement"]["descriptor_contract_sample"][
                "contract_scope"
            ],
            "offline_self_improvement_descriptor_contract_sample",
        )
        self.assertEqual(
            payload["sections"]["postclose_master"]["latest_openclaw_replay_packet"]["contract_sample"][
                "replay_descriptor_minimal"
            ]["packet_type"],
            "openclaw_replay_packet",
        )
        self.assertEqual(
            payload["sections"]["serving_latest_index"]["items"]["execution_bridge_health"][
                "deployment_contract_sample"
            ]["source_value_samples"]["windows_gateway_primary"]["source_id"],
            "windows-vm-a",
        )
        self.assertTrue(payload["summary_lines"])

    def test_review_board_and_handoff_include_control_plane_gateway_summary(self) -> None:
        meeting_state = get_meeting_state_store()
        self._store_gateway_intent(intent_id="intent-review-handoff-1")
        meeting_state.set(
            "latest_execution_gateway_receipt",
            {
                "receipt_id": "receipt-review-handoff-1",
                "intent_id": "intent-review-handoff-0",
                "gateway_source_id": "windows-vm-a",
                "deployment_role": "primary_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                "reported_at": "2026-04-08T15:01:05+08:00",
                "submitted_at": "2026-04-08T15:01:02+08:00",
                "status": "submitted",
                "broker_order_id": "qmt-review-handoff-1",
                "order": {
                    "symbol": "600519.SH",
                    "side": "BUY",
                    "price": 1688.0,
                    "quantity": 100,
                },
                "fills": [],
                "summary_lines": ["Gateway latest receipt 已更新。"],
            },
        )
        meeting_state.set(
            "latest_execution_dispatch",
            {
                "trade_date": "2026-04-08",
                "status": "queued_for_gateway",
                "queued_count": 1,
                "summary_lines": ["讨论执行已入队。"],
                "receipts": [
                    {
                        "status": "queued_for_gateway",
                        "intent_id": "intent-review-handoff-1",
                        "gateway_intent": {
                            "intent_id": "intent-review-handoff-1",
                            "approval_source": "discussion_execution_dispatch",
                        },
                    }
                ],
            },
        )
        meeting_state.set(
            "latest_tail_market_scan",
            {
                "status": "queued_for_gateway",
                "trade_date": "2026-04-08",
                "queued_count": 1,
                "summary_lines": ["尾盘扫描已入队。"],
                "items": [
                    {
                        "status": "queued_for_gateway",
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "exit_reason": "sector_retreat",
                        "review_tags": ["leader_style"],
                        "gateway_intent": {
                            "intent_id": "intent-tail-review-1",
                            "approval_source": "tail_market_scan",
                        },
                    }
                ],
            },
        )

        review_payload = self.client.get("/system/reports/review-board", params={"trade_date": "2026-04-08"}).json()
        self.assertTrue(review_payload["ok"])
        self.assertEqual(review_payload["pending_intent_count"], 1)
        self.assertEqual(review_payload["queued_for_gateway_count"], 1)
        self.assertEqual(review_payload["discussion_dispatch_queued_for_gateway_count"], 1)
        self.assertEqual(review_payload["tail_market_queued_for_gateway_count"], 1)
        self.assertEqual(review_payload["latest_gateway_source"], "windows-vm-a")
        self.assertEqual(review_payload["latest_receipt_summary"]["receipt_id"], "receipt-review-handoff-1")
        self.assertEqual(review_payload["counts"]["pending_intent_count"], 1)
        self.assertEqual(review_payload["counts"]["tail_market_queued_for_gateway_count"], 1)
        self.assertEqual(
            review_payload["sections"]["control_plane_gateway"]["discussion_dispatch"]["queued_for_gateway_count"],
            1,
        )

        handoff_payload = self.client.get(
            "/system/reports/postclose-deployment-handoff",
            params={"trade_date": "2026-04-08"},
        ).json()
        self.assertTrue(handoff_payload["available"])
        self.assertEqual(handoff_payload["pending_intent_count"], 1)
        self.assertEqual(handoff_payload["queued_for_gateway_count"], 1)
        self.assertEqual(handoff_payload["discussion_dispatch_queued_for_gateway_count"], 1)
        self.assertEqual(handoff_payload["tail_market_queued_for_gateway_count"], 1)
        self.assertEqual(handoff_payload["latest_gateway_source"], "windows-vm-a")
        self.assertEqual(handoff_payload["latest_receipt_summary"]["status"], "submitted")
        self.assertEqual(
            handoff_payload["sections"]["control_plane_gateway"]["latest_receipt_summary"]["receipt_id"],
            "receipt-review-handoff-1",
        )

    def test_offline_backtest_attribution_report_endpoint_reads_latest_export(self) -> None:
        settings = get_settings()
        report = OfflineBacktestAttributionService(now_factory=lambda: datetime(2026, 4, 7, 15, 30, 0)).build_report(
            [
                {
                    "trade_id": "bt-1",
                    "symbol": "600519.SH",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "exit_reason": "board_break",
                    "return_pct": 0.032,
                    "trade_date": "2026-04-07",
                },
                {
                    "trade_id": "bt-2",
                    "symbol": "000001.SZ",
                    "playbook": "low_absorb",
                    "regime": "range",
                    "exit_reason": "time_stop",
                    "return_pct": -0.021,
                    "trade_date": "2026-04-07",
                },
                {
                    "trade_id": "bt-3",
                    "symbol": "300750.SZ",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "exit_reason": "time_stop",
                    "return_pct": -0.008,
                    "trade_date": "2026-04-07",
                },
            ],
            weakest_bucket_dimension="playbook",
            compare_view_dimension="regime",
        )
        serving_dir = settings.storage_root / "serving"
        serving_dir.mkdir(parents=True, exist_ok=True)
        (serving_dir / "latest_offline_backtest_attribution.json").write_text(
            json.dumps(report.model_dump(), ensure_ascii=False),
            encoding="utf-8",
        )

        response = self.client.get("/system/reports/offline-backtest-attribution")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["attribution_scope"], "offline_backtest")
        self.assertEqual(payload["overview"]["trade_count"], 3)
        self.assertTrue(payload["weakest_buckets"])
        self.assertIn("regime", payload["compare_views"])
        self.assertEqual(payload["selected_weakest_bucket"]["dimension"], "playbook")
        self.assertEqual(payload["selected_compare_view"]["dimension"], "regime")
        self.assertTrue(payload["summary_lines"])

    def test_offline_backtest_metrics_report_endpoint_reads_latest_export(self) -> None:
        settings = get_settings()
        metrics = MetricsCalculator().calc(
            pd.Series(dtype=float),
            [
                {
                    "symbol": "600519.SH",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "exit_reason": "time_stop",
                    "return_pct": 0.021,
                },
                {
                    "symbol": "300750.SZ",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "exit_reason": "board_break",
                    "return_pct": -0.012,
                },
                {
                    "symbol": "000001.SZ",
                    "playbook": "low_absorb",
                    "regime": "range",
                    "exit_reason": "time_stop",
                    "return_pct": 0.008,
                },
            ],
        )
        metrics_payload = {
            "metrics_scope": metrics.metrics_scope,
            "semantics_note": metrics.semantics_note,
            "overview": metrics.export_payload["overview"],
            "by_playbook_metrics": metrics.by_playbook_metrics,
            "by_regime_metrics": metrics.by_regime_metrics,
            "by_exit_reason_metrics": metrics.by_exit_reason_metrics,
            "win_rate_by_playbook": metrics.win_rate_by_playbook,
            "avg_return_by_regime": metrics.avg_return_by_regime,
            "exit_reason_distribution": metrics.exit_reason_distribution,
            "calmar_by_playbook": metrics.calmar_by_playbook,
            "export_payload": metrics.export_payload,
        }
        serving_dir = settings.storage_root / "serving"
        serving_dir.mkdir(parents=True, exist_ok=True)
        (serving_dir / "latest_offline_backtest_metrics.json").write_text(
            json.dumps(metrics_payload, ensure_ascii=False),
            encoding="utf-8",
        )

        response = self.client.get("/system/reports/offline-backtest-metrics")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["metrics_scope"], "offline_backtest_metrics")
        self.assertEqual(payload["overview"]["total_trades"], 3)
        self.assertEqual(payload["by_playbook_metrics"][0]["key"], "leader_chase")
        self.assertTrue(payload["export_payload"])

    def test_offline_self_improvement_report_endpoint_reads_latest_export(self) -> None:
        settings = get_settings()
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 15, 35, 0))
        result = runner.run(
            [
                {
                    "trade_id": "si-1",
                    "symbol": "600519.SH",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "exit_reason": "time_stop",
                    "return_pct": -0.02,
                    "trade_date": "2026-04-07",
                },
                {
                    "trade_id": "si-2",
                    "symbol": "300750.SZ",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "exit_reason": "board_break",
                    "return_pct": -0.01,
                    "trade_date": "2026-04-07",
                },
                {
                    "trade_id": "si-3",
                    "symbol": "000001.SZ",
                    "playbook": "low_absorb",
                    "regime": "range",
                    "exit_reason": "time_stop",
                    "return_pct": 0.012,
                    "trade_date": "2026-04-07",
                },
            ]
        )
        serving_dir = settings.storage_root / "serving"
        serving_dir.mkdir(parents=True, exist_ok=True)
        written_path = runner.write_latest_offline_self_improvement_export(result, serving_dir)

        response = self.client.get("/system/reports/offline-self-improvement")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["packet_scope"], "offline_self_improvement_export")
        self.assertFalse(payload["live_execution_allowed"])
        self.assertTrue(payload["serving_ready"])
        self.assertEqual(payload["artifact_name"], written_path.name)
        self.assertEqual(payload["metrics"]["overview"]["total_trades"], 3)
        self.assertEqual(payload["proposal_packet"]["packet_scope"], "offline_self_improvement_proposal")
        self.assertFalse(payload["proposal_packet"]["live_execution_allowed"])
        self.assertTrue(payload["proposal_packet"]["proposal"]["actions"])
        self.assertEqual(
            payload["latest_descriptor"]["latest"]["serving_path"],
            f"serving/{written_path.name}",
        )
        self.assertEqual(
            payload["archive_ref"]["research_track"],
            "self_evolution_research",
        )
        self.assertEqual(
            payload["descriptor_contract_sample"]["contract_scope"],
            "offline_self_improvement_descriptor_contract_sample",
        )
        self.assertEqual(
            payload["descriptor_contract_sample"]["recommended_fields"]["main"]["serving_path"],
            "latest_descriptor.latest.serving_path",
        )

        descriptor_report = self.client.get("/system/reports/offline-self-improvement-descriptor").json()
        self.assertTrue(descriptor_report["available"])
        self.assertEqual(
            descriptor_report["descriptor"]["latest"]["artifact_name"],
            written_path.name,
        )
        self.assertEqual(
            descriptor_report["descriptor_contract_sample"]["archive_ref_sample"]["research_track"],
            "self_evolution_research",
        )

    def test_offline_self_improvement_persist_endpoint_accepts_runner_result_payload(self) -> None:
        runner = PlaybookBacktestRunner(now_factory=lambda: datetime(2026, 4, 7, 15, 35, 0))
        result = runner.run(
            [
                {
                    "trade_id": "persist-1",
                    "symbol": "600519.SH",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "exit_reason": "time_stop",
                    "return_pct": -0.02,
                    "trade_date": "2026-04-07",
                },
                {
                    "trade_id": "persist-2",
                    "symbol": "000001.SZ",
                    "playbook": "low_absorb",
                    "regime": "range",
                    "exit_reason": "board_break",
                    "return_pct": 0.01,
                    "trade_date": "2026-04-07",
                },
            ]
        )
        response = self.client.post(
            "/system/reports/offline-self-improvement/persist",
            json=result.model_dump(),
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["payload"]["serving_ready"])
        self.assertEqual(payload["payload"]["artifact_name"], "latest_offline_self_improvement_export.json")
        self.assertFalse(payload["payload"]["proposal_packet"]["live_execution_allowed"])
        self.assertEqual(
            payload["payload"]["archive_ready_manifest"]["relative_archive_path"],
            "offline_self_improvement/2026-04-07/offline_self_improvement_export-20260407-20260407T153500.json",
        )

        report = self.client.get("/system/reports/offline-self-improvement").json()
        self.assertTrue(report["available"])
        self.assertTrue(report["serving_ready"])
        postclose = self.client.get("/system/reports/postclose-master").json()
        self.assertTrue(postclose["latest_offline_self_improvement"]["serving_ready"])
        self.assertEqual(
            postclose["latest_offline_self_improvement"]["archive_ready_manifest"]["relative_archive_path"],
            payload["payload"]["archive_ready_manifest"]["relative_archive_path"],
        )
        self.assertEqual(
            postclose["latest_offline_self_improvement_descriptor"]["archive_ref"]["archive_path"],
            payload["payload"]["archive_ready_manifest"]["relative_archive_path"],
        )

        storage_root = Path(os.environ["ASHARE_STORAGE_ROOT"])
        archive_path = storage_root / "features" / payload["payload"]["archive_ready_manifest"]["relative_archive_path"]
        self.assertTrue(archive_path.exists())

    def test_pending_order_remediation_endpoint_can_cancel_stale_orders(self) -> None:
        trade_date, finalize_payload = self._prepare_finalized_discussion()
        first_intent = finalize_payload["execution_intents"]["intents"][0]
        dispatch = self.client.post(
            "/system/discussions/execution-intents/dispatch",
            json={
                "trade_date": trade_date,
                "account_id": "sim-001",
                "intent_ids": [first_intent["intent_id"]],
                "apply": True,
            },
        )
        dispatch_payload = dispatch.json()
        self.assertEqual(dispatch.status_code, 200)
        self.assertTrue(dispatch_payload["ok"])
        submitted = [item for item in dispatch_payload["receipts"] if item["status"] == "submitted"]
        self.assertGreaterEqual(len(submitted), 1)

        self.client.post(
            "/system/config",
            json={"pending_order_auto_action": "cancel", "pending_order_cancel_after_seconds": 60},
        )
        meeting_state = get_meeting_state_store()
        journal = meeting_state.get("execution_order_journal", [])
        for item in journal:
            if item.get("order_id") == submitted[0]["order"]["order_id"]:
                item["submitted_at"] = "2026-04-04T09:30:00"
        meeting_state.set("execution_order_journal", journal)

        response = self.client.post(
            "/system/discussions/execution-orders/remediation/run",
            params={"account_id": "sim-001"},
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["auto_action"], "cancel")
        self.assertGreaterEqual(payload["stale_count"], 1)
        self.assertGreaterEqual(payload["actioned_count"], 1)
        self.assertGreaterEqual(payload["cancelled_count"], 1)

        latest = self.client.get("/system/discussions/execution-orders/remediation/latest")
        latest_payload = latest.json()
        self.assertEqual(latest.status_code, 200)
        self.assertTrue(latest_payload["ok"])
        self.assertEqual(latest_payload["auto_action"], "cancel")
        self.assertGreaterEqual(latest_payload["cancelled_count"], 1)

    def test_pending_order_remediation_cancel_pushes_trade_notification(self) -> None:
        trade_date, finalize_payload = self._prepare_finalized_discussion()
        first_intent = finalize_payload["execution_intents"]["intents"][0]
        dispatch = self.client.post(
            "/system/discussions/execution-intents/dispatch",
            json={
                "trade_date": trade_date,
                "account_id": "sim-001",
                "intent_ids": [first_intent["intent_id"]],
                "apply": True,
            },
        )
        dispatch_payload = dispatch.json()
        self.assertEqual(dispatch.status_code, 200)
        self.assertTrue(dispatch_payload["ok"])
        submitted = [item for item in dispatch_payload["receipts"] if item["status"] == "submitted"]
        self.assertGreaterEqual(len(submitted), 1)

        self.client.post(
            "/system/config",
            json={"pending_order_auto_action": "cancel", "pending_order_cancel_after_seconds": 60},
        )
        meeting_state = get_meeting_state_store()
        journal = meeting_state.get("execution_order_journal", [])
        for item in journal:
            if item.get("order_id") == submitted[0]["order"]["order_id"]:
                item["submitted_at"] = "2026-04-04T09:30:00"
        meeting_state.set("execution_order_journal", journal)

        captured: list[dict] = []

        def fake_dispatch(_self, title: str, content: str, level: str = "info", force: bool = True) -> bool:
            captured.append(
                {
                    "title": title,
                    "content": content,
                    "level": level,
                    "force": force,
                }
            )
            return True

        with patch("ashare_system.notify.dispatcher.MessageDispatcher.dispatch_trade", new=fake_dispatch):
            response = self.client.post(
                "/system/discussions/execution-orders/remediation/run",
                params={"account_id": "sim-001"},
            )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["cancelled_count"], 1)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["title"], "自动撤单")
        self.assertEqual(captured[0]["level"], "warning")
        self.assertTrue(captured[0]["force"])
        self.assertIn(submitted[0]["symbol"], captured[0]["content"])
        self.assertIn(submitted[0]["name"], captured[0]["content"])

    def test_discussion_client_brief_aggregates_selection_precheck_and_dispatch(self) -> None:
        trade_date, finalize_payload = self._prepare_finalized_discussion()
        first_intent = finalize_payload["execution_intents"]["intents"][0]
        self.client.post(
            "/system/discussions/execution-intents/dispatch",
            json={
                "trade_date": trade_date,
                "account_id": "sim-001",
                "intent_ids": [first_intent["intent_id"]],
                "apply": False,
            },
        )

        response = self.client.get(
            "/system/discussions/client-brief",
            params={"trade_date": trade_date, "selection_limit": 2, "watchlist_limit": 2, "rejected_limit": 2},
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["trade_date"], trade_date)
        self.assertIn(payload["status"], {"ready", "blocked"})
        self.assertIn("overview_lines", payload)
        self.assertIn("execution_precheck_lines", payload)
        self.assertIn("execution_dispatch_lines", payload)
        self.assertGreaterEqual(len(payload["execution_precheck_lines"]), 1)
        self.assertGreaterEqual(len(payload["execution_dispatch_lines"]), 1)
        self.assertEqual(payload["execution_dispatch_status"], "preview")
        self.assertEqual(payload["execution_dispatch_submitted_count"], 0)
        self.assertEqual(payload["execution_dispatch_preview_count"], 1)
        self.assertEqual(payload["execution_dispatch_blocked_count"], 0)
        self.assertTrue(payload["execution_dispatch_available"])
        self.assertIn("执行预检:", payload["lines"])
        self.assertIn("执行回执:", payload["lines"])
        self.assertTrue(any("执行派发状态 预演" in line for line in payload["execution_dispatch_lines"]))
        self.assertIn("reply_pack", payload)
        self.assertIn("final_brief", payload)
        self.assertIn("selected_display", payload)
        self.assertIn("watchlist_display", payload)
        self.assertIn("rejected_display", payload)
        self.assertIn("shared_context", payload)
        self.assertIn("shared_context_lines", payload)
        self.assertIn("data_catalog_ref", payload)
        self.assertIn("selected_packets", payload)
        self.assertIn("reply_pack", payload)
        self.assertIn("shared_context", payload["reply_pack"])
        self.assertIn("data_catalog_ref", payload["reply_pack"])
        self.assertIn("selection_packets", payload["final_brief"])
        self.assertIn("debate_focus_lines", payload)
        self.assertIn("challenge_exchange_lines", payload)
        self.assertIn("persuasion_summary_lines", payload)
        self.assertGreaterEqual(len(payload["debate_focus_lines"]), 1)
        self.assertGreaterEqual(len(payload["challenge_exchange_lines"]), 1)
        self.assertGreaterEqual(len(payload["persuasion_summary_lines"]), 1)
        self.assertIn("争议焦点:", payload["lines"])
        self.assertIn("关键交锋:", payload["lines"])
        self.assertIn("讨论收敛:", payload["lines"])

    def test_discussion_reply_pack_surfaces_debate_focus_and_persuasion(self) -> None:
        trade_date, _finalize_payload = self._prepare_finalized_discussion()
        response = self.client.get(
            "/system/discussions/reply-pack",
            params={"trade_date": trade_date, "selected_limit": 3, "watchlist_limit": 3, "rejected_limit": 3},
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("debate_focus_lines", payload)
        self.assertIn("challenge_exchange_lines", payload)
        self.assertIn("persuasion_summary_lines", payload)
        self.assertGreaterEqual(len(payload["debate_focus_lines"]), 1)
        self.assertGreaterEqual(len(payload["challenge_exchange_lines"]), 1)
        self.assertGreaterEqual(len(payload["persuasion_summary_lines"]), 1)
        self.assertTrue(any("补充第二轮说明" in line or "仓位需要二轮确认" in line for line in payload["debate_focus_lines"]))
        self.assertTrue(any("回应" in line or "追问" in line for line in payload["challenge_exchange_lines"]))
        self.assertTrue(any("改判" in line or "已回应问题" in line for line in payload["persuasion_summary_lines"]))

    def test_live_mode_blocks_dispatch_on_mock_adapter(self) -> None:
        os.environ["ASHARE_RUN_MODE"] = "live"
        os.environ["ASHARE_EXECUTION_MODE"] = "mock"
        os.environ["ASHARE_MARKET_MODE"] = "mock"
        os.environ["ASHARE_LIVE_ENABLE"] = "true"
        self.client.close()
        reset_container()
        self.client = ASGISyncClient(create_app())

        with patch("ashare_system.apps.system_api.is_trading_session", return_value=True):
            trade_date, finalize_payload = self._prepare_finalized_discussion()
            first_intent = finalize_payload["execution_intents"]["intents"][0]

            response = self.client.post(
                "/system/discussions/execution-intents/dispatch",
                json={
                    "trade_date": trade_date,
                    "account_id": "sim-001",
                    "intent_ids": [first_intent["intent_id"]],
                    "apply": True,
                },
            )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        blocked = [item for item in payload["receipts"] if item["status"] == "paper_blocked"]
        self.assertGreaterEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["reason"], "live_mock_fallback_blocked")

    def test_emergency_stop_blocks_precheck_and_dispatch(self) -> None:
        trade_date, finalize_payload = self._prepare_finalized_discussion()
        first_intent = finalize_payload["execution_intents"]["intents"][0]

        config_response = self.client.post(
            "/system/config",
            json={"emergency_stop": True, "trading_halt_reason": "manual risk halt"},
        )
        self.assertEqual(config_response.status_code, 200)
        self.assertTrue(config_response.json()["ok"])

        precheck = self.client.get("/system/discussions/execution-precheck", params={"trade_date": trade_date})
        precheck_payload = precheck.json()
        self.assertEqual(precheck.status_code, 200)
        self.assertTrue(precheck_payload["ok"])
        self.assertTrue(precheck_payload["emergency_stop_active"])
        self.assertEqual(precheck_payload["trading_halt_reason"], "manual risk halt")
        self.assertIn("交易已暂停", precheck_payload["summary_lines"][-1])
        self.assertIn("emergency_stop_active", precheck_payload["items"][0]["blockers"])

        response = self.client.post(
            "/system/discussions/execution-intents/dispatch",
            json={
                "trade_date": trade_date,
                "account_id": "sim-001",
                "intent_ids": [first_intent["intent_id"]],
                "apply": True,
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertTrue(payload["emergency_stop_active"])
        self.assertEqual(payload["trading_halt_reason"], "manual risk halt")
        blocked = [
            item for item in payload["receipts"]
            if item["status"] in {"paper_blocked", "blocked"} and item["reason"] == "emergency_stop_active"
        ]
        self.assertGreaterEqual(len(blocked), 1)


if __name__ == "__main__":
    unittest.main()
