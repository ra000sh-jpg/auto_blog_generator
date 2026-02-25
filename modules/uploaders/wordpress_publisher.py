"""워드프레스 퍼블리셔(플레이스홀더)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .base_publisher import BasePublisher, PublishResult
from .publisher_registry import register_publisher

if TYPE_CHECKING:
    from ..images.placement import ImageInsertionPoint


@register_publisher("wordpress")
class WordPressPublisher(BasePublisher):
    """향후 워드프레스 구현을 위한 플레이스홀더 퍼블리셔."""

    def __init__(self, channel: Optional[Dict[str, Any]] = None) -> None:
        self.channel = dict(channel or {})

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
        del title, content, thumbnail, images, image_sources, image_points, tags, category
        return PublishResult(
            success=False,
            error_code="PUBLISHER_NOT_IMPLEMENTED",
            error_message="WordPress publisher coming in Phase 3",
        )

    async def test_connection(self) -> bool:
        return False
