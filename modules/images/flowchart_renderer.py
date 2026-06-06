"""블로그용 간단 흐름도 PNG 렌더러."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

from .visual_text_sanitizer import sanitize_visual_lines, sanitize_visual_text

logger = logging.getLogger(__name__)

_FONT_CANDIDATES = [
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "/System/Library/Fonts/Supplemental/AppleSDGothicNeo.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKkr-Regular.otf",
]


@dataclass(frozen=True)
class FlowchartResult:
    """흐름도 렌더링 결과."""

    path: str
    title: str
    node_count: int


def render_flowchart(
    *,
    title: str,
    nodes: Sequence[str],
    output_dir: str = "data/images",
    width: int = 1080,
    height: int = 1350,
    style: str = "default",
) -> Optional[FlowchartResult]:
    """단계형 흐름도를 PNG로 렌더링한다."""

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow not installed; flowchart rendering skipped")
        return None

    clean_title = sanitize_visual_text(title, max_chars=70) or "판단 흐름"
    clean_nodes = sanitize_visual_lines(nodes, max_chars=64)[:5]
    if len(clean_nodes) < 2:
        return None

    palette = _flowchart_palette(style)
    digest = hashlib.sha1(
        f"{clean_title}\n{'|'.join(clean_nodes)}".encode("utf-8")
    ).hexdigest()[:10]
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"flowchart_{digest}.png"

    image = Image.new("RGB", (width, height), palette["canvas"])
    draw = ImageDraw.Draw(image)

    title_font = _load_font(54)
    subtitle_font = _load_font(30)
    node_font = _load_font(36)
    number_font = _load_font(30)
    footer_font = _load_font(25)

    _draw_round_rect(
        draw,
        (54, 54, width - 54, height - 54),
        radius=42,
        fill=palette["card"],
        outline=palette["border"],
        width=2,
    )
    draw.rectangle((54, 54, width - 54, 82), fill=palette["header"])

    draw.text((92, 122), "한눈에 보는 판단 흐름", font=subtitle_font, fill=palette["header"])
    y = 178
    for line in _wrap_text(draw, clean_title, title_font, max_width=width - 184, max_lines=2):
        draw.text((92, y), line, font=title_font, fill=palette["title"])
        y += _line_height(draw, line, title_font) + 12

    y += 30
    box_left = 118
    box_right = width - 118
    box_width = box_right - box_left
    available_height = height - y - 160
    node_height = max(128, min(176, int(available_height / max(1, len(clean_nodes)) - 18)))
    gap = 28

    for index, node in enumerate(clean_nodes, start=1):
        top = y + (index - 1) * (node_height + gap)
        bottom = top + node_height
        accent = palette["accent"] if index % 2 else palette["accent_alt"]
        _draw_round_rect(
            draw,
            (box_left, top, box_right, bottom),
            radius=28,
            fill=palette["node"],
            outline=palette["node_border"],
            width=2,
        )
        draw.ellipse((box_left + 28, top + 34, box_left + 88, top + 94), fill=accent)
        number = str(index)
        number_w = _text_width(draw, number, number_font)
        draw.text(
            (box_left + 28 + (60 - number_w) / 2, top + 45),
            number,
            font=number_font,
            fill=(255, 255, 255),
        )

        lines = _wrap_text(draw, node, node_font, max_width=box_width - 150, max_lines=2)
        text_y = top + (node_height - (len(lines) * 44 + max(0, len(lines) - 1) * 6)) / 2
        for line in lines:
            draw.text((box_left + 116, text_y), line, font=node_font, fill=palette["body"])
            text_y += _line_height(draw, line, node_font) + 6

        if index < len(clean_nodes):
            arrow_x = width // 2
            arrow_top = bottom + 5
            arrow_bottom = top + node_height + gap - 7
            draw.line((arrow_x, arrow_top, arrow_x, arrow_bottom), fill=palette["arrow"], width=5)
            draw.polygon(
                [
                    (arrow_x - 16, arrow_bottom - 18),
                    (arrow_x + 16, arrow_bottom - 18),
                    (arrow_x, arrow_bottom + 8),
                ],
                fill=palette["arrow"],
            )

    footer = "복잡한 선택은 순서로 줄여서 봅니다."
    footer_w = _text_width(draw, footer, footer_font)
    draw.text(((width - footer_w) / 2, height - 92), footer, font=footer_font, fill=palette["muted"])

    image.save(str(output_path), "PNG")
    logger.info(
        "Flowchart rendered",
        extra={"path": str(output_path), "node_count": len(clean_nodes)},
    )
    return FlowchartResult(path=str(output_path), title=clean_title, node_count=len(clean_nodes))


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


def _flowchart_palette(style: str) -> dict[str, Tuple[int, int, int]]:
    """토픽 스타일별 팔레트를 반환한다."""
    if str(style or "").strip().lower() == "market_note":
        return {
            "canvas": (250, 249, 244),
            "card": (255, 255, 252),
            "border": (210, 222, 216),
            "header": (24, 91, 100),
            "title": (31, 39, 39),
            "body": (37, 45, 45),
            "muted": (91, 104, 101),
            "node": (247, 251, 248),
            "node_border": (213, 226, 219),
            "accent": (51, 147, 132),
            "accent_alt": (205, 151, 78),
            "arrow": (120, 145, 137),
        }
    return {
        "canvas": (247, 249, 248),
        "card": (255, 255, 255),
        "border": (218, 224, 222),
        "header": (32, 92, 121),
        "title": (30, 38, 42),
        "body": (42, 49, 52),
        "muted": (94, 104, 106),
        "node": (246, 250, 252),
        "node_border": (211, 224, 230),
        "accent": (45, 132, 169),
        "accent_alt": (91, 126, 94),
        "arrow": (116, 142, 151),
    }


def _draw_round_rect(draw, box, radius: int, fill, outline, width: int = 1) -> None:
    """Pillow 버전에 맞춰 라운드 사각형을 그린다."""
    try:
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)
    except AttributeError:
        draw.rectangle(box, fill=fill, outline=outline, width=width)


def _text_width(draw, text: str, font) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    except Exception:
        return len(str(text)) * 18


def _line_height(draw, text: str, font) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return max(32, bbox[3] - bbox[1])
    except Exception:
        return 40


def _wrap_text(draw, text: str, font, *, max_width: int, max_lines: int) -> list[str]:
    """픽셀 폭 기준으로 텍스트를 줄바꿈한다."""
    words = str(text or "").split()
    if not words:
        return [""]

    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if _text_width(draw, candidate, font) <= max_width or not current:
            current = candidate
            continue
        lines.append(current)
        current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if lines and _text_width(draw, lines[-1], font) > max_width:
        while lines[-1] and _text_width(draw, f"{lines[-1]}…", font) > max_width:
            lines[-1] = lines[-1][:-1].rstrip()
        lines[-1] = f"{lines[-1]}…"
    return lines or [""]
