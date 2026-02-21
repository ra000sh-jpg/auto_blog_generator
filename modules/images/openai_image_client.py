"""OpenAI 이미지 생성 클라이언트."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Optional

import httpx

from .dashscope_image_client import ImageResult

logger = logging.getLogger(__name__)


class OpenAIImageClient:
    """OpenAI 이미지 생성 API(DALL-E 계열) 클라이언트."""

    BASE_URL = "https://api.openai.com/v1"

    def __init__(
        self,
        *,
        model: str = "dall-e-3",
        timeout_sec: float = 120.0,
        output_dir: str = "data/images",
        api_key: Optional[str] = None,
    ):
        self.model = model
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self._client = httpx.AsyncClient(timeout=timeout_sec)

    def is_available(self) -> bool:
        """API 키 유효 여부를 반환한다."""
        return bool(self.api_key)

    @staticmethod
    def _normalize_size(size: str) -> str:
        """내부 size 형식을 OpenAI images 규격으로 변환한다."""
        normalized = str(size or "").strip().replace("*", "x")
        if normalized in {"1024x1024", "1024x1792", "1792x1024"}:
            return normalized

        try:
            width_text, height_text = normalized.split("x")
            width = int(width_text)
            height = int(height_text)
        except Exception:
            return "1024x1024"

        if width > height:
            return "1792x1024"
        if height > width:
            return "1024x1792"
        return "1024x1024"

    async def generate(
        self,
        prompt: str,
        style_suffix: str = "",
        size: str = "1024*1024",
        n: int = 1,
    ) -> ImageResult:
        """이미지를 생성하고 로컬 파일로 저장한다."""
        del n  # DALL-E 3는 n=1만 지원
        if not self.api_key:
            return ImageResult(success=False, error_message="OPENAI_API_KEY not set")

        request_size = self._normalize_size(size)
        full_prompt = f"{prompt}{style_suffix}".strip()
        payload = {
            "model": self.model,
            "prompt": full_prompt,
            "size": request_size,
            "n": 1,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = await self._client.post(
                f"{self.BASE_URL}/images/generations",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            items = data.get("data", [])
            image_url = ""
            if isinstance(items, list) and items:
                image_url = str(items[0].get("url", "")).strip()

            if not image_url:
                return ImageResult(success=False, error_message="OpenAI image URL missing")

            image_response = await self._client.get(image_url)
            image_response.raise_for_status()

            image_id = str(uuid.uuid4())
            file_path = self.output_dir / f"openai_{image_id}.png"
            file_path.write_bytes(image_response.content)

            return ImageResult(
                success=True,
                image_url=image_url,
                local_path=str(file_path),
            )
        except Exception as exc:
            logger.warning("OpenAI image generation failed: %s", exc)
            return ImageResult(success=False, error_message=str(exc))

    async def close(self) -> None:
        """내부 HTTP 클라이언트를 종료한다."""
        await self._client.aclose()
