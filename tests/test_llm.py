import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from modules.automation.job_store import Job, JobConfig, JobStore
from modules.automation.pipeline_service import PipelineService
from modules.llm import llm_generate_fn, reset_generator
from modules.llm.claude_client import ClaudeClient, LLMResponse
from modules.llm.content_generator import ContentGenerator, ContentResult
from modules.llm.token_budget_calibrator import calibrate_token_budget
from modules.llm.prompts import QUALITY_CHECK, SEO_OPTIMIZATION, USER_CONTENT_REQUEST
from modules.uploaders.playwright_publisher import PublishResult


def build_job(job_id: str = "llm-job-1") -> Job:
    """LLM 테스트용 Job 객체를 생성한다."""
    return Job(
        job_id=job_id,
        status="running",
        title="자동화 블로그 작성 가이드",
        seed_keywords=["블로그 자동화", "콘텐츠 생성", "SEO"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-02-19T00:00:00Z",
    )


class FakeClaudeClient:
    """ContentGenerator 테스트용 LLM 클라이언트."""

    def __init__(self, outputs: List[str]):
        self.outputs = outputs
        self.calls: List[Dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return "claude"

    async def generate_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        del max_retries, max_tokens
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
            }
        )
        return LLMResponse(
            content=self.outputs.pop(0),
            input_tokens=100,
            output_tokens=200,
            model="fake-model",
            stop_reason="end_turn",
        )


class FakeGenerator:
    """llm_generate_fn 테스트용 생성기."""

    async def generate(self, _job: Job) -> ContentResult:
        long_body = ("LLM 파이프라인 테스트 본문입니다. " * 70).strip()
        return ContentResult(
            final_content=f"# 테스트\n\n{long_body}",
            quality_gate="pass",
            quality_snapshot={"score": 90, "issues": []},
            seo_snapshot={"keywords": ["테스트"]},
            image_prompts=["테스트 썸네일"],
            llm_calls_used=2,
            provider_used="qwen",
            provider_model="qwen-plus",
            provider_fallback_from="",
        )


