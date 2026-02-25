"""
Playwright 기반 네이버 블로그 발행기

P1 #7 해결:
- PLAYWRIGHT_HEADLESS 환경변수로 운영 모드 제어
- context → browser → playwright 순서로 리소스 정리
- DRY_RUN 모드 지원

에러 코드 8종:
- AUTH_EXPIRED: 세션 만료 (재시도 불가)
- CAPTCHA_REQUIRED: 캡차 감지 (재시도 불가)
- ELEMENT_NOT_FOUND: DOM 변경 (재시도 가능)
- NETWORK_TIMEOUT: 타임아웃 (재시도 가능)
- RATE_LIMITED: 요청 제한 (재시도 가능)
- CONTENT_REJECTED: 콘텐츠 거부 (재시도 불가)
- PUBLISH_FAILED: 발행 실패 (재시도 가능)
- UNKNOWN: 미분류 (재시도 가능)
"""

import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from ..exceptions import PublishError, SessionExpiredError
from .base_publisher import PublishResult

if TYPE_CHECKING:
    from ..images.placement import ImageInsertionPoint

logger = logging.getLogger(__name__)

from .publisher_constants import (
    BLOG_WRITE_URL,
    RETRYABLE_ERRORS,
    AI_IMAGE_PREFIXES,
    THUMBNAIL_PLACEMENT_MODES,
    AI_TOGGLE_MODES,
    TITLE_SELECTORS,
    BODY_SELECTORS,
    DRAFT_CANCEL_SELECTORS,
    PUBLISH_BTN_1_SELECTORS,
    PUBLISH_BTN_2_SELECTORS,
)


async def _apply_stealth(page) -> None:
    """playwright-stealth v1 / v2 모두 호환되는 stealth 적용 함수."""
    try:
        # v1: from playwright_stealth import stealth_async
        from playwright_stealth import stealth_async  # type: ignore
        await stealth_async(page)
        return
    except ImportError:
        pass

    try:
        # v2: from playwright_stealth import Stealth
        from playwright_stealth import Stealth  # type: ignore
        await Stealth().apply_stealth_async(page)
        return
    except (ImportError, AttributeError):
        pass

    try:
        # v2 일부 빌드: stealth 메서드 직접 호출
        from playwright_stealth import Stealth  # type: ignore
        s = Stealth()
        if hasattr(s, "stealth_async"):
            await s.stealth_async(page)
        else:
            logger.warning("playwright-stealth 적용 실패: 버전 API를 인식할 수 없음. stealth 없이 진행.")
    except Exception as e:
        logger.warning(f"playwright-stealth 적용 실패 (무시하고 진행): {e}")


