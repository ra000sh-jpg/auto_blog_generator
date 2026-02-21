#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="/Users/naseunghwan/Desktop/auto_blog_generator"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_TARGET="${LAUNCH_AGENTS_DIR}/com.autoblog.smoke.plist"
LABEL="com.autoblog.smoke"
GUI_DOMAIN="gui/$(id -u)"
LOG_DIR="${WORKSPACE_DIR}/logs"
STDOUT_LOG="${LOG_DIR}/smoke.stdout.log"
STDERR_LOG="${LOG_DIR}/smoke.stderr.log"
AI_TOGGLE_REPORT="${WORKSPACE_DIR}/data/ai_toggle/last_report.json"
ERROR_PATTERN="ERROR|FAILED|Traceback|AUTH_EXPIRED|CAPTCHA_REQUIRED|NETWORK_TIMEOUT|PUBLISH_FAILED"

echo "== AutoBlog Smoke Status =="
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
echo "[INFO] 로그 경로"
echo "stdout: ${STDOUT_LOG}"
echo "stderr: ${STDERR_LOG}"

if [[ -f "${STDOUT_LOG}" ]]; then
  echo
  echo "== stdout 최근 30줄 =="
  tail -n 30 "${STDOUT_LOG}"
fi

if [[ -f "${STDERR_LOG}" ]]; then
  echo
  echo "== stderr 최근 30줄 =="
  tail -n 30 "${STDERR_LOG}"
fi

echo
echo "== 최근 AI 토글 요약 =="
if [[ -f "${AI_TOGGLE_REPORT}" ]]; then
  REPORT_MODE="$(grep -E '^  "mode": ' "${AI_TOGGLE_REPORT}" | tail -n 1 | cut -d'"' -f4 || true)"
  REPORT_URL="$(grep -E '^  "post_url": ' "${AI_TOGGLE_REPORT}" | tail -n 1 | cut -d'"' -f4 || true)"
  REPORT_EXPECTED_ON="$(grep -E '^  "expected_on": [0-9]+' "${AI_TOGGLE_REPORT}" | tail -n 1 | sed -E 's/[^0-9]//g' || true)"
  REPORT_POST_PASSED="$(grep -E '^  "post_verify_passed": [0-9]+' "${AI_TOGGLE_REPORT}" | tail -n 1 | sed -E 's/[^0-9]//g' || true)"
  REPORT_CREATED_AT="$(grep -E '^  "created_at": [0-9]+' "${AI_TOGGLE_REPORT}" | tail -n 1 | sed -E 's/[^0-9]//g' || true)"

  echo "mode: ${REPORT_MODE:-unknown}"
  echo "expected_on: ${REPORT_EXPECTED_ON:-unknown}"
  echo "post_verify_ok: ${REPORT_POST_PASSED:-unknown}"
  if [[ -n "${REPORT_EXPECTED_ON}" ]] && [[ -n "${REPORT_POST_PASSED}" ]] && [[ "${REPORT_EXPECTED_ON}" = "${REPORT_POST_PASSED}" ]]; then
    echo "ai_toggle_result: PASS"
  elif [[ -n "${REPORT_EXPECTED_ON}" ]] && [[ -n "${REPORT_POST_PASSED}" ]]; then
    echo "ai_toggle_result: FAIL"
  else
    echo "ai_toggle_result: UNKNOWN"
  fi
  if [[ -n "${REPORT_URL}" ]]; then
    echo "last_post_url: ${REPORT_URL}"
  fi
  if [[ -n "${REPORT_CREATED_AT}" ]]; then
    echo "report_created_at_unix: ${REPORT_CREATED_AT}"
  fi
else
  echo "AI 토글 리포트 파일 없음: ${AI_TOGGLE_REPORT}"
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
