"""DashScope 이미지 생성 클라이언트."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ImageResult:
    """이미지 생성 결과."""

    success: bool
    image_url: Optional[str] = None
    local_path: Optional[str] = None
    error_message: Optional[str] = None


class DashScopeImageClient:
    """DashScope 이미지 생성 클라이언트."""

    DEFAULT_BASE_URL = "https://dashscope-us.aliyuncs.com"
    SYNC_ENDPOINT = "/api/v1/services/aigc/text2image/image-synthesis"
    ASYNC_TASK_ENDPOINT = "/api/v1/tasks"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "wan2.6-t2i",
        timeout_sec: float = 120.0,
        output_dir: str = "data/images",
    ):
        resolved_api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        if not resolved_api_key:
            raise ValueError("DASHSCOPE_API_KEY 환경변수가 필요합니다.")

        self.api_key = resolved_api_key
        self.model = model
        self.timeout_sec = timeout_sec
        self.base_url = self._resolve_base_url(os.getenv("DASHSCOPE_BASE_URL", self.DEFAULT_BASE_URL))
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(timeout=timeout_sec)

    def _resolve_base_url(self, env_url: str) -> str:
        normalized = env_url.strip().lower()
        if "dashscope-us" in normalized:
            return "https://dashscope-us.aliyuncs.com"
        if "dashscope-intl" in normalized:
            return "https://dashscope-intl.aliyuncs.com"
        return self.DEFAULT_BASE_URL

    async def generate(
        self,
        prompt: str,
        style_suffix: str = "",
        size: str = "1024*1024",
        n: int = 1,
    ) -> ImageResult:
        """이미지 생성(작업 생성 -> 완료 폴링 -> 다운로드)을 수행한다."""
        full_prompt = f"{prompt}{style_suffix}"

        task_id = await self._create_task(prompt=full_prompt, size=size, n=n)
        if not task_id:
            return ImageResult(success=False, error_message="Task creation failed")

        result = await self._wait_for_task(task_id=task_id)
        if not result.success:
            return result

        if result.image_url:
            result.local_path = await self._download_image(result.image_url, task_id)
        return result

    async def _create_task(self, prompt: str, size: str, n: int) -> Optional[str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "input": {"prompt": prompt},
            "parameters": {"size": size, "n": n},
        }
        try:
            response = await self._client.post(
                f"{self.base_url}{self.SYNC_ENDPOINT}",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            task_id = str(data.get("output", {}).get("task_id", ""))
            if not task_id:
                return None
            logger.info("Image task created", extra={"task_id": task_id, "model": self.model})
            return task_id
        except Exception as exc:
            logger.error("Failed to create image task", extra={"error": str(exc)})
            return None

    async def _wait_for_task(self, task_id: str, max_wait_sec: float = 120.0) -> ImageResult:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        poll_url = f"{self.base_url}{self.ASYNC_TASK_ENDPOINT}/{task_id}"

        loop = asyncio.get_running_loop()
        start_time = loop.time()
        while True:
            if loop.time() - start_time > max_wait_sec:
                return ImageResult(success=False, error_message="Task timeout")

            try:
                response = await self._client.get(poll_url, headers=headers)
                response.raise_for_status()
                data = response.json()

                status = str(data.get("output", {}).get("task_status", ""))
                if status == "SUCCEEDED":
                    results = data.get("output", {}).get("results", [])
                    if results:
                        image_url = results[0].get("url")
                        if image_url:
                            logger.info("Image generation succeeded", extra={"task_id": task_id})
                            return ImageResult(success=True, image_url=str(image_url))
                    return ImageResult(success=False, error_message="No results")

                if status == "FAILED":
                    error_msg = str(data.get("output", {}).get("message", "Unknown error"))
                    logger.error("Image generation failed", extra={"task_id": task_id, "error": error_msg})
                    return ImageResult(success=False, error_message=error_msg)

                await asyncio.sleep(2.0)
            except Exception as exc:
                logger.error("Failed to poll task status", extra={"error": str(exc)})
                await asyncio.sleep(2.0)

    async def _download_image(self, url: str, task_id: str) -> Optional[str]:
        try:
            response = await self._client.get(url)
            response.raise_for_status()

            file_path = self.output_dir / f"dashscope_{task_id}.png"
            file_path.write_bytes(response.content)

            logger.info("Image downloaded", extra={"path": str(file_path)})
            return str(file_path)
        except Exception as exc:
            logger.error("Failed to download image", extra={"error": str(exc)})
            return None

    async def close(self) -> None:
        """내부 HTTP 클라이언트를 종료한다."""
        await self._client.aclose()
