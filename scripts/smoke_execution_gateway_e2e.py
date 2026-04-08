#!/usr/bin/env python3
"""Execution Gateway 端到端烟雾测试。

目标：
- discussion finalize -> dispatch -> worker -> receipt -> latest
- tail-market -> queued intent -> worker -> receipt
- 包含 noop_success 协议闭环
- 包含 discussion supervised preflight

该脚本默认在临时目录下启动 in-process ASGI app，不会触发 live。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ashare_system.app import create_app
from ashare_system.container import (
    get_execution_adapter,
    get_meeting_state_store,
    get_runtime_state_store,
    get_settings,
    reset_container,
)
from ashare_system.contracts import PositionSnapshot
from ashare_system.data.archive import DataArchiveStore
from ashare_system.windows_execution_gateway_worker import ExecutionGatewayWorker, GatewayWorkerConfig, _build_submitter


class SmokeFailure(RuntimeError):
    def __init__(self, stage: str, detail: object) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


class ASGISyncClient:
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


@dataclass
class SmokeResult:
    stage: str
    ok: bool
    detail: dict


class GatewaySmokeRunner:
    def __init__(self, *, max_candidates: int, account_id: str) -> None:
        self.max_candidates = max_candidates
        self.account_id = account_id
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        os.environ["ASHARE_STORAGE_ROOT"] = str(root / "state")
        os.environ["ASHARE_LOGS_DIR"] = str(root / "logs")
        os.environ["ASHARE_WORKSPACE"] = str(root)
        os.environ["ASHARE_MARKET_MODE"] = "mock"
        os.environ["ASHARE_EXECUTION_MODE"] = "mock"
        os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
        os.environ["ASHARE_RUN_MODE"] = "paper"
        os.environ["ASHARE_LIVE_ENABLE"] = "false"
        reset_container()
        self.client = ASGISyncClient(create_app())
        self.results: list[SmokeResult] = []

    def close(self) -> None:
        self.client.close()
        reset_container()
        self._tmpdir.cleanup()

    def run(self) -> dict:
        discussion = self._run_discussion_chain()
        tail_market = self._run_tail_market_chain()
        return {
            "ok": True,
            "mode": "inprocess_noop_success",
            "discussion": discussion,
            "tail_market": tail_market,
            "stages": [item.__dict__ for item in self.results],
            "summary_lines": [
                "discussion finalize -> dispatch -> worker -> receipt -> latest 已完成。",
                "tail-market queued intent -> worker -> receipt 已完成。",
                "noop_success 协议烟雾测试通过，且包含 supervised preflight。",
            ],
        }

    def _record(self, stage: str, detail: dict, *, ok: bool = True) -> None:
        self.results.append(SmokeResult(stage=stage, ok=ok, detail=detail))

    def _ensure(self, stage: str, condition: bool, detail: object) -> None:
        if not condition:
            raise SmokeFailure(stage, detail)

    def _build_worker(self) -> ExecutionGatewayWorker:
        return ExecutionGatewayWorker(
            GatewayWorkerConfig(
                control_plane_base_url="http://testserver",
                source_id="windows-vm-a",
                deployment_role="primary_gateway",
                bridge_path="linux_openclaw -> windows_gateway -> qmt_vm",
            ),
            submitter=_build_submitter("noop_success"),
            client=WorkerASGIClient(self.client),
            now_factory=lambda: datetime(2026, 4, 8, 15, 1, 2),
        )

    def _run_discussion_chain(self) -> dict:
        trade_date, finalize_payload = self._prepare_finalized_discussion()
        self._record(
            "discussion.finalize",
            {
                "trade_date": trade_date,
                "cycle_state": finalize_payload["cycle"]["discussion_state"],
                "intent_count": finalize_payload["execution_intents"]["intent_count"],
            },
        )

        precheck = self.client.get(
            "/system/discussions/execution-precheck",
            params={"trade_date": trade_date, "account_id": self.account_id},
        ).json()
        self._ensure("discussion.preflight", precheck.get("ok") is True, precheck)
        self._record(
            "discussion.preflight",
            {
                "status": precheck.get("status"),
                "approved_count": precheck.get("approved_count"),
                "blocked_count": precheck.get("blocked_count"),
                "mode": "supervised_preflight",
            },
        )

        intents = self.client.get(
            "/system/discussions/execution-intents",
            params={"trade_date": trade_date, "account_id": self.account_id},
        ).json()
        self._ensure("discussion.intents", intents.get("ok") is True, intents)
        intent_id = str((intents.get("intents") or [{}])[0].get("intent_id") or "")
        self._ensure("discussion.intents", bool(intent_id), intents)

        dispatch = self.client.post(
            "/system/discussions/execution-intents/dispatch",
            json={
                "trade_date": trade_date,
                "account_id": self.account_id,
                "intent_ids": [intent_id],
                "apply": True,
            },
        ).json()
        self._ensure(
            "discussion.dispatch",
            dispatch.get("ok") is True and dispatch.get("status") == "queued_for_gateway",
            dispatch,
        )
        self._record(
            "discussion.dispatch",
            {
                "status": dispatch.get("status"),
                "queued_count": dispatch.get("queued_count"),
                "gateway_pull_path": dispatch.get("gateway_pull_path"),
                "intent_id": intent_id,
            },
        )

        worker = self._build_worker()
        try:
            worker_payload = worker.run_once()
        finally:
            worker.close()
        self._ensure("discussion.worker", worker_payload.get("ok") is True, worker_payload)
        self._record(
            "discussion.worker",
            {
                "polled_count": worker_payload.get("polled_count"),
                "claimed_count": worker_payload.get("claimed_count"),
                "stored_count": worker_payload.get("stored_count"),
                "phase": ((worker_payload.get("results") or [{}])[0]).get("phase"),
                "result": ((worker_payload.get("results") or [{}])[0]),
            },
        )

        latest_receipt = self.client.get("/system/execution/gateway/receipts/latest").json()
        self._ensure(
            "discussion.receipt",
            latest_receipt.get("ok") is True
            and latest_receipt.get("available") is True
            and (latest_receipt.get("receipt") or {}).get("intent_id") == intent_id,
            latest_receipt,
        )
        self._record(
            "discussion.receipt",
            {
                "receipt_id": latest_receipt["receipt"].get("receipt_id"),
                "intent_id": latest_receipt["receipt"].get("intent_id"),
                "status": latest_receipt["receipt"].get("status"),
                "summary_lines": latest_receipt["receipt"].get("summary_lines", []),
            },
        )

        latest_dispatch = self.client.get(
            "/system/discussions/execution-dispatch/latest",
            params={"trade_date": trade_date},
        ).json()
        self._ensure("discussion.latest", latest_dispatch.get("ok") is True, latest_dispatch)
        intent_detail = self.client.get(f"/system/execution/gateway/intents/{intent_id}").json()
        self._ensure(
            "discussion.latest",
            intent_detail.get("available") is True and intent_detail["intent"].get("status") == "submitted",
            intent_detail,
        )
        self._record(
            "discussion.latest",
            {
                "dispatch_status": latest_dispatch.get("status"),
                "intent_status": intent_detail["intent"].get("status"),
                "intent_id": intent_id,
            },
        )
        return {
            "trade_date": trade_date,
            "intent_id": intent_id,
            "dispatch_status": dispatch.get("status"),
            "receipt_status": latest_receipt["receipt"].get("status"),
            "latest_intent_status": intent_detail["intent"].get("status"),
        }

    def _run_tail_market_chain(self) -> dict:
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
            "sector_profiles": [{"sector_name": "白酒", "life_cycle": "retreat", "strength_score": 0.8}],
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
        adapter.positions[self.account_id] = [
            PositionSnapshot(
                account_id=self.account_id,
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
                    "account_id": self.account_id,
                    "order_id": "order-buy-1",
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "decision_id": "case-600519",
                    "submitted_at": "2026-04-06T09:35:00",
                    "playbook": "leader_chase",
                    "regime": "trend",
                    "request": {"side": "BUY", "playbook": "leader_chase", "regime": "trend"},
                    "latest_status": "FILLED",
                }
            ],
        )

        tail_market = self.client.post("/system/tail-market/run", params={"account_id": self.account_id}).json()
        self._ensure(
            "tail_market.queue",
            tail_market.get("ok") is True and tail_market.get("status") == "queued_for_gateway",
            tail_market,
        )
        queued = next(item for item in tail_market["items"] if item["status"] == "queued_for_gateway")
        intent_id = queued["gateway_intent"]["intent_id"]
        self._record(
            "tail_market.queue",
            {
                "status": tail_market.get("status"),
                "queued_count": tail_market.get("queued_count"),
                "intent_id": intent_id,
                "gateway_pull_path": queued.get("gateway_pull_path"),
            },
        )

        worker = self._build_worker()
        try:
            worker_payload = worker.run_once()
        finally:
            worker.close()
        self._ensure("tail_market.worker", worker_payload.get("ok") is True, worker_payload)
        self._record(
            "tail_market.worker",
            {
                "polled_count": worker_payload.get("polled_count"),
                "claimed_count": worker_payload.get("claimed_count"),
                "stored_count": worker_payload.get("stored_count"),
                "phase": ((worker_payload.get("results") or [{}])[0]).get("phase"),
                "result": ((worker_payload.get("results") or [{}])[0]),
            },
        )

        latest_receipt = self.client.get("/system/execution/gateway/receipts/latest").json()
        self._ensure(
            "tail_market.receipt",
            latest_receipt.get("ok") is True
            and latest_receipt.get("available") is True
            and (latest_receipt.get("receipt") or {}).get("intent_id") == intent_id,
            latest_receipt,
        )
        intent_detail = self.client.get(f"/system/execution/gateway/intents/{intent_id}").json()
        self._ensure(
            "tail_market.receipt",
            intent_detail.get("available") is True and intent_detail["intent"].get("status") == "submitted",
            intent_detail,
        )
        self._record(
            "tail_market.receipt",
            {
                "receipt_id": latest_receipt["receipt"].get("receipt_id"),
                "intent_id": intent_id,
                "receipt_status": latest_receipt["receipt"].get("status"),
                "intent_status": intent_detail["intent"].get("status"),
            },
        )
        return {
            "intent_id": intent_id,
            "queue_status": tail_market.get("status"),
            "receipt_status": latest_receipt["receipt"].get("status"),
            "intent_status": intent_detail["intent"].get("status"),
        }

    def _prepare_finalized_discussion(self) -> tuple[str, dict]:
        self.client.post(
            "/runtime/jobs/pipeline",
            json={
                "universe_scope": "main-board",
                "max_candidates": self.max_candidates,
                "auto_trade": False,
                "account_id": self.account_id,
            },
        )
        cases = self.client.get("/system/cases").json()["items"]
        self._ensure("discussion.prepare", bool(cases), {"reason": "no_cases"})
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
        self.client.post("/system/discussions/opinions/batch", json={"auto_rebuild": True, "items": round_2_items})
        self.client.post(f"/system/discussions/cycles/{trade_date}/refresh")
        finalize = self.client.post(f"/system/discussions/cycles/{trade_date}/finalize")
        finalize_payload = finalize.json()
        self._ensure(
            "discussion.finalize",
            finalize.status_code == 200
            and finalize_payload.get("ok") is True
            and (finalize_payload.get("cycle") or {}).get("discussion_state") == "final_selection_ready",
            finalize_payload,
        )
        return trade_date, finalize_payload


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execution Gateway 端到端烟雾测试")
    parser.add_argument("--max-candidates", type=int, default=4)
    parser.add_argument("--account-id", default="sim-001")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    runner = GatewaySmokeRunner(max_candidates=args.max_candidates, account_id=args.account_id)
    try:
        payload = runner.run()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except SmokeFailure as exc:
        error_payload = {
            "ok": False,
            "failed_stage": exc.stage,
            "detail": exc.detail,
            "completed_stages": [item.__dict__ for item in runner.results],
        }
        print(json.dumps(error_payload, ensure_ascii=False, indent=2))
        return 1
    finally:
        runner.close()


if __name__ == "__main__":
    raise SystemExit(main())
