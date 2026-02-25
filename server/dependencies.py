"""FastAPI 의존성 주입 모듈."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from modules.automation.job_store import JobStore
from modules.config import AppConfig, load_config
from modules.llm.idea_vault_parser import IdeaVaultBatchParser
from modules.llm.llm_router import LLMRouter
from modules.llm.magic_input_parser import MagicInputParser


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
