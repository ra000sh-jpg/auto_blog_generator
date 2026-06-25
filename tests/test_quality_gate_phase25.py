"""
Phase 25: Quality Gate & Auto-Correction E2E 테스트

검증 시나리오:
1. Gate 2 통과: 점수 >= 70 → draft_ready 정상 저장
2. Gate 2 재작성 루프: 점수 < 70, grad="correction_needed" → 재생성 1회 후 저장
3. Gate 2 최종 반려: 재시도 초과 → QUALITY_REJECTED 에러 상태
4. LLM 미주입 시 Gate 2 건너뜀 → 정상 통과
5. failed_quality DB 상태: QUALITY_REJECTED 에러 코드는 STATUS_FAILED_QUALITY로 저장됨
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from modules.automation.job_store import Job, JobStore
from modules.automation.pipeline_service import PipelineService
from modules.automation.quality_evaluator import EvaluationResult, QualityEvaluator
from modules.uploaders.playwright_publisher import PublishResult


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path):
    """임시 SQLite DB."""
    return str(tmp_path / "test_quality.db")


@pytest.fixture
def store(tmp_db):
    return JobStore(db_path=tmp_db)


def _make_job(store: JobStore, job_id: str = "job_q1") -> Job:
    from modules.automation.time_utils import now_utc

    store.schedule_job(
        job_id=job_id,
        title="테스트 글",
        seed_keywords=["테크", "IT"],
        platform="naver",
        persona_id="p1",
        scheduled_at=now_utc(),
    )
    jobs = store.claim_due_jobs(limit=1)
    assert jobs, "Job claim 실패"
    return jobs[0]


class _DummyPublisher:
    async def publish(
        self,
        title: str,
        content: str,
        thumbnail=None,
        images=None,
        image_sources=None,
        image_points=None,
        tags=None,
        category=None,
        publish_mode=None,
    ) -> PublishResult:
        del title, content, thumbnail, images, image_sources, image_points, tags, category, publish_mode
        return PublishResult(success=True, url="https://blog.naver.com/test/gate2")


# ─────────────────────────────────────────────────────────────────────────────
# QualityEvaluator Unit Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestQualityEvaluatorNoLLM:
    """LLM 미주입 시 Gate 2 건너뜀 (Graceful Degradation)."""

    def test_skips_when_no_client(self):
        evaluator = QualityEvaluator(llm_client=None)
        result = asyncio.run(
            evaluator.evaluate(content="내용", persona_desc="블로거")
        )
        assert result.passed is True
        assert result.detail.get("skipped") is True


def test_pipeline_gate2_evaluates_prepared_content_key(store):
    """Gate 2는 발행 payload의 content 본문을 평가해야 한다."""

    class CaptureEvaluator:
        max_retries = 2

        def __init__(self) -> None:
            self.seen_content = ""

        async def evaluate(self, *, content: str, persona_desc: str, retry_count: int = 0):
            del persona_desc, retry_count
            self.seen_content = content
            return EvaluationResult(passed=True, score=88, gate="pass")

    async def generate_content(_job):
        del _job
        body = (
            "테스트 글은 테크와 IT 흐름을 독자가 차분하게 이해하도록 돕는 글입니다. "
            "서비스를 고를 때는 기능 이름보다 실제 사용 장면, 비용, 반복 사용 가능성을 함께 봐야 합니다. "
            "특히 자동화 도구는 처음 설정할 때보다 일주일 뒤에도 같은 품질을 유지하는지가 더 중요합니다. "
            "그래서 이 글은 체크리스트, 판단 기준, 운영 리스크를 나누어 설명합니다. "
        )
        return {
            "final_content": "Gate 2가 실제로 읽어야 하는 본문입니다. " + (body * 8),
            "quality_gate": "pass",
            "quality_snapshot": {"score": 90, "issues": []},
            "seo_snapshot": {"provider_used": "stub", "provider_model": "stub"},
            "image_prompts": [],
            "llm_token_usage": {},
        }

    store.set_system_setting("telegram_draft_approval_enabled", "false")
    job = _make_job(store, "job_gate2_payload_content")
    evaluator = CaptureEvaluator()
    pipeline = PipelineService(
        job_store=store,
        publisher=_DummyPublisher(),
        generate_fn=generate_content,
        quality_evaluator=evaluator,
    )

    assert asyncio.run(pipeline.process_generation(job)) is True
    assert "실제로 읽어야 하는 본문" in evaluator.seen_content


class TestQualityEvaluatorPass:
    """LLM 응답 점수 70 이상 → 통과."""

    def test_passes_on_high_score(self):
        mock_client = MagicMock()
        mock_client.generate = AsyncMock(
            return_value=MagicMock(
                content='{"score": 85, "passed": true, "issues": [], "feedback": ""}'
            )
        )
        evaluator = QualityEvaluator(llm_client=mock_client, max_retries=2)
        result = asyncio.run(
            evaluator.evaluate(content="좋은 글입니다", persona_desc="IT 전문가")
        )
        assert result.passed is True
        assert result.score == 85
        assert result.gate == "pass"


class TestQualityEvaluatorCorrectionNeeded:
    """점수 부족 + 재시도 남음 → gate=correction_needed."""

    def test_correction_gate_when_low_score_and_retries_left(self):
        mock_client = MagicMock()
        mock_client.generate = AsyncMock(
            return_value=MagicMock(
                content='{"score": 50, "passed": false, "issues": ["어조 불일치"], "feedback": "더 전문적인 어투로 수정하세요"}'
            )
        )
        evaluator = QualityEvaluator(llm_client=mock_client, max_retries=2)
        result = asyncio.run(
            evaluator.evaluate(content="어디서나 봤던 글체입니다", persona_desc="IT 전문가", retry_count=0)
        )
        assert result.passed is False
        assert result.gate == "correction_needed"
        assert result.error_code == "PERSONA_MISMATCH"
        assert "전문적" in result.feedback


class TestQualityEvaluatorRejected:
    """재시도 횟수 소진 → gate=rejected, error_code=QUALITY_REJECTED."""

    def test_rejected_when_max_retries_exceeded(self):
        mock_client = MagicMock()
        mock_client.generate = AsyncMock(
            return_value=MagicMock(
                content='{"score": 40, "passed": false, "issues": ["구조 불량"], "feedback": "처음부터 다시 쓰세요"}'
            )
        )
        evaluator = QualityEvaluator(llm_client=mock_client, max_retries=2)
        result = asyncio.run(
            evaluator.evaluate(content="저품질 글", persona_desc="전문가", retry_count=2)
        )
        assert result.passed is False
        assert result.gate == "rejected"
        assert result.error_code == "QUALITY_REJECTED"


# ─────────────────────────────────────────────────────────────────────────────
# JobStore Status Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestJobStoreFailedQuality:
    """QUALITY_REJECTED 에러는 STATUS_FAILED_QUALITY로 저장된다."""

    def test_quality_rejected_sets_failed_quality_status(self, store, tmp_db):
        job = _make_job(store, "job_q_rej")
        result = store.fail_job(
            job_id=job.job_id,
            error_code="QUALITY_REJECTED",
            error_message="페르소나 불일치",
            force_final=True,
        )
        assert result is True
        updated = store.get_job(job.job_id)
        assert updated is not None
        assert updated.status == store.STATUS_FAILED_QUALITY

    def test_normal_fail_still_sets_failed_status(self, store):
        job = _make_job(store, "job_q_norm")
        store.fail_job(
            job_id=job.job_id,
            error_code="NETWORK_TIMEOUT",
            error_message="타임아웃",
            force_final=True,
        )
        updated = store.get_job(job.job_id)
        assert updated is not None
        assert updated.status == store.STATUS_FAILED


# ─────────────────────────────────────────────────────────────────────────────
# Correction Prompt Builder
# ─────────────────────────────────────────────────────────────────────────────


class TestCorrectionPromptBuilder:
    """재작성 지시 프롬프트가 피드백을 포함하는지 확인."""

    def test_contains_feedback(self):
        evaluator = QualityEvaluator()
        prompt = evaluator.build_correction_prompt(
            original_content="원래 글",
            feedback="더 친근한 어투를 사용하세요",
            persona_desc="IT 블로거",
        )
        assert "더 친근한 어투" in prompt
        assert "IT 블로거" in prompt
