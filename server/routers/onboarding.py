"""온보딩 마법사 API."""

from __future__ import annotations

import json
from datetime import datetime
from typing import List

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from modules.automation.job_store import JobStore
from modules.automation.notifier import TelegramNotifier
from modules.constants import DEFAULT_FALLBACK_CATEGORY as _DEFAULT_FALLBACK_CATEGORY
from modules.llm.api_health import _check_single
from modules.llm.provider_factory import create_client
from modules.persona.questionnaire import get_question_bank_payload
from server.dependencies import get_job_store

from server.schemas.onboarding import (
    ApiVerifyRequest,
    ApiVerifyResponse,
    CategorySetupRequest,
    CategorySetupResponse,
    CompleteOnboardingResponse,
    OnboardingStatusResponse,
    PersonaLabRequest,
    PersonaLabResponse,
    PersonaQuestionBankResponse,
    ScheduleAllocationItem,
    ScheduleSetupRequest,
    ScheduleSetupResponse,
    TelegramTestRequest,
    TelegramTestResponse,
)
from modules.utils.onboarding_helper import (
    mask_secret,
    parse_json_list,
    recommend_categories,
    normalize_topic_mode,
    build_default_allocations,
    normalize_allocations,
    compile_voice_profile,
    to_json_string,
)

router = APIRouter()
_DEFAULT_IDEA_VAULT_DAILY_QUOTA = 2
_QUESTIONNAIRE_REQUIRED_COUNT = 5


@router.get(
    "/onboarding/persona/questions",
    response_model=PersonaQuestionBankResponse,
    summary="온보딩 페르소나 질문지 조회",
)
def get_persona_questions() -> PersonaQuestionBankResponse:
    """상황형 페르소나 질문지 뱅크를 반환한다."""
    payload = get_question_bank_payload(required_count=_QUESTIONNAIRE_REQUIRED_COUNT)
    return PersonaQuestionBankResponse(**payload)


