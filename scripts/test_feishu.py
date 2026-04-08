"""测试飞书通知 - 使用 Open API"""
import sys
sys.path.insert(0, "src")

from ashare_system.settings import load_settings
from ashare_system.notify.feishu import FeishuNotifier

s = load_settings()
print(f"飞书配置:")
print(f"  app_id: {s.notify.feishu_app_id}")
print(f"  chat_id: {s.notify.feishu_chat_id}")
print(f"  enabled: {s.notify.alerts_enabled}")

notifier = FeishuNotifier(s.notify.feishu_app_id, s.notify.feishu_app_secret, s.notify.feishu_chat_id)
print(f"  notifier enabled: {notifier._enabled}")

print("\n发送测试消息...")
result = notifier.send_alert("ashare-system-v2", "系统启动成功，飞书推送已连通", "info")
print(f"发送结果: {result}")
