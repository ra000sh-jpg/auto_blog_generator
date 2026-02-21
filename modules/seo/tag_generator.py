"""LLM 기반 플랫폼별 태그 생성기.

LLM4Tag(arxiv:2502.13481) 논문의 3단계 아키텍처를 단순화하여 구현:
1. 콘텐츠 + 키워드 기반 후보 태그 생성
2. 플랫폼 규칙 적용 (네이버: 10-20개, 티스토리: 5-10개)
3. LLM 신뢰도 재검증 (선택적)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from ..llm.base_client import BaseLLMClient

from .platform_strategy import PlatformInFlowStrategy, get_platform_strategy

logger = logging.getLogger(__name__)


_TAG_GENERATION_SYSTEM = (
    "You are a Korean blog SEO specialist. "
    "Generate optimized tags for Korean blog platforms. "
    "Always respond in valid JSON format only."
)

_NAVER_TAG_TEMPLATE = """다음 블로그 글에 최적화된 네이버 블로그 태그를 생성해주세요.

## 블로그 정보
제목: {title}
시드 키워드: {seed_keywords}
주제: {topic_mode}
본문 요약: {content_summary}

## 네이버 블로그 태그 규칙
- 총 {tag_count_min}~{tag_count_max}개 (목표: {tag_count_target}개)
- 한국어 태그 우선 (70%), 영어 허용 (30%)
- 정확한 검색어 그대로 사용 (Naver exact-match 태그 검색)
- 태그 형식: 띄어쓰기 없이 붙여쓰기 권장 (예: 홈카페레시피)
- 시의성 태그 포함 (예: 2026추천, 올해트렌드)

## 태그 구성
1. 핵심 키워드 태그 (3-4개): 검색량 높은 정확한 키워드
2. 연관 키워드 태그 (4-6개): 유사 의미, 변형어
3. 롱테일 태그 (2-4개): 구체적 질문형
4. 카테고리 태그 (1-2개): 주제 분류
5. 시의성 태그 (1-2개): 트렌드 반영

## 응답 형식 (JSON만 출력)
{{
  "tags": ["태그1", "태그2", ...],
  "primary_tag": "가장 중요한 단일 태그",
  "rationale": "태그 선택 이유 한 줄"
}}"""

_TISTORY_TAG_TEMPLATE = """다음 블로그 글에 최적화된 티스토리 태그를 생성해주세요.

## 블로그 정보
제목: {title}
시드 키워드: {seed_keywords}
주제: {topic_mode}
본문 요약: {content_summary}

## 티스토리 태그 규칙
- 총 {tag_count_min}~{tag_count_max}개 (목표: {tag_count_target}개)
- 한국어 + 영어 혼합 가능
- 롱테일 키워드 태그 포함
- 카테고리 역할 태그 포함
- 구글 검색 최적화 (Google E-E-A-T)

