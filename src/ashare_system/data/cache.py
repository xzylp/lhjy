"""本地数据缓存管理"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..contracts import BarSnapshot
from ..infra.safe_json import atomic_write_json, read_json_with_backup
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
        atomic_write_json(path, data)

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


class KlineCache:
    """K 线缓存与增量拉取门面。"""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._stats_path = self.cache_dir / "_stats.json"

    def get_or_fetch(
        self,
        symbol: str,
        period: str,
        count: int,
        market_adapter: Any,
        *,
        end_time: str | None = None,
    ) -> list[BarSnapshot]:
        cached = self._read_entry(symbol, period)
        now_ts = time.time()
        if cached:
            age_seconds = now_ts - float(cached.get("last_updated_ts", 0.0) or 0.0)
            if age_seconds < 3600:
                self._touch_entry(symbol, period, cached, hit=True)
                return [BarSnapshot(**item) for item in list(cached.get("bars") or [])][-count:]
        bars = list(market_adapter.get_bars([symbol], period=period, count=count, end_time=end_time) or [])
        self._write_entry(symbol, period, bars, fetched_at=now_ts, hit=False)
        return bars

    def stats(self) -> dict[str, Any]:
        payload = read_json_with_backup(self._stats_path, default={"hits": 0, "misses": 0})
        if not isinstance(payload, dict):
            payload = {"hits": 0, "misses": 0}
        cache_files = [item for item in self.cache_dir.glob("*.json") if item.name != "_stats.json"]
        hits = int(payload.get("hits", 0) or 0)
        misses = int(payload.get("misses", 0) or 0)
        total = hits + misses
        return {
            "cache_file_count": len(cache_files),
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / total, 4) if total > 0 else 0.0,
        }

    def cleanup(self, *, max_idle_seconds: int = 7 * 24 * 3600) -> int:
        removed = 0
        now_ts = time.time()
        for path in self.cache_dir.glob("*.json"):
            if path.name == "_stats.json":
                continue
            payload = read_json_with_backup(path, default={})
            last_accessed_ts = float((payload or {}).get("last_accessed_ts", 0.0) or 0.0)
            if last_accessed_ts > 0.0 and now_ts - last_accessed_ts <= max_idle_seconds:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def _entry_path(self, symbol: str, period: str) -> Path:
        safe_symbol = symbol.replace(".", "_")
        return self.cache_dir / f"{safe_symbol}_{period}.json"

    def _read_entry(self, symbol: str, period: str) -> dict[str, Any]:
        payload = read_json_with_backup(self._entry_path(symbol, period), default={})
        return payload if isinstance(payload, dict) else {}

    def _touch_entry(self, symbol: str, period: str, payload: dict[str, Any], *, hit: bool) -> None:
        payload["last_accessed_at"] = datetime.now().isoformat()
        payload["last_accessed_ts"] = time.time()
        atomic_write_json(self._entry_path(symbol, period), payload)
        self._update_stats(hit=hit)

    def _write_entry(self, symbol: str, period: str, bars: list[BarSnapshot], *, fetched_at: float, hit: bool) -> None:
        payload = {
            "symbol": symbol,
            "period": period,
            "last_updated_at": datetime.fromtimestamp(fetched_at).isoformat(),
            "last_updated_ts": fetched_at,
            "last_accessed_at": datetime.fromtimestamp(fetched_at).isoformat(),
            "last_accessed_ts": fetched_at,
            "bars": [item.model_dump() for item in bars],
        }
        atomic_write_json(self._entry_path(symbol, period), payload)
        self._update_stats(hit=hit)

    def _update_stats(self, *, hit: bool) -> None:
        payload = read_json_with_backup(self._stats_path, default={"hits": 0, "misses": 0})
        if not isinstance(payload, dict):
            payload = {"hits": 0, "misses": 0}
        key = "hits" if hit else "misses"
        payload[key] = int(payload.get(key, 0) or 0) + 1
        atomic_write_json(self._stats_path, payload)
