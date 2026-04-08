"""直接测试 XtQuant 适配器"""
import sys
sys.path.insert(0, "src")

from ashare_system.settings import load_settings
from ashare_system.infra.adapters import XtQuantExecutionAdapter

s = load_settings()

print("=== 创建 XtQuantExecutionAdapter ===")
try:
    adapter = XtQuantExecutionAdapter(s)
    print(f"Adapter mode: {adapter.mode}")
    print(f"Trader: {adapter._trader}")
    print(f"Account: {adapter._account}")

    print("\n=== 测试查询余额 ===")
    balance = adapter.get_balance(s.xtquant.account_id)
    print(f"Balance: {balance}")

    print("\n=== 测试查询持仓 ===")
    positions = adapter.get_positions(s.xtquant.account_id)
    print(f"Positions count: {len(positions)}")
    for p in positions[:3]:
        print(f"  {p.symbol}: {p.quantity} 股")

except Exception as e:
    print(f"错误: {e}")
    import traceback
    traceback.print_exc()
