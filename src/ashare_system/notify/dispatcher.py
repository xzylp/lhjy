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

    def __init__(self, feishu: FeishuNotifier) -> None:
        self.feishu = feishu
        self._records: dict[str, DispatchRecord] = {}

    def dispatch(self, channel: str, title: str, content: str, level: str = "info", force: bool = False) -> bool:
        """分发消息，支持限流"""
        if not force and self._is_rate_limited(channel):
            logger.debug("消息限流跳过: %s", channel)
            return False
        success = self.feishu.send_alert(title, content, level)
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
