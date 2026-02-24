import asyncio
from pathlib import Path

from modules.automation.job_store import JobConfig, JobStore
from modules.collectors.metrics_collector import MetricsCollector
from server.routers.stats import _build_metrics


def _build_store(tmp_path: Path, name: str = "smart_router_p2.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig())


def test_metrics_collector_records_traffic_feedback(tmp_path: Path):
    """조회수 수집 시 naver_traffic 성능 로그가 적재되어야 한다."""
    store = _build_store(tmp_path, "traffic_feedback.db")
    scheduled_at = "2026-02-24T00:00:00Z"
    assert store.schedule_job(
        job_id="traffic-job",
        title="트래픽 피드백 테스트",
        seed_keywords=["IT", "자동화"],
        platform="naver",
        persona_id="P2",
        scheduled_at=scheduled_at,
        category="IT 자동화",
    )
    claimed = store.claim_due_jobs(limit=1, now_override=scheduled_at)
    assert claimed
    store.complete_job(
        "traffic-job",
        "https://blog.naver.com/demo/1234",
        quality_snapshot={"score": 88},
        seo_snapshot={"provider_used": "deepseek", "provider_model": "deepseek-chat", "topic_mode": "it"},
    )

    collector = MetricsCollector(db_path=store.db_path, min_age_hours=0, max_age_days=365)

    async def fake_fetch_views(_url: str) -> int:
        return 1234

    collector.fetch_naver_views = fake_fetch_views  # type: ignore[assignment]
    post_payload = {
        "job_id": "traffic-job",
        "title": "트래픽 피드백 테스트",
        "result_url": "https://blog.naver.com/demo/1234",
        "published_at": "2026-02-24T00:00:00Z",
        "seo_snapshot": '{"provider_used":"deepseek","provider_model":"deepseek-chat","topic_mode":"it"}',
        "quality_snapshot": '{"score":88}',
        "tags": "[]",
    }
    asyncio.run(collector.collect_one(post_payload))

    with store.connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM model_performance_log
            WHERE feedback_source = 'naver_traffic'
            """
        ).fetchone()
    assert row is not None
    assert int(row["cnt"]) >= 1


def test_stats_dashboard_includes_score_per_won_trend(tmp_path: Path):
    """대시보드 메트릭이 원당 품질 추세 데이터를 반환해야 한다."""
    store = _build_store(tmp_path, "trend_stats.db")
    for day in range(1, 4):
        store.record_model_performance(
            model_id="deepseek-chat",
            provider="deepseek",
            topic_mode="it",
            quality_score=85.0 + day,
            cost_won=7.0 + day,
            is_free_model=False,
            slot_type="main",
            measured_at=f"2026-02-0{day}T00:00:00Z",
        )

    metrics = _build_metrics(store)
    assert isinstance(metrics.score_per_won_trend, list)
    assert len(metrics.score_per_won_trend) >= 1


def test_stats_dashboard_includes_champion_history(tmp_path: Path):
    """대시보드 메트릭이 챔피언 이력 데이터를 반환해야 한다."""
    store = _build_store(tmp_path, "champion_history_stats.db")
    store.record_champion_history(
        week_start="2026-02-24",
        champion_model="deepseek-chat",
        challenger_model="qwen-plus",
        avg_champion_score=91.2,
        topic_mode_scores={"it": 92.0, "finance": 89.5},
        cost_won=13.4,
        early_terminated=False,
        shadow_only=True,
    )

    metrics = _build_metrics(store)
    assert isinstance(metrics.champion_history, list)
    assert len(metrics.champion_history) == 1
    first = metrics.champion_history[0]
    assert first["champion_model"] == "deepseek-chat"
    assert first["challenger_model"] == "qwen-plus"


def test_traffic_feedback_100_samples_requires_manual_strong_mode(tmp_path: Path):
    """트래픽 100편 이상이어도 strong 모드가 꺼져 있으면 30%로 유지되어야 한다."""
    store = _build_store(tmp_path, "traffic_weight_mode.db")
    for index in range(100):
        store.record_model_performance(
            model_id=f"model-{index}",
            provider="deepseek",
            topic_mode="it",
            quality_score=85.0,
            cost_won=10.0,
            is_free_model=False,
            slot_type="main",
            feedback_source="naver_traffic",
            measured_at="2026-02-24T00:00:00Z",
        )

    collector = MetricsCollector(db_path=store.db_path, min_age_hours=0, max_age_days=365)
    assert collector._topic_feedback_weight("it") == 0.3

    store.set_system_setting("router_traffic_feedback_strong_mode", "true")
    assert collector._topic_feedback_weight("it") == 0.5
