import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.pipeline_service import PipelineService
from modules.automation.time_utils import now_utc
from modules.automation.worker import Worker, WorkerConfig
from modules.images.placement import convert_markdown_for_naver_editor
from modules.seo.quality_gate import QualityGateResult
from modules.uploaders.playwright_publisher import PlaywrightPublisher, PublishResult


def build_store(tmp_path: Path, db_name: str = "pipeline_test.db") -> JobStore:
    """테스트용 JobStore를 생성한다."""
    return JobStore(
        str(tmp_path / db_name),
        config=JobConfig(max_retries=3, max_llm_calls_per_job=15),
    )


class DummyPublisher:
    """파이프라인 테스트용 더미 발행기."""

    def __init__(self, success: bool = True):
        self.success = success
        self.called = 0
        self.last_payload: Dict[str, Any] = {}

    async def publish(
        self,
        title: str,
        content: str,
        thumbnail: Optional[str] = None,
        images: Optional[List[str]] = None,
        image_sources: Optional[Dict[str, Dict[str, str]]] = None,
        image_points: Optional[List] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
        publish_mode: Optional[str] = None,
    ) -> PublishResult:
        self.last_payload = {
            "title": title,
            "content": content,
            "thumbnail": thumbnail or "",
            "images": list(images or []),
            "image_sources": image_sources or {},
            "image_points": list(image_points or []),
            "tags": list(tags or []),
            "category": category or "",
            "publish_mode": publish_mode or "",
        }
        self.called += 1
        if self.success:
            return PublishResult(success=True, url=f"https://blog.naver.com/test/{self.called}")
        return PublishResult(success=False, error_code="PUBLISH_FAILED", error_message="publish failed")


class DummyVisualSidecar:
    """발행 직전 사이드카 호출 확인용 대역."""

    async def enrich_payload(self, *, job, payload):
        del job
        enriched = dict(payload)
        content = str(enriched.get("content", "") or "")
        enriched["content"] = f"{content}\n\n[IMG_0]"
        enriched["image_points"] = [
            {
                "index": 0,
                "path": "data/images/sidecar_flowchart.png",
                "marker": "[IMG_0]",
                "section_hint": "흐름도",
                "is_thumbnail": False,
            }
        ]
        enriched["image_sources"] = {
            "data/images/sidecar_flowchart.png": {
                "kind": "manual",
                "provider": "freellmapi_visual_sidecar",
                "renderer": "flowchart",
            }
        }
        enriched["quality_snapshot"] = {
            **dict(enriched.get("quality_snapshot", {}) or {}),
            "visual_sidecar": {"status": "attached", "added_count": 1},
        }
        return enriched


