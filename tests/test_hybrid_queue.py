import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.pipeline_service import PipelineService
from modules.automation.time_utils import now_utc
from modules.uploaders.playwright_publisher import PublishResult
from scripts import run_worker


def build_store(tmp_path: Path, name: str = "hybrid_queue.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


class DummyPublisher:
    async def publish(
        self,
        title: str,
        content: str,
        thumbnail: Optional[str] = None,
        images: Optional[List[str]] = None,
        image_points: Optional[List[Any]] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> PublishResult:
        del title, content, thumbnail, images, image_points, tags, category
        return PublishResult(success=True, url="https://blog.naver.com/test/hybrid")


async def _generate_fn(_job) -> Dict[str, Any]:
    long_body = ("hybrid queue 하이브리드 큐 본문입니다. " * 60).strip()
    return {
        "final_content": long_body,
        "quality_gate": "pass",
        "quality_snapshot": {"score": 92, "issues": []},
        "seo_snapshot": {"provider_used": "stub", "provider_model": "stub"},
        "image_prompts": [],
        "llm_calls_used": 1,
    }


def test_pipeline_generation_then_publication_split(tmp_path: Path):
    store = build_store(tmp_path)
    due_now = now_utc()
    assert store.schedule_job(
        job_id="hybrid-job-1",
        title="Hybrid Queue Test",
        seed_keywords=["hybrid", "queue"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
    )

    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=_generate_fn,
    )

    generate_claimed = store.claim_for_generate(limit=1, now_override=due_now)
    assert len(generate_claimed) == 1
    prepared = asyncio.run(pipeline.process_generation(generate_claimed[0]))
    assert prepared is True

    ready_job = store.get_job("hybrid-job-1")
    assert ready_job is not None
    assert ready_job.status == store.STATUS_READY
    assert ready_job.prepared_payload.get("content")

    publish_claimed = store.claim_for_publish(limit=1, now_override=due_now)
    assert len(publish_claimed) == 1
    published = asyncio.run(pipeline.process_publication(publish_claimed[0]))
    assert published is True

    final_job = store.get_job("hybrid-job-1")
    assert final_job is not None
    assert final_job.status == store.STATUS_COMPLETED
    assert final_job.result_url.endswith("/hybrid")


def test_run_worker_mode_default_all(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_worker.py"])
    args = run_worker.parse_args()
    assert args.mode == "all"


def test_run_worker_mode_accepts_generator(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_worker.py", "--mode", "generator"])
    args = run_worker.parse_args()
    assert args.mode == "generator"


def test_run_worker_mode_accepts_publisher(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_worker.py", "--mode", "publisher"])
    args = run_worker.parse_args()
    assert args.mode == "publisher"
