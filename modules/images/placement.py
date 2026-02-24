"""블로그 콘텐츠 내 이미지 배치 최적화.

최적화 규칙:
1. 썸네일: 제목 바로 아래 (첫 H2 전)
2. 본문 이미지: H2 섹션 사이에 분산 배치
3. 이미지 사이 최소 간격 유지
4. 리스트나 표 직전에는 이미지 배치 금지

네이버 에디터 지원:
- 마크다운 → 플레인 텍스트 변환 (**, ##, - 등 제거)
- 이미지 마커 삽입으로 분산 배치 지원
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class ImagePlacement:
    """이미지 배치 정보."""
    path: str
    alt_text: str
    position: int  # 삽입할 문자 위치
    placement_type: str  # thumbnail, content


@dataclass
class ImageInsertionPoint:
    """이미지 삽입 지점 정보 (네이버 에디터용)."""
    index: int           # 순서 (0=썸네일, 1~N=본문 이미지)
    path: str            # 이미지 파일 경로
    marker: str          # 마커 문자열 (예: [IMG_1])
    section_hint: str    # 삽입할 섹션 힌트
    is_thumbnail: bool   # 썸네일 여부


def optimize_image_placement(
    content: str,
    thumbnail_path: Optional[str] = None,
    content_image_paths: Optional[List[str]] = None,
    image_concepts: Optional[List[str]] = None,
    min_gap_chars: int = 300,
) -> str:
    """콘텐츠에 이미지를 최적 위치에 배치한다.

    Args:
        content: 마크다운 콘텐츠
        thumbnail_path: 썸네일 이미지 경로
        content_image_paths: 본문 이미지 경로 목록
        image_concepts: 이미지 설명 목록 (alt 텍스트용)
        min_gap_chars: 이미지 사이 최소 간격 (문자 수)

    Returns:
        이미지가 배치된 마크다운 콘텐츠
    """
    if not thumbnail_path and not content_image_paths:
        return content

    content_image_paths = content_image_paths or []
    image_concepts = image_concepts or []

    # H2 섹션 위치 찾기
    h2_positions = _find_h2_positions(content)

    # 이미지 배치 계획 수립
    placements: List[ImagePlacement] = []
    last_image_pos = -min_gap_chars  # 초기값 (첫 이미지 바로 배치 가능)

    # 1. 썸네일 배치 (첫 H2 전)
    if thumbnail_path:
        if h2_positions:
            thumb_pos = h2_positions[0]
        else:
            # H2가 없으면 첫 문단 후
            first_para_end = content.find("\n\n")
            thumb_pos = first_para_end + 1 if first_para_end > 0 else 0

        alt_text = image_concepts[0] if image_concepts else "썸네일 이미지"
        placements.append(ImagePlacement(
            path=thumbnail_path,
            alt_text=alt_text,
            position=thumb_pos,
            placement_type="thumbnail",
        ))
        last_image_pos = thumb_pos

    # 2. 본문 이미지 배치 (H2 섹션 사이에 분산)
    if content_image_paths and len(h2_positions) > 1:
        # 배치 가능한 H2 위치 필터링 (min_gap 고려)
        available_positions = []
        for i, pos in enumerate(h2_positions[1:], 1):  # 첫 H2는 썸네일용
            if pos - last_image_pos >= min_gap_chars:
                # 리스트나 표 직전인지 확인
                if not _is_before_list_or_table(content, pos):
                    available_positions.append((i, pos))

        # 이미지 균등 배치
        if available_positions:
            step = max(1, len(available_positions) // (len(content_image_paths) + 1))
            for img_idx, img_path in enumerate(content_image_paths[:4]):  # 최대 4개
                pos_idx = min((img_idx + 1) * step, len(available_positions) - 1)
                _, insert_pos = available_positions[pos_idx]

                if insert_pos - last_image_pos >= min_gap_chars:
                    alt_idx = img_idx + 1 if thumbnail_path else img_idx
                    alt_text = (
                        image_concepts[alt_idx]
                        if alt_idx < len(image_concepts)
                        else f"본문 이미지 {img_idx + 1}"
                    )
                    placements.append(ImagePlacement(
                        path=img_path,
                        alt_text=alt_text,
                        position=insert_pos,
                        placement_type="content",
                    ))
                    last_image_pos = insert_pos

    # 3. 이미지 삽입 (뒤에서부터 삽입하여 위치 유지)
    placements.sort(key=lambda p: p.position, reverse=True)

    result = content
    for placement in placements:
        image_md = _create_image_markdown(placement)
        result = result[:placement.position] + image_md + result[placement.position:]

    return result


def _find_h2_positions(content: str) -> List[int]:
    """H2 헤딩의 시작 위치를 찾는다."""
    positions = []
    for match in re.finditer(r"^##\s+.+$", content, re.MULTILINE):
        positions.append(match.start())
    return positions


def _is_before_list_or_table(content: str, position: int) -> bool:
    """해당 위치 직후에 리스트나 표가 있는지 확인한다."""
    # H2 이후 내용 추출
    after_h2 = content[position:]
    lines_after = after_h2.split("\n")[1:4]  # H2 다음 3줄

    for line in lines_after:
        stripped = line.strip()
        if not stripped:
            continue
        # 리스트 시작
        if stripped.startswith(("-", "*", "1.", "2.", "3.")):
            return True
        # 표 시작
        if stripped.startswith("|"):
            return True

    return False


def _create_image_markdown(placement: ImagePlacement) -> str:
    """이미지 마크다운을 생성한다."""
    # 파일명에서 안전한 alt 텍스트 생성
    alt_text = placement.alt_text.replace('"', "'").replace("\n", " ")

    if placement.placement_type == "thumbnail":
        return f"\n\n![{alt_text}]({placement.path})\n\n"
    else:
        return f"\n\n![{alt_text}]({placement.path})\n\n"


def extract_image_concepts_from_placements(
    placements: List[dict],
) -> Tuple[List[str], List[str]]:
    """LLM이 생성한 배치 정보에서 개념을 추출한다.

    Returns:
        (prompts, concepts) 튜플
    """
    prompts = []
    concepts = []

    for p in placements:
        if p.get("prompt"):
            prompts.append(p["prompt"])
        if p.get("concept"):
            concepts.append(p["concept"])
        elif p.get("prompt"):
            # 프롬프트에서 개념 추출 (앞 50자)
            concepts.append(p["prompt"][:50])

    return prompts, concepts


def create_naver_html_with_images(
    content: str,
    thumbnail_path: Optional[str] = None,
    content_image_paths: Optional[List[str]] = None,
) -> str:
    """네이버 블로그용 HTML로 변환하며 이미지를 배치한다.

    네이버 에디터는 마크다운을 지원하지 않으므로,
    이미지 경로는 실제 업로드 후 URL로 대체되어야 한다.
    """
    # 마크다운 → HTML 변환 (간단 버전)
    html = content

    # H2 → <h2>
    html = re.sub(r"^## (.+)$", r"<h2>\1</h2>", html, flags=re.MULTILINE)

    # H3 → <h3>
    html = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html, flags=re.MULTILINE)

    # 굵게 → <strong>
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)

    # 리스트 → <ul><li>
    lines = html.split("\n")
    result_lines = []
    in_list = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- "):
            if not in_list:
                result_lines.append("<ul>")
                in_list = True
            result_lines.append(f"<li>{stripped[2:]}</li>")
        else:
            if in_list:
                result_lines.append("</ul>")
                in_list = False
            # 빈 줄은 <br> 또는 <p> 처리
            if stripped:
                result_lines.append(f"<p>{stripped}</p>")
            else:
                result_lines.append("<br>")

    if in_list:
        result_lines.append("</ul>")

    return "\n".join(result_lines)


# ============================================================================
# 네이버 에디터용 마크다운 변환 (플레인 텍스트)
# ============================================================================

def convert_markdown_for_naver_editor(content: str) -> str:
    """마크다운을 네이버 에디터용 플레인 텍스트로 변환한다.

    네이버 스마트 에디터 ONE은 플레인 텍스트를 받으므로
    마크다운 문법을 시각적으로 구분 가능한 형태로 변환한다.

    변환 규칙:
    - ## 헤딩 → 빈 줄 + 텍스트 + 빈 줄 (시각적 구분)
    - **강조** → 텍스트만 (마크다운 제거)
    - - 리스트 → • 불릿
    - 1. 번호 → 1. 유지
    - 이미지 마크다운 → 제거 (별도 처리)

    Args:
        content: 마크다운 콘텐츠

    Returns:
        네이버 에디터에 붙여넣을 플레인 텍스트
    """
    result = content

    # 1. 이미지 마크다운 제거 (![alt](path) 형식)
    result = re.sub(r"!\[[^\]]*\]\([^)]+\)\s*", "", result)

    # 2. H2 헤딩: ## 제목 → \n\n■ 제목\n\n (시각적 구분)
    result = re.sub(
        r"^##\s+(.+)$",
        r"\n\n■ \1\n",
        result,
        flags=re.MULTILINE,
    )

    # 3. H3 헤딩: ### 제목 → \n▶ 제목\n
    result = re.sub(
        r"^###\s+(.+)$",
        r"\n▶ \1\n",
        result,
        flags=re.MULTILINE,
    )

    # 4. 굵은 글씨: **텍스트** → 텍스트
    result = re.sub(r"\*\*(.+?)\*\*", r"\1", result)

    # 5. 기울임: *텍스트* 또는 _텍스트_ → 텍스트
    result = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", result)
    result = re.sub(r"_(.+?)_", r"\1", result)

    # 6. 리스트: - 항목 → • 항목
    result = re.sub(r"^-\s+", "• ", result, flags=re.MULTILINE)
    result = re.sub(r"^\*\s+", "• ", result, flags=re.MULTILINE)

    # 7. 인라인 코드: `코드` → 코드
    result = re.sub(r"`([^`]+)`", r"\1", result)

    # 8. 링크: [텍스트](URL) → 텍스트
    result = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", result)

    # 9. 연속된 빈 줄 정리 (3개 이상 → 2개)
    result = re.sub(r"\n{3,}", "\n\n", result)

    # 10. 앞뒤 공백 정리
    result = result.strip()

    return result


def create_naver_editor_content(
    content: str,
    thumbnail_path: Optional[str] = None,
    content_image_paths: Optional[List[str]] = None,
    image_concepts: Optional[List[str]] = None,
    min_gap_chars: int = 400,
) -> Tuple[str, List[ImageInsertionPoint]]:
    """마크다운을 네이버 에디터용 텍스트로 변환하고 이미지 삽입 위치를 반환한다.

    Args:
        content: 마크다운 콘텐츠
        thumbnail_path: 썸네일 이미지 경로
        content_image_paths: 본문 이미지 경로 목록
        image_concepts: 이미지 설명 (alt 텍스트용)
        min_gap_chars: 이미지 사이 최소 간격 (문자 수)

    Returns:
        (변환된 텍스트, 이미지 삽입 위치 리스트)
        이미지 위치에는 [IMG_N] 마커가 삽입됨
    """
    content_image_paths = content_image_paths or []
    image_concepts = image_concepts or []

    # 1. 마크다운 → 플레인 텍스트 변환
    plain_text = convert_markdown_for_naver_editor(content)

    # 2. 섹션 위치 찾기 (■ 로 시작하는 줄 = H2 변환 결과)
    section_positions = _find_section_positions(plain_text)

    # 3. 이미지 삽입 계획 수립
    insertion_points: List[ImageInsertionPoint] = []
    markers_to_insert: List[Tuple[int, str, int]] = []  # (위치, 마커, 인덱스)

    img_index = 0

    # 3a. 썸네일 (첫 섹션 직전)
    if thumbnail_path:
        if section_positions:
            pos = section_positions[0]
        else:
            # 섹션이 없으면 첫 문단 후
            first_para = plain_text.find("\n\n")
            pos = first_para + 1 if first_para > 0 else 0

        marker = f"[IMG_{img_index}]"
        insertion_points.append(ImageInsertionPoint(
            index=img_index,
            path=thumbnail_path,
            marker=marker,
            section_hint="도입부",
            is_thumbnail=True,
        ))
        markers_to_insert.append((pos, marker, img_index))
        img_index += 1

    # 3b. 본문 이미지 (섹션 사이에 분산)
    if content_image_paths and len(section_positions) > 1:
        # 사용 가능한 위치 계산 (min_gap 고려)
        available = []
        last_pos = markers_to_insert[-1][0] if markers_to_insert else -min_gap_chars

        for i, pos in enumerate(section_positions[1:], 1):
            if pos - last_pos >= min_gap_chars:
                # 섹션 헤딩 추출 (힌트용)
                section_line = plain_text[pos:].split("\n")[0]
                available.append((pos, section_line.strip()))

        # 이미지 균등 분배
        if available:
            num_images = min(len(content_image_paths), 4)  # 최대 4개
            step = max(1, len(available) // (num_images + 1))

            for i, img_path in enumerate(content_image_paths[:num_images]):
                pos_idx = min((i + 1) * step, len(available) - 1)
                pos, section_hint = available[pos_idx]

                marker = f"[IMG_{img_index}]"
                alt_idx = img_index if not thumbnail_path else img_index - 1
                concept = (
                    image_concepts[alt_idx]
                    if alt_idx < len(image_concepts)
                    else section_hint
                )

                insertion_points.append(ImageInsertionPoint(
                    index=img_index,
                    path=img_path,
                    marker=marker,
                    section_hint=concept[:30] if concept else f"본문 {i + 1}",
                    is_thumbnail=False,
                ))
                markers_to_insert.append((pos, marker, img_index))
                img_index += 1

    # 4. 마커 삽입 (뒤에서부터 삽입하여 위치 유지)
    markers_to_insert.sort(key=lambda x: x[0], reverse=True)
    result = plain_text

    for pos, marker, _ in markers_to_insert:
        # 마커는 빈 줄로 감싸 단독 배치 (콜라주 방지용 단락 분리)
        result = result[:pos] + f"\n\n{marker}\n\n" + result[pos:]

    return result, insertion_points


def _find_section_positions(text: str) -> List[int]:
    """섹션 (■ 로 시작하는 줄)의 시작 위치를 찾는다."""
    positions = []
    for match in re.finditer(r"^■\s+.+$", text, re.MULTILINE):
        positions.append(match.start())
    return positions


def remove_image_markers(text: str) -> str:
    """이미지 마커 [IMG_N]를 제거한다."""
    return re.sub(r"\[IMG_\d+\]\n?", "", text)
