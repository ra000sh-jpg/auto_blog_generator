"""헬스 체크 API."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from modules.automation.time_utils import now_utc
from modules.config import AppConfig
from modules.llm.api_health import check_all_providers
from modules.llm.llm_router import LLMRouter
from server.dependencies import get_app_config, get_llm_router

router = APIRouter()


class ProviderHealth(BaseModel):
    """프로바이더 단위 상태."""

    provider: str
    model: str
    status: str
    message: str


class HealthSummary(BaseModel):
    """상태 요약."""

    total: int = 0
    ok: int = 0
    fail: int = 0


class HealthResponse(BaseModel):
    """헬스 체크 응답."""

    status: str = Field(description="ok|degraded")
    timestamp: str
    summary: HealthSummary
    providers: List[ProviderHealth]
    warnings: List[str]


def _is_key_missing_message(message: str) -> bool:
    """API 키 누락 에러 문구를 판별한다."""
    lowered = str(message).lower()
    return (
        "api_key" in lowered
        or "환경변수가 필요" in lowered
        or "required" in lowered
    )


@router.get("/health", response_model=HealthResponse, summary="API/LLM 상태 조회")
async def get_health(
    app_config: AppConfig = Depends(get_app_config),
    llm_router: LLMRouter = Depends(get_llm_router),
) -> HealthResponse:
    """API 서버 및 LLM 연동 상태를 조회한다."""
    warnings: List[str] = []

    try:
        router_settings = llm_router.get_saved_settings()
        text_api_keys = router_settings.get("text_api_keys", {})
        rows = await check_all_providers(
            skip_expensive=True,
            llm_config=app_config.llm,
            api_keys=text_api_keys,
        )
    except Exception as exc:
        rows = []
        warnings.append(f"헬스 체크 중 예외 발생: {exc}")

    providers = [ProviderHealth(**row) for row in rows]
    ok_count = sum(1 for row in providers if str(row.status).upper() == "OK")
    fail_rows = [row for row in providers if str(row.status).upper() != "OK"]
    fail_count = len(fail_rows)

    if fail_rows:
        warnings.extend([f"{row.provider}: {row.message}" for row in fail_rows])

    missing_key_failures = [
        row for row in fail_rows if _is_key_missing_message(row.message)
    ]

    # 키 누락 포함 장애 상황은 서버 에러가 아닌 degraded로 반환한다.
    if fail_count == 0:
        overall = "ok"
    elif len(missing_key_failures) == fail_count:
        overall = "degraded"
    else:
        overall = "degraded"

    return HealthResponse(
        status=overall,
        timestamp=now_utc(),
        summary=HealthSummary(total=len(providers), ok=ok_count, fail=fail_count),
        providers=providers,
        warnings=warnings,
    )