@router.get("/onboarding", response_model=OnboardingStatusResponse, summary="온보딩 상태 조회")
@router.get("/wizard/status", response_model=OnboardingStatusResponse, summary="온보딩 상태 조회(alias)")
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
            "category_mapping",
            "telegram_bot_token",
            "telegram_chat_id",
            "telegram_webhook_secret",
        ]
    )
    persona_id = settings.get("active_persona_id", "P1")
    persona_row = job_store.get_persona_profile(persona_id)
    voice_profile = persona_row["voice_profile"] if persona_row else {}
    interests = parse_json_list(settings.get("persona_interests", "[]"))
    recommended = parse_json_list(settings.get("recommended_categories", "[]"))
    categories = parse_json_list(settings.get("custom_categories", "[]"))
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
                        topic_mode=normalize_topic_mode(str(item.get("topic_mode", "cafe")).strip()),
                        count=max(0, int(item.get("count", 0))),
                        percentage=float(item.get("percentage", 0.0)) if item.get("percentage") is not None else None,
                    )
                    for item in decoded
                    if isinstance(item, dict) and str(item.get("category", "")).strip()
                ]
        except Exception:
            allocations = []
    completed = settings.get("onboarding_completed", "false").lower() == "true"
    telegram_bot_token = settings.get("telegram_bot_token", "").strip()
    telegram_chat_id = settings.get("telegram_chat_id", "").strip()
    telegram_webhook_secret = settings.get("telegram_webhook_secret", "").strip()
    telegram_configured = bool(telegram_bot_token and telegram_chat_id)

    raw_mapping = settings.get("category_mapping", "{}").strip()
    try:
        category_mapping = json.loads(raw_mapping)
        if not isinstance(category_mapping, dict):
            category_mapping = {}
    except Exception:
        category_mapping = {}

    if not recommended:
        recommended = recommend_categories(interests)
    if not categories:
        categories = recommended
    if not allocations:
        allocations = build_default_allocations(categories, non_vault_target)
    else:
        allocations = normalize_allocations(
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
        category_mapping=category_mapping,
        telegram_configured=telegram_configured,
        telegram_bot_token=mask_secret(telegram_bot_token) if telegram_bot_token else "",
        telegram_chat_id=telegram_chat_id,
        telegram_webhook_secret=mask_secret(telegram_webhook_secret) if telegram_webhook_secret else "",
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

    voice_profile = compile_voice_profile(request)
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
    job_store.set_system_setting("persona_interests", to_json_string(interests))

    recommended_categories = recommend_categories(
        interests,
        mbti=request.mbti if bool(voice_profile.get("mbti_enabled")) else "",
        age_group=request.age_group,
        gender=request.gender
    )
    job_store.set_system_setting("recommended_categories", to_json_string(recommended_categories))

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

    job_store.set_system_setting("custom_categories", to_json_string(categories))
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

    # percentage 필드가 지정된 경우 count로 자동 변환 (percentage는 반드시 보존)
    resolved_allocations: List[ScheduleAllocationItem] = []
    has_percentage = any(
        item.percentage is not None for item in request.allocations
    )
    if has_percentage and non_vault_target > 0:
        total_pct = sum(
            float(item.percentage or 0.0) for item in request.allocations
        )
        effective_pct = max(total_pct, 0.01)  # 0 분모 방지
        # 소수 누적 분배(largest-remainder)로 정확히 non_vault_target에 맞춤
        raw_counts = []
        for item in request.allocations:
            raw_counts.append((float(item.percentage or 0.0) / effective_pct) * non_vault_target)
        floored = [int(r) for r in raw_counts]  # 내림
        remainders = [(raw_counts[i] - floored[i], i) for i in range(len(raw_counts))]
        remainders.sort(key=lambda x: -x[0])  # 나머지 큰 순
        shortfall = non_vault_target - sum(floored)
        for j in range(min(shortfall, len(remainders))):
            floored[remainders[j][1]] += 1
        for idx, item in enumerate(request.allocations):
            resolved_allocations.append(
                ScheduleAllocationItem(
                    category=item.category,
                    topic_mode=item.topic_mode,
                    count=max(0, floored[idx]),
                    percentage=item.percentage,  # 원본 퍼센트 보존
                )
            )
    else:
        resolved_allocations = list(request.allocations)

    categories = parse_json_list(job_store.get_system_setting("custom_categories", "[]"))
    allocations = normalize_allocations(
        requested=resolved_allocations,
        daily_posts_target=non_vault_target,
        fallback_categories=categories,
    )
    # 정규화 결과 총합이 다르면 마지막으로 안전 보정한다.
    current_total = sum(item.count for item in allocations)
    if current_total != non_vault_target:
        if not allocations:
            allocations = build_default_allocations(categories, non_vault_target)
        else:
            delta = non_vault_target - current_total
            allocations[0].count += delta

    # 모든 카테고리를 유지한다 (count=0이어도 percentage가 있으면 살림)
    allocation_payload = [
        {
            "category": item.category,
            "topic_mode": item.topic_mode,
            "count": item.count,
            "percentage": item.percentage,
        }
        for item in allocations
    ]
    job_store.set_system_setting("scheduler_daily_posts_target", str(normalized_target))
    job_store.set_system_setting(
        "scheduler_idea_vault_daily_quota",
        str(normalized_idea_vault_quota),
    )
    job_store.set_system_setting("scheduler_category_allocations", to_json_string(allocation_payload))
    job_store.set_system_setting("category_mapping", to_json_string(request.category_mapping))

    return ScheduleSetupResponse(
        daily_posts_target=normalized_target,
        idea_vault_daily_quota=normalized_idea_vault_quota,
        allocations=[
            ScheduleAllocationItem(
                category=str(item["category"]),
                topic_mode=str(item["topic_mode"]),
                count=int(item["count"]),
                percentage=float(item["percentage"]) if item.get("percentage") is not None else None,
            )
            for item in allocation_payload
        ],
        category_mapping=request.category_mapping,
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
        job_store.set_system_setting("telegram_webhook_secret", request.webhook_secret.strip())

    return TelegramTestResponse(
        success=True,
        message="테스트 발송 성공",
    )


@router.post(
    "/onboarding/api-verify",
    response_model=ApiVerifyResponse,
    summary="온보딩 Step1 API 키 실시간 검증",
)
async def verify_api_key(request: ApiVerifyRequest) -> ApiVerifyResponse:
    """텍스트/이미지 API 키의 유효성을 즉시 검증한다."""
    provider = request.provider.strip().lower()
    api_key = request.api_key.strip()

    if not api_key:
        return ApiVerifyResponse(valid=False, message="키가 입력되지 않았습니다.")

    # 텍스트 모델인 경우 Ping 테스트
    try:
        client = create_client(
            provider=provider,
            api_key=api_key,
            timeout_sec=3.0,
            max_tokens=1,
        )
        result = await _check_single(client, timeout_sec=3.0, close_client=True)
        is_ok = bool(result.get("ok", False))
        return ApiVerifyResponse(
            valid=is_ok,
            message=str(result.get("message", "API 검증 성공" if is_ok else "API 호출 실패")),
        )
    except ValueError:
        pass  # 텍스트 모델이 아니면 이미지 모델/예외 처리로 넘어감
    except Exception as exc:
        return ApiVerifyResponse(valid=False, message=f"인증 실패: {exc}")

    # 이미지 API 프로바이더별 실제 HTTP 핑 검증
    if provider == "pexels":
        return await _verify_pexels_key(api_key)

    if provider in {"fal", "together", "openai_image"}:
        return await _verify_image_key_generic(provider, api_key)

    # 기타 알 수 없는 프로바이더: 길이 체크만
    if len(api_key) > 5:
        return ApiVerifyResponse(valid=True, message="키 입력 확인됨")
    return ApiVerifyResponse(valid=False, message="키 길이가 너무 짧습니다.")


async def _verify_pexels_key(api_key: str) -> ApiVerifyResponse:
    """Pexels API 키를 실제 HTTP 핑으로 검증한다."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.pexels.com/v1/search",
                params={"query": "test", "per_page": 1},
                headers={"Authorization": api_key},
            )
        if resp.status_code == 200:
            return ApiVerifyResponse(valid=True, message="Pexels 키 검증 성공")
        if resp.status_code == 401:
            return ApiVerifyResponse(valid=False, message="Pexels 인증 실패 (키를 확인해주세요)")
        return ApiVerifyResponse(valid=False, message=f"Pexels 응답 오류 (HTTP {resp.status_code})")
    except httpx.TimeoutException:
        return ApiVerifyResponse(valid=False, message="Pexels 응답 시간 초과")
    except Exception as exc:
        return ApiVerifyResponse(valid=False, message=f"Pexels 연결 실패: {exc}")


async def _verify_image_key_generic(provider: str, api_key: str) -> ApiVerifyResponse:
    """Fal/Together 등 이미지 AI 프로바이더 키를 형식 검증 후 확인한다."""
    # 각 프로바이더 키 최소 길이 기준 (형식 체크)
    min_lengths = {"fal": 30, "together": 40, "openai_image": 40}
    min_len = min_lengths.get(provider, 10)
    if len(api_key) < min_len:
        return ApiVerifyResponse(valid=False, message=f"키 형식이 올바르지 않습니다 (최소 {min_len}자 필요)")
    provider_labels = {"fal": "Fal.ai", "together": "Together.ai", "openai_image": "OpenAI (이미지)"}
    label = provider_labels.get(provider, provider)
    return ApiVerifyResponse(valid=True, message=f"{label} 키 형식 확인됨")


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
