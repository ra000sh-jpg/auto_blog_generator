#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="/Users/naseunghwan/Desktop/auto_blog_generator"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_TARGET="${LAUNCH_AGENTS_DIR}/com.autoblog.worker.plist"
LABEL="com.autoblog.worker"
GUI_DOMAIN="gui/$(id -u)"
LOG_DIR="${WORKSPACE_DIR}/logs"
STDOUT_LOG="${LOG_DIR}/worker.stdout.log"
STDERR_LOG="${LOG_DIR}/worker.stderr.log"
ERROR_PATTERN="AUTH_EXPIRED|CAPTCHA_REQUIRED|ELEMENT_NOT_FOUND|NETWORK_TIMEOUT|RATE_LIMITED|CONTENT_REJECTED|PUBLISH_FAILED|PIPELINE_ERROR|QUALITY_FAILED|BUDGET_EXCEEDED|WORKER_CRASH|CRITICAL|ERROR"

echo "== AutoBlog Worker Status =="
echo "label: ${LABEL}"
echo "plist: ${PLIST_TARGET}"
echo

if [[ -f "${PLIST_TARGET}" ]]; then
  echo "[INFO] plist 파일 존재"
else
  echo "[WARN] plist 파일 없음"
fi

STATUS_OUTPUT="$(launchctl print "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true)"
if [[ -n "${STATUS_OUTPUT}" ]]; then
  PID="$(printf '%s\n' "${STATUS_OUTPUT}" | awk -F' = ' '/pid =/{print $2; exit}')"
  STATE="$(printf '%s\n' "${STATUS_OUTPUT}" | awk -F' = ' '/state =/{print $2; exit}')"
  EXIT_CODE="$(printf '%s\n' "${STATUS_OUTPUT}" | awk -F' = ' '/last exit code =/{print $2; exit}')"

  echo "[OK] launchctl 서비스 등록됨"
  echo "state: ${STATE:-unknown}"
  echo "pid: ${PID:-none}"
  echo "last_exit_code: ${EXIT_CODE:-unknown}"
else
  echo "[WARN] launchctl 서비스 미등록 또는 비활성"
fi

echo
echo "[INFO] run_scheduler.py 프로세스 확인"
if pgrep -f "${WORKSPACE_DIR}/scripts/run_scheduler.py" >/dev/null 2>&1; then
  pgrep -fl "${WORKSPACE_DIR}/scripts/run_scheduler.py"
else
  echo "프로세스 없음"
fi

echo
echo "[INFO] 로그 경로"
echo "stdout: ${STDOUT_LOG}"
echo "stderr: ${STDERR_LOG}"

if [[ -f "${STDOUT_LOG}" ]]; then
  echo
  echo "== stdout 최근 30줄 =="
  tail -n 30 "${STDOUT_LOG}"
else
  echo
  echo "[WARN] stdout 로그 파일 없음"
fi

if [[ -f "${STDERR_LOG}" ]]; then
  echo
  echo "== stderr 최근 30줄 =="
  tail -n 30 "${STDERR_LOG}"
else
  echo
  echo "[WARN] stderr 로그 파일 없음"
fi

echo
echo "== 에러 코드 강조 (최근 로그 검색) =="
if [[ -f "${STDOUT_LOG}" ]] || [[ -f "${STDERR_LOG}" ]]; then
  MATCHED_LINES="$(grep -nE "${ERROR_PATTERN}" "${STDOUT_LOG}" "${STDERR_LOG}" 2>/dev/null | tail -n 30 || true)"
  if [[ -n "${MATCHED_LINES}" ]]; then
    printf '%s\n' "${MATCHED_LINES}" | sed 's/^/[!] /'
  else
    echo "감지된 에러 코드 없음"
  fi
else
  echo "로그 파일이 없어 에러 스캔을 건너뜀"
fi
