#!/usr/bin/env bash
set -euo pipefail

# 프로젝트 루트 경로를 계산한다.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"

echo "🔧 실행 중인 서비스 중지..."
shopt -s nullglob
for plist in "${LAUNCH_AGENTS_DIR}"/com.autoblog.*.plist; do
  launchctl unload "${plist}" >/dev/null 2>&1 || true
done
shopt -u nullglob

echo "🔧 최신 코드 가져오기..."
cd "${PROJECT_DIR}"
git pull origin main

echo "🔧 Python 의존성 업데이트..."
python3 -m pip install -r requirements.txt

echo "🔧 프론트엔드 의존성/빌드 업데이트..."
(
  cd "${PROJECT_DIR}/frontend"
  npm install
  npm run build
)

echo "🔧 서비스 재시작..."
shopt -s nullglob
for plist in "${LAUNCH_AGENTS_DIR}"/com.autoblog.*.plist; do
  launchctl load "${plist}"
done
shopt -u nullglob

CURRENT_HASH="$(git rev-parse --short HEAD)"
echo "✅ 업데이트 완료!"
echo "현재 커밋: ${CURRENT_HASH}"
