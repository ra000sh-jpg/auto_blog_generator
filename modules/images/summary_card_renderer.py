"""블로그 본문 요약을 뉴스카드형 PNG 이미지로 렌더링한다."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .visual_text_sanitizer import sanitize_visual_lines, sanitize_visual_text

logger = logging.getLogger(__name__)

_FONT_CANDIDATES = [
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "/System/Library/Fonts/Supplemental/AppleSDGothicNeo.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKkr-Regular.otf",
]

_EXCLUDED_HEADINGS = {
    "참고 자료",
    "참고자료",
    "출처",
    "마무리",
    "정리",
}


@dataclass(frozen=True)
class SummaryCardResult:
    """요약 카드 렌더링 결과."""

    path: str
    title: str
    bullets: List[str]


def render_summary_card(
    *,
    title: str,
    content: str,
    output_dir: str = "data/images",
    max_bullets: int = 4,
    width: int = 1080,
    height: int = 1350,
    style: str = "default",
    bullets_override: Optional[Sequence[str]] = None,
) -> Optional[SummaryCardResult]:
    """본문 핵심을 요약한 PNG 카드를 생성한다.

    LLM 호출 없이 본문 제목/소제목/문장을 추출해 사용한다. 실패하면 None을
    반환하여 기존 발행 흐름을 방해하지 않는다.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow not installed; summary card rendering skipped")
        return None

    normalized_title = sanitize_visual_text(_clean_text(title), max_chars=80) or "오늘의 공부 노트"
    raw_bullets = (
        list(bullets_override)
        if bullets_override
        else extract_summary_bullets(content=content, title=normalized_title, max_bullets=max_bullets)
    )
    bullets = sanitize_visual_lines(raw_bullets, max_chars=72)[: max(2, min(5, int(max_bullets or 4)))]
    if not bullets:
        return None

    digest = hashlib.sha1(f"{normalized_title}\n{content}".encode("utf-8")).hexdigest()[:10]
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"summary_card_{digest}.png"

    title_font = _load_font(54)
    subtitle_font = _load_font(30)
    label_font = _load_font(28)
    body_font = _load_font(38)
    number_font = _load_font(30)
    footer_font = _load_font(26)

    palette = _summary_palette(style)
    image = Image.new("RGB", (width, height), palette["canvas"])
    draw = ImageDraw.Draw(image)

    # 배경과 포인트 라인을 분리해 카드가 과하게 단색으로 보이지 않게 한다.
    _draw_round_rect(draw, (54, 54, width - 54, height - 54), radius=42, fill=palette["card"], outline=palette["border"], width=2)
    draw.rectangle((54, 54, width - 54, 80), fill=palette["header"])
    draw.rectangle((54, height - 104, width - 54, height - 54), fill=palette["footer_bg"])

    label = "함께 공부하는 요약 노트"
    if style == "market_note":
        label = "국장 전 시장노트"
    draw.text((92, 118), label, font=label_font, fill=palette["header"])
    draw.text((92, 158), "읽기 전에 먼저 잡아보는 기준", font=subtitle_font, fill=palette["muted"])

    title_lines = _wrap_text(draw, normalized_title, title_font, max_width=width - 184, max_lines=3)
    y = 238
    for line in title_lines:
        draw.text((92, y), line, font=title_font, fill=palette["title"])
        y += _line_height(draw, line, title_font) + 10

    y += 20
    draw.line((92, y, width - 92, y), fill=palette["rule"], width=3)
    y += 46

    bullet_gap = 42
    for idx, bullet in enumerate(bullets, start=1):
        circle_x = 112
        circle_y = y + 8
        accent = palette["accent"] if idx % 2 else palette["accent_alt"]
        draw.ellipse((circle_x, circle_y, circle_x + 54, circle_y + 54), fill=accent)
        num = str(idx)
        num_w = _text_width(draw, num, number_font)
        draw.text((circle_x + (54 - num_w) / 2, circle_y + 8), num, font=number_font, fill=(255, 255, 255))

        bullet_lines = _wrap_text(draw, bullet, body_font, max_width=width - 240, max_lines=3)
        text_y = y
        for line in bullet_lines:
            draw.text((190, text_y), line, font=body_font, fill=palette["body"])
            text_y += _line_height(draw, line, body_font) + 8
        y = max(circle_y + 70, text_y) + bullet_gap

        if y > height - 210:
            break

    footer = "정보보다 기준, 예측보다 자기수정"
    footer_w = _text_width(draw, footer, footer_font)
    draw.text(((width - footer_w) / 2, height - 91), footer, font=footer_font, fill=palette["muted"])

    image.save(str(output_path), "PNG")
    logger.info(
        "Summary card rendered",
        extra={"path": str(output_path), "bullet_count": len(bullets)},
    )
    return SummaryCardResult(path=str(output_path), title=normalized_title, bullets=bullets)