def schedule_and_claim(store: JobStore, job_id: str = "job-1"):
    """job을 등록 후 running 상태로 선점한다."""
    scheduled_at = now_utc()
    ok = store.schedule_job(
        job_id=job_id,
        title="테스트 포스트",
        seed_keywords=["테스트", "자동화"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled_at,
    )
    assert ok
    jobs = store.claim_due_jobs(limit=1, now_override=scheduled_at)
    assert len(jobs) == 1
    return jobs[0]


def test_convert_markdown_table_to_naver_plain_text():
    """표 이미지 렌더링이 불가능해도 raw Markdown 표가 노출되지 않아야 한다."""
    raw = (
        "## 손실을 마주하는 연습, 작은 실수에서 얻은 기준\n\n"
        "| 구분 | 배움 중심 접근 | 수익 중심 접근 |\n"
        "| --- | --- | --- |\n"
        "| 손실 경험 시 반응 | 금액보다 결정 과정을 먼저 점검한다 | 손실 자체를 실패로 인식하고 회피하려 한다 |\n"
        "| 기록의 목적 | 감정이 개입된 순간을 기준으로 삼는다 | 수익률이나 진입가 위주로 숫자만 적어둔다 |\n"
    )

    converted = convert_markdown_for_naver_editor(raw)

    assert "##" not in converted
    assert "| 구분 |" not in converted
    assert "| --- |" not in converted
    assert "■ 손실을 마주하는 연습" in converted
    assert "[표] 구분 / 배움 중심 접근 / 수익 중심 접근" in converted
    assert "• 배움 중심 접근: 금액보다 결정 과정을 먼저 점검한다" in converted


def test_ready_payload_markdown_table_is_normalized_before_publish(tmp_path: Path):
    """ready_to_publish에 원문 Markdown이 들어와도 발행 직전에 네이버용으로 보정한다."""
    store = build_store(tmp_path, "ready_markdown_table.db")
    job = schedule_and_claim(store, "ready-markdown-table-job")
    raw_content = (
        "## 손실을 마주하는 연습, 작은 실수에서 얻은 기준\n\n"
        "| 구분 | 배움 중심 접근 | 수익 중심 접근 |\n"
        "| --- | --- | --- |\n"
        "| 손실 경험 시 반응 | 금액보다 결정 과정을 먼저 점검한다 | 손실 자체를 실패로 인식하고 회피하려 한다 |\n\n"
        "이 표는 투자 기록을 함께 공부하기 위한 기준입니다."
    )
    assert store.save_prepared_payload(
        job.job_id,
        {
            "title": "표 변환 테스트",
            "content": raw_content,
            "images": [],
            "image_points": [],
            "tags": [],
            "category": "",
        },
    )
    assert store.update_job_status(job.job_id, store.STATUS_RUNNING)
    running_job = store.get_job(job.job_id)
    assert running_job is not None

    publisher = DummyPublisher()
    pipeline = PipelineService(
        job_store=store,
        publisher=publisher,
        generate_fn=lambda _job: {},
    )
    pipeline._image_output_dir = str(tmp_path / "images")
    pipeline._summary_card_enabled = True

    assert asyncio.run(pipeline.process_publication(running_job)) is True

    published_content = str(publisher.last_payload["content"])
    assert "##" not in published_content
    assert "| 구분 |" not in published_content
    assert "| --- |" not in published_content
    assert "■ 손실을 마주하는 연습" in published_content
    assert ("[IMG_" in published_content) or ("[표]" in published_content)


def test_ready_payload_attaches_summary_card_before_publish(tmp_path: Path):
    """발행 직전 본문 요약 카드 PNG가 자동으로 첨부되어야 한다."""
    store = build_store(tmp_path, "ready_summary_card.db")
    job = schedule_and_claim(store, "ready-summary-card-job")
    raw_content = (
        "오늘은 시장이 흔들릴 때 어떤 기준으로 공부를 이어갈지 정리해봅니다.\n\n"
        "## 시장 전망보다 먼저 마주해야 하는 오해\n"
        "투자 초심자는 정보가 부족해서만 흔들리는 것이 아니라 작은 손실에도 일상이 흔들릴 수 있습니다. "
        "그래서 저는 먼저 계좌 숫자보다 몸과 마음의 반응을 기록해보려 합니다.\n\n"
        "## 초기 자산 배분, 왜 첫 번째 경계선인가\n"
        "자산 배분은 수익률을 높이는 기술이라기보다 평범한 하루를 지키는 틀입니다. "
        "생활비와 투자금을 분리하면 예측이 틀린 날에도 다음 선택을 천천히 할 수 있습니다.\n\n"
        "## 손실을 마주하는 연습\n"
        "작은 손실에서 감정 반응을 적어보면 나에게 맞는 손실 한도를 조금씩 발견할 수 있습니다. "
        "이 과정은 누군가를 가르치기보다 함께 공부하는 기록에 가깝습니다. "
        "시장을 이기겠다는 마음보다, 다음에도 같은 실수를 줄이겠다는 마음이 오래 남는 기준이 됩니다.\n"
    )
    assert store.save_prepared_payload(
        job.job_id,
        {
            "title": "요약 카드 테스트",
            "content": raw_content,
            "images": [],
            "image_sources": {},
            "image_points": [],
            "tags": [],
            "category": "",
        },
    )
    assert store.update_job_status(job.job_id, store.STATUS_RUNNING)
    running_job = store.get_job(job.job_id)
    assert running_job is not None

    publisher = DummyPublisher()
    pipeline = PipelineService(
        job_store=store,
        publisher=publisher,
        generate_fn=lambda _job: {},
    )
    pipeline._image_output_dir = str(tmp_path / "images")
    pipeline._summary_card_enabled = True

    assert asyncio.run(pipeline.process_publication(running_job)) is True

    payload = publisher.last_payload
    assert "[IMG_" in str(payload["content"])
    summary_points = [
        point
        for point in payload["image_points"]
        if getattr(point, "section_hint", "") == "요약 카드"
    ]
    assert len(summary_points) == 1
    summary_path = Path(summary_points[0].path)
    assert summary_path.exists()
    assert payload["image_sources"][str(summary_path)]["provider"] == "summary_card_renderer"


def test_ready_payload_attaches_market_chart_before_publish(tmp_path: Path):
    """시장 스냅샷이 있는 ready payload는 그래프 PNG를 자동 첨부해야 한다."""
    store = build_store(tmp_path, "ready_market_chart.db")
    job = schedule_and_claim(store, "ready-market-chart-job")
    raw_content = (
        "오늘은 미장 전 지표를 보면서 제가 어떤 기준으로 공부할지 정리해봅니다.\n\n"
        "■ 숫자보다 먼저 확인할 질문\n\n"
        "지표가 모두 같은 방향을 말하는 날은 드뭅니다. 그래서 저는 수익 예측보다 먼저 "
        "나에게 필요한 확인 질문을 적어보려고 합니다.\n\n"
        "■ 변동률은 결론이 아니라 출발점\n\n"
        "SPY와 QQQ, BTC의 움직임은 서로 다른 시장의 온도를 보여줍니다. "
        "다만 이 숫자만 보고 바로 매매 결론을 내리기보다, 오늘 어떤 조건이 깨지는지 함께 보려 합니다.\n"
    )
    assert store.save_prepared_payload(
        job.job_id,
        {
            "title": "시장 그래프 테스트",
            "content": raw_content,
            "images": [],
            "image_sources": {},
            "image_points": [],
            "tags": [],
            "category": "",
            "seo_snapshot": {
                "market_snapshot": {
                    "slot": "us_preopen",
                    "scope": "us",
                    "data_points": [
                        {"symbol": "SPY", "source": "Stooq", "value": 624.1, "change_percent": 0.72},
                        {"symbol": "QQQ", "source": "Stooq", "value": 542.3, "change_percent": -0.35},
                        {"symbol": "BTC", "source": "CoinGecko", "value": 104200.0, "change_percent": 1.8},
                    ],
                }
            },
        },
    )
    assert store.update_job_status(job.job_id, store.STATUS_RUNNING)
    running_job = store.get_job(job.job_id)
    assert running_job is not None

    publisher = DummyPublisher()
    pipeline = PipelineService(
        job_store=store,
        publisher=publisher,
        generate_fn=lambda _job: {},
    )
    pipeline._image_output_dir = str(tmp_path / "images")
    pipeline._summary_card_enabled = False
    pipeline._market_chart_enabled = True

    assert asyncio.run(pipeline.process_publication(running_job)) is True

    payload = publisher.last_payload
    assert "[IMG_" in str(payload["content"])
    chart_points = [
        point
        for point in payload["image_points"]
        if getattr(point, "section_hint", "") == "시장 그래프"
    ]
    assert len(chart_points) == 1
    chart_path = Path(chart_points[0].path)
    assert chart_path.exists()
    assert payload["image_sources"][str(chart_path)]["provider"] == "market_chart_renderer"


def test_ready_payload_applies_visual_sidecar_before_publish(tmp_path: Path):
    """발행 직전 FreeLLMAPI 시각자료 사이드카가 payload를 보강해야 한다."""
    store = build_store(tmp_path, "ready_visual_sidecar.db")
    job = schedule_and_claim(store, "ready-visual-sidecar-job")
    assert store.save_prepared_payload(
        job.job_id,
        {
            "title": "사이드카 테스트",
            "content": "도입 문장입니다.\n\n■ 첫 번째 기준\n본문입니다.",
            "image_sources": {},
            "image_points": [],
            "tags": [],
            "category": "",
            "quality_snapshot": {},
            "seo_snapshot": {"topic_mode": "it"},
        },
    )
    assert store.update_job_status(job.job_id, store.STATUS_RUNNING)
    running_job = store.get_job(job.job_id)
    assert running_job is not None

    publisher = DummyPublisher()
    pipeline = PipelineService(
        job_store=store,
        publisher=publisher,
        generate_fn=lambda _job: {},
        visual_sidecar=DummyVisualSidecar(),
    )
    pipeline._summary_card_enabled = False
    pipeline._market_chart_enabled = False

    assert asyncio.run(pipeline.process_publication(running_job)) is True

    assert "[IMG_0]" in publisher.last_payload["content"]
    assert publisher.last_payload["image_sources"]["data/images/sidecar_flowchart.png"] == {
        "kind": "manual",
        "provider": "freellmapi_visual_sidecar",
        "renderer": "flowchart",
    }
    assert publisher.last_payload["image_points"][0].marker == "[IMG_0]"


def test_kr_preopen_auto_publish_passes_publish_mode_and_recommended_tags(tmp_path: Path):
    """국장전 자동발행 조건을 통과하면 공개발행 모드와 5~8개 태그를 전달한다."""

    store = build_store(tmp_path, "kr_preopen_auto_publish.db")
    due_now = now_utc()
    assert store.schedule_job(
        job_id="kr-auto-publish-job",
        title="전력설비주가 다시 주목받는 이유",
        seed_keywords=["전력설비", "AI 데이터센터", "국장"],
        platform="naver",
        persona_id="P4",
        scheduled_at=due_now,
        tags=[
            "market_daily",
            "market_slot:kr_preopen",
            "auto_publish:kr_preopen",
            "publish_mode:publish",
            "opportunity_score:88",
        ],
        category="경제 브리핑",
    )
    claimed = store.claim_due_jobs(limit=1, now_override=due_now)
    assert len(claimed) == 1
    assert store.save_prepared_payload(
        "kr-auto-publish-job",
        {
            "title": "전력설비주가 다시 주목받는 이유",
            "content": ("전력설비와 AI 데이터센터를 국장 전 기준으로 함께 공부합니다. " * 40).strip(),
            "images": [],
            "image_sources": {},
            "image_points": [],
            "tags": ["전력설비", "AI데이터센터", "국장전브리핑", "오늘의증시", "경제공부"],
            "category": "경제 브리핑",
            "quality_snapshot": {"score": 92},
            "seo_snapshot": {
                "market_snapshot": {
                    "confidence_score": 0.72,
                    "data_point_count": 2,
                    "data_points": [
                        {"symbol": "KOSPI", "source": "Stooq", "value": 2870.0},
                        {"symbol": "USD/KRW", "source": "FRED", "value": 1365.0},
                    ],
                }
            },
        },
    )
    assert store.update_job_status("kr-auto-publish-job", store.STATUS_RUNNING)
    job = store.get_job("kr-auto-publish-job")
    assert job is not None

    publisher = DummyPublisher()
    pipeline = PipelineService(
        job_store=store,
        publisher=publisher,
        generate_fn=lambda _job: {},
    )
    pipeline._summary_card_enabled = False
    pipeline._market_chart_enabled = False

    assert asyncio.run(pipeline.process_publication(job)) is True

    assert publisher.called == 1
    assert publisher.last_payload["publish_mode"] == "publish"
    assert 5 <= len(publisher.last_payload["tags"]) <= 8
    assert "market_daily" not in publisher.last_payload["tags"]


def test_kr_preopen_auto_publish_blocks_to_approval_when_score_low(tmp_path: Path):
    """자동발행 조건 미달 시 공개발행하지 않고 승인 대기로 전환한다."""

    store = build_store(tmp_path, "kr_preopen_auto_publish_block.db")
    due_now = now_utc()
    assert store.schedule_job(
        job_id="kr-auto-block-job",
        title="국장 전 고정 브리핑",
        seed_keywords=["국장", "환율"],
        platform="naver",
        persona_id="P4",
        scheduled_at=due_now,
        tags=[
            "market_daily",
            "market_slot:kr_preopen",
            "auto_publish:kr_preopen",
            "publish_mode:publish",
            "opportunity_score:30",
        ],
        category="경제 브리핑",
    )
    claimed = store.claim_due_jobs(limit=1, now_override=due_now)
    assert len(claimed) == 1
    assert store.save_prepared_payload(
        "kr-auto-block-job",
        {
            "title": "국장 전 고정 브리핑",
            "content": ("국장 전 시장 기준을 확인하는 글입니다. " * 40).strip(),
            "images": [],
            "image_points": [],
            "tags": ["국장전브리핑", "오늘의증시", "환율", "경제공부", "시장체크"],
            "category": "경제 브리핑",
            "quality_snapshot": {"score": 90},
            "seo_snapshot": {
                "market_snapshot": {
                    "confidence_score": 0.70,
                    "data_point_count": 1,
                    "data_points": [{"symbol": "KOSPI", "source": "Stooq", "value": 2870.0}],
                }
            },
        },
    )
    assert store.update_job_status("kr-auto-block-job", store.STATUS_RUNNING)
    job = store.get_job("kr-auto-block-job")
    assert job is not None

    publisher = DummyPublisher()
    pipeline = PipelineService(
        job_store=store,
        publisher=publisher,
        generate_fn=lambda _job: {},
    )
    pipeline._summary_card_enabled = False
    pipeline._market_chart_enabled = False

    assert asyncio.run(pipeline.process_publication(job)) is False

    updated = store.get_job("kr-auto-block-job")
    assert updated is not None
    assert updated.status == store.STATUS_AWAITING_APPROVAL
    assert publisher.called == 0
    saved_payload = store.load_prepared_payload("kr-auto-block-job")
    guard = saved_payload["quality_snapshot"]["auto_publish_guard"]
    assert guard["status"] == "blocked"
    assert any("글감 기회 점수" in reason for reason in guard["reasons"])


def test_kr_preopen_auto_publish_blocks_investment_recommendation_words(tmp_path: Path):
    """자동발행 조건이 좋아도 투자권유 표현이 있으면 승인 대기로 후퇴한다."""

    store = build_store(tmp_path, "kr_preopen_auto_publish_forbidden.db")
    due_now = now_utc()
    assert store.schedule_job(
        job_id="kr-auto-forbidden-job",
        title="국장 전 확인 대상 브리핑",
        seed_keywords=["국장", "환율"],
        platform="naver",
        persona_id="P4",
        scheduled_at=due_now,
        tags=[
            "market_daily",
            "market_slot:kr_preopen",
            "auto_publish:kr_preopen",
            "publish_mode:publish",
            "opportunity_score:88",
            "writing_strategy:market_preopen_scenario",
        ],
        category="경제 브리핑",
    )
    assert store.claim_due_jobs(limit=1, now_override=due_now)
    assert store.save_prepared_payload(
        "kr-auto-forbidden-job",
        {
            "title": "국장 전 확인 대상 브리핑",
            "content": ("이 종목은 매수 추천 신호라는 식의 표현이 들어간 글입니다. " * 40).strip(),
            "images": [],
            "image_points": [],
            "tags": ["국장전브리핑", "오늘의증시", "환율", "경제공부", "시장체크"],
            "category": "경제 브리핑",
            "quality_snapshot": {"score": 92},
            "seo_snapshot": {
                "market_snapshot": {
                    "confidence_score": 0.70,
                    "data_point_count": 1,
                    "data_points": [{"symbol": "KOSPI", "source": "Stooq", "value": 2870.0}],
                }
            },
        },
    )
    assert store.update_job_status("kr-auto-forbidden-job", store.STATUS_RUNNING)
    job = store.get_job("kr-auto-forbidden-job")
    assert job is not None

    publisher = DummyPublisher()
    pipeline = PipelineService(
        job_store=store,
        publisher=publisher,
        generate_fn=lambda _job: {},
    )
    pipeline._summary_card_enabled = False
    pipeline._market_chart_enabled = False

    assert asyncio.run(pipeline.process_publication(job)) is False

    updated = store.get_job("kr-auto-forbidden-job")
    assert updated is not None
    assert updated.status == store.STATUS_AWAITING_APPROVAL
    assert publisher.called == 0
    saved_payload = store.load_prepared_payload("kr-auto-forbidden-job")
    reasons = saved_payload["quality_snapshot"]["auto_publish_guard"]["reasons"]
    assert any("투자 권유 위험 표현" in reason for reason in reasons)


def test_kr_preopen_auto_publish_blocks_when_visual_validation_fails(tmp_path: Path):
    """표/카드 텍스트 검수 실패가 있으면 자동 공개발행을 보류한다."""

    store = build_store(tmp_path, "kr_preopen_visual_block.db")
    due_now = now_utc()
    assert store.schedule_job(
        job_id="kr-auto-visual-block-job",
        title="국장 전 반도체 수급 기준",
        seed_keywords=["국장", "반도체"],
        platform="naver",
        persona_id="P4",
        scheduled_at=due_now,
        tags=[
            "market_daily",
            "market_slot:kr_preopen",
            "auto_publish:kr_preopen",
            "publish_mode:publish",
            "opportunity_score:92",
        ],
        category="경제 브리핑",
    )
    claimed = store.claim_due_jobs(limit=1, now_override=due_now)
    assert len(claimed) == 1
    assert store.save_prepared_payload(
        "kr-auto-visual-block-job",
        {
            "title": "국장 전 반도체 수급 기준",
            "content": ("국장 전 시장 기준을 확인하는 글입니다. " * 40).strip(),
            "images": [],
            "image_points": [],
            "tags": ["국장전브리핑", "오늘의증시", "반도체", "환율", "시장체크"],
            "category": "경제 브리핑",
            "quality_snapshot": {
                "score": 92,
                "visual_text_validation": {
                    "passed": False,
                    "issues": ["tables:table_0_row_1_trimmed"],
                },
            },
            "seo_snapshot": {
                "market_snapshot": {
                    "confidence_score": 0.78,
                    "data_point_count": 2,
                    "data_points": [
                        {"symbol": "KOSPI", "source": "Stooq", "value": 2870.0},
                        {"symbol": "USD/KRW", "source": "FRED", "value": 1365.0},
                    ],
                }
            },
        },
    )
    assert store.update_job_status("kr-auto-visual-block-job", store.STATUS_RUNNING)
    job = store.get_job("kr-auto-visual-block-job")
    assert job is not None

    publisher = DummyPublisher()
    pipeline = PipelineService(
        job_store=store,
        publisher=publisher,
        generate_fn=lambda _job: {},
    )
    pipeline._summary_card_enabled = False
    pipeline._market_chart_enabled = False

    assert asyncio.run(pipeline.process_publication(job)) is False

    updated = store.get_job("kr-auto-visual-block-job")
    assert updated is not None
    assert updated.status == store.STATUS_AWAITING_APPROVAL
    assert publisher.called == 0
    saved_payload = store.load_prepared_payload("kr-auto-visual-block-job")
    guard = saved_payload["quality_snapshot"]["auto_publish_guard"]
    assert guard["status"] == "blocked"
    assert any("표/카드" in reason for reason in guard["reasons"])


def test_pipeline_quality_retry_mask(tmp_path: Path):
    """retry_mask가 2회 연속이면 QUALITY_FAILED로 전환되는지 검증."""
    store = build_store(tmp_path)
    job = schedule_and_claim(store, "retry-mask-job")

    async def retry_mask_generate(_job) -> Dict[str, Any]:
        return {
            "final_content": "content",
            "quality_gate": "retry_mask",
            "quality_snapshot": {},
            "seo_snapshot": {},
            "llm_calls_used": 1,
        }

    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=retry_mask_generate,
    )

    async def run_once():
        await asyncio.wait_for(pipeline.run_job(job), timeout=3)

    asyncio.run(run_once())

    updated = store.get_job("retry-mask-job")
    assert updated is not None
    assert updated.status in {store.STATUS_RETRY_WAIT, store.STATUS_FAILED}
    assert updated.error_code == "QUALITY_FAILED"
    assert updated.quality_snapshot.get("mask_retry_done") is True