## 응답 형식 (JSON만 출력)
{{
  "tags": ["태그1", "태그2", ...],
  "primary_tag": "가장 중요한 단일 태그",
  "rationale": "태그 선택 이유 한 줄"
}}"""

_PLATFORM_TEMPLATES = {
    "naver": _NAVER_TAG_TEMPLATE,
    "tistory": _TISTORY_TAG_TEMPLATE,
}


@dataclass
class TagGenerationResult:
    """태그 생성 결과."""

    tags: List[str] = field(default_factory=list)
    primary_tag: str = ""
    rationale: str = ""
    platform: str = ""
    fallback_used: bool = False  # LLM 실패 시 룰베이스 폴백 여부


class TagGenerator:
    """플랫폼별 태그를 LLM으로 생성한다.

    LLM 실패 시 시드 키워드 기반 룰베이스 폴백을 사용한다.
    """

    def __init__(self, llm_client: Optional["BaseLLMClient"] = None):
        self.llm_client = llm_client

    async def generate(
        self,
        title: str,
        seed_keywords: List[str],
        platform: str,
        topic_mode: str = "",
        content_summary: str = "",
    ) -> TagGenerationResult:
        """플랫폼에 최적화된 태그를 생성한다."""
        strategy = get_platform_strategy(platform)

        if self.llm_client:
            result = await self._generate_with_llm(
                title=title,
                seed_keywords=seed_keywords,
                platform=platform,
                strategy=strategy,
                topic_mode=topic_mode,
                content_summary=content_summary,
            )
            if result:
                return result

        # LLM 없거나 실패 시 룰베이스 폴백
        return self._fallback_tags(
            title=title,
            seed_keywords=seed_keywords,
            platform=platform,
            strategy=strategy,
        )

    async def _generate_with_llm(
        self,
        title: str,
        seed_keywords: List[str],
        platform: str,
        strategy: PlatformInFlowStrategy,
        topic_mode: str,
        content_summary: str,
    ) -> Optional[TagGenerationResult]:
        """LLM에 태그 생성을 요청한다."""
        template = _PLATFORM_TEMPLATES.get(platform.lower(), _NAVER_TAG_TEMPLATE)
        user_prompt = template.format(
            title=title,
            seed_keywords=", ".join(seed_keywords),
            topic_mode=topic_mode or "일반",
            content_summary=content_summary[:200] if content_summary else title,
            tag_count_min=strategy.tag_count_min,
            tag_count_max=strategy.tag_count_max,
            tag_count_target=strategy.tag_count_target(),
        )

        try:
            response = await self.llm_client.generate(
                system_prompt=_TAG_GENERATION_SYSTEM,
                user_prompt=user_prompt,
                temperature=0.3,
                max_tokens=400,
            )
            raw = response.content.strip()
            return self._parse_response(raw, platform)
        except Exception as exc:
            logger.warning("Tag LLM generation failed: %s", exc)
            return None

    def _parse_response(self, raw: str, platform: str) -> Optional[TagGenerationResult]:
        """LLM 응답 JSON을 파싱한다."""
        # JSON 블록 추출
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            logger.warning("No JSON found in tag generation response")
            return None

        try:
            data = json.loads(json_match.group())
            tags = data.get("tags", [])
            if not tags or not isinstance(tags, list):
                return None

            strategy = get_platform_strategy(platform)
            # 태그 수 범위 조정
            tags = [str(t).strip() for t in tags if str(t).strip()]
            tags = tags[: strategy.tag_count_max]

            return TagGenerationResult(
                tags=tags,
                primary_tag=data.get("primary_tag", tags[0] if tags else ""),
                rationale=data.get("rationale", ""),
                platform=platform,
                fallback_used=False,
            )
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            logger.warning("Tag response parse error: %s", exc)
            return None

    def _fallback_tags(
        self,
        title: str,
        seed_keywords: List[str],
        platform: str,
        strategy: PlatformInFlowStrategy,
    ) -> TagGenerationResult:
        """LLM 없이 시드 키워드 기반으로 기본 태그를 생성한다."""
        tags: List[str] = []

        # 1. 시드 키워드 → 태그 (공백 제거 버전 + 원본)
        for kw in seed_keywords:
            kw = kw.strip()
            if kw:
                tags.append(kw)
                # 공백 없는 버전 추가 (네이버 스타일)
                compact = kw.replace(" ", "")
                if compact != kw:
                    tags.append(compact)

        # 2. 제목에서 주요 명사 추출 (단순 공백 분리)
        title_words = [w.strip() for w in title.split() if len(w.strip()) >= 2]
        for word in title_words[:4]:
            if word not in tags:
                tags.append(word)

        # 3. 중복 제거 및 개수 조정
        seen: set = set()
        unique_tags: List[str] = []
        for tag in tags:
            if tag not in seen:
                seen.add(tag)
                unique_tags.append(tag)

        unique_tags = unique_tags[: strategy.tag_count_max]

        return TagGenerationResult(
            tags=unique_tags,
            primary_tag=unique_tags[0] if unique_tags else "",
            rationale="키워드 기반 자동 생성 (LLM 폴백)",
            platform=platform,
            fallback_used=True,
        )
