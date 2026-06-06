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
    store.set_system_setting("router_eval_claimed_date", "2026-02-23")
    store.set_system_setting("router_eval_claimed_job_id", "stale-job")

    for i in range(10):
        store.record_model_performance(
            model_id="gemini-2.0-flash",
            provider="gemini",
            topic_mode="cafe",
            quality_score=85.0,
            cost_won=10.0,
            is_free_model=False,
            slot_type="eval",
            measured_at=f"2026-05-{10 + i:02d}T01:00:00Z",
        )

    scheduler = SchedulerService(job_store=store, timezone_name="Asia/Seoul")
    asyncio.run(scheduler._run_daily_model_eval())

    selected = store.get_system_setting("router_eval_model_today", "")
    assert selected == "gpt-4.1-mini"
    assert store.get_system_setting("router_eval_claimed_date", "") == ""
    assert store.get_system_setting("router_eval_claimed_job_id", "") == ""


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
            measured_at=f"2026-05-{(i % 28) + 1:02d}T03:00:00Z",
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
    store.set_system_setting("router_auto_champion_switch_enabled", "true")
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
            measured_at=f"2026-05-{10 + i:02d}T02:00:00Z",
        )
        store.record_model_performance(
            model_id="gpt-4.1-mini",
            provider="openai",
            topic_mode="it",
            quality_score=89.0,
            cost_won=10.0,
            is_free_model=False,
            slot_type="main",
            measured_at=f"2026-05-{10 + i:02d}T03:00:00Z",
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
    store.set_system_setting("router_auto_champion_switch_enabled", "true")
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
            measured_at=f"2026-05-{10 + i:02d}T01:00:00Z",
        )
        store.record_model_performance(
            model_id="gpt-4.1-mini",
            provider="openai",
            topic_mode="it",
            quality_score=92.0,
            cost_won=12.0,
            is_free_model=False,
            slot_type="main",
            measured_at=f"2026-05-{10 + i:02d}T04:00:00Z",
        )

    scheduler = SchedulerService(job_store=store, timezone_name="Asia/Seoul")
    asyncio.run(scheduler._run_auto_champion_switch())

    assert store.get_system_setting("router_champion_model", "") == "gpt-4.1-mini"


def test_auto_champion_switch_defaults_to_recommendation_only(tmp_path: Path):
    """기본값에서는 챔피언을 자동 교체하지 않고 추천만 기록해야 한다."""
    store = _build_store(tmp_path, "auto_champion_recommend_only.db")
    store.set_system_setting("router_strategy_mode", "quality")
    store.set_system_setting("router_eval_min_samples", "3")
    store.set_system_setting("router_champion_switch_threshold", "2.0")
    store.set_system_setting("router_champion_model", "gemini-2.0-flash")
    store.set_system_setting(
        "router_registered_models",
        json.dumps(
            [
                {"model_id": "gemini-2.0-flash", "provider": "gemini", "active": True},
                {"model_id": "deepseek-chat", "provider": "deepseek", "active": True},
            ],
            ensure_ascii=False,
        ),
    )

    for i in range(4):
        store.record_model_performance(
            model_id="gemini-2.0-flash",
            provider="gemini",
            topic_mode="it",
            quality_score=84.0,
            cost_won=10.0,
            is_free_model=False,
            slot_type="main",
            measured_at=f"2026-05-{10 + i:02d}T01:00:00Z",
        )
        store.record_model_performance(
            model_id="deepseek-chat",
            provider="deepseek",
            topic_mode="it",
            quality_score=90.0,
            cost_won=8.0,
            is_free_model=False,
            slot_type="main",
            measured_at=f"2026-05-{10 + i:02d}T02:00:00Z",
        )

    scheduler = SchedulerService(job_store=store, timezone_name="Asia/Seoul")
    asyncio.run(scheduler._run_auto_champion_switch())

    assert store.get_system_setting("router_champion_model", "") == "gemini-2.0-flash"
    assert store.get_system_setting("router_champion_recommendation_model", "") == "deepseek-chat"


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
            measured_at=f"2026-05-{10 + index:02d}T01:00:00Z",
        )

    router = LLMRouter(job_store=store)
    main_plan = router.build_generation_plan_for_job(job=_build_job("it-specialist-job", "IT 자동화"))

    assert main_plan["quality_step"]["model"] == "qwen-plus"
    assert main_plan["competition"]["slot_type"] == "main_specialist"


