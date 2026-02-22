"""FastAPI BFF 엔트리포인트."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.dependencies import get_job_store
from server.routers.config import router as config_router
from server.routers.health import router as health_router
from server.routers.idea_vault import router as idea_vault_router
from server.routers.jobs import router as jobs_router
from server.routers.magic_input import router as magic_input_router
from server.routers.metrics import router as metrics_router
from server.routers.ai_toggle import router as ai_toggle_router
from server.routers.naver_connect import router as naver_connect_router
from server.routers.onboarding import router as onboarding_router
from server.routers.router_settings import router as router_settings_router
from server.routers.scheduler import router as scheduler_router
from server.routers.scheduler import set_scheduler_instance
from server.routers.telegram_webhook import router as telegram_webhook_router
from server.routers.telegram_webhook import collect_pending_updates

logger = logging.getLogger(__name__)


def _build_scheduler() -> Optional[object]:
    """SchedulerService 인스턴스를 구성해 반환한다.

    SCHEDULER_DISABLED=true 환경변수 시 None 을 반환한다.
    실제 LLM/Playwright 초기화는 run_scheduler_forever() 와 달리
    API 서버에서는 stub 기반으로만 동작한다.
    """
    if os.getenv("SCHEDULER_DISABLED", "false").lower() == "true":
        logger.info("Scheduler disabled via SCHEDULER_DISABLED env var")
        return None

    try:
        from modules.automation.scheduler_service import SchedulerService
        from modules.automation.notifier import TelegramNotifier
        from server.dependencies import get_job_store, get_app_config

        job_store = get_job_store()
        config = get_app_config()

        # API 서버 전용: PipelineService 없이 순수 큐 관리만 담당
        # (실제 생성·발행은 별도 run_scheduler_forever 프로세스에서 수행)
        notifier = TelegramNotifier.from_env(db_path=job_store.db_path)

        daily_target_raw = job_store.get_system_setting("scheduler_daily_posts_target", "3")
        try:
            daily_target = max(1, int(daily_target_raw))
        except (ValueError, TypeError):
            daily_target = 3

        scheduler = SchedulerService(
            pipeline_service=None,
            job_store=job_store,
            notifier=notifier,
            timezone_name="Asia/Seoul",
            daily_posts_target=daily_target,
        )
        return scheduler
    except Exception as exc:
        logger.warning("Scheduler init failed, running without scheduler: %s", exc)
        return None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """앱 생명주기 훅.

    API 서버 시작 시 DI 객체와 스케줄러를 초기화한다.
    스케줄러는 큐 시드·상태 조회 전용으로 동작한다.
    실제 LLM 생성·발행은 run_scheduler_forever() 별도 프로세스에서 수행한다.
    """
    job_store = get_job_store()

    # Telegram 오프라인 폴백: 서버 재시작 시 수면 중 쌓인 메시지 수집
    try:
        stored = await collect_pending_updates(job_store)
        if stored > 0:
            logger.info("Telegram offline fallback: stored %d items on startup", stored)
    except Exception as exc:
        logger.warning("Telegram offline fallback failed (non-fatal): %s", exc)

    scheduler = _build_scheduler()
    if scheduler is not None:
        try:
            await scheduler.start()
            set_scheduler_instance(scheduler)
            logger.info("Scheduler started within FastAPI lifespan")
        except Exception as exc:
            logger.warning("Scheduler start failed: %s", exc)
            scheduler = None

    yield

    if scheduler is not None:
        try:
            await scheduler.stop()
            logger.info("Scheduler stopped on shutdown")
        except Exception as exc:
            logger.warning("Scheduler stop error: %s", exc)


app = FastAPI(
    title="Auto Blog Generator API",
    description="CLI 코어를 감싸는 FastAPI BFF 레이어",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/api", tags=["health"])
app.include_router(jobs_router, prefix="/api", tags=["jobs"])
app.include_router(metrics_router, prefix="/api", tags=["metrics"])
app.include_router(ai_toggle_router, prefix="/api", tags=["ai-toggle"])
app.include_router(config_router, prefix="/api", tags=["config"])
app.include_router(onboarding_router, prefix="/api", tags=["onboarding"])
app.include_router(magic_input_router, prefix="/api", tags=["magic-input"])
app.include_router(idea_vault_router, prefix="/api", tags=["idea-vault"])
app.include_router(router_settings_router, prefix="/api", tags=["router-settings"])
app.include_router(naver_connect_router, prefix="/api", tags=["naver-connect"])
app.include_router(scheduler_router, prefix="/api", tags=["scheduler"])
app.include_router(telegram_webhook_router, prefix="/api", tags=["telegram"])


@app.get("/", tags=["root"])
def root() -> Dict[str, str]:
    """루트 엔드포인트."""
    return {
        "name": "auto-blog-generator-api",
        "docs": "/docs",
        "health": "/api/health",
    }
