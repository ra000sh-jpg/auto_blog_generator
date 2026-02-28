"""발행 이력을 LLM 프롬프트 텍스트로 변환한다.

MCP Memory가 줄 수 있는 텍스트 컨텍스트보다 구조화된,
블로그 작성에 특화된 주입 텍스트를 생성한다.
"""

from __future__ import annotations

from typing import List


_MEMORY_HEADER = "[발행 이력 메모리 — 글쓰기 전략에 반영]"

_RULES = """\
작성 규칙:
1) 위 주제와 키워드가 65% 이상 겹치면 반드시 새로운 각도·심화 내용으로 접근할 것
2) '관련 과거 글' 항목은 본문에 자연스럽게 언급 가능 ("이전 포스팅에서 다룬 것처럼...", "[관련글]" 형식)
3) 동일 키워드 반복 시 독자에게 새로운 가치를 1개 이상 제공할 것
4) 결과 URL이 있는 과거 글은 본문 말미 '참고' 섹션에 링크로 포함 가능"""

_DUPLICATE_WARNING = """\
⚠️ 주의: 이 주제는 최근 발행글과 유사도가 높습니다 ({sim:.0%}).
반드시 차별화된 관점, 최신 정보, 또는 더 깊은 심화 내용을 포함하세요."""


def build_memory_context_text(
    recent_posts: List[dict],
    similar_posts: List[dict],
    max_recent: int = 5,
    max_similar: int = 3,
    duplicate_threshold: float = 0.65,
) -> str:
    """메모리 컨텍스트 주입 텍스트를 생성한다.

    Args:
        recent_posts: query_topic_memory() 반환값 (같은 토픽 최근 글)
        similar_posts: find_similar_posts() 반환값 (유사 키워드 글, similarity 포함)
        max_recent: 최근글 최대 표시 수
        max_similar: 유사글 최대 표시 수
        duplicate_threshold: 중복 경고 발동 임계값

    Returns:
        LLM 시스템 프롬프트에 주입할 텍스트. 데이터 없으면 빈 문자열.
    """
    if not recent_posts and not similar_posts:
        return ""

    lines: List[str] = [_MEMORY_HEADER, ""]

    # 섹션 1: 최근 같은 카테고리 글
    if recent_posts:
        lines.append("▶ 최근 같은 카테고리에서 다룬 주제 (중복 주의):")
        for i, post in enumerate(recent_posts[:max_recent], start=1):
            date_str = str(post.get("recorded_at", ""))[:10]
            score = int(post.get("quality_score", 0))
            url = str(post.get("result_url", "")).strip()
            title = str(post.get("title", "")).strip()
            score_tag = f", 품질:{score}" if score > 0 else ""
            url_tag = f" {url}" if url else ""
            lines.append(f"{i}. {title} [{date_str}{score_tag}]{url_tag}")
        lines.append("")

    # 섹션 2: 유사 키워드 과거 글 (내부 링크 후보)
    high_sim_posts = [p for p in similar_posts if p.get("similarity", 0) >= duplicate_threshold]
    link_posts = [p for p in similar_posts if p.get("similarity", 0) < duplicate_threshold]

    if high_sim_posts:
        top = high_sim_posts[0]
        lines.append(
            _DUPLICATE_WARNING.format(sim=top.get("similarity", 0))
        )
        lines.append("")

    if link_posts:
        lines.append("▶ 키워드 유사 과거 글 (내부 링크 후보, 유사도순):")
        for i, post in enumerate(link_posts[:max_similar], start=1):
            sim_pct = int(post.get("similarity", 0) * 100)
            url = str(post.get("result_url", "")).strip()
            title = str(post.get("title", "")).strip()
            url_tag = f" {url}" if url else ""
            lines.append(f"{i}. {title} [유사:{sim_pct}%]{url_tag}")
        lines.append("")

    lines.append(_RULES)
    return "\n".join(lines)


def is_duplicate_topic(similar_posts: List[dict], threshold: float = 0.65) -> bool:
    """유사 포스트 중 threshold 이상이 있으면 중복으로 판단."""
    if not similar_posts:
        return False
    return any(p.get("similarity", 0) >= threshold for p in similar_posts)
