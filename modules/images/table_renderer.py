"""마크다운 표를 Pillow PNG 이미지로 렌더링.

마크다운 `| col | col |` 표를 감지하고 Pillow로 깔끔한 PNG 이미지를 생성.
이미지는 content 이미지와 동일한 디렉토리에 저장되며,
본문에는 [TABLE_N] 마커로 대체됨.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from .visual_text_sanitizer import VisualTextValidation, normalize_table_rows, sanitize_visual_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 폰트 로드
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = [
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",       # macOS (Korean)
    "/System/Library/Fonts/SupplementalFonts/AppleSDGothicNeo.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",  # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJKkr-Regular.otf",
]


def _load_font(size: int = 16):
    """한국어 지원 폰트를 로드한다. 실패 시 Pillow 기본 폰트 반환."""
    try:
        from PIL import ImageFont
    except ImportError:
        return None

    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:
                continue

    # 최후의 수단: Pillow 내장 기본 폰트
    try:
        return ImageFont.load_default()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 마크다운 표 파싱
# ---------------------------------------------------------------------------

_TABLE_BLOCK_RE = re.compile(
    r"((?:^\|.+\|\s*\n)+)",
    re.MULTILINE,
)


def _parse_markdown_table(table_text: str) -> Tuple[List[str], List[List[str]]]:
    """마크다운 표를 (헤더, 행 리스트)로 파싱한다."""

    headers, rows, _validation = _parse_markdown_table_with_validation(table_text)
    return headers, rows


def _parse_markdown_table_with_validation(table_text: str) -> Tuple[List[str], List[List[str]], VisualTextValidation]:
    """마크다운 표를 (헤더, 행 리스트)로 파싱한다.

    구분선(|---|---|) 행은 제거한다.

    Returns:
        (headers, rows, validation) — 첫 줄이 헤더, 나머지가 데이터 행.
        헤더나 데이터가 없으면 ([], [], validation) 반환.
    """
    lines = [l.rstrip() for l in table_text.strip().splitlines()]
    rows: List[List[str]] = []

    for line in lines:
        # 구분선 제거 (|---|---| 패턴)
        if re.match(r"^\|[-| :]+\|$", line):
            continue
        # | 로 시작하고 끝나는 행만 처리
        if not line.startswith("|"):
            continue

        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)

    if not rows:
        return [], [], VisualTextValidation(passed=False, issues=["table_empty"])

    headers = rows[0]
    data_rows = rows[1:]
    headers, data_rows, validation = normalize_table_rows(headers, data_rows)
    return headers, data_rows, validation


# ---------------------------------------------------------------------------
# PNG 렌더링
# ---------------------------------------------------------------------------

# 디자인 상수
_HEADER_BG = (44, 120, 190)       # 파란색 헤더
_HEADER_FG = (255, 255, 255)      # 흰색 텍스트
_ROW_BG_ODD = (245, 248, 252)     # 홀수 행 배경
_ROW_BG_EVEN = (255, 255, 255)    # 짝수 행 배경
_BORDER_COLOR = (180, 200, 220)   # 테두리 색
_TEXT_COLOR = (30, 30, 30)        # 데이터 텍스트
_PADDING = 12                      # 셀 패딩 (px)
_MIN_COL_WIDTH = 80               # 최소 컬럼 폭 (px)
_MAX_COL_WIDTH = 320              # 최대 컬럼 폭 (px)
_ROW_HEIGHT_RATIO = 2.6           # 행 높이 = 폰트 크기 * 이 배수
_FONT_SIZE = 15
_HEADER_FONT_SIZE = 15


def _render_table_to_png(
    headers: List[str],
    rows: List[List[str]],
    output_dir: Path,
    *,
    style: str = "default",
) -> Optional[str]:
    """표 데이터를 PNG로 렌더링하고 파일 경로를 반환한다."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow not installed; skipping table rendering")
        return None

    if not headers:
        return None

    palette = _table_palette(style)
    font = _load_font(_FONT_SIZE)
    header_font = _load_font(_HEADER_FONT_SIZE)

    num_cols = max(len(headers), max((len(r) for r in rows), default=0))

    # ── 컬럼 폭 계산 (텍스트 너비 기반) ──────────────────────────────────
    def text_width(text: str, fnt) -> int:
        """텍스트 폭 (픽셀) 추정. fnt=None이면 글자 수 기반 fallback."""
        if fnt is None:
            return len(text) * 9
        try:
            # Pillow ≥ 10.x
            dummy_img = Image.new("RGB", (1, 1))
            dummy_draw = ImageDraw.Draw(dummy_img)
            bbox = dummy_draw.textbbox((0, 0), text, font=fnt)
            return bbox[2] - bbox[0]
        except AttributeError:
            # Pillow 9.x 이하
            return fnt.getlength(text)  # type: ignore[union-attr]
        except Exception:
            return len(text) * 9

    col_widths = []
    for col_idx in range(num_cols):
        header_txt = headers[col_idx] if col_idx < len(headers) else ""
        max_w = text_width(header_txt, header_font) + _PADDING * 2
        for row in rows:
            cell_txt = row[col_idx] if col_idx < len(row) else ""
            w = text_width(cell_txt, font) + _PADDING * 2
            max_w = max(max_w, w)
        col_widths.append(min(_MAX_COL_WIDTH, max(max_w, _MIN_COL_WIDTH)))

    row_height_ratio = 3.8 if style == "market_note" else _ROW_HEIGHT_RATIO
    row_height = int(_FONT_SIZE * row_height_ratio)
    header_height = int(_HEADER_FONT_SIZE * (3.2 if style == "market_note" else _ROW_HEIGHT_RATIO))

    total_width = sum(col_widths) + 1  # +1 for right border
    total_height = header_height + len(rows) * row_height + 1

    img = Image.new("RGB", (total_width, total_height), palette["canvas"])
    draw = ImageDraw.Draw(img)

    # ── 헤더 행 렌더링 ──────────────────────────────────────────────────────
    x = 0
    for col_idx, width in enumerate(col_widths):
        draw.rectangle([x, 0, x + width, header_height], fill=palette["header_bg"])
        header_txt = sanitize_visual_text(headers[col_idx] if col_idx < len(headers) else "", max_chars=42)
        _draw_cell_text(draw, header_txt, x, 0, width, header_height, header_font, palette["header_fg"])
        # 오른쪽 경계
        draw.line([x + width, 0, x + width, header_height], fill=palette["header_line"], width=1)
        x += width

    # 헤더 아래 경계
    draw.line([0, header_height, total_width, header_height], fill=palette["border"], width=1)

    # ── 데이터 행 렌더링 ─────────────────────────────────────────────────────
    for row_idx, row in enumerate(rows):
        y = header_height + row_idx * row_height
        bg = palette["row_odd"] if row_idx % 2 == 0 else palette["row_even"]
        draw.rectangle([0, y, total_width, y + row_height], fill=bg)
        accent = _row_accent_color(row, palette) if style == "market_note" else None
        if accent:
            draw.rectangle([0, y, 7, y + row_height], fill=accent)

        x = 0
        for col_idx, width in enumerate(col_widths):
            cell_txt = sanitize_visual_text(row[col_idx] if col_idx < len(row) else "", max_chars=58)
            _draw_cell_text(draw, cell_txt, x, y, width, row_height, font, palette["text"])
            draw.line([x + width, y, x + width, y + row_height], fill=palette["border"], width=1)
            x += width

        # 행 아래 경계
        draw.line([0, y + row_height, total_width, y + row_height], fill=palette["border"], width=1)

    # 외곽 테두리
    draw.rectangle([0, 0, total_width - 1, total_height - 1], outline=palette["border"], width=1)

    # ── 파일 저장 ────────────────────────────────────────────────────────────
    output_dir.mkdir(parents=True, exist_ok=True)
    file_path = output_dir / f"table_{uuid.uuid4().hex[:8]}.png"
    img.save(str(file_path), "PNG")

    logger.info("Table rendered to PNG", extra={"path": str(file_path), "rows": len(rows)})
    return str(file_path)


