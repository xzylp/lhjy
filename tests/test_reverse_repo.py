from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from ashare_system.app import create_app
from ashare_system.container import (
    get_execution_adapter,
    get_market_adapter,
    get_message_dispatcher,
    get_runtime_config_manager,
    get_runtime_state_store,
    get_settings,
    reset_container,
)
from ashare_system.contracts import BalanceSnapshot, QuoteSnapshot
from ashare_system.reverse_repo import ReverseRepoService


class ReverseRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        os.environ["ASHARE_STORAGE_ROOT"] = str(root / "state")
        os.environ["ASHARE_LOGS_DIR"] = str(root / "logs")
        os.environ["ASHARE_WORKSPACE"] = str(root)
        os.environ["ASHARE_MARKET_MODE"] = "mock"
        os.environ["ASHARE_EXECUTION_MODE"] = "mock"
        os.environ["ASHARE_RUN_MODE"] = "paper"
        os.environ["ASHARE_LIVE_ENABLE"] = "false"
        reset_container()
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        reset_container()
        self._tmpdir.cleanup()

    def test_reverse_repo_plan_prefers_four_day_shenzhen_when_gap_below_sh_minimum(self) -> None:
        adapter = get_execution_adapter()
        adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=80000.0)
        adapter.positions["sim-001"] = []
        market = get_market_adapter()
        market.get_index_quotes = lambda symbols: [
            QuoteSnapshot(symbol="204004.SH", name="沪市4天逆回购", last_price=2.3, bid_price=2.29, ask_price=2.31, volume=100000),
            QuoteSnapshot(symbol="131809.SZ", name="深市4天逆回购", last_price=2.1, bid_price=2.09, ask_price=2.11, volume=100000),
            QuoteSnapshot(symbol="204003.SH", name="沪市3天逆回购", last_price=2.2, bid_price=2.19, ask_price=2.21, volume=100000),
            QuoteSnapshot(symbol="131800.SZ", name="深市3天逆回购", last_price=2.0, bid_price=1.99, ask_price=2.01, volume=100000),
        ]

        response = self.client.post("/system/reverse-repo/check", params={"account_id": "sim-001"})
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["selected_candidate"]["symbol"], "131809.SZ")
        self.assertEqual(payload["selected_candidate"]["days"], 4)
        self.assertEqual(payload["selected_candidate"]["planned_amount"], 70000.0)
        self.assertEqual(payload["selected_candidate"]["order_volume"], 70)

    def test_reverse_repo_live_submit_places_sell_order_without_existing_position(self) -> None:
        os.environ["ASHARE_RUN_MODE"] = "live"
        os.environ["ASHARE_LIVE_ENABLE"] = "true"
        reset_container()
        self.client = TestClient(create_app())
        adapter = get_execution_adapter()
        adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=80000.0)
        adapter.positions["sim-001"] = []
        market = get_market_adapter()
        market.get_index_quotes = lambda symbols: [
            QuoteSnapshot(symbol="131809.SZ", name="深市4天逆回购", last_price=2.0, bid_price=1.99, ask_price=2.01, volume=100000),
            QuoteSnapshot(symbol="131800.SZ", name="深市3天逆回购", last_price=1.8, bid_price=1.79, ask_price=1.81, volume=100000),
        ]
        service = ReverseRepoService(
            settings=get_settings(),
            execution_adapter=adapter,
            market_adapter=market,
            state_store=get_runtime_state_store(),
            config_mgr=get_runtime_config_manager(),
            dispatcher=get_message_dispatcher(),
            now_factory=lambda: datetime(2026, 4, 6, 10, 0, 0),
        )
        payload = service.inspect("sim-001", auto_submit=True, persist=False)
        self.assertEqual(payload["status"], "submitted")
        self.assertEqual(payload["submitted_order"]["symbol"], "131809.SZ")
        self.assertEqual(payload["submitted_order"]["side"], "SELL")
        self.assertEqual(payload["submitted_order"]["quantity"], 70)

    def test_reverse_repo_check_returns_no_gap_when_reserved_amount_is_already_met(self) -> None:
        adapter = get_execution_adapter()
        adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=100000.0, cash=5000.0)
        adapter.positions["sim-001"] = []
        proposal = self.client.post(
            "/system/params/proposals",
            json={
                "param_key": "reverse_repo_reserved_amount",
                "new_value": 0.0,
                "effective_period": "until_revoked",
                "status": "approved",
                "proposed_by": "test",
                "structured_by": "ashare",
                "approved_by": "ashare-audit",
                "reason": "disable repo reserve for test",
            },
        )
        self.assertEqual(proposal.status_code, 200)
        response = self.client.post("/system/reverse-repo/check", params={"account_id": "sim-001"})
        payload = response.json()
        self.assertEqual(payload["status"], "no_gap")


if __name__ == "__main__":
    unittest.main()
