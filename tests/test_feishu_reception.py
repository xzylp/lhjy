from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from ashare_system.app import create_app
from ashare_system.container import get_meeting_state_store, get_research_state_store, reset_container


@contextmanager
def _temporary_env(**overrides: str):
    keys = set(overrides.keys()) | {
        "ASHARE_STORAGE_ROOT",
        "ASHARE_LOGS_DIR",
        "ASHARE_EXECUTION_MODE",
        "ASHARE_MARKET_MODE",
        "ASHARE_RUN_MODE",
        "ASHARE_EXECUTION_PLANE",
        "ASHARE_ACCOUNT_ID",
        "ASHARE_FEISHU_VERIFICATION_TOKEN",
        "ASHARE_PUBLIC_BASE_URL",
        "ASHARE_FEISHU_CONTROL_PLANE_BASE_URL",
    }
    previous = {key: os.environ.get(key) for key in keys}
    with tempfile.TemporaryDirectory() as tmp_dir:
        os.environ["ASHARE_STORAGE_ROOT"] = tmp_dir
        os.environ["ASHARE_LOGS_DIR"] = str(Path(tmp_dir) / "logs")
        os.environ["ASHARE_EXECUTION_MODE"] = "mock"
        os.environ["ASHARE_MARKET_MODE"] = "mock"
        os.environ["ASHARE_RUN_MODE"] = "paper"
        os.environ["ASHARE_EXECUTION_PLANE"] = "windows_gateway"
        os.environ["ASHARE_ACCOUNT_ID"] = "test-account"
        os.environ["ASHARE_FEISHU_VERIFICATION_TOKEN"] = "verify-token"
        for key, value in overrides.items():
            os.environ[key] = value
        reset_container()
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            reset_container()


