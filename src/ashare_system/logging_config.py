"""日志配置 — 统一输出到 logs/ 目录"""

import logging
import sys
from pathlib import Path


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> None:
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

    # 文件 (追加模式, UTF-8)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ashare.{name}")
