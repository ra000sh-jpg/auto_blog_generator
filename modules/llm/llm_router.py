"""모델 라우팅/견적 계산 유틸리티."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ..automation.job_store import JobStore
from .. import constants
from ..constants import DEFAULT_FALLBACK_CATEGORY
from ..config import LLMConfig
from .vlm_router import VLM_MODEL_MATRIX

if TYPE_CHECKING:
    from ..automation.job_store import Job

USD_TO_KRW = 1400.0

# 토큰 사용량은 역할별 평균치(보수적 추정)
TOKEN_BUDGET = {
    "parser": {"input": 450, "output": 180},
    "pre_analysis": {"input": 600, "output": 1000},
    "quality_step": {"input": 5500, "output": 2800},
    "voice_step": {"input": 4200, "output": 2500},
    # 추가 파이프라인 단계 (quality_step 역할로 호출, UI 원가에 반영)
    "self_critique": {"input": 2400, "output": 800},   # 품질 자기검증 1회
    "seo_step": {"input": 1200, "output": 600},        # SEO 제목/메타 최적화
    "image_prompt": {"input": 800, "output": 400},     # image_slots 생성
    "sentence_polish": {"input": 4500, "output": 3000},
}


@dataclass(frozen=True)
class TextModelSpec:
    """텍스트 모델 스펙."""

    provider: str
    model: str
    label: str
    key_id: str
    input_cost_per_1m_usd: float
    output_cost_per_1m_usd: float
    quality_score: int
    speed_score: int

    @property
    def avg_cost_per_1k_usd(self) -> float:
        """입출력 평균 단가(1K 토큰 기준)."""
        return ((self.input_cost_per_1m_usd + self.output_cost_per_1m_usd) / 2.0) / 1000.0


@dataclass(frozen=True)
class ImageModelSpec:
    """이미지 엔진 스펙."""

    engine_id: str
    label: str
    key_id: str
    cost_per_image_krw: int
    quality_score: int
    category: str


# 가격 기준일: 2026-06-05
# 출처:
#   DeepSeek: https://api-docs.deepseek.com/quick_start/pricing
#   Gemini: https://ai.google.dev/gemini-api/docs/pricing
#   OpenAI: https://openai.com/api/pricing
#   Qwen: https://help.aliyun.com/zh/model-studio/getting-started/models
#   Claude: https://docs.anthropic.com/en/docs/about-claude/models
#   Z.AI/Groq/Cerebras: 무료 Tier (rate limit 적용)
TEXT_MODEL_MATRIX: List[TextModelSpec] = [
    # Qwen (DashScope)
    TextModelSpec(
        provider="qwen",
        model="qwen-flash",
        label="Qwen Flash",
        key_id="qwen",
        input_cost_per_1m_usd=0.05,
        output_cost_per_1m_usd=0.40,
        quality_score=80,
        speed_score=95,
    ),
    TextModelSpec(
        provider="qwen",
        model="qwen-turbo",
        label="Qwen Turbo",
        key_id="qwen",
        input_cost_per_1m_usd=0.05,
        output_cost_per_1m_usd=0.20,
        quality_score=78,
        speed_score=94,
    ),
    TextModelSpec(
        provider="qwen",
        model="qwen-plus",
        label="Qwen Plus",
        key_id="qwen",
        input_cost_per_1m_usd=0.40,
        output_cost_per_1m_usd=1.20,
        quality_score=84,
        speed_score=90,
    ),
    TextModelSpec(
        provider="qwen",
        model="qwen-max",
        label="Qwen Max",
        key_id="qwen",
        input_cost_per_1m_usd=1.60,
        output_cost_per_1m_usd=6.40,
        quality_score=91,
        speed_score=82,
    ),
    # DeepSeek
    TextModelSpec(
        provider="deepseek",
        model="deepseek-v4-flash",
        label="DeepSeek V4 Flash",
        key_id="deepseek",
        input_cost_per_1m_usd=0.14,
        output_cost_per_1m_usd=0.28,
        quality_score=88,
        speed_score=92,
    ),
    TextModelSpec(
        provider="deepseek",
        model="deepseek-v4-pro",
        label="DeepSeek V4 Pro",
        key_id="deepseek",
        input_cost_per_1m_usd=0.435,
        output_cost_per_1m_usd=0.87,
        quality_score=94,
        speed_score=78,
    ),
    TextModelSpec(
        provider="deepseek",
        model="deepseek-chat",
        label="DeepSeek Chat (legacy alias)",
        key_id="deepseek",
        input_cost_per_1m_usd=0.14,
        output_cost_per_1m_usd=0.28,
        quality_score=86,
        speed_score=88,
    ),
    TextModelSpec(
        provider="deepseek",
        model="deepseek-reasoner",
        label="DeepSeek Reasoner (legacy alias)",
        key_id="deepseek",
        input_cost_per_1m_usd=0.14,
        output_cost_per_1m_usd=0.28,
        quality_score=90,
        speed_score=75,
    ),
    # Z.AI
    TextModelSpec(
        provider="zai",
        model="glm-4.7-flash",
        label="Z.AI GLM-4.7 Flash (무료)",
        key_id="zai",
        input_cost_per_1m_usd=0.0,
        output_cost_per_1m_usd=0.0,
        quality_score=92,
        speed_score=96,
    ),
    # Gemini
    TextModelSpec(
        provider="gemini",
        model="gemini-2.0-flash-lite",
        label="Gemini 2.0 Flash Lite",
        key_id="gemini",
        input_cost_per_1m_usd=0.075,
        output_cost_per_1m_usd=0.30,
        quality_score=82,
        speed_score=96,
    ),
    TextModelSpec(
        provider="gemini",
        model="gemini-2.0-flash",
        label="Gemini 2.0 Flash",
        key_id="gemini",
        input_cost_per_1m_usd=0.10,
        output_cost_per_1m_usd=0.40,
        quality_score=90,
        speed_score=93,
    ),
    TextModelSpec(
        provider="gemini",
        model="gemini-2.5-flash",
        label="Gemini 2.5 Flash",
        key_id="gemini",
        input_cost_per_1m_usd=0.30,
        output_cost_per_1m_usd=2.50,
        quality_score=94,
        speed_score=90,
    ),
    # OpenAI
    TextModelSpec(
        provider="openai",
        model="gpt-4.1-nano",
        label="OpenAI GPT-4.1 Nano",
        key_id="openai",
        input_cost_per_1m_usd=0.10,
        output_cost_per_1m_usd=0.40,
        quality_score=85,
        speed_score=95,
    ),
    TextModelSpec(
        provider="openai",
        model="gpt-4.1-mini",
        label="OpenAI GPT-4.1 mini",
        key_id="openai",
        input_cost_per_1m_usd=0.40,
        output_cost_per_1m_usd=1.60,
        quality_score=92,
        speed_score=89,
    ),
    TextModelSpec(
        provider="openai",
        model="gpt-4.1",
        label="OpenAI GPT-4.1",
        key_id="openai",
        input_cost_per_1m_usd=2.00,
        output_cost_per_1m_usd=8.00,
        quality_score=96,
        speed_score=84,
    ),
    # Anthropic Claude
    TextModelSpec(
        provider="claude",
        model="claude-3-5-haiku-20241022",
        label="Claude Haiku 3.5",
        key_id="claude",
        input_cost_per_1m_usd=0.80,
        output_cost_per_1m_usd=4.00,
        quality_score=88,
        speed_score=93,
    ),
    TextModelSpec(
        provider="claude",
        model="claude-sonnet-4-20250514",
        label="Claude Sonnet 4",
        key_id="claude",
        input_cost_per_1m_usd=3.00,
        output_cost_per_1m_usd=15.00,
        quality_score=97,
        speed_score=83,
    ),
    # 무료 프로바이더: parser/태그 생성 등 단순 역할에 우선 라우팅
    TextModelSpec(
        provider="groq",
        model="llama-3.3-70b-versatile",
        label="Groq Llama-3.3 70B (무료)",
        key_id="groq",
        input_cost_per_1m_usd=0.0,
        output_cost_per_1m_usd=0.0,
        quality_score=80,
        speed_score=95,
    ),
    TextModelSpec(
        provider="groq",
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        label="Groq Llama-4 Scout (무료)",
        key_id="groq",
        input_cost_per_1m_usd=0.0,
        output_cost_per_1m_usd=0.0,
        quality_score=83,
        speed_score=94,
    ),
    TextModelSpec(
        provider="cerebras",
        model="gpt-oss-120b",
        label="Cerebras GPT-OSS 120B (무료)",
        key_id="cerebras",
        input_cost_per_1m_usd=0.0,
        output_cost_per_1m_usd=0.0,
        quality_score=86,
        speed_score=94,
    ),
    # NVIDIA NIM (12개월 이용권 활용)
    # deepseek-ai/deepseek-r1 → 410 Gone (2026-03 서비스 종료)
    # nvidia/llama-3.1-nemotron-70b-instruct → 404 (삭제됨)
    TextModelSpec(
        provider="nvidia",
        model="meta/llama-3.3-70b-instruct",
        label="NVIDIA Llama 3.3 70B",
        key_id="nvidia",
        input_cost_per_1m_usd=0.0,  # 이용권 기반 무료로 간주
        output_cost_per_1m_usd=0.0,
        quality_score=93,
        speed_score=92,
    ),
]

IMAGE_MODEL_MATRIX: List[ImageModelSpec] = [
    ImageModelSpec(
        engine_id="pexels",
        label="무료 스톡 (Pexels)",
        key_id="pexels",
        cost_per_image_krw=0,
        quality_score=78,
        category="free",
    ),
    ImageModelSpec(
        engine_id="together_flux",
        label="무료 AI (Together Flux)",
        key_id="together",
        cost_per_image_krw=0,
        quality_score=82,
        category="free",
    ),
    ImageModelSpec(
        engine_id="fal_flux",
        label="유료 고급 AI (Fal Flux)",
        key_id="fal",
        cost_per_image_krw=4,
        quality_score=93,
        category="paid",
    ),
    ImageModelSpec(
        engine_id="openai_dalle3",
        label="유료 고급 AI (OpenAI DALL-E 3)",
        key_id="openai_image",
        cost_per_image_krw=56,
        quality_score=96,
        category="paid",
    ),
]

DEFAULT_TEXT_KEYS = {
    "qwen": "DASHSCOPE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "zai": "ZAI_API_KEY",
}

DEFAULT_IMAGE_KEYS = {
    "pexels": "PEXELS_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fal": "FAL_KEY",
    "openai_image": "OPENAI_API_KEY",
}

DEFAULT_STRATEGY_MODE = "cost"
DEFAULT_IMAGE_ENGINE = "pexels"
DEFAULT_IMAGES_PER_POST = 1
DEFAULT_IMAGES_PER_POST_MIN = 0
DEFAULT_IMAGES_PER_POST_MAX = 4
DEFAULT_COST_STRICT_MODE = True
DEFAULT_COST_FREE_ONLY_FALLBACK = True
DEFAULT_COST_MAX_FALLBACK_USD_PER_1M = 1.0
DEFAULT_COST_RETRY_MAX_RETRIES = 6
DEFAULT_COST_RETRY_BASE_DELAY_SEC = 2.0
DEFAULT_COST_RETRY_MAX_DELAY_SEC = 20.0
DEFAULT_COST_LOCK_QUALITY_PROVIDER = True
DEFAULT_IMAGE_AI_QUOTA = "0"
IMAGE_AI_QUOTA_VALUES = {"0", "1", "2", "3", "4", "all"}
DEFAULT_IMAGE_AI_ENGINE = "together_flux"
DEFAULT_IMAGE_TOPIC_QUOTA_OVERRIDES = {
    "cafe": "0",
    "it": "1",
    "finance": "1",
    "economy": "1",
    "parenting": "0",
}
DEFAULT_TRAFFIC_FEEDBACK_STRONG_MODE = False
FREE_FALLBACK_PROVIDER_ORDER = ("zai", "nvidia", "groq", "cerebras")
FREE_FALLBACK_PROVIDER_RANK = {
    provider: index for index, provider in enumerate(FREE_FALLBACK_PROVIDER_ORDER)
}


def mask_secret(raw_value: str) -> str:
    """민감정보를 마스킹한다."""
    value = str(raw_value or "").strip()
    if not value:
        return ""
    if value.startswith("sk-"):
        tail = value[-4:] if len(value) > 7 else ""
        return f"sk-****{tail}" if tail else "sk-****"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}****{value[-2:]}"


def normalize_strategy_mode(raw_value: str) -> str:
    """전략 모드를 정규화한다."""
    value = str(raw_value or "").strip().lower()
    if value in {"balanced", "balance", "standard"}:
        return "balanced"
    if value in {"quality", "best_quality", "hq"}:
        return "quality"
    return "cost"


def normalize_vlm_strategy_setting(raw_value: Any, default: str = constants.VLM_DEFAULT_STRATEGY_MODE) -> str:
    """VLM 전략 설정값(inherit/cost/balanced/quality)을 정규화한다."""
    value = str(raw_value or "").strip().lower()
    if value in {"inherit", "cost", "balanced", "quality"}:
        return value
    fallback = str(default or "").strip().lower()
    if fallback in {"inherit", "cost", "balanced", "quality"}:
        return fallback
    return constants.VLM_DEFAULT_STRATEGY_MODE


def normalize_image_ai_quota(raw_value: Any, default: str = DEFAULT_IMAGE_AI_QUOTA) -> str:
    """AI 이미지 쿼터 값을 정규화한다."""
    value = str(raw_value or "").strip().lower()
    if value in IMAGE_AI_QUOTA_VALUES:
        return value
    try:
        numeric_value = int(value)
        if numeric_value <= 0:
            return "0"
        if numeric_value >= 4:
            return "all"
        return str(numeric_value)
    except (TypeError, ValueError):
        pass
    return str(default).strip().lower() if str(default).strip().lower() in IMAGE_AI_QUOTA_VALUES else DEFAULT_IMAGE_AI_QUOTA


def _to_bool(raw_value: Any, default: bool = False) -> bool:
    """문자열/숫자를 bool로 변환한다."""
    if isinstance(raw_value, bool):
        return raw_value
    if raw_value is None:
        return default
    text = str(raw_value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _to_int(raw_value: Any, default: int, min_value: int, max_value: int) -> int:
    """정수를 범위 내로 정규화한다."""
    try:
        value = int(raw_value)
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _to_float(raw_value: Any, default: float, min_value: float, max_value: float) -> float:
    """실수를 범위 내로 정규화한다."""
    try:
        value = float(raw_value)
    except Exception:
        value = float(default)
    if value < min_value:
        return float(min_value)
    if value > max_value:
        return float(max_value)
    return float(value)


def _parse_json_map(raw_value: str) -> Dict[str, str]:
    """JSON 객체 문자열을 dict[str, str]로 파싱한다."""
    text = str(raw_value or "").strip()
    if not text:
        return {}
    try:
        decoded = json.loads(text)
    except Exception:
        return {}
    if not isinstance(decoded, dict):
        return {}
    output: Dict[str, str] = {}
    for key, value in decoded.items():
        normalized_key = str(key).strip().lower()
        normalized_value = str(value or "").strip()
        if normalized_key:
            output[normalized_key] = normalized_value
    return output


def _read_dotenv_values(path: Path = Path(".env")) -> Dict[str, str]:
    """간단한 KEY=VALUE 형식의 .env 값을 읽는다."""
    if not path.exists() or not path.is_file():
        return {}
    values: Dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            values[key] = value.strip().strip("\"'")
    except Exception:
        return {}
    return values


def _json_text(value: Dict[str, str]) -> str:
    """dict를 JSON 문자열로 직렬화한다."""
    return json.dumps(value, ensure_ascii=False)


def _find_text_model(provider: str, model: str) -> Optional[TextModelSpec]:
    """provider/model에 맞는 모델 스펙을 찾는다."""
    normalized_provider = str(provider).strip().lower()
    normalized_model = str(model).strip().lower()
    for spec in TEXT_MODEL_MATRIX:
        if spec.provider == normalized_provider and spec.model.lower() == normalized_model:
            return spec
    return None


def _find_image_model(engine_id: str) -> Optional[ImageModelSpec]:
    """이미지 엔진 스펙을 찾는다."""
    normalized = str(engine_id).strip().lower()
    for spec in IMAGE_MODEL_MATRIX:
        if spec.engine_id == normalized:
            return spec
    return None


def _free_fallback_priority(spec: TextModelSpec) -> int:
    """무료 fallback provider 우선순위를 반환한다."""
    return FREE_FALLBACK_PROVIDER_RANK.get(
        str(spec.provider or "").strip().lower(),
        len(FREE_FALLBACK_PROVIDER_RANK),
    )


def _role_temperature(strategy_mode: str, role: str) -> float:
    """역할별 기본 temperature를 반환한다."""
    if role == "parser":
        return 0.1
    if role in {"pre_analysis", "sentence_polish"}:
        return 0.3 if strategy_mode == "cost" else 0.35
    if role == "quality_step":
        if strategy_mode == "quality":
            return 0.65
        if strategy_mode == "balanced":
            return 0.6
        return 0.55
    if role == "voice_step":
        if strategy_mode == "quality":
            return 0.45
        if strategy_mode == "balanced":
            return 0.4
        return 0.35
    return 0.6


_CHEAP_ROLES = {"parser", "pre_analysis", "sentence_polish"}
_ROLE_MIN_QUALITY = {
    "parser": 75,
    "pre_analysis": 75,
    "quality_step": 82,
    "voice_step": 80,
    "sentence_polish": 78,
}


class LLMRouter:
    """모델 라우팅/견적/설정 저장을 담당한다."""

    SETTINGS_KEYS = (
        "router_strategy_mode",
        "router_text_api_keys",
        "router_image_api_keys",
        "router_cost_strict_mode",
        "router_cost_free_only_fallback",
        "router_cost_max_fallback_usd_per_1m",
        "router_cost_retry_max_retries",
        "router_cost_retry_base_delay_sec",
        "router_cost_retry_max_delay_sec",
        "router_cost_lock_quality_provider",
        "router_image_engine",
        "router_image_ai_engine",
        "router_image_ai_quota",
        "router_image_topic_quota_overrides",
        "router_traffic_feedback_strong_mode",
        "router_image_enabled",
        "router_images_per_post",
        "router_images_per_post_min",
        "router_images_per_post_max",
        "router_eval_model_today",
        "router_eval_min_samples",
        "router_eval_last_run_date",
        "router_champion_switch_threshold",
        "router_registered_models",
        "router_champion_model",
        "router_vlm_enabled",
        "router_vlm_model",
        "router_vlm_strategy_mode",
        "router_vlm_eval_sampling_rate",
        "router_vlm_quality_floor",
        "router_vlm_max_cost_guard_krw",
        "fallback_category",
    )

    def __init__(
        self,
        *,
        job_store: Optional[JobStore] = None,
        llm_config: Optional[LLMConfig] = None,
    ):
        self.job_store = job_store
        self.llm_config = llm_config or LLMConfig()

    def get_saved_settings(self) -> Dict[str, Any]:
        """DB+환경변수를 합쳐 현재 라우팅 설정을 반환한다."""
        raw_settings: Dict[str, str] = {}
        if self.job_store:
            raw_settings = self.job_store.get_system_settings(list(self.SETTINGS_KEYS))

        raw_text_key_setting = raw_settings.get("router_text_api_keys", "")
        raw_image_key_setting = raw_settings.get("router_image_api_keys", "")
        text_api_keys = _parse_json_map(raw_text_key_setting)
        image_api_keys = _parse_json_map(raw_image_key_setting)
        dotenv_values = _read_dotenv_values()

        # DB 설정이 아예 비어 있을 때만 환경변수와 .env 키를 자동 반영한다.
        if not str(raw_text_key_setting or "").strip():
            for key_id, env_name in DEFAULT_TEXT_KEYS.items():
                if not text_api_keys.get(key_id):
                    text_api_keys[key_id] = (
                        os.getenv(env_name, "").strip() or str(dotenv_values.get(env_name, "")).strip()
                    )
        if not str(raw_image_key_setting or "").strip():
            for key_id, env_name in DEFAULT_IMAGE_KEYS.items():
                if not image_api_keys.get(key_id):
                    image_api_keys[key_id] = (
                        os.getenv(env_name, "").strip() or str(dotenv_values.get(env_name, "")).strip()
                    )

        strategy_mode = normalize_strategy_mode(raw_settings.get("router_strategy_mode", DEFAULT_STRATEGY_MODE))
        cost_strict_mode = _to_bool(
            raw_settings.get("router_cost_strict_mode", "true" if DEFAULT_COST_STRICT_MODE else "false"),
            default=DEFAULT_COST_STRICT_MODE,
        )
        cost_free_only_fallback = _to_bool(
            raw_settings.get(
                "router_cost_free_only_fallback",
                "true" if DEFAULT_COST_FREE_ONLY_FALLBACK else "false",
            ),
            default=DEFAULT_COST_FREE_ONLY_FALLBACK,
        )
        cost_max_fallback_usd_per_1m = _to_float(
            raw_settings.get(
                "router_cost_max_fallback_usd_per_1m",
                str(DEFAULT_COST_MAX_FALLBACK_USD_PER_1M),
            ),
            default=DEFAULT_COST_MAX_FALLBACK_USD_PER_1M,
            min_value=0.0,
            max_value=100.0,
        )
        cost_retry_max_retries = _to_int(
            raw_settings.get("router_cost_retry_max_retries", str(DEFAULT_COST_RETRY_MAX_RETRIES)),
            default=DEFAULT_COST_RETRY_MAX_RETRIES,
            min_value=1,
            max_value=12,
        )
        cost_retry_base_delay_sec = _to_float(
            raw_settings.get("router_cost_retry_base_delay_sec", str(DEFAULT_COST_RETRY_BASE_DELAY_SEC)),
            default=DEFAULT_COST_RETRY_BASE_DELAY_SEC,
            min_value=0.0,
            max_value=30.0,
        )
        cost_retry_max_delay_sec = _to_float(
            raw_settings.get("router_cost_retry_max_delay_sec", str(DEFAULT_COST_RETRY_MAX_DELAY_SEC)),
            default=DEFAULT_COST_RETRY_MAX_DELAY_SEC,
            min_value=0.0,
            max_value=180.0,
        )
        cost_lock_quality_provider = _to_bool(
            raw_settings.get(
                "router_cost_lock_quality_provider",
                "true" if DEFAULT_COST_LOCK_QUALITY_PROVIDER else "false",
            ),
            default=DEFAULT_COST_LOCK_QUALITY_PROVIDER,
        )
        image_engine = str(raw_settings.get("router_image_engine", DEFAULT_IMAGE_ENGINE)).strip().lower()
        if not _find_image_model(image_engine):
            image_engine = DEFAULT_IMAGE_ENGINE
        image_ai_engine = str(
            raw_settings.get("router_image_ai_engine", image_engine or DEFAULT_IMAGE_AI_ENGINE)
        ).strip().lower()
        if not _find_image_model(image_ai_engine):
            image_ai_engine = image_engine if _find_image_model(image_engine) else DEFAULT_IMAGE_AI_ENGINE
        image_ai_quota = normalize_image_ai_quota(
            raw_settings.get("router_image_ai_quota", DEFAULT_IMAGE_AI_QUOTA),
            default=DEFAULT_IMAGE_AI_QUOTA,
        )
        parsed_topic_overrides = _parse_json_map(
            raw_settings.get("router_image_topic_quota_overrides", "")
        )
        image_topic_quota_overrides = {
            key: normalize_image_ai_quota(value)
            for key, value in parsed_topic_overrides.items()
            if str(key).strip()
        }
        if not image_topic_quota_overrides:
            image_topic_quota_overrides = dict(DEFAULT_IMAGE_TOPIC_QUOTA_OVERRIDES)
        image_enabled = _to_bool(raw_settings.get("router_image_enabled", "true"), default=True)
        traffic_feedback_strong_mode = _to_bool(
            raw_settings.get("router_traffic_feedback_strong_mode", "false"),
            default=DEFAULT_TRAFFIC_FEEDBACK_STRONG_MODE,
        )
        images_per_post = _to_int(
            raw_settings.get("router_images_per_post", str(DEFAULT_IMAGES_PER_POST)),
            default=DEFAULT_IMAGES_PER_POST,
            min_value=0,
            max_value=4,
        )
        images_per_post_min = _to_int(
            raw_settings.get("router_images_per_post_min", str(DEFAULT_IMAGES_PER_POST_MIN)),
            default=DEFAULT_IMAGES_PER_POST_MIN,
            min_value=0,
            max_value=4,
        )
        images_per_post_max = _to_int(
            raw_settings.get("router_images_per_post_max", str(DEFAULT_IMAGES_PER_POST_MAX)),
            default=DEFAULT_IMAGES_PER_POST_MAX,
            min_value=0,
            max_value=4,
        )
        # min > max 방어
        if images_per_post_min > images_per_post_max:
            images_per_post_min = images_per_post_max

        eval_model_today = str(raw_settings.get("router_eval_model_today", "")).strip()
        eval_last_run_date = str(raw_settings.get("router_eval_last_run_date", "")).strip()
        try:
            eval_min_samples = max(1, int(raw_settings.get("router_eval_min_samples", "5") or "5"))
        except ValueError:
            eval_min_samples = 5
        try:
            champion_switch_threshold = max(
                0.0,
                float(raw_settings.get("router_champion_switch_threshold", "2.0") or "2.0"),
            )
        except ValueError:
            champion_switch_threshold = 2.0
        registered_models_raw = raw_settings.get("router_registered_models", "[]")
        try:
            registered_models = json.loads(registered_models_raw) if registered_models_raw else []
            if not isinstance(registered_models, list):
                registered_models = []
        except Exception:
            registered_models = []
        champion_model = str(raw_settings.get("router_champion_model", "")).strip()
        vlm_enabled = _to_bool(raw_settings.get("router_vlm_enabled", "false"), default=False)
        vlm_model = str(raw_settings.get("router_vlm_model", constants.VLM_DEFAULT_MODEL)).strip()
        if not vlm_model:
            vlm_model = constants.VLM_DEFAULT_MODEL
        vlm_strategy_mode = normalize_vlm_strategy_setting(
            raw_settings.get("router_vlm_strategy_mode", constants.VLM_DEFAULT_STRATEGY_MODE),
            default=constants.VLM_DEFAULT_STRATEGY_MODE,
        )
        vlm_eval_sampling_rate = _to_float(
            raw_settings.get("router_vlm_eval_sampling_rate", str(constants.VLM_DEFAULT_EVAL_SAMPLING_RATE)),
            default=constants.VLM_DEFAULT_EVAL_SAMPLING_RATE,
            min_value=0.0,
            max_value=1.0,
        )
        vlm_quality_floor = _to_float(
            raw_settings.get("router_vlm_quality_floor", str(constants.VLM_DEFAULT_QUALITY_FLOOR)),
            default=constants.VLM_DEFAULT_QUALITY_FLOOR,
            min_value=0.0,
            max_value=100.0,
        )
        vlm_max_cost_guard_krw = _to_float(
            raw_settings.get("router_vlm_max_cost_guard_krw", str(constants.VLM_DEFAULT_MAX_COST_GUARD_KRW)),
            default=constants.VLM_DEFAULT_MAX_COST_GUARD_KRW,
            min_value=0.0,
            max_value=100000.0,
        )
        fallback_category = str(raw_settings.get("fallback_category", "")).strip() or DEFAULT_FALLBACK_CATEGORY

        return {
            "strategy_mode": strategy_mode,
            "text_api_keys": text_api_keys,
            "image_api_keys": image_api_keys,
            "cost_strict_mode": cost_strict_mode,
            "cost_free_only_fallback": cost_free_only_fallback,
            "cost_max_fallback_usd_per_1m": cost_max_fallback_usd_per_1m,
            "cost_retry_max_retries": cost_retry_max_retries,
            "cost_retry_base_delay_sec": cost_retry_base_delay_sec,
            "cost_retry_max_delay_sec": cost_retry_max_delay_sec,
            "cost_lock_quality_provider": cost_lock_quality_provider,
            "image_engine": image_engine,
            "image_ai_engine": image_ai_engine,
            "image_ai_quota": image_ai_quota,
            "image_topic_quota_overrides": image_topic_quota_overrides,
            "traffic_feedback_strong_mode": traffic_feedback_strong_mode,
            "image_enabled": image_enabled,
            "images_per_post": images_per_post,
            "images_per_post_min": images_per_post_min,
            "images_per_post_max": images_per_post_max,
            "eval_model_today": eval_model_today,
            "eval_last_run_date": eval_last_run_date,
            "eval_min_samples": eval_min_samples,
            "champion_switch_threshold": champion_switch_threshold,
            "registered_models": registered_models,
            "champion_model": champion_model,
            "vlm_enabled": vlm_enabled,
            "vlm_model": vlm_model,
            "vlm_strategy_mode": vlm_strategy_mode,
            "vlm_eval_sampling_rate": vlm_eval_sampling_rate,
            "vlm_quality_floor": vlm_quality_floor,
            "vlm_max_cost_guard_krw": vlm_max_cost_guard_krw,
            "fallback_category": fallback_category,
        }

    def save_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """라우팅 설정을 저장하고 정규화된 결과를 반환한다."""
        current = self.get_saved_settings()
        text_keys = dict(current["text_api_keys"])
        image_keys = dict(current["image_api_keys"])

        for key, value in dict(payload.get("text_api_keys", {})).items():
            normalized_key = str(key).strip().lower()
            if normalized_key in DEFAULT_TEXT_KEYS:
                normalized_value = str(value or "").strip()
                if normalized_value:
                    text_keys[normalized_key] = normalized_value

        for key, value in dict(payload.get("image_api_keys", {})).items():
            normalized_key = str(key).strip().lower()
            if normalized_key in DEFAULT_IMAGE_KEYS:
                normalized_value = str(value or "").strip()
                if normalized_value:
                    image_keys[normalized_key] = normalized_value

        strategy_mode = normalize_strategy_mode(
            str(payload.get("strategy_mode", current["strategy_mode"])).strip()
        )
        cost_strict_mode = _to_bool(
            payload.get("cost_strict_mode", current.get("cost_strict_mode", DEFAULT_COST_STRICT_MODE)),
            default=bool(current.get("cost_strict_mode", DEFAULT_COST_STRICT_MODE)),
        )
        cost_free_only_fallback = _to_bool(
            payload.get(
                "cost_free_only_fallback",
                current.get("cost_free_only_fallback", DEFAULT_COST_FREE_ONLY_FALLBACK),
            ),
            default=bool(current.get("cost_free_only_fallback", DEFAULT_COST_FREE_ONLY_FALLBACK)),
        )
        cost_max_fallback_usd_per_1m = _to_float(
            payload.get(
                "cost_max_fallback_usd_per_1m",
                current.get("cost_max_fallback_usd_per_1m", DEFAULT_COST_MAX_FALLBACK_USD_PER_1M),
            ),
            default=float(current.get("cost_max_fallback_usd_per_1m", DEFAULT_COST_MAX_FALLBACK_USD_PER_1M)),
            min_value=0.0,
            max_value=100.0,
        )
        cost_retry_max_retries = _to_int(
            payload.get(
                "cost_retry_max_retries",
                current.get("cost_retry_max_retries", DEFAULT_COST_RETRY_MAX_RETRIES),
            ),
            default=DEFAULT_COST_RETRY_MAX_RETRIES,
            min_value=1,
            max_value=12,
        )
        cost_retry_base_delay_sec = _to_float(
            payload.get(
                "cost_retry_base_delay_sec",
                current.get("cost_retry_base_delay_sec", DEFAULT_COST_RETRY_BASE_DELAY_SEC),
            ),
            default=float(current.get("cost_retry_base_delay_sec", DEFAULT_COST_RETRY_BASE_DELAY_SEC)),
            min_value=0.0,
            max_value=30.0,
        )
        cost_retry_max_delay_sec = _to_float(
            payload.get(
                "cost_retry_max_delay_sec",
                current.get("cost_retry_max_delay_sec", DEFAULT_COST_RETRY_MAX_DELAY_SEC),
            ),
            default=float(current.get("cost_retry_max_delay_sec", DEFAULT_COST_RETRY_MAX_DELAY_SEC)),
            min_value=0.0,
            max_value=180.0,
        )
        cost_lock_quality_provider = _to_bool(
            payload.get(
                "cost_lock_quality_provider",
                current.get("cost_lock_quality_provider", DEFAULT_COST_LOCK_QUALITY_PROVIDER),
            ),
            default=bool(current.get("cost_lock_quality_provider", DEFAULT_COST_LOCK_QUALITY_PROVIDER)),
        )
        image_engine = str(payload.get("image_engine", current["image_engine"])).strip().lower()
        if not _find_image_model(image_engine):
            image_engine = current["image_engine"]
        has_explicit_ai_engine = "image_ai_engine" in payload
        image_ai_engine = str(
            payload.get(
                "image_ai_engine",
                image_engine if not has_explicit_ai_engine else current.get("image_ai_engine", image_engine),
            )
        ).strip().lower()
        if not _find_image_model(image_ai_engine):
            image_ai_engine = image_engine if not has_explicit_ai_engine else current.get("image_ai_engine", image_engine)
        image_ai_quota = normalize_image_ai_quota(
            payload.get("image_ai_quota", current.get("image_ai_quota", DEFAULT_IMAGE_AI_QUOTA)),
            default=current.get("image_ai_quota", DEFAULT_IMAGE_AI_QUOTA),
        )
        raw_topic_quota_overrides = payload.get(
            "image_topic_quota_overrides",
            current.get("image_topic_quota_overrides", {}),
        )
        topic_quota_overrides: Dict[str, str] = {}
        if isinstance(raw_topic_quota_overrides, dict):
            for key, value in raw_topic_quota_overrides.items():
                normalized_key = str(key).strip().lower()
                if not normalized_key:
                    continue
                topic_quota_overrides[normalized_key] = normalize_image_ai_quota(value)
        image_enabled = _to_bool(payload.get("image_enabled", current["image_enabled"]), default=True)
        traffic_feedback_strong_mode = _to_bool(
            payload.get("traffic_feedback_strong_mode", current.get("traffic_feedback_strong_mode", False)),
            default=current.get("traffic_feedback_strong_mode", False),
        )
        images_per_post_min = _to_int(
            payload.get("images_per_post_min", current.get("images_per_post_min", DEFAULT_IMAGES_PER_POST_MIN)),
            default=DEFAULT_IMAGES_PER_POST_MIN,
            min_value=0,
            max_value=4,
        )
        images_per_post_max = _to_int(
            payload.get("images_per_post_max", current.get("images_per_post_max", DEFAULT_IMAGES_PER_POST_MAX)),
            default=DEFAULT_IMAGES_PER_POST_MAX,
            min_value=0,
            max_value=4,
        )
        if images_per_post_min > images_per_post_max:
            images_per_post_min = images_per_post_max
        # 후방 호환: images_per_post는 max로 유지
        images_per_post = images_per_post_max

        normalized = {
            "strategy_mode": strategy_mode,
            "text_api_keys": text_keys,
            "image_api_keys": image_keys,
            "cost_strict_mode": cost_strict_mode,
            "cost_free_only_fallback": cost_free_only_fallback,
            "cost_max_fallback_usd_per_1m": cost_max_fallback_usd_per_1m,
            "cost_retry_max_retries": cost_retry_max_retries,
            "cost_retry_base_delay_sec": cost_retry_base_delay_sec,
            "cost_retry_max_delay_sec": cost_retry_max_delay_sec,
            "cost_lock_quality_provider": cost_lock_quality_provider,
            "image_engine": image_engine,
            "image_ai_engine": image_ai_engine,
            "image_ai_quota": image_ai_quota,
            "image_topic_quota_overrides": topic_quota_overrides,
            "traffic_feedback_strong_mode": traffic_feedback_strong_mode,
            "image_enabled": image_enabled,
            "images_per_post": images_per_post,
            "images_per_post_min": images_per_post_min,
            "images_per_post_max": images_per_post_max,
            "vlm_enabled": _to_bool(
                payload.get("vlm_enabled", current.get("vlm_enabled", False)),
                default=bool(current.get("vlm_enabled", False)),
            ),
            "vlm_model": str(
                payload.get("vlm_model", current.get("vlm_model", constants.VLM_DEFAULT_MODEL))
            ).strip()
            or str(current.get("vlm_model", constants.VLM_DEFAULT_MODEL)),
            "vlm_strategy_mode": normalize_vlm_strategy_setting(
                payload.get("vlm_strategy_mode", current.get("vlm_strategy_mode", constants.VLM_DEFAULT_STRATEGY_MODE)),
                default=current.get("vlm_strategy_mode", constants.VLM_DEFAULT_STRATEGY_MODE),
            ),
            "vlm_eval_sampling_rate": _to_float(
                payload.get(
                    "vlm_eval_sampling_rate",
                    current.get("vlm_eval_sampling_rate", constants.VLM_DEFAULT_EVAL_SAMPLING_RATE),
                ),
                default=float(current.get("vlm_eval_sampling_rate", constants.VLM_DEFAULT_EVAL_SAMPLING_RATE)),
                min_value=0.0,
                max_value=1.0,
            ),
            "vlm_quality_floor": _to_float(
                payload.get("vlm_quality_floor", current.get("vlm_quality_floor", constants.VLM_DEFAULT_QUALITY_FLOOR)),
                default=float(current.get("vlm_quality_floor", constants.VLM_DEFAULT_QUALITY_FLOOR)),
                min_value=0.0,
                max_value=100.0,
            ),
            "vlm_max_cost_guard_krw": _to_float(
                payload.get(
                    "vlm_max_cost_guard_krw",
                    current.get("vlm_max_cost_guard_krw", constants.VLM_DEFAULT_MAX_COST_GUARD_KRW),
                ),
                default=float(current.get("vlm_max_cost_guard_krw", constants.VLM_DEFAULT_MAX_COST_GUARD_KRW)),
                min_value=0.0,
                max_value=100000.0,
            ),
        }
        # challenger_model: 빈 문자열이면 기존 값 유지, 값이 있으면 저장
        challenger_model_raw = str(payload.get("challenger_model", "")).strip()
        if challenger_model_raw:
            normalized["challenger_model"] = challenger_model_raw
        else:
            normalized["challenger_model"] = str(current.get("challenger_model", "")).strip()

        if self.job_store:
            self.job_store.set_system_setting("router_strategy_mode", strategy_mode)
            self.job_store.set_system_setting("router_text_api_keys", _json_text(text_keys))
            self.job_store.set_system_setting("router_image_api_keys", _json_text(image_keys))
            self.job_store.set_system_setting("router_cost_strict_mode", "true" if cost_strict_mode else "false")
            self.job_store.set_system_setting(
                "router_cost_free_only_fallback",
                "true" if cost_free_only_fallback else "false",
            )
            self.job_store.set_system_setting(
                "router_cost_max_fallback_usd_per_1m",
                str(cost_max_fallback_usd_per_1m),
            )
            self.job_store.set_system_setting("router_cost_retry_max_retries", str(cost_retry_max_retries))
            self.job_store.set_system_setting("router_cost_retry_base_delay_sec", str(cost_retry_base_delay_sec))
            self.job_store.set_system_setting("router_cost_retry_max_delay_sec", str(cost_retry_max_delay_sec))
            self.job_store.set_system_setting(
                "router_cost_lock_quality_provider",
                "true" if cost_lock_quality_provider else "false",
            )
            self.job_store.set_system_setting("router_image_engine", image_engine)
            self.job_store.set_system_setting("router_image_ai_engine", image_ai_engine)
            self.job_store.set_system_setting("router_image_ai_quota", image_ai_quota)
            self.job_store.set_system_setting(
                "router_image_topic_quota_overrides",
                _json_text(topic_quota_overrides),
            )
            self.job_store.set_system_setting(
                "router_traffic_feedback_strong_mode",
                "true" if traffic_feedback_strong_mode else "false",
            )
            self.job_store.set_system_setting("router_image_enabled", "true" if image_enabled else "false")
            self.job_store.set_system_setting("router_images_per_post", str(images_per_post))
            self.job_store.set_system_setting("router_images_per_post_min", str(images_per_post_min))
            self.job_store.set_system_setting("router_images_per_post_max", str(images_per_post_max))
            self.job_store.set_system_setting(
                "router_vlm_enabled",
                "true" if bool(normalized.get("vlm_enabled", False)) else "false",
            )
            self.job_store.set_system_setting(
                "router_vlm_model",
                str(normalized.get("vlm_model", constants.VLM_DEFAULT_MODEL)),
            )
            self.job_store.set_system_setting(
                "router_vlm_strategy_mode",
                str(normalized.get("vlm_strategy_mode", constants.VLM_DEFAULT_STRATEGY_MODE)),
            )
            self.job_store.set_system_setting(
                "router_vlm_eval_sampling_rate",
                str(normalized.get("vlm_eval_sampling_rate", constants.VLM_DEFAULT_EVAL_SAMPLING_RATE)),
            )
            self.job_store.set_system_setting(
                "router_vlm_quality_floor",
                str(normalized.get("vlm_quality_floor", constants.VLM_DEFAULT_QUALITY_FLOOR)),
            )
            self.job_store.set_system_setting(
                "router_vlm_max_cost_guard_krw",
                str(normalized.get("vlm_max_cost_guard_krw", constants.VLM_DEFAULT_MAX_COST_GUARD_KRW)),
            )
            if challenger_model_raw:
                self.job_store.set_system_setting("router_challenger_model", challenger_model_raw)
            # TEXT_MODEL_MATRIX에서 사용 가능한 모델을 registered 목록에 병합한다.
            self._sync_registered_models(text_keys)

        return normalized

    def _sync_registered_models(self, text_api_keys: Dict[str, str]) -> None:
        """TEXT_MODEL_MATRIX + 설정 키 기준으로 registered_models를 병합 동기화한다.

        규칙:
        - 키가 있는 provider의 모델만 자동 추가
        - 기존 active 상태는 유지 (운영자 제어 우선)
        - 키가 없어진 모델은 자동 제거하지 않음 (이력 보존)
        """
        if not self.job_store:
            return

        raw = self.job_store.get_system_setting("router_registered_models", "[]")
        try:
            existing = json.loads(raw) if raw else []
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []

        # 기존 등록값을 정규화하여 index를 만든다.
        by_normalized_id: Dict[str, Dict[str, Any]] = {}
        ordered: List[Dict[str, Any]] = []
        for item in existing:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("model_id", "")).strip()
            if not model_id:
                continue
            normalized_id = model_id.split(":", 1)[1].strip().lower() if ":" in model_id else model_id.lower()
            if not normalized_id or normalized_id in by_normalized_id:
                continue
            entry = {
                "model_id": model_id,
                "provider": str(item.get("provider", "")).strip().lower(),
                "active": bool(item.get("active", True)),
            }
            by_normalized_id[normalized_id] = entry
            ordered.append(entry)

        changed = False
        for spec in TEXT_MODEL_MATRIX:
            if not str(text_api_keys.get(spec.key_id, "")).strip():
                continue
            normalized_id = spec.model.strip().lower()
            if not normalized_id or normalized_id in by_normalized_id:
                continue
            entry = {
                "model_id": spec.model,
                "provider": spec.provider,
                "active": True,
            }
            by_normalized_id[normalized_id] = entry
            ordered.append(entry)
            changed = True

        if changed:
            self.job_store.set_system_setting(
                "router_registered_models",
                json.dumps(ordered, ensure_ascii=False),
            )

    def build_plan(self, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """설정 기반 역할별 모델 배정과 견적 결과를 생성한다."""
        base = self.get_saved_settings()
        if overrides:
            base = self._merge_preview_settings(base, overrides)

        strategy_mode = normalize_strategy_mode(base["strategy_mode"])
        cost_strict_mode = bool(base.get("cost_strict_mode", DEFAULT_COST_STRICT_MODE))
        cost_free_only_fallback = bool(base.get("cost_free_only_fallback", DEFAULT_COST_FREE_ONLY_FALLBACK))
        cost_max_fallback_usd_per_1m = float(
            base.get("cost_max_fallback_usd_per_1m", DEFAULT_COST_MAX_FALLBACK_USD_PER_1M)
        )
        text_api_keys = dict(base["text_api_keys"])
        image_api_keys = dict(base["image_api_keys"])
        image_enabled = bool(base["image_enabled"])
        image_engine = str(base["image_engine"]).lower().strip()
        image_ai_engine = str(base.get("image_ai_engine", image_engine)).lower().strip()
        image_ai_quota = normalize_image_ai_quota(base.get("image_ai_quota", DEFAULT_IMAGE_AI_QUOTA))
        images_per_post = _to_int(base["images_per_post"], DEFAULT_IMAGES_PER_POST, 0, 4)
        images_per_post_min = _to_int(
            base.get("images_per_post_min", DEFAULT_IMAGES_PER_POST_MIN),
            default=DEFAULT_IMAGES_PER_POST_MIN,
            min_value=0,
            max_value=4,
        )

        available_text_models = self._available_text_specs(
            text_api_keys=text_api_keys,
            registered_models=list(base.get("registered_models", [])),
        )

        parser_spec = self._pick_role_model(available_text_models, strategy_mode, role="parser")
        quality_spec = self._pick_role_model(available_text_models, strategy_mode, role="quality_step")
        voice_spec = self._pick_role_model(available_text_models, strategy_mode, role="voice_step")

        quality_fallbacks = self._build_fallback_candidates(
            selected=quality_spec,
            pool=available_text_models,
            strategy_mode=strategy_mode,
            max_size=4,
            cost_strict_mode=cost_strict_mode,
            cost_free_only_fallback=cost_free_only_fallback,
            cost_max_fallback_usd_per_1m=cost_max_fallback_usd_per_1m,
        )
        voice_fallbacks = self._build_fallback_candidates(
            selected=voice_spec,
            pool=available_text_models,
            strategy_mode=strategy_mode,
            max_size=4,
            cost_strict_mode=cost_strict_mode,
            cost_free_only_fallback=cost_free_only_fallback,
            cost_max_fallback_usd_per_1m=cost_max_fallback_usd_per_1m,
        )

        parser_role_payload = self._role_payload(parser_spec, strategy_mode, "parser")
        role_payload = {
            "parser": parser_role_payload,
            # 저가 역할은 parser와 같은 모델을 사용한다.
            "pre_analysis": self._role_payload(parser_spec, strategy_mode, "pre_analysis"),
            "quality_step": self._role_payload(quality_spec, strategy_mode, "quality_step"),
            "voice_step": self._role_payload(voice_spec, strategy_mode, "voice_step"),
            "sentence_polish": self._role_payload(parser_spec, strategy_mode, "sentence_polish"),
        }
        role_payload["quality_step"]["fallback_chain"] = [
            self._model_payload(spec) for spec in quality_fallbacks
        ]
        role_payload["voice_step"]["fallback_chain"] = [
            self._model_payload(spec) for spec in voice_fallbacks
        ]
        role_payload["pre_analysis"]["fallback_chain"] = list(parser_role_payload.get("fallback_chain", []))
        role_payload["sentence_polish"]["fallback_chain"] = list(parser_role_payload.get("fallback_chain", []))

        image_spec = _find_image_model(image_engine) or _find_image_model(DEFAULT_IMAGE_ENGINE)
        image_ai_spec = _find_image_model(image_ai_engine) or _find_image_model(DEFAULT_IMAGE_AI_ENGINE)
        image_key_ok = bool(image_spec and str(image_api_keys.get(image_spec.key_id, "")).strip())
        if image_spec and image_spec.key_id == "openai_image":
            # DALL-E는 OpenAI 텍스트 키를 공유하므로 text key도 허용한다.
            image_key_ok = image_key_ok or bool(str(text_api_keys.get("openai", "")).strip())
        image_ai_key_ok = bool(image_ai_spec and str(image_api_keys.get(image_ai_spec.key_id, "")).strip())
        if image_ai_spec and image_ai_spec.key_id == "openai_image":
            image_ai_key_ok = image_ai_key_ok or bool(str(text_api_keys.get("openai", "")).strip())
        image_usable = bool(
            image_enabled
            and (
                (image_spec and image_key_ok)
                or (image_ai_spec and image_ai_key_ok)
            )
        )

        estimate = self._estimate(
            parser_spec=parser_spec,
            quality_spec=quality_spec,
            voice_spec=voice_spec,
            image_ai_spec=image_ai_spec if (image_usable and image_ai_key_ok) else None,
            image_ai_quota=image_ai_quota,
            images_per_post=images_per_post,
            images_per_post_min=images_per_post_min,
        )

        return {
            "strategy_mode": strategy_mode,
            "roles": role_payload,
            "estimate": estimate,
            "image": {
                "enabled": image_enabled,
                "engine": image_spec.engine_id if image_spec else DEFAULT_IMAGE_ENGINE,
                "engine_label": image_spec.label if image_spec else "",
                "ai_engine": image_ai_spec.engine_id if image_ai_spec else DEFAULT_IMAGE_AI_ENGINE,
                "ai_engine_label": image_ai_spec.label if image_ai_spec else "",
                "ai_quota": image_ai_quota,
                "images_per_post": images_per_post,
                "available": image_usable,
            },
            "available_text_models": [self._model_payload(item) for item in available_text_models],
        }

    def build_parser_chain(self, overrides: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """파서 역할에 사용할 모델 체인을 반환한다."""
        base = self.build_plan(overrides=overrides)
        parser_role = base["roles"]["parser"]
        chain: List[Dict[str, Any]] = []
        parser_provider = str(parser_role.get("provider", "")).strip().lower()
        parser_model = str(parser_role.get("model", "")).strip()
        saved = self.get_saved_settings()
        text_keys = saved["text_api_keys"]
        available_specs = self._available_text_specs(
            text_api_keys=dict(text_keys),
            registered_models=list(saved.get("registered_models", [])),
        )

        if parser_provider and parser_model:
            chain.append(
                {
                    "provider": parser_provider,
                    "model": parser_model,
                    "api_key": str(text_keys.get(self._provider_to_key_id(parser_provider), "")).strip(),
                }
            )

        # 파서는 speed/비용 중심으로 보조 체인을 추가한다.
        for spec in sorted(available_specs, key=lambda item: (-item.speed_score, item.avg_cost_per_1k_usd)):
            api_key = str(text_keys.get(spec.key_id, "")).strip()
            if not api_key:
                continue
            if any(
                item["provider"] == spec.provider and item["model"] == spec.model
                for item in chain
            ):
                continue
            chain.append(
                {
                    "provider": spec.provider,
                    "model": spec.model,
                    "api_key": api_key,
                }
            )
        return chain

    def build_generation_plan(self, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """품질/보이스 단계용 실행 계획을 반환한다."""
        planned = self.build_plan(overrides=overrides)
        saved = self.get_saved_settings()
        text_keys = saved["text_api_keys"]
        parser = planned["roles"]["parser"]
        quality = planned["roles"]["quality_step"]
        voice = planned["roles"]["voice_step"]
        selected_slot_type = "default"

        def role_to_runtime(role_payload: Dict[str, Any]) -> Dict[str, Any]:
            provider = str(role_payload.get("provider", "")).strip().lower()
            model = str(role_payload.get("model", "")).strip()
            return {
                "provider": provider,
                "model": model,
                "temperature": float(role_payload.get("temperature", 0.6)),
                "api_key": str(text_keys.get(self._provider_to_key_id(provider), "")).strip(),
                "fallback_chain": [
                    {
                        "provider": str(item.get("provider", "")).strip().lower(),
                        "model": str(item.get("model", "")).strip(),
                        "api_key": str(
                            text_keys.get(self._provider_to_key_id(str(item.get("provider", ""))), "")
                        ).strip(),
                    }
                    for item in list(role_payload.get("fallback_chain", []))
                ],
            }

        return {
            "strategy_mode": planned["strategy_mode"],
            "parser_step": role_to_runtime(parser),
            "quality_step": role_to_runtime(quality),
            "voice_step": role_to_runtime(voice),
            "cost_controls": {
                "strict_mode": bool(saved.get("cost_strict_mode", DEFAULT_COST_STRICT_MODE)),
                "free_only_fallback": bool(
                    saved.get("cost_free_only_fallback", DEFAULT_COST_FREE_ONLY_FALLBACK)
                ),
                "max_fallback_usd_per_1m": float(
                    saved.get("cost_max_fallback_usd_per_1m", DEFAULT_COST_MAX_FALLBACK_USD_PER_1M)
                ),
                "retry_max_retries": int(saved.get("cost_retry_max_retries", DEFAULT_COST_RETRY_MAX_RETRIES)),
                "retry_base_delay_sec": float(
                    saved.get("cost_retry_base_delay_sec", DEFAULT_COST_RETRY_BASE_DELAY_SEC)
                ),
                "retry_max_delay_sec": float(
                    saved.get("cost_retry_max_delay_sec", DEFAULT_COST_RETRY_MAX_DELAY_SEC)
                ),
                "lock_quality_provider": bool(
                    saved.get("cost_lock_quality_provider", DEFAULT_COST_LOCK_QUALITY_PROVIDER)
                ),
            },
            "estimate": planned["estimate"],
            "competition": self.get_competition_state(slot_type=selected_slot_type),
        }

    def build_generation_plan_for_job(
        self,
        *,
        job: "Job",
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """작업 컨텍스트를 반영한 품질/보이스 단계 실행 계획을 반환한다."""
        planned = self.build_plan(overrides=overrides)
        saved = self.get_saved_settings()
        text_keys = saved["text_api_keys"]
        available_specs = self._available_text_specs(
            text_api_keys=text_keys,
            registered_models=list(saved.get("registered_models", [])),
        )
        strategy_mode = str(planned["strategy_mode"]).strip().lower()

        parser_role = planned["roles"]["parser"]
        quality_role = dict(planned["roles"]["quality_step"])
        current_quality_spec = _find_text_model(
            str(quality_role.get("provider", "")),
            str(quality_role.get("model", "")),
        )
        selected_quality_spec, selected_slot_type = self._resolve_competition_quality_spec(
            job=job,
            base_spec=current_quality_spec,
            available_specs=available_specs,
            strategy_mode=strategy_mode,
            eval_model_today=saved.get("eval_model_today", ""),
            eval_min_samples=saved.get("eval_min_samples", 5),
            champion_model=saved.get("champion_model", ""),
            cost_strict_mode=bool(saved.get("cost_strict_mode", DEFAULT_COST_STRICT_MODE)),
            cost_free_only_fallback=bool(
                saved.get("cost_free_only_fallback", DEFAULT_COST_FREE_ONLY_FALLBACK)
            ),
            cost_max_fallback_usd_per_1m=float(
                saved.get("cost_max_fallback_usd_per_1m", DEFAULT_COST_MAX_FALLBACK_USD_PER_1M)
            ),
        )
        if selected_quality_spec:
            quality_role = self._role_payload(selected_quality_spec, strategy_mode, "quality_step")
            fallback_chain = self._build_fallback_candidates(
                selected=selected_quality_spec,
                pool=available_specs,
                strategy_mode=strategy_mode,
                max_size=4,
                cost_strict_mode=bool(saved.get("cost_strict_mode", DEFAULT_COST_STRICT_MODE)),
                cost_free_only_fallback=bool(
                    saved.get("cost_free_only_fallback", DEFAULT_COST_FREE_ONLY_FALLBACK)
                ),
                cost_max_fallback_usd_per_1m=float(
                    saved.get("cost_max_fallback_usd_per_1m", DEFAULT_COST_MAX_FALLBACK_USD_PER_1M)
                ),
            )
            quality_role["fallback_chain"] = [self._model_payload(spec) for spec in fallback_chain]

        voice_role = planned["roles"]["voice_step"]

        def role_to_runtime(role_payload: Dict[str, Any]) -> Dict[str, Any]:
            provider = str(role_payload.get("provider", "")).strip().lower()
            model = str(role_payload.get("model", "")).strip()
            return {
                "provider": provider,
                "model": model,
                "temperature": float(role_payload.get("temperature", 0.6)),
                "api_key": str(text_keys.get(self._provider_to_key_id(provider), "")).strip(),
                "fallback_chain": [
                    {
                        "provider": str(item.get("provider", "")).strip().lower(),
                        "model": str(item.get("model", "")).strip(),
                        "api_key": str(
                            text_keys.get(self._provider_to_key_id(str(item.get("provider", ""))), "")
                        ).strip(),
                    }
                    for item in list(role_payload.get("fallback_chain", []))
                ],
            }

        return {
            "strategy_mode": planned["strategy_mode"],
            "parser_step": role_to_runtime(parser_role),
            "quality_step": role_to_runtime(quality_role),
            "voice_step": role_to_runtime(voice_role),
            "cost_controls": {
                "strict_mode": bool(saved.get("cost_strict_mode", DEFAULT_COST_STRICT_MODE)),
                "free_only_fallback": bool(
                    saved.get("cost_free_only_fallback", DEFAULT_COST_FREE_ONLY_FALLBACK)
                ),
                "max_fallback_usd_per_1m": float(
                    saved.get("cost_max_fallback_usd_per_1m", DEFAULT_COST_MAX_FALLBACK_USD_PER_1M)
                ),
                "retry_max_retries": int(saved.get("cost_retry_max_retries", DEFAULT_COST_RETRY_MAX_RETRIES)),
                "retry_base_delay_sec": float(
                    saved.get("cost_retry_base_delay_sec", DEFAULT_COST_RETRY_BASE_DELAY_SEC)
                ),
                "retry_max_delay_sec": float(
                    saved.get("cost_retry_max_delay_sec", DEFAULT_COST_RETRY_MAX_DELAY_SEC)
                ),
                "lock_quality_provider": bool(
                    saved.get("cost_lock_quality_provider", DEFAULT_COST_LOCK_QUALITY_PROVIDER)
                ),
            },
            "estimate": planned["estimate"],
            "competition": self.get_competition_state(slot_type=selected_slot_type),
        }

    def export_for_ui(self) -> Dict[str, Any]:
        """UI 렌더링용 상태+매트릭스+현재 견적을 반환한다."""
        saved = self.get_saved_settings()
        plan = self.build_plan()
        return {
            "settings": {
                "strategy_mode": saved["strategy_mode"],
                "text_api_keys_masked": {
                    key: mask_secret(value) if value else ""
                    for key, value in saved["text_api_keys"].items()
                },
                "image_api_keys_masked": {
                    key: mask_secret(value) if value else ""
                    for key, value in saved["image_api_keys"].items()
                },
                "cost_strict_mode": bool(saved.get("cost_strict_mode", DEFAULT_COST_STRICT_MODE)),
                "cost_free_only_fallback": bool(
                    saved.get("cost_free_only_fallback", DEFAULT_COST_FREE_ONLY_FALLBACK)
                ),
                "cost_max_fallback_usd_per_1m": float(
                    saved.get("cost_max_fallback_usd_per_1m", DEFAULT_COST_MAX_FALLBACK_USD_PER_1M)
                ),
                "cost_retry_max_retries": int(saved.get("cost_retry_max_retries", DEFAULT_COST_RETRY_MAX_RETRIES)),
                "cost_retry_base_delay_sec": float(
                    saved.get("cost_retry_base_delay_sec", DEFAULT_COST_RETRY_BASE_DELAY_SEC)
                ),
                "cost_retry_max_delay_sec": float(
                    saved.get("cost_retry_max_delay_sec", DEFAULT_COST_RETRY_MAX_DELAY_SEC)
                ),
                "cost_lock_quality_provider": bool(
                    saved.get("cost_lock_quality_provider", DEFAULT_COST_LOCK_QUALITY_PROVIDER)
                ),
                "image_engine": saved["image_engine"],
                "image_ai_engine": saved.get("image_ai_engine", DEFAULT_IMAGE_AI_ENGINE),
                "image_ai_quota": saved.get("image_ai_quota", DEFAULT_IMAGE_AI_QUOTA),
                "image_topic_quota_overrides": saved.get("image_topic_quota_overrides", {}),
                "traffic_feedback_strong_mode": bool(saved.get("traffic_feedback_strong_mode", False)),
                "image_enabled": saved["image_enabled"],
                "images_per_post": saved["images_per_post"],
                "images_per_post_min": saved.get("images_per_post_min", DEFAULT_IMAGES_PER_POST_MIN),
                "images_per_post_max": saved.get("images_per_post_max", DEFAULT_IMAGES_PER_POST_MAX),
                "vlm_enabled": bool(saved.get("vlm_enabled", False)),
                "vlm_model": str(saved.get("vlm_model", constants.VLM_DEFAULT_MODEL)),
                "vlm_strategy_mode": normalize_vlm_strategy_setting(
                    saved.get("vlm_strategy_mode", constants.VLM_DEFAULT_STRATEGY_MODE),
                    default=constants.VLM_DEFAULT_STRATEGY_MODE,
                ),
                "vlm_eval_sampling_rate": float(
                    saved.get("vlm_eval_sampling_rate", constants.VLM_DEFAULT_EVAL_SAMPLING_RATE)
                ),
                "vlm_quality_floor": float(saved.get("vlm_quality_floor", constants.VLM_DEFAULT_QUALITY_FLOOR)),
                "vlm_max_cost_guard_krw": float(
                    saved.get("vlm_max_cost_guard_krw", constants.VLM_DEFAULT_MAX_COST_GUARD_KRW)
                ),
            },
            "quote": plan["estimate"],
            "roles": plan["roles"],
            "competition": self.get_competition_state(),
            "matrix": {
                "text_models": [self._model_payload(item) for item in TEXT_MODEL_MATRIX],
                "image_models": [self._image_payload(item) for item in IMAGE_MODEL_MATRIX],
                "vlm_models": self._vlm_matrix_payload(saved),
            },
        }

    def get_competition_state(self, *, slot_type: str = "default") -> Dict[str, Any]:
        """경쟁 상태를 UI/런타임 공통 포맷으로 반환한다."""
        saved = self.get_saved_settings()
        return {
            "champion_model": str(saved.get("champion_model", "")),
            "eval_model_today": str(saved.get("eval_model_today", "")),
            "registered_models": list(saved.get("registered_models", [])),
            "slot_type": slot_type,
            # 하위호환 필드
            "phase": "eval_continuous",
            "week_start": "",
            "apply_at": "",
            "shadow_mode": False,
            "challenger_model": "",
            "fallback_category": str(saved.get("fallback_category", DEFAULT_FALLBACK_CATEGORY)),
        }

    def _merge_preview_settings(
        self,
        current: Dict[str, Any],
        overrides: Dict[str, Any],
    ) -> Dict[str, Any]:
        """미리보기 요청을 현재 설정과 병합한다."""
        images_per_post_min = _to_int(
            overrides.get("images_per_post_min", current.get("images_per_post_min", DEFAULT_IMAGES_PER_POST_MIN)),
            default=DEFAULT_IMAGES_PER_POST_MIN,
            min_value=0,
            max_value=4,
        )
        images_per_post_max = _to_int(
            overrides.get("images_per_post_max", current.get("images_per_post_max", DEFAULT_IMAGES_PER_POST_MAX)),
            default=DEFAULT_IMAGES_PER_POST_MAX,
            min_value=0,
            max_value=4,
        )
        if images_per_post_min > images_per_post_max:
            images_per_post_min = images_per_post_max
        merged = {
            "strategy_mode": normalize_strategy_mode(overrides.get("strategy_mode", current["strategy_mode"])),
            "text_api_keys": dict(current["text_api_keys"]),
            "image_api_keys": dict(current["image_api_keys"]),
            "cost_strict_mode": _to_bool(
                overrides.get("cost_strict_mode", current.get("cost_strict_mode", DEFAULT_COST_STRICT_MODE)),
                default=bool(current.get("cost_strict_mode", DEFAULT_COST_STRICT_MODE)),
            ),
            "cost_free_only_fallback": _to_bool(
                overrides.get(
                    "cost_free_only_fallback",
                    current.get("cost_free_only_fallback", DEFAULT_COST_FREE_ONLY_FALLBACK),
                ),
                default=bool(current.get("cost_free_only_fallback", DEFAULT_COST_FREE_ONLY_FALLBACK)),
            ),
            "cost_max_fallback_usd_per_1m": _to_float(
                overrides.get(
                    "cost_max_fallback_usd_per_1m",
                    current.get("cost_max_fallback_usd_per_1m", DEFAULT_COST_MAX_FALLBACK_USD_PER_1M),
                ),
                default=float(current.get("cost_max_fallback_usd_per_1m", DEFAULT_COST_MAX_FALLBACK_USD_PER_1M)),
                min_value=0.0,
                max_value=100.0,
            ),
            "cost_retry_max_retries": _to_int(
                overrides.get(
                    "cost_retry_max_retries",
                    current.get("cost_retry_max_retries", DEFAULT_COST_RETRY_MAX_RETRIES),
                ),
                default=DEFAULT_COST_RETRY_MAX_RETRIES,
                min_value=1,
                max_value=12,
            ),
            "cost_retry_base_delay_sec": _to_float(
                overrides.get(
                    "cost_retry_base_delay_sec",
                    current.get("cost_retry_base_delay_sec", DEFAULT_COST_RETRY_BASE_DELAY_SEC),
                ),
                default=float(current.get("cost_retry_base_delay_sec", DEFAULT_COST_RETRY_BASE_DELAY_SEC)),
                min_value=0.0,
                max_value=30.0,
            ),
            "cost_retry_max_delay_sec": _to_float(
                overrides.get(
                    "cost_retry_max_delay_sec",
                    current.get("cost_retry_max_delay_sec", DEFAULT_COST_RETRY_MAX_DELAY_SEC),
                ),
                default=float(current.get("cost_retry_max_delay_sec", DEFAULT_COST_RETRY_MAX_DELAY_SEC)),
                min_value=0.0,
                max_value=180.0,
            ),
            "cost_lock_quality_provider": _to_bool(
                overrides.get(
                    "cost_lock_quality_provider",
                    current.get("cost_lock_quality_provider", DEFAULT_COST_LOCK_QUALITY_PROVIDER),
                ),
                default=bool(current.get("cost_lock_quality_provider", DEFAULT_COST_LOCK_QUALITY_PROVIDER)),
            ),
            "image_engine": str(overrides.get("image_engine", current["image_engine"])).strip().lower(),
            "image_ai_engine": "",
            "image_ai_quota": normalize_image_ai_quota(
                overrides.get("image_ai_quota", current.get("image_ai_quota", DEFAULT_IMAGE_AI_QUOTA)),
                default=current.get("image_ai_quota", DEFAULT_IMAGE_AI_QUOTA),
            ),
            "image_topic_quota_overrides": dict(current.get("image_topic_quota_overrides", {})),
            "traffic_feedback_strong_mode": _to_bool(
                overrides.get(
                    "traffic_feedback_strong_mode",
                    current.get("traffic_feedback_strong_mode", DEFAULT_TRAFFIC_FEEDBACK_STRONG_MODE),
                ),
                default=bool(current.get("traffic_feedback_strong_mode", DEFAULT_TRAFFIC_FEEDBACK_STRONG_MODE)),
            ),
            "image_enabled": _to_bool(overrides.get("image_enabled", current["image_enabled"]), default=True),
            "images_per_post": images_per_post_max,
            "images_per_post_min": images_per_post_min,
            "images_per_post_max": images_per_post_max,
            "vlm_enabled": _to_bool(
                overrides.get("vlm_enabled", current.get("vlm_enabled", False)),
                default=bool(current.get("vlm_enabled", False)),
            ),
            "vlm_model": str(overrides.get("vlm_model", current.get("vlm_model", constants.VLM_DEFAULT_MODEL))).strip()
            or str(current.get("vlm_model", constants.VLM_DEFAULT_MODEL)),
            "vlm_strategy_mode": normalize_vlm_strategy_setting(
                overrides.get("vlm_strategy_mode", current.get("vlm_strategy_mode", constants.VLM_DEFAULT_STRATEGY_MODE)),
                default=current.get("vlm_strategy_mode", constants.VLM_DEFAULT_STRATEGY_MODE),
            ),
            "vlm_eval_sampling_rate": _to_float(
                overrides.get(
                    "vlm_eval_sampling_rate",
                    current.get("vlm_eval_sampling_rate", constants.VLM_DEFAULT_EVAL_SAMPLING_RATE),
                ),
                default=float(current.get("vlm_eval_sampling_rate", constants.VLM_DEFAULT_EVAL_SAMPLING_RATE)),
                min_value=0.0,
                max_value=1.0,
            ),
            "vlm_quality_floor": _to_float(
                overrides.get("vlm_quality_floor", current.get("vlm_quality_floor", constants.VLM_DEFAULT_QUALITY_FLOOR)),
                default=float(current.get("vlm_quality_floor", constants.VLM_DEFAULT_QUALITY_FLOOR)),
                min_value=0.0,
                max_value=100.0,
            ),
            "vlm_max_cost_guard_krw": _to_float(
                overrides.get(
                    "vlm_max_cost_guard_krw",
                    current.get("vlm_max_cost_guard_krw", constants.VLM_DEFAULT_MAX_COST_GUARD_KRW),
                ),
                default=float(current.get("vlm_max_cost_guard_krw", constants.VLM_DEFAULT_MAX_COST_GUARD_KRW)),
                min_value=0.0,
                max_value=100000.0,
            ),
        }
        has_explicit_ai_engine = "image_ai_engine" in overrides
        merged["image_ai_engine"] = str(
            overrides.get(
                "image_ai_engine",
                merged["image_engine"] if not has_explicit_ai_engine else current.get("image_ai_engine", DEFAULT_IMAGE_AI_ENGINE),
            )
        ).strip().lower()
        for key, value in dict(overrides.get("text_api_keys", {})).items():
            normalized_key = str(key).strip().lower()
            if normalized_key in DEFAULT_TEXT_KEYS:
                merged["text_api_keys"][normalized_key] = str(value or "").strip()

        for key, value in dict(overrides.get("image_api_keys", {})).items():
            normalized_key = str(key).strip().lower()
            if normalized_key in DEFAULT_IMAGE_KEYS:
                merged["image_api_keys"][normalized_key] = str(value or "").strip()

        if not _find_image_model(merged["image_engine"]):
            merged["image_engine"] = current["image_engine"]
        if not _find_image_model(merged["image_ai_engine"]):
            merged["image_ai_engine"] = (
                merged["image_engine"] if not has_explicit_ai_engine else current.get("image_ai_engine", DEFAULT_IMAGE_AI_ENGINE)
            )
        raw_topic_overrides = overrides.get("image_topic_quota_overrides")
        if isinstance(raw_topic_overrides, dict):
            normalized_topic_overrides: Dict[str, str] = {}
            for key, value in raw_topic_overrides.items():
                normalized_key = str(key).strip().lower()
                if not normalized_key:
                    continue
                normalized_topic_overrides[normalized_key] = normalize_image_ai_quota(value)
            merged["image_topic_quota_overrides"] = normalized_topic_overrides
        return merged

    def _available_text_specs(
        self,
        text_api_keys: Dict[str, str],
        registered_models: Optional[List[Dict[str, Any]]] = None,
    ) -> List[TextModelSpec]:
        """현재 사용 가능한 텍스트 모델 스펙을 반환한다."""
        available_specs: List[TextModelSpec] = [
            spec for spec in TEXT_MODEL_MATRIX if str(text_api_keys.get(spec.key_id, "")).strip()
        ]
        if registered_models:
            # 운영자가 비활성(active=false)로 표시한 모델은 라우팅 후보에서 제외한다.
            available_specs = [
                spec for spec in available_specs
                if self._is_spec_enabled_by_registry(spec=spec, registered_models=registered_models)
            ]
        if available_specs:
            return available_specs

        fallback_provider = str(self.llm_config.primary_provider).strip().lower()
        fallback_model = str(self.llm_config.primary_model).strip()
        fallback_spec = _find_text_model(fallback_provider, fallback_model)
        if not fallback_spec:
            return []
        if registered_models and not self._is_spec_enabled_by_registry(
            spec=fallback_spec,
            registered_models=registered_models,
        ):
            return []
        return [fallback_spec]

    def _is_spec_enabled_by_registry(
        self,
        *,
        spec: TextModelSpec,
        registered_models: List[Dict[str, Any]],
    ) -> bool:
        """registered_models 활성 상태로 spec 사용 가능 여부를 판정한다."""
        normalized_provider = str(spec.provider).strip().lower()
        normalized_model = str(spec.model).strip().lower()
        normalized_with_provider = f"{normalized_provider}:{normalized_model}"
        normalized_suffix = normalized_model.split("/")[-1]

        matched_active: Optional[bool] = None
        matched_priority = -1
        provider_entries: List[Dict[str, Any]] = []

        for item in registered_models:
            raw_model_id = str(item.get("model_id", "")).strip().lower()
            if not raw_model_id:
                continue
            raw_provider = str(item.get("provider", "")).strip().lower()
            if raw_provider == normalized_provider:
                provider_entries.append(item)

            # provider가 지정된 엔트리는 더 높은 우선순위로 판정한다.
            priority = 2 if raw_provider else 1
            if raw_provider and raw_provider != normalized_provider:
                continue

            is_match = (
                raw_model_id == normalized_model
                or raw_model_id == normalized_with_provider
                or raw_model_id.split("/")[-1] == normalized_suffix
            )
            if not is_match:
                continue

            if priority >= matched_priority:
                matched_priority = priority
                matched_active = bool(item.get("active", True))

        if matched_active is None:
            # registered_models가 비어 있지 않다면 운영자가 명시한 allowlist로 해석한다.
            return False
        return matched_active

    def _normalize_category_name(self, value: str) -> str:
        """카테고리 비교를 위해 공백/대소문자를 정규화한다."""
        return "".join(str(value or "").lower().split())

    def _resolve_job_slot_type(
        self,
        *,
        job: "Job",
        eval_model_today: str,
        eval_min_samples: int = 1,
    ) -> str:
        """작업이 eval 슬롯인지 main 슬롯인지 판별한다."""
        del job, eval_min_samples
        if not str(eval_model_today).strip():
            return "main"
        if not self.job_store:
            return "main"
        try:
            kst = timezone(timedelta(hours=9))
            today_key = datetime.now(kst).strftime("%Y-%m-%d")
            # 오늘 이미 eval 슬롯을 1회 배정했다면, 성공 여부와 무관하게 main으로 전환한다.
            claimed_date = str(
                self.job_store.get_system_setting("router_eval_claimed_date", "")
            ).strip()
            if claimed_date == today_key:
                return "main"
            today_eval_count = self.job_store.get_today_eval_job_count(today_key)
            if int(today_eval_count) >= 1:
                return "main"
        except Exception:
            return "main"
        return "eval"

    def _mark_eval_slot_claimed(self, *, job: "Job") -> None:
        """오늘 eval 슬롯 배정 여부를 기록한다."""
        if not self.job_store:
            return
        try:
            kst = timezone(timedelta(hours=9))
            today_key = datetime.now(kst).strftime("%Y-%m-%d")
            self.job_store.set_system_setting("router_eval_claimed_date", today_key)
            self.job_store.set_system_setting("router_eval_claimed_job_id", str(job.job_id))
        except Exception:
            return

    def _find_text_model_by_model_id(
        self,
        *,
        model_id: str,
        available_specs: List[TextModelSpec],
    ) -> Optional[TextModelSpec]:
        """model_id 문자열로 사용 가능한 모델 스펙을 찾는다."""
        normalized = str(model_id or "").strip().lower()
        if not normalized:
            return None

        if ":" in normalized:
            provider_name, model_name = normalized.split(":", 1)
            for spec in available_specs:
                if spec.provider == provider_name and spec.model.lower() == model_name:
                    return spec

        for spec in available_specs:
            if spec.model.lower() == normalized:
                return spec

        # 구형 모델 ID(예: llama-4-scout...)로 저장된 값을
        # 정규 모델 ID(meta-llama/llama-4-scout...)와 호환 매칭한다.
        normalized_suffix = normalized.split("/")[-1]
        for spec in available_specs:
            spec_model = spec.model.lower()
            if spec_model.split("/")[-1] == normalized_suffix:
                return spec

        for spec in available_specs:
            candidate = f"{spec.provider}:{spec.model}".lower()
            if candidate == normalized:
                return spec
        return None

    def _resolve_competition_quality_spec(
        self,
        *,
        job: "Job",
        base_spec: Optional[TextModelSpec],
        available_specs: List[TextModelSpec],
        strategy_mode: str,
        eval_model_today: str,
        eval_min_samples: int,
        champion_model: str,
        cost_strict_mode: bool = DEFAULT_COST_STRICT_MODE,
        cost_free_only_fallback: bool = DEFAULT_COST_FREE_ONLY_FALLBACK,
        cost_max_fallback_usd_per_1m: float = DEFAULT_COST_MAX_FALLBACK_USD_PER_1M,
    ) -> Tuple[Optional[TextModelSpec], str]:
        """전략 모드와 eval 슬롯을 반영해 quality_step 모델을 결정한다."""
        slot_type = self._resolve_job_slot_type(
            job=job,
            eval_model_today=eval_model_today,
            eval_min_samples=eval_min_samples,
        )
        selected_slot_type = "main"
        selected_spec: Optional[TextModelSpec] = None

        if slot_type == "eval":
            eval_spec = self._find_text_model_by_model_id(
                model_id=eval_model_today,
                available_specs=available_specs,
            )
            if eval_spec:
                selected_spec = eval_spec
                selected_slot_type = "eval"
                # strict 정책으로 eval 모델이 걸러지면 eval 슬롯 소진 처리하지 않는다.
                coerced_eval = self._coerce_cost_strict_quality_spec(
                    preferred_spec=selected_spec,
                    available_specs=available_specs,
                    strategy_mode=strategy_mode,
                    cost_strict_mode=cost_strict_mode,
                    cost_free_only_fallback=cost_free_only_fallback,
                    cost_max_fallback_usd_per_1m=cost_max_fallback_usd_per_1m,
                )
                if coerced_eval and coerced_eval.provider == eval_spec.provider and coerced_eval.model == eval_spec.model:
                    self._mark_eval_slot_claimed(job=job)
                    return coerced_eval, selected_slot_type
                selected_spec = None
                selected_slot_type = "main"
            slot_type = "main"

        if slot_type == "main" and selected_spec is None:
            specialist = self._find_topic_specialist_model(job=job, available_specs=available_specs)
            if specialist:
                selected_spec = specialist
                selected_slot_type = "main_specialist"

        if slot_type == "main" and selected_spec is None and champion_model:
            champion_spec = self._find_text_model_by_model_id(
                model_id=champion_model,
                available_specs=available_specs,
            )
            if champion_spec:
                selected_spec = champion_spec
                selected_slot_type = "main"

        if selected_spec is None:
            selected_spec = base_spec
            selected_slot_type = "main"

        selected_spec = self._coerce_cost_strict_quality_spec(
            preferred_spec=selected_spec,
            available_specs=available_specs,
            strategy_mode=strategy_mode,
            cost_strict_mode=cost_strict_mode,
            cost_free_only_fallback=cost_free_only_fallback,
            cost_max_fallback_usd_per_1m=cost_max_fallback_usd_per_1m,
        )
        return selected_spec, selected_slot_type

    def _resolve_topic_mode_from_job(self, job: "Job") -> str:
        """작업 텍스트 문맥에서 topic_mode를 추정한다."""
        category_text = str(getattr(job, "category", "")).strip().lower()
        keywords_text = " ".join(str(item).strip().lower() for item in list(getattr(job, "seed_keywords", [])))
        merged = f"{category_text} {keywords_text}".strip()
        if any(token in merged for token in ("경제", "finance", "economy", "투자", "주식", "재테크", "금리", "환율")):
            return "finance"
        if any(token in merged for token in ("it", "개발", "코드", "ai", "자동화", "테크")):
            return "it"
        if any(token in merged for token in ("육아", "아이", "부모", "가정", "교육", "parenting")):
            return "parenting"
        if any(token in merged for token in ("건강", "의학", "의료", "운동", "수면", "식단", "health")):
            return "health"
        return "cafe"

    def _find_topic_specialist_model(
        self,
        *,
        job: "Job",
        available_specs: List[TextModelSpec],
    ) -> Optional[TextModelSpec]:
        """topic_mode 이력 10편 이상일 때 전문화 모델을 선택한다."""
        if not self.job_store:
            return None
        topic_mode = self._resolve_topic_mode_from_job(job)
        ninety_days_ago = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = self.job_store.get_model_performance_summary(
            since=ninety_days_ago,
            slot_types=["main", "eval"],
            topic_mode=topic_mode,
        )
        eligible = [item for item in summary if int(item.get("samples", 0) or 0) >= 10]
        if not eligible:
            return None
        eligible.sort(
            key=lambda x: (
                -float(x.get("avg_quality_score", 0.0)),
                -int(x.get("samples", 0)),
                float(x.get("avg_cost_won", 0.0)),
            )
        )
        best_model_id = str(eligible[0].get("model_id", "")).strip()
        return self._find_text_model_by_model_id(
            model_id=best_model_id,
            available_specs=available_specs,
        )

    def _pick_role_model(
        self,
        candidates: List[TextModelSpec],
        strategy_mode: str,
        role: str,
    ) -> Optional[TextModelSpec]:
        """역할별 우선순위로 모델을 선택한다."""
        if not candidates:
            return None

        threshold = _ROLE_MIN_QUALITY.get(role, 75)
        is_cheap_role = role in _CHEAP_ROLES
        by_cost = sorted(
            candidates,
            key=lambda item: (
                item.avg_cost_per_1k_usd,
                _free_fallback_priority(item) if self._is_free_spec(item) else len(FREE_FALLBACK_PROVIDER_RANK),
                -item.quality_score,
                -item.speed_score,
            ),
        )
        by_quality = sorted(candidates, key=lambda item: (-item.quality_score, item.avg_cost_per_1k_usd))

        if is_cheap_role:
            free_candidates = [
                item
                for item in candidates
                if item.input_cost_per_1m_usd == 0.0
                and item.output_cost_per_1m_usd == 0.0
                and item.quality_score >= threshold
            ]
            if free_candidates:
                return self._sort_free_fallback_candidates(free_candidates)[0]
            for item in by_cost:
                if item.quality_score >= threshold:
                    return item
            return by_cost[0]

        if strategy_mode == "quality":
            return by_quality[0]

        if strategy_mode == "balanced":
            quality_values = sorted([item.quality_score for item in candidates])
            percentile_index = max(0, int(len(quality_values) * 0.8) - 1)
            quality_p80 = quality_values[percentile_index] if quality_values else 0
            balanced_candidates = [
                item for item in candidates if item.quality_score >= quality_p80 and item.quality_score >= threshold
            ]
            if balanced_candidates:
                return sorted(balanced_candidates, key=lambda item: item.avg_cost_per_1k_usd)[0]
            return by_quality[0]

        for item in by_cost:
            if item.quality_score >= threshold:
                return item
        return by_cost[0]

    def _is_cost_strict_active(self, *, strategy_mode: str, cost_strict_mode: bool) -> bool:
        """가성비 strict 모드 활성 여부를 반환한다."""
        return str(strategy_mode).strip().lower() == "cost" and bool(cost_strict_mode)

    def _is_free_spec(self, spec: TextModelSpec) -> bool:
        """모델이 무료 티어인지 판정한다."""
        return float(spec.input_cost_per_1m_usd) == 0.0 and float(spec.output_cost_per_1m_usd) == 0.0

    def _spec_avg_cost_per_1m_usd(self, spec: TextModelSpec) -> float:
        """모델의 평균 단가(USD/1M)를 계산한다."""
        return (float(spec.input_cost_per_1m_usd) + float(spec.output_cost_per_1m_usd)) / 2.0

    def _filter_cost_strict_fallback_candidates(
        self,
        *,
        candidates: List[TextModelSpec],
        free_only: bool,
        max_fallback_usd_per_1m: float,
    ) -> List[TextModelSpec]:
        """가성비 strict 정책에 맞게 fallback 후보를 필터링한다."""
        if not candidates:
            return []

        free_candidates = self._sort_free_fallback_candidates(
            [item for item in candidates if self._is_free_spec(item)]
        )
        if free_candidates:
            return free_candidates

        if free_only:
            return []

        safe_cap = max(0.0, float(max_fallback_usd_per_1m or 0.0))
        low_cost_candidates = [
            item for item in candidates if self._spec_avg_cost_per_1m_usd(item) <= safe_cap
        ]
        if low_cost_candidates:
            return low_cost_candidates

        return candidates

    def _sort_free_fallback_candidates(self, candidates: List[TextModelSpec]) -> List[TextModelSpec]:
        """무료 fallback 후보를 provider당 대표 모델 하나로 정렬한다."""
        ordered = sorted(
            candidates,
            key=lambda item: (
                _free_fallback_priority(item),
                -item.quality_score,
                -item.speed_score,
                item.provider,
                item.model,
            ),
        )
        deduped: List[TextModelSpec] = []
        seen_providers: set[str] = set()
        for item in ordered:
            provider = str(item.provider or "").strip().lower()
            if provider in seen_providers:
                continue
            seen_providers.add(provider)
            deduped.append(item)
        return deduped

    def _coerce_cost_strict_quality_spec(
        self,
        *,
        preferred_spec: Optional[TextModelSpec],
        available_specs: List[TextModelSpec],
        strategy_mode: str,
        cost_strict_mode: bool,
        cost_free_only_fallback: bool,
        cost_max_fallback_usd_per_1m: float,
    ) -> Optional[TextModelSpec]:
        """가성비 strict 정책으로 quality_step 모델을 보정한다."""
        if not self._is_cost_strict_active(strategy_mode=strategy_mode, cost_strict_mode=cost_strict_mode):
            return preferred_spec

        pool = self._filter_cost_strict_fallback_candidates(
            candidates=list(available_specs),
            free_only=cost_free_only_fallback,
            max_fallback_usd_per_1m=cost_max_fallback_usd_per_1m,
        )
        if not pool:
            return preferred_spec

        if preferred_spec and any(
            item.provider == preferred_spec.provider and item.model == preferred_spec.model for item in pool
        ):
            return preferred_spec

        return self._pick_role_model(pool, "cost", role="quality_step") or preferred_spec

    def _build_fallback_candidates(
        self,
        *,
        selected: Optional[TextModelSpec],
        pool: List[TextModelSpec],
        strategy_mode: str,
        max_size: int,
        cost_strict_mode: bool = DEFAULT_COST_STRICT_MODE,
        cost_free_only_fallback: bool = DEFAULT_COST_FREE_ONLY_FALLBACK,
        cost_max_fallback_usd_per_1m: float = DEFAULT_COST_MAX_FALLBACK_USD_PER_1M,
    ) -> List[TextModelSpec]:
        """선택 모델을 제외한 fallback 후보를 계산한다."""
        if not selected:
            return []
        candidates = [
            item
            for item in pool
            if not (item.provider == selected.provider and item.model == selected.model)
        ]
        if not candidates:
            return []

        if cost_free_only_fallback:
            free_candidates = self._sort_free_fallback_candidates(
                [item for item in candidates if self._is_free_spec(item)]
            )
            return free_candidates[: max(0, max_size)]

        if strategy_mode in {"quality", "balanced"}:
            # quality/balanced 모드: 품질 근접도 우선
            # 동일 provider는 품질이 같을 때 다른 provider보다 뒤로 밀림(key=1 vs 0)
            # → provider 단위 장애(key 정지 등) 시 early escape 용이
            ordered = sorted(
                candidates,
                key=lambda item: (
                    abs(item.quality_score - selected.quality_score),
                    1 if item.provider == selected.provider else 0,
                    item.avg_cost_per_1k_usd,
                ),
            )
        else:
            # cost 모드: 비용 절대값 오름차순 (무료 → 저가 순)
            # 동일 provider도 비용 동점 시 뒤로 밀림
            ordered = sorted(
                candidates,
                key=lambda item: (
                    item.avg_cost_per_1k_usd,
                    1 if item.provider == selected.provider else 0,
                    abs(item.quality_score - selected.quality_score),
                ),
            )
            if self._is_cost_strict_active(strategy_mode=strategy_mode, cost_strict_mode=cost_strict_mode):
                ordered = self._filter_cost_strict_fallback_candidates(
                    candidates=ordered,
                    free_only=cost_free_only_fallback,
                    max_fallback_usd_per_1m=cost_max_fallback_usd_per_1m,
                )
        return ordered[: max(0, max_size)]

    def _estimate(
        self,
        *,
        parser_spec: Optional[TextModelSpec],
        quality_spec: Optional[TextModelSpec],
        voice_spec: Optional[TextModelSpec],
        image_ai_spec: Optional[ImageModelSpec],
        image_ai_quota: str,
        images_per_post: int,
        images_per_post_min: int = 0,
    ) -> Dict[str, Any]:
        """역할 배정 기반 비용/품질 추정치를 계산한다."""

        def role_cost_krw(spec: Optional[TextModelSpec], role: str) -> float:
            if not spec:
                return 0.0
            budget = TOKEN_BUDGET[role]
            input_cost = (budget["input"] / 1_000_000.0) * spec.input_cost_per_1m_usd
            output_cost = (budget["output"] / 1_000_000.0) * spec.output_cost_per_1m_usd
            return (input_cost + output_cost) * USD_TO_KRW

        parser_cost = role_cost_krw(parser_spec, "parser")
        quality_cost = role_cost_krw(quality_spec, "quality_step")
        voice_cost = role_cost_krw(voice_spec, "voice_step")

        # 추가 파이프라인 단계: quality_spec 모델로 호출되는 self_critique, SEO, 이미지 슬롯
        # 이 단계들은 내부적으로 quality_step 역할의 모델을 사용한다.
        def extra_step_cost(role: str) -> float:
            if not quality_spec:
                return 0.0
            budget = TOKEN_BUDGET[role]
            input_c = (budget["input"] / 1_000_000.0) * quality_spec.input_cost_per_1m_usd
            output_c = (budget["output"] / 1_000_000.0) * quality_spec.output_cost_per_1m_usd
            return (input_c + output_c) * USD_TO_KRW

        self_critique_cost = extra_step_cost("self_critique")
        seo_cost = extra_step_cost("seo_step")
        image_prompt_cost = extra_step_cost("image_prompt")
        
        def extra_cheap_cost(role: str) -> float:
            """저가 역할 비용을 parser 모델 기준으로 계산한다."""
            if not parser_spec:
                return 0.0
            budget = TOKEN_BUDGET.get(role)
            if not budget:
                return 0.0
            input_cost = (budget["input"] / 1_000_000.0) * parser_spec.input_cost_per_1m_usd
            output_cost = (budget["output"] / 1_000_000.0) * parser_spec.output_cost_per_1m_usd
            return (input_cost + output_cost) * USD_TO_KRW

        pre_analysis_cost = extra_cheap_cost("pre_analysis")
        sentence_polish_cost = extra_cheap_cost("sentence_polish")

        text_cost = (
            parser_cost
            + quality_cost
            + voice_cost
            + self_critique_cost
            + seo_cost
            + image_prompt_cost
            + pre_analysis_cost
            + sentence_polish_cost
        )

        def resolve_ai_count(total_images: int) -> int:
            safe_total = max(0, int(total_images))
            normalized_quota = normalize_image_ai_quota(image_ai_quota)
            if normalized_quota == "all":
                return safe_total
            try:
                return min(int(normalized_quota), safe_total)
            except (TypeError, ValueError):
                return 0

        ai_count_max = resolve_ai_count(images_per_post)
        ai_count_min = resolve_ai_count(images_per_post_min)
        stock_count_max = max(0, int(images_per_post) - ai_count_max)
        stock_count_min = max(0, int(images_per_post_min) - ai_count_min)

        ai_cost_per_unit = float(image_ai_spec.cost_per_image_krw if image_ai_spec else 0)
        image_cost_max = ai_cost_per_unit * ai_count_max
        image_cost_min = ai_cost_per_unit * ai_count_min
        image_cost = image_cost_max
        total_cost = text_cost + image_cost

        parser_quality = parser_spec.quality_score if parser_spec else 50
        quality_quality = quality_spec.quality_score if quality_spec else 55
        voice_quality = voice_spec.quality_score if voice_spec else 55
        image_quality = image_ai_spec.quality_score if image_ai_spec else 60
        quality_score = round(
            (parser_quality * 0.1) + (quality_quality * 0.55) + (voice_quality * 0.30) + (image_quality * 0.05)
        )

        # Range 비용: 최소(images_per_post_min장), 최대(images_per_post장)
        cost_min = text_cost + image_cost_min
        cost_max = text_cost + image_cost_max

        daily_posts = 8
        if self.job_store:
            try:
                raw_alloc = self.job_store.get_system_setting("scheduler_category_allocations", "[]")
                alloc_list = json.loads(raw_alloc) if raw_alloc else []
                alloc_count = 0
                if isinstance(alloc_list, list):
                    alloc_count = sum(int(item.get("count", 0)) for item in alloc_list if isinstance(item, dict))
                idea_vault_quota = int(
                    self.job_store.get_system_setting("scheduler_idea_vault_daily_quota", "0") or 0
                )
                computed = alloc_count + idea_vault_quota
                if computed > 0:
                    daily_posts = computed
            except Exception:
                pass

        monthly_cost = total_cost * daily_posts * 30
        monthly_cost_min = cost_min * daily_posts * 30
        monthly_cost_max = cost_max * daily_posts * 30

        return {
            "currency": "KRW",
            "text_cost_krw": int(round(text_cost)),
            "image_cost_krw": int(round(image_cost)),
            "total_cost_krw": int(round(total_cost)),
            "image_cost_min_krw": int(round(image_cost_min)),
            "image_cost_max_krw": int(round(image_cost_max)),
            "cost_min_krw": int(round(cost_min)),
            "cost_max_krw": int(round(cost_max)),
            "ai_image_count": ai_count_max,
            "stock_image_count": stock_count_max,
            "ai_image_count_min": ai_count_min,
            "ai_image_count_max": ai_count_max,
            "stock_image_count_min": stock_count_min,
            "stock_image_count_max": stock_count_max,
            "quality_score": max(0, min(100, quality_score)),
            "daily_posts": int(daily_posts),
            "monthly_cost_krw": int(round(monthly_cost)),
            "monthly_cost_min_krw": int(round(monthly_cost_min)),
            "monthly_cost_max_krw": int(round(monthly_cost_max)),
        }

    def _provider_to_key_id(self, provider: str) -> str:
        """provider명을 key_id로 변환한다."""
        normalized = str(provider or "").strip().lower()
        if normalized in {"qwen", "deepseek", "gemini", "openai", "claude", "groq", "cerebras", "nvidia", "zai"}:
            return normalized
        return "qwen"

    def _model_payload(self, spec: Optional[TextModelSpec]) -> Dict[str, Any]:
        """텍스트 모델 스펙을 직렬화한다."""
        if not spec:
            return {
                "provider": "",
                "model": "",
                "label": "Not Available",
                "quality_score": 0,
                "speed_score": 0,
                "avg_cost_per_1k_usd": 0.0,
            }
        return {
            "provider": spec.provider,
            "model": spec.model,
            "label": spec.label,
            "key_id": spec.key_id,
            "quality_score": spec.quality_score,
            "speed_score": spec.speed_score,
            "avg_cost_per_1k_usd": round(spec.avg_cost_per_1k_usd, 6),
            "input_cost_per_1m_usd": spec.input_cost_per_1m_usd,
            "output_cost_per_1m_usd": spec.output_cost_per_1m_usd,
        }

    def _role_payload(
        self,
        spec: Optional[TextModelSpec],
        strategy_mode: str,
        role: str,
    ) -> Dict[str, Any]:
        """역할 배정 payload를 생성한다."""
        payload = self._model_payload(spec)
        payload["role"] = role
        payload["temperature"] = _role_temperature(strategy_mode, role)
        return payload

    def _image_payload(self, spec: ImageModelSpec) -> Dict[str, Any]:
        """이미지 엔진 스펙을 직렬화한다."""
        return {
            "engine_id": spec.engine_id,
            "label": spec.label,
            "key_id": spec.key_id,
            "cost_per_image_krw": spec.cost_per_image_krw,
            "quality_score": spec.quality_score,
            "category": spec.category,
        }

    def _vlm_matrix_payload(self, saved_settings: Dict[str, Any]) -> List[Dict[str, Any]]:
        """VLM 카탈로그(우선) 또는 static matrix(폴백)를 UI 포맷으로 반환한다."""
        text_keys = dict(saved_settings.get("text_api_keys", {}))
        usd_to_krw = self._resolve_vlm_usd_to_krw()
        rows: List[Dict[str, Any]] = []

        list_fn = getattr(self.job_store, "list_vlm_catalog_entries", None) if self.job_store else None
        if callable(list_fn):
            try:
                rows = list_fn(limit=1000)
            except Exception:
                rows = []

        if rows:
            payload: List[Dict[str, Any]] = []
            for row in rows:
                provider = str(row.get("provider", "")).strip().lower()
                model = str(row.get("model", "")).strip()
                key_id = str(row.get("key_id", provider)).strip().lower() or provider
                if not provider or not model:
                    continue
                currency = str(row.get("currency", "USD") or "USD").strip().upper()
                input_cost_per_1m = float(row.get("input_cost_per_1m", 0.0) or 0.0)
                output_cost_per_1m = float(row.get("output_cost_per_1m", 0.0) or 0.0)
                payload.append(
                    {
                        "provider": provider,
                        "client_provider": str(row.get("client_provider", f"{provider}_vlm")).strip().lower(),
                        "model": model,
                        "label": str(row.get("label", model)).strip() or model,
                        "key_id": key_id,
                        "status": str(row.get("status", "discovered")).strip().lower(),
                        "supports_image": bool(row.get("supports_image", True)),
                        "include_in_competition": bool(row.get("include_in_competition", False)),
                        "quality_score": float(row.get("quality_score", 0.0) or 0.0),
                        "reliability_score": float(row.get("reliability_score", 0.0) or 0.0),
                        "scoring_bias_offset": float(row.get("scoring_bias_offset", 0.0) or 0.0),
                        "input_cost_per_1m": input_cost_per_1m,
                        "output_cost_per_1m": output_cost_per_1m,
                        "currency": currency,
                        "estimated_cost_krw": self._estimate_vlm_cost_krw(
                            input_cost_per_1m=input_cost_per_1m,
                            output_cost_per_1m=output_cost_per_1m,
                            currency=currency,
                            usd_to_krw=usd_to_krw,
                        ),
                        "key_configured": bool(str(text_keys.get(key_id, "")).strip()),
                        "source": "catalog",
                    }
                )
            if payload:
                return payload

        fallback_payload: List[Dict[str, Any]] = []
        for spec in VLM_MODEL_MATRIX:
            fallback_payload.append(
                {
                    "provider": spec.provider,
                    "client_provider": spec.client_provider,
                    "model": spec.model,
                    "label": spec.label,
                    "key_id": spec.key_id,
                    "status": "static",
                    "supports_image": bool(spec.supports_image),
                    "include_in_competition": False,
                    "quality_score": float(spec.quality_score),
                    "reliability_score": float(spec.reliability_score),
                    "scoring_bias_offset": float(spec.scoring_bias_offset),
                    "input_cost_per_1m": float(spec.input_cost_per_1m_usd),
                    "output_cost_per_1m": float(spec.output_cost_per_1m_usd),
                    "currency": "USD",
                    "estimated_cost_krw": self._estimate_vlm_cost_krw(
                        input_cost_per_1m=float(spec.input_cost_per_1m_usd),
                        output_cost_per_1m=float(spec.output_cost_per_1m_usd),
                        currency="USD",
                        usd_to_krw=usd_to_krw,
                    ),
                    "key_configured": bool(str(text_keys.get(spec.key_id, "")).strip()),
                    "source": "static_matrix",
                }
            )
        return fallback_payload

    def _resolve_vlm_usd_to_krw(self) -> float:
        """VLM 단가 환산에 사용할 USD->KRW 환율을 반환한다."""
        default_rate = float(constants.VLM_DEFAULT_USD_TO_KRW)
        if not self.job_store:
            return default_rate
        raw = str(self.job_store.get_system_setting("vlm_usd_to_krw", str(default_rate))).strip()
        try:
            parsed = float(raw)
        except ValueError:
            parsed = default_rate
        return parsed if parsed > 0 else default_rate

    def _estimate_vlm_cost_krw(
        self,
        *,
        input_cost_per_1m: float,
        output_cost_per_1m: float,
        currency: str,
        usd_to_krw: float,
    ) -> float:
        """VLM 평가 1회(추정 토큰)당 KRW 비용을 계산한다."""
        estimated_cost = (
            (float(constants.VLM_ROUTER_EST_INPUT_TOKENS) / 1_000_000.0) * max(0.0, float(input_cost_per_1m))
            + (float(constants.VLM_ROUTER_EST_OUTPUT_TOKENS) / 1_000_000.0) * max(0.0, float(output_cost_per_1m))
        )
        normalized_currency = str(currency or "USD").strip().upper()
        if normalized_currency == "KRW":
            return round(estimated_cost, 4)
        return round(estimated_cost * max(1.0, float(usd_to_krw)), 4)


def provider_label(name: str) -> str:
    """알림 메시지용 provider 라벨."""
    mapping = {
        "qwen": "Qwen",
        "deepseek": "DeepSeek",
        "gemini": "Gemini",
        "openai": "OpenAI",
        "claude": "Claude",
        "groq": "Groq",
        "cerebras": "Cerebras",
        "nvidia": "NVIDIA",
        "zai": "Z.AI",
    }
    return mapping.get(str(name).strip().lower(), name)
