#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${WORKSPACE_DIR}/logs"
STDOUT_LOG="${LOG_DIR}/smoke.stdout.log"
STDERR_LOG="${LOG_DIR}/smoke.stderr.log"

mkdir -p "${LOG_DIR}"
touch "${STDOUT_LOG}" "${STDERR_LOG}"

echo "== AutoBlog Smoke Logs =="
echo "stdout: ${STDOUT_LOG}"
echo "stderr: ${STDERR_LOG}"
echo "종료: Ctrl+C"
echo

tail -n 120 -f "${STDOUT_LOG}" "${STDERR_LOG}"
