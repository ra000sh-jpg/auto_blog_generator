"""LLM 기반 블로그 콘텐츠 생성 파이프라인.

품질 향상 전략:
- A. 프롬프트 고도화 (페르소나, Few-shot, Chain-of-Thought)
- B. 멀티스텝 생성 (아웃라인 → 섹션별 → 통합)
- C. 재작성 루프 (품질 점수 기반 자동 재생성)
- D. 톤/스타일 다양화
- E. 팩트체크 단계
- 이미지 배치 최적화
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..automation.job_store import Job
from ..collectors import RssNewsCollector
from ..constants import DEFAULT_FALLBACK_CATEGORY
from ..exceptions import ContentGenerationError, RateLimitError
from ..rag import CrossEncoderRagSearchEngine
from ..seo.platform_strategy import get_platform_strategy
from .base_client import BaseLLMClient, LLMResponse
from .circuit_breaker import ProviderCircuitBreaker, ProviderCircuitOpenError
from .claude_client import ClaudeClient
from .llm_router import provider_label
from .prompts import (
    ECONOMY_SYSTEM_PROMPT,
    ECONOMY_TOPIC_PROMPT,
    FACT_CHECK_REQUEST,
    FACT_CHECK_REVISION,
    IMAGE_PROMPT_GENERATION,
    OUTLINE_GENERATION,
    QUALITY_LAYER_CONTENT_REQUEST,
    QUALITY_LAYER_ECONOMY_PROMPT,
    QUALITY_LAYER_SYSTEM_PROMPT,
    QUALITY_CHECK,
    REWRITE_REQUEST,
    SECTION_DRAFT,
    SECTION_INTEGRATION,
    SEO_OPTIMIZATION,
    SYSTEM_BLOG_WRITER,
    USER_CONTENT_REQUEST,
    VOICE_REWRITE_REQUEST,
    get_persona_profile,
    get_tone_profile,
    normalize_topic_mode,
)

logger = logging.getLogger(__name__)


@dataclass
class QualityResult:
    """품질 검증 결과."""
    score: int
    gate: str  # pass, retry_mask, retry_all
    breakdown: Dict[str, int] = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)
    improvements: List[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class FactCheckResult:
    """팩트체크 결과."""
    claims: List[Dict[str, Any]] = field(default_factory=list)
    overall_risk: str = "low"
    recommendation: str = ""
    needs_revision: bool = False


@dataclass
class ContentResult:
    """콘텐츠 생성 결과."""
    final_content: str
    quality_gate: str
    quality_snapshot: Dict[str, Any]
    seo_snapshot: Dict[str, Any]
    image_prompts: List[str]
    image_placements: List[Dict[str, Any]] = field(default_factory=list)
    image_slots: List[Dict[str, Any]] = field(default_factory=list)
    llm_calls_used: int = 0
    provider_used: str = ""
    provider_model: str = ""
    provider_fallback_from: str = ""
    generation_method: str = "single"  # single, multistep
    rewrite_count: int = 0
    fact_check_applied: bool = False
    raw_content: str = ""
    voice_rewrite_applied: bool = False
    rag_context: List[Dict[str, str]] = field(default_factory=list)
    llm_token_usage: Dict[str, Dict[str, Any]] = field(default_factory=dict)


class ContentGenerator:
    """블로그 콘텐츠 생성 파이프라인.

    품질 향상 기능:
    - 멀티스텝 생성 모드 (use_multistep=True)
    - 자동 재작성 루프 (max_rewrites > 0)
    - 팩트체크 (enable_fact_check=True)
    - 톤/스타일 선택
    """

    MAIN_SLOT_QUALITY_THRESHOLD = 80
    TEST_SLOT_QUALITY_THRESHOLD = 70
    QUALITY_RETRY_MASK_FLOOR = 60

    def __init__(
        self,
        primary_client: Optional[BaseLLMClient] = None,
        secondary_client: Optional[BaseLLMClient] = None,
        voice_client: Optional[BaseLLMClient] = None,
        client: Optional[BaseLLMClient] = None,
        additional_clients: Optional[List[BaseLLMClient]] = None,
        enable_quality_check: bool = True,
        enable_seo_optimization: bool = True,
        enable_fact_check: bool = False,
        use_multistep: bool = False,
        max_rewrites: int = 2,
        min_quality_score: int = 70,
        temperature: float = 0.7,
        fallback_to_secondary: bool = True,
        max_tokens: int = 4096,
        rss_news_collector: Optional[RssNewsCollector] = None,
        rag_search_engine: Optional[CrossEncoderRagSearchEngine] = None,
        enable_voice_rewrite: bool = True,
        db_path: str = "data/automation.db",
        fallback_alert_fn: Optional[Any] = None,
        circuit_breaker: Optional[ProviderCircuitBreaker] = None,
    ):
        resolved_primary = primary_client or client or ClaudeClient()
        self.primary = resolved_primary
        self.secondary = secondary_client or resolved_primary
        self.voice_client = voice_client or self.secondary
        self.additional_clients: List[BaseLLMClient] = additional_clients or []

        # 기존 설정
        self.enable_quality_check = enable_quality_check
        self.enable_seo_optimization = enable_seo_optimization
        self.temperature = temperature
        self.fallback_to_secondary = fallback_to_secondary
        self.max_tokens = max_tokens

        # 품질 향상 설정
        self.enable_fact_check = enable_fact_check
        self.use_multistep = use_multistep
        self.max_rewrites = max_rewrites
        self.min_quality_score = min_quality_score
        self.rss_news_collector = rss_news_collector or RssNewsCollector()
        self.rag_search_engine = rag_search_engine or CrossEncoderRagSearchEngine(
            news_collector=self.rss_news_collector,
            cross_encoder_model="BAAI/bge-reranker-base",
            candidate_top_k=20,
            final_top_k=2,
            cross_encoder_enabled=True,
        )
        self.enable_voice_rewrite = enable_voice_rewrite
        self.db_path = db_path
        self.fallback_alert_fn = fallback_alert_fn
        self.circuit_breaker = circuit_breaker

    async def generate(
        self,
        job: Job,
        tone: Optional[str] = None,
        persona_id: Optional[str] = None,
    ) -> ContentResult:
        """전체 생성 파이프라인 실행."""
        llm_calls = 0
        provider_used = self.primary.provider_name
        provider_model = ""
        provider_fallback_from = ""
        rewrite_count = 0
        token_usage = self._init_token_usage()

        # 톤/페르소나 설정
        persona = get_persona_profile(persona_id or job.persona_id or "P1")
        tone_profile = get_tone_profile(tone or persona.default_tone)
        topic_mode = self._resolve_topic_mode(job=job, persona=persona)
        saved_voice_profile = self._load_saved_voice_profile(persona_id or job.persona_id or "P1")
        is_idea_vault_job = any(str(tag).lower() == "idea_vault" for tag in (job.tags or []))
        required_quality_score, quality_slot_type = self._resolve_quality_threshold(job)

        news_context: List[Dict[str, str]] = []
        if self._is_economy_topic(topic_mode):
            news_context = self._collect_news_context(job.seed_keywords, max_items=3)
        elif is_idea_vault_job:
            idea_query = [job.title] + list(job.seed_keywords)
            news_context = self._collect_news_context(idea_query, max_items=1)

        # 폴백 체인 구성
        fallback_chain = self._build_fallback_chain()

        # Step 1: 품질 레이어 원문 생성 (Voice 간섭 없음)
        if self.use_multistep:
            draft, provider_model, calls = await self._generate_multistep(
                job,
                persona,
                tone_profile,
                fallback_chain,
                topic_mode=topic_mode,
                news_context=news_context,
                quality_only=True,
                token_usage=token_usage,
            )
            llm_calls += calls
            generation_method = "multistep"
        else:
            draft, provider_model, provider_used, provider_fallback_from = await self._generate_single(
                job,
                persona,
                tone_profile,
                fallback_chain,
                topic_mode=topic_mode,
                news_context=news_context,
                quality_only=True,
                token_usage=token_usage,
            )
            llm_calls += 1
            generation_method = "single"

        raw_content = draft

        # Step 2: 품질 레이어 SEO 최적화
        if self.enable_seo_optimization:
            raw_content = await self._apply_seo(
                raw_content,
                job.seed_keywords,
                self.secondary,
                token_usage=token_usage,
            )
            llm_calls += 1

        # Step 3: 품질 레이어 팩트체크 (전략 E)
        fact_check_applied = False
        if self.enable_fact_check:
            fact_result = await self._fact_check(
                raw_content,
                self.secondary,
                token_usage=token_usage,
            )
            llm_calls += 1
            if fact_result.needs_revision:
                raw_content = await self._apply_fact_revisions(
                    raw_content,
                    fact_result,
                    self.secondary,
                    token_usage=token_usage,
                )
                llm_calls += 1
                fact_check_applied = True

        # Step 4: 품질 레이어 검증 및 재작성 루프 (전략 C)
        quality_result = QualityResult(score=100, gate="pass")
        if self.enable_quality_check:
            for attempt in range(self.max_rewrites + 1):
                quality_result = await self._check_quality(
                    raw_content,
                    job,
                    self.secondary,
                    pass_score_threshold=required_quality_score,
                    token_usage=token_usage,
                )
                llm_calls += 1

                if quality_result.score >= required_quality_score:
                    break

                if attempt < self.max_rewrites:
                    logger.info(
                        "Quality score %d < %d, rewriting (attempt %d/%d)",
                        quality_result.score,
                        required_quality_score,
                        attempt + 1,
                        self.max_rewrites,
                    )
                    raw_content = await self._rewrite_content(
                        raw_content,
                        quality_result,
                        self.secondary,
                        token_usage=token_usage,
                    )
                    llm_calls += 1
                    rewrite_count += 1

        if news_context:
            raw_content = self._append_news_sources(raw_content, news_context)

        # Step 5: Voice 레이어 리라이트 (내용/길이 유지, 말투만 조정)
        content = raw_content
        voice_rewrite_applied = False
        if self.enable_voice_rewrite:
            content = await self._apply_voice_rewrite(
                raw_content=raw_content,
                persona=persona,
                tone_profile=tone_profile,
                voice_profile=saved_voice_profile,
                client=self.voice_client,
                token_usage=token_usage,
            )
            llm_calls += 1
            voice_rewrite_applied = content != raw_content

        # Step 6: 이미지 프롬프트 생성
        image_prompts, image_placements, image_slots = await self._generate_image_prompts(
            content,
            job.title,
            job.seed_keywords,
            self.secondary,
            token_usage=token_usage,
        )
        llm_calls += 1

        # SEO 스냅샷 구성
        seo_snapshot = {
            "keywords": job.seed_keywords,
            "keyword_count": sum(
                content.lower().count(kw.lower()) for kw in job.seed_keywords
            ),
            "provider_used": provider_used,
            "provider_model": provider_model,
            "provider_fallback_from": provider_fallback_from,
            "tone": tone_profile.name,
            "persona": persona.name,
            "platform": job.platform,
            "topic_mode": topic_mode,
            "voice_rewrite_applied": voice_rewrite_applied,
            "voice_profile_loaded": bool(saved_voice_profile),
        }

        return ContentResult(
            final_content=content,
            quality_gate=quality_result.gate,
            quality_snapshot={
                "gate": quality_result.gate,
                "score": quality_result.score,
                "breakdown": quality_result.breakdown,
                "issues": quality_result.issues,
                "improvements": quality_result.improvements,
                "summary": quality_result.summary,
                "raw_content_length": len(raw_content),
                "final_content_length": len(content),
                "required_quality_score": required_quality_score,
                "quality_slot_type": quality_slot_type,
                "pipeline_layers": {
                    "quality_topic_mode": topic_mode,
                    "voice_rewrite_applied": voice_rewrite_applied,
                },
            },
            seo_snapshot=seo_snapshot,
            image_prompts=image_prompts,
            image_placements=image_placements,
            image_slots=image_slots,
            llm_calls_used=llm_calls,
            provider_used=provider_used,
            provider_model=provider_model,
            provider_fallback_from=provider_fallback_from,
            generation_method=generation_method,
            rewrite_count=rewrite_count,
            fact_check_applied=fact_check_applied,
            raw_content=raw_content,
            voice_rewrite_applied=voice_rewrite_applied,
            rag_context=news_context,
            llm_token_usage=token_usage,
        )

    def _init_token_usage(self) -> Dict[str, Dict[str, Any]]:
        """단계별 토큰 집계 버킷을 초기화한다."""
        return {
            "parser": {"input_tokens": 0, "output_tokens": 0, "calls": 0, "provider": "", "model": ""},
            "quality_step": {"input_tokens": 0, "output_tokens": 0, "calls": 0, "provider": "", "model": ""},
            "voice_step": {"input_tokens": 0, "output_tokens": 0, "calls": 0, "provider": "", "model": ""},
        }

    def _accumulate_token_usage(
        self,
        *,
        token_usage: Optional[Dict[str, Dict[str, Any]]],
        role: str,
        response: LLMResponse,
        provider: str,
    ) -> None:
        """LLM 응답 토큰을 역할별로 누적한다."""
        if token_usage is None:
            return
        bucket = token_usage.get(role)
        if not isinstance(bucket, dict):
            return

        bucket["input_tokens"] = int(bucket.get("input_tokens", 0) or 0) + max(0, int(response.input_tokens or 0))
        bucket["output_tokens"] = int(bucket.get("output_tokens", 0) or 0) + max(0, int(response.output_tokens or 0))
        bucket["calls"] = int(bucket.get("calls", 0) or 0) + 1

        normalized_provider = str(provider or "").strip()
        current_provider = str(bucket.get("provider", "")).strip()
        if not current_provider:
            bucket["provider"] = normalized_provider
        elif current_provider != normalized_provider:
            bucket["provider"] = "mixed"

        if not str(bucket.get("model", "")).strip():
            bucket["model"] = str(response.model or "").strip()

    async def _generate_with_usage(
        self,
        *,
        client: BaseLLMClient,
        role: str,
        token_usage: Optional[Dict[str, Dict[str, Any]]],
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        max_retries: int = 3,
    ) -> LLMResponse:
        """LLM 호출 후 토큰 사용량을 자동 누적한다."""
        provider_name = str(client.provider_name or "").strip().lower()
        if self.circuit_breaker and self.circuit_breaker.is_open(provider_name):
            logger.warning("Circuit open on %s, skipping provider", provider_name)
            raise ProviderCircuitOpenError(provider_name)
        try:
            response = await client.generate_with_retry(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                max_retries=max_retries,
            )
            if self.circuit_breaker:
                self.circuit_breaker.record_success(provider_name)
        except RateLimitError:
            if self.circuit_breaker:
                self.circuit_breaker.record_failure(provider_name)
            raise
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if self.circuit_breaker and status_code in (401, 403):
                self.circuit_breaker.record_failure(provider_name)
            raise
        self._accumulate_token_usage(
            token_usage=token_usage,
            role=role,
            response=response,
            provider=client.provider_name,
        )
        return response

    def _build_fallback_chain(self) -> List[BaseLLMClient]:
        """폴백 체인을 구성한다."""
        chain = [self.primary]
        if self.fallback_to_secondary and self.secondary is not self.primary:
            chain.append(self.secondary)
        chain.extend(self.additional_clients)
        return chain

    def _is_economy_topic(self, topic_mode: str) -> bool:
        """경제/RAG 적용 대상 토픽인지 확인한다."""
        return normalize_topic_mode(topic_mode) == "finance"

    def _resolve_topic_mode(self, job: Job, persona: Any) -> str:
        """품질 레이어에서 사용할 주제를 Job 문맥 기준으로 결정한다."""
        category_mode = self._infer_topic_mode_from_text(job.category)
        if category_mode:
            return category_mode

        keyword_mode = self._infer_topic_mode_from_keywords(job.seed_keywords)
        if keyword_mode:
            return keyword_mode

        persona_mode = normalize_topic_mode(getattr(persona, "topic_mode", job.persona_id or "cafe"))
        if persona_mode in {"cafe", "it", "parenting", "finance"}:
            return persona_mode
        return "cafe"

    def _infer_topic_mode_from_keywords(self, keywords: List[str]) -> Optional[str]:
        """시드 키워드에서 토픽 모드를 추정한다."""
        if not keywords:
            return None
        text = " ".join(str(keyword).strip() for keyword in keywords if str(keyword).strip())
        if not text:
            return None
        return self._infer_topic_mode_from_text(text)

    def _infer_topic_mode_from_text(self, text: str) -> Optional[str]:
        """자유 텍스트에서 토픽 모드를 추정한다."""
        lowered = str(text or "").strip().lower()
        if not lowered:
            return None
        if any(token in lowered for token in ("경제", "finance", "economy", "투자", "주식", "재테크", "금리", "환율")):
            return "finance"
        if any(token in lowered for token in ("it", "개발", "코드", "ai", "자동화", "테크", "앱")):
            return "it"
        if any(token in lowered for token in ("육아", "아이", "부모", "가정", "교육", "parenting", "family")):
            return "parenting"
        if any(token in lowered for token in ("카페", "맛집", "커피", "레시피", "요리", "브런치")):
            return "cafe"
        return None

    def _normalize_category_name(self, value: str) -> str:
        """카테고리 비교를 위해 공백/대소문자를 정규화한다."""
        lowered = str(value or "").strip().lower()
        return re.sub(r"\s+", "", lowered)

    def _load_fallback_category(self) -> str:
        """system_settings에서 fallback_category를 로드한다."""
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute(
                    """
                    SELECT setting_value
                    FROM system_settings
                    WHERE setting_key = 'fallback_category'
                    """,
                ).fetchone()
            finally:
                conn.close()
        except Exception:
            return DEFAULT_FALLBACK_CATEGORY

        saved_value = str(row[0]).strip() if row and row[0] is not None else ""
        return saved_value or DEFAULT_FALLBACK_CATEGORY

    def _resolve_quality_threshold(self, job: Job) -> Tuple[int, str]:
        """카테고리 슬롯에 따라 품질 통과 점수를 결정한다."""
        fallback_category = self._load_fallback_category()
        job_category = self._normalize_category_name(job.category)
        fallback_normalized = self._normalize_category_name(fallback_category)

        if job_category and fallback_normalized and job_category == fallback_normalized:
            return self.TEST_SLOT_QUALITY_THRESHOLD, "test"
        return self.MAIN_SLOT_QUALITY_THRESHOLD, "main"

    def _collect_news_context(self, keywords: List[str], max_items: int = 3) -> List[Dict[str, str]]:
        """키워드 기반 RSS 뉴스 컨텍스트를 수집한다."""
        query_text = " ".join(str(keyword).strip() for keyword in keywords if str(keyword).strip())
        try:
            # 1단: 후보 문서(최대 20) 수집 + 2단: Cross-Encoder 재정렬
            news_items = self.rag_search_engine.retrieve(
                keywords=keywords,
                query_text=query_text,
            )
            if not news_items:
                logger.warning(
                    "Economy RAG news not found. Falling back to generic template."
                )
                return []
            limited_items = news_items[: max(1, min(2, max_items))]
            stats = self.rag_search_engine.last_stats
            logger.info(
                "Economy RAG contexts selected",
                extra={
                    "candidate_count": stats.candidate_count,
                    "selected_count": len(limited_items),
                    "reranker": stats.reranker,
                    "reranker_model": stats.model_name,
                },
            )
            return limited_items
        except Exception as exc:
            logger.warning(
                "Economy RAG collection failed. Falling back to generic template: %s",
                exc,
            )
            return []

    def _build_news_data_text(self, news_context: List[Dict[str, str]]) -> str:
        """뉴스 컨텍스트를 프롬프트 텍스트로 변환한다."""
        blocks: List[str] = []
        for index, item in enumerate(news_context, start=1):
            title = str(item.get("title", "")).strip()
            link = str(item.get("link", "")).strip()
            content = str(item.get("content", "")).strip()
            if not (title or content):
                continue
            blocks.append(
                f"[기사 {index}]\n"
                f"제목: {title}\n"
                f"링크: {link}\n"
                f"요약: {content}"
            )
        return "\n\n".join(blocks).strip()

    def _append_news_sources(self, content: str, news_context: List[Dict[str, str]]) -> str:
        """본문 하단에 출처 링크를 URL 노출 형식으로 고정 추가한다."""
        source_lines: List[str] = []
        for item in news_context:
            title = str(item.get("title", "")).strip()
            link = str(item.get("link", "")).strip()
            if not title or not link:
                continue
            source_lines.append(f"참고 자료: {title} ( {link} )")

        if not source_lines:
            return content

        source_block = "\n".join(source_lines)
        if source_block in content:
            return content
        return f"{content.rstrip()}\n\n{source_block}\n"

    def _load_saved_voice_profile(self, persona_id: str) -> Dict[str, Any]:
        """DB에 저장된 사용자 Voice_Profile을 로드한다."""
        resolved_persona = str(persona_id or "").strip().upper() or "P1"
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute(
                    """
                    SELECT profile_json
                    FROM persona_profiles
                    WHERE persona_id = ?
                    """,
                    (resolved_persona,),
                ).fetchone()
            finally:
                conn.close()
        except Exception:
            return {}
        if not row:
            return {}
        try:
            parsed = json.loads(str(row[0] or "{}"))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
        return {}

    def _build_voice_profile_text(self, voice_profile: Dict[str, Any]) -> str:
        """Voice_Profile dict를 프롬프트 텍스트로 변환한다."""
        if not voice_profile:
            return "- structure: balanced\n- evidence: balanced\n- tone: natural\n- style_strength: 40"

        lines = []
        for key in ("mbti", "age_group", "gender", "structure", "evidence", "distance", "criticism", "density", "style_strength"):
            if key not in voice_profile or not voice_profile[key]:
                continue
            lines.append(f"- {key}: {voice_profile.get(key)}")
        scores = voice_profile.get("scores", {})
        if isinstance(scores, dict) and scores:
            lines.append(f"- scores: {json.dumps(scores, ensure_ascii=False)}")
        return "\n".join(lines) if lines else "- style_strength: 40"

    def _extract_h2_headings(self, content: str) -> List[str]:
        """본문의 H2 제목 목록을 추출한다."""
        headings: List[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                headings.append(stripped)
        return headings

    def _extract_urls(self, content: str) -> List[str]:
        """본문 내 URL 목록을 추출한다."""
        return re.findall(r"https?://[^\s)]+", content)

    def _extract_numeric_tokens(self, content: str) -> List[str]:
        """본문 내 주요 숫자 토큰을 추출한다."""
        raw_tokens = re.findall(r"\b\d[\d,]*(?:\.\d+)?(?:%|원|달러|명|개|회|배|점|시간|일|년|월)?\b", content)
        # 순서 보존 중복 제거
        deduped: List[str] = []
        for token in raw_tokens:
            if token not in deduped:
                deduped.append(token)
        return deduped

    def _is_voice_rewrite_safe(self, raw_content: str, rewritten: str) -> Tuple[bool, str]:
        """Voice rewrite 결과가 정보 훼손 없이 안전한지 검증한다."""
        raw_h2 = self._extract_h2_headings(raw_content)
        rewritten_h2 = self._extract_h2_headings(rewritten)
        if raw_h2 and raw_h2 != rewritten_h2:
            return False, "h2_structure_changed"

        raw_urls = self._extract_urls(raw_content)
        rewritten_urls = self._extract_urls(rewritten)
        if raw_urls and set(raw_urls) != set(rewritten_urls):
            return False, "url_set_changed"

        raw_numbers = self._extract_numeric_tokens(raw_content)
        rewritten_numbers = self._extract_numeric_tokens(rewritten)
        if raw_numbers:
            missing_numbers = [token for token in raw_numbers if token not in rewritten_numbers]
            if missing_numbers:
                return False, f"numeric_token_missing:{','.join(missing_numbers[:5])}"

        return True, "ok"

    async def _apply_voice_rewrite(
        self,
        *,
        raw_content: str,
        persona: Any,
        tone_profile: Any,
        voice_profile: Dict[str, Any],
        client: BaseLLMClient,
        token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> str:
        """품질 레이어 결과를 Voice 레이어로 리라이트한다."""
        if not raw_content.strip():
            return raw_content

        style_strength_value = voice_profile.get("style_strength", 40) if isinstance(voice_profile, dict) else 40
        try:
            style_strength = int(style_strength_value)
        except (TypeError, ValueError):
            style_strength = 40
        if style_strength <= 5:
            # 스타일 강도가 매우 낮으면 원문을 그대로 유지한다.
            return raw_content

        voice_profile_text = self._build_voice_profile_text(voice_profile)
        user_prompt = VOICE_REWRITE_REQUEST.format(
            voice_profile=voice_profile_text,
            persona_prefix=persona.prompt_prefix,
            tone_suffix=tone_profile.prompt_suffix,
            content=raw_content,
        )
        try:
            response = await self._generate_with_usage(
                client=client,
                role="voice_step",
                token_usage=token_usage,
                system_prompt="당신은 한국어 스타일 리라이터입니다. 정보는 유지하고 표현만 조정하세요.",
                user_prompt=user_prompt,
                temperature=max(0.2, min(0.7, style_strength / 100.0)),
                max_tokens=self.max_tokens,
                max_retries=2,
            )
        except Exception as exc:
            logger.warning("Voice rewrite failed, using raw content: %s", exc)
            return raw_content

        rewritten = response.content.strip()
        if not rewritten:
            return raw_content

        raw_length = len(raw_content)
        rewritten_length = len(rewritten)
        ratio = rewritten_length / max(raw_length, 1)
        if ratio < 0.9 or ratio > 1.1:
            logger.warning(
                "Voice rewrite length drift detected, fallback raw (ratio=%.2f)",
                ratio,
            )
            return raw_content
        safe, reason = self._is_voice_rewrite_safe(raw_content, rewritten)
        if not safe:
            logger.warning(
                "Voice rewrite semantic drift detected, fallback raw (reason=%s)",
                reason,
            )
            return raw_content
        return rewritten

    async def _generate_single(
        self,
        job: Job,
        persona: Any,
        tone_profile: Any,
        fallback_chain: List[BaseLLMClient],
        topic_mode: str,
        news_context: Optional[List[Dict[str, str]]] = None,
        quality_only: bool = False,
        token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Tuple[str, str, str, str]:
        """단일 호출로 초안 생성."""
        provider_used = self.primary.provider_name
        provider_model = ""
        provider_fallback_from = ""

        for idx, client in enumerate(fallback_chain):
            try:
                draft, provider_model = await self._generate_draft(
                    job,
                    client,
                    persona,
                    tone_profile,
                    topic_mode=topic_mode,
                    news_context=news_context,
                    quality_only=quality_only,
                    token_usage=token_usage,
                )
                provider_used = client.provider_name
                if idx > 0:
                    provider_fallback_from = fallback_chain[0].provider_name
                    self._notify_fallback_success(
                        from_provider=provider_fallback_from,
                        to_provider=provider_used,
                        title=job.title,
                    )
                return draft, provider_model, provider_used, provider_fallback_from
            except ProviderCircuitOpenError as exc:
                next_client = fallback_chain[idx + 1] if idx + 1 < len(fallback_chain) else None
                if next_client:
                    logger.warning(
                        "Circuit open detected on %s. Switching to %s",
                        exc.provider,
                        next_client.provider_name,
                    )
                    continue
                raise ContentGenerationError(
                    f"All providers blocked by circuit breaker. Last provider: {exc.provider}"
                ) from exc
            except RateLimitError as exc:
                next_client = fallback_chain[idx + 1] if idx + 1 < len(fallback_chain) else None
                if next_client:
                    logger.warning(
                        "Rate limit detected on %s. Switching to %s (%s)",
                        client.provider_name,
                        next_client.provider_name,
                        exc,
                    )
                    continue
                raise ContentGenerationError(f"All providers failed with rate limit. Last error: {exc}") from exc
            except Exception as exc:
                next_client = fallback_chain[idx + 1] if idx + 1 < len(fallback_chain) else None
                if next_client:
                    warning_message = (
                        f"[WARNING] {self._provider_label(client.provider_name)} API failed. "
                        f"Falling back to {self._provider_label(next_client.provider_name)}..."
                    )
                    # 터미널에서 즉시 보이도록 표준 출력에도 남긴다.
                    print(warning_message)
                    logger.warning(
                        "%s (%s)",
                        warning_message,
                        exc,
                    )
                else:
                    raise ContentGenerationError(f"All providers failed. Last error: {exc}") from exc

        raise ContentGenerationError("No providers available")

    def _notify_fallback_success(
        self,
        *,
        from_provider: str,
        to_provider: str,
        title: str,
    ) -> None:
        """폴백 성공 알림 콜백을 호출한다."""
        callback = self.fallback_alert_fn
        if callback is None:
            return
        payload = {
            "from_provider": from_provider,
            "to_provider": to_provider,
            "title": title,
            "message": (
                f"🚨 [API 장애 감지] {provider_label(from_provider)} 응답 실패. "
                f"{provider_label(to_provider)}로 대체해 블로그 작성 완료: {title}"
            ),
        }
        try:
            callback(payload)
        except Exception:
            logger.debug("fallback alert callback failed", exc_info=True)

    async def _generate_multistep(
        self,
        job: Job,
        persona: Any,
        tone_profile: Any,
        fallback_chain: List[BaseLLMClient],
        topic_mode: str,
        news_context: Optional[List[Dict[str, str]]] = None,
        quality_only: bool = False,
        token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Tuple[str, str, int]:
        """멀티스텝 생성: 아웃라인 → 섹션별 → 통합 (전략 B)."""
        llm_calls = 0
        client = fallback_chain[0]
        news_data_text = self._build_news_data_text(news_context or [])
        tone_suffix = "" if quality_only else tone_profile.prompt_suffix
        voice_profile_text = self._build_voice_profile_text(self._load_saved_voice_profile(job.persona_id or "P1"))
        voice_injection = f"\n\n[Voice Profile (Writing Style)]\n{voice_profile_text}"
        system_prompt = QUALITY_LAYER_SYSTEM_PROMPT if quality_only else f"{SYSTEM_BLOG_WRITER}{voice_injection}"

        # Step 1: 아웃라인 생성
        outline_prompt = OUTLINE_GENERATION.format(
            title=job.title,
            keywords=", ".join(job.seed_keywords),
            audience="실무 적용 독자",
            tone="중립 정보형" if quality_only else tone_profile.name,
        )
        if news_data_text:
            outline_prompt = (
                f"{outline_prompt}\n\n[NewsData]\n{news_data_text}\n\n"
                "반드시 NewsData의 사실에 기반해 아웃라인을 구성하세요."
            )

        try:
            outline_response = await self._generate_with_usage(
                client=client,
                role="quality_step",
                token_usage=token_usage,
                system_prompt=system_prompt,
                user_prompt=outline_prompt,
                temperature=0.5,
                max_tokens=1000,
            )
            llm_calls += 1
            outline = self._parse_outline(outline_response.content)
        except Exception as exc:
            logger.warning("Multistep outline failed, falling back to single: %s", exc)
            draft, model, _, _ = await self._generate_single(
                job,
                persona,
                tone_profile,
                fallback_chain,
                topic_mode=topic_mode,
                news_context=news_context,
                quality_only=quality_only,
                token_usage=token_usage,
            )
            return draft, model, 1

        if not outline.get("sections"):
            logger.warning("Invalid outline, falling back to single generation")
            draft, model, _, _ = await self._generate_single(
                job,
                persona,
                tone_profile,
                fallback_chain,
                topic_mode=topic_mode,
                news_context=news_context,
                quality_only=quality_only,
                token_usage=token_usage,
            )
            return draft, model, llm_calls + 1

        # Step 2: 섹션별 작성
        sections = []
        previous_context = outline.get("hook", "")

        for section in outline["sections"][:5]:  # 최대 5섹션
            section_prompt = SECTION_DRAFT.format(
                title=job.title,
                previous_context=previous_context[-300:],
                section_title=section.get("h2", ""),
                key_points=", ".join(section.get("key_points", [])),
                tone_suffix=tone_suffix,
            )
            if news_data_text:
                section_prompt = (
                    f"{section_prompt}\n\n[NewsData]\n{news_data_text}\n\n"
                    "섹션은 NewsData 범위의 팩트만 사용하세요."
                )

            try:
                section_response = await self._generate_with_usage(
                    client=client,
                    role="quality_step",
                    token_usage=token_usage,
                    system_prompt=system_prompt,
                    user_prompt=section_prompt,
                    temperature=self.temperature,
                    max_tokens=800,
                )
                llm_calls += 1
                section_content = section_response.content.strip()
                sections.append(section_content)
                previous_context = section_content
            except Exception as exc:
                logger.warning("Section generation failed: %s", exc)
                continue

        if not sections:
            logger.warning("No sections generated, falling back to single")
            draft, model, _, _ = await self._generate_single(
                job,
                persona,
                tone_profile,
                fallback_chain,
                topic_mode=topic_mode,
                news_context=news_context,
                quality_only=quality_only,
                token_usage=token_usage,
            )
            return draft, model, llm_calls + 1

        # Step 3: 통합
        integration_prompt = SECTION_INTEGRATION.format(
            sections="\n\n---\n\n".join(sections),
            tone_suffix=tone_suffix,
        )
        if news_data_text:
            integration_prompt = (
                f"{integration_prompt}\n\n[NewsData]\n{news_data_text}\n\n"
                "통합 시 NewsData에 없는 사실 추가를 금지하세요."
            )

        try:
            integration_response = await self._generate_with_usage(
                client=client,
                role="quality_step",
                token_usage=token_usage,
                system_prompt=system_prompt,
                user_prompt=integration_prompt,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            llm_calls += 1
            return integration_response.content.strip(), integration_response.model, llm_calls
        except Exception as exc:
            logger.warning("Integration failed, using concatenated sections: %s", exc)
            return "\n\n".join(sections), client.provider_name, llm_calls

    def _parse_outline(self, raw: str) -> Dict[str, Any]:
        """아웃라인 JSON을 파싱한다."""
        raw = raw.strip()

        # JSON 블록 추출
        json_match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
        if json_match:
            raw = json_match.group(1)
        else:
            json_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if json_match:
                raw = json_match.group(0)

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    async def _generate_draft(
        self,
        job: Job,
        client: BaseLLMClient,
        persona: Any,
        tone_profile: Any,
        topic_mode: str,
        news_context: Optional[List[Dict[str, str]]] = None,
        quality_only: bool = False,
        token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Tuple[str, str]:
        """초안 생성."""
        topic_mode = normalize_topic_mode(topic_mode or "cafe")
        news_data_text = self._build_news_data_text(news_context or [])

        # 플랫폼별 SEO 전략 로드
        platform_strategy = get_platform_strategy(job.platform)
        seo_snippet = platform_strategy.to_prompt_snippet()

        voice_profile_text = self._build_voice_profile_text(self._load_saved_voice_profile(job.persona_id or "P1"))
        voice_injection = f"\n\n[Voice Profile (Writing Style)]\n{voice_profile_text}"

        use_economy_rag = bool(news_data_text) and self._is_economy_topic(topic_mode)
        if quality_only and use_economy_rag:
            user_prompt = QUALITY_LAYER_ECONOMY_PROMPT.format(
                title=job.title,
                keywords=", ".join(job.seed_keywords),
                category=topic_mode,
                news_data=news_data_text,
            )
            system_prompt = f"{QUALITY_LAYER_SYSTEM_PROMPT}\n\n{ECONOMY_SYSTEM_PROMPT}"
        elif quality_only:
            user_prompt = QUALITY_LAYER_CONTENT_REQUEST.format(
                title=job.title,
                keywords=", ".join(job.seed_keywords),
                category=topic_mode,
            )
            if news_data_text:
                user_prompt = (
                    f"{user_prompt}\n\n"
                    f"[NewsData]\n{news_data_text}\n\n"
                    "추가 규칙:\n"
                    "1) NewsData의 최신 사실을 본문에 최소 1회 반영\n"
                    "2) NewsData에 없는 수치/인용은 생성 금지\n"
                )
            system_prompt = QUALITY_LAYER_SYSTEM_PROMPT
        elif use_economy_rag:
            user_prompt = ECONOMY_TOPIC_PROMPT.format(
                title=job.title,
                keywords=", ".join(job.seed_keywords),
                category=topic_mode,
                news_data=news_data_text,
                persona_prefix=persona.prompt_prefix,
                tone_suffix=tone_profile.prompt_suffix,
            )
            system_prompt = f"{SYSTEM_BLOG_WRITER}{voice_injection}\n\n{ECONOMY_SYSTEM_PROMPT}"
        else:
            user_prompt = USER_CONTENT_REQUEST.format(
                title=job.title,
                keywords=", ".join(job.seed_keywords),
                category=topic_mode,
                persona_prefix=persona.prompt_prefix,
                tone_suffix=tone_profile.prompt_suffix,
            )
            system_prompt = f"{SYSTEM_BLOG_WRITER}{voice_injection}"

        # SEO 전략 지침 삽입
        user_prompt = f"{user_prompt}\n\n{seo_snippet}"

        try:
            response = await self._generate_with_usage(
                client=client,
                role="quality_step",
                token_usage=token_usage,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:
            raise ContentGenerationError(f"draft generation failed: {exc}") from exc

        if not response.content.strip():
            raise ContentGenerationError("draft generation returned empty content")

        return response.content.strip(), response.model

    async def _apply_seo(
        self,
        content: str,
        keywords: List[str],
        client: BaseLLMClient,
        token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> str:
        """SEO 최적화 적용."""
        user_prompt = (
            SEO_OPTIMIZATION.format(keywords=", ".join(keywords))
            + "\n\n[원본 콘텐츠]\n"
            + content
        )
        try:
            response = await self._generate_with_usage(
                client=client,
                role="quality_step",
                token_usage=token_usage,
                system_prompt=SYSTEM_BLOG_WRITER,
                user_prompt=user_prompt,
                temperature=max(0.2, self.temperature - 0.1),
                max_tokens=self.max_tokens,
            )
        except Exception as exc:
            logger.warning("SEO optimization failed, using original: %s", exc)
            return content

        return response.content.strip() or content

    async def _fact_check(
        self,
        content: str,
        client: BaseLLMClient,
        token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> FactCheckResult:
        """팩트체크 수행 (전략 E)."""
        user_prompt = f"{FACT_CHECK_REQUEST}\n\n[콘텐츠]\n{content}"

        try:
            response = await self._generate_with_usage(
                client=client,
                role="quality_step",
                token_usage=token_usage,
                system_prompt="당신은 팩트체크 전문가입니다.",
                user_prompt=user_prompt,
                temperature=0.1,
                max_tokens=1500,
            )
            parsed = self._parse_json_response(response.content)

            claims = parsed.get("claims", [])
            high_risk_claims = [c for c in claims if c.get("risk_level") == "high"]

            return FactCheckResult(
                claims=claims,
                overall_risk=parsed.get("overall_risk", "low"),
                recommendation=parsed.get("recommendation", ""),
                needs_revision=len(high_risk_claims) > 0,
            )
        except Exception as exc:
            logger.warning("Fact check failed: %s", exc)
            return FactCheckResult()

    async def _apply_fact_revisions(
        self,
        content: str,
        fact_result: FactCheckResult,
        client: BaseLLMClient,
        token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> str:
        """팩트체크 결과를 반영하여 수정."""
        claims_text = json.dumps(fact_result.claims, ensure_ascii=False, indent=2)
        user_prompt = FACT_CHECK_REVISION.format(claims=claims_text) + f"\n\n[원본 콘텐츠]\n{content}"

        try:
            response = await self._generate_with_usage(
                client=client,
                role="quality_step",
                token_usage=token_usage,
                system_prompt=SYSTEM_BLOG_WRITER,
                user_prompt=user_prompt,
                temperature=0.3,
                max_tokens=self.max_tokens,
            )
            return response.content.strip() or content
        except Exception as exc:
            logger.warning("Fact revision failed: %s", exc)
            return content

    async def _check_quality(
        self,
        content: str,
        job: Job,
        client: BaseLLMClient,
        pass_score_threshold: int,
        token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> QualityResult:
        """품질 검증."""
        user_prompt = f"{QUALITY_CHECK}\n\n[제목]\n{job.title}\n\n[콘텐츠]\n{content}"

        try:
            response = await self._generate_with_usage(
                client=client,
                role="quality_step",
                token_usage=token_usage,
                system_prompt="당신은 엄격한 콘텐츠 편집자입니다.",
                user_prompt=user_prompt,
                temperature=0.1,
                max_tokens=1000,
            )
        except Exception as exc:
            logger.warning("Quality check failed: %s", exc)
            return QualityResult(
                score=60,
                gate="retry_mask",
                issues=["quality_check_error"],
                summary=str(exc),
            )

        parsed = self._parse_json_response(response.content)
        score = int(parsed.get("score", 50))

        # 게이트 결정
        if score >= pass_score_threshold:
            gate = "pass"
        elif score >= self.QUALITY_RETRY_MASK_FLOOR:
            gate = "retry_mask"
        else:
            gate = "retry_all"

        return QualityResult(
            score=score,
            gate=gate,
            breakdown=parsed.get("breakdown", {}),
            issues=parsed.get("issues", []),
            improvements=parsed.get("improvements", []),
            summary=str(parsed.get("summary", "")),
        )

    async def _rewrite_content(
        self,
        content: str,
        quality_result: QualityResult,
        client: BaseLLMClient,
        token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> str:
        """품질 피드백을 반영하여 재작성 (전략 C)."""
        user_prompt = REWRITE_REQUEST.format(
            content=content,
            score=quality_result.score,
            issues=", ".join(quality_result.issues),
            improvements=", ".join(quality_result.improvements),
        )

        try:
            response = await self._generate_with_usage(
                client=client,
                role="quality_step",
                token_usage=token_usage,
                system_prompt=SYSTEM_BLOG_WRITER,
                user_prompt=user_prompt,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            return response.content.strip() or content
        except Exception as exc:
            logger.warning("Rewrite failed: %s", exc)
            return content

    async def _generate_image_prompts(
        self,
        content: str,
        title: str,
        keywords: List[str],
        client: BaseLLMClient,
        token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Tuple[List[str], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """이미지 프롬프트 생성 (블로그 최적화)."""
        # 섹션 제목 추출
        headings = re.findall(r"^##\s+(.+)$", content, flags=re.MULTILINE)

        user_prompt = IMAGE_PROMPT_GENERATION.format(
            title=title,
            keywords=", ".join(keywords),
            sections=", ".join(headings[:5]),
        )

        try:
            response = await self._generate_with_usage(
                client=client,
                role="quality_step",
                token_usage=token_usage,
                system_prompt="당신은 블로그 이미지 전문가입니다.",
                user_prompt=user_prompt,
                temperature=0.5,
                max_tokens=1000,
            )
            parsed = self._parse_json_response(response.content)

            prompts = []
            placements = []
            slots = []

            raw_slots = parsed.get("image_slots", [])
            if isinstance(raw_slots, list):
                for index, raw_slot in enumerate(raw_slots[:4]):
                    if not isinstance(raw_slot, dict):
                        continue
                    prompt = str(raw_slot.get("prompt", "")).strip()
                    if not prompt:
                        continue
                    slot_role_raw = str(raw_slot.get("slot_role", "content")).strip().lower()
                    slot_role = "thumbnail" if slot_role_raw == "thumbnail" else "content"
                    preferred_raw = str(raw_slot.get("preferred_type", "real")).strip().lower()
                    preferred_type = "ai_generated" if preferred_raw == "ai_generated" else "real"
                    score_raw = raw_slot.get("ai_generation_score", 0)
                    try:
                        score_value = int(score_raw)
                    except (TypeError, ValueError):
                        score_value = 0
                    score = max(0, min(100, score_value))
                    recommended_raw = raw_slot.get("recommended", False)
                    if isinstance(recommended_raw, bool):
                        recommended = recommended_raw
                    else:
                        recommended = str(recommended_raw).strip().lower() in {"1", "true", "yes", "on"}
                    concept = str(raw_slot.get("concept", "")).strip()
                    after_section = str(raw_slot.get("after_section", "")).strip()
                    image_type = str(raw_slot.get("type", "illustration")).strip() or "illustration"
                    reason = str(raw_slot.get("reason", "")).strip()
                    slot_id = str(raw_slot.get("slot_id", "")).strip() or f"{slot_role}_{index}"

                    slot_payload = {
                        "slot_id": slot_id,
                        "slot_role": slot_role,
                        "prompt": prompt,
                        "concept": concept,
                        "after_section": after_section,
                        "type": image_type,
                        "preferred_type": preferred_type,
                        "recommended": recommended,
                        "ai_generation_score": score,
                        "reason": reason,
                    }
                    slots.append(slot_payload)
                    prompts.append(prompt)
                    placements.append(
                        {
                            "type": slot_role,
                            "prompt": prompt,
                            "concept": concept,
                            "after_section": after_section,
                            "image_type": image_type,
                            "slot_id": slot_id,
                            "slot_role": slot_role,
                            "preferred_type": preferred_type,
                            "recommended": recommended,
                            "ai_generation_score": score,
                            "reason": reason,
                            "placement": "title_below" if slot_role == "thumbnail" else "",
                        }
                    )

            # 최신 image_slots 형식이 유효하면 우선 사용한다.
            if slots:
                return prompts, placements, slots

            # 썸네일
            thumbnail = parsed.get("thumbnail", {})
            if thumbnail.get("prompt"):
                thumbnail_prompt = str(thumbnail["prompt"]).strip()
                prompts.append(thumbnail_prompt)
                placements.append(
                    {
                    "type": "thumbnail",
                    "prompt": thumbnail_prompt,
                    "concept": thumbnail.get("concept", ""),
                    "placement": "title_below",
                    }
                )
                slots.append(
                    {
                        "slot_id": "thumb_0",
                        "slot_role": "thumbnail",
                        "prompt": thumbnail_prompt,
                        "concept": str(thumbnail.get("concept", "")).strip(),
                        "after_section": "",
                        "type": "illustration",
                        "preferred_type": "real",
                        "recommended": False,
                        "ai_generation_score": 0,
                        "reason": "legacy_thumbnail_format",
                    }
                )

            # 본문 이미지
            for idx, img in enumerate(parsed.get("content_images", [])[:4], start=1):
                if img.get("prompt"):
                    content_prompt = str(img["prompt"]).strip()
                    prompts.append(content_prompt)
                    placements.append(
                        {
                        "type": "content",
                        "prompt": content_prompt,
                        "concept": img.get("concept", ""),
                        "after_section": img.get("after_section", ""),
                        "image_type": img.get("type", "illustration"),
                        }
                    )
                    slots.append(
                        {
                            "slot_id": f"content_{idx}",
                            "slot_role": "content",
                            "prompt": content_prompt,
                            "concept": str(img.get("concept", "")).strip(),
                            "after_section": str(img.get("after_section", "")).strip(),
                            "type": str(img.get("type", "illustration")).strip() or "illustration",
                            "preferred_type": "real",
                            "recommended": False,
                            "ai_generation_score": 0,
                            "reason": "legacy_content_images_format",
                        }
                    )

            return prompts, placements, slots

        except Exception as exc:
            logger.warning("Image prompt generation failed: %s", exc)
            # 폴백: 기본 프롬프트
            return self._extract_image_prompts_fallback(content, title), [], []

    def _extract_image_prompts_fallback(self, content: str, title: str) -> List[str]:
        """이미지 프롬프트 폴백 생성."""
        headings = re.findall(r"^##\s+(.+)$", content, flags=re.MULTILINE)
        prompts = [
            f"Blog thumbnail about {title}, modern illustration, vibrant colors, professional"
        ]
        for heading in headings[:4]:
            prompts.append(
                f"Illustration explaining {heading}, infographic style, clean design"
            )
        return prompts

    def _parse_json_response(self, raw: str) -> Dict[str, Any]:
        """JSON 응답을 파싱한다."""
        raw = raw.strip()

        # JSON 블록 추출
        json_match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
        if json_match:
            raw = json_match.group(1)

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # JSON 객체 추출 시도
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        return {}

    def _provider_label(self, provider_name: str) -> str:
        """프로바이더 표시명."""
        labels = {
            "qwen": "Qwen",
            "deepseek": "DeepSeek",
            "claude": "Claude",
            "groq": "Groq",
            "cerebras": "Cerebras",
            "gemini": "Gemini",
        }
        return labels.get(provider_name, provider_name)
