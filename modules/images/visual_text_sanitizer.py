"""표/카드 이미지에 들어가는 짧은 문구 정화 유틸리티."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence


_COMMON_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("테그", "태그"),
    ("수익율", "수익률"),
    ("상승율", "상승률"),
    ("하락율", "하락률"),
    ("변동율", "변동률"),
    ("전일대비", "전일 대비"),
    ("금리인상", "금리 인상"),
    ("금리인하", "금리 인하"),
    ("데이터 센터", "데이터센터"),
    ("나스닥선물", "나스닥 선물"),
)

_PROMPT_LABEL_RE = re.compile(r"\[(?:출력|본문|수정본|리라이트본|요약|카드)\]")
_MARKDOWN_TOKEN_RE = re.compile(r"[*_`>#]+")


@dataclass(frozen=True)
class VisualTextValidation:
    """시각자료 텍스트 검수 결과."""

    passed: bool
    issues: List[str] = field(default_factory=list)


def sanitize_visual_text(text: str, *, max_chars: int = 80) -> str:
    """카드/표에 들어갈 짧은 텍스트를 정돈한다."""

    cleaned = str(text or "")
    cleaned = _PROMPT_LABEL_RE.sub("", cleaned)
    cleaned = _MARKDOWN_TOKEN_RE.sub("", cleaned)
    cleaned = cleaned.replace("&nbsp;", " ").replace("\u200b", "")
    for before, after in _COMMON_REPLACEMENTS:
        cleaned = cleaned.replace(before, after)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s+([,.!?%])", r"\1", cleaned)
    cleaned = re.sub(r"([가-힣A-Za-z])([+\-]\d)", r"\1 \2", cleaned)
    if max_chars > 0 and len(cleaned) > max_chars:
        cleaned = cleaned[: max(1, max_chars - 1)].rstrip() + "…"
    return cleaned


def sanitize_visual_lines(lines: Iterable[str], *, max_chars: int = 80) -> list[str]:
    """여러 줄 텍스트를 중복 제거하며 정돈한다."""

    output: list[str] = []
    seen: set[str] = set()
    for line in lines:
        cleaned = sanitize_visual_text(str(line), max_chars=max_chars)
        key = re.sub(r"\s+", "", cleaned)
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
    return output


def normalize_table_rows(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    max_cell_chars: int = 56,
) -> tuple[list[str], list[list[str]], VisualTextValidation]:
    """표 셀 수와 텍스트를 이미지 렌더링 전에 정규화한다."""

    clean_headers = [
        sanitize_visual_text(str(header), max_chars=max_cell_chars)
        for header in headers
    ]
    column_count = max(1, len(clean_headers))
    issues: list[str] = []
    clean_rows: list[list[str]] = []

    for row_index, row in enumerate(rows):
        cells = [sanitize_visual_text(str(cell), max_chars=max_cell_chars) for cell in row]
        if len(cells) < column_count:
            issues.append(f"row_{row_index}_padded")
            cells.extend([""] * (column_count - len(cells)))
        elif len(cells) > column_count:
            issues.append(f"row_{row_index}_trimmed")
            overflow = " ".join(cells[column_count - 1 :]).strip()
            cells = cells[: column_count - 1] + [
                sanitize_visual_text(overflow, max_chars=max_cell_chars)
            ]
        clean_rows.append(cells)

    validation = VisualTextValidation(
        passed=not any(issue.endswith("_trimmed") for issue in issues),
        issues=issues,
    )
    return clean_headers, clean_rows, validation


def validate_visual_texts(values: Iterable[str]) -> VisualTextValidation:
    """마크다운 잔재와 과도한 숫자 밀도를 검사한다."""

    issues: list[str] = []
    for index, value in enumerate(values):
        text = str(value or "")
        if re.search(r"\|[-: ]+\||```|^\s{0,3}#{1,6}\s+", text, flags=re.MULTILINE):
            issues.append(f"text_{index}_markdown_residue")
        if len(re.findall(r"\d", text)) >= 10:
            issues.append(f"text_{index}_numeric_dense")
    return VisualTextValidation(passed=not issues, issues=issues)
