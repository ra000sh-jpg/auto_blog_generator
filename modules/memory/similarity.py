"""키워드·제목 기반 경량 유사도 계산.

MCP Knowledge Graph의 관계 추론 대신 도메인 최적화된 유사도를 사용한다.
외부 ML 라이브러리 없음. sentence-transformers 불필요.
"""

from __future__ import annotations

import re
from typing import List


def keyword_jaccard(keywords_a: List[str], keywords_b: List[str]) -> float:
    """키워드 집합 Jaccard 유사도 (0.0 ~ 1.0).

    두 글의 핵심 키워드 집합이 얼마나 겹치는지 측정.
    - 0.6 이상: 같은 주제 다른 각도
    - 0.8 이상: 사실상 중복
    """
    set_a = {k.lower().strip() for k in keywords_a if k.strip()}
    set_b = {k.lower().strip() for k in keywords_b if k.strip()}
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def title_token_overlap(title_a: str, title_b: str) -> float:
    """제목 토큰 겹침 비율 (0.0 ~ 1.0).

    한글 2자 이상 / 영문 3자 이상 토큰을 추출하여 비교.
    """
    def _tokenize(text: str) -> set:
        lowered = text.lower()
        ko_tokens = set(re.findall(r"[가-힣]{2,}", lowered))
        en_tokens = set(re.findall(r"[a-z]{3,}", lowered))
        return ko_tokens | en_tokens

    tokens_a = _tokenize(title_a)
    tokens_b = _tokenize(title_b)
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))


def combined_similarity(
    title: str,
    keywords: List[str],
    other_title: str,
    other_keywords: List[str],
    kw_weight: float = 0.65,
    title_weight: float = 0.35,
) -> float:
    """키워드(65%) + 제목(35%) 복합 유사도.

    Args:
        kw_weight: 키워드 Jaccard 가중치 (기본 0.65)
        title_weight: 제목 토큰 겹침 가중치 (기본 0.35)

    Returns:
        0.0 ~ 1.0 유사도 점수
    """
    kw_sim = keyword_jaccard(keywords, other_keywords)
    title_sim = title_token_overlap(title, other_title)
    return kw_weight * kw_sim + title_weight * title_sim


def find_similar_posts(
    title: str,
    keywords: List[str],
    candidates: List[dict],
    threshold: float = 0.3,
    top_k: int = 5,
) -> List[dict]:
    """후보 목록에서 유사한 과거 글을 찾는다.

    Args:
        candidates: query_topic_memory() 반환값
        threshold: 이 값 이상인 결과만 포함
        top_k: 반환할 최대 수

    Returns:
        유사도 내림차순 정렬된 과거 글 목록 (각 항목에 'similarity' 키 추가)
    """
    scored: list = []
    for post in candidates:
        sim = combined_similarity(
            title=title,
            keywords=keywords,
            other_title=str(post.get("title", "")),
            other_keywords=list(post.get("keywords", [])),
        )
        if sim >= threshold:
            scored.append({**post, "similarity": round(sim, 3)})

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:top_k]
