"""스케줄러 사이클 실행 책임 분리 모듈."""

from __future__ import annotations

import asyncio
from collections import Counter
import json
import logging
import random
import re
import uuid
from datetime import date, datetime, time as time_obj, timedelta, timezone
from time import perf_counter
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .. import constants
from ..constants import DEFAULT_FALLBACK_CATEGORY
from .time_utils import now_utc, parse_iso

if TYPE_CHECKING:
    from ..collectors.idea_vault_auto_collector import IdeaVaultAutoCollector
    from ..collectors.metrics_collector import MetricsCollector
    from ..seo.feedback_analyzer import FeedbackAnalyzer
    from .job_store import JobStore
    from .notifier import TelegramNotifier
    from .pipeline_service import PipelineService
    from .trend_job_service import TrendJobService
    from .scheduler_service import SchedulerService

logger = logging.getLogger(__name__)

_MULTICHANNEL_SETTING_KEY = "multichannel_enabled"
_IMPLEMENTED_SUB_PLATFORMS = {"naver", "tistory"}

async def cycle_run_startup_catchup(service: "SchedulerService") -> None:
    """시작 시점에 놓친 작업을 보정 실행한다."""
    logger.info("Running startup catch-up")
    await service._run_daily_quota_seed()
    await service._run_draft_prefetch()
    await service._run_daily_target_check()
    await service._run_metrics_collection()
    await service._run_daily_model_eval()
    await service._run_auto_champion_switch()
    await service._run_feedback_analysis()
    await service._run_feedback_rule_maintenance()
    await service._run_text_model_discovery_sync()
    await service._run_vlm_discovery_sync()
    await service._run_vlm_pricing_sync()
    await service._run_vlm_validation_sync()
    await service._run_sub_job_catchup()
    await service._run_sub_job_publish_catchup()



async def cycle_run_idea_vault_auto_collect(service: "SchedulerService") -> None:
    """RSS 피드에서 아이디어를 수집해 idea_vault 에 저장한다 (Track A)."""
    logger.info("Running idea vault auto collect")
    if not service.idea_vault_collector:
        return
    try:
        saved = await service.idea_vault_collector.run_once()
        logger.info("Idea vault auto collect: saved %d items", saved)
    except Exception as exc:
        logger.error("Idea vault auto collect failed: %s", exc)



async def cycle_run_trend_collection(service: "SchedulerService") -> None:
    logger.info("Running trend collection")
    if not service.trend_service:
        return
    try:
        created = service.trend_service.fetch_and_create_jobs()
        logger.info("Trend jobs created: %d", len(created))
    except Exception as exc:
        logger.error("Trend collection failed: %s", exc)



async def cycle_run_metrics_collection(service: "SchedulerService") -> None:
    logger.info("Running metrics collection")
    if not service.metrics_collector:
        return
    if service.job_store:
        today_key = service._today_key()
        last_key = service.job_store.get_system_setting("scheduler_last_metrics_date", "")
        if last_key == today_key:
            logger.debug("Metrics collection skipped: already collected today (%s)", today_key)
            return
    try:
        count = await service.metrics_collector.collect_all_pending()
        if service.job_store:
            service.job_store.set_system_setting("scheduler_last_metrics_date", service._today_key())
        logger.info("Metrics collected: %d", count)
    except Exception as exc:
        logger.error("Metrics collection failed: %s", exc)



async def cycle_run_feedback_analysis(service: "SchedulerService") -> None:
    logger.info("Running feedback analysis")
    if not service.feedback_analyzer:
        return
    if service.job_store:
        week_key = service._week_key()
        last_key = service.job_store.get_system_setting("scheduler_last_feedback_week", "")
        if last_key == week_key:
            logger.debug("Feedback analysis skipped: already analyzed week (%s)", week_key)
            return
    try:
        snapshot = await service.feedback_analyzer.run_analysis(
            platform="naver",
            trigger="scheduled",
            apply_updates=True,
        )
        if service.job_store:
            service.job_store.set_system_setting("scheduler_last_feedback_week", service._week_key())
        if snapshot:
            logger.info("Feedback analysis complete")
    except Exception as exc:
        logger.error("Feedback analysis failed: %s", exc)


async def cycle_run_feedback_rule_maintenance(service: "SchedulerService") -> None:
    """자동 피드백 후보 상태 정리 및 재알림/롤백 평가를 수행한다."""
    if not service.job_store:
        return

    stale_timeout_hours = max(1, int(constants.FEEDBACK_PENDING_TIMEOUT_HOURS))
    snooze_hours = max(1, int(constants.FEEDBACK_SNOOZE_REMIND_HOURS))
    notify_limit = max(1, int(constants.FEEDBACK_CANDIDATE_BATCH_LIMIT))

    stale_snoozed = 0
    reopened = 0
    try:
        stale_snoozed = int(
            service.job_store.auto_snooze_stale_feedback_candidates(
                stale_hours=stale_timeout_hours,
                remind_hours=snooze_hours,
            )
        )
    except Exception as exc:
        logger.warning("Feedback maintenance: stale snooze failed: %s", exc)

    try:
        reopened = int(service.job_store.reopen_due_snoozed_feedback_candidates(limit=notify_limit * 2))
    except Exception as exc:
        logger.warning("Feedback maintenance: reopen snoozed failed: %s", exc)

    notified = 0
    if service.notifier and getattr(service.notifier, "enabled", False):
        list_fn = getattr(service.job_store, "list_feedback_candidates_to_notify", None)
        notify_fn = getattr(service.pipeline_service, "_notify_feedback_candidate", None)
        if callable(list_fn) and callable(notify_fn):
            try:
                pending_candidates = list_fn(limit=notify_limit)
            except Exception:
                pending_candidates = []
            for candidate in pending_candidates:
                try:
                    notify_fn(candidate=candidate)
                    notified += 1
                except Exception:
                    logger.debug("Feedback maintenance: candidate notify skipped", exc_info=True)

    rollback_result = {"evaluated": 0, "kept": 0, "observed": 0, "rolled_back": 0}
    try:
        rollback_result = service.job_store.evaluate_feedback_rule_rollbacks(
            min_posts=int(constants.FEEDBACK_DECISION_MIN_POSTS),
            noise_floor=float(constants.FEEDBACK_NOISE_FLOOR),
            keep_threshold=float(constants.FEEDBACK_KEEP_THRESHOLD),
        )
    except Exception as exc:
        logger.warning("Feedback maintenance: rollback evaluation failed: %s", exc)

    if any([
        stale_snoozed > 0,
        reopened > 0,
        notified > 0,
        int(rollback_result.get("rolled_back", 0)) > 0,
    ]):
        logger.info(
            "Feedback maintenance complete",
            extra={
                "stale_snoozed": stale_snoozed,
                "reopened": reopened,
                "notified": notified,
                "rollback": rollback_result,
            },
        )


def _is_vlm_sync_due(service: "SchedulerService", *, setting_key: str, stale_hours: int) -> bool:
    """마지막 동기화 시각 기반으로 실행 필요 여부를 계산한다."""
    if not service.job_store:
        return False
    raw = str(service.job_store.get_system_setting(setting_key, "")).strip()
    if not raw:
        return True
    try:
        last_run = parse_iso(raw)
    except Exception:
        return True
    elapsed = datetime.now(timezone.utc) - last_run
    return elapsed >= timedelta(hours=max(1, int(stale_hours)))


async def cycle_run_vlm_discovery_sync(service: "SchedulerService") -> None:
    """VLM 카탈로그를 공식 매트릭스 기준으로 동기화한다."""
    if not service.job_store:
        return

    stale_hours = max(1, int(constants.VLM_DISCOVERY_SYNC_STALE_HOURS))
    if not _is_vlm_sync_due(
        service,
        setting_key="vlm_last_discovery_sync_at",
        stale_hours=stale_hours,
    ):
        logger.debug("VLM discovery sync skipped: stale window not reached")
        return

    try:
        from .vlm_discovery_worker import VLMDiscoveryWorker

        worker = VLMDiscoveryWorker(job_store=service.job_store)
        stats = worker.sync_catalog()
        logger.info(
            "VLM discovery sync complete: inserted=%d updated=%d unchanged=%d deprecated=%d",
            int(stats.get("inserted", 0)),
            int(stats.get("updated", 0)),
            int(stats.get("unchanged", 0)),
            int(stats.get("deprecated", 0)),
        )
    except Exception as exc:
        logger.error("VLM discovery sync failed: %s", exc)


async def cycle_run_text_model_discovery_sync(service: "SchedulerService") -> None:
    """텍스트 모델 공식 카탈로그를 동기화한다."""
    if not service.job_store:
        return

    stale_hours = max(1, int(constants.TEXT_MODEL_DISCOVERY_SYNC_STALE_HOURS))
    if not _is_vlm_sync_due(
        service,
        setting_key="text_model_last_discovery_sync_at",
        stale_hours=stale_hours,
    ):
        logger.debug("Text model discovery sync skipped: stale window not reached")
        return

    try:
        from .text_model_discovery_worker import TextModelDiscoveryWorker

        worker = TextModelDiscoveryWorker(job_store=service.job_store)
        stats = worker.sync_catalog()
        logger.info(
            "Text model discovery sync complete: inserted=%d updated=%d unchanged=%d deprecated=%d failures=%d registered_added=%d",
            int(stats.get("inserted", 0)),
            int(stats.get("updated", 0)),
            int(stats.get("unchanged", 0)),
            int(stats.get("deprecated", 0)),
            int(stats.get("source_failures", 0)),
            int(stats.get("registered_added", 0)),
        )
    except Exception as exc:
        logger.error("Text model discovery sync failed: %s", exc)


