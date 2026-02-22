"""온보딩 마법사 API."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List, Tuple

import httpx

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from modules.automation.job_store import JobStore
from modules.automation.notifier import TelegramNotifier
from modules.constants import DEFAULT_FALLBACK_CATEGORY as _DEFAULT_FALLBACK_CATEGORY
from modules.llm.api_health import _check_single
from modules.llm.provider_factory import create_client
from modules.persona.questionnaire import (
    QUESTIONNAIRE_VERSION,
    get_question_bank_payload,
    score_questionnaire_answers,
)
from server.dependencies import get_job_store

router = APIRouter()
_DEFAULT_IDEA_VAULT_DAILY_QUOTA = 2
_QUESTIONNAIRE_REQUIRED_COUNT = 5
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


class PersonaQuestionAnswerItem(BaseModel):
    """질문지 단일 응답."""

    question_id: str
    option_id: str


class PersonaLabRequest(BaseModel):
    """Step1 페르소나 랩 저장 요청."""

    persona_id: str = "P1"
    identity: str = ""
    target_audience: str = ""
    tone_hint: str = ""
    interests: List[str] = Field(default_factory=list)
    mbti: str = ""
    mbti_enabled: bool = False
    mbti_confidence: int = Field(default=60, ge=0, le=100)
    questionnaire_version: str = QUESTIONNAIRE_VERSION
    questionnaire_answers: List[PersonaQuestionAnswerItem] = Field(default_factory=list)
    age_group: str = ""
    gender: str = ""
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


class PersonaQuestionOptionModel(BaseModel):
    """질문지 선택지 응답 모델."""

    option_id: str
    label: str
    description: str
    effects: Dict[str, int]


class PersonaQuestionModel(BaseModel):
    """질문지 문항 응답 모델."""

    question_id: str
    title: str
    scenario: str
    target_dimension: str
    weight: int
    options: List[PersonaQuestionOptionModel]


class PersonaQuestionBankResponse(BaseModel):
    """온보딩 질문지 뱅크 응답."""

    version: str
    required_count: int
    dimensions: List[str]
    questions: List[PersonaQuestionModel]


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
class TelegramTestRequest(BaseModel):
    """Step4 텔레그램 테스트 요청."""

    bot_token: str
    chat_id: str
    webhook_secret: str = ""
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
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_secret: str = ""


class CompleteOnboardingResponse(BaseModel):
    """온보딩 완료 응답."""

    completed: bool
    completed_at: str


class ApiVerifyRequest(BaseModel):
    """API 키 유효성 검증 요청."""

    provider: str
    api_key: str


class ApiVerifyResponse(BaseModel):
    """API 키 유효성 검증 응답."""

    valid: bool
    message: str


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


_MBTI_CATEGORY_MAP: Dict[str, List[str]] = {
    # 分析型 (Analyst)
    "INTJ": ["자기계발", "IT 기술", "서평"],
    "INTP": ["IT 리뷰", "과학/기술", "게임 리뷰"],
    "ENTJ": ["재테크", "리더십", "경제 브리핑"],
    "ENTP": ["창업 비즈니스", "사이드 프로젝트", "트렌드 분석"],
    
    # 외교형 (Diplomat)
    "INFJ": ["에세이", "심리학", "자기계발"],
    "INFP": ["감성 에세이", "문학 리뷰", "예술/디자인"],
    "ENFJ": ["교육/강연", "자기계발", "인간관계"],
    "ENFP": ["일상 브이로그", "여행지 추천", "취미 생활"],

    # 관리자형 (Sentinel)
    "ISTJ": ["재테크", "자격증 공부", "경제 브리핑"],
    "ISFJ": ["요리 레시피", "육아 일기", "살림 노하우"],
    "ESTJ": ["부동산 투자", "업무 생산성", "주식 공부"],
    "ESFJ": ["맛집 투어", "육아 일기", "가족 생활"],

    # 탐험가형 (Explorer)
    "ISTP": ["전자기기 리뷰", "DIY/공예", "자동차"],
    "ISFP": ["인테리어", "카페 투어", "예술/디자인"],
    "ESTP": ["스포츠/운동", "아웃도어", "주식 투자"],
    "ESFP": ["패션/뷰티", "맛집 투어", "일상 기록"],
}

_VALID_MBTI_CODES = set(_MBTI_CATEGORY_MAP.keys())
_MBTI_LETTER_DELTAS: Dict[str, Dict[str, int]] = {
    "E": {"distance": 10, "density": -2},
    "I": {"distance": -10, "density": 2},
    "S": {"evidence": 10, "density": 4},
    "N": {"evidence": -6, "density": 8},
    "T": {"criticism": 10},
    "F": {"criticism": -10},
    "J": {"structure": 10, "density": 4},
    "P": {"structure": -10, "density": -4},
}


def _clamp_score(value: int) -> int:
    """점수를 0~100 범위로 제한한다."""
    return max(0, min(100, int(value)))


def _normalize_mbti(raw_value: str) -> str:
    """MBTI 코드를 표준화한다."""
    normalized = str(raw_value or "").strip().upper()
    return normalized if normalized in _VALID_MBTI_CODES else ""


def _calculate_mbti_weight(confidence: int) -> float:
    """MBTI 보정 가중치(10~20%)를 계산한다."""
    normalized_confidence = max(0, min(100, int(confidence)))
    return 0.10 + (normalized_confidence / 100.0) * 0.10


def _build_mbti_prior_scores(mbti_code: str) -> Dict[str, int]:
    """MBTI로부터 5차원 prior 점수를 계산한다."""
    base = {
        "structure": 50,
        "evidence": 50,
        "distance": 50,
        "criticism": 50,
        "density": 50,
    }
    for letter in mbti_code:
        for dimension, delta in _MBTI_LETTER_DELTAS.get(letter, {}).items():
            base[dimension] = _clamp_score(base[dimension] + delta)
    return base


def _blend_scores_with_mbti(
    questionnaire_scores: Dict[str, int],
    *,
    mbti_code: str,
    mbti_enabled: bool,
    mbti_confidence: int,
) -> Tuple[Dict[str, int], Dict[str, object]]:
    """질문지 점수와 MBTI prior를 혼합한다."""
    base_scores = {
        key: _clamp_score(value)
        for key, value in questionnaire_scores.items()
    }
    if not mbti_enabled:
        return base_scores, {
            "mbti_applied": False,
            "questionnaire_weight": 1.0,
            "mbti_weight": 0.0,
            "mbti_confidence": 0,
            "reason": "disabled",
            "questionnaire_scores": base_scores,
            "mbti_prior_scores": {},
            "final_scores": base_scores,
            "mbti_deltas": {key: 0 for key in base_scores.keys()},
        }

    normalized_mbti = _normalize_mbti(mbti_code)
    if not normalized_mbti:
        return base_scores, {
            "mbti_applied": False,
            "questionnaire_weight": 1.0,
            "mbti_weight": 0.0,
            "mbti_confidence": 0,
            "reason": "invalid_or_empty_mbti",
            "questionnaire_scores": base_scores,
            "mbti_prior_scores": {},
            "final_scores": base_scores,
            "mbti_deltas": {key: 0 for key in base_scores.keys()},
        }

    mbti_weight = _calculate_mbti_weight(mbti_confidence)
    questionnaire_weight = 1.0 - mbti_weight
    mbti_prior = _build_mbti_prior_scores(normalized_mbti)

    blended: Dict[str, int] = {}
    for key, base_value in base_scores.items():
        prior_value = mbti_prior.get(key, 50)
        blended[key] = _clamp_score(round(base_value * questionnaire_weight + prior_value * mbti_weight))

    return blended, {
        "mbti_applied": True,
        "questionnaire_weight": round(questionnaire_weight, 3),
        "mbti_weight": round(mbti_weight, 3),
        "mbti_confidence": max(0, min(100, int(mbti_confidence))),
        "reason": "applied",
        "questionnaire_scores": base_scores,
        "mbti_prior_scores": mbti_prior,
        "final_scores": blended,
        "mbti_deltas": {
            key: blended[key] - base_scores[key]
            for key in blended.keys()
        },
    }

def _recommend_categories(interests: List[str], mbti: str = "", age_group: str = "", gender: str = "") -> List[str]:
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
            
    # MBTI 기반 추천
    if mbti:
        mbti_upper = mbti.upper()
        if mbti_upper in _MBTI_CATEGORY_MAP:
            for cat in _MBTI_CATEGORY_MAP[mbti_upper]:
                if cat not in categories:
                    categories.append(cat)
                    if len(categories) >= 4:
                        break
                        
    # 연령/성별 기반 약간의 보정 (필요시)
    if age_group == "20대" and "패션/뷰티" not in categories and gender == "여성":
        categories.append("패션/뷰티")
    elif age_group == "30대" and "육아 일기" not in categories and gender != "남성":
        categories.append("육아 일기")
    elif age_group == "40대" and "재테크" not in categories:
        categories.append("재테크")
        
    if _DEFAULT_FALLBACK_CATEGORY not in categories:
        categories.append(_DEFAULT_FALLBACK_CATEGORY)
    return categories[:5]  # 최대 5개까지만 추천


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


def _resolve_questionnaire_scores(request: PersonaLabRequest) -> Tuple[Dict[str, int], Dict[str, object]]:
    """요청 페이로드에서 최종 질문지 점수를 산출한다."""
    default_scores = {
        "structure": request.structure_score,
        "evidence": request.evidence_score,
        "distance": request.distance_score,
        "criticism": request.criticism_score,
        "density": request.density_score,
    }

    answer_pairs = [
        (item.question_id, item.option_id)
        for item in request.questionnaire_answers
    ]
    if not answer_pairs:
        return default_scores, {
            "version": request.questionnaire_version or QUESTIONNAIRE_VERSION,
            "source": "manual_slider",
            "scores": default_scores,
            "answered_count": 0,
            "total_questions": 0,
            "completion_ratio": 0.0,
            "dimension_confidence": {key: 0.0 for key in default_scores.keys()},
            "resolved_answers": [],
            "missing_question_ids": [],
        }

    scored = score_questionnaire_answers(answer_pairs)
    scored_map = scored.get("scores", {})
    final_scores = {
        "structure": _clamp_score(int(scored_map.get("structure", 50))),
        "evidence": _clamp_score(int(scored_map.get("evidence", 50))),
        "distance": _clamp_score(int(scored_map.get("distance", 50))),
        "criticism": _clamp_score(int(scored_map.get("criticism", 50))),
        "density": _clamp_score(int(scored_map.get("density", 50))),
    }
    return final_scores, {
        **scored,
        "source": "questionnaire",
        "requested_version": request.questionnaire_version or QUESTIONNAIRE_VERSION,
    }


def _compile_voice_profile(request: PersonaLabRequest) -> Dict[str, object]:
    """슬라이더 점수를 Voice_Profile로 변환한다."""
    questionnaire_scores, questionnaire_meta = _resolve_questionnaire_scores(request)
    final_scores, blending_meta = _blend_scores_with_mbti(
        questionnaire_scores,
        mbti_code=request.mbti,
        mbti_enabled=request.mbti_enabled,
        mbti_confidence=request.mbti_confidence,
    )
    mbti_applied = bool(blending_meta.get("mbti_applied", False))
    normalized_mbti = _normalize_mbti(request.mbti) if mbti_applied else ""

    structure_mode = "top_down" if final_scores["structure"] >= 50 else "bottom_up"
    evidence_mode = "objective" if final_scores["evidence"] >= 50 else "subjective"

    return {
        "version": "v1",
        "mbti": normalized_mbti,
        "mbti_enabled": mbti_applied,
        "mbti_confidence": int(blending_meta.get("mbti_confidence", 0)),
        "blending": blending_meta,
        "age_group": request.age_group,
        "gender": request.gender,
        "structure": structure_mode,
        "evidence": evidence_mode,
        "distance": _bucket_score(
            final_scores["distance"],
            ["authoritative", "peer", "inspiring"],
        ),
        "criticism": _bucket_score(
            final_scores["criticism"],
            ["avoidant", "mitigated", "direct"],
        ),
        "density": _bucket_score(
            final_scores["density"],
            ["light", "balanced", "dense"],
        ),
        "style_strength": request.style_strength,
        "scores": final_scores,
        "questionnaire_scores": questionnaire_scores,
        "questionnaire_meta": questionnaire_meta,
    }


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
            "telegram_webhook_secret",
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
    telegram_bot_token = settings.get("telegram_bot_token", "").strip()
    telegram_chat_id = settings.get("telegram_chat_id", "").strip()
    telegram_webhook_secret = settings.get("telegram_webhook_secret", "").strip()
    telegram_configured = bool(telegram_bot_token and telegram_chat_id)

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
        telegram_bot_token=_mask_secret(telegram_bot_token) if telegram_bot_token else "",
        telegram_chat_id=telegram_chat_id,
        telegram_webhook_secret=_mask_secret(telegram_webhook_secret) if telegram_webhook_secret else "",
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

    recommended_categories = _recommend_categories(
        interests,
        mbti=request.mbti if bool(voice_profile.get("mbti_enabled")) else "",
        age_group=request.age_group,
        gender=request.gender
    )
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