class DummyPublisher:
    """PipelineService 테스트용 더미 발행기."""

    async def publish(
        self,
        title: str,
        content: str,
        thumbnail: Optional[str] = None,
        images: Optional[List[str]] = None,
        image_sources: Optional[Dict[str, Dict[str, str]]] = None,
        image_points: Optional[List] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> PublishResult:
        del title, content, thumbnail, images, image_sources, image_points, tags, category
        return PublishResult(success=True, url="https://blog.naver.com/test/llm")


def test_claude_client_init_requires_api_key(monkeypatch: pytest.MonkeyPatch):
    """API 키가 없으면 초기화에 실패해야 한다."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ValueError):
        ClaudeClient(api_key=None)


def test_content_generator_returns_valid_structure():
    """생성 결과 구조와 주요 필드를 검증한다."""
    outputs = [
        '{"reader_current_knowledge":"기초 이해","reader_misconceptions":[],"reader_top_questions":[],"emotional_curve":{"opening_emotion":"호기심","turning_point":"전환","closing_emotion":"실행"},"recommended_structure":[]}',
        "# 자동화 블로그 작성 가이드\n\n## 시작하기\n\n초안 본문",
        "# 자동화 블로그 작성 가이드\n\n## SEO 시작하기\n\n최적화 본문",
        '{"score": 88, "issues": [], "summary": "양호"}',
        "# 자동화 블로그 작성 가이드\n\n## SEO 시작하기\n\n리라이트 본문",
        '{"thumbnail": {"prompt": "test thumbnail"}, "content_images": []}',  # 이미지 프롬프트 생성
    ]
    generator = ContentGenerator(client=FakeClaudeClient(outputs))
    result = asyncio.run(generator.generate(build_job()))

    assert result.final_content
    assert result.quality_gate == "pass"
    assert result.quality_snapshot["score"] == 88
    assert "keywords" in result.seo_snapshot
    assert result.llm_calls_used == 6  # pre-analysis + draft + SEO + quality + voice rewrite + image prompts
    assert result.provider_used in {"qwen", "deepseek", "claude"}
    assert len(result.image_prompts) >= 1
    assert result.voice_rewrite_applied is True
    assert result.llm_token_usage["pre_analysis"]["calls"] >= 1
    assert result.llm_token_usage["quality_step"]["calls"] >= 4
    assert result.llm_token_usage["voice_step"]["calls"] >= 1
    assert result.llm_token_usage["quality_step"]["input_tokens"] > 0


def test_content_generator_supports_image_slots_schema():
    """image_slots 응답 스키마를 파싱하고 최대 4개까지 유지해야 한다."""
    outputs = [
        '{"reader_current_knowledge":"기초 이해","reader_misconceptions":[],"reader_top_questions":[],"emotional_curve":{"opening_emotion":"호기심","turning_point":"전환","closing_emotion":"실행"},"recommended_structure":[]}',
        "# 자동화 블로그 작성 가이드\n\n## 시작하기\n\n초안 본문",
        "# 자동화 블로그 작성 가이드\n\n## SEO 시작하기\n\n최적화 본문",
        '{"score": 88, "issues": [], "summary": "양호"}',
        "# 자동화 블로그 작성 가이드\n\n## SEO 시작하기\n\n리라이트 본문",
        """
        {
          "image_slots": [
            {"slot_id":"thumb_0","slot_role":"thumbnail","prompt":"thumb prompt","preferred_type":"real","recommended":false,"ai_generation_score":30,"reason":"thumb"},
            {"slot_id":"content_1","slot_role":"content","prompt":"content prompt 1","preferred_type":"ai_generated","recommended":true,"ai_generation_score":90,"reason":"c1"},
            {"slot_id":"content_2","slot_role":"content","prompt":"content prompt 2","preferred_type":"real","recommended":false,"ai_generation_score":40,"reason":"c2"},
            {"slot_id":"content_3","slot_role":"content","prompt":"content prompt 3","preferred_type":"ai_generated","recommended":true,"ai_generation_score":88,"reason":"c3"},
            {"slot_id":"content_4","slot_role":"content","prompt":"content prompt 4","preferred_type":"real","recommended":false,"ai_generation_score":10,"reason":"c4"}
          ]
        }
        """.strip(),
    ]
    generator = ContentGenerator(client=FakeClaudeClient(outputs))
    result = asyncio.run(generator.generate(build_job("slots-schema-job")))

    assert len(result.image_slots) == 4
    assert len(result.image_prompts) == 4
    assert result.image_slots[0]["slot_role"] == "thumbnail"
    assert result.image_slots[1]["slot_role"] == "content"
    assert result.image_slots[1]["ai_generation_score"] == 90
    assert result.image_slots[1]["preferred_type"] == "ai_generated"


def test_voice_rewrite_falls_back_when_numeric_fact_changes():
    """Voice 리라이트가 숫자 사실을 바꾸면 원문으로 폴백해야 한다."""
    outputs = [
        '{"reader_current_knowledge":"기초 이해","reader_misconceptions":[],"reader_top_questions":[],"emotional_curve":{"opening_emotion":"호기심","turning_point":"전환","closing_emotion":"실행"},"recommended_structure":[]}',
        "# 테스트\n\n## 핵심 지표\n\n이번 달 전환율은 42% 입니다.\n",
        "# 테스트\n\n## 핵심 지표\n\n이번 달 전환율은 42% 입니다.\n",
        '{"score": 92, "issues": [], "summary": "좋음"}',
        "# 테스트\n\n## 핵심 지표\n\n이번 달 전환율은 55% 입니다.\n",
        '{"thumbnail": {"prompt": "test thumbnail"}, "content_images": []}',
    ]
    generator = ContentGenerator(client=FakeClaudeClient(outputs))
    result = asyncio.run(generator.generate(build_job("voice-guard-job")))

    assert "42%" in result.final_content
    assert "55%" not in result.final_content
    assert result.voice_rewrite_applied is False


def test_quality_threshold_main_slot_requires_80(tmp_path: Path):
    """메인 슬롯은 80점 기준으로 품질 통과 여부를 판단해야 한다."""
    outputs = [
        '{"reader_current_knowledge":"기초 이해","reader_misconceptions":[],"reader_top_questions":[],"emotional_curve":{"opening_emotion":"호기심","turning_point":"전환","closing_emotion":"실행"},"recommended_structure":[]}',
        "# 테스트\n\n## 본문\n\n메인 슬롯 초안",
        "# 테스트\n\n## 본문\n\n메인 슬롯 초안",
        '{"score": 75, "issues": ["depth"], "summary": "보완 필요"}',
        '{"thumbnail": {"prompt": "test thumbnail"}, "content_images": []}',
    ]
    generator = ContentGenerator(
        client=FakeClaudeClient(outputs),
        max_rewrites=0,
        enable_voice_rewrite=False,
        db_path=str(tmp_path / "main_threshold.db"),
    )
    result = asyncio.run(generator.generate(build_job("main-threshold-job")))

    assert result.quality_gate == "retry_mask"
    assert result.quality_snapshot["required_quality_score"] == 80
    assert result.quality_snapshot["quality_slot_type"] == "main"


def test_quality_threshold_test_slot_uses_fallback_category(tmp_path: Path):
    """fallback_category 슬롯은 70점 기준으로 품질 통과해야 한다."""
    db_path = tmp_path / "test_threshold.db"
    store = JobStore(str(db_path), config=JobConfig())
    store.set_system_setting("fallback_category", "다양한 생각")

    outputs = [
        '{"reader_current_knowledge":"기초 이해","reader_misconceptions":[],"reader_top_questions":[],"emotional_curve":{"opening_emotion":"호기심","turning_point":"전환","closing_emotion":"실행"},"recommended_structure":[]}',
        "# 테스트\n\n## 본문\n\n테스트 슬롯 초안",
        "# 테스트\n\n## 본문\n\n테스트 슬롯 초안",
        '{"score": 75, "issues": [], "summary": "통과"}',
        '{"thumbnail": {"prompt": "test thumbnail"}, "content_images": []}',
    ]
    generator = ContentGenerator(
        client=FakeClaudeClient(outputs),
        max_rewrites=0,
        enable_voice_rewrite=False,
        db_path=str(db_path),
    )
    job = build_job("test-threshold-job")
    job.category = "다양한 생각"

    result = asyncio.run(generator.generate(job))

    assert result.quality_gate == "pass"
    assert result.quality_snapshot["required_quality_score"] == 70
    assert result.quality_snapshot["quality_slot_type"] == "test"


def test_quality_check_retries_with_simple_prompt_when_parse_fails():
    """품질 JSON 파싱 실패 시 단순 프롬프트로 1회 재평가해야 한다."""
    outputs = [
        '{"reader_current_knowledge":"기초 이해","reader_misconceptions":[],"reader_top_questions":[],"emotional_curve":{"opening_emotion":"호기심","turning_point":"전환","closing_emotion":"실행"},"recommended_structure":[]}',
        "# 테스트\n\n## 본문\n\n초안 본문",
        "# 테스트\n\n## 본문\n\nSEO 반영 본문",
        "전체적으로 괜찮지만 보강 여지가 있습니다.",  # JSON/점수 모두 없음 → 단순 프롬프트 재시도 유도
        '{"score": 82, "issues": ["세부 사례 보강"], "summary": "양호"}',  # 단순 프롬프트 재시도 결과
        "# 테스트\n\n## 본문\n\nSEO 반영 본문",
        '{"thumbnail": {"prompt": "test thumbnail"}, "content_images": []}',
    ]
    generator = ContentGenerator(
        client=FakeClaudeClient(outputs),
        max_rewrites=0,
    )
    result = asyncio.run(generator.generate(build_job("quality-parse-fallback-job")))

    assert result.quality_gate == "pass"
    assert result.quality_snapshot["score"] == 82
    assert any("JSON 형식으로만 답변" in call["user_prompt"] for call in generator.primary.calls)


def test_quality_check_extracts_score_from_plain_text_response():
    """비JSON 품질 응답에서도 점수 텍스트를 추출해 게이트를 판단해야 한다."""
    outputs = [
        '{"reader_current_knowledge":"기초 이해","reader_misconceptions":[],"reader_top_questions":[],"emotional_curve":{"opening_emotion":"호기심","turning_point":"전환","closing_emotion":"실행"},"recommended_structure":[]}',
        "# 테스트\n\n## 본문\n\n초안 본문",
        "# 테스트\n\n## 본문\n\nSEO 반영 본문",
        "전반적으로 검토가 필요합니다.",  # 원본 응답: JSON/점수 없음
        "최종 점수: 84/100. 전반적으로 양호합니다.",  # 재평가 응답: 비JSON 점수 텍스트
        "# 테스트\n\n## 본문\n\nSEO 반영 본문",
        '{"thumbnail": {"prompt": "test thumbnail"}, "content_images": []}',
    ]
    generator = ContentGenerator(
        client=FakeClaudeClient(outputs),
        max_rewrites=0,
    )
    result = asyncio.run(generator.generate(build_job("quality-text-score-job")))

    assert result.quality_gate == "pass"
    assert result.quality_snapshot["score"] == 84
    assert any("JSON 형식으로만 답변" in call["user_prompt"] for call in generator.primary.calls)


def test_quality_check_prefers_simple_json_over_plain_text_score():
    """원본 응답에 점수 텍스트가 있어도 단순 JSON 재평가 결과를 우선해야 한다."""
    outputs = [
        '{"reader_current_knowledge":"기초 이해","reader_misconceptions":[],"reader_top_questions":[],"emotional_curve":{"opening_emotion":"호기심","turning_point":"전환","closing_emotion":"실행"},"recommended_structure":[]}',
        "# 테스트\n\n## 본문\n\n초안 본문",
        "# 테스트\n\n## 본문\n\nSEO 반영 본문",
        "점수는 53점입니다. 개선이 필요합니다.",  # 원본 텍스트 점수(낮음)
        '{"score": 82, "issues": ["구체성 보강"], "summary": "양호"}',  # 단순 재평가 점수(우선 반영)
        "# 테스트\n\n## 본문\n\nSEO 반영 본문",
        '{"thumbnail": {"prompt": "test thumbnail"}, "content_images": []}',
    ]
    generator = ContentGenerator(
        client=FakeClaudeClient(outputs),
        max_rewrites=0,
    )
    result = asyncio.run(generator.generate(build_job("quality-json-priority-job")))

    assert result.quality_gate == "pass"
    assert result.quality_snapshot["score"] == 82
    assert any("JSON 형식으로만 답변" in call["user_prompt"] for call in generator.primary.calls)


def test_quality_check_low_confidence_text_score_is_clamped_to_retry_mask_floor(tmp_path):
    """재평가도 비JSON일 때 추출 점수는 retry_all로 떨어지지 않게 floor로 보정해야 한다.

    실제 DB의 llm_retry_mask_floor 설정값에 영향받지 않도록 격리 DB를 사용한다.
    """
    outputs = [
        '{"reader_current_knowledge":"기초 이해","reader_misconceptions":[],"reader_top_questions":[],"emotional_curve":{"opening_emotion":"호기심","turning_point":"전환","closing_emotion":"실행"},"recommended_structure":[]}',
        "# 테스트\n\n## 본문\n\n초안 본문",
        "# 테스트\n\n## 본문\n\nSEO 반영 본문",
        "전체적으로 검토가 필요합니다.",  # 원본 응답: JSON/점수 없음
        "점수는 53점입니다. 전반적으로 개선 필요.",  # 재평가 응답: 점수 텍스트만 존재
        "# 테스트\n\n## 본문\n\nSEO 반영 본문",
        '{"thumbnail": {"prompt": "test thumbnail"}, "content_images": []}',
    ]
    # 격리 DB: system_settings에 floor 설정 없음 → 클래스 기본값(60) 사용
    generator = ContentGenerator(
        client=FakeClaudeClient(outputs),
        max_rewrites=0,
        db_path=str(tmp_path / "isolated.db"),
    )
    result = asyncio.run(generator.generate(build_job("quality-text-floor-job")))

    assert result.quality_gate == "retry_mask"
    assert result.quality_snapshot["score"] == ContentGenerator.QUALITY_RETRY_MASK_FLOOR


def test_limit_exact_keyword_repetition_reduces_overuse():
    """정확 구문 키워드가 과반복되면 후반부를 변주 표현으로 바꿔야 한다."""
    generator = ContentGenerator(
        client=FakeClaudeClient([]),
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
    )
    content = (
        "봄맞이 카페 인테리어는 중요해요. "
        "봄맞이 카페 인테리어를 준비하면 좋아요. "
        "봄맞이 카페 인테리어 사례를 보면 도움돼요. "
        "봄맞이 카페 인테리어를 체크해보세요."
    )

    updated = generator._limit_exact_keyword_repetition(
        content=content,
        keywords=["봄맞이 카페 인테리어"],
        max_exact_matches=2,
    )

    assert updated.lower().count("봄맞이 카페 인테리어") <= 2
    assert updated != content


def test_limit_exact_keyword_repetition_keeps_content_when_under_limit():
    """반복 횟수가 임계치 이하면 원문을 유지해야 한다."""
    generator = ContentGenerator(
        client=FakeClaudeClient([]),
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
    )
    content = "봄맞이 카페 인테리어를 준비해요. 카페 인테리어 방향을 정리해요."

    updated = generator._limit_exact_keyword_repetition(
        content=content,
        keywords=["봄맞이 카페 인테리어"],
        max_exact_matches=2,
    )

    assert updated == content


def test_sanitize_meta_headings_and_normalize_h2_levels():
    """메타 제목은 제거되고 H3/H4만 있는 구조는 H2로 정규화되어야 한다."""
    generator = ContentGenerator(
        client=FakeClaudeClient([]),
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
    )
    content = "### 개선된 콘텐츠\n\n### 봄맞이 카페 인테리어 팁\n\n#### 실행 방법\n본문"

    sanitized = generator._sanitize_meta_headings(content)
    normalized = generator._normalize_heading_levels(sanitized)

    assert "개선된 콘텐츠" not in normalized
    assert "## 봄맞이 카페 인테리어 팁" in normalized
    assert "## 실행 방법" in normalized


def test_estimate_quality_score_fallback_returns_reasonable_score():
    """점수 누락 시 구조 기반 휴리스틱 점수가 계산되어야 한다."""
    generator = ContentGenerator(
        client=FakeClaudeClient([]),
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
    )
    content = (
        "## 도입\n충분히 긴 설명 문장입니다.\n\n"
        "## 실행 체크리스트\n- 항목 1\n- 항목 2\n\n"
        "## 마무리\n정리 문장입니다."
    )
    score = generator._estimate_quality_score_fallback(
        content=content,
        keywords=["봄맞이 카페 인테리어"],
        parsed={},
        raw_text="",
    )

    assert score is not None
    assert 55 <= score <= 88


def test_compute_keyword_count_applies_semantic_floor():
    """정확 일치가 0이어도 의미 포함이면 keyword_count는 최소 1이어야 한다."""
    generator = ContentGenerator(
        client=FakeClaudeClient([]),
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
    )
    content = "초등 저학년 아이의 생활 습관을 함께 점검해요."

    count = generator._compute_keyword_count(
        content=content,
        keywords=["초등 저학년 생활습관"],
    )

    assert count == 1


def test_compute_keyword_count_returns_zero_when_no_semantic_hit():
    """정확/의미 모두 없으면 keyword_count는 0이어야 한다."""
    generator = ContentGenerator(
        client=FakeClaudeClient([]),
        enable_quality_check=False,
        enable_seo_optimization=False,
        enable_voice_rewrite=False,
    )
    content = "카페 원두 보관과 추출 온도에 대한 글입니다."

    count = generator._compute_keyword_count(
        content=content,
        keywords=["초등 저학년 생활습관"],
    )

    assert count == 0


def test_llm_generate_fn_compatible_with_pipeline(monkeypatch: pytest.MonkeyPatch):
    """PipelineService가 기대하는 결과 스키마를 반환하는지 검증한다."""
    import modules.llm as llm_module

    reset_generator()
    monkeypatch.setattr(llm_module, "get_generator", lambda config=None: FakeGenerator())
    result = asyncio.run(llm_generate_fn(build_job("compat-job")))

    required_keys = {
        "final_content",
        "quality_gate",
        "quality_snapshot",
        "seo_snapshot",
        "image_prompts",
        "llm_calls_used",
        "llm_token_usage",
    }
    assert required_keys.issubset(result.keys())
    assert result["quality_gate"] == "pass"
    assert result["llm_calls_used"] == 2


def test_prompts_contain_required_placeholders():
    """프롬프트 템플릿의 필수 플레이스홀더를 검증한다."""
    assert "{title}" in USER_CONTENT_REQUEST
    assert "{keywords}" in USER_CONTENT_REQUEST
    assert "{category}" in USER_CONTENT_REQUEST
    assert "{keywords}" in SEO_OPTIMIZATION
    assert "JSON" in QUALITY_CHECK


def test_full_pipeline_with_mocked_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """LLM 응답 모킹으로 파이프라인 완료 상태를 검증한다."""
    import modules.llm as llm_module

    db_path = tmp_path / "llm_pipeline.db"
    store = JobStore(str(db_path), config=JobConfig(max_llm_calls_per_job=15))
    scheduled_at = "2026-02-19T01:00:00Z"
    assert store.schedule_job(
        job_id="pipeline-llm-job",
        title="LLM 파이프라인 테스트",
        seed_keywords=["LLM", "파이프라인"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled_at,
    )

    claimed_jobs = store.claim_due_jobs(limit=1, now_override=scheduled_at)
    assert len(claimed_jobs) == 1

    reset_generator()
    monkeypatch.setattr(llm_module, "get_generator", lambda config=None: FakeGenerator())
    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=llm_module.llm_generate_fn,
    )

    asyncio.run(pipeline.run_job(claimed_jobs[0]))

    final_job = store.get_job("pipeline-llm-job")
    assert final_job is not None
    assert final_job.status == store.STATUS_COMPLETED
    assert final_job.result_url == "https://blog.naver.com/test/llm"
    assert final_job.llm_call_count == 2


def test_token_budget_calibrator_recommends_from_observed_metrics(tmp_path: Path):
    """충분한 샘플이 쌓이면 보정 권장치가 기본값 이상으로 계산되어야 한다."""
    store = JobStore(str(tmp_path / "calibrator.db"), config=JobConfig(max_llm_calls_per_job=20))
    scheduled_at = "2026-02-19T01:00:00Z"
    assert store.schedule_job(
        job_id="calib-job-1",
        title="보정 테스트",
        seed_keywords=["토큰"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled_at,
    )

    # 품질 단계 샘플 3건 적재
    for idx in range(3):
        store.record_job_metric(
            job_id="calib-job-1",
            metric_type="quality_step",
            status="ok",
            input_tokens=4000 + (idx * 50),
            output_tokens=2800 + (idx * 40),
            provider="qwen",
            detail={"model": "qwen-plus"},
        )

    result = calibrate_token_budget(store, min_samples=3, safety_margin=1.1)
    assert result.observed_samples["quality_step"] == 3
    assert result.recommended["quality_step"]["input"] >= 4000
    assert result.recommended["quality_step"]["output"] >= 2800
