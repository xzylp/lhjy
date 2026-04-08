"""本地数据缓存管理"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..contracts import BarSnapshot
from ..logging_config import get_logger

logger = get_logger("data.cache")


class DataCache:
    """基于文件的本地数据缓存"""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_bars(self, symbol: str, period: str) -> list[BarSnapshot] | None:
        """从缓存读取 K 线，过期返回 None"""
        path = self._bar_path(symbol, period)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - data.get("ts", 0) > 3600:
            return None
        return [BarSnapshot(**b) for b in data.get("bars", [])]

    def put_bars(self, symbol: str, period: str, bars: list[BarSnapshot]) -> None:
        """写入 K 线缓存"""
        path = self._bar_path(symbol, period)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "ts": time.time(),
            "bars": [b.model_dump() for b in bars],
        }
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def invalidate(self, symbol: str, period: str) -> None:
        path = self._bar_path(symbol, period)
        if path.exists():
            path.unlink()

    def clear_all(self) -> int:
        """清空全部缓存，返回删除文件数"""
        count = 0
        for f in self.cache_dir.rglob("*.json"):
            f.unlink()
            count += 1
        logger.info("缓存已清空: %d 个文件", count)
        return count

    def _bar_path(self, symbol: str, period: str) -> Path:
        safe_symbol = symbol.replace(".", "_")
        return self.cache_dir / f"{safe_symbol}_{period}.json"
