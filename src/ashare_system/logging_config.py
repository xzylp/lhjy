"""日志配置 — 统一输出到 logs/ 目录"""

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def setup_logging(logs_dir: Path, level: int = logging.INFO, retention_days: int = 7) -> None:
    """配置全局日志: 同时输出到控制台和文件"""
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "ashare_system.log"

    root = logging.getLogger()
    root.setLevel(level)

    # 清除已有 handler，避免重复
    root.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 文件: 按天轮转，最多保留 retention_days 份，防止日志持续膨胀
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=max(int(retention_days), 1),
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ashare.{name}")
