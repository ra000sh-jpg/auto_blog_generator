"""fal.ai FLUX 이미지 생성 클라이언트."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx

from .dashscope_image_client import ImageResult

logger = logging.getLogger(__name__)


class FalFluxImageClient:
    """fal.ai FLUX 이미지 생성 클라이언트."""

    DEFAULT_ENDPOINT = "https://fal.run/fal-ai/flux/schnell"

    def __init__(
        self,
        *,
        endpoint: Optional[str] = None,
        timeout_sec: float = 120.0,
        output_dir: str = "data/images",
        api_key: Optional[str] = None,
    ):
        self.endpoint = str(endpoint or os.getenv("FAL_IMAGE_ENDPOINT", self.DEFAULT_ENDPOINT)).strip()
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key or os.getenv("FAL_KEY", "")
        self._client = httpx.AsyncClient(timeout=timeout_sec)

    def is_available(self) -> bool:
        """API 키 유효 여부를 반환한다."""
        return bool(self.api_key)

    @staticmethod
    def _resolve_image_size(size: str) -> str:
        """size를 fal 이미지 규격 키로 변환한다."""
        normalized = str(size or "").strip().replace("*", "x")
        try:
            width_text, height_text = normalized.split("x")
            width = int(width_text)
            height = int(height_text)
        except Exception:
            return "square_hd"

        if width > height:
            return "landscape_16_9"
        if height > width:
            return "portrait_16_9"
        return "square_hd"

    @staticmethod
    def _extract_image_url(payload: Any) -> str:
        """fal 응답에서 첫 이미지 URL을 추출한다."""
        if not isinstance(payload, dict):
            return ""

        images = payload.get("images")
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, dict):
                return str(first.get("url", "")).strip()
            if isinstance(first, str):
                return first.strip()

        image = payload.get("image")
        if isinstance(image, dict):
            return str(image.get("url", "")).strip()
        if isinstance(image, str):
            return image.strip()
        return ""

    async def generate(
        self,
        prompt: str,
        style_suffix: str = "",
        size: str = "1024*1024",
        n: int = 1,
    ) -> ImageResult:
        """이미지를 생성하고 로컬 파일로 저장한다."""
        del n  # fal 기본 경로는 단일 생성으로 사용
        if not self.api_key:
            return ImageResult(success=False, error_message="FAL_KEY not set")

        full_prompt = f"{prompt}{style_suffix}".strip()
        image_size = self._resolve_image_size(size)
        payload = {
            "prompt": full_prompt,
            "image_size": image_size,
            "num_images": 1,
        }
        headers = {
            "Authorization": f"Key {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = await self._client.post(self.endpoint, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            image_url = self._extract_image_url(data)
            if not image_url:
                return ImageResult(success=False, error_message="fal image URL missing")

            image_response = await self._client.get(image_url)
            image_response.raise_for_status()

            image_id = str(uuid.uuid4())
            file_path = self.output_dir / f"fal_{image_id}.png"
            file_path.write_bytes(image_response.content)

            return ImageResult(
                success=True,
                image_url=image_url,
                local_path=str(file_path),
            )
        except Exception as exc:
            logger.warning("fal image generation failed: %s", exc)
            return ImageResult(success=False, error_message=str(exc))

    async def close(self) -> None:
        """내부 HTTP 클라이언트를 종료한다."""
        await self._client.aclose()
