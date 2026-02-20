#!/usr/bin/env bash
set -euo pipefail

# 고정 경로: 설치 대상 워크스페이스
WORKSPACE_DIR="/Users/naseunghwan/Desktop/auto_blog_generator"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_TARGET="${LAUNCH_AGENTS_DIR}/com.autoblog.worker.plist"
LABEL="com.autoblog.worker"
GUI_DOMAIN="gui/$(id -u)"

# 동작 중 서비스가 있으면 먼저 종료한다.
launchctl bootout "${GUI_DOMAIN}/${LABEL}" >/dev/null 2>&1 || true
launchctl unload -w "${PLIST_TARGET}" >/dev/null 2>&1 || true

if [[ -f "${PLIST_TARGET}" ]]; then
  rm -f "${PLIST_TARGET}"
fi

echo "[OK] LaunchAgent 제거 완료"
echo "  - label: ${LABEL}"
echo "  - removed plist: ${PLIST_TARGET}"
echo "  - logs kept: ${WORKSPACE_DIR}/logs"
