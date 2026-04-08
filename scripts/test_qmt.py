"""QMT 自动启动测试脚本"""
import sys
sys.path.insert(0, r"D:\Coding\lhjy\ashare-system-v2\src")

from ashare_system.infra.qmt_launcher import QMTLauncher
from ashare_system.settings import load_settings

s = load_settings()
launcher = QMTLauncher(s.xtquant)

print(f"QMT exe: {s.xtquant.exe_path}")
print(f"QMT exe exists: {s.xtquant.exe_path.exists()}")
print(f"QMT running: {launcher.is_running()}")
print(f"password configured: {'yes' if s.xtquant.password else 'no'}")
print()
print("启动 QMT...")
ok = launcher.start()
print(f"启动结果: {ok}")
status = launcher.get_status()
print(f"PID: {status.pid}")
print(f"running: {status.running}")
