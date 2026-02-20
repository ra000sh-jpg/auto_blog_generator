#!/usr/bin/env bash
set -euo pipefail

# 고정 경로: 설치 대상 워크스페이스
WORKSPACE_DIR="/Users/naseunghwan/Desktop/auto_blog_generator"
PLIST_SOURCE="${WORKSPACE_DIR}/scripts/com.autoblog.worker.plist"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_TARGET="${LAUNCH_AGENTS_DIR}/com.autoblog.worker.plist"
LOG_DIR="${WORKSPACE_DIR}/logs"
LABEL="com.autoblog.worker"
GUI_DOMAIN="gui/$(id -u)"

if [[ ! -f "${PLIST_SOURCE}" ]]; then
  echo "[ERROR] plist 템플릿을 찾을 수 없습니다: ${PLIST_SOURCE}"
  exit 1
fi

# 로그/LaunchAgents 디렉터리를 미리 만들어 런치 실패를 방지한다.
mkdir -p "${LAUNCH_AGENTS_DIR}" "${LOG_DIR}"
touch "${LOG_DIR}/worker.stdout.log" "${LOG_DIR}/worker.stderr.log"

cp "${PLIST_SOURCE}" "${PLIST_TARGET}"
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
echo "  - stdout: ${LOG_DIR}/worker.stdout.log"
echo "  - stderr: ${LOG_DIR}/worker.stderr.log"
