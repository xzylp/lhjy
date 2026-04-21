"""XtQuant 行情数据适配器"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx

from ..contracts import BarSnapshot, OrderBookLevel, OrderBookSnapshot, QuoteSnapshot
from ..data.quality import BarQualityChecker
from ..infra.filters import get_price_limit_ratio
from ..settings import AppSettings
from .filters import filter_a_share, filter_main_board
from .xtquant_runtime import load_xtquant_modules
from ..logging_config import get_logger
from .go_client import GoPlatformClient

logger = get_logger("market.adapter")
bar_quality_checker = BarQualityChecker()


class MarketDataAdapter:
    """行情数据适配器基类"""

    def sync_history(self, symbols: list[str], period: str, start_time: str) -> dict:
        raise NotImplementedError

    def subscribe(self, symbols: list[str]) -> dict:
        raise NotImplementedError

    def get_snapshots(self, symbols: list[str]) -> list[QuoteSnapshot]:
        raise NotImplementedError

    def get_bars(
        self,
        symbols: list[str],
        period: str,
        count: int = 1,
        end_time: str | None = None,
    ) -> list[BarSnapshot]:
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

    def search_symbols(self, query: str, limit: int = 10) -> list[dict[str, str]]:
        raise NotImplementedError

    def get_order_book_snapshots(self, symbols: list[str]) -> list[OrderBookSnapshot]:
        raise NotImplementedError


@dataclass
class MockMarketDataAdapter(MarketDataAdapter):
    """模拟行情适配器"""
    subscribed: set[str] = field(default_factory=set)
    universe: list[str] = field(default_factory=lambda: [
        "600519.SH", "600036.SH", "000001.SZ", "002202.SZ", "002882.SZ", "002594.SZ", "300750.SZ", "688981.SH",
    ])
    symbol_names: dict[str, str] = field(default_factory=lambda: {
        "600519.SH": "贵州茅台",
        "600036.SH": "招商银行",
        "000001.SZ": "平安银行",
        "002202.SZ": "金风科技",
        "002882.SZ": "金龙羽",
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

    def get_bars(
        self,
        symbols: list[str],
        period: str,
        count: int = 1,
        end_time: str | None = None,
    ) -> list[BarSnapshot]:
        accepted = filter_a_share(symbols or self.get_main_board_universe())
        result: list[BarSnapshot] = []
        base_time = datetime.fromisoformat(end_time) if end_time else datetime.fromisoformat("2026-04-03T09:31:00+08:00")
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

    def search_symbols(self, query: str, limit: int = 10) -> list[dict[str, str]]:
        normalized = str(query or "").strip()
        if not normalized:
            return []
        upper_query = normalized.upper()
        results: list[dict[str, str]] = []
        seen: set[str] = set()
        for symbol in self.get_a_share_universe():
            name = self.get_symbol_name(symbol)
            if symbol == upper_query or name == normalized:
                return [{"symbol": symbol, "name": name}]
        for symbol in self.get_a_share_universe():
            if symbol in seen:
                continue
            name = self.get_symbol_name(symbol)
            if normalized and normalized in name:
                results.append({"symbol": symbol, "name": name})
                seen.add(symbol)
                if len(results) >= limit:
                    break
        return results

    def get_order_book_snapshots(self, symbols: list[str]) -> list[OrderBookSnapshot]:
        accepted = filter_a_share(symbols or self.get_main_board_universe())
        snapshots: list[OrderBookSnapshot] = []
        for index, symbol in enumerate(accepted):
            last_price = 10.0 + index
            captured_at = datetime.now().isoformat()
            bid_base = 12000 + index * 500
            ask_base = 9000 + index * 400
            snapshots.append(
                OrderBookSnapshot(
                    symbol=symbol,
                    name=self.get_symbol_name(symbol),
                    last_price=last_price,
                    pre_close=last_price * 0.985,
                    total_volume=250_000 + index * 5_000,
                    total_amount=(250_000 + index * 5_000) * last_price,
                    bids=[
                        OrderBookLevel(price=round(last_price - step * 0.01, 2), volume=float(bid_base - step * 900))
                        for step in range(5)
                    ],
                    asks=[
                        OrderBookLevel(price=round(last_price + (step + 1) * 0.01, 2), volume=float(ask_base + step * 600))
                        for step in range(5)
                    ],
                    buy_volume=float(bid_base * 2.2),
                    sell_volume=float(ask_base * 1.6),
                    large_buy_volume=float(bid_base * 0.8),
                    large_sell_volume=float(ask_base * 0.45),
                    captured_at=captured_at,
                )
            )
        return snapshots


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

    def get_bars(
        self,
        symbols: list[str],
        period: str,
        count: int = 1,
        end_time: str | None = None,
    ) -> list[BarSnapshot]:
        accepted = filter_a_share(symbols or self.get_main_board_universe())
        kwargs = {
            "field_list": ["open", "high", "low", "close", "volume", "amount", "preClose"],
            "stock_list": accepted,
            "period": period,
            "count": max(int(count or 1), 1),
        }
        if end_time:
            kwargs["end_time"] = end_time
        try:
            frame_map = self.xtdata.get_market_data_ex(**kwargs)
        except TypeError:
            kwargs.pop("end_time", None)
            frame_map = self.xtdata.get_market_data_ex(**kwargs)
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
        cleaned, alerts = bar_quality_checker.validate_bars(bars)
        for alert in alerts[:50]:
            logger.warning("行情质量告警[%s] %s %s %s", alert.issue, alert.symbol, alert.trade_time, alert.detail)
        return cleaned

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

    def get_order_book_snapshots(self, symbols: list[str]) -> list[OrderBookSnapshot]:
        accepted = filter_a_share(symbols or self.get_main_board_universe())
        if not accepted:
            return []
        try:
            raw = self.xtdata.get_full_tick(accepted)
        except Exception:
            raw = {}
        snapshots: list[OrderBookSnapshot] = []
        captured_at = datetime.now().isoformat()
        for symbol in accepted:
            item = dict(raw.get(symbol) or {})
            bid_prices = item.get("bidPrice") or []
            ask_prices = item.get("askPrice") or []
            bid_volumes = item.get("bidVol") or item.get("bidVolume") or []
            ask_volumes = item.get("askVol") or item.get("askVolume") or []
            bids = [
                OrderBookLevel(price=float(price or 0.0), volume=float((bid_volumes[idx] if idx < len(bid_volumes) else 0.0) or 0.0))
                for idx, price in enumerate(list(bid_prices)[:5])
                if float(price or 0.0) > 0
            ]
            asks = [
                OrderBookLevel(price=float(price or 0.0), volume=float((ask_volumes[idx] if idx < len(ask_volumes) else 0.0) or 0.0))
                for idx, price in enumerate(list(ask_prices)[:5])
                if float(price or 0.0) > 0
            ]
            snapshots.append(
                OrderBookSnapshot(
                    symbol=symbol,
                    name=self.get_symbol_name(symbol),
                    last_price=float(item.get("lastPrice", 0.0) or 0.0),
                    pre_close=float(item.get("preClose", 0.0) or 0.0),
                    total_volume=float(item.get("volume", 0.0) or 0.0),
                    total_amount=float(item.get("amount", 0.0) or 0.0),
                    bids=bids,
                    asks=asks,
                    buy_volume=float(item.get("bidVolSum", 0.0) or sum(level.volume for level in bids)),
                    sell_volume=float(item.get("askVolSum", 0.0) or sum(level.volume for level in asks)),
                    large_buy_volume=float(item.get("bigBuyVolume", 0.0) or 0.0),
                    large_sell_volume=float(item.get("bigSellVolume", 0.0) or 0.0),
                    captured_at=captured_at,
                )
            )
        return snapshots

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

    def search_symbols(self, query: str, limit: int = 10) -> list[dict[str, str]]:
        normalized = str(query or "").strip()
        if not normalized:
            return []
        upper_query = normalized.upper()
        results: list[dict[str, str]] = []
        seen: set[str] = set()
        for symbol in self.get_a_share_universe():
            name = self.get_symbol_name(symbol)
            if symbol == upper_query or name == normalized:
                return [{"symbol": symbol, "name": name}]
        for symbol in self.get_a_share_universe():
            if symbol in seen:
                continue
            name = self.get_symbol_name(symbol)
            if normalized and normalized in name:
                results.append({"symbol": symbol, "name": name})
                seen.add(symbol)
                if len(results) >= limit:
                    break
        return results


class WindowsProxyMarketDataAdapter(MarketDataAdapter):
    """通过 Windows 侧 HTTP 行情桥访问 QMT 行情。"""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._base_url = str(settings.windows_gateway.base_url or "").rstrip("/")
        if not self._base_url:
            raise RuntimeError("ASHARE_WINDOWS_GATEWAY_BASE_URL 未配置")
        self._timeout_sec = float(settings.windows_gateway.timeout_sec or 10.0)
        self._token = self._load_token()
        if not self._token:
            raise RuntimeError("Windows Gateway token 未配置，请设置 ASHARE_WINDOWS_GATEWAY_TOKEN 或 TOKEN_FILE")
        self._client = httpx.Client(timeout=self._timeout_sec, trust_env=False)
        self._name_cache: dict[str, str] = {}
        self._instrument_cache: dict[str, dict[str, Any]] = {}
        self._full_a_share_name_cache_ready = False

    def _timeout_for_path(self, method: str, path: str) -> float:
        if method == "GET" and path == "/qmt/quote/tick":
            return min(self._timeout_sec, 4.0)
        if method == "GET" and path == "/qmt/quote/kline":
            return min(self._timeout_sec, 6.0)
        if method == "GET" and path in {"/qmt/quote/universe", "/qmt/quote/instruments"}:
            return min(self._timeout_sec, 6.0)
        return self._timeout_sec

    @staticmethod
    def _gateway_error_prefix(status_code: int) -> str:
        if status_code == 429:
            return "windows_gateway_overloaded"
        if status_code == 401:
            return "windows_gateway_auth_failed"
        if status_code == 404:
            return "windows_gateway_not_found"
        if status_code == 408:
            return "windows_gateway_timeout"
        if 500 <= status_code <= 599:
            return "windows_gateway_upstream_error"
        return "windows_gateway_http_error"

    def _load_token(self) -> str:
        explicit = str(self.settings.windows_gateway.token or "").strip()
        if explicit:
            return explicit
        token_file = str(self.settings.windows_gateway.token_file or "").strip()
        if not token_file:
            return ""
        path = Path(token_file)
        if not path.exists():
            raise RuntimeError(f"Windows Gateway token 文件不存在: {path}")
        return path.read_text(encoding="utf-8").strip()

    def _headers(self) -> dict[str, str]:
        return {"X-Ashare-Token": self._token}

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        request_timeout = timeout or self._timeout_for_path(method, path)
        try:
            response = self._client.request(
                method,
                f"{self._base_url}{path}",
                headers=self._headers(),
                params=params,
                timeout=request_timeout,
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"windows_gateway_timeout: {path} | elapsed>{request_timeout:.1f}s") from exc
        except httpx.ConnectError as exc:
            raise RuntimeError(f"windows_gateway_unavailable: {path} | {exc}") from exc
        if response.status_code >= 400:
            detail = ""
            try:
                raw_payload = response.json()
                if isinstance(raw_payload, dict):
                    detail = str(
                        raw_payload.get("last_error")
                        or raw_payload.get("message")
                        or raw_payload.get("error")
                        or raw_payload.get("detail")
                        or ""
                    )
            except Exception:
                detail = response.text[:200]
            raise RuntimeError(f"{self._gateway_error_prefix(response.status_code)}: {path} | {detail}".strip())
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"windows_gateway_invalid_payload: {path}")
        if payload.get("ok", True) is False:
            error = (
                payload.get("last_error")
                or payload.get("message")
                or payload.get("error")
                or payload.get("detail")
                or "windows_gateway_request_failed"
            )
            error_text = str(error)
            if "invalid token" in error_text.lower():
                raise RuntimeError(f"windows_gateway_auth_failed: {path} | {error_text}")
            raise RuntimeError(f"windows_gateway_request_failed: {path} | {error_text}")
        return payload

    @staticmethod
    def _coerce_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _coerce_price_level(cls, value: Any) -> float:
        if isinstance(value, list):
            for item in value:
                if item not in (None, ""):
                    return cls._coerce_float(item)
            return 0.0
        return cls._coerce_float(value)

    @staticmethod
    def _extract_rows(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
        for key in keys:
            raw = payload.get(key)
            if isinstance(raw, list):
                return [item for item in raw if isinstance(item, dict)]
            if isinstance(raw, dict):
                rows: list[dict[str, Any]] = []
                for row_key, item in raw.items():
                    if not isinstance(item, dict):
                        continue
                    copied = dict(item)
                    copied.setdefault("symbol", str(item.get("symbol") or row_key))
                    rows.append(copied)
                if rows:
                    return rows
        return []

    @staticmethod
    def _extract_symbol_list(payload: dict[str, Any]) -> list[str]:
        candidates = [
            payload.get("symbols"),
            payload.get("items"),
            payload.get("data"),
        ]
        for raw in candidates:
            if isinstance(raw, list):
                return [str(item).strip() for item in raw if str(item).strip()]
            if isinstance(raw, dict) and isinstance(raw.get("symbols"), list):
                return [str(item).strip() for item in raw["symbols"] if str(item).strip()]
        return []

    @staticmethod
    def _extract_symbol_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        candidates: list[Any] = [
            payload.get("quotes"),
            payload.get("ticks"),
            payload.get("data"),
        ]
        for raw in candidates:
            if not isinstance(raw, dict):
                continue
            symbol_map: dict[str, dict[str, Any]] = {}
            for key, item in raw.items():
                if not isinstance(item, dict):
                    continue
                if "." not in str(key) and "." not in str(item.get("symbol") or ""):
                    continue
                symbol = str(item.get("symbol") or key).strip()
                if symbol:
                    symbol_map[symbol] = item
            if symbol_map:
                return symbol_map
        return {}

    @staticmethod
    def _extract_bars_map(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("bars"), dict):
            raw_map = data["bars"]
        elif isinstance(payload.get("bars"), dict):
            raw_map = payload["bars"]
        else:
            raw_map = {}
        result: dict[str, list[dict[str, Any]]] = {}
        for symbol, rows in raw_map.items():
            if not isinstance(rows, list):
                continue
            dict_rows = [item for item in rows if isinstance(item, dict)]
            if dict_rows:
                result[str(symbol)] = dict_rows
        return result

    @staticmethod
    def _extract_columnar_bars(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        time_list = data.get("trade_time") or data.get("time")
        open_list = data.get("open")
        high_list = data.get("high")
        low_list = data.get("low")
        close_list = data.get("close")
        volume_list = data.get("volume")
        amount_list = data.get("amount")
        pre_close_list = data.get("preClose")
        columns = [time_list, open_list, high_list, low_list, close_list, volume_list, amount_list]
        if not all(isinstance(column, list) for column in columns):
            return []
        size = min(len(column) for column in columns)
        if size <= 0:
            return []
        bars: list[dict[str, Any]] = []
        for index in range(size):
            row = {
                "time": time_list[index],
                "trade_time": time_list[index],
                "open": open_list[index],
                "high": high_list[index],
                "low": low_list[index],
                "close": close_list[index],
                "volume": volume_list[index],
                "amount": amount_list[index],
            }
            if isinstance(pre_close_list, list) and index < len(pre_close_list):
                row["preClose"] = pre_close_list[index]
            bars.append(row)
        return bars

    def _warm_instrument_cache(self, symbols: list[str]) -> None:
        unresolved = [symbol for symbol in dict.fromkeys(symbols) if symbol and symbol not in self._name_cache]
        if not unresolved:
            return
        payload = self._request_json("GET", "/qmt/quote/instruments", params={"codes": ",".join(unresolved)})
        for item in self._extract_rows(payload, "instruments", "items", "data"):
            symbol = str(item.get("symbol") or item.get("code") or "").strip()
            if not symbol:
                continue
            self._instrument_cache[symbol] = dict(item)
            name = str(item.get("name") or item.get("instrument_name") or item.get("stock_name") or "").strip()
            if name:
                self._name_cache[symbol] = name

    def _warm_all_a_share_instruments(self, batch_size: int = 800) -> None:
        if self._full_a_share_name_cache_ready:
            return
        symbols = self.get_a_share_universe()
        if not symbols:
            return
        for start in range(0, len(symbols), batch_size):
            self._warm_instrument_cache(symbols[start:start + batch_size])
        self._full_a_share_name_cache_ready = True

    def sync_history(self, symbols: list[str], period: str, start_time: str) -> dict:
        accepted = filter_a_share(symbols)
        return {"accepted_symbols": accepted, "period": period, "start_time": start_time}

    def subscribe(self, symbols: list[str]) -> dict:
        accepted = filter_a_share(symbols)
        return {"accepted_symbols": accepted, "subscription_count": len(accepted)}

    def get_snapshots(self, symbols: list[str]) -> list[QuoteSnapshot]:
        accepted = filter_a_share(symbols or self.get_main_board_universe())
        if not accepted:
            return []
        try:
            self._warm_instrument_cache(accepted)
        except Exception:
            pass
        payload = self._request_json("GET", "/qmt/quote/tick", params={"codes": ",".join(accepted)})
        quote_map = self._extract_symbol_map(payload)
        return [
            QuoteSnapshot(
                symbol=symbol,
                name=str(
                    quote_map.get(symbol, {}).get("name")
                    or quote_map.get(symbol, {}).get("instrument_name")
                    or self._name_cache.get(symbol)
                    or symbol
                ),
                last_price=self._coerce_float(
                    quote_map.get(symbol, {}).get("lastPrice")
                    or quote_map.get(symbol, {}).get("last_price")
                ),
                bid_price=self._coerce_price_level(
                    quote_map.get(symbol, {}).get("bidPrice")
                    or quote_map.get(symbol, {}).get("bid_price")
                ),
                ask_price=self._coerce_price_level(
                    quote_map.get(symbol, {}).get("askPrice")
                    or quote_map.get(symbol, {}).get("ask_price")
                ),
                volume=self._coerce_float(
                    quote_map.get(symbol, {}).get("volume")
                    or quote_map.get(symbol, {}).get("vol")
                ),
                pre_close=self._coerce_float(
                    quote_map.get(symbol, {}).get("preClose")
                    or quote_map.get(symbol, {}).get("lastClose")
                    or quote_map.get(symbol, {}).get("pre_close")
                ),
            )
            for symbol in accepted
        ]

    def get_bars(
        self,
        symbols: list[str],
        period: str,
        count: int = 1,
        end_time: str | None = None,
    ) -> list[BarSnapshot]:
        accepted = filter_a_share(symbols or self.get_main_board_universe())
        if not accepted:
            return []
        params: dict[str, Any] = {
            "codes": ",".join(accepted),
            "period": period,
            "count": max(int(count or 1), 1),
        }
        if end_time:
            params["end_time"] = end_time
        payload = self._request_json("GET", "/qmt/quote/kline", params=params)
        bars_map = self._extract_bars_map(payload)
        result: list[BarSnapshot] = []
        for symbol in accepted:
            rows = bars_map.get(symbol, [])
            if not rows and len(accepted) == 1:
                rows = self._extract_columnar_bars(payload)
            for row in rows:
                trade_time = str(row.get("trade_time") or row.get("time") or "").strip()
                result.append(
                    BarSnapshot(
                        symbol=symbol,
                        period=period,
                        open=self._coerce_float(row.get("open")),
                        high=self._coerce_float(row.get("high")),
                        low=self._coerce_float(row.get("low")),
                        close=self._coerce_float(row.get("close")),
                        volume=self._coerce_float(row.get("volume")),
                        amount=self._coerce_float(row.get("amount")),
                        trade_time=trade_time,
                        pre_close=self._coerce_float(row.get("preClose") or row.get("lastClose")),
                    )
                )
        cleaned, alerts = bar_quality_checker.validate_bars(result)
        for alert in alerts[:50]:
            logger.warning("行情质量告警[%s] %s %s %s", alert.issue, alert.symbol, alert.trade_time, alert.detail)
        return cleaned

    def get_main_board_universe(self) -> list[str]:
        payload = self._request_json("GET", "/qmt/quote/universe", params={"scope": "main_board"})
        return filter_main_board(self._extract_symbol_list(payload))

    def get_a_share_universe(self) -> list[str]:
        payload = self._request_json("GET", "/qmt/quote/universe", params={"a_share_only": "true"})
        return filter_a_share(self._extract_symbol_list(payload))

    def get_sectors(self) -> list[str]:
        payload = self._request_json("GET", "/qmt/quote/sectors")
        raw = payload.get("sectors")
        if raw is None:
            raw = payload.get("data")
        if not isinstance(raw, list):
            return []
        seen: set[str] = set()
        sectors: list[str] = []
        for item in raw:
            sector = str(item).strip()
            if not sector or sector in seen:
                continue
            seen.add(sector)
            sectors.append(sector)
        return sectors

    def get_sector_symbols(self, sector_name: str) -> list[str]:
        try:
            payload = self._request_json("GET", "/qmt/quote/sector-members", params={"sector": sector_name})
        except Exception:
            # 中文板块名仍可能受 Windows 侧编码实现影响，先对上层容错，不阻塞主流程。
            return []
        return filter_a_share(self._extract_symbol_list(payload))

    def get_index_quotes(self, symbols: list[str]) -> list[QuoteSnapshot]:
        accepted = [symbol for symbol in dict.fromkeys(symbols) if symbol]
        if not accepted:
            return []
        try:
            self._warm_instrument_cache(accepted)
        except Exception:
            pass
        payload = self._request_json("GET", "/qmt/quote/tick", params={"codes": ",".join(accepted)})
        quote_map = self._extract_symbol_map(payload)
        return [
            QuoteSnapshot(
                symbol=symbol,
                name=str(
                    quote_map.get(symbol, {}).get("name")
                    or quote_map.get(symbol, {}).get("instrument_name")
                    or self._name_cache.get(symbol)
                    or symbol
                ),
                last_price=self._coerce_float(
                    quote_map.get(symbol, {}).get("lastPrice")
                    or quote_map.get(symbol, {}).get("last_price")
                ),
                bid_price=self._coerce_price_level(
                    quote_map.get(symbol, {}).get("bidPrice")
                    or quote_map.get(symbol, {}).get("bid_price")
                ),
                ask_price=self._coerce_price_level(
                    quote_map.get(symbol, {}).get("askPrice")
                    or quote_map.get(symbol, {}).get("ask_price")
                ),
                volume=self._coerce_float(
                    quote_map.get(symbol, {}).get("volume")
                    or quote_map.get(symbol, {}).get("vol")
                ),
                pre_close=self._coerce_float(
                    quote_map.get(symbol, {}).get("preClose")
                    or quote_map.get(symbol, {}).get("lastClose")
                    or quote_map.get(symbol, {}).get("pre_close")
                ),
            )
            for symbol in accepted
        ]

    def get_order_book_snapshots(self, symbols: list[str]) -> list[OrderBookSnapshot]:
        accepted = filter_a_share(symbols or self.get_main_board_universe())
        if not accepted:
            return []
        payload = self._request_json("GET", "/qmt/quote/tick", params={"codes": ",".join(accepted)})
        quote_map = self._extract_symbol_map(payload)
        captured_at = datetime.now().isoformat()
        snapshots: list[OrderBookSnapshot] = []
        for symbol in accepted:
            item = dict(quote_map.get(symbol) or {})
            bid_prices = list(item.get("bidPrice") or item.get("bid_price") or [])
            ask_prices = list(item.get("askPrice") or item.get("ask_price") or [])
            bid_volumes = list(item.get("bidVol") or item.get("bidVolume") or item.get("bid_volume") or [])
            ask_volumes = list(item.get("askVol") or item.get("askVolume") or item.get("ask_volume") or [])
            bids = [
                OrderBookLevel(
                    price=self._coerce_float(price),
                    volume=self._coerce_float(bid_volumes[idx] if idx < len(bid_volumes) else 0.0),
                )
                for idx, price in enumerate(bid_prices[:5])
                if self._coerce_float(price) > 0
            ]
            asks = [
                OrderBookLevel(
                    price=self._coerce_float(price),
                    volume=self._coerce_float(ask_volumes[idx] if idx < len(ask_volumes) else 0.0),
                )
                for idx, price in enumerate(ask_prices[:5])
                if self._coerce_float(price) > 0
            ]
            snapshots.append(
                OrderBookSnapshot(
                    symbol=symbol,
                    name=str(item.get("name") or item.get("instrument_name") or self._name_cache.get(symbol) or symbol),
                    last_price=self._coerce_float(item.get("lastPrice") or item.get("last_price")),
                    pre_close=self._coerce_float(item.get("preClose") or item.get("lastClose") or item.get("pre_close")),
                    total_volume=self._coerce_float(item.get("volume") or item.get("vol")),
                    total_amount=self._coerce_float(item.get("amount") or item.get("turnover")),
                    bids=bids,
                    asks=asks,
                    buy_volume=self._coerce_float(item.get("bidVolSum") or item.get("buyVolume") or sum(level.volume for level in bids)),
                    sell_volume=self._coerce_float(item.get("askVolSum") or item.get("sellVolume") or sum(level.volume for level in asks)),
                    large_buy_volume=self._coerce_float(item.get("bigBuyVolume") or item.get("largeBuyVolume")),
                    large_sell_volume=self._coerce_float(item.get("bigSellVolume") or item.get("largeSellVolume")),
                    captured_at=str(item.get("captured_at") or item.get("time") or captured_at),
                )
            )
        return snapshots

    def get_symbol_name(self, symbol: str) -> str:
        cached = self._name_cache.get(symbol)
        if cached:
            return cached
        try:
            self._warm_instrument_cache([symbol])
        except Exception:
            return symbol
        return self._name_cache.get(symbol, symbol)

    def search_symbols(self, query: str, limit: int = 10) -> list[dict[str, str]]:
        normalized = str(query or "").strip()
        if not normalized:
            return []
        upper_query = normalized.upper()
        if "." in upper_query and upper_query in self._name_cache:
            return [{"symbol": upper_query, "name": self._name_cache[upper_query]}]

        try:
            self._warm_all_a_share_instruments()
        except Exception:
            pass

        exact: list[dict[str, str]] = []
        partial: list[dict[str, str]] = []
        for symbol, name in self._name_cache.items():
            if symbol == upper_query or name == normalized:
                exact.append({"symbol": symbol, "name": name})
            elif normalized and normalized in name:
                partial.append({"symbol": symbol, "name": name})
        if exact:
            return exact[:1]
        if partial:
            return partial[:limit]

        results: list[dict[str, str]] = []
        seen: set[str] = set()
        try:
            for symbol in self.get_a_share_universe():
                if symbol in seen:
                    continue
                name = self.get_symbol_name(symbol)
                if symbol == upper_query or name == normalized:
                    return [{"symbol": symbol, "name": name}]
                if normalized and normalized in name:
                    results.append({"symbol": symbol, "name": name})
                    seen.add(symbol)
                    if len(results) >= limit:
                        break
        except Exception:
            return []
        return results


class GoPlatformMarketDataAdapter(WindowsProxyMarketDataAdapter):
    """通过 Linux 本地 Go 并发数据平台访问行情。"""

    def __init__(self, settings: AppSettings) -> None:
        super().__init__(settings)
        self._go_client = GoPlatformClient(settings)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """覆盖请求逻辑，优先使用 Go 平台，失败时 fallback 到 Windows Gateway"""
        try:
            return self._go_client.get_json(path, params=params, timeout=timeout)
        except Exception as exc:
            if method == "GET":
                logger.warning(f"go_platform_fallback | GET {path} | error={exc} | falling back to windows_proxy")
                try:
                    result = super()._request_json(method, path, params=params, timeout=timeout)
                    if isinstance(result, dict):
                        result["_fallback"] = True
                        result["_fallback_reason"] = str(exc)
                    return result
                except Exception as fallback_exc:
                    logger.error(f"go_platform_fallback_failed | GET {path} | error={fallback_exc}")
                    raise
            raise


def build_market_adapter(mode: str, settings: AppSettings) -> MarketDataAdapter:
    if mode == "go_platform":
        adapter = GoPlatformMarketDataAdapter(settings)
        adapter.mode = "go_platform"
        return adapter
    if mode == "windows_proxy":
        adapter = WindowsProxyMarketDataAdapter(settings)
        adapter.mode = "windows_proxy"
        return adapter
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
