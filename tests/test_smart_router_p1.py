import asyncio
import json
from pathlib import Path

from modules.automation.job_store import Job, JobConfig, JobStore
from modules.automation.pipeline_service import PipelineService
from modules.automation.scheduler_service import SchedulerService
from modules.llm.llm_router import LLMRouter


class _DummyPublisher:
    async def publish(self, **kwargs):  # noqa: ANN003
        del kwargs
        raise RuntimeError("not used in this test")


async def _dummy_generate(_job: Job) -> dict:
    return {}


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
    """모델 성능 로그가 저장되고 eval/main 집계가 가능해야 한다."""
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
        slot_type="eval",
        post_id="https://blog.naver.com/test/2",
        measured_at="2026-02-24T02:00:00Z",
    )

    summary = store.get_model_performance_summary(
        since="2026-02-24T00:00:00Z",
        slot_types=["main", "eval"],
    )
    assert summary
    assert summary[0]["model_id"] == "deepseek-chat"
    assert summary[0]["samples"] == 2

    eval_summary = store.get_model_performance_summary(
        since="2026-02-24T00:00:00Z",
        slot_types=["eval"],
    )
    assert eval_summary
    assert eval_summary[0]["samples"] == 1


def test_daily_eval_selects_fewest_samples_model(tmp_path: Path):
    """샘플이 가장 적은 registered 모델이 eval 대상으로 선정되어야 한다."""
    store = _build_store(tmp_path, "daily_eval_fewest.db")
    store.set_system_setting(
        "router_registered_models",
        json.dumps(
            [
                {"model_id": "gemini-2.0-flash", "provider": "gemini", "active": True},
                {"model_id": "gpt-4.1-mini", "provider": "openai", "active": True},
            ],
            ensure_ascii=False,
        ),
    )

    for i in range(10):
        store.record_model_performance(
            model_id="gemini-2.0-flash",
            provider="gemini",
            topic_mode="cafe",
            quality_score=85.0,
            cost_won=10.0,
            is_free_model=False,
            slot_type="eval",
            measured_at=f"2026-02-{10 + i:02d}T01:00:00Z",
        )

    scheduler = SchedulerService(job_store=store, timezone_name="Asia/Seoul")
    asyncio.run(scheduler._run_daily_model_eval())

    selected = store.get_system_setting("router_eval_model_today", "")
    assert selected == "gpt-4.1-mini"


def test_daily_eval_new_model_zero_samples_priority(tmp_path: Path):
    """신규 등록 모델(samples=0)이 기존 모델보다 우선 선택되어야 한다."""
    store = _build_store(tmp_path, "daily_eval_new_model.db")
    store.set_system_setting(
        "router_registered_models",
        json.dumps(
            [
                {"model_id": "deepseek-chat", "provider": "deepseek", "active": True},
                {"model_id": "gemini-2.0-flash", "provider": "gemini", "active": True},
            ],
            ensure_ascii=False,
        ),
    )

    for i in range(100):
        store.record_model_performance(
            model_id="deepseek-chat",
            provider="deepseek",
            topic_mode="it",
            quality_score=84.0,
            cost_won=9.0,
            is_free_model=False,
            slot_type="main",
            measured_at=f"2026-01-{(i % 28) + 1:02d}T03:00:00Z",
        )

    scheduler = SchedulerService(job_store=store, timezone_name="Asia/Seoul")
    asyncio.run(scheduler._run_daily_model_eval())

    selected = store.get_system_setting("router_eval_model_today", "")
    assert selected == "gemini-2.0-flash"


