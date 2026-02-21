"""2단 검색 기반 RAG 엔진."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..collectors.rss_news_collector import RssNewsCollector

logger = logging.getLogger(__name__)


@dataclass
class RetrievalStats:
    """검색/재정렬 통계."""

    candidate_count: int = 0
    selected_count: int = 0
    reranker: str = "lexical"
    model_name: str = ""


class CrossEncoderRagSearchEngine:
    """Bi-Encoder 후보 + Cross-Encoder 재정렬 엔진."""

    def __init__(
        self,
        *,
        news_collector: Optional[RssNewsCollector] = None,
        cross_encoder_model: str = "BAAI/bge-reranker-base",
        candidate_top_k: int = 20,
        final_top_k: int = 2,
        cross_encoder_enabled: bool = True,
    ):
        self.news_collector = news_collector or RssNewsCollector()
        self.cross_encoder_model = cross_encoder_model
        self.candidate_top_k = max(5, candidate_top_k)
        self.final_top_k = max(1, final_top_k)
        self.cross_encoder_enabled = bool(cross_encoder_enabled)

        self._cross_encoder: Optional[Any] = None
        self._cross_encoder_load_failed = False
        self.last_stats = RetrievalStats()

    def retrieve(
        self,
        *,
        keywords: Sequence[str],
        query_text: str = "",
        within_hours: int = 24,
    ) -> List[Dict[str, str]]:
        """키워드 기반으로 후보를 수집하고 상위 문서를 반환한다."""
        try:
            candidates = self.news_collector.fetch_relevant_news(
                keywords=list(keywords),
                within_hours=within_hours,
                max_items=self.candidate_top_k,
            )
        except TypeError:
            # 하위 호환: 테스트 더블/구버전 시그니처(max_items만 지원)
            candidates = self.news_collector.fetch_relevant_news(
                keywords=list(keywords),
                max_items=self.candidate_top_k,
            )
        if not candidates:
            self.last_stats = RetrievalStats(candidate_count=0, selected_count=0, reranker="none")
            return []

        resolved_query = str(query_text or "").strip()
        if not resolved_query:
            resolved_query = " ".join(str(item).strip() for item in keywords if str(item).strip())

        ranked_docs, reranker_name, model_name = self._rerank_documents(
            query_text=resolved_query,
            candidates=candidates,
        )
        selected = ranked_docs[: self.final_top_k]
        self.last_stats = RetrievalStats(
            candidate_count=len(candidates),
            selected_count=len(selected),
            reranker=reranker_name,
            model_name=model_name,
        )
        return selected

    def _rerank_documents(
        self,
        *,
        query_text: str,
        candidates: List[Dict[str, str]],
    ) -> Tuple[List[Dict[str, str]], str, str]:
        """후보 문서를 재정렬한다."""
        normalized_query = str(query_text or "").strip()
        if not normalized_query:
            return candidates, "lexical", ""

        scored: List[Tuple[float, Dict[str, str]]] = []
        cross_encoder = self._get_cross_encoder()
        if cross_encoder is not None:
            try:
                pairs = [
                    (
                        normalized_query,
                        self._build_doc_text(item),
                    )
                    for item in candidates
                ]
                predictions = cross_encoder.predict(pairs)
                for doc, score in zip(candidates, predictions):
                    scored_doc = dict(doc)
                    scored_doc["rerank_score"] = f"{float(score):.6f}"
                    scored_doc["rerank_method"] = "cross_encoder"
                    scored.append((float(score), scored_doc))
                scored.sort(key=lambda item: item[0], reverse=True)
                return (
                    [item[1] for item in scored],
                    "cross_encoder",
                    self.cross_encoder_model,
                )
            except Exception as exc:
                logger.warning("Cross-Encoder rerank failed, fallback lexical: %s", exc)

        # Cross-Encoder 미사용/실패 시 lexical 재정렬로 계속 진행한다.
        for doc in candidates:
            lexical_score = self._lexical_score(query_text=normalized_query, document_text=self._build_doc_text(doc))
            scored_doc = dict(doc)
            scored_doc["rerank_score"] = f"{lexical_score:.6f}"
            scored_doc["rerank_method"] = "lexical"
            scored.append((lexical_score, scored_doc))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored], "lexical", ""

    def _get_cross_encoder(self) -> Optional[Any]:
        """Cross-Encoder 인스턴스를 지연 로딩한다."""
        if not self.cross_encoder_enabled or self._cross_encoder_load_failed:
            return None
        if self._cross_encoder is not None:
            return self._cross_encoder

        try:
            from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]
        except Exception as exc:
            logger.warning("sentence-transformers unavailable, lexical rerank only: %s", exc)
            self._cross_encoder_load_failed = True
            return None

        try:
            # 로컬 CPU 환경을 기본으로 사용한다.
            self._cross_encoder = CrossEncoder(self.cross_encoder_model, device="cpu")
            return self._cross_encoder
        except Exception as exc:
            logger.warning("Cross-Encoder model load failed, lexical rerank only: %s", exc)
            self._cross_encoder_load_failed = True
            return None

    def _build_doc_text(self, doc: Dict[str, str]) -> str:
        """문서 재정렬용 결합 텍스트를 만든다."""
        title = str(doc.get("title", "")).strip()
        content = str(doc.get("content", "")).strip()
        return f"{title}\n{content}".strip()

    def _lexical_score(self, *, query_text: str, document_text: str) -> float:
        """가벼운 토큰 기반 점수를 계산한다."""
        query_tokens = self._tokenize(query_text)
        if not query_tokens:
            return 0.0

        document_tokens = self._tokenize(document_text)
        if not document_tokens:
            return 0.0

        query_set = set(query_tokens)
        document_set = set(document_tokens)
        overlap = len(query_set.intersection(document_set))
        coverage = overlap / max(1, len(query_set))
        density = overlap / max(1, len(document_set))
        document_lower = document_text.lower()
        substring_hits = sum(1 for token in query_set if token in document_lower)
        substring_coverage = substring_hits / max(1, len(query_set))

        # 구문 일치 보너스: 질의 전체가 포함되면 가점
        query_phrase = re.sub(r"\s+", " ", query_text.strip().lower())
        doc_text = re.sub(r"\s+", " ", document_lower.strip())
        phrase_bonus = 0.15 if query_phrase and query_phrase in doc_text else 0.0

        return (coverage * 0.55) + (substring_coverage * 0.35) + (density * 0.1) + phrase_bonus

    def _tokenize(self, text: str) -> List[str]:
        """한글/영문/숫자 토큰을 추출한다."""
        lowered = str(text or "").lower()
        if not lowered:
            return []
        return re.findall(r"[가-힣a-z0-9]{2,}", lowered)
