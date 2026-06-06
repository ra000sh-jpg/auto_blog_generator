"""매크로 데이터 파이프라인 공통 모델."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


MACRO_DOCUMENT_STATUSES = {
    "new",
    "downloaded",
    "parsed",
    "analyzed",
    "failed",
    "unsupported",
}

MACRO_CANDIDATE_STATUSES = {
    "draft",
    "needs_review",
    "approved",
    "rejected",
    "published",
}


@dataclass(frozen=True)
class MacroSourceConfig:
    """기관별 수집 설정."""

    source: str
    list_url: str
    base_url: str
    keywords: tuple[str, ...]
    max_detail_fetch: int = 10


@dataclass(frozen=True)
class MacroDocumentCandidate:
    """수집 단계에서 발견한 문서 후보."""

    source: str
    title: str
    url: str
    published_at: str = ""
    file_url: str = ""
    file_type: str = "html"
    attachments: tuple[Dict[str, str], ...] = field(default_factory=tuple)
    status: str = "new"
    hash: str = ""


@dataclass(frozen=True)
class MacroMetric:
    """원문 근거를 포함한 단일 수치."""

    key: str
    label: str
    value: str
    evidence: str
    confidence: float = 0.75


@dataclass(frozen=True)
class MacroAnalysisResult:
    """문서 분석 결과."""

    parsed: Dict[str, Any]
    metrics: Dict[str, Any]
    insight: Dict[str, Any]
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    quality: Dict[str, Any] = field(default_factory=dict)
