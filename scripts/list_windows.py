"""列出所有当前窗口标题，用于找 QMT 登录窗口"""
import sys
sys.path.insert(0, r"D:\Coding\lhjy\ashare-system-v2\src")

try:
    from pywinauto import Desktop
    d = Desktop(backend="uia")
    print("=== 当前所有窗口 (uia) ===")
    for w in d.windows():
        try:
            title = w.window_text()
            cls = w.class_name()
            if title:
                print(f"  标题: {repr(title):40s}  类名: {cls}")
        except Exception:
            pass
except Exception as e:
    print(f"uia 失败: {e}")

try:
    from pywinauto import Desktop
    d = Desktop(backend="win32")
    print("\n=== 当前所有窗口 (win32) ===")
    for w in d.windows():
        try:
            title = w.window_text()
            cls = w.class_name()
            if title:
                print(f"  标题: {repr(title):40s}  类名: {cls}")
        except Exception:
            pass
except Exception as e:
    print(f"win32 失败: {e}")
