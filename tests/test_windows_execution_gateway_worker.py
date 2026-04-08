from __future__ import annotations

from datetime import datetime

import httpx
from unittest.mock import patch

from ashare_system.contracts import OrderSnapshot, PlaceOrderRequest
from ashare_system.infra.adapters import ExecutionAdapter
from ashare_system.windows_execution_gateway_worker import (
    ExecutionGatewayWorker,
    GatewayWorkerConfig,
    _build_submitter,
    build_adapter_submitter,
)


def _build_worker(
    handler,
    *,
    submitter=None,
    now_factory=None,
) -> ExecutionGatewayWorker:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://control-plane")
    return ExecutionGatewayWorker(
        GatewayWorkerConfig(
            control_plane_base_url="http://control-plane",
            source_id="windows-vm-a",
            deployment_role="primary_gateway",
            bridge_path="linux_openclaw -> windows_gateway -> qmt_vm",
        ),
        submitter=submitter,
        client=client,
        now_factory=now_factory,
    )


def test_worker_run_once_claims_submits_and_posts_receipt() -> None:
    calls: list[tuple[str, str, dict | None]] = []
    fixed_now = lambda: datetime(2026, 4, 8, 15, 1, 2)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = None
        if request.content:
            payload = __import__("json").loads(request.content.decode("utf-8"))
        calls.append((request.method, request.url.path, payload))
        if request.method == "GET" and request.url.path == "/system/execution/gateway/intents/pending":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "items": [
                        {
                            "intent_scope": "execution_intent_packet",
                            "intent_id": "intent-1",
                            "intent_version": "v1",
                            "generated_at": "2026-04-08T15:00:00",
                            "trade_date": "2026-04-08",
                            "account_id": "sim-001",
                            "symbol": "600519.SH",
                            "side": "BUY",
                            "quantity": 100,
                            "price": 1688.0,
                            "run_mode": "paper",
                            "execution_plane": "windows_gateway",
                            "approval_source": "discussion_execution_dispatch",
                            "approved_by": "linux_control_plane",
                            "approved_at": "2026-04-08T15:00:00",
                            "idempotency_key": "ik-1",
                            "status": "approved",
                            "request": {
                                "account_id": "sim-001",
                                "symbol": "600519.SH",
                                "side": "BUY",
                                "quantity": 100,
                                "price": 1688.0,
                                "request_id": "req-1",
                            },
                            "strategy_context": {},
                            "risk_context": {},
                            "discussion_context": {},
                            "claim": {},
                            "summary_lines": [],
                        }
                    ],
                },
            )
        if request.method == "POST" and request.url.path == "/system/execution/gateway/intents/claim":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "claim_status": "claimed",
                    "intent": {
                        "intent_scope": "execution_intent_packet",
                        "intent_id": "intent-1",
                        "intent_version": "v1",
                        "generated_at": "2026-04-08T15:00:00",
                        "trade_date": "2026-04-08",
                        "account_id": "sim-001",
                        "symbol": "600519.SH",
                        "side": "BUY",
                        "quantity": 100,
                        "price": 1688.0,
                        "run_mode": "paper",
                        "execution_plane": "windows_gateway",
                        "approval_source": "discussion_execution_dispatch",
                        "approved_by": "linux_control_plane",
                        "approved_at": "2026-04-08T15:00:00",
                        "idempotency_key": "ik-1",
                        "status": "claimed",
                        "request": {
                            "account_id": "sim-001",
                            "symbol": "600519.SH",
                            "side": "BUY",
                            "quantity": 100,
                            "price": 1688.0,
                            "request_id": "req-1",
                        },
                        "strategy_context": {},
                        "risk_context": {},
                        "discussion_context": {},
                        "claim": {
                            "gateway_source_id": "windows-vm-a",
                            "deployment_role": "primary_gateway",
                            "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                            "claimed_at": "2026-04-08T15:01:02",
                        },
                        "summary_lines": [],
                    },
                    "reason": "",
                },
            )
        if request.method == "POST" and request.url.path == "/system/execution/gateway/receipts":
            assert payload is not None
            assert payload["intent_id"] == "intent-1"
            assert payload["status"] == "submitted"
            assert payload["gateway_source_id"] == "windows-vm-a"
            assert payload["deployment_role"] == "primary_gateway"
            assert payload["bridge_path"] == "linux_openclaw -> windows_gateway -> qmt_vm"
            assert payload["broker_order_id"] == "broker-1"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "stored": True,
                    "reason": "",
                    "latest_receipt": payload,
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def submitter(intent: dict) -> dict:
        assert intent["intent_id"] == "intent-1"
        return {
            "status": "submitted",
            "broker_order_id": "broker-1",
            "broker_session_id": "session-1",
            "order": {
                "order_id": "broker-1",
                "account_id": "sim-001",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "price": 1688.0,
                "status": "PENDING",
            },
            "summary_lines": ["Windows Gateway 已提交订单。"],
        }

    worker = _build_worker(handler, submitter=submitter, now_factory=fixed_now)
    try:
        payload = worker.run_once()
    finally:
        worker.close()

    assert payload["ok"] is True
    assert payload["polled_count"] == 1
    assert payload["claimed_count"] == 1
    assert payload["stored_count"] == 1
    assert payload["failed_count"] == 0
    assert [item[1] for item in calls] == [
        "/system/execution/gateway/intents/pending",
        "/system/execution/gateway/intents/claim",
        "/system/execution/gateway/receipts",
    ]