def test_auto_champion_switch_respects_strategy_cost(tmp_path: Path):
    """cost 모드에서 score_per_won이 우수하면 챔피언이 교체되어야 한다."""
    store = _build_store(tmp_path, "auto_champion_cost.db")
    store.set_system_setting("router_strategy_mode", "cost")
    store.set_system_setting("router_eval_min_samples", "5")
    store.set_system_setting("router_champion_switch_threshold", "1.0")
    store.set_system_setting("router_champion_model", "gemini-2.0-flash")
    store.set_system_setting(
        "router_registered_models",
        json.dumps(
            [
                {"model_id": "gemini-2.0-flash", "provider": "gemini", "active": True},
                {"model_id": "gpt-4.1-mini", "provider": "openai", "active": True},
            ],
            ensure_ascii=False,
        ),
    )

    for i in range(6):
        store.record_model_performance(
            model_id="gemini-2.0-flash",
            provider="gemini",
            topic_mode="it",
            quality_score=90.0,
            cost_won=20.0,
            is_free_model=False,
            slot_type="main",
            measured_at=f"2026-02-{10 + i:02d}T02:00:00Z",
        )
        store.record_model_performance(
            model_id="gpt-4.1-mini",
            provider="openai",
            topic_mode="it",
            quality_score=89.0,
            cost_won=10.0,
            is_free_model=False,
            slot_type="main",
            measured_at=f"2026-02-{10 + i:02d}T03:00:00Z",
        )

    scheduler = SchedulerService(job_store=store, timezone_name="Asia/Seoul")
    asyncio.run(scheduler._run_auto_champion_switch())

    assert store.get_system_setting("router_champion_model", "") == "gpt-4.1-mini"


def test_auto_champion_switch_respects_strategy_quality(tmp_path: Path):
    """quality 모드에서 avg_quality가 우수하면 챔피언이 교체되어야 한다."""
    store = _build_store(tmp_path, "auto_champion_quality.db")
    store.set_system_setting("router_strategy_mode", "quality")
    store.set_system_setting("router_eval_min_samples", "5")
    store.set_system_setting("router_champion_switch_threshold", "2.0")
    store.set_system_setting("router_champion_model", "gemini-2.0-flash")
    store.set_system_setting(
        "router_registered_models",
        json.dumps(
            [
                {"model_id": "gemini-2.0-flash", "provider": "gemini", "active": True},
                {"model_id": "gpt-4.1-mini", "provider": "openai", "active": True},
            ],
            ensure_ascii=False,
        ),
    )

    for i in range(6):
        store.record_model_performance(
            model_id="gemini-2.0-flash",
            provider="gemini",
            topic_mode="it",
            quality_score=88.0,
            cost_won=8.0,
            is_free_model=False,
            slot_type="main",
            measured_at=f"2026-02-{10 + i:02d}T01:00:00Z",
        )
        store.record_model_performance(
            model_id="gpt-4.1-mini",
            provider="openai",
            topic_mode="it",
            quality_score=92.0,
            cost_won=12.0,
            is_free_model=False,
            slot_type="main",
            measured_at=f"2026-02-{10 + i:02d}T04:00:00Z",
        )

    scheduler = SchedulerService(job_store=store, timezone_name="Asia/Seoul")
    asyncio.run(scheduler._run_auto_champion_switch())

    assert store.get_system_setting("router_champion_model", "") == "gpt-4.1-mini"


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
    store.set_system_setting("router_champion_model", "deepseek-chat")
    store.set_system_setting("router_eval_model_today", "")

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


def test_eval_slot_type_recorded_in_performance_log(tmp_path: Path):
    """eval 모델이 사용되면 model_performance_log에 slot_type='eval'로 기록되어야 한다."""
    store = _build_store(tmp_path, "eval_slot_log.db")
    store.set_system_setting("router_eval_model_today", "gpt-4.1-mini")

    pipeline = PipelineService(
        job_store=store,
        publisher=_DummyPublisher(),
        generate_fn=_dummy_generate,
    )
    job = _build_job("eval-slot-job", "IT 자동화")

    payload = {
        "seo_snapshot": {
            "provider_used": "openai",
            "provider_model": "openai:gpt-4.1-mini",
            "topic_mode": "it",
        },
        "quality_snapshot": {"score": 91.0},
        "llm_token_usage": {},
    }

    assert pipeline._resolve_slot_type(job, payload) == "eval"  # noqa: SLF001
    pipeline._record_model_performance(job=job, payload=payload, post_id="https://blog.naver.com/test/eval")  # noqa: SLF001

    with store.connection() as conn:
        row = conn.execute(
            """
            SELECT slot_type
            FROM model_performance_log
            ORDER BY measured_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert row is not None
    assert str(row["slot_type"]) == "eval"
