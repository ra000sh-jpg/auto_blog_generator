"""스케줄러 시드 생성 책임 분리 모듈."""

from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List

from ..constants import DEFAULT_FALLBACK_CATEGORY

if TYPE_CHECKING:
    from .scheduler_service import SchedulerService

logger = logging.getLogger(__name__)


async def run_daily_quota_seed(service: "SchedulerService") -> None:
    """매일 자정에 사용자 설정 비율대로 큐를 생성한다."""
    if not service.job_store:
        return

    now_local = service._get_now_local()
    today_local = now_local.date().isoformat()
    last_seed_date = service.job_store.get_system_setting("scheduler_last_seed_date", "")
    if last_seed_date == today_local:
        return

    daily_target, allocations = service._load_daily_quota_allocations()
    if daily_target <= 0:
        return

    idea_vault_quota = service._get_configured_idea_vault_quota(daily_target)
    non_vault_target = max(0, daily_target - idea_vault_quota)

    # 가중 확률 랜덤 추출 (random.choices)
    selected_categories: List[Dict[str, Any]] = []
    if non_vault_target > 0 and allocations:
        population: List[Dict[str, Any]] = []
        weights: List[int] = []
        for alloc in allocations:
            w = int(alloc.get("count", 0))
            if w > 0:
                population.append(alloc)
                weights.append(w)

        if population:
            selected_categories = random.choices(population, weights=weights, k=non_vault_target)
        else:
            # 가중치가 모두 0인 경우 fallback으로 채움
            fallback_alloc = {
                "category": DEFAULT_FALLBACK_CATEGORY,
                "topic_mode": "cafe",
            }
            selected_categories = [fallback_alloc] * non_vault_target

    created = 0
    created_non_vault = 0
    created_idea_vault = 0
    seed_base_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    sequence = 0

    for allocation in selected_categories:
        category_name = str(allocation.get("category", "")).strip()
        topic_mode = str(allocation.get("topic_mode", "cafe")).strip()
        if not category_name:
            continue

        persona_id = service._persona_id_for_topic(topic_mode)
        scheduled_at = (seed_base_utc + timedelta(minutes=sequence)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sequence += 1
        title = service._build_seed_title(
            category=category_name,
            topic_mode=topic_mode,
            local_date=today_local,
            sequence=sequence,
        )
        seed_keywords = service._build_seed_keywords(category_name, topic_mode)
        success = service.job_store.schedule_job(
            job_id=str(uuid.uuid4()),
            title=title,
            seed_keywords=seed_keywords,
            platform="naver",
            persona_id=persona_id,
            scheduled_at=scheduled_at,
            max_retries=3,
            category=category_name,
        )
        if success:
            created += 1
            created_non_vault += 1

    if idea_vault_quota > 0:
        claim_fn = getattr(service.job_store, "claim_random_idea_vault_items", None)
        release_fn = getattr(service.job_store, "release_idea_vault_job_lock", None)
        seen_duplicate_idea_ids: set[int] = set()
        if claim_fn and callable(claim_fn):
            idea_job_ids = [str(uuid.uuid4()) for _ in range(idea_vault_quota)]
            claimed_items = claim_fn(idea_job_ids)
            if len(claimed_items) < idea_vault_quota:
                logger.info(
                    "Idea vault stock is short; strict holiday rule keeps unfilled quota",
                    extra={
                        "requested": idea_vault_quota,
                        "claimed": len(claimed_items),
                    },
                )
            for claimed in claimed_items:
                idea_row_id = 0
                try:
                    idea_row_id = int(claimed.get("id", 0) or 0)
                except Exception:
                    idea_row_id = 0
                if idea_row_id and idea_row_id in seen_duplicate_idea_ids:
                    continue

                idea_job_id = str(claimed.get("queued_job_id", "")).strip()
                raw_text = str(claimed.get("raw_text", "")).strip()
                category_name = str(claimed.get("mapped_category", "")).strip() or DEFAULT_FALLBACK_CATEGORY
                topic_mode = service._normalize_topic_mode(str(claimed.get("topic_mode", "")).strip())
                if not idea_job_id or not raw_text:
                    continue
                sequence += 1
                scheduled_at = (
                    seed_base_utc + timedelta(minutes=sequence)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                title = service._build_vault_seed_title(
                    raw_text=raw_text,
                    local_date=today_local,
                    sequence=sequence,
                )
                seed_keywords = service._build_vault_seed_keywords(
                    raw_text=raw_text,
                    category=category_name,
                    topic_mode=topic_mode,
                )

                # 중복으로 판단되면 아이디어 잠금을 즉시 해제하고 이번 배치에서 재선점하지 않는다.
                _memory_store = getattr(service, "memory_store", None)
                if _memory_store is not None:
                    try:
                        _memory_config = getattr(_memory_store, "_config", None)
                        _threshold = 0.50
                        if _memory_config is not None:
                            _threshold = float(
                                getattr(
                                    _memory_config,
                                    "precheck_duplicate_threshold",
                                    getattr(_memory_config, "duplicate_threshold", 0.50),
                                )
                            )
                        _is_duplicate = bool(
                            _memory_store.is_duplicate_before_job(
                                title=title,
                                keywords=seed_keywords,
                                topic_mode=topic_mode,
                                similarity_threshold=_threshold,
                                lookback_weeks=None,
                                platform="naver",
                            )
                        )
                        if _is_duplicate:
                            logger.info(
                                "Idea vault job skipped (duplicate in topic_memory): %s",
                                title[:60],
                                extra={"topic_mode": topic_mode},
                            )
                            if idea_row_id:
                                seen_duplicate_idea_ids.add(idea_row_id)
                            if release_fn and callable(release_fn):
                                release_fn(idea_job_id)
                            continue
                    except Exception as dup_exc:
                        logger.debug("Idea vault duplicate check failed (non-critical): %s", dup_exc)

                persona_id = service._persona_id_for_topic(topic_mode)
                success = service.job_store.schedule_job(
                    job_id=idea_job_id,
                    title=title,
                    seed_keywords=seed_keywords,
                    platform="naver",
                    persona_id=persona_id,
                    scheduled_at=scheduled_at,
                    max_retries=3,
                    tags=["idea_vault"],
                    category=category_name,
                )
                if success:
                    created += 1
                    created_idea_vault += 1
                elif release_fn and callable(release_fn):
                    release_fn(idea_job_id)
        else:
            logger.debug("Idea vault claim function is not available")

    service.job_store.set_system_setting("scheduler_last_seed_date", today_local)
    service.job_store.set_system_setting("scheduler_last_seed_count", str(created))
    service.job_store.set_system_setting("scheduler_last_seed_non_vault_count", str(created_non_vault))
    service.job_store.set_system_setting("scheduler_last_seed_idea_vault_count", str(created_idea_vault))
    logger.info(
        "Daily quota seed completed",
        extra={
            "date": today_local,
            "target": daily_target,
            "created_count": created,
            "allocation_count": len(selected_categories),
            "idea_vault_quota": idea_vault_quota,
            "created_non_vault": created_non_vault,
            "created_idea_vault": created_idea_vault,
        },
    )
