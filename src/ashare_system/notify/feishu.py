"""飞书推送 — 通过飞书 Open API 发送消息"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from ..logging_config import get_logger

logger = get_logger("notify.feishu")

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"


@dataclass
class FeishuMessage:
    title: str
    content: str
    msg_type: str = "text"  # "text" | "markdown"


class FeishuNotifier:
    """飞书 Open API 推送"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        chat_id: str,
        important_chat_id: str = "",
        supervision_chat_id: str = "",
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.chat_id = chat_id
        self.important_chat_id = important_chat_id
        self.supervision_chat_id = supervision_chat_id
        self._enabled = bool(app_id and app_secret and (chat_id or important_chat_id or supervision_chat_id))
        self._token: str = ""

    def _get_token(self) -> str:
        """获取 tenant_access_token"""
        resp = httpx.post(TOKEN_URL, json={
            "app_id": self.app_id,
            "app_secret": self.app_secret,
        }, timeout=5.0)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取飞书 token 失败: {data}")
        self._token = data["tenant_access_token"]
        return self._token

    def send_text(self, text: str, *, channel: str = "default", receive_id: str = "") -> bool:
        return self._send_msg("text", json.dumps({"text": text}), channel=channel, receive_id=receive_id)

    def send_markdown(self, title: str, content: str, *, channel: str = "default", receive_id: str = "") -> bool:
        card = json.dumps({
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "markdown", "content": content}],
        })
        return self._send_msg("interactive", card, channel=channel, receive_id=receive_id)

    def send_card(self, card: dict, *, channel: str = "default", receive_id: str = "") -> bool:
        return self._send_msg("interactive", json.dumps(card, ensure_ascii=False), channel=channel, receive_id=receive_id)

    def send_alert(
        self,
        title: str,
        body: str,
        level: str = "info",
        *,
        channel: str = "default",
        receive_id: str = "",
    ) -> bool:
        emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level, "ℹ️")
        return self.send_markdown(f"{emoji} {title}", body, channel=channel, receive_id=receive_id)

    def _resolve_receive_id(self, channel: str, receive_id: str = "") -> str:
        if receive_id:
            return receive_id
        if channel == "monitor_changes" and self.supervision_chat_id:
            return self.supervision_chat_id
        if channel in {"trade", "discussion_summary", "governance_update", "live_execution_alert", "report", "alert"}:
            return self.important_chat_id or self.chat_id
        return self.chat_id

    def _send_msg(self, msg_type: str, content: str, *, channel: str = "default", receive_id: str = "") -> bool:
        if not self._enabled:
            logger.debug("飞书推送未配置，跳过")
            return False
        try:
            token = self._get_token()
            receive_id = self._resolve_receive_id(channel, receive_id=receive_id)
            if not receive_id:
                logger.debug("飞书推送缺少 receive_id，跳过")
                return False
            resp = httpx.post(SEND_URL, headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            }, json={
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": content,
            }, timeout=10.0)
            data = resp.json()
            if data.get("code") != 0:
                logger.warning("飞书推送失败: %s", data.get("msg"))
                return False
            logger.info("飞书消息发送成功")
            return True
        except Exception as e:
            logger.warning("飞书推送异常: %s", e)
            return False
