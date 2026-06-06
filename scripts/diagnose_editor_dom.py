"""네이버 스마트에디터 DOM/화면 진단 스크립트."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

from modules.uploaders.editor_diagnostics import run_naver_editor_diagnostics


def parse_args() -> argparse.Namespace:
    """CLI 인자를 파싱한다."""

    parser = argparse.ArgumentParser(description="네이버 스마트에디터 DOM/화면 진단")
    parser.add_argument("--headful", action="store_true", help="브라우저 화면 표시")
    parser.add_argument(
        "--channel",
        default=os.getenv("PLAYWRIGHT_BROWSER_CHANNEL", ""),
        help="브라우저 채널 예: chrome",
    )
    parser.add_argument(
        "--output-dir",
        default=os.getenv("NAVER_EDITOR_PREFLIGHT_DIR", "data/editor_diagnostics"),
        help="진단 리포트/스크린샷 저장 디렉터리",
    )
    return parser.parse_args()


async def main() -> None:
    """네이버 글쓰기 화면을 열어 진단 리포트를 생성한다."""

    args = parse_args()
    blog_id = os.getenv("NAVER_BLOG_ID", "").strip()
    if not blog_id:
        raise RuntimeError("NAVER_BLOG_ID 환경변수가 필요합니다.")

    report = await run_naver_editor_diagnostics(
        blog_id=blog_id,
        output_dir=args.output_dir,
        headless=not args.headful,
        browser_channel=str(args.channel or "").strip(),
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    print()
    print(f"상태: {report.status}")
    print(f"리포트: {report.report_path}")
    print(f"스크린샷: {report.screenshot_path}")
    if report.failures:
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
