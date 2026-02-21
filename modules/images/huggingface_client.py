"""Hugging Face Inference API 이미지 생성 클라이언트."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from .dashscope_image_client import ImageResult

logger = logging.getLogger(__name__)


class HuggingFaceImageClient:
    """Hugging Face Inference Providers 이미지 생성 클라이언트.

    무료 티어로 FLUX.1-schnell 모델을 사용할 수 있다.
    huggingface_hub 라이브러리를 사용한다.
    """

    def __init__(
        self,
        model: str = "black-forest-labs/FLUX.1-schnell",
        timeout_sec: float = 120.0,
        output_dir: str = "data/images",
        api_key: Optional[str] = None,
    ):
        self.model = model
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key or os.getenv("HF_TOKEN", "")
        self.timeout_sec = timeout_sec

    @staticmethod
    def _parse_size(size: str) -> tuple[int, int]:
        """'1024*1024' 형식을 (width, height)로 변환한다."""
        try:
            w, h = size.split("*")
            return int(w), int(h)
        except (ValueError, AttributeError):
            return 1024, 1024

    def is_available(self) -> bool:
        """API 키가 설정되어 있고 huggingface_hub가 설치되어 있는지 확인한다."""
        if not self.api_key:
            return False
        try:
            from huggingface_hub import InferenceClient  # noqa: F401
            return True
        except ImportError:
            logger.warning("huggingface_hub not installed, HuggingFace client unavailable")
            return False

    async def generate(
        self,
        prompt: str,
        style_suffix: str = "",
        size: str = "1024*1024",
        n: int = 1,
    ) -> ImageResult:
        """이미지를 생성하고 로컬에 저장한 뒤 결과를 반환한다."""
        if not self.api_key:
            return ImageResult(success=False, error_message="HF_TOKEN not set")

        try:
            from huggingface_hub import InferenceClient
        except ImportError:
            return ImageResult(success=False, error_message="huggingface_hub not installed")

        full_prompt = f"{prompt}{style_suffix}"
        width, height = self._parse_size(size)

        try:
            logger.info(
                "HuggingFace Inference request",
                extra={"model": self.model, "size": f"{width}x{height}"},
            )

            # 동기 InferenceClient를 비동기로 래핑
            def _generate_sync():
                client = InferenceClient(token=self.api_key)
                return client.text_to_image(
                    prompt=full_prompt,
                    model=self.model,
                    width=width,
                    height=height,
                )

            # 블로킹 호출을 executor에서 실행
            loop = asyncio.get_event_loop()
            image = await loop.run_in_executor(None, _generate_sync)

            image_id = str(uuid.uuid4())
            file_path = self.output_dir / f"huggingface_{image_id}.png"
            image.save(str(file_path))

            logger.info("HuggingFace image saved", extra={"path": str(file_path)})
            return ImageResult(
                success=True,
                image_url=f"hf://{self.model}",
                local_path=str(file_path),
            )
        except Exception as exc:
            error_msg = str(exc)[:200]
            logger.error("HuggingFace generation failed", extra={"error": error_msg})
            return ImageResult(success=False, error_message=error_msg)

    async def close(self) -> None:
        """리소스 정리 (huggingface_hub는 별도 정리 불필요)."""
        pass
