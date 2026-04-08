"""QMT 自动启动管理器 — 启动/UI自动登录/健康监控/自动重启"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from ..logging_config import get_logger
from ..settings import XtQuantSettings

logger = get_logger("infra.qmt_launcher")

QMT_PROCESS_NAME = "XtMiniQmt.exe"
HEALTH_CHECK_INTERVAL = 30
MAX_RESTART_ATTEMPTS = 3

        # 登录窗口可能的标题关键词
LOGIN_WINDOW_TITLES = ["登录", "Login", "XtMiniQmt", "国金", "迅投"]

# Qt 窗口类名 (兼容 Qt5/Qt6)
QT_WINDOW_CLASSES = ["Qt5QWindowIcon", "Qt6QWindowIcon", "QWidget"]

# 密码输入框可能的类名
PASSWORD_EDIT_CLASSES = ["Edit", "QLineEdit", "LineEdit"]


@dataclass
class QMTStatus:
    running: bool
    pid: int | None = None
    uptime_sec: float = 0.0
    restart_count: int = 0
    last_error: str = ""


class QMTLauncher:
    """QMT 自动启动管理器 (pywinauto UI 自动登录)"""

    def __init__(self, settings: XtQuantSettings) -> None:
        self.settings = settings
        self._start_time: float = 0.0
        self._restart_count: int = 0

    def is_running(self) -> bool:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {QMT_PROCESS_NAME}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return QMT_PROCESS_NAME.lower() in result.stdout.lower()
        except Exception as e:
            logger.warning("检查 QMT 进程失败: %s", e)
            return False

    def get_pid(self) -> int | None:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {QMT_PROCESS_NAME}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().splitlines():
                parts = line.strip('"').split('","')
                if len(parts) >= 2 and QMT_PROCESS_NAME.lower() in parts[0].lower():
                    return int(parts[1])
        except Exception:
            pass
        return None

    def _launch_process(self) -> bool:
        """启动 XtMiniQmt.exe 进程"""
        exe = self.settings.exe_path
        if not exe.exists():
            logger.error("QMT 可执行文件不存在: %s", exe)
            return False
        try:
            subprocess.Popen(
                [str(exe)],
                cwd=str(exe.parent),
                creationflags=subprocess.CREATE_NEW_CONSOLE if hasattr(subprocess, "CREATE_NEW_CONSOLE") else 0,
            )
            self._start_time = time.time()
            logger.info("QMT 进程已启动，等待登录窗口...")
            return True
        except Exception as e:
            logger.error("启动 QMT 进程失败: %s", e)
            return False

    def _auto_login(self, timeout: int = 20) -> bool:
        """用 pywinauto win32 后端自动填入密码并登录"""
        if not self.settings.password:
            logger.warning("未配置 QMT 密码，跳过自动登录")
            return self.is_running()
        try:
            from pywinauto import Desktop
        except ImportError:
            logger.warning("pywinauto 未安装，跳过自动登录")
            return self.is_running()

        logger.info("等待 QMT 登录窗口 (最多 %ds)...", timeout)
        deadline = time.time() + timeout
        login_win = None

        # QMT 使用 Qt5/Qt6，尝试多种后端和类名
        while time.time() < deadline:
            time.sleep(1)
            try:
                desktop = Desktop(backend="win32")
                # 尝试多种 Qt 窗口类名
                for cls in QT_WINDOW_CLASSES:
                    try:
                        wins = desktop.windows(class_name=cls)
                        for w in wins:
                            title = w.window_text()
                            if any(kw in title for kw in LOGIN_WINDOW_TITLES):
                                login_win = w
                                logger.info("找到登录窗口: %s (class=%s)", title, w.class_name())
                                break
                        if login_win:
                            break
                    except Exception:
                        continue
                if login_win:
                    break
            except Exception:
                continue

        if login_win is None:
            logger.warning("未找到 QMT 登录窗口，可能已自动登录")
            return self.is_running()

        # 填入密码并登录
        try:
            time.sleep(0.8)
            # Qt5 的输入框类名通常是 Qt5QWindowIcon 或 Edit
            # 用 type_keys 直接向窗口发送按键
            login_win.set_focus()
            time.sleep(0.3)

            # 尝试找 Edit 控件 (密码框)
            children = login_win.children()
            logger.info("登录窗口子控件数: %d", len(children))

            pwd_filled = False
            for child in children:
                try:
                    cls = child.class_name()
                    text = child.window_text()
                    logger.debug("子控件: class=%s text=%r", cls, text)
                    # 兼容多种 Edit 控件类名
                    if any(c in cls for c in PASSWORD_EDIT_CLASSES):
                        # 找到输入框，清空后填密码
                        child.set_focus()
                        child.type_keys("^a{DELETE}" + self.settings.password, with_spaces=False)
                        logger.info("密码已填入 (class=%s)", cls)
                        pwd_filled = True
                        break
                except Exception:
                    continue

            if not pwd_filled:
                # 备用方案: 直接向窗口发送 Tab + 密码
                logger.info("备用方案: 直接发送密码按键")
                login_win.type_keys("{TAB}" + self.settings.password + "{ENTER}", with_spaces=False)

            # 按 Enter 或点击登录按钮
            time.sleep(0.3)
            btn_clicked = False
            for child in login_win.children():
                try:
                    text = child.window_text()
                    if any(kw in text for kw in ["登录", "Login", "确定", "OK"]):
                        child.click()
                        logger.info("点击登录按钮: %s", text)
                        btn_clicked = True
                        break
                except Exception:
                    continue

            if not btn_clicked:
                login_win.type_keys("{ENTER}")
                logger.info("发送 Enter 键登录")

            time.sleep(3)
            return self.is_running()

        except Exception as e:
            logger.error("自动登录失败: %s", e)
            return self.is_running()

    def start(self) -> bool:
        """启动 QMT 并自动登录"""
        if self.is_running():
            logger.info("QMT 已在运行，跳过启动")
            return True

        if not self._launch_process():
            return False

        # 等待登录窗口并自动填密码
        ok = self._auto_login(timeout=self.settings.startup_wait_sec)

        if ok:
            logger.info("QMT 启动并登录成功")
        else:
            logger.warning("QMT 启动后未能确认登录状态，继续等待...")
            # 再等一段时间
            for _ in range(10):
                time.sleep(2)
                if self.is_running():
                    logger.info("QMT 已就绪")
                    return True

        return self.is_running()

    def stop(self) -> bool:
        if not self.is_running():
            return True
        try:
            subprocess.run(["taskkill", "/F", "/IM", QMT_PROCESS_NAME], capture_output=True, timeout=10)
            time.sleep(2)
            logger.info("QMT 已停止")
            return not self.is_running()
        except Exception as e:
            logger.error("停止 QMT 失败: %s", e)
            return False

    def restart(self) -> bool:
        if self._restart_count >= MAX_RESTART_ATTEMPTS:
            logger.error("QMT 重启次数超限 (%d)，停止重试", MAX_RESTART_ATTEMPTS)
            return False
        logger.warning("重启 QMT (第 %d 次)...", self._restart_count + 1)
        self.stop()
        time.sleep(3)
        success = self.start()
        if success:
            self._restart_count += 1
        return success

    def ensure_running(self) -> bool:
        if self.is_running():
            return True
        if not self.settings.auto_start:
            logger.warning("QMT 未运行且 auto_start=false，跳过")
            return False
        return self.start()

    def get_status(self) -> QMTStatus:
        running = self.is_running()
        pid = self.get_pid() if running else None
        uptime = time.time() - self._start_time if running and self._start_time > 0 else 0.0
        return QMTStatus(running=running, pid=pid, uptime_sec=uptime, restart_count=self._restart_count)

    def watchdog_loop(self, interval: int = HEALTH_CHECK_INTERVAL) -> None:
        """守护循环 — 定期检查 QMT 健康，崩溃时自动重启"""
        logger.info("QMT 守护进程启动 (检查间隔 %ds)", interval)
        while True:
            try:
                if not self.is_running():
                    logger.warning("QMT 进程消失，尝试重启...")
                    self.restart()
                else:
                    logger.debug("QMT 健康检查: 正常运行")
                time.sleep(interval)
            except KeyboardInterrupt:
                logger.info("QMT 守护进程停止")
                break
            except Exception as e:
                logger.error("守护进程异常: %s", e)
                time.sleep(interval)
