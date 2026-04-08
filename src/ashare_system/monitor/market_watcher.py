"""全天盯盘服务"""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass, field

from .alert_engine import AlertEngine, AlertEvent
from .exit_monitor import ExitMonitor
from .persistence import MonitorStateService
from ..logging_config import get_logger

logger = get_logger("monitor.market_watcher")


def _empty_exit_snapshot() -> dict:
    return {
        "version": "v1",
        "checked_at": 0.0,
        "signal_count": 0,
        "watched_symbols": [],
        "by_symbol": [],
        "by_reason": [],
        "by_severity": [],
        "by_tag": [],
        "summary_lines": ["当前无退出监控信号。"],
        "items": [],
    }


@dataclass
class WatcherState:
    is_running: bool = False
    watched_symbols: list[str] = field(default_factory=list)
    alert_count: int = 0
    last_check_time: float = 0.0
    last_exit_signal_count: int = 0
    last_exit_signals: list[dict] = field(default_factory=list)
    last_exit_summary: dict = field(default_factory=dict)
    last_exit_snapshot: dict = field(default_factory=_empty_exit_snapshot)


class MarketWatcher:
    """全天盯盘服务"""

    def __init__(
        self,
        market_adapter=None,
        state_service: MonitorStateService | None = None,
        dossier_precompute_service=None,
        exit_monitor: ExitMonitor | None = None,
        exit_context_provider=None,
        board_snapshot_provider=None,
    ) -> None:
        self.market = market_adapter
        self.alert_engine = AlertEngine()
        self.state = WatcherState()
        self._alert_callbacks: list = []
        self.state_service = state_service
        self.dossier_precompute_service = dossier_precompute_service
        self.exit_monitor = exit_monitor
        self.exit_context_provider = exit_context_provider
        self.board_snapshot_provider = board_snapshot_provider

    def add_symbols(self, symbols: list[str]) -> None:
        self.state.watched_symbols = list(set(self.state.watched_symbols + symbols))
        logger.info("盯盘标的: %d 只", len(self.state.watched_symbols))

    def on_alert(self, callback) -> None:
        self._alert_callbacks.append(callback)

    def get_latest_exit_signals(self) -> list[dict]:
        return list(self.state.last_exit_signals)

    def get_latest_exit_summary(self) -> dict:
        return dict(self.state.last_exit_summary)

    def get_latest_exit_snapshot(self) -> dict:
        return deepcopy(self.state.last_exit_snapshot)

    def _build_exit_snapshot(self, summary: dict) -> dict:
        snapshot = _empty_exit_snapshot()
        snapshot["checked_at"] = self.state.last_check_time
        snapshot["signal_count"] = self.state.last_exit_signal_count
        snapshot["watched_symbols"] = sorted(self.state.watched_symbols)
        snapshot["by_symbol"] = list(summary.get("by_symbol", []))
        snapshot["by_reason"] = list(summary.get("by_reason", []))
        snapshot["by_severity"] = list(summary.get("by_severity", []))
        snapshot["by_tag"] = list(summary.get("by_tag", []))
        snapshot["summary_lines"] = list(summary.get("summary_lines", snapshot["summary_lines"]))
        snapshot["items"] = list(summary.get("items", []))
        return snapshot

    def _resolve_provider_payload(self, provider):
        if callable(provider):
            return provider(list(self.state.watched_symbols))
        return provider

    def check_once(self) -> list[AlertEvent]:
        """执行一次盯盘检查"""
        if not self.market or not self.state.watched_symbols:
            return []
        try:
            snapshots = self.market.get_snapshots(self.state.watched_symbols)
            alerts = self.alert_engine.check_batch(snapshots)
            exit_signals: list[dict] = []
            if self.exit_monitor:
                contexts = self._resolve_provider_payload(self.exit_context_provider) or {}
                board_snapshots = self._resolve_provider_payload(self.board_snapshot_provider) or {}
                monitor_signals = self.exit_monitor.check_batch(snapshots, contexts, board_snapshots)
                exit_signals = [signal.to_dict() for signal in monitor_signals]
                self.state.last_exit_signal_count = len(exit_signals)
                self.state.last_exit_signals = exit_signals
                self.state.last_exit_summary = self.exit_monitor.summarize(monitor_signals)
            else:
                self.state.last_exit_summary = {}
                self.state.last_exit_signal_count = 0
                self.state.last_exit_signals = []
            self.state.alert_count += len(alerts)
            self.state.last_check_time = time.time()
            self.state.last_exit_snapshot = self._build_exit_snapshot(self.state.last_exit_summary)
            if self.state_service:
                self.state_service.save_exit_snapshot(self.state.last_exit_snapshot, trigger="market_watcher")
                self.state_service.record_alert_events(alerts, snapshots)
                self.state_service.save_heartbeat_if_due(snapshots, alerts, trigger="market_watcher")
                candidate_poll = self.state_service.mark_poll_if_due("candidate", trigger="market_watcher")
                self.state_service.mark_poll_if_due("focus", trigger="market_watcher")
                self.state_service.mark_poll_if_due("execution", trigger="market_watcher")
            else:
                candidate_poll = {"triggered": True}
            if self.dossier_precompute_service:
                if candidate_poll.get("triggered"):
                    self.dossier_precompute_service.refresh_if_due(
                        source="candidate_pool",
                        trigger="market_watcher",
                    )
            for alert in alerts:
                logger.info("预警: [%s] %s", alert.severity, alert.message)
                for cb in self._alert_callbacks:
                    cb(alert)
            if exit_signals:
                logger.info("退出监控信号: %d 条", len(exit_signals))
            return alerts
        except Exception as e:
            logger.warning("盯盘检查失败: %s", e)
            return []
