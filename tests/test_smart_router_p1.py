import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.automation.job_store import Job, JobConfig, JobStore
from modules.automation.scheduler_service import SchedulerService
from modules.llm.llm_router import LLMRouter


def _build_store(tmp_path: Path, name: str = "smart_router_p1.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig())


def _build_job(job_id: str, category: str) -> Job:
    return Job(
        job_id=job_id,
        status="queued",
        title="테스트 포스트",
        seed_keywords=["테스트", "자동화"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-02-24T00:00:00Z",
        category=category,
    )


def test_model_performance_log_and_summary(tmp_path: Path):
    """모델 성능 로그가 저장되고 요약 집계가 가능해야 한다."""
    store = _build_store(tmp_path, "performance.db")

    store.record_model_performance(
        model_id="deepseek-chat",
        provider="deepseek",
        topic_mode="it",
        quality_score=91.0,
        cost_won=8.2,
        is_free_model=False,
        slot_type="main",
        post_id="https://blog.naver.com/test/1",
        measured_at="2026-02-24T01:00:00Z",
    )
    store.record_model_performance(
        model_id="deepseek-chat",
        provider="deepseek",
        topic_mode="it",
        quality_score=89.0,
        cost_won=8.0,
        is_free_model=False,
        slot_type="main",
        post_id="https://blog.naver.com/test/2",
        measured_at="2026-02-24T02:00:00Z",
    )

    summary = store.get_model_performance_summary(
        since="2026-02-24T00:00:00Z",
        slot_types=["main"],
    )
    assert summary
    assert summary[0]["model_id"] == "deepseek-chat"
    assert summary[0]["samples"] == 2
    assert summary[0]["avg_quality_score"] >= 89.0


def test_weekly_competition_state_machine(tmp_path: Path):
    """주간 경쟁 상태가 testing -> champion_ops 로 전이되어야 한다."""
    store = _build_store(tmp_path, "competition_state.db")
    store.set_system_setting(
        "router_text_api_keys",
        json.dumps(
            {
                "qwen": "qwen-test-key",
                "deepseek": "deepseek-test-key",
                "groq": "groq-test-key",
            },
            ensure_ascii=False,
        ),
    )

    scheduler = SchedulerService(job_store=store, timezone_name="Asia/Seoul")
    monday = datetime(2026, 2, 23, 0, 6, tzinfo=timezone(timedelta(hours=9)))
    scheduler._get_now_local = lambda: monday  # type: ignore[assignment]
    asyncio.run(scheduler._run_weekly_model_competition())

    week_start = "2026-02-23"
    state = store.get_weekly_competition_state(week_start)
    assert state is not None
    assert state["phase"] == "testing"
    assert store.get_system_setting("router_competition_phase", "") == "testing"
    assert store.get_system_setting("router_shadow_mode", "") == "true"

    challenger_model = store.get_system_setting("router_challenger_model", "")
    champion_model = store.get_system_setting("router_champion_model", "")
    assert challenger_model
    assert champion_model

    store.record_model_performance(
        model_id=champion_model,
        provider="deepseek",
        topic_mode="it",
        quality_score=82.0,
        cost_won=9.0,
        is_free_model=False,
        slot_type="shadow",
        measured_at="2026-02-24T02:00:00Z",
    )
    store.record_model_performance(
        model_id=challenger_model,
        provider="qwen",
        topic_mode="it",
        quality_score=91.0,
        cost_won=9.5,
        is_free_model=False,
        slot_type="shadow",
        measured_at="2026-02-24T03:00:00Z",
    )

    thursday = datetime(2026, 2, 26, 0, 6, tzinfo=timezone(timedelta(hours=9)))
    scheduler._get_now_local = lambda: thursday  # type: ignore[assignment]
    asyncio.run(scheduler._run_weekly_model_competition())

    promoted = store.get_weekly_competition_state(week_start)
    assert promoted is not None
    assert promoted["phase"] == "champion_ops"
    assert store.get_system_setting("router_competition_phase", "") == "champion_ops"
    assert store.get_system_setting("router_shadow_mode", "") == "false"
    assert store.get_system_setting("router_champion_model", "") == challenger_model


def test_llm_router_applies_champion_and_challenger_by_slot(tmp_path: Path):
    """작업 슬롯(main/shadow)에 따라 라우터 모델이 달라져야 한다."""
    store = _build_store(tmp_path, "router_slot.db")
    store.set_system_setting(
        "router_text_api_keys",
        json.dumps(
            {
                "qwen": "qwen-test-key",
                "deepseek": "deepseek-test-key",
            },
            ensure_ascii=False,
        ),
    )
    store.set_system_setting("fallback_category", "다양한 생각들")
    store.set_system_setting("router_competition_phase", "champion_ops")
    store.set_system_setting("router_champion_model", "deepseek-chat")
    store.set_system_setting("router_challenger_model", "qwen-plus")

    router = LLMRouter(job_store=store)
    main_plan = router.build_generation_plan_for_job(job=_build_job("main-job", "IT 자동화"))
    shadow_plan = router.build_generation_plan_for_job(job=_build_job("shadow-job", "다양한 생각들"))

    assert main_plan["quality_step"]["model"] == "deepseek-chat"
    assert main_plan["competition"]["slot_type"] == "main"
    assert shadow_plan["quality_step"]["model"] == "qwen-plus"
    assert shadow_plan["competition"]["slot_type"] == "challenger"


def test_llm_router_topic_specialist_overrides_champion(tmp_path: Path):
    """topic_mode 이력 10건 이상이면 전문화 모델을 우선 선택해야 한다."""
    store = _build_store(tmp_path, "router_specialist.db")
    store.set_system_setting(
        "router_text_api_keys",
        json.dumps(
            {
                "qwen": "qwen-test-key",
                "deepseek": "deepseek-test-key",
            },
            ensure_ascii=False,
        ),
    )
    store.set_system_setting("fallback_category", "다양한 생각들")
    store.set_system_setting("router_competition_phase", "champion_ops")
    store.set_system_setting("router_champion_model", "deepseek-chat")
    store.set_system_setting("router_challenger_model", "qwen-plus")

    for index in range(10):
        store.record_model_performance(
            model_id="qwen-plus",
            provider="qwen",
            topic_mode="it",
            quality_score=90.0 + (index * 0.1),
            cost_won=8.0,
            is_free_model=False,
            slot_type="main",
            measured_at=f"2026-02-{10 + index:02d}T01:00:00Z",
        )

    router = LLMRouter(job_store=store)
    main_plan = router.build_generation_plan_for_job(job=_build_job("it-specialist-job", "IT 자동화"))

    assert main_plan["quality_step"]["model"] == "qwen-plus"
    assert main_plan["competition"]["slot_type"] == "main_specialist"
