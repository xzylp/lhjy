"""测试 XtQuant 模块加载"""
import sys
sys.path.insert(0, "src")

from ashare_system.infra.xtquant_runtime import load_xtquant_modules
from ashare_system.settings import load_settings

s = load_settings()
print("加载 XtQuant 模块...")
modules = load_xtquant_modules(str(s.xtquant.root), str(s.xtquant.service_root))
print(f"成功: {list(modules.keys())}")
print("xtdata version:", modules["xtdata"].__version__)
