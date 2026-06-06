import asyncio
from pathlib import Path
from types import SimpleNamespace

from modules.automation.visual_sidecar import VisualSidecar, build_visual_sidecar_from_env


class FakePlanner:
    """테스트용 FreeLLMAPI planner 대역."""

    def __init__(self, plan):
        self.plan = plan
        self.last_context = None

    async def plan_visuals(self, context):
        self.last_context = context
        return self.plan


class BrokenPlanner:
    """장애 상황을 재현하는 planner 대역."""

    async def plan_visuals(self, context):
        del context
        raise RuntimeError("planner down")


class FakeImageClient:
    """Pexels/Pollinations generate 인터페이스 대역."""

    def __init__(self, path: Path):
        self.path = path

    async def generate(self, prompt, style_suffix="", size="1024*768", n=1):
        del prompt, style_suffix, size, n
        self.path.write_bytes(b"fake-image")
        return SimpleNamespace(success=True, local_path=str(self.path))


def _job(topic_mode="it"):
    return SimpleNamespace(
        job_id="visual-sidecar-job",
        platform="naver",
        title="시각자료 테스트",
        seed_keywords=["자동화", "판단 흐름"],
        category="IT/테크" if topic_mode == "it" else "재테크/금융",
    )


def _payload(topic_mode="it"):
    return {
        "title": "시각자료 테스트",
        "content": "도입 문장입니다.\n\n■ 첫 번째 기준\n본문입니다.",
        "image_sources": {},
        "image_points": [],
        "quality_snapshot": {},
        "seo_snapshot": {
            "topic_mode": topic_mode,
            "market_snapshot": {
                "data_points": [
                    {"symbol": "KOSPI", "label": "코스피", "value": 2800, "change_percent": 0.8},
                    {"symbol": "USD/KRW", "label": "환율", "value": 1380, "change_percent": -0.2},
                ]
            },
        },
    }


def test_visual_sidecar_adds_flowchart_and_table(tmp_path):
    """FreeLLMAPI 제안을 로컬 PNG 시각자료로 추가해야 한다."""
    planner = FakePlanner(
        {
            "visuals": [
                {
                    "type": "table",
                    "title": "체크 표",
                    "headers": ["구분", "확인 기준"],
                    "rows": [["환율", "방향 확인"], ["금리", "부담 확인"]],
                },
                {
                    "type": "flowchart",
                    "title": "판단 흐름",
                    "nodes": ["자료 확인", "기준 비교", "행동 줄이기"],
                },
            ]
        }
    )
    sidecar = VisualSidecar(
        planner=planner,
        output_dir=str(tmp_path),
        max_visuals=2,
        planner_model="auto",
        planner_base_url="http://127.0.0.1:3001/v1",
    )

    enriched = asyncio.run(sidecar.enrich_payload(job=_job("it"), payload=_payload("it")))

    assert enriched["quality_snapshot"]["visual_sidecar"]["status"] == "attached"
    assert enriched["quality_snapshot"]["visual_sidecar"]["added_count"] == 2
    assert "[IMG_0]" in enriched["content"]
    assert "[IMG_1]" in enriched["content"]
    assert len(enriched["image_points"]) == 2
    renderers = {meta["renderer"] for meta in enriched["image_sources"].values()}
    assert renderers == {"flowchart", "table"}
    assert all(meta["provider"] == "freellmapi_visual_sidecar" for meta in enriched["image_sources"].values())
    assert all(Path(point["path"]).exists() for point in enriched["image_points"])


def test_visual_sidecar_uses_only_market_snapshot_for_chart(tmp_path):
    """경제 그래프는 payload의 market_snapshot 데이터가 있을 때만 생성해야 한다."""
    planner = FakePlanner({"visuals": [{"type": "market_chart", "title": "시장 지표"}]})
    sidecar = VisualSidecar(planner=planner, output_dir=str(tmp_path), max_visuals=1)

    enriched = asyncio.run(sidecar.enrich_payload(job=_job("finance"), payload=_payload("finance")))

    assert enriched["quality_snapshot"]["visual_sidecar"]["added_count"] == 1
    assert next(iter(enriched["image_sources"].values()))["renderer"] == "market_chart"

    missing_snapshot = _payload("finance")
    missing_snapshot["seo_snapshot"]["market_snapshot"] = {"data_points": []}
    skipped = asyncio.run(sidecar.enrich_payload(job=_job("finance"), payload=missing_snapshot))

    assert skipped["quality_snapshot"]["visual_sidecar"]["added_count"] == 0
    assert skipped["image_points"] == []


def test_visual_sidecar_skips_planner_failure_without_payload_damage(tmp_path):
    """FreeLLMAPI 장애 시 본문/이미지 목록은 그대로 유지해야 한다."""
    sidecar = VisualSidecar(planner=BrokenPlanner(), output_dir=str(tmp_path), max_visuals=2)
    original = _payload("it")

    enriched = asyncio.run(sidecar.enrich_payload(job=_job("it"), payload=original))

    assert enriched["content"] == original["content"]
    assert enriched["image_points"] == []
    assert enriched["image_sources"] == {}
    assert enriched["quality_snapshot"]["visual_sidecar"]["status"] == "failed"


def test_visual_sidecar_can_attach_pexels_and_pollinations_candidates(tmp_path):
    """검색어/프롬프트 제안을 기존 무료 이미지 클라이언트 후보로 붙일 수 있어야 한다."""
    planner = FakePlanner(
        {
            "visuals": [
                {"type": "pexels", "query": "developer workflow desk"},
                {"type": "pollinations", "prompt": "clean diagram concept, no text"},
            ]
        }
    )
    sidecar = VisualSidecar(
        planner=planner,
        output_dir=str(tmp_path),
        max_visuals=2,
        pexels_client=FakeImageClient(tmp_path / "pexels.jpg"),
        pollinations_client=FakeImageClient(tmp_path / "pollinations.png"),
    )

    enriched = asyncio.run(sidecar.enrich_payload(job=_job("cafe"), payload=_payload("cafe")))

    metas = list(enriched["image_sources"].values())
    assert {meta["renderer"] for meta in metas} == {"pexels", "pollinations"}
    assert {meta["kind"] for meta in metas} == {"stock", "ai_generated"}


def test_visual_sidecar_env_factory_disabled_by_default(monkeypatch):
    """기능 플래그 기본값은 꺼짐이어야 한다."""
    monkeypatch.delenv("FREELLMAPI_VISUAL_SIDECAR_ENABLED", raising=False)

    assert build_visual_sidecar_from_env(output_dir="data/images") is None