def extract_summary_bullets(*, content: str, title: str = "", max_bullets: int = 4) -> List[str]:
    """본문에서 카드에 넣을 요약 문장을 추출한다."""
    limit = max(2, min(5, int(max_bullets or 4)))
    raw_text = str(content or "")
    candidates: List[str] = []

    for heading in re.findall(r"(?m)^\s{0,3}#{2,3}\s+(.+?)\s*$", raw_text):
        cleaned = _clean_text(heading)
        if _is_good_candidate(cleaned, title=title, min_len=8, max_len=52):
            candidates.append(cleaned)

    for heading in re.findall(r"(?m)^\s*[■◆▶▷]\s+(.+?)\s*$", raw_text):
        cleaned = _clean_text(heading)
        if _is_good_candidate(cleaned, title=title, min_len=8, max_len=52):
            candidates.append(cleaned)

    plain = _markdown_to_plain_text(raw_text)
    for sentence in _split_korean_sentences(plain):
        cleaned = _clean_text(sentence)
        if _is_good_candidate(cleaned, title=title, min_len=16, max_len=72):
            candidates.append(cleaned)

    unique: List[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = re.sub(r"\s+", "", candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
        if len(unique) >= limit:
            break

    return unique[:limit]


def _load_font(size: int):
    """한국어 출력 가능한 폰트를 로드한다."""
    from PIL import ImageFont

    for candidate in _FONT_CANDIDATES:
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size)
        except Exception:
            continue
    return ImageFont.load_default()


def _markdown_to_plain_text(content: str) -> str:
    """요약 추출용으로 마크다운 문법을 제거한다."""
    text = str(content or "")
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[(?:IMG|TABLE)_\d+\]", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(?m)^\s*\|[\s:|\-]+\|\s*$", " ", text)
    text = re.sub(r"(?m)^\s*\|(.+)\|\s*$", lambda match: " ".join(part.strip() for part in match.group(1).split("|")), text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*[■◆▶▷]\s+", "", text)
    text = re.sub(r"[*_`>~-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _split_korean_sentences(text: str) -> List[str]:
    """한국어 문장을 간단히 분리한다."""
    parts = re.split(r"(?<=[.!?。])\s+|(?<=요)\s+|(?<=다)\s+|(?<=죠)\s+", str(text or ""))
    return [part.strip() for part in parts if part.strip()]


def _clean_text(text: str) -> str:
    """카드에 들어갈 텍스트를 정돈한다."""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    cleaned = re.sub(r"^[•\-–\d.()\s]+", "", cleaned).strip()
    cleaned = cleaned.replace("[출력]", "").replace("참고 자료:", "").strip()
    return sanitize_visual_text(cleaned, max_chars=100)


def _is_good_candidate(text: str, *, title: str = "", min_len: int = 10, max_len: int = 80) -> bool:
    """요약 카드 후보 문장으로 적절한지 판단한다."""
    if not text:
        return False
    compact = re.sub(r"\s+", "", text)
    if len(compact) < min_len or len(compact) > max_len:
        return False
    if compact == re.sub(r"\s+", "", title):
        return False
    if text.strip() in _EXCLUDED_HEADINGS:
        return False
    if text.startswith(("http://", "https://")):
        return False
    if len(re.findall(r"\d", text)) >= 8:
        return False
    return True


def _summary_palette(style: str) -> dict[str, Tuple[int, int, int]]:
    """요약 카드 스타일별 팔레트."""

    if str(style or "").strip().lower() == "market_note":
        return {
            "canvas": (250, 249, 244),
            "card": (255, 255, 252),
            "border": (210, 222, 216),
            "header": (24, 91, 100),
            "footer_bg": (237, 246, 241),
            "title": (31, 39, 39),
            "body": (37, 45, 45),
            "muted": (91, 104, 101),
            "rule": (215, 226, 220),
            "accent": (51, 147, 132),
            "accent_alt": (205, 151, 78),
        }
    return {
        "canvas": (248, 249, 246),
        "card": (255, 255, 255),
        "border": (218, 224, 219),
        "header": (33, 94, 122),
        "footer_bg": (238, 244, 241),
        "title": (26, 31, 35),
        "body": (38, 43, 45),
        "muted": (98, 105, 105),
        "rule": (222, 228, 225),
        "accent": (42, 120, 135),
        "accent_alt": (117, 91, 58),
    }


def _wrap_text(draw, text: str, font, *, max_width: int, max_lines: int) -> List[str]:
    """픽셀 폭 기준으로 텍스트를 줄바꿈한다."""
    chars = list(str(text or "").strip())
    if not chars:
        return []

    lines: List[str] = []
    current = ""
    for char in chars:
        candidate = current + char
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current.strip())
            current = char
            if len(lines) >= max_lines:
                break
        else:
            current = candidate

    if current and len(lines) < max_lines:
        lines.append(current.strip())

    if len(lines) == max_lines and len("".join(lines)) < len(str(text)):
        lines[-1] = _ellipsize(draw, lines[-1], font, max_width=max_width)

    return [line for line in lines if line]


def _ellipsize(draw, text: str, font, *, max_width: int) -> str:
    """마지막 줄을 폭에 맞게 말줄임한다."""
    value = str(text or "").rstrip()
    while value and _text_width(draw, value + "...", font) > max_width:
        value = value[:-1]
    return f"{value}..." if value else "..."


def _text_width(draw, text: str, font) -> int:
    """텍스트 픽셀 폭을 계산한다."""
    try:
        bbox = draw.textbbox((0, 0), str(text), font=font)
        return int(bbox[2] - bbox[0])
    except Exception:
        return len(str(text)) * 18


def _line_height(draw, text: str, font) -> int:
    """텍스트 한 줄 높이를 계산한다."""
    try:
        bbox = draw.textbbox((0, 0), str(text or "가"), font=font)
        return int(bbox[3] - bbox[1])
    except Exception:
        return 42


def _draw_round_rect(draw, box: Sequence[int], *, radius: int, fill: Tuple[int, int, int], outline: Tuple[int, int, int], width: int) -> None:
    """Pillow 버전 차이를 흡수해 둥근 사각형을 그린다."""
    try:
        draw.rounded_rectangle(tuple(box), radius=radius, fill=fill, outline=outline, width=width)
    except Exception:
        draw.rectangle(tuple(box), fill=fill, outline=outline, width=width)
