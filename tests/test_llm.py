import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from modules.automation.job_store import Job, JobConfig, JobStore
from modules.automation.pipeline_service import PipelineService
from modules.llm import llm_generate_fn, reset_generator
from modules.llm.claude_client import ClaudeClient, LLMResponse
from modules.llm.content_generator import ContentGenerator, ContentResult
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
        image_points: Optional[List] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> PublishResult:
        del title, content, thumbnail, images, image_points, tags, category
        return PublishResult(success=True, url="https://blog.naver.com/test/llm")


def test_claude_client_init_requires_api_key(monkeypatch: pytest.MonkeyPatch):
    """API 키가 없으면 초기화에 실패해야 한다."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ValueError):
        ClaudeClient(api_key=None)


def test_content_generator_returns_valid_structure():
    """생성 결과 구조와 주요 필드를 검증한다."""
    outputs = [
        "# 자동화 블로그 작성 가이드\n\n## 시작하기\n\n초안 본문",
        "# 자동화 블로그 작성 가이드\n\n## SEO 시작하기\n\n최적화 본문",
        '{"score": 88, "issues": [], "summary": "양호"}',
        '{"thumbnail": {"prompt": "test thumbnail"}, "content_images": []}',  # 이미지 프롬프트 생성
    ]
    generator = ContentGenerator(client=FakeClaudeClient(outputs))
    result = asyncio.run(generator.generate(build_job()))

    assert result.final_content
    assert result.quality_gate == "pass"
    assert result.quality_snapshot["score"] == 88
    assert "keywords" in result.seo_snapshot
    assert result.llm_calls_used == 4  # draft + SEO + quality + image prompts
    assert result.provider_used in {"qwen", "deepseek", "claude"}
    assert len(result.image_prompts) >= 1


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