async def cycle_run_vlm_pricing_sync(service: "SchedulerService") -> None:
    """VLM 카탈로그 단가/환율 정보를 동기화한다."""
    if not service.job_store:
        return

    stale_hours = max(1, int(constants.VLM_PRICING_SYNC_STALE_HOURS))
    if not _is_vlm_sync_due(
        service,
        setting_key="vlm_last_price_sync_at",
        stale_hours=stale_hours,
    ):
        logger.debug("VLM pricing sync skipped: stale window not reached")
        return

    raw_rate = str(
        service.job_store.get_system_setting(
            "vlm_usd_to_krw",
            str(constants.VLM_DEFAULT_USD_TO_KRW),
        )
    ).strip()
    try:
        usd_to_krw = float(raw_rate)
    except ValueError:
        usd_to_krw = float(constants.VLM_DEFAULT_USD_TO_KRW)
    if usd_to_krw <= 0:
        usd_to_krw = float(constants.VLM_DEFAULT_USD_TO_KRW)

    try:
        from .vlm_pricing_worker import VLMPricingWorker

        worker = VLMPricingWorker(job_store=service.job_store, usd_to_krw=usd_to_krw)
        stats = worker.sync_prices()
        logger.info(
            "VLM pricing sync complete: changed=%d unchanged=%d skipped=%d fx=%.2f",
            int(stats.get("changed", 0)),
            int(stats.get("unchanged", 0)),
            int(stats.get("skipped", 0)),
            usd_to_krw,
        )
    except Exception as exc:
        logger.error("VLM pricing sync failed: %s", exc)


def _parse_bool_setting(raw_value: str, default: bool) -> bool:
    """문자열 설정값을 bool로 파싱한다."""
    value = str(raw_value or "").strip().lower()
    if not value:
        return bool(default)
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


async def cycle_run_vlm_validation_sync(service: "SchedulerService") -> None:
    """discovered/shadow 상태 VLM 후보를 검증한다."""
    if not service.job_store:
        return

    stale_hours = max(1, int(constants.VLM_VALIDATION_SYNC_STALE_HOURS))
    if not _is_vlm_sync_due(
        service,
        setting_key="vlm_last_validation_sync_at",
        stale_hours=stale_hours,
    ):
        logger.debug("VLM validation sync skipped: stale window not reached")
        return

    raw_limit = str(
        service.job_store.get_system_setting(
            "vlm_validation_candidate_limit",
            str(constants.VLM_VALIDATION_CANDIDATE_LIMIT),
        )
    ).strip()
    try:
        candidate_limit = max(1, min(100, int(raw_limit)))
    except ValueError:
        candidate_limit = int(constants.VLM_VALIDATION_CANDIDATE_LIMIT)

    auto_activate = _parse_bool_setting(
        service.job_store.get_system_setting(
            "vlm_validation_auto_activate",
            "true" if constants.VLM_VALIDATION_AUTO_ACTIVATE else "false",
        ),
        default=constants.VLM_VALIDATION_AUTO_ACTIVATE,
    )

    try:
        from .vlm_validation_worker import VLMValidationWorker

        worker = VLMValidationWorker(job_store=service.job_store)
        stats = worker.run_cycle(limit=candidate_limit, auto_activate=auto_activate)
        service.job_store.set_system_setting("vlm_last_validation_sync_at", now_utc())
        logger.info(
            "VLM validation sync complete: moved_shadow=%d activated=%d rejected=%d observed=%d limit=%d auto_activate=%s",
            int(stats.get("moved_shadow", 0)),
            int(stats.get("activated", 0)),
            int(stats.get("rejected", 0)),
            int(stats.get("observed", 0)),
            candidate_limit,
            str(auto_activate).lower(),
        )
    except Exception as exc:
        logger.error("VLM validation sync failed: %s", exc)



