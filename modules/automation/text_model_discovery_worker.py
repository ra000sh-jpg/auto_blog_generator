"""공식 문서 기반 텍스트 모델 카탈로그 동기화 워커."""

from __future__ import annotations

from dataclasses import dataclass
import html as html_lib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import httpx

from .job_store import JobStore
from .time_utils import now_utc
from .. import constants
from ..llm.llm_router import DEFAULT_TEXT_KEYS, TEXT_MODEL_MATRIX

logger = logging.getLogger(__name__)


FetchFn = Callable[[str], str]


@dataclass(frozen=True)
class TextModelSource:
    """공식 텍스트 모델 소스 정의."""

    provider: str
    url: str
    parser: Callable[[str, str], List[Dict[str, Any]]]


DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing"
QWEN_PRICING_URL = "https://www.alibabacloud.com/help/en/model-studio/model-pricing"

DEFAULT_ROUTER_AUTO_REGISTER_MODELS = {
    "qwen-flash",
    "qwen-turbo",
    "qwen-plus",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
}


KNOWN_QWEN_PRICES: Dict[str, Dict[str, Any]] = {
    "qwen-flash": {
        "input_cost_per_1m": 0.05,
        "output_cost_per_1m": 0.40,
        "quality_score": 80,
        "speed_score": 95,
    },
    "qwen-plus": {
        "input_cost_per_1m": 0.40,
        "output_cost_per_1m": 1.20,
        "quality_score": 84,
        "speed_score": 90,
    },
    "qwen-plus-latest": {
        "input_cost_per_1m": 0.40,
        "output_cost_per_1m": 1.20,
        "quality_score": 84,
        "speed_score": 90,
    },
    "qwen-plus-us": {
        "input_cost_per_1m": 0.40,
        "output_cost_per_1m": 1.20,
        "quality_score": 84,
        "speed_score": 90,
    },
    "qwen-turbo": {
        "input_cost_per_1m": 0.05,
        "output_cost_per_1m": 0.20,
        "quality_score": 78,
        "speed_score": 94,
    },
    "qwen-max": {
        "input_cost_per_1m": 1.60,
        "output_cost_per_1m": 6.40,
        "quality_score": 91,
        "speed_score": 82,
    },
    "qwen3.5-flash": {
        "input_cost_per_1m": 0.10,
        "output_cost_per_1m": 0.40,
        "quality_score": 82,
        "speed_score": 95,
    },
    "qwen3.5-plus": {
        "input_cost_per_1m": 0.40,
        "output_cost_per_1m": 2.40,
        "quality_score": 86,
        "speed_score": 88,
    },
}


def _clean_html(raw_html: str) -> str:
    """HTML을 가벼운 텍스트로 정규화한다."""
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", raw_html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _entry(
    *,
    provider: str,
    model: str,
    key_id: str,
    label: str,
    source_url: str,
    input_cost_per_1m: float,
    output_cost_per_1m: float,
    cache_hit_input_cost_per_1m: float = 0.0,
    supports_json: bool = False,
    supports_tool_calls: bool = False,
    supports_thinking: bool = False,
    context_window: int = 0,
    max_output_tokens: int = 0,
    quality_score: float = 0.0,
    speed_score: float = 0.0,
    reliability_score: float = 70.0,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "key_id": key_id,
        "label": label,
        "status": "discovered",
        "include_in_competition": False,
        "supports_json": supports_json,
        "supports_tool_calls": supports_tool_calls,
        "supports_thinking": supports_thinking,
        "context_window": context_window,
        "max_output_tokens": max_output_tokens,
        "quality_score": quality_score,
        "speed_score": speed_score,
        "reliability_score": reliability_score,
        "input_cost_per_1m": input_cost_per_1m,
        "output_cost_per_1m": output_cost_per_1m,
        "cache_hit_input_cost_per_1m": cache_hit_input_cost_per_1m,
        "currency": "USD",
        "source_url": source_url,
        "metadata_json": metadata if isinstance(metadata, dict) else {},
    }


