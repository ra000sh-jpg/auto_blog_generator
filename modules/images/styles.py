"""이미지 스타일 프리셋."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass
class ImageStyle:
    name: str
    suffix: str
    description: str


THUMBNAIL_STYLES: Dict[str, ImageStyle] = {
    "van_gogh_duotone": ImageStyle(
        name="Van Gogh Duotone",
        suffix=(
            ", oil painting style, bold swirling brushstrokes like Van Gogh, "
            "vibrant duotone color palette, dramatic lighting, artistic masterpiece"
        ),
        description="고흐 스타일 + 듀오톤 색감",
    ),
    "neo_impressionist": ImageStyle(
        name="Neo-Impressionist",
        suffix=", pointillism style like Seurat, small dots of color, modern vibrant palette, artistic illustration",
        description="점묘법 + 현대적 색감",
    ),
    "stylized_oil": ImageStyle(
        name="Stylized Oil Paint",
        suffix=", stylized oil painting, thick impasto brushstrokes, rich textures, warm color tones, artistic",
        description="스타일화된 유화",
    ),
}

CONTENT_STYLES: Dict[str, ImageStyle] = {
    "monet_soft": ImageStyle(
        name="Monet Soft",
        suffix=", impressionist style like Claude Monet, soft light, gentle color blending, dreamy atmosphere, peaceful mood",
        description="모네 스타일 - 부드럽고 몽환적",
    ),
    "watercolor_gentle": ImageStyle(
        name="Watercolor Gentle",
        suffix=", soft watercolor painting, gentle washes, pastel colors, delicate and airy, artistic illustration",
        description="부드러운 수채화",
    ),
    "minimal_illustration": ImageStyle(
        name="Minimal Illustration",
        suffix=", clean minimal illustration, simple shapes, soft colors, modern design, white background",
        description="미니멀 일러스트",
    ),
}

DEFAULT_THUMBNAIL_STYLE = "van_gogh_duotone"
DEFAULT_CONTENT_STYLE = "monet_soft"


def get_thumbnail_style(style_name: str = DEFAULT_THUMBNAIL_STYLE) -> ImageStyle:
    """썸네일 스타일을 반환한다."""
    return THUMBNAIL_STYLES.get(style_name, THUMBNAIL_STYLES[DEFAULT_THUMBNAIL_STYLE])


def get_content_style(style_name: str = DEFAULT_CONTENT_STYLE) -> ImageStyle:
    """본문 스타일을 반환한다."""
    return CONTENT_STYLES.get(style_name, CONTENT_STYLES[DEFAULT_CONTENT_STYLE])
