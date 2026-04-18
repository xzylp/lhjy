"""结构化事件抓取器。

优先调用 akshare 官方数据接口；接口缺失或抓取失败时返回空结果，不注入任何伪造事件。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..contracts import EventFetchResult, MarketEvent, StructuredEvent
from ..logging_config import get_logger

logger = get_logger("data.event_fetcher")


class EventFetcher:
    """盘前结构化事件抓取入口。"""

    def fetch_today_events(
        self,
        symbols: list[str],
        trade_date: str | None = None,
    ) -> EventFetchResult:
        generated_at = datetime.now().isoformat()
        resolved_trade_date = trade_date or date.today().isoformat()
        normalized_symbols = [
            symbol
            for symbol in dict.fromkeys(self._normalize_symbol(item) for item in symbols)
            if symbol
        ]
        if not normalized_symbols:
            return EventFetchResult(
                trade_date=resolved_trade_date,
                generated_at=generated_at,
                summary_lines=["未提供有效 symbol，结构化事件抓取返回空结果。"],
            )

        events: list[StructuredEvent] = []
        events.extend(self._fetch_suspension_events(normalized_symbols, resolved_trade_date))
        events.extend(self._fetch_hold_change_events(normalized_symbols, resolved_trade_date))
        events.extend(self._fetch_earnings_warning_events(normalized_symbols, resolved_trade_date))
        deduped = self._dedupe_events(events)
        summary_lines = self._build_summary_lines(deduped, resolved_trade_date)
        return EventFetchResult(
            trade_date=resolved_trade_date,
            generated_at=generated_at,
            events=deduped,
            summary_lines=summary_lines,
        )

    def fetch_incremental(
        self,
        symbols: list[str],
        *,
        since: str | None = None,
        trade_date: str | None = None,
        event_bus: Any | None = None,
    ) -> EventFetchResult:
        """抓取 since 之后的增量事件，并按需发射 NEGATIVE_NEWS。"""
        result = self.fetch_today_events(symbols, trade_date=trade_date)
        since_dt = self._parse_datetime(since)
        if since_dt is None:
            incremental_events = list(result.events)
        else:
            incremental_events = []
            for item in result.events:
                published_at = self._parse_datetime(item.published_at)
                if published_at is None or published_at >= since_dt:
                    incremental_events.append(item)

        if event_bus is not None:
            self.emit_negative_news_events(incremental_events, event_bus=event_bus)

        return result.model_copy(
            update={
                "events": incremental_events,
                "summary_lines": self._build_incremental_summary_lines(
                    incremental_events,
                    result.trade_date,
                    since=since,
                ),
            }
        )

    def emit_negative_news_events(self, events: list[StructuredEvent], *, event_bus: Any) -> int:
        emitted = 0
        for item in events:
            negative = item.impact == "negative" or "negative" in item.tags or item.severity == "block"
            if not negative:
                continue
            event_bus.publish_sync(
                MarketEvent(
                    event_type="NEGATIVE_NEWS",
                    symbol=item.symbol,
                    payload={
                        "event_type": item.event_type,
                        "title": item.title,
                        "summary": item.summary,
                        "severity": item.severity,
                        "impact": item.impact,
                        "published_at": item.published_at,
                        "source": item.source,
                        "tags": list(item.tags),
                    },
                    timestamp=item.published_at or datetime.now().isoformat(),
                    priority=2 if item.severity in {"block", "high"} else 1,
                    source="event_fetcher.incremental",
                )
            )
            emitted += 1
        return emitted

    def _fetch_suspension_events(self, symbols: list[str], trade_date: str) -> list[StructuredEvent]:
        func_name, frame = self._call_first_available(
            [
                ("stock_tfp_em", {"date": self._compact_trade_date(trade_date)}),
                ("stock_tfp_em", {}),
            ],
            description="停复牌",
        )
        if frame is None:
            return []
        allowed = set(symbols)
        resolved_trade_date = self._compact_trade_date(trade_date)
        events: list[StructuredEvent] = []
        for row in self._iter_rows(frame):
            symbol = self._normalize_symbol(
                row.get("代码")
                or row.get("证券代码")
                or row.get("股票代码")
                or row.get("symbol")
            )
            if not symbol or symbol not in allowed:
                continue
            name = self._string(row.get("名称") or row.get("证券简称") or row.get("股票简称"))
            suspend_at = self._compact_trade_date(
                self._date_string(row.get("停牌时间") or row.get("停牌日期") or row.get("停牌起始日"))
            )
            resume_at = self._compact_trade_date(
                self._date_string(row.get("复牌时间") or row.get("复牌日期") or row.get("预计复牌时间"))
            )
            if suspend_at and resume_at:
                if not (
                    self._matches_trade_date(suspend_at, trade_date)
                    or self._matches_trade_date(resume_at, trade_date)
                ):
                    continue
            elif suspend_at and not self._matches_trade_date(suspend_at, trade_date):
                continue
            elif resume_at and not self._matches_trade_date(resume_at, trade_date):
                continue
            reason = self._string(row.get("停牌原因") or row.get("停牌事项") or row.get("原因"))
            is_resumption = bool(resume_at and resume_at <= resolved_trade_date)
            event_type = "resumption" if is_resumption else "suspension"
            impact = "neutral" if is_resumption else "block"
            severity = "info" if is_resumption else "block"
            title_prefix = "复牌提示" if is_resumption else "停牌提示"
            title = f"{title_prefix}：{name or symbol}{(' - ' + reason) if reason else ''}"
            tags = [event_type, "negative"] if not is_resumption else [event_type]
            summary = (
                f"停牌时间={suspend_at or '未知'}，复牌时间={resume_at or '待定'}，原因={reason or '未披露'}。"
            )
            events.append(
                StructuredEvent(
                    symbol=symbol,
                    name=name,
                    event_type=event_type,
                    impact=impact,
                    severity=severity,
                    title=title,
                    published_at=self._event_time(trade_date, suspend_at or resume_at),
                    source=f"akshare.{func_name}" if func_name else "akshare",
                    tags=tags,
                    category="announcements",
                    impact_scope="symbol",
                    summary=summary,
                )
            )
        return events

    def _fetch_hold_change_events(self, symbols: list[str], trade_date: str) -> list[StructuredEvent]:
        func_name, frame = self._call_first_available(
            [
                ("stock_hold_management_person_em", {"symbol": "全部"}),
                ("stock_hold_control_person_em", {"symbol": "全部"}),
                ("stock_hold_control_stock_em", {"symbol": "全部"}),
                ("stock_hold_control_em", {}),
            ],
            description="增减持",
        )
        if frame is None:
            return []
        allowed = set(symbols)
        events: list[StructuredEvent] = []
        for row in self._iter_rows(frame):
            symbol = self._normalize_symbol(
                row.get("代码")
                or row.get("证券代码")
                or row.get("股票代码")
                or row.get("symbol")
            )
            if not symbol or symbol not in allowed:
                continue
            name = self._string(row.get("名称") or row.get("证券简称") or row.get("股票简称"))
            direction = self._string(
                row.get("变动方向")
                or row.get("变动类型")
                or row.get("增减")
                or row.get("操作方向")
                or row.get("持股变动信息")
            )
            if "减" not in direction and "增" not in direction:
                continue
            amount = self._string(row.get("变动数量") or row.get("变动股数") or row.get("数量"))
            ratio = self._string(row.get("变动比例") or row.get("占总股本比例") or row.get("比例"))
            event_date = self._date_string(
                row.get("公告日")
                or row.get("最新公告日期")
                or row.get("截止日")
                or row.get("变动截止日")
                or row.get("日期")
            )
            if event_date and not self._matches_trade_date(event_date, trade_date):
                continue
            is_reduction = "减" in direction
            tags = ["shareholder_reduction", "negative"] if is_reduction else ["shareholder_increase", "positive"]
            events.append(
                StructuredEvent(
                    symbol=symbol,
                    name=name,
                    event_type="shareholder_reduction" if is_reduction else "shareholder_increase",
                    impact="negative" if is_reduction else "positive",
                    severity="high" if is_reduction else "medium",
                    title=f"{name or symbol} 出现{direction}",
                    published_at=self._event_time(trade_date, event_date),
                    source=f"akshare.{func_name}" if func_name else "akshare",
                    tags=tags,
                    category="announcements",
                    impact_scope="symbol",
                    summary=f"方向={direction}，数量={amount or '未披露'}，比例={ratio or '未披露'}。",
                )
            )
        return events

    def _fetch_earnings_warning_events(self, symbols: list[str], trade_date: str) -> list[StructuredEvent]:
        func_name, frame = self._call_first_available(
            [
                ("stock_yjyg_em", {"date": self._compact_trade_date(trade_date)}),
                ("stock_yjyg_em", {}),
            ],
            description="业绩预告",
        )
        if frame is None:
            return []
        allowed = set(symbols)
        events: list[StructuredEvent] = []
        for row in self._iter_rows(frame):
            symbol = self._normalize_symbol(
                row.get("代码")
                or row.get("证券代码")
                or row.get("股票代码")
                or row.get("symbol")
            )
            if not symbol or symbol not in allowed:
                continue
            name = self._string(row.get("名称") or row.get("证券简称") or row.get("股票简称"))
            notice = self._string(
                row.get("预告类型")
                or row.get("业绩变动")
                or row.get("业绩变动方向")
                or row.get("业绩预告类型")
            )
            detail = self._string(row.get("业绩变动原因") or row.get("摘要") or row.get("业绩变动原因说明"))
            negative = any(word in notice or word in detail for word in ("预减", "预亏", "首亏", "续亏", "下滑", "减少", "亏损"))
            if not notice and not detail:
                continue
            event_date = self._date_string(row.get("公告日期") or row.get("最新公告日期") or row.get("日期"))
            if event_date and not self._matches_trade_date(event_date, trade_date):
                continue
            tags = ["earnings_forecast"]
            if negative:
                tags.extend(["negative", "warning"])
            events.append(
                StructuredEvent(
                    symbol=symbol,
                    name=name,
                    event_type="earnings_warning" if negative else "earnings_forecast",
                    impact="negative" if negative else "neutral",
                    severity="high" if negative else "medium",
                    title=f"{name or symbol} 业绩预告：{notice or detail}",
                    published_at=self._event_time(trade_date, event_date),
                    source=f"akshare.{func_name}" if func_name else "akshare",
                    tags=tags,
                    category="announcements",
                    impact_scope="symbol",
                    summary=detail or notice,
                )
            )
        return events

    @staticmethod
    def _iter_rows(frame: Any) -> list[dict[str, Any]]:
        if frame is None:
            return []
        if hasattr(frame, "iterrows"):
            return [row.to_dict() if hasattr(row, "to_dict") else dict(row) for _, row in frame.iterrows()]
        if isinstance(frame, list):
            return [dict(item) for item in frame if isinstance(item, dict)]
        return []

    def _call_first_available(
        self,
        candidates: list[tuple[str, dict[str, Any]]],
        *,
        description: str,
    ) -> tuple[str | None, Any]:
        try:
            import akshare as ak
        except ModuleNotFoundError:
            logger.warning("akshare 未安装，跳过%s抓取。", description)
            return None, None

        last_error: Exception | None = None
        for func_name, kwargs in candidates:
            func = getattr(ak, func_name, None)
            if func is None:
                continue
            try:
                return func_name, func(**kwargs)
            except TypeError:
                try:
                    return func_name, func()
                except Exception as exc:  # pragma: no cover - 防御性分支
                    last_error = exc
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            logger.warning("%s抓取失败，将返回空结果: %s", description, last_error)
        return None, None

    @staticmethod
    def _dedupe_events(events: list[StructuredEvent]) -> list[StructuredEvent]:
        deduped: dict[str, StructuredEvent] = {}
        for item in events:
            key = "|".join(
                [
                    item.symbol,
                    item.event_type,
                    item.title,
                    item.published_at,
                ]
            )
            deduped[key] = item
        return list(deduped.values())

    @staticmethod
    def _build_summary_lines(events: list[StructuredEvent], trade_date: str) -> list[str]:
        if not events:
            return [f"{trade_date} 未抓到结构化事件，返回空结果。"]
        by_type: dict[str, int] = {}
        blocked_symbols = set()
        for item in events:
            by_type[item.event_type] = by_type.get(item.event_type, 0) + 1
            if item.impact == "block":
                blocked_symbols.add(item.symbol)
        parts = ", ".join(f"{key}={value}" for key, value in sorted(by_type.items()))
        lines = [f"{trade_date} 结构化事件抓取完成: total={len(events)} {parts}。"]
        if blocked_symbols:
            lines.append(f"事件阻断标的: {', '.join(sorted(blocked_symbols))}。")
        return lines

    @staticmethod
    def _build_incremental_summary_lines(
        events: list[StructuredEvent],
        trade_date: str,
        *,
        since: str | None = None,
    ) -> list[str]:
        base = f"{trade_date} 增量事件抓取完成: total={len(events)}"
        if since:
            base += f" since={since}"
        if not events:
            return [base + "。"]
        negative_count = sum(
            1
            for item in events
            if item.impact == "negative" or "negative" in item.tags or item.severity == "block"
        )
        return [
            base + f" negative={negative_count}。",
            f"最新事件: {events[0].symbol} {events[0].title}",
        ]

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
        if raw.startswith(("4", "8")):
            return f"{raw}.BJ"
        return raw

    @staticmethod
    def _string(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _date_string(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return text.replace("/", "-").replace(".", "-")

    @staticmethod
    def _compact_trade_date(value: str) -> str:
        return str(value or "").replace("-", "").strip()

    @staticmethod
    def _event_time(trade_date: str, event_date: str | None) -> str:
        resolved = str(event_date or "").strip()
        if not resolved:
            return f"{trade_date}T07:30:00"
        if "T" in resolved:
            return resolved
        if " " in resolved:
            return resolved.replace(" ", "T")
        return f"{resolved}T07:30:00"

    @classmethod
    def _matches_trade_date(cls, event_date: str | None, trade_date: str) -> bool:
        resolved_event_date = cls._compact_trade_date(cls._date_string(event_date))
        resolved_trade_date = cls._compact_trade_date(trade_date)
        if not resolved_event_date or not resolved_trade_date:
            return False
        return resolved_event_date.startswith(resolved_trade_date)

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
