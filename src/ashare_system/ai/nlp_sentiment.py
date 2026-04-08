"""NLP 情感分析模型"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..logging_config import get_logger

logger = get_logger("ai.nlp_sentiment")

# 扩展关键词词典 (强/中/弱 三级)
POSITIVE_STRONG = ["大幅超预期", "爆发式增长", "重大利好", "战略合作", "重组成功", "业绩大增"]
POSITIVE_MID = ["超预期", "增长", "利好", "增持", "突破", "中标", "提升", "扭亏", "回购"]
POSITIVE_WEAK = ["稳健", "符合预期", "小幅增长", "略有提升"]

NEGATIVE_STRONG = ["净亏损", "重大亏损", "被迫退市", "破产重整", "监管处罚", "强制退市"]
NEGATIVE_MID = ["净亏", "下跌", "风险", "减持", "跌停", "下滑", "处罚", "回落", "亏损"]
NEGATIVE_WEAK = ["略有下滑", "小幅亏损", "不及预期"]

NEGATION_WORDS = ["并非", "不是", "未能", "没有", "不会", "无法", "否认", "不存在"]


@dataclass
class SentimentResult:
    text: str
    score: float        # -1.0 到 1.0
    label: str          # "positive" | "neutral" | "negative"
    confidence: float


class NLPSentimentAnalyzer:
    """NLP 情感分析器 (关键词词典 + 否定句检测)"""

    def analyze(self, text: str) -> SentimentResult:
        """分析单条文本情感"""
        score = self._calc_score(text)
        label = "positive" if score > 0.1 else ("negative" if score < -0.1 else "neutral")
        confidence = min(abs(score) * 2, 1.0)
        return SentimentResult(text=text[:100], score=score, label=label, confidence=confidence)

    def analyze_batch(self, texts: list[str]) -> list[SentimentResult]:
        return [self.analyze(t) for t in texts]

    def _calc_score(self, text: str) -> float:
        # 可选 jieba 分词提升准确率
        words = self._segment(text)
        pos = 0.0
        neg = 0.0
        for kw in POSITIVE_STRONG:
            if kw in text and not self._has_negation(text, kw):
                pos += 1.0
        for kw in POSITIVE_MID:
            if kw in text and not self._has_negation(text, kw):
                pos += 0.5
        for kw in POSITIVE_WEAK:
            if kw in text and not self._has_negation(text, kw):
                pos += 0.2
        for kw in NEGATIVE_STRONG:
            if kw in text:
                neg += 1.0
        for kw in NEGATIVE_MID:
            if kw in text:
                neg += 0.5
        for kw in NEGATIVE_WEAK:
            if kw in text:
                neg += 0.2
        # jieba 分词匹配 (减少误匹配)
        if words:
            word_set = set(words)
            for kw in POSITIVE_MID:
                if kw in word_set and kw not in text[:text.find(kw) + len(kw)]:
                    pos += 0.3  # 分词确认的额外加分
            for kw in NEGATIVE_MID:
                if kw in word_set:
                    neg += 0.3
        total = pos + neg
        if total == 0:
            return 0.0
        return (pos - neg) / (total + 1)

    @staticmethod
    def _segment(text: str) -> list[str] | None:
        """jieba 分词 (可选依赖)"""
        try:
            import jieba
            return list(jieba.cut(text))
        except ImportError:
            return None

    @staticmethod
    def _has_negation(text: str, keyword: str) -> bool:
        """检测关键词前是否有否定词"""
        idx = text.find(keyword)
        if idx < 0:
            return False
        context = text[max(0, idx - 5):idx]
        return any(neg in context for neg in NEGATION_WORDS)
