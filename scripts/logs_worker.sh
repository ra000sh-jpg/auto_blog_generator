#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="/Users/naseunghwan/Desktop/auto_blog_generator"
LOG_DIR="${WORKSPACE_DIR}/logs"
STDOUT_LOG="${LOG_DIR}/worker.stdout.log"
STDERR_LOG="${LOG_DIR}/worker.stderr.log"

mkdir -p "${LOG_DIR}"
touch "${STDOUT_LOG}" "${STDERR_LOG}"

echo "== AutoBlog Worker Logs =="
echo "stdout: ${STDOUT_LOG}"
echo "stderr: ${STDERR_LOG}"
echo "종료: Ctrl+C"
echo

tail -n 100 -f "${STDOUT_LOG}" "${STDERR_LOG}"
