"""발행 후 시각 품질을 VLM으로 평가한다."""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import constants
from ..llm.openai_compat_client import OpenAICompatClient
from .vlm_prompt_adapter import build_vlm_prompt

logger = logging.getLogger(__name__)


@dataclass
class VisualEvalResult:
    """시각 품질 평가 결과."""

    total_score: int = 0
    layout: int = 0
    readability: int = 0
    image_quality: int = 0
    visual_consistency: int = 0
    overall_impression: int = 0
    suggestions: List[str] = field(default_factory=list)
    screenshot_path: str = ""
    provider_used: str = ""
    model_key: str = ""
    model_used: str = ""
    evaluated_at: str = ""
    estimated_cost_krw: float = 0.0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_score": int(self.total_score),
            "layout": int(self.layout),
            "readability": int(self.readability),
            "image_quality": int(self.image_quality),
            "visual_consistency": int(self.visual_consistency),
            "overall_impression": int(self.overall_impression),
            "suggestions": list(self.suggestions),
            "screenshot_path": str(self.screenshot_path),
            "provider_used": str(self.provider_used),
            "model_key": str(self.model_key),
            "model_used": str(self.model_used),
            "evaluated_at": str(self.evaluated_at),
            "estimated_cost_krw": float(self.estimated_cost_krw or 0.0),
            "error": str(self.error),
        }


