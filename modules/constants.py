"""프로젝트 전역 상수 정의.

모든 하드코딩된 기본값은 이 파일 한 곳에서만 정의한다.
실제 런타임 값은 DB(system_settings)에서 우선 조회하고,
없을 때 여기서 정의한 상수를 fallback으로 사용한다.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 카테고리
# ---------------------------------------------------------------------------

# 사용자가 블로그 카테고리를 아무것도 설정하지 않았을 때 사용하는 기본 fallback.
# 실제 운영에서는 온보딩 시 사용자가 입력한 값이 DB에 저장되어 이 값은 사용되지 않는다.
DEFAULT_FALLBACK_CATEGORY: str = "다양한 생각들"

# ---------------------------------------------------------------------------
# 스케줄러 운영 정책
# ---------------------------------------------------------------------------

# 활성 시간대 (KST 기준, 시작 이상 ~ 종료 미만)
ACTIVE_HOURS_START: int = 8    # 08:00
ACTIVE_HOURS_END: int = 22     # 22:00
ACTIVE_HOURS_DISPLAY: str = "08:00~22:00"

# ---------------------------------------------------------------------------
# 타이밍 / 폴링 상수
# ---------------------------------------------------------------------------

# 스케줄러 워커 폴링
SCHEDULER_GENERATOR_POLL_SEC: int = 30
SCHEDULER_PUBLISHER_POLL_SEC: int = 20
SCHEDULER_TELEGRAM_UPDATE_POLL_SEC: int = 20

# 스케줄러 내부 반복 지연
SCHEDULER_SUBJOB_STEP_SLEEP_SEC: float = 0.1
SCHEDULER_DRAFT_PREFETCH_STEP_SLEEP_SEC: float = 0.3
SCHEDULER_DAEMON_KEEPALIVE_SEC: int = 3600

# 발행/LLM 공통 타이밍
PLAYWRIGHT_ACTION_DELAY_SEC: float = 0.6
LLM_RETRY_BASE_DELAY_SEC: float = 1.0
LLM_REQUEST_TIMEOUT_SEC: float = 60.0
PUBLISH_RETRY_SLEEP_SEC: float = 5.0
METRICS_POLL_INTERVAL_SEC: int = 300
PIPELINE_STUB_ASYNC_DELAY_SEC: float = 0.1

# ---------------------------------------------------------------------------
# LLM 기본 모델
# ---------------------------------------------------------------------------

DEFAULT_DEEPSEEK_MODEL: str = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_PRO_MODEL: str = "deepseek-v4-pro"

# ---------------------------------------------------------------------------
# VLM 시각 평가
# ---------------------------------------------------------------------------

VLM_DEFAULT_MODEL: str = "meta/llama-3.2-90b-vision-instruct"
VLM_SCREENSHOT_VIEWPORT_WIDTH: int = 1280
VLM_SCREENSHOT_VIEWPORT_HEIGHT: int = 800
VLM_SCREENSHOT_WAIT_SEC: float = 3.0
VLM_SCREENSHOT_RETENTION_MAX: int = 50
VLM_MAX_IMAGE_SIZE_BYTES: int = 5 * 1024 * 1024
VLM_REQUEST_TIMEOUT_SEC: float = 60.0
VLM_DEFAULT_STRATEGY_MODE: str = "inherit"
VLM_DEFAULT_EVAL_SAMPLING_RATE: float = 0.5
VLM_DEFAULT_QUALITY_FLOOR: float = 65.0
VLM_DEFAULT_MAX_COST_GUARD_KRW: float = 30.0
VLM_ROUTER_EST_INPUT_TOKENS: int = 2500
VLM_ROUTER_EST_OUTPUT_TOKENS: int = 300
VLM_ROUTER_MAX_CANDIDATES: int = 3
VLM_EARLY_EXIT_MIN_QUALITY_SCORE: float = 60.0
VLM_EARLY_EXIT_MIN_CONTENT_LENGTH: int = 3500
VLM_DEFAULT_USD_TO_KRW: float = 1400.0
VLM_DISCOVERY_SYNC_STALE_HOURS: int = 18
VLM_PRICING_SYNC_STALE_HOURS: int = 18
VLM_VALIDATION_SYNC_STALE_HOURS: int = 6
VLM_VALIDATION_CANDIDATE_LIMIT: int = 10
VLM_VALIDATION_AUTO_ACTIVATE: bool = True
VLM_SCHED_DISCOVERY_HOUR: int = 3
VLM_SCHED_DISCOVERY_MINUTE: int = 15
VLM_SCHED_PRICING_HOUR: int = 3
VLM_SCHED_PRICING_MINUTE: int = 30
VLM_SCHED_VALIDATION_HOURS: str = "*/6"
VLM_SCHED_VALIDATION_MINUTE: int = 45

# ---------------------------------------------------------------------------
# 텍스트 모델 카탈로그/가격 동기화
# ---------------------------------------------------------------------------

TEXT_MODEL_DISCOVERY_SYNC_STALE_HOURS: int = 24
TEXT_MODEL_SCHED_DISCOVERY_HOUR: int = 3
TEXT_MODEL_SCHED_DISCOVERY_MINUTE: int = 5

# ---------------------------------------------------------------------------
# Voice Rewrite 품질 가드
# ---------------------------------------------------------------------------
# Quality Layer가 중립적으로 작성한 뒤 Voice가 말투를 추가하면 자연스럽게 길어진다.
# 1.15였던 원래 값은 Voice 적용 후 길이 증가(~23%)를 오탐하여 fallback을 유발했음.
# 정보 보전은 _is_voice_rewrite_safe() 의미론 검사가 담당하므로 길이 허용치를 완화.
VOICE_REWRITE_MIN_LENGTH_RATIO: float = 0.85   # 85% 미만 → 내용 삭제 위험
VOICE_REWRITE_MAX_LENGTH_RATIO: float = 1.30   # 130% 초과 → 내용 추가/반복 위험

# ---------------------------------------------------------------------------
# Auto Feedback Loop
# ---------------------------------------------------------------------------

FEEDBACK_MIN_OBSERVATION_COUNT: int = 5
FEEDBACK_NOISE_FLOOR: float = 5.0
FEEDBACK_KEEP_THRESHOLD: float = 3.0
FEEDBACK_DECISION_MIN_POSTS: int = 3
FEEDBACK_MAX_CONCURRENT_ACTIVE_RULES: int = 3
FEEDBACK_PENDING_TIMEOUT_HOURS: int = 24
FEEDBACK_SNOOZE_REMIND_HOURS: int = 72
FEEDBACK_CALLBACK_TOKEN_TTL_HOURS: int = 24
FEEDBACK_CANDIDATE_BATCH_LIMIT: int = 5

# 워커 폴링 간격 (초)
DEFAULT_GENERATOR_POLL_SECONDS: int = SCHEDULER_GENERATOR_POLL_SEC
DEFAULT_PUBLISHER_POLL_SECONDS: int = SCHEDULER_PUBLISHER_POLL_SEC
DEFAULT_TELEGRAM_UPDATE_POLL_SECONDS: int = SCHEDULER_TELEGRAM_UPDATE_POLL_SEC

# 일일 발행 목표 (DB system_settings 미설정 시 fallback)
DEFAULT_DAILY_TARGET: int = 3

# 아이디어 창고 일일 소진 쿼터
DEFAULT_IDEA_VAULT_DAILY_QUOTA: int = 2