def parse_deepseek_models(raw_html: str, source_url: str = DEEPSEEK_PRICING_URL) -> List[Dict[str, Any]]:
    """DeepSeek 공식 가격 페이지에서 텍스트 모델 후보를 추출한다."""
    text = _clean_html(raw_html)
    found = set(re.findall(r"\bdeepseek-[a-z0-9.-]+\b", text.lower()))
    entries: List[Dict[str, Any]] = []

    if "deepseek-v4-flash" in found:
        entries.append(
            _entry(
                provider="deepseek",
                model="deepseek-v4-flash",
                key_id="deepseek",
                label="DeepSeek V4 Flash",
                source_url=source_url,
                input_cost_per_1m=0.14,
                output_cost_per_1m=0.28,
                cache_hit_input_cost_per_1m=0.0028,
                supports_json=True,
                supports_tool_calls=True,
                supports_thinking=True,
                context_window=1_000_000,
                max_output_tokens=384_000,
                quality_score=88,
                speed_score=92,
                reliability_score=88,
                metadata={
                    "source": "official_deepseek_pricing",
                    "pricing_note": "cache miss 기준. cache hit 가격 별도 저장.",
                },
            )
        )
    if "deepseek-v4-pro" in found:
        entries.append(
            _entry(
                provider="deepseek",
                model="deepseek-v4-pro",
                key_id="deepseek",
                label="DeepSeek V4 Pro",
                source_url=source_url,
                input_cost_per_1m=0.435,
                output_cost_per_1m=0.87,
                cache_hit_input_cost_per_1m=0.003625,
                supports_json=True,
                supports_tool_calls=True,
                supports_thinking=True,
                context_window=1_000_000,
                max_output_tokens=384_000,
                quality_score=94,
                speed_score=78,
                reliability_score=86,
                metadata={
                    "source": "official_deepseek_pricing",
                    "pricing_note": "cache miss 기준. cache hit 가격 별도 저장.",
                },
            )
        )
    return entries


def _qwen_model_candidates(text: str) -> List[str]:
    """Qwen 공식 가격 페이지에서 범용 텍스트 모델명 후보만 추린다."""
    blocked = (
        "vl",
        "omni",
        "audio",
        "tts",
        "asr",
        "image",
        "embedding",
        "rerank",
        "coder",
    )
    allowed_markers = ("plus", "flash", "turbo", "max")
    candidates = set()
    for raw in re.findall(r"\bqwen[0-9a-zA-Z_.-]*\b", text):
        model = raw.strip(".,:;()[]{}").lower()
        if not model.startswith("qwen"):
            continue
        if any(marker in model for marker in blocked):
            continue
        if not any(marker in model for marker in allowed_markers):
            continue
        candidates.add(model)
    return sorted(candidates)


def parse_qwen_models(raw_html: str, source_url: str = QWEN_PRICING_URL) -> List[Dict[str, Any]]:
    """Alibaba Cloud Model Studio 공식 가격 페이지에서 Qwen 텍스트 모델 후보를 추출한다."""
    text = _clean_html(raw_html)
    entries: List[Dict[str, Any]] = []
    for model in _qwen_model_candidates(text):
        known = KNOWN_QWEN_PRICES.get(model, {})
        price_known = bool(known)
        entries.append(
            _entry(
                provider="qwen",
                model=model,
                key_id="qwen",
                label=model.replace("-", " ").title(),
                source_url=source_url,
                input_cost_per_1m=float(known.get("input_cost_per_1m", 0.0) or 0.0),
                output_cost_per_1m=float(known.get("output_cost_per_1m", 0.0) or 0.0),
                supports_json=price_known,
                supports_tool_calls=price_known,
                supports_thinking=("plus" in model or "max" in model),
                context_window=1_000_000 if ("plus" in model or "flash" in model) else 0,
                max_output_tokens=0,
                quality_score=float(known.get("quality_score", 0.0) or 0.0),
                speed_score=float(known.get("speed_score", 0.0) or 0.0),
                reliability_score=82 if price_known else 60,
                metadata={
                    "source": "official_alibaba_model_studio_pricing",
                    "price_known": price_known,
                    "pricing_note": "Qwen은 배포 모드별 가격 차이가 있어 보수적 기준을 우선 적용한다.",
                },
            )
        )
    return entries


