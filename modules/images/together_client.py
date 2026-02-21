"""Together.ai FLUX 이미지 생성 클라이언트."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Optional

import httpx

from .dashscope_image_client import ImageResult

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.together.xyz/v1/images/generations"


class TogetherImageClient:
    """Together.ai FLUX 이미지 생성 클라이언트.

    3개월 무료 FLUX.1-schnell 모델을 사용한다.
    """

    def __init__(
        self,
        model: str = "black-forest-labs/FLUX.1-schnell-Free",
        timeout_sec: float = 120.0,
        output_dir: str = "data/images",
        api_key: Optional[str] = None,
    ):
        self.model = model
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key or os.getenv("TOGETHER_API_KEY", "")
        self._client = httpx.AsyncClient(timeout=timeout_sec)

    @staticmethod
    def _parse_size(size: str) -> tuple[int, int]:
        """'1024*1024' 형식을 (width, height)로 변환한다."""
        try:
            w, h = size.split("*")
            return int(w), int(h)
        except (ValueError, AttributeError):
            return 1024, 1024

    def is_available(self) -> bool:
        """API 키가 설정되어 있는지 확인한다."""
        return bool(self.api_key)

    async def generate(
        self,
        prompt: str,
        style_suffix: str = "",
        size: str = "1024*1024",
        n: int = 1,
    ) -> ImageResult:
        """이미지를 생성하고 로컬에 저장한 뒤 결과를 반환한다."""
        if not self.api_key:
            return ImageResult(success=False, error_message="TOGETHER_API_KEY not set")

        full_prompt = f"{prompt}{style_suffix}"
        width, height = self._parse_size(size)

        payload = {
            "model": self.model,
            "prompt": full_prompt,
            "width": width,
            "height": height,
            "n": 1,
            "response_format": "b64_json",
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            logger.info(
                "Together.ai request",
                extra={"model": self.model, "size": f"{width}x{height}"},
            )
            response = await self._client.post(_BASE_URL, json=payload, headers=headers)
            response.raise_for_status()

            data = response.json()
            b64_data = data["data"][0]["b64_json"]

            import base64
            image_bytes = base64.b64decode(b64_data)

            image_id = str(uuid.uuid4())
            file_path = self.output_dir / f"together_{image_id}.png"
            file_path.write_bytes(image_bytes)

            logger.info("Together.ai image saved", extra={"path": str(file_path)})
            return ImageResult(
                success=True,
                image_url=f"together://{self.model}",
                local_path=str(file_path),
            )
        except Exception as exc:
            logger.error("Together.ai generation failed", extra={"error": str(exc)})
            return ImageResult(success=False, error_message=str(exc))

    async def close(self) -> None:
        """내부 HTTP 클라이언트를 종료한다."""
        await self._client.aclose()
