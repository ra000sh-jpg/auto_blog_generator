"""블로그 이미지 생성기."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Tuple

from .pollinations_client import PollinationsImageClient
from .styles import get_content_style, get_thumbnail_style

if TYPE_CHECKING:
    from ..llm.base_client import BaseLLMClient
    from .dashscope_image_client import ImageResult

logger = logging.getLogger(__name__)


class ImageClient(Protocol):
    """이미지 생성 클라이언트 프로토콜."""

    async def generate(
        self, prompt: str, style_suffix: str, size: str, n: int = 1
    ) -> "ImageResult": ...

_PROMPT_TRANSLATE_SYSTEM = (
    "You are an expert at writing image generation prompts for FLUX diffusion models. "
    "Given a Korean blog post title and keywords, write a concise, vivid English image prompt "
    "(max 80 words) that captures the concept visually. "
    "Output only the prompt text, no explanation."
)

# 블로그 최적화 이미지 프롬프트 템플릿
_THUMBNAIL_TEMPLATE = (
    "Professional blog thumbnail illustration: {concept}. "
    "Modern flat design with vibrant colors, clean composition, "
    "centered subject with subtle background, "
    "space for text overlay on left side, "
    "high quality digital art, 4K resolution"
)

_CONTENT_IMAGE_TEMPLATE = (
    "Informative blog illustration: {concept}. "
    "Clean infographic style, soft pastel colors, "
    "educational visual, minimalist design, "
    "easy to understand diagram or scene, "
    "professional quality, web-optimized"
)


@dataclass
class GeneratedImages:
    """생성된 이미지 경로 집합."""

    thumbnail_path: Optional[str] = None
    content_paths: List[str] = field(default_factory=list)
    source_kind_by_path: Dict[str, str] = field(default_factory=dict)
    provider_by_path: Dict[str, str] = field(default_factory=dict)
    generation_logs: List[Dict[str, Any]] = field(default_factory=list)
    free_tier_exhausted: bool = False
    free_tier_exhausted_events: List[Dict[str, str]] = field(default_factory=list)

    def bind_source_meta(self, path: Optional[str], source_kind: str, provider: str) -> None:
        """이미지 경로에 소스 메타데이터를 바인딩한다."""
        if not path:
            return
        normalized_kind = str(source_kind or "unknown").strip().lower() or "unknown"
        normalized_provider = str(provider or "unknown").strip().lower() or "unknown"
        self.source_kind_by_path[str(path)] = normalized_kind
        self.provider_by_path[str(path)] = normalized_provider

    def append_generation_log(
        self,
        *,
        slot_id: str,
        slot_role: str,
        provider: str,
        status: str,
        source_kind: str = "unknown",
        latency_ms: float = 0.0,
        fallback_reason: str = "",
        cost_usd: float = 0.0,
        source_url: str = "",
    ) -> None:
        """슬롯 단위 이미지 생성 로그를 저장한다."""
        self.generation_logs.append(
            {
                "slot_id": str(slot_id or "").strip(),
                "slot_role": str(slot_role or "").strip().lower() or "content",
                "provider": str(provider or "").strip().lower() or "unknown",
                "status": str(status or "").strip().lower() or "failed",
                "source_kind": str(source_kind or "").strip().lower() or "unknown",
                "latency_ms": float(latency_ms or 0.0),
                "fallback_reason": str(fallback_reason or "").strip(),
                "cost_usd": float(cost_usd or 0.0),
                "source_url": str(source_url or "").strip(),
            }
        )


class ImageGenerator:
    """썸네일/본문 이미지를 생성한다.

    다중 프로바이더 폴백과 병렬 생성을 지원한다.
    폴백 순서: Pollinations → HuggingFace → Together.ai → Placeholder

    토픽별 이미지 소스 전략:
    - cafe, parenting: 본문 이미지에 스톡 포토 우선 (실사 선호)
    - it, finance: AI 생성 + 스톡 포토 혼합
    """

    # 토픽별 본문 이미지 소스 전략
    TOPIC_IMAGE_STRATEGY = {
        "cafe": "stock_first",      # 커피, 카페 인테리어 → 실사 우선
        "parenting": "stock_first", # 아이, 육아용품 → 실사 우선
        "it": "mixed",              # 기술, IT → AI + 스톡 혼합
        "finance": "mixed",         # 금융, 차트 → AI + 스톡 혼합
        "economy": "mixed",         # finance 별칭
    }

    def __init__(
        self,
        client: Optional[PollinationsImageClient] = None,
        fallback_clients: Optional[List[Any]] = None,
        stock_client: Optional[Any] = None,
        thumbnail_style: str = "van_gogh_duotone",
        content_style: str = "monet_soft",
        thumbnail_size: str = "1024*1024",
        content_size: str = "1024*768",
        max_content_images: int = 4,
        prompt_translator: Optional[Any] = None,
        parallel: bool = True,
        topic_mode: str = "cafe",
        content_strategy_override: Optional[str] = None,
        ai_image_quota: Optional[str] = None,
        ai_topic_quota_overrides: Optional[Dict[str, str]] = None,
        ai_engine_id: str = "",
        free_tier_daily_limit: int = 0,
        job_store: Optional[Any] = None,
    ):
        self.client = client or PollinationsImageClient()
        self.fallback_clients: List[Any] = fallback_clients or []
        self.stock_client: Optional[Any] = stock_client
        self.thumbnail_style = get_thumbnail_style(thumbnail_style)
        self.content_style = get_content_style(content_style)
        self.thumbnail_size = thumbnail_size
        self.content_size = content_size
        self.max_content_images = max(0, max_content_images)
        # Gemini 등 LLM 클라이언트 (한국어 → 영어 프롬프트 번역용)
        self.prompt_translator: Optional[BaseLLMClient] = prompt_translator
        self.parallel = parallel
        self.topic_mode = topic_mode
        normalized_strategy = str(content_strategy_override or "").strip().lower()
        self.content_strategy_override = (
            normalized_strategy if normalized_strategy in {"stock_first", "mixed", "ai_only"} else None
        )
        self.ai_image_quota = self._normalize_ai_quota(ai_image_quota)
        raw_topic_quota_overrides = ai_topic_quota_overrides or {}
        self.ai_topic_quota_overrides = {
            str(key).strip().lower(): self._normalize_ai_quota(value) or "0"
            for key, value in raw_topic_quota_overrides.items()
            if str(key).strip()
        }
        self.ai_engine_id = str(ai_engine_id or "").strip().lower()
        self.free_tier_daily_limit = max(0, int(free_tier_daily_limit or 0))
        self.job_store = job_store

    async def generate_for_post(
        self,
        title: str,
        keywords: List[str],
        image_prompts: Optional[List[str]] = None,
        image_slots: Optional[List[Dict[str, Any]]] = None,
        topic_mode: Optional[str] = None,
    ) -> GeneratedImages:
        """포스트용 썸네일/본문 이미지를 생성한다.

        병렬 모드가 활성화되면 모든 이미지를 동시에 생성한다.
        토픽별 전략에 따라 본문 이미지는 스톡 포토를 우선 사용할 수 있다.
        """
        generated = GeneratedImages()
        if self.ai_image_quota is not None:
            return await self._generate_for_post_with_quota(
                generated=generated,
                title=title,
                keywords=keywords,
                image_prompts=image_prompts,
                image_slots=image_slots,
                topic_mode=topic_mode,
            )

        strategy = self.content_strategy_override or self.TOPIC_IMAGE_STRATEGY.get(self.topic_mode, "mixed")

        # 프롬프트 준비
        thumbnail_prompt = await self._build_thumbnail_prompt(title, keywords)
        content_prompts: List[str] = []

        normalized_slots = self._normalize_image_slots(image_slots)
        if normalized_slots:
            thumbnail_slots = [slot for slot in normalized_slots if slot.get("slot_role") == "thumbnail"]
            if thumbnail_slots:
                thumbnail_concept = str(thumbnail_slots[0].get("prompt", "")).strip()
                if thumbnail_concept:
                    thumbnail_prompt = await self._build_thumbnail_prompt_from_concept(thumbnail_concept)

            content_slots = [slot for slot in normalized_slots if slot.get("slot_role") == "content"]
            for raw_slot in content_slots[: self.max_content_images]:
                slot_prompt = str(raw_slot.get("prompt", "")).strip()
                if slot_prompt:
                    content_prompts.append(await self._build_content_prompt(slot_prompt))
        elif image_prompts:
            for raw_prompt in image_prompts[: self.max_content_images]:
                content_prompts.append(await self._build_content_prompt(raw_prompt))

        # 키워드 기반 스톡 포토 검색어 준비
        stock_queries = self._prepare_stock_queries(keywords, content_prompts)

        if self.parallel:
            # 병렬 생성
            tasks = [
                self._generate_with_fallback(
                    thumbnail_prompt, self.thumbnail_style.suffix, self.thumbnail_size
                )
            ]

            # 본문 이미지 태스크 추가 (전략에 따라 다름)
            for idx, prompt in enumerate(content_prompts):
                stock_query = stock_queries[idx] if idx < len(stock_queries) else None
                tasks.append(
                    self._generate_content_image(prompt, strategy, stock_query, idx)
                )

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 결과 처리
            if not isinstance(results[0], Exception):
                thumb_result, source_kind, provider = results[0]
                if thumb_result.success:
                    generated.thumbnail_path = thumb_result.local_path
                    generated.bind_source_meta(
                        path=thumb_result.local_path,
                        source_kind=source_kind,
                        provider=provider,
                    )
                    logger.info(
                        "Thumbnail generated",
                        extra={
                            "path": thumb_result.local_path,
                            "source_kind": source_kind,
                            "provider": provider,
                        },
                    )

            for result in results[1:]:
                if isinstance(result, Exception):
                    continue
                image_result, source_kind, provider = result
                if image_result.success and image_result.local_path:
                    generated.content_paths.append(image_result.local_path)
                    generated.bind_source_meta(
                        path=image_result.local_path,
                        source_kind=source_kind,
                        provider=provider,
                    )
                    logger.info(
                        "Content image generated",
                        extra={
                            "path": image_result.local_path,
                            "source_kind": source_kind,
                            "provider": provider,
                        },
                    )
        else:
            # 순차 생성
            thumbnail_result, thumb_source_kind, thumb_provider = await self._generate_with_fallback(
                thumbnail_prompt, self.thumbnail_style.suffix, self.thumbnail_size
            )
            if thumbnail_result.success and thumbnail_result.local_path:
                generated.thumbnail_path = thumbnail_result.local_path
                generated.bind_source_meta(
                    path=thumbnail_result.local_path,
                    source_kind=thumb_source_kind,
                    provider=thumb_provider,
                )
                logger.info(
                    "Thumbnail generated",
                    extra={
                        "path": thumbnail_result.local_path,
                        "source_kind": thumb_source_kind,
                        "provider": thumb_provider,
                    },
                )

            for idx, prompt in enumerate(content_prompts):
                stock_query = stock_queries[idx] if idx < len(stock_queries) else None
                content_result, source_kind, provider = await self._generate_content_image(
                    prompt,
                    strategy,
                    stock_query,
                    idx,
                )
                if content_result.success and content_result.local_path:
                    generated.content_paths.append(content_result.local_path)
                    generated.bind_source_meta(
                        path=content_result.local_path,
                        source_kind=source_kind,
                        provider=provider,
                    )
                    logger.info(
                        "Content image generated",
                        extra={
                            "path": content_result.local_path,
                            "source_kind": source_kind,
                            "provider": provider,
                        },
                    )

        return generated

    async def _generate_for_post_with_quota(
        self,
        *,
        generated: GeneratedImages,
        title: str,
        keywords: List[str],
        image_prompts: Optional[List[str]],
        image_slots: Optional[List[Dict[str, Any]]],
        topic_mode: Optional[str],
    ) -> GeneratedImages:
        """quota 기반 슬롯 할당 전략으로 이미지를 생성한다."""
        slots = await self._build_slots_for_quota(
            title=title,
            keywords=keywords,
            image_prompts=image_prompts,
            image_slots=image_slots,
        )
        if not slots:
            return generated

        effective_quota = self._resolve_ai_quota_for_topic(topic_mode)
        ai_slot_ids = self._select_ai_slot_ids(slots, effective_quota)
        content_slots = [slot for slot in slots if str(slot.get("slot_role", "")) == "content"]
        stock_queries = self._prepare_stock_queries(
            keywords=keywords,
            content_prompts=[str(slot.get("prompt", "")).strip() for slot in content_slots],
        )
        content_query_map: Dict[str, str] = {}
        for idx, slot in enumerate(content_slots):
            slot_id = str(slot.get("slot_id", "")).strip()
            if not slot_id:
                continue
            if idx < len(stock_queries):
                content_query_map[slot_id] = stock_queries[idx]

        async def run_slot(slot: Dict[str, Any]) -> Dict[str, Any]:
            slot_id = str(slot.get("slot_id", "")).strip()
            slot_role = str(slot.get("slot_role", "content")).strip().lower()
            prompt = str(slot.get("prompt", "")).strip()
            render_prompt = str(slot.get("render_prompt", prompt)).strip() or prompt
            size = self.thumbnail_size if slot_role == "thumbnail" else self.content_size
            style_suffix = self.thumbnail_style.suffix if slot_role == "thumbnail" else self.content_style.suffix
            stock_query = content_query_map.get(slot_id, prompt)
            assign_ai = slot_id in ai_slot_ids
            start = perf_counter()

            if assign_ai:
                image_result, source_kind, provider, detail = await self._generate_ai_slot(
                    prompt=render_prompt,
                    stock_query=stock_query,
                    style_suffix=style_suffix,
                    size=size,
                )
            else:
                image_result, source_kind, provider, detail = await self._generate_stock_slot(
                    prompt=render_prompt,
                    stock_query=stock_query,
                    style_suffix=style_suffix,
                    size=size,
                )

            latency_ms = float(detail.get("latency_ms", 0.0) or 0.0)
            if latency_ms <= 0.0:
                latency_ms = round((perf_counter() - start) * 1000, 2)

            return {
                "slot_id": slot_id,
                "slot_role": slot_role,
                "image_result": image_result,
                "source_kind": source_kind,
                "provider": provider,
                "detail": detail,
                "latency_ms": latency_ms,
            }

        if self.parallel:
            raw_results = await asyncio.gather(
                *(run_slot(slot) for slot in slots),
                return_exceptions=True,
            )
        else:
            raw_results: List[Any] = []
            for slot in slots:
                raw_results.append(await run_slot(slot))

        for item in raw_results:
            if isinstance(item, Exception):
                logger.warning("Slot image generation task failed: %s", item)
                continue

            slot_id = str(item.get("slot_id", "")).strip()
            slot_role = str(item.get("slot_role", "content")).strip().lower()
            image_result = item.get("image_result")
            source_kind = str(item.get("source_kind", "unknown")).strip().lower() or "unknown"
            provider = str(item.get("provider", "unknown")).strip().lower() or "unknown"
            detail = item.get("detail", {}) if isinstance(item.get("detail"), dict) else {}
            latency_ms = float(item.get("latency_ms", 0.0) or 0.0)
            fallback_reason = str(detail.get("fallback_reason", "")).strip()
            status = str(detail.get("status", "failed")).strip().lower() or "failed"
            source_url = ""
            if image_result is not None:
                source_url = str(getattr(image_result, "image_url", "") or "").strip()

            if image_result is not None and getattr(image_result, "success", False) and getattr(image_result, "local_path", None):
                local_path = str(getattr(image_result, "local_path"))
                if slot_role == "thumbnail" and not generated.thumbnail_path:
                    generated.thumbnail_path = local_path
                elif slot_role == "content":
                    generated.content_paths.append(local_path)
                generated.bind_source_meta(
                    path=local_path,
                    source_kind=source_kind,
                    provider=provider,
                )
                if status == "failed":
                    status = "success"

            generated.append_generation_log(
                slot_id=slot_id,
                slot_role=slot_role,
                provider=provider,
                status=status,
                source_kind=source_kind,
                latency_ms=latency_ms,
                fallback_reason=fallback_reason,
                cost_usd=self._estimate_cost_usd(provider),
                source_url=source_url,
            )

            if bool(detail.get("free_tier_exhausted", False)):
                generated.free_tier_exhausted = True
                generated.free_tier_exhausted_events.append(
                    {
                        "provider": str(detail.get("exhausted_provider", provider)).strip().lower() or provider,
                        "slot_id": slot_id,
                        "reason": str(detail.get("free_tier_reason", "") or fallback_reason),
                    }
                )

        return generated

    async def _build_slots_for_quota(
        self,
        *,
        title: str,
        keywords: List[str],
        image_prompts: Optional[List[str]],
        image_slots: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """quota 전략에 사용할 슬롯 목록을 구성한다."""
        slots: List[Dict[str, Any]] = []
        normalized_slots = self._normalize_image_slots(image_slots)

        thumbnail_added = False
        content_count = 0
        for raw_slot in normalized_slots:
            slot_role = str(raw_slot.get("slot_role", "content")).strip().lower()
            slot_prompt = str(raw_slot.get("prompt", "")).strip()
            if not slot_prompt:
                continue
            if slot_role == "thumbnail":
                if thumbnail_added:
                    continue
                render_prompt = await self._build_thumbnail_prompt_from_concept(slot_prompt)
                thumbnail_added = True
            else:
                if content_count >= self.max_content_images:
                    continue
                render_prompt = await self._build_content_prompt(slot_prompt)
                content_count += 1
            slot_payload = dict(raw_slot)
            slot_payload["slot_role"] = slot_role
            slot_payload["prompt"] = slot_prompt
            slot_payload["render_prompt"] = render_prompt
            slots.append(slot_payload)

        if not thumbnail_added:
            thumbnail_prompt = await self._build_thumbnail_prompt(title, keywords)
            slots.insert(
                0,
                {
                    "slot_id": "thumb_0",
                    "slot_role": "thumbnail",
                    "prompt": title,
                    "render_prompt": thumbnail_prompt,
                    "preferred_type": "real",
                    "recommended": False,
                    "ai_generation_score": 0,
                    "reason": "default_thumbnail_slot",
                },
            )

        if content_count == 0 and image_prompts:
            for idx, raw_prompt in enumerate(image_prompts[: self.max_content_images], start=1):
                prompt = str(raw_prompt or "").strip()
                if not prompt:
                    continue
                slots.append(
                    {
                        "slot_id": f"content_{idx}",
                        "slot_role": "content",
                        "prompt": prompt,
                        "render_prompt": await self._build_content_prompt(prompt),
                        "preferred_type": "real",
                        "recommended": False,
                        "ai_generation_score": 0,
                        "reason": "legacy_content_prompt",
                    }
                )
        return slots

    def _normalize_ai_quota(self, raw_quota: Optional[str]) -> Optional[str]:
        """AI 이미지 quota 값을 정규화한다."""
        if raw_quota is None:
            return None
        value = str(raw_quota).strip().lower()
        if value in {"1", "all"}:
            return value
        return "0"

    def _resolve_ai_quota_for_topic(self, topic_mode: Optional[str]) -> str:
        """토픽별 override를 반영한 유효 quota를 계산한다."""
        base_quota = self._normalize_ai_quota(self.ai_image_quota) or "0"
        normalized_topic = str(topic_mode or self.topic_mode or "").strip().lower()
        if not normalized_topic:
            return base_quota
        return self.ai_topic_quota_overrides.get(normalized_topic, base_quota)

    def _quota_to_count(self, quota_value: str) -> int:
        """quota 문자열을 슬롯 수로 변환한다."""
        normalized = self._normalize_ai_quota(quota_value) or "0"
        if normalized == "1":
            return 1
        if normalized == "all":
            return 4
        return 0

    def _select_ai_slot_ids(self, slots: List[Dict[str, Any]], quota_value: str) -> set[str]:
        """슬롯 메타를 기반으로 AI 생성 대상을 선택한다."""
        quota_count = self._quota_to_count(quota_value)
        if quota_count <= 0:
            return set()

        ranked: List[Tuple[Tuple[int, int, int], str]] = []
        for slot in slots:
            slot_id = str(slot.get("slot_id", "")).strip()
            if not slot_id:
                continue
            score_raw = slot.get("ai_generation_score", 0)
            try:
                score = int(score_raw)
            except (TypeError, ValueError):
                score = 0
            score = max(0, min(100, score))
            recommended = bool(slot.get("recommended", False))
            preferred_type = str(slot.get("preferred_type", "real")).strip().lower()
            slot_role = str(slot.get("slot_role", "content")).strip().lower()

            if score > 0:
                level = 0
            elif recommended:
                level = 1
            elif preferred_type == "ai_generated":
                level = 2
            else:
                level = 3

            if level >= 3:
                continue

            # content 우선: content=0, thumbnail=1
            role_rank = 0 if slot_role == "content" else 1
            ranked.append(((level, -score, role_rank), slot_id))

        ranked.sort(key=lambda item: item[0])
        selected = [slot_id for _, slot_id in ranked[:quota_count]]
        return set(selected)

    async def _generate_ai_slot(
        self,
        *,
        prompt: str,
        stock_query: str,
        style_suffix: str,
        size: str,
    ) -> Tuple["ImageResult", str, str, Dict[str, Any]]:
        """AI 슬롯 생성 후 필요 시 Pexels로 폴백한다."""
        result, source_kind, provider, detail = await self._generate_with_fallback_detail(
            prompt=prompt,
            style_suffix=style_suffix,
            size=size,
        )

        if bool(detail.get("free_tier_exhausted", False)) and self.stock_client is not None:
            try:
                stock_result = await self.stock_client.generate(
                    prompt=stock_query,
                    size=size,
                )
                if stock_result.success:
                    stock_provider = self._provider_name_from_client(self.stock_client)
                    detail["status"] = "fallback"
                    detail["fallback_reason"] = "free_tier_exhausted"
                    return stock_result, "stock", stock_provider, detail
            except Exception as exc:
                logger.warning("Stock fallback after free tier exhaustion failed: %s", exc)

        if result.success:
            detail["status"] = "fallback" if bool(detail.get("used_fallback", False)) else "success"
        else:
            detail["status"] = "failed"
            if not detail.get("fallback_reason"):
                detail["fallback_reason"] = "ai_generation_failed"
        return result, source_kind, provider, detail

    async def _generate_stock_slot(
        self,
        *,
        prompt: str,
        stock_query: str,
        style_suffix: str,
        size: str,
    ) -> Tuple["ImageResult", str, str, Dict[str, Any]]:
        """스톡 슬롯 생성 후 실패 시 AI로 폴백한다."""
        from .dashscope_image_client import ImageResult

        if self.stock_client is not None:
            try:
                stock_result = await self.stock_client.generate(
                    prompt=stock_query,
                    size=size,
                )
                if stock_result.success:
                    provider = self._provider_name_from_client(self.stock_client)
                    return stock_result, "stock", provider, {"status": "success"}
            except Exception as exc:
                logger.warning("Stock image provider failed: %s", exc)

        result, source_kind, provider, detail = await self._generate_with_fallback_detail(
            prompt=prompt,
            style_suffix=style_suffix,
            size=size,
        )
        if result.success:
            detail["status"] = "fallback"
            detail["fallback_reason"] = detail.get("fallback_reason", "stock_unavailable")
            return result, source_kind, provider, detail

        return (
            ImageResult(success=False, error_message="Stock and AI generation failed"),
            "unknown",
            "unknown",
            {
                "status": "failed",
                "fallback_reason": "stock_and_ai_failed",
                "free_tier_exhausted": bool(detail.get("free_tier_exhausted", False)),
                "exhausted_provider": str(detail.get("exhausted_provider", "")).strip().lower(),
                "free_tier_reason": str(detail.get("free_tier_reason", "")).strip(),
            },
        )

    def _normalize_image_slots(
        self,
        image_slots: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """image_slots 입력을 내부 표준 형태로 정규화한다."""
        if not isinstance(image_slots, list):
            return []
        normalized: List[Dict[str, Any]] = []
        for index, raw_slot in enumerate(image_slots[:4]):
            if not isinstance(raw_slot, dict):
                continue
            prompt = str(raw_slot.get("prompt", "")).strip()
            if not prompt:
                continue
            slot_role_raw = str(raw_slot.get("slot_role", "content")).strip().lower()
            slot_role = "thumbnail" if slot_role_raw == "thumbnail" else "content"
            slot_id = str(raw_slot.get("slot_id", "")).strip() or f"{slot_role}_{index}"
            preferred_raw = str(raw_slot.get("preferred_type", "real")).strip().lower()
            preferred_type = "ai_generated" if preferred_raw == "ai_generated" else "real"
            recommended_raw = raw_slot.get("recommended", False)
            if isinstance(recommended_raw, bool):
                recommended = recommended_raw
            else:
                recommended = str(recommended_raw).strip().lower() in {"1", "true", "yes", "on"}
            score_raw = raw_slot.get("ai_generation_score", 0)
            try:
                score = int(score_raw)
            except (TypeError, ValueError):
                score = 0
            score = max(0, min(100, score))
            reason = str(raw_slot.get("reason", "")).strip()
            normalized.append(
                {
                    "slot_id": slot_id,
                    "slot_role": slot_role,
                    "prompt": prompt,
                    "preferred_type": preferred_type,
                    "recommended": recommended,
                    "ai_generation_score": score,
                    "reason": reason,
                }
            )
        return normalized

    async def _generate_content_image(
        self,
        prompt: str,
        strategy: str,
        stock_query: Optional[str] = None,
        image_index: int = 0,
    ) -> Tuple["ImageResult", str, str]:
        """본문 이미지를 전략에 따라 생성한다.

        Args:
            prompt: AI 이미지 생성 프롬프트
            strategy: 'stock_first', 'mixed', 'ai_only'
            stock_query: 스톡 포토 검색어 (없으면 prompt에서 추출)
            image_index: 생성 중인 본문 이미지의 인덱스 (mixed 전략에 사용)
        """
        from .dashscope_image_client import ImageResult

        # 스톡 클라이언트가 없거나 ai_only 전략이면 AI 생성만 사용
        if not self.stock_client or strategy == "ai_only":
            return await self._generate_with_fallback(
                prompt, self.content_style.suffix, self.content_size
            )

        query = stock_query or prompt

        if strategy == "stock_first":
            # 스톡 포토 우선, 실패 시 AI 폴백
            stock_result = await self.stock_client.generate(
                prompt=query,
                size=self.content_size,
            )
            if stock_result.success:
                provider = self._provider_name_from_client(self.stock_client)
                logger.info(
                    "Content image from stock photo",
                    extra={
                        "query": query[:50],
                        "path": stock_result.local_path,
                        "source_kind": "stock",
                        "provider": provider,
                    },
                )
                return stock_result, "stock", provider

            # 스톡 실패 → AI 폴백
            logger.info("Stock photo failed, falling back to AI generation")
            return await self._generate_with_fallback(
                prompt, self.content_style.suffix, self.content_size
            )

        # mixed 전략: 짝수 인덱스는 스톡, 홀수는 AI (첫 본문은 스톡)
        if strategy == "mixed":
            if image_index % 2 == 0:
                # 스톡 시도 후 실패하면 AI
                stock_result = await self.stock_client.generate(
                    prompt=query,
                    size=self.content_size,
                )
                if stock_result.success:
                    provider = self._provider_name_from_client(self.stock_client)
                    return stock_result, "stock", provider
                return await self._generate_with_fallback(
                    prompt, self.content_style.suffix, self.content_size
                )
            else:
                # 홀수인덱스 = 무조건 AI 
                return await self._generate_with_fallback(
                    prompt, self.content_style.suffix, self.content_size
                )
                
        # 기타 혹은 fallback 스톡 시도
        stock_result = await self.stock_client.generate(
            prompt=query,
            size=self.content_size,
        )
        if stock_result.success:
            provider = self._provider_name_from_client(self.stock_client)
            return stock_result, "stock", provider

        return await self._generate_with_fallback(
            prompt, self.content_style.suffix, self.content_size
        )

    def _prepare_stock_queries(
        self,
        keywords: List[str],
        content_prompts: List[str],
    ) -> List[str]:
        """스톡 포토 검색에 최적화된 쿼리를 준비한다."""
        queries = []

        # 키워드 기반 쿼리 (영어로 변환 권장)
        keyword_query = " ".join(keywords[:3]) if keywords else ""

        for prompt in content_prompts:
            # 프롬프트에서 핵심 개념 추출
            if ":" in prompt:
                # "concept: description" 형식이면 concept 부분 사용
                concept = prompt.split(":")[1].strip().split(".")[0]
            else:
                concept = prompt.split(",")[0].strip()

            # 키워드와 개념 조합
            query = f"{keyword_query} {concept}".strip()
            queries.append(query[:100])  # 너무 긴 쿼리 방지

        return queries

    async def _generate_with_fallback(
        self, prompt: str, style_suffix: str, size: str
    ) -> Tuple["ImageResult", str, str]:
        """폴백 체인을 통해 이미지를 생성한다.

        Pollinations → HuggingFace → Together.ai → (Pollinations placeholder)
        """
        result, source_kind, provider, _ = await self._generate_with_fallback_detail(
            prompt=prompt,
            style_suffix=style_suffix,
            size=size,
        )
        return result, source_kind, provider

    async def _generate_with_fallback_detail(
        self,
        *,
        prompt: str,
        style_suffix: str,
        size: str,
    ) -> Tuple["ImageResult", str, str, Dict[str, Any]]:
        """폴백 체인을 통해 이미지를 생성하고 상세 메타를 반환한다."""
        clients = [self.client] + self.fallback_clients
        last_placeholder_result: Optional[Tuple["ImageResult", str, str]] = None
        free_tier_exhausted = False
        exhausted_provider = ""
        free_tier_reason = ""
        used_fallback = False
        fallback_index = 0
        last_error_message = ""

        for idx, client in enumerate(clients):
            provider_name = self._provider_name_from_client(client)
            if self._is_free_tier_daily_limit_reached(provider_name):
                free_tier_exhausted = True
                exhausted_provider = provider_name
                free_tier_reason = "daily_limit_reached"
                logger.warning("Free-tier daily limit reached: provider=%s", provider_name)
                continue

            try:
                result = await client.generate(
                    prompt=prompt,
                    style_suffix=style_suffix,
                    size=size,
                )
                if not result.success:
                    error_message = str(result.error_message or "").strip()
                    if self._is_free_tier_exhausted_error(provider_name, error_message):
                        free_tier_exhausted = True
                        exhausted_provider = provider_name
                        free_tier_reason = error_message[:200]
                        logger.warning(
                            "Free-tier exhaustion detected from response: provider=%s error=%s",
                            provider_name,
                            error_message[:120],
                        )
                    last_error_message = error_message
                    continue

                if result.success:
                    source_kind = self._infer_source_kind(
                        provider_name,
                        result.local_path,
                        getattr(result, "image_url", ""),
                    )
                    if self._is_free_provider(provider_name):
                        self._increment_free_tier_daily_usage(provider_name)

                    # 플레이스홀더인 경우, 다음 폴백이 있으면 계속 시도
                    # 실제 파일명은 "placeholder_xxx.jpg" 패턴이므로 source_kind도 함께 본다.
                    local_path_text = str(result.local_path or "").strip().lower()
                    is_placeholder = (
                        source_kind == "placeholder"
                        or "placeholder_" in local_path_text
                        or "_placeholder" in local_path_text
                    )
                    if is_placeholder and idx < len(clients) - 1:
                        logger.info(
                            "Skipping placeholder, trying next fallback",
                            extra={
                                "provider": provider_name,
                                "source_kind": source_kind,
                            },
                        )
                        last_placeholder_result = (result, source_kind, provider_name)
                        continue

                    if idx > 0:
                        used_fallback = True
                        fallback_index = idx
                        logger.info(
                            "Image generated via fallback",
                            extra={
                                "provider": provider_name,
                                "fallback_idx": idx,
                                "source_kind": source_kind,
                            },
                        )
                    return (
                        result,
                        source_kind,
                        provider_name,
                        {
                            "used_fallback": used_fallback,
                            "fallback_index": fallback_index,
                            "free_tier_exhausted": free_tier_exhausted,
                            "exhausted_provider": exhausted_provider,
                            "free_tier_reason": free_tier_reason,
                            "status": "success",
                        },
                    )
            except Exception as exc:
                error_message = str(exc)
                if self._is_free_tier_exhausted_error(provider_name, error_message):
                    free_tier_exhausted = True
                    exhausted_provider = provider_name
                    free_tier_reason = error_message[:200]
                    logger.warning(
                        "Free-tier exhaustion detected from exception: provider=%s error=%s",
                        provider_name,
                        error_message[:120],
                    )
                last_error_message = error_message
                logger.warning(
                    "Image provider %s failed: %s",
                    client.__class__.__name__,
                    exc,
                )

        # 모든 클라이언트 실패 시, 마지막 플레이스홀더가 있으면 그것을 반환
        if last_placeholder_result:
            logger.warning("All providers failed, using placeholder")
            result, source_kind, provider_name = last_placeholder_result
            return (
                result,
                source_kind,
                provider_name,
                {
                    "used_fallback": True,
                    "fallback_index": len(clients),
                    "free_tier_exhausted": free_tier_exhausted,
                    "exhausted_provider": exhausted_provider,
                    "free_tier_reason": free_tier_reason,
                    "status": "fallback",
                    "fallback_reason": "placeholder_fallback",
                },
            )

        from .dashscope_image_client import ImageResult
        return (
            ImageResult(success=False, error_message=last_error_message or "All image providers failed"),
            "unknown",
            "unknown",
            {
                "used_fallback": False,
                "fallback_index": len(clients),
                "free_tier_exhausted": free_tier_exhausted,
                "exhausted_provider": exhausted_provider,
                "free_tier_reason": free_tier_reason,
                "status": "failed",
                "fallback_reason": "all_providers_failed",
            },
        )

    def _is_free_tier_exhausted_error(self, provider_name: str, error_message: str) -> bool:
        """무료 티어 소진으로 볼 수 있는 에러를 판별한다."""
        if not self._is_free_provider(provider_name):
            return False
        normalized = str(error_message or "").strip().lower()
        if not normalized:
            return False
        if re.search(r"\b(402|403|429)\b", normalized):
            return True
        exhausted_tokens = ("quota", "rate limit", "too many requests", "payment required", "credit")
        return any(token in normalized for token in exhausted_tokens)

    def _is_free_provider(self, provider_name: str) -> bool:
        """무료 티어 사용량 추적 대상 프로바이더인지 확인한다."""
        provider = str(provider_name or "").strip().lower()
        return provider in {"together_flux", "together"}

    def _daily_usage_key(self, provider_name: str) -> str:
        """프로바이더별 일일 사용량 설정 키를 생성한다."""
        kst_date = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
        provider = str(provider_name or "unknown").strip().lower() or "unknown"
        return f"image_free_tier_usage_{provider}_{kst_date}"

    def _is_free_tier_daily_limit_reached(self, provider_name: str) -> bool:
        """일일 무료 티어 상한 도달 여부를 확인한다."""
        if not self._is_free_provider(provider_name) or self.free_tier_daily_limit <= 0:
            return False
        if self.job_store is None:
            return False
        get_setting = getattr(self.job_store, "get_system_setting", None)
        if not get_setting or not callable(get_setting):
            return False
        raw_count = str(get_setting(self._daily_usage_key(provider_name), "0")).strip()
        try:
            count = int(raw_count)
        except ValueError:
            count = 0
        return count >= self.free_tier_daily_limit

    def _increment_free_tier_daily_usage(self, provider_name: str) -> None:
        """일일 무료 티어 사용량 카운터를 증가시킨다."""
        if not self._is_free_provider(provider_name) or self.free_tier_daily_limit <= 0:
            return
        if self.job_store is None:
            return
        get_setting = getattr(self.job_store, "get_system_setting", None)
        set_setting = getattr(self.job_store, "set_system_setting", None)
        if not callable(get_setting) or not callable(set_setting):
            return
        key = self._daily_usage_key(provider_name)
        raw_count = str(get_setting(key, "0")).strip()
        try:
            current = int(raw_count)
        except ValueError:
            current = 0
        set_setting(key, str(current + 1))

    def _provider_name_from_client(self, client: Any) -> str:
        """클라이언트 객체에서 provider 이름을 추출한다."""
        if client is None:
            return "unknown"
        raw_name = getattr(client, "__class__", type(client)).__name__
        normalized = str(raw_name or "unknown").strip().lower()
        if normalized.endswith("client"):
            normalized = normalized[:-6]
        if "together" in normalized:
            return "together_flux"
        if "fal" in normalized:
            return "fal_flux"
        if "openai" in normalized:
            return "openai_dalle3"
        if "pexels" in normalized:
            return "pexels"
        if "pollinations" in normalized:
            return "pollinations"
        if "huggingface" in normalized:
            return "huggingface"
        if "dashscope" in normalized:
            return "dashscope"
        return normalized or "unknown"

    def _estimate_cost_usd(self, provider_name: str) -> float:
        """프로바이더 기준 대략적인 이미지 단가(USD)를 반환한다."""
        provider = str(provider_name or "").strip().lower()
        if provider == "fal_flux":
            return 0.003
        if provider == "openai_dalle3":
            return 0.0415
        return 0.0

    def _infer_source_kind(
        self,
        provider_name: str,
        local_path: Optional[str],
        image_url: Optional[str] = None,
    ) -> str:
        """provider/결과 경로를 기반으로 이미지 소스 종류를 추론한다."""
        provider = str(provider_name or "").strip().lower()
        path_name = str(local_path or "").strip().lower()
        url_text = str(image_url or "").strip().lower()

        if "placeholder" in path_name:
            return "placeholder"

        if "pexels" in provider:
            return "stock"
        if url_text.startswith("stock://"):
            return "stock"

        ai_provider_tokens = (
            "together",
            "fal",
            "openai",
            "dashscope",
            "huggingface",
            "pollinations",
        )
        if any(token in provider for token in ai_provider_tokens):
            return "ai"
        if url_text.startswith("ai://"):
            return "ai"

        return "unknown"

    async def _build_thumbnail_prompt(self, title: str, keywords: List[str]) -> str:
        """썸네일용 프롬프트를 생성한다. 블로그 최적화 템플릿 사용."""
        keyword_text = ", ".join(keywords[:3]) if keywords else "lifestyle, tips, guide"

        # 제목과 키워드를 기반으로 개념 추출
        concept = f"{title} - {keyword_text}"

        # 블로그 최적화 템플릿 적용
        base_prompt = _THUMBNAIL_TEMPLATE.format(concept=concept)

        # Gemini 번역기가 있으면 프롬프트 개선
        return await self._translate_prompt(base_prompt)

    async def _build_thumbnail_prompt_from_concept(self, concept: str) -> str:
        """슬롯 concept 기반 썸네일 프롬프트를 생성한다."""
        base_prompt = _THUMBNAIL_TEMPLATE.format(concept=concept)
        return await self._translate_prompt(base_prompt)

    async def _build_content_prompt(self, raw_prompt: str, section_context: str = "") -> str:
        """본문 이미지용 프롬프트를 생성한다. 블로그 최적화 템플릿 사용."""
        # 기본 개념 추출
        concept = raw_prompt if raw_prompt else section_context

        # 블로그 최적화 템플릿 적용
        base_prompt = _CONTENT_IMAGE_TEMPLATE.format(concept=concept)

        # Gemini 번역기가 있으면 프롬프트 개선
        return await self._translate_prompt(base_prompt)

    async def _translate_prompt(self, prompt: str) -> str:
        """Gemini로 프롬프트를 영어로 개선한다. 실패 시 원본 반환."""
        if self.prompt_translator is None:
            return prompt
        try:
            response = await self.prompt_translator.generate(
                system_prompt=_PROMPT_TRANSLATE_SYSTEM,
                user_prompt=prompt,
                temperature=0.4,
                max_tokens=150,
            )
            translated = response.content.strip()
            if translated:
                logger.debug("Prompt translated: %s → %s", prompt[:40], translated[:40])
                return translated
        except Exception as exc:
            logger.warning("Prompt translation failed, using original: %s", exc)
        return prompt

    async def close(self) -> None:
        """내부 클라이언트를 정리한다."""
        await self.client.close()
        for client in self.fallback_clients:
            if hasattr(client, "close"):
                await client.close()
        if self.stock_client and hasattr(self.stock_client, "close"):
            await self.stock_client.close()
