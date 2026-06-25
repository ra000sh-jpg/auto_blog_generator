"""본문 수치 주장과 Source Pack 근거를 대조하는 Claim Ledger."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


SOURCE_SECTION_MARKERS = (
    "■ 참고한 공식/시장 데이터",
    "## 참고한 공식/시장 데이터",
)

NUMERIC_CLAIM_PATTERN = re.compile(
    r"(?<![\w])[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w])[-+]?\d+(?:\.\d+)?"
)

DATE_LIKE_PATTERN = re.compile(
    r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}$|^\d{1,2}[:시]\d{0,2}$"
)


@dataclass(frozen=True)
class ClaimRecord:
    """본문에서 찾은 수치 주장 한 건."""

    claim_id: str
    text: str
    numbers: tuple[float, ...] = ()
    supported: bool = False
    evidence: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON 저장 가능한 dict로 변환한다."""

        return {
            "claim_id": self.claim_id,
            "text": self.text,
            "numbers": list(self.numbers),
            "supported": self.supported,
            "evidence": list(self.evidence),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ClaimLedgerResult:
    """Claim Ledger 평가 결과."""

    status: str
    claims: tuple[ClaimRecord, ...] = ()
    unsupported_claims: tuple[ClaimRecord, ...] = ()
    checked_claim_count: int = 0
    supported_claim_count: int = 0
    unsupported_claim_count: int = 0
    evidence_metric_count: int = 0
    evidence_source_count: int = 0
    reasons: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """모든 수치 주장이 근거와 연결됐는지 반환한다."""

        return self.unsupported_claim_count == 0

    def to_dict(self) -> dict[str, Any]:
        """JSON 저장 가능한 dict로 변환한다."""

        return {
            "schema_version": "claim_ledger.v1",
            "status": self.status,
            "checked_claim_count": self.checked_claim_count,
            "supported_claim_count": self.supported_claim_count,
            "unsupported_claim_count": self.unsupported_claim_count,
            "evidence_metric_count": self.evidence_metric_count,
            "evidence_source_count": self.evidence_source_count,
            "claims": [claim.to_dict() for claim in self.claims],
            "unsupported_claims": [claim.to_dict() for claim in self.unsupported_claims],
            "reasons": list(self.reasons),
        }


def build_claim_ledger(
    *,
    content: str,
    source_pack: Mapping[str, Any],
    max_unsupported_claims: int = 0,
) -> ClaimLedgerResult:
    """본문의 수치 주장이 Source Pack 근거와 연결되는지 평가한다."""

    del max_unsupported_claims
    evidence = _build_evidence_index(source_pack)
    sentences = _split_claim_sentences(_strip_source_section(content))
    claims: list[ClaimRecord] = []
    for sentence in sentences:
        numbers = _extract_numbers(sentence)
        if not numbers:
            continue
        if _is_noise_numeric_sentence(sentence, numbers):
            continue
        supported, evidence_labels, reason = _match_claim(sentence, numbers, evidence)
        claims.append(
            ClaimRecord(
                claim_id=f"claim_{len(claims) + 1}",
                text=sentence,
                numbers=tuple(numbers),
                supported=supported,
                evidence=tuple(evidence_labels),
                reason=reason,
            )
        )

    unsupported = tuple(claim for claim in claims if not claim.supported)
    status = "passed" if not unsupported else "blocked"
    reasons = tuple(f"근거 없는 수치 주장: {claim.text[:120]}" for claim in unsupported[:5])
    return ClaimLedgerResult(
        status=status,
        claims=tuple(claims),
        unsupported_claims=unsupported,
        checked_claim_count=len(claims),
        supported_claim_count=len(claims) - len(unsupported),
        unsupported_claim_count=len(unsupported),
        evidence_metric_count=len(evidence["metric_values"]),
        evidence_source_count=len(evidence["source_names"]),
        reasons=reasons,
    )


def _build_evidence_index(source_pack: Mapping[str, Any]) -> dict[str, Any]:
    metric_values: list[tuple[float, str]] = []
    metric_aliases: set[str] = set()
    source_names: set[str] = set()

    for metric in _mapping_items(source_pack.get("confirmed_metrics")):
        source = str(metric.get("source", "") or "").strip()
        key = str(metric.get("key", "") or "").strip()
        label = str(metric.get("label", "") or "").strip()
        value = _float_or_none(metric.get("value"))
        evidence_label = " / ".join(item for item in (source, key or label) if item)
        if source:
            source_names.add(source.lower())
        for alias in (key, label):
            if alias:
                metric_aliases.add(alias.lower())
        if value is not None:
            metric_values.append((value, evidence_label or source or key or label))

    for source in _mapping_items(source_pack.get("sources")):
        source_name = str(source.get("source", "") or "").strip()
        metric_key = str(source.get("metric_key", "") or "").strip()
        title = str(source.get("title", "") or "").strip()
        raw_id = str(source.get("raw_id", "") or "").strip()
        value = _float_or_none(source.get("value"))
        if source_name:
            source_names.add(source_name.lower())
        for alias in (metric_key, title, raw_id):
            if alias:
                metric_aliases.add(alias.lower())
        if value is not None:
            evidence_label = " / ".join(item for item in (source_name, metric_key or title) if item)
            metric_values.append((value, evidence_label or source_name or metric_key or title))

    return {
        "metric_values": tuple(metric_values),
        "metric_aliases": tuple(metric_aliases),
        "source_names": tuple(source_names),
    }


def _match_claim(
    sentence: str,
    numbers: Sequence[float],
    evidence: Mapping[str, Any],
) -> tuple[bool, list[str], str]:
    sentence_l = sentence.lower()
    matched: list[str] = []
    for number in numbers:
        number_matched = False
        for value, label in evidence.get("metric_values", ()):
            if _numbers_close(number, value):
                number_matched = True
                if label:
                    matched.append(str(label))
                break
        if not number_matched:
            aliases = evidence.get("metric_aliases", ())
            sources = evidence.get("source_names", ())
            alias_hit = any(alias and alias in sentence_l for alias in aliases)
            source_hit = any(source and source in sentence_l for source in sources)
            if alias_hit and source_hit:
                number_matched = True
                matched.append("source_and_metric_alias")
        if not number_matched:
            return False, matched, "수치가 Source Pack의 확인 수치와 연결되지 않았습니다."
    return True, _dedupe(matched), "Source Pack 확인 수치와 연결됐습니다."


def _numbers_close(left: float, right: float) -> bool:
    if abs(left - right) <= 0.01:
        return True
    if abs(left - right) <= max(abs(right) * 0.005, 0.05):
        return True
    return False


def _split_claim_sentences(content: str) -> list[str]:
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?。！？])\s+|(?<=다\.)\s+|(?<=요\.)\s+|(?<=니다\.)\s+|[\n\r]+", text)
    sentences: list[str] = []
    for part in parts:
        cleaned = re.sub(r"^[•\-*\d.\s]+", "", str(part or "").strip())
        if cleaned and cleaned not in sentences:
            sentences.append(cleaned)
    return sentences


