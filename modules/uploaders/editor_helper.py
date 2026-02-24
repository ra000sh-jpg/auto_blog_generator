import asyncio
import random
import re
from pathlib import Path
from typing import Any, Callable, Coroutine, List, Optional


class NaverEditorHelper:
    """네이버 스마트 에디터 ONE 조작 및 타이핑, 마커 교체용 컴포지션 클래스"""

    def __init__(self, page):
        self.page = page

    async def type_naturally(self, text: str):
        """인간적인 타이핑 모사"""
        for char in text:
            await self.page.keyboard.type(char)
            await asyncio.sleep(random.uniform(0.04, 0.12))

    async def human_delay(self, min_ms: int = 500, max_ms: int = 2000):
        """인간적인 딜레이 모사"""
        await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)

    async def insert_content_with_markers(
        self,
        content: str,
        images: Optional[List[str]],
        thumbnail: Optional[str],
        image_points: Optional[List[Any]],
        thumbnail_placement_mode: str,
        upload_image_callback: Callable[[str], Coroutine[Any, Any, None]],
        insert_separator_callback: Callable[[str], Coroutine[Any, Any, None]],
    ) -> None:
        """본문 내용과 이미지를 교차 마커 기반으로 삽입하거나 일괄 첨부한다."""
        if image_points:
            # 마커를 기준으로 텍스트와 이미지를 교차 삽입한다.
            if thumbnail_placement_mode == "cover":
                marker_points = {pt.marker: pt for pt in image_points if not pt.is_thumbnail}
            else:
                marker_points = {pt.marker: pt for pt in image_points}

            pattern = re.compile(r"(\[IMG_\d+\])")
            parts = pattern.split(content)

            for part in parts:
                if not part:
                    continue
                if part in marker_points:
                    point = marker_points[part]
                    if Path(point.path).exists():
                        await insert_separator_callback("before")
                        await upload_image_callback(point.path)
                        await insert_separator_callback("after")
                elif pattern.match(part):
                    # 마커인데 파일 매핑이 없으면 무시한다.
                    continue
                else:
                    part_clean = part.strip("\n")
                    if part_clean:
                        await self.page.keyboard.insert_text(part_clean + "\n")
                        await self.human_delay(500, 1000)
        else:
            # 예전 로직 fallback (마커 기반 포인트가 없을 때 전체 텍스트 후 이미지 일괄 첨부)
            clean_content = re.sub(r"\[IMG_\d+\]\n?", "", content)
            await self.page.keyboard.insert_text(clean_content)
            await self.human_delay(1000, 2000)

            images_to_upload = [p for p in (images or []) if Path(p).exists()]
            if (
                thumbnail_placement_mode != "cover"
                and thumbnail
                and Path(thumbnail).exists()
            ):
                images_to_upload = [thumbnail, *images_to_upload]
            for img_path in images_to_upload:
                await insert_separator_callback("before")
                await upload_image_callback(img_path)
                await insert_separator_callback("after")
                await self.human_delay(1200, 2400)
