"""네이버 퍼블리셔 어댑터."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import urlparse

from .base_publisher import BasePublisher, PublishResult
from .playwright_publisher import PlaywrightPublisher
from .publisher_registry import register_publisher

if TYPE_CHECKING:
    from ..images.placement import ImageInsertionPoint


def _extract_blog_id(blog_url: str) -> str:
    """네이버 blog_url에서 blog_id를 추출한다."""
    text = str(blog_url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"

    parsed = urlparse(text)
    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts:
        return path_parts[0]
    return ""


@register_publisher("naver")
class NaverPublisher(BasePublisher):
    """기존 PlaywrightPublisher를 BasePublisher 인터페이스로 래핑한다."""

    RETRYABLE_ERRORS = getattr(PlaywrightPublisher, "RETRYABLE_ERRORS", set())

    def __init__(
        self,
        blog_id: str = "",
        session_dir: str = "",
        channel: Optional[Dict[str, Any]] = None,
    ) -> None:
        resolved_blog_id = str(blog_id or "").strip()
        resolved_session_dir = str(session_dir or "").strip()

        if isinstance(channel, dict):
            raw_auth = str(channel.get("auth_json", "{}") or "{}")
            try:
                auth = json.loads(raw_auth)
            except Exception:
                auth = {}
            if not isinstance(auth, dict):
                auth = {}

            channel_id = str(channel.get("channel_id", "")).strip()
            if not resolved_session_dir:
                resolved_session_dir = str(auth.get("session_dir", "")).strip()
            if not resolved_session_dir:
                resolved_session_dir = f"data/sessions/naver_{channel_id or 'default'}"

            if not resolved_blog_id:
                resolved_blog_id = _extract_blog_id(str(channel.get("blog_url", "")))

        self.blog_id = resolved_blog_id or "dry-run"
        self.session_dir = resolved_session_dir or "data/sessions/naver"
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
