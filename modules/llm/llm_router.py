"""모델 라우팅/견적 계산 유틸리티."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ..automation.job_store import JobStore
from ..constants import DEFAULT_FALLBACK_CATEGORY
from ..config import LLMConfig

if TYPE_CHECKING:
    from ..automation.job_store import Job

USD_TO_KRW = 1350.0

# 토큰 사용량은 역할별 평균치(보수적 추정)
TOKEN_BUDGET = {
    "parser": {"input": 450, "output": 180},
    "quality_step": {"input": 3600, "output": 2400},
    "voice_step": {"input": 2900, "output": 2200},
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


TEXT_MODEL_MATRIX: List[TextModelSpec] = [
    TextModelSpec(
        provider="qwen",
        model="qwen-plus",
        label="Qwen Plus",
        key_id="qwen",
        input_cost_per_1m_usd=0.28,
        output_cost_per_1m_usd=0.84,
        quality_score=84,
        speed_score=90,
    ),
    TextModelSpec(
        provider="deepseek",
        model="deepseek-chat",
        label="DeepSeek Chat",
        key_id="deepseek",
        input_cost_per_1m_usd=0.27,
        output_cost_per_1m_usd=1.10,
        quality_score=86,
        speed_score=88,
    ),
    TextModelSpec(
        provider="gemini",
        model="gemini-2.0-flash",
        label="Gemini 2.0 Flash",
        key_id="gemini",
        input_cost_per_1m_usd=0.35,
        output_cost_per_1m_usd=1.05,
        quality_score=90,
        speed_score=93,
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
        provider="claude",
        model="claude-sonnet-4-20250514",
        label="Claude Sonnet 4",
        key_id="claude",
        input_cost_per_1m_usd=3.00,
        output_cost_per_1m_usd=15.00,
        quality_score=97,
        speed_score=83,
    ),
    # 무료 프로바이더: parser·태그 생성 등 단순 역할에 우선 라우팅
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
        provider="cerebras",
        model="llama3.1-8b",
        label="Cerebras Llama3.1 8B (무료)",
        key_id="cerebras",
        input_cost_per_1m_usd=0.0,
        output_cost_per_1m_usd=0.0,
        quality_score=76,
        speed_score=97,
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
DEFAULT_IMAGES_PER_POST_MAX = 2
DEFAULT_COMPETITION_PHASE = "idle"
COMPETITION_PHASES = {"idle", "testing", "champion_ops", "completed"}


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
    if value in {"quality", "best_quality", "hq"}:
        return "quality"
    return "cost"


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


def _role_temperature(strategy_mode: str, role: str) -> float:
    """역할별 기본 temperature를 반환한다."""
    if role == "parser":
        return 0.1
    if role == "quality_step":
        return 0.55 if strategy_mode == "cost" else 0.65
    if role == "voice_step":
        return 0.35 if strategy_mode == "cost" else 0.45
    return 0.6


class LLMRouter:
    """모델 라우팅/견적/설정 저장을 담당한다."""

    SETTINGS_KEYS = (
        "router_strategy_mode",
        "router_text_api_keys",
        "router_image_api_keys",
        "router_image_engine",
        "router_image_enabled",
        "router_images_per_post",
        "router_images_per_post_min",
        "router_images_per_post_max",
        "router_competition_phase",
        "router_competition_week_start",
        "router_competition_apply_at",
        "router_shadow_mode",
        "router_champion_model",
        "router_challenger_model",
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

        text_api_keys = _parse_json_map(raw_settings.get("router_text_api_keys", ""))
        image_api_keys = _parse_json_map(raw_settings.get("router_image_api_keys", ""))

        # DB 값이 비어 있으면 환경변수 키를 자동 반영한다.
        for key_id, env_name in DEFAULT_TEXT_KEYS.items():
            if not text_api_keys.get(key_id):
                text_api_keys[key_id] = os.getenv(env_name, "").strip()
        for key_id, env_name in DEFAULT_IMAGE_KEYS.items():
            if not image_api_keys.get(key_id):
                image_api_keys[key_id] = os.getenv(env_name, "").strip()

        strategy_mode = normalize_strategy_mode(raw_settings.get("router_strategy_mode", DEFAULT_STRATEGY_MODE))
        image_engine = str(raw_settings.get("router_image_engine", DEFAULT_IMAGE_ENGINE)).strip().lower()
        if not _find_image_model(image_engine):
            image_engine = DEFAULT_IMAGE_ENGINE
        image_enabled = _to_bool(raw_settings.get("router_image_enabled", "true"), default=True)
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

        phase_raw = str(raw_settings.get("router_competition_phase", DEFAULT_COMPETITION_PHASE)).strip().lower()
        competition_phase = phase_raw if phase_raw in COMPETITION_PHASES else DEFAULT_COMPETITION_PHASE
        shadow_mode = _to_bool(raw_settings.get("router_shadow_mode", "false"), default=False)
        champion_model = str(raw_settings.get("router_champion_model", "")).strip()
        challenger_model = str(raw_settings.get("router_challenger_model", "")).strip()
        competition_week_start = str(raw_settings.get("router_competition_week_start", "")).strip()
        competition_apply_at = str(raw_settings.get("router_competition_apply_at", "")).strip()
        fallback_category = str(raw_settings.get("fallback_category", "")).strip() or DEFAULT_FALLBACK_CATEGORY

        return {
            "strategy_mode": strategy_mode,
            "text_api_keys": text_api_keys,
            "image_api_keys": image_api_keys,
            "image_engine": image_engine,
            "image_enabled": image_enabled,
            "images_per_post": images_per_post,
            "images_per_post_min": images_per_post_min,
            "images_per_post_max": images_per_post_max,
            "competition_phase": competition_phase,
            "competition_week_start": competition_week_start,
            "competition_apply_at": competition_apply_at,
            "shadow_mode": shadow_mode,
            "champion_model": champion_model,
            "challenger_model": challenger_model,
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
        image_engine = str(payload.get("image_engine", current["image_engine"])).strip().lower()
        if not _find_image_model(image_engine):
            image_engine = current["image_engine"]
        image_enabled = _to_bool(payload.get("image_enabled", current["image_enabled"]), default=True)
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
            "image_engine": image_engine,
            "image_enabled": image_enabled,
            "images_per_post": images_per_post,
            "images_per_post_min": images_per_post_min,
            "images_per_post_max": images_per_post_max,
        }
        if self.job_store:
            self.job_store.set_system_setting("router_strategy_mode", strategy_mode)
            self.job_store.set_system_setting("router_text_api_keys", _json_text(text_keys))
            self.job_store.set_system_setting("router_image_api_keys", _json_text(image_keys))
            self.job_store.set_system_setting("router_image_engine", image_engine)
            self.job_store.set_system_setting("router_image_enabled", "true" if image_enabled else "false")
            self.job_store.set_system_setting("router_images_per_post", str(images_per_post))
            self.job_store.set_system_setting("router_images_per_post_min", str(images_per_post_min))
            self.job_store.set_system_setting("router_images_per_post_max", str(images_per_post_max))

        return normalized

    def build_plan(self, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """설정 기반 역할별 모델 배정과 견적 결과를 생성한다."""
        base = self.get_saved_settings()
        if overrides:
            base = self._merge_preview_settings(base, overrides)

        strategy_mode = normalize_strategy_mode(base["strategy_mode"])
        text_api_keys = dict(base["text_api_keys"])
        image_api_keys = dict(base["image_api_keys"])
        image_enabled = bool(base["image_enabled"])
        image_engine = str(base["image_engine"]).lower().strip()
        images_per_post = _to_int(base["images_per_post"], DEFAULT_IMAGES_PER_POST, 0, 4)
        images_per_post_min = _to_int(
            base.get("images_per_post_min", DEFAULT_IMAGES_PER_POST_MIN),
            default=DEFAULT_IMAGES_PER_POST_MIN,
            min_value=0,
            max_value=4,
        )

        available_text_models = [
            spec for spec in TEXT_MODEL_MATRIX if str(text_api_keys.get(spec.key_id, "")).strip()
        ]
        if not available_text_models:
            # 사용 가능한 키가 없으면 환경 설정값으로 최소 라우팅 정보를 제공한다.
            fallback_provider = str(self.llm_config.primary_provider).strip().lower()
            fallback_model = str(self.llm_config.primary_model).strip()
            fallback_spec = _find_text_model(fallback_provider, fallback_model)
            if fallback_spec:
                available_text_models = [fallback_spec]

        parser_spec = self._pick_role_model(available_text_models, strategy_mode, role="parser")
        quality_spec = self._pick_role_model(available_text_models, strategy_mode, role="quality_step")
        voice_spec = self._pick_role_model(available_text_models, strategy_mode, role="voice_step")

        quality_fallbacks = self._build_fallback_candidates(
            selected=quality_spec,
            pool=available_text_models,
            strategy_mode=strategy_mode,
            max_size=3,
        )
        voice_fallbacks = self._build_fallback_candidates(
            selected=voice_spec,
            pool=available_text_models,
            strategy_mode=strategy_mode,
            max_size=2,
        )

        role_payload = {
            "parser": self._role_payload(parser_spec, strategy_mode, "parser"),
            "quality_step": self._role_payload(quality_spec, strategy_mode, "quality_step"),
            "voice_step": self._role_payload(voice_spec, strategy_mode, "voice_step"),
        }
        role_payload["quality_step"]["fallback_chain"] = [
            self._model_payload(spec) for spec in quality_fallbacks
        ]
        role_payload["voice_step"]["fallback_chain"] = [
            self._model_payload(spec) for spec in voice_fallbacks
        ]

        image_spec = _find_image_model(image_engine) or _find_image_model(DEFAULT_IMAGE_ENGINE)
        image_key_ok = bool(image_spec and str(image_api_keys.get(image_spec.key_id, "")).strip())
        if image_spec and image_spec.key_id == "openai_image":
            # DALL-E는 OpenAI 텍스트 키를 공유하므로 text key도 허용한다.
            image_key_ok = image_key_ok or bool(str(text_api_keys.get("openai", "")).strip())
        image_usable = bool(image_enabled and image_spec and image_key_ok)

        estimate = self._estimate(
            parser_spec=parser_spec,
            quality_spec=quality_spec,
            voice_spec=voice_spec,
            image_spec=image_spec if image_usable else None,
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

        if parser_provider and parser_model:
            chain.append(
                {
                    "provider": parser_provider,
                    "model": parser_model,
                    "api_key": str(text_keys.get(self._provider_to_key_id(parser_provider), "")).strip(),
                }
            )

        # 파서는 speed/비용 중심으로 보조 체인을 추가한다.
        for spec in sorted(TEXT_MODEL_MATRIX, key=lambda item: (-item.speed_score, item.avg_cost_per_1k_usd)):
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
            "quality_step": role_to_runtime(quality),
            "voice_step": role_to_runtime(voice),
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
        available_specs = self._available_text_specs(text_keys)
        strategy_mode = str(planned["strategy_mode"]).strip().lower()

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
            fallback_category=saved.get("fallback_category", DEFAULT_FALLBACK_CATEGORY),
            phase=saved.get("competition_phase", DEFAULT_COMPETITION_PHASE),
            champion_model=saved.get("champion_model", ""),
            challenger_model=saved.get("challenger_model", ""),
        )
        if selected_quality_spec:
            quality_role = self._role_payload(selected_quality_spec, strategy_mode, "quality_step")
            fallback_chain = self._build_fallback_candidates(
                selected=selected_quality_spec,
                pool=available_specs,
                strategy_mode=strategy_mode,
                max_size=3,
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
            "quality_step": role_to_runtime(quality_role),
            "voice_step": role_to_runtime(voice_role),
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
                "image_engine": saved["image_engine"],
                "image_enabled": saved["image_enabled"],
                "images_per_post": saved["images_per_post"],
                "images_per_post_min": saved.get("images_per_post_min", DEFAULT_IMAGES_PER_POST_MIN),
                "images_per_post_max": saved.get("images_per_post_max", DEFAULT_IMAGES_PER_POST_MAX),
            },
            "quote": plan["estimate"],
            "roles": plan["roles"],
            "competition": self.get_competition_state(),
            "matrix": {
                "text_models": [self._model_payload(item) for item in TEXT_MODEL_MATRIX],
                "image_models": [self._image_payload(item) for item in IMAGE_MODEL_MATRIX],
            },
        }

    def get_competition_state(self, *, slot_type: str = "default") -> Dict[str, Any]:
        """주간 경쟁 상태를 UI/런타임 공통 포맷으로 반환한다."""
        saved = self.get_saved_settings()
        return {
            "phase": str(saved.get("competition_phase", DEFAULT_COMPETITION_PHASE)),
            "week_start": str(saved.get("competition_week_start", "")),
            "apply_at": str(saved.get("competition_apply_at", "")),
            "shadow_mode": bool(saved.get("shadow_mode", False)),
            "champion_model": str(saved.get("champion_model", "")),
            "challenger_model": str(saved.get("challenger_model", "")),
            "fallback_category": str(saved.get("fallback_category", DEFAULT_FALLBACK_CATEGORY)),
            "slot_type": slot_type,
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
            "image_engine": str(overrides.get("image_engine", current["image_engine"])).strip().lower(),
            "image_enabled": _to_bool(overrides.get("image_enabled", current["image_enabled"]), default=True),
            "images_per_post": images_per_post_max,
            "images_per_post_min": images_per_post_min,
            "images_per_post_max": images_per_post_max,
        }
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
        return merged

    def _available_text_specs(self, text_api_keys: Dict[str, str]) -> List[TextModelSpec]:
        """현재 사용 가능한 텍스트 모델 스펙을 반환한다."""
        available_specs = [
            spec for spec in TEXT_MODEL_MATRIX if str(text_api_keys.get(spec.key_id, "")).strip()
        ]
        if available_specs:
            return available_specs

        fallback_provider = str(self.llm_config.primary_provider).strip().lower()
        fallback_model = str(self.llm_config.primary_model).strip()
        fallback_spec = _find_text_model(fallback_provider, fallback_model)
        return [fallback_spec] if fallback_spec else []

    def _normalize_category_name(self, value: str) -> str:
        """카테고리 비교를 위해 공백/대소문자를 정규화한다."""
        return "".join(str(value or "").lower().split())

    def _resolve_job_slot_type(
        self,
        *,
        job: "Job",
        fallback_category: str,
        phase: str,
    ) -> str:
        """작업이 main/shadow/challenger 중 어떤 슬롯인지 판별한다."""
        normalized_job_category = self._normalize_category_name(getattr(job, "category", ""))
        normalized_fallback = self._normalize_category_name(fallback_category)
        if normalized_job_category and normalized_job_category == normalized_fallback:
            if str(phase).strip().lower() == "champion_ops":
                return "challenger"
            return "shadow"
        return "main"

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
        fallback_category: str,
        phase: str,
        champion_model: str,
        challenger_model: str,
    ) -> Tuple[Optional[TextModelSpec], str]:
        """경쟁 상태를 반영해 quality_step 모델을 결정한다."""
        del strategy_mode
        slot_type = self._resolve_job_slot_type(
            job=job,
            fallback_category=fallback_category,
            phase=phase,
        )
        selected = base_spec
        if slot_type == "main":
            selected = self._find_text_model_by_model_id(
                model_id=champion_model,
                available_specs=available_specs,
            ) or base_spec
            return selected, slot_type

        preferred_challenger = self._find_text_model_by_model_id(
            model_id=challenger_model,
            available_specs=available_specs,
        )
        if preferred_challenger:
            return preferred_challenger, slot_type

        fallback_champion = self._find_text_model_by_model_id(
            model_id=champion_model,
            available_specs=available_specs,
        )
        if fallback_champion:
            return fallback_champion, slot_type
        return selected, slot_type

    def _pick_role_model(
        self,
        candidates: List[TextModelSpec],
        strategy_mode: str,
        role: str,
    ) -> Optional[TextModelSpec]:
        """역할별 우선순위로 모델을 선택한다."""
        if not candidates:
            return None

        by_cost = sorted(candidates, key=lambda item: (item.avg_cost_per_1k_usd, -item.quality_score))
        by_quality = sorted(candidates, key=lambda item: (-item.quality_score, item.avg_cost_per_1k_usd))
        role_min_quality = {"parser": 75, "quality_step": 84, "voice_step": 80}
        threshold = role_min_quality.get(role, 75)

        if strategy_mode == "quality":
            if role == "parser":
                # 파서는 품질 모드에서도 지연을 줄이기 위해 속도 우선 선택한다.
                by_speed = sorted(candidates, key=lambda item: (-item.speed_score, item.avg_cost_per_1k_usd))
                return by_speed[0]
            return by_quality[0]

        # cost 전략: parser 역할은 무료 프로바이더(Groq/Cerebras)를 1순위로 선택
        if role == "parser":
            free_candidates = [
                item for item in candidates
                if item.input_cost_per_1m_usd == 0.0 and item.output_cost_per_1m_usd == 0.0
                and item.quality_score >= threshold
            ]
            if free_candidates:
                return sorted(free_candidates, key=lambda item: -item.speed_score)[0]

        for item in by_cost:
            if item.quality_score >= threshold:
                return item
        return by_cost[0]

    def _build_fallback_candidates(
        self,
        *,
        selected: Optional[TextModelSpec],
        pool: List[TextModelSpec],
        strategy_mode: str,
        max_size: int,
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

        if strategy_mode == "quality":
            ordered = sorted(
                candidates,
                key=lambda item: (
                    abs(item.quality_score - selected.quality_score),
                    item.avg_cost_per_1k_usd,
                ),
            )
        else:
            ordered = sorted(
                candidates,
                key=lambda item: (
                    abs(item.avg_cost_per_1k_usd - selected.avg_cost_per_1k_usd),
                    abs(item.quality_score - selected.quality_score),
                ),
            )
        return ordered[: max(0, max_size)]

    def _estimate(
        self,
        *,
        parser_spec: Optional[TextModelSpec],
        quality_spec: Optional[TextModelSpec],
        voice_spec: Optional[TextModelSpec],
        image_spec: Optional[ImageModelSpec],
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
        text_cost = parser_cost + quality_cost + voice_cost
        image_cost = float((image_spec.cost_per_image_krw if image_spec else 0) * max(0, images_per_post))
        total_cost = text_cost + image_cost

        parser_quality = parser_spec.quality_score if parser_spec else 50
        quality_quality = quality_spec.quality_score if quality_spec else 55
        voice_quality = voice_spec.quality_score if voice_spec else 55
        image_quality = image_spec.quality_score if image_spec else 60
        quality_score = round(
            (parser_quality * 0.1) + (quality_quality * 0.55) + (voice_quality * 0.30) + (image_quality * 0.05)
        )

        # Range 비용: 최소(images_per_post_min장), 최대(images_per_post장)
        safe_min = max(0, min(images_per_post_min, images_per_post))
        image_cost_per_unit = float(image_spec.cost_per_image_krw if image_spec else 0)
        image_cost_max = image_cost_per_unit * max(0, images_per_post)
        image_cost_min = image_cost_per_unit * safe_min
        cost_min = text_cost + image_cost_min
        cost_max = text_cost + image_cost_max

        return {
            "currency": "KRW",
            "text_cost_krw": int(round(text_cost)),
            "image_cost_krw": int(round(image_cost)),
            "total_cost_krw": int(round(total_cost)),
            "cost_min_krw": int(round(cost_min)),
            "cost_max_krw": int(round(cost_max)),
            "quality_score": max(0, min(100, quality_score)),
        }

    def _provider_to_key_id(self, provider: str) -> str:
        """provider명을 key_id로 변환한다."""
        normalized = str(provider or "").strip().lower()
        if normalized in {"qwen", "deepseek", "gemini", "openai", "claude", "groq", "cerebras"}:
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


def provider_label(name: str) -> str:
    """알림 메시지용 provider 라벨."""
    mapping = {
        "qwen": "Qwen",
        "deepseek": "DeepSeek",
        "gemini": "Gemini",
        "openai": "OpenAI",
        "claude": "Claude",
    }
    return mapping.get(str(name).strip().lower(), name)