def _draw_cell_text(
    draw,
    text: str,
    x: int,
    y: int,
    width: int,
    height: int,
    font,
    color: Tuple[int, int, int],
) -> None:
    """셀 내 텍스트를 수직/수평 중앙 정렬로 그린다."""
    text = sanitize_visual_text(str(text), max_chars=120)
    max_width = max(20, width - _PADDING * 2)
    lines = _wrap_cell_text(draw, text, font, max_width=max_width, max_lines=2)

    try:
        from PIL import Image, ImageDraw
        dummy_img = Image.new("RGB", (1, 1))
        dummy_draw = ImageDraw.Draw(dummy_img)
        line_sizes = []
        for line in lines:
            if font is not None:
                bbox = dummy_draw.textbbox((0, 0), line, font=font)
                line_sizes.append((bbox[2] - bbox[0], bbox[3] - bbox[1]))
            else:
                line_sizes.append((len(line) * 9, 16))
    except Exception:
        line_sizes = [(len(line) * 9, 16) for line in lines]

    total_text_height = sum(size[1] for size in line_sizes) + max(0, len(lines) - 1) * 4
    ty = y + (height - total_text_height) // 2

    for line, (tw, th) in zip(lines, line_sizes):
        tx = x + (width - tw) // 2
        draw.text((tx, ty), line, font=font, fill=color)
        ty += th + 4


