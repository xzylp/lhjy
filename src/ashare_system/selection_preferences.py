"""选股偏好与排除规则。"""

from __future__ import annotations

import re
from typing import Iterable


_SPLIT_PATTERN = re.compile(r"[，,、；;|/]+")
_TRIM_SUFFIXES = ("概念股", "行业股", "板块股", "题材股", "概念", "行业", "板块", "题材", "个股", "股票", "股")


def normalize_excluded_theme_keywords(raw: str | Iterable[str] | None) -> list[str]:
    """把排除主题配置归一为关键词列表。"""

    if raw is None:
        return []
    if isinstance(raw, str):
        parts = _SPLIT_PATTERN.split(raw)
    else:
        parts = [str(item or "") for item in raw]

    normalized: list[str] = []
    seen: set[str] = set()
    for part in parts:
        token = str(part or "").strip()
        token = _trim_theme_token(token)
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized


def match_excluded_theme(
    keywords: Iterable[str],
    *,
    name: str = "",
    resolved_sector: str = "",
    extra_texts: Iterable[str] | None = None,
) -> dict[str, str] | None:
    """判断标的是否命中排除主题。"""

    fields = [
        ("name", str(name or "").strip()),
        ("sector", str(resolved_sector or "").strip()),
    ]
    for text in extra_texts or ():
        value = str(text or "").strip()
        if value:
            fields.append(("context", value))

    for keyword in normalize_excluded_theme_keywords(list(keywords)):
        for field_name, field_text in fields:
            if keyword and field_text and keyword in field_text:
                return {
                    "keyword": keyword,
                    "field": field_name,
                    "matched_text": field_text,
                }
    return None


def _trim_theme_token(value: str) -> str:
    text = str(value or "").strip()
    while True:
        next_text = text
        for suffix in _TRIM_SUFFIXES:
            if next_text.endswith(suffix) and len(next_text) > len(suffix):
                next_text = next_text[: -len(suffix)].strip()
        if next_text == text:
            return text
        text = next_text
