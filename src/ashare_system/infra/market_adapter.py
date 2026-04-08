"""XtQuant 行情数据适配器"""

from __future__ import annotations

from datetime import datetime, timedelta
from dataclasses import dataclass, field
from uuid import uuid4

from ..contracts import BarSnapshot, QuoteSnapshot
from ..infra.filters import get_price_limit_ratio
from ..settings import AppSettings
from .filters import filter_a_share, filter_main_board
from .xtquant_runtime import load_xtquant_modules


class MarketDataAdapter:
    """行情数据适配器基类"""

    def sync_history(self, symbols: list[str], period: str, start_time: str) -> dict:
        raise NotImplementedError

    def subscribe(self, symbols: list[str]) -> dict:
        raise NotImplementedError

    def get_snapshots(self, symbols: list[str]) -> list[QuoteSnapshot]:
        raise NotImplementedError

    def get_bars(self, symbols: list[str], period: str, count: int = 1) -> list[BarSnapshot]:
        raise NotImplementedError

    def get_main_board_universe(self) -> list[str]:
        raise NotImplementedError

    def get_a_share_universe(self) -> list[str]:
        raise NotImplementedError

    def get_sectors(self) -> list[str]:
        raise NotImplementedError

    def get_sector_symbols(self, sector_name: str) -> list[str]:
        raise NotImplementedError

    def get_index_quotes(self, symbols: list[str]) -> list[QuoteSnapshot]:
        raise NotImplementedError

    def get_symbol_name(self, symbol: str) -> str:
        raise NotImplementedError


@dataclass
class MockMarketDataAdapter(MarketDataAdapter):
    """模拟行情适配器"""
    subscribed: set[str] = field(default_factory=set)
    universe: list[str] = field(default_factory=lambda: [
        "600519.SH", "600036.SH", "000001.SZ", "002594.SZ", "300750.SZ", "688981.SH",
    ])
    symbol_names: dict[str, str] = field(default_factory=lambda: {
        "600519.SH": "贵州茅台",
        "600036.SH": "招商银行",
        "000001.SZ": "平安银行",
        "002594.SZ": "比亚迪",
        "300750.SZ": "宁德时代",
        "688981.SH": "中芯国际",
        "204003.SH": "沪市3天逆回购",
        "204004.SH": "沪市4天逆回购",
        "131800.SZ": "深市3天逆回购",
        "131809.SZ": "深市4天逆回购",
    })

    def sync_history(self, symbols: list[str], period: str, start_time: str) -> dict:
        return {"accepted_symbols": filter_a_share(symbols), "period": period, "start_time": start_time}

    def subscribe(self, symbols: list[str]) -> dict:
        accepted = filter_a_share(symbols)
        self.subscribed.update(accepted)
        return {"accepted_symbols": accepted, "subscription_count": len(self.subscribed)}

    def get_snapshots(self, symbols: list[str]) -> list[QuoteSnapshot]:
        accepted = filter_a_share(symbols or self.get_main_board_universe())
        return [
            QuoteSnapshot(
                symbol=s,
                name=self.get_symbol_name(s),
                last_price=10.0 + i,
                bid_price=9.99 + i,
                ask_price=10.01 + i,
                volume=100_000 + i * 1000,
            )
            for i, s in enumerate(accepted)
        ]

    def get_bars(self, symbols: list[str], period: str, count: int = 1) -> list[BarSnapshot]:
        accepted = filter_a_share(symbols or self.get_main_board_universe())
        result: list[BarSnapshot] = []
        base_time = datetime.fromisoformat("2026-04-03T09:31:00+08:00")
        bar_count = max(int(count or 1), 1)
        for i, symbol in enumerate(accepted):
            limit_ratio = get_price_limit_ratio(symbol)
            for index in range(bar_count):
                day_offset = bar_count - index - 1
                anchor = base_time - timedelta(days=day_offset)
                pre_close = round(10.0 + i + index * 0.05, 4)
                if index and index % 15 == 0:
                    close = round(pre_close * (1 + limit_ratio), 4)
                    open_price = round(pre_close * 1.012, 4)
                    low = round(pre_close * 1.004, 4)
                    high = close
                elif index and index % 10 == 0:
                    high = round(pre_close * (1 + limit_ratio), 4)
                    close = round(pre_close * (1 + limit_ratio * 0.35), 4)
                    open_price = round(pre_close * (1 + limit_ratio * 0.12), 4)
                    low = round(pre_close * 0.988, 4)
                else:
                    drift = ((index % 6) - 2) * 0.012
                    close = round(pre_close * (1 + drift), 4)
                    open_price = round(pre_close * (1 + drift * 0.4), 4)
                    high = round(max(open_price, close) * 1.012, 4)
                    low = round(min(open_price, close) * 0.988, 4)
                result.append(
                    BarSnapshot(
                        symbol=symbol,
                        period=period,
                        open=open_price,
                        high=high,
                        low=low,
                        close=round(close, 4),
                        volume=120_000 + index * 1_500,
                        amount=1_500_000 + index * 12_000,
                        trade_time=anchor.isoformat(),
                        pre_close=pre_close,
                    )
                )
        return result

    def get_main_board_universe(self) -> list[str]:
        return filter_main_board(self.universe)

    def get_a_share_universe(self) -> list[str]:
        return filter_a_share(self.universe)

    def get_sectors(self) -> list[str]:
        return ["行业A", "行业B"]

    def get_sector_symbols(self, sector_name: str) -> list[str]:
        return [self.universe[0]] if self.universe else []

    def get_index_quotes(self, symbols: list[str]) -> list[QuoteSnapshot]:
        return [
            QuoteSnapshot(
                symbol=s,
                name=self.get_symbol_name(s),
                last_price=1.8 + i * 0.05,
                bid_price=1.79 + i * 0.05,
                ask_price=1.81 + i * 0.05,
                volume=1_000_000 + i * 10_000,
            )
            for i, s in enumerate(symbols)
        ]

    def get_symbol_name(self, symbol: str) -> str:
        return self.symbol_names.get(symbol, symbol)


