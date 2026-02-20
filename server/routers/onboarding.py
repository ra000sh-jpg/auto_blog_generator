"""온보딩 마법사 API."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from modules.automation.job_store import JobStore
from modules.automation.notifier import TelegramNotifier
from server.dependencies import get_job_store

router = APIRouter()

_DEFAULT_FALLBACK_CATEGORY = "다양한 생각"
_DEFAULT_IDEA_VAULT_DAILY_QUOTA = 2
_INTEREST_CATEGORY_MAP: Dict[str, str] = {
    "카페": "카페 운영 노하우",
    "맛집": "맛집 탐방",
    "커피": "커피 기록",
    "개발": "IT 자동화",
    "코딩": "개발 메모",
    "ai": "AI 활용",
    "육아": "육아 일기",
    "아이": "아이 성장 기록",
    "경제": "경제 브리핑",
    "주식": "투자 메모",
    "재테크": "재테크 노트",
}


class PersonaLabRequest(BaseModel):
    """Step1 페르소나 랩 저장 요청."""

    persona_id: str = "P1"
    identity: str = ""
    target_audience: str = ""
    tone_hint: str = ""
    interests: List[str] = Field(default_factory=list)
    structure_score: int = Field(ge=0, le=100)
    evidence_score: int = Field(ge=0, le=100)
    distance_score: int = Field(ge=0, le=100)
    criticism_score: int = Field(ge=0, le=100)
    density_score: int = Field(ge=0, le=100)
    style_strength: int = Field(default=40, ge=0, le=100)


class PersonaLabResponse(BaseModel):
    """Step1 저장 응답."""

    persona_id: str
    voice_profile: Dict[str, object]
    recommended_categories: List[str]


class CategorySetupRequest(BaseModel):
    """Step2 카테고리 저장 요청."""

    categories: List[str] = Field(default_factory=list)
    fallback_category: str = _DEFAULT_FALLBACK_CATEGORY


class CategorySetupResponse(BaseModel):
    """Step2 저장 응답."""

    categories: List[str]
    fallback_category: str


class ScheduleAllocationItem(BaseModel):
    """카테고리별 일간 할당량."""

    category: str
    topic_mode: str = "cafe"
    count: int = Field(default=0, ge=0, le=20)


class ScheduleSetupRequest(BaseModel):
    """Step3 스케줄/비율 저장 요청."""

    daily_posts_target: int = Field(default=3, ge=1, le=20)
    idea_vault_daily_quota: int = Field(default=_DEFAULT_IDEA_VAULT_DAILY_QUOTA, ge=0, le=20)
    allocations: List[ScheduleAllocationItem] = Field(default_factory=list)


class ScheduleSetupResponse(BaseModel):
    """Step3 스케줄/비율 저장 응답."""

    daily_posts_target: int
    idea_vault_daily_quota: int
    allocations: List[ScheduleAllocationItem]


class TelegramTestRequest(BaseModel):
    """Step4 텔레그램 테스트 요청."""

    bot_token: str
    chat_id: str
    save: bool = True


class TelegramTestResponse(BaseModel):
    """Step4 텔레그램 테스트 응답."""

    success: bool
    message: str


class OnboardingStatusResponse(BaseModel):
    """온보딩 상태 조회 응답."""

    completed: bool
    persona_id: str
    interests: List[str]
    voice_profile: Dict[str, object]
    recommended_categories: List[str]
    categories: List[str]
    fallback_category: str
    daily_posts_target: int
    idea_vault_daily_quota: int
    category_allocations: List[ScheduleAllocationItem]
    telegram_configured: bool


class CompleteOnboardingResponse(BaseModel):
    """온보딩 완료 응답."""

    completed: bool
    completed_at: str


def _to_json_string(value: object) -> str:
    """값을 JSON 문자열로 직렬화한다."""
    return json.dumps(value, ensure_ascii=False)


def _parse_json_list(raw_value: str) -> List[str]:
    """JSON 문자열 리스트를 안전 파싱한다."""
    try:
        decoded = json.loads(raw_value)
        if isinstance(decoded, list):
            normalized = []
            for item in decoded:
                text = str(item).strip()
                if text and text not in normalized:
                    normalized.append(text)
            return normalized
    except Exception:
        pass
    return []


def _recommend_categories(interests: List[str]) -> List[str]:
    """관심사 기반 카테고리 추천을 생성한다."""
    categories: List[str] = []
    for interest in interests:
        cleaned = str(interest).strip()
        if not cleaned:
            continue
        matched = None
        lowered = cleaned.lower()
        for keyword, category_name in _INTEREST_CATEGORY_MAP.items():
            if keyword.lower() in lowered:
                matched = category_name
                break
        if matched is None:
            matched = f"{cleaned} 이야기"
        if matched not in categories:
            categories.append(matched)
    if _DEFAULT_FALLBACK_CATEGORY not in categories:
        categories.append(_DEFAULT_FALLBACK_CATEGORY)
    return categories


def _infer_topic_mode(category_name: str) -> str:
    """카테고리 이름에서 토픽 모드를 추정한다."""
    lowered = str(category_name).strip().lower()
    if any(token in lowered for token in ("경제", "finance", "economy", "투자", "주식", "재테크")):
        return "finance"
    if any(token in lowered for token in ("it", "개발", "코드", "자동화", "ai", "테크")):
        return "it"
    if any(token in lowered for token in ("육아", "아이", "부모", "가정", "parenting", "family")):
        return "parenting"
    return "cafe"


def _normalize_topic_mode(raw_mode: str, fallback_category: str = "") -> str:
    """토픽 모드를 허용 범위(cafe/it/parenting/finance)로 정규화한다."""
    lowered = str(raw_mode).strip().lower()
    if lowered == "economy":
        return "finance"
    if lowered in {"cafe", "it", "parenting", "finance"}:
        return lowered
    return _infer_topic_mode(fallback_category or raw_mode)


def _build_default_allocations(categories: List[str], daily_posts_target: int) -> List[ScheduleAllocationItem]:
    """카테고리 목록 기반 기본 할당량을 생성한다."""
    normalized_categories = [value for value in categories if str(value).strip()]
    if not normalized_categories:
        normalized_categories = [_DEFAULT_FALLBACK_CATEGORY]

    buckets = [
        ScheduleAllocationItem(
            category=category_name,
            topic_mode=_infer_topic_mode(category_name),
            count=0,
        )
        for category_name in normalized_categories
    ]
    for index in range(max(0, daily_posts_target)):
        target_index = index % len(buckets)
        buckets[target_index].count += 1
    return buckets


def _normalize_allocations(
    requested: List[ScheduleAllocationItem],
    daily_posts_target: int,
    fallback_categories: List[str],
) -> List[ScheduleAllocationItem]:
    """요청된 할당량을 정규화해 정확히 daily_posts_target에 맞춘다."""
    items: List[ScheduleAllocationItem] = []
    for item in requested:
        category_name = str(item.category).strip()
        if not category_name:
            continue
        topic_mode = _normalize_topic_mode(item.topic_mode, category_name)
        items.append(
            ScheduleAllocationItem(
                category=category_name,
                topic_mode=topic_mode,
                count=max(0, int(item.count)),
            )
        )

    if not items:
        return _build_default_allocations(fallback_categories, daily_posts_target)

    total = sum(item.count for item in items)
    if total <= 0:
        return _build_default_allocations([item.category for item in items], daily_posts_target)

    if total < daily_posts_target:
        short = daily_posts_target - total
        items[0].count += short
        return items

    if total > daily_posts_target:
        overflow = total - daily_posts_target
        # 뒤에서부터 차감해 앞쪽 우선순위를 최대한 유지한다.
        for item in reversed(items):
            if overflow <= 0:
                break
            deductible = min(item.count, overflow)
            item.count -= deductible
            overflow -= deductible
        return [item for item in items if item.count > 0]

    return items


def _bucket_score(score: int, labels: List[str]) -> str:
    """0~100 점수를 3단계 버킷 라벨로 변환한다."""
    if score <= 33:
        return labels[0]
    if score <= 66:
        return labels[1]
    return labels[2]


def _compile_voice_profile(request: PersonaLabRequest) -> Dict[str, object]:
    """슬라이더 점수를 Voice_Profile로 변환한다."""
    structure_mode = "top_down" if request.structure_score >= 50 else "bottom_up"
    evidence_mode = "objective" if request.evidence_score >= 50 else "subjective"

    return {
        "version": "v1",
        "structure": structure_mode,
        "evidence": evidence_mode,
        "distance": _bucket_score(
            request.distance_score,
            ["authoritative", "peer", "inspiring"],
        ),
        "criticism": _bucket_score(
            request.criticism_score,
            ["avoidant", "mitigated", "direct"],
        ),
        "density": _bucket_score(
            request.density_score,
            ["light", "balanced", "dense"],
        ),
        "style_strength": request.style_strength,
        "scores": {
            "structure": request.structure_score,
            "evidence": request.evidence_score,
            "distance": request.distance_score,
            "criticism": request.criticism_score,
            "density": request.density_score,
        },
    }


@router.get("/onboarding", response_model=OnboardingStatusResponse, summary="온보딩 상태 조회")
def get_onboarding_status(
    job_store: JobStore = Depends(get_job_store),
) -> OnboardingStatusResponse:
    """온보딩 진행 상태와 저장된 값을 반환한다."""
    settings = job_store.get_system_settings(
        [
            "onboarding_completed",
            "onboarding_completed_at",
            "active_persona_id",
            "persona_interests",
            "recommended_categories",
            "custom_categories",
            "fallback_category",
            "scheduler_daily_posts_target",
            "scheduler_idea_vault_daily_quota",
            "scheduler_category_allocations",
            "telegram_bot_token",
            "telegram_chat_id",
        ]
    )
    persona_id = settings.get("active_persona_id", "P1")
    persona_row = job_store.get_persona_profile(persona_id)
    voice_profile = persona_row["voice_profile"] if persona_row else {}
    interests = _parse_json_list(settings.get("persona_interests", "[]"))
    recommended = _parse_json_list(settings.get("recommended_categories", "[]"))
    categories = _parse_json_list(settings.get("custom_categories", "[]"))
    fallback_category = settings.get("fallback_category", _DEFAULT_FALLBACK_CATEGORY) or _DEFAULT_FALLBACK_CATEGORY
    raw_daily_target = settings.get("scheduler_daily_posts_target", "").strip()
    try:
        daily_posts_target = max(1, min(20, int(raw_daily_target))) if raw_daily_target else 3
    except ValueError:
        daily_posts_target = 3
    raw_idea_vault_quota = settings.get("scheduler_idea_vault_daily_quota", "").strip()
    try:
        idea_vault_daily_quota = (
            max(0, min(daily_posts_target, int(raw_idea_vault_quota)))
            if raw_idea_vault_quota
            else min(daily_posts_target, _DEFAULT_IDEA_VAULT_DAILY_QUOTA)
        )
    except ValueError:
        idea_vault_daily_quota = min(daily_posts_target, _DEFAULT_IDEA_VAULT_DAILY_QUOTA)
    non_vault_target = max(0, daily_posts_target - idea_vault_daily_quota)

    raw_allocations = settings.get("scheduler_category_allocations", "").strip()
    allocations: List[ScheduleAllocationItem] = []
    if raw_allocations:
        try:
            decoded = json.loads(raw_allocations)
            if isinstance(decoded, list):
                allocations = [
                    ScheduleAllocationItem(
                        category=str(item.get("category", "")).strip(),
                        topic_mode=_normalize_topic_mode(str(item.get("topic_mode", "cafe")).strip()),
                        count=max(0, int(item.get("count", 0))),
                    )
                    for item in decoded
                    if isinstance(item, dict) and str(item.get("category", "")).strip()
                ]
        except Exception:
            allocations = []
    completed = settings.get("onboarding_completed", "false").lower() == "true"
    telegram_configured = bool(
        settings.get("telegram_bot_token", "").strip()
        and settings.get("telegram_chat_id", "").strip()
    )

    if not recommended:
        recommended = _recommend_categories(interests)
    if not categories:
        categories = recommended
    if not allocations:
        allocations = _build_default_allocations(categories, non_vault_target)
    else:
        allocations = _normalize_allocations(
            requested=allocations,
            daily_posts_target=non_vault_target,
            fallback_categories=categories,
        )

    return OnboardingStatusResponse(
        completed=completed,
        persona_id=persona_id,
        interests=interests,
        voice_profile=voice_profile,
        recommended_categories=recommended,
        categories=categories,
        fallback_category=fallback_category,
        daily_posts_target=daily_posts_target,
        idea_vault_daily_quota=idea_vault_daily_quota,
        category_allocations=allocations,
        telegram_configured=telegram_configured,
    )


@router.post("/onboarding/persona", response_model=PersonaLabResponse, summary="온보딩 Step1 저장")
def save_persona_lab(
    request: PersonaLabRequest,
    job_store: JobStore = Depends(get_job_store),
) -> PersonaLabResponse:
    """페르소나 랩 결과를 저장한다."""
    persona_id = request.persona_id.strip().upper() or "P1"
    if persona_id not in {"P1", "P2", "P3", "P4"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="persona_id는 P1~P4만 지원합니다.",
        )

    interests = []
    for interest in request.interests:
        value = str(interest).strip()
        if value and value not in interests:
            interests.append(value)

    voice_profile = _compile_voice_profile(request)
    persona_payload = {
        "identity": request.identity.strip(),
        "target_audience": request.target_audience.strip(),
        "tone_hint": request.tone_hint.strip(),
        "interests": interests,
    }
    job_store.upsert_persona_profile(
        persona_id=persona_id,
        persona_payload=persona_payload,
        profile_payload=voice_profile,
    )
    job_store.set_system_setting("active_persona_id", persona_id)
    job_store.set_system_setting("persona_interests", _to_json_string(interests))

    recommended_categories = _recommend_categories(interests)
    job_store.set_system_setting("recommended_categories", _to_json_string(recommended_categories))

    return PersonaLabResponse(
        persona_id=persona_id,
        voice_profile=voice_profile,
        recommended_categories=recommended_categories,
    )


@router.post("/onboarding/categories", response_model=CategorySetupResponse, summary="온보딩 Step2 저장")
def save_categories(
    request: CategorySetupRequest,
    job_store: JobStore = Depends(get_job_store),
) -> CategorySetupResponse:
    """카테고리 설정을 저장한다."""
    categories = []
    for category in request.categories:
        value = str(category).strip()
        if value and value not in categories:
            categories.append(value)

    fallback_category = request.fallback_category.strip() or _DEFAULT_FALLBACK_CATEGORY
    if fallback_category not in categories:
        categories.append(fallback_category)
    if _DEFAULT_FALLBACK_CATEGORY not in categories:
        categories.append(_DEFAULT_FALLBACK_CATEGORY)

    job_store.set_system_setting("custom_categories", _to_json_string(categories))
    job_store.set_system_setting("fallback_category", fallback_category)

    return CategorySetupResponse(
        categories=categories,
        fallback_category=fallback_category,
    )


@router.post("/onboarding/schedule", response_model=ScheduleSetupResponse, summary="온보딩 Step3 저장")
def save_schedule(
    request: ScheduleSetupRequest,
    job_store: JobStore = Depends(get_job_store),
) -> ScheduleSetupResponse:
    """일간 발행량/카테고리 비율 설정을 저장한다."""
    normalized_target = max(1, min(20, int(request.daily_posts_target)))
    normalized_idea_vault_quota = max(
        0,
        min(normalized_target, int(request.idea_vault_daily_quota)),
    )
    non_vault_target = max(0, normalized_target - normalized_idea_vault_quota)
    categories = _parse_json_list(job_store.get_system_setting("custom_categories", "[]"))
    allocations = _normalize_allocations(
        requested=request.allocations,
        daily_posts_target=non_vault_target,
        fallback_categories=categories,
    )
    # 정규화 결과 총합이 다르면 마지막으로 안전 보정한다.
    current_total = sum(item.count for item in allocations)
    if current_total != non_vault_target:
        if not allocations:
            allocations = _build_default_allocations(categories, non_vault_target)
        else:
            delta = non_vault_target - current_total
            allocations[0].count += delta

    allocation_payload = [
        {
            "category": item.category,
            "topic_mode": item.topic_mode,
            "count": item.count,
        }
        for item in allocations
        if item.count > 0
    ]
    job_store.set_system_setting("scheduler_daily_posts_target", str(normalized_target))
    job_store.set_system_setting(
        "scheduler_idea_vault_daily_quota",
        str(normalized_idea_vault_quota),
    )
    job_store.set_system_setting("scheduler_category_allocations", _to_json_string(allocation_payload))

    return ScheduleSetupResponse(
        daily_posts_target=normalized_target,
        idea_vault_daily_quota=normalized_idea_vault_quota,
        allocations=[
            ScheduleAllocationItem(
                category=str(item["category"]),
                topic_mode=str(item["topic_mode"]),
                count=int(item["count"]),
            )
            for item in allocation_payload
        ],
    )


@router.post(
    "/onboarding/telegram/test",
    response_model=TelegramTestResponse,
    summary="온보딩 Step4 텔레그램 테스트",
)
async def test_telegram(
    request: TelegramTestRequest,
    job_store: JobStore = Depends(get_job_store),
) -> TelegramTestResponse:
    """텔레그램 테스트 메시지를 전송한다."""
    notifier = TelegramNotifier(
        bot_token=request.bot_token.strip(),
        chat_id=request.chat_id.strip(),
    )
    if not notifier.enabled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Bot Token과 Chat ID를 모두 입력해 주세요.",
        )

    text = "Auto Blog Onboarding 테스트 메시지입니다. 알림 연동이 정상입니다."
    success = await notifier.send_message(text, disable_notification=False)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="텔레그램 테스트 발송에 실패했습니다.",
        )

    if request.save:
        job_store.set_system_setting("telegram_bot_token", request.bot_token.strip())
        job_store.set_system_setting("telegram_chat_id", request.chat_id.strip())

    return TelegramTestResponse(
        success=True,
        message="테스트 발송 성공",
    )


@router.post(
    "/onboarding/complete",
    response_model=CompleteOnboardingResponse,
    summary="온보딩 완료 처리",
)
def complete_onboarding(
    job_store: JobStore = Depends(get_job_store),
) -> CompleteOnboardingResponse:
    """온보딩 완료 플래그를 저장한다."""
    completed_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    job_store.set_system_setting("onboarding_completed", "true")
    job_store.set_system_setting("onboarding_completed_at", completed_at)

    return CompleteOnboardingResponse(
        completed=True,
        completed_at=completed_at,
    )
