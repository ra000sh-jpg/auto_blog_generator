import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.pipeline_service import PipelineService
from modules.llm.claude_client import LLMResponse
from modules.llm.content_generator import ContentGenerator
from modules.uploaders.playwright_publisher import PublishResult


class FakeLLMClient:
    """E2E 파이프라인 시뮬레이션용 가짜 LLM 클라이언트."""

    def __init__(self, outputs: List[str]):
        self.outputs = list(outputs)
        self.calls: List[Dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return "qwen"

    async def generate_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        del max_retries, max_tokens
        if not self.outputs:
            raise RuntimeError("FakeLLMClient outputs exhausted")
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt[:300],
                "temperature": temperature,
            }
        )
        return LLMResponse(
            content=self.outputs.pop(0),
            input_tokens=240,
            output_tokens=380,
            model="fake-qwen-plus",
            stop_reason="end_turn",
        )


@dataclass
class FakeGeneratedImages:
    """이미지 생성 결과 더미."""

    thumbnail_path: str
    content_paths: List[str]
    source_kind_by_path: Dict[str, str]
    provider_by_path: Dict[str, str]


class FakeImageGenerator:
    """이미지 생성 파이프라인 시뮬레이션용 더미."""

    def __init__(self, base_dir: Path):
        self.thumb_path = base_dir / "thumb_ai.png"
        self.body_path = base_dir / "body_ai.png"
        self.thumb_path.write_bytes(b"fake-thumb")
        self.body_path.write_bytes(b"fake-body")

    async def generate_for_post(
        self,
        title: str,
        keywords: List[str],
        image_prompts: Optional[List[str]] = None,
    ) -> FakeGeneratedImages:
        del title, keywords, image_prompts
        source_kind = {
            str(self.thumb_path): "ai_generated",
            str(self.body_path): "ai_generated",
        }
        providers = {
            str(self.thumb_path): "huggingface",
            str(self.body_path): "huggingface",
        }
        return FakeGeneratedImages(
            thumbnail_path=str(self.thumb_path),
            content_paths=[str(self.body_path)],
            source_kind_by_path=source_kind,
            provider_by_path=providers,
        )