def test_worker_posts_failed_receipt_when_submitter_raises() -> None:
    fixed_now = lambda: datetime(2026, 4, 8, 15, 2, 3)
    receipt_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "items": [
                        {
                            "intent_scope": "execution_intent_packet",
                            "intent_id": "intent-2",
                            "intent_version": "v1",
                            "generated_at": "2026-04-08T15:00:00",
                            "trade_date": "2026-04-08",
                            "account_id": "sim-001",
                            "symbol": "000001.SZ",
                            "side": "SELL",
                            "quantity": 100,
                            "price": 12.3,
                            "run_mode": "paper",
                            "execution_plane": "windows_gateway",
                            "approval_source": "tail_market_scan",
                            "approved_by": "linux_control_plane",
                            "approved_at": "2026-04-08T15:00:00",
                            "idempotency_key": "ik-2",
                            "status": "approved",
                            "request": {
                                "account_id": "sim-001",
                                "symbol": "000001.SZ",
                                "side": "SELL",
                                "quantity": 100,
                                "price": 12.3,
                                "request_id": "req-2",
                            },
                            "strategy_context": {},
                            "risk_context": {},
                            "discussion_context": {},
                            "claim": {},
                            "summary_lines": [],
                        }
                    ],
                },
            )
        if request.method == "POST" and request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"ok": True, "claim_status": "claimed", "intent": {
                "intent_scope": "execution_intent_packet",
                "intent_id": "intent-2",
                "intent_version": "v1",
                "generated_at": "2026-04-08T15:00:00",
                "trade_date": "2026-04-08",
                "account_id": "sim-001",
                "symbol": "000001.SZ",
                "side": "SELL",
                "quantity": 100,
                "price": 12.3,
                "run_mode": "paper",
                "execution_plane": "windows_gateway",
                "approval_source": "tail_market_scan",
                "approved_by": "linux_control_plane",
                "approved_at": "2026-04-08T15:00:00",
                "idempotency_key": "ik-2",
                "status": "claimed",
                "request": {
                    "account_id": "sim-001",
                    "symbol": "000001.SZ",
                    "side": "SELL",
                    "quantity": 100,
                    "price": 12.3,
                    "request_id": "req-2",
                },
                "strategy_context": {},
                "risk_context": {},
                "discussion_context": {},
                "claim": {},
                "summary_lines": [],
            }, "reason": ""})
        if request.method == "POST" and request.url.path.endswith("/receipts"):
            payload = __import__("json").loads(request.content.decode("utf-8"))
            receipt_payloads.append(payload)
            return httpx.Response(200, json={"ok": True, "stored": True, "reason": "", "latest_receipt": payload})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def submitter(_intent: dict) -> dict:
        raise RuntimeError("qmt disconnected")

    worker = _build_worker(handler, submitter=submitter, now_factory=fixed_now)
    try:
        payload = worker.run_once()
    finally:
        worker.close()

    assert payload["ok"] is True
    assert payload["stored_count"] == 1
    assert payload["failed_count"] == 0
    assert payload["results"][0]["phase"] == "submit"
    assert payload["results"][0]["submit_ok"] is False
    assert "qmt disconnected" in payload["results"][0]["submit_error"]
    assert receipt_payloads[0]["status"] == "failed"
    assert receipt_payloads[0]["error_code"] == "gateway_submit_failed"
    assert "qmt disconnected" in receipt_payloads[0]["error_message"]


