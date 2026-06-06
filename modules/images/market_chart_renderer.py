"""시장 데이터 스냅샷을 블로그용 PNG 시각자료로 렌더링한다."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_FONT_CANDIDATES = [
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "/System/Library/Fonts/Supplemental/AppleSDGothicNeo.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKkr-Regular.otf",
]


@dataclass(frozen=True)
class MarketChartResult:
    """시장 시각자료 렌더링 결과."""

    path: str
    title: str
    mode: str
    point_count: int


def render_market_chart(
    *,
    market_snapshot: Mapping[str, Any],
    title: str = "",
    output_dir: str = "data/images",
    width: int = 1200,
    height: int = 760,
) -> Optional[MarketChartResult]:
    """시장 스냅샷을 변동률 그래프 또는 지표 보드 PNG로 만든다."""

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("Pillow not installed; market chart rendering skipped")
        return None

    points = extract_market_chart_points(market_snapshot)
    if len(points) < 2:
        return None

    change_points = [point for point in points if isinstance(point.get("change_percent"), (int, float))]
    if len(change_points) >= 2:
        mode = "change_bar"
        render_points = change_points[:7]
        chart_title = "한눈에 보는 변동률"
    else:
        mode = "indicator_board"
        render_points = points[:6]
        chart_title = "한눈에 보는 시장 지표"

    base_title = _clean_text(title) or _snapshot_slot_label(market_snapshot) or chart_title
    digest_payload = {
        "title": base_title,
        "mode": mode,
        "points": render_points,
    }
    digest = hashlib.sha1(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:10]
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"market_chart_{digest}.png"

    title_font = _load_font(43)
    label_font = _load_font(28)
    small_font = _load_font(23)
    value_font = _load_font(34)
    percent_font = _load_font(31)
    footer_font = _load_font(22)

    image = Image.new("RGB", (width, height), (247, 249, 247))
    draw = ImageDraw.Draw(image)

    _draw_round_rect(
        draw,
        (44, 44, width - 44, height - 44),
        radius=28,
        fill=(255, 255, 255),
        outline=(220, 226, 222),
        width=2,
    )
    draw.rectangle((44, 44, width - 44, 72), fill=(28, 88, 112))

    eyebrow = _snapshot_slot_label(market_snapshot) or "시장 공부 노트"
    draw.text((78, 98), eyebrow, font=label_font, fill=(28, 88, 112))
    draw.text((78, 137), chart_title, font=title_font, fill=(28, 33, 36))

    source_note = _build_source_note(render_points)
    if source_note:
        draw.text((78, 188), source_note, font=small_font, fill=(102, 110, 110))

    if mode == "change_bar":
        _draw_change_bars(
            draw,
            render_points,
            box=(78, 235, width - 78, height - 118),
            label_font=label_font,
            small_font=small_font,
            percent_font=percent_font,
        )
    else:
        _draw_indicator_board(
            draw,
            render_points,
            box=(78, 235, width - 78, height - 118),
            label_font=label_font,
            small_font=small_font,
            value_font=value_font,
        )

    footer = "서로 다른 단위는 방향 예측이 아니라 함께 확인할 체크리스트로만 봅니다."
    footer_w = _text_width(draw, footer, footer_font)
    draw.text(((width - footer_w) / 2, height - 91), footer, font=footer_font, fill=(84, 92, 92))

    image.save(str(output_path), "PNG")
    logger.info(
        "Market chart rendered",
        extra={"path": str(output_path), "mode": mode, "point_count": len(render_points)},
    )
    return MarketChartResult(
        path=str(output_path),
        title=base_title,
        mode=mode,
        point_count=len(render_points),
    )


def extract_market_chart_points(market_snapshot: Mapping[str, Any]) -> List[dict[str, Any]]:
    """seo_snapshot.market_snapshot에서 렌더링 가능한 지표만 추린다."""

    raw_points = market_snapshot.get("data_points", []) if isinstance(market_snapshot, Mapping) else []
    if not isinstance(raw_points, (list, tuple)):
        return []

    points: List[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_points:
        if not isinstance(raw, Mapping):
            continue
        symbol = _clean_text(str(raw.get("symbol", "")))
        label = _clean_text(str(raw.get("label", ""))) or symbol
        if not symbol and not label:
            continue

        value = _to_float(raw.get("value"))
        change = _to_float(raw.get("change_percent"))
        if value is None and change is None:
            continue

        key = re.sub(r"\s+", "", f"{symbol}:{label}").lower()
        if key in seen:
            continue
        seen.add(key)
        points.append(
            {
                "symbol": symbol or label,
                "label": label or symbol,
                "source": _clean_text(str(raw.get("source", ""))),
                "value": value,
                "change_percent": change,
                "url": str(raw.get("url", "") or "").strip(),
            }
        )
        if len(points) >= 10:
            break

    return points


def _draw_change_bars(
    draw: Any,
    points: Sequence[Mapping[str, Any]],
    *,
    box: Tuple[int, int, int, int],
    label_font: Any,
    small_font: Any,
    percent_font: Any,
) -> None:
    """변동률을 기준선 중심 막대그래프로 그린다."""

    left, top, right, bottom = box
    chart_left = left + 250
    chart_right = right - 135
    axis_x = chart_left + (chart_right - chart_left) // 2
    max_abs = max(abs(float(point.get("change_percent") or 0.0)) for point in points)
    max_abs = max(max_abs, 1.0)
    row_height = max(56, min(82, (bottom - top) // max(1, len(points))))
    draw.line((axis_x, top - 8, axis_x, bottom + 8), fill=(202, 210, 208), width=3)

    for index, point in enumerate(points):
        y = top + index * row_height + 8
        center_y = y + 24
        label = _shorten(point.get("label") or point.get("symbol") or "", 14)
        symbol = _shorten(point.get("symbol") or "", 13)
        change = float(point.get("change_percent") or 0.0)
        value = point.get("value")
        source = _shorten(point.get("source") or "", 16)
        accent = (39, 133, 126) if change >= 0 else (177, 82, 78)

        draw.text((left, y), label, font=label_font, fill=(34, 39, 42))
        meta = " · ".join(part for part in [symbol, source] if part and part != label)
        if meta:
            draw.text((left, y + 35), meta, font=small_font, fill=(101, 111, 111))

        span = (chart_right - chart_left) / 2
        bar_len = int(span * min(1.0, abs(change) / max_abs))
        if change >= 0:
            bar_box = (axis_x, center_y - 15, axis_x + max(5, bar_len), center_y + 15)
        else:
            bar_box = (axis_x - max(5, bar_len), center_y - 15, axis_x, center_y + 15)
        _draw_round_rect(draw, bar_box, radius=13, fill=accent, outline=accent, width=1)

        percent_text = f"{change:+.2f}%"
        draw.text((right - 112, y + 4), percent_text, font=percent_font, fill=accent)
        if value is not None and math.isfinite(float(value)):
            value_text = f"값 {_format_number(value)}"
            draw.text((right - 112, y + 42), value_text, font=small_font, fill=(98, 106, 106))


def _draw_indicator_board(
    draw: Any,
    points: Sequence[Mapping[str, Any]],
    *,
    box: Tuple[int, int, int, int],
    label_font: Any,
    small_font: Any,
    value_font: Any,
) -> None:
    """단위가 다른 지표를 카드형 보드로 그린다."""

    left, top, right, bottom = box
    gap = 22
    columns = 2
    rows = int(math.ceil(len(points) / columns))
    cell_w = (right - left - gap) // columns
    cell_h = max(112, (bottom - top - gap * max(0, rows - 1)) // max(1, rows))
    accents = [(39, 133, 126), (113, 91, 64), (44, 105, 145), (154, 93, 82)]

    for index, point in enumerate(points):
        row = index // columns
        col = index % columns
        x1 = left + col * (cell_w + gap)
        y1 = top + row * (cell_h + gap)
        x2 = x1 + cell_w
        y2 = min(y1 + cell_h, bottom)
        accent = accents[index % len(accents)]
        _draw_round_rect(
            draw,
            (x1, y1, x2, y2),
            radius=18,
            fill=(250, 251, 249),
            outline=(218, 225, 222),
            width=2,
        )
        draw.rectangle((x1, y1, x1 + 10, y2), fill=accent)

        label = _shorten(point.get("label") or point.get("symbol") or "", 18)
        symbol = _shorten(point.get("symbol") or "", 16)
        source = _shorten(point.get("source") or "", 18)
        value = point.get("value")
        value_text = _format_number(value) if value is not None else "미수집"

        draw.text((x1 + 34, y1 + 22), label, font=label_font, fill=(34, 39, 42))
        draw.text((x1 + 34, y1 + 62), value_text, font=value_font, fill=accent)
        meta = " · ".join(part for part in [symbol, source] if part and part != label)
        if meta:
            draw.text((x1 + 34, y2 - 38), meta, font=small_font, fill=(101, 111, 111))


def _load_font(size: int) -> Any:
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


def _snapshot_slot_label(market_snapshot: Mapping[str, Any]) -> str:
    """스냅샷 슬롯을 사람이 읽는 짧은 라벨로 바꾼다."""

    slot = str(market_snapshot.get("slot", "") or "").strip().lower()
    scope = str(market_snapshot.get("scope", "") or "").strip().lower()
    labels = {
        "kr_preopen": "국장 전 브리핑",
        "us_preopen": "미장 전 브리핑",
        "evergreen_insight": "주말 통찰 노트",
        "weekly_reflection": "주간 복기 노트",
    }
    if slot in labels:
        return labels[slot]
    if scope == "kr":
        return "국장 공부 노트"
    if scope == "us":
        return "미장 공부 노트"
    return ""


def _build_source_note(points: Sequence[Mapping[str, Any]]) -> str:
    """상단에 표시할 출처 요약을 만든다."""

    sources: List[str] = []
    for point in points:
        source = _clean_text(str(point.get("source", "")))
        if source and source not in sources:
            sources.append(source)
        if len(sources) >= 3:
            break
    if not sources:
        return ""
    return "출처: " + ", ".join(sources)


def _to_float(value: Any) -> Optional[float]:
    """문자열 퍼센트와 숫자를 float로 정규화한다."""

    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        return None
    return numeric if math.isfinite(numeric) else None


def _format_number(value: Any) -> str:
    """시장 숫자를 이미지에 맞는 길이로 줄인다."""

    numeric = _to_float(value)
    if numeric is None:
        return str(value or "").strip() or "미수집"
    abs_value = abs(numeric)
    if abs_value >= 1000:
        return f"{numeric:,.2f}".rstrip("0").rstrip(".")
    if abs_value >= 100:
        return f"{numeric:.2f}".rstrip("0").rstrip(".")
    if abs_value >= 10:
        return f"{numeric:.3f}".rstrip("0").rstrip(".")
    return f"{numeric:.4f}".rstrip("0").rstrip(".")


def _clean_text(text: str) -> str:
    """이미지에 들어갈 텍스트를 정돈한다."""

    return re.sub(r"\s+", " ", str(text or "")).strip()


def _shorten(text: Any, max_chars: int) -> str:
    """너무 긴 라벨을 잘라 이미지 겹침을 막는다."""

    value = _clean_text(str(text or ""))
    if len(value) <= max_chars:
        return value
    return value[: max(1, max_chars - 3)].rstrip() + "..."


def _text_width(draw: Any, text: str, font: Any) -> int:
    """텍스트 픽셀 폭을 계산한다."""

    try:
        bbox = draw.textbbox((0, 0), str(text), font=font)
        return int(bbox[2] - bbox[0])
    except Exception:
        return len(str(text)) * 18


def _draw_round_rect(
    draw: Any,
    box: Sequence[int],
    *,
    radius: int,
    fill: Tuple[int, int, int],
    outline: Tuple[int, int, int],
    width: int,
) -> None:
    """Pillow 버전 차이를 흡수해 둥근 사각형을 그린다."""

    try:
        draw.rounded_rectangle(tuple(box), radius=radius, fill=fill, outline=outline, width=width)
    except Exception:
        draw.rectangle(tuple(box), fill=fill, outline=outline, width=width)
