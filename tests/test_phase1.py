"""Phase 1 基座层基础测试"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ashare_system.settings import load_settings, AppSettings
from ashare_system.contracts import BalanceSnapshot, BarSnapshot, QuoteSnapshot, DataQuality, MarketProfile, PlaceOrderRequest, CancelOrderRequest, OrderSnapshot, PositionSnapshot, TradeSnapshot
from ashare_system.container import get_settings, get_execution_adapter, get_market_adapter, reset_container
from ashare_system.infra.filters import filter_a_share, filter_main_board, is_a_share_symbol
from ashare_system.infra.adapters import MockExecutionAdapter, XtQuantExecutionAdapter, build_execution_adapter
from ashare_system.infra.healthcheck import EnvironmentHealthcheck
from ashare_system.infra.market_adapter import MockMarketDataAdapter, build_market_adapter
from ashare_system.infra.audit_store import StateStore
from ashare_system.data.archive import DataArchiveStore
from ashare_system.data.fetcher import DataFetcher
from ashare_system.data.cleaner import DataCleaner
from ashare_system.data.validator import DataValidator
from ashare_system.discussion.candidate_case import CandidateCaseService
from ashare_system.execution_safety import (
    board_limit_pct,
    is_limit_down,
    is_limit_up,
    is_price_deviation_exceeded,
    is_snapshot_fresh,
    is_trading_session,
    snapshot_age_seconds,
)
from ashare_system.execution_reconciliation import ExecutionReconciliationService
from ashare_system.pending_order_remediation import PendingOrderRemediationService
from ashare_system.precompute import DossierPrecomputeService
from ashare_system.pending_order_inspection import PendingOrderInspectionService
from ashare_system.account_state import AccountStateService
from ashare_system.runtime_config import RuntimeConfigManager
from ashare_system.scheduler import Scheduler, build_postclose_review_board_summary, run_tail_market_scan
from ashare_system.run import _start_qmt_if_needed
from ashare_system.apps.execution_api import build_router as build_execution_router
from ashare_system.startup_recovery import StartupRecoveryService


class DummyTradeDispatcher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def dispatch_trade(self, title: str, content: str, level: str = "info", force: bool = True) -> bool:
        self.calls.append(
            {
                "channel": "trade",
                "title": title,
                "content": content,
                "level": level,
                "force": force,
            }
        )
        return True


class TestSettings:
    def test_load_settings(self):
        s = load_settings()
        assert isinstance(s, AppSettings)
        assert s.app_name == "ashare-system-v2"
        assert s.run_mode in ("dry-run", "paper", "live")

    def test_xtquant_settings(self):
        s = load_settings()
        assert s.xtquant.account_id


class TestContracts:
    def test_balance_snapshot(self):
        b = BalanceSnapshot(account_id="test", total_asset=100000, cash=50000)
        assert b.frozen_cash == 0.0

    def test_data_quality(self):
        q = DataQuality(source="real")
        assert q.completeness == 1.0

    def test_market_profile(self):
        p = MarketProfile(sentiment_phase="回暖")
        assert p.position_ceiling == 0.6


class TestFilters:
    def test_a_share_symbol(self):
        assert is_a_share_symbol("600519.SH")
        assert is_a_share_symbol("000001.SZ")
        assert not is_a_share_symbol("AAPL")
        assert not is_a_share_symbol("invalid")

    def test_filter_a_share(self):
        result = filter_a_share(["600519.SH", "INVALID", "000001.SZ", "600519.SH"])
        assert result == ["600519.SH", "000001.SZ"]

    def test_filter_main_board(self):
        result = filter_main_board(["600519.SH", "300750.SZ", "688981.SH"])
        assert result == ["600519.SH"]


class TestAdapters:
    def test_mock_execution(self):
        adapter = MockExecutionAdapter()
        balance = adapter.get_balance("sim-001")
        assert balance.total_asset == 1_000_000
        positions = adapter.get_positions("sim-001")
        assert len(positions) == 2

    def test_mock_market(self):
        adapter = MockMarketDataAdapter()
        snaps = adapter.get_snapshots(["600519.SH"])
        assert len(snaps) == 1
        assert snaps[0].symbol == "600519.SH"

    def test_mock_get_order_and_cancel_confirm(self):
        adapter = MockExecutionAdapter()
        order = adapter.place_order(
            PlaceOrderRequest(
                account_id="sim-001",
                symbol="000001.SZ",
                side="BUY",
                quantity=100,
                price=10.0,
                request_id="req-mock-1",
            )
        )
        fetched = adapter.get_order("sim-001", order.order_id)
        assert fetched.order_id == order.order_id
        assert fetched.status == "PENDING"

        cancelled = adapter.cancel_order("sim-001", order.order_id)
        assert cancelled.status == "CANCELLED"
        fetched_after = adapter.get_order("sim-001", order.order_id)
        assert fetched_after.status == "CANCELLED"

    def test_live_execution_adapter_init_failure_raises(self):
        settings = load_settings()
        settings.run_mode = "live"
        with patch("ashare_system.infra.adapters.XtQuantExecutionAdapter", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="live 模式禁止回退到 mock-fallback"):
                build_execution_adapter("xtquant", settings)

    def test_live_market_adapter_init_failure_raises(self):
        settings = load_settings()
        settings.run_mode = "live"
        with patch("ashare_system.infra.market_adapter.XtQuantMarketDataAdapter", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="live 模式禁止行情适配器回退到 mock-fallback"):
                build_market_adapter("xtquant", settings)

    def test_live_healthcheck_requires_live_enable_and_real_adapters(self):
        settings = load_settings()
        settings.run_mode = "live"
        settings.live_trade_enabled = False
        settings.execution_mode = "xtquant"
        settings.market_mode = "xtquant"
        healthcheck = EnvironmentHealthcheck(settings)
        with patch("ashare_system.infra.healthcheck.build_execution_adapter", return_value=SimpleNamespace(mode="xtquant")):
            with patch("ashare_system.infra.healthcheck.build_market_adapter", return_value=SimpleNamespace(mode="xtquant")):
                result = healthcheck.run()
        assert result.ok is False
        checks = {item["name"]: item for item in result.checks}
        assert checks["live_trade_enabled"]["status"] == "invalid"

    def test_xtquant_order_failure_does_not_consume_request_id(self, tmp_path):
        settings = load_settings()
        settings.storage_root = tmp_path

        class FakeTrader:
            def __init__(self) -> None:
                self.calls = 0

            def order_stock(self, *args):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("broker down")
                return 123456

        adapter = XtQuantExecutionAdapter.__new__(XtQuantExecutionAdapter)
        adapter.settings = settings
        adapter.modules = {"xtconstant": SimpleNamespace(STOCK_BUY=1, STOCK_SELL=2, FIX_PRICE=11)}
        adapter._trader = None
        adapter._account = None
        adapter._request_store = tmp_path / "dispatched_requests.json"
        adapter._dispatched = set()
        trader = FakeTrader()
        adapter._ensure_trader = lambda: (trader, object())
        adapter.get_balance = lambda account_id: BalanceSnapshot(account_id=account_id, total_asset=100000, cash=100000)
        adapter.get_positions = lambda account_id: []

        request = PlaceOrderRequest(
            account_id="sim-001",
            symbol="000001.SZ",
            side="BUY",
            quantity=100,
            price=10.0,
            request_id="req-retry-1",
        )

        with pytest.raises(RuntimeError, match="broker down"):
            adapter.place_order(request)
        assert "req-retry-1" not in adapter._dispatched
        assert not adapter._request_store.exists()

        order = adapter.place_order(request)
        assert order.order_id == "123456"
        assert "req-retry-1" in adapter._dispatched
        assert adapter._request_store.exists()

    def test_xtquant_negative_order_id_does_not_consume_request_id(self, tmp_path):
        settings = load_settings()
        settings.storage_root = tmp_path

        adapter = XtQuantExecutionAdapter.__new__(XtQuantExecutionAdapter)
        adapter.settings = settings
        adapter.modules = {"xtconstant": SimpleNamespace(STOCK_BUY=1, STOCK_SELL=2, FIX_PRICE=11)}
        adapter._trader = None
        adapter._account = None
        adapter._request_store = tmp_path / "dispatched_requests.json"
        adapter._dispatched = set()
        trader = SimpleNamespace(order_stock=lambda *args: -7)
        adapter._ensure_trader = lambda: (trader, object())
        adapter.get_balance = lambda account_id: BalanceSnapshot(account_id=account_id, total_asset=100000, cash=100000)
        adapter.get_positions = lambda account_id: []

        request = PlaceOrderRequest(
            account_id="sim-001",
            symbol="000001.SZ",
            side="BUY",
            quantity=100,
            price=10.0,
            request_id="req-negative-1",
        )

        with pytest.raises(RuntimeError, match="下单失败: -7"):
            adapter.place_order(request)
        assert "req-negative-1" not in adapter._dispatched
        assert not adapter._request_store.exists()

    def test_xtquant_retryable_failure_retries_once_then_succeeds(self, tmp_path):
        settings = load_settings()
        settings.storage_root = tmp_path
        settings.execution_submit_retry_attempts = 2
        settings.execution_submit_retry_backoff_ms = 0

        class FakeTrader:
            def __init__(self) -> None:
                self.calls = 0

            def order_stock(self, *args):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("network timeout")
                return 654321

        adapter = XtQuantExecutionAdapter.__new__(XtQuantExecutionAdapter)
        adapter.settings = settings
        adapter.modules = {"xtconstant": SimpleNamespace(STOCK_BUY=1, STOCK_SELL=2, FIX_PRICE=11)}
        adapter._trader = None
        adapter._account = None
        adapter._request_store = tmp_path / "dispatched_requests.json"
        adapter._dispatched = set()
        trader = FakeTrader()
        adapter._ensure_trader = lambda: (trader, object())
        adapter.get_balance = lambda account_id: BalanceSnapshot(account_id=account_id, total_asset=100000, cash=100000)
        adapter.get_positions = lambda account_id: []

        request = PlaceOrderRequest(
            account_id="sim-001",
            symbol="000001.SZ",
            side="BUY",
            quantity=100,
            price=10.0,
            request_id="req-retryable-1",
        )

        order = adapter.place_order(request)
        assert order.order_id == "654321"
        assert trader.calls == 2
        assert "req-retryable-1" in adapter._dispatched

    def test_xtquant_non_retryable_failure_does_not_retry(self, tmp_path):
        settings = load_settings()
        settings.storage_root = tmp_path
        settings.execution_submit_retry_attempts = 3
        settings.execution_submit_retry_backoff_ms = 0

        class FakeTrader:
            def __init__(self) -> None:
                self.calls = 0

            def order_stock(self, *args):
                self.calls += 1
                raise RuntimeError("price out of range")

        adapter = XtQuantExecutionAdapter.__new__(XtQuantExecutionAdapter)
        adapter.settings = settings
        adapter.modules = {"xtconstant": SimpleNamespace(STOCK_BUY=1, STOCK_SELL=2, FIX_PRICE=11)}
        adapter._trader = None
        adapter._account = None
        adapter._request_store = tmp_path / "dispatched_requests.json"
        adapter._dispatched = set()
        trader = FakeTrader()
        adapter._ensure_trader = lambda: (trader, object())
        adapter.get_balance = lambda account_id: BalanceSnapshot(account_id=account_id, total_asset=100000, cash=100000)
        adapter.get_positions = lambda account_id: []

        request = PlaceOrderRequest(
            account_id="sim-001",
            symbol="000001.SZ",
            side="BUY",
            quantity=100,
            price=10.0,
            request_id="req-non-retryable-1",
        )

        with pytest.raises(RuntimeError, match="price out of range"):
            adapter.place_order(request)
        assert trader.calls == 1
        assert "req-non-retryable-1" not in adapter._dispatched

    def test_xtquant_cancel_returns_cancel_requested_when_order_not_final(self, tmp_path):
        settings = load_settings()
        settings.storage_root = tmp_path

        adapter = XtQuantExecutionAdapter.__new__(XtQuantExecutionAdapter)
        adapter.settings = settings
        adapter.modules = {"xtconstant": SimpleNamespace(STOCK_BUY=1, STOCK_SELL=2, FIX_PRICE=11)}
        adapter._trader = None
        adapter._account = None
        adapter._request_store = tmp_path / "dispatched_requests.json"
        adapter._dispatched = set()
        trader = SimpleNamespace(cancel_order_stock=lambda *args: 0)
        adapter._ensure_trader = lambda: (trader, object())
        accepted = OrderSnapshot(
            order_id="12345",
            account_id="sim-001",
            symbol="000001.SZ",
            side="BUY",
            quantity=100,
            price=10.0,
            status="ACCEPTED",
        )
        adapter.get_orders = lambda account_id: [accepted]

        cancelled = adapter.cancel_order("sim-001", "12345")
        assert cancelled.status == "CANCEL_REQUESTED"

    def test_xtquant_get_balance_raises_clear_error_when_asset_unavailable(self, tmp_path):
        settings = load_settings()
        settings.storage_root = tmp_path

        adapter = XtQuantExecutionAdapter.__new__(XtQuantExecutionAdapter)
        adapter.settings = settings
        adapter.modules = {"xtconstant": SimpleNamespace()}
        adapter._trader = None
        adapter._account = None
        adapter._request_store = tmp_path / "dispatched_requests.json"
        adapter._dispatched = set()
        trader = SimpleNamespace(query_stock_asset=lambda account: None)
        adapter._ensure_trader = lambda: (trader, object())

        with pytest.raises(RuntimeError, match="xtquant balance unavailable"):
            adapter.get_balance("sim-001")

    def test_xtquant_ensure_trader_retries_connect_before_success(self, tmp_path):
        settings = load_settings()
        settings.storage_root = tmp_path
        settings.xtquant.account_id = "8890130545"
        settings.xtquant.account_type = "STOCK"
        settings.xtquant.userdata = tmp_path / "userdata_mini"
        settings.xtquant.userdata.mkdir(parents=True, exist_ok=True)
        settings.xtquant.session_id = 8890130545

        connect_results = [-1, -1, 0]

        class FakeXtQuantTrader:
            def __init__(self, *_args, **_kwargs) -> None:
                self.started = False

            def start(self):
                self.started = True
                return None

            def connect(self):
                return connect_results.pop(0)

            def subscribe(self, _account):
                return 0

        fake_account = object()
        fake_xttype = SimpleNamespace(StockAccount=lambda account_id, account_type: fake_account)
        fake_xtconstant = SimpleNamespace()

        adapter = XtQuantExecutionAdapter.__new__(XtQuantExecutionAdapter)
        adapter.settings = settings
        adapter.modules = {
            "xttrader": SimpleNamespace(XtQuantTrader=FakeXtQuantTrader),
            "xttype": fake_xttype,
            "xtconstant": fake_xtconstant,
        }
        adapter._trader = None
        adapter._account = None
        adapter._request_store = tmp_path / "dispatched_requests.json"
        adapter._dispatched = set()

        with patch("ashare_system.infra.adapters.time.sleep", return_value=None):
            trader, account = adapter._ensure_trader()

        assert trader.started is True
        assert account is fake_account
        assert connect_results == []

    def test_execution_api_supports_get_single_order(self):
        adapter = MockExecutionAdapter()
        order = adapter.place_order(
            PlaceOrderRequest(
                account_id="sim-001",
                symbol="000001.SZ",
                side="BUY",
                quantity=100,
                price=10.0,
                request_id="req-api-1",
            )
        )
        app = FastAPI()
        app.include_router(build_execution_router(adapter, "mock"))
        client = TestClient(app)

        response = client.get(f"/execution/orders/sim-001/{order.order_id}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["order_id"] == order.order_id
        assert payload["status"] == "PENDING"

    def test_execution_api_balance_returns_503_for_runtime_failure(self):
        class FailingBalanceAdapter(MockExecutionAdapter):
            def get_balance(self, account_id: str) -> BalanceSnapshot:
                raise RuntimeError("xtquant balance unavailable for account sim-001")

        app = FastAPI()
        app.include_router(build_execution_router(FailingBalanceAdapter(), "xtquant"))
        client = TestClient(app)

        response = client.get("/execution/balance/sim-001")
        payload = response.json()
        assert response.status_code == 503
        assert "xtquant balance unavailable" in payload["detail"]

    def test_execution_api_place_order_dispatches_trade_notification(self, tmp_path):
        adapter = MockExecutionAdapter()
        dispatcher = DummyTradeDispatcher()
        state_store = StateStore(tmp_path / "meeting_state.json")
        app = FastAPI()
        app.include_router(
            build_execution_router(
                adapter,
                "mock",
                dispatcher=dispatcher,
                meeting_state_store=state_store,
            )
        )
        client = TestClient(app)

        response = client.post(
            "/execution/orders",
            json={
                "account_id": "sim-001",
                "symbol": "000001.SZ",
                "side": "BUY",
                "quantity": 100,
                "price": 10.0,
                "request_id": "req-api-notify-1",
                "decision_id": "case-1",
            },
        )
        payload = response.json()
        assert response.status_code == 200
        assert payload["status"] == "PENDING"
        assert len(dispatcher.calls) == 1
        assert dispatcher.calls[0]["channel"] == "trade"
        assert dispatcher.calls[0]["title"] == "交易下单"
        assert "000001.SZ" in dispatcher.calls[0]["content"]
        assert "case-1" in dispatcher.calls[0]["content"]
        journal = state_store.get("execution_order_journal", [])
        assert len(journal) == 1
        assert journal[0]["order_id"] == payload["order_id"]
        assert journal[0]["decision_id"] == "case-1"

    def test_execution_api_sell_order_persists_exit_metadata(self, tmp_path):
        adapter = MockExecutionAdapter()
        state_store = StateStore(tmp_path / "meeting_state.json")
        app = FastAPI()
        app.include_router(
            build_execution_router(
                adapter,
                "mock",
                meeting_state_store=state_store,
            )
        )
        client = TestClient(app)

        response = client.post(
            "/execution/orders",
            json={
                "account_id": "sim-001",
                "symbol": "600519.SH",
                "side": "SELL",
                "quantity": 100,
                "price": 1590.0,
                "request_id": "req-api-sell-1",
                "decision_id": "case-sell-1",
                "trade_date": "2026-04-06",
                "playbook": "leader_chase",
                "regime": "trend",
                "exit_reason": "sector_retreat",
            },
        )
        payload = response.json()
        assert response.status_code == 200
        assert payload["status"] == "PENDING"
        journal = state_store.get("execution_order_journal", [])
        assert len(journal) == 1
        assert journal[0]["order_id"] == payload["order_id"]
        assert journal[0]["trade_date"] == "2026-04-06"
        assert journal[0]["playbook"] == "leader_chase"
        assert journal[0]["regime"] == "trend"
        assert journal[0]["exit_reason"] == "sector_retreat"
        assert journal[0]["request"]["exit_reason"] == "sector_retreat"

    def test_execution_api_cancel_order_dispatches_trade_notification(self, tmp_path):
        adapter = MockExecutionAdapter()
        dispatcher = DummyTradeDispatcher()
        state_store = StateStore(tmp_path / "meeting_state.json")
        app = FastAPI()
        app.include_router(
            build_execution_router(
                adapter,
                "mock",
                dispatcher=dispatcher,
                meeting_state_store=state_store,
            )
        )
        client = TestClient(app)

        order = adapter.place_order(
            PlaceOrderRequest(
                account_id="sim-001",
                symbol="000001.SZ",
                side="BUY",
                quantity=100,
                price=10.0,
                request_id="req-api-cancel-1",
                decision_id="case-cancel-1",
            )
        )
        state_store.set(
            "execution_order_journal",
            [
                {
                    "trade_date": "2026-04-04",
                    "account_id": "sim-001",
                    "order_id": order.order_id,
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "decision_id": "case-cancel-1",
                    "submitted_at": "2026-04-04T10:00:00",
                }
            ],
        )

        response = client.request(
            "DELETE",
            "/execution/orders",
            json={
                "account_id": "sim-001",
                "order_id": order.order_id,
                "request_id": "req-api-cancel-2",
            },
        )
        payload = response.json()
        assert response.status_code == 200
        assert payload["status"] == "CANCELLED"
        assert len(dispatcher.calls) == 1
        assert dispatcher.calls[0]["title"] == "交易撤单"
        assert "平安银行" in dispatcher.calls[0]["content"]
        assert "case-cancel-1" in dispatcher.calls[0]["content"]


class TestDataLayer:
    def test_fetcher(self):
        market = MockMarketDataAdapter()
        fetcher = DataFetcher(market)
        snaps = fetcher.fetch_snapshots(["600519.SH"])
        assert len(snaps) == 1
        result = fetcher.fetch_bars(["600519.SH"], "1d")
        assert result.quality.source == "real"

    def test_fetcher_empty_on_failure(self):
        """数据获取失败必须返回空，不能注入假数据"""
        fetcher = DataFetcher(None)
        snaps = fetcher.fetch_snapshots(["600519.SH"])
        assert snaps == []

    def test_validator(self):
        from ashare_system.contracts import BarSnapshot
        v = DataValidator()
        bars = [BarSnapshot(symbol="600519.SH", period="1d", open=10, high=12, low=9, close=11, volume=1000, amount=11000, trade_time="2026-04-03")]
        issues = v.validate_bars(bars)
        assert issues == []

    def test_cleaner(self):
        c = DataCleaner()
        result = c.clean([])
        assert result == []


class TestScheduler:
    def test_scheduler_tasks(self):
        s = Scheduler()
        assert len(s.tasks) >= 7
        names = [t.name for t in s.tasks]
        assert "新闻扫描" in names
        assert "日终数据拉取" in names
        assert "股性画像刷新" in names
        assert "参数治理巡检" in names

    def test_build_postclose_review_board_summary_aggregates_sections(self):
        payload = build_postclose_review_board_summary(
            inspection_payload={
                "action_item_count": 2,
                "high_priority_action_item_count": 1,
                "summary_lines": ["参数治理巡检命中 2 项，其中 1 项高优先级。"],
            },
            tail_market_payload={
                "trade_date": "2026-04-06",
                "summary_lines": ["尾盘卖出扫描完成: positions=1 signals=1 submitted=0 preview=1 errors=0."],
                "items": [
                    {
                        "symbol": "600519.SH",
                        "exit_reason": "sector_retreat",
                        "review_tags": ["sector_retreat", "leader_style"],
                    }
                ],
            },
            discussion_context={
                "trade_date": "2026-04-06",
                "status": "blocked",
                "client_brief": {
                    "status": "blocked",
                    "lines": ["讨论收口存在执行阻断。"],
                },
                "finalize_packet": {
                    "status": "blocked",
                    "blocked": True,
                    "execution_precheck": {
                        "blocked_count": 1,
                        "approved_count": 0,
                    },
                },
            },
        )
        assert payload["available"] is True
        assert payload["trade_date"] == "2026-04-06"
        assert payload["discussion_status"] == "blocked"
        assert payload["counts"]["governance_high_priority_action_item_count"] == 1
        assert payload["counts"]["tail_market_count"] == 1
        assert payload["counts"]["discussion_blocked_count"] == 1
        assert payload["summary_lines"][0].startswith("盘后 review board 摘要")

    def test_tail_market_scan_submits_sell_and_persists_metadata(self, tmp_path):
        settings = load_settings()
        settings.run_mode = "paper"
        settings.live_trade_enabled = False
        settings.execution_plane = "local_xtquant"
        settings.xtquant.account_id = "8890130545"

        adapter = MockExecutionAdapter()
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
        market = MockMarketDataAdapter()
        meeting_state = StateStore(tmp_path / "meeting_state.json")
        runtime_state = StateStore(tmp_path / "runtime_state.json")
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
        runtime_state.set(
            "latest_runtime_context",
            {
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
                        "strength_score": 0.82,
                    }
                ],
                "playbook_contexts": [
                    {
                        "playbook": "leader_chase",
                        "symbol": "600519.SH",
                        "sector": "白酒",
                        "entry_window": "09:30-10:00",
                        "confidence": 0.93,
                        "rank_in_sector": 1,
                        "leader_score": 0.95,
                        "style_tag": "leader",
                        "exit_params": {
                            "atr_pct": 0.015,
                            "open_failure_minutes": 5,
                            "max_hold_minutes": 240,
                            "time_stop": "14:50",
                        },
                    }
                ],
            },
        )

        payload = run_tail_market_scan(
            settings=settings,
            market=market,
            execution_adapter=adapter,
            meeting_state_store=meeting_state,
            runtime_state_store=runtime_state,
            now_factory=lambda: datetime(2026, 4, 6, 14, 50, 0),
        )

        assert payload["status"] == "ok"
        assert payload["submitted_count"] == 1
        submitted = next(item for item in payload["items"] if item["status"] == "submitted")
        assert submitted["exit_reason"] == "sector_retreat"
        journal = meeting_state.get("execution_order_journal", [])
        sell_journal = journal[-1]
        assert sell_journal["order_id"] == submitted["order_id"]
        assert sell_journal["trade_date"] == "2026-04-06"
        assert sell_journal["playbook"] == "leader_chase"
        assert sell_journal["regime"] == "trend"
        assert sell_journal["exit_reason"] == "sector_retreat"
        assert sell_journal["request"]["side"] == "SELL"
        assert meeting_state.get("latest_tail_market_scan")["submitted_count"] == 1

    def test_tail_market_scan_queues_gateway_intent_on_windows_gateway_plane(self, tmp_path):
        settings = load_settings()
        settings.run_mode = "paper"
        settings.live_trade_enabled = False
        settings.execution_plane = "windows_gateway"
        settings.xtquant.account_id = "8890130545"

        adapter = MockExecutionAdapter()
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
        market = MockMarketDataAdapter()
        meeting_state = StateStore(tmp_path / "meeting_state.json")
        runtime_state = StateStore(tmp_path / "runtime_state.json")
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
        runtime_state.set(
            "latest_runtime_context",
            {
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
                        "strength_score": 0.82,
                    }
                ],
                "playbook_contexts": [
                    {
                        "playbook": "leader_chase",
                        "symbol": "600519.SH",
                        "sector": "白酒",
                        "entry_window": "09:30-10:00",
                        "confidence": 0.93,
                        "rank_in_sector": 1,
                        "leader_score": 0.95,
                        "style_tag": "leader",
                        "exit_params": {
                            "atr_pct": 0.015,
                            "open_failure_minutes": 5,
                            "max_hold_minutes": 240,
                            "time_stop": "14:50",
                        },
                    }
                ],
            },
        )

        payload = run_tail_market_scan(
            settings=settings,
            market=market,
            execution_adapter=adapter,
            meeting_state_store=meeting_state,
            runtime_state_store=runtime_state,
            now_factory=lambda: datetime(2026, 4, 6, 14, 50, 0),
        )

        assert payload["status"] == "queued_for_gateway"
        assert payload["execution_plane"] == "windows_gateway"
        assert payload["submitted_count"] == 0
        assert payload["queued_count"] == 1
        assert payload["preview_count"] == 0
        queued = next(item for item in payload["items"] if item["status"] == "queued_for_gateway")
        assert queued["execution_plane"] == "windows_gateway"
        assert queued["request"]["side"] == "SELL"
        assert queued["gateway_pull_path"] == "/system/execution/gateway/intents/pending"
        assert queued["gateway_intent"]["intent_id"] == queued["request"]["request_id"]
        assert queued["gateway_intent"]["approval_source"] == "tail_market_scan"
        assert queued["gateway_intent"]["execution_plane"] == "windows_gateway"
        assert queued["gateway_intent"]["discussion_context"]["trigger_source"] == "tail_market_scan"
        assert queued["gateway_intent"]["strategy_context"]["exit_reason"] == queued["exit_reason"]
        pending = meeting_state.get("pending_execution_intents", [])
        assert len(pending) == 1
        assert pending[0]["intent_id"] == queued["gateway_intent"]["intent_id"]
        assert pending[0]["status"] == "approved"
        assert len(meeting_state.get("execution_order_journal", [])) == 1
        assert meeting_state.get("latest_tail_market_scan")["submitted_count"] == 0
        assert meeting_state.get("latest_tail_market_scan")["queued_count"] == 1


class TestRuntimeSafety:
    def test_live_qmt_start_failure_raises(self):
        settings = load_settings()
        settings.run_mode = "live"
        settings.execution_mode = "xtquant"
        settings.market_mode = "xtquant"
        settings.xtquant.auto_start = True
        launcher = SimpleNamespace(ensure_running=lambda: False, watchdog_loop=lambda: None)
        with patch("ashare_system.infra.qmt_launcher.QMTLauncher", return_value=launcher):
            with pytest.raises(RuntimeError, match="live 模式下 QMT 启动失败"):
                _start_qmt_if_needed(settings)


class TestExecutionSafetyHelpers:
    def test_is_trading_session_detects_open_and_closed_windows(self):
        assert is_trading_session(datetime(2026, 4, 6, 9, 35, 0)) is True
        assert is_trading_session(datetime(2026, 4, 6, 11, 45, 0)) is False
        assert is_trading_session(datetime(2026, 4, 4, 10, 0, 0)) is False

    def test_board_limit_pct_and_limit_detection_follow_market_segments(self):
        assert board_limit_pct("600519.SH") == 0.1
        assert board_limit_pct("300750.SZ") == 0.2
        assert board_limit_pct("830001.BJ") == 0.3
        assert is_limit_up("300750.SZ", last_price=12.0, pre_close=10.0) is True
        assert is_limit_down("600519.SH", last_price=9.0, pre_close=10.0) is True

    def test_snapshot_freshness_uses_iso_timestamp_age(self):
        now = datetime(2026, 4, 6, 10, 0, 0)
        snapshot_at = "2026-04-06T09:56:00"
        assert snapshot_age_seconds(snapshot_at, now) == 240.0
        assert is_snapshot_fresh(snapshot_at, now, max_age_seconds=300) is True
        assert is_snapshot_fresh(snapshot_at, now, max_age_seconds=180) is False

    def test_price_deviation_detects_outside_orderbook_band(self):
        assert is_price_deviation_exceeded(10.5, bid_price=10.0, ask_price=10.1, max_deviation_pct=0.02) is True
        assert is_price_deviation_exceeded(10.08, bid_price=10.0, ask_price=10.1, max_deviation_pct=0.02) is False


class TestPendingOrderInspection:
    def test_inspection_marks_stale_pending_order_using_dispatch_journal(self, tmp_path):
        clock = [datetime(2026, 4, 6, 10, 5, 0)]
        adapter = MockExecutionAdapter()
        order = adapter.place_order(
            PlaceOrderRequest(
                account_id="sim-001",
                symbol="000001.SZ",
                side="BUY",
                quantity=100,
                price=10.0,
                request_id="req-inspect-1",
            )
        )
        state_store = StateStore(tmp_path / "meeting_state.json")
        state_store.set(
            "execution_order_journal",
            [
                {
                    "order_id": order.order_id,
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "trade_date": "2026-04-06",
                    "decision_id": "case-1",
                    "submitted_at": "2026-04-06T10:00:00",
                }
            ],
        )
        service = PendingOrderInspectionService(adapter, state_store, now_factory=lambda: clock[0])

        payload = service.inspect("sim-001", warn_after_seconds=120, persist=True)

        assert payload["status"] == "warning"
        assert payload["pending_count"] == 1
        assert payload["stale_count"] == 1
        assert payload["items"][0]["name"] == "平安银行"
        assert payload["items"][0]["is_stale"] is True
        assert payload["items"][0]["age_seconds"] == 300.0


class TestTailMarketBehaviorAwareExit:
    def test_tail_market_scan_reads_behavior_profile_from_dossier(self, tmp_path):
        settings = load_settings()
        settings.run_mode = "paper"
        settings.live_trade_enabled = False
        settings.storage_root = tmp_path / "state"
        settings.workspace = tmp_path
        settings.logs_dir = tmp_path / "logs"
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        settings.logs_dir.mkdir(parents=True, exist_ok=True)

        adapter = MockExecutionAdapter()
        adapter.positions["sim-001"] = [
            PositionSnapshot(
                account_id="sim-001",
                symbol="600519.SH",
                quantity=100,
                available=100,
                cost_price=10.0,
                last_price=10.02,
            )
        ]
        market = MockMarketDataAdapter()
        market.get_snapshots = lambda symbols: [
            QuoteSnapshot(
                symbol="600519.SH",
                name="贵州茅台",
                last_price=10.02,
                bid_price=10.01,
                ask_price=10.03,
                volume=100_000,
                pre_close=10.0,
            )
        ]
        meeting_state = StateStore(tmp_path / "meeting_state.json")
        runtime_state = StateStore(tmp_path / "runtime_state.json")
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
        runtime_state.set(
            "latest_runtime_context",
            {
                "available": True,
                "resource": "runtime_context",
                "trade_date": "2026-04-06",
                "generated_at": "2026-04-06T14:45:00",
                "market_profile": {
                    "sentiment_phase": "主升",
                    "regime": "trend",
                    "allowed_playbooks": ["leader_chase"],
                },
                "playbook_contexts": [
                    {
                        "playbook": "leader_chase",
                        "symbol": "600519.SH",
                        "sector": "白酒",
                        "entry_window": "09:30-10:00",
                        "confidence": 0.93,
                        "rank_in_sector": 1,
                        "leader_score": 0.95,
                        "style_tag": "leader",
                        "exit_params": {
                            "atr_pct": 0.015,
                            "open_failure_minutes": 5,
                            "max_hold_minutes": 240,
                            "time_stop": "14:50",
                        },
                    }
                ],
            },
        )
        DataArchiveStore(settings.storage_root).persist_dossier_pack(
            {
                "pack_id": "dossier-tail-1",
                "trade_date": "2026-04-06",
                "generated_at": "2026-04-06T14:45:00",
                "expires_at": "2026-04-06T15:05:00",
                "signature": "tail-market-test",
                "items": [
                    {
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "resolved_sector": "白酒",
                        "assigned_playbook": "leader_chase",
                        "symbol_context": {
                            "symbol": "600519.SH",
                            "market_relative": {"relative_strength_pct": -0.008},
                            "behavior_profile": {
                                "symbol": "600519.SH",
                                "optimal_hold_days": 1,
                                "style_tag": "leader",
                                "leader_frequency_30d": 0.61,
                                "avg_sector_rank_30d": 1.8,
                            },
                        },
                    }
                ],
            }
        )

        payload = run_tail_market_scan(
            settings=settings,
            market=market,
            execution_adapter=adapter,
            meeting_state_store=meeting_state,
            runtime_state_store=runtime_state,
            now_factory=lambda: datetime(2026, 4, 6, 10, 55, 0),
        )

        assert payload["status"] == "ok"
        assert payload["submitted_count"] == 1
        submitted = next(item for item in payload["items"] if item["status"] == "submitted")
        assert submitted["exit_reason"] == "time_stop"
        assert submitted["behavior_profile"]["style_tag"] == "leader"
        assert submitted["relative_strength_5m"] == -0.008

    def test_tail_market_scan_uses_intraday_fade_and_monitor_alerts(self, tmp_path):
        settings = load_settings()
        settings.run_mode = "paper"
        settings.live_trade_enabled = False
        settings.storage_root = tmp_path / "state"
        settings.workspace = tmp_path
        settings.logs_dir = tmp_path / "logs"
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        settings.logs_dir.mkdir(parents=True, exist_ok=True)

        adapter = MockExecutionAdapter()
        adapter.positions["sim-001"] = [
            PositionSnapshot(
                account_id="sim-001",
                symbol="600519.SH",
                quantity=100,
                available=100,
                cost_price=10.0,
                last_price=10.18,
            )
        ]
        market = MockMarketDataAdapter()
        market.get_snapshots = lambda symbols: [
            QuoteSnapshot(
                symbol="600519.SH",
                name="贵州茅台",
                last_price=10.18,
                bid_price=10.17,
                ask_price=10.19,
                volume=180_000,
                pre_close=10.0,
            )
        ]
        market.get_bars = lambda symbols, period, count=1: [
            BarSnapshot(
                symbol="600519.SH",
                period="5m",
                open=10.30,
                high=10.32,
                low=10.18,
                close=10.24,
                volume=100_000,
                amount=1_000_000,
                trade_time="2026-04-06T13:30:00+08:00",
                pre_close=10.0,
            ),
            BarSnapshot(
                symbol="600519.SH",
                period="5m",
                open=10.24,
                high=10.52,
                low=10.20,
                close=10.22,
                volume=180_000,
                amount=2_000_000,
                trade_time="2026-04-06T13:35:00+08:00",
                pre_close=10.24,
            ),
            BarSnapshot(
                symbol="600519.SH",
                period="5m",
                open=10.22,
                high=10.21,
                low=10.16,
                close=10.18,
                volume=160_000,
                amount=1_900_000,
                trade_time="2026-04-06T13:40:00+08:00",
                pre_close=10.22,
            ),
        ]
        meeting_state = StateStore(tmp_path / "meeting_state.json")
        runtime_state = StateStore(tmp_path / "runtime_state.json")
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
                    "submitted_at": "2026-04-06T13:20:00",
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
        runtime_state.set(
            "latest_runtime_context",
            {
                "available": True,
                "resource": "runtime_context",
                "trade_date": "2026-04-06",
                "generated_at": "2026-04-06T14:10:00",
                "market_profile": {
                    "sentiment_phase": "主升",
                    "regime": "trend",
                    "allowed_playbooks": ["leader_chase"],
                },
                "playbook_contexts": [
                    {
                        "playbook": "leader_chase",
                        "symbol": "600519.SH",
                        "sector": "白酒",
                        "entry_window": "09:30-10:00",
                        "confidence": 0.93,
                        "rank_in_sector": 1,
                        "leader_score": 0.95,
                        "style_tag": "leader",
                        "exit_params": {
                            "atr_pct": 0.015,
                            "open_failure_minutes": 5,
                            "max_hold_minutes": 240,
                            "time_stop": "14:50",
                        },
                    }
                ],
            },
        )
        archive = DataArchiveStore(settings.storage_root)
        archive.persist_dossier_pack(
            {
                "pack_id": "dossier-tail-intraday-1",
                "trade_date": "2026-04-06",
                "generated_at": "2026-04-06T14:10:00",
                "expires_at": "2026-04-06T15:00:00",
                "signature": "tail-market-intraday",
                "behavior_profiles": [
                    {
                        "symbol": "600519.SH",
                        "optimal_hold_days": 1,
                        "style_tag": "leader",
                        "leader_frequency_30d": 0.72,
                        "avg_sector_rank_30d": 1.0,
                    }
                ],
                "items": [
                    {
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "resolved_sector": "白酒",
                        "assigned_playbook": "leader_chase",
                        "behavior_profile": {
                            "symbol": "600519.SH",
                            "optimal_hold_days": 1,
                            "style_tag": "leader",
                            "leader_frequency_30d": 0.72,
                            "avg_sector_rank_30d": 1.0,
                        },
                        "symbol_context": {
                            "symbol": "600519.SH",
                            "market_relative": {"relative_strength_pct": 0.004},
                            "behavior_profile": {
                                "symbol": "600519.SH",
                                "optimal_hold_days": 1,
                                "style_tag": "leader",
                                "leader_frequency_30d": 0.72,
                                "avg_sector_rank_30d": 1.0,
                            },
                        },
                    }
                ],
            }
        )
        archive.persist_monitor_context(
            "2026-04-06",
            {
                "available": True,
                "resource": "monitor_context",
                "trade_date": "2026-04-06",
                "generated_at": "2026-04-06T14:18:00",
                "event_count": 1,
                "recent_events": [
                    {
                        "symbol": "600519.SH",
                        "alert_type": "price_spike",
                        "severity": "warning",
                        "change_pct": -0.035,
                    }
                ],
            },
        )

        payload = run_tail_market_scan(
            settings=settings,
            market=market,
            execution_adapter=adapter,
            meeting_state_store=meeting_state,
            runtime_state_store=runtime_state,
            now_factory=lambda: datetime(2026, 4, 6, 14, 20, 0),
        )

        assert payload["status"] == "ok"
        assert payload["submitted_count"] == 1
        submitted = next(item for item in payload["items"] if item["status"] == "submitted")
        assert submitted["exit_reason"] == "time_stop"
        assert submitted["intraday_drawdown_pct"] is not None
        assert submitted["intraday_drawdown_pct"] >= 0.025
        assert submitted["negative_alert_count"] == 1

    def test_tail_market_scan_uses_1m_microstructure_fast_exit(self, tmp_path):
        settings = load_settings()
        settings.run_mode = "paper"
        settings.live_trade_enabled = False
        settings.storage_root = tmp_path / "state"
        settings.workspace = tmp_path
        settings.logs_dir = tmp_path / "logs"
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        settings.logs_dir.mkdir(parents=True, exist_ok=True)

        adapter = MockExecutionAdapter()
        adapter.positions["sim-001"] = [
            PositionSnapshot(
                account_id="sim-001",
                symbol="600519.SH",
                quantity=100,
                available=100,
                cost_price=10.0,
                last_price=10.02,
            )
        ]
        market = MockMarketDataAdapter()
        market.get_snapshots = lambda symbols: [
            QuoteSnapshot(
                symbol="600519.SH",
                name="贵州茅台",
                last_price=10.02,
                bid_price=10.01,
                ask_price=10.03,
                volume=160_000,
                pre_close=10.0,
            )
        ]

        def fake_get_bars(symbols, period, count=1):
            if period == "1m":
                return [
                    BarSnapshot(
                        symbol="600519.SH",
                        period="1m",
                        open=10.16,
                        high=10.18,
                        low=10.12,
                        close=10.14,
                        volume=70_000,
                        amount=700_000,
                        trade_time="2026-04-06T13:37:00+08:00",
                        pre_close=10.16,
                    ),
                    BarSnapshot(
                        symbol="600519.SH",
                        period="1m",
                        open=10.14,
                        high=10.15,
                        low=10.08,
                        close=10.10,
                        volume=82_000,
                        amount=820_000,
                        trade_time="2026-04-06T13:38:00+08:00",
                        pre_close=10.14,
                    ),
                    BarSnapshot(
                        symbol="600519.SH",
                        period="1m",
                        open=10.10,
                        high=10.11,
                        low=10.01,
                        close=10.02,
                        volume=95_000,
                        amount=950_000,
                        trade_time="2026-04-06T13:39:00+08:00",
                        pre_close=10.10,
                    ),
                    BarSnapshot(
                        symbol="600036.SH",
                        period="1m",
                        open=10.03,
                        high=10.04,
                        low=10.02,
                        close=10.03,
                        volume=50_000,
                        amount=500_000,
                        trade_time="2026-04-06T13:37:00+08:00",
                        pre_close=10.03,
                    ),
                    BarSnapshot(
                        symbol="600036.SH",
                        period="1m",
                        open=10.03,
                        high=10.04,
                        low=10.02,
                        close=10.04,
                        volume=52_000,
                        amount=520_000,
                        trade_time="2026-04-06T13:38:00+08:00",
                        pre_close=10.03,
                    ),
                    BarSnapshot(
                        symbol="600036.SH",
                        period="1m",
                        open=10.04,
                        high=10.05,
                        low=10.03,
                        close=10.04,
                        volume=55_000,
                        amount=550_000,
                        trade_time="2026-04-06T13:39:00+08:00",
                        pre_close=10.04,
                    ),
                ]
            return [
                BarSnapshot(
                    symbol="600519.SH",
                    period="5m",
                    open=10.00,
                    high=10.10,
                    low=9.99,
                    close=10.08,
                    volume=120_000,
                    amount=1_200_000,
                    trade_time="2026-04-06T13:25:00+08:00",
                    pre_close=10.0,
                ),
                BarSnapshot(
                    symbol="600519.SH",
                    period="5m",
                    open=10.08,
                    high=10.22,
                    low=10.05,
                    close=10.10,
                    volume=125_000,
                    amount=1_250_000,
                    trade_time="2026-04-06T13:30:00+08:00",
                    pre_close=10.08,
                ),
                BarSnapshot(
                    symbol="600519.SH",
                    period="5m",
                    open=10.10,
                    high=10.12,
                    low=10.00,
                    close=10.02,
                    volume=150_000,
                    amount=1_500_000,
                    trade_time="2026-04-06T13:35:00+08:00",
                    pre_close=10.10,
                ),
                BarSnapshot(
                    symbol="600036.SH",
                    period="5m",
                    open=10.00,
                    high=10.03,
                    low=9.99,
                    close=10.02,
                    volume=100_000,
                    amount=1_000_000,
                    trade_time="2026-04-06T13:25:00+08:00",
                    pre_close=10.0,
                ),
                BarSnapshot(
                    symbol="600036.SH",
                    period="5m",
                    open=10.02,
                    high=10.05,
                    low=10.01,
                    close=10.03,
                    volume=105_000,
                    amount=1_050_000,
                    trade_time="2026-04-06T13:30:00+08:00",
                    pre_close=10.02,
                ),
                BarSnapshot(
                    symbol="600036.SH",
                    period="5m",
                    open=10.03,
                    high=10.06,
                    low=10.02,
                    close=10.04,
                    volume=110_000,
                    amount=1_100_000,
                    trade_time="2026-04-06T13:35:00+08:00",
                    pre_close=10.03,
                ),
            ]

        market.get_bars = fake_get_bars
        meeting_state = StateStore(tmp_path / "meeting_state.json")
        runtime_state = StateStore(tmp_path / "runtime_state.json")
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
                    "submitted_at": "2026-04-06T13:15:00",
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
        runtime_state.set(
            "latest_runtime_context",
            {
                "available": True,
                "resource": "runtime_context",
                "trade_date": "2026-04-06",
                "generated_at": "2026-04-06T13:35:00",
                "market_profile": {
                    "sentiment_phase": "主升",
                    "regime": "trend",
                    "allowed_playbooks": ["leader_chase"],
                },
                "playbook_contexts": [
                    {
                        "playbook": "leader_chase",
                        "symbol": "600519.SH",
                        "sector": "白酒",
                        "entry_window": "09:30-10:00",
                        "confidence": 0.93,
                        "rank_in_sector": 1,
                        "leader_score": 0.95,
                        "style_tag": "leader",
                        "exit_params": {
                            "atr_pct": 0.015,
                            "open_failure_minutes": 5,
                            "max_hold_minutes": 240,
                            "time_stop": "14:50",
                        },
                    },
                    {
                        "playbook": "leader_chase",
                        "symbol": "600036.SH",
                        "sector": "白酒",
                        "entry_window": "09:30-10:00",
                        "confidence": 0.86,
                        "rank_in_sector": 2,
                        "leader_score": 0.88,
                        "style_tag": "leader",
                        "exit_params": {
                            "atr_pct": 0.015,
                            "open_failure_minutes": 5,
                            "max_hold_minutes": 240,
                            "time_stop": "14:50",
                        },
                    },
                ],
            },
        )
        archive = DataArchiveStore(settings.storage_root)
        archive.persist_dossier_pack(
            {
                "pack_id": "dossier-tail-micro-1",
                "trade_date": "2026-04-06",
                "generated_at": "2026-04-06T13:35:00",
                "expires_at": "2026-04-06T15:00:00",
                "signature": "tail-market-micro",
                "items": [
                    {
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "resolved_sector": "白酒",
                        "assigned_playbook": "leader_chase",
                        "behavior_profile": {
                            "symbol": "600519.SH",
                            "optimal_hold_days": 1,
                            "style_tag": "leader",
                            "leader_frequency_30d": 0.7,
                            "avg_sector_rank_30d": 1.2,
                        },
                    },
                    {
                        "symbol": "600036.SH",
                        "name": "招商银行",
                        "resolved_sector": "白酒",
                        "assigned_playbook": "leader_chase",
                    },
                ],
            }
        )

        payload = run_tail_market_scan(
            settings=settings,
            market=market,
            execution_adapter=adapter,
            meeting_state_store=meeting_state,
            runtime_state_store=runtime_state,
            now_factory=lambda: datetime.fromisoformat("2026-04-06T13:40:00+08:00"),
        )

        assert payload["status"] == "ok"
        assert payload["submitted_count"] == 1
        submitted = next(item for item in payload["items"] if item["status"] == "submitted")
        assert submitted["exit_reason"] == "time_stop"
        assert submitted["micro_1m_return_3_sum"] <= -0.01
        assert submitted["micro_1m_drawdown_pct"] >= 0.012
        assert submitted["micro_1m_negative_bars"] >= 2
        journal = meeting_state.get("execution_order_journal", [])
        sell_journal = journal[-1]
        assert sell_journal["exit_context_snapshot"]["exit_params"]["micro_1m_return_3_sum"] <= -0.01
        assert "microstructure_fast_exit" in sell_journal["review_tags"]

    def test_tail_market_scan_marks_failed_micro_rebound(self, tmp_path):
        settings = load_settings()
        settings.run_mode = "paper"
        settings.live_trade_enabled = False
        settings.storage_root = tmp_path / "state"
        settings.workspace = tmp_path
        settings.logs_dir = tmp_path / "logs"
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        settings.logs_dir.mkdir(parents=True, exist_ok=True)

        adapter = MockExecutionAdapter()
        adapter.positions["sim-001"] = [
            PositionSnapshot(
                account_id="sim-001",
                symbol="600519.SH",
                quantity=100,
                available=100,
                cost_price=10.0,
                last_price=9.97,
            )
        ]
        market = MockMarketDataAdapter()
        market.get_snapshots = lambda symbols: [
            QuoteSnapshot(
                symbol="600519.SH",
                name="贵州茅台",
                last_price=9.97,
                bid_price=9.96,
                ask_price=9.98,
                volume=150_000,
                pre_close=10.0,
            )
        ]

        def fake_get_bars(symbols, period, count=1):
            if period == "1m":
                return [
                    BarSnapshot(
                        symbol="600519.SH",
                        period="1m",
                        open=10.10,
                        high=10.12,
                        low=10.00,
                        close=10.05,
                        volume=60_000,
                        amount=600_000,
                        trade_time="2026-04-06T13:37:00+08:00",
                        pre_close=10.10,
                    ),
                    BarSnapshot(
                        symbol="600519.SH",
                        period="1m",
                        open=10.05,
                        high=10.06,
                        low=9.98,
                        close=10.01,
                        volume=72_000,
                        amount=720_000,
                        trade_time="2026-04-06T13:38:00+08:00",
                        pre_close=10.05,
                    ),
                    BarSnapshot(
                        symbol="600519.SH",
                        period="1m",
                        open=10.01,
                        high=10.02,
                        low=9.97,
                        close=9.97,
                        volume=80_000,
                        amount=800_000,
                        trade_time="2026-04-06T13:39:00+08:00",
                        pre_close=10.01,
                    ),
                    BarSnapshot(
                        symbol="600036.SH",
                        period="1m",
                        open=10.03,
                        high=10.04,
                        low=10.02,
                        close=10.03,
                        volume=50_000,
                        amount=500_000,
                        trade_time="2026-04-06T13:37:00+08:00",
                        pre_close=10.03,
                    ),
                    BarSnapshot(
                        symbol="600036.SH",
                        period="1m",
                        open=10.03,
                        high=10.05,
                        low=10.02,
                        close=10.04,
                        volume=52_000,
                        amount=520_000,
                        trade_time="2026-04-06T13:38:00+08:00",
                        pre_close=10.03,
                    ),
                    BarSnapshot(
                        symbol="600036.SH",
                        period="1m",
                        open=10.04,
                        high=10.05,
                        low=10.03,
                        close=10.04,
                        volume=55_000,
                        amount=550_000,
                        trade_time="2026-04-06T13:39:00+08:00",
                        pre_close=10.04,
                    ),
                ]
            return [
                BarSnapshot(
                    symbol="600519.SH",
                    period="5m",
                    open=10.00,
                    high=10.10,
                    low=9.99,
                    close=10.06,
                    volume=110_000,
                    amount=1_100_000,
                    trade_time="2026-04-06T13:25:00+08:00",
                    pre_close=10.0,
                ),
                BarSnapshot(
                    symbol="600519.SH",
                    period="5m",
                    open=10.06,
                    high=10.12,
                    low=10.00,
                    close=10.01,
                    volume=120_000,
                    amount=1_200_000,
                    trade_time="2026-04-06T13:30:00+08:00",
                    pre_close=10.06,
                ),
                BarSnapshot(
                    symbol="600519.SH",
                    period="5m",
                    open=10.01,
                    high=10.03,
                    low=9.97,
                    close=9.97,
                    volume=140_000,
                    amount=1_400_000,
                    trade_time="2026-04-06T13:35:00+08:00",
                    pre_close=10.01,
                ),
                BarSnapshot(
                    symbol="600036.SH",
                    period="5m",
                    open=10.00,
                    high=10.03,
                    low=9.99,
                    close=10.02,
                    volume=90_000,
                    amount=900_000,
                    trade_time="2026-04-06T13:25:00+08:00",
                    pre_close=10.0,
                ),
                BarSnapshot(
                    symbol="600036.SH",
                    period="5m",
                    open=10.02,
                    high=10.04,
                    low=10.00,
                    close=10.03,
                    volume=92_000,
                    amount=920_000,
                    trade_time="2026-04-06T13:30:00+08:00",
                    pre_close=10.02,
                ),
                BarSnapshot(
                    symbol="600036.SH",
                    period="5m",
                    open=10.03,
                    high=10.05,
                    low=10.01,
                    close=10.04,
                    volume=95_000,
                    amount=950_000,
                    trade_time="2026-04-06T13:35:00+08:00",
                    pre_close=10.03,
                ),
            ]

        market.get_bars = fake_get_bars

        meeting_state = StateStore(settings.storage_root / "meeting_state.json")
        runtime_state = StateStore(settings.storage_root / "runtime_state.json")
        meeting_state.set(
            "execution_order_journal",
            [
                {
                    "trade_date": "2026-04-06",
                    "order_id": "buy-001",
                    "account_id": "sim-001",
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "decision_id": "case-001",
                    "submitted_at": "2026-04-06T13:15:00+08:00",
                    "request": {"behavior_profile": {"style_tag": "leader", "optimal_hold_days": 1}},
                }
            ],
        )
        runtime_state.set(
            "latest_runtime_context",
            {
                "trade_date": "2026-04-06",
                "market_profile": {"regime": "trend", "sector_profiles": []},
                "playbook_contexts": [
                    {
                        "symbol": "600519.SH",
                        "playbook": "leader_chase",
                        "sector": "白酒",
                        "exit_params": {},
                    }
                ],
            },
        )
        runtime_state.set(
            "latest_dossier_pack",
            {
                "trade_date": "2026-04-06",
                "items": [
                    {
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "resolved_sector": "白酒",
                        "assigned_playbook": "leader_chase",
                        "symbol_context": {
                            "market_relative": {"relative_strength_pct": -0.006},
                            "behavior_profile": {"style_tag": "leader", "optimal_hold_days": 1},
                        },
                    },
                    {
                        "symbol": "600036.SH",
                        "name": "招商银行",
                        "resolved_sector": "白酒",
                        "assigned_playbook": "leader_chase",
                    },
                ],
            },
        )
        archive = DataArchiveStore(settings.storage_root)
        archive.persist_monitor_context(
            "2026-04-06",
            {
                "available": True,
                "resource": "monitor_context",
                "trade_date": "2026-04-06",
                "generated_at": "2026-04-06T13:38:00",
                "event_count": 1,
                "recent_events": [
                    {
                        "symbol": "600519.SH",
                        "alert_type": "price_spike",
                        "severity": "warning",
                        "change_pct": -0.02,
                    }
                ],
            },
        )

        payload = run_tail_market_scan(
            settings=settings,
            market=market,
            execution_adapter=adapter,
            meeting_state_store=meeting_state,
            runtime_state_store=runtime_state,
            now_factory=lambda: datetime(2026, 4, 6, 13, 40, 0),
        )

        assert payload["status"] == "ok"
        assert payload["submitted_count"] == 1
        submitted = next(item for item in payload["items"] if item["status"] == "submitted")
        assert submitted["exit_reason"] == "time_stop"
        assert submitted["micro_1m_drawdown_pct"] >= 0.01
        assert submitted["micro_1m_rebound_from_low_pct"] <= 0.004
        assert submitted["micro_1m_latest_return_pct"] <= -0.003
        journal = meeting_state.get("execution_order_journal", [])
        sell_journal = journal[-1]
        assert "micro_rebound_failed" in sell_journal["review_tags"]

    def test_tail_market_scan_uses_sector_relative_intraday_weakness(self, tmp_path):
        settings = load_settings()
        settings.run_mode = "paper"
        settings.live_trade_enabled = False
        settings.storage_root = tmp_path / "state"
        settings.workspace = tmp_path
        settings.logs_dir = tmp_path / "logs"
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        settings.logs_dir.mkdir(parents=True, exist_ok=True)

        adapter = MockExecutionAdapter()
        adapter.positions["sim-001"] = [
            PositionSnapshot(
                account_id="sim-001",
                symbol="600519.SH",
                quantity=100,
                available=100,
                cost_price=10.0,
                last_price=10.04,
            )
        ]
        market = MockMarketDataAdapter()
        market.get_snapshots = lambda symbols: [
            QuoteSnapshot(
                symbol="600519.SH",
                name="贵州茅台",
                last_price=10.04,
                bid_price=10.03,
                ask_price=10.05,
                volume=220_000,
                pre_close=10.0,
            )
        ]
        market.get_bars = lambda symbols, period, count=1: [
            BarSnapshot(
                symbol="600519.SH",
                period="5m",
                open=10.00,
                high=10.05,
                low=9.99,
                close=10.03,
                volume=120_000,
                amount=1_200_000,
                trade_time="2026-04-06T13:30:00+08:00",
                pre_close=10.0,
            ),
            BarSnapshot(
                symbol="600519.SH",
                period="5m",
                open=10.03,
                high=10.05,
                low=10.02,
                close=10.04,
                volume=110_000,
                amount=1_100_000,
                trade_time="2026-04-06T13:35:00+08:00",
                pre_close=10.03,
            ),
            BarSnapshot(
                symbol="600036.SH",
                period="5m",
                open=10.00,
                high=10.18,
                low=9.99,
                close=10.15,
                volume=150_000,
                amount=1_500_000,
                trade_time="2026-04-06T13:30:00+08:00",
                pre_close=10.0,
            ),
            BarSnapshot(
                symbol="600036.SH",
                period="5m",
                open=10.15,
                high=10.32,
                low=10.14,
                close=10.30,
                volume=180_000,
                amount=1_800_000,
                trade_time="2026-04-06T13:35:00+08:00",
                pre_close=10.15,
            ),
        ]
        meeting_state = StateStore(tmp_path / "meeting_state.json")
        runtime_state = StateStore(tmp_path / "runtime_state.json")
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
                    "submitted_at": "2026-04-06T13:20:00",
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
        runtime_state.set(
            "latest_runtime_context",
            {
                "available": True,
                "resource": "runtime_context",
                "trade_date": "2026-04-06",
                "generated_at": "2026-04-06T14:05:00",
                "market_profile": {
                    "sentiment_phase": "主升",
                    "regime": "trend",
                    "allowed_playbooks": ["leader_chase"],
                },
                "playbook_contexts": [
                    {
                        "playbook": "leader_chase",
                        "symbol": "600519.SH",
                        "sector": "白酒",
                        "entry_window": "09:30-10:00",
                        "confidence": 0.94,
                        "rank_in_sector": 2,
                        "leader_score": 0.91,
                        "style_tag": "leader",
                        "exit_params": {
                            "atr_pct": 0.015,
                            "open_failure_minutes": 5,
                            "max_hold_minutes": 240,
                            "time_stop": "14:50",
                        },
                    },
                    {
                        "playbook": "leader_chase",
                        "symbol": "600036.SH",
                        "sector": "白酒",
                        "entry_window": "09:30-10:00",
                        "confidence": 0.88,
                        "rank_in_sector": 1,
                        "leader_score": 0.96,
                        "style_tag": "leader",
                        "exit_params": {
                            "atr_pct": 0.015,
                            "open_failure_minutes": 5,
                            "max_hold_minutes": 240,
                            "time_stop": "14:50",
                        },
                    },
                ],
            },
        )
        archive = DataArchiveStore(settings.storage_root)
        archive.persist_dossier_pack(
            {
                "pack_id": "dossier-tail-sector-relative-1",
                "trade_date": "2026-04-06",
                "generated_at": "2026-04-06T14:05:00",
                "expires_at": "2026-04-06T15:00:00",
                "signature": "tail-market-sector-relative",
                "items": [
                    {
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "resolved_sector": "白酒",
                        "assigned_playbook": "leader_chase",
                        "behavior_profile": {
                            "symbol": "600519.SH",
                            "optimal_hold_days": 1,
                            "style_tag": "leader",
                            "leader_frequency_30d": 0.68,
                            "avg_sector_rank_30d": 1.6,
                        },
                    },
                    {
                        "symbol": "600036.SH",
                        "name": "招商银行",
                        "resolved_sector": "白酒",
                        "assigned_playbook": "leader_chase",
                    },
                ],
            }
        )

        payload = run_tail_market_scan(
            settings=settings,
            market=market,
            execution_adapter=adapter,
            meeting_state_store=meeting_state,
            runtime_state_store=runtime_state,
            now_factory=lambda: datetime(2026, 4, 6, 14, 10, 0),
        )

        assert payload["status"] == "ok"
        assert payload["submitted_count"] == 1
        submitted = next(item for item in payload["items"] if item["status"] == "submitted")
        assert submitted["exit_reason"] == "time_stop"
        assert submitted["intraday_change_pct"] > 0
        assert submitted["sector_intraday_change_pct"] > submitted["intraday_change_pct"]
        assert submitted["sector_relative_strength_5m"] <= -0.015

    def test_tail_market_scan_uses_sector_relative_trend_weakness(self, tmp_path):
        settings = load_settings()
        settings.run_mode = "paper"
        settings.live_trade_enabled = False
        settings.storage_root = tmp_path / "state"
        settings.workspace = tmp_path
        settings.logs_dir = tmp_path / "logs"
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        settings.logs_dir.mkdir(parents=True, exist_ok=True)

        adapter = MockExecutionAdapter()
        adapter.positions["sim-001"] = [
            PositionSnapshot(
                account_id="sim-001",
                symbol="600519.SH",
                quantity=100,
                available=100,
                cost_price=10.0,
                last_price=10.10,
            )
        ]
        market = MockMarketDataAdapter()
        market.get_snapshots = lambda symbols: [
            QuoteSnapshot(
                symbol="600519.SH",
                name="贵州茅台",
                last_price=10.10,
                bid_price=10.09,
                ask_price=10.11,
                volume=250_000,
                pre_close=10.0,
            )
        ]
        market.get_bars = lambda symbols, period, count=1: [
            BarSnapshot(
                symbol="600519.SH",
                period="5m",
                open=10.00,
                high=10.05,
                low=9.99,
                close=10.04,
                volume=120_000,
                amount=1_200_000,
                trade_time="2026-04-06T13:30:00+08:00",
                pre_close=10.0,
            ),
            BarSnapshot(
                symbol="600519.SH",
                period="5m",
                open=10.04,
                high=10.07,
                low=10.03,
                close=10.06,
                volume=115_000,
                amount=1_150_000,
                trade_time="2026-04-06T13:35:00+08:00",
                pre_close=10.04,
            ),
            BarSnapshot(
                symbol="600519.SH",
                period="5m",
                open=10.06,
                high=10.11,
                low=10.05,
                close=10.10,
                volume=130_000,
                amount=1_300_000,
                trade_time="2026-04-06T13:40:00+08:00",
                pre_close=10.06,
            ),
            BarSnapshot(
                symbol="600036.SH",
                period="5m",
                open=10.00,
                high=10.06,
                low=9.99,
                close=10.05,
                volume=150_000,
                amount=1_500_000,
                trade_time="2026-04-06T13:30:00+08:00",
                pre_close=10.0,
            ),
            BarSnapshot(
                symbol="600036.SH",
                period="5m",
                open=10.05,
                high=10.13,
                low=10.04,
                close=10.12,
                volume=175_000,
                amount=1_750_000,
                trade_time="2026-04-06T13:35:00+08:00",
                pre_close=10.05,
            ),
            BarSnapshot(
                symbol="600036.SH",
                period="5m",
                open=10.12,
                high=10.23,
                low=10.11,
                close=10.22,
                volume=210_000,
                amount=2_100_000,
                trade_time="2026-04-06T13:40:00+08:00",
                pre_close=10.12,
            ),
        ]
        meeting_state = StateStore(tmp_path / "meeting_state.json")
        runtime_state = StateStore(tmp_path / "runtime_state.json")
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
                    "submitted_at": "2026-04-06T13:20:00",
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
        runtime_state.set(
            "latest_runtime_context",
            {
                "available": True,
                "resource": "runtime_context",
                "trade_date": "2026-04-06",
                "generated_at": "2026-04-06T14:05:00",
                "market_profile": {
                    "sentiment_phase": "主升",
                    "regime": "trend",
                    "allowed_playbooks": ["leader_chase"],
                },
                "playbook_contexts": [
                    {
                        "playbook": "leader_chase",
                        "symbol": "600519.SH",
                        "sector": "白酒",
                        "entry_window": "09:30-10:00",
                        "confidence": 0.94,
                        "rank_in_sector": 2,
                        "leader_score": 0.91,
                        "style_tag": "leader",
                        "exit_params": {
                            "atr_pct": 0.015,
                            "open_failure_minutes": 5,
                            "max_hold_minutes": 240,
                            "time_stop": "14:50",
                        },
                    },
                    {
                        "playbook": "leader_chase",
                        "symbol": "600036.SH",
                        "sector": "白酒",
                        "entry_window": "09:30-10:00",
                        "confidence": 0.89,
                        "rank_in_sector": 1,
                        "leader_score": 0.95,
                        "style_tag": "leader",
                        "exit_params": {
                            "atr_pct": 0.015,
                            "open_failure_minutes": 5,
                            "max_hold_minutes": 240,
                            "time_stop": "14:50",
                        },
                    },
                ],
            },
        )
        archive = DataArchiveStore(settings.storage_root)
        archive.persist_dossier_pack(
            {
                "pack_id": "dossier-tail-sector-trend-1",
                "trade_date": "2026-04-06",
                "generated_at": "2026-04-06T14:05:00",
                "expires_at": "2026-04-06T15:00:00",
                "signature": "tail-market-sector-trend",
                "items": [
                    {
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "resolved_sector": "白酒",
                        "assigned_playbook": "leader_chase",
                        "behavior_profile": {
                            "symbol": "600519.SH",
                            "optimal_hold_days": 1,
                            "style_tag": "leader",
                            "leader_frequency_30d": 0.68,
                            "avg_sector_rank_30d": 1.6,
                        },
                    },
                    {
                        "symbol": "600036.SH",
                        "name": "招商银行",
                        "resolved_sector": "白酒",
                        "assigned_playbook": "leader_chase",
                    },
                ],
            }
        )

        payload = run_tail_market_scan(
            settings=settings,
            market=market,
            execution_adapter=adapter,
            meeting_state_store=meeting_state,
            runtime_state_store=runtime_state,
            now_factory=lambda: datetime(2026, 4, 6, 14, 10, 0),
        )

        assert payload["status"] == "ok"
        assert payload["submitted_count"] == 1
        submitted = next(item for item in payload["items"] if item["status"] == "submitted")
        assert submitted["exit_reason"] == "time_stop"
        assert -0.015 < submitted["sector_relative_strength_5m"] < 0
        assert submitted["sector_underperform_bars_5m"] >= 2
        assert submitted["sector_relative_trend_5m"] <= -0.01
        journal = meeting_state.get("execution_order_journal", [])
        sell_journal = journal[-1]
        assert sell_journal["exit_context_snapshot"]["sector_underperform_bars_5m"] >= 2
        assert sell_journal["exit_context_snapshot"]["sector_relative_trend_5m"] <= -0.01
        assert "sector_relative_trend_weak" in sell_journal["review_tags"]


class TestStartupRecovery:
    def test_recovery_syncs_existing_order_status_into_journal(self, tmp_path):
        adapter = MockExecutionAdapter()
        order = adapter.place_order(
            PlaceOrderRequest(
                account_id="sim-001",
                symbol="000001.SZ",
                side="BUY",
                quantity=100,
                price=10.0,
                request_id="req-recover-1",
            )
        )
        state_store = StateStore(tmp_path / "meeting_state.json")
        state_store.set(
            "execution_order_journal",
            [
                {
                    "order_id": order.order_id,
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "submitted_at": "2026-04-06T10:00:00",
                }
            ],
        )
        service = StartupRecoveryService(adapter, state_store, now_factory=lambda: datetime(2026, 4, 6, 10, 5, 0))

        payload = service.recover("sim-001", persist=True)

        assert payload["status"] == "ok"
        assert payload["order_count"] >= 1
        assert payload["pending_count"] >= 1
        journal = state_store.get("execution_order_journal")
        assert journal[0]["latest_status"] == "PENDING"
        assert state_store.get("latest_startup_recovery")["status"] == "ok"


class TestExecutionReconciliation:
    def test_reconciliation_aggregates_trade_fill_and_balance_snapshot(self, tmp_path):
        adapter = MockExecutionAdapter()
        order = adapter.place_order(
            PlaceOrderRequest(
                account_id="sim-001",
                symbol="000001.SZ",
                side="BUY",
                quantity=100,
                price=10.0,
                request_id="req-reconcile-1",
            )
        )
        adapter.trades["sim-001"] = [
            TradeSnapshot(
                trade_id="trade-1",
                order_id=order.order_id,
                account_id="sim-001",
                symbol="000001.SZ",
                side="BUY",
                quantity=100,
                price=10.05,
            )
        ]
        state_store = StateStore(tmp_path / "meeting_state.json")
        state_store.set(
            "execution_order_journal",
            [
                {
                    "order_id": order.order_id,
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "decision_id": "case-1",
                    "trade_date": "2026-04-06",
                    "submitted_at": "2026-04-06T10:00:00",
                }
            ],
        )
        service = ExecutionReconciliationService(adapter, state_store, now_factory=lambda: datetime(2026, 4, 6, 10, 5, 0))

        payload = service.reconcile("sim-001", persist=True)

        assert payload["status"] == "ok"
        assert payload["matched_order_count"] == 1
        assert payload["filled_order_count"] == 1
        assert payload["trade_count"] == 1
        assert payload["items"][0]["filled_quantity"] == 100
        assert payload["items"][0]["avg_fill_price"] == 10.05
        assert payload["balance"]["account_id"] == "sim-001"
        assert state_store.get("latest_execution_reconciliation")["status"] == "ok"


class TestPendingOrderRemediation:
    def test_remediation_can_cancel_stale_pending_order(self, tmp_path):
        clock = [datetime(2026, 4, 6, 10, 5, 0)]
        adapter = MockExecutionAdapter()
        order = adapter.place_order(
            PlaceOrderRequest(
                account_id="sim-001",
                symbol="000001.SZ",
                side="BUY",
                quantity=100,
                price=10.0,
                request_id="req-remediate-1",
            )
        )
        state_store = StateStore(tmp_path / "meeting_state.json")
        state_store.set(
            "execution_order_journal",
            [
                {
                    "order_id": order.order_id,
                    "symbol": "000001.SZ",
                    "name": "平安银行",
                    "trade_date": "2026-04-06",
                    "decision_id": "case-1",
                    "submitted_at": "2026-04-06T09:40:00",
                }
            ],
        )
        inspection = PendingOrderInspectionService(adapter, state_store, now_factory=lambda: clock[0])
        service = PendingOrderRemediationService(adapter, state_store, inspection, now_factory=lambda: clock[0])

        payload = service.remediate("sim-001", auto_action="cancel", cancel_after_seconds=600, persist=True)

        assert payload["status"] == "actioned"
        assert payload["stale_count"] == 1
        assert payload["actioned_count"] == 1
        assert payload["cancelled_count"] == 1
        assert payload["receipts"][0]["status"] == "cancel_submitted"
        assert adapter.get_order("sim-001", order.order_id).status == "CANCELLED"
        assert state_store.get("latest_pending_order_remediation")["status"] == "actioned"


class TestAccountState:
    def test_account_state_tracks_daily_pnl_against_session_baseline(self, tmp_path):
        settings = load_settings()
        state_store = StateStore(tmp_path / "meeting_state.json")
        adapter = MockExecutionAdapter()
        service = AccountStateService(
            settings,
            adapter,
            state_store,
            now_factory=lambda: datetime(2026, 4, 6, 10, 0, 0),
        )

        first = service.snapshot("sim-001", persist=True)
        assert first["status"] == "ok"
        assert first["verified"] is True
        assert first["metrics"]["daily_pnl"] == 0.0

        adapter.balances["sim-001"] = BalanceSnapshot(account_id="sim-001", total_asset=1_002_000, cash=602_000)
        second = service.snapshot("sim-001", persist=True)
        assert second["metrics"]["daily_pnl"] == 2000.0
        assert second["metrics"]["cash"] == 602000.0
        assert state_store.get("latest_account_state")["metrics"]["daily_pnl"] == 2000.0


class TestPrecomputePolicy:
    def test_refresh_if_due_skips_while_fresh(self, tmp_path):
        clock = [datetime(2026, 4, 4, 10, 0, 0)]
        settings = load_settings()
        settings.workspace = tmp_path
        settings.storage_root = tmp_path / "state"
        settings.logs_dir = tmp_path / "logs"
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        config_mgr = RuntimeConfigManager(tmp_path / "runtime_config.json")
        config_mgr.update(
            **{
                "watch.candidate_poll_seconds": 300,
                "watch.heartbeat_save_seconds": 300,
                "snapshots.market_snapshot_ttl_seconds": 300,
            }
        )
        runtime_state = StateStore(tmp_path / "runtime_state.json")
        research_state = StateStore(tmp_path / "research_state.json")
        case_service = CandidateCaseService(tmp_path / "candidate_cases.json", now_factory=lambda: clock[0])
        market = MockMarketDataAdapter()
        service = DossierPrecomputeService(
            settings=settings,
            market_adapter=market,
            research_state_store=research_state,
            runtime_state_store=runtime_state,
            candidate_case_service=case_service,
            config_mgr=config_mgr,
            now_factory=lambda: clock[0],
        )

        report = {
            "job_id": "runtime-1",
            "generated_at": "2026-04-04T10:00:00",
            "top_picks": [
                {"symbol": "600519.SH", "name": "贵州茅台", "rank": 1, "selection_score": 90.0, "action": "BUY", "summary": "强势"},
                {"symbol": "000001.SZ", "name": "平安银行", "rank": 2, "selection_score": 80.0, "action": "BUY", "summary": "跟随"},
            ],
            "summary": {"buy_count": 2, "hold_count": 0},
        }
        runtime_state.set("latest_runtime_report", report)
        case_service.sync_from_runtime_report(report, focus_pool_capacity=10, execution_pool_capacity=3)

        first = service.refresh_if_due(trigger="test")
        second = service.refresh_if_due(trigger="test")

        assert first["refreshed"] is True
        assert first["reason"] == "missing"
        assert second["refreshed"] is False
        assert second["reason"] == "fresh"

    def test_refresh_if_due_respects_poll_interval_before_signature_change_refresh(self, tmp_path):
        clock = [datetime(2026, 4, 4, 10, 0, 0)]
        settings = load_settings()
        settings.workspace = tmp_path
        settings.storage_root = tmp_path / "state"
        settings.logs_dir = tmp_path / "logs"
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        config_mgr = RuntimeConfigManager(tmp_path / "runtime_config.json")
        config_mgr.update(
            **{
                "watch.candidate_poll_seconds": 300,
                "snapshots.market_snapshot_ttl_seconds": 600,
            }
        )
        runtime_state = StateStore(tmp_path / "runtime_state.json")
        research_state = StateStore(tmp_path / "research_state.json")
        case_service = CandidateCaseService(tmp_path / "candidate_cases.json", now_factory=lambda: clock[0])
        market = MockMarketDataAdapter()
        service = DossierPrecomputeService(
            settings=settings,
            market_adapter=market,
            research_state_store=research_state,
            runtime_state_store=runtime_state,
            candidate_case_service=case_service,
            config_mgr=config_mgr,
            now_factory=lambda: clock[0],
        )

        first_report = {
            "job_id": "runtime-1",
            "generated_at": "2026-04-04T10:00:00",
            "top_picks": [
                {"symbol": "600519.SH", "name": "贵州茅台", "rank": 1, "selection_score": 90.0, "action": "BUY", "summary": "强势"},
                {"symbol": "000001.SZ", "name": "平安银行", "rank": 2, "selection_score": 80.0, "action": "BUY", "summary": "跟随"},
            ],
            "summary": {"buy_count": 2, "hold_count": 0},
        }
        runtime_state.set("latest_runtime_report", first_report)
        case_service.sync_from_runtime_report(first_report, focus_pool_capacity=10, execution_pool_capacity=3)
        service.refresh_if_due(trigger="test")

        second_report = {
            "job_id": "runtime-2",
            "generated_at": "2026-04-04T10:01:00",
            "top_picks": [
                {"symbol": "600036.SH", "name": "招商银行", "rank": 1, "selection_score": 91.0, "action": "BUY", "summary": "新候选"},
                {"symbol": "000001.SZ", "name": "平安银行", "rank": 2, "selection_score": 80.0, "action": "BUY", "summary": "跟随"},
            ],
            "summary": {"buy_count": 2, "hold_count": 0},
        }
        clock[0] = datetime(2026, 4, 4, 10, 1, 0)
        runtime_state.set("latest_runtime_report", second_report)
        case_service.sync_from_runtime_report(second_report, focus_pool_capacity=10, execution_pool_capacity=3)

        early = service.refresh_if_due(trigger="test")
        assert early["refreshed"] is False
        assert early["reason"] == "poll_interval"

        clock[0] = datetime(2026, 4, 4, 10, 5, 1)
        later = service.refresh_if_due(trigger="test")
        assert later["refreshed"] is True
        assert later["reason"] == "signature_changed"


class TestContainer:
    def test_get_settings(self):
        reset_container()
        s = get_settings()
        assert s.app_name == "ashare-system-v2"

    def test_get_adapters(self):
        reset_container()
        e = get_execution_adapter()
        m = get_market_adapter()
        assert e is not None
        assert m is not None
