"""自然语言参数调整解释器。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from .param_registry import EffectivePeriod, ParameterRegistry
from ..selection_preferences import normalize_excluded_theme_keywords

_NUM_TOKEN = r"(\d+(?:\.\d+)?|[一二两三四五六七八九十])"
_UNIT_TOKEN = r"(秒钟|秒|分钟|分|小时|时|天|日|周|星期|只|个|成|%|元|块|万|万元|千|百)?"
_CN_NUMBERS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass(frozen=True)
class AdjustmentSpec:
    param_key: str
    aliases: tuple[str, ...]
    kind: str
    config_key: str | None = None


class ParsedAdjustment(BaseModel):
    param_key: str
    scope: str
    value_type: str
    new_value: int | float | str | bool
    config_key: str | None = None
    config_value: int | float | str | bool | None = None
    matched_alias: str
    matched_text: str
    effective_period: EffectivePeriod | None = None


class NaturalLanguageAdjustmentResult(BaseModel):
    instruction: str
    matched: list[ParsedAdjustment] = Field(default_factory=list)
    unmatched: list[str] = Field(default_factory=list)
    inferred_effective_period: EffectivePeriod | None = None


class NaturalLanguageAdjustmentInterpreter:
    def __init__(self, registry: ParameterRegistry) -> None:
        self._registry = registry

    def interpret(self, instruction: str) -> NaturalLanguageAdjustmentResult:
        text = instruction.strip()
        inferred_period = self._infer_effective_period(text)
        matched: list[ParsedAdjustment] = []
        seen_keys: set[str] = set()

        for spec in _SPECS:
            parsed = self._match_spec(text, spec, inferred_period)
            if not parsed or parsed.param_key in seen_keys:
                continue
            matched.append(parsed)
            seen_keys.add(parsed.param_key)

        unmatched = [] if matched else ["未识别出可调整项，请明确说明池子规模、轮询/心跳时间或保留时长。"]
        return NaturalLanguageAdjustmentResult(
            instruction=text,
            matched=matched,
            unmatched=unmatched,
            inferred_effective_period=inferred_period,
        )

    def _match_spec(
        self,
        text: str,
        spec: AdjustmentSpec,
        inferred_period: EffectivePeriod | None,
    ) -> ParsedAdjustment | None:
        if spec.kind == "exclude_keywords":
            return self._match_excluded_keywords(text, spec, inferred_period)
        alias_group = "|".join(re.escape(alias) for alias in spec.aliases)
        patterns = [
            rf"(?P<alias>{alias_group})[^0-9一二两三四五六七八九十]{{0,12}}?(?P<num>{_NUM_TOKEN})\s*(?P<unit>{_UNIT_TOKEN})",
            rf"(?P<num>{_NUM_TOKEN})\s*(?P<unit>{_UNIT_TOKEN})[^。；，,\n]{{0,8}}?(?P<alias>{alias_group})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            alias = match.group("alias")
            raw_number = match.group("num")
            unit = match.group("unit") or ""
            value = self._convert_value(raw_number, unit, spec.kind)
            definition = self._registry.get(spec.param_key)
            if definition is None:
                return None
            return ParsedAdjustment(
                param_key=spec.param_key,
                scope=definition.scope,
                value_type=definition.value_type,
                new_value=value,
                config_key=spec.config_key,
                config_value=value if spec.config_key else None,
                matched_alias=alias,
                matched_text=match.group(0),
                effective_period=inferred_period,
            )
        return None

    def _match_excluded_keywords(
        self,
        text: str,
        spec: AdjustmentSpec,
        inferred_period: EffectivePeriod | None,
    ) -> ParsedAdjustment | None:
        alias_group = "|".join(re.escape(alias) for alias in spec.aliases)
        match = re.search(rf"(?P<alias>{alias_group})(?P<targets>[^。；\n]+)", text)
        if not match:
            return None
        targets = self._extract_excluded_targets(match.group("targets"))
        if not targets:
            return None
        definition = self._registry.get(spec.param_key)
        if definition is None:
            return None
        value = ",".join(targets)
        return ParsedAdjustment(
            param_key=spec.param_key,
            scope=definition.scope,
            value_type=definition.value_type,
            new_value=value,
            config_key=spec.config_key,
            config_value=value if spec.config_key else None,
            matched_alias=match.group("alias"),
            matched_text=match.group(0),
            effective_period=inferred_period,
        )

    def _convert_value(self, raw_number: str, unit: str, kind: str) -> int | float:
        numeric = self._parse_number(raw_number)
        if kind == "seconds":
            if unit in {"分钟", "分"}:
                return int(numeric * 60)
            if unit in {"小时", "时"}:
                return int(numeric * 3600)
            return int(numeric)
        if kind == "days":
            if unit in {"周", "星期"}:
                return int(numeric * 5)
            return int(numeric)
        if kind == "percent":
            if unit == "成":
                return round(numeric / 10.0, 4)
            if unit == "%" or numeric > 1:
                return round(numeric / 100.0, 4)
            return round(numeric, 4)
        if kind == "amount":
            if unit in {"万", "万元"}:
                return round(numeric * 10000, 2)
            if unit == "千":
                return round(numeric * 1000, 2)
            if unit == "百":
                return round(numeric * 100, 2)
            return round(numeric, 2)
        return int(numeric)

    @staticmethod
    def _parse_number(raw: str) -> float:
        if raw in _CN_NUMBERS:
            return float(_CN_NUMBERS[raw])
        return float(raw)

    @staticmethod
    def _infer_effective_period(text: str) -> EffectivePeriod | None:
        if any(token in text for token in ["长期", "一直", "以后都", "直到撤销"]):
            return "until_revoked"
        if any(token in text for token in ["明天", "下个交易日", "下一交易日"]):
            return "next_trading_day"
        if any(token in text for token in ["今天", "今日", "盘中", "本次", "马上"]):
            return "today_session"
        return None

    @staticmethod
    def _extract_excluded_targets(text: str) -> list[str]:
        clause = str(text or "").strip()
        suffix_hits = re.findall(r"([A-Za-z0-9\u4e00-\u9fff]+?)(?:概念股|行业股|板块股|题材股|概念|行业|板块|题材|个股|股票|股)", clause)
        if suffix_hits:
            return normalize_excluded_theme_keywords(suffix_hits)
        fallback = re.split(r"[，,、；;和及与/]", clause)
        cleaned = []
        for item in fallback:
            token = str(item or "").strip()
            token = re.sub(r"^(的|先|暂时|今天|今日|盘中|本次)", "", token)
            token = re.sub(r"(为主|这些|这类|方向|标的|即可|都|等|等等)$", "", token).strip()
            if token:
                cleaned.append(token)
        return normalize_excluded_theme_keywords(cleaned)


_SPECS = [
    AdjustmentSpec("excluded_theme_keywords", ("不买", "不要买", "先不买", "先不碰", "禁买", "排除", "剔除", "回避", "避开"), "exclude_keywords"),
    AdjustmentSpec("monitor_auction_heartbeat_save_seconds", ("竞价心跳", "开盘心跳", "尾盘心跳"), "seconds", "watch.auction_heartbeat_save_seconds"),
    AdjustmentSpec("monitor_heartbeat_save_seconds", ("心跳时间", "心跳频率", "心跳"), "seconds", "watch.heartbeat_save_seconds"),
    AdjustmentSpec("candidate_poll_seconds", ("候选轮询", "候选刷新", "候选巡检"), "seconds", "watch.candidate_poll_seconds"),
    AdjustmentSpec("focus_poll_seconds", ("重点轮询", "观察轮询", "重点刷新", "观察刷新"), "seconds", "watch.focus_poll_seconds"),
    AdjustmentSpec("execution_poll_seconds", ("执行轮询", "执行刷新", "执行巡检"), "seconds", "watch.execution_poll_seconds"),
    AdjustmentSpec("base_pool_capacity", ("候选池", "股票池", "基础池"), "integer", "screener_pool_size"),
    AdjustmentSpec("focus_pool_capacity", ("重点池", "观察池", "焦点池"), "integer"),
    AdjustmentSpec("watchlist_capacity", ("观察名单", "观察列表"), "integer"),
    AdjustmentSpec("execution_pool_capacity", ("执行池", "最终池", "推荐池"), "integer", "max_buy_count"),
    AdjustmentSpec("dossier_retention_trading_days", ("保存时长", "保留时长", "留存时长", "保留天数"), "days", "snapshots.dossier_retention_trading_days"),
    AdjustmentSpec("archive_retention_trading_days", ("归档时长", "归档保留", "归档天数"), "days", "snapshots.archive_retention_trading_days"),
    AdjustmentSpec("max_total_position", ("总仓位", "总体仓位"), "percent", "max_total_position"),
    AdjustmentSpec("equity_position_limit", ("测试仓位", "股票测试仓位", "测试股票仓位", "测试资金仓位", "仓位", "股票仓位", "持仓仓位"), "percent", "equity_position_limit"),
    AdjustmentSpec("max_single_position", ("单票仓位", "单股仓位"), "percent", "max_single_position"),
    AdjustmentSpec("max_single_amount", ("单票金额上限", "单票金额", "单股金额上限", "单股金额", "个股最多", "单股最多", "个股不超过", "单股不超过", "个股上限", "单股上限"), "amount", "max_single_amount"),
    AdjustmentSpec("daily_loss_limit", ("日亏损上限", "日损失上限", "单日止损"), "percent", "daily_loss_limit"),
    AdjustmentSpec("reverse_repo_target_ratio", ("逆回购目标", "逆回购仓位", "逆回购占比"), "percent", "reverse_repo_target_ratio"),
    AdjustmentSpec("minimum_total_invested_amount", ("总持仓基线", "测试总持仓", "总持仓金额", "测试总资金"), "amount", "minimum_total_invested_amount"),
    AdjustmentSpec("reverse_repo_reserved_amount", ("逆回购保留金额", "逆回购保留", "逆回购金额", "逆回购预留"), "amount", "reverse_repo_reserved_amount"),
]
