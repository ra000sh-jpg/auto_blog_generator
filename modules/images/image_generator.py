"""블로그 이미지 생성기."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
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

    def bind_source_meta(self, path: Optional[str], source_kind: str, provider: str) -> None:
        """이미지 경로에 소스 메타데이터를 바인딩한다."""
        if not path:
            return
        normalized_kind = str(source_kind or "unknown").strip().lower() or "unknown"
        normalized_provider = str(provider or "unknown").strip().lower() or "unknown"
        self.source_kind_by_path[str(path)] = normalized_kind
        self.provider_by_path[str(path)] = normalized_provider


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
        max_content_images: int = 2,
        prompt_translator: Optional[Any] = None,
        parallel: bool = True,
        topic_mode: str = "cafe",
        content_strategy_override: Optional[str] = None,
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

    async def generate_for_post(
        self,
        title: str,
        keywords: List[str],
        image_prompts: Optional[List[str]] = None,
    ) -> GeneratedImages:
        """포스트용 썸네일/본문 이미지를 생성한다.

        병렬 모드가 활성화되면 모든 이미지를 동시에 생성한다.
        토픽별 전략에 따라 본문 이미지는 스톡 포토를 우선 사용할 수 있다.
        """
        generated = GeneratedImages()
        strategy = self.content_strategy_override or self.TOPIC_IMAGE_STRATEGY.get(self.topic_mode, "mixed")

        # 프롬프트 준비
        thumbnail_prompt = await self._build_thumbnail_prompt(title, keywords)
        content_prompts: List[str] = []
        if image_prompts:
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
        clients = [self.client] + self.fallback_clients
        last_placeholder_result: Optional[Tuple["ImageResult", str, str]] = None

        for idx, client in enumerate(clients):
            try:
                result = await client.generate(
                    prompt=prompt,
                    style_suffix=style_suffix,
                    size=size,
                )
                if result.success:
                    provider_name = self._provider_name_from_client(client)
                    source_kind = self._infer_source_kind(provider_name, result.local_path)

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
                        logger.info(
                            "Image generated via fallback",
                            extra={
                                "provider": provider_name,
                                "fallback_idx": idx,
                                "source_kind": source_kind,
                            },
                        )
                    return result, source_kind, provider_name
            except Exception as exc:
                logger.warning(
                    "Image provider %s failed: %s",
                    client.__class__.__name__,
                    exc,
                )

        # 모든 클라이언트 실패 시, 마지막 플레이스홀더가 있으면 그것을 반환
        if last_placeholder_result:
            logger.warning("All providers failed, using placeholder")
            return last_placeholder_result

        from .dashscope_image_client import ImageResult
        return (
            ImageResult(success=False, error_message="All image providers failed"),
            "unknown",
            "unknown",
        )

    def _provider_name_from_client(self, client: Any) -> str:
        """클라이언트 객체에서 provider 이름을 추출한다."""
        if client is None:
            return "unknown"
        raw_name = getattr(client, "__class__", type(client)).__name__
        normalized = str(raw_name or "unknown").strip().lower()
        if normalized.endswith("client"):
            normalized = normalized[:-6]
        return normalized or "unknown"

    def _infer_source_kind(self, provider_name: str, local_path: Optional[str]) -> str:
        """provider/결과 경로를 기반으로 이미지 소스 종류를 추론한다."""
        provider = str(provider_name or "").strip().lower()
        path_name = str(local_path or "").strip().lower()

        if "placeholder" in path_name:
            return "placeholder"

        if "pexels" in provider:
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