def _wrap_cell_text(draw, text: str, font, *, max_width: int, max_lines: int) -> List[str]:
    """셀 텍스트를 픽셀 폭 기준으로 1~2줄 줄바꿈한다."""

    if not text:
        return [""]

    def width_of(value: str) -> int:
        if font is None:
            return len(value) * 9
        try:
            bbox = draw.textbbox((0, 0), value, font=font)
            return bbox[2] - bbox[0]
        except Exception:
            return len(value) * 9

    lines: List[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and width_of(candidate) > max_width:
            lines.append(current)
            current = char
            if len(lines) >= max_lines:
                break
        else:
            current = candidate
    if len(lines) < max_lines and current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if lines and width_of(lines[-1]) > max_width:
        while lines[-1] and width_of(lines[-1] + "…") > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines or [""]


def _table_palette(style: str) -> dict[str, Tuple[int, int, int]]:
    if str(style or "").strip().lower() == "market_note":
        return {
            "canvas": (250, 249, 244),
            "header_bg": (24, 91, 100),
            "header_fg": (255, 255, 255),
            "header_line": (220, 237, 232),
            "row_odd": (245, 250, 247),
            "row_even": (255, 255, 252),
            "border": (206, 220, 214),
            "text": (36, 43, 43),
            "positive": (51, 147, 132),
            "negative": (196, 92, 86),
            "neutral": (195, 151, 73),
        }
    return {
        "canvas": (255, 255, 255),
        "header_bg": _HEADER_BG,
        "header_fg": _HEADER_FG,
        "header_line": _HEADER_FG,
        "row_odd": _ROW_BG_ODD,
        "row_even": _ROW_BG_EVEN,
        "border": _BORDER_COLOR,
        "text": _TEXT_COLOR,
        "positive": (42, 120, 135),
        "negative": (177, 82, 78),
        "neutral": (117, 91, 58),
    }


def _row_accent_color(row: List[str], palette: dict[str, Tuple[int, int, int]]) -> Optional[Tuple[int, int, int]]:
    text = " ".join(str(cell) for cell in row)
    if re.search(r"(\+\s?\d|상승|강세|개선|증가|회복)", text):
        return palette["positive"]
    if re.search(r"(-\s?\d|하락|약세|부담|경계|감소|둔화)", text):
        return palette["negative"]
    if re.search(r"(중립|관망|확인|체크)", text):
        return palette["neutral"]
    return None


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------

def extract_and_render_tables(
    content: str,
    output_dir: str = "data/images",
    style: str = "default",
) -> Tuple[str, List[str]]:
    """마크다운 본문에서 표를 추출해 PNG로 렌더링하고 [TABLE_N] 마커로 대체한다.

    Args:
        content: 마크다운 본문 텍스트
        output_dir: PNG 저장 디렉토리

    Returns:
        (modified_content, table_paths)
        - modified_content: 표가 [TABLE_N] 마커로 대체된 본문
        - table_paths: 렌더링된 PNG 파일 경로 리스트 (순서 = TABLE_0, TABLE_1, ...)
    """
    modified, table_paths, _validation = extract_and_render_tables_with_validation(
        content=content,
        output_dir=output_dir,
        style=style,
    )
    return modified, table_paths


def extract_and_render_tables_with_validation(
    content: str,
    output_dir: str = "data/images",
    style: str = "default",
) -> Tuple[str, List[str], VisualTextValidation]:
    """마크다운 표를 PNG로 렌더링하고 텍스트 검수 결과도 함께 반환한다."""

    out_path = Path(output_dir)
    table_paths: List[str] = []
    validation_issues: list[str] = []
    modified = content

    # 표 블록을 뒤에서부터 처리해야 위치 인덱스가 흐트러지지 않음
    # → re.finditer 결과를 역순으로
    matches = list(_TABLE_BLOCK_RE.finditer(content))

    for match in reversed(matches):
        table_text = match.group(1)
        table_number = len(table_paths)
        headers, rows, validation = _parse_markdown_table_with_validation(table_text)
        validation_issues.extend(
            f"table_{table_number}_{issue}" for issue in validation.issues
        )

        if not headers:
            continue

        png_path = _render_table_to_png(headers, rows, out_path, style=style)
        if png_path is None:
            # Pillow 없거나 렌더링 실패 → 표 그대로 유지
            validation_issues.append(f"table_{table_number}_render_failed")
            continue

        # 인덱스는 정방향 번호 (나중에 역순 처리이므로 임시로 uuid 사용,
        # 최종 인덱스 부여는 아래서 처리)
        table_paths.append(png_path)

        marker = f"[TABLE_{len(table_paths) - 1}]"
        start, end = match.start(), match.end()
        modified = modified[:start] + f"\n{marker}\n" + modified[end:]

    # 역순 처리로 TABLE_ 인덱스가 역전됨 → 역전 교정
    # 예: 표 2개라면 → 먼저 처리된(아래 표) = TABLE_0, 나중(위 표) = TABLE_1
    # 실제 본문 순서에 맞게 [TABLE_0], [TABLE_1] 재배정
    num_tables = len(table_paths)
    if num_tables > 1:
        # 역순 처리 → table_paths[0] = 마지막 표, table_paths[-1] = 첫 번째 표
        table_paths = list(reversed(table_paths))
        # 마커도 재배정: [TABLE_0]~[TABLE_N-1] → 올바른 순서로
        for old_idx in range(num_tables - 1, -1, -1):
            new_idx = num_tables - 1 - old_idx
            if old_idx != new_idx:
                modified = modified.replace(
                    f"[TABLE_{old_idx}]",
                    f"[TABLE_TEMP_{new_idx}]",
                )
        modified = modified.replace("[TABLE_TEMP_", "[TABLE_")

    logger.info(
        "Tables extracted and rendered",
        extra={"count": num_tables, "output_dir": str(out_path)},
    )
    return modified, table_paths, VisualTextValidation(
        passed=not validation_issues,
        issues=validation_issues,
    )
