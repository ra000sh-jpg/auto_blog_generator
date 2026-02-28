"""시맨틱 중복 검사용 임베딩 프로바이더 추상화."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, List, Optional, Protocol, Sequence

import httpx

logger = logging.getLogger(__name__)


class EmbeddingProvider(Protocol):
    """임베딩 프로바이더 인터페이스."""

    @property
    def model_name(self) -> str:  # pragma: no cover - protocol
        ...

    async def embed_texts(self, texts: Sequence[str]) -> List[List[float]]:  # pragma: no cover - protocol
        ...


def _normalize_texts(texts: Sequence[str]) -> List[str]:
    """임베딩 요청 전에 입력 텍스트를 정리한다."""
    payload: List[str] = []
    for text in texts:
        normalized = str(text or "").strip()
        if normalized:
            payload.append(normalized)
    return payload


def _resolve_local_model_name(model_name: str) -> str:
    """로컬 임베딩 모델 별칭을 실제 식별자로 변환한다."""
    normalized = str(model_name or "").strip().lower()
    alias_map = {
        # 한국어/다국어 지원을 우선한다.
        "bge-small-ko": "BAAI/bge-m3",
        "bge-m3": "BAAI/bge-m3",
    }
    return alias_map.get(normalized, str(model_name or "").strip() or "BAAI/bge-m3")


@dataclass
class LocalEmbeddingProvider:
    """sentence-transformers 기반 로컬 임베딩 프로바이더."""

    requested_model_name: str = "bge-small-ko"
    timeout_sec: float = 4.0

    def __post_init__(self) -> None:
        self._resolved_model_name = _resolve_local_model_name(self.requested_model_name)
        self._model: Optional[Any] = None

    @property
    def model_name(self) -> str:
        return self.requested_model_name

    def _get_model(self) -> Any:
        """모델을 지연 로딩한다."""
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except Exception as exc:
            raise RuntimeError("sentence-transformers unavailable") from exc

        self._model = SentenceTransformer(self._resolved_model_name, device="cpu")
        return self._model

    async def embed_texts(self, texts: Sequence[str]) -> List[List[float]]:
        """텍스트 목록을 임베딩 벡터로 변환한다."""
        payload = _normalize_texts(texts)
        if not payload:
            return []

        model = await asyncio.to_thread(self._get_model)
        try:
            encoded = await asyncio.wait_for(
                asyncio.to_thread(
                    model.encode,
                    payload,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                ),
                timeout=max(1.0, float(self.timeout_sec)),
            )
        except Exception as exc:
            raise RuntimeError("local embedding encode failed") from exc

        vectors: List[List[float]] = []
        for vector in encoded:
            try:
                vectors.append([float(value) for value in vector])
            except Exception:
                vectors.append([])
        return vectors


@dataclass
class OpenAIEmbeddingProvider:
    """OpenAI Embeddings API 프로바이더."""

    api_key: str
    model: str = "text-embedding-3-small"
    timeout_sec: float = 8.0
    base_url: str = "https://api.openai.com/v1"

    @property
    def model_name(self) -> str:
        return self.model

    async def embed_texts(self, texts: Sequence[str]) -> List[List[float]]:
        """OpenAI API로 임베딩을 조회한다."""
        payload = _normalize_texts(texts)
        if not payload:
            return []

        url = f"{self.base_url.rstrip('/')}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "input": payload,
        }
        async with httpx.AsyncClient(timeout=max(1.0, float(self.timeout_sec))) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()

        rows = data.get("data", [])
        ordered = sorted(rows, key=lambda row: int(row.get("index", 0)))
        vectors: List[List[float]] = []
        for row in ordered:
            embedding = row.get("embedding", [])
            vectors.append([float(value) for value in embedding])
        return vectors


def build_embedding_provider(memory_config: Any) -> Optional[EmbeddingProvider]:
    """설정 기반 임베딩 프로바이더를 생성한다."""
    if not memory_config or not bool(getattr(memory_config, "semantic_enabled", False)):
        return None

    provider_name = str(getattr(memory_config, "semantic_provider", "local")).strip().lower()
    model_name = str(getattr(memory_config, "semantic_model", "bge-small-ko")).strip() or "bge-small-ko"
    timeout_sec = float(getattr(memory_config, "embedding_timeout_sec", 4.0))

    if provider_name == "openai":
        api_key = str(os.getenv("OPENAI_API_KEY", "")).strip()
        if not api_key:
            logger.warning("Semantic provider=openai but OPENAI_API_KEY is missing, fallback lexical-only")
            return None
        return OpenAIEmbeddingProvider(
            api_key=api_key,
            model=model_name or "text-embedding-3-small",
            timeout_sec=max(1.0, timeout_sec),
        )

    return LocalEmbeddingProvider(
        requested_model_name=model_name,
        timeout_sec=max(1.0, timeout_sec),
    )

