import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from ashare_system.app import create_app
from ashare_system.container import (
    get_execution_gateway_state_store,
    reset_container,
)
from ashare_system.execution_gateway import (
    enqueue_execution_gateway_intent,
    get_pending_execution_intents,
    retry_stale_claimed_intents,
)


class ExecutionBridgeConcurrentTests(unittest.TestCase):
    def test_gateway_claims_multiple_intents_without_duplicate_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                gateway_store = get_execution_gateway_state_store()
                for index in range(5):
                    enqueue_execution_gateway_intent(
                        gateway_store,
                        {
                            "intent_id": f"intent-concurrent-{index}",
                            "trade_date": "2026-04-20",
                            "account_id": "sim-001",
                            "symbol": f"60000{index}.SH",
                            "side": "BUY",
                            "quantity": 100,
                            "price": 10.0,
                            "request": {
                                "request_id": f"req-{index}",
                                "account_id": "sim-001",
                                "symbol": f"60000{index}.SH",
                                "side": "BUY",
                                "quantity": 100,
                                "price": 10.0,
                            },
                        },
                        run_mode="paper",
                        approval_source="unit_test",
                    )

                with TestClient(create_app()) as client:
                    for index in range(5):
                        payload = client.post(
                            "/system/execution/gateway/intents/claim",
                            json={
                                "intent_id": f"intent-concurrent-{index}",
                                "gateway_source_id": "windows-vm-a",
                                "deployment_role": "worker",
                                "bridge_path": "linux->windows->qmt",
                                "claimed_at": "2026-04-20T14:30:00+08:00",
                            },
                        ).json()
                        self.assertTrue(payload["ok"])
                        self.assertEqual(payload["claim_status"], "claimed")

                    conflict = client.post(
                        "/system/execution/gateway/intents/claim",
                        json={
                            "intent_id": "intent-concurrent-0",
                            "gateway_source_id": "windows-vm-b",
                            "deployment_role": "worker",
                            "bridge_path": "linux->windows->qmt",
                            "claimed_at": "2026-04-20T14:31:00+08:00",
                        },
                    ).json()
                    self.assertFalse(conflict["ok"])
                    self.assertEqual(conflict["claim_status"], "conflict")

                pending = get_pending_execution_intents(gateway_store)
                self.assertEqual(len(pending), 5)
                self.assertTrue(all(str(item.get("status") or "") == "claimed" for item in pending))
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()

    def test_gateway_receipt_rejected_and_stale_claim_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous = {
                key: os.environ.get(key)
                for key in [
                    "ASHARE_STORAGE_ROOT",
                    "ASHARE_LOGS_DIR",
                    "ASHARE_EXECUTION_MODE",
                    "ASHARE_MARKET_MODE",
                    "ASHARE_RUN_MODE",
                    "ASHARE_EXECUTION_PLANE",
                    "ASHARE_ACCOUNT_ID",
                ]
            }
            os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
            os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
            os.environ["ASHARE_EXECUTION_MODE"] = "mock"
            os.environ["ASHARE_MARKET_MODE"] = "mock"
            os.environ["ASHARE_RUN_MODE"] = "paper"
            os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
            os.environ["ASHARE_ACCOUNT_ID"] = "sim-001"
            reset_container()

            try:
                gateway_store = get_execution_gateway_state_store()
                enqueue_execution_gateway_intent(
                    gateway_store,
                    {
                        "intent_id": "intent-reject-001",
                        "trade_date": "2026-04-20",
                        "account_id": "sim-001",
                        "symbol": "000001.SZ",
                        "side": "BUY",
                        "quantity": 100,
                        "price": 10.0,
                        "request": {
                            "request_id": "req-reject-001",
                            "account_id": "sim-001",
                            "symbol": "000001.SZ",
                            "side": "BUY",
                            "quantity": 100,
                            "price": 10.0,
                        },
                    },
                    run_mode="paper",
                    approval_source="unit_test",
                )

                with TestClient(create_app()) as client:
                    claim_payload = client.post(
                        "/system/execution/gateway/intents/claim",
                        json={
                            "intent_id": "intent-reject-001",
                            "gateway_source_id": "windows-vm-a",
                            "deployment_role": "worker",
                            "bridge_path": "linux->windows->qmt",
                            "claimed_at": "2026-04-20T14:35:00+08:00",
                        },
                    ).json()
                    self.assertTrue(claim_payload["ok"])

                    receipt_payload = client.post(
                        "/system/execution/gateway/receipts",
                        json={
                            "receipt_id": "receipt-reject-001",
                            "intent_id": "intent-reject-001",
                            "gateway_source_id": "windows-vm-a",
                            "deployment_role": "worker",
                            "bridge_path": "linux->windows->qmt",
                            "reported_at": "2026-04-20T14:35:20+08:00",
                            "submitted_at": "2026-04-20T14:35:18+08:00",
                            "status": "rejected",
                            "broker_order_id": "1082234806",
                            "error_code": "57",
                            "error_message": "broker rejected",
                        },
                    ).json()
                    self.assertTrue(receipt_payload["ok"])
                    self.assertEqual(receipt_payload["latest_receipt"]["status"], "rejected")

                enqueue_execution_gateway_intent(
                    gateway_store,
                    {
                        "intent_id": "intent-stale-001",
                        "trade_date": "2026-04-20",
                        "account_id": "sim-001",
                        "symbol": "000002.SZ",
                        "side": "BUY",
                        "quantity": 100,
                        "price": 10.0,
                        "request": {
                            "request_id": "req-stale-001",
                            "account_id": "sim-001",
                            "symbol": "000002.SZ",
                            "side": "BUY",
                            "quantity": 100,
                            "price": 10.0,
                        },
                        "status": "claimed",
                        "claim": {
                            "gateway_source_id": "windows-vm-a",
                            "claimed_at": "2026-04-20T14:00:00+08:00",
                        },
                    },
                    run_mode="paper",
                    approval_source="unit_test",
                )
                retry_payload = retry_stale_claimed_intents(
                    gateway_store,
                    now=datetime.fromisoformat("2026-04-20T14:10:30+08:00"),
                    stale_after_seconds=300,
                )
                self.assertEqual(retry_payload["retry_count"], 1)
                pending = get_pending_execution_intents(gateway_store)
                stale_item = next(item for item in pending if item["intent_id"] == "intent-stale-001")
                self.assertEqual(stale_item["status"], "approved")
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                reset_container()
