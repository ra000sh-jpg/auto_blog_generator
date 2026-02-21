"""생성 콘텐츠 품질 게이트."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class GateIssue:
    """품질 이슈 단위."""

    stage: str
    code: str
    message: str
    severity: str = "medium"

    def to_dict(self) -> Dict[str, str]:
        return {
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass
class QualityGateResult:
    """품질 게이트 평가 결과."""

    passed: bool
    gate: str
    score: int
    error_code: str
    summary: str
    stage_results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    issues: List[GateIssue] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "gate": self.gate,
            "score": self.score,
            "error_code": self.error_code,
            "summary": self.summary,
            "stage_results": self.stage_results,
            "issues": [item.to_dict() for item in self.issues],
        }


class QualityGate:
    """규칙/근거 기반 품질 필터."""

    DEFAULT_BANNED_PATTERNS: Tuple[Tuple[str, str], ...] = (
        (r"도박", "gambling_term"),
        (r"마약", "drug_term"),
        (r"불법\s*대출", "illegal_loan_term"),
        (r"성인\s*인증\s*없이", "unsafe_adult_phrase"),
    )
    SOURCE_LINE_PATTERN = re.compile(r"^\s*참고 자료\s*:\s*.+\(\s*https?://[^)]+\s*\)\s*$")

    # 환각 탐지: 현재 연도(2026)보다 미래인 연도 단독 등장
    _FUTURE_YEAR_PATTERN = re.compile(r"\b(202[7-9]|20[3-9]\d)\b")
    # 환각 탐지: 단락 내 구체적 수치(숫자+단위) 과밀 — 5개 이상
    _NUMERIC_FACT_PATTERN = re.compile(r"\d+[\s]?(?:%|원|달러|배|명|회|개|번|점|배|분|시간|일|년|월|위)")
    # 키워드 밀도 최소 임계값: 단락 수 대비 키워드 등장 비율
    _MIN_KEYWORD_DENSITY = 0.15

    def __init__(
        self,
        min_content_chars: int = 500,
        banned_patterns: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        self.min_content_chars = max(300, int(min_content_chars))
        pattern_defs = banned_patterns or list(self.DEFAULT_BANNED_PATTERNS)
        self._compiled_patterns: List[Tuple[re.Pattern[str], str]] = [
            (re.compile(pattern, flags=re.IGNORECASE), code)
            for pattern, code in pattern_defs
        ]

    def evaluate(
        self,
        *,
        title: str,
        content: str,
        seed_keywords: List[str],
        topic_mode: str = "",
        rag_context: Optional[List[Dict[str, str]]] = None,
    ) -> QualityGateResult:
        """콘텐츠를 3단계로 검증한다."""
        issues: List[GateIssue] = []
        stage_results: Dict[str, Dict[str, Any]] = {}

        # 1) 규칙 기반 검증
        rules_passed, rules_payload, rules_issues = self._check_rules(
            title=title,
            content=content,
            seed_keywords=seed_keywords,
        )
        stage_results["rules"] = rules_payload
        issues.extend(rules_issues)

        # 2) RAG 근거 대조
        rag_passed, rag_payload, rag_issues = self._check_rag_alignment(
            content=content,
            topic_mode=topic_mode,
            rag_context=rag_context or [],
        )
        stage_results["rag_alignment"] = rag_payload
        issues.extend(rag_issues)

        # 3) 구조화 결과 생성
        passed = rules_passed and rag_passed
        if passed:
            return QualityGateResult(
                passed=True,
                gate="pass",
                score=92,
                error_code="",
                summary="품질 게이트 통과",
                stage_results=stage_results,
                issues=[],
            )

        score = max(20, 92 - len(issues) * 16)
        return QualityGateResult(
            passed=False,
            gate="retry",
            score=score,
            error_code="QUALITY_FAILED",
            summary="품질 게이트 미달",
            stage_results=stage_results,
            issues=issues,
        )

    def repair_content(
        self,
        *,
        content: str,
        issues: List[GateIssue],
        title: str,
        seed_keywords: List[str],
    ) -> str:
        """규칙 기반으로 자동 복구 가능한 항목만 보정한다."""
        repaired = content
        issue_codes = {item.code for item in issues}

        # 금칙어는 마스킹 처리로 급한 차단을 피한다.
        if any(code.endswith("_term") or code == "unsafe_adult_phrase" for code in issue_codes):
            for pattern, _code in self._compiled_patterns:
                repaired = pattern.sub("[민감표현 제거]", repaired)

        # 미래 연도 환각: 해당 연도를 "[연도 확인 필요]"로 교체한다.
        if "hallucination_future_year" in issue_codes:
            repaired = self._FUTURE_YEAR_PATTERN.sub("[연도 확인 필요]", repaired)

        # 길이 부족 시 보강 단락을 추가한다.
        if "content_too_short" in issue_codes:
            keyword_text = ", ".join(seed_keywords[:4]) or title
            supplement = (
                "\n\n## 추가 정리\n"
                f"{title} 주제에서 핵심은 {keyword_text}입니다. "
                "실행 순서와 체크리스트를 다시 점검하고, 실제 사례를 바탕으로 적용 계획을 세워보세요.\n"
            )
            repaired = f"{repaired.rstrip()}{supplement}"

        return repaired

    def _check_rules(
        self,
        *,
        title: str,
        content: str,
        seed_keywords: List[str],
    ) -> Tuple[bool, Dict[str, Any], List[GateIssue]]:
        issues: List[GateIssue] = []
        stripped = content.strip()
        content_length = len(stripped)

        if content_length < self.min_content_chars:
            issues.append(
                GateIssue(
                    stage="rules",
                    code="content_too_short",
                    message=f"본문 길이가 너무 짧습니다. (현재 {content_length}자)",
                    severity="high",
                )
            )

        lowered = stripped.lower()
        keyword_hits = 0
        for keyword in seed_keywords:
            token = str(keyword).strip().lower()
            if token and token in lowered:
                keyword_hits += 1
        if seed_keywords and keyword_hits == 0:
            issues.append(
                GateIssue(
                    stage="rules",
                    code="keyword_miss",
                    message="시드 키워드가 본문에 반영되지 않았습니다.",
                    severity="medium",
                )
            )

        title_token = title.strip().lower()
        if title_token and title_token not in lowered:
            issues.append(
                GateIssue(
                    stage="rules",
                    code="title_mismatch",
                    message="제목 핵심 문구가 본문에 부족합니다.",
                    severity="low",
                )
            )

        banned_matches: List[str] = []
        for pattern, code in self._compiled_patterns:
            match = pattern.search(stripped)
            if not match:
                continue
            banned_matches.append(code)
            issues.append(
                GateIssue(
                    stage="rules",
                    code=code,
                    message=f"금칙어 패턴 감지: {pattern.pattern}",
                    severity="high",
                )
            )

        # 환각 감지 1: 미래 연도(2027년 이후) 단독 등장
        future_years = self._FUTURE_YEAR_PATTERN.findall(stripped)
        if future_years:
            issues.append(
                GateIssue(
                    stage="rules",
                    code="hallucination_future_year",
                    message=f"미래 연도 표현 감지: {', '.join(set(future_years))}",
                    severity="medium",
                )
            )

        # 환각 감지 2: 단락 내 수치 과밀 (단락당 5개 초과)
        paragraphs = [p for p in stripped.split("\n\n") if p.strip()]
        dense_paragraphs = 0
        for para in paragraphs:
            if len(self._NUMERIC_FACT_PATTERN.findall(para)) > 5:
                dense_paragraphs += 1
        if dense_paragraphs >= 2:
            issues.append(
                GateIssue(
                    stage="rules",
                    code="hallucination_numeric_dense",
                    message=f"수치 과밀 단락 {dense_paragraphs}개 감지 (단락당 5개 초과)",
                    severity="medium",
                )
            )

        # 키워드 밀도 검사: 키워드 등장 단락 수 / 전체 단락 수 >= 0.15
        keyword_density_score = 0.0
        if seed_keywords and paragraphs:
            keyword_para_hits = 0
            for para in paragraphs:
                para_lower = para.lower()
                if any(str(kw).strip().lower() in para_lower for kw in seed_keywords if kw):
                    keyword_para_hits += 1
            keyword_density_score = keyword_para_hits / len(paragraphs)
            if keyword_density_score < self._MIN_KEYWORD_DENSITY and keyword_hits == 0:
                issues.append(
                    GateIssue(
                        stage="rules",
                        code="keyword_density_low",
                        message=f"키워드 밀도 부족: {keyword_density_score:.2f} (기준 {self._MIN_KEYWORD_DENSITY})",
                        severity="medium",
                    )
                )

        payload = {
            "passed": self._is_stage_passed(issues),
            "content_length": content_length,
            "min_content_chars": self.min_content_chars,
            "keyword_hits": keyword_hits,
            "keyword_total": len(seed_keywords),
            "banned_matches": banned_matches,
            "future_years_detected": future_years,
            "dense_paragraphs": dense_paragraphs,
            "keyword_density_score": round(keyword_density_score, 3),
        }
        return self._is_stage_passed(issues), payload, issues

    def _check_rag_alignment(
        self,
        *,
        content: str,
        topic_mode: str,
        rag_context: List[Dict[str, str]],
    ) -> Tuple[bool, Dict[str, Any], List[GateIssue]]:
        issues: List[GateIssue] = []
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        source_lines = [line for line in lines if self.SOURCE_LINE_PATTERN.match(line)]

        rag_required = bool(rag_context) or topic_mode in {"finance", "economy"}
        if rag_required and not source_lines:
            issues.append(
                GateIssue(
                    stage="rag_alignment",
                    code="rag_source_missing",
                    message="RAG 근거 출처(참고 자료) 표기가 없습니다.",
                    severity="high",
                )
            )

        expected_min_sources = 0
        if rag_context:
            expected_min_sources = min(3, len(rag_context))
            if len(source_lines) < expected_min_sources:
                issues.append(
                    GateIssue(
                        stage="rag_alignment",
                        code="rag_source_count_low",
                        message=f"RAG 출처 개수가 부족합니다. (현재 {len(source_lines)}개)",
                        severity="medium",
                    )
                )

        payload = {
            "passed": self._is_stage_passed(issues),
            "rag_required": rag_required,
            "source_lines": len(source_lines),
            "expected_min_sources": expected_min_sources,
        }
        return self._is_stage_passed(issues), payload, issues

    def _is_stage_passed(self, issues: List[GateIssue]) -> bool:
        """low 수준 이슈는 경고로만 처리한다."""
        for issue in issues:
            if issue.severity in {"high", "medium"}:
                return False
        return True
