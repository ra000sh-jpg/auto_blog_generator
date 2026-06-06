"""정부 매크로 문서 핵심 수치 추출기."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from .models import MacroMetric


class MacroMetricExtractor:
    """본문에서 수출입/산업/국가별 수치를 근거 문장과 함께 추출한다."""

    INDUSTRY_KEYWORDS = {
        "semiconductor": "반도체",
        "auto": "자동차",
        "petrochemical": "석유화학",
        "steel": "철강",
        "display": "디스플레이",
        "shipbuilding": "선박",
        "battery": "이차전지",
    }
    COUNTRY_KEYWORDS = {
        "us": "미국",
        "china": "중국",
        "asean": "아세안",
        "eu": "EU",
        "japan": "일본",
    }

    def extract(self, text: str) -> Dict[str, Any]:
        """본문에서 핵심 지표를 추출한다."""
        sentences = self._split_sentences(text)
        metrics: List[MacroMetric] = []
        metrics.extend(self._extract_main_trade_metrics(sentences))
        metrics.extend(self._extract_keyword_metrics(sentences, self.INDUSTRY_KEYWORDS, prefix="industry"))
        metrics.extend(self._extract_keyword_metrics(sentences, self.COUNTRY_KEYWORDS, prefix="country"))

        deduped = self._dedupe(metrics)
        return {
            "metrics": [
                {
                    "key": item.key,
                    "label": item.label,
                    "value": item.value,
                    "evidence": item.evidence,
                    "confidence": item.confidence,
                }
                for item in deduped
            ],
            "by_key": {item.key: item.value for item in deduped},
            "metric_count": len(deduped),
            "official_source_count": 1 if deduped else 0,
            "numeric_claims_have_evidence": all(bool(item.evidence) for item in deduped),
        }

    def _extract_main_trade_metrics(self, sentences: List[str]) -> List[MacroMetric]:
        output: List[MacroMetric] = []
        for sentence in sentences:
            normalized = sentence.replace(" ", "")
            if "수출" in normalized:
                value = self._find_percent_for_keyword(sentence, "수출")
                if value:
                    output.append(MacroMetric("export_growth_yoy", "수출 증감률", value, sentence, 0.8))
                amount = self._find_amount_for_keyword(sentence, "수출")
                if amount:
                    output.append(MacroMetric("export_amount_usd_eok", "수출금액", amount, sentence, 0.82))
            if "수입" in normalized:
                value = self._find_percent_for_keyword(sentence, "수입")
                if value:
                    output.append(MacroMetric("import_growth_yoy", "수입 증감률", value, sentence, 0.8))
                amount = self._find_amount_for_keyword(sentence, "수입")
                if amount:
                    output.append(MacroMetric("import_amount_usd_eok", "수입금액", amount, sentence, 0.82))
            if "무역수지" in normalized or "수지" in normalized:
                value = self._find_trade_balance(sentence)
                if value:
                    output.append(MacroMetric("trade_balance", "무역수지", value, sentence, 0.78))
        return output

    def _extract_keyword_metrics(
        self,
        sentences: List[str],
        keywords: Dict[str, str],
        *,
        prefix: str,
    ) -> List[MacroMetric]:
        output: List[MacroMetric] = []
        for sentence in sentences:
            for key, label in keywords.items():
                if label.lower() not in sentence.lower():
                    continue
                value = self._find_percent_for_keyword(sentence, label)
                if not value:
                    continue
                output.append(
                    MacroMetric(
                        key=f"{prefix}_{key}_growth",
                        label=f"{label} 증감률",
                        value=value,
                        evidence=sentence,
                        confidence=0.72,
                    )
                )
        return output

    def _find_percent(self, sentence: str) -> str:
        match = re.search(r"([+\-△▲]?\s*\d+(?:\.\d+)?)\s*%", sentence)
        if not match:
            # '8.4퍼센트' 표현 보조 처리
            match = re.search(r"([+\-△▲]?\s*\d+(?:\.\d+)?)\s*퍼센트", sentence)
        if not match:
            return ""
        value = match.group(1).replace(" ", "")
        value = value.replace("▲", "+").replace("△", "-")
        if not value.startswith(("+", "-")) and "증가" in sentence:
            value = f"+{value}"
        if not value.startswith(("+", "-")) and ("감소" in sentence or "하락" in sentence):
            value = f"-{value}"
        return f"{value}%"

    def _find_percent_for_keyword(self, sentence: str, keyword: str) -> str:
        """여러 수치가 한 문장에 있을 때 키워드 주변의 퍼센트를 우선 선택한다."""
        text = str(sentence or "")
        keyword_text = str(keyword or "")
        positions = [match.start() for match in re.finditer(re.escape(keyword_text), text, flags=re.IGNORECASE)]
        for position in positions:
            forward_window = text[position : position + 140]
            value = self._find_percent(forward_window)
            if value:
                return value
        for position in positions:
            nearby_window = text[max(0, position - 50) : position + 90]
            value = self._find_percent(nearby_window)
            if value:
                return value
        return self._find_percent(text)

    def _find_trade_balance(self, sentence: str) -> str:
        match = re.search(r"(흑자|적자)\s*([0-9,.]+)\s*(억|조)?\s*달러", sentence)
        if match:
            direction, amount, unit = match.groups()
            return f"{direction} {amount}{unit or ''} 달러"
        match = re.search(r"([0-9,.]+)\s*(억|조)?\s*달러\s*(흑자|적자)", sentence)
        if match:
            amount, unit, direction = match.groups()
            return f"{direction} {amount}{unit or ''} 달러"
        return ""

    def _find_amount_for_keyword(self, sentence: str, keyword: str) -> str:
        """키워드 주변의 달러 금액을 억 달러 단위 문자열로 추출한다."""
        text = str(sentence or "")
        keyword_text = str(keyword or "")
        positions = [match.start() for match in re.finditer(re.escape(keyword_text), text, flags=re.IGNORECASE)]
        for position in positions:
            window = text[position : position + 80]
            value = self._find_usd_amount(window)
            if value:
                return value
        return ""

    def _find_usd_amount(self, sentence: str) -> str:
        text = str(sentence or "")
        if any(token in text for token in ("상회", "돌파", "넘어", "넘는", "넘은", "이상")):
            return ""
        match = re.search(r"([0-9,.]+)\s*(억|조)?\s*달러", text)
        if not match:
            return ""
        amount, unit = match.groups()
        return f"{amount}{unit or ''} 달러"

    def _split_sentences(self, text: str) -> List[str]:
        normalized = re.sub(r"[ \t\r\f\v]+", " ", str(text or ""))
        raw_sentences = re.split(r"(?<=[.!?。])\s+|\n+", normalized)
        output = []
        for sentence in raw_sentences:
            cleaned = sentence.strip()
            if len(cleaned) < 8:
                continue
            output.append(cleaned[:500])
        return output[:300]

    def _dedupe(self, metrics: List[MacroMetric]) -> List[MacroMetric]:
        seen: set[str] = set()
        output: List[MacroMetric] = []
        for item in metrics:
            if item.key in seen:
                continue
            seen.add(item.key)
            output.append(item)
        return output
