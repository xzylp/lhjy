"""消息分发器 — 路由 + 限流"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .feishu import FeishuNotifier
from ..logging_config import get_logger

logger = get_logger("notify.dispatcher")

RATE_LIMIT_SECONDS = 3  # 同类消息最小间隔


@dataclass
class DispatchRecord:
    last_sent: float = 0.0
    count: int = 0


class MessageDispatcher:
    """消息分发器: 统一路由 + 限流"""

    def __init__(
        self,
        feishu: FeishuNotifier,
        *,
        supervision_feishu: FeishuNotifier | None = None,
        execution_feishu: FeishuNotifier | None = None,
    ) -> None:
        self.feishu = feishu
        self.supervision_feishu = supervision_feishu
        self.execution_feishu = execution_feishu
        self._records: dict[str, DispatchRecord] = {}

    @staticmethod
    def _should_dispatch_message(channel: str, title: str, content: str, level: str, force: bool) -> bool:
        if force:
            return True
        normalized_channel = str(channel or "").strip().lower()
        text = f"{title}\n{content}".lower()
        if normalized_channel == "monitor_changes":
            suppressed_markers = (
                "候选排序变化",
                "前3变化",
                "进入前3",
                "execution_pool",
                "进入 execution_pool",
                "移出 execution_pool",
                "入池",
                "候选刷新",
                "观察池",
            )
            critical_markers = (
                "风控",
                "risk_gate",
                "审计",
                "audit_gate",
                "阻断",
                "失败",
                "异常",
                "超时",
                "桥接",
            )
            if any(marker in text for marker in suppressed_markers) and not any(marker in text for marker in critical_markers):
                return False
        return True

    def _resolve_notifier(self, channel: str) -> FeishuNotifier:
        normalized = str(channel or "default").strip().lower()
        if normalized in {"monitor_changes", "supervision", "督办"} and self.supervision_feishu:
            return self.supervision_feishu
        if normalized in {"trade", "live_execution_alert", "execution", "回执"} and self.execution_feishu:
            return self.execution_feishu
        return self.feishu

    def dispatch(self, channel: str, title: str, content: str, level: str = "info", force: bool = False) -> bool:
        """分发消息，支持限流"""
        if not self._should_dispatch_message(channel, title, content, level, force):
            logger.info("消息策略抑制: channel=%s title=%s", channel, title)
            return False
        if not force and self._is_rate_limited(channel):
            logger.debug("消息限流跳过: %s", channel)
            return False
        notifier = self._resolve_notifier(channel)
        success = notifier.send_alert(title, content, level, channel=channel)
        self._update_record(channel)
        return success

    def dispatch_trade(self, title: str, content: str, level: str = "info", force: bool = True) -> bool:
        return self.dispatch("trade", title, content, level, force=force)

    def dispatch_alert(self, content: str) -> bool:
        return self.dispatch("alert", "风控告警", content, "warning", force=True)

    def dispatch_report(self, title: str, content: str) -> bool:
        return self.dispatch("report", title, content, "info")

    def dispatch_monitor_changes(self, title: str, content: str, level: str = "info", force: bool = False) -> bool:
        return self.dispatch("monitor_changes", title, content, level, force=force)

    def dispatch_discussion_summary(self, title: str, content: str, level: str = "info", force: bool = False) -> bool:
        return self.dispatch("discussion_summary", title, content, level, force=force)

    def dispatch_governance_update(self, title: str, content: str, level: str = "info", force: bool = False) -> bool:
        return self.dispatch("governance_update", title, content, level, force=force)

    def dispatch_live_execution_alert(self, title: str, content: str, level: str = "warning", force: bool = False) -> bool:
        return self.dispatch("live_execution_alert", title, content, level, force=force)

    def _is_rate_limited(self, channel: str) -> bool:
        rec = self._records.get(channel)
        if rec is None:
            return False
        return time.time() - rec.last_sent < RATE_LIMIT_SECONDS

    def _update_record(self, channel: str) -> None:
        rec = self._records.setdefault(channel, DispatchRecord())
        rec.last_sent = time.time()
        rec.count += 1
