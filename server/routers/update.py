"""업데이트 실행/조회 API."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.responses import StreamingResponse

router = APIRouter()

PROJECT_DIR = Path(__file__).resolve().parents[2]
UPDATE_SCRIPT_PATH = PROJECT_DIR / "scripts" / "update.sh"


class UpdateVersionResponse(BaseModel):
    commit_hash: str
    commit_message: str
    committed_at: str


class UpdateCheckResponse(BaseModel):
    behind: int
    up_to_date: bool


def _run_git_command(args: list[str]) -> str:
    """git 명령을 실행하고 표준 출력을 반환한다."""
    result = subprocess.run(
        ["git", *args],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or "git 명령 실행 실패"
        raise RuntimeError(stderr)
    return result.stdout.strip()


@router.get(
    "/update/version",
    response_model=UpdateVersionResponse,
    summary="현재 배포 버전 정보 조회",
)
def get_update_version() -> UpdateVersionResponse:
    try:
        raw = _run_git_command(["log", "-1", '--format=%H|%s|%ai'])
        commit_hash, commit_message, committed_at = raw.split("|", 2)
        return UpdateVersionResponse(
            commit_hash=commit_hash,
            commit_message=commit_message,
            committed_at=committed_at,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"버전 조회 실패: {exc}") from exc


@router.get(
    "/update/check",
    response_model=UpdateCheckResponse,
    summary="원격 저장소 대비 업데이트 필요 여부 조회",
)
def check_update() -> UpdateCheckResponse:
    try:
        # 원격 main 브랜치 기준으로 최신 정보를 가져온다.
        subprocess.run(
            ["git", "fetch", "origin", "main", "--quiet"],
            cwd=PROJECT_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
        raw_count = _run_git_command(["rev-list", "HEAD..origin/main", "--count"])
        behind = int(raw_count or "0")
        return UpdateCheckResponse(
            behind=behind,
            up_to_date=behind == 0,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"업데이트 확인 실패: {exc}") from exc


@router.post(
    "/update/run",
    summary="업데이트 스크립트 실행(로그 스트리밍)",
)
async def run_update() -> StreamingResponse:
    if not UPDATE_SCRIPT_PATH.exists():
        raise HTTPException(status_code=404, detail=f"업데이트 스크립트를 찾을 수 없습니다: {UPDATE_SCRIPT_PATH}")

    async def stream_log():
        process = await asyncio.create_subprocess_exec(
            "bash",
            str(UPDATE_SCRIPT_PATH),
            cwd=str(PROJECT_DIR),
            env=os.environ.copy(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None

        while True:
            line = await process.stdout.readline()
            if not line:
                break
            yield line.decode("utf-8", errors="replace")

        exit_code = await process.wait()
        if exit_code != 0:
            yield f"❌ 업데이트 스크립트가 실패했습니다. exit={exit_code}\n"

    return StreamingResponse(stream_log(), media_type="text/plain; charset=utf-8")
