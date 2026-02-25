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

# 워커 폴링 간격 (초)
DEFAULT_GENERATOR_POLL_SECONDS: int = 30
DEFAULT_PUBLISHER_POLL_SECONDS: int = 20

# 일일 발행 목표 (DB system_settings 미설정 시 fallback)
DEFAULT_DAILY_TARGET: int = 3

# 아이디어 창고 일일 소진 쿼터
DEFAULT_IDEA_VAULT_DAILY_QUOTA: int = 2
