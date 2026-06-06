"""브라우저 기반 정부 첨부파일 다운로드 보조기."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit


class PlaywrightAttachmentDownloader:
    """HTTP 다운로드가 막힌 첨부를 실제 브라우저 클릭으로 내려받는다."""

    def download(self, *, detail_url: str, file_url: str, timeout_sec: float = 20.0) -> bytes:
        if not detail_url or not file_url:
            raise ValueError("detail_url and file_url are required")
        try:
            from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]
        except Exception as exc:
            raise RuntimeError("playwright is not installed") from exc

        token = self._locator_token(file_url)
        timeout_ms = int(max(5.0, timeout_sec) * 1000)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            try:
                page.goto(detail_url, wait_until="domcontentloaded", timeout=timeout_ms)
                locator = page.locator(f"a[href*='{token}']").first if token else page.locator("a[href*='/attach/down/']").first
                with page.expect_download(timeout=timeout_ms) as download_info:
                    locator.click(timeout=timeout_ms)
                download = download_info.value
                path = download.path()
                if not path:
                    raise RuntimeError("download path is empty")
                return Path(path).read_bytes()
            finally:
                context.close()
                browser.close()

    def _locator_token(self, file_url: str) -> str:
        path = urlsplit(str(file_url or "")).path.rstrip("/")
        parts = [part for part in path.split("/") if part]
        return parts[-1] if parts else ""
