import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.pipeline_service import PipelineService
from modules.automation.scheduler_seed import _build_kr_preopen_opportunity_seed
from modules.automation.scheduler_service import SchedulerService
from modules.automation.trend_job_service import CATEGORY_TO_TOPIC, TrendJobService
from modules.automation.time_utils import now_utc
from modules.market import BlogSlot
from modules.uploaders.playwright_publisher import PublishResult


def build_store(tmp_path: Path, name: str = "scheduler_test.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


class DummyPublisher:
    async def publish(
        self,
        title: str,
        content: str,
        thumbnail: Optional[str] = None,
        images: Optional[List[str]] = None,
        image_sources: Optional[Dict[str, Dict[str, str]]] = None,
        image_points: Optional[List[Any]] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> PublishResult:
        del title, content, thumbnail, images, image_sources, image_points, tags, category
        return PublishResult(success=True, url="https://blog.naver.com/test/1")


def test_trend_job_service_creates_jobs(tmp_path: Path):
    store = build_store(tmp_path)
    service = TrendJobService(job_store=store, max_jobs_per_run=2)

    class MockCollector:
        def fetch_trending_keywords(self, category_name: str, count: int) -> List[str]:
            del category_name, count
            return ["테스트키워드1", "테스트키워드2"]

    service.collector = MockCollector()  # type: ignore[assignment]
    job_ids = service.fetch_and_create_jobs(categories=["디지털/가전"])
    assert len(job_ids) == 2
    assert all(store.get_job(job_id) is not None for job_id in job_ids)


def test_category_to_topic_mapping():
    assert CATEGORY_TO_TOPIC["출산/육아"] == "parenting"
    assert CATEGORY_TO_TOPIC["디지털/가전"] == "it"
    assert CATEGORY_TO_TOPIC["생활/건강"] == "cafe"


def test_kr_preopen_opportunity_seed_adds_auto_publish_tags_without_api_keys():
    """글감 후보 수집이 불가능해도 국장전 자동발행 요청 태그는 남긴다."""

    result = _build_kr_preopen_opportunity_seed(
        service=object(),  # type: ignore[arg-type]
        original_slot=BlogSlot.KR_PREOPEN,
        resolved_slot=BlogSlot.KR_PREOPEN,
        local_dt=datetime(2026, 6, 8, 7, 0, tzinfo=timezone.utc),
    )

    assert "auto_publish:kr_preopen" in result["tags"]
    assert "publish_mode:publish" in result["tags"]
    assert "opportunity_status:fallback" in result["tags"]


def test_scheduler_service_setup():
    scheduler = SchedulerService(daily_posts_target=3, min_post_interval_minutes=60)
    scheduler.setup_scheduler()
    assert scheduler._scheduler is not None
    assert scheduler.daily_posts_target == 3
    assert scheduler.min_post_interval_minutes == 60


def test_scheduler_starts_telegram_update_poll_task(tmp_path: Path):
    """스케줄러 시작 시 텔레그램 버튼 수집 루프가 별도 태스크로 올라와야 한다."""
    store = build_store(tmp_path, "telegram-poll-start.db")
    scheduler = SchedulerService(job_store=store)
    started: list[str] = []

    async def fake_telegram_loop() -> None:
        started.append("telegram")

    scheduler._telegram_update_poll_loop = fake_telegram_loop  # type: ignore[method-assign]

    async def run_case() -> None:
        await scheduler.start()
        await asyncio.sleep(0)
        assert started == ["telegram"]
        assert scheduler._telegram_update_task is not None
        await scheduler.stop()

    asyncio.run(run_case())


def test_scheduler_collects_telegram_pending_updates(tmp_path: Path, monkeypatch):
    """스케줄러 사이클이 서버 웹훅 모듈의 getUpdates 폴백을 호출해야 한다."""
    import server.routers.telegram_webhook as telegram_router

    store = build_store(tmp_path, "telegram-poll-collect.db")
    scheduler = SchedulerService(job_store=store)
    calls: list[JobStore] = []

    async def fake_collect_pending_updates(job_store: JobStore) -> int:
        calls.append(job_store)
        return 2

    monkeypatch.setattr(
        telegram_router,
        "collect_pending_updates",
        fake_collect_pending_updates,
    )

    result = asyncio.run(scheduler._collect_telegram_pending_updates())

    assert result == 2
    assert calls == [store]


def test_scheduler_misfire_grace_time():
    scheduler = SchedulerService()
    assert scheduler.MISFIRE_GRACE_TIME == 86400


def test_daily_target_check_outside_active_hours():
    calls: Dict[str, int] = {"count": 0}

    @dataclass
    class PipelineStub:
        async def run_next_pending_job(self) -> bool:
            calls["count"] += 1
            return True

    scheduler = SchedulerService(
        pipeline_service=PipelineStub(),  # type: ignore[arg-type]
        daily_posts_target=3,
    )
    scheduler._get_now_local = lambda: datetime(2026, 2, 21, 3, 0, 0)  # type: ignore[assignment]

    asyncio.run(scheduler._run_daily_target_check())
    assert calls["count"] == 0


def test_post_interval_check():
    calls: Dict[str, int] = {"count": 0}

    @dataclass
    class PipelineStub:
        async def run_next_pending_job(self) -> bool:
            calls["count"] += 1
            return True

    @dataclass
    class JobStoreStub:
        def get_today_completed_count(self) -> int:
            return 0

        def get_last_completed_time(self) -> Optional[datetime]:
            return datetime.now(timezone.utc) - timedelta(minutes=30)

        def get_system_setting(self, key: str, default: str = "") -> str:
            return default

    scheduler = SchedulerService(
        pipeline_service=PipelineStub(),  # type: ignore[arg-type]
        job_store=JobStoreStub(),  # type: ignore[arg-type]
        min_post_interval_minutes=60,
    )
    scheduler._get_now_local = lambda: datetime(2026, 2, 21, 10, 0, 0)  # type: ignore[assignment]

    asyncio.run(scheduler._run_daily_target_check())
    assert calls["count"] == 0


def test_pipeline_run_next_pending_job(tmp_path: Path):
    store = build_store(tmp_path)
    due_now = now_utc()
    assert store.schedule_job(
        job_id="scheduler-pending-job",
        title="Scheduler Pending Job",
        seed_keywords=["scheduler", "pending"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
    )

    async def generate_fn(_job) -> Dict[str, Any]:
        long_body = ("scheduler pending 스케줄러 테스트 본문입니다. " * 60).strip()
        return {
            "final_content": long_body,
            "quality_gate": "pass",
            "quality_snapshot": {"score": 90, "issues": []},
            "seo_snapshot": {"provider_used": "stub", "provider_model": "stub"},
            "image_prompts": [],
            "llm_calls_used": 1,
        }

    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=generate_fn,
    )

    executed = asyncio.run(pipeline.run_next_pending_job())
    assert executed is True

    updated = store.get_job("scheduler-pending-job")
    assert updated is not None
    assert updated.status == store.STATUS_COMPLETED


def test_jobstore_today_count_and_last_completed_time(tmp_path: Path):
    store = build_store(tmp_path)
    due_now = now_utc()
    assert store.schedule_job(
        job_id="scheduler-completed-job",
        title="Completed Job",
        seed_keywords=["completed"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
    )
    claimed = store.claim_due_jobs(limit=1, now_override=due_now)
    assert len(claimed) == 1
    assert store.complete_job(
        job_id="scheduler-completed-job",
        result_url="https://blog.naver.com/test/completed",
    )

    assert store.get_today_completed_count() >= 1
    assert store.get_last_completed_time() is not None


def test_jobstore_ready_claim_flow(tmp_path: Path):
    store = build_store(tmp_path)
    due_now = now_utc()
    assert store.schedule_job(
        job_id="scheduler-ready-job",
        title="Ready Job",
        seed_keywords=["ready"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
    )
    claimed = store.claim_due_jobs(limit=1, now_override=due_now)
    assert len(claimed) == 1
    assert store.save_prepared_payload(
        "scheduler-ready-job",
        {"title": "Ready Job", "content": "초안 본문", "images": [], "image_points": []},
    )
    assert store.get_ready_to_publish_count() == 1

    publish_claimed = store.claim_ready_jobs(limit=1, now_override=due_now)
    assert len(publish_claimed) == 1
    assert publish_claimed[0].job_id == "scheduler-ready-job"
    assert publish_claimed[0].prepared_payload.get("content") == "초안 본문"


def test_jobstore_claim_filters_required_tag_for_generate_and_publish(tmp_path: Path):
    """필수 태그가 지정되면 생성/발행 claim 모두 해당 태그의 잡만 선점한다."""

    due_now = now_utc()

    generate_store = build_store(tmp_path, "scheduler_required_tag_generate.db")
    assert generate_store.schedule_job(
        job_id="generate-legacy-job",
        title="예전 일반 글",
        seed_keywords=["legacy"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
        tags=["not_market_daily"],
    )
    assert generate_store.schedule_job(
        job_id="generate-market-job",
        title="시장 브리핑 글",
        seed_keywords=["market"],
        platform="naver",
        persona_id="P4",
        scheduled_at=due_now,
        tags=["market_daily", "market_slot:kr_preopen"],
    )

    generated_claims = generate_store.claim_due_jobs(
        limit=2,
        now_override=due_now,
        required_tag="market_daily",
    )

    assert [job.job_id for job in generated_claims] == ["generate-market-job"]
    assert generate_store.get_job("generate-legacy-job").status == generate_store.STATUS_QUEUED
    assert generate_store.get_job("generate-market-job").status == generate_store.STATUS_RUNNING

    publish_store = build_store(tmp_path, "scheduler_required_tag_publish.db")
    assert publish_store.schedule_job(
        job_id="publish-legacy-job",
        title="발행 대기 일반 글",
        seed_keywords=["legacy"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
        tags=["not_market_daily"],
    )
    assert publish_store.schedule_job(
        job_id="publish-market-job",
        title="발행 대기 시장 글",
        seed_keywords=["market"],
        platform="naver",
        persona_id="P4",
        scheduled_at=due_now,
        tags=["market_daily", "market_slot:us_preopen"],
    )

    prepared_claims = publish_store.claim_due_jobs(limit=2, now_override=due_now)
    assert {job.job_id for job in prepared_claims} == {"publish-legacy-job", "publish-market-job"}
    for job in prepared_claims:
        assert publish_store.save_prepared_payload(
            job.job_id,
            {"title": job.title, "content": "준비된 초안 본문", "images": [], "image_points": []},
        )

    publish_claims = publish_store.claim_ready_jobs(
        limit=2,
        now_override=due_now,
        required_tag="market_daily",
    )

    assert [job.job_id for job in publish_claims] == ["publish-market-job"]
    assert publish_store.get_job("publish-legacy-job").status == publish_store.STATUS_READY
    assert publish_store.get_job("publish-market-job").status == publish_store.STATUS_RUNNING


def test_pipeline_prepare_then_publish_ready_job(tmp_path: Path):
    store = build_store(tmp_path)
    store.set_system_setting("telegram_draft_approval_enabled", "false")
    due_now = now_utc()
    assert store.schedule_job(
        job_id="scheduler-prepare-publish-job",
        title="Prepare Publish Job",
        seed_keywords=["prepare", "publish"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
    )

    async def generate_fn(_job) -> Dict[str, Any]:
        long_body = ("prepare publish 준비 발행 테스트 본문입니다. " * 60).strip()
        return {
            "final_content": long_body,
            "quality_gate": "pass",
            "quality_snapshot": {"score": 88, "issues": []},
            "seo_snapshot": {"provider_used": "stub", "provider_model": "stub"},
            "image_prompts": [],
            "llm_calls_used": 1,
        }

    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=generate_fn,
    )

    prepared = asyncio.run(pipeline.prepare_next_pending_job())
    assert prepared is True
    ready_job = store.get_job("scheduler-prepare-publish-job")
    assert ready_job is not None
    assert ready_job.status == store.STATUS_READY
    assert ready_job.prepared_payload.get("content")

    published = asyncio.run(pipeline.publish_next_ready_job())
    assert published is True

    completed_job = store.get_job("scheduler-prepare-publish-job")
    assert completed_job is not None
    assert completed_job.status == store.STATUS_COMPLETED
    assert completed_job.prepared_payload == {}


def test_weighted_publish_slots_are_deterministic_with_seed():
    """가중 분포 발행 슬롯은 seed가 같으면 항상 동일해야 한다."""
    scheduler_a = SchedulerService(
        daily_posts_target=3,
        min_post_interval_minutes=60,
        random_seed=20260220,
    )
    scheduler_b = SchedulerService(
        daily_posts_target=3,
        min_post_interval_minutes=60,
        random_seed=20260220,
    )

    target_date = date(2026, 2, 20)
    slots_a = scheduler_a._build_daily_publish_slots(target_date)
    slots_b = scheduler_b._build_daily_publish_slots(target_date)

    assert [slot.isoformat() for slot in slots_a] == [slot.isoformat() for slot in slots_b]
    assert len(slots_a) == 3
    assert slots_a == sorted(slots_a)

    for index in range(1, len(slots_a)):
        gap_minutes = (slots_a[index] - slots_a[index - 1]).total_seconds() / 60.0
        assert gap_minutes >= 60.0


def test_daily_quota_seed_mixes_idea_vault_and_non_vault(tmp_path: Path):
    """자정 시드 생성 시 비율대로 일반 할당+아이디어 창고가 함께 생성되어야 한다."""
    store = build_store(tmp_path, "scheduler_seed_mix.db")
    store.set_system_setting("scheduler_daily_posts_target", "5")
    store.set_system_setting("scheduler_market_daily_enabled", "false")
    store.set_system_setting("scheduler_idea_vault_daily_quota", "2")
    store.set_system_setting(
        "scheduler_category_allocations",
        json.dumps(
            [
                {"category": "IT 자동화", "topic_mode": "it", "count": 2},
                {"category": "경제 브리핑", "topic_mode": "finance", "count": 2},
                {"category": "다양한 생각", "topic_mode": "cafe", "count": 1},
            ],
            ensure_ascii=False,
        ),
    )
    inserted = store.add_idea_vault_items(
        [
            {
                "raw_text": "카페 오픈 루틴 개선 아이디어",
                "mapped_category": "다양한 생각",
                "topic_mode": "cafe",
                "parser_used": "test",
            },
            {
                "raw_text": "AI 자동화로 업무시간 절감한 경험",
                "mapped_category": "IT 자동화",
                "topic_mode": "it",
                "parser_used": "test",
            },
            {
                "raw_text": "이번 주 금리 흐름 정리",
                "mapped_category": "경제 브리핑",
                "topic_mode": "finance",
                "parser_used": "test",
            },
        ]
    )
    assert inserted == 3

    scheduler = SchedulerService(job_store=store)
    scheduler._get_now_local = lambda: datetime(2026, 2, 22, 0, 6, 0)  # type: ignore[assignment]
    asyncio.run(scheduler._run_daily_quota_seed())

    with store.connection() as conn:
        rows = conn.execute("SELECT tags FROM jobs ORDER BY created_at ASC").fetchall()
    assert len(rows) == 5
    idea_vault_jobs = 0
    for row in rows:
        tags = json.loads(row["tags"] or "[]")
        if "idea_vault" in tags:
            idea_vault_jobs += 1
    assert idea_vault_jobs == 2

    stats = store.get_idea_vault_stats()
    assert stats["pending"] == 1
    assert stats["queued"] == 2


def test_daily_quota_seed_respects_strict_holiday_when_idea_stock_empty(tmp_path: Path):
    """아이디어 재고가 없으면 빈자리를 일반 할당으로 대체하지 않아야 한다."""
    store = build_store(tmp_path, "scheduler_seed_strict.db")
    store.set_system_setting("scheduler_daily_posts_target", "5")
    store.set_system_setting("scheduler_market_daily_enabled", "false")
    store.set_system_setting("scheduler_idea_vault_daily_quota", "2")
    store.set_system_setting(
        "scheduler_category_allocations",
        json.dumps(
            [
                {"category": "IT 자동화", "topic_mode": "it", "count": 3},
                {"category": "다양한 생각", "topic_mode": "cafe", "count": 2},
            ],
            ensure_ascii=False,
        ),
    )

    scheduler = SchedulerService(job_store=store)
    scheduler._get_now_local = lambda: datetime(2026, 2, 23, 0, 6, 0)  # type: ignore[assignment]
    asyncio.run(scheduler._run_daily_quota_seed())

    with store.connection() as conn:
        total_row = conn.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()
        idea_row = conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE tags LIKE '%idea_vault%'"
        ).fetchone()
    assert total_row is not None
    assert int(total_row["count"]) == 3  # 5건 목표 중 idea_vault 2건 미충족 -> 엄격 휴업
    assert idea_row is not None
    assert int(idea_row["count"]) == 0


def test_market_daily_seed_creates_three_fixed_slots_on_weekday(tmp_path: Path):
    """시장 브리핑 모드에서는 평일에 국장/미장/통찰형 3편을 고정 생성한다."""

    store = build_store(tmp_path, "scheduler_market_daily.db")
    store.set_system_setting("scheduler_daily_posts_target", "3")
    store.set_system_setting("scheduler_market_daily_enabled", "true")

    scheduler = SchedulerService(job_store=store)
    scheduler._get_now_local = lambda: datetime(2026, 6, 8, 0, 6, 0)  # type: ignore[assignment]
    asyncio.run(scheduler._run_daily_quota_seed())

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT title, scheduled_at, tags, category FROM jobs ORDER BY scheduled_at ASC"
        ).fetchall()

    assert len(rows) == 3
    all_tags = [json.loads(row["tags"] or "[]") for row in rows]
    flattened = {tag for tags in all_tags for tag in tags}
    assert "market_slot:kr_preopen" in flattened
    assert "market_slot:us_preopen" in flattened
    assert "market_slot:evergreen_insight" in flattened
    assert all(row["category"] == "경제 브리핑" for row in rows)
    assert any(row["scheduled_at"] == "2026-06-07T23:10:00Z" for row in rows)
    assert any(row["scheduled_at"] == "2026-06-08T11:30:00Z" for row in rows)
    assert store.get_system_setting("scheduler_last_seed_mode", "") == "market_daily"
    assert store.get_system_setting("scheduler_last_seed_market_count", "") == "2"


def test_market_daily_seed_adds_extra_opportunity_when_target_exceeds_three(
    tmp_path: Path,
    monkeypatch,
):
    """하루 목표가 4편 이상이면 검증된 시장 기회 글감을 추가 슬롯으로 생성한다."""

    import modules.collectors.naver_search as naver_search_module
    import modules.market as market_module

    class FakeNaverSearchCollector:
        enabled = True

        def search(self, query: str, *, service: str = "blog", display: int = 5, sort: str = "sim"):
            del display, sort
            if service == "news":
                return [
                    SimpleNamespace(
                        title=f"{query} 한국은행 공시 실적 {index}",
                        description="공시와 실적 기반 시장 뉴스",
                    )
                    for index in range(10)
                ]
            return []

    class FakeMarketDataCollector:
        def collect(self, *args, **kwargs):
            del args, kwargs
            return None

    monkeypatch.setattr(naver_search_module, "NaverSearchCollector", FakeNaverSearchCollector)
    monkeypatch.setattr(market_module, "MarketDataCollector", FakeMarketDataCollector)

    store = build_store(tmp_path, "scheduler_market_daily_extra.db")
    store.set_system_setting("scheduler_daily_posts_target", "4")
    store.set_system_setting("scheduler_market_daily_enabled", "true")
    store.set_system_setting("scheduler_market_extra_opportunity_min_score", "70")

    scheduler = SchedulerService(job_store=store)
    scheduler._get_now_local = lambda: datetime(2026, 6, 8, 0, 6, 0)  # type: ignore[assignment]
    asyncio.run(scheduler._run_daily_quota_seed())

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT title, scheduled_at, tags, category FROM jobs ORDER BY scheduled_at ASC"
        ).fetchall()

    assert len(rows) == 4
    all_tags = [json.loads(row["tags"] or "[]") for row in rows]
    flattened = {tag for tags in all_tags for tag in tags}
    assert "market_slot:opportunity" in flattened
    assert "market_extra:opportunity" in flattened
    assert "오늘의증시" in flattened
    assert any(row["scheduled_at"] == "2026-06-08T02:40:00Z" for row in rows)
    assert all(row["category"] == "경제 브리핑" for row in rows)
    assert store.get_system_setting("scheduler_last_seed_count", "") == "4"
    assert store.get_system_setting("scheduler_last_seed_opportunity_count", "") == "1"


def test_market_daily_seed_is_default_mode(tmp_path: Path):
    """별도 설정이 없으면 시장 브리핑 하루 3편 모드를 기본 사용한다."""

    store = build_store(tmp_path, "scheduler_market_daily_default.db")
    store.set_system_setting("scheduler_daily_posts_target", "3")

    scheduler = SchedulerService(job_store=store)
    scheduler._get_now_local = lambda: datetime(2026, 6, 8, 0, 6, 0)  # type: ignore[assignment]
    asyncio.run(scheduler._run_daily_quota_seed())

    with store.connection() as conn:
        rows = conn.execute("SELECT tags FROM jobs ORDER BY scheduled_at ASC").fetchall()

    flattened = {tag for row in rows for tag in json.loads(row["tags"] or "[]")}
    assert len(rows) == 3
    assert "market_daily" in flattened
    assert store.get_system_setting("scheduler_last_seed_mode", "") == "market_daily"


def test_market_daily_publish_slots_are_fixed_to_operating_plan(tmp_path: Path):
    """시장 브리핑 모드에서는 발행 슬롯도 08:10/18:30/미장 전으로 고정한다."""

    store = build_store(tmp_path, "scheduler_market_publish_slots.db")
    store.set_system_setting("scheduler_market_daily_enabled", "true")
    scheduler = SchedulerService(job_store=store)
    scheduler._get_now_local = lambda: datetime(2026, 6, 8, 0, 6, 0)  # type: ignore[assignment]

    slots = scheduler._build_daily_publish_slots(date(2026, 6, 8), daily_target=3)
    hhmm = [(slot.hour, slot.minute) for slot in slots]

    assert hhmm == [(8, 10), (18, 30), (20, 30)]


def test_market_daily_scheduler_ignores_legacy_non_market_jobs(tmp_path: Path):
    """시장 모드 자동 워커는 오래된 일반 잡보다 market_daily 잡을 우선 처리한다."""

    store = build_store(tmp_path, "scheduler_market_required_tag.db")
    store.set_system_setting("scheduler_market_daily_enabled", "true")
    store.set_system_setting("telegram_draft_approval_enabled", "false")
    due_now = now_utc()
    assert store.schedule_job(
        job_id="legacy-general-job",
        title="예전 일반 상품 글",
        seed_keywords=["legacy"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
        tags=[],
        category="생활",
    )
    assert store.schedule_job(
        job_id="market-target-job",
        title="시장 브리핑 글",
        seed_keywords=["시장", "브리핑"],
        platform="naver",
        persona_id="P4",
        scheduled_at=due_now,
        tags=["market_daily", "market_slot:evergreen_insight"],
        category="경제 브리핑",
    )

    async def generate_fn(_job) -> Dict[str, Any]:
        long_body = ("시장 모드 태그 필터 테스트 본문입니다. " * 70).strip()
        return {
            "final_content": long_body,
            "quality_gate": "pass",
            "quality_snapshot": {"score": 90, "issues": []},
            "seo_snapshot": {"provider_used": "stub", "provider_model": "stub"},
            "image_prompts": [],
            "llm_calls_used": 1,
        }

    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=generate_fn,
    )
    scheduler = SchedulerService(job_store=store, pipeline_service=pipeline)

    prepared = asyncio.run(scheduler._prepare_next_available_job(job_kind=store.JOB_KIND_MASTER))

    assert prepared is True
    assert store.get_job("legacy-general-job").status == store.STATUS_QUEUED
    assert store.get_job("market-target-job").status == store.STATUS_READY


def test_market_daily_seed_replaces_weekend_market_slots(tmp_path: Path):
    """주말에는 국장/미장 브리핑 슬롯을 통찰형 글로 대체한다."""

    store = build_store(tmp_path, "scheduler_market_weekend.db")
    store.set_system_setting("scheduler_daily_posts_target", "3")
    store.set_system_setting("scheduler_market_daily_enabled", "true")

    scheduler = SchedulerService(job_store=store)
    scheduler._get_now_local = lambda: datetime(2026, 6, 6, 0, 6, 0)  # type: ignore[assignment]
    asyncio.run(scheduler._run_daily_quota_seed())

    with store.connection() as conn:
        rows = conn.execute("SELECT tags FROM jobs ORDER BY created_at ASC").fetchall()

    assert len(rows) == 3
    flattened = [tag for row in rows for tag in json.loads(row["tags"] or "[]")]
    assert "market_slot:kr_preopen" not in flattened
    assert "market_slot:us_preopen" not in flattened
    assert flattened.count("market_slot:evergreen_insight") == 2
    assert flattened.count("market_slot:weekly_reflection") == 1
    assert store.get_system_setting("scheduler_last_seed_market_count", "") == "0"
    assert store.get_system_setting("scheduler_last_seed_evergreen_count", "") == "3"


def test_market_plus_category_ramp_week1_creates_market_four_and_it_one(tmp_path: Path):
    """확장 전략 1주차에는 경제 4편과 IT 1편을 생성한다."""

    store = build_store(tmp_path, "scheduler_market_plus_week1.db")
    store.set_system_setting("scheduler_strategy_mode", "market_plus_category_ramp")
    store.set_system_setting("scheduler_market_base_target", "4")
    store.set_system_setting("category_ramp_start_date", "2026-06-08")

    scheduler = SchedulerService(job_store=store)
    scheduler._get_now_local = lambda: datetime(2026, 6, 8, 0, 6, 0)  # type: ignore[assignment]
    asyncio.run(scheduler._run_daily_quota_seed())

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT tags, category, persona_id FROM jobs ORDER BY scheduled_at ASC"
        ).fetchall()

    assert len(rows) == 5
    all_tags = [json.loads(row["tags"] or "[]") for row in rows]
    flattened = {tag for tags in all_tags for tag in tags}
    assert store.get_system_setting("scheduler_last_seed_mode", "") == "market_plus_category_ramp"
    assert store.get_system_setting("scheduler_last_seed_market_count", "") == "4"
    assert store.get_system_setting("scheduler_last_seed_category_expansion_count", "") == "1"
    assert "category_topic:it" in flattened
    assert any(tag.startswith("category_template:it_") for tag in flattened)
    assert "writing_strategy:market_preopen_scenario" in flattened
    assert any(tag.startswith("writing_strategy:it_") for tag in flattened)
    assert "approval_required:category_expansion" in flattened
    category_rows = [row for row in rows if "category_expansion" in json.loads(row["tags"] or "[]")]
    assert len(category_rows) == 1
    assert category_rows[0]["category"] == "IT/테크"
    assert "auto_publish:kr_preopen" not in json.loads(category_rows[0]["tags"] or "[]")


def test_market_plus_category_ramp_week2_adds_health(tmp_path: Path):
    """확장 전략 2주차에는 IT와 건강을 추가한다."""

    store = build_store(tmp_path, "scheduler_market_plus_week2.db")
    store.set_system_setting("scheduler_strategy_mode", "market_plus_category_ramp")
    store.set_system_setting("scheduler_market_base_target", "4")
    store.set_system_setting("category_ramp_start_date", "2026-06-01")

    scheduler = SchedulerService(job_store=store)
    scheduler._get_now_local = lambda: datetime(2026, 6, 8, 0, 6, 0)  # type: ignore[assignment]
    asyncio.run(scheduler._run_daily_quota_seed())

    with store.connection() as conn:
        rows = conn.execute("SELECT tags FROM jobs ORDER BY scheduled_at ASC").fetchall()

    flattened = {tag for row in rows for tag in json.loads(row["tags"] or "[]")}
    assert len(rows) == 6
    assert "category_topic:it" in flattened
    assert "category_topic:health" in flattened
    assert store.get_system_setting("scheduler_last_seed_category_ramp_week", "") == "2"


def test_market_plus_category_ramp_week3_adds_parenting(tmp_path: Path):
    """확장 전략 3주차부터 IT/건강/육아를 모두 추가한다."""

    store = build_store(tmp_path, "scheduler_market_plus_week3.db")
    store.set_system_setting("scheduler_strategy_mode", "market_plus_category_ramp")
    store.set_system_setting("scheduler_market_base_target", "4")
    store.set_system_setting("category_ramp_start_date", "2026-05-25")

    scheduler = SchedulerService(job_store=store)
    scheduler._get_now_local = lambda: datetime(2026, 6, 8, 0, 6, 0)  # type: ignore[assignment]
    asyncio.run(scheduler._run_daily_quota_seed())

    with store.connection() as conn:
        rows = conn.execute("SELECT tags FROM jobs ORDER BY scheduled_at ASC").fetchall()

    flattened = {tag for row in rows for tag in json.loads(row["tags"] or "[]")}
    assert len(rows) == 7
    assert {"category_topic:it", "category_topic:health", "category_topic:parenting"} <= flattened
    assert store.get_system_setting("scheduler_last_seed_category_ramp_week", "") == "3"


def test_market_plus_category_ramp_skips_when_krx_closed(tmp_path: Path):
    """확장 전략은 KRX 비영업일에는 자동 시드를 만들지 않는다."""

    store = build_store(tmp_path, "scheduler_market_plus_closed.db")
    store.set_system_setting("scheduler_strategy_mode", "market_plus_category_ramp")

    scheduler = SchedulerService(job_store=store)
    scheduler._get_now_local = lambda: datetime(2026, 6, 6, 0, 6, 0)  # type: ignore[assignment]
    asyncio.run(scheduler._run_daily_quota_seed())

    with store.connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()

    assert int(row["count"]) == 0
    assert store.get_system_setting("scheduler_last_seed_skip_reason", "") == "krx_closed"


def test_category_expansion_forces_draft_approval_and_filters_internal_tags(tmp_path: Path):
    """확장 카테고리는 전역 승인 설정이 꺼져도 승인 대기로 보내고 내부 태그를 제거한다."""

    store = build_store(tmp_path, "scheduler_category_approval.db")
    store.set_system_setting("telegram_draft_approval_enabled", "false")
    due_now = now_utc()
    assert store.schedule_job(
        job_id="category-expansion-job",
        title="AI 도구 비교 글",
        seed_keywords=["AI", "비교"],
        platform="naver",
        persona_id="P2",
        scheduled_at=due_now,
        tags=[
            "AI",
            "category_expansion",
            "category_topic:it",
            "category_template:it_compare_decide",
            "writing_strategy:it_compare_decide",
            "writing_intent:compare_decide",
            "writing_axis:ev25_cmp45_chk20_risk10",
            "approval_required:category_expansion",
        ],
        category="IT/테크",
    )
    job = store.get_job("category-expansion-job")
    assert job is not None

    async def generate_fn(_job) -> Dict[str, Any]:
        return {}

    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=generate_fn,
    )

    assert pipeline._should_request_draft_approval(job=job, payload={"publish_mode": ""}) is True
    assert pipeline._public_publish_tags(job.tags) == ["AI"]


def test_scheduler_sub_job_catchup_creates_missing_sub_jobs(tmp_path: Path):
    store = build_store(tmp_path, "scheduler_sub_catchup.db")
    store.set_system_setting("multichannel_enabled", "true")

    master_channel_id = "channel-master-1"
    sub_channel_id = "channel-sub-1"
    unsupported_channel_id = "channel-wp-1"

    assert store.insert_channel(
        {
            "channel_id": master_channel_id,
            "platform": "naver",
            "label": "Master",
            "blog_url": "https://blog.naver.com/master",
            "persona_id": "P1",
            "persona_desc": "",
            "daily_target": 0,
            "style_level": 2,
            "style_model": "",
            "publish_delay_minutes": 90,
            "is_master": True,
            "auth_json": "{}",
            "active": True,
        }
    )
    assert store.insert_channel(
        {
            "channel_id": sub_channel_id,
            "platform": "naver",
            "label": "Sub Naver",
            "blog_url": "https://blog.naver.com/sub",
            "persona_id": "P2",
            "persona_desc": "",
            "daily_target": 0,
            "style_level": 2,
            "style_model": "",
            "publish_delay_minutes": 120,
            "is_master": False,
            "auth_json": '{"session_dir":"data/sessions/naver_sub"}',
            "active": True,
        }
    )
    assert store.insert_channel(
        {
            "channel_id": unsupported_channel_id,
            "platform": "wordpress",
            "label": "Sub WP",
            "blog_url": "https://example.com",
            "persona_id": "P3",
            "persona_desc": "",
            "daily_target": 0,
            "style_level": 2,
            "style_model": "",
            "publish_delay_minutes": 120,
            "is_master": False,
            "auth_json": "{}",
            "active": True,
        }
    )

    due_now = now_utc()
    master_job_id = "master-completed-1"
    assert store.schedule_job(
        job_id=master_job_id,
        title="Master Completed",
        seed_keywords=["alpha", "beta"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
        tags=["tag1"],
        category="IT 자동화",
        job_kind=store.JOB_KIND_MASTER,
    )
    claimed = store.claim_due_jobs(limit=1, now_override=due_now, job_kind=store.JOB_KIND_MASTER)
    assert len(claimed) == 1
    assert store.complete_job(
        job_id=master_job_id,
        result_url="https://blog.naver.com/master/1",
    )

    scheduler = SchedulerService(job_store=store)
    asyncio.run(scheduler._run_sub_job_catchup())
    asyncio.run(scheduler._run_sub_job_catchup())  # 중복 호출 시에도 1건만 유지

    created_sub = store.get_sub_job_by_master_channel(master_job_id, sub_channel_id)
    assert created_sub is not None
    assert created_sub.job_kind == store.JOB_KIND_SUB
    assert created_sub.platform == "naver"
    assert created_sub.status == store.STATUS_QUEUED

    unsupported_sub = store.get_sub_job_by_master_channel(master_job_id, unsupported_channel_id)
    assert unsupported_sub is None


def test_scheduler_sub_job_catchup_logs_stats_with_skip_reason(tmp_path: Path, caplog) -> None:
    store = build_store(tmp_path, "scheduler_sub_catchup_logs.db")
    store.set_system_setting("multichannel_enabled", "true")

    assert store.insert_channel(
        {
            "channel_id": "channel-master-log",
            "platform": "naver",
            "label": "Master",
            "blog_url": "https://blog.naver.com/master-log",
            "persona_id": "P1",
            "is_master": True,
            "auth_json": "{}",
            "active": True,
        }
    )
    assert store.insert_channel(
        {
            "channel_id": "channel-sub-wp-log",
            "platform": "wordpress",
            "label": "Sub WP",
            "blog_url": "https://example.com",
            "persona_id": "P2",
            "is_master": False,
            "auth_json": "{}",
            "active": True,
        }
    )

    due_now = now_utc()
    assert store.schedule_job(
        job_id="master-log-1",
        title="Master Log",
        seed_keywords=["alpha"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
        job_kind=store.JOB_KIND_MASTER,
    )
    claimed = store.claim_due_jobs(limit=1, now_override=due_now, job_kind=store.JOB_KIND_MASTER)
    assert len(claimed) == 1
    assert store.complete_job("master-log-1", "https://blog.naver.com/master-log/1")

    scheduler = SchedulerService(job_store=store)
    caplog.set_level("INFO")
    asyncio.run(scheduler._run_sub_job_catchup())

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "Sub job catch-up stats" in messages
    assert "throughput_per_min" in messages
    assert "publisher_not_implemented" in messages


def test_scheduler_sub_job_publish_catchup_logs_stats_when_no_due_jobs(tmp_path: Path, caplog) -> None:
    store = build_store(tmp_path, "scheduler_sub_publish_logs.db")

    class PipelineStub:
        async def publish_next_ready_job(self, job_kind: Optional[str] = None) -> bool:
            del job_kind
            return False

        async def prepare_next_pending_job(self, job_kind: Optional[str] = None) -> bool:
            del job_kind
            return False

    scheduler = SchedulerService(
        job_store=store,
        pipeline_service=PipelineStub(),  # type: ignore[arg-type]
    )
    caplog.set_level("INFO")
    asyncio.run(scheduler._run_sub_job_publish_catchup(max_jobs=3))

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "Sub job publish catch-up stats" in messages
    assert "throughput_per_min" in messages
    assert "no_due_jobs" in messages


def test_jobstore_counts_and_claims_support_job_kind_filter(tmp_path: Path):
    store = build_store(tmp_path, "scheduler_kind_filter.db")
    due_now = now_utc()

    assert store.schedule_job(
        job_id="master-kind-1",
        title="Master Kind",
        seed_keywords=["master"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
        job_kind=store.JOB_KIND_MASTER,
    )
    assert store.schedule_job(
        job_id="sub-kind-1",
        title="Sub Kind",
        seed_keywords=["sub"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
        job_kind=store.JOB_KIND_SUB,
        master_job_id="master-kind-1",
        channel_id="channel-sub-kind",
    )

    master_claimed = store.claim_due_jobs(limit=1, now_override=due_now, job_kind=store.JOB_KIND_MASTER)
    sub_claimed = store.claim_due_jobs(limit=1, now_override=due_now, job_kind=store.JOB_KIND_SUB)
    assert len(master_claimed) == 1
    assert len(sub_claimed) == 1

    assert store.complete_job("master-kind-1", "https://blog.naver.com/master-kind/1")
    assert store.complete_job("sub-kind-1", "https://blog.naver.com/sub-kind/1")

    assert store.get_today_completed_count() >= 2
    assert store.get_today_completed_count(job_kind=store.JOB_KIND_MASTER) == 1
    assert store.get_today_completed_count(job_kind=store.JOB_KIND_SUB) == 1


def test_scheduler_setup_registers_cost_efficiency_alert_job(tmp_path: Path):
    """notifier+job_store가 있으면 발행 효율 경보 잡이 등록되어야 한다."""
    store = build_store(tmp_path, "scheduler_cost_job.db")

    class NotifierStub:
        enabled = True

        async def send_message(self, message: str) -> bool:
            del message
            return True

    scheduler = SchedulerService(
        job_store=store,
        notifier=NotifierStub(),  # type: ignore[arg-type]
    )
    scheduler.setup_scheduler()
    assert scheduler._scheduler is not None

    if hasattr(scheduler._scheduler, "get_job"):
        assert scheduler._scheduler.get_job("cost_efficiency_alert") is not None
    else:
        assert "cost_efficiency_alert" in scheduler._scheduler.jobs


def test_scheduler_setup_registers_feedback_rule_maintenance_job(tmp_path: Path):
    """notifier+job_store가 있으면 피드백 규칙 유지보수 잡이 등록되어야 한다."""
    store = build_store(tmp_path, "scheduler_feedback_maintenance.db")

    class NotifierStub:
        enabled = True

        async def send_message(self, message: str) -> bool:
            del message
            return True

    scheduler = SchedulerService(
        job_store=store,
        notifier=NotifierStub(),  # type: ignore[arg-type]
    )
    scheduler.setup_scheduler()
    assert scheduler._scheduler is not None

    if hasattr(scheduler._scheduler, "get_job"):
        assert scheduler._scheduler.get_job("feedback_rule_maintenance") is not None
    else:
        assert "feedback_rule_maintenance" in scheduler._scheduler.jobs


def test_scheduler_setup_registers_vlm_sync_jobs(tmp_path: Path):
    """job_store가 있으면 모델/매크로 동기화 잡이 등록되어야 한다."""
    store = build_store(tmp_path, "scheduler_vlm_jobs.db")
    scheduler = SchedulerService(job_store=store)
    scheduler.setup_scheduler()
    assert scheduler._scheduler is not None

    expected_job_ids = {
        "text_model_discovery_sync",
        "vlm_discovery_sync",
        "vlm_pricing_sync",
        "vlm_validation_sync",
        "macro_source_sync",
    }
    if hasattr(scheduler._scheduler, "get_job"):
        for job_id in expected_job_ids:
            assert scheduler._scheduler.get_job(job_id) is not None
    else:
        for job_id in expected_job_ids:
            assert job_id in scheduler._scheduler.jobs


def test_cost_efficiency_alert_sends_once_per_day(tmp_path: Path):
    """오늘 호출량 임계치 이상 + 발행 0건이면 하루 1회만 경보를 전송해야 한다."""
    store = build_store(tmp_path, "scheduler_cost_alert_once.db")
    sent_messages: List[str] = []

    class NotifierStub:
        enabled = True

        async def send_message(self, message: str) -> bool:
            sent_messages.append(message)
            return True

    scheduler = SchedulerService(
        job_store=store,
        notifier=NotifierStub(),  # type: ignore[arg-type]
    )
    assert store.schedule_job(
        job_id="cost-alert-job",
        title="비용 경보 테스트",
        seed_keywords=["cost", "alert"],
        platform="naver",
        persona_id="P1",
        scheduled_at=now_utc(),
    )
    store.record_job_metric(
        "cost-alert-job",
        metric_type="quality_step",
        status="ok",
        input_tokens=10_000,
        output_tokens=2_000,
        provider="deepseek",
        detail={"calls": 12},
    )

    asyncio.run(scheduler._run_cost_efficiency_alert())
    asyncio.run(scheduler._run_cost_efficiency_alert())

    assert len(sent_messages) == 1
    assert "발행 효율 경보" in sent_messages[0]
    assert "LLM 호출: 12건" in sent_messages[0]
    assert store.get_system_setting("scheduler_last_cost_alert_date", "") == scheduler._today_key()


def test_cost_efficiency_alert_ignores_old_llm_metrics(tmp_path: Path):
    """경보 호출량은 전체 누적이 아니라 오늘 KST 생성 호출만 집계해야 한다."""
    store = build_store(tmp_path, "scheduler_cost_alert_old_metrics.db")
    sent_messages: List[str] = []

    class NotifierStub:
        enabled = True

        async def send_message(self, message: str) -> bool:
            sent_messages.append(message)
            return True

    scheduler = SchedulerService(
        job_store=store,
        notifier=NotifierStub(),  # type: ignore[arg-type]
    )
    scheduler._get_now_local = lambda: datetime(2026, 6, 6, 14, 30, 0)  # type: ignore[assignment]
    assert store.schedule_job(
        job_id="old-cost-alert-job",
        title="오래된 비용 경보 테스트",
        seed_keywords=["old", "cost"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-06-04T15:00:00Z",
    )

    with store.connection() as conn:
        conn.execute(
            """
            INSERT INTO job_metrics (
                job_id,
                metric_type,
                status,
                input_tokens,
                output_tokens,
                provider,
                detail_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old-cost-alert-job",
                "quality_step",
                "ok",
                10_000,
                2_000,
                "deepseek",
                json.dumps({"calls": 300}),
                "2026-06-04T15:00:00Z",
            ),
        )

    asyncio.run(scheduler._run_cost_efficiency_alert())

    assert sent_messages == []
