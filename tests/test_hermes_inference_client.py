import unittest
from unittest.mock import patch

from ashare_system.hermes.inference_client import HermesInferenceClient
from ashare_system.settings import HermesSettings


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class HermesInferenceClientTests(unittest.TestCase):
    def test_fast_slot_unavailable_falls_back_to_compat(self) -> None:
        settings = HermesSettings(
            minimax_provider_name="MiniMax",
            minimax_model="MiniMax-M2.7",
            minimax_base_url="",
            minimax_api_key="",
            compat_provider_name="Chunfeng",
            compat_base_url="https://example.com/v1",
            compat_api_key="test-key",
            compat_model="gpt-5.4",
        )
        client = HermesInferenceClient(settings)

        with patch("httpx.Client.post", return_value=_FakeResponse({"choices": [{"message": {"content": "收到，已切到可用强模型。"}}]})) as mocked_post:
            result = client.complete(
                question="你是谁？",
                prefer_fast=True,
                require_deep_reasoning=False,
                system_prompt="你是测试助手。",
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.selected_slot["provider_id"], "minimax")
        self.assertEqual(result.effective_slot["provider_id"], "compat-gpt54")
        self.assertIn("selected_slot_unavailable", result.fallback_reason)
        self.assertIn("gpt-5.4", str(mocked_post.call_args.kwargs["json"]["model"]))
        self.assertEqual(result.text, "收到，已切到可用强模型。")

    def test_returns_error_when_no_provider_is_configured(self) -> None:
        settings = HermesSettings(
            minimax_base_url="",
            minimax_api_key="",
            compat_base_url="",
            compat_api_key="",
        )
        client = HermesInferenceClient(settings)

        result = client.complete(
            question="测试",
            prefer_fast=True,
            require_deep_reasoning=False,
            system_prompt="你是测试助手。",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_provider_configured")
        self.assertEqual(result.effective_slot, {})


if __name__ == "__main__":
    unittest.main()
