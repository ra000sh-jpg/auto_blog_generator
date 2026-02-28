import asyncio
from pathlib import Path
from typing import Any, Dict, List, Sequence

from modules.automation.job_store import JobConfig, JobStore
from modules.config import MemoryConfig
from modules.memory.embedding_provider import LocalEmbeddingProvider, build_embedding_provider
from modules.memory.gap_analyzer import GapAnalyzer
from modules.memory.hybrid_similarity import (
    compute_semantic_scores,
    find_similar_posts_with_optional_semantic,
    should_apply_semantic,
)
from modules.memory.topic_store import TopicMemoryStore


def _build_store(tmp_path: Path, name: str = "memory_semantic_phaseA.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


def _insert_sample_topic_memory(store: JobStore) -> None:
    store.insert_topic_memory(
        job_id="sem-1",
        title="강아지 훈련 시작 가이드",
        keywords=["강아지", "훈련"],
        topic_mode="it",
        platform="naver",
        persona_id="P1",
        summary="summary",
        result_url="https://example.com/1",
        quality_score=90,
    )
    store.insert_topic_memory(
        job_id="sem-2",
        title="반려견 교육 기본기",
        keywords=["반려견", "교육"],
        topic_mode="it",
        platform="naver",
        persona_id="P1",
        summary="summary",
        result_url="https://example.com/2",
        quality_score=88,
    )


def test_topic_embedding_crud_and_candidates(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    _insert_sample_topic_memory(store)

    store.upsert_topic_embedding(
        job_id="sem-1",
        embedding=[0.1, 0.2, 0.3],
        model_name="bge-small-ko",
    )
    found = store.get_topic_embeddings(["sem-1", "sem-2"], model_name="bge-small-ko")
    assert "sem-1" in found
    assert found["sem-1"] == [0.1, 0.2, 0.3]
    assert "sem-2" not in found

    candidates = store.list_topic_embedding_candidates(topic_mode="it", platform="naver", limit=5)
    assert len(candidates) == 2
    assert all(item["topic_mode"] == "it" for item in candidates)
    assert all(item["platform"] == "naver" for item in candidates)


def test_build_embedding_provider_local_default() -> None:
    config = MemoryConfig(semantic_enabled=True, semantic_provider="local", semantic_model="bge-small-ko")
    provider = build_embedding_provider(config)
    assert isinstance(provider, LocalEmbeddingProvider)
    assert provider.model_name == "bge-small-ko"


def test_build_embedding_provider_openai_without_key_returns_none(monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = MemoryConfig(semantic_enabled=True, semantic_provider="openai", semantic_model="text-embedding-3-small")
    provider = build_embedding_provider(config)
    assert provider is None


def test_should_apply_semantic_canary_topic() -> None:
    config = MemoryConfig(semantic_enabled=True, semantic_canary_topic="it")
    assert should_apply_semantic(config, "it") is True
    assert should_apply_semantic(config, "finance") is False


def test_find_similar_posts_with_optional_semantic_prefers_semantic_score() -> None:
    candidates: List[Dict[str, Any]] = [
        {"job_id": "a", "title": "강아지 훈련 루틴", "keywords": ["강아지", "훈련"]},
        {"job_id": "b", "title": "반려견 교육 루틴", "keywords": ["반려견", "교육"]},
    ]
    semantic_scores = {"a": 0.30, "b": 0.92}
    result = find_similar_posts_with_optional_semantic(
        title="반려견 교육 시작",
        keywords=["반려견", "교육"],
        candidates=candidates,
        threshold=0.0,
        top_k=2,
        semantic_enabled=True,
        semantic_scores=semantic_scores,
        lexical_weight=0.45,
        semantic_weight=0.55,
    )
    assert len(result) == 2
    assert result[0]["job_id"] == "b"
    assert "semantic_similarity" in result[0]


def test_find_similar_posts_with_optional_semantic_off_uses_lexical() -> None:
    candidates: List[Dict[str, Any]] = [
        {"job_id": "a", "title": "강아지 훈련 루틴", "keywords": ["강아지", "훈련"]},
        {"job_id": "b", "title": "반려견 교육 루틴", "keywords": ["반려견", "교육"]},
    ]
    result = find_similar_posts_with_optional_semantic(
        title="강아지 훈련 시작",
        keywords=["강아지", "훈련"],
        candidates=candidates,
        threshold=0.2,
        top_k=1,
        semantic_enabled=False,
    )
    assert len(result) == 1
    assert result[0]["job_id"] == "a"


def test_compute_semantic_scores_with_stub_provider(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    _insert_sample_topic_memory(store)

    class StubProvider:
        model_name = "bge-small-ko"

        async def embed_texts(self, texts: Sequence[str]) -> List[List[float]]:
            vectors: List[List[float]] = []
            for text in texts:
                normalized = str(text)
                if "강아지" in normalized:
                    vectors.append([1.0, 0.0])
                elif "반려견" in normalized:
                    vectors.append([0.0, 1.0])
                else:
                    vectors.append([0.7, 0.3])
            return vectors

    candidates = store.query_topic_memory(topic_mode="it", platform="naver", limit=10)
    scores = store.get_topic_embeddings(["sem-1", "sem-2"], model_name="bge-small-ko")
    assert scores == {}

    semantic_scores = asyncio.run(
        compute_semantic_scores(
            title="반려견 교육 가이드",
            keywords=["반려견", "교육"],
            candidates=candidates,
            embedding_provider=StubProvider(),
            job_store=store,
            model_name="bge-small-ko",
            max_candidates=10,
        )
    )
    assert "sem-1" in semantic_scores
    assert "sem-2" in semantic_scores
    assert semantic_scores["sem-2"] > semantic_scores["sem-1"]


def test_gap_analyzer_uses_semantic_on_canary_topic(tmp_path: Path, monkeypatch: Any) -> None:
    store = _build_store(tmp_path, name="semantic_gap_canary.db")
    store.insert_topic_memory(
        job_id="gap-1",
        title="금리 인상 전망",
        keywords=["금리", "전망"],
        topic_mode="it",
        platform="naver",
        persona_id="P1",
        summary="summary",
        result_url="https://example.com/g1",
        quality_score=80,
    )
    store.insert_topic_memory(
        job_id="gap-2",
        title="채권 투자 기초",
        keywords=["채권", "투자"],
        topic_mode="it",
        platform="naver",
        persona_id="P1",
        summary="summary",
        result_url="https://example.com/g2",
        quality_score=80,
    )

    class StubProvider:
        model_name = "bge-small-ko"

        async def embed_texts(self, texts: Sequence[str]) -> List[List[float]]:
            del texts
            return [[0.1, 0.9]]

    async def _fake_scores(**kwargs: Any) -> Dict[str, float]:
        del kwargs
        return {"gap-1": 0.12, "gap-2": 0.95}

    monkeypatch.setattr("modules.memory.embedding_provider.build_embedding_provider", lambda cfg: StubProvider())
    monkeypatch.setattr("modules.memory.hybrid_similarity.compute_semantic_scores", _fake_scores)

    config = MemoryConfig(
        semantic_enabled=True,
        semantic_canary_topic="it",
        hybrid_threshold=0.5,
        lexical_weight=0.45,
        semantic_weight=0.55,
    )
    analyzer = GapAnalyzer(job_store=store, config=config)
    detected = analyzer.is_duplicate_before_job(
        title="반려견 교육 시작",
        keywords=["반려견", "교육"],
        topic_mode="it",
        platform="naver",
    )
    assert detected is True


def test_gap_analyzer_skips_semantic_outside_canary(tmp_path: Path, monkeypatch: Any) -> None:
    store = _build_store(tmp_path, name="semantic_gap_non_canary.db")
    store.insert_topic_memory(
        job_id="gap-x",
        title="금리 인상 전망",
        keywords=["금리", "전망"],
        topic_mode="finance",
        platform="naver",
        persona_id="P1",
        summary="summary",
        result_url="https://example.com/gx",
        quality_score=80,
    )

    async def _raise_scores(**kwargs: Any) -> Dict[str, float]:
        del kwargs
        raise AssertionError("semantic scoring should not run for non-canary topic")

    monkeypatch.setattr("modules.memory.hybrid_similarity.compute_semantic_scores", _raise_scores)

    config = MemoryConfig(
        semantic_enabled=True,
        semantic_canary_topic="it",
        precheck_duplicate_threshold=0.9,
    )
    analyzer = GapAnalyzer(job_store=store, config=config)
    detected = analyzer.is_duplicate_before_job(
        title="반려견 교육 시작",
        keywords=["반려견", "교육"],
        topic_mode="finance",
        platform="naver",
    )
    assert detected is False


def test_topic_memory_store_record_post_stores_embedding_on_canary(tmp_path: Path, monkeypatch: Any) -> None:
    store = _build_store(tmp_path, name="semantic_record_canary.db")
    memory_config = MemoryConfig(
        semantic_enabled=True,
        semantic_canary_topic="it",
        semantic_model="bge-small-ko",
        embedding_timeout_sec=2.0,
    )
    memory_store = TopicMemoryStore(job_store=store, config=memory_config)

    class StubProvider:
        model_name = "bge-small-ko"

        async def embed_texts(self, texts: Sequence[str]) -> List[List[float]]:
            del texts
            return [[0.3, 0.4, 0.5]]

    monkeypatch.setattr("modules.memory.embedding_provider.build_embedding_provider", lambda cfg: StubProvider())

    memory_store.record_post(
        job_id="record-canary-1",
        title="반려견 교육 시작",
        keywords=["반려견", "교육"],
        topic_mode="it",
        platform="naver",
        persona_id="P1",
        result_url="https://example.com/canary",
        quality_score=91,
    )
    embeddings = store.get_topic_embeddings(["record-canary-1"], model_name="bge-small-ko")
    assert embeddings.get("record-canary-1") == [0.3, 0.4, 0.5]


def test_topic_memory_store_record_post_skips_embedding_outside_canary(tmp_path: Path, monkeypatch: Any) -> None:
    store = _build_store(tmp_path, name="semantic_record_skip.db")
    memory_config = MemoryConfig(
        semantic_enabled=True,
        semantic_canary_topic="it",
        semantic_model="bge-small-ko",
    )
    memory_store = TopicMemoryStore(job_store=store, config=memory_config)

    class StubProvider:
        model_name = "bge-small-ko"

        async def embed_texts(self, texts: Sequence[str]) -> List[List[float]]:
            del texts
            raise AssertionError("outside canary topic should not request embeddings")

    monkeypatch.setattr("modules.memory.embedding_provider.build_embedding_provider", lambda cfg: StubProvider())

    memory_store.record_post(
        job_id="record-skip-1",
        title="반려견 교육 시작",
        keywords=["반려견", "교육"],
        topic_mode="finance",
        platform="naver",
        persona_id="P1",
        result_url="https://example.com/skip",
        quality_score=90,
    )
    embeddings = store.get_topic_embeddings(["record-skip-1"], model_name="bge-small-ko")
    assert embeddings == {}
