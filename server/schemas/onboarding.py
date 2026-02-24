from typing import Dict, List, Optional
from pydantic import BaseModel, Field

from modules.constants import DEFAULT_FALLBACK_CATEGORY as _DEFAULT_FALLBACK_CATEGORY
from modules.persona.questionnaire import QUESTIONNAIRE_VERSION

_DEFAULT_IDEA_VAULT_DAILY_QUOTA = 2


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
    percentage: Optional[float] = Field(default=None, ge=0.0, le=100.0)


class ScheduleSetupRequest(BaseModel):
    """Step3 스케줄/비율 저장 요청."""

    daily_posts_target: int = Field(default=3, ge=1, le=20)
    idea_vault_daily_quota: int = Field(default=_DEFAULT_IDEA_VAULT_DAILY_QUOTA, ge=0, le=20)
    allocations: List[ScheduleAllocationItem] = Field(default_factory=list)
    category_mapping: Dict[str, str] = Field(default_factory=dict)


class ScheduleSetupResponse(BaseModel):
    """Step3 스케줄/비율 저장 응답."""

    daily_posts_target: int
    idea_vault_daily_quota: int
    allocations: List[ScheduleAllocationItem]
    category_mapping: Dict[str, str]


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
    category_mapping: Dict[str, str]
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