def test_pipeline_quality_retry_all(tmp_path: Path):
    """retry_all 결과가 retry_wait으로 전환되는지 검증."""
    store = build_store(tmp_path)
    job = schedule_and_claim(store, "retry-all-job")

    async def retry_all_generate(_job) -> Dict[str, Any]:
        return {
            "final_content": "content",
            "quality_gate": "retry_all",
            "quality_snapshot": {},
            "seo_snapshot": {},
            "llm_calls_used": 2,
        }

    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=retry_all_generate,
    )

    asyncio.run(pipeline.run_job(job))

    updated = store.get_job("retry-all-job")
    assert updated is not None
    assert updated.status == store.STATUS_RETRY_WAIT
    assert updated.error_code == "QUALITY_FAILED"
    assert updated.retry_count == 1


def test_pipeline_llm_budget_exceeded(tmp_path: Path):
    """LLM 예산 초과 시 BUDGET_EXCEEDED로 실패하는지 검증."""
    store = build_store(tmp_path)
    job = schedule_and_claim(store, "budget-exceeded-job")
    store.increment_llm_calls(job.job_id, 15)

    generate_called = {"count": 0}

    async def generate_never_called(_job) -> Dict[str, Any]:
        generate_called["count"] += 1
        return {"quality_gate": "pass", "final_content": "x", "llm_calls_used": 1}

    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=generate_never_called,
    )
    asyncio.run(pipeline.run_job(job))

    updated = store.get_job(job.job_id)
    assert updated is not None
    assert updated.status == store.STATUS_FAILED
    assert updated.error_code == "BUDGET_EXCEEDED"
    assert generate_called["count"] == 0


