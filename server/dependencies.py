"""FastAPI 의존성 주입 모듈."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from modules.automation.job_store import JobStore
from modules.automation.pipeline_service import PipelineService, stub_generate_fn
from modules.config import AppConfig, load_config
from modules.llm.idea_vault_parser import IdeaVaultBatchParser
from modules.llm.llm_router import LLMRouter
from modules.llm.magic_input_parser import MagicInputParser
from modules.uploaders.playwright_publisher import PublishResult


class _NoopPublisher:
    """API 서버 전용 더미 발행기.

    FastAPI는 워커를 직접 실행하지 않으므로 실제 발행을 수행하지 않는다.
    """

    async def publish(  # pragma: no cover - 향후 확장 포인트
        self,
        title: str,
        content: str,
        thumbnail: Optional[str] = None,
        images: Optional[list[str]] = None,
        image_points: Optional[list] = None,
        tags: Optional[list[str]] = None,
        category: Optional[str] = None,
    ) -> PublishResult:
        del title, content, thumbnail, images, image_points, tags, category
        return PublishResult(
            success=False,
            error_code="API_ONLY_MODE",
            error_message="FastAPI 서버는 워커 실행 없이 상태 조회/등록만 처리합니다.",
        )


@lru_cache(maxsize=1)
def get_app_config() -> AppConfig:
    """애플리케이션 설정을 반환한다."""
    return load_config()


@lru_cache(maxsize=1)
def get_db_path() -> str:
    """DB 경로를 반환한다."""
    return os.getenv("AUTOBLOG_DB_PATH", "data/automation.db")


@lru_cache(maxsize=1)
def get_job_store() -> JobStore:
    """JobStore 인스턴스를 반환한다."""
    return JobStore(db_path=get_db_path())


@lru_cache(maxsize=1)
def get_pipeline_service() -> PipelineService:
    """PipelineService 인스턴스를 반환한다.

    현재 Step 1에서는 라우터에서 직접 사용하지 않지만,
    이후 확장을 위해 DI 구조만 먼저 고정한다.
    """
    config = get_app_config()
    quality_evaluator = None
    try:
        from modules.llm.provider_factory import create_client
        from modules.automation.quality_evaluator import QualityEvaluator
        eval_client = create_client(
            provider=config.llm.primary_provider,
            model=config.llm.primary_model,
            timeout_sec=config.llm.timeout_sec,
        )
        quality_evaluator = QualityEvaluator(llm_client=eval_client)
    except Exception:
        pass

    return PipelineService(
        job_store=get_job_store(),
        publisher=_NoopPublisher(),
        generate_fn=stub_generate_fn,
        quality_evaluator=quality_evaluator,
    )


@lru_cache(maxsize=1)
def get_llm_router() -> LLMRouter:
    """LLM 라우터 인스턴스를 반환한다."""
    return LLMRouter(
        job_store=get_job_store(),
        llm_config=get_app_config().llm,
    )


@lru_cache(maxsize=1)
def get_magic_input_parser() -> MagicInputParser:
    """매직 인풋 파서를 반환한다."""
    return MagicInputParser(
        llm_config=get_app_config().llm,
        llm_router=get_llm_router(),
    )


@lru_cache(maxsize=1)
def get_idea_vault_parser() -> IdeaVaultBatchParser:
    """아이디어 창고 배치 파서를 반환한다."""
    return IdeaVaultBatchParser(llm_config=get_app_config().llm)
