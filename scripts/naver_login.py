"""
네이버 세션 초기화 스크립트

사용법:
    python scripts/naver_login.py

동작:
    1. Chromium 브라우저를 headful 모드로 열고
    2. 네이버 로그인 페이지로 이동합니다
    3. 수동으로 로그인(2FA 포함)을 완료하면
    4. 세션 state가 data/sessions/naver/state.json에 저장됩니다

저장 경로:
    data/sessions/naver/state.json

안내:
    - 로그인 완료 후 블로그 홈 화면이 나오면 Enter를 누르세요
    - 2단계 인증(OTP/SMS) 필요 시 완료 후 Enter
"""

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).parent.parent))

StealthFunction = Callable[[Any], Awaitable[None]]


async def noop_stealth(_page: Any) -> None:
    """stealth 적용 실패 시 사용하는 no-op 함수."""
    return None


def resolve_stealth_function() -> StealthFunction:
    """playwright-stealth v1/v2 호환 stealth 함수를 반환한다."""
    try:
        stealth_module = importlib.import_module("playwright_stealth")
    except ImportError:
        print("⚠️  playwright-stealth 적용 불가 - stealth 없이 진행합니다.")
        return noop_stealth

    stealth_async = getattr(stealth_module, "stealth_async", None)
    if callable(stealth_async):
        async def apply_v1(page: Any) -> None:
            await stealth_async(page)
        return apply_v1

    stealth_class = getattr(stealth_module, "Stealth", None)
    if stealth_class is not None:
        try:
            stealth_instance = stealth_class()
        except Exception:
            print("⚠️  playwright-stealth 초기화 실패 - stealth 없이 진행합니다.")
            return noop_stealth

        apply_stealth_async = getattr(stealth_instance, "apply_stealth_async", None)
        if callable(apply_stealth_async):
            async def apply_v2(page: Any) -> None:
                await apply_stealth_async(page)
            return apply_v2

        stealth_method_async = getattr(stealth_instance, "stealth_async", None)
        if callable(stealth_method_async):
            async def apply_v2_alt(page: Any) -> None:
                await stealth_method_async(page)
            return apply_v2_alt

    print("⚠️  playwright-stealth 버전 비호환 - stealth 없이 진행합니다.")
    return noop_stealth


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        print("❌ playwright 미설치.")
        print("   pip3 install playwright playwright-stealth")
        print("   python3 -m playwright install chromium")
        raise SystemExit(1) from e

    stealth_fn = resolve_stealth_function()

    session_path = Path("data/sessions/naver")
    session_path.mkdir(parents=True, exist_ok=True)
    state_file = session_path / "state.json"

    print("=" * 55)
    print("  네이버 세션 초기화 스크립트")
    print("=" * 55)
    print()
    print("브라우저가 열립니다.")
    print("네이버 로그인을 완료한 뒤 이 터미널로 돌아와")
    print("Enter 키를 눌러 세션을 저장하세요.")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--start-maximized",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()
        await stealth_fn(page)

        # ── 네이버 로그인 페이지 열기 ──────────────────────
        await page.goto(
            "https://nid.naver.com/nidlogin.login?mode=form",
            wait_until="domcontentloaded",
        )

        print("✅ 브라우저 열림 → 로그인 진행 후 터미널에서 Enter 입력")

        # 터미널에서 Enter 대기
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: input("\n[Enter] 로그인 완료 후 Enter를 눌러 세션 저장: "),
        )

        # 로그인 여부 확인
        current_url = page.url
        if "nidlogin" in current_url or "login" in current_url.lower():
            print()
            print("⚠️  아직 로그인 페이지에 있습니다.")
            print("   로그인을 완료한 뒤 다시 Enter를 눌러주세요.")
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: input("[Enter] 재확인: "),
            )

        # 세션 저장
        await context.storage_state(path=str(state_file))
        print()
        print(f"✅ 세션 저장 완료: {state_file}")
        print()

        # 블로그 ID 안내
        blog_url = "https://blog.naver.com/"
        await page.goto(blog_url, wait_until="domcontentloaded")
        final_url = page.url
        print("─" * 55)
        print("다음 명령으로 실발행 워커를 시작하세요:")
        print()
        print("  export NAVER_BLOG_ID=<your_blog_id>")
        print("  python scripts/run_worker.py --poll-interval 10")
        print()
        print(f"현재 페이지 URL: {final_url}")
        print("URL 에서 blog.naver.com/ 뒤의 블로그 ID를 확인하세요.")
        print("─" * 55)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