async def cycle_run_sub_job_catchup(service: "SchedulerService") -> None:
    """완료된 마스터 잡을 기준으로 누락된 서브 잡을 생성한다."""
    if not service.job_store:
        return
    started_at = perf_counter()
    skip_reasons: Counter[str] = Counter()
    created_count = 0
    scanned_pairs = 0
    master_count = 0
    sub_channel_count = 0

    if not service._is_multichannel_enabled():
        skip_reasons["multichannel_disabled"] += 1
        service._log_sub_job_catchup_stats(
            created_count=created_count,
            scanned_pairs=scanned_pairs,
            master_count=master_count,
            sub_channel_count=sub_channel_count,
            skip_reasons=skip_reasons,
            started_at=started_at,
        )
        return

    sub_channels = service.job_store.get_active_sub_channels()
    sub_channel_count = len(sub_channels)
    if not sub_channels:
        skip_reasons["no_active_sub_channels"] += 1
        service._log_sub_job_catchup_stats(
            created_count=created_count,
            scanned_pairs=scanned_pairs,
            master_count=master_count,
            sub_channel_count=sub_channel_count,
            skip_reasons=skip_reasons,
            started_at=started_at,
        )
        return

    masters = service.job_store.list_recent_completed_jobs(
        limit=200,
        job_kind=service._master_job_kind(),
    )
    master_count = len(masters)
    if not masters:
        skip_reasons["no_recent_completed_masters"] += 1
        service._log_sub_job_catchup_stats(
            created_count=created_count,
            scanned_pairs=scanned_pairs,
            master_count=master_count,
            sub_channel_count=sub_channel_count,
            skip_reasons=skip_reasons,
            started_at=started_at,
        )
        return

    for master in masters:
        base_time = parse_iso(master.completed_at or master.updated_at)
        for channel in sub_channels:
            scanned_pairs += 1
            platform = str(channel.get("platform", "")).strip().lower()
            if platform not in _IMPLEMENTED_SUB_PLATFORMS:
                skip_reasons["publisher_not_implemented"] += 1
                continue

            channel_id = str(channel.get("channel_id", "")).strip()
            if not channel_id:
                skip_reasons["missing_channel_id"] += 1
                continue

            existing = service.job_store.get_sub_job_by_master_channel(master.job_id, channel_id)
            if existing:
                skip_reasons["already_exists"] += 1
                continue

            delay_minutes = max(0, int(channel.get("publish_delay_minutes", 90)))
            scheduled_at = (base_time + timedelta(minutes=delay_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
            sub_job_id = str(uuid.uuid4())
            sub_persona = str(channel.get("persona_id", "")).strip() or master.persona_id
            sub_title = f"[{str(channel.get('label', '')).strip()}] {master.title}"

            success = service.job_store.schedule_job(
                job_id=sub_job_id,
                title=sub_title,
                seed_keywords=list(master.seed_keywords),
                platform=platform,
                persona_id=sub_persona,
                scheduled_at=scheduled_at,
                max_retries=max(1, int(master.max_retries)),
                tags=list(master.tags or []),
                category=str(master.category or ""),
                job_kind=service._sub_job_kind(),
                master_job_id=master.job_id,
                channel_id=channel_id,
                status=service.job_store.STATUS_QUEUED,
            )
            if success:
                created_count += 1
            else:
                skip_reasons["schedule_failed"] += 1

    service._log_sub_job_catchup_stats(
        created_count=created_count,
        scanned_pairs=scanned_pairs,
        master_count=master_count,
        sub_channel_count=sub_channel_count,
        skip_reasons=skip_reasons,
        started_at=started_at,
    )



async def cycle_run_sub_job_publish_catchup(service: "SchedulerService", max_jobs: int = 20) -> None:
    """예약 시각이 지난 서브 잡을 우선 처리한다."""
    if not service.job_store or not service.pipeline_service:
        return

    started_at = perf_counter()
    skip_reasons: Counter[str] = Counter()
    processed = 0
    published_count = 0
    prepared_count = 0
    limit = max(1, int(max_jobs or 20))

    for _ in range(limit):
        published = await service._publish_next_available_job(job_kind=service._sub_job_kind())
        if published:
            processed += 1
            published_count += 1
            await asyncio.sleep(constants.SCHEDULER_SUBJOB_STEP_SLEEP_SEC)
            continue

        ready_count = service._get_ready_count(job_kind=service._sub_job_kind())
        due_count = service._get_due_count(job_kind=service._sub_job_kind())
        if ready_count <= 0 and due_count <= 0:
            skip_reasons["no_due_jobs"] += 1
            break

        prepared = await service._prepare_next_available_job(job_kind=service._sub_job_kind())
        if prepared:
            processed += 1
            prepared_count += 1
            await asyncio.sleep(constants.SCHEDULER_SUBJOB_STEP_SLEEP_SEC)
            continue

        if ready_count > 0:
            skip_reasons["ready_claim_or_publish_failed"] += 1
        if due_count > 0:
            skip_reasons["due_claim_or_prepare_failed"] += 1
        break

    if processed >= limit:
        skip_reasons["max_jobs_limit_reached"] += 1

    elapsed_sec = max(0.001, perf_counter() - started_at)
    throughput_per_min = processed * 60.0 / elapsed_sec
    logger.info(
        "Sub job publish catch-up stats: processed=%d published=%d prepared=%d limit=%d throughput_per_min=%.2f skip_reasons=%s elapsed_sec=%.2f",
        processed,
        published_count,
        prepared_count,
        limit,
        throughput_per_min,
        json.dumps(dict(skip_reasons), ensure_ascii=False, sort_keys=True),
        elapsed_sec,
    )



async def cycle_run_daily_summary_notification(service: "SchedulerService") -> None:
    """KST 22:30 일일 요약 알림을 전송한다."""
    if not service.notifier or not service.notifier.enabled or not service.job_store:
        return

    now_local = service._get_now_local()
    if service._last_daily_summary_date == now_local.date():
        return

    completed = service._get_today_post_count()
    failed = service._get_today_failed_count()
    queue_stats = service.job_store.get_queue_stats()
    ready_count = int(queue_stats.get("ready_to_publish", 0))
    queued_count = int(queue_stats.get("queued", 0))
    configured_target = service._get_configured_daily_target()
    idea_pending_count = service._get_idea_vault_pending_count()
    idea_daily_quota = service._get_configured_idea_vault_quota(configured_target)

    try:
        sent = await service.notifier.notify_daily_summary(
            local_date=now_local.strftime("%Y-%m-%d"),
            target=configured_target,
            completed=completed,
            failed=failed,
            ready_count=ready_count,
            queued_count=queued_count,
            idea_pending_count=idea_pending_count,
            idea_daily_quota=idea_daily_quota,
        )
        if sent:
            service._last_daily_summary_date = now_local.date()
    except Exception as exc:
        logger.warning("Daily summary notify failed: %s", exc)



async def cycle_run_cost_efficiency_alert(service: "SchedulerService") -> None:
    """LLM 호출 대비 발행이 0건일 때 비용 효율 경보를 전송한다."""
    if not service.notifier or not service.notifier.enabled or not service.job_store:
        return

    today_key = service._today_key()
    last_alert_date = service.job_store.get_system_setting("scheduler_last_cost_alert_date", "")
    if last_alert_date == today_key:
        return

    today_completed = service._get_today_post_count()
    if today_completed > 0:
        return

    try:
        total_calls = _count_today_llm_generation_calls(service, today_key=today_key)
    except Exception as exc:
        logger.warning("Cost efficiency alert stats read failed: %s", exc)
        return

    call_threshold = 10
    if total_calls < call_threshold:
        return

    message = (
        f"⚠️ [발행 효율 경보]\n"
        f"- 날짜: {today_key}\n"
        f"- LLM 호출: {total_calls}건\n"
        f"- 발행 완료: 0건\n\n"
        f"API 비용이 발생하는데 발행이 없습니다.\n"
        f"Playwright·LLM 오류 또는 잡 stuck 여부를 확인하세요."
    )
    try:
        sent = await service.notifier.send_message(message)
        if sent:
            service.job_store.set_system_setting("scheduler_last_cost_alert_date", today_key)
            logger.info("Cost efficiency alert sent (llm_calls=%d)", total_calls)
    except Exception as exc:
        logger.warning("Cost efficiency alert send failed: %s", exc)


def _count_today_llm_generation_calls(
    service: "SchedulerService",
    *,
    today_key: str,
) -> int:
    """KST 기준 오늘 생성/문체 LLM 호출 수를 계산한다."""
    if not service.job_store:
        return 0

    try:
        local_tz = ZoneInfo(service.timezone_name)
    except Exception:
        local_tz = timezone(timedelta(hours=9))

    start_local = datetime.strptime(today_key, "%Y-%m-%d").replace(tzinfo=local_tz)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with service.job_store.connection() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(
                COALESCE(
                    NULLIF(CAST(json_extract(detail_json, '$.calls') AS INTEGER), 0),
                    1
                )
            ), 0) AS total_calls
            FROM job_metrics
            WHERE created_at >= ?
              AND created_at < ?
              AND metric_type IN ('quality_step', 'voice_step')
              AND (
                provider != ''
                OR input_tokens > 0
                OR output_tokens > 0
              )
            """,
            (start_utc, end_utc),
        ).fetchone()
    return int(row["total_calls"] or 0) if row else 0


async def cycle_collect_telegram_pending_updates(service: "SchedulerService") -> int:
    """텔레그램 getUpdates 폴백을 주기적으로 실행한다."""
    if not service.job_store:
        return 0
    try:
        from server.routers.telegram_webhook import collect_pending_updates
    except Exception as exc:
        logger.warning("Telegram update collector unavailable: %s", exc)
        return 0

    try:
        return int(await collect_pending_updates(service.job_store))
    except Exception as exc:
        logger.warning("Telegram update collector failed: %s", exc)
        return 0


async def cycle_run_daily_target_check(service: "SchedulerService") -> None:
    """일일 목표 기반으로 준비된 초안을 1건씩 천천히 발행한다."""
    now_local = service._get_now_local()
    configured_target = service._get_configured_daily_target()
    service._ensure_daily_publish_slots(now_local.date(), configured_target)

    if not (service.ACTIVE_HOURS[0] <= now_local.hour < service.ACTIVE_HOURS[1]):
        logger.debug("Outside active hours: %s", now_local)
        return

    today_completed = service._get_today_post_count()
    remaining = configured_target - today_completed
    if remaining <= 0:
        logger.debug("Daily target reached: %d/%d", today_completed, configured_target)
        return

    if not service._is_publish_interval_ready(now_local, today_completed):
        return

    if not service.pipeline_service:
        return

    ready_count = service._get_ready_draft_count()
    if ready_count <= 0:
        logger.info(
            "No prepared draft to publish yet (%d/%d completed)",
            today_completed,
            configured_target,
        )
        return

    logger.info(
        "Daily target check passed (%d/%d), publishing prepared draft",
        today_completed,
        configured_target,
    )
    try:
        published = await service._publish_next_available_job(
            job_kind=service._master_job_kind() if service.job_store else None
        )
        if published:
            service._publish_wait_until_utc = datetime.now(timezone.utc) + timedelta(
                minutes=service.min_post_interval_minutes
            )
        else:
            # 준비된 초안이 없거나 발행이 실패한 경우 짧은 대기 후 재확인한다.
            service._publish_wait_until_utc = datetime.now(timezone.utc) + timedelta(minutes=10)
    except Exception as exc:
        logger.error("Daily target execution failed: %s", exc)
        service._publish_wait_until_utc = datetime.now(timezone.utc) + timedelta(minutes=10)



async def cycle_run_draft_prefetch(service: "SchedulerService") -> None:
    """리소스 여유 시 오늘 목표치만큼 초안을 선생성한다.

    LLM 다중 호출 도중 CPU 가 급등하면 watchdog 태스크가 interrupt_event 를
    set 하고, for 루프는 매 반복마다 이를 확인해 즉시(mid-generation) 탈출한다.
    """
    if not service.pipeline_service:
        return

    ready_count = service._get_ready_draft_count()
    configured_target = service._get_configured_daily_target()
    # CPU 여유 상태일 때 버퍼 목표를 동적으로 상향 (최대 9)
    cpu_allowed, cpu_avg = service._cpu_monitor.check()
    extended_buffer = min(9, service.DRAFT_BUFFER_TARGET + 3) if cpu_allowed and cpu_avg < service._cpu_monitor.start_threshold_percent else service.DRAFT_BUFFER_TARGET
    target_buffer = max(extended_buffer, configured_target + 2)
    needed = max(0, target_buffer - ready_count)
    if needed <= 0:
        logger.debug(
            "Draft buffer is enough (%d ready, target=%d, cpu_avg=%.1f%%)",
            ready_count,
            target_buffer,
            cpu_avg,
        )
        return

    if not service._has_resource_headroom():
        logger.info("Skip draft prefetch due to high resource usage or paused state")
        return

    logger.info(
        "Draft prefetch start (need=%d, ready=%d, target=%d)",
        needed,
        ready_count,
        target_buffer,
    )

    # mid-generation 인터럽트를 위한 이벤트 + watchdog 태스크 시작
    interrupt_event = service._cpu_monitor.make_interrupt_event()
    watchdog_task = asyncio.create_task(
        service._cpu_monitor.run_interrupt_watchdog(interrupt_event),
        name="cpu-interrupt-watchdog",
    )

    prepared_count = 0
    try:
        for _ in range(needed):
            # watchdog 가 CPU 급등을 감지하면 즉시 루프 탈출
            if interrupt_event.is_set():
                logger.info(
                    "Draft prefetch interrupted by CPU watchdog "
                    "(prepared=%d, needed=%d)",
                    prepared_count,
                    needed,
                )
                break
            if not service._has_resource_headroom():
                logger.info(
                    "Draft prefetch stopped: resource headroom lost "
                    "(prepared=%d, needed=%d)",
                    prepared_count,
                    needed,
                )
                break
            prepared = await service._prepare_next_available_job(
                job_kind=service._master_job_kind() if service.job_store else None
            )
            if not prepared:
                break
            prepared_count += 1
            await asyncio.sleep(constants.SCHEDULER_DRAFT_PREFETCH_STEP_SLEEP_SEC)
    finally:
        # watchdog 정리: 루프가 끝나면 이벤트를 set 해 watchdog 코루틴도 종료
        interrupt_event.set()
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:
            pass

    logger.info("Draft prefetch done (prepared=%d, needed=%d)", prepared_count, needed)



async def cycle_prepare_next_available_job(service: "SchedulerService", job_kind: Optional[str] = None) -> bool:
    """파이프라인이 지원하는 방식으로 다음 초안을 생성한다."""
    prepare_fn = getattr(service.pipeline_service, "prepare_next_pending_job", None)
    if prepare_fn and callable(prepare_fn):
        required_tag = _scheduler_required_job_tag(service)
        try:
            return bool(await prepare_fn(job_kind=job_kind, required_tag=required_tag))
        except TypeError:
            try:
                return bool(await prepare_fn(job_kind=job_kind))
            except TypeError:
                return bool(await prepare_fn())
    return False



async def cycle_publish_next_available_job(service: "SchedulerService", job_kind: Optional[str] = None) -> bool:
    """파이프라인이 지원하는 방식으로 다음 발행을 실행한다."""
    publish_fn = getattr(service.pipeline_service, "publish_next_ready_job", None)
    if publish_fn and callable(publish_fn):
        required_tag = _scheduler_required_job_tag(service)
        try:
            return bool(await publish_fn(job_kind=job_kind, required_tag=required_tag))
        except TypeError:
            try:
                return bool(await publish_fn(job_kind=job_kind))
            except TypeError:
                return bool(await publish_fn())
    run_fn = getattr(service.pipeline_service, "run_next_pending_job", None)
    if run_fn and callable(run_fn):
        required_tag = _scheduler_required_job_tag(service)
        try:
            return bool(await run_fn(job_kind=job_kind, required_tag=required_tag))
        except TypeError:
            try:
                return bool(await run_fn(job_kind=job_kind))
            except TypeError:
                return bool(await run_fn())
    return False



def _scheduler_required_job_tag(service: "SchedulerService") -> Optional[str]:
    """자동 워커가 처리할 필수 태그를 반환한다."""

    raw = ""
    if service.job_store:
        raw = service.job_store.get_system_setting("scheduler_required_tag", "")
    if str(raw or "").strip():
        return str(raw).strip()
    if _is_market_daily_publish_mode(service):
        return "market_daily"
    return None


def cycle_is_publish_interval_ready(
    service: "SchedulerService",
    now_local: datetime,
    today_completed: int,
) -> bool:
    """발행 최소 간격/일간 분포 슬롯 조건을 확인한다."""
    now_utc = datetime.now(timezone.utc)

    if service._publish_wait_until_utc and now_utc < service._publish_wait_until_utc:
        wait_seconds = (service._publish_wait_until_utc - now_utc).total_seconds()
        logger.debug("Waiting publish cooldown (%.0f sec left)", wait_seconds)
        return False

    last_completed = service._get_last_post_time()
    if last_completed:
        elapsed_minutes = (now_utc - last_completed).total_seconds() / 60.0
        if elapsed_minutes < service.min_post_interval_minutes:
            logger.debug(
                "Post interval not met (elapsed=%.1fmin, required=%dmin)",
                elapsed_minutes,
                service.min_post_interval_minutes,
            )
            return False

    if today_completed >= len(service._daily_publish_slots):
        logger.debug("No remaining publish slots for today")
        return False

    next_slot = service._daily_publish_slots[today_completed]
    if now_local < next_slot:
        wait_seconds = (next_slot - now_local).total_seconds()
        logger.debug(
            "Waiting next weighted publish slot (%.0f sec left)",
            wait_seconds,
        )
        return False

    return True



def cycle_has_resource_headroom(service: "SchedulerService") -> bool:
    """CPU/메모리 사용량이 임계값 이내인지 확인한다."""
    cpu_allowed, cpu_avg = service._cpu_monitor.check()
    memory_percent = service._get_memory_percent()
    logger.debug(
        "Resource check cpu_avg=%.1f%% mem=%.1f%% (cpu start/stop %.1f/%.1f, mem<=%.1f)",
        cpu_avg,
        memory_percent,
        service.cpu_start_threshold_percent,
        service.cpu_stop_threshold_percent,
        service.memory_threshold_percent,
    )
    return cpu_allowed and memory_percent <= service.memory_threshold_percent



def cycle_ensure_daily_publish_slots(service: "SchedulerService", target_date: date, daily_target: int) -> None:
    """당일 발행 슬롯을 준비한다."""
    if (
        service._publish_slot_date == target_date
        and service._daily_publish_slots
        and len(service._daily_publish_slots) == max(1, daily_target)
    ):
        return

    service._daily_publish_slots = service._build_daily_publish_slots(target_date, daily_target)
    service._publish_slot_date = target_date
    service._publish_wait_until_utc = None
    logger.info(
        "Daily weighted publish slots generated",
        extra={
            "date": target_date.isoformat(),
            "slots": [slot.isoformat() for slot in service._daily_publish_slots],
        },
    )



def cycle_get_publish_anchor_hours(service: "SchedulerService") -> tuple:
    """발행 앵커 시간대를 반환한다.

    DB system_settings 의 ``publish_anchor_hours`` 키에 쉼표 구분 정수 목록을
    저장하면 런타임에 반영된다.  예) ``"9,12,19"``
    설정이 없거나 파싱에 실패하면 클래스 상수 ``PUBLISH_ANCHOR_HOURS`` 를 사용한다.
    """
    if service.job_store:
        raw = service.job_store.get_system_setting("publish_anchor_hours", "")
        if raw and raw.strip():
            try:
                hours = tuple(
                    int(h.strip())
                    for h in raw.split(",")
                    if h.strip().isdigit()
                )
                if hours:
                    return hours
            except Exception:
                pass
    return service.PUBLISH_ANCHOR_HOURS



def cycle_build_daily_publish_slots(service: "SchedulerService", target_date: date, daily_target: Optional[int] = None) -> List[datetime]:
    """출/점/퇴 중심 가우시안 분포로 하루 발행 슬롯을 만든다.

    앵커 시간(기본 9·12·19시)마다 σ=20분 정규분포 지터(jitter)를 더해
    자연스러운 발행 시간대를 구성한다.  앵커 시간은 DB 설정
    ``publish_anchor_hours`` 에서 동적으로 읽어온다.
    """
    local_tz = service._get_now_local().tzinfo
    if local_tz is None:
        local_tz = timezone(timedelta(hours=9))

    resolved_target = max(1, int(daily_target or service.daily_posts_target))
    if _is_market_daily_publish_mode(service):
        return _build_market_daily_publish_slots(
            target_date=target_date,
            local_tz=local_tz,
            daily_target=resolved_target,
        )

    anchor_hours = service._get_publish_anchor_hours()
    rng = service._build_rng_for_date(target_date)
    candidates: List[datetime] = []

    for index in range(resolved_target):
        if index < len(anchor_hours):
            base_hour = anchor_hours[index]
        else:
            base_hour = rng.choices(
                population=list(anchor_hours),
                weights=[0.45, 0.35, 0.20][: len(anchor_hours)],
                k=1,
            )[0]

        base_time = datetime.combine(
            target_date,
            time_obj(hour=base_hour, minute=0),
            tzinfo=local_tz,
        )
        # σ=20분 가우시안 지터, ±40분 클램프 (기존 σ=25, ±45→55 에서 조정)
        jitter_minutes = int(rng.gauss(0, 20))
        jitter_minutes = max(-40, min(40, jitter_minutes))
        slot = base_time + timedelta(minutes=jitter_minutes)

        start_bound = datetime.combine(
            target_date,
            time_obj(hour=service.ACTIVE_HOURS[0], minute=0),
            tzinfo=local_tz,
        )
        end_bound = datetime.combine(
            target_date,
            time_obj(hour=service.ACTIVE_HOURS[1] - 1, minute=55),
            tzinfo=local_tz,
        )
        if slot < start_bound:
            slot = start_bound
        if slot > end_bound:
            slot = end_bound

        candidates.append(slot)

    candidates.sort()
    min_interval = timedelta(minutes=service.min_post_interval_minutes)
    max_interval = timedelta(minutes=service.publish_interval_max_minutes)
    for index in range(1, len(candidates)):
        previous = candidates[index - 1]
        current = candidates[index]
        gap = current - previous
        if gap < min_interval:
            candidates[index] = previous + min_interval
        elif gap > max_interval:
            candidates[index] = previous + max_interval

    return candidates



def _is_market_daily_publish_mode(service: "SchedulerService") -> bool:
    """시장 브리핑 고정 발행 슬롯 모드 여부를 반환한다."""

    raw = ""
    if service.job_store:
        raw = service.job_store.get_system_setting("scheduler_market_daily_enabled", "")
    if not str(raw or "").strip():
        import os

        raw = os.getenv("SCHEDULER_MARKET_DAILY_ENABLED", "true")
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _build_market_daily_publish_slots(
    *,
    target_date: date,
    local_tz: Any,
    daily_target: int,
) -> List[datetime]:
    """시장 브리핑 운영용 고정 발행 슬롯을 반환한다."""

    from ..market import get_us_preopen_kst

    fixed_slots = sorted(
        [
            datetime.combine(target_date, time_obj(hour=8, minute=10), tzinfo=local_tz),
            datetime.combine(target_date, time_obj(hour=18, minute=30), tzinfo=local_tz),
            get_us_preopen_kst(target_date).astimezone(local_tz),
        ]
    )
    if daily_target <= len(fixed_slots):
        return fixed_slots[:daily_target]

    extended = list(fixed_slots)
    last_slot = extended[-1]
    for index in range(daily_target - len(fixed_slots)):
        extended.append(last_slot + timedelta(minutes=70 * (index + 1)))
    return sorted(extended)


def _normalize_model_id(value: str) -> str:
    """모델 식별자를 비교 가능한 형태로 정규화한다."""
    normalized = str(value or "").strip().lower()
    if not normalized:
        return ""
    if ":" in normalized:
        return normalized.split(":", 1)[1].strip()
    return normalized


async def cycle_run_daily_model_eval(service: "SchedulerService") -> None:
    """매일 1회 오늘의 eval 대상 모델을 선정하고 저장한다."""
    if not service.job_store:
        return

    today_key = service._today_key()
    last_run = service.job_store.get_system_setting("router_eval_last_run_date", "")
    if last_run == today_key:
        logger.debug("Daily eval already ran today (%s)", today_key)
        return

    raw_registered = service.job_store.get_system_setting("router_registered_models", "[]")
    try:
        registered_models = json.loads(raw_registered) if raw_registered else []
        if not isinstance(registered_models, list):
            registered_models = []
    except Exception:
        registered_models = []

    active_models = [
        model for model in registered_models
        if isinstance(model, dict) and bool(model.get("active", True))
    ]
    if not active_models:
        logger.info("Daily eval skipped: no active registered models")
        return

    since_90d = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = service.job_store.get_model_performance_summary(
        since=since_90d,
        slot_types=["eval", "main", "shadow", "challenger"],
    )

    samples_map: Dict[str, int] = {}
    for row in summary:
        model_id = _normalize_model_id(str(row.get("model_id", "")))
        if not model_id:
            continue
        samples_map[model_id] = int(row.get("samples", 0) or 0)

    candidates: List[Dict[str, Any]] = []
    for model in active_models:
        model_id = str(model.get("model_id", "")).strip()
        if not model_id:
            continue
        normalized_model_id = _normalize_model_id(model_id)
        candidates.append(
            {
                "model_id": model_id,
                "provider": str(model.get("provider", "")).strip().lower(),
                "samples": samples_map.get(normalized_model_id, 0),
            }
        )

    if not candidates:
        logger.info("Daily eval skipped: no candidate models")
        return

    candidates.sort(key=lambda item: (int(item["samples"]), str(item["model_id"])))
    selected = candidates[0]
    service.job_store.set_system_setting("router_eval_model_today", str(selected["model_id"]))
    service.job_store.set_system_setting("router_eval_last_run_date", today_key)
    # 새 eval 모델을 선정하면 당일 배정 플래그를 초기화한다.
    service.job_store.set_system_setting("router_eval_claimed_date", "")
    service.job_store.set_system_setting("router_eval_claimed_job_id", "")
    logger.info(
        "Daily eval model selected: %s (samples=%d)",
        selected["model_id"],
        selected["samples"],
    )


async def cycle_auto_champion_switch(service: "SchedulerService") -> None:
    """누적 성능 데이터 기준으로 챔피언 모델 자동 교체를 시도한다."""
    if not service.job_store:
        return

    raw_registered = service.job_store.get_system_setting("router_registered_models", "[]")
    try:
        registered_models = json.loads(raw_registered) if raw_registered else []
        if not isinstance(registered_models, list):
            registered_models = []
    except Exception:
        registered_models = []
    active_model_ids = {
        _normalize_model_id(str(model.get("model_id", "")))
        for model in registered_models
        if isinstance(model, dict) and bool(model.get("active", True))
    }
    active_model_ids.discard("")
    if not active_model_ids:
        return

    try:
        min_samples = max(1, int(service.job_store.get_system_setting("router_eval_min_samples", "5") or "5"))
    except ValueError:
        min_samples = 5
    try:
        threshold = max(0.0, float(
            service.job_store.get_system_setting("router_champion_switch_threshold", "2.0") or "2.0"
        ))
    except ValueError:
        threshold = 2.0

    _raw_strategy = str(service.job_store.get_system_setting("router_strategy_mode", "cost")).strip().lower()
    strategy_mode = _raw_strategy if _raw_strategy in ("quality", "balanced", "cost") else "cost"
    current_champion = str(service.job_store.get_system_setting("router_champion_model", "")).strip()
    normalized_current = _normalize_model_id(current_champion)

    since_90d = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = service.job_store.get_model_performance_summary(
        since=since_90d,
        slot_types=["eval", "main", "shadow", "challenger"],
    )
    filtered_summary = [
        item for item in summary
        if _normalize_model_id(str(item.get("model_id", ""))) in active_model_ids
    ]
    if not filtered_summary:
        return

    eligible = [item for item in filtered_summary if int(item.get("samples", 0) or 0) >= min_samples]
    if not eligible:
        return

    if strategy_mode == "quality":
        eligible.sort(key=lambda item: -float(item.get("avg_quality_score", 0.0) or 0.0))
    elif strategy_mode == "balanced":
        # quality 60% + score_per_won 40% 가중 복합 지표
        eligible.sort(
            key=lambda item: -(
                float(item.get("avg_quality_score", 0.0) or 0.0) * 0.6
                + float(item.get("avg_score_per_won", 0.0) or 0.0) * 0.4
            )
        )
    else:  # cost
        eligible.sort(key=lambda item: -(float(item.get("avg_score_per_won", 0.0) or 0.0)))

    best = eligible[0]
    best_model_id = str(best.get("model_id", "")).strip()
    normalized_best = _normalize_model_id(best_model_id)
    if not best_model_id or normalized_best == normalized_current:
        return

    champion_data = next(
        (
            item for item in filtered_summary
            if _normalize_model_id(str(item.get("model_id", ""))) == normalized_current
        ),
        None,
    )

    should_switch = False
    if strategy_mode == "quality":
        champion_quality = float(champion_data.get("avg_quality_score", 0.0) or 0.0) if champion_data else 0.0
        best_quality = float(best.get("avg_quality_score", 0.0) or 0.0)
        should_switch = champion_data is None or best_quality > champion_quality + threshold
    elif strategy_mode == "balanced":
        champion_quality = float(champion_data.get("avg_quality_score", 0.0) or 0.0) if champion_data else 0.0
        champion_spr = float(champion_data.get("avg_score_per_won", 0.0) or 0.0) if champion_data else 0.0
        best_quality = float(best.get("avg_quality_score", 0.0) or 0.0)
        best_spr = float(best.get("avg_score_per_won", 0.0) or 0.0)
        # 품질 threshold 충족 OR (비용효율 threshold 충족 + 품질 1점 이내 허용)
        should_switch = champion_data is None or (
            best_quality > champion_quality + threshold
            or (best_spr > champion_spr + threshold and best_quality >= champion_quality - 1.0)
        )
    else:  # cost
        champion_spr = float(champion_data.get("avg_score_per_won", 0.0) or 0.0) if champion_data else 0.0
        champion_quality = float(champion_data.get("avg_quality_score", 0.0) or 0.0) if champion_data else 0.0
        best_spr = float(best.get("avg_score_per_won", 0.0) or 0.0)
        best_quality = float(best.get("avg_quality_score", 0.0) or 0.0)
        should_switch = champion_data is None or (
            best_spr > champion_spr + threshold
            and best_quality >= champion_quality - 2.0
        )

    if not should_switch:
        return

    auto_switch_enabled = _parse_bool_setting(
        service.job_store.get_system_setting("router_auto_champion_switch_enabled", "false"),
        default=False,
    )
    if not auto_switch_enabled:
        recommendation_detail = {
            "previous": current_champion or "",
            "recommended": best_model_id,
            "strategy_mode": strategy_mode,
            "avg_quality_score": float(best.get("avg_quality_score", 0.0) or 0.0),
            "avg_score_per_won": float(best.get("avg_score_per_won", 0.0) or 0.0),
            "avg_cost_won": float(best.get("avg_cost_won", 0.0) or 0.0),
            "samples": int(best.get("samples", 0) or 0),
            "recommended_at": now_utc(),
        }
        service.job_store.set_system_setting("router_champion_recommendation_model", best_model_id)
        service.job_store.set_system_setting(
            "router_champion_recommendation_detail",
            json.dumps(recommendation_detail, ensure_ascii=False),
        )
        logger.info(
            "Champion recommendation recorded without auto switch: %s -> %s",
            current_champion,
            best_model_id,
        )
        if service.notifier and getattr(service.notifier, "enabled", False):
            message = (
                "🏆 챔피언 모델 교체 후보\n"
                "자동 교체는 꺼져 있어요. 성능 기록만 남겼습니다.\n"
                f"• 현재: {current_champion or '-'}\n"
                f"• 후보: {best_model_id}\n"
                f"• 품질: {float(best.get('avg_quality_score', 0.0) or 0.0):.1f}점\n"
                f"• 비용효율: {float(best.get('avg_score_per_won', 0.0) or 0.0):.2f}"
            )
            try:
                await service.notifier.send_message(message)
            except Exception:
                logger.debug("Champion recommendation notification failed", exc_info=True)
        return

    service.job_store.set_system_setting("router_champion_model", best_model_id)
    logger.info("Champion switched: %s -> %s", current_champion, best_model_id)

    # champion_history 기록
    try:
        week_start = (
            datetime.now(timezone.utc) - timedelta(days=datetime.now(timezone.utc).weekday())
        ).strftime("%Y-%m-%d")
        topic_mode_scores: Dict[str, float] = {}
        for topic in ("cafe", "it", "finance", "parenting"):
            topic_summary = service.job_store.get_model_performance_summary(
                since=since_90d,
                slot_types=["eval", "main"],
                topic_mode=topic,
            )
            for row in topic_summary:
                if _normalize_model_id(str(row.get("model_id", ""))) == normalized_best:
                    score = row.get("avg_quality_score")
                    if score is not None:
                        topic_mode_scores[topic] = float(score)
                    break
        record_fn = getattr(service.job_store, "record_champion_history", None)
        if callable(record_fn):
            record_fn(
                week_start=week_start,
                champion_model=best_model_id,
                challenger_model=current_champion or "",
                avg_champion_score=float(best.get("avg_quality_score", 0.0) or 0.0),
                topic_mode_scores=topic_mode_scores,
                cost_won=float(best.get("avg_cost_won", 0.0) or 0.0),
                early_terminated=False,
                shadow_only=False,
            )
            logger.info("Champion history recorded for week %s: %s", week_start, best_model_id)
    except Exception:
        logger.debug("Champion history recording failed", exc_info=True)

    if service.notifier and getattr(service.notifier, "enabled", False):
        message = (
            "🏆 챔피언 모델 자동 교체\n"
            f"• 이전: {current_champion or '-'}\n"
            f"• 신규: {best_model_id}\n"
            f"• 품질: {float(best.get('avg_quality_score', 0.0) or 0.0):.1f}점\n"
            f"• 비용효율: {float(best.get('avg_score_per_won', 0.0) or 0.0):.2f}"
        )
        try:
            await service.notifier.send_message(message)
        except Exception:
            logger.debug("Champion switch notification failed", exc_info=True)



def cycle_get_configured_daily_target(service: "SchedulerService") -> int:
    """DB 설정을 포함한 일간 목표 발행량을 반환한다."""
    default_target = max(1, service.daily_posts_target or service.DEFAULT_DAILY_TARGET)
    if not service.job_store:
        return default_target
    get_setting = getattr(service.job_store, "get_system_setting", None)
    if not get_setting or not callable(get_setting):
        return default_target
    raw = str(get_setting("scheduler_daily_posts_target", "")).strip()
    if not raw:
        import os

        raw = str(os.getenv("SCHEDULER_DAILY_POSTS_TARGET", "")).strip()
    if not raw:
        return default_target
    try:
        value = int(raw)
    except ValueError:
        return default_target
    return max(1, min(20, value))



def cycle_load_daily_quota_allocations(service: "SchedulerService") -> tuple[int, List[Dict[str, Any]]]:
    """DB에서 카테고리 할당량 설정을 읽어 정규화한다."""
    daily_target = service._get_configured_daily_target()
    if not service.job_store:
        return daily_target, []
    get_setting = getattr(service.job_store, "get_system_setting", None)
    if not get_setting or not callable(get_setting):
        return daily_target, service._build_default_quota_allocations(daily_target)

    raw_allocations = str(get_setting("scheduler_category_allocations", "")).strip()
    allocations: List[Dict[str, Any]] = []
    if raw_allocations:
        try:
            decoded = json.loads(raw_allocations)
            if isinstance(decoded, list):
                for item in decoded:
                    if not isinstance(item, dict):
                        continue
                    category_name = str(item.get("category", "")).strip()
                    topic_mode = service._normalize_topic_mode(str(item.get("topic_mode", "")).strip())
                    count = max(0, int(item.get("count", 0)))
                    if not category_name:
                        continue
                    allocations.append(
                        {
                            "category": category_name,
                            "topic_mode": topic_mode,
                            "count": count,
                        }
                    )
        except Exception:
            allocations = []

    if not allocations:
        allocations = service._build_default_quota_allocations(daily_target)

    total = sum(int(item.get("count", 0)) for item in allocations)
    if total <= 0:
        allocations = service._build_default_quota_allocations(daily_target)

    return daily_target, allocations




def cycle_get_configured_idea_vault_quota(service: "SchedulerService", daily_target: int) -> int:
    """일간 아이디어 창고 할당량을 반환한다."""
    default_quota = min(
        max(0, int(daily_target)),
        service.DEFAULT_IDEA_VAULT_DAILY_QUOTA,
    )
    if not service.job_store:
        return default_quota
    get_setting = getattr(service.job_store, "get_system_setting", None)
    if not get_setting or not callable(get_setting):
        return default_quota
    raw = str(get_setting("scheduler_idea_vault_daily_quota", "")).strip()
    if not raw:
        return default_quota
    try:
        value = int(raw)
    except ValueError:
        return default_quota
    return max(0, min(max(0, int(daily_target)), value))



def cycle_get_idea_vault_pending_count(service: "SchedulerService") -> int:
    """아이디어 창고 pending 재고를 반환한다."""
    if not service.job_store:
        return 0
    getter = getattr(service.job_store, "get_idea_vault_pending_count", None)
    if not getter or not callable(getter):
        return 0
    try:
        return max(0, int(getter()))
    except Exception:
        return 0



def cycle_build_default_quota_allocations(service: "SchedulerService", daily_target: int) -> List[Dict[str, Any]]:
    """설정이 없을 때 기본 할당량을 만든다."""
    if not service.job_store:
        return [
            {
                "category": DEFAULT_FALLBACK_CATEGORY,
                "topic_mode": "cafe",
                "count": max(1, daily_target),
            }
        ]

    get_setting = getattr(service.job_store, "get_system_setting", None)
    if not get_setting or not callable(get_setting):
        return [
            {
                "category": DEFAULT_FALLBACK_CATEGORY,
                "topic_mode": "cafe",
                "count": max(1, daily_target),
            }
        ]

    raw_categories = str(get_setting("custom_categories", "[]"))
    categories: List[str] = []
    try:
        decoded = json.loads(raw_categories)
        if isinstance(decoded, list):
            for item in decoded:
                text = str(item).strip()
                if text and text not in categories:
                    categories.append(text)
    except Exception:
        categories = []

    if not categories:
        # DB에 저장된 fallback_category를 우선 사용하고, 없으면 전역 상수 사용
        saved_fallback = str(get_setting("fallback_category", "")).strip()
        categories = [saved_fallback if saved_fallback else DEFAULT_FALLBACK_CATEGORY]

    allocations = [
        {
            "category": category_name,
            "topic_mode": service._infer_topic_mode_from_category(category_name),
            "count": 0,
        }
        for category_name in categories
    ]
    for index in range(max(1, daily_target)):
        allocations[index % len(allocations)]["count"] = int(allocations[index % len(allocations)]["count"]) + 1
    return allocations



def cycle_normalize_topic_mode(service: "SchedulerService", raw_mode: str) -> str:
    """토픽 모드를 허용 범위로 정규화한다."""
    lowered = raw_mode.lower().strip()
    if lowered == "economy":
        return "finance"
    if lowered in {"cafe", "it", "parenting", "finance", "health"}:
        return lowered
    return "cafe"



def cycle_infer_topic_mode_from_category(service: "SchedulerService", category_name: str) -> str:
    """카테고리 문자열 기반 토픽 모드를 추정한다."""
    lowered = str(category_name).lower()
    if any(token in lowered for token in ("경제", "finance", "투자", "주식", "재테크")):
        return "finance"
    if any(token in lowered for token in ("it", "개발", "코드", "ai", "자동화", "테크")):
        return "it"
    if any(token in lowered for token in ("육아", "아이", "부모", "가정")):
        return "parenting"
    if any(token in lowered for token in ("건강", "의학", "의료", "운동", "수면", "식단", "health")):
        return "health"
    return "cafe"



def cycle_persona_id_for_topic(service: "SchedulerService", topic_mode: str) -> str:
    """토픽 모드별 기본 페르소나를 반환한다."""
    mapping = {
        "cafe": "P1",
        "it": "P2",
        "parenting": "P3",
        "finance": "P4",
        "health": "P1",
    }
    return mapping.get(service._normalize_topic_mode(topic_mode), "P1")



def cycle_build_seed_title(
    service: "SchedulerService",
    *,
    category: str,
    topic_mode: str,
    local_date: str,
    sequence: int,
) -> str:
    """자정 큐 시드용 제목을 생성한다."""
    label = {
        "cafe": "라이프",
        "it": "IT",
        "parenting": "육아",
        "finance": "경제",
        "health": "건강",
    }.get(service._normalize_topic_mode(topic_mode), "라이프")
    return f"{local_date} {label} 브리핑 #{sequence} - {category}"



def cycle_build_seed_keywords(service: "SchedulerService", category: str, topic_mode: str) -> List[str]:
    """자정 큐 시드용 키워드를 생성한다."""
    base_keywords = {
        "cafe": ["일상", "노하우", "리뷰"],
        "it": ["IT", "자동화", "생산성"],
        "parenting": ["육아", "가정", "성장"],
        "finance": ["경제", "재테크", "투자"],
        "health": ["건강", "습관", "근거"],
    }.get(service._normalize_topic_mode(topic_mode), ["일상", "정보"])

    category_token = str(category).strip()
    keywords = [category_token] if category_token else []
    for token in base_keywords:
        if token not in keywords:
            keywords.append(token)
    return keywords[:3]



def cycle_build_vault_seed_title(
    service: "SchedulerService",
    *,
    raw_text: str,
    local_date: str,
    sequence: int,
) -> str:
    """아이디어 창고 시드용 제목을 생성한다."""
    normalized = re.sub(r"\s+", " ", str(raw_text).strip())
    if not normalized:
        normalized = "아이디어 메모"
    if len(normalized) > 42:
        normalized = f"{normalized[:42].rstrip()}..."
    return f"{local_date} 아이디어 브리핑 #{sequence} - {normalized}"



def cycle_build_vault_seed_keywords(
    service: "SchedulerService",
    *,
    raw_text: str,
    category: str,
    topic_mode: str,
) -> List[str]:
    """아이디어 문장에서 시드 키워드를 생성한다."""
    keywords: List[str] = []
    category_name = str(category).strip()
    if category_name:
        keywords.append(category_name)

    tokens = re.findall(r"[가-힣A-Za-z0-9]{2,20}", str(raw_text))
    for token in tokens:
        normalized = token.strip()
        if not normalized or normalized in keywords:
            continue
        keywords.append(normalized)
        if len(keywords) >= 3:
            break

    if len(keywords) < 2:
        fallback = service._build_seed_keywords(category=category, topic_mode=topic_mode)
        for token in fallback:
            if token not in keywords:
                keywords.append(token)
            if len(keywords) >= 3:
                break
    return keywords[:3]



def cycle_build_rng_for_date(service: "SchedulerService", target_date: date) -> random.Random:
    """날짜 단위 고정 시드를 생성한다."""
    if service.random_seed is None:
        return random.Random()
    return random.Random(f"{service.random_seed}:{target_date.isoformat()}")



def cycle_get_memory_percent(service: "SchedulerService") -> float:
    """메모리 사용률을 퍼센트로 반환한다."""
    try:
        import psutil  # type: ignore[import-untyped]

        return float(psutil.virtual_memory().percent)
    except Exception:
        return 0.0



def cycle_get_ready_draft_count(service: "SchedulerService") -> int:
    """현재 ready 상태 초안 개수를 조회한다."""
    if not service.job_store:
        return 0
    get_count = getattr(service.job_store, "get_ready_to_publish_count", None)
    if get_count and callable(get_count):
        required_tag = _scheduler_required_job_tag(service)
        try:
            return int(get_count(job_kind=service._master_job_kind(), required_tag=required_tag))
        except TypeError:
            try:
                return int(get_count(job_kind=service._master_job_kind()))
            except TypeError:
                return int(get_count())
    return 0



def cycle_get_ready_count(service: "SchedulerService", job_kind: Optional[str] = None) -> int:
    """지정한 잡 kind의 ready 상태 개수를 반환한다."""
    if not service.job_store:
        return 0
    get_count = getattr(service.job_store, "get_ready_to_publish_count", None)
    if get_count and callable(get_count):
        required_tag = _scheduler_required_job_tag(service)
        try:
            return int(get_count(job_kind=job_kind, required_tag=required_tag))
        except TypeError:
            try:
                return int(get_count(job_kind=job_kind))
            except TypeError:
                return int(get_count())
    return 0



def cycle_get_due_count(service: "SchedulerService", job_kind: Optional[str] = None) -> int:
    """지정한 잡 kind의 실행 가능(queued/retry_wait) 개수를 반환한다."""
    if not service.job_store:
        return 0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    normalized_kind = str(job_kind or "").strip().lower()
    with service.job_store.connection() as conn:
        if normalized_kind:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM jobs
                WHERE job_kind = ?
                  AND (
                    (status = 'queued' AND scheduled_at <= ?)
                    OR
                    (status = 'retry_wait' AND next_retry_at <= ?)
                  )
                """,
                (normalized_kind, now, now),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM jobs
                WHERE (status = 'queued' AND scheduled_at <= ?)
                   OR (status = 'retry_wait' AND next_retry_at <= ?)
                """,
                (now, now),
            ).fetchone()
    return int(row["total"] or 0) if row else 0



def cycle_log_sub_job_catchup_stats(
    service: "SchedulerService",
    *,
    created_count: int,
    scanned_pairs: int,
    master_count: int,
    sub_channel_count: int,
    skip_reasons: Counter[str],
    started_at: float,
) -> None:
    """서브 잡 catch-up 실행 통계를 로그로 남긴다."""
    elapsed_sec = max(0.001, perf_counter() - started_at)
    throughput_per_min = created_count * 60.0 / elapsed_sec
    scan_per_min = scanned_pairs * 60.0 / elapsed_sec
    logger.info(
        "Sub job catch-up stats: masters=%d sub_channels=%d scanned_pairs=%d created=%d throughput_per_min=%.2f scan_per_min=%.2f skip_reasons=%s elapsed_sec=%.2f",
        master_count,
        sub_channel_count,
        scanned_pairs,
        created_count,
        throughput_per_min,
        scan_per_min,
        json.dumps(dict(skip_reasons), ensure_ascii=False, sort_keys=True),
        elapsed_sec,
    )



def cycle_get_now_local(service: "SchedulerService") -> datetime:
    """로컬 타임존 시각을 반환한다."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(service.timezone_name))
    except Exception:
        return datetime.now()



