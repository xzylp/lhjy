"""运行时动态配置 — Agent 可通过 API 读写，持久化到文件"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock

from pydantic import BaseModel, Field

from .logging_config import get_logger

logger = get_logger("runtime_config")


class TradingScope(BaseModel):
    """买股范围"""
    allow_main_board: bool = True       # 主板 600/000
    allow_chinext: bool = False         # 创业板 300
    allow_star: bool = False            # 科创板 688
    allow_bse: bool = False             # 北交所
    allow_st: bool = False              # ST股

    def allowed_prefixes(self) -> list[str]:
        prefixes = []
        if self.allow_main_board:
            prefixes.extend(["600", "601", "603", "605", "000", "001", "002", "003"])
        if self.allow_chinext:
            prefixes.extend(["300", "301"])
        if self.allow_star:
            prefixes.extend(["688", "689"])
        if self.allow_bse:
            prefixes.extend(["43", "83", "87", "88", "92"])
        return prefixes

    def is_allowed(self, symbol: str) -> bool:
        code = symbol.split(".", 1)[0] if "." in symbol else symbol
        return any(code.startswith(p) for p in self.allowed_prefixes())


class SnapshotPolicy(BaseModel):
    """状态快照保留策略"""

    market_snapshot_ttl_seconds: int = 600
    dossier_retention_trading_days: int = 5
    archive_retention_trading_days: int = 20


class WatchPolicy(BaseModel):
    """盯盘采集与落盘节奏"""

    candidate_poll_seconds: int = 300
    focus_poll_seconds: int = 60
    execution_poll_seconds: int = 30
    heartbeat_save_seconds: int = 600
    auction_heartbeat_save_seconds: int = 300
    event_debounce_seconds: int = 45


class RuntimeConfig(BaseModel):
    """运行时可调参数 — 通过飞书/Agent动态修改"""
    # 选股
    max_buy_count: int = 3              # 每次最多买入几只
    max_hold_count: int = 5             # 最多同时持有几只
    screener_pool_size: int = 30        # 粗筛候选池大小

    # 仓位
    max_total_position: float = 0.8     # 最高总仓位 (0-1)
    equity_position_limit: float = 0.2  # 测试阶段股票仓位上限 (不含逆回购)
    max_single_position: float = 0.25   # 单票最高仓位 (0-1)
    max_single_amount: float = 50000.0  # 单票最高金额 (元)
    reverse_repo_target_ratio: float = 0.7  # 逆回购目标占比
    minimum_total_invested_amount: float = 100000.0  # 测试期总持仓基线
    reverse_repo_reserved_amount: float = 70000.0  # 测试期逆回购保留金额
    reverse_repo_auto_repurchase_enabled: bool = True  # 逆回购到期后自动回补
    reverse_repo_min_term_days: int = 3  # 自动回补最短期限
    reverse_repo_max_term_days: int = 4  # 自动回补最长期限
    reverse_repo_prefer_longer_term: bool = True  # 优先更长期限

    # 买股范围
    scope: TradingScope = Field(default_factory=TradingScope)

    # 风控
    daily_loss_limit: float = 0.05      # 日亏损上限
    execution_price_deviation_pct: float = 0.02  # 实盘下单价偏离盘口阈值
    pending_order_warn_seconds: int = 300  # 未决订单告警时长阈值
    pending_order_auto_action: str = "alert_only"  # alert_only | cancel
    pending_order_cancel_after_seconds: int = 900  # 超时后自动撤单阈值
    emergency_stop: bool = False        # 紧急停止交易
    trading_halt_reason: str = ""       # 临时停交易由说明

    # 快照 / 盯盘
    snapshots: SnapshotPolicy = Field(default_factory=SnapshotPolicy)
    watch: WatchPolicy = Field(default_factory=WatchPolicy)


class RuntimeConfigManager:
    """运行时配置管理器 — 线程安全，自动持久化"""

    def __init__(self, config_path: Path) -> None:
        self._path = config_path
        self._lock = Lock()
        self._config = self._load()

    def get(self) -> RuntimeConfig:
        with self._lock:
            return self._config.model_copy()

    def update(self, **kwargs) -> RuntimeConfig:
        """部分更新配置"""
        with self._lock:
            data = self._config.model_dump()
            for key, value in kwargs.items():
                self._apply_update(data, key, value)
            self._config = RuntimeConfig(**data)
            self._save()
            logger.info("运行时配置更新: %s", kwargs)
            return self._config.model_copy()

    def _apply_update(self, data: dict, key: str, value) -> None:
        if "." in key:
            cursor = data
            parts = key.split(".")
            for part in parts[:-1]:
                nested = cursor.get(part)
                if not isinstance(nested, dict):
                    nested = {}
                    cursor[part] = nested
                cursor = nested
            cursor[parts[-1]] = value
            return

        current = data.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            for nested_key, nested_value in value.items():
                self._apply_update(current, nested_key, nested_value)
            return
        data[key] = value

    def _load(self) -> RuntimeConfig:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                return RuntimeConfig(**data)
            except Exception as e:
                logger.warning("配置加载失败，使用默认值: %s", e)
        return RuntimeConfig()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._config.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
