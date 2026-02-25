#!/usr/bin/env bash
set -euo pipefail

SCRIPT_SOURCE="${BASH_SOURCE[0]}"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_SOURCE}")" && pwd)"
PROJECT_DIR=""
FRONTEND_DIR=""
LOG_DIR=""
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
SERVER_PLIST="${LAUNCH_AGENTS_DIR}/com.autoblog.server.plist"
SCHEDULER_PLIST="${LAUNCH_AGENTS_DIR}/com.autoblog.scheduler.plist"
REPO_URL="${AUTO_BLOG_REPO_URL:-https://github.com/ra000sh-jpg/auto_blog_generator.git}"
INSTALL_DIR="${AUTO_BLOG_INSTALL_DIR:-${HOME}/auto_blog_generator}"

step() {
  echo "🔧 $1..."
}

ensure_brew_shellenv() {
  if [[ -x /opt/homebrew/bin/brew ]]; then
    # Apple Silicon Homebrew 경로를 셸 환경에 반영한다.
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then
    # Intel Homebrew 경로를 셸 환경에 반영한다.
    eval "$(/usr/local/bin/brew shellenv)"
  fi
}

is_python_311_or_higher() {
  if ! command -v python3 >/dev/null 2>&1; then
    return 1
  fi
  python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

is_node_18_or_higher() {
  if ! command -v node >/dev/null 2>&1; then
    return 1
  fi
  local version major
  version="$(node --version 2>/dev/null | sed 's/^v//')"
  major="${version%%.*}"
  if [[ ! "${major}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  [[ "${major}" -ge 18 ]]
}

step "환경 확인"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "❌ 이 설치 스크립트는 macOS 전용입니다."
  exit 1
fi

ARCH="$(uname -m)"
if [[ "${ARCH}" != "arm64" && "${ARCH}" != "x86_64" ]]; then
  echo "❌ 지원하지 않는 CPU 아키텍처입니다: ${ARCH}"
  exit 1
fi
echo "✓ macOS 확인 완료 (ARCH=${ARCH})"

step "프로젝트 소스 준비"
if [[ -f "${SCRIPT_DIR}/../requirements.txt" ]]; then
  PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
  echo "✓ 이미 설치됨"
else
  if ! command -v git >/dev/null 2>&1; then
    echo "❌ git이 필요합니다. 먼저 Xcode Command Line Tools를 설치해 주세요."
    echo "   실행: xcode-select --install"
    exit 1
  fi

  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    echo "기존 설치 디렉터리를 업데이트합니다: ${INSTALL_DIR}"
    git -C "${INSTALL_DIR}" pull --ff-only origin main
  elif [[ -d "${INSTALL_DIR}" && -n "$(ls -A "${INSTALL_DIR}" 2>/dev/null || true)" ]]; then
    echo "❌ 설치 경로가 비어있지 않습니다: ${INSTALL_DIR}"
    echo "   AUTO_BLOG_INSTALL_DIR 환경변수로 다른 경로를 지정하세요."
    exit 1
  else
    git clone "${REPO_URL}" "${INSTALL_DIR}"
  fi
  PROJECT_DIR="${INSTALL_DIR}"
fi

FRONTEND_DIR="${PROJECT_DIR}/frontend"
LOG_DIR="${PROJECT_DIR}/logs"

step "Homebrew 설치 확인"
if command -v brew >/dev/null 2>&1; then
  echo "✓ 이미 설치됨"
else
  echo "Homebrew가 없어 자동 설치를 시작합니다."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
ensure_brew_shellenv
brew update

step "Python 3.11+ 확인"
if is_python_311_or_higher; then
  echo "✓ 이미 설치됨"
else
  brew install python@3.11
fi

step "Node.js 18+ 확인"
if is_node_18_or_higher; then
  echo "✓ 이미 설치됨"
else
  brew install node@18
fi

step "Playwright 브라우저 설치"
python3 -m pip install playwright
python3 -m playwright install chromium

step "프로젝트 의존성 설치"
python3 -m pip install -r "${PROJECT_DIR}/requirements.txt"
(
  cd "${FRONTEND_DIR}"
  npm install
  npm run build
)

step ".env 파일 초기화"
if [[ -f "${PROJECT_DIR}/.env" ]]; then
  echo "✓ 이미 설치됨"
else
  if [[ -f "${PROJECT_DIR}/.env.example" ]]; then
    cp "${PROJECT_DIR}/.env.example" "${PROJECT_DIR}/.env"
  else
    touch "${PROJECT_DIR}/.env"
  fi
fi

step "launchd 서비스 파일 생성/등록"
mkdir -p "${LAUNCH_AGENTS_DIR}" "${LOG_DIR}"

cat > "${SERVER_PLIST}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.autoblog.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>cd "${PROJECT_DIR}" &amp;&amp; python3 -m uvicorn server.main:app --host 127.0.0.1 --port 8000</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${PROJECT_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/server.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/server.err.log</string>
</dict>
</plist>
PLIST

cat > "${SCHEDULER_PLIST}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.autoblog.scheduler</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>cd "${PROJECT_DIR}" &amp;&amp; python3 scripts/run_scheduler.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${PROJECT_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/scheduler.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/scheduler.err.log</string>
</dict>
</plist>
PLIST

launchctl unload "${SERVER_PLIST}" >/dev/null 2>&1 || true
launchctl unload "${SCHEDULER_PLIST}" >/dev/null 2>&1 || true
launchctl load "${SERVER_PLIST}"
launchctl load "${SCHEDULER_PLIST}"

step "CLI 심볼릭 링크 등록"
CLI_SOURCE="${PROJECT_DIR}/scripts/auto-blog"
CLI_TARGET="/usr/local/bin/auto-blog"
if ln -sf "${CLI_SOURCE}" "${CLI_TARGET}" 2>/dev/null; then
  echo "✓ /usr/local/bin/auto-blog 링크 생성 완료"
else
  echo "권한이 필요하여 sudo로 링크를 생성합니다."
  sudo ln -sf "${CLI_SOURCE}" "${CLI_TARGET}"
fi

echo "✅ 설치 완료! http://localhost:3000 에서 접속하세요."
echo "초기 설정 파일: ${PROJECT_DIR}/.env"
