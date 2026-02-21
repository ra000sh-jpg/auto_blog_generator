#!/usr/bin/env bash
set -euo pipefail

# 프로젝트 루트로 이동한다.
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

# 기본값은 환경변수로 덮어쓸 수 있다.
TITLE="${SMOKE_TITLE:-[SMOKE] AI Toggle $(date '+%Y-%m-%d %H:%M')}"
KEYWORDS="${SMOKE_KEYWORDS:-테스트,AI토글,스모크}"
CATEGORY="${SMOKE_CATEGORY:-it}"
PERSONA="${SMOKE_PERSONA:-P1}"
VERIFY_MIN_EXPECTED="${SMOKE_VERIFY_MIN_EXPECTED:-1}"
HEADFUL_MODE="${SMOKE_HEADFUL:-true}"

SESSION_FILE="data/sessions/naver/state.json"
if [[ ! -f "$SESSION_FILE" ]]; then
  echo "❌ 세션 파일이 없습니다: $SESSION_FILE"
  echo "   먼저 python3 scripts/naver_login.py 를 실행해 로그인 세션을 생성하세요."
  exit 1
fi

export PLAYWRIGHT_HEADLESS=false
export NAVER_AI_TOGGLE_MODE="${NAVER_AI_TOGGLE_MODE:-metadata}"
export NAVER_AI_TOGGLE_POST_VERIFY=true

echo "======================================================="
echo "  Headful 스모크 테스트 시작"
echo "======================================================="
echo "  title      : $TITLE"
echo "  keywords   : $KEYWORDS"
echo "  category   : $CATEGORY"
echo "  persona    : $PERSONA"
echo "  toggleMode : $NAVER_AI_TOGGLE_MODE"
echo "  headful    : $HEADFUL_MODE"
echo "======================================================="

PUBLISH_ARGS=(
  --title "$TITLE"
  --keywords "$KEYWORDS"
  --category "$CATEGORY"
  --persona "$PERSONA"
  --use-llm
  --ai-only-images
  --ai-toggle-mode "$NAVER_AI_TOGGLE_MODE"
  --verify-ai-toggle
  --verify-min-expected "$VERIFY_MIN_EXPECTED"
)

HEADFUL_MODE_LOWER="$(printf '%s' "${HEADFUL_MODE}" | tr '[:upper:]' '[:lower:]')"
if [[ "${HEADFUL_MODE_LOWER}" == "true" ]]; then
  PUBLISH_ARGS+=(--headful)
fi

python3 scripts/publish_once.py "${PUBLISH_ARGS[@]}"

python3 - <<'PY'
import json
from pathlib import Path

report_path = Path("data/ai_toggle/last_report.json")
if not report_path.exists():
    print("❌ 리포트 없음: data/ai_toggle/last_report.json")
    raise SystemExit(1)

report = json.loads(report_path.read_text(encoding="utf-8"))
summary = report.get("summary", {})
pre = summary.get("prepublish", {})
post = summary.get("postverify", {})

print("=======================================================")
print("  AI 토글 요약")
print("=======================================================")
print(f"  expected_on : {report.get('expected_on', 0)}")
print(f"  actual_on   : {report.get('actual_on', 0)}")
print(f"  post_passed : {report.get('post_verify_passed', 0)}")
print(
    "  prepublish  : expected={expected} verified={verified} repaired={repaired} failed={failed}".format(
        expected=pre.get("expected_on", 0),
        verified=pre.get("verified_on", 0),
        repaired=pre.get("repaired", 0),
        failed=pre.get("failed", 0),
    )
)
print(
    "  postverify  : expected={expected} passed={passed} failed={failed}".format(
        expected=post.get("expected_on", 0),
        passed=post.get("passed", 0),
        failed=post.get("failed", 0),
    )
)
print("=======================================================")
PY
