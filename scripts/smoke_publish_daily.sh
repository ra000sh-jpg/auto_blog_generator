#!/usr/bin/env bash
set -euo pipefail

# 일일 자동 스모크 실행용 래퍼 스크립트
WORKSPACE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${WORKSPACE_DIR}"

# 자동 실행은 화면 점유를 줄이기 위해 기본 headless로 수행한다.
export SMOKE_HEADFUL="${SMOKE_HEADFUL:-false}"
export SMOKE_TITLE="${SMOKE_TITLE:-[DAILY_SMOKE] AI Toggle $(date '+%Y-%m-%d')}"
export SMOKE_KEYWORDS="${SMOKE_KEYWORDS:-일일스모크,AI토글,자동검증}"
export SMOKE_CATEGORY="${SMOKE_CATEGORY:-it}"
export SMOKE_PERSONA="${SMOKE_PERSONA:-P1}"

bash "${WORKSPACE_DIR}/scripts/smoke_publish_headful.sh"