class PayloadDumpPublisher:
    """발행 직전 payload를 파일/콘솔에 덤프하는 더미 발행기."""

    def __init__(self, dump_path: Path):
        self.dump_path = dump_path
        self.last_payload: Dict[str, Any] = {}

    async def publish(
        self,
        title: str,
        content: str,
        thumbnail: Optional[str] = None,
        images: Optional[List[str]] = None,
        image_sources: Optional[Dict[str, Dict[str, str]]] = None,
        image_points: Optional[List[Any]] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> PublishResult:
        serialized_points: List[Dict[str, Any]] = []
        for point in image_points or []:
            if hasattr(point, "index"):
                serialized_points.append(
                    {
                        "index": int(getattr(point, "index", -1)),
                        "path": str(getattr(point, "path", "")),
                        "marker": str(getattr(point, "marker", "")),
                        "section_hint": str(getattr(point, "section_hint", "")),
                        "is_thumbnail": bool(getattr(point, "is_thumbnail", False)),
                    }
                )
            elif isinstance(point, dict):
                serialized_points.append(point)

        self.last_payload = {
            "title": title,
            "content": content,
            "thumbnail": thumbnail or "",
            "images": list(images or []),
            "image_sources": image_sources or {},
            "image_points": serialized_points,
            "tags": list(tags or []),
            "category": category or "",
        }
        self.dump_path.parent.mkdir(parents=True, exist_ok=True)
        self.dump_path.write_text(
            json.dumps(self.last_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("[E2E] Final publish payload dump:")
        print(json.dumps({"title": title, "content_preview": content[:400]}, ensure_ascii=False, indent=2))
        print(f"[E2E] Dump file: {self.dump_path}")
        return PublishResult(success=True, url="https://blog.naver.com/sim/e2e")


def _build_long_content(title: str) -> str:
    """품질 게이트 통과용 충분히 긴 마크다운 본문을 생성한다."""
    paragraph = (
        f"{title} 글에서 개발자 키보드 추천 기준은 타건감, 소음, 손목 피로도입니다. "
        "기계식 키보드 선택 시 스위치 특성과 소프트웨어 지원 여부를 함께 확인해야 합니다. "
        "실사용 환경에서 체감 성능은 사용 시간대와 작업 패턴에 따라 달라질 수 있습니다. "
    )
    repeated = (paragraph * 8).strip()
    return (
        f"# {title}\n\n"
        "## 핵심 결론\n"
        f"{repeated}\n\n"
        "## 체크리스트\n"
        f"{repeated}\n\n"
        "추가 스펙은 https://example.com/spec 에서 확인할 수 있으며, "
        "이번 비교 실험의 만족도는 42%였습니다.\n"
    )


def test_full_pipeline_publish_sim(tmp_path: Path):
    """온보딩 Voice Profile + Safety Guard + 발행 직전 payload 덤프를 E2E로 검증한다."""
    db_path = tmp_path / "full_pipeline_e2e.db"
    store = JobStore(str(db_path), config=JobConfig(max_llm_calls_per_job=20))

    # 온보딩에서 저장된 페르소나(질문지+MBTI 혼합 결과)를 DB에 주입한다.
    voice_profile = {
        "version": "v1",
        "mbti": "INTJ",
        "mbti_enabled": True,
        "mbti_confidence": 82,
        "structure": "top_down",
        "evidence": "objective",
        "distance": "authoritative",
        "criticism": "direct",
        "density": "dense",
        "style_strength": 62,
        "scores": {
            "structure": 84,
            "evidence": 88,
            "distance": 72,
            "criticism": 76,
            "density": 69,
        },
        "questionnaire_meta": {
            "source": "questionnaire",
            "answered_count": 6,
            "total_questions": 7,
            "completion_ratio": 0.857,
        },
    }
    store.upsert_persona_profile(
        persona_id="P2",
        persona_payload={"identity": "직설적 IT 분석가", "interests": ["키보드", "업무 자동화"]},
        profile_payload=voice_profile,
    )

    scheduled_at = "2026-02-22T01:00:00Z"
    title = "개발자 키보드 추천 완전 가이드"
    assert store.schedule_job(
        job_id="e2e-full-pipeline-job",
        title=title,
        seed_keywords=["개발자 키보드 추천", "기계식 키보드", "타건감"],
        platform="naver",
        persona_id="P2",
        scheduled_at=scheduled_at,
        category="IT 자동화",
    )
    claimed_jobs = store.claim_due_jobs(limit=1, now_override=scheduled_at)
    assert len(claimed_jobs) == 1

    draft = _build_long_content(title)
    seo_refined = f"{draft}\n실전 적용 순서는 환경 측정 → 키캡/축 점검 → 일주일 검증입니다.\n"
    rewritten_with_fact_drift = seo_refined.replace("42%", "55%")
    fake_outputs = [
        draft,
        seo_refined,
        '{"score": 91, "issues": [], "summary": "quality pass"}',
        rewritten_with_fact_drift,
        '{"thumbnail":{"prompt":"키보드 썸네일","concept":"키보드 책상"},'
        '"content_images":[{"prompt":"타건 장면","concept":"손과 키보드","after_section":"핵심 결론","type":"illustration"}]}',
    ]

    generator = ContentGenerator(
        client=FakeLLMClient(fake_outputs),
        db_path=str(db_path),
        enable_quality_check=True,
        enable_seo_optimization=True,
        enable_voice_rewrite=True,
        enable_fact_check=False,
    )

    async def generate_fn(job):
        result = await generator.generate(job)
        return asdict(result)

    payload_dump_path = tmp_path / "final_publish_payload.json"
    publisher = PayloadDumpPublisher(payload_dump_path)
    pipeline = PipelineService(
        job_store=store,
        publisher=publisher,
        generate_fn=generate_fn,
        image_generator=FakeImageGenerator(tmp_path),
    )

    asyncio.run(pipeline.run_job(claimed_jobs[0]))

    updated = store.get_job("e2e-full-pipeline-job")
    assert updated is not None
    assert updated.status == store.STATUS_COMPLETED
    assert updated.result_url == "https://blog.naver.com/sim/e2e"
    assert payload_dump_path.exists()

    dumped = json.loads(payload_dump_path.read_text(encoding="utf-8"))
    assert dumped["title"] == title
    assert "42%" in dumped["content"]
    assert "55%" not in dumped["content"]  # Voice rewrite fact drift -> 원문 롤백 검증
    assert "https://example.com/spec" in dumped["content"]
    assert len(dumped["images"]) == 1
    assert dumped["thumbnail"] != ""
    assert len(dumped["image_points"]) >= 1
    assert any(marker["marker"].startswith("[IMG_") for marker in dumped["image_points"])

    assert updated.quality_snapshot.get("pipeline_layers", {}).get("voice_rewrite_applied") is False
    print("ALL PASSED: full pipeline publish simulation")

