import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ashare_system.feishu_longconn import FeishuLongConnectionBridge, _normalize_local_base_url
from ashare_system.infra.audit_store import StateStore


class _ImmediateThread:
    def __init__(self, *, target=None, daemon=None, name=None) -> None:
        self._target = target

    def start(self) -> None:
        if self._target:
            self._target()


class _ReplySender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def send_text(self, text: str, *, channel: str = "default", receive_id: str = "") -> bool:
        self.calls.append((text, receive_id))
        return True


class FeishuLongConnectionTests(unittest.TestCase):
    def test_normalize_local_base_url_defaults_to_localhost(self) -> None:
        self.assertEqual(_normalize_local_base_url("", 8100), "http://127.0.0.1:8100")

    @patch("ashare_system.feishu_longconn.httpx.post")
    def test_message_event_is_forwarded_to_control_plane(self, mock_post) -> None:
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {"ok": True, "processed": True}

        with TemporaryDirectory() as tmp_dir:
            bridge = FeishuLongConnectionBridge(
                "http://127.0.0.1:8100",
                StateStore(Path(tmp_dir) / "feishu_longconn_state.json"),
            )
            payload = bridge.handle_message_event({"header": {"event_type": "im.message.receive_v1"}})

            self.assertTrue(payload["queued"])
            mock_post.assert_called_once()
            self.assertIn("/system/feishu/events", mock_post.call_args.args[0])

    @patch("ashare_system.feishu_longconn.threading.Thread", _ImmediateThread)
    @patch("ashare_system.feishu_longconn.httpx.post")
    def test_message_event_dispatches_reply_back_to_original_chat(self, mock_post) -> None:
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {
            "ok": True,
            "processed": True,
            "reason": "control_plane_link_explained",
            "reply_to_chat_id": "oc_test_chat",
            "reply_lines": [
                "这是程序内部的交易主线流程入口。",
                "核心阶段：盘前预热、盘中发现。",
            ],
        }

        with TemporaryDirectory() as tmp_dir:
            reply_sender = _ReplySender()
            bridge = FeishuLongConnectionBridge(
                "http://127.0.0.1:8100",
                StateStore(Path(tmp_dir) / "feishu_longconn_state.json"),
                reply_sender=reply_sender,
            )
            bridge.handle_message_event({"header": {"event_type": "im.message.receive_v1"}})

            self.assertEqual(
                reply_sender.calls,
                [("这是程序内部的交易主线流程入口。\n核心阶段：盘前预热、盘中发现。", "oc_test_chat")],
            )

    @patch("ashare_system.feishu_longconn.httpx.post")
    def test_url_preview_is_forwarded_and_returns_inline(self, mock_post) -> None:
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {"inline": {"title": "Agent 监督看板"}}

        with TemporaryDirectory() as tmp_dir:
            bridge = FeishuLongConnectionBridge(
                "http://127.0.0.1:8100",
                StateStore(Path(tmp_dir) / "feishu_longconn_state.json"),
            )
            payload = bridge.handle_url_preview({"header": {"event_type": "url.preview.get"}})

            self.assertEqual(payload["inline"]["title"], "Agent 监督看板")

    def test_mark_worker_started_persists_heartbeat(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            state_store = StateStore(Path(tmp_dir) / "feishu_longconn_state.json")
            bridge = FeishuLongConnectionBridge(
                "http://127.0.0.1:8100",
                state_store,
            )

            bridge.mark_worker_started()

            state_store._load()
            self.assertEqual(state_store.data["status"], "starting")
            self.assertIn("last_heartbeat_at", state_store.data)
            self.assertEqual(state_store.data["pid"], bridge.state_store.data["pid"])


if __name__ == "__main__":
    unittest.main()
