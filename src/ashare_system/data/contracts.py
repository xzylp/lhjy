"""数据层契约"""

from typing import Literal

from pydantic import BaseModel, Field

from ..contracts import BarSnapshot, DataQuality


StalenessLevel = Literal["fresh", "warm", "stale", "expired", "missing"]


class CleanedBar(BaseModel):
    """清洗后的 K 线数据"""
    bar: BarSnapshot
    quality: DataQuality
    adjusted_close: float = 0.0
    change_pct: float = 0.0
    turnover_rate: float = 0.0
    is_suspended: bool = False
    is_st: bool = False
    is_limit_up: bool = False
    is_limit_down: bool = False


class StockMeta(BaseModel):
    """股票元数据"""
    symbol: str
    name: str = ""
    list_days: int = 0
    industry: str = ""
    is_st: bool = False
    total_shares: float = 0.0
    float_shares: float = 0.0


class FetchResult(BaseModel):
    """数据获取结果"""
    symbols: list[str] = Field(default_factory=list)
    bars: list[BarSnapshot] = Field(default_factory=list)
    quality: DataQuality = Field(default_factory=lambda: DataQuality(source="unavailable"))
    errors: list[str] = Field(default_factory=list)


class FreshnessMeta(BaseModel):
    """统一新鲜度元信息。"""

    source_at: str | None = None
    fetched_at: str | None = None
    generated_at: str | None = None
    expires_at: str | None = None
    staleness_level: StalenessLevel = "missing"


class MarketBarRecord(FreshnessMeta):
    """标准化历史 K 线记录。"""

    symbol: str
    name: str = ""
    period: Literal["1m", "5m", "15m", "60m", "1d"]
    trade_time: str
    source: str = "xtquant"
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0
    pre_close: float = 0.0


class MarketSnapshotRecord(FreshnessMeta):
    """标准化实时行情快照。"""

    symbol: str
    name: str = ""
    source: str = "xtquant"
    snapshot_at: str
    last_price: float
    bid_price: float
    ask_price: float
    pre_close: float = 0.0
    change_pct: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    turnover_rate: float = 0.0


class IndexSnapshotRecord(FreshnessMeta):
    """标准化指数/大盘快照。"""

    index_symbol: str
    index_name: str = ""
    source: str = "xtquant"
    snapshot_at: str
    last_price: float
    change_pct: float = 0.0
    volume: float = 0.0
    amount: float = 0.0


class MarketStructureSnapshotRecord(FreshnessMeta):
    """标准化市场结构快照。"""

    snapshot_at: str
    source: str = "derived"
    limit_up_count: int = 0
    limit_down_count: int = 0
    limit_up_streak_highest: int = 0
    turnover_total: float = 0.0
    broad_breadth: float = 0.0
    market_sentiment_label: str = ""
    sector_rankings: list[dict] = Field(default_factory=list)


class EventRecord(FreshnessMeta):
    """统一事件记录。"""

    event_id: str
    symbol: str = ""
    name: str = ""
    source: str
    source_type: str = ""
    category: str
    title: str
    summary: str
    severity: str = "info"
    sentiment: str = "neutral"
    event_at: str
    recorded_at: str | None = None
    dedupe_key: str = ""
    impact_scope: Literal["market", "sector", "symbol", "macro", "unknown"] = "unknown"
    evidence_url: str = ""
    payload: dict = Field(default_factory=dict)


class SymbolContextRecord(FreshnessMeta):
    """标准化个股上下文记录。"""

    trade_date: str
    symbol: str
    name: str = ""
    signature: str = ""
    payload: dict = Field(default_factory=dict)


class DossierRecord(FreshnessMeta):
    """标准化候选 dossier 记录。"""

    trade_date: str
    symbol: str
    name: str = ""
    signature: str = ""
    payload: dict = Field(default_factory=dict)
