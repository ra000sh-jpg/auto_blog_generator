"""네이버 퍼블리셔 어댑터."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from .base_publisher import BasePublisher, PublishResult
from .playwright_publisher import PlaywrightPublisher

if TYPE_CHECKING:
    from ..images.placement import ImageInsertionPoint


class NaverPublisher(BasePublisher):
    """기존 PlaywrightPublisher를 BasePublisher 인터페이스로 래핑한다."""

    RETRYABLE_ERRORS = getattr(PlaywrightPublisher, "RETRYABLE_ERRORS", set())

    def __init__(self, blog_id: str, session_dir: str) -> None:
        self.blog_id = str(blog_id or "").strip() or "dry-run"
        self.session_dir = str(session_dir or "").strip() or "data/sessions/naver"
        self._publisher = PlaywrightPublisher(
            blog_id=self.blog_id,
            session_dir=self.session_dir,
        )

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
        return await self._publisher.publish(
            title=title,
            content=content,
            thumbnail=thumbnail,
            images=images,
            image_sources=image_sources,
            image_points=image_points,
            tags=tags,
            category=category,
        )

    async def test_connection(self) -> bool:
        """세션 파일 존재 여부로 네이버 연결 가능성을 점검한다."""
        state_path = Path(self.session_dir) / "state.json"
        return state_path.exists()