def test_pipeline_already_published_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """이미 발행된 job은 complete_job 없이 스킵되는지 검증."""
    store = build_store(tmp_path)
    job = schedule_and_claim(store, "already-published-job")

    with store.connection() as conn:
        conn.execute(
            "UPDATE jobs SET result_url = ? WHERE job_id = ?",
            ("https://blog.naver.com/existing/123", job.job_id),
        )

    complete_spy = MagicMock(wraps=store.complete_job)
    monkeypatch.setattr(store, "complete_job", complete_spy)

    publisher = DummyPublisher()
    pipeline = PipelineService(job_store=store, publisher=publisher, generate_fn=lambda _j: {})
    asyncio.run(pipeline.run_job(job))

    assert complete_spy.call_count == 0
    assert publisher.called == 0


def test_playwright_dry_run_returns_url(monkeypatch: pytest.MonkeyPatch):
    """DRY_RUN=true면 실제 브라우저 없이 URL을 반환하는지 검증."""
    monkeypatch.setenv("DRY_RUN", "true")
    publisher = PlaywrightPublisher(blog_id="dry-run")
    result = asyncio.run(publisher.publish(title="테스트", content="본문"))
    assert result.success is True
    assert result.url == "https://blog.naver.com/dry-run/000000000000"


def test_playwright_cleanup_order():
    """cleanup이 context -> browser -> playwright 순서로 호출되는지 검증."""
    publisher = PlaywrightPublisher(blog_id="cleanup-order")
    close_order = []

    class ContextMock:
        async def close(self):
            close_order.append("context")

    class BrowserMock:
        async def close(self):
            close_order.append("browser")

    class PlaywrightMock:
        async def stop(self):
            close_order.append("playwright")

    publisher._context = ContextMock()
    publisher._browser = BrowserMock()
    publisher._playwright = PlaywrightMock()

    asyncio.run(publisher._cleanup())

    assert close_order == ["context", "browser", "playwright"]
    assert publisher._context is None
    assert publisher._browser is None
    assert publisher._playwright is None