def cycle_today_key(service: "SchedulerService") -> str:
    """로컬 날짜 키(YYYY-MM-DD)를 반환한다."""
    return service._get_now_local().strftime("%Y-%m-%d")



def cycle_week_key(service: "SchedulerService") -> str:
    """로컬 주차 키(YYYY-Www)를 반환한다."""
    local_now = service._get_now_local()
    week_start = local_now - timedelta(days=local_now.weekday())
    return week_start.strftime("%Y-W%W")



def cycle_is_multichannel_enabled(service: "SchedulerService") -> bool:
    """멀티채널 기능 플래그를 반환한다."""
    if not service.job_store:
        return False
    raw = str(
        service.job_store.get_system_setting(_MULTICHANNEL_SETTING_KEY, "false")
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}



def cycle_master_job_kind(service: "SchedulerService") -> str:
    """마스터 잡 kind 상수를 안전하게 반환한다."""
    if not service.job_store:
        return "master"
    return str(getattr(service.job_store, "JOB_KIND_MASTER", "master"))



def cycle_sub_job_kind(service: "SchedulerService") -> str:
    """서브 잡 kind 상수를 안전하게 반환한다."""
    if not service.job_store:
        return "sub"
    return str(getattr(service.job_store, "JOB_KIND_SUB", "sub"))



