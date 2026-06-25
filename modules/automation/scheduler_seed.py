"""스케줄러 시드 생성 책임 분리 모듈."""

from __future__ import annotations

import logging
import os
import random
import uuid
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List
from zoneinfo import ZoneInfo

from ..constants import DEFAULT_FALLBACK_CATEGORY
from ..market import (
    BlogSlot,
    MarketOpenState,
    MarketScope,
    get_default_daily_slots,
    get_us_preopen_kst,
    resolve_daily_slots,
)

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

    strategy_mode = _scheduler_strategy_mode(service)
    if strategy_mode == "market_plus_category_ramp":
        await _run_market_plus_category_ramp_seed(
            service=service,
            now_local=now_local,
            today_local=today_local,
            daily_target=daily_target,
        )
        return

    if _is_market_daily_seed_enabled(service):
        await _run_market_daily_seed(
            service=service,
            now_local=now_local,
            today_local=today_local,
            daily_target=daily_target,
        )
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


async def _run_market_plus_category_ramp_seed(
    *,
    service: "SchedulerService",
    now_local: datetime,
    today_local: str,
    daily_target: int,
) -> None:
    """경제 4편 기본 + 카테고리 확장 램프 시드를 생성한다."""

    if not service.job_store:
        return

    local_dt = _ensure_local_datetime(now_local, service.timezone_name)
    local_date = local_dt.date()
    state = _build_market_open_state(service, local_date)
    if not state.krx_open:
        service.job_store.set_system_setting("scheduler_last_seed_date", today_local)
        service.job_store.set_system_setting("scheduler_last_seed_count", "0")
        service.job_store.set_system_setting("scheduler_last_seed_non_vault_count", "0")
        service.job_store.set_system_setting("scheduler_last_seed_idea_vault_count", "0")
        service.job_store.set_system_setting("scheduler_last_seed_market_count", "0")
        service.job_store.set_system_setting("scheduler_last_seed_category_expansion_count", "0")
        service.job_store.set_system_setting("scheduler_last_seed_mode", "market_plus_category_ramp")
        service.job_store.set_system_setting("scheduler_last_seed_skip_reason", "krx_closed")
        logger.info(
            "Market plus category ramp skipped because KRX is closed",
            extra={"date": today_local, "is_weekend": state.is_weekend},
        )
        return

    market_base_target = _market_base_target(service)
    await _run_market_daily_seed(
        service=service,
        now_local=local_dt,
        today_local=today_local,
        daily_target=market_base_target,
    )
    market_created = _count_jobs_for_local_date(
        service=service,
        local_date=local_date,
        required_tag="market_daily",
    )
    fallback_market_count = _ensure_market_base_jobs(
        service=service,
        local_date=local_date,
        market_base_target=market_base_target,
        current_count=market_created,
    )
    market_created += fallback_market_count

    ramp_topics, ramp_week = _category_ramp_topics(service=service, local_date=local_date)
    category_created = 0
    for index, topic_mode in enumerate(ramp_topics, start=1):
        brief = _build_category_expansion_brief(
            service=service,
            topic_mode=topic_mode,
        )
        sequence = market_created + index
        success = service.job_store.schedule_job(
            job_id=str(uuid.uuid4()),
            title=brief.title,
            seed_keywords=list(brief.seed_keywords),
            platform="naver",
            persona_id=service._persona_id_for_topic(brief.topic_mode),
            scheduled_at=_scheduled_at_for_category_expansion(
                local_date=local_date,
                timezone_name=service.timezone_name,
                topic_mode=brief.topic_mode,
                fallback_index=index,
            ),
            max_retries=3,
            tags=_tags_for_category_expansion(
                brief=brief,
                sequence=sequence,
                local_date=local_date,
                ramp_week=ramp_week,
            ),
            category=brief.category,
        )
        if success:
            category_created += 1

    total_created = market_created + category_created
    service.job_store.set_system_setting("scheduler_last_seed_date", today_local)
    service.job_store.set_system_setting("scheduler_last_seed_count", str(total_created))
    service.job_store.set_system_setting("scheduler_last_seed_non_vault_count", str(total_created))
    service.job_store.set_system_setting("scheduler_last_seed_idea_vault_count", "0")
    service.job_store.set_system_setting("scheduler_last_seed_market_count", str(market_created))
    service.job_store.set_system_setting("scheduler_last_seed_category_expansion_count", str(category_created))
    service.job_store.set_system_setting("scheduler_last_seed_category_ramp_week", str(ramp_week))
    service.job_store.set_system_setting(
        "scheduler_last_seed_category_topics",
        json.dumps(ramp_topics, ensure_ascii=False),
    )
    service.job_store.set_system_setting("scheduler_last_seed_mode", "market_plus_category_ramp")
    logger.info(
        "Market plus category ramp seed completed",
        extra={
            "date": today_local,
            "market_count": market_created,
            "category_count": category_created,
            "ramp_week": ramp_week,
            "topics": ramp_topics,
            "daily_target": daily_target,
        },
    )