class TextModelDiscoveryWorker:
    """공식 문서와 로컬 매트릭스를 합쳐 텍스트 모델 카탈로그를 동기화한다."""

    def __init__(
        self,
        *,
        job_store: JobStore,
        fetcher: Optional[FetchFn] = None,
        timeout_sec: float = 20.0,
        sources: Optional[List[TextModelSource]] = None,
    ) -> None:
        self.job_store = job_store
        self.fetcher = fetcher
        self.timeout_sec = float(timeout_sec or 20.0)
        self.sources = sources or [
            TextModelSource("deepseek", DEEPSEEK_PRICING_URL, parse_deepseek_models),
            TextModelSource("qwen", QWEN_PRICING_URL, parse_qwen_models),
        ]

    def sync_catalog(self) -> Dict[str, int]:
        """텍스트 모델 카탈로그를 동기화하고 라우터 등록 후보를 보정한다."""
        before_rows = self.job_store.list_text_model_catalog_entries(limit=1000)
        before_pairs = {(row["provider"], row["model"]) for row in before_rows}

        entries = self._build_matrix_entries()
        official_providers: List[str] = []
        source_failures = 0
        for source in self.sources:
            try:
                html_text = self._fetch(source.url)
                parsed = source.parser(html_text, source.url)
                if not parsed:
                    source_failures += 1
                    self._record_source_failure(source, "no models parsed")
                    continue
                entries = self._merge_entries(entries, parsed)
                official_providers.append(source.provider)
            except Exception as exc:
                source_failures += 1
                self._record_source_failure(source, f"{exc.__class__.__name__}: {exc}")
                logger.warning("Text model discovery source failed: %s", source.provider, exc_info=True)

        stats = self.job_store.upsert_text_model_catalog_entries(entries)
        source_pairs = [(item["provider"], item["model"]) for item in entries]
        deprecated = self.job_store.mark_missing_text_models_deprecated(
            source_pairs,
            providers=official_providers,
        )

        after_rows = self.job_store.list_text_model_catalog_entries(limit=1000)
        after_map = {(row["provider"], row["model"]): row for row in after_rows}
        for provider, model in sorted(pair for pair in after_map if pair not in before_pairs):
            self.job_store.record_text_model_discovery_event(
                event_type="discovered",
                provider=provider,
                model=model,
                detail={
                    "source": after_map[(provider, model)].get("source_url", ""),
                    "metadata": after_map[(provider, model)].get("metadata_json", {}),
                },
            )

        registered_added = self._sync_known_matrix_models_to_router_registry()
        self.job_store.set_system_setting("text_model_last_discovery_sync_at", now_utc())
        return {
            "inserted": int(stats.get("inserted", 0)),
            "updated": int(stats.get("updated", 0)),
            "unchanged": int(stats.get("unchanged", 0)),
            "deprecated": int(deprecated),
            "source_failures": int(source_failures),
            "registered_added": int(registered_added),
        }

    def _fetch(self, url: str) -> str:
        if self.fetcher:
            return str(self.fetcher(url) or "")
        with httpx.Client(timeout=self.timeout_sec, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text

    def _build_matrix_entries(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for spec in TEXT_MODEL_MATRIX:
            entries.append(
                _entry(
                    provider=spec.provider,
                    model=spec.model,
                    key_id=spec.key_id,
                    label=spec.label,
                    source_url="local:TEXT_MODEL_MATRIX",
                    input_cost_per_1m=float(spec.input_cost_per_1m_usd),
                    output_cost_per_1m=float(spec.output_cost_per_1m_usd),
                    supports_json=True,
                    supports_tool_calls=True,
                    supports_thinking=("deepseek-v4" in spec.model or "reasoner" in spec.model),
                    quality_score=float(spec.quality_score),
                    speed_score=float(spec.speed_score),
                    reliability_score=75.0,
                    metadata={
                        "source": "local_text_model_matrix",
                        "source_priority": "fallback",
                    },
                )
            )
        return entries

    def _merge_entries(
        self,
        base_entries: Iterable[Dict[str, Any]],
        official_entries: Iterable[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for item in base_entries:
            provider = str(item.get("provider", "")).strip().lower()
            model = str(item.get("model", "")).strip()
            if provider and model:
                merged[(provider, model)] = dict(item)
        for item in official_entries:
            provider = str(item.get("provider", "")).strip().lower()
            model = str(item.get("model", "")).strip()
            if provider and model:
                merged[(provider, model)] = dict(item)
        return list(merged.values())

    def _record_source_failure(self, source: TextModelSource, message: str) -> None:
        self.job_store.record_text_model_discovery_event(
            event_type="source_failed",
            provider=source.provider,
            model="bulk",
            detail={"source_url": source.url, "message": message},
        )

    def _sync_known_matrix_models_to_router_registry(self) -> int:
        """키가 있는 기존 매트릭스 모델을 router_registered_models에 보강한다."""
        text_keys = self._load_configured_text_keys()
        if not text_keys:
            return 0

        raw_registered = self.job_store.get_system_setting("router_registered_models", "[]")
        try:
            registered = json.loads(raw_registered) if raw_registered else []
            if not isinstance(registered, list):
                registered = []
        except Exception:
            registered = []

        existing = {
            str(item.get("model_id", "")).strip().lower()
            for item in registered
            if isinstance(item, dict) and str(item.get("model_id", "")).strip()
        }
        allowed_providers = {str(source.provider).strip().lower() for source in self.sources}
        added = 0
        for spec in TEXT_MODEL_MATRIX:
            if allowed_providers and spec.provider not in allowed_providers:
                continue
            if spec.model not in DEFAULT_ROUTER_AUTO_REGISTER_MODELS:
                continue
            if not str(text_keys.get(spec.key_id, "")).strip():
                continue
            model_id = str(spec.model).strip()
            if not model_id or model_id.lower() in existing:
                continue
            registered.append(
                {
                    "model_id": model_id,
                    "provider": spec.provider,
                    "active": True,
                }
            )
            existing.add(model_id.lower())
            added += 1

        if added:
            self.job_store.set_system_setting(
                "router_registered_models",
                json.dumps(registered, ensure_ascii=False),
            )
        return added

    def _load_configured_text_keys(self) -> Dict[str, str]:
        """DB, 환경변수, .env 순서로 텍스트 API 키 존재 여부만 확인한다."""
        raw_keys = self.job_store.get_system_setting("router_text_api_keys", "{}")
        try:
            text_keys = json.loads(raw_keys) if raw_keys else {}
            if not isinstance(text_keys, dict):
                text_keys = {}
        except Exception:
            text_keys = {}

        env_file_values = self._read_dotenv_values(Path(".env"))
        for key_id, env_name in DEFAULT_TEXT_KEYS.items():
            if str(text_keys.get(key_id, "")).strip():
                continue
            env_value = os.getenv(env_name, "").strip()
            if env_value:
                text_keys[key_id] = env_value
                continue
            dotenv_value = str(env_file_values.get(env_name, "")).strip()
            if dotenv_value:
                text_keys[key_id] = dotenv_value
        return {str(key).strip().lower(): str(value).strip() for key, value in text_keys.items() if str(value).strip()}

    def _read_dotenv_values(self, path: Path) -> Dict[str, str]:
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
                value = value.strip().strip("\"'")
                values[key] = value
        except Exception:
            return {}
        return values


__all__ = [
    "DEEPSEEK_PRICING_URL",
    "QWEN_PRICING_URL",
    "TextModelDiscoveryWorker",
    "TextModelSource",
    "parse_deepseek_models",
    "parse_qwen_models",
]