class XtQuantMarketDataAdapter(MarketDataAdapter):
    """XtQuant 真实行情适配器"""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.modules = load_xtquant_modules(str(settings.xtquant.root), str(settings.xtquant.service_root))
        self.xtdata = self.modules["xtdata"]
        self.subscriptions: dict[str, int] = {}
        self._name_cache: dict[str, str] = {}
        if settings.xtquant.market_port is not None:
            self.xtdata.connect(settings.xtquant.market_host, settings.xtquant.market_port)

    def sync_history(self, symbols: list[str], period: str, start_time: str) -> dict:
        accepted = filter_a_share(symbols)
        for symbol in accepted:
            self.xtdata.download_history_data(symbol, period=period, start_time=start_time)
        return {"accepted_symbols": accepted, "period": period, "start_time": start_time}

    def subscribe(self, symbols: list[str]) -> dict:
        accepted = filter_a_share(symbols)
        for symbol in accepted:
            self.subscriptions[symbol] = self.xtdata.subscribe_quote(symbol, period="tick", count=0, callback=None)
        return {"accepted_symbols": accepted, "subscription_count": len(self.subscriptions)}

    def _get_snapshot_map(self, symbols: list[str]) -> dict[str, dict[str, float]]:
        try:
            raw = self.xtdata.get_full_tick(symbols)
            result = {}
            for symbol, item in raw.items():
                bid = item.get("bidPrice", [0.0])
                ask = item.get("askPrice", [0.0])
                result[symbol] = {
                    "last_price": float(item.get("lastPrice", 0.0) or 0.0),
                    "bid_price": float((bid[0] if isinstance(bid, list) and bid else bid) or 0.0),
                    "ask_price": float((ask[0] if isinstance(ask, list) and ask else ask) or 0.0),
                    "volume": float(item.get("volume", 0.0) or 0.0),
                    "pre_close": float(item.get("preClose", 0.0) or 0.0),
                }
            return result
        except Exception:
            frame_map = self.xtdata.get_market_data_ex(
                field_list=["close", "volume", "preClose"], stock_list=symbols, period="1m", count=1,
            )
            result = {}
            for symbol in symbols:
                frame = frame_map.get(symbol)
                if frame is None or frame.empty:
                    continue
                row = frame.iloc[-1]
                lp = float(row["close"])
                result[symbol] = {"last_price": lp, "bid_price": lp, "ask_price": lp, "volume": float(row["volume"]), "pre_close": float(row.get("preClose", 0.0))}
            return result

    def get_snapshots(self, symbols: list[str]) -> list[QuoteSnapshot]:
        accepted = filter_a_share(symbols or self.get_main_board_universe())
        snap = self._get_snapshot_map(accepted)
        return [
            QuoteSnapshot(
                symbol=s,
                name=self.get_symbol_name(s),
                last_price=float(snap.get(s, {}).get("last_price", 0.0)),
                bid_price=float(snap.get(s, {}).get("bid_price", 0.0)),
                ask_price=float(snap.get(s, {}).get("ask_price", 0.0)),
                volume=float(snap.get(s, {}).get("volume", 0.0)),
                pre_close=float(snap.get(s, {}).get("pre_close", 0.0)),
            )
            for s in accepted
        ]

    def get_bars(self, symbols: list[str], period: str, count: int = 1) -> list[BarSnapshot]:
        accepted = filter_a_share(symbols or self.get_main_board_universe())
        frame_map = self.xtdata.get_market_data_ex(
            field_list=["open", "high", "low", "close", "volume", "amount", "preClose"],
            stock_list=accepted,
            period=period,
            count=max(int(count or 1), 1),
        )
        bars = []
        for symbol in accepted:
            frame = frame_map.get(symbol)
            if frame is None or frame.empty:
                continue
            for trade_time, row in frame.iterrows():
                bars.append(BarSnapshot(
                    symbol=symbol,
                    period=period,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    amount=float(row["amount"]),
                    trade_time=str(trade_time),
                    pre_close=float(row.get("preClose", 0.0) or 0.0),
                ))
        return bars

    def get_main_board_universe(self) -> list[str]:
        return filter_main_board(self.get_a_share_universe())

    def get_a_share_universe(self) -> list[str]:
        sectors = ["沪深A股", "上证A股", "深证A股", "创业板", "科创板", "北证A股", "北交所"]
        symbols: list[str] = []
        for name in sectors:
            try:
                symbols.extend(self.xtdata.get_stock_list_in_sector(name))
            except Exception:
                continue
        if not symbols:
            symbols = self.xtdata.get_stock_list_in_sector("沪深A股")
        return filter_a_share(symbols)

    def get_sectors(self) -> list[str]:
        return [s for s in self.xtdata.get_sector_list() if any(k in s for k in ("行业", "概念", "地域", "板块"))]

    def get_sector_symbols(self, sector_name: str) -> list[str]:
        return self.xtdata.get_stock_list_in_sector(sector_name)

    def get_index_quotes(self, symbols: list[str]) -> list[QuoteSnapshot]:
        snap = self._get_snapshot_map(symbols)
        return [
            QuoteSnapshot(
                symbol=s,
                name=self.get_symbol_name(s),
                last_price=float(snap.get(s, {}).get("last_price", 0.0)),
                bid_price=float(snap.get(s, {}).get("bid_price", 0.0)),
                ask_price=float(snap.get(s, {}).get("ask_price", 0.0)),
                volume=float(snap.get(s, {}).get("volume", 0.0)),
                pre_close=float(snap.get(s, {}).get("pre_close", 0.0)),
            )
            for s in symbols
        ]

    def get_symbol_name(self, symbol: str) -> str:
        cached = self._name_cache.get(symbol)
        if cached:
            return cached
        try:
            detail = self.xtdata.get_instrument_detail(symbol)
            name = str(detail.get("InstrumentName") or detail.get("instrumentName") or detail.get("stockName") or "").strip()
            if name:
                self._name_cache[symbol] = name
                return name
        except Exception:
            pass
        return symbol


def build_market_adapter(mode: str, settings: AppSettings) -> MarketDataAdapter:
    if mode == "xtquant":
        try:
            adapter = XtQuantMarketDataAdapter(settings)
            adapter.mode = "xtquant"
            return adapter
        except Exception as exc:
            if settings.run_mode == "live":
                raise RuntimeError(f"live 模式禁止行情适配器回退到 mock-fallback: {exc}") from exc
            adapter = MockMarketDataAdapter()
            adapter.mode = "mock-fallback"
            return adapter
    adapter = MockMarketDataAdapter()
    adapter.mode = "mock"
    return adapter