def test_parser_chain_respects_provider_level_inactive_registry(tmp_path: Path):
    """provider 등록 모델이 전부 비활성이면 미등록 alias도 파서 체인에서 제외한다."""

    store = _build_store(tmp_path, "router_inactive_provider.db")
    store.set_system_setting(
        "router_text_api_keys",
        json.dumps(
            {
                "qwen": "qwen-key",
                "deepseek": "deepseek-key",
                "groq": "groq-key",
            },
            ensure_ascii=False,
        ),
    )
    store.set_system_setting(
        "router_registered_models",
        json.dumps(
            [
                {"model_id": "qwen-plus", "provider": "qwen", "active": True},
                {"model_id": "llama-3.3-70b-versatile", "provider": "groq", "active": True},
                {"model_id": "deepseek-chat", "provider": "deepseek", "active": False},
                {"model_id": "deepseek-reasoner", "provider": "deepseek", "active": False},
            ],
            ensure_ascii=False,
        ),
    )

    router = LLMRouter(job_store=store)
    chain = router.build_parser_chain()

    assert chain
    assert not any(item["provider"] == "deepseek" for item in chain)


def test_eval_slot_is_claimed_once_per_day_even_if_not_published(tmp_path: Path):
    """eval 슬롯은 당일 1회만 배정되고, 발행 성공 로그가 없어도 재배정되지 않아야 한다."""
    store = _build_store(tmp_path, "eval_claim_once.db")
    store.set_system_setting(
        "router_text_api_keys",
        json.dumps({"openai": "openai-test-key"}, ensure_ascii=False),
    )
    store.set_system_setting("router_eval_model_today", "gpt-4.1-mini")

    router = LLMRouter(job_store=store)
    first_plan = router.build_generation_plan_for_job(job=_build_job("eval-claim-1", "IT 자동화"))
    second_plan = router.build_generation_plan_for_job(job=_build_job("eval-claim-2", "IT 자동화"))

    assert first_plan["competition"]["slot_type"] == "eval"
    assert second_plan["competition"]["slot_type"] != "eval"
    assert store.get_system_setting("router_eval_claimed_job_id", "") == "eval-claim-1"


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


def test_cost_strict_blocks_paid_champion_and_eval(tmp_path: Path):
    """가성비 strict 모드에서는 유료 champion/eval 모델을 무료 품질 모델로 강등해야 한다."""
    store = _build_store(tmp_path, "cost_strict_block_paid.db")
    store.set_system_setting("router_strategy_mode", "cost")
    store.set_system_setting("router_cost_strict_mode", "true")
    store.set_system_setting("router_cost_free_only_fallback", "true")
    store.set_system_setting("router_cost_max_fallback_usd_per_1m", "0.6")
    store.set_system_setting(
        "router_text_api_keys",
        json.dumps(
            {
                "gemini": "gemini-test-key",
                "nvidia": "nvidia-test-key",
                "groq": "groq-test-key",
                "cerebras": "cerebras-test-key",
            },
            ensure_ascii=False,
        ),
    )
    store.set_system_setting("router_champion_model", "gemini-2.0-flash")
    store.set_system_setting("router_eval_model_today", "gemini-2.5-flash")
    store.set_system_setting("router_eval_claimed_date", "")
    store.set_system_setting("router_eval_claimed_job_id", "")

    router = LLMRouter(job_store=store)
    plan = router.build_generation_plan_for_job(job=_build_job("cost-strict-guard", "IT 자동화"))

    assert plan["quality_step"]["provider"] in {"nvidia", "groq", "cerebras"}
    assert plan["competition"]["slot_type"] != "eval"
    fallback_providers = [str(item.get("provider", "")) for item in plan["quality_step"].get("fallback_chain", [])]
    assert "gemini" not in fallback_providers