def cycle_get_today_post_count(service: "SchedulerService") -> int:
    if not service.job_store:
        return 0
    try:
        return service.job_store.get_today_completed_count(
            job_kind=service._master_job_kind()
        )
    except TypeError:
        return service.job_store.get_today_completed_count()



def cycle_get_last_post_time(service: "SchedulerService") -> Optional[datetime]:
    if not service.job_store:
        return None
    try:
        return service.job_store.get_last_completed_time(
            job_kind=service._master_job_kind()
        )
    except TypeError:
        return service.job_store.get_last_completed_time()



def cycle_get_today_failed_count(service: "SchedulerService") -> int:
    if not service.job_store:
        return 0
    get_count = getattr(service.job_store, "get_today_failed_count", None)
    if get_count and callable(get_count):
        try:
            return int(get_count(job_kind=service._master_job_kind()))
        except TypeError:
            return int(get_count())
    return 0




async def run_scheduler_forever(service_cls, 
    daily_posts_target: Optional[int] = None,
    db_path: Optional[str] = None,
    min_post_interval_minutes: int = 60,
    publish_interval_max_minutes: int = 110,
    cpu_start_threshold_percent: float = 40.0,
    cpu_stop_threshold_percent: float = 55.0,
    cpu_avg_window: int = 5,
    memory_threshold_percent: float = 88.0,
    generator_poll_seconds: int = constants.DEFAULT_GENERATOR_POLL_SECONDS,
    publisher_poll_seconds: int = constants.DEFAULT_PUBLISHER_POLL_SECONDS,
    random_seed: Optional[int] = None,
) -> None:
    """스케줄러를 시작하고 종료 신호까지 대기한다."""
    from ..collectors.metrics_collector import MetricsCollector
    from ..config import load_config
    from ..images.runtime_factory import build_runtime_image_generator
    from ..llm import get_generator, llm_generate_fn
    from ..logging_config import setup_logging
    from ..uploaders.playwright_publisher import PlaywrightPublisher
    from .job_store import JobConfig, JobStore
    from .notifier import TelegramNotifier
    from .pipeline_service import PipelineService, stub_generate_fn
    from .trend_job_service import TrendJobService

    import os

    config = load_config()
    setup_logging(level=config.logging.level, log_format=config.logging.format)

    resolved_db_path = db_path or os.getenv("AUTOBLOG_DB_PATH", "data/automation.db")
    resolved_daily_posts_target = (
        int(daily_posts_target)
        if daily_posts_target is not None
        else service_cls.DEFAULT_DAILY_TARGET
    )
    if resolved_daily_posts_target < 1:
        resolved_daily_posts_target = service_cls.DEFAULT_DAILY_TARGET

    job_config = JobConfig(max_llm_calls_per_job=config.pipeline.max_llm_calls_per_job)
    job_store = JobStore(db_path=resolved_db_path, config=job_config)
    metrics_collector = MetricsCollector(db_path=job_store.db_path)

    dry_run = False
    blog_id = ""

    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    blog_id = os.getenv("NAVER_BLOG_ID", "")

    if dry_run and not blog_id:
        blog_id = "dry-run"

    notifier = TelegramNotifier.from_env()
    publisher = PlaywrightPublisher(blog_id=blog_id or "dry-run")
    image_generator = None
    try:
        image_generator = build_runtime_image_generator(
            app_config=config,
            job_store=job_store,
        )
        if image_generator:
            logger.info("Scheduler image backend: router-driven runtime factory")
    except Exception as exc:
        logger.warning("Scheduler image runtime init failed: %s", exc)

    generate_fn = stub_generate_fn
    try:
        # 스케줄러는 LLM 생성기를 기본 사용하되, 초기화 실패 시 stub로 안전 폴백한다.
        get_generator(
            config.llm,
            job_store=job_store,
            notifier=notifier,
        )
        generate_fn = llm_generate_fn
        logger.info("Scheduler generation backend: llm")
    except Exception as exc:
        logger.warning("Scheduler LLM init failed, fallback to stub: %s", exc)

    quality_evaluator = None
    feedback_analyzer = None
    vlm_evaluator = None
    try:
        from ..llm.provider_factory import create_client
        from .quality_evaluator import QualityEvaluator
        eval_client = create_client(
            provider=config.llm.primary_provider,
            model=config.llm.primary_model,
            timeout_sec=config.llm.timeout_sec,
        )
        quality_evaluator = QualityEvaluator(llm_client=eval_client)
        logger.info("Scheduler quality evaluator backend: llm")
    except Exception as exc:
        logger.warning("Scheduler quality evaluator init failed: %s", exc)

    try:
        from ..evaluation.visual_evaluator import VisualQualityEvaluator
        from ..llm.circuit_breaker import ProviderCircuitBreaker
        from ..llm.llm_router import LLMRouter
        from ..llm.provider_factory import create_client
        from ..llm.vlm_router import VLMRouter, normalize_vlm_strategy_mode

        router = LLMRouter(job_store=job_store, llm_config=config.llm)
        saved = router.get_saved_settings()
        text_keys = dict(saved.get("text_api_keys", {}))
        vlm_enabled = bool(saved.get("vlm_enabled", False))
        if vlm_enabled:
            strategy_mode = normalize_vlm_strategy_mode(
                saved.get("vlm_strategy_mode", constants.VLM_DEFAULT_STRATEGY_MODE),
                fallback_strategy=saved.get("strategy_mode", "cost"),
            )
            vlm_router = VLMRouter(job_store=job_store)
            route_chain = vlm_router.build_route(
                strategy_mode=strategy_mode,
                text_api_keys=text_keys,
                preferred_model=str(saved.get("vlm_model", constants.VLM_DEFAULT_MODEL)),
                quality_floor=float(saved.get("vlm_quality_floor", constants.VLM_DEFAULT_QUALITY_FLOOR)),
                max_cost_guard_krw=float(
                    saved.get("vlm_max_cost_guard_krw", constants.VLM_DEFAULT_MAX_COST_GUARD_KRW)
                ),
                max_candidates=constants.VLM_ROUTER_MAX_CANDIDATES,
            )
            if route_chain:
                vlm_breaker = ProviderCircuitBreaker(
                    job_store=job_store,
                    notifier=notifier,
                    fail_threshold=3,
                    open_ttl_seconds=300,
                )
                vlm_breaker.load_all_from_db(
                    [f"{item.client_provider}:{item.model}".strip().lower() for item in route_chain]
                )

                clients = []
                score_bias_map: Dict[str, float] = {}
                route_labels = []
                for item in route_chain:
                    api_key = str(text_keys.get(item.key_id, "")).strip()
                    if not api_key:
                        continue
                    try:
                        client = create_client(
                            provider=item.client_provider,
                            model=item.model,
                            timeout_sec=constants.VLM_REQUEST_TIMEOUT_SEC,
                            api_key=api_key,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Scheduler VLM client init skipped: %s/%s (%s)",
                            item.client_provider,
                            item.model,
                            exc,
                        )
                        continue
                    clients.append(client)
                    score_bias_map[f"{item.client_provider}:{item.model}".strip().lower()] = float(
                        item.scoring_bias_offset or 0.0
                    )
                    route_labels.append(f"{item.client_provider}:{item.model}")

                if clients:
                    primary_client, *fallback_clients = clients
                    vlm_evaluator = VisualQualityEvaluator(
                        vlm_client=primary_client,
                        fallback_clients=fallback_clients,
                        circuit_breaker=vlm_breaker,
                        score_bias_map=score_bias_map,
                    )
                    logger.info("Scheduler visual evaluator backend: %s", " -> ".join(route_labels))
                else:
                    logger.info("Scheduler visual evaluator disabled (no available VLM clients)")
            else:
                logger.info("Scheduler visual evaluator disabled (no route candidates)")
        else:
            logger.info(
                "Scheduler visual evaluator disabled (enabled=%s)",
                vlm_enabled,
            )
    except Exception as exc:
        logger.warning("Scheduler visual evaluator init failed: %s", exc)

    try:
        from ..seo.feedback_analyzer import FeedbackAnalyzer
        # FeedbackAnalyzer용 LLM 클라이언트: 분석 전용이므로 동일 프로바이더 사용
        feedback_llm_client = None
        try:
            from ..llm.provider_factory import create_client as _create_fb_client
            feedback_llm_client = _create_fb_client(
                provider=config.llm.primary_provider,
                model=config.llm.primary_model,
                timeout_sec=config.llm.timeout_sec,
            )
        except Exception:
            pass
        feedback_analyzer = FeedbackAnalyzer(
            db_path=job_store.db_path,
            llm_client=feedback_llm_client,
        )
        logger.info("Scheduler feedback analyzer: enabled")
    except Exception as exc:
        logger.warning("Scheduler feedback analyzer init failed: %s", exc)

    # ── 메모리 스토어 초기화 (Phase 2) ──
    _scheduler_memory_store = None
    try:
        if config.memory.enabled:
            from ..memory.topic_store import TopicMemoryStore
            _scheduler_memory_store = TopicMemoryStore(
                job_store=job_store,
                config=config.memory,
            )
    except Exception as _mem_exc:
        import logging as _logging
        _logging.getLogger(__name__).warning("Scheduler memory store init failed: %s", _mem_exc)

    trend_service = TrendJobService(
        job_store=job_store,
        memory_store=_scheduler_memory_store,
    )

    pipeline_service = PipelineService(
        job_store=job_store,
        publisher=publisher,
        generate_fn=generate_fn,
        notifier=notifier,
        internal_retry_attempts=1,
        queue_retry_limit=1,
        retry_max_attempts=config.retry.max_retries,
        retry_backoff_base_sec=config.retry.backoff_base_sec,
        retry_backoff_max_sec=config.retry.backoff_max_sec,
        image_generator=image_generator,
        quality_evaluator=quality_evaluator,
        vlm_evaluator=vlm_evaluator,
        memory_store=_scheduler_memory_store,
    )

    scheduler = service_cls(
        trend_service=trend_service,
        pipeline_service=pipeline_service,
        metrics_collector=metrics_collector,
        feedback_analyzer=feedback_analyzer,
        job_store=job_store,
        timezone_name="Asia/Seoul",
        daily_posts_target=resolved_daily_posts_target,
        min_post_interval_minutes=min_post_interval_minutes,
        publish_interval_min_minutes=min_post_interval_minutes,
        publish_interval_max_minutes=publish_interval_max_minutes,
        cpu_start_threshold_percent=cpu_start_threshold_percent,
        cpu_stop_threshold_percent=cpu_stop_threshold_percent,
        cpu_avg_window=cpu_avg_window,
        memory_threshold_percent=memory_threshold_percent,
        generator_poll_seconds=generator_poll_seconds,
        publisher_poll_seconds=publisher_poll_seconds,
        random_seed=random_seed,
        notifier=notifier,
        memory_store=_scheduler_memory_store,
    )
    await scheduler.start()

    try:
        while True:
            await asyncio.sleep(constants.SCHEDULER_DAEMON_KEEPALIVE_SEC)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await scheduler.stop()
    finally:
        if image_generator:
            close_fn = getattr(image_generator, "close", None)
            if close_fn and callable(close_fn):
                try:
                    await close_fn()
                except Exception:
                    pass