async def _run_market_daily_seed(
    *,
    service: "SchedulerService",
    now_local: datetime,
    today_local: str,
    daily_target: int,
) -> None:
    """시장 브리핑 중심 시드를 생성한다."""

    if not service.job_store:
        return

    local_dt = _ensure_local_datetime(now_local, service.timezone_name)
    local_date = local_dt.date()
    state = _build_market_open_state(service, local_date)
    resolved_slots = resolve_daily_slots(state)
    created = 0
    market_count = 0
    evergreen_count = 0
    opportunity_count = 0
    selected_opportunity_keywords: set[str] = set()

    for sequence, original_slot in enumerate(get_default_daily_slots(), start=1):
        resolved_slot = resolved_slots.get(original_slot, original_slot)
        scope = _scope_for_slot(resolved_slot)
        scheduled_at = _scheduled_at_for_slot(
            local_date=local_date,
            original_slot=original_slot,
            timezone_name=service.timezone_name,
        )
        tags = _tags_for_market_slot(
            original_slot=original_slot,
            resolved_slot=resolved_slot,
            scope=scope,
            sequence=sequence,
            local_date=local_date,
        )
        title = _title_for_market_slot(
            local_date=local_date,
            original_slot=original_slot,
            resolved_slot=resolved_slot,
            sequence=sequence,
        )
        seed_keywords = _keywords_for_market_slot(resolved_slot, original_slot)
        category = "경제 브리핑"
        opportunity = _build_kr_preopen_opportunity_seed(
            service=service,
            original_slot=original_slot,
            resolved_slot=resolved_slot,
            local_dt=local_dt,
        )
        if opportunity:
            title = str(opportunity.get("title", "") or title)
            seed_keywords = list(opportunity.get("seed_keywords", seed_keywords) or seed_keywords)
            tags.extend(str(tag) for tag in opportunity.get("tags", []) if str(tag).strip())
            category = str(opportunity.get("category", category) or category)
            keyword = str(opportunity.get("opportunity_keyword", "") or "").strip().lower()
            if keyword:
                selected_opportunity_keywords.add(keyword)
        tags = _unique_nonempty(
            [
                *tags,
                *_writing_strategy_tags_for_market(
                    title=title,
                    tags=tags,
                    seed_keywords=seed_keywords,
                ),
            ]
        )

        success = service.job_store.schedule_job(
            job_id=str(uuid.uuid4()),
            title=title,
            seed_keywords=seed_keywords,
            platform="naver",
            persona_id=service._persona_id_for_topic("finance"),
            scheduled_at=scheduled_at,
            max_retries=3,
            tags=tags,
            category=category,
        )
        if not success:
            continue
        created += 1
        if resolved_slot in {BlogSlot.KR_PREOPEN, BlogSlot.US_PREOPEN}:
            market_count += 1
        else:
            evergreen_count += 1

    extra_limit = _market_extra_opportunity_limit(
        service=service,
        daily_target=daily_target,
        base_count=len(get_default_daily_slots()),
    )
    extra_opportunities = _build_market_extra_opportunity_seeds(
        service=service,
        local_dt=local_dt,
        local_date=local_date,
        limit=extra_limit,
        exclude_keywords=selected_opportunity_keywords,
    )
    for extra_index, extra in enumerate(extra_opportunities, start=1):
        sequence = len(get_default_daily_slots()) + extra_index
        extra_title = str(extra.get("title", "") or _title_for_extra_opportunity(local_date, extra_index))
        extra_seed_keywords = list(extra.get("seed_keywords", []) or [])
        extra_tags = [
            "market_daily",
            f"daily_slot:{sequence}",
            "market_slot:opportunity",
            "market_origin_slot:opportunity",
            f"market_scope:{MarketScope.KR.value}",
            f"local_date:{local_date.isoformat()}",
            "market_extra:opportunity",
            *[str(tag) for tag in extra.get("tags", []) if str(tag).strip()],
        ]
        extra_tags = _unique_nonempty(
            [
                *extra_tags,
                *_writing_strategy_tags_for_market(
                    title=extra_title,
                    tags=extra_tags,
                    seed_keywords=extra_seed_keywords,
                ),
            ]
        )
        success = service.job_store.schedule_job(
            job_id=str(uuid.uuid4()),
            title=extra_title,
            seed_keywords=extra_seed_keywords,
            platform="naver",
            persona_id=service._persona_id_for_topic("finance"),
            scheduled_at=_scheduled_at_for_extra_opportunity(
                local_date=local_date,
                timezone_name=service.timezone_name,
                extra_index=extra_index,
            ),
            max_retries=3,
            tags=extra_tags,
            category=str(extra.get("category", "경제 브리핑") or "경제 브리핑"),
        )
        if not success:
            continue
        created += 1
        market_count += 1
        opportunity_count += 1

    service.job_store.set_system_setting("scheduler_last_seed_date", today_local)
    service.job_store.set_system_setting("scheduler_last_seed_count", str(created))
    service.job_store.set_system_setting("scheduler_last_seed_non_vault_count", str(created))
    service.job_store.set_system_setting("scheduler_last_seed_idea_vault_count", "0")
    service.job_store.set_system_setting("scheduler_last_seed_market_count", str(market_count))
    service.job_store.set_system_setting("scheduler_last_seed_evergreen_count", str(evergreen_count))
    service.job_store.set_system_setting("scheduler_last_seed_opportunity_count", str(opportunity_count))
    service.job_store.set_system_setting("scheduler_last_seed_mode", "market_daily")
    logger.info(
        "Market daily seed completed",
        extra={
            "date": today_local,
            "created_count": created,
            "market_count": market_count,
            "evergreen_count": evergreen_count,
            "opportunity_count": opportunity_count,
            "is_weekend": state.is_weekend,
            "krx_open": state.krx_open,
            "us_open": state.us_open,
        },
    )


