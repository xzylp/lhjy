
import unittest
from unittest.mock import MagicMock, patch
import httpx
from ashare_system.settings import AppSettings
from ashare_system.infra.go_client import GoPlatformClient
from ashare_system.infra.market_adapter import GoPlatformMarketDataAdapter
from ashare_system.infra.adapters import GoPlatformExecutionAdapter
from ashare_system.infra.healthcheck import EnvironmentHealthcheck

class TestFix20260416(unittest.TestCase):
    def setUp(self):
        self.settings = AppSettings()
        self.settings.go_platform.enabled = True
        self.settings.go_platform.base_url = "http://127.0.0.1:18793"
        self.settings.go_platform.timeout_sec = 1.0

    def test_go_platform_client_timeout(self):
        client = GoPlatformClient(self.settings)
        
        with patch.object(client.client, 'request') as mock_req:
            mock_req.side_effect = httpx.TimeoutException("timeout")
            
            with self.assertRaises(RuntimeError) as cm:
                client.get_json("/qmt/quote/tick")
            self.assertIn("go_platform_timeout", str(cm.exception))

    def test_market_adapter_fallback(self):
        adapter = GoPlatformMarketDataAdapter(self.settings)
        
        # Mock Go platform failing
        with patch.object(adapter._go_client, 'get_json') as mock_go:
            mock_go.side_effect = RuntimeError("go_platform_unavailable")
            
            # Mock Windows Gateway succeeding
            with patch("ashare_system.infra.market_adapter.WindowsProxyMarketDataAdapter._request_json") as mock_win:
                mock_win.return_value = {"ok": True, "ticks": {"600519.SH": {"lastPrice": 1500.0}}}
                
                result = adapter._request_json("GET", "/qmt/quote/tick", params={"codes": "600519.SH"})
                
                self.assertTrue(result["_fallback"])
                self.assertEqual(result["ticks"]["600519.SH"]["lastPrice"], 1500.0)
                mock_win.assert_called_once()

    def test_execution_adapter_no_fallback_on_post(self):
        adapter = GoPlatformExecutionAdapter(self.settings)
        
        # Mock Go platform failing on POST
        with patch.object(adapter._go_client, 'request') as mock_go:
            mock_go.side_effect = RuntimeError("go_platform_error")
            
            with self.assertRaises(RuntimeError):
                adapter._request_json("POST", "/qmt/trade/order", json_payload={})

    def test_execution_adapter_fallback_on_get(self):
        adapter = GoPlatformExecutionAdapter(self.settings)
        
        # Mock Go platform failing on GET
        with patch.object(adapter._go_client, 'get_json') as mock_go:
            mock_go.side_effect = RuntimeError("go_platform_unavailable")
            
            # Mock Windows Gateway succeeding
            with patch("ashare_system.infra.adapters.WindowsProxyExecutionAdapter._request_json") as mock_win:
                mock_win.return_value = {"ok": True, "asset": {"total_asset": 100000.0}}
                
                result = adapter._request_json("GET", "/qmt/account/asset")
                
                self.assertTrue(result["_fallback"])
                self.assertEqual(result["asset"]["total_asset"], 100000.0)

    def test_healthcheck_accepts_go_platform_in_live_mode(self):
        self.settings.run_mode = "live"
        self.settings.live_trade_enabled = True
        self.settings.execution_mode = "go_platform"
        self.settings.market_mode = "go_platform"

        result = EnvironmentHealthcheck(self.settings).run()
        check_map = {item["name"]: item for item in result.checks}

        self.assertEqual(check_map["execution_mode"]["status"], "ok")
        self.assertEqual(check_map["market_mode"]["status"], "ok")
        self.assertEqual(check_map["execution_adapter_mode"]["status"], "ok")
        self.assertEqual(check_map["market_adapter_mode"]["status"], "ok")
        self.assertEqual(check_map["xtquant_root"]["status"], "warning")
        self.assertEqual(check_map["xtquantservice_root"]["status"], "warning")

if __name__ == "__main__":
    unittest.main()
