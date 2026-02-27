"""애플리케이션 설정 로더."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict

import yaml  # type: ignore[import-untyped]


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "text"


@dataclass
class PublisherConfig:
    headless: bool = True
    timeout_ms: int = 45000
    human_delay_min_ms: int = 500
    human_delay_max_ms: int = 2000


@dataclass
class PipelineConfig:
    max_llm_calls_per_job: int = 15
    quality_gate_enabled: bool = True


@dataclass
class RetryConfig:
    max_retries: int = 3
    backoff_base_sec: float = 2.0
    backoff_max_sec: float = 60.0


@dataclass
class LLMConfig:
    primary_provider: str = "qwen"
    primary_model: str = "qwen-plus"
    secondary_provider: str = "deepseek"
    secondary_model: str = "deepseek-chat"
    # 3순위 폴백 (쉼표 구분, 순서대로 시도)
    tertiary_providers: str = "groq,cerebras"
    tertiary_models: str = "llama-3.3-70b-versatile,llama3.1-8b"
    max_tokens: int = 4096
    temperature: float = 0.7
    timeout_sec: float = 120.0
    enable_quality_check: bool = True
    enable_seo_optimization: bool = True
    fallback_to_secondary: bool = True
    # Gemini 이미지 프롬프트 번역
    gemini_image_prompt_translation: bool = True
    gemini_model: str = "gemini-2.0-flash"
    # 품질 향상 옵션 (전략 B, C, E)
    use_multistep: bool = False  # 멀티스텝 생성 (아웃라인 → 섹션 → 통합)
    max_rewrites: int = 2  # 품질 미달 시 최대 재작성 횟수
    min_quality_score: int = 70  # 최소 품질 점수 (0-100)
    enable_fact_check: bool = False  # 팩트체크 단계 활성화
    default_tone: str = "conversational"  # 기본 톤 (conversational, professional, storytelling, educational)
    enable_voice_rewrite: bool = True  # 2-Step 파이프라인 Voice 레이어 활성화


@dataclass
class ImageConfig:
    enabled: bool = True
    model: str = "flux"
    thumbnail_style: str = "van_gogh_duotone"
    content_style: str = "monet_soft"
    thumbnail_size: str = "1024*1024"
    content_size: str = "1024*768"
    max_content_images: int = 4
    output_dir: str = "data/images"


@dataclass
class SEOConfig:
    """플랫폼별 SEO 유입 전략 설정."""

    # 태그 자동 생성
    enable_tag_generation: bool = True
    tag_llm_provider: str = "deepseek"   # 태그 생성 전용 LLM (비용↓)
    tag_llm_model: str = "deepseek-chat"

    # 피드백 분석
    enable_feedback_analysis: bool = False  # 성과 데이터 충분 시 활성화
    feedback_llm_provider: str = "deepseek"
    feedback_llm_model: str = "deepseek-chat"
    feedback_min_posts: int = 5             # 분석에 필요한 최소 포스트 수
    feedback_analysis_days: int = 30        # 분석 기간 (일)

    # 네이버 기본 카테고리
    naver_default_category: str = "생활·노하우·쇼핑"


@dataclass
class WebSearchConfig:
    """웹 검색 및 본문 추출 설정."""

    enabled: bool = False
    provider: str = "brave"
    api_key: str = ""
    timeout_sec: float = 10.0
    fetch_timeout_sec: float = 15.0
    max_results: int = 5
    fetch_max_chars: int = 3000


@dataclass
class AppConfig:
    logging: LoggingConfig
    publisher: PublisherConfig
    pipeline: PipelineConfig
    retry: RetryConfig
    llm: LLMConfig
    images: ImageConfig
    seo: SEOConfig = None  # type: ignore[assignment]
    web_search: WebSearchConfig = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.seo is None:
            self.seo = SEOConfig()
        if self.web_search is None:
            self.web_search = WebSearchConfig()


def load_config(config_dir: str = "config") -> AppConfig:
    """설정을 로드한다. 우선순위: 환경변수 > local.yaml > default.yaml."""
    base_path = Path(config_dir)
    default_data = _load_yaml(base_path / "default.yaml")
    local_data = _load_yaml(base_path / "local.yaml")

    merged = _deep_merge(default_data, local_data)
    merged = _apply_env_overrides(merged)

    return AppConfig(
        logging=LoggingConfig(**merged.get("logging", {})),
        publisher=PublisherConfig(**merged.get("publisher", {})),
        pipeline=PipelineConfig(**merged.get("pipeline", {})),
        retry=RetryConfig(**merged.get("retry", {})),
        llm=LLMConfig(**merged.get("llm", {})),
        images=ImageConfig(**merged.get("images", {})),
        seo=SEOConfig(**merged.get("seo", {})),
        web_search=WebSearchConfig(**merged.get("web_search", {})),
    )


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config must be mapping object: {path}")
    return data


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _apply_env_overrides(config_data: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(config_data)

    env_map: Dict[str, tuple[str, str, Callable[[str], Any]]] = {
        "LOG_LEVEL": ("logging", "level", str),
        "LOG_FORMAT": ("logging", "format", str),
        "PUBLISHER_HEADLESS": ("publisher", "headless", _parse_bool),
        "PUBLISHER_TIMEOUT_MS": ("publisher", "timeout_ms", int),
        "PUBLISHER_HUMAN_DELAY_MIN_MS": ("publisher", "human_delay_min_ms", int),
        "PUBLISHER_HUMAN_DELAY_MAX_MS": ("publisher", "human_delay_max_ms", int),
        "PIPELINE_MAX_LLM_CALLS_PER_JOB": ("pipeline", "max_llm_calls_per_job", int),
        "PIPELINE_QUALITY_GATE_ENABLED": ("pipeline", "quality_gate_enabled", _parse_bool),
        "RETRY_MAX_RETRIES": ("retry", "max_retries", int),
        "RETRY_BACKOFF_BASE_SEC": ("retry", "backoff_base_sec", float),
        "RETRY_BACKOFF_MAX_SEC": ("retry", "backoff_max_sec", float),
        "LLM_PRIMARY_PROVIDER": ("llm", "primary_provider", str),
        "LLM_PRIMARY_MODEL": ("llm", "primary_model", str),
        "LLM_SECONDARY_PROVIDER": ("llm", "secondary_provider", str),
        "LLM_SECONDARY_MODEL": ("llm", "secondary_model", str),
        # 이전 단일 모델 설정과의 하위 호환
        "LLM_MODEL": ("llm", "primary_model", str),
        "CLAUDE_MODEL": ("llm", "primary_model", str),
        "LLM_MAX_TOKENS": ("llm", "max_tokens", int),
        "CLAUDE_MAX_TOKENS": ("llm", "max_tokens", int),
        "LLM_TEMPERATURE": ("llm", "temperature", float),
        "LLM_TIMEOUT_SEC": ("llm", "timeout_sec", float),
        "LLM_ENABLE_QUALITY_CHECK": ("llm", "enable_quality_check", _parse_bool),
        "LLM_ENABLE_SEO_OPTIMIZATION": ("llm", "enable_seo_optimization", _parse_bool),
        "LLM_FALLBACK_TO_SECONDARY": ("llm", "fallback_to_secondary", _parse_bool),
        "LLM_USE_MULTISTEP": ("llm", "use_multistep", _parse_bool),
        "LLM_MAX_REWRITES": ("llm", "max_rewrites", int),
        "LLM_MIN_QUALITY_SCORE": ("llm", "min_quality_score", int),
        "LLM_ENABLE_FACT_CHECK": ("llm", "enable_fact_check", _parse_bool),
        "LLM_DEFAULT_TONE": ("llm", "default_tone", str),
        "LLM_ENABLE_VOICE_REWRITE": ("llm", "enable_voice_rewrite", _parse_bool),
        "IMAGES_ENABLED": ("images", "enabled", _parse_bool),
        "IMAGES_MODEL": ("images", "model", str),
        "IMAGES_THUMBNAIL_STYLE": ("images", "thumbnail_style", str),
        "IMAGES_CONTENT_STYLE": ("images", "content_style", str),
        "IMAGES_THUMBNAIL_SIZE": ("images", "thumbnail_size", str),
        "IMAGES_CONTENT_SIZE": ("images", "content_size", str),
        "IMAGES_MAX_CONTENT_IMAGES": ("images", "max_content_images", int),
        "IMAGES_OUTPUT_DIR": ("images", "output_dir", str),
        # SEO 유입 전략
        "SEO_ENABLE_TAG_GENERATION": ("seo", "enable_tag_generation", _parse_bool),
        "SEO_TAG_LLM_PROVIDER": ("seo", "tag_llm_provider", str),
        "SEO_TAG_LLM_MODEL": ("seo", "tag_llm_model", str),
        "SEO_ENABLE_FEEDBACK_ANALYSIS": ("seo", "enable_feedback_analysis", _parse_bool),
        "SEO_FEEDBACK_LLM_PROVIDER": ("seo", "feedback_llm_provider", str),
        "SEO_FEEDBACK_LLM_MODEL": ("seo", "feedback_llm_model", str),
        "SEO_FEEDBACK_MIN_POSTS": ("seo", "feedback_min_posts", int),
        "SEO_FEEDBACK_ANALYSIS_DAYS": ("seo", "feedback_analysis_days", int),
        "SEO_NAVER_DEFAULT_CATEGORY": ("seo", "naver_default_category", str),
        # 웹 검색
        "WEB_SEARCH_ENABLED": ("web_search", "enabled", _parse_bool),
        "WEB_SEARCH_PROVIDER": ("web_search", "provider", str),
        "BRAVE_API_KEY": ("web_search", "api_key", str),
        "WEB_SEARCH_TIMEOUT_SEC": ("web_search", "timeout_sec", float),
        "WEB_SEARCH_FETCH_TIMEOUT_SEC": ("web_search", "fetch_timeout_sec", float),
        "WEB_SEARCH_MAX_RESULTS": ("web_search", "max_results", int),
        "WEB_SEARCH_FETCH_MAX_CHARS": ("web_search", "fetch_max_chars", int),
    }

    for env_name, (section, key, caster) in env_map.items():
        raw_value = os.getenv(env_name)
        if raw_value is None:
            continue
        section_data = result.setdefault(section, {})
        section_data[key] = caster(raw_value)

    return result