def _is_market_daily_seed_enabled(service: "SchedulerService") -> bool:
    """시장 브리핑 고정 슬롯 모드 여부를 반환한다."""

    raw = ""
    if service.job_store:
        raw = service.job_store.get_system_setting(
            "scheduler_market_daily_enabled",
            os.getenv("SCHEDULER_MARKET_DAILY_ENABLED", "true"),
        )
    else:
        raw = os.getenv("SCHEDULER_MARKET_DAILY_ENABLED", "true")
    return _is_truthy(raw)


def _scheduler_strategy_mode(service: "SchedulerService") -> str:
    """스케줄러 전략 모드를 반환한다."""

    raw = _service_setting(
        service,
        "scheduler_strategy_mode",
        os.getenv("SCHEDULER_STRATEGY_MODE", ""),
    )
    return str(raw or "").strip().lower()


def _market_base_target(service: "SchedulerService") -> int:
    """확장 전략에서 사용할 경제 기본 발행 수를 반환한다."""

    raw = _service_setting(
        service,
        "scheduler_market_base_target",
        os.getenv("SCHEDULER_MARKET_BASE_TARGET", "4"),
    )
    try:
        value = int(float(str(raw or "4").strip()))
    except Exception:
        value = 4
    return max(3, min(7, value))


def _category_ramp_topics(
    *,
    service: "SchedulerService",
    local_date: date,
) -> tuple[list[str], int]:
    """램프 시작일 기준으로 확장 카테고리 목록과 주차를 반환한다."""

    raw_start = _service_setting(service, "category_ramp_start_date", "")
    start_date = _parse_date(raw_start)
    if start_date is None:
        start_date = local_date
        if service.job_store:
            service.job_store.set_system_setting("category_ramp_start_date", start_date.isoformat())

    days = max(0, (local_date - start_date).days)
    ramp_week = min(3, (days // 7) + 1)
    if ramp_week <= 1:
        return ["it"], 1
    if ramp_week == 2:
        return ["it", "health"], 2
    return ["it", "health", "parenting"], 3


def _build_category_expansion_brief(
    *,
    service: "SchedulerService",
    topic_mode: str,
):
    """카테고리 확장 글감 요약을 만든다."""

    from ..content_sources import CategoryOpportunityEngine

    template_mode = _service_setting(service, "category_template_mode", "auto") or "auto"
    engine = CategoryOpportunityEngine(
        sources=_load_creator_sources(service),
        source_items=_load_category_source_items(service),
    )
    return engine.build_brief(
        topic_mode=topic_mode,
        template_mode=template_mode,
        recent_template_ids=_recent_category_template_ids(service, topic_mode=topic_mode),
    )


def _load_creator_sources(service: "SchedulerService"):
    """설정에 등록된 외부 채널 watchlist를 기본값에 병합한다."""

    from ..content_sources import CreatorSource, default_creator_sources

    sources = list(default_creator_sources())
    raw = _service_setting(service, "creator_source_watchlist", "")
    if not raw:
        return sources
    try:
        payload = json.loads(raw)
    except Exception:
        logger.debug("creator_source_watchlist parsing failed")
        return sources
    if not isinstance(payload, list):
        return sources
    for item in payload:
        if not isinstance(item, dict):
            continue
        topic_mode = str(item.get("topic_mode") or item.get("topic") or "").strip().lower()
        platform = str(item.get("platform") or "manual").strip().lower()
        channel_name = str(item.get("channel_name") or item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if topic_mode not in {"it", "health", "parenting"} or not channel_name:
            continue
        try:
            priority = int(float(str(item.get("priority", 50))))
        except Exception:
            priority = 50
        sources.append(
            CreatorSource(
                topic_mode=topic_mode,
                platform=platform,
                channel_name=channel_name,
                url=url,
                priority=max(1, min(100, priority)),
            )
        )
    return sources


def _load_category_source_items(service: "SchedulerService"):
    """수동/공개 링크 기반 최신 글감 후보 설정을 읽는다."""

    from ..content_sources import SourceItem

    raw = _service_setting(service, "category_source_items", "")
    if not raw:
        raw = _service_setting(service, "creator_source_items", "")
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except Exception:
        logger.debug("category_source_items parsing failed")
        return []
    if not isinstance(payload, list):
        return []
    items = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        topic_mode = str(item.get("topic_mode") or item.get("topic") or "").strip().lower()
        title = str(item.get("title") or "").strip()
        if topic_mode not in {"it", "health", "parenting"} or not title:
            continue
        raw_keywords = item.get("keywords", [])
        keywords = tuple(
            str(keyword).strip()
            for keyword in raw_keywords
            if str(keyword).strip()
        ) if isinstance(raw_keywords, list) else ()
        items.append(
            SourceItem(
                topic_mode=topic_mode,
                title=title,
                source_name=str(item.get("source_name") or item.get("channel_name") or item.get("name") or "").strip(),
                platform=str(item.get("platform") or "manual").strip().lower(),
                url=str(item.get("url") or "").strip(),
                summary=str(item.get("summary") or item.get("description") or "").strip(),
                published_at=str(item.get("published_at") or "").strip(),
                keywords=keywords,
            )
        )
    return items


def _recent_category_template_ids(
    service: "SchedulerService",
    *,
    topic_mode: str,
    limit: int = 12,
) -> list[str]:
    """최근 사용한 카테고리 템플릿 ID를 반환한다."""

    if not service.job_store:
        return []
    try:
        with service.job_store.connection() as conn:
            rows = conn.execute(
                """
                SELECT tags FROM jobs
                WHERE tags LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (f"%category_topic:{topic_mode}%", int(limit)),
            ).fetchall()
    except Exception:
        return []
    template_ids: list[str] = []
    for row in rows:
        try:
            tags = json.loads(row["tags"] or "[]")
        except Exception:
            tags = []
        for tag in tags:
            raw = str(tag or "").strip()
            if raw.lower().startswith("category_template:"):
                template_ids.append(raw.split(":", 1)[1])
                break
    return template_ids


def _ensure_market_base_jobs(
    *,
    service: "SchedulerService",
    local_date: date,
    market_base_target: int,
    current_count: int,
) -> int:
    """시장 글감 엔진이 부족할 때 경제 기본 편수를 보수적으로 채운다."""

    missing = max(0, int(market_base_target or 0) - int(current_count or 0))
    created = 0
    for index in range(1, missing + 1):
        sequence = current_count + index
        title = f"{local_date.isoformat()} 시장 기회 점검 {index} - 오늘 다시 볼 변수"
        seed_keywords = ["시장 기회", "경제 브리핑", "투자 공부", "리스크 관리"]
        tags = [
            "market_daily",
            f"daily_slot:{sequence}",
            "market_slot:opportunity",
            "market_origin_slot:opportunity",
            f"market_scope:{MarketScope.KR.value}",
            f"local_date:{local_date.isoformat()}",
            "market_extra:fallback",
            "opportunity_status:fallback",
            "오늘의증시",
            "시장체크",
            "경제공부",
        ]
        tags = _unique_nonempty(
            [
                *tags,
                *_writing_strategy_tags_for_market(
                    title=title,
                    tags=tags,
                    seed_keywords=seed_keywords,
                ),
            ]
        )
        success = service.job_store.schedule_job(
            job_id=str(uuid.uuid4()),
            title=title,
            seed_keywords=seed_keywords,
            platform="naver",
            persona_id=service._persona_id_for_topic("finance"),
            scheduled_at=_scheduled_at_for_extra_opportunity(
                local_date=local_date,
                timezone_name=service.timezone_name,
                extra_index=index,
            ),
            max_retries=3,
            tags=tags,
            category="경제 브리핑",
        )
        if success:
            created += 1
    return created


def _count_jobs_for_local_date(
    *,
    service: "SchedulerService",
    local_date: date,
    required_tag: str,
) -> int:
    """특정 로컬 날짜 태그를 가진 job 수를 센다."""

    if not service.job_store:
        return 0
    try:
        with service.job_store.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM jobs
                WHERE tags LIKE ? AND tags LIKE ?
                """,
                (f"%local_date:{local_date.isoformat()}%", f"%{required_tag}%"),
            ).fetchone()
        return int(row["count"] if row is not None else 0)
    except Exception:
        return 0


def _scheduled_at_for_category_expansion(
    *,
    local_date: date,
    timezone_name: str,
    topic_mode: str,
    fallback_index: int,
) -> str:
    """확장 카테고리 예약 시각을 UTC ISO로 반환한다."""

    anchors = {
        "it": time(hour=10, minute=40),
        "health": time(hour=15, minute=40),
        "parenting": time(hour=21, minute=40),
    }
    anchor = anchors.get(str(topic_mode or "").strip().lower())
    if anchor is None:
        anchor = time(hour=10 + max(0, int(fallback_index) - 1) * 3, minute=40)
    local_dt = datetime.combine(local_date, anchor, tzinfo=_safe_zoneinfo(timezone_name))
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tags_for_category_expansion(
    *,
    brief,
    sequence: int,
    local_date: date,
    ramp_week: int,
) -> list[str]:
    """확장 카테고리 job 태그를 만든다."""

    source_tags = [
        f"creator_source:{_tag_safe(source.channel_name)}"
        for source in list(getattr(brief, "sources", ()) or ())[:4]
        if str(getattr(source, "channel_name", "")).strip()
    ]
    safety_issues = list(getattr(brief, "safety_issues", ()) or ())
    tags = [
        *list(getattr(brief, "tags", ()) or ()),
        "category_expansion",
        f"daily_slot:{sequence}",
        f"category_topic:{brief.topic_mode}",
        f"category_template:{brief.template_id}",
        f"category_score:{int(round(float(brief.score.total)))}",
        f"category_ramp_week:{ramp_week}",
        f"local_date:{local_date.isoformat()}",
        "approval_required:category_expansion",
        *source_tags,
    ]
    if safety_issues:
        tags.append("category_safety:needs_review")
    tags.extend(_writing_strategy_tags_for_category(brief=brief, tags=tags))
    return _unique_nonempty(tags)


def _parse_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(str(raw or "").strip())
    except Exception:
        return None


def _build_kr_preopen_opportunity_seed(
    *,
    service: "SchedulerService",
    original_slot: BlogSlot,
    resolved_slot: BlogSlot,
    local_dt: datetime,
) -> Dict[str, Any]:
    """국장전 슬롯에 시장 글감 추천 결과를 반영한다."""

    if original_slot != BlogSlot.KR_PREOPEN or resolved_slot != BlogSlot.KR_PREOPEN:
        return {}

    result = None
    try:
        from ..collectors.naver_search import NaverSearchCollector
        from ..market import MarketDataCollector, select_best_kr_preopen_opportunity

        result = select_best_kr_preopen_opportunity(
            naver_search_collector=NaverSearchCollector(),
            market_data_collector=MarketDataCollector(),
            now=local_dt.astimezone(timezone.utc),
        )
    except Exception as exc:
        logger.debug("KR preopen opportunity selection skipped: %s", exc)

    tags = ["auto_publish:kr_preopen", "publish_mode:publish"]
    if not result:
        direction_seed = _build_bigkinds_direction_seed(service=service)
        if direction_seed:
            direction_seed["tags"] = _unique_nonempty([*tags, *direction_seed.get("tags", [])])
            return direction_seed
        tags.extend(["opportunity_status:fallback", "opportunity_score:0"])
        return {"tags": tags}

    candidate, brief = result
    keyword = str(candidate.keyword).strip()
    score = int(round(float(candidate.opportunity_score or 0.0)))
    seed_keywords = [keyword, *list(candidate.entities)[:4]]
    seed_keywords = [item for item in dict.fromkeys(seed_keywords) if item]
    tags.extend(
        [
            "opportunity_status:selected",
            f"opportunity_score:{score}",
            f"opportunity_keyword:{_tag_safe(keyword)}",
            "opportunity_evidence:market",
        ]
    )
    return {
        "title": brief.title,
        "seed_keywords": seed_keywords,
        "tags": tags,
        "category": "경제 브리핑",
        "opportunity_keyword": keyword,
        "opportunity_score": score,
    }


def _build_bigkinds_direction_seed(*, service: "SchedulerService") -> Dict[str, Any]:
    """빅카인즈/네이버 신호로 국장전 방향성 제목을 만든다."""

    if not _bigkinds_direction_enabled(service):
        return {}
    try:
        from ..collectors.bigkinds_public import BigKindsPublicCollector
        from ..market.direction_signal import (
            DirectionSignalAggregator,
            collect_naver_direction_signals,
            direction_signal_to_issue_dict,
            signals_from_bigkinds_issues,
        )
        from ..market.directional_topic_planner import plan_directional_topic

        collector = BigKindsPublicCollector()
        issues = collector.collect_directional_issues(max_items=8)
        direction_signals = signals_from_bigkinds_issues(issues)
        naver_query = str(getattr(issues[0], "issue_title", "") or "") if issues else "국장 반도체 환율 수급"
        direction_signals.extend(collect_naver_direction_signals(naver_query, max_per_service=2))
        direction_plan = DirectionSignalAggregator().aggregate(
            direction_signals,
            confirmed_metrics=(),
            seed_keywords=["국장", "반도체", "환율", "수급"],
            scope="kr",
        )
        planner_issues = [direction_signal_to_issue_dict(signal) for signal in direction_plan.ranked_signals] if direction_plan else []
        if planner_issues and direction_plan is not None:
            planner_issues[0]["direction_signal_plan"] = direction_plan.to_dict()
        intent = plan_directional_topic(
            base_title="국장 개장 전 브리핑",
            issues=planner_issues or issues,
            confirmed_metrics=(),
            seed_keywords=["국장", "반도체", "환율", "수급"],
            scope="kr",
        )
    except Exception as exc:
        logger.debug("BigKinds directional seed skipped: %s", exc)
        return {}

    if intent is None or not intent.primary_title:
        return {}

    issue_title = str(intent.issue_title or "").strip()
    seed_keywords = _unique_nonempty([issue_title, *[role.metric_key for role in intent.evidence_roles]])
    if not seed_keywords:
        seed_keywords = ["국장", "오늘의 이슈", "시장 판단", "리스크 확인"]
    signal_plan = intent.direction_signal_plan or {}
    selected_signal = signal_plan.get("selected_signal", {}) if isinstance(signal_plan, dict) else {}
    signal_score = int(round(float(signal_plan.get("score", 65) or 65))) if isinstance(signal_plan, dict) else 65
    selected_source = str(selected_signal.get("source", "") if isinstance(selected_signal, dict) else "").strip()
    selected_tier = str(selected_signal.get("source_tier", "") if isinstance(selected_signal, dict) else "").strip()
    source_tag = "direction_source:bigkinds" if "bigkinds" in selected_source.lower() else "direction_source:multi_source"
    return {
        "title": intent.primary_title,
        "seed_keywords": seed_keywords[:5],
        "tags": _unique_nonempty([
            "opportunity_status:directional",
            f"opportunity_score:{signal_score}",
            source_tag,
            "direction_source:multi_source",
            f"direction_signal_source:{_tag_safe(selected_source)[:36]}" if selected_source else "",
            f"direction_signal_tier:{_tag_safe(selected_tier)[:24]}" if selected_tier else "",
            f"direction_issue:{_tag_safe(issue_title)[:40]}",
            f"direction_angle:{_tag_safe(intent.angle)}",
            f"direction_article_type:{_tag_safe(intent.article_type)[:24]}",
            "opportunity_evidence:direction_signal",
        ]),
        "category": "경제 브리핑",
        "opportunity_keyword": issue_title,
        "opportunity_score": signal_score,
    }


def _bigkinds_direction_enabled(service: "SchedulerService") -> bool:
    """빅카인즈 공개 이슈 기반 방향성 제목 활성 여부를 반환한다."""

    job_store = getattr(service, "job_store", None)
    default = os.getenv("SCHEDULER_BIGKINDS_DIRECTION_ENABLED", "false")
    try:
        raw = job_store.get_system_setting("scheduler_bigkinds_direction_enabled", default) if job_store else default
    except Exception:
        raw = default
    return _is_truthy(raw)


def _market_extra_opportunity_limit(
    *,
    service: "SchedulerService",
    daily_target: int,
    base_count: int,
) -> int:
    """하루 목표가 기본 슬롯보다 클 때 생성할 추가 기회 슬롯 수를 반환한다."""

    remaining = max(0, int(daily_target or 0) - int(base_count or 0))
    if remaining <= 0:
        return 0
    raw = _service_setting(
        service,
        "scheduler_market_extra_opportunity_limit",
        os.getenv("SCHEDULER_MARKET_EXTRA_OPPORTUNITY_LIMIT", "1"),
    )
    try:
        configured = int(float(str(raw or "1").strip()))
    except Exception:
        configured = 1
    configured = max(0, min(5, configured))
    return min(remaining, configured)


def _market_extra_opportunity_min_score(service: "SchedulerService") -> float:
    """추가 기회 슬롯으로 채택할 최소 점수를 반환한다."""

    raw = _service_setting(
        service,
        "scheduler_market_extra_opportunity_min_score",
        os.getenv("SCHEDULER_MARKET_EXTRA_OPPORTUNITY_MIN_SCORE", "75"),
    )
    try:
        score = float(str(raw or "75").strip())
    except Exception:
        score = 75.0
    return max(0.0, min(100.0, score))


def _build_market_extra_opportunity_seeds(
    *,
    service: "SchedulerService",
    local_dt: datetime,
    local_date: date,
    limit: int,
    exclude_keywords: set[str],
) -> List[Dict[str, Any]]:
    """시장 글감 엔진으로 추가 발행 후보를 만든다."""

    if limit <= 0 or local_date.weekday() >= 5:
        return []

    try:
        from ..collectors.naver_search import NaverSearchCollector
        from ..market import MarketDataCollector, MarketOpportunityEngine

        engine = MarketOpportunityEngine(
            naver_search_collector=NaverSearchCollector(),
            market_data_collector=MarketDataCollector(),
        )
        candidates = engine.discover_kr_preopen(
            top_k=max(5, int(limit) + len(exclude_keywords) + 3),
            now=local_dt.astimezone(timezone.utc),
        )
    except Exception as exc:
        logger.debug("Market extra opportunity discovery skipped: %s", exc)
        return []

    min_score = _market_extra_opportunity_min_score(service)
    selected: List[Dict[str, Any]] = []
    seen = {str(keyword or "").strip().lower() for keyword in exclude_keywords if str(keyword or "").strip()}

    for candidate in candidates:
        keyword = str(getattr(candidate, "keyword", "") or "").strip()
        keyword_key = keyword.lower()
        if not keyword or keyword_key in seen:
            continue
        score = float(getattr(candidate, "opportunity_score", 0.0) or 0.0)
        if score < min_score:
            continue
        try:
            brief = engine.build_brief(candidate)
        except Exception as exc:
            logger.debug("Market extra opportunity brief skipped: %s", exc)
            continue

        entities = [str(item).strip() for item in list(getattr(candidate, "entities", ()) or ()) if str(item).strip()]
        score_tag = int(round(score))
        seed_keywords = _unique_nonempty([keyword, *entities[:4]])
        public_tags = _unique_nonempty(
            [
                keyword,
                *entities[:3],
                "오늘의증시",
                "시장체크",
                "경제공부",
            ]
        )[:8]
        selected.append(
            {
                "title": brief.title,
                "seed_keywords": seed_keywords,
                "tags": [
                    *public_tags,
                    "opportunity_status:selected",
                    f"opportunity_score:{score_tag}",
                    f"opportunity_keyword:{_tag_safe(keyword)}",
                    "opportunity_evidence:market",
                    "opportunity_extra:market",
                ],
                "category": "경제 브리핑",
                "opportunity_keyword": keyword,
                "opportunity_score": score_tag,
            }
        )
        seen.add(keyword_key)
        if len(selected) >= limit:
            break

    return selected


def _scheduled_at_for_extra_opportunity(
    *,
    local_date: date,
    timezone_name: str,
    extra_index: int,
) -> str:
    """추가 기회 슬롯의 예약 시각을 UTC ISO로 반환한다."""

    local_tz = _safe_zoneinfo(timezone_name)
    anchors = (
        time(hour=11, minute=40),
        time(hour=15, minute=10),
        time(hour=21, minute=40),
    )
    index = max(1, int(extra_index or 1))
    if index <= len(anchors):
        local_dt = datetime.combine(local_date, anchors[index - 1], tzinfo=local_tz)
    else:
        local_dt = datetime.combine(local_date, anchors[-1], tzinfo=local_tz) + timedelta(
            minutes=70 * (index - len(anchors))
        )
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _title_for_extra_opportunity(local_date: date, extra_index: int) -> str:
    """추가 기회 슬롯 fallback 제목을 만든다."""

    return f"{local_date.isoformat()} 시장 기회 점검 {extra_index} - 지금 다시 볼 변수"


def _build_market_open_state(service: "SchedulerService", local_date: date) -> MarketOpenState:
    """주말/수동 휴장 목록 기반 시장 개장 상태를 만든다."""

    is_weekend = local_date.weekday() >= 5
    krx_closed_dates = _date_set_setting(service, "scheduler_krx_closed_dates")
    us_closed_dates = _date_set_setting(service, "scheduler_us_closed_dates")
    today_key = local_date.isoformat()
    return MarketOpenState(
        krx_open=(not is_weekend) and today_key not in krx_closed_dates,
        us_open=(not is_weekend) and today_key not in us_closed_dates,
        is_weekend=is_weekend,
    )


def _scheduled_at_for_slot(
    *,
    local_date: date,
    original_slot: BlogSlot,
    timezone_name: str,
) -> str:
    """원래 슬롯 기준 예약 시각을 UTC ISO로 반환한다."""

    local_tz = _safe_zoneinfo(timezone_name)
    if original_slot == BlogSlot.KR_PREOPEN:
        local_dt = datetime.combine(local_date, time(hour=8, minute=10), tzinfo=local_tz)
    elif original_slot == BlogSlot.US_PREOPEN:
        local_dt = get_us_preopen_kst(local_date)
    else:
        local_dt = datetime.combine(local_date, time(hour=18, minute=30), tzinfo=local_tz)
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scope_for_slot(slot: BlogSlot) -> MarketScope:
    """슬롯별 시장 범위를 반환한다."""

    if slot == BlogSlot.KR_PREOPEN:
        return MarketScope.KR
    if slot == BlogSlot.US_PREOPEN:
        return MarketScope.US
    return MarketScope.EVERGREEN


def _title_for_market_slot(
    *,
    local_date: date,
    original_slot: BlogSlot,
    resolved_slot: BlogSlot,
    sequence: int,
) -> str:
    """시장 브리핑용 제목을 만든다."""

    day = local_date.isoformat()
    if resolved_slot == BlogSlot.KR_PREOPEN:
        return f"{day} 국장 개장 전 브리핑 - 밤사이 데이터가 남긴 기준"
    if resolved_slot == BlogSlot.US_PREOPEN:
        return f"{day} 미장 개장 전 브리핑 - 아시아와 선물이 말해주는 기준"
    if resolved_slot == BlogSlot.WEEKLY_REFLECTION:
        return f"{day} 주간 투자 복기 - 숫자보다 먼저 볼 습관"
    if original_slot == BlogSlot.KR_PREOPEN:
        return f"{day} 아침 투자 공부 노트 - 시장이 쉬어도 남는 기준"
    if original_slot == BlogSlot.US_PREOPEN:
        return f"{day} 저녁 투자 공부 노트 - 예측보다 먼저 세울 기준"
    return f"{day} 투자 통찰 노트 {sequence} - 오늘 잘라낼 생각과 남길 기준"


def _keywords_for_market_slot(
    resolved_slot: BlogSlot,
    original_slot: BlogSlot,
) -> List[str]:
    """시장 브리핑용 시드 키워드를 반환한다."""

    if resolved_slot == BlogSlot.KR_PREOPEN:
        return ["국장", "미국 증시", "환율", "반도체", "외국인 수급"]
    if resolved_slot == BlogSlot.US_PREOPEN:
        return ["미장", "나스닥 선물", "아시아 증시", "비트코인", "미국 금리"]
    if resolved_slot == BlogSlot.WEEKLY_REFLECTION:
        return ["투자 복기", "리스크 관리", "기록 습관", "초심자", "선택과 집중"]
    if original_slot == BlogSlot.KR_PREOPEN:
        return ["투자 공부", "국장 휴장", "시장 심리", "리스크 관리", "초심자"]
    if original_slot == BlogSlot.US_PREOPEN:
        return ["투자 공부", "미장 휴장", "예측 과잉", "기록 습관", "초심자"]
    return ["투자 공부", "자기 개발", "리스크 관리", "기록", "통찰"]


def _tags_for_market_slot(
    *,
    original_slot: BlogSlot,
    resolved_slot: BlogSlot,
    scope: MarketScope,
    sequence: int,
    local_date: date,
) -> List[str]:
    """시장 브리핑 작업 태그를 만든다."""

    return [
        "market_daily",
        f"daily_slot:{sequence}",
        f"market_slot:{_slot_tag(resolved_slot)}",
        f"market_origin_slot:{_slot_tag(original_slot)}",
        f"market_scope:{scope.value}",
        f"local_date:{local_date.isoformat()}",
    ]


def _writing_strategy_tags_for_market(
    *,
    title: str,
    tags: List[str],
    seed_keywords: List[str],
) -> List[str]:
    """경제 글쓰기 전략 태그를 반환한다."""

    try:
        from ..content_sources import select_market_writing_strategy, writing_strategy_tags

        plan = select_market_writing_strategy(
            title=title,
            tags=tags,
            seed_keywords=seed_keywords,
        )
        return writing_strategy_tags(plan)
    except Exception:
        logger.debug("market writing strategy tag selection skipped", exc_info=True)
        return []


def _writing_strategy_tags_for_category(*, brief: Any, tags: List[str]) -> List[str]:
    """확장 카테고리 글쓰기 전략 태그를 반환한다."""

    try:
        from ..content_sources import select_category_writing_strategy, writing_strategy_tags

        plan = select_category_writing_strategy(
            topic_mode=str(getattr(brief, "topic_mode", "") or ""),
            template_id=str(getattr(brief, "template_id", "") or ""),
            title=str(getattr(brief, "title", "") or ""),
            tags=tags,
        )
        return writing_strategy_tags(plan) if plan is not None else []
    except Exception:
        logger.debug("category writing strategy tag selection skipped", exc_info=True)
        return []


def _slot_tag(slot: BlogSlot) -> str:
    return slot.value.lower()


def _tag_safe(value: str) -> str:
    return str(value or "").strip().replace(" ", "_").replace(":", "_")


def _unique_nonempty(values: List[str]) -> List[str]:
    """공백과 중복을 제거한 순서 보존 리스트를 반환한다."""

    result: List[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        key = item.lower()
        if not item or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _service_setting(service: "SchedulerService", key: str, default: str = "") -> str:
    """시스템 설정을 우선 읽고 없으면 기본값을 반환한다."""

    if service.job_store:
        return service.job_store.get_system_setting(key, default)
    return default


def _date_set_setting(service: "SchedulerService", key: str) -> set[str]:
    raw = ""
    if service.job_store:
        raw = service.job_store.get_system_setting(key, os.getenv(key.upper(), ""))
    else:
        raw = os.getenv(key.upper(), "")
    return {part.strip() for part in str(raw or "").split(",") if part.strip()}


def _ensure_local_datetime(value: datetime, timezone_name: str) -> datetime:
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=_safe_zoneinfo(timezone_name))


def _safe_zoneinfo(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(timezone_name or "Asia/Seoul"))
    except Exception:
        return ZoneInfo("Asia/Seoul")


def _is_truthy(raw: Any) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y", "on"}