def test_pipeline_records_quality_step_per_provider_breakdown(tmp_path: Path):
    """quality_step by_provider가 있으면 mixed 집계 대신 provider별 row를 저장해야 한다."""
    store = _build_store(tmp_path, "quality_breakdown_metrics.db")
    pipeline = PipelineService(
        job_store=store,
        publisher=_DummyPublisher(),
        generate_fn=_dummy_generate,
    )
    store.schedule_job(
        job_id="breakdown-job",
        title="breakdown",
        seed_keywords=["테스트"],
        platform="naver",
        persona_id="P1",
        scheduled_at="2026-03-03T00:00:00Z",
    )
    token_usage = {
        "quality_step": {
            "input_tokens": 1500,
            "output_tokens": 800,
            "calls": 3,
            "provider": "mixed",
            "model": "mixed",
            "by_provider": {
                "nvidia": {
                    "input_tokens": 900,
                    "output_tokens": 300,
                    "calls": 2,
                    "model": "meta/llama-3.3-70b-instruct",
                },
                "gemini": {
                    "input_tokens": 600,
                    "output_tokens": 500,
                    "calls": 1,
                    "model": "gemini-2.0-flash",
                },
            },
        },
        "parser": {"input_tokens": 0, "output_tokens": 0, "calls": 0, "provider": "", "model": "", "by_provider": {}},
        "voice_step": {
            "input_tokens": 100,
            "output_tokens": 50,
            "calls": 1,
            "provider": "nvidia",
            "model": "meta/llama-3.3-70b-instruct",
            "by_provider": {
                "nvidia": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "calls": 1,
                    "model": "meta/llama-3.3-70b-instruct",
                }
            },
        },
    }

    pipeline._record_llm_usage_metrics(job_id="breakdown-job", token_usage=token_usage)  # noqa: SLF001

    with store.connection() as conn:
        rows = conn.execute(
            """
            SELECT provider, input_tokens, output_tokens
            FROM job_metrics
            WHERE job_id = ? AND metric_type = 'quality_step'
            ORDER BY provider
            """,
            ("breakdown-job",),
        ).fetchall()

    assert len(rows) == 2
    assert [str(row["provider"]) for row in rows] == ["gemini", "nvidia"]


def test_auto_champion_switch_respects_strategy_balanced(tmp_path: Path):
    """balanced 모드에서 품질 threshold 충족 시 챔피언이 교체되어야 한다."""
    store = _build_store(tmp_path, "auto_champion_balanced.db")
    store.set_system_setting("router_strategy_mode", "balanced")
    store.set_system_setting("router_eval_min_samples", "5")
    store.set_system_setting("router_champion_switch_threshold", "2.0")
    store.set_system_setting("router_champion_model", "gemini-2.0-flash")
    store.set_system_setting("router_auto_champion_switch_enabled", "true")
    store.set_system_setting(
        "router_registered_models",
        json.dumps(
            [
                {"model_id": "gemini-2.0-flash", "provider": "gemini", "active": True},
                {"model_id": "deepseek-chat", "provider": "deepseek", "active": True},
            ],
            ensure_ascii=False,
        ),
    )

    for i in range(6):
        store.record_model_performance(
            model_id="gemini-2.0-flash",
            provider="gemini",
            topic_mode="it",
            quality_score=85.0,
            cost_won=14.0,
            is_free_model=False,
            slot_type="main",
            measured_at=f"2026-05-{10 + i:02d}T01:00:00Z",
        )
        store.record_model_performance(
            model_id="deepseek-chat",
            provider="deepseek",
            topic_mode="it",
            quality_score=90.0,  # +5점: quality threshold 2.0 초과
            cost_won=9.0,
            is_free_model=False,
            slot_type="main",
            measured_at=f"2026-05-{10 + i:02d}T02:00:00Z",
        )

    scheduler = SchedulerService(job_store=store, timezone_name="Asia/Seoul")
    asyncio.run(scheduler._run_auto_champion_switch())

    assert store.get_system_setting("router_champion_model", "") == "deepseek-chat"


