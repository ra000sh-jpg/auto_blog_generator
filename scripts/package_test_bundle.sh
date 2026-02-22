#!/usr/bin/env bash
set -euo pipefail

# 프로젝트 루트를 계산한다.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${ROOT_DIR}/dist"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
ARCHIVE_NAME="auto_blog_generator_test_bundle_${TIMESTAMP}.tar.gz"
ARCHIVE_PATH="${OUT_DIR}/${ARCHIVE_NAME}"

mkdir -p "${OUT_DIR}"

# 민감정보/런타임 산출물/대용량 캐시를 제외한 전달용 번들을 생성한다.
tar -czf "${ARCHIVE_PATH}" \
  --exclude=".git" \
  --exclude=".env" \
  --exclude=".env.*" \
  --exclude=".DS_Store" \
  --exclude=".pytest_cache" \
  --exclude=".mypy_cache" \
  --exclude=".ruff_cache" \
  --exclude=".pydeps" \
  --exclude="frontend/node_modules" \
  --exclude="frontend/.next" \
  --exclude="data" \
  --exclude="logs" \
  --exclude="dist" \
  -C "${ROOT_DIR}" \
  .

echo "[OK] Test bundle created"
echo "  - path: ${ARCHIVE_PATH}"
echo "  - next: transfer this file to target laptop and extract"
