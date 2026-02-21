#!/usr/bin/env bash
set -euo pipefail

# 프로젝트 루트를 기준으로 백엔드/프론트엔드를 동시에 실행한다.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_PID=""
FRONTEND_PID=""

cleanup() {
  # 종료 시 자식 프로세스를 정리해서 좀비 프로세스를 방지한다.
  if [[ -n "${FRONTEND_PID}" ]] && kill -0 "${FRONTEND_PID}" >/dev/null 2>&1; then
    kill "${FRONTEND_PID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${BACKEND_PID}" ]] && kill -0 "${BACKEND_PID}" >/dev/null 2>&1; then
    kill "${BACKEND_PID}" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

cd "${ROOT_DIR}"

echo "[start_dev] Starting FastAPI on :8000"
python3 -m uvicorn server.main:app --reload --port 8000 &
BACKEND_PID=$!

echo "[start_dev] Starting Next.js on :3000"
cd "${ROOT_DIR}/frontend"
npm run dev -- --port 3000 &
FRONTEND_PID=$!

echo "[start_dev] backend pid=${BACKEND_PID}, frontend pid=${FRONTEND_PID}"
echo "[start_dev] Press Ctrl+C to stop both services."

# macOS 기본 bash(3.x) 호환을 위해 wait -n 대신 폴링 루프를 사용한다.
while true; do
  if ! kill -0 "${BACKEND_PID}" >/dev/null 2>&1; then
    echo "[start_dev] backend exited"
    break
  fi
  if ! kill -0 "${FRONTEND_PID}" >/dev/null 2>&1; then
    echo "[start_dev] frontend exited"
    break
  fi
  sleep 1
done