def test_playwright_error_classification():
    """에러 문자열에 따른 분류 코드가 기대값과 일치하는지 검증."""
    publisher = PlaywrightPublisher(blog_id="error-classify")
    assert publisher._classify_error(Exception("Timeout 30000ms exceeded")) == "NETWORK_TIMEOUT"
    assert publisher._classify_error(Exception("selector not found")) == "ELEMENT_NOT_FOUND"
    assert publisher._classify_error(Exception("HTTP 429 rate limited")) == "RATE_LIMITED"


def test_pipeline_sub_job_uses_channel_publisher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """서브 잡은 채널 기반 퍼블리셔를 선택해서 발행해야 한다."""
    store = build_store(tmp_path, "sub_channel_publish.db")
    store.set_system_setting("telegram_draft_approval_enabled", "false")
    channel_id = "channel-sub-1"
    assert store.insert_channel(
        {
            "channel_id": channel_id,
            "platform": "tistory",
            "label": "Tistory Sub",
            "blog_url": "https://sample.tistory.com",
            "persona_id": "P1",
            "persona_desc": "",
            "daily_target": 0,
            "style_level": 2,
            "style_model": "",
            "publish_delay_minutes": 90,
            "is_master": False,
            "auth_json": '{"access_token":"x","blog_name":"sample"}',
            "active": True,
        }
    )

    scheduled_at = now_utc()
    assert store.schedule_job(
        job_id="sub-job-1",
        title="서브 잡 테스트",
        seed_keywords=["a", "b"],
        platform="tistory",
        persona_id="P1",
        scheduled_at=scheduled_at,
        job_kind=store.JOB_KIND_SUB,
        master_job_id="master-job-1",
        channel_id=channel_id,
    )
    claimed = store.claim_due_jobs(limit=1, now_override=scheduled_at)
    assert len(claimed) == 1
    job = claimed[0]

    base_publisher = DummyPublisher(success=True)
    sub_publisher = DummyPublisher(success=True)

    def _fake_get_publisher(_channel: Dict[str, Any]) -> DummyPublisher:
        return sub_publisher

    monkeypatch.setattr(
        "modules.automation.pipeline_service.get_publisher",
        _fake_get_publisher,
    )

    async def generate_ok(_job) -> Dict[str, Any]:
        return {
            "final_content": "content",
            "quality_gate": "pass",
            "quality_snapshot": {},
            "seo_snapshot": {},
            "llm_calls_used": 1,
        }

    pipeline = PipelineService(
        job_store=store,
        publisher=base_publisher,
        generate_fn=generate_ok,
    )
    monkeypatch.setattr(
        pipeline,
        "_evaluate_quality_gate",
        lambda **_kwargs: QualityGateResult(
            passed=True,
            gate="pass",
            score=95,
            error_code="",
            summary="ok",
        ),
    )
    asyncio.run(pipeline.run_job(job))

    updated = store.get_job("sub-job-1")
    assert updated is not None
    assert updated.status == store.STATUS_COMPLETED
    assert updated.result_url.endswith("/1")
    assert base_publisher.called == 0
    assert sub_publisher.called == 1


