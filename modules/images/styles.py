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
    # ── 정보성/제품 비교 글 (가전, IT, 재테크 등) ─────────────────────
    "product_clean": ImageStyle(
        name="Product Clean",
        suffix=(
            ", professional product photography style, clean white or light gray background, "
            "sharp focus, studio lighting, commercial quality, high resolution, "
            "realistic and trustworthy look"
        ),
        description="제품 사진풍 — 가전·IT·쇼핑 비교 글에 최적",
    ),
    "editorial_bold": ImageStyle(
        name="Editorial Bold",
        suffix=(
            ", bold editorial magazine style, strong typography composition, "
            "clean modern layout, vibrant accent color on neutral background, "
            "professional blog header, 4K quality"
        ),
        description="에디토리얼 매거진풍 — 정보성 글 범용",
    ),
    # ── 감성/라이프스타일 글 (카페, 육아 등) ─────────────────────────
    "lifestyle_warm": ImageStyle(
        name="Lifestyle Warm",
        suffix=(
            ", warm lifestyle photography style, natural light, cozy atmosphere, "
            "authentic feel, soft bokeh background, editorial photo"
        ),
        description="따뜻한 라이프스타일풍 — 카페·육아 글에 최적",
    ),
    # ── 레거시 (하위호환 유지) ──────────────────────────────────────
    "van_gogh_duotone": ImageStyle(
        name="Van Gogh Duotone",
        suffix=(
            ", oil painting style, bold swirling brushstrokes like Van Gogh, "
            "vibrant duotone color palette, dramatic lighting, artistic masterpiece"
        ),
        description="고흐 스타일 + 듀오톤 색감 (레거시)",
    ),
    "neo_impressionist": ImageStyle(
        name="Neo-Impressionist",
        suffix=", pointillism style like Seurat, small dots of color, modern vibrant palette, artistic illustration",
        description="점묘법 + 현대적 색감 (레거시)",
    ),
    "stylized_oil": ImageStyle(
        name="Stylized Oil Paint",
        suffix=", stylized oil painting, thick impasto brushstrokes, rich textures, warm color tones, artistic",
        description="스타일화된 유화 (레거시)",
    ),
}

CONTENT_STYLES: Dict[str, ImageStyle] = {
    # ── 정보성/제품 비교 글 ──────────────────────────────────────────
    "clean_infographic": ImageStyle(
        name="Clean Infographic",
        suffix=(
            ", clean infographic illustration style, flat design, soft pastel background, "
            "simple icons and shapes, modern minimal, easy to read, white space, "
            "professional diagram quality"
        ),
        description="클린 인포그래픽 — 정보성·비교 글 범용",
    ),
    "realistic_scene": ImageStyle(
        name="Realistic Scene",
        suffix=(
            ", realistic scene photography style, natural lighting, "
            "authentic everyday setting, sharp detail, relatable situation, "
            "editorial quality"
        ),
        description="실사 장면풍 — 생활밀착 정보 글에 최적",
    ),
    # ── 감성/라이프스타일 글 ─────────────────────────────────────────
    "cozy_lifestyle": ImageStyle(
        name="Cozy Lifestyle",
        suffix=(
            ", cozy lifestyle illustration, warm tones, soft watercolor-like texture, "
            "gentle light, homey atmosphere, relatable scene"
        ),
        description="아늑한 라이프스타일 — 카페·육아 감성 글",
    ),
    # ── 레거시 (하위호환 유지) ──────────────────────────────────────
    "monet_soft": ImageStyle(
        name="Monet Soft",
        suffix=", impressionist style like Claude Monet, soft light, gentle color blending, dreamy atmosphere, peaceful mood",
        description="모네 스타일 (레거시)",
    ),
    "watercolor_gentle": ImageStyle(
        name="Watercolor Gentle",
        suffix=", soft watercolor painting, gentle washes, pastel colors, delicate and airy, artistic illustration",
        description="부드러운 수채화 (레거시)",
    ),
    "minimal_illustration": ImageStyle(
        name="Minimal Illustration",
        suffix=", clean minimal illustration, simple shapes, soft colors, modern design, white background",
        description="미니멀 일러스트 (레거시)",
    ),
}

DEFAULT_THUMBNAIL_STYLE = "product_clean"
DEFAULT_CONTENT_STYLE = "clean_infographic"

# 토픽 모드별 권장 스타일 (runtime_factory 등에서 참조 가능)
TOPIC_THUMBNAIL_STYLE: Dict[str, str] = {
    "cafe": "lifestyle_warm",
    "parenting": "lifestyle_warm",
    "it": "editorial_bold",
    "finance": "editorial_bold",
    "default": "product_clean",
}

TOPIC_CONTENT_STYLE: Dict[str, str] = {
    "cafe": "cozy_lifestyle",
    "parenting": "cozy_lifestyle",
    "it": "clean_infographic",
    "finance": "clean_infographic",
    "default": "clean_infographic",
}


def get_thumbnail_style(style_name: str = DEFAULT_THUMBNAIL_STYLE) -> ImageStyle:
    """썸네일 스타일을 반환한다."""
    return THUMBNAIL_STYLES.get(style_name, THUMBNAIL_STYLES[DEFAULT_THUMBNAIL_STYLE])


def get_content_style(style_name: str = DEFAULT_CONTENT_STYLE) -> ImageStyle:
    """본문 스타일을 반환한다."""
    return CONTENT_STYLES.get(style_name, CONTENT_STYLES[DEFAULT_CONTENT_STYLE])
