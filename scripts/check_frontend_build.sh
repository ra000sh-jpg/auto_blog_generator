#!/usr/bin/env bash
set -euo pipefail

# 프론트엔드 빌드 검증 스크립트.
# Turbopack 제약이 있는 환경에서도 재현 가능하도록 webpack 빌드를 사용한다.

WORKSPACE_DIR="/Users/naseunghwan/Desktop/auto_blog_generator"
FRONTEND_DIR="${WORKSPACE_DIR}/frontend"

cd "${FRONTEND_DIR}"

echo "== Frontend Lint =="
npm run lint

echo
echo "== Frontend Build (webpack) =="
npx next build --webpack

echo
echo "✅ Frontend lint/build verification passed"
