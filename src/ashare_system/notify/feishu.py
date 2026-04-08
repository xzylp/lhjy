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

    def __init__(self, app_id: str, app_secret: str, chat_id: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.chat_id = chat_id
        self._enabled = bool(app_id and app_secret and chat_id)
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

    def send_text(self, text: str) -> bool:
        return self._send_msg("text", json.dumps({"text": text}))

    def send_markdown(self, title: str, content: str) -> bool:
        card = json.dumps({
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "markdown", "content": content}],
        })
        return self._send_msg("interactive", card)

    def send_alert(self, title: str, body: str, level: str = "info") -> bool:
        emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level, "ℹ️")
        return self.send_markdown(f"{emoji} {title}", body)

    def _send_msg(self, msg_type: str, content: str) -> bool:
        if not self._enabled:
            logger.debug("飞书推送未配置，跳过")
            return False
        try:
            token = self._get_token()
            resp = httpx.post(SEND_URL, headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            }, json={
                "receive_id": self.chat_id,
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
