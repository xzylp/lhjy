"""飞书长连接 Worker。"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from .infra.audit_store import StateStore
from .logging_config import get_logger
from .notify.feishu import FeishuNotifier

logger = get_logger("feishu.longconn")


def _normalize_local_base_url(value: str, default_port: int) -> str:
    text = str(value or "").strip()
    if text:
        return text.rstrip("/")
    return f"http://127.0.0.1:{default_port}"


@dataclass
class FeishuLongConnectionBridge:
    control_plane_base_url: str
    state_store: StateStore
    timeout_sec: float = 10.0
    message_timeout_sec: float = 40.0
    heartbeat_interval_sec: float = 15.0
    reply_sender: FeishuNotifier | None = None

    def __post_init__(self) -> None:
        self.control_plane_base_url = self.control_plane_base_url.rstrip("/")
        self._heartbeat_started = False
        self._heartbeat_lock = threading.Lock()

    def _save_state(self, **updates: Any) -> None:
        current = dict(self.state_store.data or {})
        current.update(updates)
        self.state_store.data = current
        self.state_store._save()

    def _mark_heartbeat(self) -> None:
        self._save_state(
            pid=os.getpid(),
            last_heartbeat_at=datetime.now().isoformat(),
        )

    def start_heartbeat(self) -> None:
        with self._heartbeat_lock:
            if self._heartbeat_started:
                return
            self._heartbeat_started = True

        def _run() -> None:
            while True:
                try:
                    self._mark_heartbeat()
                except Exception as exc:
                    logger.error("飞书长连接心跳写入失败: %s", exc)
                time.sleep(max(float(self.heartbeat_interval_sec or 15.0), 5.0))

        threading.Thread(target=_run, daemon=True, name="feishu-longconn-heartbeat").start()

    def mark_worker_started(self) -> None:
        self._save_state(
            status="starting",
            pid=os.getpid(),
            worker_started_at=datetime.now().isoformat(),
            control_plane_base_url=self.control_plane_base_url,
            last_error="",
        )
        self._mark_heartbeat()

    def mark_connected(self, conn_url: str, reconnect_count: int) -> None:
        self._save_state(
            status="connected",
            pid=os.getpid(),
            last_connected_at=datetime.now().isoformat(),
            last_conn_url=conn_url,
            reconnect_count=reconnect_count,
            last_error="",
            last_error_at="",
        )
        self._mark_heartbeat()

    def mark_event_received(self, event_type: str) -> None:
        self._save_state(
            status="connected",
            last_event_at=datetime.now().isoformat(),
            last_event_type=event_type,
        )
        self._mark_heartbeat()

    def mark_error(self, error: str) -> None:
        self._save_state(
            status="degraded",
            last_error=str(error),
            last_error_at=datetime.now().isoformat(),
        )
        self._mark_heartbeat()

    def mark_disconnected(self) -> None:
        self._save_state(
            status="disconnected",
        )
        self._mark_heartbeat()

    def _marshal_payload(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            decoded = json.loads(payload)
            if isinstance(decoded, dict):
                return decoded
        raise RuntimeError(f"不支持的飞书长连接负载类型: {type(payload)!r}")

    def _forward_to_control_plane(self, payload: dict[str, Any], *, timeout_sec: float) -> dict[str, Any]:
        response = httpx.post(
            f"{self.control_plane_base_url}/system/feishu/events",
            json=payload,
            timeout=timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("控制面返回了非对象 JSON")
        return data

    def _send_reply_if_needed(self, forwarded: dict[str, Any]) -> None:
        if self.reply_sender is None:
            return
        receive_id = str(forwarded.get("reply_to_chat_id") or "").strip()
        reply_lines = forwarded.get("reply_lines") or []
        if not receive_id:
            return
        reply_card = dict(forwarded.get("reply_card") or {})
        dispatched = False
        if isinstance(reply_card.get("card"), dict) and reply_card.get("card"):
            dispatched = self.reply_sender.send_card(
                dict(reply_card.get("card") or {}),
                receive_id=receive_id,
            )
        elif reply_card.get("title") and reply_card.get("markdown"):
            dispatched = self.reply_sender.send_markdown(
                str(reply_card.get("title")),
                str(reply_card.get("markdown")),
                receive_id=receive_id,
            )
        elif isinstance(reply_lines, list):
            text = "\n".join(str(line or "").strip() for line in reply_lines if str(line or "").strip())
            if not text:
                return
            dispatched = self.reply_sender.send_text(text, receive_id=receive_id)
        else:
            return
        logger.info(
            "飞书长连接消息回复%s: chat_id=%s reason=%s",
            "已发送" if dispatched else "未发送",
            receive_id,
            str(forwarded.get("reason") or ""),
        )

    def handle_message_event(self, payload: Any) -> dict[str, Any]:
        normalized = self._marshal_payload(payload)
        self.mark_event_received("im.message.receive_v1")
        def _run() -> None:
            try:
                forwarded = self._forward_to_control_plane(normalized, timeout_sec=self.message_timeout_sec)
                self._send_reply_if_needed(forwarded)
                logger.info(
                    "飞书长连接消息事件已转发: processed=%s reason=%s",
                    forwarded.get("processed"),
                    forwarded.get("reason"),
                )
            except Exception as exc:
                self.mark_error(str(exc))
                logger.error("飞书长连接消息事件转发失败: %s", exc)

        threading.Thread(target=_run, daemon=True, name="feishu-longconn-message-forward").start()
        return {
            "ok": True,
            "queued": True,
            "event_type": "im.message.receive_v1",
        }

    def handle_url_preview(self, payload: Any) -> dict[str, Any]:
        normalized = self._marshal_payload(payload)
        self.mark_event_received("url.preview.get")
        forwarded = self._forward_to_control_plane(normalized, timeout_sec=self.timeout_sec)
        inline = forwarded.get("inline") or {}
        if not isinstance(inline, dict):
            inline = {}
        logger.info("飞书长连接链接预览已转发: title=%s", inline.get("title"))
        return {"inline": inline}


class MonitoringWSClient:
    def __init__(self, base_client_cls, bridge: FeishuLongConnectionBridge, *args, **kwargs) -> None:
        self._bridge = bridge
        self._client = base_client_cls(*args, **kwargs)
        original_connect = self._client._connect
        original_disconnect = self._client._disconnect
        original_handle_message = self._client._handle_message

        async def wrapped_connect():
            await original_connect()
            self._bridge.mark_connected(self._client._conn_url, self._client._reconnect_count)

        async def wrapped_disconnect():
            await original_disconnect()
            self._bridge.mark_disconnected()

        async def wrapped_handle_message(msg):
            try:
                await original_handle_message(msg)
            except Exception as exc:
                self._bridge.mark_error(str(exc))
                raise

        self._client._connect = wrapped_connect
        self._client._disconnect = wrapped_disconnect
        self._client._handle_message = wrapped_handle_message

    def start(self) -> None:
        self._client.start()


def _require_lark_sdk():
    try:
        import lark_oapi as lark
        from lark_oapi.event.callback.model.p2_url_preview_get import P2URLPreviewGetResponse
    except ImportError as exc:
        raise RuntimeError(
            "未安装飞书 Python SDK。请执行: /srv/projects/ashare-system-v2/.venv/bin/python -m pip install -U lark-oapi"
        ) from exc
    return lark, P2URLPreviewGetResponse


def run_feishu_long_connection(settings) -> None:
    lark, P2URLPreviewGetResponse = _require_lark_sdk()

    app_id = str(settings.notify.feishu_app_id or "").strip()
    app_secret = str(settings.notify.feishu_app_secret or "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("飞书长连接启动失败: 缺少 ASHARE_FEISHU_APP_ID 或 ASHARE_FEISHU_APP_SECRET")

    bridge = FeishuLongConnectionBridge(
        control_plane_base_url=_normalize_local_base_url(
            settings.notify.feishu_control_plane_base_url,
            settings.service.port,
        ),
        state_store=StateStore(settings.storage_root / "feishu_longconn_state.json"),
        timeout_sec=max(float(settings.service.probe_timeout_sec or 3.0), 3.0),
        message_timeout_sec=max(float(settings.service.probe_timeout_sec or 3.0), 40.0),
        reply_sender=FeishuNotifier(
            app_id,
            app_secret,
            str(settings.notify.feishu_chat_id or "").strip(),
            important_chat_id=str(settings.notify.feishu_important_chat_id or "").strip(),
            supervision_chat_id=str(settings.notify.feishu_supervision_chat_id or "").strip(),
        ),
    )
    bridge.mark_worker_started()
    bridge.start_heartbeat()

    def do_p2_im_message_receive_v1(data) -> None:
        payload = json.loads(lark.JSON.marshal(data))
        bridge.handle_message_event(payload)

    def do_p2_url_preview_get(data):
        payload = json.loads(lark.JSON.marshal(data))
        forwarded = bridge.handle_url_preview(payload)
        return P2URLPreviewGetResponse(forwarded)

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
        .register_p2_url_preview_get(do_p2_url_preview_get)
        .build()
    )

    logger.info(
        "飞书长连接启动: control_plane_base_url=%s app_id=%s",
        bridge.control_plane_base_url,
        app_id,
    )
    cli = MonitoringWSClient(
        lark.ws.Client,
        bridge,
        app_id,
        app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG,
    )
    try:
        cli.start()
    except Exception as exc:
        bridge.mark_error(str(exc))
        raise
