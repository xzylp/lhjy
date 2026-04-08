"""退出监控器测试。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from ashare_system.contracts import ExitContext, QuoteSnapshot
from ashare_system.infra.audit_store import StateStore
from ashare_system.monitor.exit_monitor import ExitMonitor
from ashare_system.monitor.market_watcher import MarketWatcher
from ashare_system.monitor.persistence import (
    MonitorStateService,
    build_execution_bridge_health_client_template,
    build_execution_bridge_health_deployment_contract_sample,
    build_execution_bridge_health_ingress_payload,
    get_execution_bridge_health_latest_descriptor,
)


def make_quote(symbol: str = "600519.SH", last_price: float = 101.0) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=symbol,
        name="测试标的",
        last_price=last_price,
        bid_price=last_price - 0.01,
        ask_price=last_price + 0.01,
        volume=500_000,
        pre_close=100.0,
    )


def make_context(symbol: str = "600519.SH", **overrides) -> ExitContext:
    payload = {
        "symbol": symbol,
        "playbook": "leader_chase",
        "entry_price": 100.0,
        "entry_time": "2026-04-07T09:35:00",
        "sector_name": "白酒",
        "is_limit_up": False,
        "is_bomb": False,
        "sector_retreat": False,
        "relative_strength_5m": 0.01,
        "intraday_change_pct": 0.01,
        "intraday_drawdown_pct": -0.01,
        "rebound_from_low_pct": 0.03,
        "negative_alert_count": 0,
        "sector_relative_trend_5m": 0.01,
        "sector_underperform_bars_5m": 0,
        "exit_params": {"soft_stop_loss_pct": 0.03},
    }
    payload.update(overrides)
    return ExitContext(**payload)


class FakeMarketAdapter:
    def __init__(self, quotes: list[QuoteSnapshot]) -> None:
        self._quotes = {quote.symbol: quote for quote in quotes}

    def get_snapshots(self, symbols: list[str]) -> list[QuoteSnapshot]:
        return [self._quotes[symbol] for symbol in symbols if symbol in self._quotes]


class TestExitMonitor:
    def test_execution_bridge_deployment_contract_sample_helper(self) -> None:
        sample = build_execution_bridge_health_deployment_contract_sample(api_base_url="http://127.0.0.1:8100")

        assert sample["request_samples"]["windows_gateway_minimal_post_body"]["trigger"] == "windows_gateway"
        assert sample["request_samples"]["windows_gateway_minimal_post_body"]["health"]["source_id"] == "windows-vm-a"
        assert (
            sample["request_samples"]["windows_gateway_primary_post_body"]["health"]["deployment_role"]
            == "primary_gateway"
        )
        assert (
            sample["request_samples"]["windows_gateway_backup_post_body"]["health"]["deployment_role"]
            == "backup_gateway"
        )
        assert (
            sample["request_samples"]["windows_gateway_backup_post_body"]["health"]["bridge_path"]
            == "linux_openclaw -> windows_gateway_backup -> qmt_vm"
        )
        assert sample["http_samples"]["windows_gateway_post"]["path"] == "/system/monitor/execution-bridge-health"
        assert "curl -X POST \"http://127.0.0.1:8100/system/monitor/execution-bridge-health\"" in sample[
            "http_samples"
        ]["curl_post_example"]
        assert sample["source_value_samples"]["windows_gateway_backup"]["source_id"] == "windows-vm-b"

    def test_execution_bridge_deployment_contract_sample_aligns_with_latest_descriptor(self) -> None:
        descriptor = get_execution_bridge_health_latest_descriptor()
        sample = build_execution_bridge_health_deployment_contract_sample()
        latest_read = sample["read_samples"]["linux_latest_read_example"]
        trend_read = sample["read_samples"]["linux_trend_read_example"]

        assert latest_read["root_key"] == descriptor["latest_execution_bridge_health"]["root_key"]
        assert trend_read["root_key"] == descriptor["execution_bridge_health_trend_summary"]["root_key"]
        assert latest_read["recommended_fields"] == descriptor["latest_execution_bridge_health"]["recommended_fields"]
        assert (
            trend_read["recommended_fields"]
            == descriptor["execution_bridge_health_trend_summary"]["recommended_fields"]
        )
        assert latest_read["example_values"]["source_id"] == "windows-vm-a"
        assert trend_read["example_values"]["latest_deployment_role"] == "primary_gateway"
        assert trend_read["example_values"]["health_trend_snapshot"]["latest_qmt_vm_status"] == "healthy"

    def test_execution_bridge_client_template_exposes_minimal_request_body(self) -> None:
        template = build_execution_bridge_health_client_template(
            source_id="windows-vm-a",
            deployment_role="primary_gateway",
            bridge_path="linux_openclaw -> windows_gateway -> qmt_vm",
        )

        assert template["method"] == "POST"
        assert template["path"] == "/system/monitor/execution-bridge-health"
        assert template["content_type"] == "application/json"
        assert template["minimal_request_body"]["trigger"] == "windows_gateway"
        assert template["minimal_request_body"]["health"]["source_id"] == "windows-vm-a"
        assert template["minimal_request_body"]["health"]["deployment_role"] == "primary_gateway"
        assert template["minimal_request_body"]["health"]["bridge_path"] == "linux_openclaw -> windows_gateway -> qmt_vm"
        assert template["top_level_health_defaults"]["overall_status"] == "unknown"
        assert template["top_level_health_defaults"]["gateway_online"] is False
        assert template["latest_read_descriptor"]["latest_execution_bridge_health"]["root_key"] == "latest_execution_bridge_health"
        assert template["source_value_suggestions"]["windows_gateway"]["source_id"] == "windows-vm-a"

    def test_execution_bridge_latest_descriptor_exposes_recommended_read_keys(self) -> None:
        descriptor = get_execution_bridge_health_latest_descriptor()

        assert (
            descriptor["latest_execution_bridge_health"]["recommended_fields"]["source_id"]
            == "latest_execution_bridge_health.health.source_id"
        )
        assert (
            descriptor["execution_bridge_health_trend_summary"]["recommended_fields"]["latest_bridge_path"]
            == "execution_bridge_health_trend_summary.latest_bridge_path"
        )
        assert descriptor["source_value_suggestions"]["windows_gateway_primary"]["deployment_role"] == "primary_gateway"
        assert descriptor["source_value_suggestions"]["windows_gateway_backup"]["bridge_path"] == (
            "linux_openclaw -> windows_gateway_backup -> qmt_vm"
        )

    def test_execution_bridge_ingress_payload_helper_minimal_contract(self) -> None:
        ingress_payload = build_execution_bridge_health_ingress_payload(
            source_id="windows-vm-a",
            deployment_role="primary_gateway",
            bridge_path="linux_openclaw -> windows_gateway -> qmt_vm",
        )
        health = ingress_payload["health"]

        assert ingress_payload["trigger"] == "windows_gateway"
        assert health["version"] == "v1"
        assert health["reported_at"] == ""
        assert health["source_id"] == "windows-vm-a"
        assert health["deployment_role"] == "primary_gateway"
        assert health["bridge_path"] == "linux_openclaw -> windows_gateway -> qmt_vm"
        assert health["gateway_online"] is False
        assert health["qmt_connected"] is False
        assert health["windows_execution_gateway"]["key"] == "windows_execution_gateway"
        assert health["qmt_vm"]["key"] == "qmt_vm"
        assert [item["key"] for item in health["component_health"]] == ["windows_execution_gateway", "qmt_vm"]

    def test_execution_bridge_ingress_payload_helper_supports_health_override(self) -> None:
        ingress_payload = build_execution_bridge_health_ingress_payload(
            trigger="remote_poll",
            health={
                "reported_at": "2026-04-08T10:00:00+08:00",
                "source_id": "windows-vm-b",
                "deployment_role": "backup_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway_backup -> qmt_vm",
                "gateway_online": True,
                "qmt_connected": True,
                "windows_execution_gateway": {"status": "healthy", "reachable": True, "latency_ms": 8.5},
                "qmt_vm": {"status": "healthy", "reachable": True, "latency_ms": 15.0},
            },
        )
        health = ingress_payload["health"]

        assert ingress_payload["trigger"] == "remote_poll"
        assert health["reported_at"] == "2026-04-08T10:00:00+08:00"
        assert health["source_id"] == "windows-vm-b"
        assert health["deployment_role"] == "backup_gateway"
        assert health["bridge_path"] == "linux_openclaw -> windows_gateway_backup -> qmt_vm"
        assert health["gateway_online"] is True
        assert health["qmt_connected"] is True
        assert health["windows_execution_gateway"]["latency_ms"] == 8.5
        assert health["qmt_vm"]["latency_ms"] == 15.0

    def test_monitor_state_service_exposes_empty_latest_exit_snapshot_contract(self, tmp_path: Path) -> None:
        state_service = MonitorStateService(state_store=StateStore(tmp_path / "monitor_state.json"))

        latest_exit = state_service.get_latest_exit_snapshot()

        assert latest_exit["snapshot_id"] == ""
        assert latest_exit["generated_at"] == ""
        assert latest_exit["trigger"] == ""
        assert latest_exit["snapshot"]["version"] == "v1"
        assert latest_exit["snapshot"]["signal_count"] == 0
        assert latest_exit["snapshot"]["by_symbol"] == []
        assert latest_exit["snapshot"]["by_reason"] == []
        assert latest_exit["snapshot"]["by_severity"] == []
        assert latest_exit["snapshot"]["by_tag"] == []
        assert latest_exit["snapshot"]["summary_lines"] == ["当前无退出监控信号。"]

    def test_monitor_state_service_exposes_empty_execution_bridge_health_contract(self, tmp_path: Path) -> None:
        state_service = MonitorStateService(state_store=StateStore(tmp_path / "monitor_state.json"))

        latest_health = state_service.get_latest_execution_bridge_health()
        state = state_service.get_state()
        trend = state["execution_bridge_health_trend_summary"]

        assert latest_health["health_id"] == ""
        assert latest_health["generated_at"] == ""
        assert latest_health["trigger"] == ""
        assert latest_health["health"]["version"] == "v1"
        assert latest_health["health"]["overall_status"] == "unknown"
        assert latest_health["health"]["reported_at"] == ""
        assert latest_health["health"]["source_id"] == ""
        assert latest_health["health"]["deployment_role"] == ""
        assert latest_health["health"]["bridge_path"] == ""
        assert latest_health["health"]["gateway_online"] is False
        assert latest_health["health"]["qmt_connected"] is False
        assert latest_health["health"]["attention_components"] == []
        assert latest_health["health"]["attention_component_keys"] == []
        assert latest_health["health"]["windows_execution_gateway"]["label"] == "Windows Execution Gateway"
        assert latest_health["health"]["qmt_vm"]["label"] == "QMT VM"
        assert [item["key"] for item in latest_health["health"]["component_health"]] == [
            "windows_execution_gateway",
            "qmt_vm",
        ]
        assert latest_health["health"]["summary_lines"] == ["Windows Execution Gateway 健康快照缺失。"]
        assert state["latest_execution_bridge_health"] == latest_health
        assert state["execution_bridge_health_history"] == []
        assert trend["available"] is False
        assert trend["snapshot_count"] == 0
        assert trend["latest_reported_at"] == ""
        assert trend["latest_source_id"] == ""
        assert trend["latest_deployment_role"] == ""
        assert trend["latest_bridge_path"] == ""
        assert trend["latest_overall_status"] == "unknown"
        assert trend["overall_status_counts"] == {"healthy": 0, "degraded": 0, "down": 0, "unknown": 0}
        assert trend["windows_execution_gateway"]["latest_status"] == "unknown"
        assert trend["qmt_vm"]["latest_status"] == "unknown"
        assert trend["attention_ratio"] == 0.0
        assert trend["component_trends"][0]["key"] == "windows_execution_gateway"
        assert trend["component_trends"][1]["key"] == "qmt_vm"
        assert trend["health_trend_snapshot"]["latest_source_id"] == ""

    def test_execution_bridge_health_remote_reporting_fields_and_legacy_compat(self, tmp_path: Path) -> None:
        state_service = MonitorStateService(state_store=StateStore(tmp_path / "monitor_state.json"))

        state_service.save_execution_bridge_health(
            {
                "reported_at": "2026-04-08T09:30:15+08:00",
                "source_id": "qmt-gateway-vm-01",
                "deployment_role": "windows_vm_gateway",
                "bridge_path": "linux_openclaw->windows_vm->qmt",
                "gateway_online": True,
                "qmt_connected": True,
            }
        )

        latest = state_service.get_latest_execution_bridge_health()
        state = state_service.get_state()

        assert latest["health"]["reported_at"] == "2026-04-08T09:30:15+08:00"
        assert latest["health"]["source_id"] == "qmt-gateway-vm-01"
        assert latest["health"]["deployment_role"] == "windows_vm_gateway"
        assert latest["health"]["bridge_path"] == "linux_openclaw->windows_vm->qmt"
        assert state["execution_bridge_health_history"][-1]["reported_at"] == "2026-04-08T09:30:15+08:00"
        assert state["execution_bridge_health_history"][-1]["source_id"] == "qmt-gateway-vm-01"
        assert state["execution_bridge_health_history"][-1]["deployment_role"] == "windows_vm_gateway"
        assert state["execution_bridge_health_history"][-1]["bridge_path"] == "linux_openclaw->windows_vm->qmt"

        state_service.save_execution_bridge_health(
            {
                "gateway_online": True,
                "qmt_connected": False,
            }
        )
        legacy_latest = state_service.get_latest_execution_bridge_health()
        assert legacy_latest["health"]["reported_at"] == ""
        assert legacy_latest["health"]["source_id"] == ""
        assert legacy_latest["health"]["deployment_role"] == ""
        assert legacy_latest["health"]["bridge_path"] == ""

    def test_build_execution_bridge_health_ingress_payload_matches_system_api_envelope(self, tmp_path: Path) -> None:
        state_service = MonitorStateService(state_store=StateStore(tmp_path / "monitor_state.json"))

        ingress = build_execution_bridge_health_ingress_payload(
            {
                "updated_at": "2026-04-08T09:31:00+08:00",
                "gateway_online": True,
                "qmt_connected": False,
                "attention_components": ["QMT VM"],
                "windows_execution_gateway": {"status": "healthy", "reachable": True, "latency_ms": 18.0},
                "qmt_vm": {"status": "down", "reachable": False, "latency_ms": 0.0, "detail": "qmt disconnected"},
            },
            trigger="windows_gateway",
            source_id="qmt-gateway-vm-02",
            deployment_role="backup_gateway",
            bridge_path="linux_openclaw -> windows_gateway_backup -> qmt_vm",
        )

        assert ingress["trigger"] == "windows_gateway"
        assert ingress["health"]["version"] == "v1"
        assert ingress["health"]["reported_at"] == "2026-04-08T09:31:00+08:00"
        assert ingress["health"]["source_id"] == "qmt-gateway-vm-02"
        assert ingress["health"]["deployment_role"] == "backup_gateway"
        assert ingress["health"]["bridge_path"] == "linux_openclaw -> windows_gateway_backup -> qmt_vm"
        assert ingress["health"]["attention_components"] == ["QMT VM"]
        assert ingress["health"]["attention_component_keys"] == ["qmt_vm"]
        assert ingress["health"]["component_health"][0]["key"] == "windows_execution_gateway"
        assert ingress["health"]["component_health"][1]["key"] == "qmt_vm"

        latest = state_service.save_execution_bridge_health(ingress["health"], trigger=ingress["trigger"])

        assert latest["trigger"] == "windows_gateway"
        assert latest["health"]["source_id"] == "qmt-gateway-vm-02"
        assert latest["health"]["deployment_role"] == "backup_gateway"
        assert latest["health"]["bridge_path"] == "linux_openclaw -> windows_gateway_backup -> qmt_vm"

    def test_market_watcher_exposes_empty_exit_snapshot_before_checks(self) -> None:
        watcher = MarketWatcher()
        snapshot = watcher.get_latest_exit_snapshot()

        assert snapshot["version"] == "v1"
        assert snapshot["checked_at"] == 0.0
        assert snapshot["signal_count"] == 0
        assert snapshot["by_symbol"] == []
        assert snapshot["by_reason"] == []
        assert snapshot["by_severity"] == []
        assert snapshot["by_tag"] == []
        assert snapshot["items"] == []
        assert snapshot["summary_lines"] == ["当前无退出监控信号。"]

    def test_check_returns_empty_for_calm_context(self) -> None:
        monitor = ExitMonitor()
        signals = monitor.check(make_quote(), make_context())
        assert signals == []

    def test_check_emits_minimal_exit_signals(self) -> None:
        monitor = ExitMonitor()
        signals = monitor.check(
            make_quote(last_price=96.0),
            make_context(
                is_bomb=True,
                rebound_from_low_pct=0.005,
                sector_retreat=True,
                negative_alert_count=3,
                intraday_change_pct=-0.04,
                intraday_drawdown_pct=-0.05,
                sector_relative_trend_5m=-0.03,
                sector_underperform_bars_5m=4,
            ),
            board_snapshot={"reseal_count": 0},
        )
        assert len(signals) >= 3
        assert signals[0].signal_type == "bomb_exit"
        assert signals[0].reason == "board_break"
        assert signals[0].severity == "IMMEDIATE"
        assert "board_break" in signals[0].tags
        assert "micro_rebound_failed" in signals[0].tags
        signal_types = {item.signal_type for item in signals}
        assert "sector_retreat_exit" in signal_types
        assert "soft_stop_loss_exit" in signal_types

    def test_check_batch_and_summary(self) -> None:
        monitor = ExitMonitor()
        snapshots = [make_quote("600519.SH", last_price=96.0), make_quote("000001.SZ", last_price=98.0)]
        contexts = {
            "600519.SH": make_context(
                "600519.SH",
                is_bomb=True,
                rebound_from_low_pct=0.005,
                sector_retreat=True,
                negative_alert_count=3,
                intraday_change_pct=-0.04,
                intraday_drawdown_pct=-0.05,
                sector_relative_trend_5m=-0.03,
                sector_underperform_bars_5m=4,
            ),
            "000001.SZ": make_context(
                "000001.SZ",
                sector_retreat=True,
                sector_relative_trend_5m=-0.02,
                sector_underperform_bars_5m=4,
            ),
        }
        signals = monitor.check_batch(snapshots, contexts, {"600519.SH": {"reseal_count": 0}})
        summary = monitor.summarize(signals)
        assert summary["count"] >= 5
        assert "600519.SH" in summary["symbols"]
        assert summary["by_symbol"][0]["key"] == "600519.SH"
        assert {item["key"] for item in summary["by_reason"]} >= {"board_break", "sector_retreat", "time_stop"}
        assert any(item["key"] == "IMMEDIATE" for item in summary["by_severity"])
        assert any(item["key"] == "negative_alert" for item in summary["by_tag"])
        assert summary["summary_lines"]
        assert any("主要原因" in line for line in summary["summary_lines"])

    def test_market_watcher_exposes_exit_monitor_signals(self) -> None:
        watcher = MarketWatcher(
            market_adapter=FakeMarketAdapter([make_quote(last_price=96.0)]),
            exit_monitor=ExitMonitor(),
            exit_context_provider={
                "600519.SH": make_context(
                    is_bomb=True,
                    rebound_from_low_pct=0.005,
                    sector_retreat=True,
                    negative_alert_count=3,
                    intraday_change_pct=-0.04,
                    intraday_drawdown_pct=-0.05,
                    sector_relative_trend_5m=-0.03,
                    sector_underperform_bars_5m=4,
                )
            },
            board_snapshot_provider={"600519.SH": {"reseal_count": 0}},
        )
        watcher.add_symbols(["600519.SH"])

        alerts = watcher.check_once()
        signals = watcher.get_latest_exit_signals()
        summary = watcher.get_latest_exit_summary()
        snapshot = watcher.get_latest_exit_snapshot()

        assert isinstance(alerts, list)
        assert watcher.state.last_exit_signal_count >= 3
        assert signals[0]["symbol"] == "600519.SH"
        assert signals[0]["signal_type"] == "bomb_exit"
        assert signals[0]["reason"] == "board_break"
        assert "board_break" in signals[0]["tags"]
        assert summary["by_symbol"][0]["key"] == "600519.SH"
        assert any(item["key"] == "board_break" for item in summary["by_reason"])
        assert any("退出监控共" in line for line in summary["summary_lines"])
        assert snapshot["version"] == "v1"
        assert snapshot["checked_at"] == watcher.state.last_check_time
        assert snapshot["signal_count"] == watcher.state.last_exit_signal_count
        assert snapshot["watched_symbols"] == ["600519.SH"]
        assert snapshot["by_symbol"] == summary["by_symbol"]
        assert snapshot["by_reason"] == summary["by_reason"]
        assert snapshot["by_severity"] == summary["by_severity"]
        assert snapshot["by_tag"] == summary["by_tag"]
        assert snapshot["items"] == summary["items"]

    def test_market_watcher_persists_latest_exit_snapshot_into_state_service(self, tmp_path: Path) -> None:
        state_service = MonitorStateService(state_store=StateStore(tmp_path / "monitor_state.json"))
        watcher = MarketWatcher(
            market_adapter=FakeMarketAdapter([make_quote(last_price=96.0)]),
            state_service=state_service,
            exit_monitor=ExitMonitor(),
            exit_context_provider={
                "600519.SH": make_context(
                    is_bomb=True,
                    rebound_from_low_pct=0.005,
                    sector_retreat=True,
                    negative_alert_count=3,
                    intraday_change_pct=-0.04,
                    intraday_drawdown_pct=-0.05,
                    sector_relative_trend_5m=-0.03,
                    sector_underperform_bars_5m=4,
                )
            },
            board_snapshot_provider={"600519.SH": {"reseal_count": 0}},
        )
        watcher.add_symbols(["600519.SH"])

        watcher.check_once()
        state = state_service.get_state()
        latest_exit = state["latest_exit_snapshot"]
        stable_latest_exit = state_service.get_latest_exit_snapshot()

        assert latest_exit is not None
        assert latest_exit["trigger"] == "market_watcher"
        assert latest_exit["snapshot"]["version"] == "v1"
        assert latest_exit["snapshot"]["signal_count"] >= 1
        assert latest_exit["snapshot"]["watched_symbols"] == ["600519.SH"]
        assert latest_exit["snapshot"]["by_reason"]
        assert stable_latest_exit == latest_exit
        assert state["exit_snapshot_history"]
        assert state["exit_snapshot_history"][-1]["signal_count"] >= 1
        assert isinstance(state["exit_snapshot_history"][-1]["reason_counts"], dict)
        assert state["exit_snapshot_trend_summary"]["available"] is True
        assert state["latest_execution_bridge_health"]["health"]["version"] == "v1"
        assert state["execution_bridge_health_trend_summary"]["available"] is False

    def test_exit_snapshot_trend_summary_aggregates_recent_history(self, tmp_path: Path) -> None:
        state_service = MonitorStateService(state_store=StateStore(tmp_path / "monitor_state.json"))

        state_service.save_exit_snapshot(
            {
                "signal_count": 2,
                "by_reason": [{"key": "sector_retreat", "count": 1}, {"key": "time_stop", "count": 1}],
                "summary_lines": ["退出监控命中 2 条信号。"],
            }
        )
        state_service.save_exit_snapshot(
            {
                "signal_count": 1,
                "by_reason": [{"key": "sector_retreat", "count": 1}],
                "summary_lines": ["退出监控命中 1 条信号。"],
            }
        )
        state_service.save_exit_snapshot(
            {
                "signal_count": 0,
                "by_reason": [],
                "summary_lines": ["当前无退出监控信号。"],
            }
        )

        trend = state_service.get_exit_snapshot_trend_summary(recent_limit=3)

        assert trend["available"] is True
        assert trend["snapshot_count"] == 3
        assert trend["total_signals"] == 3
        assert trend["latest_signal_count"] == 0
        assert trend["avg_signal_count"] == 1.0
        assert trend["max_signal_count"] == 2
        assert trend["signal_count_series"] == [2, 1, 0]
        assert trend["by_reason"][0]["key"] == "sector_retreat"
        assert trend["by_reason"][0]["count"] == 2
        assert trend["summary_lines"]

    def test_execution_bridge_health_trend_summary_aggregates_gateway_and_qmt_history(self, tmp_path: Path) -> None:
        clock = [datetime(2026, 4, 8, 9, 30, 0)]

        def now_factory() -> datetime:
            current = clock[0]
            clock[0] = current + timedelta(minutes=5)
            return current

        state_service = MonitorStateService(
            state_store=StateStore(tmp_path / "monitor_state.json"),
            now_factory=now_factory,
        )

        state_service.save_execution_bridge_health(
            {
                "checked_at": 1712539800.0,
                "reported_at": "2026-04-08T09:30:00+08:00",
                "source_id": "windows-vm-a",
                "deployment_role": "primary_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                "gateway_online": True,
                "qmt_connected": True,
                "session_fresh_seconds": 15,
                "summary_lines": ["执行面状态稳定。"],
            }
        )
        state_service.save_execution_bridge_health(
            {
                "checked_at": 1712540100.0,
                "reported_at": "2026-04-08T09:35:00+08:00",
                "source_id": "windows-vm-a",
                "deployment_role": "primary_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                "gateway_online": True,
                "qmt_connected": True,
                "session_fresh_seconds": 45,
                "windows_execution_gateway": {
                    "status": "degraded",
                    "reachable": True,
                    "latency_ms": 1800.0,
                    "error_count": 2,
                    "detail": "gateway poll timeout",
                },
                "qmt_vm": {
                    "status": "healthy",
                    "reachable": True,
                    "latency_ms": 120.0,
                },
            }
        )
        state_service.save_execution_bridge_health(
            {
                "checked_at": 1712540400.0,
                "reported_at": "2026-04-08T09:40:00+08:00",
                "source_id": "windows-vm-b",
                "deployment_role": "backup_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway_backup -> qmt_vm",
                "gateway_online": False,
                "qmt_connected": True,
                "session_fresh_seconds": 650,
                "last_error": "gateway process unavailable",
                "windows_execution_gateway": {
                    "reachable": False,
                    "latency_ms": 0.0,
                    "error_count": 3,
                    "last_error_at": "2026-04-08T09:39:00",
                },
                "qmt_vm": {
                    "reachable": True,
                    "latency_ms": 260.0,
                    "staleness_seconds": 650.0,
                    "error_count": 1,
                },
            }
        )

        latest_health = state_service.get_latest_execution_bridge_health()
        trend = state_service.get_execution_bridge_health_trend_summary(recent_limit=3)
        state = state_service.get_state()

        assert latest_health["health"]["overall_status"] == "down"
        assert latest_health["health"]["reported_at"] == "2026-04-08T09:40:00+08:00"
        assert latest_health["health"]["source_id"] == "windows-vm-b"
        assert latest_health["health"]["deployment_role"] == "backup_gateway"
        assert latest_health["health"]["bridge_path"] == "linux_openclaw -> windows_gateway_backup -> qmt_vm"
        assert latest_health["health"]["windows_execution_gateway"]["status"] == "down"
        assert latest_health["health"]["qmt_vm"]["status"] == "degraded"
        assert "Windows Execution Gateway" in latest_health["health"]["attention_components"]
        assert "QMT VM" in latest_health["health"]["attention_components"]
        assert latest_health["health"]["attention_component_keys"] == ["windows_execution_gateway", "qmt_vm"]
        assert latest_health["health"]["component_health"][0]["key"] == "windows_execution_gateway"
        assert latest_health["health"]["component_health"][1]["key"] == "qmt_vm"

        assert trend["available"] is True
        assert trend["snapshot_count"] == 3
        assert trend["latest_reported_at"] == "2026-04-08T09:40:00+08:00"
        assert trend["latest_source_id"] == "windows-vm-b"
        assert trend["latest_deployment_role"] == "backup_gateway"
        assert trend["latest_bridge_path"] == "linux_openclaw -> windows_gateway_backup -> qmt_vm"
        assert trend["latest_overall_status"] == "down"
        assert trend["overall_status_counts"] == {"healthy": 1, "degraded": 1, "down": 1, "unknown": 0}
        assert trend["latest_gateway_status"] == "down"
        assert trend["latest_qmt_vm_status"] == "degraded"
        assert trend["attention_snapshot_count"] == 2
        assert trend["attention_ratio"] == 0.666667
        assert trend["gateway_online_ratio"] == 0.666667
        assert trend["qmt_connected_ratio"] == 1.0
        assert trend["windows_execution_gateway"]["max_latency_ms"] == 1800.0
        assert trend["windows_execution_gateway"]["error_count_total"] == 5
        assert trend["qmt_vm"]["max_staleness_seconds"] == 650.0
        assert trend["component_trends"][0]["key"] == "windows_execution_gateway"
        assert trend["component_trends"][1]["key"] == "qmt_vm"
        assert trend["health_trend_snapshot"]["latest_gateway_status"] == "down"
        assert trend["health_trend_snapshot"]["latest_source_id"] == "windows-vm-b"
        assert trend["summary_lines"]

        assert state["latest_execution_bridge_health"]["health"]["overall_status"] == "down"
        assert state["execution_bridge_health_history"][-1]["reported_at"] == "2026-04-08T09:40:00+08:00"
        assert state["execution_bridge_health_history"][-1]["source_id"] == "windows-vm-b"
        assert state["execution_bridge_health_history"][-1]["deployment_role"] == "backup_gateway"
        assert state["execution_bridge_health_history"][-1]["bridge_path"] == "linux_openclaw -> windows_gateway_backup -> qmt_vm"
        assert state["execution_bridge_health_history"][-1]["overall_status"] == "down"
        assert state["execution_bridge_health_trend_summary"]["latest_gateway_status"] == "down"

    def test_execution_bridge_health_snapshot_and_trend_summary(self, tmp_path: Path) -> None:
        state_service = MonitorStateService(state_store=StateStore(tmp_path / "monitor_state.json"))

        state_service.save_execution_bridge_health(
            {
                "overall_status": "healthy",
                "reported_at": "2026-04-08T09:30:00+08:00",
                "source_id": "windows-vm-a",
                "deployment_role": "primary_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                "gateway_online": True,
                "qmt_connected": True,
                "account_id": "A10001",
                "session_fresh_seconds": 2,
                "attention_components": [],
                "windows_execution_gateway": {"status": "healthy", "reachable": True, "latency_ms": 6.5},
                "qmt_vm": {"status": "healthy", "reachable": True, "latency_ms": 8.2},
                "summary_lines": ["gateway/qmt 正常。"],
                "updated_at": "2026-04-08T09:30:00",
            }
        )
        state_service.save_execution_bridge_health(
            {
                "overall_status": "degraded",
                "reported_at": "2026-04-08T09:31:00+08:00",
                "source_id": "windows-vm-a",
                "deployment_role": "primary_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                "gateway_online": True,
                "qmt_connected": False,
                "account_id": "A10001",
                "session_fresh_seconds": 9,
                "attention_components": ["qmt_vm"],
                "windows_execution_gateway": {"status": "healthy", "reachable": True, "latency_ms": 7.1},
                "qmt_vm": {"status": "down", "reachable": False, "latency_ms": 0.0},
                "last_error": "qmt rpc timeout",
                "summary_lines": ["gateway 在线但 qmt 断连。"],
                "updated_at": "2026-04-08T09:31:00",
            }
        )
        state_service.save_execution_bridge_health(
            {
                "overall_status": "down",
                "reported_at": "2026-04-08T09:32:00+08:00",
                "source_id": "windows-vm-a",
                "deployment_role": "primary_gateway",
                "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
                "gateway_online": False,
                "qmt_connected": False,
                "account_id": "A10001",
                "session_fresh_seconds": 30,
                "attention_components": ["windows_execution_gateway", "qmt_vm"],
                "windows_execution_gateway": {"status": "down", "reachable": False, "latency_ms": 0.0},
                "qmt_vm": {"status": "down", "reachable": False, "latency_ms": 0.0},
                "last_error": "gateway offline",
                "summary_lines": ["gateway 离线。"],
                "updated_at": "2026-04-08T09:32:00",
            }
        )

        state = state_service.get_state()
        latest = state_service.get_latest_execution_bridge_health()
        trend = state_service.get_execution_bridge_health_trend_summary(recent_limit=3)

        assert latest["trigger"] == "windows_gateway"
        assert latest["health"]["gateway_online"] is False
        assert latest["health"]["qmt_connected"] is False
        assert latest["health"]["last_error"] == "gateway offline"
        assert latest["health"]["reported_at"] == "2026-04-08T09:32:00+08:00"
        assert latest["health"]["source_id"] == "windows-vm-a"
        assert latest["health"]["deployment_role"] == "primary_gateway"
        assert latest["health"]["bridge_path"] == "linux_openclaw -> windows_gateway -> qmt_vm"
        assert latest["health"]["overall_status"] == "down"
        assert latest["health"]["windows_execution_gateway"]["status"] == "down"
        assert latest["health"]["qmt_vm"]["status"] == "down"
        assert len(state["execution_bridge_health_history"]) == 3
        assert trend["available"] is True
        assert trend["snapshot_count"] == 3
        assert trend["latest_reported_at"] == "2026-04-08T09:32:00+08:00"
        assert trend["latest_source_id"] == "windows-vm-a"
        assert trend["latest_deployment_role"] == "primary_gateway"
        assert trend["latest_bridge_path"] == "linux_openclaw -> windows_gateway -> qmt_vm"
        assert trend["latest_overall_status"] == "down"
        assert trend["trend_status"] == "degrading"
        assert trend["gateway_online_ratio"] == 0.666667
        assert trend["qmt_connected_ratio"] == 0.333333
        assert trend["latest_gateway_online"] is False
        assert trend["latest_qmt_connected"] is False
        assert trend["latest_gateway_status"] == "down"
        assert trend["latest_qmt_vm_status"] == "down"
        assert trend["latest_session_fresh_seconds"] == 30
        assert trend["last_error_count"] == 2
        assert trend["attention_snapshot_count"] == 2
        assert trend["attention_ratio"] == 0.666667
        assert trend["latest_attention_components"] == ["Windows Execution Gateway", "QMT VM"]
        assert trend["latest_attention_component_keys"] == ["windows_execution_gateway", "qmt_vm"]
        assert trend["overall_status_counts"] == {"healthy": 1, "degraded": 1, "down": 1, "unknown": 0}
        assert trend["windows_execution_gateway"]["latest_status"] == "down"
        assert trend["qmt_vm"]["latest_status"] == "down"
        assert trend["component_trends"][0]["key"] == "windows_execution_gateway"
        assert trend["component_trends"][1]["key"] == "qmt_vm"
        assert trend["health_trend_snapshot"]["latest_source_id"] == "windows-vm-a"
        assert trend["health_trend_snapshot"]["trend_status"] == "degrading"
        assert trend["summary_lines"]
