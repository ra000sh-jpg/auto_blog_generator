"""FastAPI BFF 엔트리포인트."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict

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


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """앱 생명주기 훅.

    API 서버 시작 시 DI 객체를 미리 초기화해 초기 요청 지연을 줄인다.
    """
    _ = get_job_store()
    yield


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


@app.get("/", tags=["root"])
def root() -> Dict[str, str]:
    """루트 엔드포인트."""
    return {
        "name": "auto-blog-generator-api",
        "docs": "/docs",
        "health": "/api/health",
    }