def _strip_source_section(content: str) -> str:
    text = str(content or "")
    cut_indexes = [text.find(marker) for marker in SOURCE_SECTION_MARKERS if marker in text]
    if not cut_indexes:
        return text
    return text[: min(index for index in cut_indexes if index >= 0)]


def _extract_numbers(sentence: str) -> list[float]:
    numbers: list[float] = []
    for match in NUMERIC_CLAIM_PATTERN.finditer(sentence):
        raw = match.group(0)
        if _looks_like_bullet_number(sentence, match.start(), raw):
            continue
        if DATE_LIKE_PATTERN.match(raw):
            continue
        value = _float_or_none(raw.replace(",", ""))
        if value is None:
            continue
        numbers.append(value)
    return numbers


def _is_noise_numeric_sentence(sentence: str, numbers: Sequence[float]) -> bool:
    stripped = sentence.strip()
    if not stripped:
        return True
    if all(abs(number) <= 10 for number in numbers) and re.search(r"\b\d+\s*(가지|번째|단계|번|개)\b", stripped):
        return True
    if re.fullmatch(r"[\d\s./:-]+", stripped):
        return True
    if re.search(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", stripped) and len(numbers) <= 3:
        return True
    return False


def _looks_like_bullet_number(sentence: str, start: int, raw: str) -> bool:
    prefix = sentence[:start].strip()
    suffix = sentence[start + len(raw) : start + len(raw) + 2]
    if not prefix and suffix.startswith((".", ")")):
        return True
    return False


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dedupe(values: Sequence[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    return deduped
