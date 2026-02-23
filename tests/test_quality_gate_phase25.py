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
from modules.automation.quality_evaluator import EvaluationResult, QualityEvaluator


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