class FeishuReceptionTests(unittest.TestCase):
    def test_feishu_events_route_executes_discussion_bootstrap_command(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                runtime_payload = client.post(
                    "/runtime/jobs/pipeline",
                    json={
                        "symbols": ["000001.SZ", "000002.SZ", "000004.SZ"],
                        "max_candidates": 3,
                        "account_id": "test-account",
                    },
                ).json()
                trade_date = str(runtime_payload["generated_at"])[:10]

                payload = client.post(
                    "/system/feishu/events",
                    json={
                        "token": "verify-token",
                        "schema": "2.0",
                        "header": {"event_type": "im.message.receive_v1"},
                        "event": {
                            "sender": {"sender_id": {"open_id": "ou_test_sender"}},
                            "message": {
                                "chat_id": "oc_test_chat",
                                "chat_type": "p2p",
                                "content": "{\"text\":\"那你启动啊。\"}",
                            },
                        },
                    },
                ).json()

                self.assertTrue(payload["ok"])
                self.assertTrue(payload["processed"])
                self.assertEqual(payload["reason"], "natural_language_action_executed")
                self.assertEqual(payload["topic"], "action")
                self.assertEqual(payload["trade_date"], trade_date)
                self.assertTrue(any("round 1" in line for line in payload["reply_lines"]))

                cycle_payload = client.get(f"/system/discussions/cycles/{trade_date}").json()
                self.assertEqual(cycle_payload["discussion_state"], "round_1_running")

    def test_feishu_events_route_maps_card_entries_to_dashboard_pages(self) -> None:
        with _temporary_env(
            ASHARE_PUBLIC_BASE_URL="http://100.75.91.75",
            ASHARE_FEISHU_CONTROL_PLANE_BASE_URL="http://127.0.0.1:18793",
        ):
            trade_date = datetime.now().date().isoformat()
            get_research_state_store().set(
                "summary",
                {
                    "trade_date": trade_date,
                    "updated_at": datetime.now().isoformat(),
                    "symbols": ["600519.SH"],
                    "news_count": 1,
                    "announcement_count": 0,
                    "event_titles": ["政策催化"],
                },
            )
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/events",
                    json={
                        "token": "verify-token",
                        "schema": "2.0",
                        "header": {"event_type": "im.message.receive_v1"},
                        "event": {
                            "sender": {"sender_id": {"open_id": "ou_test_sender"}},
                            "message": {
                                "chat_id": "oc_test_chat",
                                "chat_type": "group",
                                "content": "{\"text\":\"@_user_1 现在状态怎么样\"}",
                                "mentions": [
                                    {
                                        "key": "@_user_1",
                                        "name": "南风·量化",
                                        "mentioned_type": "bot",
                                    }
                                ],
                            },
                        },
                    },
                ).json()

                action_block = next(
                    item for item in payload["reply_card"]["card"]["elements"] if item.get("tag") == "action"
                )
                urls = [str(item.get("url") or "") for item in action_block.get("actions", [])]
                self.assertTrue(urls)
                self.assertTrue(all(url.startswith("http://100.75.91.75/") for url in urls))
                self.assertTrue(all("/dashboard" in url for url in urls))
                self.assertTrue(all("/system/" not in url for url in urls))

    def test_feishu_ask_routes_why_no_buy_question_to_execution(self) -> None:
        with _temporary_env():
            trade_date = datetime.now().date().isoformat()
            get_meeting_state_store().set(
                "latest_execution_precheck",
                {
                    "trade_date": trade_date,
                    "generated_at": datetime.now().isoformat(),
                    "status": "blocked",
                    "approved_count": 0,
                    "blocked_count": 0,
                    "summary_lines": ["当前没有通过预检的买入对象。"],
                },
            )
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/ask",
                    json={"question": "今天为什么没有买入操作？", "trade_date": trade_date},
                ).json()
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["topic"], "execution")
                self.assertTrue(any("执行席位" in line or "执行预检" in line for line in payload["answer_lines"]))

    def test_feishu_ask_supervision_bot_redirects_execution_question(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/ask",
                    json={"question": "今天为什么没有买入操作？", "bot_role": "supervision"},
                ).json()
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["topic"], "handoff")
                self.assertEqual(payload["target_role"], "execution")
                self.assertTrue(any("Hermes回执" in line for line in payload["answer_lines"]))

    def test_feishu_ask_main_bot_no_longer_returns_help_page_for_generic_question(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/ask",
                    json={"question": "现在整体跑到哪一步了？", "bot_role": "main"},
                ).json()
                self.assertTrue(payload["ok"])
                self.assertNotEqual(payload["topic"], "help")
                self.assertFalse(any("固定五类主题问答器" in line for line in payload["answer_lines"]))

    def test_feishu_ask_open_question_falls_back_to_open_chat(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/ask",
                    json={"question": "你觉得今天还缺什么？", "bot_role": "main"},
                ).json()

                self.assertTrue(payload["ok"])
                self.assertEqual(payload["topic"], "open_chat")
                self.assertTrue(any("主控自由回答" in line for line in payload["answer_lines"]))

    def test_feishu_events_route_uses_receiver_bot_role_for_reply(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/events",
                    json={
                        "token": "verify-token",
                        "__receiver_bot_role": "execution",
                        "__receiver_bot_name": "Hermes回执",
                        "__receiver_bot_id": "bot-execution",
                        "schema": "2.0",
                        "header": {"event_type": "im.message.receive_v1", "app_id": "cli_exec_bot"},
                        "event": {
                            "sender": {"sender_id": {"open_id": "ou_exec_sender"}},
                            "message": {
                                "chat_id": "oc_exec_chat",
                                "chat_type": "group",
                                "content": "{\"text\":\"@_user_1 现在整体情况如何\"}",
                                "mentions": [
                                    {
                                        "key": "@_user_1",
                                        "name": "Hermes回执",
                                        "mentioned_type": "bot",
                                    }
                                ],
                            },
                        },
                    },
                ).json()

                self.assertTrue(payload["ok"])
                self.assertTrue(payload["processed"])
                self.assertEqual(payload["receiver_bot_role"], "execution")
                self.assertEqual(payload["bot_role"], "execution")
                self.assertTrue(payload["reply_card"]["title"].startswith("Hermes回执"))

    def test_feishu_events_route_accepts_real_world_mentions_without_mentioned_type(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/events",
                    json={
                        "token": "verify-token",
                        "__receiver_bot_role": "main",
                        "__receiver_bot_name": "Hermes主控",
                        "__receiver_bot_id": "bot-main",
                        "__receiver_bot_app_id": "cli_main_bot",
                        "schema": "2.0",
                        "header": {"event_type": "im.message.receive_v1", "app_id": "cli_main_bot"},
                        "event": {
                            "sender": {"sender_id": {"open_id": "ou_main_sender"}},
                            "message": {
                                "chat_id": "oc_main_chat",
                                "chat_type": "group",
                                "content": "{\"text\":\"@_user_1 你是谁？\"}",
                                "mentions": [
                                    {
                                        "key": "@_user_1",
                                        "name": "Hermes·主控 ",
                                        "id": {"open_id": "ou_target"},
                                    }
                                ],
                            },
                        },
                    },
                ).json()

                self.assertTrue(payload["ok"])
                self.assertTrue(payload["processed"])
                self.assertEqual(payload["bot_role"], "main")
                self.assertTrue(payload["reply_card"]["title"].startswith("Hermes主控"))

    def test_feishu_events_route_ignores_message_for_other_bot_in_same_group(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/events",
                    json={
                        "token": "verify-token",
                        "__receiver_bot_role": "execution",
                        "__receiver_bot_name": "Hermes回执",
                        "__receiver_bot_id": "bot-execution",
                        "schema": "2.0",
                        "header": {"event_type": "im.message.receive_v1", "app_id": "cli_exec_bot"},
                        "event": {
                            "sender": {"sender_id": {"open_id": "ou_exec_sender"}},
                            "message": {
                                "chat_id": "oc_exec_chat",
                                "chat_type": "group",
                                "content": "{\"text\":\"@_user_1 明天会下雨吗\"}",
                                "mentions": [
                                    {
                                        "key": "@_user_1",
                                        "name": "Hermes主控",
                                        "mentioned_type": "bot",
                                    }
                                ],
                            },
                        },
                    },
                ).json()

                self.assertTrue(payload["ok"])
                self.assertFalse(payload["processed"])
                self.assertEqual(payload["reason"], "message_ignored")
                self.assertEqual(payload["addressing_reason"], "mention_not_for_current_bot")

    def test_feishu_ask_identity_question_returns_main_bot_identity(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/ask",
                    json={"question": "你是谁？", "bot_role": "main"},
                ).json()

                self.assertTrue(payload["ok"])
                self.assertEqual(payload["topic"], "casual_chat")
                self.assertTrue(any("Hermes主控" in line for line in payload["answer_lines"]))
                self.assertTrue(any("不是 OpenClaw" in line for line in payload["answer_lines"]))

    def test_feishu_ask_identity_question_returns_supervision_bot_identity(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/ask",
                    json={"question": "你是谁？", "bot_role": "supervision"},
                ).json()

                self.assertTrue(payload["ok"])
                self.assertEqual(payload["topic"], "casual_chat")
                self.assertTrue(any("Hermes督办" in line for line in payload["answer_lines"]))

    def test_feishu_ask_identity_question_returns_execution_bot_identity(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/ask",
                    json={"question": "你是谁？", "bot_role": "execution"},
                ).json()

                self.assertTrue(payload["ok"])
                self.assertEqual(payload["topic"], "casual_chat")
                self.assertTrue(any("Hermes回执" in line for line in payload["answer_lines"]))

    def test_feishu_ask_execution_bot_redirects_weather_question_to_main(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/ask",
                    json={"question": "明天会下雨吗", "bot_role": "execution"},
                ).json()

                self.assertTrue(payload["ok"])
                self.assertEqual(payload["topic"], "casual_chat")
                self.assertTrue(any("天气" in line for line in payload["answer_lines"]))

    def test_feishu_ask_supervision_bot_handles_personhood_chat_as_casual(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/ask",
                    json={"question": "你是哪里人？", "bot_role": "supervision"},
                ).json()

                self.assertTrue(payload["ok"])
                self.assertEqual(payload["topic"], "casual_chat")
                self.assertTrue(any("不是自然人" in line for line in payload["answer_lines"]))

    def test_feishu_ask_model_question_uses_lightweight_chat_path(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/ask",
                    json={"question": "你用的是什么模型？", "bot_role": "main"},
                ).json()

                self.assertTrue(payload["ok"])
                self.assertEqual(payload["topic"], "casual_chat")
                self.assertTrue(any("gpt-5.4" in line for line in payload["answer_lines"]))

    def test_feishu_ask_supervision_card_complaint_uses_lightweight_chat_path(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/ask",
                    json={"question": "你现在怎么不发卡片了？", "bot_role": "supervision"},
                ).json()

                self.assertTrue(payload["ok"])
                self.assertEqual(payload["topic"], "casual_chat")
                self.assertTrue(any("卡片" in line for line in payload["answer_lines"]))

    def test_feishu_ask_execution_card_complaint_returns_plain_text_mode(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/ask",
                    json={"question": "你每次回答问题为什么都带个卡片？", "bot_role": "execution"},
                ).json()

                self.assertTrue(payload["ok"])
                self.assertEqual(payload["topic"], "casual_chat")
                self.assertEqual(payload["reply_mode"], "text")
                self.assertTrue(any("轻量文本优先" in line for line in payload["reply_lines"]))

    def test_feishu_ask_main_card_question_returns_plain_text_mode(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/ask",
                    json={"question": "你说话不会带卡片吧？", "bot_role": "main"},
                ).json()

                self.assertTrue(payload["ok"])
                self.assertEqual(payload["topic"], "casual_chat")
                self.assertEqual(payload["reply_mode"], "text")
                self.assertTrue(any("不该每次都带卡片" in line for line in payload["reply_lines"]))

    def test_feishu_ask_execution_can_reply_to_meta_chat_quickly(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                payload = client.post(
                    "/system/feishu/ask",
                    json={"question": "你确定或者是不会说话的？", "bot_role": "execution"},
                ).json()

                self.assertTrue(payload["ok"])
                self.assertEqual(payload["topic"], "casual_chat")
                self.assertTrue(any("会，我在" in line for line in payload["answer_lines"]))

    def test_feishu_events_route_handles_market_watch_directive_as_action(self) -> None:
        with _temporary_env():
            with TestClient(create_app()) as client:
                runtime_payload = client.post(
                    "/runtime/jobs/pipeline",
                    json={
                        "symbols": ["000001.SZ", "000002.SZ", "000004.SZ"],
                        "max_candidates": 3,
                        "account_id": "test-account",
                    },
                ).json()
                trade_date = str(runtime_payload["generated_at"])[:10]

                payload = client.post(
                    "/system/feishu/events",
                    json={
                        "token": "verify-token",
                        "schema": "2.0",
                        "header": {"event_type": "im.message.receive_v1"},
                        "event": {
                            "sender": {"sender_id": {"open_id": "ou_test_watch_sender"}},
                            "message": {
                                "chat_id": "oc_test_watch_chat",
                                "chat_type": "p2p",
                                "content": "{\"text\":\"早上好，今天你先自己盯一下盘面，有异常直接安排。\"}",
                            },
                        },
                    },
                ).json()

                self.assertTrue(payload["ok"])
                self.assertTrue(payload["processed"])
                self.assertEqual(payload["reason"], "natural_language_action_executed")
                self.assertEqual(payload["topic"], "action")
                self.assertEqual(payload["trade_date"], trade_date)
                self.assertTrue(any("盯盘" in line for line in payload["reply_lines"]))

                cycle_payload = client.get(f"/system/discussions/cycles/{trade_date}").json()
                self.assertEqual(cycle_payload["discussion_state"], "round_1_running")


if __name__ == "__main__":
    unittest.main()
