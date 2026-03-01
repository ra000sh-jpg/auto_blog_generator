#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_TARGET="${LAUNCH_AGENTS_DIR}/com.autoblog.smoke.plist"
LOG_DIR="${WORKSPACE_DIR}/logs"
LABEL="com.autoblog.smoke"
GUI_DOMAIN="gui/$(id -u)"

mkdir -p "${LAUNCH_AGENTS_DIR}" "${LOG_DIR}"
touch "${LOG_DIR}/smoke.stdout.log" "${LOG_DIR}/smoke.stderr.log"

cat > "${PLIST_TARGET}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.autoblog.smoke</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${WORKSPACE_DIR}/scripts/smoke_publish_daily.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${WORKSPACE_DIR}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>

    <!-- 매일 21:40 KST에 스모크 테스트를 실행한다. -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>21</integer>
        <key>Minute</key>
        <integer>40</integer>
    </dict>

    <key>RunAtLoad</key>
    <false/>

    <key>KeepAlive</key>
    <false/>

    <key>ProcessType</key>
    <string>Background</string>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/smoke.stdout.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/smoke.stderr.log</string>
</dict>
</plist>
PLIST

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
