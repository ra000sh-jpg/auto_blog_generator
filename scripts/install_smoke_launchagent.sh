#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="/Users/naseunghwan/Desktop/auto_blog_generator"
PLIST_SOURCE="${WORKSPACE_DIR}/scripts/com.autoblog.smoke.plist"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_TARGET="${LAUNCH_AGENTS_DIR}/com.autoblog.smoke.plist"
LOG_DIR="${WORKSPACE_DIR}/logs"
LABEL="com.autoblog.smoke"
GUI_DOMAIN="gui/$(id -u)"

if [[ ! -f "${PLIST_SOURCE}" ]]; then
  echo "[ERROR] plist 템플릿을 찾을 수 없습니다: ${PLIST_SOURCE}"
  exit 1
fi

mkdir -p "${LAUNCH_AGENTS_DIR}" "${LOG_DIR}"
touch "${LOG_DIR}/smoke.stdout.log" "${LOG_DIR}/smoke.stderr.log"

cp "${PLIST_SOURCE}" "${PLIST_TARGET}"
chmod 644 "${PLIST_TARGET}"

launchctl bootout "${GUI_DOMAIN}/${LABEL}" >/dev/null 2>&1 || true
launchctl unload -w "${PLIST_TARGET}" >/dev/null 2>&1 || true

if launchctl bootstrap "${GUI_DOMAIN}" "${PLIST_TARGET}" >/dev/null 2>&1; then
  :
else
  launchctl load -w "${PLIST_TARGET}"
fi

launchctl enable "${GUI_DOMAIN}/${LABEL}" >/dev/null 2>&1 || true

echo "[OK] Smoke LaunchAgent 설치 완료"
echo "  - label: ${LABEL}"
echo "  - plist: ${PLIST_TARGET}"
echo "  - schedule: daily 21:40"
echo "  - stdout: ${LOG_DIR}/smoke.stdout.log"
echo "  - stderr: ${LOG_DIR}/smoke.stderr.log"