def test_schedule_post_idempotency(tmp_path: Path):
    """동일 idempotency 키 입력 시 두 번째 등록이 거절되는지 검증."""
    store = build_store(tmp_path)
    scheduled_at = "2026-02-21T00:00:00Z"

    first = store.schedule_job(
        job_id="idem-first",
        title="중복 방지 테스트",
        seed_keywords=["중복", "검증"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled_at,
    )
    second = store.schedule_job(
        job_id="idem-second",
        title="중복 방지 테스트",
        seed_keywords=["중복", "다른키워드"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled_at,
    )

    assert first is True
    assert second is False


def test_worker_graceful_shutdown(tmp_path: Path):
    """shutdown 요청 시 실행 중 job이 timeout 내 완료되는지 검증."""
    store = build_store(tmp_path)
    scheduled_at = now_utc()

    assert store.schedule_job(
        job_id="graceful-job",
        title="Graceful Shutdown",
        seed_keywords=["워커", "종료"],
        platform="naver",
        persona_id="P1",
        scheduled_at=scheduled_at,
    )

    async def process_job(job):
        await asyncio.sleep(0.2)
        store.complete_job(job.job_id, "https://blog.naver.com/graceful/1")

    async def scenario():
        worker = Worker(
            job_store=store,
            process_job=process_job,
            config=WorkerConfig(
                poll_interval_sec=0.05,
                max_concurrent_jobs=1,
                heartbeat_interval_sec=1,
                reaper_interval_sec=1,
                graceful_shutdown_timeout_sec=2,
            ),
        )
        worker_task = asyncio.create_task(worker.run())
        try:
            for _ in range(200):
                if worker.active_job_count > 0:
                    break
                await asyncio.sleep(0.01)
            assert worker.active_job_count == 1
            await worker.shutdown()
            await asyncio.wait_for(worker_task, timeout=5)
        finally:
            if not worker_task.done():
                worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await worker_task

    asyncio.run(scenario())

    updated = store.get_job("graceful-job")
    assert updated is not None
    assert updated.status == store.STATUS_COMPLETED


def test_pipeline_records_image_generation_log_and_free_tier_alert(tmp_path: Path):
    """이미지 생성 로그 저장과 무료티어 소진 알림 1회 정책을 검증한다."""
    from modules.images.image_generator import GeneratedImages

    store = build_store(tmp_path, "pipeline_image_log.db")
    job = schedule_and_claim(store, "image-log-job")

    async def simple_generate(_job) -> Dict[str, Any]:
        long_body = ("이미지 로그 테스트 본문입니다. " * 80).strip()
        return {
            "final_content": f"# 제목\n\n{long_body}",
            "quality_gate": "pass",
            "quality_snapshot": {},
            "seo_snapshot": {"topic_mode": "it"},
            "image_prompts": ["차트 이미지"],
            "llm_calls_used": 1,
        }

    class DummyImageGenerator:
        async def generate_for_post(self, title: str, keywords: list[str], image_prompts=None, image_slots=None):  # noqa: ANN001, ARG002
            generated = GeneratedImages(
                thumbnail_path="/tmp/thumb.jpg",
                content_paths=["/tmp/content.jpg"],
                source_kind_by_path={"/tmp/thumb.jpg": "stock", "/tmp/content.jpg": "stock"},
                provider_by_path={"/tmp/thumb.jpg": "pexels", "/tmp/content.jpg": "pexels"},
            )
            generated.generation_logs = [
                {
                    "slot_id": "thumb_0",
                    "slot_role": "thumbnail",
                    "provider": "pexels",
                    "status": "success",
                    "source_kind": "stock",
                    "latency_ms": 15.4,
                    "fallback_reason": "",
                    "cost_usd": 0.0,
                    "source_url": "https://pexels.test/thumb",
                }
            ]
            generated.free_tier_exhausted = True
            generated.free_tier_exhausted_events = [
                {"provider": "together_flux", "slot_id": "content_1", "reason": "HTTP 429"}
            ]
            return generated

    class DummyNotifier:
        def __init__(self):
            self.messages: list[str] = []

        def send_message_background(self, text: str, disable_notification: bool = False):  # noqa: ARG002
            self.messages.append(text)

        def notify_critical_background(self, *, error_code: str, message: str, job_id: str = ""):  # noqa: ARG002
            return None

    notifier = DummyNotifier()
    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=simple_generate,
        image_generator=DummyImageGenerator(),
        notifier=notifier,
    )

    asyncio.run(pipeline.run_job(job))

    logs = store.list_image_generation_logs(post_id=job.job_id)
    assert logs
    assert logs[0]["slot_id"] == "thumb_0"
    assert logs[0]["provider"] == "pexels"
    assert len(notifier.messages) == 1

    # 같은 날짜/같은 provider 재호출은 알림이 중복 전송되면 안 된다.
    pipeline._notify_image_free_tier_exhausted(
        job_id=job.job_id,
        events=[{"provider": "together_flux", "slot_id": "content_1", "reason": "HTTP 429"}],
    )
    assert len(notifier.messages) == 1


def test_pipeline_passes_topic_mode_to_image_generator(tmp_path: Path):
    """파이프라인은 seo_snapshot.topic_mode를 이미지 생성기에 전달해야 한다."""
    from modules.images.image_generator import GeneratedImages

    store = build_store(tmp_path, "pipeline_topic_mode.db")
    job = schedule_and_claim(store, "topic-mode-job")
    captured_topic_modes: list[str] = []

    async def simple_generate(_job) -> Dict[str, Any]:
        long_body = ("토픽 모드 전달 테스트 본문입니다. " * 80).strip()
        return {
            "final_content": f"# 제목\n\n{long_body}",
            "quality_gate": "pass",
            "quality_snapshot": {},
            "seo_snapshot": {"topic_mode": "finance"},
            "image_prompts": ["financial chart"],
            "llm_calls_used": 1,
        }

    class CaptureImageGenerator:
        async def generate_for_post(self, title: str, keywords: list[str], image_prompts=None, image_slots=None, topic_mode=None):  # noqa: ANN001, ARG002
            captured_topic_modes.append(str(topic_mode))
            return GeneratedImages(
                thumbnail_path="/tmp/thumb_topic.jpg",
                content_paths=[],
                source_kind_by_path={"/tmp/thumb_topic.jpg": "stock"},
                provider_by_path={"/tmp/thumb_topic.jpg": "pexels"},
            )

    class PassQualityGate:
        def evaluate(self, **kwargs):  # noqa: ANN003, ANN002
            return QualityGateResult(
                passed=True,
                gate="pass",
                score=95,
                error_code="",
                summary="ok",
            )

        def repair_content(self, **kwargs):  # noqa: ANN003, ANN002
            return str(kwargs.get("content", ""))

    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=simple_generate,
        image_generator=CaptureImageGenerator(),
        quality_gate=PassQualityGate(),
    )

    asyncio.run(pipeline.run_job(job))
    assert captured_topic_modes == ["finance"]
