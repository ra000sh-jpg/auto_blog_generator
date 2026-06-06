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

import asyncio
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ..automation.job_store import Job
from .. import constants
from ..constants import DEFAULT_FALLBACK_CATEGORY
from ..content_sources import (
    get_templates_for_topic,
    render_strategy_prompt,
    select_category_writing_strategy,
    select_market_writing_strategy,
)
from ..exceptions import ContentGenerationError, RateLimitError
from ..seo.platform_strategy import get_platform_strategy
from .base_client import BaseLLMClient, LLMResponse
from .circuit_breaker import ProviderCircuitBreaker, ProviderCircuitOpenError
from .claude_client import ClaudeClient
from .llm_router import provider_label
from .insight_strategy import (
    InsightQualityEvaluator,
    build_insight_strategy,
)
from .prompts import (
    ANTI_AI_PATTERN_BRIEF,
    ANTI_AI_PATTERN_RULES,
    COGNITIVE_DEPTH_BY_TOPIC,
    COGNITIVE_DEPTH_COMMON,
    ECONOMY_SYSTEM_PROMPT,
    ECONOMY_TOPIC_PROMPT,
    EMOTIONAL_ARCHITECTURE_PROMPT,
    FACT_CHECK_REQUEST,
    FACT_CHECK_REVISION,
    IMAGE_PROMPT_GENERATION,
    OUTLINE_GENERATION,
    PRE_WRITING_ANALYSIS_PROMPT,
    QUALITY_LAYER_CONTENT_REQUEST,
    QUALITY_LAYER_ECONOMY_PROMPT,
    QUALITY_LAYER_SYSTEM_PROMPT,
    QUALITY_CHECK,
    QUALITY_CHECK_SIMPLE,
    REWRITE_REQUEST,
    SENTENCE_CRAFT_CHECKLIST,
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

if TYPE_CHECKING:
    from ..collectors import RssNewsCollector
    from ..rag import CrossEncoderRagSearchEngine


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
    _FALLBACK_ALERT_DEDUP_KEYS: set[str] = set()

    def __init__(
        self,
        primary_client: Optional[BaseLLMClient] = None,
        secondary_client: Optional[BaseLLMClient] = None,
        voice_client: Optional[BaseLLMClient] = None,
        parser_client: Optional[BaseLLMClient] = None,
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
        rss_news_collector: Optional["RssNewsCollector"] = None,
        rag_search_engine: Optional["CrossEncoderRagSearchEngine"] = None,
        enable_voice_rewrite: bool = True,
        db_path: str = "data/automation.db",
        fallback_alert_fn: Optional[Any] = None,
        circuit_breaker: Optional[ProviderCircuitBreaker] = None,
        web_search_client: Optional[Any] = None,
        web_fetch_client: Optional[Any] = None,
        web_search_max_results: int = 5,
        naver_search_collector: Optional[Any] = None,
        market_data_collector: Optional[Any] = None,
        memory_store: Optional[Any] = None,
        strategy_mode: str = "cost",
        cost_strict_mode: bool = False,
        cost_retry_max_retries: int = 6,
        cost_retry_base_delay_sec: float = 2.0,
        cost_retry_max_delay_sec: float = 20.0,
        cost_lock_quality_provider: bool = True,
    ):
        resolved_primary = primary_client or client or ClaudeClient()
        self.primary = resolved_primary
        self.secondary = secondary_client or resolved_primary
        self.voice_client = voice_client or self.secondary
        self.parser_client = parser_client or self.secondary
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
        if rss_news_collector is not None:
            self.rss_news_collector = rss_news_collector
        else:
            try:
                from ..collectors import RssNewsCollector

                self.rss_news_collector = RssNewsCollector()
            except Exception as exc:
                logger.warning("RSS news collector init skipped: %s", exc)
                self.rss_news_collector = None
        if rag_search_engine is not None:
            self.rag_search_engine = rag_search_engine
        else:
            try:
                from ..rag import CrossEncoderRagSearchEngine

                self.rag_search_engine = CrossEncoderRagSearchEngine(
                    news_collector=self.rss_news_collector,
                    cross_encoder_model="BAAI/bge-reranker-base",
                    candidate_top_k=20,
                    final_top_k=2,
                    cross_encoder_enabled=True,
                )
            except Exception as exc:
                logger.warning("RAG search engine init skipped: %s", exc)
                self.rag_search_engine = None
        self.enable_voice_rewrite = enable_voice_rewrite
        self.db_path = db_path
        self.fallback_alert_fn = fallback_alert_fn
        self.circuit_breaker = circuit_breaker
        self.web_search_client = web_search_client
        self.web_fetch_client = web_fetch_client
        self.web_search_max_results = max(1, int(web_search_max_results))
        self.naver_search_collector = naver_search_collector
        self.market_data_collector = market_data_collector
        self.memory_store = memory_store
        self.strategy_mode = str(strategy_mode or "cost").strip().lower()
        self.cost_strict_mode = bool(cost_strict_mode)
        self.cost_retry_max_retries = max(1, int(cost_retry_max_retries or 1))
        self.cost_retry_base_delay_sec = max(0.0, float(cost_retry_base_delay_sec or 0.0))
        self.cost_retry_max_delay_sec = max(
            self.cost_retry_base_delay_sec,
            float(cost_retry_max_delay_sec or self.cost_retry_base_delay_sec),
        )
        self.cost_lock_quality_provider = bool(cost_lock_quality_provider)
        self._active_memory_context: str = ""  # generate() 실행 중 임시 저장
        self.insight_quality_evaluator = InsightQualityEvaluator()
        # 품질 게이트 점수 기준은 환경변수/DB(system_settings)로 운영 중 조정 가능하다.
        self.MAIN_SLOT_QUALITY_THRESHOLD = self._load_int_setting(
            setting_key="llm_main_quality_threshold",
            env_name="LLM_MAIN_QUALITY_THRESHOLD",
            default=self.MAIN_SLOT_QUALITY_THRESHOLD,
            min_value=65,
            max_value=95,
        )
        self.TEST_SLOT_QUALITY_THRESHOLD = self._load_int_setting(
            setting_key="llm_test_quality_threshold",
            env_name="LLM_TEST_QUALITY_THRESHOLD",
            default=self.TEST_SLOT_QUALITY_THRESHOLD,
            min_value=60,
            max_value=90,
        )
        self.QUALITY_RETRY_MASK_FLOOR = self._load_int_setting(
            setting_key="llm_retry_mask_floor",
            env_name="LLM_RETRY_MASK_FLOOR",
            default=self.QUALITY_RETRY_MASK_FLOOR,
            min_value=50,
            max_value=85,
        )

    async def aclose(self) -> None:
        """생성기 내부의 비동기 클라이언트를 정리한다."""
        for client in (self.web_search_client, self.web_fetch_client):
            if client is None:
                continue
            close_fn = getattr(client, "close", None)
            if not callable(close_fn):
                continue
            try:
                maybe_coro = close_fn()
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro
            except Exception as exc:
                logger.debug("Client close skipped: %s", exc)

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
        writing_strategy_plan = self._resolve_writing_strategy_plan(job=job, topic_mode=topic_mode)
        insight_strategy = build_insight_strategy(
            title=job.title,
            keywords=job.seed_keywords,
            topic_mode=topic_mode,
        )
        insight_strategy_prompt = insight_strategy.to_prompt_block()
        saved_voice_profile = self._load_saved_voice_profile(persona_id or job.persona_id or "P1")
        active_feedback_rules = self._load_active_feedback_rules()
        is_idea_vault_job = any(str(tag).lower() == "idea_vault" for tag in (job.tags or []))
        required_quality_score, quality_slot_type = self._resolve_quality_threshold(job)
        market_snapshot_meta: Dict[str, Any] = {}

        news_context: List[Dict[str, str]] = []
        if self._is_economy_topic(topic_mode):
            news_context = self._collect_news_context(job.seed_keywords, max_items=3)
            if not news_context:
                news_context = await self._collect_web_context(
                    keywords=job.seed_keywords,
                    fallback_text=job.title,
                    max_items=3,
                )
        elif is_idea_vault_job:
            idea_query = [job.title] + list(job.seed_keywords)
            news_context = self._collect_news_context(idea_query, max_items=1)
            if not news_context:
                news_context = await self._collect_web_context(
                    keywords=idea_query,
                    fallback_text=job.title,
                    max_items=1,
                )
        else:
            news_context = await self._collect_web_context(
                keywords=job.seed_keywords,
                fallback_text=job.title,
                max_items=2,
            )

        market_context, market_snapshot_meta = self._collect_market_snapshot_context(job)
        if market_context:
            news_context = market_context + news_context

        naver_context = self._collect_naver_context(
            job=job,
            topic_mode=topic_mode,
            existing_count=len(news_context),
        )
        if naver_context:
            news_context.extend(naver_context)

        # ── 메모리 컨텍스트 수집 (발행 이력 기반, non-critical) ──
        self._active_memory_context = self._collect_memory_context(
            job=job,
            topic_mode=topic_mode,
        )

        # 폴백 체인 구성
        fallback_chain = self._build_fallback_chain()
        active_quality_client = fallback_chain[0] if fallback_chain else self.primary
        lock_quality_provider = self._is_cost_strict_active() and self.cost_lock_quality_provider

        # Step 0: 사전 분석(Call A, 저가 모델)
        pre_analysis = await self._run_pre_writing_analysis(
            job=job,
            topic_mode=topic_mode,
            insight_strategy_prompt=insight_strategy_prompt,
            token_usage=token_usage,
        )
        llm_calls += 1

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
                pre_analysis=pre_analysis,
                active_feedback_rules=active_feedback_rules,
                insight_strategy_prompt=insight_strategy_prompt,
            )
            llm_calls += calls
            generation_method = "multistep"
            active_quality_client = fallback_chain[0] if fallback_chain else self.primary
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
                pre_analysis=pre_analysis,
                active_feedback_rules=active_feedback_rules,
                insight_strategy_prompt=insight_strategy_prompt,
            )
            llm_calls += 1
            generation_method = "single"
            if lock_quality_provider:
                for candidate in fallback_chain:
                    if str(candidate.provider_name).strip().lower() == str(provider_used).strip().lower():
                        active_quality_client = candidate
                        break

        quality_stage_client = active_quality_client if lock_quality_provider else self.secondary

        raw_content = draft

        # Step 2: 품질 레이어 SEO 최적화
        if self.enable_seo_optimization:
            raw_content = await self._apply_seo(
                raw_content,
                job.seed_keywords,
                quality_stage_client,
                token_usage=token_usage,
                active_feedback_rules=active_feedback_rules,
            )
            llm_calls += 1

        # Step 3: 품질 레이어 팩트체크 (전략 E)
        fact_check_applied = False
        if self.enable_fact_check:
            fact_result = await self._fact_check(
                raw_content,
                quality_stage_client,
                token_usage=token_usage,
            )
            llm_calls += 1
            if fact_result.needs_revision:
                raw_content = await self._apply_fact_revisions(
                    raw_content,
                    fact_result,
                    quality_stage_client,
                    token_usage=token_usage,
                )
                llm_calls += 1
                fact_check_applied = True

        # Step 4: 품질 레이어 검증 및 재작성 루프 (전략 C)
        quality_result = QualityResult(score=100, gate="pass")
        quality_client = self._select_quality_client(quality_stage_client=quality_stage_client)
        quality_backup_client = None if lock_quality_provider else self.primary
        if self.enable_quality_check:
            for attempt in range(self.max_rewrites + 1):
                quality_result = await self._check_quality(
                    raw_content,
                    job,
                    quality_client,
                    backup_client=quality_backup_client,
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
                        quality_client,
                        token_usage=token_usage,
                    )
                    llm_calls += 1
                    rewrite_count += 1

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
                active_feedback_rules=active_feedback_rules,
            )
            llm_calls += 1
            voice_rewrite_applied = content != raw_content

        # Step 6: 최종 문장 다듬기(Call D, 저가 모델)
        if content and len(content) > 100:
            content = await self._run_sentence_polish(
                content=content,
                insight_strategy_prompt=insight_strategy_prompt,
                token_usage=token_usage,
            )
            llm_calls += 1

        # Step 6-1: 정확 구문 키워드 과반복 완화 (SEO/가독성 보호)
        content = self._sanitize_meta_headings(content)
        content = self._normalize_heading_levels(content)
        content = self._normalize_markdown_spacing(content)
        content = self._repair_markdown_tables(content)
        content = self._sanitize_language_artifacts(content)
        content = self._sanitize_generic_blog_phrases(content)
        if news_context:
            content = self._sanitize_market_sensitive_claims(content, news_context)
        if self._should_limit_keyword_repetition(job=job, topic_mode=topic_mode):
            content = self._limit_exact_keyword_repetition(
                content=content,
                keywords=job.seed_keywords,
                max_exact_matches=2,
            )
        if news_context:
            content = self._append_news_sources(content, news_context)
        if self._is_economy_topic(topic_mode):
            content = self._append_market_safety_disclaimer(content)

        insight_quality = self.insight_quality_evaluator.evaluate(
            content=content,
            title=job.title,
            keywords=job.seed_keywords,
            topic_mode=topic_mode,
            strategy=insight_strategy,
        )
        if (
            insight_quality.needs_rewrite
            and self.enable_quality_check
            and self.max_rewrites > 0
            and self._is_insight_rewrite_candidate(content)
        ):
            improved_content = await self._rewrite_for_insight_quality(
                content=content,
                insight_quality=insight_quality,
                client=quality_client,
                news_context=news_context,
                token_usage=token_usage,
            )
            if improved_content != content:
                content = improved_content
                llm_calls += 1
                insight_quality = self.insight_quality_evaluator.evaluate(
                    content=content,
                    title=job.title,
                    keywords=job.seed_keywords,
                    topic_mode=topic_mode,
                    strategy=insight_strategy,
                )

        # Step 7: 이미지 프롬프트 생성
        image_prompts, image_placements, image_slots = await self._generate_image_prompts(
            content,
            job.title,
            job.seed_keywords,
            quality_stage_client,
            token_usage=token_usage,
        )
        llm_calls += 1

        # SEO 스냅샷 구성
        seo_snapshot = {
            "keywords": job.seed_keywords,
            "keyword_count": self._compute_keyword_count(
                content=content,
                keywords=job.seed_keywords,
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
            "insight_strategy": insight_strategy.to_dict(),
            "source_context_count": len(news_context),
            "market_snapshot": market_snapshot_meta,
        }

        writing_strategy_snapshot = (
            writing_strategy_plan.to_snapshot() if writing_strategy_plan is not None else {}
        )
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
                "insight_quality": insight_quality.to_dict(),
                "writing_strategy": writing_strategy_snapshot,
                "raw_content_length": len(raw_content),
                "final_content_length": len(content),
                "required_quality_score": required_quality_score,
                "quality_slot_type": quality_slot_type,
                "pipeline_layers": {
                    "quality_topic_mode": topic_mode,
                    "voice_rewrite_applied": voice_rewrite_applied,
                    "applied_feedback_rules": active_feedback_rules,
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
            "parser": {
                "input_tokens": 0,
                "output_tokens": 0,
                "calls": 0,
                "provider": "",
                "model": "",
                "by_provider": {},
            },
            "pre_analysis": {
                "input_tokens": 0,
                "output_tokens": 0,
                "calls": 0,
                "provider": "",
                "model": "",
                "by_provider": {},
            },
            "quality_step": {
                "input_tokens": 0,
                "output_tokens": 0,
                "calls": 0,
                "provider": "",
                "model": "",
                "by_provider": {},
            },
            "voice_step": {
                "input_tokens": 0,
                "output_tokens": 0,
                "calls": 0,
                "provider": "",
                "model": "",
                "by_provider": {},
            },
            "sentence_polish": {
                "input_tokens": 0,
                "output_tokens": 0,
                "calls": 0,
                "provider": "",
                "model": "",
                "by_provider": {},
            },
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

        normalized_provider = str(provider or "").strip().lower() or "unknown"
        current_provider = str(bucket.get("provider", "")).strip()
        if not current_provider:
            bucket["provider"] = normalized_provider
        elif current_provider != normalized_provider:
            bucket["provider"] = "mixed"

        if not str(bucket.get("model", "")).strip():
            bucket["model"] = str(response.model or "").strip()

        by_provider = bucket.get("by_provider", {})
        if not isinstance(by_provider, dict):
            by_provider = {}
        provider_bucket = by_provider.get(
            normalized_provider,
            {"input_tokens": 0, "output_tokens": 0, "calls": 0, "model": ""},
        )
        provider_bucket["input_tokens"] = int(provider_bucket.get("input_tokens", 0) or 0) + max(
            0, int(response.input_tokens or 0)
        )
        provider_bucket["output_tokens"] = int(provider_bucket.get("output_tokens", 0) or 0) + max(
            0, int(response.output_tokens or 0)
        )
        provider_bucket["calls"] = int(provider_bucket.get("calls", 0) or 0) + 1
        current_model = str(provider_bucket.get("model", "")).strip()
        response_model = str(response.model or "").strip()
        if not current_model:
            provider_bucket["model"] = response_model
        elif response_model and current_model != response_model:
            provider_bucket["model"] = "mixed"
        by_provider[normalized_provider] = provider_bucket
        bucket["by_provider"] = by_provider

    def _is_cost_strict_active(self) -> bool:
        """가성비 strict 모드 활성 여부를 반환한다."""
        return self.strategy_mode == "cost" and self.cost_strict_mode

    def _resolve_retry_profile(self, *, role: str, max_retries: int) -> Tuple[int, Optional[float], Optional[float]]:
        """역할별 재시도 파라미터를 계산한다."""
        effective_retries = max(1, int(max_retries or 1))
        if not self._is_cost_strict_active():
            return effective_retries, None, None
        # 가성비 strict 모드에서는 무료/저가 모델에 더 오래 대기하며 재시도한다.
        strict_roles = {"parser", "pre_analysis", "quality_step", "voice_step", "sentence_polish"}
        if role not in strict_roles:
            return effective_retries, None, None
        return (
            max(effective_retries, self.cost_retry_max_retries),
            self.cost_retry_base_delay_sec,
            self.cost_retry_max_delay_sec,
        )

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
        effective_retries, retry_base_delay_sec, retry_max_delay_sec = self._resolve_retry_profile(
            role=role,
            max_retries=max_retries,
        )
        if self.circuit_breaker and self.circuit_breaker.is_open(provider_name):
            logger.warning("Circuit open on %s, skipping provider", provider_name)
            raise ProviderCircuitOpenError(provider_name)
        try:
            try:
                response = await client.generate_with_retry(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    max_retries=effective_retries,
                    retry_base_delay_sec=retry_base_delay_sec,
                    retry_max_delay_sec=retry_max_delay_sec,
                )
            except TypeError as exc:
                error_text = str(exc)
                if "retry_base_delay_sec" not in error_text and "retry_max_delay_sec" not in error_text:
                    raise
                # 구형 클라이언트/테스트 더블과의 하위호환
                response = await client.generate_with_retry(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    max_retries=effective_retries,
                )
            if self.circuit_breaker:
                self.circuit_breaker.record_success(provider_name)
        except RateLimitError:
            if self.circuit_breaker:
                self.circuit_breaker.record_failure(provider_name)
            raise
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if self.circuit_breaker and status_code in (401, 403, 404, 410):
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

    def _select_pre_analysis_client(self) -> BaseLLMClient:
        """사전 분석에 사용할 안정적인 경량 클라이언트를 고른다."""

        preferred_order = [
            self.parser_client,
            self.secondary,
            self.primary,
            self.voice_client,
            *self.additional_clients,
        ]
        risky_providers = {"groq", "cerebras", "nvidia"}
        for client in preferred_order:
            if client is None:
                continue
            provider = str(getattr(client, "provider_name", "") or "").strip().lower()
            if provider and provider not in risky_providers:
                return client
        for client in preferred_order:
            if client is not None:
                return client
        return self.primary

    async def _run_pre_writing_analysis(
        self,
        job: Job,
        topic_mode: str,
        *,
        insight_strategy_prompt: str = "",
        token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Call A: 사전 사고 분석을 수행한다."""
        user_prompt = PRE_WRITING_ANALYSIS_PROMPT.format(
            title=job.title,
            keywords=", ".join(job.seed_keywords),
            category=topic_mode,
        )
        if insight_strategy_prompt:
            user_prompt = (
                f"{user_prompt}\n\n{insight_strategy_prompt}\n\n"
                "사전 분석에서도 위 전략을 반영해, 독자를 가르치는 구조가 아니라 "
                "작성자가 함께 공부하며 판단 기준을 정리하는 구조로 설계하세요."
        )
        system_prompt = "당신은 블로그 글의 전략 설계사입니다. 반드시 유효한 JSON으로만 응답하세요."
        cheap_client = self._select_pre_analysis_client()
        try:
            response = await self._generate_with_usage(
                client=cheap_client,
                role="pre_analysis",
                token_usage=token_usage,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.3,
                max_tokens=1200,
                max_retries=2,
            )
            raw = response.content.strip()
            if "```" in raw:
                json_match = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
                if json_match:
                    raw = json_match.group(1).strip()
            parsed = self._parse_json_response(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception as exc:
            logger.warning("Call A (pre_writing_analysis) 실패, 기본값으로 진행: %s", exc)
            return {}

    async def _run_sentence_polish(
        self,
        content: str,
        *,
        insight_strategy_prompt: str = "",
        token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> str:
        """Call D: 최종 문장 표현을 다듬는다."""
        user_prompt = SENTENCE_CRAFT_CHECKLIST.format(content=content)
        if insight_strategy_prompt:
            user_prompt = (
                f"{user_prompt}\n\n{insight_strategy_prompt}\n\n"
                "[최종 쉬운 학습 문체 보정]\n"
                "- 어려운 용어는 고등학생도 이해할 수 있게 쉬운 말로 풀어주세요.\n"
                "- 독자를 가르치거나 지시하지 말고, 작성자가 함께 공부하며 확인하는 태도로 바꾸세요.\n"
                "- 단정형 투자 표현은 조건부 표현으로 낮추세요.\n"
                "- 정보, 수치, URL, H2 구조는 절대 바꾸지 마세요.\n"
            )
        system_prompt = (
            "당신은 한국어 편집 전문가입니다. "
            "원문의 정보, H2 구조, 문단 수, URL, 수치는 절대 변경하지 마세요. "
            "문장 표현과 리듬만 다듬으세요. "
            "최종 문체는 고등학생도 이해할 수 있는 쉬운 한국어와 함께 공부하는 1인칭 태도입니다."
        )
        cheap_client = self._select_sentence_polish_client()
        try:
            response = await self._generate_with_usage(
                client=cheap_client,
                role="sentence_polish",
                token_usage=token_usage,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.3,
                max_tokens=4000,
                max_retries=2,
            )
            polished = response.content.strip()
            if not polished:
                return self._local_plain_language_polish(content)

            # H2 섹션 수가 변하면 구조가 바뀌었다고 보고 원문 유지
            if polished.count("## ") != content.count("## "):
                logger.warning(
                    "Call D: H2 개수 불일치 (원본 %d, 다듬기 %d) -> 원문 유지",
                    content.count("## "),
                    polished.count("## "),
                )
                return self._local_plain_language_polish(content)

            # 길이 변동이 과도하면 의미 훼손 위험이 있어 원문 유지
            if abs(len(polished) - len(content)) / max(len(content), 1) > 0.15:
                logger.warning("Call D: 길이 변화 15%% 초과 -> 원문 유지")
                return self._local_plain_language_polish(content)
            return self._local_plain_language_polish(polished)
        except Exception as exc:
            logger.warning("Call D (sentence_polish) 실패, 원문 유지: %s", exc)
            return self._local_plain_language_polish(content)

    def _select_sentence_polish_client(self) -> BaseLLMClient:
        """문장 다듬기에 사용할 안정적인 클라이언트를 고른다."""
        preferred_order = [
            self.secondary,
            self.primary,
            self.parser_client,
            self.voice_client,
            *self.additional_clients,
        ]
        risky_providers = {"groq", "cerebras", "nvidia"}
        for client in preferred_order:
            if client is None:
                continue
            provider = str(getattr(client, "provider_name", "") or "").strip().lower()
            if provider and provider not in risky_providers:
                return client
        for client in preferred_order:
            if client is not None:
                return client
        return self.primary

    def _local_plain_language_polish(self, content: str) -> str:
        """LLM 다듬기 실패 시 구조를 보존하며 쉬운 문장으로 최소 보정한다."""
        lines = str(content or "").splitlines()
        if not lines:
            return content

        polished_lines: List[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                polished_lines.append(line)
                continue
            bullet_match = re.match(r"^(\s*(?:[-*]|\d+[\.)])\s+)(.+)$", line)
            if bullet_match:
                prefix, body = bullet_match.groups()
                polished_lines.append(f"{prefix}{self._simplify_plain_text_line(body.strip())}")
                continue
            if self._is_markdown_structural_line(stripped):
                polished_lines.append(line)
                continue
            leading = line[: len(line) - len(line.lstrip())]
            simplified = self._simplify_plain_text_line(stripped)
            polished_lines.append(f"{leading}{simplified}")
        return "\n".join(polished_lines).strip()

    def _is_markdown_structural_line(self, stripped: str) -> bool:
        """표/목록/제목/링크 등 구조 라인은 로컬 보정에서 제외한다."""
        return (
            stripped.startswith(("#", "|", "-", "*", ">", "```"))
            or bool(re.match(r"^\d+[\.)]\s+", stripped))
            or stripped.startswith("참고 자료:")
        )

    def _simplify_plain_text_line(self, line: str) -> str:
        """단정 표현을 낮추고 어려운 용어 설명을 보강한다."""
        text = str(line or "").strip()
        replacements = [
            ("펀더멘털", "펀더멘털(기업이나 산업의 기초 체력)"),
            ("프리마켓", "프리마켓(미국 정규장 전에 열리는 거래)"),
            ("선물", "선물(앞으로의 가격을 미리 거래하는 상품)"),
            ("외국인 수급", "외국인 수급(외국인 투자자의 사고파는 흐름)"),
            ("환율", "환율(원화와 달러의 교환 비율)"),
            ("금리", "금리(돈을 빌릴 때 붙는 이자율)"),
            ("ETF", "ETF(여러 자산을 한 바구니처럼 담은 상장 펀드)"),
            ("변동성", "변동성(가격이 흔들리는 정도)"),
            ("동조화", "동조화(서로 비슷한 방향으로 움직이는 현상)"),
        ]
        for source, target in replacements:
            if source in text and target not in text:
                text = text.replace(source, target, 1)
        if "수급" in text and "수급(" not in text:
            text = text.replace("수급", "수급(사고파는 힘의 균형)", 1)

        soft_replacements = {
            "확실합니다": "그렇게 볼 여지가 있습니다",
            "분명합니다": "그렇게 볼 여지가 있습니다",
            "이어질 것입니다": "이어질 가능성을 확인해보려 합니다",
            "수혜가 예상됩니다": "영향을 받을 수 있는지 살펴보려 합니다",
            "오를 것입니다": "오를지 확인이 필요합니다",
            "내릴 것입니다": "내릴지 확인이 필요합니다",
        }
        for source, target in soft_replacements.items():
            text = text.replace(source, target)

        text = re.sub(r"\s*->\s*", ". 그 다음 ", text)
        return self._split_overlong_korean_sentences(text)

    def _split_overlong_korean_sentences(self, text: str, *, max_chars: int = 95) -> str:
        """너무 긴 한국어 문장을 의미 훼손이 적은 연결어에서 나눈다."""
        sentences = re.split(r"(?<=[.!?。])\s+", str(text or "").strip())
        output: List[str] = []
        for sentence in sentences:
            if len(sentence) <= max_chars:
                output.append(sentence)
                continue
            split_done = False
            for marker in (
                " 하지만 ",
                " 그래서 ",
                " 다만 ",
                " 반대로 ",
                " 그런데 ",
                " 여기서 ",
                " 이때 ",
                " 쉽게 말하면 ",
                " 예를 들어 ",
                " 이 말은 ",
            ):
                idx = sentence.find(marker)
                if 35 <= idx <= len(sentence) - 25:
                    first = sentence[:idx].rstrip()
                    second = sentence[idx + 1 :].lstrip()
                    output.extend([first, second])
                    split_done = True
                    break
            if not split_done:
                output.append(sentence)
        return " ".join(part for part in output if part).strip()

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
        if persona_mode in {"cafe", "it", "parenting", "finance", "health"}:
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
        if any(token in lowered for token in ("건강", "의학", "의료", "운동", "수면", "식단", "health")):
            return "health"
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

    def _load_int_setting(
        self,
        *,
        setting_key: str,
        env_name: str,
        default: int,
        min_value: int,
        max_value: int,
    ) -> int:
        """정수 설정을 env 우선, 없으면 DB(system_settings)에서 읽는다."""
        raw_env = str(os.getenv(env_name, "")).strip()
        if raw_env:
            try:
                value = int(raw_env)
                return max(min_value, min(max_value, value))
            except Exception:
                pass

        try:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute(
                    """
                    SELECT setting_value
                    FROM system_settings
                    WHERE setting_key = ?
                    """,
                    (str(setting_key).strip(),),
                ).fetchone()
            finally:
                conn.close()
        except Exception:
            row = None

        if row and row[0] is not None:
            try:
                value = int(str(row[0]).strip())
                return max(min_value, min(max_value, value))
            except Exception:
                pass

        return max(min_value, min(max_value, int(default)))

    def _load_active_feedback_rules(self) -> List[str]:
        """자동 반영으로 활성화된 피드백 규칙 목록을 조회한다."""
        limit = max(1, int(constants.FEEDBACK_MAX_CONCURRENT_ACTIVE_RULES))
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT rule_text
                    FROM feedback_rule_active
                    WHERE status = 'active'
                    ORDER BY activated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return []

        rules: List[str] = []
        for row in rows:
            text = str(row["rule_text"] or "").strip()
            if text:
                rules.append(text)
        return rules

    # 이미지/레이아웃 스코프 규칙 감지 키워드 (텍스트 생성 단계에서 제외)
    # 주의: "배치", "삽입", "크기"는 텍스트 문맥에서도 자주 쓰이므로 의도적으로 제외
    #   예) "핵심 문장을 앞에 배치하세요", "키워드를 자연스럽게 배치하세요"
    _IMAGE_SCOPE_KEYWORDS: List[str] = [
        "이미지", "사진", "썸네일", "thumbnail", "image", "photo",
        "여백", "spacing", "레이아웃", "layout", "시각", "visual",
        "해상도",
    ]

    @classmethod
    def _is_image_scope_rule(cls, rule_text: str) -> bool:
        """피드백 규칙이 이미지/레이아웃 스코프인지 키워드로 판단한다.

        이미지·레이아웃 관련 규칙은 텍스트 생성(SEO, Voice) 단계에 주입하지 않는다.
        """
        lower = rule_text.lower()
        return any(kw in lower for kw in cls._IMAGE_SCOPE_KEYWORDS)

    @classmethod
    def _filter_rules_for_text_stage(cls, rules: List[str]) -> List[str]:
        """텍스트 생성 단계용으로 이미지 스코프 규칙을 제거한다."""
        return [r for r in rules if not cls._is_image_scope_rule(r)]

    def _build_feedback_rules_injection(self, active_rules: List[str]) -> str:
        """활성 피드백 규칙을 프롬프트 주입 문자열로 변환한다."""
        if not active_rules:
            return ""

        lines = []
        for item in active_rules[: max(1, int(constants.FEEDBACK_MAX_CONCURRENT_ACTIVE_RULES))]:
            normalized = str(item or "").strip()
            if not normalized:
                continue
            lines.append(f"- {normalized}")
        if not lines:
            return ""

        return (
            "\n\n[최근 시각 품질 개선 규칙]\n"
            + "\n".join(lines)
            + "\n추가 지시:\n"
            "- 위 규칙을 우선 반영하되, 사실 왜곡 없이 자연스럽게 적용하세요.\n"
            "- 규칙 간 충돌이 있으면 가독성과 독자 이해를 우선하세요.\n"
        )

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
        if self.rag_search_engine is None:
            return []
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

    async def _collect_web_context(
        self,
        keywords: List[str],
        fallback_text: str = "",
        max_items: int = 2,
    ) -> List[Dict[str, str]]:
        """웹 검색 기반 외부 컨텍스트를 수집한다."""
        if self.web_search_client is None:
            return []

        query_parts: List[str] = [
            str(keyword).strip()
            for keyword in keywords
            if str(keyword).strip()
        ]
        if not query_parts and str(fallback_text).strip():
            # 키워드가 비어도 제목으로 검색 폴백한다.
            query_parts.append(str(fallback_text).strip())
        if not query_parts:
            return []

        query = " ".join(query_parts)
        search_limit = min(max(self.web_search_max_results, max_items + 2), 20)
        try:
            search_results = await self.web_search_client.search(
                query,
                max_results=search_limit,
            )
        except Exception as exc:
            logger.warning("Web search failed: %s", exc)
            return []

        if not search_results:
            return []

        # score 필드가 있는 경우 점수순으로 정렬 (BraveSearchClient 응답에서 이미 처리되지만 안전장치로 추가)
        scored_results = sorted(search_results, key=lambda x: getattr(x, "score", 0.0), reverse=True)
        selected_results = scored_results[: max_items + 2]

        async def _fetch_candidate(sr: Any) -> str:
            base_content = str(getattr(sr, "snippet", "")).strip()
            url = str(getattr(sr, "url", "")).strip()
            if self.web_fetch_client is None or not url:
                return base_content
            try:
                # 전체 지연을 줄이기 위해 개별 URL fetch에 타임아웃을 건다.
                fetched = await asyncio.wait_for(
                    self.web_fetch_client.fetch_content(url),
                    timeout=8.0,
                )
            except Exception as exc:
                logger.debug("Web fetch failed for %s: %s", url, exc)
                return base_content
            if isinstance(fetched, dict):
                text = str(fetched.get("content", "")).strip()
                if text:
                    return text
            return base_content

        # fetch는 병렬로 수행해 토픽당 지연 시간을 줄인다.
        fetched_contents = await asyncio.gather(
            *[_fetch_candidate(sr) for sr in selected_results],
            return_exceptions=False,
        )

        contexts: List[Dict[str, str]] = []
        for sr, fetched_text in zip(selected_results, fetched_contents):
            title = str(getattr(sr, "title", "")).strip()
            link = str(getattr(sr, "url", "")).strip()
            content = str(fetched_text).strip()
            score = getattr(sr, "score", 0.0)
            
            if not title or not link or not content:
                continue
            contexts.append(
                {
                    "title": title,
                    "link": link,
                    "content": content,
                    "score": score,
                }
            )
            if len(contexts) >= max_items:
                break

        if contexts:
            avg_score = sum(c["score"] for c in contexts) / len(contexts)
            logger.info(
                "Web context collected (Scoring applied)",
                extra={
                    "query": query[:80],
                    "result_count": len(contexts),
                    "avg_score": round(avg_score, 2),
                },
            )
        return contexts

    def _collect_market_snapshot_context(
        self,
        job: Job,
    ) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
        """시장 슬롯 작업이면 무료 시장 스냅샷을 LLM 컨텍스트로 변환한다."""

        slot = self._resolve_market_slot(job)
        scope = self._resolve_market_scope(job)
        if slot is None:
            return [], {}

        try:
            from ..market import BlogSlot, MarketDataCollector, MarketScope
        except Exception as exc:
            logger.debug("Market modules unavailable: %s", exc)
            return [], {}

        if scope == MarketScope.EVERGREEN and slot in {
            BlogSlot.EVERGREEN_INSIGHT,
            BlogSlot.WEEKLY_REFLECTION,
        }:
            return [], {
                "slot": slot.value,
                "scope": scope.value,
                "mode": "evergreen",
                "reason": "휴장/주말 대체 슬롯은 실시간 숫자보다 통찰형 글을 우선한다.",
            }

        collector = self.market_data_collector
        if collector is None:
            try:
                collector = MarketDataCollector()
                self.market_data_collector = collector
            except Exception as exc:
                logger.warning("Market data collector init skipped: %s", exc)
                return [], {}

        try:
            snapshot = collector.collect(scope, slot=slot, max_news_items=4)
        except Exception as exc:
            logger.warning("Market data snapshot failed: %s", exc)
            return [], {
                "slot": slot.value,
                "scope": scope.value,
                "mode": "collection_failed",
                "reason": str(exc),
            }

        context = self._market_snapshot_to_context(snapshot)
        data_point_meta: List[Dict[str, Any]] = []
        for point in list(getattr(snapshot, "data_points", ()) or ())[:12]:
            observed_at = getattr(point, "observed_at", None)
            if hasattr(observed_at, "isoformat"):
                observed_text = observed_at.isoformat()
            else:
                observed_text = str(observed_at or "")
            data_point_meta.append(
                {
                    "symbol": str(getattr(point, "symbol", "") or "").strip(),
                    "label": str(getattr(point, "label", "") or "").strip(),
                    "source": str(getattr(point, "source", "") or "").strip(),
                    "value": getattr(point, "value", None),
                    "change_percent": getattr(point, "change_percent", None),
                    "observed_at": observed_text,
                    "url": str(getattr(point, "url", "") or "").strip(),
                }
            )
        meta = {
            "slot": str(getattr(snapshot.slot, "value", snapshot.slot or "")),
            "scope": str(getattr(snapshot.scope, "value", snapshot.scope)),
            "mode": str(getattr(snapshot.data_mode, "value", snapshot.data_mode)),
            "confidence_score": getattr(snapshot.confidence, "score", 0.0),
            "allow_numeric_claims": bool(getattr(snapshot.confidence, "allow_numeric_claims", False)),
            "data_point_count": len(getattr(snapshot, "data_points", ()) or ()),
            "data_points": data_point_meta,
            "chart_recommended": len(data_point_meta) >= 2,
            "news_item_count": len(getattr(snapshot, "news_items", ()) or ()),
            "skipped_source_count": len(getattr(snapshot, "skipped_sources", ()) or ()),
            "reason": str(getattr(snapshot.confidence, "reason", "")),
        }
        return ([context] if context else []), meta

    def _build_market_slot_writing_injection(self, job: Job) -> str:
        """시장/통찰 슬롯별 글 완성도 지시를 만든다."""

        slot = self._resolve_market_slot(job)
        if slot is None:
            return ""
        try:
            from ..market import BlogSlot
        except Exception:
            return ""

        strategy_plan = select_market_writing_strategy(
            title=job.title,
            tags=job.tags or [],
            seed_keywords=job.seed_keywords,
        )
        strategy_prompt = render_strategy_prompt(strategy_plan, heading="경제 글쓰기 전략 라우터 지시")
        common_guard = """
[경제 글 안전장치]
- 본문 어딘가에 근거 블록, 반대 신호 블록, 투자권유 회피 블록을 모두 포함하세요.
- 특정 종목은 "추천"이 아니라 "확인 대상" 또는 "관찰할 변수"로만 표현하세요.
- 숫자에는 기준일/출처/한계를 함께 적고, NewsData에 없는 수치 생성은 금지합니다.
- "투자 판단은 개인 책임이며, 이 글은 공부용 시장 정리" 취지의 문장을 자연스럽게 남기세요.
""".strip()

        if slot in {BlogSlot.EVERGREEN_INSIGHT, BlogSlot.WEEKLY_REFLECTION}:
            base_prompt = """
[통찰형 시장 슬롯 작성 지시]
- 실시간 숫자 요약이 아니라, 투자 초심자가 가져갈 판단 기준을 정리하세요.
- 본문은 공백 포함 최소 1,500자 이상으로 완성하세요.
- 서론 뒤 H2 소제목을 최소 4개 작성하세요.
- 각 H2는 서로 다른 역할을 맡습니다: 경험/오해, 기준, 리스크 장치, 오늘의 공부 질문.
- 생활 제약을 1회 이상 자연스럽게 넣으세요. 예: 시간, 체력, 가족, 현금흐름, 초심자의 불안.
- "정보보다 기준", "흔들릴 때 돌아갈 기준", "자기수정"을 추상어로 외치지 말고 기록/체크리스트/질문으로 풀어 쓰세요.
- 마지막에는 "오늘 함께 확인할 공부 질문" 2~3개를 남기고 글을 완결하세요.
- 중간에서 끊긴 문장이나 미완성 문단을 남기지 마세요.
""".strip()
            return f"{strategy_prompt}\n\n{base_prompt}\n\n{common_guard}"

        base_prompt = """
[시장 브리핑 슬롯 작성 지시]
- 숫자보다 먼저 오늘 확인할 기준을 분명히 적으세요.
- 표를 만들면 모든 행의 열 개수를 맞추고, 모르는 값은 "미수집"으로 적으세요.
- 투자 결론처럼 보이는 단정 표현은 피하고 조건과 한계를 같이 쓰세요.
""".strip()
        return f"{strategy_prompt}\n\n{base_prompt}\n\n{common_guard}"

    def _resolve_writing_strategy_plan(self, *, job: Job, topic_mode: str) -> Any:
        """job 태그와 토픽으로 글쓰기 전략 계획을 반환한다."""

        try:
            if self._resolve_market_slot(job) is not None or self._is_economy_topic(topic_mode):
                return select_market_writing_strategy(
                    title=job.title,
                    tags=job.tags or [],
                    seed_keywords=job.seed_keywords,
                )
            template_id = self._first_tag_value(job, "category_template:")
            if template_id:
                return select_category_writing_strategy(
                    topic_mode=topic_mode,
                    template_id=template_id,
                    title=job.title,
                    tags=job.tags or [],
                )
        except Exception:
            logger.debug("writing strategy plan resolution skipped", extra={"job_id": job.job_id}, exc_info=True)
        return None

    def _append_market_safety_disclaimer(self, content: str) -> str:
        """경제 글 말미에 투자권유 회피 문구를 보강한다."""

        text = str(content or "").rstrip()
        if not text:
            return text
        normalized = re.sub(r"\s+", " ", text)
        if "공부용 시장 정리" in normalized and "투자 판단" in normalized:
            return text
        disclaimer = (
            "이 글은 특정 종목의 거래를 권하는 글이 아니라 시장을 공부하기 위한 정리입니다. "
            "최종 투자 판단과 책임은 각자에게 있다는 점을 꼭 함께 확인해 주세요."
        )
        return f"{text}\n\n{disclaimer}"

    def _build_category_template_writing_injection(self, job: Job, topic_mode: str) -> str:
        """확장 카테고리 글 양식 지시를 만든다."""

        template_id = self._first_tag_value(job, "category_template:")
        if not template_id:
            return ""
        templates = get_templates_for_topic(topic_mode)
        template = next((item for item in templates if item.template_id == template_id), None)
        if template is None:
            return ""

        source_names = [
            self._tag_display_name(tag.split(":", 1)[1])
            for tag in (job.tags or [])
            if str(tag or "").lower().startswith("creator_source:")
        ][:4]
        sources_text = ", ".join(source_names) if source_names else "등록된 watchlist"
        health_guard = ""
        if normalize_topic_mode(topic_mode) == "health":
            health_guard = (
                "\n- 건강 글은 진단, 치료, 완치, 보장 표현을 쓰지 말고 "
                "근거 수준과 전문가 상담이 필요한 경우를 반드시 함께 적으세요."
            )
        strategy_plan = select_category_writing_strategy(
            topic_mode=topic_mode,
            template_id=template.template_id,
            title=job.title,
            tags=job.tags or [],
        )
        strategy_prompt = (
            f"\n\n{render_strategy_prompt(strategy_plan, heading='확장 카테고리 글쓰기 전략 라우터 지시')}"
            if strategy_plan is not None
            else ""
        )

        return f"""
[카테고리 글 양식 지시]
- 선택 양식: {template.label} ({template.template_id})
- 참고 채널: {sources_text}
- 본문 구조: {template.structure_hint}
- 표/카드 역할: {template.card_role}
- 외부 콘텐츠를 복사하지 말고 핵심 주장과 관점을 참고해 새 글로 재구성하세요.
- 단일 출처처럼 보이지 않도록 최소 2개 이상의 관점 또는 확인 질문을 포함하세요.{health_guard}{strategy_prompt}
""".strip()

    def _tag_display_name(self, value: str) -> str:
        """태그 안전 문자열을 사람이 읽기 쉬운 이름으로 바꾼다."""

        return str(value or "").strip().replace("_", " ")

    def _market_snapshot_to_context(self, snapshot: Any) -> Dict[str, str]:
        """시장 스냅샷을 기존 NewsData 프롬프트 형식에 맞춘다."""

        slot_value = str(getattr(getattr(snapshot, "slot", None), "value", getattr(snapshot, "slot", "") or ""))
        scope_value = str(getattr(getattr(snapshot, "scope", None), "value", getattr(snapshot, "scope", "") or ""))
        confidence = getattr(snapshot, "confidence", None)
        mode = str(getattr(getattr(snapshot, "data_mode", ""), "value", getattr(snapshot, "data_mode", "")))
        allow_numeric = bool(getattr(confidence, "allow_numeric_claims", False))
        reason = str(getattr(confidence, "reason", "")).strip()

        lines: List[str] = [
            f"슬롯: {slot_value or '시장 브리핑'}",
            f"범위: {scope_value or 'global'}",
            "소스 정책: light - 키 없이 접근 가능한 공개 CSV/RSS/원문 링크를 우선 사용",
            f"데이터 모드: {mode or 'unknown'}",
            f"수치 단정 허용: {'예' if allow_numeric else '아니오'}",
            "문체 지침: 수치는 예측의 결론이 아니라 함께 공부할 재료로 설명할 것.",
            "문체 지침: 고등학생도 이해할 수 있게 쉬운 말로 풀고, 가르치기보다 같이 확인하는 태도로 쓸 것.",
            "문체 지침: ETF, 금리, 환율, 선물, 수급 같은 용어는 처음 나오는 문장 바로 뒤에 쉬운 설명을 붙일 것.",
            "문체 지침: 한 문장은 되도록 60자 안팎으로 짧게 쓰고, 긴 비교 문장은 둘로 나눌 것.",
            "구조 지침: 서론 뒤 H2 소제목을 최소 4개 만들고, 본문은 최소 1,500자 이상으로 완성할 것.",
            "표 지침: 표를 만들면 모든 행의 열 개수를 맞추고, 모르는 값은 '미수집'이라고 적을 것.",
            "금지: '[출력]', '[본문]', '수정본', '리라이트본' 같은 프롬프트 라벨을 본문에 남기지 말 것.",
            "금지: 제공된 스냅샷에 없는 역사적 사례, 평균 수치, 인물 변화, 종목별 확정 전망을 새로 만들어 넣지 말 것.",
            "금지: '확실하다', '이어질 것이다', '수혜가 예상된다'처럼 투자 결론처럼 들리는 표현을 피할 것.",
        ]
        if reason:
            lines.append(f"신뢰도 판단: {reason}")
        if not allow_numeric:
            lines.append(
                "작성 제한: 아래 숫자는 참고 지표로만 사용하고, 상승/하락 예측이나 매매 판단으로 단정하지 말 것."
            )
            lines.append(
                "작성 방향: 숫자 표보다 오늘 확인할 조건, 리스크, 잘라낼 행동을 중심으로 설명할 것."
            )

        data_points = list(getattr(snapshot, "data_points", ()) or ())
        if data_points:
            lines.append("핵심 지표:")
            for point in data_points[:10]:
                symbol = str(getattr(point, "symbol", "")).strip()
                source = str(getattr(point, "source", "")).strip()
                value = getattr(point, "value", None)
                change = getattr(point, "change_percent", None)
                if value is None:
                    continue
                value_text = self._format_market_number(value)
                if change is None:
                    lines.append(f"- {symbol}: {value_text} ({source})")
                else:
                    change_text = self._format_market_number(change)
                    lines.append(f"- {symbol}: {value_text}, 24h/최근 변동 {change_text}% ({source})")

        news_items = list(getattr(snapshot, "news_items", ()) or ())
        if news_items:
            lines.append("연결 뉴스/공시:")
            for item in news_items[:4]:
                title = str(getattr(item, "title", "")).strip()
                source = str(getattr(item, "source", "")).strip()
                if title:
                    lines.append(f"- {title} ({source})")

        skipped_sources = list(getattr(snapshot, "skipped_sources", ()) or ())
        if skipped_sources:
            lines.append("데이터 부족 시 처리:")
            for item in skipped_sources[:5]:
                source = str(getattr(item, "source", "")).strip()
                skipped_reason = str(getattr(item, "reason", "")).strip()
                if source or skipped_reason:
                    lines.append(f"- {source}: {skipped_reason}")

        fallback_hints = list(getattr(snapshot, "fallback_topic_hints", ()) or ())
        if fallback_hints:
            lines.append("데이터가 약할 때 사용할 통찰형 방향:")
            for hint in fallback_hints[:3]:
                lines.append(f"- {hint}")

        first_link = ""
        for point in data_points:
            first_link = str(getattr(point, "url", "")).strip()
            if first_link:
                break
        if not first_link:
            for item in news_items:
                first_link = str(getattr(item, "url", "")).strip()
                if first_link:
                    break

        return {
            "title": f"시장 데이터 스냅샷: {slot_value or scope_value or 'market'}",
            "link": first_link,
            "content": "\n".join(lines).strip(),
            "source": "MarketSnapshot",
        }

    def _format_market_number(self, value: Any) -> str:
        """시장 숫자를 블로그 문장에 쓰기 쉬운 길이로 줄인다."""

        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return str(value)
        text = f"{numeric:.2f}"
        return text.rstrip("0").rstrip(".")

    def _collect_naver_context(
        self,
        *,
        job: Job,
        topic_mode: str,
        existing_count: int,
    ) -> List[Dict[str, str]]:
        """네이버 검색 API 컨텍스트를 선택적으로 보강한다."""

        collector = self.naver_search_collector
        if collector is None or not bool(getattr(collector, "enabled", False)):
            return []

        is_market_job = self._resolve_market_slot(job) is not None
        should_collect = is_market_job or (self._is_economy_topic(topic_mode) and existing_count < 2)
        if not should_collect:
            return []

        query_parts = [str(job.title).strip()]
        query_parts.extend(str(keyword).strip() for keyword in job.seed_keywords if str(keyword).strip())
        query = " ".join(item for item in query_parts if item).strip()
        if not query:
            return []

        try:
            contexts = collector.collect_context(
                query,
                services=("news", "blog"),
                per_service=2 if is_market_job else 1,
            )
        except Exception as exc:
            logger.warning("Naver search context failed: %s", exc)
            return []

        normalized: List[Dict[str, str]] = []
        for item in contexts:
            title = str(item.get("title", "")).strip()
            link = str(item.get("link", "")).strip()
            content = str(item.get("content", "")).strip()
            if not (title and link and content):
                continue
            normalized.append(
                {
                    "title": title,
                    "link": link,
                    "content": content,
                    "source": str(item.get("source", "Naver Search")).strip(),
                }
            )
            if len(normalized) >= 3:
                break
        return normalized

    def _resolve_market_slot(self, job: Job) -> Optional[Any]:
        """작업 태그에서 시장 브리핑 슬롯을 찾는다."""

        raw_slot = self._first_tag_value(job, "market_slot:")
        if not raw_slot:
            return None
        try:
            from ..market import BlogSlot
        except Exception:
            return None

        slot_map = {
            "kr_preopen": BlogSlot.KR_PREOPEN,
            "us_preopen": BlogSlot.US_PREOPEN,
            "evergreen_insight": BlogSlot.EVERGREEN_INSIGHT,
            "weekly_reflection": BlogSlot.WEEKLY_REFLECTION,
        }
        return slot_map.get(raw_slot.strip().lower())

    def _resolve_market_scope(self, job: Job) -> Any:
        """작업 태그에서 시장 범위를 찾고 없으면 슬롯으로 추론한다."""

        try:
            from ..market import BlogSlot, MarketScope
        except Exception:
            return "global"

        raw_scope = self._first_tag_value(job, "market_scope:")
        if raw_scope:
            try:
                return MarketScope(raw_scope.strip().lower())
            except ValueError:
                pass

        slot = self._resolve_market_slot(job)
        if slot == BlogSlot.KR_PREOPEN:
            return MarketScope.KR
        if slot == BlogSlot.US_PREOPEN:
            return MarketScope.US
        return MarketScope.EVERGREEN

    def _first_tag_value(self, job: Job, prefix: str) -> str:
        """태그 목록에서 prefix 뒤 값을 반환한다."""

        normalized_prefix = str(prefix or "").strip().lower()
        for tag in job.tags or []:
            text = str(tag or "").strip()
            if text.lower().startswith(normalized_prefix):
                return text[len(normalized_prefix):].strip()
        return ""

    def _collect_memory_context(
        self,
        job: "Job",
        topic_mode: str,
    ) -> str:
        """발행 이력 기반 메모리 컨텍스트 텍스트를 생성한다.

        TopicMemoryStore가 None이거나 실패해도 빈 문자열을 반환 (non-critical).
        generate()에서 호출되어 _active_memory_context에 저장된다.
        """
        if self.memory_store is None:
            return ""

        try:
            from ..memory.similarity import find_similar_posts
            from ..memory.context_builder import build_memory_context_text

            # Phase B-1: 백필은 비동기 요청(큐) 우선, 미지원 시 기존 동기 함수 폴백
            request_backfill_fn = getattr(self.memory_store, "request_backfill", None)
            if callable(request_backfill_fn):
                request_backfill_fn(limit=300)
            else:
                ensure_fn = getattr(self.memory_store, "ensure_backfilled", None)
                if callable(ensure_fn):
                    ensure_fn()

            # 같은 토픽 최근 글
            recent = self.memory_store.get_recent_by_topic(
                topic_mode=topic_mode,
                persona_id=str(job.persona_id or "P1"),
            )

            # 유사 키워드 글 (전 토픽 대상)
            cross_recent = self.memory_store.get_cross_topic_recent(limit=50)
            similar = find_similar_posts(
                title=str(job.title),
                keywords=list(job.seed_keywords),
                candidates=cross_recent,
                threshold=0.25,
                top_k=5,
            )

            text = build_memory_context_text(
                recent_posts=recent,
                similar_posts=similar,
            )
            if text:
                logger.info(
                    "Memory context injected",
                    extra={
                        "topic_mode": topic_mode,
                        "recent_count": len(recent),
                        "similar_count": len(similar),
                    },
                )
            return text

        except Exception as exc:
            logger.debug("Memory context collection failed (non-critical): %s", exc)
            return ""

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

        content = re.sub(r"\n+참고 자료:\s*[\s\S]*$", "", str(content or "").rstrip()).rstrip()
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
        active_feedback_rules: Optional[List[str]] = None,
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
        # 이미지 스코프 규칙은 텍스트(Voice) 단계와 무관하므로 필터링
        text_rules = self._filter_rules_for_text_stage(active_feedback_rules or [])
        feedback_injection = self._build_feedback_rules_injection(text_rules)
        if feedback_injection:
            user_prompt = f"{user_prompt}{feedback_injection}"
        try:
            response = await self._generate_with_usage(
                client=client,
                role="voice_step",
                token_usage=token_usage,
                system_prompt=(
                    "당신은 네이버 블로그 전문 리라이터입니다.\n"
                    "당신의 임무는 정보를 그대로 유지하면서, 글이 실제 블로거가 쓴 것처럼 느껴지도록 말투를 전면 교체하는 것입니다.\n"
                    "관공서 보고서 같은 딱딱한 문체는 절대 금지입니다.\n"
                    "네이버 블로그 독자는 친근하고 편안한 ~해요/~거든요 체를 기대합니다.\n\n"
                    f"{ANTI_AI_PATTERN_RULES}"
                ),
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
        if (
            ratio < constants.VOICE_REWRITE_MIN_LENGTH_RATIO
            or ratio > constants.VOICE_REWRITE_MAX_LENGTH_RATIO
        ):
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
        pre_analysis: Optional[Dict[str, Any]] = None,
        active_feedback_rules: Optional[List[str]] = None,
        insight_strategy_prompt: str = "",
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
                    pre_analysis=pre_analysis,
                    active_feedback_rules=active_feedback_rules,
                    insight_strategy_prompt=insight_strategy_prompt,
                )
                provider_used = client.provider_name
                if idx > 0:
                    provider_fallback_from = fallback_chain[0].provider_name
                    self._notify_fallback_success(
                        from_provider=provider_fallback_from,
                        to_provider=provider_used,
                        title=job.title,
                        job_id=job.job_id,
                    )
                return draft, provider_model, provider_used, provider_fallback_from
            except ProviderCircuitOpenError as exc:
                next_client = fallback_chain[idx + 1] if idx + 1 < len(fallback_chain) else None
                if next_client:
                    logger.warning(
                        "Circuit open detected on %s. Switching to %s",
                        exc.provider,
                        self._client_display_label(next_client),
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
                        self._client_display_label(client),
                        self._client_display_label(next_client),
                        exc,
                    )
                    continue
                raise ContentGenerationError(f"All providers failed with rate limit. Last error: {exc}") from exc
            except Exception as exc:
                next_client = fallback_chain[idx + 1] if idx + 1 < len(fallback_chain) else None
                if next_client:
                    warning_message = (
                        f"[WARNING] {self._client_display_label(client)} failed. "
                        f"Falling back to {self._client_display_label(next_client)}..."
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
        job_id: str = "",
    ) -> None:
        """폴백 성공 알림 콜백을 호출한다."""
        callback = self.fallback_alert_fn
        if callback is None:
            return
        dedupe_key = f"{str(job_id).strip()}::{str(from_provider).strip().lower()}->{str(to_provider).strip().lower()}"
        if str(job_id).strip():
            if dedupe_key in self._FALLBACK_ALERT_DEDUP_KEYS:
                return
            # 무한 증가 방지: 충분히 커지면 비운다.
            if len(self._FALLBACK_ALERT_DEDUP_KEYS) >= 10000:
                self._FALLBACK_ALERT_DEDUP_KEYS.clear()
            self._FALLBACK_ALERT_DEDUP_KEYS.add(dedupe_key)
        payload = {
            "job_id": str(job_id).strip(),
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
        pre_analysis: Optional[Dict[str, Any]] = None,
        active_feedback_rules: Optional[List[str]] = None,
        insight_strategy_prompt: str = "",
    ) -> Tuple[str, str, int]:
        """멀티스텝 생성: 아웃라인 → 섹션별 → 통합 (전략 B)."""
        llm_calls = 0
        client = fallback_chain[0]
        news_data_text = self._build_news_data_text(news_context or [])
        tone_suffix = "" if quality_only else tone_profile.prompt_suffix
        feedback_rules_injection = self._build_feedback_rules_injection(active_feedback_rules or [])
        voice_profile_text = self._build_voice_profile_text(self._load_saved_voice_profile(job.persona_id or "P1"))
        voice_injection = f"\n\n[Voice Profile (Writing Style)]\n{voice_profile_text}"
        system_prompt = QUALITY_LAYER_SYSTEM_PROMPT if quality_only else f"{SYSTEM_BLOG_WRITER}{voice_injection}"
        category_template_injection = self._build_category_template_writing_injection(job, topic_mode)

        # Step 1: 아웃라인 생성
        outline_prompt = OUTLINE_GENERATION.format(
            title=job.title,
            keywords=", ".join(job.seed_keywords),
            audience="투자 초심자와 자기 개발 관심자. 고등학생도 이해할 수 있는 쉬운 설명을 기대하는 독자",
            tone="중립 정보형" if quality_only else tone_profile.name,
        )
        if insight_strategy_prompt:
            outline_prompt = f"{outline_prompt}\n\n{insight_strategy_prompt}"
        if category_template_injection:
            outline_prompt = f"{outline_prompt}\n\n{category_template_injection}"
        if news_data_text:
            outline_prompt = (
                f"{outline_prompt}\n\n[NewsData]\n{news_data_text}\n\n"
                "반드시 NewsData의 사실에 기반해 아웃라인을 구성하세요."
            )
        if feedback_rules_injection:
            outline_prompt = f"{outline_prompt}{feedback_rules_injection}"

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
                pre_analysis=pre_analysis,
                active_feedback_rules=active_feedback_rules,
                insight_strategy_prompt=insight_strategy_prompt,
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
                pre_analysis=pre_analysis,
                active_feedback_rules=active_feedback_rules,
                insight_strategy_prompt=insight_strategy_prompt,
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
            if insight_strategy_prompt:
                section_prompt = f"{section_prompt}\n\n{insight_strategy_prompt}"
            if category_template_injection:
                section_prompt = f"{section_prompt}\n\n{category_template_injection}"
            if feedback_rules_injection:
                section_prompt = f"{section_prompt}{feedback_rules_injection}"

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
                pre_analysis=pre_analysis,
                active_feedback_rules=active_feedback_rules,
                insight_strategy_prompt=insight_strategy_prompt,
            )
            return draft, model, llm_calls + 1

        # Step 3: 통합
        integration_prompt = SECTION_INTEGRATION.format(
            sections="\n\n---\n\n".join(sections),
            tone_suffix=tone_suffix,
        )
        if insight_strategy_prompt:
            integration_prompt = f"{integration_prompt}\n\n{insight_strategy_prompt}"
        if category_template_injection:
            integration_prompt = f"{integration_prompt}\n\n{category_template_injection}"
        if news_data_text:
            integration_prompt = (
                f"{integration_prompt}\n\n[NewsData]\n{news_data_text}\n\n"
                "통합 시 NewsData에 없는 사실 추가를 금지하세요."
            )
        if feedback_rules_injection:
            integration_prompt = f"{integration_prompt}{feedback_rules_injection}"

        # AI 패턴 방지 규칙 주입 (multistep integration 단계)
        integration_prompt = f"{integration_prompt}\n\n{ANTI_AI_PATTERN_BRIEF}"

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
        pre_analysis: Optional[Dict[str, Any]] = None,
        active_feedback_rules: Optional[List[str]] = None,
        insight_strategy_prompt: str = "",
    ) -> Tuple[str, str]:
        """초안 생성."""
        topic_mode = normalize_topic_mode(topic_mode or "cafe")
        news_data_text = self._build_news_data_text(news_context or [])

        # 플랫폼별 SEO 전략 로드
        platform_strategy = get_platform_strategy(job.platform)
        seo_snippet = platform_strategy.to_prompt_snippet()

        voice_profile_text = self._build_voice_profile_text(self._load_saved_voice_profile(job.persona_id or "P1"))
        voice_injection = f"\n\n[Voice Profile (Writing Style)]\n{voice_profile_text}"

        cognitive_injection = ""
        emotional_injection = ""
        pre_analysis_injection = ""

        if quality_only:
            depth_common = COGNITIVE_DEPTH_COMMON
            depth_topic = COGNITIVE_DEPTH_BY_TOPIC.get(topic_mode, "")
            cognitive_injection = f"\n\n{depth_common}"
            if depth_topic:
                cognitive_injection += f"\n\n{depth_topic}"

            if pre_analysis and isinstance(pre_analysis.get("emotional_curve"), dict):
                emotional_curve = pre_analysis.get("emotional_curve", {})
                emotional_injection = "\n\n" + EMOTIONAL_ARCHITECTURE_PROMPT.format(
                    opening_emotion=emotional_curve.get("opening_emotion", "호기심"),
                    turning_point=emotional_curve.get("turning_point", "본문 중반에서 핵심 발견"),
                    closing_emotion=emotional_curve.get("closing_emotion", "실행 의지와 여운"),
                )

            if pre_analysis:
                reader_knowledge = str(pre_analysis.get("reader_current_knowledge", "")).strip()
                misconceptions = pre_analysis.get("reader_misconceptions", [])
                questions = pre_analysis.get("reader_top_questions", [])
                structure = pre_analysis.get("recommended_structure", [])
                misconceptions_text = ", ".join(str(item) for item in misconceptions) if isinstance(misconceptions, list) else "없음"
                questions_text = ", ".join(str(item) for item in questions) if isinstance(questions, list) else "없음"
                structure_text = json.dumps(structure, ensure_ascii=False) if isinstance(structure, list) and structure else "자유 구성"
                pre_analysis_injection = f"""

[사전 분석 결과 - 이 내용을 글의 방향에 반영하세요]
독자의 현재 지식: {reader_knowledge}
흔한 오해: {misconceptions_text}
독자의 궁금증: {questions_text}
추천 구조: {structure_text}

추가 지시:
- 이 주제에서 대부분이 믿지만 실제로는 다른 것을 최소 1개 언급하세요.
- 전혀 다른 분야의 원리와 연결점을 1개 이상 제시하세요.
"""

        # ── 메모리 컨텍스트 주입 (발행 이력 기반) ──
        memory_injection = (
            f"\n\n{self._active_memory_context}" if self._active_memory_context else ""
        )
        feedback_rules_injection = self._build_feedback_rules_injection(active_feedback_rules or [])

        use_economy_rag = bool(news_data_text) and self._is_economy_topic(topic_mode)
        if quality_only and use_economy_rag:
            user_prompt = QUALITY_LAYER_ECONOMY_PROMPT.format(
                title=job.title,
                keywords=", ".join(job.seed_keywords),
                category=topic_mode,
                news_data=news_data_text,
            )
            system_prompt = (
                f"{QUALITY_LAYER_SYSTEM_PROMPT}\n\n{ECONOMY_SYSTEM_PROMPT}"
                f"{cognitive_injection}{emotional_injection}{pre_analysis_injection}"
                f"{memory_injection}"
            )
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
            system_prompt = (
                QUALITY_LAYER_SYSTEM_PROMPT
                + cognitive_injection
                + emotional_injection
                + pre_analysis_injection
                + memory_injection
            )
        elif use_economy_rag:
            user_prompt = ECONOMY_TOPIC_PROMPT.format(
                title=job.title,
                keywords=", ".join(job.seed_keywords),
                category=topic_mode,
                news_data=news_data_text,
                persona_prefix=persona.prompt_prefix,
                tone_suffix=tone_profile.prompt_suffix,
            )
            system_prompt = f"{SYSTEM_BLOG_WRITER}{voice_injection}\n\n{ECONOMY_SYSTEM_PROMPT}{memory_injection}"
        else:
            user_prompt = USER_CONTENT_REQUEST.format(
                title=job.title,
                keywords=", ".join(job.seed_keywords),
                category=topic_mode,
                persona_prefix=persona.prompt_prefix,
                tone_suffix=tone_profile.prompt_suffix,
            )
            system_prompt = f"{SYSTEM_BLOG_WRITER}{voice_injection}{memory_injection}"

        if feedback_rules_injection:
            user_prompt = f"{user_prompt}{feedback_rules_injection}"

        market_slot_injection = self._build_market_slot_writing_injection(job)
        if market_slot_injection:
            user_prompt = f"{user_prompt}\n\n{market_slot_injection}"

        if insight_strategy_prompt:
            user_prompt = f"{user_prompt}\n\n{insight_strategy_prompt}"

        category_template_injection = self._build_category_template_writing_injection(job, topic_mode)
        if category_template_injection:
            user_prompt = f"{user_prompt}\n\n{category_template_injection}"

        # AI 패턴 방지 규칙 주입 (초안 생성 단계)
        user_prompt = f"{user_prompt}\n\n{ANTI_AI_PATTERN_BRIEF}"

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
        active_feedback_rules: Optional[List[str]] = None,
    ) -> str:
        """SEO 최적화 적용."""
        user_prompt = (
            SEO_OPTIMIZATION.format(keywords=", ".join(keywords))
            + "\n\n[원본 콘텐츠]\n"
            + content
        )
        # 이미지 스코프 규칙은 텍스트(SEO) 단계와 무관하므로 필터링
        text_rules = self._filter_rules_for_text_stage(active_feedback_rules or [])
        feedback_injection = self._build_feedback_rules_injection(text_rules)
        if feedback_injection:
            user_prompt = f"{user_prompt}{feedback_injection}"
        try:
            response = await self._generate_with_usage(
                client=client,
                role="quality_step",
                token_usage=token_usage,
                system_prompt=QUALITY_LAYER_SYSTEM_PROMPT,
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
                system_prompt=QUALITY_LAYER_SYSTEM_PROMPT,
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
        backup_client: Optional[BaseLLMClient],
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
            # 1차 품질 평가 모델이 실패하면 백업 모델로 즉시 재평가한다.
            if backup_client is not None and backup_client.provider_name != client.provider_name:
                backup_prompt = (
                    f"{QUALITY_CHECK_SIMPLE}\n\n[제목]\n{job.title}\n\n[콘텐츠]\n{content}"
                )
                try:
                    backup_response = await self._generate_with_usage(
                        client=backup_client,
                        role="quality_step",
                        token_usage=token_usage,
                        system_prompt="당신은 엄격한 콘텐츠 편집자입니다.",
                        user_prompt=backup_prompt,
                        temperature=0.0,
                        max_tokens=700,
                    )
                    backup_raw = backup_response.content
                    backup_parsed = self._parse_json_response(backup_raw)
                    backup_score = self._extract_quality_score(backup_parsed)
                    if backup_score is None:
                        backup_score = self._extract_quality_score_from_text(backup_raw)

                    if backup_score is not None:
                        if backup_score >= pass_score_threshold:
                            gate = "pass"
                        elif backup_score >= self.QUALITY_RETRY_MASK_FLOOR:
                            gate = "retry_mask"
                        else:
                            gate = "retry_all"
                        return QualityResult(
                            score=backup_score,
                            gate=gate,
                            breakdown=backup_parsed.get("breakdown", {}),
                            issues=self._normalize_string_list(backup_parsed.get("issues", [])),
                            improvements=self._normalize_string_list(backup_parsed.get("improvements", [])),
                            summary=str(backup_parsed.get("summary", "")).strip()
                            or self._extract_quality_summary(backup_raw),
                        )
                except Exception as backup_exc:
                    logger.warning("Quality check backup on exception failed: %s", backup_exc)

            return QualityResult(
                score=60,
                gate="retry_mask",
                issues=["quality_check_error"],
                summary=str(exc),
            )

        original_quality_raw = response.content
        quality_raw = response.content
        parsed = self._parse_json_response(response.content)
        score = self._extract_quality_score(parsed)
        score_source = "json" if score is not None else ""

        # 모델이 JSON 형식을 지키지 않는 경우, 단순 포맷 프롬프트로 1회 재시도한다.
        if score is None:
            fallback_prompt = (
                f"{QUALITY_CHECK_SIMPLE}\n\n[제목]\n{job.title}\n\n[콘텐츠]\n{content}"
            )
            try:
                fallback_response = await self._generate_with_usage(
                    client=client,
                    role="quality_step",
                    token_usage=token_usage,
                    system_prompt="당신은 엄격한 콘텐츠 편집자입니다.",
                    user_prompt=fallback_prompt,
                    temperature=0.0,
                    max_tokens=700,
                )
                quality_raw = fallback_response.content
                parsed = self._parse_json_response(fallback_response.content)
                score = self._extract_quality_score(parsed)
                if score is not None:
                    score_source = "json_fallback"
            except Exception as exc:
                logger.warning("Quality check fallback failed: %s", exc)

        # JSON 파싱이 끝까지 실패하면 최후 수단으로 텍스트에서 점수를 추출한다.
        if score is None:
            score = self._extract_quality_score_from_text(quality_raw)
            if score is None:
                score = self._extract_quality_score_from_text(original_quality_raw)
                quality_raw = original_quality_raw
            if score is not None:
                score_source = "text_fallback"

        # 점수를 끝내 얻지 못했으면 본문 구조 기반 휴리스틱 점수를 계산한다.
        if score is None:
            score = self._estimate_quality_score_fallback(
                content=content,
                keywords=job.seed_keywords,
                parsed=parsed,
                raw_text=quality_raw,
            )
            if score is not None:
                score_source = "heuristic"

        if score is None:
            logger.warning("Quality JSON parse failed, using safe fallback score")
            return QualityResult(
                score=60,
                gate="retry_mask",
                issues=["quality_check_parse_error"],
                summary="quality_check_parse_error",
            )

        # 1차 평가가 저신뢰 결과면, 주 생성 모델로 1회 재검증해 점수 신뢰도를 높인다.
        if (
            backup_client is not None
            and backup_client.provider_name != client.provider_name
            and (score_source in {"", "text_fallback"})
        ):
            backup_prompt = (
                f"{QUALITY_CHECK_SIMPLE}\n\n[제목]\n{job.title}\n\n[콘텐츠]\n{content}"
            )
            try:
                backup_response = await self._generate_with_usage(
                    client=backup_client,
                    role="quality_step",
                    token_usage=token_usage,
                    system_prompt="당신은 엄격한 콘텐츠 편집자입니다.",
                    user_prompt=backup_prompt,
                    temperature=0.0,
                    max_tokens=700,
                )
                backup_raw = backup_response.content
                backup_parsed = self._parse_json_response(backup_raw)
                backup_score = self._extract_quality_score(backup_parsed)
                if backup_score is None:
                    backup_score = self._extract_quality_score_from_text(backup_raw)
                if backup_score is not None:
                    score = backup_score
                    parsed = backup_parsed if backup_parsed else parsed
                    quality_raw = backup_raw
                    score_source = "backup"
                    logger.info(
                        "Quality score revalidated with backup provider: %s -> %s (%d)",
                        client.provider_name,
                        backup_client.provider_name,
                        score,
                    )
            except Exception as exc:
                logger.warning("Quality backup recheck failed: %s", exc)

        # 비JSON 텍스트 점수는 신뢰도가 낮으므로 과도한 retry_all은 피한다.
        has_breakdown = isinstance(parsed.get("breakdown"), dict) and bool(parsed.get("breakdown"))
        has_issues = bool(self._normalize_string_list(parsed.get("issues", [])))
        if (
            score_source == "text_fallback"
            and score < self.QUALITY_RETRY_MASK_FLOOR
            and not has_breakdown
            and not has_issues
        ):
            logger.warning(
                "Low-confidence quality text score=%d adjusted to floor=%d",
                score,
                self.QUALITY_RETRY_MASK_FLOOR,
            )
            score = self.QUALITY_RETRY_MASK_FLOOR

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
            issues=self._normalize_string_list(parsed.get("issues", [])),
            improvements=self._normalize_string_list(parsed.get("improvements", [])),
            summary=str(parsed.get("summary", "")).strip() or self._extract_quality_summary(quality_raw),
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
                system_prompt=QUALITY_LAYER_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            return response.content.strip() or content
        except Exception as exc:
            logger.warning("Rewrite failed: %s", exc)
            return content

    def _strip_news_source_block(self, content: str) -> str:
        """후처리 출처 블록을 재작성 대상에서 제외한다."""

        return re.sub(r"\n+참고 자료:\s*[\s\S]*$", "", str(content or "").rstrip()).rstrip()

    def _is_insight_rewrite_candidate(self, content: str) -> bool:
        """통찰 리라이트를 시도할 만큼 원고 본문이 충분한지 확인한다."""

        body = self._strip_news_source_block(content)
        return len(body.strip()) >= 500

    async def _rewrite_for_insight_quality(
        self,
        *,
        content: str,
        insight_quality: Any,
        client: BaseLLMClient,
        news_context: Optional[List[Dict[str, str]]] = None,
        token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> str:
        """통찰 품질 평가가 낮을 때 쉬운 1인칭 학습 문체로 한 번 더 보정한다."""

        original_body = self._strip_news_source_block(content)
        if not original_body.strip():
            return content

        issues = ", ".join(getattr(insight_quality, "issues", []) or [])
        score = getattr(insight_quality, "overall_score", 0)
        user_prompt = f"""
다음 블로그 원고는 통찰 품질 평가에서 보강이 필요하다는 판정을 받았습니다.

현재 점수: {score}/100
문제점: {issues}

아래 원고를 다시 다듬어 주세요.

반드시 지킬 것:
- H2 소제목, 표, 리스트 구조는 유지하세요.
- 원문에 없는 인물, 기관, 정책, 날짜, 수치, 상관관계, 인과관계를 새로 추가하지 마세요.
- 이미 있는 숫자와 고유명사는 삭제하지 마세요.
- 어려운 문장은 두 문장으로 나누고, 용어는 쉬운 말로 바로 풀어 주세요.
- "저도 같이 확인해보겠습니다", "제가 오늘 남겨둘 기준은"처럼 함께 공부하는 1인칭 기록체로 낮춰 주세요.
- "중요합니다", "도움이 됩니다", "전략을 조정할 수 있습니다" 같은 일반론 반복을 줄이세요.
- 경제/투자 글에 카페 운영 경험을 억지로 넣지 마세요.
- 출처/참고 자료 섹션은 만들지 마세요. 출처는 후처리에서 붙습니다.

[원고]
{original_body}

[출력]
수정된 Markdown 본문만 출력하세요.
""".strip()

        try:
            response = await self._generate_with_usage(
                client=client,
                role="quality_step",
                token_usage=token_usage,
                system_prompt=QUALITY_LAYER_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                temperature=min(0.45, self.temperature),
                max_tokens=self.max_tokens,
            )
        except Exception as exc:
            logger.warning("Insight rewrite failed: %s", exc)
            return content

        candidate = response.content.strip()
        if not candidate:
            return content

        candidate = self._sanitize_meta_headings(candidate)
        candidate = self._normalize_heading_levels(candidate)
        candidate = self._normalize_markdown_spacing(candidate)
        candidate = self._repair_markdown_tables(candidate)
        candidate = self._sanitize_language_artifacts(candidate)
        candidate = self._sanitize_generic_blog_phrases(candidate)
        if news_context:
            candidate = self._sanitize_market_sensitive_claims(candidate, news_context)
        safe, reason = self._is_voice_rewrite_safe(original_body, candidate)
        if not safe:
            logger.warning("Insight rewrite semantic drift detected, fallback raw (reason=%s)", reason)
            return content

        if news_context:
            return self._append_news_sources(candidate, news_context)
        return candidate

    async def _generate_image_prompts(
        self,
        content: str,
        title: str,
        keywords: List[str],
        client: BaseLLMClient,
        token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Tuple[List[str], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """이미지 프롬프트 생성 (블로그 최적화)."""
        # 섹션 제목 + 첫 문장 추출 (이미지 배치 판단 정확도 개선)
        def _extract_section_summaries(text: str, max_sections: int = 5) -> str:
            blocks = re.split(r"(?=^##\s)", text, flags=re.MULTILINE)
            summaries = []
            for block in blocks:
                m = re.match(r"^##\s+(.+)$", block.strip(), re.MULTILINE)
                if not m:
                    continue
                heading = m.group(1).strip()
                lines = block.strip().split("\n")[1:]
                first = next(
                    (ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")),
                    ""
                )
                summaries.append(f"{heading}: {first[:80]}" if first else heading)
                if len(summaries) >= max_sections:
                    break
            return "\n".join(summaries) if summaries else ""

        sections_text = _extract_section_summaries(content)

        user_prompt = IMAGE_PROMPT_GENERATION.format(
            title=title,
            keywords=", ".join(keywords),
            sections=sections_text,
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
                    pexels_query = str(raw_slot.get("pexels_query", "")).strip()

                    slot_payload = {
                        "slot_id": slot_id,
                        "slot_role": slot_role,
                        "prompt": prompt,
                        "concept": concept,
                        "pexels_query": pexels_query,
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
        if not raw:
            return {}

        candidates: List[str] = []

        # 1) 코드블록 후보 수집 (```json``` + 일반 ``` ``` 모두 허용)
        for match in re.finditer(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL | re.IGNORECASE):
            block = str(match.group(1) or "").strip()
            if block:
                candidates.append(block)

        # 2) 전체 텍스트 자체도 후보에 포함
        candidates.append(raw)

        # 3) 텍스트에서 균형 잡힌 JSON 객체 후보를 추출
        candidates.extend(self._extract_json_object_candidates(raw))

        # 4) 후보를 순차적으로 파싱
        for candidate in candidates:
            parsed = self._parse_json_candidate(candidate)
            if parsed:
                return parsed

        return {}

    def _parse_json_candidate(self, candidate: str) -> Dict[str, Any]:
        """JSON 후보 문자열을 dict로 파싱한다."""
        target = str(candidate or "").strip()
        if not target:
            return {}

        try:
            parsed = json.loads(target)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # 코드블록 라벨/후행 텍스트가 섞인 경우를 대비해 객체만 한 번 더 추출한다.
        for obj in self._extract_json_object_candidates(target):
            try:
                parsed = json.loads(obj)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue

        return {}

    def _extract_json_object_candidates(self, text: str) -> List[str]:
        """문자열에서 균형 잡힌 JSON 객체 문자열 후보를 추출한다."""
        source = str(text or "")
        if "{" not in source:
            return []

        candidates: List[str] = []
        depth = 0
        start_idx = -1
        in_string = False
        escape = False

        for index, char in enumerate(source):
            if in_string:
                if escape:
                    escape = False
                    continue
                if char == "\\":
                    escape = True
                    continue
                if char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
                continue

            if char == "{":
                if depth == 0:
                    start_idx = index
                depth += 1
                continue

            if char == "}":
                if depth <= 0:
                    continue
                depth -= 1
                if depth == 0 and start_idx >= 0:
                    candidate = source[start_idx : index + 1].strip()
                    if candidate:
                        candidates.append(candidate)
                    start_idx = -1

        return candidates

    def _extract_quality_score(self, parsed: Dict[str, Any]) -> Optional[int]:
        """품질 응답에서 점수를 안전하게 추출한다."""
        if not isinstance(parsed, dict):
            return None

        score_raw: Any = parsed.get("score")
        if score_raw is None:
            score_raw = parsed.get("overall_score")

        score_value: Optional[int] = None
        if isinstance(score_raw, (int, float)):
            score_value = int(round(float(score_raw)))
        elif isinstance(score_raw, str):
            match = re.search(r"-?\d+", score_raw)
            if match:
                score_value = int(match.group(0))

        # score 키가 비어 있으면 breakdown 합계로 보정한다.
        if score_value is None:
            breakdown = parsed.get("breakdown", {})
            if isinstance(breakdown, dict):
                subtotal = 0
                for key in ("information_quality", "structure", "writing_quality", "seo"):
                    value = breakdown.get(key)
                    if isinstance(value, (int, float)):
                        subtotal += int(round(float(value)))
                    elif isinstance(value, str):
                        match = re.search(r"-?\d+", value)
                        if match:
                            subtotal += int(match.group(0))
                if subtotal > 0:
                    score_value = subtotal

        if score_value is None:
            return None

        return max(0, min(100, int(score_value)))

    def _normalize_string_list(self, value: Any) -> List[str]:
        """문자열 리스트 필드를 안전하게 정규화한다."""
        if isinstance(value, list):
            normalized: List[str] = []
            for item in value:
                text = str(item).strip()
                if text:
                    normalized.append(text)
            return normalized
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        return []

    def _extract_quality_score_from_text(self, raw: str) -> Optional[int]:
        """비JSON 품질 응답에서 점수를 추출한다."""
        text = str(raw or "").strip()
        if not text:
            return None

        patterns = (
            r"(?:score|점수)\s*[:=]?\s*(\d{1,3})",
            r"(\d{1,3})\s*/\s*100",
            r"총점\s*(\d{1,3})",
            r"(\d{1,3})\s*점",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                value = int(match.group(1))
                return max(0, min(100, value))
        return None

    def _extract_quality_summary(self, raw: str) -> str:
        """비JSON 응답에서 짧은 요약 문장을 추출한다."""
        text = str(raw or "").strip()
        if not text:
            return ""
        text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).replace("```", "")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        return lines[0][:180]

    def _select_quality_client(self, *, quality_stage_client: Optional[BaseLLMClient] = None) -> BaseLLMClient:
        """품질 체크/재작성에 사용할 클라이언트를 선택한다."""
        if self._is_cost_strict_active() and self.cost_lock_quality_provider and quality_stage_client is not None:
            return quality_stage_client
        secondary_name = str(getattr(self.secondary, "provider_name", "")).strip().lower()
        primary_name = str(getattr(self.primary, "provider_name", "")).strip().lower()
        if secondary_name == "cerebras" and primary_name and primary_name != secondary_name:
            return self.primary
        return self.secondary

    def _sanitize_meta_headings(self, content: str) -> str:
        """리라이트 과정에서 유입된 메타 제목을 제거한다."""
        lines = str(content or "").splitlines()
        if not lines:
            return content

        banned_tokens = ("개선된 콘텐츠", "수정본", "리라이트본")
        first_index = -1
        for idx, line in enumerate(lines):
            if line.strip():
                first_index = idx
                break
        if first_index < 0:
            return content

        first_line = lines[first_index].strip()
        if not first_line.startswith("#"):
            return content
        if not any(token in first_line for token in banned_tokens):
            return content

        del lines[first_index]
        # 제목 바로 아래 빈 줄은 함께 정리해 본문 시작을 자연스럽게 맞춘다.
        if first_index < len(lines) and not lines[first_index].strip():
            del lines[first_index]
        return "\n".join(lines).strip()

    def _normalize_heading_levels(self, content: str) -> str:
        """모델이 밀어낸 제목 계층을 네이버 본문용으로 정규화한다."""
        text = str(content or "")
        if not text:
            return text

        lines = text.splitlines()
        first_heading_index = next((idx for idx, line in enumerate(lines) if line.strip().startswith("#")), -1)
        has_h3_or_lower = any(re.match(r"^\s*#{3,6}\s+", line) for line in lines)
        if first_heading_index >= 0 and lines[first_heading_index].lstrip().startswith("## ") and has_h3_or_lower:
            lines[first_heading_index] = re.sub(r"^\s*##\s+", "# ", lines[first_heading_index], count=1)
            text = "\n".join(lines)
            return re.sub(r"(?m)^\s*#{3,6}\s+", "## ", text).strip()

        if re.search(r"(?m)^\s*##\s+", text):
            return text
        if not re.search(r"(?m)^\s*#{3,6}\s+", text):
            return text

        return re.sub(r"(?m)^\s*#{3,6}\s+", "## ", text).strip()

    def _normalize_markdown_spacing(self, content: str) -> str:
        """제목/문단 사이 공백을 정리해 모바일 가독성을 높인다."""

        text = str(content or "").strip()
        if not text:
            return text
        text = re.sub(r"(?m)^(#{1,6}\s+.+)\n(?!\n)", r"\1\n\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _repair_markdown_tables(self, content: str) -> str:
        """열 개수가 깨진 마크다운 표 행을 제거한다."""

        lines = str(content or "").splitlines()
        repaired: List[str] = []
        expected_columns: Optional[int] = None

        for line in lines:
            stripped = line.strip()
            is_table_line = stripped.startswith("|") and stripped.endswith("|")
            if not is_table_line:
                expected_columns = None
                repaired.append(line)
                continue

            columns = [cell.strip() for cell in stripped.strip("|").split("|")]
            is_separator = bool(columns) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in columns)
            if expected_columns is None:
                expected_columns = len(columns)
                repaired.append(line)
                continue
            if is_separator:
                expected_columns = len(columns)
                repaired.append(line)
                continue
            if len(columns) != expected_columns:
                logger.warning(
                    "Broken markdown table row removed (expected=%d, actual=%d): %s",
                    expected_columns,
                    len(columns),
                    stripped[:120],
                )
                continue
            repaired.append(line)

        return "\n".join(repaired).strip()

    def _sanitize_language_artifacts(self, content: str) -> str:
        """모델 출력에 섞일 수 있는 외국어/치환 아티팩트를 최소 보정한다."""

        text = str(content or "")
        replacements = {
            "昨日": "어젯밤",
            "最新": "최신",
            "同時": "동시에",
            "市場": "시장",
            " गलत": " 잘못",
            "गलत": "잘못",
            "ボ": "보",
            "interessring": "흥미롭게",
            "interesting": "흥미롭게",
        }
        for source, target in replacements.items():
            text = text.replace(source, target)
        # 한국어 블로그 본문에 섞인 외국 문자 잔재는 신뢰도를 크게 떨어뜨린다.
        text = re.sub(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]+", "", text)
        text = re.sub(r"[\u0900-\u097f]+", "", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        # 이전 키워드 변주 로직이 만든 어색한 흔적은 최종 산출물에 남기지 않는다.
        text = re.sub(r"\b이 주제이\b", "이 주제가", text)
        # 프롬프트 꼬리가 본문에 섞인 경우 제거한다.
        text = re.sub(r"(?m)^\s*\[(?:출력|원고|본문|응답)\]\s*$", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text

    def _sanitize_generic_blog_phrases(self, content: str) -> str:
        """블로그 품질을 떨어뜨리는 강의식 일반론을 기록형 문장으로 낮춘다."""

        text = str(content or "")
        replacements = {
            "분석하여 투자에 참고할 수 있는 정보를 제공하겠습니다.": "같이 확인한 뒤, 제가 남길 기준을 정리해보겠습니다.",
            "분석하여 투자에 참고할 수 있는 정보를 제공하겠습니다": "같이 확인한 뒤, 제가 남길 기준을 정리해보겠습니다",
            "투자에 참고할 수 있는 정보를 제공하겠습니다.": "저도 오늘은 판단 기준만 남겨보겠습니다.",
            "투자에 참고할 수 있는 정보를 제공하겠습니다": "저도 오늘은 판단 기준만 남겨보겠습니다",
            "더 나은 투자 판단을 내릴 수 있습니다.": "다음 판단이 조금 덜 흔들릴 수 있다고 봅니다.",
            "성공적인 투자를 할 수 있습니다.": "다음 선택을 조금 더 차분히 볼 수 있다고 생각합니다.",
            "성공적인 투자를 할 수 있습니다": "다음 선택을 조금 더 차분히 볼 수 있다고 생각합니다",
        }
        for source, target in replacements.items():
            text = text.replace(source, target)

        text = re.sub(
            r"([^\n.]+?)하는 것이 중요합니다\.",
            r"\1하는 쪽으로 기준을 남겨보겠습니다.",
            text,
        )
        text = re.sub(
            r"(?<!매우 )중요합니다\.",
            "기준으로 남겨보겠습니다.",
            text,
        )
        text = re.sub(
            r"(?<!큰 )도움이 됩니다\.",
            "다음에 다시 확인하기 좋습니다.",
            text,
        )
        text = text.replace(
            "투자 공부는 **마라톤이 아니라 등산**입니다.",
            "투자 공부는 멀리 맞히는 일이 아니라, 오늘의 기준을 하나씩 확인하는 일에 가깝습니다.",
        )
        text = text.replace(
            "투자 공부는 **마라톤이 아니라 등산**입니다",
            "투자 공부는 멀리 맞히는 일이 아니라, 오늘의 기준을 하나씩 확인하는 일에 가깝습니다",
        )
        return text

    def _sanitize_market_sensitive_claims(
        self,
        content: str,
        news_context: List[Dict[str, str]],
    ) -> str:
        """시장 브리핑에서 공식 확인이 필요한 인물/정책 단정을 보수적으로 낮춘다."""

        del news_context  # 현재는 보수적 차단 우선. 출처 검증 확장 시 사용한다.
        lines = str(content or "").splitlines()
        if not lines:
            return content

        sanitized: List[str] = []
        inserted_guard = False
        skip_following_pronoun_line = False
        for line in lines:
            stripped = line.strip()
            sensitive_person_line = bool(
                re.search(r"(Kevin Warsh|케빈\s*워시|워시)", stripped)
                and re.search(r"(연준|Fed|의장|취임|임명|금리|정책)", stripped, flags=re.IGNORECASE)
            )
            sensitive_fed_chair_line = bool(
                "연준 의장" in stripped and re.search(r"(취임|임명|선임)", stripped)
            )
            if sensitive_person_line or sensitive_fed_chair_line:
                if not inserted_guard:
                    sanitized.append(
                        "연준 인사나 정책 관련 뉴스는 제목만으로 단정하지 않고, 공식 발표 원문을 확인한 뒤 기록하겠습니다."
                    )
                    inserted_guard = True
                skip_following_pronoun_line = True
                continue

            if skip_following_pronoun_line and stripped.startswith(("그는", "그가", "해당 인물")):
                continue
            skip_following_pronoun_line = False
            sanitized.append(line)

        return "\n".join(sanitized).strip()

    def _should_limit_keyword_repetition(self, *, job: Job, topic_mode: str) -> bool:
        """시장/금융 글에서는 핵심 용어 보존을 우선한다."""

        if self._resolve_market_slot(job) is not None:
            return False
        if normalize_topic_mode(topic_mode) == "finance":
            return False
        return True

    def _build_keyword_variants(self, keyword: str) -> List[str]:
        """키워드 구문의 과반복을 완화할 대체 표현을 생성한다."""
        base = str(keyword or "").strip()
        if not base:
            return []

        compact = re.sub(r"\s+", "", base)
        protected_keywords = {
            "국장",
            "미장",
            "증시",
            "환율",
            "금리",
            "반도체",
            "비트코인",
            "이더리움",
            "리스크",
            "투자",
            "주식",
        }
        if len(compact) <= 4 or compact in protected_keywords:
            return []

        variants: List[str] = []
        parts = [part for part in base.split() if part]

        # 키워드 의미를 유지하면서 자연스럽게 변주한다.
        if len(parts) >= 2:
            variants.append(" ".join(parts[1:]))     # 앞 단어 제거
            variants.append(" ".join(parts[:-1]))    # 끝 단어 제거
            if len(parts[-1]) >= 3:
                variants.append(f"{parts[-1]} 흐름")

        deduped: List[str] = []
        for item in variants:
            normalized = str(item or "").strip()
            if not normalized:
                continue
            if normalized.lower() == base.lower():
                continue
            if normalized in deduped:
                continue
            deduped.append(normalized)
        return deduped

    def _limit_exact_keyword_repetition(
        self,
        *,
        content: str,
        keywords: List[str],
        max_exact_matches: int = 2,
    ) -> str:
        """동일 키워드 구문이 과도하게 반복되면 후반부를 변주 표현으로 치환한다."""
        text = str(content or "")
        if not text:
            return text
        if max_exact_matches < 1:
            max_exact_matches = 1

        updated = text
        for raw_keyword in keywords:
            keyword = str(raw_keyword or "").strip()
            if not keyword:
                continue

            pattern = re.compile(re.escape(keyword), flags=re.IGNORECASE)
            total_matches = len(list(pattern.finditer(updated)))
            if total_matches <= max_exact_matches:
                continue

            variants = self._build_keyword_variants(keyword)
            if not variants:
                continue

            seen = 0
            replace_index = 0

            def _replace(match: re.Match[str]) -> str:
                nonlocal seen, replace_index
                seen += 1
                if seen <= max_exact_matches:
                    return match.group(0)
                replacement = variants[replace_index % len(variants)]
                replace_index += 1
                return replacement

            updated = pattern.sub(_replace, updated)
            reduced = len(list(pattern.finditer(updated)))
            logger.info(
                "Keyword repetition reduced: '%s' %d -> %d",
                keyword,
                total_matches,
                reduced,
            )

        return updated

    def _estimate_quality_score_fallback(
        self,
        *,
        content: str,
        keywords: List[str],
        parsed: Dict[str, Any],
        raw_text: str,
    ) -> Optional[int]:
        """점수 누락 시 본문 구조/반복도 기반으로 보수적 점수를 계산한다."""
        text = str(content or "").strip()
        if not text:
            return None

        score = 78

        length = len(text)
        if length >= 1400:
            score += 6
        elif length >= 1000:
            score += 3
        elif length < 800:
            score -= 8

        h2_count = len(self._extract_h2_headings(text))
        if 3 <= h2_count <= 6:
            score += 5
        elif h2_count == 0:
            score -= 12
        else:
            score -= 4

        if re.search(r"(?m)^\s*[-*]\s+", text):
            score += 2
        if "| ---" in text and "|" in text:
            score += 2

        for raw_keyword in keywords:
            keyword = str(raw_keyword or "").strip()
            if not keyword:
                continue
            count = text.lower().count(keyword.lower())
            if count > 2:
                score -= min(10, (count - 2) * 2)

        issues = self._normalize_string_list(parsed.get("issues", []))
        if issues:
            score -= min(20, len(issues) * 4)

        summary = str(parsed.get("summary", "")).strip() or str(raw_text or "").strip()
        negative_tokens = ("부족", "개선 필요", "미흡", "어색", "반복", "불명확")
        positive_tokens = ("양호", "좋음", "충분", "명확")
        if any(token in summary for token in negative_tokens):
            score -= 4
        elif any(token in summary for token in positive_tokens):
            score += 2

        # 휴리스틱 점수는 과신하지 않도록 범위를 제한한다.
        return max(55, min(88, int(score)))

    def _compute_keyword_count(self, *, content: str, keywords: List[str]) -> int:
        """SEO 스냅샷용 키워드 카운트를 계산한다.

        규칙:
        - 기본은 정확 구문 일치 횟수 합계
        - 정확 일치가 0이어도 키워드 의미가 포함되면 최소 1을 부여
        """
        text = str(content or "")
        if not text or not keywords:
            return 0

        exact_count = 0
        lowered = text.lower()
        for raw_keyword in keywords:
            keyword = str(raw_keyword or "").strip().lower()
            if not keyword:
                continue
            exact_count += lowered.count(keyword)

        if exact_count > 0:
            return exact_count

        semantic_hit = any(
            self._has_keyword_semantic_presence(content=text, keyword=str(item or ""))
            for item in keywords
        )
        return 1 if semantic_hit else 0

    def _has_keyword_semantic_presence(self, *, content: str, keyword: str) -> bool:
        """정확 구문이 아니어도 키워드 의미가 본문에 포함됐는지 판별한다."""
        base_keyword = str(keyword or "").strip()
        if not base_keyword:
            return False

        lowered_content = str(content or "").lower()
        lowered_keyword = base_keyword.lower()

        # 공백 차이만 있는 경우(예: 생활습관 vs 생활 습관)를 의미 포함으로 인정한다.
        content_no_space = re.sub(r"\s+", "", lowered_content)
        keyword_no_space = re.sub(r"\s+", "", lowered_keyword)
        if keyword_no_space and keyword_no_space in content_no_space:
            return True

        parts = [part.strip() for part in base_keyword.split() if part.strip()]
        if not parts:
            return False

        token_hits = sum(1 for part in parts if part.lower() in lowered_content)
        if len(parts) == 1:
            return token_hits >= 1
        if len(parts) == 2:
            return token_hits >= 2
        return token_hits >= 2

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

    def _client_display_label(self, client: BaseLLMClient) -> str:
        """로그에 표시할 provider/model 라벨을 만든다."""

        provider = str(getattr(client, "provider_name", "") or "").strip()
        label = self._provider_label(provider)
        model = str(getattr(client, "model", "") or "").strip()
        if model:
            return f"{label}({model})"
        return label
