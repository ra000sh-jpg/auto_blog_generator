"""Pollinations.ai 이미지 생성 클라이언트 (API 키 불필요)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

from .dashscope_image_client import ImageResult

logger = logging.getLogger(__name__)

_BASE_URL = "https://image.pollinations.ai/prompt"
_LOREM_PICSUM_URL = "https://picsum.photos"


class PollinationsImageClient:
    """Pollinations.ai HTTP 이미지 생성 클라이언트.

    API 키 없이 무료로 사용 가능하며 FLUX 모델을 사용한다.
    530 에러 등 서버 장애 시 재시도 + Lorem Picsum 플레이스홀더 폴백을 지원한다.
    """

    def __init__(
        self,
        model: str = "flux",
        timeout_sec: float = 120.0,
        output_dir: str = "data/images",
        max_retries: int = 3,
        retry_backoff_sec: float = 2.0,
        use_placeholder_fallback: bool = True,
    ):
        self.model = model
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(timeout=timeout_sec)
        self.max_retries = max_retries
        self.retry_backoff_sec = retry_backoff_sec
        self.use_placeholder_fallback = use_placeholder_fallback

    @staticmethod
    def _parse_size(size: str) -> tuple[int, int]:
        """'1024*1024' 형식을 (width, height)로 변환한다."""
        try:
            w, h = size.split("*")
            return int(w), int(h)
        except (ValueError, AttributeError):
            return 1024, 1024

    async def generate(
        self,
        prompt: str,
        style_suffix: str = "",
        size: str = "1024*1024",
        n: int = 1,
    ) -> ImageResult:
        """이미지를 생성하고 로컬에 저장한 뒤 결과를 반환한다.

        Pollinations 실패 시 재시도 후 Lorem Picsum 플레이스홀더로 폴백한다.
        """
        full_prompt = f"{prompt}{style_suffix}"
        width, height = self._parse_size(size)

        url = (
            f"{_BASE_URL}/{quote(full_prompt)}"
            f"?width={width}&height={height}&model={self.model}&nologo=true"
        )

        last_error: Optional[Exception] = None

        # Pollinations 재시도 루프
        for attempt in range(self.max_retries):
            try:
                logger.info(
                    "Pollinations request",
                    extra={"model": self.model, "size": f"{width}x{height}", "attempt": attempt + 1},
                )
                response = await self._client.get(url, follow_redirects=True)
                response.raise_for_status()

                image_id = str(uuid.uuid4())
                file_path = self.output_dir / f"pollinations_{image_id}.png"
                file_path.write_bytes(response.content)

                logger.info("Pollinations image saved", extra={"path": str(file_path)})
                return ImageResult(
                    success=True,
                    image_url=url,
                    local_path=str(file_path),
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Pollinations attempt %d/%d failed: %s",
                    attempt + 1,
                    self.max_retries,
                    exc,
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_backoff_sec * (2 ** attempt))

        # 모든 재시도 실패 → Lorem Picsum 플레이스홀더 폴백
        if self.use_placeholder_fallback:
            return await self._fetch_placeholder(width, height, str(last_error))

        logger.error("Pollinations generation failed", extra={"error": str(last_error)})
        return ImageResult(success=False, error_message=str(last_error))

    async def _fetch_placeholder(
        self, width: int, height: int, original_error: str
    ) -> ImageResult:
        """Lorem Picsum에서 플레이스홀더 이미지를 가져온다."""
        seed = uuid.uuid4().int % 1000
        placeholder_url = f"{_LOREM_PICSUM_URL}/seed/{seed}/{width}/{height}"

        try:
            logger.info("Fetching placeholder from Lorem Picsum", extra={"url": placeholder_url})
            # 플레이스홀더용 별도 클라이언트 (30초 타임아웃)
            async with httpx.AsyncClient(timeout=30.0) as placeholder_client:
                response = await placeholder_client.get(placeholder_url, follow_redirects=True)
                response.raise_for_status()

                image_id = str(uuid.uuid4())
                file_path = self.output_dir / f"placeholder_{image_id}.jpg"
                file_path.write_bytes(response.content)

                logger.info(
                    "Placeholder image saved (Pollinations unavailable)",
                    extra={"path": str(file_path), "original_error": original_error},
                )
                return ImageResult(
                    success=True,
                    image_url=placeholder_url,
                    local_path=str(file_path),
                )
        except Exception as exc:
            logger.error("Placeholder fetch also failed", extra={"error": str(exc)})
            return ImageResult(
                success=False,
                error_message=f"Pollinations: {original_error}, Placeholder: {exc}",
            )

    async def close(self) -> None:
        """내부 HTTP 클라이언트를 종료한다."""
        await self._client.aclose()
