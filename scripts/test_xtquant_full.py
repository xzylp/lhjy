"""测试 XtQuant 完整连接"""
import sys
sys.path.insert(0, "src")

from ashare_system.settings import load_settings
from ashare_system.infra.xtquant_runtime import load_xtquant_modules
from ashare_system.infra.qmt_launcher import QMTLauncher

s = load_settings()

print("=== 1. 加载 XtQuant 模块 ===")
modules = load_xtquant_modules(str(s.xtquant.root), str(s.xtquant.service_root))
print(f"成功: {list(modules.keys())}")

print("\n=== 2. 检查 QMT 状态 ===")
launcher = QMTLauncher(s.xtquant)
print(f"QMT running: {launcher.is_running()}")

print("\n=== 3. 测试 XtQuant 连接 ===")
xttrader = modules["xttrader"]
xttype = modules["xttype"]
xtconstant = modules["xtconstant"]

# 创建交易员
trader = xttrader.XtQuantTrader(str(s.xtquant.userdata), int(s.xtquant.session_id))
print(f"Trader created")

# 启动
result = trader.start()
print(f"start result: {result}")

# 连接
result = trader.connect()
print(f"connect result: {result}")

if result == 0:
    print("连接成功!")
    # 创建账户
    account = xttype.StockAccount(s.xtquant.account_id, s.xtquant.account_type)
    print(f"Account: {account.account_id}")
else:
    print(f"连接失败! 错误码: {result}")