def test_champion_history_recorded_on_switch(tmp_path: Path):
    """챔피언 교체 후 champion_history 테이블에 이력이 자동 기록되어야 한다."""
    store = _build_store(tmp_path, "champion_history_auto.db")
    store.set_system_setting("router_strategy_mode", "quality")
    store.set_system_setting("router_eval_min_samples", "3")
    store.set_system_setting("router_champion_switch_threshold", "2.0")
    store.set_system_setting("router_champion_model", "gemini-2.0-flash")
    store.set_system_setting("router_auto_champion_switch_enabled", "true")
    store.set_system_setting(
        "router_registered_models",
        json.dumps(
            [
                {"model_id": "gemini-2.0-flash", "provider": "gemini", "active": True},
                {"model_id": "deepseek-chat", "provider": "deepseek", "active": True},
            ],
            ensure_ascii=False,
        ),
    )

    for i in range(4):
        store.record_model_performance(
            model_id="gemini-2.0-flash",
            provider="gemini",
            topic_mode="cafe",
            quality_score=82.0,
            cost_won=12.0,
            is_free_model=False,
            slot_type="main",
            measured_at=f"2026-05-{10 + i:02d}T01:00:00Z",
        )
        store.record_model_performance(
            model_id="deepseek-chat",
            provider="deepseek",
            topic_mode="cafe",
            quality_score=88.0,  # +6점: threshold 2.0 초과
            cost_won=8.0,
            is_free_model=False,
            slot_type="main",
            measured_at=f"2026-05-{10 + i:02d}T02:00:00Z",
        )

    scheduler = SchedulerService(job_store=store, timezone_name="Asia/Seoul")
    asyncio.run(scheduler._run_auto_champion_switch())

    # 챔피언 교체 확인
    assert store.get_system_setting("router_champion_model", "") == "deepseek-chat"

    # champion_history 자동 기록 확인
    history = store.list_champion_history(limit=1)
    assert len(history) == 1
    assert history[0]["champion_model"] == "deepseek-chat"
    assert history[0]["challenger_model"] == "gemini-2.0-flash"
    assert history[0]["avg_champion_score"] >= 88.0


def test_cost_estimation_includes_v2_stages(tmp_path: Path):
    """pre_analysis 및 sentence_polish 토큰이 비용 추정에 포함되어야 한다."""
    store = _build_store(tmp_path, "cost_v2_stages.db")
    pipeline = PipelineService(
        job_store=store,
        publisher=_DummyPublisher(),
        generate_fn=_dummy_generate,
    )

    token_usage_v2 = {
        "parser": {"input_tokens": 100, "output_tokens": 50, "calls": 1},
        "pre_analysis": {"input_tokens": 300, "output_tokens": 200, "calls": 1},
        "quality_step": {"input_tokens": 1500, "output_tokens": 800, "calls": 1},
        "voice_step": {"input_tokens": 2000, "output_tokens": 1200, "calls": 1},
        "sentence_polish": {"input_tokens": 400, "output_tokens": 300, "calls": 1},
    }
    token_usage_v1 = {
        "parser": {"input_tokens": 100, "output_tokens": 50, "calls": 1},
        "quality_step": {"input_tokens": 1500, "output_tokens": 800, "calls": 1},
        "voice_step": {"input_tokens": 2000, "output_tokens": 1200, "calls": 1},
    }

    cost_v2 = pipeline._estimate_text_cost_won("deepseek", token_usage_v2)  # noqa: SLF001
    cost_v1 = pipeline._estimate_text_cost_won("deepseek", token_usage_v1)  # noqa: SLF001

    # V2 스테이지(pre_analysis + sentence_polish)가 포함되면 비용이 더 높아야 함
    assert cost_v2 > cost_v1
