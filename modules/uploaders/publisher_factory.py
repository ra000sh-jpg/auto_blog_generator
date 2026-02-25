"""채널 정보 기반 퍼블리셔 팩토리."""

from __future__ import annotations

import json
from typing import Any, Dict
from urllib.parse import urlparse

from .base_publisher import BasePublisher
from .naver_publisher import NaverPublisher
from .tistory_publisher import TistoryPublisher


def extract_blog_id(blog_url: str) -> str:
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


def _extract_tistory_blog_name(channel: Dict[str, Any]) -> str:
    raw_url = str(channel.get("blog_url", "")).strip()
    if not raw_url:
        return ""
    if "://" not in raw_url:
        raw_url = f"https://{raw_url}"
    parsed = urlparse(raw_url)
    hostname = str(parsed.hostname or "").strip().lower()
    if hostname.endswith(".tistory.com"):
        return hostname.replace(".tistory.com", "").strip()
    return ""


def get_publisher(channel: Dict[str, Any]) -> BasePublisher:
    """채널 플랫폼에 맞는 퍼블리셔를 생성한다."""
    platform = str(channel.get("platform", "")).strip().lower()
    raw_auth = str(channel.get("auth_json", "{}") or "{}")
    try:
        auth = json.loads(raw_auth)
    except Exception:
        auth = {}
    if not isinstance(auth, dict):
        auth = {}

    if platform == "naver":
        channel_id = str(channel.get("channel_id", "")).strip()
        session_dir = str(auth.get("session_dir", "")).strip()
        if not session_dir:
            session_dir = f"data/sessions/naver_{channel_id or 'default'}"
        blog_id = extract_blog_id(str(channel.get("blog_url", "")))
        if not blog_id:
            blog_id = "dry-run"
        return NaverPublisher(blog_id=blog_id, session_dir=session_dir)

    if platform == "tistory":
        access_token = str(auth.get("access_token", "")).strip()
        blog_name = str(auth.get("blog_name", "")).strip() or _extract_tistory_blog_name(channel)
        return TistoryPublisher(
            access_token=access_token,
            blog_name=blog_name,
        )

    if platform == "wordpress":
        raise NotImplementedError("WordPress publisher coming in Phase 3")

    raise ValueError(f"Unsupported platform: {platform}")
