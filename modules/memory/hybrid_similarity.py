"""Hybrid(lexical + semantic) 유사도 계산 유틸."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .embedding_provider import EmbeddingProvider
from .similarity import combined_similarity, find_similar_posts

logger = logging.getLogger(__name__)


def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """코사인 유사도를 계산한다."""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for a, b in zip(vec_a, vec_b):
        dot += float(a) * float(b)
        norm_a += float(a) * float(a)
        norm_b += float(b) * float(b)
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / ((norm_a ** 0.5) * (norm_b ** 0.5))


def hybrid_score(
    lexical_score: float,
    semantic_score: float,
    lexical_weight: float = 0.45,
    semantic_weight: float = 0.55,
) -> float:
    """lexical + semantic 가중 평균 점수를 계산한다."""
    lw = max(0.0, float(lexical_weight))
    sw = max(0.0, float(semantic_weight))
    weight_sum = lw + sw
    if weight_sum <= 0:
        return max(0.0, min(1.0, float(lexical_score)))
    normalized_lexical = lw / weight_sum
    normalized_semantic = sw / weight_sum
    score = (float(lexical_score) * normalized_lexical) + (float(semantic_score) * normalized_semantic)
    return max(0.0, min(1.0, score))


def should_apply_semantic(memory_config: Any, topic_mode: str) -> bool:
    """시맨틱 검사 적용 여부를 판단한다."""
    if not memory_config or not bool(getattr(memory_config, "semantic_enabled", False)):
        return False
    canary_topic = str(getattr(memory_config, "semantic_canary_topic", "")).strip().lower()
    if not canary_topic:
        return True
    return str(topic_mode or "").strip().lower() == canary_topic


def _build_embedding_text(title: str, keywords: List[str]) -> str:
    """임베딩 입력 텍스트를 구성한다."""
    kw_text = ", ".join(str(keyword).strip() for keyword in keywords if str(keyword).strip())
    if kw_text:
        return f"{str(title).strip()}\n키워드: {kw_text}".strip()
    return str(title).strip()


async def compute_semantic_scores(
    *,
    title: str,
    keywords: List[str],
    candidates: List[Dict[str, Any]],
    embedding_provider: Optional[EmbeddingProvider],
    job_store: Any,
    model_name: str,
    max_candidates: int = 80,
) -> Dict[str, float]:
    """후보 문서의 semantic cosine 점수를 계산한다."""
    if embedding_provider is None or job_store is None:
        return {}

    max_n = max(1, int(max_candidates))
    filtered = [item for item in candidates if str(item.get("job_id", "")).strip()][:max_n]
    if not filtered:
        return {}

    job_ids = [str(item["job_id"]).strip() for item in filtered]
    get_fn = getattr(job_store, "get_topic_embeddings", None)
    upsert_fn = getattr(job_store, "upsert_topic_embedding", None)
    if not callable(get_fn):
        return {}

    existing = get_fn(job_ids, model_name=model_name)
    missing_payload: List[Dict[str, Any]] = []
    for item in filtered:
        job_id = str(item.get("job_id", "")).strip()
        if job_id and job_id not in existing:
            missing_payload.append(item)

    # 저장된 임베딩이 없으면 후보 임베딩을 계산해 캐시한다.
    if missing_payload:
        candidate_texts = [
            _build_embedding_text(
                title=str(item.get("title", "")),
                keywords=list(item.get("keywords", [])),
            )
            for item in missing_payload
        ]
        try:
            candidate_vectors = await embedding_provider.embed_texts(candidate_texts)
            for item, vector in zip(missing_payload, candidate_vectors):
                job_id = str(item.get("job_id", "")).strip()
                if not job_id or not vector:
                    continue
                if callable(upsert_fn):
                    upsert_fn(job_id=job_id, embedding=vector, model_name=model_name)
                existing[job_id] = vector
        except Exception as exc:
            logger.debug("Candidate semantic embedding failed (fallback lexical): %s", exc)

    # 쿼리 벡터 계산
    query_text = _build_embedding_text(title=title, keywords=keywords)
    if not query_text:
        return {}
    try:
        query_vectors = await embedding_provider.embed_texts([query_text])
    except Exception as exc:
        logger.debug("Query semantic embedding failed (fallback lexical): %s", exc)
        return {}
    if not query_vectors or not query_vectors[0]:
        return {}
    query_vector = query_vectors[0]

    scores: Dict[str, float] = {}
    for job_id in job_ids:
        candidate_vector = existing.get(job_id, [])
        if not candidate_vector:
            continue
        scores[job_id] = cosine_similarity(query_vector, candidate_vector)
    return scores


def find_hybrid_similar_posts(
    *,
    title: str,
    keywords: List[str],
    candidates: List[Dict[str, Any]],
    threshold: float = 0.58,
    top_k: int = 5,
    semantic_scores: Optional[Dict[str, float]] = None,
    lexical_weight: float = 0.45,
    semantic_weight: float = 0.55,
) -> List[Dict[str, Any]]:
    """Hybrid 점수로 유사한 글을 반환한다."""
    if not candidates:
        return []
    semantic_map = semantic_scores or {}
    payload: List[Dict[str, Any]] = []
    for post in candidates:
        lexical = combined_similarity(
            title=title,
            keywords=keywords,
            other_title=str(post.get("title", "")),
            other_keywords=list(post.get("keywords", [])),
        )
        semantic = float(semantic_map.get(str(post.get("job_id", "")), 0.0))
        score = hybrid_score(
            lexical_score=lexical,
            semantic_score=semantic,
            lexical_weight=lexical_weight,
            semantic_weight=semantic_weight,
        )
        if score < float(threshold):
            continue
        payload.append(
            {
                **post,
                "lexical_similarity": round(lexical, 3),
                "semantic_similarity": round(semantic, 3),
                "similarity": round(score, 3),
            }
        )
    payload.sort(key=lambda item: float(item.get("similarity", 0.0)), reverse=True)
    return payload[: max(1, int(top_k))]


def find_similar_posts_with_optional_semantic(
    *,
    title: str,
    keywords: List[str],
    candidates: List[Dict[str, Any]],
    threshold: float,
    top_k: int,
    semantic_enabled: bool,
    semantic_scores: Optional[Dict[str, float]] = None,
    lexical_weight: float = 0.45,
    semantic_weight: float = 0.55,
) -> List[Dict[str, Any]]:
    """semantic ON/OFF를 포함한 통합 호출 헬퍼."""
    if not semantic_enabled:
        return find_similar_posts(
            title=title,
            keywords=keywords,
            candidates=candidates,
            threshold=threshold,
            top_k=top_k,
        )
    return find_hybrid_similar_posts(
        title=title,
        keywords=keywords,
        candidates=candidates,
        threshold=threshold,
        top_k=top_k,
        semantic_scores=semantic_scores,
        lexical_weight=lexical_weight,
        semantic_weight=semantic_weight,
    )

