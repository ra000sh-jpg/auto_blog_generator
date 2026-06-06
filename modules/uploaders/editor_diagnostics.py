"""네이버 스마트에디터 화면 구조 사전 진단."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .publisher_constants import (
    BLOG_WRITE_URL,
    BODY_SELECTORS,
    DRAFT_SAVE_SELECTORS,
    PUBLISH_BTN_1_SELECTORS,
    TITLE_SELECTORS,
)


AI_TOGGLE_DIAGNOSTIC_SELECTORS = [
    "button:has-text('AI')",
    "text=AI 활용",
    "text=AI 생성",
    "[aria-label*='AI']",
    "[class*='ai'] button",
    "[class*='ai'] [role='switch']",
    "[class*='toggle']",
]

IMAGE_DIAGNOSTIC_SELECTORS = [
    "input[type='file']",
    "button[aria-label='사진']",
    "button[aria-label='이미지']",
    "[data-name='image']",
    ".se-image-toolbar-button",
    "[class*='image'] button",
]


@dataclass(frozen=True)
class SelectorGroupDiagnostic:
    """셀렉터 그룹별 화면 매칭 결과."""

    name: str
    required: bool
    selectors: tuple[str, ...]
    matched_selectors: tuple[str, ...] = ()
    total_count: int = 0
    visible_count: int = 0
    samples: tuple[Dict[str, str], ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """필수 그룹 통과 여부를 반환한다."""

        if not self.required:
            return True
        return self.visible_count > 0


@dataclass
class EditorDiagnosticReport:
    """네이버 에디터 진단 리포트."""

    status: str
    stage: str
    current_url: str
    page_title: str
    captured_at: str
    selector_groups: list[SelectorGroupDiagnostic]
    dom_summary: Dict[str, Any]
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    screenshot_path: str = ""
    report_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """JSON 저장용 dict로 변환한다."""

        return {
            "status": self.status,
            "stage": self.stage,
            "current_url": self.current_url,
            "page_title": self.page_title,
            "captured_at": self.captured_at,
            "selector_groups": [asdict(item) for item in self.selector_groups],
            "dom_summary": self.dom_summary,
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "recommendations": list(self.recommendations),
            "screenshot_path": self.screenshot_path,
            "report_path": self.report_path,
        }


async def diagnose_editor_page(
    page: Any,
    *,
    stage: str = "preflight",
    output_dir: str | Path = "data/editor_diagnostics",
    save_screenshot: bool = True,
) -> EditorDiagnosticReport:
    """열려 있는 네이버 에디터 페이지를 진단하고 리포트를 저장한다."""

    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    current_url = await _safe_page_url(page)
    page_title = await _safe_page_title(page)
    selector_groups = [
        await _check_selector_group(page, "title_input", TITLE_SELECTORS, required=True),
        await _check_selector_group(page, "body_input", BODY_SELECTORS, required=True),
        await _check_selector_group(page, "publish_button", PUBLISH_BTN_1_SELECTORS, required=False),
        await _check_selector_group(page, "draft_save_button", DRAFT_SAVE_SELECTORS, required=False),
        await _check_selector_group(page, "image_controls", IMAGE_DIAGNOSTIC_SELECTORS, required=False),
        await _check_selector_group(page, "ai_toggle_controls", AI_TOGGLE_DIAGNOSTIC_SELECTORS, required=False),
    ]
    dom_summary = await _extract_dom_summary(page)

    report = evaluate_editor_diagnostics(
        current_url=current_url,
        page_title=page_title,
        stage=stage,
        captured_at=captured_at,
        selector_groups=selector_groups,
        dom_summary=dom_summary,
    )

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    safe_stage = re.sub(r"[^a-zA-Z0-9_-]", "_", str(stage or "preflight"))
    timestamp = int(time.time())

    if save_screenshot:
        screenshot_path = output_path / f"{safe_stage}_{timestamp}.png"
        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
            report.screenshot_path = str(screenshot_path)
        except Exception as exc:
            report.warnings.append(f"스크린샷 저장 실패: {exc}")
            report.status = _resolve_status(report.failures, report.warnings)

    report_path = output_path / f"{safe_stage}_{timestamp}.json"
    report.report_path = str(report_path)
    report_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    latest_path = output_path / "last_report.json"
    latest_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def evaluate_editor_diagnostics(
    *,
    current_url: str,
    page_title: str = "",
    stage: str = "preflight",
    captured_at: str = "",
    selector_groups: Sequence[SelectorGroupDiagnostic],
    dom_summary: Dict[str, Any] | None = None,
) -> EditorDiagnosticReport:
    """수집된 DOM/셀렉터 결과를 건강 상태로 평가한다."""

    summary = dict(dom_summary or {})
    failures: list[str] = []
    warnings: list[str] = []
    recommendations: list[str] = []

    if "nidlogin" in str(current_url).lower():
        failures.append("네이버 로그인 페이지로 이동했습니다. 저장된 세션이 만료되었을 가능성이 큽니다.")
        recommendations.append("scripts/naver_login.py로 로그인 세션을 다시 생성하세요.")

    if bool(summary.get("captcha_visible")):
        failures.append("캡차 화면이 감지되었습니다.")
        recommendations.append("브라우저를 headful 모드로 열어 수동 확인이 필요합니다.")

    if bool(summary.get("draft_recovery_prompt_visible")):
        warnings.append("작성 중인 글 복구 팝업이 화면에 남아 있습니다.")
        recommendations.append("임시저장 복구 팝업 취소 루틴이 현재 화면에서 동작하는지 확인하세요.")

    group_by_name = {item.name: item for item in selector_groups}
    for group in selector_groups:
        if group.required and not group.ok:
            failures.append(f"필수 입력 영역을 찾지 못했습니다: {group.name}")
            recommendations.append(f"{group.name} 셀렉터 후보를 최신 DOM 기준으로 갱신하세요.")

    publish_group = group_by_name.get("publish_button")
    if publish_group and publish_group.visible_count == 0:
        warnings.append("발행 버튼 후보가 현재 화면에서 보이지 않습니다.")
        recommendations.append("상단 툴바 또는 발행 버튼 셀렉터 변경 여부를 점검하세요.")

    save_group = group_by_name.get("draft_save_button")
    if save_group and save_group.visible_count == 0:
        warnings.append("임시저장 버튼 후보가 현재 화면에서 보이지 않습니다.")
        recommendations.append("네이버 임시저장 버튼 셀렉터 변경 여부를 점검하세요.")

    image_group = group_by_name.get("image_controls")
    if image_group and image_group.visible_count == 0:
        warnings.append("이미지 업로드 후보가 현재 화면에서 보이지 않습니다.")
        recommendations.append("이미지 버튼/file input 진단 결과를 기준으로 업로드 전략을 보정하세요.")

    ai_group = group_by_name.get("ai_toggle_controls")
    if ai_group and ai_group.visible_count == 0:
        warnings.append("AI 활용 표시 토글 후보가 현재 화면에서 보이지 않습니다.")
        recommendations.append("AI 이미지 사용 시 토글 위치가 발행 설정 팝업 내부로 이동했는지 확인하세요.")

    status = _resolve_status(failures, warnings)
    return EditorDiagnosticReport(
        status=status,
        stage=stage,
        current_url=current_url,
        page_title=page_title,
        captured_at=captured_at,
        selector_groups=list(selector_groups),
        dom_summary=summary,
        failures=failures,
        warnings=warnings,
        recommendations=_dedupe(recommendations),
    )


async def run_naver_editor_diagnostics(
    *,
    blog_id: str,
    session_state_path: str | Path = "data/sessions/naver/state.json",
    output_dir: str | Path = "data/editor_diagnostics",
    headless: bool = True,
    browser_channel: str = "",
    timeout_ms: int = 60_000,
) -> EditorDiagnosticReport:
    """새 브라우저를 열어 네이버 글쓰기 화면 진단을 수행한다."""

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("playwright 설치 필요: pip3 install playwright && python3 -m playwright install chromium") from exc

    state_path = Path(session_state_path)
    if not state_path.exists():
        raise RuntimeError(f"세션 파일이 없습니다: {state_path}")

    write_url = BLOG_WRITE_URL.format(blog_id=blog_id)
    async with async_playwright() as playwright:
        launch_options: Dict[str, Any] = {
            "headless": bool(headless),
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        if browser_channel:
            launch_options["channel"] = browser_channel

        try:
            browser = await playwright.chromium.launch(**launch_options)
        except Exception:
            if not browser_channel:
                raise
            launch_options.pop("channel", None)
            browser = await playwright.chromium.launch(**launch_options)

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            storage_state=str(state_path),
        )
        page = await context.new_page()
        try:
            await page.goto(write_url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass
            await page.wait_for_timeout(3_000)
            return await diagnose_editor_page(
                page,
                stage="standalone",
                output_dir=output_dir,
                save_screenshot=True,
            )
        finally:
            await context.close()
            await browser.close()


async def _check_selector_group(
    page: Any,
    name: str,
    selectors: Sequence[str],
    *,
    required: bool,
) -> SelectorGroupDiagnostic:
    matched: list[str] = []
    samples: list[Dict[str, str]] = []
    errors: list[str] = []
    total_count = 0
    visible_count = 0

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            total_count += int(count or 0)
            local_visible = 0
            for index in range(min(int(count or 0), 5)):
                item = locator.nth(index)
                try:
                    is_visible = await item.is_visible()
                except Exception:
                    is_visible = False
                if not is_visible:
                    continue
                local_visible += 1
                if len(samples) < 5:
                    samples.append(
                        {
                            "selector": selector,
                            "text": await _safe_locator_text(item),
                            "class": await _safe_locator_attr(item, "class"),
                            "aria_label": await _safe_locator_attr(item, "aria-label"),
                        }
                    )
            if local_visible > 0:
                matched.append(selector)
                visible_count += local_visible
        except Exception as exc:
            errors.append(f"{selector}: {exc}")

    return SelectorGroupDiagnostic(
        name=name,
        required=required,
        selectors=tuple(selectors),
        matched_selectors=tuple(matched),
        total_count=total_count,
        visible_count=visible_count,
        samples=tuple(samples),
        errors=tuple(errors),
    )


async def _extract_dom_summary(page: Any) -> Dict[str, Any]:
    script = """
    () => {
      const isVisible = (node) => {
        if (!node) return false;
        const style = window.getComputedStyle(node);
        if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      };
      const visibleCount = (selector) => {
        try {
          return Array.from(document.querySelectorAll(selector)).filter(isVisible).length;
        } catch (_) {
          return 0;
        }
      };
      const allText = String(document.body && document.body.textContent || "");
      const overlaySelectors = [
        "[class*='dimmed']",
        "[class*='layer_popup']",
        "[role='dialog']",
        "[class*='modal']",
        "[class*='popup']"
      ];
      const overlays = overlaySelectors.reduce((sum, selector) => sum + visibleCount(selector), 0);
      return {
        body_text_sample: allText.replace(/\\s+/g, " ").trim().slice(0, 500),
        file_input_count: document.querySelectorAll("input[type='file']").length,
        visible_toolbar_count: visibleCount("[class*='se-toolbar']"),
        visible_overlay_count: overlays,
        draft_recovery_prompt_visible: /작성\\s*중인\\s*글\\s*이\\s*있습니다|이어서\\s*작성하시겠습니까/.test(allText),
        captcha_visible: Boolean(
          document.querySelector("#captcha,.captcha,[class*='captcha'],iframe[src*='captcha'],iframe[src*='recaptcha']")
        ),
        ai_text_visible: /AI\\s*활용|AI\\s*생성|인공지능|출처/.test(allText),
        image_text_visible: /사진|이미지|내 PC|직접 업로드/.test(allText)
      };
    }
    """
    try:
        payload = await page.evaluate(script)
        return dict(payload or {})
    except Exception as exc:
        return {"error": str(exc)}


async def _safe_page_url(page: Any) -> str:
    try:
        return str(page.url or "")
    except Exception:
        return ""


async def _safe_page_title(page: Any) -> str:
    try:
        return str(await page.title())
    except Exception:
        return ""


async def _safe_locator_text(locator: Any) -> str:
    try:
        text = await locator.inner_text(timeout=500)
        return " ".join(str(text or "").split())[:160]
    except Exception:
        return ""


async def _safe_locator_attr(locator: Any, attr: str) -> str:
    try:
        return str(await locator.get_attribute(attr, timeout=500) or "")[:200]
    except Exception:
        return ""


def _resolve_status(failures: Sequence[str], warnings: Sequence[str]) -> str:
    if failures:
        return "unhealthy"
    if warnings:
        return "degraded"
    return "healthy"


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
