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
FRONTEND_PLIST="${LAUNCH_AGENTS_DIR}/com.autoblog.frontend.plist"
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

ensure_node_toolchain_path() {
  local node20_prefix node_prefix
  node20_prefix="$(brew --prefix node@20 2>/dev/null || true)"
  if [[ -n "${node20_prefix}" && -d "${node20_prefix}/bin" ]]; then
    export PATH="${node20_prefix}/bin:${PATH}"
  fi

  node_prefix="$(brew --prefix node 2>/dev/null || true)"
  if [[ -n "${node_prefix}" && -d "${node_prefix}/bin" ]]; then
    export PATH="${node_prefix}/bin:${PATH}"
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

is_node_20_9_or_higher() {
  if ! command -v node >/dev/null 2>&1; then
    return 1
  fi
  local version major minor
  version="$(node --version 2>/dev/null | sed 's/^v//')"
  if [[ "${version}" != *.* ]]; then
    return 1
  fi
  major="${version%%.*}"
  minor="${version#*.}"
  minor="${minor%%.*}"
  if [[ ! "${major}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  if [[ ! "${minor}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  if [[ "${major}" -gt 20 ]]; then
    return 0
  fi
  if [[ "${major}" -lt 20 ]]; then
    return 1
  fi
  [[ "${minor}" -ge 9 ]]
}

step "환경 확인"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "❌ 이 설치 스크립트는 macOS 전용입니다."
  exit 1
fi

PROJECT_DIR_REAL="$(cd "${PROJECT_DIR:-${INSTALL_DIR:-.}}" &>/dev/null && pwd || echo "")"
if [[ "${PROJECT_DIR_REAL}" == *"/Desktop"* || "${PROJECT_DIR_REAL}" == *"/Documents"* || "${PROJECT_DIR_REAL}" == *"/Downloads"* ]]; then
  echo "⚠️  [경고] 프로젝트가 macOS 보호 폴더(Desktop/Documents/Downloads)에 있습니다."
  echo "   이 경우 launchd 서비스가 PermissionError로 실행되지 않을 수 있습니다."
  echo "   설치 완료 후 서비스가 동작하지 않으면 프로젝트 폴더를 홈 디렉토리($HOME)로 이동해 주세요."
  echo ""
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

step "Node.js 20.9+ 확인"
if is_node_20_9_or_higher; then
  echo "✓ 이미 설치됨"
else
  brew install node@20
fi
ensure_node_toolchain_path
if ! command -v npm >/dev/null 2>&1; then
  # node@20이 keg-only인 환경에서 PATH 노출을 시도한다.
  brew link --overwrite --force node@20 >/dev/null 2>&1 || true
  ensure_node_toolchain_path
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "❌ npm 명령을 찾을 수 없습니다. 터미널 재실행 후 다시 시도하거나, brew install node를 실행해 주세요."
  exit 1
fi
echo "✓ npm 경로 확인 완료: $(command -v npm)"

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

# Python 및 Node 실행 경로 탐색 (시스템 기본 경로가 꼬일 경우를 대비해 브루 경로 우선 탐색)
PYTHON_EXECUTABLE="$(command -v python3.11 || command -v python3 || echo "python3")"
NODE_EXECUTABLE="$(command -v node || echo "node")"

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
    <string>cd "${PROJECT_DIR}" &amp;&amp; "${PYTHON_EXECUTABLE}" -m uvicorn server.main:app --host 127.0.0.1 --port 8000</string>
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
    <string>cd "${PROJECT_DIR}" &amp;&amp; "${PYTHON_EXECUTABLE}" scripts/run_scheduler.py</string>
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

cat > "${FRONTEND_PLIST}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.autoblog.frontend</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>cd "${FRONTEND_DIR}" &amp;&amp; "${NODE_EXECUTABLE}" ./node_modules/.bin/next start -p 3000</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${FRONTEND_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/frontend.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/frontend.err.log</string>
</dict>
</plist>
PLIST

launchctl unload "${SERVER_PLIST}" >/dev/null 2>&1 || true
launchctl unload "${SCHEDULER_PLIST}" >/dev/null 2>&1 || true
launchctl unload "${FRONTEND_PLIST}" >/dev/null 2>&1 || true
launchctl load "${SERVER_PLIST}"
launchctl load "${SCHEDULER_PLIST}"
launchctl load "${FRONTEND_PLIST}"

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