def test_worker_run_once_reports_poll_phase_when_pending_fetch_fails() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"ok": False, "error": "control_plane_unavailable"})

    worker = _build_worker(handler)
    try:
        payload = worker.run_once()
    finally:
        worker.close()

    assert payload["ok"] is False
    assert payload["polled_count"] == 0
    assert payload["failed_count"] == 1
    assert payload["results"][0]["phase"] == "poll"
    assert "poll pending intents failed" in payload["results"][0]["reason"]


def test_build_submitter_noop_success_returns_protocol_smoke_payload() -> None:
    submitter = _build_submitter("noop_success")

    payload = submitter(
        {
            "intent_id": "intent-noop-1",
            "account_id": "sim-001",
            "symbol": "600519.SH",
            "side": "BUY",
            "quantity": 100,
            "price": 1688.0,
            "request": {
                "account_id": "sim-001",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "price": 1688.0,
                "request_id": "req-noop-1",
            },
        }
    )

    assert payload["status"] == "submitted"
    assert payload["broker_order_id"] == "req-noop-1"
    assert payload["order"]["order_id"] == "req-noop-1"
    assert payload["summary_lines"] == ["noop_success 模式：未接真实 QMT，仅完成协议闭环演练。"]


class _FakeExecutionAdapter(ExecutionAdapter):
    def __init__(self) -> None:
        self.mode = "xtquant"
        self.requests: list[PlaceOrderRequest] = []

    def get_balance(self, account_id: str):
        raise NotImplementedError

    def get_positions(self, account_id: str):
        raise NotImplementedError

    def get_orders(self, account_id: str):
        raise NotImplementedError

    def get_trades(self, account_id: str):
        raise NotImplementedError

    def place_order(self, request: PlaceOrderRequest) -> OrderSnapshot:
        self.requests.append(request)
        return OrderSnapshot(
            order_id="xt-ord-1",
            account_id=request.account_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=request.price,
            status="PENDING",
        )

    def cancel_order(self, account_id: str, order_id: str) -> OrderSnapshot:
        raise NotImplementedError


def test_build_adapter_submitter_maps_order_snapshot_to_receipt_payload() -> None:
    adapter = _FakeExecutionAdapter()
    submitter = build_adapter_submitter(adapter)

    payload = submitter(
        {
            "intent_id": "intent-3",
            "request": {
                "account_id": "sim-001",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "price": 1688.0,
                "request_id": "req-3",
            },
        }
    )

    assert len(adapter.requests) == 1
    assert payload["status"] == "submitted"
    assert payload["broker_order_id"] == "xt-ord-1"
    assert payload["order"]["order_id"] == "xt-ord-1"
    assert payload["raw_payload"]["adapter_mode"] == "xtquant"
    assert payload["raw_payload"]["request_id"] == "req-3"


def test_build_submitter_xtquant_uses_provided_adapter() -> None:
    adapter = _FakeExecutionAdapter()
    submitter = _build_submitter("xtquant", adapter=adapter)

    payload = submitter(
        {
            "intent_id": "intent-4",
            "request": {
                "account_id": "sim-001",
                "symbol": "000001.SZ",
                "side": "SELL",
                "quantity": 200,
                "price": 12.3,
                "request_id": "req-4",
            },
        }
    )

    assert len(adapter.requests) == 1
    assert adapter.requests[0].symbol == "000001.SZ"
    assert payload["broker_order_id"] == "xt-ord-1"


def test_build_submitter_xtquant_builds_real_adapter_when_not_injected() -> None:
    adapter = _FakeExecutionAdapter()
    with patch("ashare_system.windows_execution_gateway_worker.load_settings", return_value=object()):
        with patch("ashare_system.windows_execution_gateway_worker.XtQuantExecutionAdapter", return_value=adapter):
            submitter = _build_submitter("xtquant")

    payload = submitter(
        {
            "intent_id": "intent-5",
            "request": {
                "account_id": "sim-001",
                "symbol": "600519.SH",
                "side": "BUY",
                "quantity": 100,
                "price": 1688.0,
                "request_id": "req-5",
            },
        }
    )

    assert len(adapter.requests) == 1
    assert payload["raw_payload"]["adapter_mode"] == "xtquant"
