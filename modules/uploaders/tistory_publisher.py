"""티스토리 Open API 퍼블리셔."""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .base_publisher import BasePublisher, PublishResult

if TYPE_CHECKING:
    from ..images.placement import ImageInsertionPoint


class TistoryPublisher(BasePublisher):
    """티스토리 Open API 기반 발행기."""

    RETRYABLE_ERRORS = frozenset({"NETWORK_TIMEOUT", "RATE_LIMITED", "PUBLISH_FAILED", "UNKNOWN"})
    API_BASE = "https://www.tistory.com/apis"

    def __init__(
        self,
        *,
        access_token: str,
        blog_name: str,
    ) -> None:
        self.access_token = str(access_token or "").strip()
        self.blog_name = str(blog_name or "").strip()

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
        del thumbnail, images, image_sources, image_points, category

        if os.getenv("DRY_RUN", "false").strip().lower() == "true":
            return PublishResult(
                success=True,
                url=f"https://{self.blog_name}.tistory.com/mock",
            )

        if not self.access_token or not self.blog_name:
            return PublishResult(
                success=False,
                error_code="AUTH_EXPIRED",
                error_message="티스토리 access_token/blog_name이 필요합니다.",
            )

        body = {
            "access_token": self.access_token,
            "output": "json",
            "blogName": self.blog_name,
            "title": str(title or "").strip(),
            "content": str(content or "").strip(),
            "visibility": "3",
            "acceptComment": "1",
        }
        if tags:
            normalized_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
            if normalized_tags:
                body["tag"] = ",".join(normalized_tags)

        try:
            payload = await asyncio.to_thread(
                self._request_json,
                f"{self.API_BASE}/post/write",
                body,
            )
        except TimeoutError:
            return PublishResult(
                success=False,
                error_code="NETWORK_TIMEOUT",
                error_message="티스토리 발행 API 타임아웃",
            )
        except Exception as exc:
            return PublishResult(
                success=False,
                error_code="PUBLISH_FAILED",
                error_message=str(exc)[:300],
            )

        tistory_payload = payload.get("tistory", {}) if isinstance(payload, dict) else {}
        status = str(tistory_payload.get("status", ""))
        if status != "200":
            return PublishResult(
                success=False,
                error_code="PUBLISH_FAILED",
                error_message=str(tistory_payload.get("error_message", "티스토리 발행 실패"))[:300],
            )

        item = tistory_payload.get("item", {}) if isinstance(tistory_payload.get("item"), dict) else {}
        post_url = str(item.get("url", "")).strip()
        if not post_url:
            post_id = str(item.get("postId", "")).strip()
            if post_id:
                post_url = f"https://{self.blog_name}.tistory.com/{post_id}"

        return PublishResult(
            success=True,
            url=post_url,
        )

    async def test_connection(self) -> bool:
        if not self.access_token or not self.blog_name:
            return False
        try:
            await asyncio.to_thread(
                self._request_json,
                f"{self.API_BASE}/blog/info",
                {
                    "access_token": self.access_token,
                    "output": "json",
                },
            )
            return True
        except Exception:
            return False

    def _request_json(self, url: str, payload: Dict[str, str]) -> Dict[str, object]:
        encoded = urlencode(payload).encode("utf-8")
        request = Request(
            url=url,
            data=encoded,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        try:
            with urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            if exc.code == 429:
                raise RuntimeError("RATE_LIMITED") from exc
            raise RuntimeError(f"HTTP_ERROR:{exc.code}") from exc
        except URLError as exc:
            raise TimeoutError("NETWORK_TIMEOUT") from exc

        parsed = json.loads(raw or "{}")
        if not isinstance(parsed, dict):
            raise RuntimeError("INVALID_RESPONSE")
        return parsed