class VisualQualityEvaluator:
    """발행된 블로그 포스트의 시각적 완성도를 VLM으로 평가한다."""

    def __init__(
        self,
        vlm_client: OpenAICompatClient,
        fallback_clients: Optional[List[OpenAICompatClient]] = None,
        circuit_breaker: Optional[Any] = None,
        score_bias_map: Optional[Dict[str, float]] = None,
        screenshot_dir: str = "data/screenshots/vlm",
        screenshot_retention_max: int = constants.VLM_SCREENSHOT_RETENTION_MAX,
    ) -> None:
        self.vlm_client = vlm_client
        self.fallback_clients = list(fallback_clients or [])
        self._clients: List[OpenAICompatClient] = [vlm_client, *self.fallback_clients]
        self._circuit_breaker = circuit_breaker
        self._score_bias_map = {
            str(key or "").strip().lower(): float(value or 0.0)
            for key, value in dict(score_bias_map or {}).items()
            if str(key or "").strip()
        }
        self.screenshot_dir = Path(screenshot_dir)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_retention_max = max(1, int(screenshot_retention_max or 50))

    async def evaluate(self, post_url: str, job_id: str) -> VisualEvalResult:
        """포스트 스크린샷을 캡처하고 VLM으로 점수를 산출한다."""
        evaluated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = VisualEvalResult(evaluated_at=evaluated_at, model_used=str(self.vlm_client.model))

        try:
            screenshot_path = await self._capture_screenshot(url=post_url, job_id=job_id)
            result.screenshot_path = screenshot_path
            resized_path, media_type = self._resize_if_needed(screenshot_path)
            image_base64 = base64.b64encode(Path(resized_path).read_bytes()).decode("utf-8")

            last_exc: Optional[Exception] = None
            for client in self._clients:
                provider = self._resolve_provider_name(client)
                model = str(getattr(client, "model", "") or "").strip()
                model_key = f"{provider}:{model}".lower()
                if self._is_circuit_open(model_key):
                    continue

                try:
                    response = await client.generate_vision_with_retry(
                        text_prompt=self._build_evaluation_prompt(provider=provider, model=model),
                        image_base64=image_base64,
                        image_media_type=media_type,
                        max_retries=3,
                        temperature=0.2,
                        max_tokens=900,
                    )
                    parsed = self._parse_eval_response(response.content)
                    parsed.screenshot_path = screenshot_path
                    parsed.provider_used = provider
                    parsed.model_key = model_key
                    parsed.model_used = str(response.model or model)
                    parsed.evaluated_at = evaluated_at
                    parsed.total_score = self._apply_score_bias(parsed.total_score, model_key)
                    self._record_success(model_key)
                    return parsed
                except Exception as exc:
                    last_exc = exc
                    self._record_failure(model_key)
                    continue

            if last_exc is not None:
                raise last_exc
            raise RuntimeError("no available vlm client")
        except Exception as exc:
            logger.warning("Visual evaluation failed: %s", exc)
            result.error = str(exc)
            return result

    async def _capture_screenshot(self, url: str, job_id: str) -> str:
        """Playwright로 공개 URL 방문 후 풀페이지 스크린샷 촬영."""
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            raise RuntimeError("playwright is not installed") from exc

        safe_job_id = re.sub(r"[^a-zA-Z0-9_-]", "_", str(job_id or "job"))
        screenshot_path = self.screenshot_dir / f"{safe_job_id}_{int(time.time())}.png"
        state_path = Path("data/sessions/naver/state.json")

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context_kwargs: Dict[str, Any] = {
                "viewport": {
                    "width": constants.VLM_SCREENSHOT_VIEWPORT_WIDTH,
                    "height": constants.VLM_SCREENSHOT_VIEWPORT_HEIGHT,
                }
            }
            # 비공개 글 접근 가능성을 대비해 기존 세션 쿠키를 재사용한다.
            if state_path.exists():
                context_kwargs["storage_state"] = str(state_path)

            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(int(constants.VLM_SCREENSHOT_WAIT_SEC * 1000))
                await page.screenshot(path=str(screenshot_path), full_page=True)
            finally:
                await context.close()
                await browser.close()

        self._prune_old_screenshots()
        return str(screenshot_path)

    def _build_evaluation_prompt(self, *, provider: str = "", model: str = "") -> str:
        """VLM 평가 프롬프트를 구성한다."""
        base_prompt = (
            "당신은 블로그 포스트의 시각적 완성도를 평가하는 전문가입니다.\n"
            "첨부된 블로그 포스트 스크린샷을 분석하고 아래 5개 항목을 채점하세요.\n\n"
            "평가 항목 (합계 100점)\n"
            "1. layout (0-20): 전체 레이아웃 구조, 여백, 섹션 구분\n"
            "2. readability (0-25): 텍스트 가독성, 폰트 크기, 줄 간격, 문단 길이\n"
            "3. image_quality (0-20): 이미지 해상도, 크기 적절성, 본문과의 조화\n"
            "4. visual_consistency (0-15): 색상 일관성, 디자인 톤, 브랜딩\n"
            "5. overall_impression (0-20): 전문성, 신뢰감, 첫인상\n\n"
            "반드시 유효한 JSON으로만 응답하세요.\n"
            "{\n"
            '  "layout": 0,\n'
            '  "readability": 0,\n'
            '  "image_quality": 0,\n'
            '  "visual_consistency": 0,\n'
            '  "overall_impression": 0,\n'
            '  "total_score": 0,\n'
            '  "suggestions": ["개선 제안 1", "개선 제안 2", "개선 제안 3"]\n'
            "}"
        )
        return build_vlm_prompt(provider=provider, model=model, base_prompt=base_prompt)

    def _parse_eval_response(self, raw: str) -> VisualEvalResult:
        """VLM JSON 응답을 파싱한다."""
        text = str(raw or "").strip()
        if not text:
            return VisualEvalResult(error="empty response")

        try:
            parsed = json.loads(text)
        except Exception:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                return VisualEvalResult(error="invalid json")
            try:
                parsed = json.loads(match.group(0))
            except Exception:
                return VisualEvalResult(error="invalid json")

        if not isinstance(parsed, dict):
            return VisualEvalResult(error="response is not object")

        layout = self._clamp_score(parsed.get("layout"), 0, 20)
        readability = self._clamp_score(parsed.get("readability"), 0, 25)
        image_quality = self._clamp_score(parsed.get("image_quality"), 0, 20)
        visual_consistency = self._clamp_score(parsed.get("visual_consistency"), 0, 15)
        overall_impression = self._clamp_score(parsed.get("overall_impression"), 0, 20)
        score_sum = layout + readability + image_quality + visual_consistency + overall_impression
        total_score = self._clamp_score(parsed.get("total_score"), 0, 100)
        if total_score <= 0:
            total_score = score_sum
        elif abs(total_score - score_sum) > 10:
            # 합계가 지나치게 어긋나면 계산값을 우선한다.
            total_score = score_sum

        raw_suggestions = parsed.get("suggestions")
        suggestions: List[str] = []
        if isinstance(raw_suggestions, list):
            for item in raw_suggestions:
                value = str(item or "").strip()
                if value:
                    suggestions.append(value)
                if len(suggestions) >= 3:
                    break

        return VisualEvalResult(
            total_score=total_score,
            layout=layout,
            readability=readability,
            image_quality=image_quality,
            visual_consistency=visual_consistency,
            overall_impression=overall_impression,
            suggestions=suggestions,
        )

    def _resize_if_needed(self, image_path: str) -> Tuple[str, str]:
        """이미지가 제한 크기를 넘으면 JPEG로 압축한다."""
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"image not found: {path}")
        if path.stat().st_size <= constants.VLM_MAX_IMAGE_SIZE_BYTES:
            return str(path), "image/png"

        try:
            from PIL import Image
        except Exception:
            logger.warning("Pillow unavailable, using original image for VLM")
            return str(path), "image/png"

        converted_path = path.with_suffix(".vlm.jpg")
        with Image.open(path) as image:
            current = image.convert("RGB")
            quality = 88

            for _ in range(8):
                current.save(
                    converted_path,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                )
                if converted_path.stat().st_size <= constants.VLM_MAX_IMAGE_SIZE_BYTES:
                    return str(converted_path), "image/jpeg"

                quality = max(45, quality - 10)
                if quality <= 55:
                    width = max(640, int(current.width * 0.85))
                    height = max(360, int(current.height * 0.85))
                    current = current.resize((width, height), Image.Resampling.LANCZOS)

        return str(converted_path), "image/jpeg"

    def _prune_old_screenshots(self) -> None:
        """오래된 스크린샷을 정리한다."""
        try:
            files = [path for path in self.screenshot_dir.glob("*.png") if path.is_file()]
            files.extend([path for path in self.screenshot_dir.glob("*.jpg") if path.is_file()])
            files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
            for stale in files[self.screenshot_retention_max :]:
                try:
                    stale.unlink(missing_ok=True)
                except Exception:
                    continue
        except Exception:
            return

    def _resolve_provider_name(self, client: OpenAICompatClient) -> str:
        """클라이언트에서 프로바이더명을 추론한다."""
        provider = str(getattr(client, "provider_name", "") or "").strip().lower()
        if provider:
            return provider
        return str(getattr(client, "_provider", "") or "").strip().lower() or "unknown_vlm"

    def _is_circuit_open(self, key: str) -> bool:
        """회로 오픈 상태를 확인한다."""
        breaker = self._circuit_breaker
        if breaker is None:
            return False
        try:
            return bool(breaker.is_open(key))
        except Exception:
            return False

    def _record_success(self, key: str) -> None:
        """회로 차단기에 성공을 기록한다."""
        breaker = self._circuit_breaker
        if breaker is None:
            return
        try:
            breaker.record_success(key)
        except Exception:
            return

    def _record_failure(self, key: str) -> None:
        """회로 차단기에 실패를 기록한다."""
        breaker = self._circuit_breaker
        if breaker is None:
            return
        try:
            breaker.record_failure(key)
        except Exception:
            return

    def _apply_score_bias(self, score: int, model_key: str) -> int:
        """모델 편향 보정값을 적용한다."""
        offset = float(self._score_bias_map.get(str(model_key or "").strip().lower(), 0.0) or 0.0)
        adjusted = int(round(float(score) + offset))
        return max(0, min(100, adjusted))

    @staticmethod
    def _clamp_score(value: Any, min_value: int, max_value: int) -> int:
        """점수 범위를 강제한다."""
        try:
            normalized = int(float(value))
        except Exception:
            normalized = 0
        return max(min_value, min(max_value, normalized))
