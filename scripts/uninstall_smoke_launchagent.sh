#!/usr/bin/env bash
set -euo pipefail

LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_TARGET="${LAUNCH_AGENTS_DIR}/com.autoblog.smoke.plist"
LABEL="com.autoblog.smoke"
GUI_DOMAIN="gui/$(id -u)"

launchctl bootout "${GUI_DOMAIN}/${LABEL}" >/dev/null 2>&1 || true
launchctl unload -w "${PLIST_TARGET}" >/dev/null 2>&1 || true

if [[ -f "${PLIST_TARGET}" ]]; then
  rm -f "${PLIST_TARGET}"
fi

echo "[OK] Smoke LaunchAgent 제거 완료"
echo "  - label: ${LABEL}"
echo "  - removed plist: ${PLIST_TARGET}"
