"""候选股 dossier 预计算与持久化。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from .contracts import StockBehaviorProfile
from .data.archive import DataArchiveStore
from .data.contracts import MarketStructureSnapshotRecord
from .data.freshness import build_freshness_meta
from .data.fetcher import DataFetcher, DataPipeline
from .discussion.candidate_case import CandidateCaseService
from .infra.filters import get_price_limit_ratio
from .infra.audit_store import StateStore
from .runtime_config import RuntimeConfig, RuntimeConfigManager
from .settings import AppSettings
from .strategy.stock_profile import StockProfileBuilder


CORE_INDEX_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
    ("000852.SH", "中证1000"),
)
GENERIC_EVENT_TAGS: frozenset[str] = frozenset(
    {
        "announcement",
        "announcements",
        "event",
        "events",
        "intraday",
        "macro",
        "market",
        "news",
        "notice",
        "policy",
    }
)


class DossierPrecomputeService:
    def __init__(
        self,
        settings: AppSettings,
        market_adapter,
        research_state_store: StateStore,
        runtime_state_store: StateStore,
        candidate_case_service: CandidateCaseService | None = None,
        config_mgr: RuntimeConfigManager | None = None,
        now_factory=None,
    ) -> None:
        self._settings = settings
        self._market = market_adapter
        self._research_state_store = research_state_store
        self._runtime_state_store = runtime_state_store
        self._candidate_case_service = candidate_case_service
        self._config_mgr = config_mgr
        self._now_factory = now_factory or datetime.now
        fetcher = DataFetcher(market_adapter)
        self._fetcher = fetcher
        self._pipeline = DataPipeline(fetcher, settings.storage_root / "cache")
        self._archive_store = DataArchiveStore(settings.storage_root)
        self._stock_profile_builder = StockProfileBuilder()

    def precompute(
        self,
        trade_date: str | None = None,
        symbols: list[str] | None = None,
        source: str = "candidate_pool",
        limit: int = 30,
        force: bool = False,
        as_of_time: str | None = None,
    ) -> dict:
        resolved_as_of_time = self._resolve_as_of_time(as_of_time)
        resolved_trade_date = trade_date or self._resolve_trade_date(resolved_as_of_time)
        resolved_symbols = self._resolve_symbols(resolved_trade_date, symbols or [], source, limit)
        if not resolved_symbols:
            raise ValueError("no symbols available for dossier precompute")

        now = self._now_factory()
        signature = self._build_signature(resolved_trade_date, resolved_symbols, source, resolved_as_of_time)
        latest = self._research_state_store.get("latest_dossier_pack")
        if latest and not force and latest.get("signature") == signature and self._is_fresh(latest, now):
            reused = dict(latest)
            reused["reused"] = True
            self._archive_store.persist_dossier_pack(reused)
            if reused.get("market_context"):
                self._archive_store.persist_market_context(resolved_trade_date, reused["market_context"])
            if reused.get("event_context"):
                self._archive_store.persist_event_context(resolved_trade_date, reused["event_context"])
            symbol_contexts = [
                item["symbol_context"]
                for item in reused.get("items", [])
                if isinstance(item, dict) and item.get("symbol_context")
            ]
            if symbol_contexts:
                self._archive_store.persist_symbol_contexts(
                    resolved_trade_date,
                    symbol_contexts,
                    generated_at=reused.get("generated_at") or now.isoformat(),
                    signature=reused.get("signature", ""),
                )
            return reused

        case_map = self._case_map(resolved_trade_date)
        snapshots = self._fetcher.fetch_snapshots(resolved_symbols)
        snapshot_map = {item.symbol: item for item in snapshots}
        daily_bars = self._pipeline.get_daily_bars(resolved_symbols, count=60, as_of_time=resolved_as_of_time)
        bar_history_map: dict[str, list] = {}
        for item in daily_bars:
            bar_history_map.setdefault(item.bar.symbol, []).append(item)
        bar_map = {symbol: items[-1] for symbol, items in bar_history_map.items() if items}
        behavior_profile_map, behavior_profile_record_map, behavior_profile_context = self._build_behavior_profiles(
            trade_date=resolved_trade_date,
            symbols=resolved_symbols,
            history_map=bar_history_map,
            generated_at=now.isoformat(),
            force_rebuild=force,
        )
        research_items = self._research_items(as_of_time=resolved_as_of_time)
        research_map = self._research_map(resolved_symbols, research_items)
        index_snapshots = self._fetch_core_index_snapshots()
        market_context = self._build_market_context(index_snapshots, now.isoformat())
        event_context = self._build_event_context(resolved_symbols, research_items, now.isoformat())

        self._archive_store.persist_symbol_snapshots(snapshots, generated_at=now.isoformat())
        self._archive_store.persist_market_bars(
            [item.bar for item in daily_bars],
            generated_at=now.isoformat(),
            name_map={symbol: self._resolve_name(symbol, case_map.get(symbol), snapshot_map.get(symbol)) for symbol in resolved_symbols},
        )
        self._archive_store.persist_index_snapshots(
            index_snapshots,
            generated_at=now.isoformat(),
            index_name_map=dict(CORE_INDEX_SYMBOLS),
        )
        self._archive_store.persist_market_context(resolved_trade_date, market_context)
        self._archive_store.persist_event_context(resolved_trade_date, event_context)
        self._archive_store.persist_market_structure_snapshot(
            self._build_market_structure_snapshot(index_snapshots, now.isoformat())
        )
        self._persist_behavior_profile_artifact(
            trade_date=resolved_trade_date,
            generated_at=now.isoformat(),
            signature=signature,
            records=behavior_profile_record_map,
        )

        items = []
        symbol_contexts = []
        for index, symbol in enumerate(resolved_symbols, start=1):
            case = case_map.get(symbol)
            snap = snapshot_map.get(symbol)
            bar = bar_map.get(symbol)
            events = research_map.get(symbol, [])
            name = self._resolve_name(symbol, case, snap)
            attributed_events = self._attribute_events(symbol, research_items)
            behavior_profile_record = behavior_profile_record_map.get(symbol, {})
            symbol_context = self._build_symbol_context(
                trade_date=resolved_trade_date,
                symbol=symbol,
                name=name,
                snapshot=snap,
                daily_bar=bar,
                behavior_profile=behavior_profile_map.get(symbol),
                behavior_profile_source=str(behavior_profile_record.get("source") or ""),
                behavior_profile_trade_date=str(behavior_profile_record.get("profile_trade_date") or ""),
                market_context=market_context,
                attributed_events=attributed_events,
                generated_at=now.isoformat(),
            )
            symbol_contexts.append(symbol_context)
            items.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "rank": case.runtime_snapshot.rank if case else index,
                    "selection_score": case.runtime_snapshot.selection_score if case else None,
                    "final_status": case.final_status if case else "watchlist",
                    "risk_gate": case.risk_gate if case else "pending",
                    "audit_gate": case.audit_gate if case else "pending",
                    "reason": (
                        (case.selected_reason or case.rejected_reason or case.runtime_snapshot.summary)
                        if case
                        else ""
                    ),
                    "market_snapshot": (
                        {
                            "last_price": snap.last_price,
                            "pre_close": snap.pre_close,
                            "change_pct": self._change_pct(snap.last_price, snap.pre_close),
                            "volume": snap.volume,
                        }
                        if snap
                        else {}
                    ),
                    "daily_bar": (
                        {
                            "open": bar.bar.open,
                            "high": bar.bar.high,
                            "low": bar.bar.low,
                            "close": bar.bar.close,
                            "volume": bar.bar.volume,
                            "trade_time": bar.bar.trade_time,
                            "change_pct": bar.change_pct,
                            "is_limit_up": bar.is_limit_up,
                            "is_limit_down": bar.is_limit_down,
                        }
                        if bar
                        else {}
                    ),
                    "research": {
                        "event_count": len(events),
                        "latest_titles": [item["title"] for item in events[:5]],
                    },
                    "market_context": market_context,
                    "event_context": {
                        "event_count": symbol_context["event_summary"]["total_related_event_count"],
                        "latest_titles": symbol_context["event_summary"]["latest_titles"],
                        "by_scope_counts": symbol_context["event_summary"]["counts_by_scope"],
                        "generated_at": event_context.get("generated_at"),
                        "staleness_level": event_context.get("staleness_level", "fresh"),
                    },
                    "behavior_profile": (
                        behavior_profile_map[symbol].model_dump()
                        if symbol in behavior_profile_map
                        else None
                    ),
                    "behavior_profile_source": behavior_profile_record.get("source"),
                    "behavior_profile_trade_date": behavior_profile_record.get("profile_trade_date"),
                    "symbol_context": symbol_context,
                }
            )

        self._archive_store.persist_symbol_contexts(
            resolved_trade_date,
            symbol_contexts,
            generated_at=now.isoformat(),
            signature=signature,
        )

        ttl_seconds = self._runtime_config().snapshots.market_snapshot_ttl_seconds
        pack = {
            "pack_id": f"dossier-{now.strftime('%Y%m%d%H%M%S')}",
            "trade_date": resolved_trade_date,
            "source": source,
            "symbol_count": len(items),
            "generated_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "as_of_time": resolved_as_of_time,
            "signature": signature,
            "reused": False,
            "market_context": market_context,
            "event_context": event_context,
            "behavior_profile_context": behavior_profile_context,
            "behavior_profiles": [item.model_dump() for item in behavior_profile_map.values()],
            "items": items,
        }
        self._persist(pack)
        self._archive_store.persist_dossier_pack(pack)
        return pack

    def get_latest(self) -> dict | None:
        return self._research_state_store.get("latest_dossier_pack")

    def get_latest_status(self) -> dict:
        latest = self.get_latest()
        if not latest:
            return {
                "available": False,
                "is_fresh": False,
                "expires_in_seconds": None,
            }

        now = self._now_factory()
        expires_at = latest.get("expires_at")
        expires_in_seconds: int | None = None
        is_fresh = False
        if expires_at:
            remaining = (datetime.fromisoformat(expires_at) - now).total_seconds()
            expires_in_seconds = max(int(remaining), 0)
            is_fresh = remaining >= 0

        return {
            "available": True,
            "is_fresh": is_fresh,
            "expires_in_seconds": expires_in_seconds,
            **latest,
        }

    def precompute_latest_candidates(
        self,
        trade_date: str | None = None,
        source: str = "candidate_pool",
        limit: int = 30,
        force: bool = False,
        as_of_time: str | None = None,
    ) -> dict | None:
        resolved_as_of_time = self._resolve_as_of_time(as_of_time)
        resolved_trade_date = trade_date or self._resolve_trade_date(resolved_as_of_time)
        symbols = self._resolve_candidate_symbols(resolved_trade_date, limit)
        if not symbols:
            return None
        return self.precompute(
            trade_date=resolved_trade_date,
            symbols=symbols,
            source=source,
            limit=limit,
            force=force,
            as_of_time=resolved_as_of_time,
        )

    def refresh_behavior_profiles(
        self,
        trade_date: str | None = None,
        symbols: list[str] | None = None,
        source: str = "candidate_pool",
        limit: int = 30,
        force: bool = False,
        trigger: str = "manual",
    ) -> dict:
        """独立刷新股性画像 artifact，不触发 dossier pack 预计算。"""
        resolved_trade_date = trade_date or self._resolve_trade_date()
        resolved_symbols = self._resolve_symbols(resolved_trade_date, symbols or [], source, limit)
        if not resolved_symbols:
            return {
                "ok": False,
                "refreshed": False,
                "reason": "no_symbols",
                "trigger": trigger,
                "trade_date": resolved_trade_date,
                "source": source,
                "symbol_count": 0,
                "profile_count": 0,
                "coverage_ratio": 0.0,
                "source_counts": {"computed": 0, "artifact_cache": 0, "history_cache": 0},
                "missing_symbols": [],
            }

        now = self._now_factory()
        daily_bars = self._pipeline.get_daily_bars(resolved_symbols, count=60, as_of_time=self._resolve_as_of_time(None))
        history_map: dict[str, list] = {}
        for item in daily_bars:
            history_map.setdefault(item.bar.symbol, []).append(item)

        profile_map, profile_record_map, profile_context = self._build_behavior_profiles(
            trade_date=resolved_trade_date,
            symbols=resolved_symbols,
            history_map=history_map,
            generated_at=now.isoformat(),
            force_rebuild=force,
        )
        signature = self._build_signature(
            resolved_trade_date,
            resolved_symbols,
            f"behavior_profile_refresh:{source}",
        )
        artifact = self._persist_behavior_profile_artifact(
            trade_date=resolved_trade_date,
            generated_at=now.isoformat(),
            signature=signature,
            records=profile_record_map,
        )
        return {
            "ok": True,
            "refreshed": True,
            "trigger": trigger,
            "trade_date": resolved_trade_date,
            "source": source,
            "symbol_count": len(resolved_symbols),
            "profile_count": len(profile_map),
            "coverage_ratio": profile_context.get("coverage_ratio", 0.0),
            "source_counts": profile_context.get("source_counts", {}),
            "missing_symbols": profile_context.get("missing_symbols", []),
            "generated_at": now.isoformat(),
            "signature": signature,
            "artifact_symbol_count": int(artifact.get("symbol_count", 0) or 0),
        }

    def should_refresh_candidates(
        self,
        trade_date: str | None = None,
        source: str = "candidate_pool",
        limit: int = 30,
        as_of_time: str | None = None,
    ) -> dict:
        now = self._now_factory()
        resolved_as_of_time = self._resolve_as_of_time(as_of_time)
        resolved_trade_date = trade_date or self._resolve_trade_date(resolved_as_of_time)
        symbols = self._resolve_candidate_symbols(resolved_trade_date, limit)
        if not symbols:
            return {
                "should_refresh": False,
                "reason": "no_candidates",
                "trade_date": resolved_trade_date,
                "source": source,
                "as_of_time": resolved_as_of_time,
                "symbol_count": 0,
                "symbols": [],
            }

        signature = self._build_signature(resolved_trade_date, symbols, source, resolved_as_of_time)
        latest = self.get_latest()
        interval_seconds = self._runtime_config().watch.candidate_poll_seconds
        if not latest:
            return {
                "should_refresh": True,
                "reason": "missing",
                "trade_date": resolved_trade_date,
                "source": source,
                "as_of_time": resolved_as_of_time,
                "symbol_count": len(symbols),
                "symbols": symbols,
                "signature": signature,
                "interval_seconds": interval_seconds,
                "elapsed_seconds": None,
                "is_fresh": False,
                "signature_changed": True,
            }

        generated_at = latest.get("generated_at")
        elapsed_seconds = None
        if generated_at:
            elapsed_seconds = max(int((now - datetime.fromisoformat(generated_at)).total_seconds()), 0)
        is_fresh = self._is_fresh(latest, now)
        signature_changed = latest.get("signature") != signature

        if signature_changed or not is_fresh:
            if elapsed_seconds is None or elapsed_seconds >= interval_seconds:
                reason = "signature_changed" if signature_changed else "expired"
                return {
                "should_refresh": True,
                "reason": reason,
                "trade_date": resolved_trade_date,
                "source": source,
                "as_of_time": resolved_as_of_time,
                "symbol_count": len(symbols),
                "symbols": symbols,
                "signature": signature,
                    "interval_seconds": interval_seconds,
                    "elapsed_seconds": elapsed_seconds,
                    "is_fresh": is_fresh,
                    "signature_changed": signature_changed,
                }
            return {
                "should_refresh": False,
                "reason": "poll_interval",
                "trade_date": resolved_trade_date,
                "source": source,
                "as_of_time": resolved_as_of_time,
                "symbol_count": len(symbols),
                "symbols": symbols,
                "signature": signature,
                "interval_seconds": interval_seconds,
                "elapsed_seconds": elapsed_seconds,
                "is_fresh": is_fresh,
                "signature_changed": signature_changed,
            }

        return {
            "should_refresh": False,
            "reason": "fresh",
            "trade_date": resolved_trade_date,
            "source": source,
            "as_of_time": resolved_as_of_time,
            "symbol_count": len(symbols),
            "symbols": symbols,
            "signature": signature,
            "interval_seconds": interval_seconds,
            "elapsed_seconds": elapsed_seconds,
            "is_fresh": is_fresh,
            "signature_changed": False,
        }

    def refresh_if_due(
        self,
        trade_date: str | None = None,
        source: str = "candidate_pool",
        limit: int = 30,
        force: bool = False,
        trigger: str = "scheduler",
        as_of_time: str | None = None,
    ) -> dict:
        decision = self.should_refresh_candidates(
            trade_date=trade_date,
            source=source,
            limit=limit,
            as_of_time=as_of_time,
        )
        if force:
            decision["should_refresh"] = True
            decision["reason"] = "forced"
        if not decision["should_refresh"]:
            return {
                "ok": True,
                "refreshed": False,
                "trigger": trigger,
                **decision,
            }

        pack = self.precompute(
            trade_date=decision["trade_date"],
            symbols=decision["symbols"],
            source=source,
            limit=limit,
            force=force,
            as_of_time=decision.get("as_of_time"),
        )
        return {
            "ok": True,
            "refreshed": True,
            "trigger": trigger,
            "reason": decision["reason"],
            "trade_date": pack["trade_date"],
            "source": pack["source"],
            "symbol_count": pack["symbol_count"],
            "pack_id": pack["pack_id"],
            "reused": pack["reused"],
            "expires_at": pack["expires_at"],
            "signature": pack["signature"],
            "interval_seconds": decision.get("interval_seconds"),
            "elapsed_seconds": decision.get("elapsed_seconds"),
        }

    def _persist(self, pack: dict) -> None:
        history = self._research_state_store.get("dossier_pack_history", [])
        history.append(pack)
        self._research_state_store.set("latest_dossier_pack", pack)
        self._research_state_store.set("dossier_pack_history", self._retain_trade_dates(history))

    def _persist_behavior_profile_artifact(
        self,
        *,
        trade_date: str,
        generated_at: str,
        signature: str,
        records: dict[str, dict],
    ) -> dict:
        existing = self._research_state_store.get("latest_stock_behavior_profiles", {})
        merged: dict[str, dict] = {}
        if existing.get("trade_date") == trade_date:
            merged.update(
                {
                    item["symbol"]: item
                    for item in existing.get("items", [])
                    if isinstance(item, dict) and item.get("symbol") and item.get("profile")
                }
            )
        for symbol, record in records.items():
            current = merged.get(symbol)
            if current is None or self._should_replace_behavior_profile_record(current, record, trade_date):
                merged[symbol] = record

        items = list(merged.values())
        payload = {
            "trade_date": trade_date,
            "generated_at": generated_at,
            "signature": signature,
            "symbol_count": len(items),
            "source_counts": self._behavior_profile_source_counts(items),
            "items": items,
        }
        history = self._research_state_store.get("stock_behavior_profile_history", [])
        history.append(payload)
        self._research_state_store.set("latest_stock_behavior_profiles", payload)
        self._research_state_store.set("stock_behavior_profile_history", self._retain_trade_dates(history))

        serving_root = self._settings.storage_root / "serving"
        serving_root.mkdir(parents=True, exist_ok=True)
        serving_path = serving_root / "latest_stock_behavior_profiles.json"
        serving_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def _retain_trade_dates(self, history: list[dict]) -> list[dict]:
        retain_days = self._runtime_config().snapshots.dossier_retention_trading_days
        seen_dates: list[str] = []
        retained_reversed: list[dict] = []
        for item in reversed(history):
            trade_date = item.get("trade_date")
            if trade_date not in seen_dates:
                seen_dates.append(trade_date)
            if len(seen_dates) > retain_days:
                continue
            retained_reversed.append(item)
        return list(reversed(retained_reversed))

    def _resolve_trade_date(self, as_of_time: str | None = None) -> str:
        if as_of_time:
            try:
                return datetime.fromisoformat(as_of_time).date().isoformat()
            except ValueError:
                pass
        latest_runtime = self._runtime_state_store.get("latest_runtime_report", {})
        generated_at = latest_runtime.get("generated_at")
        if generated_at:
            return datetime.fromisoformat(generated_at).date().isoformat()
        return self._now_factory().date().isoformat()

    def _resolve_symbols(self, trade_date: str, symbols: list[str], source: str, limit: int) -> list[str]:
        if symbols:
            return symbols[:limit]
        if self._candidate_case_service and source == "candidate_pool":
            cases = self._candidate_case_service.list_cases(trade_date=trade_date, limit=limit)
            if cases:
                return [item.symbol for item in cases[:limit]]
        latest_runtime = self._runtime_state_store.get("latest_runtime_report", {})
        top_picks = latest_runtime.get("top_picks", [])
        if top_picks:
            return [item["symbol"] for item in top_picks[:limit]]
        return self._fetcher.fetch_universe("main-board")[:limit]

    def _resolve_candidate_symbols(self, trade_date: str, limit: int) -> list[str]:
        if self._candidate_case_service:
            cases = self._candidate_case_service.list_cases(trade_date=trade_date, limit=limit)
            if cases:
                return [item.symbol for item in cases[:limit]]
        latest_runtime = self._runtime_state_store.get("latest_runtime_report", {})
        top_picks = latest_runtime.get("top_picks", [])
        if top_picks:
            return [item["symbol"] for item in top_picks[:limit]]
        selected_symbols = latest_runtime.get("selected_symbols", [])
        if selected_symbols:
            return selected_symbols[:limit]
        return []

    def _case_map(self, trade_date: str) -> dict[str, object]:
        if not self._candidate_case_service:
            return {}
        cases = self._candidate_case_service.list_cases(trade_date=trade_date, limit=500)
        return {item.symbol: item for item in cases}

    def _research_items(self, as_of_time: str | None = None) -> list[dict]:
        news = self._research_state_store.get("news", [])
        announcements = self._research_state_store.get("announcements", [])
        policy = self._research_state_store.get("policy", [])
        items = [self._normalize_event_item(item) for item in (news + announcements + policy)]
        cutoff = self._parse_optional_datetime(as_of_time)
        if cutoff is not None:
            items = [
                item for item in items
                if (
                    self._parse_optional_datetime(self._event_sort_key(item)) is None
                    or self._parse_optional_datetime(self._event_sort_key(item)) <= cutoff
                )
            ]
        items.sort(key=self._event_sort_key, reverse=True)
        return items

    def _research_map(self, symbols: list[str], research_items: list[dict]) -> dict[str, list[dict]]:
        allowed = set(symbols)
        result: dict[str, list[dict]] = {}
        for item in research_items:
            if item.get("symbol") not in allowed:
                continue
            result.setdefault(item["symbol"], []).append(item)
        return result

    def _runtime_config(self):
        return self._config_mgr.get() if self._config_mgr else RuntimeConfig()

    @staticmethod
    def _build_signature(trade_date: str, symbols: list[str], source: str, as_of_time: str | None = None) -> str:
        suffix = f":{as_of_time}" if as_of_time else ""
        return f"{trade_date}:{source}:{','.join(symbols)}{suffix}"

    @staticmethod
    def _resolve_as_of_time(as_of_time: str | None) -> str | None:
        text = str(as_of_time or "").strip()
        return text or None

    @staticmethod
    def _parse_optional_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _is_fresh(payload: dict, now: datetime) -> bool:
        expires_at = payload.get("expires_at")
        if not expires_at:
            return False
        return now <= datetime.fromisoformat(expires_at)

    def _resolve_name(self, symbol: str, case, snapshot) -> str:
        if case and case.name:
            return case.name
        if snapshot and snapshot.name:
            return snapshot.name
        return self._market.get_symbol_name(symbol)

    @staticmethod
    def _change_pct(last_price: float, pre_close: float) -> float:
        if not pre_close:
            return 0.0
        return round((last_price - pre_close) / pre_close, 6)

    def _fetch_core_index_snapshots(self):
        symbols = [item[0] for item in CORE_INDEX_SYMBOLS]
        return self._fetcher.fetch_index_quotes(symbols)

    def _build_market_context(self, index_snapshots, generated_at: str) -> dict:
        name_map = dict(CORE_INDEX_SYMBOLS)
        items = []
        summary_lines = []
        for item in index_snapshots:
            change_pct = self._change_pct(item.last_price, item.pre_close)
            payload = {
                "symbol": item.symbol,
                "name": name_map.get(item.symbol, item.name or item.symbol),
                "last_price": item.last_price,
                "pre_close": item.pre_close,
                "change_pct": change_pct,
                "volume": item.volume,
            }
            items.append(payload)
            if len(summary_lines) < 4:
                summary_lines.append(f"{payload['name']} {change_pct * 100:.2f}%")
        freshness = build_freshness_meta(
            source_at=generated_at,
            fetched_at=generated_at,
            generated_at=generated_at,
            fresh_seconds=60,
        )
        return {
            **freshness.model_dump(),
            "generated_at": generated_at,
            "snapshot_at": generated_at,
            "index_count": len(items),
            "items": items,
            "summary_lines": summary_lines,
        }

    def _build_event_context(self, symbols: list[str], research_items: list[dict], generated_at: str) -> dict:
        related = self._collect_related_events(symbols, research_items)
        counts_by_scope = {scope: 0 for scope in ("market", "sector", "symbol", "macro", "unknown")}
        counts_by_category: dict[str, int] = {}
        symbol_counts = {symbol: 0 for symbol in symbols}
        latest_titles: list[str] = []
        latest_event_at: str | None = None
        highlights: list[dict] = []
        by_scope: dict[str, list[dict]] = {scope: [] for scope in ("market", "sector", "symbol", "macro", "unknown")}
        for item in related:
            scope = item.get("impact_scope", "unknown")
            category = item.get("category", "news")
            counts_by_scope[scope] = counts_by_scope.get(scope, 0) + 1
            counts_by_category[category] = counts_by_category.get(category, 0) + 1
            item_symbol = item.get("symbol")
            if item_symbol in symbol_counts:
                symbol_counts[item_symbol] += 1
            title = item.get("title")
            if title and title not in latest_titles and len(latest_titles) < 10:
                latest_titles.append(title)
            event_at = item.get("event_at") or item.get("recorded_at")
            if event_at and (latest_event_at is None or event_at > latest_event_at):
                latest_event_at = event_at
            digest = self._event_digest(item)
            if len(highlights) < 12:
                highlights.append(digest)
            if len(by_scope.setdefault(scope, [])) < 8:
                by_scope[scope].append(digest)
        freshness = build_freshness_meta(
            source_at=latest_event_at or generated_at,
            fetched_at=generated_at,
            generated_at=generated_at,
            fresh_seconds=300,
            warm_seconds=3600,
        )
        return {
            **freshness.model_dump(),
            "generated_at": generated_at,
            "symbol_event_counts": symbol_counts,
            "counts_by_scope": counts_by_scope,
            "counts_by_category": counts_by_category,
            "latest_titles": latest_titles,
            "total_event_count": len(related),
            "highlights": highlights,
            "by_scope": by_scope,
        }

    def _build_symbol_context(
        self,
        *,
        trade_date: str,
        symbol: str,
        name: str,
        snapshot,
        daily_bar,
        behavior_profile: StockBehaviorProfile | None,
        behavior_profile_source: str,
        behavior_profile_trade_date: str,
        market_context: dict,
        attributed_events: dict[str, list[dict]],
        generated_at: str,
    ) -> dict:
        market_relative = self._build_market_relative_context(snapshot, daily_bar, market_context)
        event_summary = self._build_symbol_event_summary(attributed_events)
        freshness = build_freshness_meta(
            source_at=event_summary.get("latest_event_at") or generated_at,
            fetched_at=generated_at,
            generated_at=generated_at,
            fresh_seconds=300,
            warm_seconds=3600,
        )
        return {
            **freshness.model_dump(),
            "trade_date": trade_date,
            "symbol": symbol,
            "name": name,
            "generated_at": generated_at,
            "market_relative": market_relative,
            "sector_relative": {
                "comparison_status": "event_proxy_only",
                "reason": "sector_price_series_not_ready",
                "sector_tags": event_summary["sector_tags"],
                "sector_event_count": event_summary["counts_by_scope"]["sector"],
                "sector_latest_titles": event_summary["latest_by_scope"]["sector"],
            },
            "event_summary": event_summary,
            "event_attribution": {
                scope: [self._event_digest(item) for item in items[:5]]
                for scope, items in attributed_events.items()
            },
            "behavior_profile": behavior_profile.model_dump() if behavior_profile is not None else None,
            "behavior_profile_source": behavior_profile_source or None,
            "behavior_profile_trade_date": behavior_profile_trade_date or None,
            "evidence_chain": {
                "snapshot_available": bool(snapshot),
                "daily_bar_available": bool(daily_bar),
                "event_ids": event_summary["event_ids"],
                "evidence_urls": event_summary["evidence_urls"],
            },
        }

    def _build_behavior_profiles(
        self,
        *,
        trade_date: str,
        symbols: list[str],
        history_map: dict[str, list],
        generated_at: str,
        force_rebuild: bool = False,
    ) -> tuple[dict[str, StockBehaviorProfile], dict[str, dict], dict]:
        profiles: dict[str, StockBehaviorProfile] = {}
        records: dict[str, dict] = {}
        source_counts = {"computed": 0, "artifact_cache": 0, "history_cache": 0}
        cached_records = self._load_behavior_profile_records(symbols)
        for symbol in symbols:
            cached_record = cached_records.get(symbol)
            cached_profile = self._profile_from_record(symbol, cached_record)
            cached_trade_date = str((cached_record or {}).get("profile_trade_date") or "")
            items = history_map.get(symbol, [])

            if cached_profile is not None and cached_trade_date == trade_date and not force_rebuild:
                profiles[symbol] = cached_profile
                records[symbol] = self._make_behavior_profile_record(
                    symbol=symbol,
                    profile=cached_profile,
                    generated_at=generated_at,
                    profile_trade_date=trade_date,
                    source="artifact_cache",
                    sample_days=int((cached_record or {}).get("sample_days") or 0),
                    zt_sample_days=int((cached_record or {}).get("zt_sample_days") or 0),
                )
                source_counts["artifact_cache"] += 1
                continue

            if items:
                history_rows, zt_sample_days = self._history_rows_from_bars(symbol, items)
                profile = self._stock_profile_builder.build(symbol, history_rows, baseline=cached_profile)
                profiles[symbol] = profile
                records[symbol] = self._make_behavior_profile_record(
                    symbol=symbol,
                    profile=profile,
                    generated_at=generated_at,
                    profile_trade_date=trade_date,
                    source="computed",
                    sample_days=len(history_rows),
                    zt_sample_days=zt_sample_days,
                )
                source_counts["computed"] += 1
                continue

            if cached_profile is not None:
                source = "artifact_cache" if cached_trade_date == trade_date else "history_cache"
                profiles[symbol] = cached_profile
                records[symbol] = self._make_behavior_profile_record(
                    symbol=symbol,
                    profile=cached_profile,
                    generated_at=generated_at,
                    profile_trade_date=cached_trade_date or trade_date,
                    source=source,
                    sample_days=int((cached_record or {}).get("sample_days") or 0),
                    zt_sample_days=int((cached_record or {}).get("zt_sample_days") or 0),
                )
                source_counts[source] += 1

        coverage_ratio = round(len(profiles) / len(symbols), 4) if symbols else 0.0
        missing_symbols = [symbol for symbol in symbols if symbol not in profiles]
        context = {
            "trade_date": trade_date,
            "generated_at": generated_at,
            "profile_count": len(profiles),
            "coverage_ratio": coverage_ratio,
            "source_counts": source_counts,
            "missing_symbols": missing_symbols,
        }
        return profiles, records, context

    def _history_rows_from_bars(self, symbol: str, items: list) -> tuple[list[dict], int]:
        ordered = sorted(items, key=lambda item: item.bar.trade_time)
        closes = [float(item.bar.close) for item in ordered]
        limit_ratio = get_price_limit_ratio(symbol)
        history_rows: list[dict] = []
        zt_sample_days = 0
        for index, item in enumerate(ordered):
            current_close = closes[index]
            pre_close = float(item.bar.pre_close or (closes[index - 1] if index > 0 else current_close) or current_close)
            limit_price = pre_close * (1 + limit_ratio)
            touched_limit = bool(item.bar.high >= limit_price * 0.998) if pre_close > 0 else False
            closed_limit = bool(item.is_limit_up or current_close >= limit_price * 0.998) if pre_close > 0 else False
            intraday_pullback = bool(item.bar.low <= limit_price * 0.985) if pre_close > 0 else False
            if touched_limit:
                zt_sample_days += 1

            def _future_return(days: int) -> float:
                target_index = index + days
                if target_index >= len(closes) or current_close <= 0:
                    return 0.0
                return round((closes[target_index] - current_close) / current_close, 4)

            change_pct = float(item.change_pct or 0.0)
            history_rows.append(
                {
                    "is_zt": touched_limit,
                    "seal_success": closed_limit,
                    "bombed": touched_limit and not closed_limit,
                    "afternoon_resealed": closed_limit and intraday_pullback,
                    "next_day_return": _future_return(1),
                    "return_day_1": _future_return(1),
                    "return_day_2": _future_return(2),
                    "return_day_3": _future_return(3),
                    "sector_rank": 1 if closed_limit else 2 if touched_limit else 3 if change_pct >= 4 else 5 if change_pct >= 0 else 8,
                    "is_leader": bool(closed_limit or (touched_limit and change_pct >= 4)),
                }
            )
        return history_rows, zt_sample_days

    def _load_behavior_profile_records(self, symbols: list[str]) -> dict[str, dict]:
        allowed = set(symbols)
        records: dict[str, dict] = {}
        latest = self._research_state_store.get("latest_stock_behavior_profiles", {})
        history = self._research_state_store.get("stock_behavior_profile_history", [])
        packs = [*history]
        if latest and latest not in packs:
            packs.append(latest)
        for pack in reversed(packs):
            for item in pack.get("items", []):
                normalized = self._normalize_behavior_profile_record(item, pack_trade_date=pack.get("trade_date"))
                if normalized is None:
                    continue
                symbol = normalized["symbol"]
                if symbol in records or symbol not in allowed:
                    continue
                records[symbol] = normalized
            if len(records) >= len(allowed):
                break
        return records

    @staticmethod
    def _profile_from_record(symbol: str, record: dict | None) -> StockBehaviorProfile | None:
        if not record:
            return None
        payload = record.get("profile")
        if not isinstance(payload, dict):
            return None
        return StockBehaviorProfile.model_validate({"symbol": symbol, **payload})

    @staticmethod
    def _make_behavior_profile_record(
        *,
        symbol: str,
        profile: StockBehaviorProfile,
        generated_at: str,
        profile_trade_date: str,
        source: str,
        sample_days: int,
        zt_sample_days: int,
    ) -> dict:
        return {
            "symbol": symbol,
            "generated_at": generated_at,
            "profile_trade_date": profile_trade_date,
            "source": source,
            "sample_days": sample_days,
            "zt_sample_days": zt_sample_days,
            "profile": profile.model_dump(),
        }

    @staticmethod
    def _normalize_behavior_profile_record(item: dict, pack_trade_date: str | None = None) -> dict | None:
        if not isinstance(item, dict):
            return None
        symbol = str(item.get("symbol") or "")
        payload = item.get("profile")
        if not symbol or not isinstance(payload, dict):
            return None
        profile = StockBehaviorProfile.model_validate({"symbol": symbol, **payload})
        return {
            "symbol": symbol,
            "generated_at": str(item.get("generated_at") or ""),
            "profile_trade_date": str(item.get("profile_trade_date") or pack_trade_date or ""),
            "source": str(item.get("source") or "history_cache"),
            "sample_days": int(item.get("sample_days") or 0),
            "zt_sample_days": int(item.get("zt_sample_days") or 0),
            "profile": profile.model_dump(),
        }

    @staticmethod
    def _behavior_profile_source_counts(items: list[dict]) -> dict[str, int]:
        counts = {"computed": 0, "artifact_cache": 0, "history_cache": 0}
        for item in items:
            source = str(item.get("source") or "")
            if source in counts:
                counts[source] += 1
        return counts

    @staticmethod
    def _should_replace_behavior_profile_record(current: dict, incoming: dict, trade_date: str) -> bool:
        def _score(record: dict) -> tuple[int, int, str]:
            profile_trade_date = str(record.get("profile_trade_date") or "")
            source = str(record.get("source") or "")
            return (
                1 if profile_trade_date == trade_date else 0,
                1 if source == "computed" else 0,
                str(record.get("generated_at") or ""),
            )

        return _score(incoming) >= _score(current)

    def _build_market_relative_context(self, snapshot, daily_bar, market_context: dict) -> dict:
        benchmark = None
        for item in market_context.get("items", []):
            if item.get("symbol") == "000300.SH":
                benchmark = item
                break
        if benchmark is None and market_context.get("items"):
            benchmark = market_context["items"][0]

        symbol_change_pct = None
        if snapshot:
            symbol_change_pct = self._change_pct(snapshot.last_price, snapshot.pre_close)
        elif daily_bar:
            symbol_change_pct = daily_bar.change_pct
        benchmark_change_pct = benchmark.get("change_pct") if benchmark else None
        relative_strength_pct = None
        posture = "unavailable"
        if symbol_change_pct is not None and benchmark_change_pct is not None:
            relative_strength_pct = round(symbol_change_pct - benchmark_change_pct, 6)
            if relative_strength_pct >= 0.01:
                posture = "outperform"
            elif relative_strength_pct <= -0.01:
                posture = "underperform"
            else:
                posture = "inline"
        return {
            "symbol_change_pct": symbol_change_pct,
            "benchmark_symbol": benchmark.get("symbol") if benchmark else None,
            "benchmark_name": benchmark.get("name") if benchmark else None,
            "benchmark_change_pct": benchmark_change_pct,
            "relative_strength_pct": relative_strength_pct,
            "posture": posture,
        }

    def _build_symbol_event_summary(self, attributed_events: dict[str, list[dict]]) -> dict:
        all_related: dict[str, dict] = {}
        latest_titles: list[str] = []
        event_ids: list[str] = []
        evidence_urls: list[str] = []
        latest_event_at: str | None = None
        counts_by_scope = {scope: len(attributed_events.get(scope, [])) for scope in ("market", "sector", "symbol")}
        latest_by_scope = {
            scope: [item.get("title", "") for item in attributed_events.get(scope, [])[:3] if item.get("title")]
            for scope in ("market", "sector", "symbol")
        }
        sector_tags = sorted(
            {
                tag
                for item in attributed_events.get("sector", [])
                for tag in item.get("tags", [])
            }
        )
        for scope in ("market", "sector", "symbol"):
            for item in attributed_events.get(scope, []):
                dedupe_key = item.get("dedupe_key") or item.get("event_id") or item.get("title", "")
                all_related.setdefault(dedupe_key, item)
                title = item.get("title")
                if title and title not in latest_titles and len(latest_titles) < 8:
                    latest_titles.append(title)
                event_id = item.get("event_id")
                if event_id and event_id not in event_ids:
                    event_ids.append(event_id)
                evidence_url = item.get("evidence_url")
                if evidence_url and evidence_url not in evidence_urls:
                    evidence_urls.append(evidence_url)
                event_at = item.get("event_at") or item.get("recorded_at")
                if event_at and (latest_event_at is None or event_at > latest_event_at):
                    latest_event_at = event_at
        categories: dict[str, int] = {}
        high_severity_count = 0
        for item in all_related.values():
            category = item.get("category", "news")
            categories[category] = categories.get(category, 0) + 1
            if item.get("severity") in {"warning", "critical"}:
                high_severity_count += 1
        return {
            "counts_by_scope": counts_by_scope,
            "total_related_event_count": len(all_related),
            "latest_titles": latest_titles,
            "latest_by_scope": latest_by_scope,
            "latest_event_at": latest_event_at,
            "high_severity_count": high_severity_count,
            "categories": categories,
            "sector_tags": sector_tags,
            "event_ids": event_ids[:10],
            "evidence_urls": evidence_urls[:10],
        }

    def _collect_related_events(self, symbols: list[str], research_items: list[dict]) -> list[dict]:
        allowed = set(symbols)
        pool_tags: set[str] = set()
        for symbol in symbols:
            pool_tags.update(self._symbol_event_tags(symbol, research_items))
        related: list[dict] = []
        seen_keys: set[str] = set()
        for item in research_items:
            scope = item.get("impact_scope", "unknown")
            include = False
            if scope in {"market", "macro"}:
                include = True
            elif item.get("symbol") in allowed:
                include = True
            elif scope == "sector" and pool_tags and pool_tags.intersection(item.get("tags", [])):
                include = True
            if not include:
                continue
            dedupe_key = item.get("dedupe_key") or item.get("event_id") or item.get("title", "")
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            related.append(item)
        return related

    def _attribute_events(self, symbol: str, research_items: list[dict]) -> dict[str, list[dict]]:
        attributed = {"market": [], "sector": [], "symbol": []}
        symbol_tags = self._symbol_event_tags(symbol, research_items)
        for item in research_items:
            scope = item.get("impact_scope", "unknown")
            item_symbol = item.get("symbol")
            if scope in {"market", "macro"}:
                attributed["market"].append(item)
                continue
            if item_symbol == symbol:
                attributed["symbol"].append(item)
                if scope == "sector":
                    attributed["sector"].append(item)
                continue
            if scope == "sector" and symbol_tags and symbol_tags.intersection(item.get("tags", [])):
                attributed["sector"].append(item)
        for scope in attributed:
            attributed[scope].sort(key=self._event_sort_key, reverse=True)
        return attributed

    def _symbol_event_tags(self, symbol: str, research_items: list[dict]) -> set[str]:
        tags: set[str] = set()
        for item in research_items:
            if item.get("symbol") != symbol:
                continue
            tags.update(item.get("tags", []))
        return tags

    def _normalize_event_item(self, item: dict) -> dict:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        raw_tags = item.get("tags")
        if raw_tags is None:
            raw_tags = payload.get("tags", [])
        impact_scope = str(item.get("impact_scope") or "unknown").strip().lower()
        if impact_scope not in {"market", "sector", "symbol", "macro", "unknown"}:
            impact_scope = "unknown"
        return {
            "event_id": item.get("event_id") or item.get("dedupe_key") or item.get("title", ""),
            "symbol": item.get("symbol", ""),
            "name": item.get("name", ""),
            "title": item.get("title", ""),
            "summary": item.get("summary", ""),
            "source": item.get("source", ""),
            "source_type": item.get("source_type") or item.get("category", ""),
            "category": item.get("category") or item.get("event_type") or "news",
            "event_type": item.get("event_type") or payload.get("event_type") or item.get("category") or "news",
            "impact": item.get("impact") or payload.get("impact") or "neutral",
            "severity": item.get("severity", "info"),
            "sentiment": item.get("sentiment", "neutral"),
            "event_at": item.get("event_at") or item.get("event_time") or item.get("recorded_at") or "",
            "recorded_at": item.get("recorded_at") or item.get("generated_at") or "",
            "impact_scope": impact_scope,
            "evidence_url": item.get("evidence_url", ""),
            "dedupe_key": item.get("dedupe_key", ""),
            "tags": self._normalize_tags(raw_tags),
            "staleness_level": item.get("staleness_level", "missing"),
        }

    @staticmethod
    def _event_sort_key(item: dict) -> str:
        return item.get("event_at") or item.get("recorded_at") or ""

    @staticmethod
    def _normalize_tags(raw_tags) -> list[str]:
        normalized: list[str] = []
        if not isinstance(raw_tags, list):
            return normalized
        for value in raw_tags:
            text = str(value).strip().lower()
            if not text or text in GENERIC_EVENT_TAGS or text in normalized:
                continue
            normalized.append(text)
        return normalized

    @staticmethod
    def _event_digest(item: dict) -> dict:
        return {
            "event_id": item.get("event_id"),
            "symbol": item.get("symbol"),
            "title": item.get("title"),
            "category": item.get("category"),
            "event_type": item.get("event_type"),
            "impact": item.get("impact"),
            "severity": item.get("severity"),
            "sentiment": item.get("sentiment"),
            "impact_scope": item.get("impact_scope"),
            "event_at": item.get("event_at"),
            "tags": item.get("tags", []),
            "evidence_url": item.get("evidence_url", ""),
        }

    def _build_market_structure_snapshot(self, index_snapshots, generated_at: str) -> MarketStructureSnapshotRecord:
        avg_change_pct = 0.0
        if index_snapshots:
            avg_change_pct = sum(self._change_pct(item.last_price, item.pre_close) for item in index_snapshots) / len(index_snapshots)
        if avg_change_pct >= 0.01:
            sentiment_label = "risk_on"
        elif avg_change_pct <= -0.01:
            sentiment_label = "risk_off"
        else:
            sentiment_label = "balanced"
        return MarketStructureSnapshotRecord(
            snapshot_at=generated_at,
            broad_breadth=round(avg_change_pct, 6),
            market_sentiment_label=sentiment_label,
            **build_freshness_meta(
                source_at=generated_at,
                fetched_at=generated_at,
                generated_at=generated_at,
                fresh_seconds=60,
            ).model_dump(),
        )
