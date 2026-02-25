#!/usr/bin/env bash
set -euo pipefail

# 워크스페이스는 현재 스크립트 위치 기준으로 계산
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PLIST_SOURCE="${WORKSPACE_DIR}/scripts/com.autoblog.worker.plist"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_TARGET="${LAUNCH_AGENTS_DIR}/com.autoblog.worker.plist"
LOG_DIR="${WORKSPACE_DIR}/logs"
LABEL="com.autoblog.worker"
GUI_DOMAIN="gui/$(id -u)"
RUN_SCHEDULER_PATH="${WORKSPACE_DIR}/scripts/run_scheduler.py"

PYTHON_PATH="${AUTOBLOG_PYTHON:-}"
if [[ -z "${PYTHON_PATH}" ]]; then
  if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python3" ]]; then
    PYTHON_PATH="${VIRTUAL_ENV}/bin/python3"
  else
    PYTHON_PATH="$(command -v python3 || true)"
  fi
fi
if [[ -z "${PYTHON_PATH}" || ! -x "${PYTHON_PATH}" ]]; then
  echo "[ERROR] 실행 가능한 python3 경로를 찾지 못했습니다."
  exit 1
fi

DB_PATH="${AUTOBLOG_DB_PATH:-${WORKSPACE_DIR}/data/automation.db}"

if [[ ! -f "${PLIST_SOURCE}" ]]; then
  echo "[ERROR] plist 템플릿을 찾을 수 없습니다: ${PLIST_SOURCE}"
  exit 1
fi

# 로그/LaunchAgents 디렉터리를 미리 만들어 런치 실패를 방지한다.
mkdir -p "${LAUNCH_AGENTS_DIR}" "${LOG_DIR}"
touch "${LOG_DIR}/worker.stdout.log" "${LOG_DIR}/worker.stderr.log"

# 템플릿을 현재 환경값으로 치환해 플리스 생성
sed \
  -e "s#{{PYTHON_PATH}}#${PYTHON_PATH}#g" \
  -e "s#{{RUN_SCHEDULER_PATH}}#${RUN_SCHEDULER_PATH}#g" \
  -e "s#{{WORKSPACE_DIR}}#${WORKSPACE_DIR}#g" \
  -e "s#{{DB_PATH}}#${DB_PATH}#g" \
  "${PLIST_SOURCE}" > "${PLIST_TARGET}"
chmod 644 "${PLIST_TARGET}"

# 기존 서비스가 있으면 먼저 내린다.
launchctl bootout "${GUI_DOMAIN}/${LABEL}" >/dev/null 2>&1 || true
launchctl unload -w "${PLIST_TARGET}" >/dev/null 2>&1 || true

# 최신 방식 우선, 실패 시 구형 방식으로 폴백한다.
if launchctl bootstrap "${GUI_DOMAIN}" "${PLIST_TARGET}" >/dev/null 2>&1; then
  :
else
  launchctl load -w "${PLIST_TARGET}"
fi

launchctl enable "${GUI_DOMAIN}/${LABEL}" >/dev/null 2>&1 || true
launchctl kickstart -k "${GUI_DOMAIN}/${LABEL}" >/dev/null 2>&1 || true

echo "[OK] LaunchAgent 설치 완료"
echo "  - label: ${LABEL}"
echo "  - plist: ${PLIST_TARGET}"
echo "  - python: ${PYTHON_PATH}"
echo "  - db: ${DB_PATH}"
echo "  - stdout: ${LOG_DIR}/worker.stdout.log"
echo "  - stderr: ${LOG_DIR}/worker.stderr.log"
