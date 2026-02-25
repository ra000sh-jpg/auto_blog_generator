"""퍼블리셔 공통 인터페이스."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from ..images.placement import ImageInsertionPoint


@dataclass
class PublishResult:
    success: bool
    url: str = ""
    error_code: str = ""
    error_message: str = ""


class BasePublisher(ABC):
    """플랫폼별 발행기 공통 인터페이스."""

    @abstractmethod
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
        """포스트를 발행하고 결과를 반환한다."""

    @abstractmethod
    async def test_connection(self) -> bool:
        """채널 인증/연결 상태를 점검한다."""
