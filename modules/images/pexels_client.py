"""Pexels API 스톡 포토 검색 클라이언트."""

from __future__ import annotations

import logging
import os
import random
import uuid
from pathlib import Path
from typing import Optional

import aiohttp

from .dashscope_image_client import ImageResult

logger = logging.getLogger(__name__)


class PexelsImageClient:
    """Pexels API를 사용한 무료 스톡 포토 검색 클라이언트.

    - 무료 API (월 200회 → 승인 후 무제한)
    - 고품질 사진, 상업적 사용 가능
    - 키워드 기반 검색으로 주제에 맞는 실사 이미지 제공

    사용 시 주의:
    - Pexels 출처 표기 권장 (필수는 아님)
    - AI 생성 이미지 대신 실사 사진이 필요한 경우 사용
    """

    BASE_URL = "https://api.pexels.com/v1"

    def __init__(
        self,
        timeout_sec: float = 30.0,
        output_dir: str = "data/images",
        api_key: Optional[str] = None,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key or os.getenv("PEXELS_API_KEY", "")
        self.timeout_sec = timeout_sec
        self._session: Optional[aiohttp.ClientSession] = None

    def is_available(self) -> bool:
        """API 키가 설정되어 있는지 확인한다."""
        return bool(self.api_key)

    async def _get_session(self) -> aiohttp.ClientSession:
        """aiohttp 세션을 반환한다 (lazy init)."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": self.api_key},
                timeout=aiohttp.ClientTimeout(total=self.timeout_sec),
            )
        return self._session

    async def search(
        self,
        query: str,
        per_page: int = 15,
        page: int = 1,
        orientation: Optional[str] = None,  # landscape, portrait, square
        size: Optional[str] = None,  # large, medium, small
    ) -> list[dict]:
        """키워드로 사진을 검색하고 결과 목록을 반환한다.

        Returns:
            사진 정보 딕셔너리 리스트 (id, url, photographer, src 등)
        """
        if not self.api_key:
            logger.warning("PEXELS_API_KEY not set")
            return []

        params = {
            "query": query,
            "per_page": per_page,
            "page": page,
        }
        if orientation:
            params["orientation"] = orientation
        if size:
            params["size"] = size

        try:
            session = await self._get_session()
            async with session.get(f"{self.BASE_URL}/search", params=params) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(
                        "Pexels search failed",
                        extra={"status": resp.status, "error": error_text[:200]},
                    )
                    return []

                data = await resp.json()
                return data.get("photos", [])
        except Exception as exc:
            logger.error("Pexels API error", extra={"error": str(exc)[:200]})
            return []

    async def download_photo(
        self,
        photo: dict,
        size_key: str = "large",  # original, large2x, large, medium, small, etc.
    ) -> Optional[str]:
        """사진을 로컬에 다운로드하고 파일 경로를 반환한다.

        Args:
            photo: Pexels API의 photo 객체
            size_key: src 딕셔너리의 키 (original, large2x, large, medium, small 등)

        Returns:
            로컬 파일 경로 또는 None (실패 시)
        """
        src = photo.get("src", {})
        url = src.get(size_key) or src.get("large") or src.get("original")

        if not url:
            logger.warning("No valid URL in photo src", extra={"photo_id": photo.get("id")})
            return None

        try:
            session = await self._get_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error(
                        "Pexels download failed",
                        extra={"status": resp.status, "url": url[:100]},
                    )
                    return None

                # 확장자 추출
                content_type = resp.headers.get("content-type", "")
                if "jpeg" in content_type or "jpg" in content_type:
                    ext = ".jpg"
                elif "png" in content_type:
                    ext = ".png"
                elif "webp" in content_type:
                    ext = ".webp"
                else:
                    ext = ".jpg"  # 기본값

                image_id = str(uuid.uuid4())
                file_path = self.output_dir / f"pexels_{image_id}{ext}"

                content = await resp.read()
                file_path.write_bytes(content)

                logger.info(
                    "Pexels photo downloaded",
                    extra={
                        "path": str(file_path),
                        "photo_id": photo.get("id"),
                        "photographer": photo.get("photographer"),
                    },
                )
                return str(file_path)
        except Exception as exc:
            logger.error("Pexels download error", extra={"error": str(exc)[:200]})
            return None

    async def generate(
        self,
        prompt: str,
        style_suffix: str = "",
        size: str = "1024*1024",
        n: int = 1,
    ) -> ImageResult:
        """이미지 생성 인터페이스와 호환되는 검색+다운로드 메서드.

        prompt를 검색어로 사용하여 사진을 찾고 다운로드한다.
        다른 이미지 클라이언트와 동일한 인터페이스를 제공한다.

        Args:
            prompt: 검색 키워드 (영어 권장)
            style_suffix: 무시됨 (스톡 포토에는 스타일 적용 불가)
            size: '1024*1024' 형식 - landscape/portrait/square 결정에 사용
            n: 무시됨 (항상 1개 반환)

        Returns:
            ImageResult 객체
        """
        if not self.api_key:
            return ImageResult(success=False, error_message="PEXELS_API_KEY not set")

        # 사이즈에서 orientation 추론
        orientation = self._infer_orientation(size)

        # 프롬프트에서 검색어 추출 (간단한 정제)
        search_query = self._clean_prompt_for_search(prompt)

        logger.info(
            "Pexels search",
            extra={"query": search_query, "orientation": orientation},
        )

        photos = await self.search(
            query=search_query,
            per_page=10,
            orientation=orientation,
        )

        if not photos:
            return ImageResult(
                success=False,
                error_message=f"No photos found for: {search_query}",
            )

        # 첫 번째 사진 다운로드 (추후 랜덤 선택 등 개선 가능)
        photo = random.choice(photos[:5]) if len(photos) >= 5 else photos[0]

        local_path = await self.download_photo(photo, size_key="large")

        if not local_path:
            return ImageResult(
                success=False,
                error_message="Failed to download photo",
            )

        return ImageResult(
            success=True,
            image_url=photo.get("url", ""),
            local_path=local_path,
        )

    @staticmethod
    def _infer_orientation(size: str) -> Optional[str]:
        """사이즈 문자열에서 orientation을 추론한다."""
        try:
            w, h = size.split("*")
            width, height = int(w), int(h)
            if width > height:
                return "landscape"
            elif height > width:
                return "portrait"
            else:
                return "square"
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _clean_prompt_for_search(prompt: str) -> str:
        """프롬프트를 검색어로 정제한다.

        AI 이미지 생성 프롬프트는 보통 길고 상세하므로,
        Pexels 검색에 적합하도록 핵심 키워드만 추출한다.
        """
        # 쉼표로 구분된 경우 앞부분 사용
        if "," in prompt:
            parts = prompt.split(",")
            # 앞의 2-3개 부분만 사용
            search_parts = parts[:3]
            return " ".join(p.strip() for p in search_parts)

        # 너무 긴 프롬프트는 앞부분만 사용
        words = prompt.split()
        if len(words) > 8:
            return " ".join(words[:8])

        return prompt

    async def close(self) -> None:
        """aiohttp 세션을 정리한다."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
