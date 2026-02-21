"""매직 인풋 API."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from modules.automation.job_store import JobStore
from modules.automation.time_utils import now_utc, parse_iso
from modules.llm.magic_input_parser import MagicInputParseResult, MagicInputParser
from modules.llm.prompts import normalize_topic_mode
from modules.seo.platform_strategy import get_category_for_topic
from server.dependencies import get_job_store, get_magic_input_parser

router = APIRouter()


class MagicInputRequest(BaseModel):
    """매직 인풋 파싱 요청."""

    instruction: str = Field(description="자연어 지시문")
    platform: str = "naver"
    scheduled_at: Optional[str] = None


class MagicInputParseResponse(BaseModel):
    """매직 인풋 파싱 응답."""

    title: str
    seed_keywords: List[str]
    persona_id: str
    topic_mode: str
    schedule_time: Optional[str]
    confidence: float
    parser_used: str
    raw: Dict[str, Any]


class MagicCreateJobRequest(MagicInputRequest):
    """매직 인풋 기반 Job 생성 요청."""

    title_override: Optional[str] = None
    persona_id_override: Optional[str] = None
    topic_mode_override: Optional[str] = None
    keywords_override: List[str] = Field(default_factory=list)
    category_override: Optional[str] = None
    max_retries: int = 3
    tags: List[str] = Field(default_factory=list)


class MagicCreateJobResponse(BaseModel):
    """매직 인풋 기반 Job 생성 응답."""

    job_id: str
    status: str
    scheduled_at: str
    platform: str
    title: str
    seed_keywords: List[str]
    persona_id: str
    topic_mode: str
    category: str
    parser_used: str


def _normalize_scheduled_at(raw_value: Optional[str]) -> str:
    """입력 시간을 UTC ISO 문자열로 정규화한다."""
    if not raw_value:
        return now_utc()
    parsed = parse_iso(raw_value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_keywords(keywords: List[str]) -> List[str]:
    """키워드를 정리한다."""
    normalized = []
    seen = set()
    for keyword in keywords:
        value = str(keyword).strip()
        if not value:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(value)
    return normalized


def _to_int(raw_value: Any) -> int:
    """숫자형 값을 안전하게 정수로 변환한다."""
    try:
        return max(0, int(raw_value))
    except Exception:
        return 0


def _to_parse_response(parsed: MagicInputParseResult) -> MagicInputParseResponse:
    """파싱 결과 DTO를 API 응답으로 변환한다."""
    return MagicInputParseResponse(
        title=parsed.title,
        seed_keywords=parsed.seed_keywords,
        persona_id=parsed.persona_id,
        topic_mode=normalize_topic_mode(parsed.topic_mode),
        schedule_time=parsed.schedule_time,
        confidence=parsed.confidence,
        parser_used=parsed.parser_used,
        raw=parsed.raw,
    )


@router.post(
    "/magic-input/parse",
    response_model=MagicInputParseResponse,
    summary="자연어 매직 인풋 파싱",
)
async def parse_magic_input(
    request: MagicInputRequest,
    parser: MagicInputParser = Depends(get_magic_input_parser),
) -> MagicInputParseResponse:
    """자연어 지시문을 Job 파라미터로 분해한다."""
    instruction = request.instruction.strip()
    if not instruction:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="instruction은 비어 있을 수 없습니다.",
        )
    parsed = await parser.parse(instruction)
    return _to_parse_response(parsed)


@router.post(
    "/magic-input/jobs",
    response_model=MagicCreateJobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="매직 인풋으로 Job 생성",
)
async def create_job_from_magic_input(
    request: MagicCreateJobRequest,
    dry_run: bool = Query(default=False),
    parser: MagicInputParser = Depends(get_magic_input_parser),
    job_store: JobStore = Depends(get_job_store),
) -> MagicCreateJobResponse:
    """자연어 지시문을 파싱해 즉시 Job 큐에 삽입한다."""
    del dry_run  # 호환 옵션, 현재는 API 레벨 동작 동일
    instruction = request.instruction.strip()
    if not instruction:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="instruction은 비어 있을 수 없습니다.",
        )

    parsed = await parser.parse(instruction)
    parse_payload = _to_parse_response(parsed)

    title = (request.title_override or parse_payload.title).strip()
    if not title:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="title을 추출하지 못했습니다.",
        )

    platform = request.platform.strip().lower() or "naver"
    persona_id = (request.persona_id_override or parse_payload.persona_id).strip().upper() or "P1"
    if persona_id not in {"P1", "P2", "P3", "P4"}:
        persona_id = "P1"

    raw_topic = request.topic_mode_override or parse_payload.topic_mode
    topic_mode = normalize_topic_mode(raw_topic)
    if topic_mode not in {"cafe", "parenting", "it", "finance"}:
        topic_mode = "cafe"

    seed_keywords = _normalize_keywords(request.keywords_override or parse_payload.seed_keywords)
    if not seed_keywords:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="키워드를 추출하지 못했습니다.",
        )

    category = (request.category_override or "").strip()
    if not category:
        category = get_category_for_topic(topic_mode=topic_mode, platform=platform)

    resolved_schedule = request.scheduled_at or parse_payload.schedule_time
    scheduled_at = _normalize_scheduled_at(resolved_schedule)
    job_id = str(uuid.uuid4())
    success = job_store.schedule_job(
        job_id=job_id,
        title=title,
        seed_keywords=seed_keywords,
        platform=platform,
        persona_id=persona_id,
        scheduled_at=scheduled_at,
        max_retries=max(1, int(request.max_retries)),
        tags=request.tags,
        category=category,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="중복 작업 또는 동일 idempotency key로 등록 실패했습니다.",
        )

    parser_provider = str(parse_payload.parser_used or "").strip().lower()
    parser_model = str(parse_payload.raw.get("model", "")).strip()
    parser_input_tokens = _to_int(parse_payload.raw.get("input_tokens"))
    parser_output_tokens = _to_int(parse_payload.raw.get("output_tokens"))
    parser_calls = 1 if parser_provider and parser_provider != "heuristic" else 0
    job_store.record_job_metric(
        job_id=job_id,
        metric_type="parser",
        status="ok",
        input_tokens=parser_input_tokens,
        output_tokens=parser_output_tokens,
        provider=parser_provider,
        detail={
            "model": parser_model,
            "calls": parser_calls,
            "parser_used": parse_payload.parser_used,
        },
    )

    return MagicCreateJobResponse(
        job_id=job_id,
        status=job_store.STATUS_QUEUED,
        scheduled_at=scheduled_at,
        platform=platform,
        title=title,
        seed_keywords=seed_keywords,
        persona_id=persona_id,
        topic_mode=topic_mode,
        category=category,
        parser_used=parse_payload.parser_used,
    )
