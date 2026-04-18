"""集合竞价数据抓取器。

优先通过 QMT Gateway 的 /auction/snapshot 接口拉取实时竞价快照；
Gateway 不可用时 fallback 到 akshare.stock_zh_a_spot_em() 延迟快照。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from ..contracts import AuctionSnapshot
from ..logging_config import get_logger

logger = get_logger("data.auction_fetcher")


class AuctionFetcher:
    """集合竞价快照抓取入口。

    在 09:15 / 09:20 / 09:24 三个时间点各调用一次 fetch_snapshots()。
    """

    def __init__(
        self,
        gateway_url: str | None = None,
        timeout_seconds: int = 5,
    ) -> None:
        self._gateway_url = gateway_url
        self._timeout_seconds = timeout_seconds

    def fetch_snapshots(
        self,
        symbols: list[str],
        prev_closes: dict[str, float] | None = None,
        prev_volume_5d_avgs: dict[str, int] | None = None,
        trade_date: str | None = None,
    ) -> list[AuctionSnapshot]:
        """拉取指定标的的竞价快照。

        Args:
            symbols: 候选标的列表
            prev_closes: {symbol: 昨收价}
            prev_volume_5d_avgs: {symbol: 近5日平均成交量}
            trade_date: 交易日（默认当天）

        Returns:
            竞价快照列表

        TODO:
            1. 实现 _fetch_from_gateway() — 调用 Windows Gateway /auction/snapshot
            2. 实现 _fetch_from_akshare() — 调用 akshare.stock_zh_a_spot_em()
            3. 合并结果并计算 open_change_pct 和 volume_ratio
        """
        resolved_trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        timestamp = datetime.now().strftime("%H:%M:%S")
        prev_closes = prev_closes or {}
        prev_volume_5d_avgs = prev_volume_5d_avgs or {}

        # 先尝试 Gateway，失败后 fallback akshare
        snapshots = self._fetch_from_gateway(symbols, timestamp)
        if not snapshots:
            snapshots = self._fetch_from_akshare(symbols, timestamp)

        # 补充 prev_close 和 volume_ratio 计算
        results: list[AuctionSnapshot] = []
        for snap in snapshots:
            prev_close = prev_closes.get(snap.symbol, snap.prev_close)
            prev_vol = prev_volume_5d_avgs.get(snap.symbol, 0)
            if prev_close > 0:
                open_change_pct = (snap.price - prev_close) / prev_close
            else:
                open_change_pct = 0.0
            results.append(snap.model_copy(update={
                "prev_close": prev_close,
                "prev_volume_5d_avg": prev_vol,
                "open_change_pct": round(open_change_pct, 6),
                "timestamp": timestamp,
            }))
        logger.info("竞价快照抓取完成: symbols=%d snapshots=%d timestamp=%s",
                     len(symbols), len(results), timestamp)
        return results

    def _fetch_from_gateway(self, symbols: list[str], timestamp: str) -> list[AuctionSnapshot]:
        """通过 QMT Gateway 拉取竞价快照。

        TODO: 实现 HTTP 调用 self._gateway_url + '/auction/snapshot'
              请求体 {"symbols": symbols, "timestamp": timestamp}
              解析响应并转为 AuctionSnapshot 列表
        """
        if not self._gateway_url:
            return []
        try:
            endpoint = self._gateway_url.rstrip("/")
            if not endpoint.endswith("/auction/snapshot"):
                endpoint = f"{endpoint}/auction/snapshot"
            with httpx.Client(timeout=self._timeout_seconds, trust_env=False) as client:
                response = client.post(
                    endpoint,
                    json={"symbols": symbols, "timestamp": timestamp},
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            logger.warning("Gateway 竞价快照抓取失败: url=%s error=%s", self._gateway_url, exc)
            return []

        items = []
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = (
                payload.get("snapshots")
                or payload.get("items")
                or payload.get("data")
                or []
            )
        results: list[AuctionSnapshot] = []
        for item in items:
            snapshot = self._build_snapshot_from_payload(item, timestamp)
            if snapshot is not None:
                results.append(snapshot)
        return results

    def _fetch_from_akshare(self, symbols: list[str], timestamp: str) -> list[AuctionSnapshot]:
        """通过 akshare 拉取延迟竞价快照（fallback）。

        TODO: 调用 akshare.stock_zh_a_spot_em()
              过滤出 symbols 中标的
              从返回 DataFrame 中提取 '最新价', '成交量', '昨收' 等字段
              构造 AuctionSnapshot 列表
        """
        try:
            import akshare as ak
        except ModuleNotFoundError:
            logger.warning("akshare 未安装，跳过竞价快照抓取。")
            return []

        try:
            frame = ak.stock_zh_a_spot_em()
        except Exception as exc:
            logger.warning("akshare 竞价快照抓取失败: %s", exc)
            return []

        allowed = {self._normalize_symbol(symbol) for symbol in symbols if self._normalize_symbol(symbol)}
        results: list[AuctionSnapshot] = []
        for row in frame.to_dict("records"):
            symbol = self._normalize_symbol(
                row.get("代码")
                or row.get("symbol")
                or row.get("证券代码")
                or row.get("股票代码")
            )
            if not symbol or symbol not in allowed:
                continue
            snapshot = self._build_snapshot_from_payload(row, timestamp)
            if snapshot is not None:
                results.append(snapshot)
        return results

    @staticmethod
    def _normalize_symbol(value: Any) -> str:
        raw = str(value or "").strip().upper()
        if not raw:
            return ""
        if "." in raw:
            return raw
        if raw.startswith(("60", "68", "90")):
            return f"{raw}.SH"
        if raw.startswith(("00", "30", "20")):
            return f"{raw}.SZ"
        return raw

    def _build_snapshot_from_payload(self, payload: Any, timestamp: str) -> AuctionSnapshot | None:
        if not isinstance(payload, dict):
            return None
        symbol = self._normalize_symbol(
            payload.get("symbol")
            or payload.get("代码")
            or payload.get("证券代码")
            or payload.get("股票代码")
        )
        if not symbol:
            return None
        price = self._safe_float(
            payload.get("price")
            or payload.get("最新价")
            or payload.get("现价")
            or payload.get("成交价")
            or payload.get("auction_price")
        )
        volume = int(self._safe_float(
            payload.get("volume")
            or payload.get("成交量")
            or payload.get("竞价量")
            or payload.get("auction_volume")
        ))
        prev_close = self._safe_float(
            payload.get("prev_close")
            or payload.get("昨收")
            or payload.get("前收盘")
            or payload.get("pre_close")
        )
        name = str(
            payload.get("name")
            or payload.get("名称")
            or payload.get("证券简称")
            or payload.get("股票简称")
            or ""
        ).strip()
        prev_volume_5d_avg = int(self._safe_float(
            payload.get("prev_volume_5d_avg")
            or payload.get("5日均量")
            or payload.get("五日均量")
            or payload.get("avg_volume_5d")
        ))
        if price <= 0:
            return None
        open_change_pct = (price - prev_close) / prev_close if prev_close > 0 else 0.0
        return AuctionSnapshot(
            symbol=symbol,
            name=name,
            price=price,
            volume=max(volume, 0),
            prev_close=max(prev_close, 0.0),
            prev_volume_5d_avg=max(prev_volume_5d_avg, 0),
            timestamp=timestamp,
            open_change_pct=round(open_change_pct, 6),
        )

    @staticmethod
    def _safe_float(value: Any) -> float:
        if value in (None, "", "-", "--"):
            return 0.0
        text = str(value).strip().replace(",", "")
        try:
            return float(text)
        except (TypeError, ValueError):
            return 0.0
