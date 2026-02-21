"""네이버 세션 연동 API."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from modules.uploaders.playwright_publisher import _apply_stealth

router = APIRouter()

_CONNECT_LOCK = asyncio.Lock()
_STATE_PATH = Path("data/sessions/naver/state.json")


class NaverConnectStartRequest(BaseModel):
    """네이버 연동 시작 요청."""

    timeout_sec: int = Field(default=300, ge=60, le=900)


class NaverConnectResponse(BaseModel):
    """네이버 연동 응답."""

    success: bool
    connected: bool
    message: str
    state_path: str
    current_url: str = ""


class NaverConnectStatusResponse(BaseModel):
    """네이버 연동 상태 응답."""

    connected: bool
    state_path: str
    exists: bool
    updated_at_epoch: float = 0.0


def _state_status() -> Dict[str, object]:
    """세션 파일 상태를 계산한다."""
    exists = _STATE_PATH.exists()
    updated_at = _STATE_PATH.stat().st_mtime if exists else 0.0
    return {
        "connected": exists,
        "state_path": str(_STATE_PATH),
        "exists": exists,
        "updated_at_epoch": float(updated_at),
    }


@router.get(
    "/naver/connect/status",
    response_model=NaverConnectStatusResponse,
    summary="네이버 연동 상태 조회",
)
def get_naver_connect_status() -> NaverConnectStatusResponse:
    """현재 세션 파일 존재 여부를 반환한다."""
    status_payload = _state_status()
    return NaverConnectStatusResponse(**status_payload)


@router.post(
    "/naver/connect/start",
    response_model=NaverConnectResponse,
    summary="네이버 연동 팝업 시작",
)
async def start_naver_connect(
    request: NaverConnectStartRequest,
) -> NaverConnectResponse:
    """headful Playwright로 로그인 팝업을 띄우고 세션을 저장한다."""
    if _CONNECT_LOCK.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이미 다른 네이버 연동 세션이 진행 중입니다.",
        )

    async with _CONNECT_LOCK:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # pragma: no cover
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="playwright 미설치 또는 브라우저 런타임 준비가 필요합니다.",
            ) from exc

        browser = None
        current_url = ""
        deadline = time.monotonic() + float(request.timeout_sec)
        connected = False
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=False,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--start-maximized",
                    ],
                )
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/121.0.0.0 Safari/537.36"
                    ),
                )
                page = await context.new_page()
                await _apply_stealth(page)
                await page.goto(
                    "https://nid.naver.com/nidlogin.login?mode=form",
                    wait_until="domcontentloaded",
                )

                while time.monotonic() < deadline:
                    current_url = page.url
                    cookies = await context.cookies()
                    has_naver_auth_cookie = any(
                        str(cookie.get("name", "")).upper() in {"NID_AUT", "NID_SES"}
                        and str(cookie.get("value", "")).strip()
                        for cookie in cookies
                    )
                    if has_naver_auth_cookie and "nidlogin" not in current_url:
                        connected = True
                        break
                    if "blog.naver.com" in current_url and "nidlogin" not in current_url:
                        connected = True
                        break
                    await asyncio.sleep(1.0)

                if connected:
                    await context.storage_state(path=str(_STATE_PATH))
                await context.close()
                await browser.close()
                browser = None
        finally:
            if browser is not None:
                await browser.close()

        if connected:
            return NaverConnectResponse(
                success=True,
                connected=True,
                message="네이버 로그인 연동이 완료되었습니다.",
                state_path=str(_STATE_PATH),
                current_url=current_url,
            )
        return NaverConnectResponse(
            success=False,
            connected=False,
            message="로그인 완료를 감지하지 못했습니다. 다시 시도해 주세요.",
            state_path=str(_STATE_PATH),
            current_url=current_url,
        )

