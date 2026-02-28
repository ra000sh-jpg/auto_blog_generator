import asyncio
from pathlib import Path
from typing import Any

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.scheduler_workers import memory_worker_loop
from modules.config import MemoryConfig
from modules.memory.topic_store import TopicMemoryStore


def _build_store(tmp_path: Path, name: str = "memory_async_phaseB.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


def _memory_async_config() -> MemoryConfig:
    return MemoryConfig(
        enabled=True,
        backfill_on_init=True,
        async_pipeline_enabled=True,
        async_queue_maxsize=10,
        async_retry_limit=2,
        async_retry_backoff_sec=0.1,
        semantic_enabled=False,
    )


def test_record_post_enqueues_when_async_enabled(tmp_path: Path) -> None:
    store = _build_store(tmp_path, "enqueue_only.db")
    memory_store = TopicMemoryStore(job_store=store, config=_memory_async_config())
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=10)
    memory_store.bind_event_queue(queue)

    memory_store.record_post(
        job_id="async-1",
        title="비동기 메모리 테스트",
        keywords=["비동기", "메모리"],
        topic_mode="it",
        platform="naver",
        persona_id="P1",
        result_url="https://example.com/async-1",
        quality_score=90,
    )
    assert queue.qsize() == 1
    assert store.query_topic_memory(topic_mode="it", platform="naver", limit=10) == []

    event = queue.get_nowait()
    assert event["type"] == "record_post"


def test_process_memory_event_persists_topic_memory(tmp_path: Path) -> None:
    store = _build_store(tmp_path, "process_event.db")
    memory_store = TopicMemoryStore(job_store=store, config=_memory_async_config())

    ok = memory_store.process_memory_event(
        {
            "type": "record_post",
            "payload": {
                "job_id": "async-2",
                "title": "이벤트 처리 테스트",
                "keywords": ["이벤트", "처리"],
                "topic_mode": "it",
                "platform": "naver",
                "persona_id": "P1",
                "result_url": "https://example.com/async-2",
                "quality_score": 88,
            },
            "attempts": 0,
        }
    )
    assert ok is True

    rows = store.query_topic_memory(topic_mode="it", platform="naver", limit=10)
    assert len(rows) == 1
    assert rows[0]["job_id"] == "async-2"


def test_request_backfill_enqueues_event(tmp_path: Path) -> None:
    store = _build_store(tmp_path, "request_backfill.db")
    memory_store = TopicMemoryStore(job_store=store, config=_memory_async_config())
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=10)
    memory_store.bind_event_queue(queue)

    memory_store.request_backfill(limit=123)
    assert queue.qsize() == 1
    event = queue.get_nowait()
    assert event["type"] == "ensure_backfill"
    assert event["payload"]["limit"] == 123


def test_record_post_fallback_sync_when_queue_full(tmp_path: Path) -> None:
    store = _build_store(tmp_path, "queue_full_fallback.db")
    memory_store = TopicMemoryStore(job_store=store, config=_memory_async_config())
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
    memory_store.bind_event_queue(queue)
    queue.put_nowait({"type": "dummy", "payload": {}, "attempts": 0})

    memory_store.record_post(
        job_id="async-fallback-1",
        title="큐 포화 폴백",
        keywords=["큐", "포화"],
        topic_mode="it",
        platform="naver",
        persona_id="P1",
        result_url="https://example.com/fallback",
        quality_score=90,
    )
    rows = store.query_topic_memory(topic_mode="it", platform="naver", limit=10)
    assert len(rows) == 1
    assert rows[0]["job_id"] == "async-fallback-1"


def test_memory_worker_loop_consumes_queue(tmp_path: Path) -> None:
    store = _build_store(tmp_path, "worker_consume.db")
    memory_store = TopicMemoryStore(job_store=store, config=_memory_async_config())
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=10)
    memory_store.bind_event_queue(queue)

    class ServiceStub:
        pass

    service = ServiceStub()
    service._memory_event_queue = queue
    service.memory_store = memory_store

    async def _run() -> None:
        worker = asyncio.create_task(memory_worker_loop(service))
        try:
            await queue.put(
                {
                    "type": "record_post",
                    "payload": {
                        "job_id": "worker-1",
                        "title": "워커 소비 테스트",
                        "keywords": ["워커", "소비"],
                        "topic_mode": "it",
                        "platform": "naver",
                        "persona_id": "P1",
                        "result_url": "https://example.com/worker-1",
                        "quality_score": 87,
                    },
                    "attempts": 0,
                }
            )
            await asyncio.sleep(0.3)
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())
    rows = store.query_topic_memory(topic_mode="it", platform="naver", limit=10)
    assert len(rows) == 1
    assert rows[0]["job_id"] == "worker-1"