class PlaywrightPublisher:
    """
    네이버 블로그 Playwright 발행기.

    환경변수:
        PLAYWRIGHT_HEADLESS: "true"(기본) / "false"
        DRY_RUN: "true" / "false"(기본)
    """

    BLOG_WRITE_URL = BLOG_WRITE_URL
    RETRYABLE_ERRORS = RETRYABLE_ERRORS
    AI_IMAGE_PREFIXES = AI_IMAGE_PREFIXES
    THUMBNAIL_PLACEMENT_MODES = THUMBNAIL_PLACEMENT_MODES
    AI_TOGGLE_MODES = AI_TOGGLE_MODES

    def __init__(self, blog_id: str, session_dir: str = "data/sessions/naver"):
        self.blog_id = blog_id
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self._headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() == "true"
        self._dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
        raw_mode = str(os.getenv("THUMBNAIL_PLACEMENT_MODE", "body_top")).strip().lower()
        self._thumbnail_placement_mode = raw_mode if raw_mode in self.THUMBNAIL_PLACEMENT_MODES else "body_top"
        raw_force_ai_toggle = str(os.getenv("NAVER_AI_TOGGLE_FORCE", "false")).strip().lower()
        raw_ai_toggle_mode = str(os.getenv("NAVER_AI_TOGGLE_MODE", "")).strip().lower()
        if raw_ai_toggle_mode not in self.AI_TOGGLE_MODES:
            raw_ai_toggle_mode = "force" if raw_force_ai_toggle in {"1", "true", "yes", "on"} else "metadata"
        self._ai_toggle_mode = raw_ai_toggle_mode
        self._force_ai_toggle = self._ai_toggle_mode == "force"
        raw_post_verify = str(os.getenv("NAVER_AI_TOGGLE_POST_VERIFY", "true")).strip().lower()
        self._ai_toggle_post_verify = raw_post_verify in {"1", "true", "yes", "on"}
        raw_telegram_alert = str(os.getenv("NAVER_AI_TOGGLE_TELEGRAM_ALERT", "true")).strip().lower()
        self._ai_toggle_telegram_alert = raw_telegram_alert in {"1", "true", "yes", "on"}
        raw_streak = str(os.getenv("NAVER_AI_TOGGLE_ALERT_STREAK", "3")).strip()
        try:
            self._ai_toggle_alert_streak = max(1, min(20, int(raw_streak)))
        except Exception:
            self._ai_toggle_alert_streak = 3
        self._screenshot_retention_max = self._read_retention_limit(
            "NAVER_SCREENSHOT_RETENTION_MAX",
            default=30,
        )
        self._ai_report_retention_max = self._read_retention_limit(
            "NAVER_AI_REPORT_RETENTION_MAX",
            default=30,
        )
        raw_width = str(os.getenv("IMAGE_UPLOAD_WIDTH", "800")).strip()
        try:
            self._image_upload_target_width = max(320, min(1200, int(raw_width)))
        except Exception:
            self._image_upload_target_width = 800

        # 리소스 (명시적 정리)
        self._playwright = None
        self._browser = None
        self._context = None
        self._image_source_meta_lookup: Dict[str, Dict[str, str]] = {}
        self._ai_toggle_audit_rows: List[Dict[str, Any]] = []
        self._ai_toggle_summary: Dict[str, Any] = {}
        self._telegram_notifier = None
        self._overlay_dismiss_in_progress = False

    @staticmethod
    def _is_draft_recovery_prompt_text(text: str) -> bool:
        """임시저장 글 복구 팝업 문구인지 판별한다."""
        normalized = " ".join(str(text or "").split())
        if not normalized:
            return False
        return (
            "작성 중인 글이 있습니다" in normalized
            or "작성중인 글이 있습니다" in normalized
            or "이어서 작성하시겠습니까" in normalized
        )

    @staticmethod
    def _is_reserved_publish_popup_text(text: str) -> bool:
        """예약 발행 글 레이어 제목/본문 문구인지 판별한다."""
        normalized = " ".join(str(text or "").split())
        if not normalized:
            return False
        return (
            "예약 발행 글" in normalized
            or "예약발행글" in normalized
            or "예약 발행" in normalized
        )

    def _set_image_source_lookup(self, image_sources: Optional[Dict[str, Dict[str, str]]]) -> None:
        """발행 payload의 이미지 소스 메타를 조회용 맵으로 정규화한다."""
        self._image_source_meta_lookup = {}
        if not image_sources:
            return

        for raw_path, raw_meta in image_sources.items():
            path_text = str(raw_path or "").strip()
            if not path_text:
                continue
            if isinstance(raw_meta, dict):
                kind = str(raw_meta.get("kind", "unknown")).strip().lower() or "unknown"
                provider = str(raw_meta.get("provider", "unknown")).strip().lower() or "unknown"
            else:
                kind = "unknown"
                provider = "unknown"

            meta = {"kind": kind, "provider": provider}
            for variant in self._build_path_variants(path_text):
                self._image_source_meta_lookup[variant] = meta

    def _build_path_variants(self, path_text: str) -> List[str]:
        """경로 매칭 정확도를 높이기 위한 변형 문자열을 만든다."""
        path = Path(str(path_text))
        variants = {
            str(path_text).strip().lower(),
            str(path).strip().lower(),
            path.name.strip().lower(),
        }
        try:
            variants.add(str(path.resolve()).strip().lower())
        except Exception:
            pass
        return [variant for variant in variants if variant]

    def _get_image_source_meta(self, image_path: str) -> Dict[str, str]:
        """이미지 경로로 소스 메타데이터를 조회한다."""
        for variant in self._build_path_variants(image_path):
            if variant in self._image_source_meta_lookup:
                return self._image_source_meta_lookup[variant]
        return {"kind": "unknown", "provider": "unknown"}

    async def publish(
        self,
        title: str,
        content: str,
        thumbnail: Optional[str] = None,
        images: Optional[List[str]] = None,
        image_sources: Optional[Dict[str, Dict[str, str]]] = None,
        image_points: Optional[List["ImageInsertionPoint"]] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> PublishResult:
        """
        블로그 포스트 발행.

        Args:
            title: 포스트 제목
            content: 본문 내용 (네이버 에디터용 변환된 텍스트, 마커 포함 가능)
            thumbnail: 썸네일 이미지 경로
            images: 본문 이미지 경로 리스트 (image_points 없을 때 폴백)
            image_sources: 이미지 경로별 소스 메타데이터(kind/provider)
            image_points: 이미지 삽입 위치 정보 (마커 기반 배치용)
            tags: 발행 태그 목록 (네이버 태그 입력 필드에 주입)
            category: 발행 카테고리

        Returns:
            PublishResult
        """
        publish_start = time.perf_counter()
        page = None
        try:
            self._ai_toggle_audit_rows = []
            self._ai_toggle_summary = {}
            logger.info(
                "AI 활용 설정 판정 모드: %s",
                self._ai_toggle_mode,
            )
            # 드라이런 모드
            if self._dry_run:
                logger.info(
                    "[DRY RUN] Publishing",
                    extra={"title": title, "tags": tags, "category": category},
                )
                return PublishResult(
                    success=True,
                    url="https://blog.naver.com/dry-run/000000000000",
                )

            self._set_image_source_lookup(image_sources)
            page = await self._init_browser()
            return await self._do_publish(
                page,
                title,
                content,
                thumbnail,
                images,
                image_sources,
                image_points,
                tags,
                category,
            )

        except PublishError as exc:
            logger.warning(
                "Publish error",
                extra={
                    "error_code": exc.error_code,
                    "retryable": exc.retryable,
                    "context": exc.context,
                },
            )
            if page:
                await self._save_screenshot(page, f"error_{exc.error_code}")
            return PublishResult(
                success=False,
                error_code=exc.error_code,
                error_message=str(exc),
            )
        except Exception as e:
            error_code = self._classify_error(e)
            logger.exception(f"Publish failed: {error_code}")

            if page:
                await self._save_screenshot(page, f"error_{error_code}")

            return PublishResult(
                success=False,
                error_code=error_code,
                error_message=str(e)[:500],
            )
        finally:
            self._image_source_meta_lookup = {}
            await self._cleanup()
            logger.info(
                "Publish finished",
                extra={"duration_ms": round((time.perf_counter() - publish_start) * 1000, 2)},
            )

    async def _init_browser(self):
        """Stealth 모드 브라우저 초기화 (playwright-stealth v1/v2 호환)"""
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError(
                "playwright 미설치. "
                "pip3 install playwright playwright-stealth && python3 -m playwright install chromium"
            ) from e

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        session_state = self._session_state_path()
        self._context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=self._random_ua(),
            storage_state=str(session_state) if session_state.exists() else None,
        )

        page = await self._context.new_page()
        await _apply_stealth(page)
        return page

    async def _do_publish(
        self,
        page,
        title: str,
        content: str,
        thumbnail: Optional[str],
        images: Optional[List[str]],
        image_sources: Optional[Dict[str, Dict[str, str]]] = None,
        image_points: Optional[List["ImageInsertionPoint"]] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> PublishResult:
        """실제 발행 흐름 (네이버 스마트 에디터 ONE 기준)"""
        del image_sources
        write_url = BLOG_WRITE_URL.format(blog_id=self.blog_id)

        await page.goto(write_url, wait_until="networkidle", timeout=45_000)
        await self._human_delay(3000, 5000)

        # 로그인 체크
        if "nidlogin" in page.url:
            raise SessionExpiredError(
                "세션 만료. 수동 로그인 후 session state 갱신 필요.",
                context={"current_url": page.url},
            )

        # 캡차 체크
        if await self._detect_captcha(page):
            await self._save_screenshot(page, "captcha_detected")
            raise PublishError(
                "캡차 감지. 수동 개입 필요.",
                "CAPTCHA_REQUIRED",
                retryable=False,
                context={"current_url": page.url},
            )

        # ── 기존 임시저장 글 복구 팝업 처리 (항상 취소) ───────────────
        await self._dismiss_existing_draft_popup(page, wait_sec=12.0)

        # ── 도움말 패널 닫기 (처음 접속 시 자동으로 열림) ────────────
        try:
            close_btn = page.locator(".se-help-panel-close-button, [class*='help'] [class*='close']")
            if await close_btn.count() > 0:
                await close_btn.first.click()
                await self._human_delay(500, 1000)
                logger.info("도움말 패널 닫음")
        except Exception:
            pass  # 패널 없으면 무시

        # ── 에디터 준비 상태 확인 (지연/오버레이 복구) ───────────────
        await self._ensure_editor_ready(page)

        # ── 스마트 에디터 ONE: iframe 없이 직접 접근 ─────────────────
        # 제목 영역 클릭 및 입력
        # 셀렉터 우선순위: se-title-text > se-section-documentTitle 내 paragraph
        title_selectors = TITLE_SELECTORS
        title_clicked = False
        for sel in title_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click(timeout=8_000)
                    title_clicked = True
                    logger.info(f"제목 셀렉터 사용: {sel}")
                    break
            except Exception:
                continue

        if not title_clicked:
            await self._save_screenshot(page, "title_not_found")
            raise PublishError(
                "제목 입력 영역을 찾을 수 없습니다.",
                "ELEMENT_NOT_FOUND",
                retryable=True,
                context={"selectors": title_selectors, "current_url": page.url},
            )

        from .editor_helper import NaverEditorHelper
        editor = NaverEditorHelper(page)

        await self._human_delay(300, 600)
        await editor.type_naturally(title)
        await self._human_delay(500, 1000)

        # ── 썸네일 업로드 (정책 기반) ─────────────────────────────────
        # 기본 정책은 body_top이며, cover 모드일 때만 제목 배경 업로드를 시도한다.
        if (
            self._thumbnail_placement_mode == "cover"
            and thumbnail
            and Path(thumbnail).exists()
        ):
            await self._upload_thumbnail(page, thumbnail)

        # ── 본문 내용 클릭 및 포커스 ─────────────────────────────────
        # 제목 배경 썸네일 업로드 후 명시적으로 본문을 다시 클릭하여 포커스를 본문으로 가져온다
        body_selectors = BODY_SELECTORS
        body_clicked = False
        for sel in body_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click(timeout=8_000)
                    body_clicked = True
                    logger.info(f"본문 셀렉터 사용 (포커스 이동): {sel}")
                    break
            except Exception:
                continue

        if not body_clicked:
            logger.warning("본문 셀렉터 미발견 — Tab 키로 이동 시도 (비권장)")
            await page.keyboard.press("Tab")
            await self._human_delay(300, 500)

        # ── 본문 내용 및 이미지 교차(Interleave) 입력 ──────────────────────────────
        async def do_upload(path: str):
            await self._upload_image(page, path)

        async def do_sep(stage: str):
            await self._insert_non_gallery_separator(page, stage=stage)

        await editor.insert_content_with_markers(
            content=content,
            images=images,
            thumbnail=thumbnail,
            image_points=image_points,
            thumbnail_placement_mode=self._thumbnail_placement_mode,
            upload_image_callback=do_upload,
            insert_separator_callback=do_sep,
        )

        # ── AI 활용 설정 사전 점검 (필요 시 1회 자가복구) ──────────────
        await self._run_ai_toggle_prepublish_validation(page)

        # ── 발행 버튼 클릭 (1단계: 설정 팝업 열기) ───────────────────
        # 상단 툴바 내에 있는 '발행' 텍스트 버튼 찾기
        await self._dismiss_blocking_layer_popup(page)
        publish_btn_selector_1 = PUBLISH_BTN_1_SELECTORS
        
        publish_btn_1 = None
        for sel in publish_btn_selector_1:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    publish_btn_1 = btn
                    break
            except Exception:
                continue

        if publish_btn_1:
            await publish_btn_1.click()
            logger.info(f"1차 발행 버튼 클릭 (설정 팝업 오픈) : {sel}")
            await self._human_delay(1500, 2500)
        else:
            await self._save_screenshot(page, "publish_btn_1_not_found")
            raise PublishError(
                "1차 발행 버튼을 찾을 수 없습니다.",
                "ELEMENT_NOT_FOUND",
                retryable=True,
                context={"selectors": publish_btn_selector_1, "current_url": page.url},
            )

        # ── 발행 설정 팝업: 태그 / 카테고리 입력 ─────────────────────
        await self._fill_publish_settings(page, tags=tags, category=category)

        # ── 발행 버튼 클릭 (2단계: 최종 확인) ───────────────────────
        # 설정 팝업(.layer_result) 내의 '발행' 버튼 찾기
        await self._dismiss_blocking_layer_popup(page, preserve_reserved_publish=True)
        publish_btn_selector_2 = PUBLISH_BTN_2_SELECTORS

        publish_btn_2 = None
        for sel in publish_btn_selector_2:
            try:
                # 팝업이 렌더링될 때까지 기다림
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    # waitForVisible
                    await btn.wait_for(state="visible", timeout=5000)
                    publish_btn_2 = btn
                    break
            except Exception:
                continue

        if publish_btn_2:
            try:
                await publish_btn_2.click()
                logger.info(f"2차 발행 버튼 클릭 (최종 발행) : {sel}")
            except Exception as click_err:
                # 클릭 후 페이지 전환 중 timeout 발생 가능 - 무시하고 URL 추출 시도
                logger.warning(f"2차 발행 버튼 클릭 중 예외 (무시): {click_err}")
            await self._human_delay(3000, 5000)
        else:
            await self._save_screenshot(page, "publish_btn_2_not_found")
            raise PublishError(
                "2차 발행 버튼(팝업 내)을 찾을 수 없습니다.",
                "ELEMENT_NOT_FOUND",
                retryable=True,
                context={"selectors": publish_btn_selector_2, "current_url": page.url},
            )

        # ── URL 추출 ──────────────────────────────────────────────────
        post_url = await self._extract_post_url(page)
        if not post_url:
            raise PublishError(
                "발행 후 URL 추출 실패",
                "PUBLISH_FAILED",
                retryable=True,
                context={"current_url": page.url},
            )

        # 세션 저장
        if self._context is not None:
            await self._context.storage_state(path=str(self._session_state_path()))
        logger.info(f"Published: {post_url}")
        await self._run_ai_toggle_postpublish_verification(post_url)
        self._persist_ai_toggle_report(post_url)

        return PublishResult(success=True, url=post_url)

    async def _dismiss_existing_draft_popup(self, page, wait_sec: float = 8.0) -> None:
        """작성 중인 글 복구 팝업이 뜨면 '취소'를 눌러 새 글 작성으로 진입한다."""
        deadline = time.perf_counter() + max(1.0, float(wait_sec))
        while time.perf_counter() < deadline:
            has_prompt = await self._has_draft_recovery_prompt(page)
            if not has_prompt:
                if await self._has_visible_title_input(page):
                    return
                await asyncio.sleep(0.35)
                continue

            clicked = False
            cancel_selectors = DRAFT_CANCEL_SELECTORS
            for selector in cancel_selectors:
                try:
                    buttons = page.locator(selector)
                    count = await buttons.count()
                    if count == 0:
                        continue
                    for index in range(min(count, 10)):
                        button = buttons.nth(index)
                        if not await button.is_visible():
                            continue
                        await self._activate_toggle_target(page, button, aggressive=True)
                        await asyncio.sleep(0.25)
                        if not await self._has_draft_recovery_prompt(page):
                            logger.info("임시저장 복구 팝업 감지: 취소 버튼 클릭 성공")
                            return
                        clicked = True
                except Exception:
                    continue

            if not clicked:
                try:
                    payload = await page.evaluate(
                        """
                        () => {
                          const isVisible = (node) => {
                            if (!node) return false;
                            const style = window.getComputedStyle(node);
                            if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
                            const rect = node.getBoundingClientRect();
                            return rect.width > 0 && rect.height > 0;
                          };
                          const dispatchClick = (node) => {
                            if (!node) return;
                            const events = ["pointerdown", "mousedown", "mouseup", "click"];
                            for (const type of events) {
                              node.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, composed: true }));
                            }
                          };
                          const allNodes = Array.from(document.querySelectorAll("div,span,p,button,[role='button']"));
                          const promptNode = allNodes.find((node) => {
                            if (!isVisible(node)) return false;
                            const text = String(node.textContent || "").trim();
                            return /작성\\s*중인\\s*글\\s*이\\s*있습니다|이어서\\s*작성하시겠습니까/.test(text);
                          });
                          if (!promptNode) return { clicked: false, fallbackX: null, fallbackY: null };

                          const scope = promptNode.closest("[role='dialog'], [class*='dialog'], [class*='modal'], [class*='popup'], [class*='layer'], body") || document.body;
                          const candidates = Array.from(
                            scope.querySelectorAll("button,[role='button'],a,span,div")
                          ).filter((node) => /취소/.test(String(node.textContent || "").trim()) && isVisible(node));
                          const rect = promptNode.getBoundingClientRect();
                          if (candidates.length > 0) {
                            dispatchClick(candidates[0]);
                            return { clicked: true, fallbackX: rect.left + rect.width * 0.4, fallbackY: rect.top + rect.height * 1.8 };
                          }
                          return { clicked: false, fallbackX: rect.left + rect.width * 0.4, fallbackY: rect.top + rect.height * 1.8 };

                          return { clicked: false, fallbackX: null, fallbackY: null };
                        }
                        """
                    )
                    clicked = bool(payload and payload.get("clicked"))
                    if clicked:
                        logger.info("임시저장 복구 팝업 감지: JS 취소 클릭")
                    if not clicked and payload and payload.get("fallbackX") is not None and payload.get("fallbackY") is not None:
                        try:
                            await page.mouse.click(float(payload["fallbackX"]), float(payload["fallbackY"]))
                            await asyncio.sleep(0.25)
                            if not await self._has_draft_recovery_prompt(page):
                                logger.info("임시저장 복구 팝업 감지: 좌표 취소 클릭 성공")
                                return
                            clicked = True
                        except Exception:
                            pass
                except Exception:
                    clicked = False

            if not clicked:
                try:
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.35)
                except Exception:
                    pass
                await asyncio.sleep(0.35)
                continue

            await asyncio.sleep(0.8)
            if not await self._has_draft_recovery_prompt(page):
                return

        if await self._has_draft_recovery_prompt(page):
            logger.warning("임시저장 복구 팝업 감지했지만 취소 버튼 클릭 실패")
            await self._save_screenshot(page, "draft_recovery_popup_cancel_failed")

    async def _has_draft_recovery_prompt(self, page) -> bool:
        """현재 화면에 임시저장 복구 팝업이 남아있는지 확인한다."""
        phrases = [
            "작성 중인 글이 있습니다",
            "작성중인 글이 있습니다",
            "이어서 작성하시겠습니까",
        ]
        for phrase in phrases:
            try:
                nodes = page.locator(f"text={phrase}")
                count = await nodes.count()
                if count == 0:
                    continue
                for index in range(min(count, 5)):
                    node = nodes.nth(index)
                    if await node.is_visible():
                        return True
            except Exception:
                continue

        try:
            payload = await page.evaluate(
                """
                () => {
                  const isVisible = (node) => {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
                    const rect = node.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };
                  const roots = Array.from(
                    document.querySelectorAll(
                      "[role='dialog'], [class*='dialog'], [class*='modal'], [class*='popup'], [class*='layer']"
                    )
                  );
                  for (const node of roots) {
                    if (!isVisible(node)) continue;
                    const text = String(node.textContent || "").trim();
                    if (/작성\\s*중인\\s*글\\s*이\\s*있습니다|이어서\\s*작성하시겠습니까/.test(text)) {
                      return true;
                    }
                  }
                  return false;
                }
                """
            )
            return bool(payload)
        except Exception:
            return False

    async def _has_visible_title_input(self, page) -> bool:
        """제목 입력 영역이 화면에 노출되었는지 확인한다."""
        if await self._has_draft_recovery_prompt(page):
            return False

        selectors = [
            ".se-section-documentTitle .se-text-paragraph",
            ".se-title-text",
            "[data-component='documentTitle'] [contenteditable]",
            ".se-section-title .se-text-paragraph",
        ]
        for selector in selectors:
            try:
                node = page.locator(selector).first
                if await node.count() > 0 and await node.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _ensure_editor_ready(self, page) -> None:
        """제목/본문 입력 가능한 에디터 상태를 보장한다."""
        title_selectors = [
            ".se-section-documentTitle .se-text-paragraph",
            ".se-title-text",
            "[data-component='documentTitle'] [contenteditable]",
            ".se-section-title .se-text-paragraph",
        ]
        for attempt in range(2):
            for selector in title_selectors:
                try:
                    await page.wait_for_selector(selector, state="visible", timeout=6_000)
                    return
                except Exception:
                    continue

            # 팝업/오버레이가 남아 있을 수 있어 복구 루틴을 재시도한다.
            await self._dismiss_existing_draft_popup(page, wait_sec=4.0)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await asyncio.sleep(0.8)

            if attempt == 0:
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=45_000)
                    await self._human_delay(1200, 2000)
                    await self._dismiss_existing_draft_popup(page, wait_sec=6.0)
                except Exception as exc:
                    logger.warning("에디터 준비 재시도 중 reload 실패: %s", exc)

        logger.warning("에디터 준비 상태 확인 실패(후속 셀렉터 단계로 계속 진행)")

    async def _upload_thumbnail(self, page, path: str):
        """썸네일 업로드 (실패해도 발행은 계속 진행)."""
        if not Path(path).exists():
            logger.warning("Thumbnail file not found: %s", path)
            return
        upload_path = self._prepare_image_for_upload(path)

        cover_button_selectors = [
            ".se-cover-button-local-image-upload",
            ".se-cover-attach-button-container .se-cover-button-local-image-upload",
            ".se-cover-button-wrap .se-cover-button-local-image-upload",
            ".se-cover-button-sns-image-upload",
            "[class*='se-cover-image'] button",
            "button[aria-label*='커버']",
            "button[aria-label*='cover']",
            "[class*='coverimage'] button",
        ]
        submenu_selectors = [
            "button:has-text('내 PC')",
            "button:has-text('직접 업로드')",
            "[role='menuitem']:has-text('내 PC')",
            "[role='menuitem']:has-text('직접 업로드')",
            "li:has-text('내 PC')",
            "li:has-text('직접 업로드')",
        ]

        try:
            title_area = page.locator(".se-section-documentTitle .se-text-paragraph").first
            if await title_area.count() > 0:
                try:
                    await title_area.hover()
                    await title_area.click()
                    await self._human_delay(300, 700)
                except Exception:
                    pass
            try:
                await page.wait_for_selector(
                    ".se-cover-button-local-image-upload, [class*='se-cover-image'] button, [class*='coverimage'] button",
                    timeout=3000,
                )
            except Exception:
                pass

            # 전략 1: 커버 버튼 -> FileChooser
            if await self._try_file_chooser_via_selectors(page, upload_path, cover_button_selectors, "thumbnail-A"):
                await self._after_upload_wait()
                await self._apply_ai_usage_compliance(
                    page=page,
                    image_path=path,
                    location="thumbnail",
                    uploaded_path=upload_path,
                )
                logger.info("썸네일 업로드 완료(전략 A): %s", path)
                return
            logger.warning("썸네일 전략 A 실패, 전략 B 진행")

            # 전략 2: 숨겨진 input 직접 set_files
            if await self._try_direct_file_input(page, upload_path, "thumbnail-B"):
                await self._after_upload_wait()
                await self._apply_ai_usage_compliance(
                    page=page,
                    image_path=path,
                    location="thumbnail",
                    uploaded_path=upload_path,
                )
                logger.info("썸네일 업로드 완료(전략 B): %s", path)
                return
            logger.warning("썸네일 전략 B 실패, 전략 C 진행")

            # 전략 3: 커버 버튼 클릭 후 서브메뉴에서 업로드
            if await self._try_submenu_upload(page, upload_path, cover_button_selectors, submenu_selectors, "thumbnail-C"):
                await self._after_upload_wait()
                await self._apply_ai_usage_compliance(
                    page=page,
                    image_path=path,
                    location="thumbnail",
                    uploaded_path=upload_path,
                )
                logger.info("썸네일 업로드 완료(전략 C): %s", path)
                return

            logger.warning("썸네일 업로드 실패(선택 사항이므로 계속 진행): %s", path)
        except Exception as exc:
            logger.warning("Thumbnail upload failed: %s (%s)", path, exc)

    async def _upload_image(self, page, path: str):
        """본문 이미지 업로드 (실패해도 발행은 계속 진행)."""
        if not Path(path).exists():
            logger.warning("Image file not found: %s", path)
            return
        upload_path = self._prepare_image_for_upload(path)

        image_button_selectors = [
            "button[aria-label='사진']",
            "button[aria-label='이미지']",
            "[class*='se-toolbar'] button:has-text('사진')",
            "[data-name='image']",
            ".se-image-toolbar-button",
        ]
        submenu_selectors = [
            "button:has-text('내 PC')",
            "button:has-text('직접 업로드')",
            "[role='menuitem']:has-text('내 PC')",
            "[role='menuitem']:has-text('직접 업로드')",
            "li:has-text('내 PC')",
            "li:has-text('직접 업로드')",
        ]

        before_count = await self._count_editor_images(page)
        await self._dismiss_blocking_layer_popup(page)

        try:
            # 전략 A: 숨겨진 input 직접 set_files
            if await self._try_direct_file_input(page, upload_path, "image-A"):
                if await self._wait_for_image_count_increase(page, before_count):
                    await self._after_upload_wait()
                    await self._align_latest_uploaded_image_to_center(page, path)
                    await self._apply_ai_usage_compliance(
                        page=page,
                        image_path=path,
                        location="body",
                        uploaded_path=upload_path,
                    )
                    logger.info("본문 이미지 업로드 완료(전략 A): %s", path)
                    return
                logger.warning("본문 이미지 전략 A 업로드 확인 실패")

            # 전략 B: 툴바 버튼 -> FileChooser
            await self._dismiss_blocking_layer_popup(page)
            if await self._try_file_chooser_via_selectors(page, upload_path, image_button_selectors, "image-B"):
                if await self._wait_for_image_count_increase(page, before_count):
                    await self._after_upload_wait()
                    await self._align_latest_uploaded_image_to_center(page, path)
                    await self._apply_ai_usage_compliance(
                        page=page,
                        image_path=path,
                        location="body",
                        uploaded_path=upload_path,
                    )
                    logger.info("본문 이미지 업로드 완료(전략 B): %s", path)
                    return
                logger.warning("본문 이미지 전략 B 업로드 확인 실패")

            # 전략 C: 서브메뉴(내 PC/직접 업로드) -> FileChooser
            await self._dismiss_blocking_layer_popup(page)
            if await self._try_submenu_upload(page, upload_path, image_button_selectors, submenu_selectors, "image-C"):
                if await self._wait_for_image_count_increase(page, before_count):
                    await self._after_upload_wait()
                    await self._align_latest_uploaded_image_to_center(page, path)
                    await self._apply_ai_usage_compliance(
                        page=page,
                        image_path=path,
                        location="body",
                        uploaded_path=upload_path,
                    )
                    logger.info("본문 이미지 업로드 완료(전략 C): %s", path)
                    return
                logger.warning("본문 이미지 전략 C 업로드 확인 실패")

            # 전략 D: 클립보드 붙여넣기 fallback (best effort)
            if await self._try_clipboard_paste(upload_path, page):
                if await self._wait_for_image_count_increase(page, before_count):
                    await self._after_upload_wait()
                    await self._align_latest_uploaded_image_to_center(page, path)
                    await self._apply_ai_usage_compliance(
                        page=page,
                        image_path=path,
                        location="body",
                        uploaded_path=upload_path,
                    )
                    logger.info("본문 이미지 업로드 완료(전략 D): %s", path)
                    return
                logger.warning("본문 이미지 전략 D 업로드 확인 실패")

            logger.warning("본문 이미지 업로드 실패(계속 진행): %s", path)
        except Exception as exc:
            logger.warning("Image upload failed: %s (%s)", path, exc)

    async def _count_editor_images(self, page) -> int:
        """에디터 본문 내 img[src] 개수를 계산한다."""
        try:
            count = await page.evaluate(
                """
                () => {
                  const selectors = [
                    '.se-main-container img[src]',
                    '.se-component-content img[src]',
                    '.se-content img[src]'
                  ];
                  const nodes = selectors.flatMap((sel) => Array.from(document.querySelectorAll(sel)));
                  const srcSet = new Set(nodes.map((img) => img.getAttribute('src') || '').filter(Boolean));
                  return srcSet.size;
                }
                """
            )
            return int(count)
        except Exception:
            return 0

    async def _wait_for_image_count_increase(self, page, before_count: int, timeout_sec: float = 12.0) -> bool:
        """img[src] 개수 증가를 기다린다."""
        start = time.time()
        while time.time() - start < timeout_sec:
            current_count = await self._count_editor_images(page)
            if current_count > before_count:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _dismiss_blocking_layer_popup(
        self,
        page,
        max_rounds: int = 4,
        preserve_reserved_publish: bool = False,
    ) -> None:
        """업로드/발행 버튼 클릭을 가로채는 dimmed/모달 레이어를 반복 해제한다."""
        if self._overlay_dismiss_in_progress:
            return

        self._overlay_dismiss_in_progress = True
        try:
            rounds = max(1, int(max_rounds))
            for _ in range(rounds):
                try:
                    blocked = await page.evaluate(
                        """
                        () => {
                          const isVisible = (el) => {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
                            const rect = el.getBoundingClientRect();
                            return rect.width > 0 && rect.height > 0;
                          };
                          const selectors = [
                            "[class*='dimmed']",
                            "[class*='layer_popup'][class*='isShow']",
                            ".layer_popup__WjlfW.isShow__Ml4bq",
                            "[role='dialog']",
                            "[class*='modal']",
                            "[class*='popup']",
                          ];
                          for (const selector of selectors) {
                            const nodes = Array.from(document.querySelectorAll(selector));
                            for (const node of nodes) {
                              if (!isVisible(node)) continue;
                              const style = window.getComputedStyle(node);
                              if (style.pointerEvents === "none") continue;
                              return true;
                            }
                          }
                          return false;
                        }
                        """
                    )
                except Exception:
                    blocked = False

                if not blocked:
                    return

                closed_any = False
                if not preserve_reserved_publish:
                    close_selectors = [
                        "[class*='layer_popup'] button:has-text('취소')",
                        "[class*='layer_popup'] button:has-text('닫기')",
                        "[class*='layer_popup'] [role='button']:has-text('취소')",
                        "[class*='layer_popup'] [role='button']:has-text('닫기')",
                        "[class*='layer_popup'] button[aria-label*='닫기']",
                        "[class*='layer_popup'] button[aria-label*='close']",
                        "[class*='layer_popup'] button[class*='close']",
                        "[class*='layer_popup'] [class*='close']",
                        "[class*='popup_container'] [class*='close']",
                        ".layer_popup__WjlfW button:has-text('취소')",
                        ".layer_popup__WjlfW button:has-text('닫기')",
                        ".layer_popup__WjlfW [class*='close']",
                        "[role='dialog'] button:has-text('취소')",
                        "[role='dialog'] button:has-text('닫기')",
                    ]
                    for selector in close_selectors:
                        try:
                            nodes = page.locator(selector)
                            count = await nodes.count()
                            if count == 0:
                                continue
                            for index in range(min(count, 8)):
                                node = nodes.nth(index)
                                if not await node.is_visible():
                                    continue
                                try:
                                    await node.click(timeout=900, force=True)
                                except Exception:
                                    try:
                                        await node.dispatch_event("click")
                                    except Exception:
                                        continue
                                closed_any = True
                                await asyncio.sleep(0.08)
                        except Exception:
                            continue

                # 텍스트 없는 X 버튼(예약 발행 글)까지 포함해 JS로 일괄 닫기
                try:
                    payload = await page.evaluate(
                        """
                        ({ preserveReserved }) => {
                          const isVisible = (el) => {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
                            const rect = el.getBoundingClientRect();
                            return rect.width > 0 && rect.height > 0;
                          };
                          const dispatchClick = (node) => {
                            if (!node) return;
                            const events = ["pointerdown", "mousedown", "mouseup", "click"];
                            for (const type of events) {
                              node.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, composed: true }));
                            }
                          };
                          const isReservedPublishPopup = (text) => /예약\\s*발행\\s*글|예약\\s*발행/i.test(String(text || ""));
                          const isCloseLike = (node) => {
                            if (!node) return false;
                            const text = String(node.textContent || "").trim().toLowerCase();
                            const aria = String(node.getAttribute("aria-label") || "").toLowerCase();
                            const cls = String(node.className || "").toLowerCase();
                            return (
                              text === "x"
                              || text === "×"
                              || text === "닫기"
                              || text === "취소"
                              || aria.includes("닫기")
                              || aria.includes("close")
                              || cls.includes("close")
                            );
                          };

                          let closed = 0;
                          const roots = Array.from(
                            document.querySelectorAll("[class*='layer_popup'], [class*='popup'], [role='dialog'], [class*='modal']")
                          ).filter((node) => isVisible(node));

                          const reservedRoots = roots.filter((node) => isReservedPublishPopup(node.textContent || ""));
                          const keepReservedRoot = preserveReserved && reservedRoots.length > 0
                            ? reservedRoots[reservedRoots.length - 1]
                            : null;

                          roots.sort((a, b) => {
                            const at = isReservedPublishPopup(a.textContent || "") ? 0 : 1;
                            const bt = isReservedPublishPopup(b.textContent || "") ? 0 : 1;
                            return at - bt;
                          });

                          for (const root of roots) {
                            const rootText = String(root.textContent || "");
                            const isReservedRoot = isReservedPublishPopup(rootText);
                            if (preserveReserved && keepReservedRoot && isReservedRoot && root === keepReservedRoot) {
                              continue;
                            }
                            const candidates = Array.from(
                              root.querySelectorAll(
                                "button,[role='button'],a,[class*='close'],[aria-label*='닫기'],[aria-label*='close'],span,div,i"
                              )
                            )
                              .filter((node) => isVisible(node));
                            if (candidates.length === 0) continue;

                            let target = candidates.find((node) => isCloseLike(node)) || null;
                            if (!target && isReservedPublishPopup(rootText)) {
                              const rootRect = root.getBoundingClientRect();
                              const sorted = [...candidates].sort((a, b) => {
                                const ra = a.getBoundingClientRect();
                                const rb = b.getBoundingClientRect();
                                const sa = Math.abs(ra.top - rootRect.top) + Math.abs(rootRect.right - ra.right);
                                const sb = Math.abs(rb.top - rootRect.top) + Math.abs(rootRect.right - rb.right);
                                return sa - sb;
                              });
                              target = sorted[0] || null;
                            }
                            if (!target) continue;
                            dispatchClick(target);
                            closed += 1;
                          }

                          let dimmedClicks = 0;
                          if (!(preserveReserved && keepReservedRoot)) {
                            const dimmedNodes = Array.from(document.querySelectorAll("[class*='dimmed']"))
                              .filter((node) => isVisible(node));
                            for (const node of dimmedNodes) {
                              dispatchClick(node);
                              dimmedClicks += 1;
                            }
                          }
                          return { closed, dimmedClicks };
                        }
                        """,
                        {"preserveReserved": bool(preserve_reserved_publish)},
                    )
                    if isinstance(payload, dict):
                        if int(payload.get("closed", 0)) > 0 or int(payload.get("dimmedClicks", 0)) > 0:
                            closed_any = True
                except Exception:
                    pass

                if not preserve_reserved_publish:
                    # 최후 수단: 예약 발행 글 팝업 우상단 좌표 클릭
                    try:
                        coords = await page.evaluate(
                            """
                            () => {
                              const isVisible = (el) => {
                                if (!el) return false;
                                const style = window.getComputedStyle(el);
                                if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
                                const rect = el.getBoundingClientRect();
                                return rect.width > 0 && rect.height > 0;
                              };
                              const roots = Array.from(
                                document.querySelectorAll("[class*='layer_popup'], [class*='popup'], [role='dialog'], [class*='modal']")
                              ).filter((el) => isVisible(el));
                              const reserved = roots.find((el) => /예약\\s*발행\\s*글|예약\\s*발행/i.test(String(el.textContent || "")));
                              if (!reserved) return null;
                              const rect = reserved.getBoundingClientRect();
                              return { x: Math.floor(rect.right - 20), y: Math.floor(rect.top + 20) };
                            }
                            """
                        )
                        if isinstance(coords, dict) and "x" in coords and "y" in coords:
                            await page.mouse.click(float(coords["x"]), float(coords["y"]))
                            closed_any = True
                            await asyncio.sleep(0.15)
                    except Exception:
                        pass

                if not preserve_reserved_publish:
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass

                await asyncio.sleep(0.2 if closed_any else 0.3)
        finally:
            self._overlay_dismiss_in_progress = False

    async def _try_direct_file_input(self, page, path: str, label: str) -> bool:
        """숨겨진 file input을 직접 노출 후 set_files를 시도한다."""
        selectors = (
            "input[type='file'][accept*='image'], "
            "input[type='file'][accept*='png'], "
            "input[type='file'][accept*='jpg'], "
            "input[type='file']"
        )
        locator = page.locator(selectors)
        try:
            count = await locator.count()
        except Exception:
            count = 0
        if count == 0:
            logger.warning("%s: file input을 찾지 못했습니다.", label)
            return False

        for idx in range(min(count, 8)):
            target = locator.nth(idx)
            try:
                handle = await target.element_handle()
                if handle is None:
                    continue
                await page.evaluate(
                    """(el) => {
                        el.style.display = 'block';
                        el.style.visibility = 'visible';
                        el.style.opacity = '1';
                        el.hidden = false;
                      }""",
                    handle,
                )
                await target.set_input_files(path)
                logger.info("%s: direct file input 성공 (index=%s)", label, idx)
                return True
            except Exception as exc:
                logger.warning("%s: direct file input 실패 (index=%s, error=%s)", label, idx, exc)
                continue
        return False

    async def _try_file_chooser_via_selectors(
        self,
        page,
        path: str,
        selectors: list[str],
        label: str,
    ) -> bool:
        """여러 셀렉터를 순회하며 FileChooser 업로드를 시도한다."""
        for selector in selectors:
            try:
                await self._dismiss_blocking_layer_popup(page)
                button = page.locator(selector).first
                if await button.count() == 0:
                    continue
                try:
                    await button.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                async with page.expect_file_chooser(timeout=8000) as fc_info:
                    try:
                        await button.click(timeout=4000)
                    except Exception:
                        await self._activate_toggle_target(page, button, aggressive=True)
                file_chooser = await fc_info.value
                await file_chooser.set_files(path)
                logger.info("%s: file chooser 성공 (selector=%s)", label, selector)
                return True
            except Exception as exc:
                logger.warning("%s: file chooser 실패 (selector=%s, error=%s)", label, selector, exc)
        return False

    async def _try_submenu_upload(
        self,
        page,
        path: str,
        trigger_selectors: list[str],
        submenu_selectors: list[str],
        label: str,
    ) -> bool:
        """버튼 클릭 후 서브메뉴(내 PC/직접 업로드) 경유 업로드를 시도한다."""
        for trigger in trigger_selectors:
            try:
                await self._dismiss_blocking_layer_popup(page)
                trigger_button = page.locator(trigger).first
                if await trigger_button.count() == 0:
                    continue
                try:
                    await trigger_button.click(timeout=3000)
                except Exception:
                    await self._activate_toggle_target(page, trigger_button, aggressive=True)
                await asyncio.sleep(0.5)
            except Exception as exc:
                logger.warning("%s: trigger 클릭 실패 (selector=%s, error=%s)", label, trigger, exc)
                continue

            for submenu in submenu_selectors:
                try:
                    await self._dismiss_blocking_layer_popup(page)
                    item = page.locator(submenu).first
                    if await item.count() == 0:
                        continue
                    async with page.expect_file_chooser(timeout=8000) as fc_info:
                        try:
                            await item.click(timeout=3000)
                        except Exception:
                            await self._activate_toggle_target(page, item, aggressive=True)
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(path)
                    logger.info("%s: submenu file chooser 성공 (trigger=%s, submenu=%s)", label, trigger, submenu)
                    return True
                except Exception as exc:
                    logger.warning(
                        "%s: submenu file chooser 실패 (trigger=%s, submenu=%s, error=%s)",
                        label,
                        trigger,
                        submenu,
                        exc,
                    )
        return False

    async def _try_clipboard_paste(self, path: str, page) -> bool:
        """클립보드 붙여넣기 fallback을 시도한다."""
        try:
            import pyperclip  # type: ignore[import-not-found]
        except Exception:
            logger.warning("image-D: pyperclip 미설치로 클립보드 fallback 생략")
            return False

        try:
            pyperclip.copy(str(Path(path).resolve()))
            await page.keyboard.press("ControlOrMeta+V")
            await asyncio.sleep(3.0)
            logger.info("image-D: 클립보드 붙여넣기 시도 완료")
            return True
        except Exception as exc:
            logger.warning("image-D: 클립보드 붙여넣기 실패 (%s)", exc)
            return False

    async def _after_upload_wait(self) -> None:
        """업로드 후 에디터 반응 대기를 수행한다."""
        await asyncio.sleep(3.0)
        await self._human_delay(2000, 4000)

    async def _align_latest_uploaded_image_to_center(self, page, image_path: str) -> None:
        """업로드 직후 최신 이미지를 중앙정렬한다."""
        try:
            await self._recover_from_smarteditor_image_mode(page, stage="align-start")
            await self._focus_latest_uploaded_image(page)
            await self._human_delay(180, 260)

            clicked = await self._try_center_align_click(page)
            if not clicked:
                clicked = await self._force_center_align_with_dom(page)

            await self._recover_from_smarteditor_image_mode(page, stage="align-after-click")

            await asyncio.sleep(0.25)
            if clicked and await self._is_latest_image_centered(page):
                logger.info("이미지 중앙정렬 완료: %s", image_path)
                return

            logger.warning("이미지 중앙정렬 확인 실패(계속 진행): %s", image_path)
        except Exception as exc:
            logger.warning("이미지 중앙정렬 처리 실패(%s): %s", image_path, exc)

    async def _try_center_align_click(self, page) -> bool:
        """툴바 버튼 클릭으로 최신 이미지 중앙정렬을 시도한다."""
        selectors = [
            "button[aria-label*='가운데']",
            "button[aria-label*='중앙']",
            "button[title*='가운데']",
            "button[title*='중앙']",
            "[class*='image-toolbar'] button[class*='align-center']",
            "[class*='image-toolbar'] [data-name='align-center']",
            "[class*='image-toolbar'] button:has-text('가운데')",
            "[class*='image-toolbar'] button:has-text('중앙')",
            "[class*='floating'] button[class*='align-center']",
            "[class*='component-toolbar'] button[class*='align-center']",
        ]

        for selector in selectors:
            try:
                buttons = page.locator(selector)
                count = await buttons.count()
                if count == 0:
                    continue
                for index in range(min(count, 3)):
                    button = buttons.nth(index)
                    if not await button.is_visible():
                        continue
                    await self._activate_toggle_target(page, button, aggressive=True)
                    await asyncio.sleep(0.2)
                    if await self._is_latest_image_centered(page):
                        logger.debug("중앙정렬 버튼 클릭 성공: %s", selector)
                        return True
            except Exception:
                continue

        # 광범위한 JS 폴백 클릭은 예기치 않은 팝업을 열 수 있어 사용하지 않는다.
        return False

    async def _force_center_align_with_dom(self, page) -> bool:
        """버튼 클릭이 실패하면 DOM 스타일/속성 조작으로 중앙정렬을 강제한다."""
        try:
            payload = await page.evaluate(
                """
                () => {
                  const candidates = [];
                  const selectors = [
                    "[class*='se-component-image']",
                    "[class*='se-image']",
                    ".se-component-content:has(img)",
                  ];
                  for (const selector of selectors) {
                    for (const node of Array.from(document.querySelectorAll(selector))) {
                      if (node.querySelector("img")) candidates.push(node);
                    }
                    if (candidates.length > 0) break;
                  }
                  if (candidates.length === 0) return { updated: false };
                  const component = candidates[candidates.length - 1];
                  const image = component.querySelector("img");
                  if (!image) return { updated: false };

                  // 에디터 직렬화에 반영될 가능성이 높은 속성과 스타일을 함께 세팅한다.
                  component.setAttribute("data-align", "center");
                  component.style.textAlign = "center";
                  component.style.marginLeft = "auto";
                  component.style.marginRight = "auto";
                  component.classList.add("se-image-align-center");
                  component.classList.remove("se-image-align-left");
                  component.classList.remove("se-image-align-right");

                  image.style.display = "block";
                  image.style.marginLeft = "auto";
                  image.style.marginRight = "auto";
                  image.style.float = "none";

                  const parent = component.parentElement;
                  if (parent) {
                    parent.style.textAlign = "center";
                  }
                  return { updated: true };
                }
                """
            )
            return bool(payload and payload.get("updated"))
        except Exception:
            return False

    async def _is_latest_image_centered(self, page) -> bool:
        """최신 이미지 컴포넌트가 중앙정렬 상태인지 판별한다."""
        try:
            payload = await page.evaluate(
                """
                () => {
                  const selectors = [
                    "[class*='se-component-image']",
                    "[class*='se-image']",
                    ".se-component-content:has(img)",
                  ];
                  let component = null;
                  for (const selector of selectors) {
                    const nodes = Array.from(document.querySelectorAll(selector)).filter((node) => node.querySelector("img"));
                    if (nodes.length > 0) {
                      component = nodes[nodes.length - 1];
                      break;
                    }
                  }
                  if (!component) return false;
                  const image = component.querySelector("img");
                  if (!image) return false;

                  const className = String(component.className || "").toLowerCase();
                  const dataAlign = String(component.getAttribute("data-align") || "").toLowerCase();
                  const componentStyle = window.getComputedStyle(component);
                  const imageStyle = window.getComputedStyle(image);
                  const section = component.closest(".se-section-image, [class*='se-section-image'], [class*='se-section']");
                  const sectionClass = String((section && section.className) || "").toLowerCase();
                  const sectionAlign = String((section && section.getAttribute("data-align")) || "").toLowerCase();

                  if (/align-center|center/.test(className) && !/align-left|align-right/.test(className)) return true;
                  if (dataAlign === "center") return true;
                  if (/se-section-align-center|align-center/.test(sectionClass)) return true;
                  if (sectionAlign === "center") return true;
                  if (componentStyle.textAlign === "center") return true;
                  if (imageStyle.marginLeft === "auto" && imageStyle.marginRight === "auto") return true;
                  return false;
                }
                """
            )
            return bool(payload)
        except Exception:
            return False

    def _prepare_image_for_upload(self, source_path: str) -> str:
        """업로드 전에 이미지를 가독성 폭으로 리사이즈한다."""
        src = Path(str(source_path))
        if not src.exists():
            return str(src)

        try:
            from PIL import Image  # type: ignore[import-not-found]
        except Exception:
            logger.warning("Pillow 미설치로 원본 이미지를 그대로 사용합니다: %s", source_path)
            return str(src)

        try:
            with Image.open(src) as image:
                width, height = image.size
                target_width = self._image_upload_target_width
                if width <= target_width:
                    return str(src)

                target_height = max(1, int((height * target_width) / max(1, width)))
                resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
                resized = image.resize((target_width, target_height), resampling)

                output_dir = src.parent / "resized"
                output_dir.mkdir(parents=True, exist_ok=True)
                output_path = output_dir / f"{src.stem}_w{target_width}{src.suffix or '.png'}"

                save_target = resized
                if output_path.suffix.lower() in {".jpg", ".jpeg"} and resized.mode in {"RGBA", "LA", "P"}:
                    save_target = resized.convert("RGB")

                suffix = output_path.suffix.lower()
                if suffix in {".jpg", ".jpeg"}:
                    save_target.save(output_path, quality=92, optimize=True)
                elif suffix == ".png":
                    save_target.save(output_path, optimize=True)
                else:
                    save_target.save(output_path)
                logger.info(
                    "Image resized before upload: %s -> %s (%dx%d -> %dx%d)",
                    source_path,
                    output_path,
                    width,
                    height,
                    target_width,
                    target_height,
                )
                return str(output_path)
        except Exception as exc:
            logger.warning("이미지 리사이즈 실패, 원본 업로드로 폴백: %s (%s)", source_path, exc)
            return str(src)

    async def _insert_non_gallery_separator(self, page, stage: str = "before") -> None:
        """이미지 콜라주 자동 묶음을 피하기 위해 문단 분리 노드를 삽입한다."""
        try:
            await page.keyboard.press("Enter")
            await self._human_delay(120, 240)
            await page.keyboard.insert_text(" ")
            await self._human_delay(80, 160)
            await page.keyboard.press("Enter")
            await self._human_delay(120, 240)
        except Exception:
            pass

        # 스마트에디터가 연속 이미지를 갤러리로 합치는 현상을 막기 위해 빈 p 노드를 강제 삽입한다.
        inserted = await self._inject_empty_paragraph_after_latest_image(page)
        if inserted:
            logger.debug("Non-gallery separator inserted (%s)", stage)

    async def _inject_empty_paragraph_after_latest_image(self, page) -> bool:
        """최신 이미지 컴포넌트 뒤에 빈 텍스트 문단을 추가하고 커서를 이동한다."""
        try:
            payload = await page.evaluate(
                """
                () => {
                  const selectors = [
                    "[class*='se-component-image']",
                    "[class*='se-image']",
                    ".se-component-content:has(img)",
                  ];
                  let lastComponent = null;
                  for (const selector of selectors) {
                    const nodes = Array.from(document.querySelectorAll(selector));
                    if (nodes.length > 0) {
                      lastComponent = nodes[nodes.length - 1];
                      break;
                    }
                  }
                  if (!lastComponent || !lastComponent.parentElement) {
                    return { inserted: false };
                  }

                  const paragraph = document.createElement("p");
                  paragraph.className = "se-text-paragraph se-text-paragraph-align-left";
                  paragraph.innerHTML = "&nbsp;";
                  paragraph.setAttribute("data-autoblog-separator", "true");

                  if (lastComponent.nextSibling) {
                    lastComponent.parentElement.insertBefore(paragraph, lastComponent.nextSibling);
                  } else {
                    lastComponent.parentElement.appendChild(paragraph);
                  }

                  const range = document.createRange();
                  range.selectNodeContents(paragraph);
                  range.collapse(false);
                  const selection = window.getSelection();
                  if (selection) {
                    selection.removeAllRanges();
                    selection.addRange(range);
                  }
                  return { inserted: true };
                }
                """
            )
            return bool(payload and payload.get("inserted"))
        except Exception:
            return False

    def _is_ai_generated_image(self, image_path: str) -> bool:
        """메타데이터 우선으로 AI 생성 이미지를 판별한다."""
        source_meta = self._get_image_source_meta(image_path)
        source_kind = str(source_meta.get("kind", "unknown")).strip().lower()
        if source_kind:
            if source_kind == "ai":
                return True
            if source_kind in {"stock", "placeholder", "manual"}:
                return False

        # 하위 호환: 메타데이터가 비어 있으면 파일명 규칙으로 폴백한다.
        filename = Path(str(image_path)).name.strip().lower()
        if not filename:
            return False
        return any(filename.startswith(prefix) for prefix in self.AI_IMAGE_PREFIXES)

    def _build_image_match_tokens(
        self,
        image_path: str,
        uploaded_path: Optional[str] = None,
    ) -> List[str]:
        """원본/리사이즈 경로 기반으로 이미지 매칭 토큰 목록을 만든다."""
        tokens: List[str] = []
        for raw in [image_path, uploaded_path]:
            if not raw:
                continue
            path = Path(str(raw))
            name = path.name.strip().lower()
            stem = path.stem.strip().lower()
            if name and name not in tokens:
                tokens.append(name)
            if stem and stem not in tokens:
                tokens.append(stem)
        return tokens

    def _decide_ai_toggle(self, image_path: str) -> Dict[str, Any]:
        """현재 모드(force/metadata)와 메타데이터를 기준으로 토글 기대값을 계산한다."""
        source_meta = self._get_image_source_meta(image_path)
        source_kind = str(source_meta.get("kind", "unknown")).strip().lower() or "unknown"
        provider = str(source_meta.get("provider", "unknown")).strip().lower() or "unknown"
        if self._ai_toggle_mode == "off":
            should_toggle = False
        elif self._ai_toggle_mode == "force":
            should_toggle = True
        else:
            should_toggle = self._is_ai_generated_image(image_path)
        return {
            "should_toggle": bool(should_toggle),
            "source_kind": source_kind,
            "provider": provider,
            "mode": self._ai_toggle_mode,
        }

    @staticmethod
    def _has_selected_class(class_text: str) -> bool:
        """클래스 문자열에 선택/활성(ON) 상태가 포함됐는지 검사한다."""
        normalized = f" {str(class_text or '').strip().lower()} "
        return (
            " se-is-selected " in normalized
            or " is-selected " in normalized
            or " is-on " in normalized
            or " on " in normalized
            or " checked " in normalized
            or " active " in normalized
        )

    @staticmethod
    def _read_retention_limit(env_name: str, default: int) -> int:
        """보관 개수 환경변수를 안전하게 정수로 읽는다."""
        try:
            raw = str(os.getenv(env_name, str(default))).strip()
            value = int(raw)
            return max(1, min(500, value))
        except Exception:
            return max(1, int(default))

    @classmethod
    def _is_ai_toggle_on_snapshot(cls, snapshot: Dict[str, Any]) -> bool:
        """토글 DOM 스냅샷을 기준으로 ON 상태를 판정한다."""
        class_fields = [
            str(snapshot.get("buttonClass", "")),
            str(snapshot.get("wrapperClass", "")),
            str(snapshot.get("markClass", "")),
            str(snapshot.get("toggleClass", "")),
        ]
        if any(cls._has_selected_class(value) for value in class_fields):
            return True

        attr_fields = [
            str(snapshot.get("buttonAriaChecked", "")),
            str(snapshot.get("buttonAriaPressed", "")),
            str(snapshot.get("buttonDataActive", "")),
            str(snapshot.get("toggleAriaChecked", "")),
            str(snapshot.get("toggleAriaPressed", "")),
            str(snapshot.get("toggleDataActive", "")),
            str(snapshot.get("wrapperAriaChecked", "")),
            str(snapshot.get("wrapperAriaPressed", "")),
            str(snapshot.get("wrapperDataActive", "")),
        ]
        if any(value.strip().lower() == "true" for value in attr_fields):
            return True

        checked_fields = [
            snapshot.get("buttonChecked"),
            snapshot.get("toggleChecked"),
            snapshot.get("wrapperChecked"),
        ]
        return any(value is True for value in checked_fields)

    @staticmethod
    def _prune_old_debug_files(directory: Path, pattern: str, keep: int) -> None:
        """오래된 디버그 파일을 정리해 디스크 사용량을 억제한다."""
        if keep <= 0 or not directory.exists():
            return
        try:
            candidates = [path for path in directory.glob(pattern) if path.is_file()]
            candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            for stale in candidates[keep:]:
                try:
                    stale.unlink(missing_ok=True)
                except Exception:
                    continue
        except Exception:
            return

    @staticmethod
    def _ai_toggle_report_dir() -> Path:
        """AI 토글 리포트 디렉터리를 반환한다."""
        raw = str(os.getenv("NAVER_AI_TOGGLE_REPORT_DIR", "data/ai_toggle")).strip()
        return Path(raw or "data/ai_toggle")

    def _count_recent_ai_toggle_failure_streak(self, max_scan: int = 20) -> int:
        """최근 리포트 기준 연속 실패 횟수를 계산한다."""
        report_dir = self._ai_toggle_report_dir()
        if not report_dir.exists():
            return 0
        try:
            history = [path for path in report_dir.glob("report_*.json") if path.is_file()]
            history.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        except Exception:
            return 0

        streak = 0
        for report_path in history[: max(1, int(max_scan))]:
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
            if not isinstance(summary, dict):
                break
            pre = summary.get("prepublish", {}) if isinstance(summary.get("prepublish", {}), dict) else {}
            post = summary.get("postverify", {}) if isinstance(summary.get("postverify", {}), dict) else {}
            pre_failed = int(pre.get("failed", 0) or 0)
            post_failed = int(post.get("failed", 0) or 0)
            if pre_failed > 0 or post_failed > 0:
                streak += 1
                continue
            break
        return streak

    def _record_ai_toggle_audit(
        self,
        *,
        image_path: str,
        uploaded_path: Optional[str],
        location: str,
        source_kind: str,
        provider: str,
        expected_on: bool,
        actual_on: Optional[bool],
        note: str,
        attempts: int,
    ) -> None:
        """이미지별 AI 토글 기대값/실측값을 감사 로그 구조로 누적한다."""
        self._ai_toggle_audit_rows.append(
            {
                "image_path": image_path,
                "uploaded_path": uploaded_path,
                "location": location,
                "source_kind": source_kind,
                "provider": provider,
                "expected_on": bool(expected_on),
                "actual_on": actual_on,
                "mode": self._ai_toggle_mode,
                "tokens": self._build_image_match_tokens(image_path, uploaded_path),
                "attempts": int(attempts),
                "note": note[:200],
            }
        )

    def _build_ai_toggle_report(self, post_url: str = "") -> Dict[str, Any]:
        """AI 토글 점검 리포트 데이터를 구성한다."""
        rows = list(self._ai_toggle_audit_rows)
        expected_on = sum(1 for row in rows if row.get("expected_on") is True)
        actual_on = sum(1 for row in rows if row.get("actual_on") is True)
        post_passed = sum(1 for row in rows if row.get("post_verify_on") is True)
        return {
            "mode": self._ai_toggle_mode,
            "post_url": post_url,
            "rows": rows,
            "expected_on": expected_on,
            "actual_on": actual_on,
            "post_verify_passed": post_passed,
            "summary": dict(self._ai_toggle_summary),
            "created_at": int(time.time()),
        }

    def _persist_ai_toggle_report(self, post_url: str = "") -> None:
        """AI 토글 리포트를 파일로 저장한다."""
        try:
            report_dir = self._ai_toggle_report_dir()
            report_dir.mkdir(parents=True, exist_ok=True)
            report = self._build_ai_toggle_report(post_url)
            target = report_dir / "last_report.json"
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            timestamp = int(report.get("created_at", int(time.time())))
            history_path = report_dir / f"report_{timestamp}.json"
            history_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            self._prune_old_debug_files(
                report_dir,
                "report_*.json",
                keep=self._ai_report_retention_max,
            )
        except Exception as exc:
            logger.warning("AI 토글 리포트 저장 실패: %s", exc)

    def _get_telegram_notifier(self):
        """텔레그램 알림 인스턴스를 지연 초기화한다."""
        if self._telegram_notifier is not None:
            return self._telegram_notifier
        try:
            from ..automation.notifier import TelegramNotifier

            self._telegram_notifier = TelegramNotifier.from_env()
        except Exception:
            self._telegram_notifier = None
        return self._telegram_notifier

    def _notify_ai_toggle_alert_background(self, title: str, details: List[str]) -> None:
        """AI 토글 검증 실패 알림을 텔레그램으로 비동기 전송한다."""
        if not self._ai_toggle_telegram_alert:
            return
        notifier = self._get_telegram_notifier()
        if notifier is None or not getattr(notifier, "enabled", False):
            return
        lines = [title, f"- blog_id: {self.blog_id}", f"- mode: {self._ai_toggle_mode}"]
        pre = self._ai_toggle_summary.get("prepublish", {})
        if isinstance(pre, dict) and pre:
            lines.append(
                "- prepublish: expected={expected} verified={verified} repaired={repaired} failed={failed}".format(
                    expected=int(pre.get("expected_on", 0)),
                    verified=int(pre.get("verified_on", 0)),
                    repaired=int(pre.get("repaired", 0)),
                    failed=int(pre.get("failed", 0)),
                )
            )
        post = self._ai_toggle_summary.get("postverify", {})
        if isinstance(post, dict) and post:
            lines.append(
                "- postverify: expected={expected} passed={passed} failed={failed}".format(
                    expected=int(post.get("expected_on", 0)),
                    passed=int(post.get("passed", 0)),
                    failed=int(post.get("failed", 0)),
                )
            )
        current_failed = False
        if isinstance(pre, dict) and int(pre.get("failed", 0)) > 0:
            current_failed = True
        if isinstance(post, dict) and int(post.get("failed", 0)) > 0:
            current_failed = True
        if current_failed:
            previous_streak = self._count_recent_ai_toggle_failure_streak(max_scan=30)
            streak = previous_streak + 1
            lines.append(f"- recent_failure_streak: {streak}")
            if streak >= self._ai_toggle_alert_streak:
                lines.insert(0, f"🚨 연속 AI 토글 실패 {streak}회 (임계값 {self._ai_toggle_alert_streak})")
        lines.extend(details[:10])
        try:
            notifier.send_message_background("\n".join(lines), disable_notification=False)
        except Exception:
            return

    async def _read_selected_image_ai_toggle_state(self, page) -> Dict[str, Any]:
        """현재 선택된 이미지 컴포넌트의 AI 토글 상태를 읽는다."""
        try:
            payload = await page.evaluate(
                """
                () => {
                  const components = Array.from(
                    document.querySelectorAll(
                      ".se-section-image, .se-component.se-image, [class*='se-section-image'], [class*='se-component-image'], [class*='se-image']"
                    )
                  ).filter((node) => node.querySelector("img[src]"));
                  if (components.length === 0) {
                    return { found: false, on: false, buttonClass: "", sectionClass: "" };
                  }

                  const isSelected = (node) => {
                    const className = String(node.className || "").toLowerCase();
                    return /se-is-selected|se-is-activated|selected|focus|active/.test(className);
                  };

                  const selected = components.find((node) => isSelected(node));
                  const section = selected || components[components.length - 1];
                  const aiButton = section
                    ? (
                      section.querySelector(".se-set-ai-mark-button-toggle, [class*='ai-mark-button-toggle']")
                      || section.querySelector(".se-set-ai-mark-button:not([class*='wrapper']), [class*='ai-mark-button']:not([class*='wrapper'])")
                      || section.querySelector(".se-set-ai-mark-button-wrapper, [class*='ai-mark-button-wrapper']")
                    )
                    : null;
                  if (!aiButton) {
                    return {
                      found: false,
                      on: false,
                      buttonClass: "",
                      sectionClass: String((section && section.className) || ""),
                    };
                  }

                  const buttonClass = String(aiButton.className || "");
                  const relatedNodes = [];
                  if (aiButton) relatedNodes.push(aiButton);
                  const wrapperNode = aiButton.closest(".se-set-ai-mark-button-wrapper, [class*='ai-mark-button-wrapper']");
                  if (wrapperNode) relatedNodes.push(wrapperNode);
                  const markNode = wrapperNode
                    ? wrapperNode.querySelector(".se-set-ai-mark-button:not([class*='wrapper']), [class*='ai-mark-button']:not([class*='wrapper'])")
                    : aiButton.closest(".se-set-ai-mark-button, [class*='ai-mark-button']");
                  if (markNode) relatedNodes.push(markNode);
                  const toggleNode = wrapperNode
                    ? wrapperNode.querySelector(".se-set-ai-mark-button-toggle, [class*='ai-mark-button-toggle'], input[type='checkbox'], [role='switch'], [class*='toggle']")
                    : (
                      aiButton.querySelector("input[type='checkbox'], [role='switch'], [class*='toggle']")
                      || aiButton.closest(".se-set-ai-mark-button-toggle, [class*='ai-mark-button-toggle']")
                    );
                  if (toggleNode) relatedNodes.push(toggleNode);
                  const isNodeOn = (node) => {
                    if (!node) return false;
                    const cls = ` ${String(node.className || "").toLowerCase()} `;
                    if (
                      cls.includes(" se-is-selected ")
                      || cls.includes(" is-selected ")
                      || cls.includes(" is-on ")
                      || cls.includes(" se-is-on ")
                      || cls.includes(" on ")
                      || cls.includes(" active ")
                      || cls.includes(" checked ")
                      || cls.includes(" enabled ")
                    ) {
                      return true;
                    }
                    const attrs = [
                      String(node.getAttribute("aria-checked") || "").toLowerCase(),
                      String(node.getAttribute("aria-pressed") || "").toLowerCase(),
                      String(node.getAttribute("data-active") || "").toLowerCase(),
                    ];
                    if (attrs.includes("true")) return true;
                    if (typeof node.checked === "boolean" && node.checked === true) return true;
                    const nested = node.querySelector && node.querySelector("input[type='checkbox']");
                    if (nested && typeof nested.checked === "boolean" && nested.checked === true) return true;
                    return false;
                  };
                  const on = relatedNodes.some((node) => isNodeOn(node));
                  return {
                    found: true,
                    on,
                    buttonClass,
                    wrapperClass: String((wrapperNode && wrapperNode.className) || ""),
                    markClass: String((markNode && markNode.className) || ""),
                    toggleClass: String((toggleNode && toggleNode.className) || ""),
                    buttonAriaChecked: String(aiButton.getAttribute("aria-checked") || ""),
                    buttonAriaPressed: String(aiButton.getAttribute("aria-pressed") || ""),
                    buttonDataActive: String(aiButton.getAttribute("data-active") || ""),
                    buttonChecked: typeof aiButton.checked === "boolean" ? aiButton.checked : null,
                    toggleAriaChecked: String((toggleNode && toggleNode.getAttribute ? toggleNode.getAttribute("aria-checked") : "") || ""),
                    toggleAriaPressed: String((toggleNode && toggleNode.getAttribute ? toggleNode.getAttribute("aria-pressed") : "") || ""),
                    toggleDataActive: String((toggleNode && toggleNode.getAttribute ? toggleNode.getAttribute("data-active") : "") || ""),
                    toggleChecked: typeof (toggleNode && toggleNode.checked) === "boolean" ? toggleNode.checked : null,
                    wrapperAriaChecked: String((wrapperNode && wrapperNode.getAttribute ? wrapperNode.getAttribute("aria-checked") : "") || ""),
                    wrapperAriaPressed: String((wrapperNode && wrapperNode.getAttribute ? wrapperNode.getAttribute("aria-pressed") : "") || ""),
                    wrapperDataActive: String((wrapperNode && wrapperNode.getAttribute ? wrapperNode.getAttribute("data-active") : "") || ""),
                    wrapperChecked: typeof (wrapperNode && wrapperNode.checked) === "boolean" ? wrapperNode.checked : null,
                    sectionClass: String((section && section.className) || ""),
                  };
                }
                """
            )
            if not isinstance(payload, dict):
                return {"found": False, "on": False, "button_class": "", "section_class": ""}
            normalized_on = bool(payload.get("on")) or self._is_ai_toggle_on_snapshot(payload)
            return {
                "found": bool(payload.get("found")),
                "on": normalized_on,
                "button_class": str(payload.get("buttonClass", "")),
                "section_class": str(payload.get("sectionClass", "")),
            }
        except Exception:
            return {"found": False, "on": False, "button_class": "", "section_class": ""}

    async def _focus_image_by_tokens(self, page, tokens: List[str]) -> bool:
        """이미지 src와 토큰을 매칭해 대상 이미지를 선택한다."""
        normalized = [str(token).strip().lower() for token in tokens if str(token).strip()]
        if not normalized:
            return False
        try:
            payload = await page.evaluate(
                """
                ({ tokens }) => {
                  const candidates = Array.from(
                    document.querySelectorAll(
                      ".se-section-image img[src], .se-component-content img[src], .se-main-container img[src], img[src]"
                    )
                  );
                  if (candidates.length === 0) return { focused: false, src: "" };
                  const target = candidates.find((img) => {
                    const src = String(img.getAttribute("src") || "").toLowerCase();
                    return tokens.some((token) => token && src.includes(token));
                  });
                  if (!target) return { focused: false, src: "" };
                  target.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
                  const events = ["pointerdown", "mousedown", "mouseup", "click"];
                  for (const type of events) {
                    target.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, composed: true }));
                  }
                  return { focused: true, src: String(target.getAttribute("src") || "") };
                }
                """,
                {"tokens": normalized},
            )
            return bool(isinstance(payload, dict) and payload.get("focused"))
        except Exception:
            return False

    async def _apply_ai_usage_compliance(
        self,
        *,
        page,
        image_path: str,
        location: str,
        uploaded_path: Optional[str] = None,
    ) -> None:
        """이미지별 AI 활용 토글을 적용하고 클래스 기반으로 성공 여부를 검증한다."""
        decision = self._decide_ai_toggle(image_path)
        should_toggle = bool(decision["should_toggle"])
        source_kind = str(decision["source_kind"])
        provider = str(decision["provider"])
        mode = str(decision["mode"])
        actual_on: Optional[bool] = None
        attempts = 0

        try:
            # 토글 시도 전에 가로채기 팝업을 강하게 정리한다.
            # 0.6s 대기: React synthetic event 처리 완료 시간 확보
            for round_index in range(3):
                await self._dismiss_blocking_layer_popup(page, max_rounds=4, preserve_reserved_publish=False)
                await asyncio.sleep(0.6)
                popup_state = await self._collect_popup_debug_state(page)
                if popup_state.get("count", 0) == 0:
                    break
                logger.warning(
                    "AI 토글 전 팝업 잔류 감지(%s/%s): %s",
                    round_index + 1,
                    3,
                    popup_state,
                )

            await self._recover_from_smarteditor_image_mode(page, stage="ai-toggle-start")
            await self._focus_latest_uploaded_image(page)

            if mode == "force":
                logger.info(
                    "AI 활용 설정 강제 모드 활성: %s (location=%s, kind=%s, provider=%s)",
                    image_path,
                    location,
                    source_kind,
                    provider,
                )
            elif mode == "off":
                logger.info(
                    "AI 활용 설정 OFF 모드: %s (location=%s, kind=%s, provider=%s)",
                    image_path,
                    location,
                    source_kind,
                    provider,
                )

            if not should_toggle:
                state = await self._read_selected_image_ai_toggle_state(page)
                actual_on = bool(state["on"]) if state["found"] else None
                logger.info(
                    "AI 활용 설정 생략: %s (location=%s, kind=%s, provider=%s, actual_on=%s)",
                    image_path,
                    location,
                    source_kind,
                    provider,
                    actual_on,
                )
                self._record_ai_toggle_audit(
                    image_path=image_path,
                    uploaded_path=uploaded_path,
                    location=location,
                    source_kind=source_kind,
                    provider=provider,
                    expected_on=False,
                    actual_on=actual_on,
                    note="skip_non_ai" if mode == "metadata" else "skip_mode_off",
                    attempts=attempts,
                )
                return

            for attempt in range(1, 4):
                attempts = attempt
                await self._dismiss_blocking_layer_popup(page, max_rounds=2, preserve_reserved_publish=False)
                await self._recover_from_smarteditor_image_mode(page, stage="ai-toggle-loop")
                toggled = await self._ensure_ai_usage_toggle_on(page)
                state = await self._read_selected_image_ai_toggle_state(page)
                actual_on = bool(state["on"]) if state["found"] else False
                if toggled and actual_on:
                    logger.info(
                        "AI 활용 설정 ON 완료 (%s): %s (kind=%s, provider=%s, attempts=%s)",
                        location,
                        image_path,
                        source_kind,
                        provider,
                        attempt,
                    )
                    self._record_ai_toggle_audit(
                        image_path=image_path,
                        uploaded_path=uploaded_path,
                        location=location,
                        source_kind=source_kind,
                        provider=provider,
                        expected_on=True,
                        actual_on=True,
                        note="on_verified",
                        attempts=attempts,
                    )
                    return
                await asyncio.sleep(0.8)

            # 마지막 1회 자가복구: 이미지 재선택 후 토글 재시도
            attempts += 1
            await self._focus_latest_uploaded_image(page)
            await self._recover_from_smarteditor_image_mode(page, stage="ai-toggle-self-heal")
            await self._ensure_ai_usage_toggle_on(page)
            state = await self._read_selected_image_ai_toggle_state(page)
            actual_on = bool(state["on"]) if state["found"] else False
            if actual_on:
                logger.info(
                    "AI 활용 설정 ON 완료 (%s): %s (kind=%s, provider=%s, attempts=%s, note=self-heal)",
                    location,
                    image_path,
                    source_kind,
                    provider,
                    attempts,
                )
                self._record_ai_toggle_audit(
                    image_path=image_path,
                    uploaded_path=uploaded_path,
                    location=location,
                    source_kind=source_kind,
                    provider=provider,
                    expected_on=True,
                    actual_on=True,
                    note="on_verified_self_heal",
                    attempts=attempts,
                )
                return

            logger.warning(
                "AI 활용 설정 ON 실패 (%s): %s (kind=%s, provider=%s, attempts=%s)",
                location,
                image_path,
                source_kind,
                provider,
                attempts,
            )
            popup_state = await self._collect_popup_debug_state(page)
            if popup_state.get("count", 0) > 0:
                logger.warning("AI 활용 설정 실패 시 팝업 상태: %s", popup_state)
            self._record_ai_toggle_audit(
                image_path=image_path,
                uploaded_path=uploaded_path,
                location=location,
                source_kind=source_kind,
                provider=provider,
                expected_on=True,
                actual_on=False,
                note="on_failed",
                attempts=attempts,
            )
            await self._save_screenshot(page, "ai_toggle_on_failed")
        except Exception as exc:
            logger.warning(
                "AI 활용 설정 처리 실패 (%s): %s (kind=%s, provider=%s)",
                image_path,
                exc,
                source_kind,
                provider,
            )
            self._record_ai_toggle_audit(
                image_path=image_path,
                uploaded_path=uploaded_path,
                location=location,
                source_kind=source_kind,
                provider=provider,
                expected_on=should_toggle,
                actual_on=actual_on,
                note=f"exception:{type(exc).__name__}",
                attempts=attempts,
            )
        finally:
            try:
                await self._dismiss_blocking_layer_popup(page)
            except Exception:
                pass

    async def _collect_popup_debug_state(self, page) -> Dict[str, Any]:
        """현재 보이는 팝업/모달 상태를 수집한다."""
        try:
            payload = await page.evaluate(
                """
                () => {
                  const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };
                  const roots = Array.from(
                    document.querySelectorAll("[class*='layer_popup'], [class*='popup'], [role='dialog'], [class*='modal']")
                  ).filter((el) => isVisible(el));
                  const texts = roots.map((el) => String(el.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 120));
                  const classes = roots.map((el) => String(el.className || "").slice(0, 120));
                  const reserved = texts.filter((text) => /예약\\s*발행\\s*글|예약\\s*발행/i.test(text)).length;
                  return { count: roots.length, reserved, texts, classes };
                }
                """
            )
            if isinstance(payload, dict):
                return {
                    "count": int(payload.get("count", 0) or 0),
                    "reserved": int(payload.get("reserved", 0) or 0),
                    "texts": payload.get("texts", []),
                    "classes": payload.get("classes", []),
                }
        except Exception:
            pass
        return {"count": 0, "reserved": 0, "texts": [], "classes": []}

    async def _run_ai_toggle_prepublish_validation(self, page) -> None:
        """발행 직전 이미지별 AI 토글 기대값 대비 실측값을 점검하고 1회 자가복구한다."""
        if not self._ai_toggle_audit_rows:
            return

        expected_rows = [row for row in self._ai_toggle_audit_rows if row.get("expected_on") is True]
        if not expected_rows:
            logger.info(
                "AI 활용 설정 사전 점검: 기대 ON 대상 없음 (rows=%s)",
                len(self._ai_toggle_audit_rows),
            )
            self._ai_toggle_summary["prepublish"] = {
                "expected_on": 0,
                "verified_on": 0,
                "repaired": 0,
                "failed": 0,
            }
            self._persist_ai_toggle_report()
            return

        repaired = 0
        failed_paths: List[str] = []
        for row in expected_rows:
            if row.get("actual_on") is True:
                continue
            tokens = [str(token) for token in row.get("tokens", []) if str(token).strip()]
            focused = await self._focus_image_by_tokens(page, tokens)
            if not focused:
                failed_paths.append(str(row.get("image_path", "")))
                row["prepublish_retry"] = "focus_failed"
                continue
            await self._recover_from_smarteditor_image_mode(page, stage="prepublish-validate")
            await self._ensure_ai_usage_toggle_on(page)
            state = await self._read_selected_image_ai_toggle_state(page)
            row["prepublish_retry"] = "done"
            row["actual_on"] = bool(state["on"]) if state["found"] else False
            if row["actual_on"] is True:
                repaired += 1
            else:
                failed_paths.append(str(row.get("image_path", "")))

        expected_true_count = len(expected_rows)
        verified_count = sum(1 for row in expected_rows if row.get("actual_on") is True)
        logger.info(
            "AI 활용 설정 사전 점검 요약: expected_on=%s verified_on=%s repaired=%s failed=%s",
            expected_true_count,
            verified_count,
            repaired,
            len(failed_paths),
        )
        self._ai_toggle_summary["prepublish"] = {
            "expected_on": expected_true_count,
            "verified_on": verified_count,
            "repaired": repaired,
            "failed": len(failed_paths),
        }
        if failed_paths:
            logger.warning("AI 활용 설정 사전 점검 미해결 대상: %s", ", ".join(failed_paths))
            await self._save_screenshot(page, "ai_toggle_prepublish_failed")
            self._notify_ai_toggle_alert_background(
                "🚨 [AI 토글 사전검증 실패]",
                [f"- unresolved: {path}" for path in failed_paths],
            )
        self._persist_ai_toggle_report()

    def _extract_log_no_from_post_url(self, post_url: str) -> str:
        """발행 URL에서 logNo를 추출한다."""
        try:
            parsed = urlparse(str(post_url))
            query = parse_qs(parsed.query)
            log_no = query.get("logNo", [""])[0]
            if str(log_no).strip():
                return str(log_no).strip()
            path_parts = [part for part in parsed.path.split("/") if part]
            if path_parts and path_parts[-1].isdigit():
                return path_parts[-1]
        except Exception:
            return ""
        return ""

    def _build_update_url_from_post_url(self, post_url: str) -> str:
        """발행 URL 기반으로 수정 페이지 URL을 구성한다."""
        log_no = self._extract_log_no_from_post_url(post_url)
        if not log_no:
            return ""
        return f"https://blog.naver.com/{self.blog_id}?Redirect=Update&logNo={log_no}"

    async def _run_ai_toggle_postpublish_verification(self, post_url: str) -> None:
        """발행 후 수정 페이지에서 이미지별 AI 토글 상태를 재검증한다."""
        expected_count = sum(1 for row in self._ai_toggle_audit_rows if row.get("expected_on") is True)
        if not self._ai_toggle_post_verify:
            self._ai_toggle_summary["postverify"] = {
                "expected_on": expected_count,
                "passed": -1,
                "failed": 0,
                "skipped": "disabled",
            }
            return
        if not self._ai_toggle_audit_rows:
            self._ai_toggle_summary["postverify"] = {
                "expected_on": 0,
                "passed": 0,
                "failed": 0,
                "skipped": "no_rows",
            }
            return
        if self._context is None:
            self._ai_toggle_summary["postverify"] = {
                "expected_on": expected_count,
                "passed": 0,
                "failed": -1,
                "skipped": "no_context",
            }
            return

        update_url = self._build_update_url_from_post_url(post_url)
        if not update_url:
            logger.warning("AI 활용 설정 사후검증 생략: update URL 구성 실패 (%s)", post_url)
            self._ai_toggle_summary["postverify"] = {
                "expected_on": expected_count,
                "passed": 0,
                "failed": -1,
                "skipped": "invalid_update_url",
            }
            return

        verify_page = None
        try:
            verify_page = await self._context.new_page()
            await verify_page.goto(update_url, wait_until="domcontentloaded", timeout=45_000)
            await asyncio.sleep(2.0)

            target_frame = verify_page.frame(name="mainFrame")
            if target_frame is None:
                for frame in verify_page.frames:
                    if "PostUpdateForm.naver" in str(frame.url):
                        target_frame = frame
                        break
            frame = target_frame if target_frame is not None else verify_page

            failures: List[str] = []
            for row in self._ai_toggle_audit_rows:
                if row.get("expected_on") is not True:
                    continue
                tokens = [str(token) for token in row.get("tokens", []) if str(token).strip()]
                payload = await frame.evaluate(
                    """
                    ({ tokens }) => {
                      const components = Array.from(
                        document.querySelectorAll(
                          ".se-section-image, .se-component.se-image, [class*='se-section-image'], [class*='se-component-image'], [class*='se-image']"
                        )
                      ).filter((node) => node.querySelector("img[src]"));
                      const normalizedTokens = (tokens || []).map((token) => String(token || "").toLowerCase()).filter(Boolean);
                      if (components.length === 0) return { found: false, on: false, src: "", buttonClass: "" };

                      let target = components.find((section) => {
                        const img = section.querySelector("img[src]");
                        if (!img) return false;
                        const src = String(img.getAttribute("src") || "").toLowerCase();
                        return normalizedTokens.some((token) => src.includes(token));
                      });
                      if (!target) target = components[components.length - 1];

                      const img = target.querySelector("img[src]");
                      if (img) {
                        img.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
                        const events = ["pointerdown", "mousedown", "mouseup", "click"];
                        for (const type of events) {
                          img.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, composed: true }));
                        }
                      }

                      const button = target.querySelector(
                        ".se-set-ai-mark-button-toggle, [class*='ai-mark-button-toggle'], .se-set-ai-mark-button:not([class*='wrapper']), [class*='ai-mark-button']:not([class*='wrapper'])"
                      );
                      if (!button) {
                        return {
                          found: false,
                          on: false,
                          src: String((img && img.getAttribute("src")) || ""),
                          buttonClass: "",
                        };
                      }
                      const buttonClass = String(button.className || "");
                      const relatedNodes = [];
                      if (button) relatedNodes.push(button);
                      const wrapper = button.closest(".se-set-ai-mark-button-wrapper, [class*='ai-mark-button-wrapper']");
                      if (wrapper) relatedNodes.push(wrapper);
                      const markNode = wrapper
                        ? wrapper.querySelector(".se-set-ai-mark-button:not([class*='wrapper']), [class*='ai-mark-button']:not([class*='wrapper'])")
                        : button.closest(".se-set-ai-mark-button, [class*='ai-mark-button']");
                      if (markNode) relatedNodes.push(markNode);
                      const toggleNode = wrapper
                        ? wrapper.querySelector(".se-set-ai-mark-button-toggle, [class*='ai-mark-button-toggle'], input[type='checkbox'], [role='switch'], [class*='toggle']")
                        : (
                          button.querySelector("input[type='checkbox'], [role='switch'], [class*='toggle']")
                          || button.closest(".se-set-ai-mark-button-toggle, [class*='ai-mark-button-toggle']")
                        );
                      if (toggleNode) relatedNodes.push(toggleNode);
                      const isNodeOn = (node) => {
                        if (!node) return false;
                        const cls = ` ${String(node.className || "").toLowerCase()} `;
                        if (
                          cls.includes(" se-is-selected ")
                          || cls.includes(" is-selected ")
                          || cls.includes(" is-on ")
                          || cls.includes(" se-is-on ")
                          || cls.includes(" on ")
                          || cls.includes(" active ")
                          || cls.includes(" checked ")
                          || cls.includes(" enabled ")
                        ) {
                          return true;
                        }
                        const attrs = [
                          String(node.getAttribute("aria-checked") || "").toLowerCase(),
                          String(node.getAttribute("aria-pressed") || "").toLowerCase(),
                          String(node.getAttribute("data-active") || "").toLowerCase(),
                        ];
                        if (attrs.includes("true")) return true;
                        if (typeof node.checked === "boolean" && node.checked === true) return true;
                        const nested = node.querySelector && node.querySelector("input[type='checkbox']");
                        if (nested && typeof nested.checked === "boolean" && nested.checked === true) return true;
                        return false;
                      };
                      const on = relatedNodes.some((node) => isNodeOn(node));
                      return {
                        found: true,
                        on,
                        src: String((img && img.getAttribute("src")) || ""),
                        buttonClass,
                        wrapperClass: String((wrapper && wrapper.className) || ""),
                        markClass: String((markNode && markNode.className) || ""),
                        toggleClass: String((toggleNode && toggleNode.className) || ""),
                        buttonAriaChecked: String(button.getAttribute("aria-checked") || ""),
                        buttonAriaPressed: String(button.getAttribute("aria-pressed") || ""),
                        buttonDataActive: String(button.getAttribute("data-active") || ""),
                        buttonChecked: typeof button.checked === "boolean" ? button.checked : null,
                        toggleAriaChecked: String((toggleNode && toggleNode.getAttribute ? toggleNode.getAttribute("aria-checked") : "") || ""),
                        toggleAriaPressed: String((toggleNode && toggleNode.getAttribute ? toggleNode.getAttribute("aria-pressed") : "") || ""),
                        toggleDataActive: String((toggleNode && toggleNode.getAttribute ? toggleNode.getAttribute("data-active") : "") || ""),
                        toggleChecked: typeof (toggleNode && toggleNode.checked) === "boolean" ? toggleNode.checked : null,
                        wrapperAriaChecked: String((wrapper && wrapper.getAttribute ? wrapper.getAttribute("aria-checked") : "") || ""),
                        wrapperAriaPressed: String((wrapper && wrapper.getAttribute ? wrapper.getAttribute("aria-pressed") : "") || ""),
                        wrapperDataActive: String((wrapper && wrapper.getAttribute ? wrapper.getAttribute("data-active") : "") || ""),
                        wrapperChecked: typeof (wrapper && wrapper.checked) === "boolean" ? wrapper.checked : null,
                      };
                    }
                    """,
                    {"tokens": tokens},
                )
                found = bool(payload.get("found")) if isinstance(payload, dict) else False
                on = self._is_ai_toggle_on_snapshot(payload) if isinstance(payload, dict) and found else False
                row["post_verify_found"] = found
                row["post_verify_on"] = on
                if not on:
                    failures.append(str(row.get("image_path", "")))
                logger.info(
                    "AI 활용 설정 사후검증: path=%s expected_on=%s actual_on=%s found=%s kind=%s provider=%s",
                    row.get("image_path"),
                    row.get("expected_on"),
                    on,
                    found,
                    row.get("source_kind"),
                    row.get("provider"),
                )

            passed_count = sum(1 for row in self._ai_toggle_audit_rows if row.get("post_verify_on") is True)
            logger.info(
                "AI 활용 설정 사후검증 요약: expected_on=%s passed=%s failed=%s",
                expected_count,
                passed_count,
                len(failures),
            )
            self._ai_toggle_summary["postverify"] = {
                "expected_on": expected_count,
                "passed": passed_count,
                "failed": len(failures),
            }
            if failures:
                logger.warning("AI 활용 설정 사후검증 실패 대상: %s", ", ".join(failures))
                await self._save_screenshot(verify_page, "ai_toggle_postverify_failed")
                self._notify_ai_toggle_alert_background(
                    "🚨 [AI 토글 사후검증 실패]",
                    [f"- unresolved: {path}" for path in failures],
                )
        except Exception as exc:
            logger.warning("AI 활용 설정 사후검증 실패: %s", exc)
            self._ai_toggle_summary["postverify"] = {
                "expected_on": sum(1 for row in self._ai_toggle_audit_rows if row.get("expected_on") is True),
                "passed": 0,
                "failed": -1,
            }
            self._notify_ai_toggle_alert_background(
                "🚨 [AI 토글 사후검증 예외]",
                [f"- error: {type(exc).__name__}", f"- detail: {str(exc)[:200]}"],
            )
        finally:
            self._persist_ai_toggle_report(post_url)
            if verify_page is not None:
                try:
                    await verify_page.close()
                except Exception:
                    pass

    async def _focus_latest_uploaded_image(self, page) -> None:
        """가장 최근 업로드 이미지에 포커스를 맞춘다."""
        await self._recover_from_smarteditor_image_mode(page, stage="focus-start")
        selectors = [
            ".se-section-image img[src]",
            "[class*='se-section-image'] img[src]",
            ".se-component-content img[src]",
            ".se-main-container img[src]",
            ".se-content img[src]",
            "[class*='se-cover'] img[src]",
        ]
        for selector in selectors:
            try:
                nodes = page.locator(selector)
                count = await nodes.count()
                if count <= 0:
                    continue
                latest = nodes.nth(max(0, count - 1))
                await latest.scroll_into_view_if_needed(timeout=1500)
                await self._human_delay(200, 400)
                # 마우스 호버를 우선적으로 수행하여 AI 버튼 토글 노출을 트리거
                await latest.hover(timeout=2000)
                await self._human_delay(300, 500)
                await latest.click(timeout=2000)
                await self._human_delay(120, 220)
                # 더블클릭은 이미지 편집(검은 SmartEditor) 진입을 유발할 수 있어 금지한다.
                await self._recover_from_smarteditor_image_mode(page, stage="focus-after-click")
                await asyncio.sleep(0.3)
                return
            except Exception:
                continue

    async def _recover_from_smarteditor_image_mode(self, page, stage: str) -> None:
        """검은 배경 SmartEditor 이미지 편집 모드에 들어가면 즉시 빠져나온다."""
        try:
            if not await self._is_smarteditor_image_mode(page):
                return

            logger.warning("SmartEditor 이미지 편집 모드 감지(%s): 복구 시도", stage)

            # 1) ESC 우선 시도
            for _ in range(2):
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
                await asyncio.sleep(0.35)
                if not await self._is_smarteditor_image_mode(page):
                    return

            # 2) 종료/취소/뒤로 계열 버튼 클릭 시도
            exit_selectors = [
                "button[aria-label*='닫기']",
                "button[aria-label*='취소']",
                "button[aria-label*='뒤로']",
                "button:has-text('닫기')",
                "button:has-text('취소')",
                "button:has-text('뒤로')",
            ]
            for selector in exit_selectors:
                try:
                    nodes = page.locator(selector)
                    count = await nodes.count()
                    if count == 0:
                        continue
                    for index in range(min(count, 3)):
                        node = nodes.nth(index)
                        if not await node.is_visible():
                            continue
                        await self._activate_toggle_target(page, node, aggressive=True)
                        await asyncio.sleep(0.35)
                        if not await self._is_smarteditor_image_mode(page):
                            return
                except Exception:
                    continue

            # 3) 최후 수단: 브라우저 뒤로가기
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=4000)
                await asyncio.sleep(0.5)
            except Exception:
                pass

            if await self._is_smarteditor_image_mode(page):
                await self._save_screenshot(page, "smarteditor_image_mode_stuck")
        except Exception:
            return

    async def _is_smarteditor_image_mode(self, page) -> bool:
        """검은 배경 SmartEditor 이미지 편집 모드 여부를 감지한다."""
        # 텍스트 기반 빠른 감지
        try:
            label = page.locator("text=SmartEditor").first
            if await label.count() > 0 and await label.is_visible():
                payload = await page.evaluate(
                    """
                    () => {
                      const el = Array.from(document.querySelectorAll("*"))
                        .find((node) => String(node.textContent || "").trim() === "SmartEditor");
                      if (!el) return false;
                      const style = window.getComputedStyle(el);
                      if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
                      const bg = String(window.getComputedStyle(document.body).backgroundColor || "");
                      const darkBody = /rgb\\((\\s*0\\s*,\\s*0\\s*,\\s*0\\s*|\\s*1\\s*,\\s*1\\s*,\\s*1\\s*)\\)/.test(bg) || bg === "rgba(0, 0, 0, 1)";
                      return darkBody || true;
                    }
                    """
                )
                if bool(payload):
                    return True
        except Exception:
            pass

        # 폴백: 제목 입력 셀렉터가 전혀 없고 화면이 어두운 경우
        try:
            if await self._has_visible_title_input(page):
                return False
            payload = await page.evaluate(
                """
                () => {
                  const bg = String(window.getComputedStyle(document.body).backgroundColor || "");
                  return /rgb\\((\\s*0\\s*,\\s*0\\s*,\\s*0\\s*|\\s*1\\s*,\\s*1\\s*,\\s*1\\s*)\\)/.test(bg) || bg === "rgba(0, 0, 0, 1)";
                }
                """
            )
            return bool(payload)
        except Exception:
            return False

    async def _ensure_ai_usage_toggle_on(self, page) -> bool:
        """AI 활용 설정 토글을 찾아 ON 상태로 만든다."""
        # 선택된 이미지 컴포넌트 내부 토글을 우선 시도한다.
        try:
            local_result = await self._toggle_ai_usage_from_selected_component(page)
            if local_result:
                return True
        except Exception:
            pass

        toggle_selectors = [
            ".se-section-image .se-set-ai-mark-button",
            "[class*='se-section-image'] [class*='ai-mark-button']",
            ".se-component.se-image [class*='ai-mark-button']",
            ".se-section-image [role='switch'][aria-label*='AI']",
            ".se-section-image input[type='checkbox'][aria-label*='AI']",
        ]
        badge_selectors = [
            "span.se-component-ai-badge",
            "[class*='ai-badge']",
            "[class*='aiBadge']",
        ]

        await self._wait_for_click_interceptor_clear(page, timeout_ms=1400)

        # 배지 요소가 있으면 먼저 클릭해 우측 설정 패널 노출을 유도한다.
        for badge_selector in badge_selectors:
            try:
                badge = page.locator(badge_selector).first
                if await badge.count() > 0:
                    await badge.hover(timeout=1200)
                    await self._human_delay(180, 280)
                    await self._activate_toggle_target(page, badge)
                    await asyncio.sleep(0.25)
                    if await self._is_any_ai_toggle_on(page, toggle_selectors):
                        return True
                    await self._activate_toggle_target(page, badge, aggressive=True)
                    await asyncio.sleep(0.25)
                    # 토글 자체가 이미 ON이면 빠르게 종료한다.
                    if await self._is_any_ai_toggle_on(page, toggle_selectors):
                        return True
                    await asyncio.sleep(0.2)
                    break
            except Exception:
                continue

        for selector in toggle_selectors:
            try:
                toggles = page.locator(selector)
                count = await toggles.count()
                if count == 0:
                    continue
                max_targets = min(count, 3)

                for index in range(max_targets):
                    toggle = toggles.nth(index)
                    if not await toggle.is_visible():
                        continue

                    is_on = await self._is_toggle_on(toggle)
                    if is_on:
                        return True

                    await self._human_delay(180, 260)
                    await self._activate_toggle_target(page, toggle)
                    await asyncio.sleep(0.45)
                    if await self._is_toggle_on(toggle):
                        return True

                    # 기본 클릭에 반응이 없을 때 강공 모드로 1회 더 시도한다.
                    await self._activate_toggle_target(page, toggle, aggressive=True)
                    await asyncio.sleep(0.35)
                    if await self._is_toggle_on(toggle):
                        return True
                    if await self._is_any_ai_toggle_on(page, toggle_selectors):
                        return True
            except Exception:
                continue

        # 셀렉터 실패 시 텍스트 기반 DOM 탐색을 JS로 폴백한다.
        try:
            payload = await page.evaluate(
                """
                () => {
                  const isOn = (node) => {
                    if (!node) return false;
                    const attrs = [
                      String(node.getAttribute("aria-pressed") || "").toLowerCase(),
                      String(node.getAttribute("aria-checked") || "").toLowerCase(),
                      String(node.getAttribute("data-checked") || "").toLowerCase(),
                    ];
                    if (attrs.includes("true")) return true;
                    const className = String(node.className || "").toLowerCase();
                    if (/(on|active|checked|selected|enabled)/.test(className)) return true;
                    return false;
                  };
                  const findClickable = (root) => {
                    if (!root) return null;
                    return root.closest("button,[role='button'],label,[class*='toggle'],[class*='switch']");
                  };
                  const findInScope = (root) => {
                    if (!root) return null;
                    const scope = root.closest("label,li,div,section,aside,[class*='panel'],[class*='popover']");
                    if (!scope) return null;
                    return scope.querySelector("[role='switch'],input[type='checkbox'],button,[role='button'],label,[class*='toggle'],[class*='switch']");
                  };

                  const nodes = Array.from(document.querySelectorAll("button,span,div,label,a"));
                  const labelNode = nodes.find((el) => /AI\\s*활용\\s*설정/.test((el.textContent || "").trim()));
                  const badgeNode = document.querySelector("span.se-component-ai-badge,[class*='ai-badge'],[class*='aiBadge']");
                  const candidates = [
                    findInScope(labelNode),
                    findClickable(labelNode),
                    findInScope(badgeNode),
                    findClickable(badgeNode),
                  ].filter(Boolean);
                  if (candidates.length === 0) return {found: false, on: false};

                  const clicked = new Set();
                  for (const candidate of candidates) {
                    if (isOn(candidate)) return {found: true, on: true};
                    if (clicked.has(candidate)) continue;
                    clicked.add(candidate);
                    if (candidate.tagName === "INPUT" && String(candidate.type).toLowerCase() === "checkbox") {
                      candidate.checked = true;
                      candidate.dispatchEvent(new Event("input", { bubbles: true }));
                      candidate.dispatchEvent(new Event("change", { bubbles: true }));
                    } else {
                      candidate.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, cancelable: true, composed: true }));
                      candidate.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, composed: true }));
                      candidate.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, composed: true }));
                      candidate.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, composed: true }));
                    }
                    if (isOn(candidate)) return {found: true, on: true};
                  }
                  return {found: true, on: false};
                }
                """
            )
            if bool(payload and payload.get("found") and payload.get("on")):
                return True
        except Exception:
            pass

        # 최후 폴백: 선택 이미지 우하단 토글 핫스팟 좌표 클릭
        # 0.5s 대기: 에디터 DOM 렌더링이 완료된 후 좌표 계산을 정확히 하기 위해
        await asyncio.sleep(0.5)
        for _ in range(2):
            try:
                if await self._click_selected_image_ai_hotspot(page):
                    state = await self._read_selected_image_ai_toggle_state(page)
                    if bool(state.get("found")) and bool(state.get("on")):
                        return True
            except Exception:
                continue
            await asyncio.sleep(0.25)
        return False

    async def _click_selected_image_ai_hotspot(self, page) -> bool:
        """선택 이미지의 우하단 AI 토글 예상 위치를 좌표 클릭한다."""
        try:
            payload = await page.evaluate(
                """
                () => {
                  const components = Array.from(
                    document.querySelectorAll(
                      ".se-section-image, .se-component.se-image, [class*='se-section-image'], [class*='se-component-image'], [class*='se-image']"
                    )
                  ).filter((node) => node.querySelector("img[src]"));
                  if (components.length === 0) return { ok: false, x: 0, y: 0 };
                  const selected = components.find((node) => /selected|focus|active|activated/.test(String(node.className || "").toLowerCase()));
                  const section = selected || components[components.length - 1];
                  const rect = section.getBoundingClientRect();
                  if (!rect || rect.width <= 0 || rect.height <= 0) return { ok: false, x: 0, y: 0 };
                  const x = Math.max(4, Math.floor(rect.right - Math.min(28, rect.width * 0.08)));
                  const y = Math.max(4, Math.floor(rect.bottom - Math.min(16, rect.height * 0.08)));
                  return { ok: true, x, y };
                }
                """
            )
            if not isinstance(payload, dict) or not payload.get("ok"):
                return False
            await page.mouse.click(float(payload["x"]), float(payload["y"]))
            return True
        except Exception:
            return False

    async def _toggle_ai_usage_from_selected_component(self, page) -> bool:
        """선택된 이미지 카드 내부의 AI 활용 토글을 직접 ON으로 전환한다."""
        try:
            payload = await page.evaluate(
                """
                () => {
                  const isOn = (node) => {
                    if (!node) return false;
                    const className = ` ${String(node.className || "").toLowerCase()} `;
                    if (
                      className.includes(" se-is-selected ")
                      || className.includes(" is-selected ")
                      || className.includes(" is-on ")
                      || className.includes(" on ")
                      || className.includes(" active ")
                    ) {
                      return true;
                    }
                    const attrs = [
                      String(node.getAttribute("aria-checked") || "").toLowerCase(),
                      String(node.getAttribute("aria-pressed") || "").toLowerCase(),
                      String(node.getAttribute("data-checked") || "").toLowerCase(),
                    ];
                    if (attrs.includes("true")) return true;
                    const input = node.matches && node.matches("input[type='checkbox']")
                      ? node
                      : (node.querySelector ? node.querySelector("input[type='checkbox']") : null);
                    return Boolean(input && input.checked);
                  };
                  const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === "none" || style.visibility === "hidden") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };
                  const dispatchMouse = (el) => {
                    const events = ["pointerdown", "mousedown", "mouseup", "click"];
                    for (const type of events) {
                      el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, composed: true }));
                    }
                  };

                  const imageComponents = Array.from(
                    document.querySelectorAll(
                      ".se-section-image, .se-component.se-image, [class*='se-section-image'], [class*='se-component-image'], [class*='se-image']"
                    )
                  ).filter((el) => el.querySelector("img[src]"));
                  if (imageComponents.length === 0) return { found: false, on: false };

                  // 선택된 컴포넌트를 우선 사용하고, 없으면 최신 이미지를 사용한다.
                  const selected = imageComponents.find((el) => /selected|focus|active/.test(String(el.className || "").toLowerCase()));
                  const component = selected || imageComponents[imageComponents.length - 1];
                  const markButton = component.querySelector(
                    ".se-set-ai-mark-button:not([class*='wrapper']), [class*='ai-mark-button']:not([class*='wrapper'])"
                  );
                  const toggleButton = component.querySelector(".se-set-ai-mark-button-toggle, [class*='ai-mark-button-toggle']");
                  const wrapper = component.querySelector(".se-set-ai-mark-button-wrapper, [class*='ai-mark-button-wrapper']");
                  if (!markButton && !toggleButton && !wrapper) {
                    return { found: false, on: false };
                  }
                  if (isOn(markButton)) {
                    return { found: true, on: true };
                  }

                  const clickTargets = [toggleButton, wrapper, markButton].filter(Boolean);
                  for (const target of clickTargets) {
                    if (!isVisible(target)) continue;
                    dispatchMouse(target);
                    if (isOn(markButton) || isOn(toggleButton) || isOn(wrapper) || isOn(target)) {
                      return { found: true, on: true };
                    }
                  }

                  return { found: true, on: Boolean(isOn(markButton) || isOn(toggleButton) || isOn(wrapper)) };
                }
                """
            )
            return bool(payload and payload.get("found") and payload.get("on"))
        except Exception:
            return False

    async def _is_any_ai_toggle_on(self, page, selectors: Optional[List[str]] = None) -> bool:
        """페이지 내 AI 토글이 하나라도 ON인지 빠르게 판별한다."""
        candidates = selectors or [
            "[role='switch']",
            "input[type='checkbox'][aria-label*='AI']",
            "input[type='checkbox'][name*='ai']",
            "button:has-text('AI 활용 설정')",
        ]
        for selector in candidates:
            try:
                nodes = page.locator(selector)
                count = await nodes.count()
                if count <= 0:
                    continue
                for index in range(min(count, 3)):
                    if await self._is_toggle_on(nodes.nth(index)):
                        return True
            except Exception:
                continue
        return False

    async def _wait_for_click_interceptor_clear(self, page, timeout_ms: int = 1200) -> None:
        """오버레이/로딩 마스크가 클릭을 가로채는 짧은 구간을 회피한다."""
        timeout_sec = max(0.2, float(timeout_ms) / 1000.0)
        deadline = time.perf_counter() + timeout_sec
        while time.perf_counter() < deadline:
            try:
                blocked = await page.evaluate(
                    """
                    () => {
                      const selectors = [
                        "[role='dialog']",
                        "[aria-busy='true']",
                        "[class*='loading']",
                        "[class*='spinner']",
                        "[class*='dimmed']",
                        "[class*='modal']",
                        "[class*='popup']",
                        "[class*='toast']",
                        "[class*='layer']",
                      ];
                      const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                      };
                      for (const selector of selectors) {
                        const nodes = Array.from(document.querySelectorAll(selector));
                        for (const node of nodes) {
                          if (!isVisible(node)) continue;
                          const style = window.getComputedStyle(node);
                          if (style.pointerEvents === "none") continue;
                          return true;
                        }
                      }
                      return false;
                    }
                    """
                )
                if not bool(blocked):
                    return
                if not self._overlay_dismiss_in_progress:
                    await self._dismiss_blocking_layer_popup(page, max_rounds=1)
            except Exception:
                return
            await asyncio.sleep(0.1)

    async def _activate_toggle_target(self, page, target, aggressive: bool = False) -> None:
        """일반 클릭이 막힐 때를 대비한 다단계 클릭 우회."""
        await self._wait_for_click_interceptor_clear(page, timeout_ms=900)

        if not aggressive:
            # 1) 표준 클릭
            try:
                await target.click(timeout=1500)
                return
            except Exception:
                pass

            # 2) 강제 클릭
            try:
                await target.click(timeout=1500, force=True)
                return
            except Exception:
                pass

        # 3) 이벤트 강제 디스패치
        try:
            await target.dispatch_event("click")
            return
        except Exception:
            pass

        # 4) DOM 이벤트 체인 직접 호출
        try:
            await target.evaluate(
                """
                (el) => {
                  if (!el) return;
                  const events = ["pointerdown", "mousedown", "mouseup", "click"];
                  for (const type of events) {
                    el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, composed: true }));
                  }
                }
                """
            )
            return
        except Exception:
            pass

        # 5) 포커스 후 Space/Enter 토글
        try:
            await target.focus()
            await asyncio.sleep(0.1)
            await page.keyboard.press("Space")
            await asyncio.sleep(0.12)
            await page.keyboard.press("Enter")
            return
        except Exception:
            pass

        # 6) Bounding box 중심/오프셋 좌표 클릭
        try:
            box = await target.bounding_box()
            if box and box["width"] > 0 and box["height"] > 0:
                click_points = [(0.5, 0.5)]
                if aggressive:
                    click_points.extend(
                        [
                            (0.25, 0.5),
                            (0.75, 0.5),
                            (0.5, 0.25),
                            (0.5, 0.75),
                        ]
                    )
                for rx, ry in click_points:
                    x = box["x"] + (box["width"] * rx)
                    y = box["y"] + (box["height"] * ry)
                    await page.mouse.click(x, y)
                    await asyncio.sleep(0.08)
                return
        except Exception:
            pass

    async def _is_toggle_on(self, locator) -> bool:
        """토글 요소의 ON 상태를 판별한다."""
        try:
            payload = await locator.evaluate(
                """
                (node) => {
                  const extractAttrs = (el) => {
                    if (!el) return [];
                    return [
                      String(el.getAttribute("aria-pressed") || "").toLowerCase(),
                      String(el.getAttribute("aria-checked") || "").toLowerCase(),
                      String(el.getAttribute("data-checked") || "").toLowerCase(),
                    ];
                  };
                  const hasOnClass = (el) => {
                    if (!el) return false;
                    const className = String(el.className || "").toLowerCase();
                    return /(on|active|checked|selected|enabled|is-on)/.test(className);
                  };
                  const hasCheckedInput = (el) => {
                    if (!el) return false;
                    if (el.tagName === "INPUT" && String(el.type).toLowerCase() === "checkbox") {
                      return Boolean(el.checked);
                    }
                    const input = el.querySelector("input[type='checkbox']");
                    return Boolean(input && input.checked);
                  };
                  const isOn = (el) => {
                    if (!el) return false;
                    const attrs = extractAttrs(el);
                    return attrs.includes("true") || hasOnClass(el) || hasCheckedInput(el);
                  };

                  if (!node) return false;
                  if (isOn(node)) return true;

                  const scope = node.closest("label,button,[role='switch'],[class*='switch'],[class*='toggle'],[class*='ai']");
                  if (scope && isOn(scope)) return true;

                  if (scope) {
                    const nestedCandidates = scope.querySelectorAll("[role='switch'],input[type='checkbox'],button,[class*='switch'],[class*='toggle']");
                    for (const candidate of Array.from(nestedCandidates).slice(0, 5)) {
                      if (isOn(candidate)) return true;
                    }
                  }
                  return false;
                }
                """
            )
            return bool(payload)
        except Exception:
            return False

    async def _extract_post_url(self, page) -> str:
        """발행 완료 후 URL 추출 (폴링 방식)"""
        # 발행 완료 URL 패턴: blog.naver.com/{id}/{post_no}
        # 또는 blog.naver.com/PostView.naver?blogId={id}&logNo={post_no}

        start_time = time.time()
        timeout_sec = 25  # 25초로 증가

        while time.time() - start_time < timeout_sec:
            try:
                current_url = page.url
            except Exception:
                await asyncio.sleep(1)
                continue

            # 글쓰기 페이지(postwrite)를 벗어났는지 확인
            if "postwrite" not in current_url and self.blog_id in current_url:
                logger.info(f"URL 변경 감지: {current_url}")
                return current_url

            # URL이 안 바뀌었더라도, 화면에 발행 완료 지표가 있으면 성공으로 간주
            try:
                success_indicators = [
                    ".se-viewer-layout",  # 스마트 에디터 뷰어
                    "a:has-text('수정하기')",
                    "button:has-text('수정')",
                    "button:has-text('삭제')",
                    "button:has-text('URL 복사')",
                    ".blog-view",  # 블로그 뷰어
                    "[class*='viewer']",  # 뷰어 관련 클래스
                    "a[href*='PostView']",  # 포스트 뷰 링크
                ]
                for sel in success_indicators:
                    try:
                        if await page.locator(sel).count() > 0:
                            # URL 다시 확인
                            current_url = page.url
                            logger.info(f"발행 성공 지표 발견 ({sel}), URL: {current_url}")
                            return current_url
                    except Exception:
                        continue
            except Exception:
                pass

            await asyncio.sleep(1)

        # timeout 후에도 현재 URL 반환 (발행은 성공했을 수 있음)
        try:
            final_url = page.url
        except Exception:
            final_url = ""
        logger.warning(f"URL 추출 timeout, 현재 URL 반환: {final_url}")
        return final_url

    async def _detect_captcha(self, page) -> bool:
        """캡차 감지"""
        selectors = [
            "#captcha",
            ".captcha",
            '[class*="captcha"]',
            'iframe[src*="captcha"]',
            'iframe[src*="recaptcha"]',
        ]
        for selector in selectors:
            if await page.query_selector(selector):
                return True
        return False

    async def _save_screenshot(self, page, name: str):
        """디버그 스크린샷 저장"""
        screenshots_dir = Path("data/screenshots")
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        path = screenshots_dir / f"{name}_{int(time.time())}.png"
        try:
            await page.screenshot(path=str(path))
            logger.info(f"Screenshot saved: {path}")
            self._prune_old_debug_files(
                screenshots_dir,
                "*.png",
                keep=self._screenshot_retention_max,
            )
        except Exception:
            pass

    async def _type_naturally(self, page, text: str):
        """인간적인 타이핑"""
        for char in text:
            await page.keyboard.type(char)
            await asyncio.sleep(random.uniform(0.04, 0.12))

    async def _human_delay(self, min_ms: int = 500, max_ms: int = 2000):
        """인간적인 딜레이"""
        await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)

    def _random_ua(self) -> str:
        """랜덤 User-Agent"""
        agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
        ]
        return random.choice(agents)

    def _session_state_path(self) -> Path:
        return self.session_dir / "state.json"

    def _classify_error(self, error: Exception) -> str:
        """에러 코드 분류"""
        msg = str(error).lower()
        if "timeout" in msg:
            return "NETWORK_TIMEOUT"
        if "element" in msg or "selector" in msg or "locator" in msg:
            return "ELEMENT_NOT_FOUND"
        if "rate" in msg or "429" in msg:
            return "RATE_LIMITED"
        if "content" in msg or "reject" in msg:
            return "CONTENT_REJECTED"
        return "UNKNOWN"

    async def _fill_publish_settings(
        self,
        page,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> None:
        """발행 설정 팝업에서 태그와 카테고리를 입력한다.

        네이버 스마트 에디터 발행 팝업 내 태그 입력 필드에 태그를 하나씩 입력한다.
        실패해도 발행 자체는 계속 진행한다.
        """
        # 팝업이 완전히 로드될 때까지 대기
        await self._human_delay(500, 1000)

        # ── 카테고리 선택 ─────────────────────────────────────────────
        if category:
            category_selectors = [
                # 네이버 스마트 에디터 발행 팝업 카테고리
                ".layer_result .se-category-select",
                ".layer_result select[name*='category']",
                ".publish_setting__wrap select",
                ".category_select select",
                "[class*='category'] select",
                # data 속성 기반
                "[data-testid*='category'] select",
                "[data-name='category'] select",
                # 라벨 기반 (근접 선택)
                "label:has-text('카테고리') + select",
                "label:has-text('카테고리') ~ select",
                # 클래스 패턴
                ".publish_layer select",
                ".blog_category select",
            ]
            for sel in category_selectors:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.select_option(label=category)
                        logger.info("카테고리 설정: %s (selector: %s)", category, sel)
                        await self._human_delay(300, 600)
                        break
                except Exception:
                    continue

        # ── 태그 입력 ─────────────────────────────────────────────────
        if not tags:
            return

        # 네이버 발행 팝업의 태그 입력 셀렉터 (우선순위 순)
        tag_input_selectors = [
            # placeholder 기반 (가장 신뢰성 높음)
            "input[placeholder*='태그를 입력']",
            "input[placeholder*='태그 입력']",
            "input[placeholder*='태그']",
            # 발행 레이어 내 태그 영역
            ".layer_result input[placeholder*='태그']",
            ".layer_result .se-tag-input input",
            ".layer_result [class*='tag'] input",
            # 클래스명 패턴
            ".publish_setting__wrap input[placeholder*='태그']",
            ".tag_area input",
            ".tag_input input",
            "[class*='tag_input'] input",
            "[class*='tagInput'] input",
            # data 속성 기반
            "[data-testid*='tag'] input",
            "[data-name='tag'] input",
            "input[name*='tag']",
            # 일반적인 텍스트 입력 (최후 수단)
            ".layer_result input[type='text']:not([readonly])",
        ]

        tag_input = None
        for sel in tag_input_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    # 실제로 보이는지 확인
                    if await el.is_visible():
                        tag_input = el
                        logger.info("태그 입력 필드 발견: %s", sel)
                        break
            except Exception:
                continue

        if tag_input is None:
            await self._save_screenshot(page, "tag_input_not_found")
            logger.warning("태그 입력 필드를 찾지 못함 - 태그 생략")
            return

        success_count = 0
        for tag in tags:
            tag = tag.strip()
            if not tag:
                continue
            try:
                await tag_input.click(timeout=3000)
                await self._human_delay(200, 400)
                await tag_input.fill("")
                await self._type_naturally(page, tag)
                await asyncio.sleep(0.3)
                # Enter로 태그 확정
                await page.keyboard.press("Enter")
                await self._human_delay(300, 600)
                success_count += 1
            except Exception as exc:
                logger.warning("태그 입력 실패 (tag=%s): %s", tag, exc)
                # 실패 시 입력 필드 재탐색
                try:
                    tag_input = page.locator("input[placeholder*='태그']").first
                except Exception:
                    pass
                continue

        logger.info("태그 입력 완료: %d/%d", success_count, len(tags))

    async def _cleanup(self):
        """리소스 정리 (context → browser → playwright 순서)"""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
