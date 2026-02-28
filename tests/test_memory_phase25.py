from pathlib import Path
from typing import Any, Dict, List

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.time_utils import now_utc
from modules.automation.trend_job_service import TrendJobService
from modules.config import MemoryConfig
from modules.memory.topic_store import TopicMemoryStore


def _build_store(tmp_path: Path, name: str = "memory_phase25.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


def _memory_config() -> MemoryConfig:
    return MemoryConfig(
        enabled=True,
        lookback_weeks=8,
        max_recent_posts=5,
        max_similar_posts=3,
        duplicate_threshold=0.65,
        precheck_duplicate_threshold=0.50,
        backfill_on_init=False,
        min_quality_score=0,
    )


def test_query_topic_memory_platform_filter_and_platform_field(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    store.insert_topic_memory(
        job_id="tm-naver",
        title="네이버 테스트",
        keywords=["테스트", "네이버"],
        topic_mode="cafe",
        platform="naver",
        persona_id="P1",
        summary="summary",
        result_url="https://example.com/naver",
        quality_score=90,
    )
    store.insert_topic_memory(
        job_id="tm-tistory",
        title="티스토리 테스트",
        keywords=["테스트", "티스토리"],
        topic_mode="it",
        platform="tistory",
        persona_id="P1",
        summary="summary",
        result_url="https://example.com/tistory",
        quality_score=90,
    )

    only_naver = store.query_topic_memory(platform="naver", limit=10)
    assert len(only_naver) == 1
    assert only_naver[0]["platform"] == "naver"


def test_topic_coverage_and_keyword_frequency(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    store.insert_topic_memory(
        job_id="cov-1",
        title="카페 원두",
        keywords=["원두", "커피"],
        topic_mode="cafe",
        platform="naver",
        persona_id="P1",
        summary="summary",
        result_url="https://example.com/1",
        quality_score=80,
    )
    store.insert_topic_memory(
        job_id="cov-2",
        title="카페 드리퍼",
        keywords=["원두", "드리퍼"],
        topic_mode="cafe",
        platform="naver",
        persona_id="P1",
        summary="summary",
        result_url="https://example.com/2",
        quality_score=80,
    )
    store.insert_topic_memory(
        job_id="cov-3",
        title="IT 키보드",
        keywords=["키보드", "리뷰"],
        topic_mode="it",
        platform="naver",
        persona_id="P1",
        summary="summary",
        result_url="https://example.com/3",
        quality_score=80,
    )

    coverage = store.get_topic_coverage_stats(lookback_days=56, platform="naver")
    assert coverage["cafe"] == 2
    assert coverage["it"] == 1

    freqs = dict(store.get_keyword_frequencies(topic_mode="cafe", lookback_days=56, top_n=10))
    assert freqs["원두"] == 2
    assert freqs["커피"] == 1


def test_has_recent_similar_active_job(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    assert store.schedule_job(
        job_id="active-dup",
        title="테스트 키워드 완벽 가이드",
        seed_keywords=["테스트 키워드", "가이드"],
        platform="naver",
        persona_id="P1",
        scheduled_at=now_utc(),
    )

    assert store.has_recent_similar_active_job(
        keyword="테스트 키워드",
        platform="naver",
        lookback_days=7,
    )
    assert not store.has_recent_similar_active_job(
        keyword="완전히다른키워드",
        platform="naver",
        lookback_days=7,
    )


def test_trend_job_service_blocks_active_duplicate(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    assert store.schedule_job(
        job_id="dup-seeded-job",
        title="에어프라이어 완벽 가이드",
        seed_keywords=["에어프라이어", "가이드"],
        platform="naver",
        persona_id="P1",
        scheduled_at=now_utc(),
    )

    service = TrendJobService(job_store=store, max_jobs_per_run=2)

    class MockCollector:
        def fetch_trending_keywords(self, category_name: str, count: int) -> List[str]:
            del category_name, count
            return ["에어프라이어"]

    service.collector = MockCollector()  # type: ignore[assignment]
    created = service.fetch_and_create_jobs(categories=["생활/건강"])
    assert created == []


def test_trend_job_service_uses_memory_store_when_active_job_absent(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    calls: Dict[str, Any] = {}

    class MemoryStoreStub:
        def __init__(self) -> None:
            self._config = type(
                "ConfigStub",
                (),
                {"precheck_duplicate_threshold": 0.42, "duplicate_threshold": 0.65},
            )()

        def is_duplicate_before_job(self, **kwargs: Any) -> bool:
            calls.update(kwargs)
            return True

    service = TrendJobService(
        job_store=store,
        memory_store=MemoryStoreStub(),
    )
    assert service._has_recent_job("중복키워드", days=7, topic_mode="cafe")
    assert calls["lookback_weeks"] == 1
    assert calls["similarity_threshold"] == 0.42


def test_topic_memory_store_facades(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    memory_store = TopicMemoryStore(job_store=store, config=_memory_config())

    store.insert_topic_memory(
        job_id="facade-1",
        title="커피 머신 추천",
        keywords=["커피", "머신"],
        topic_mode="cafe",
        platform="naver",
        persona_id="P1",
        summary="summary",
        result_url="https://example.com/facade",
        quality_score=90,
    )

    coverage = memory_store.get_coverage_stats(platform="naver")
    assert coverage.get("cafe", 0) >= 1

    is_dup = memory_store.is_duplicate_before_job(
        title="커피 머신 추천 가이드",
        keywords=["커피", "머신"],
        topic_mode="cafe",
        platform="naver",
    )
    assert is_dup is True
