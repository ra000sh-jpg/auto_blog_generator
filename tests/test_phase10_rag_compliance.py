from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from modules.rag.search_engine import CrossEncoderRagSearchEngine
from modules.uploaders.playwright_publisher import PlaywrightPublisher


class _FakeCollector:
    """RAG 검색 엔진 테스트용 수집기."""

    def __init__(self, rows: List[Dict[str, str]]):
        self.rows = list(rows)
        self.calls: List[Dict[str, Any]] = []

    def fetch_relevant_news(
        self,
        keywords: Sequence[str],
        feed_urls: Optional[Sequence[str]] = None,
        within_hours: int = 24,
        max_items: int = 3,
    ) -> List[Dict[str, str]]:
        del feed_urls
        self.calls.append(
            {
                "keywords": list(keywords),
                "within_hours": within_hours,
                "max_items": max_items,
            }
        )
        return list(self.rows)[: max_items]


def test_cross_encoder_engine_uses_top2_with_lexical_rerank():
    """Cross-Encoder 비활성 시 lexical 재정렬로 상위 2건을 선택해야 한다."""
    collector = _FakeCollector(
        [
            {
                "title": "환율 전망 리포트",
                "link": "https://example.com/a",
                "content": "원달러 환율과 금리 흐름을 분석합니다.",
            },
            {
                "title": "육아 일상 브이로그",
                "link": "https://example.com/b",
                "content": "아이와 산책한 후기입니다.",
            },
            {
                "title": "기준금리 동결 뉴스",
                "link": "https://example.com/c",
                "content": "한국은행이 기준금리를 동결했습니다.",
            },
        ]
    )
    engine = CrossEncoderRagSearchEngine(
        news_collector=collector,  # type: ignore[arg-type]
        candidate_top_k=20,
        final_top_k=2,
        cross_encoder_enabled=False,
    )

    rows = engine.retrieve(
        keywords=["환율", "금리"],
        query_text="환율 금리",
    )

    assert len(rows) == 2
    assert rows[0]["link"] in {"https://example.com/a", "https://example.com/c"}
    assert rows[1]["link"] in {"https://example.com/a", "https://example.com/c"}
    assert rows[0]["rerank_method"] == "lexical"
    assert rows[1]["rerank_method"] == "lexical"
    assert collector.calls[0]["max_items"] == 20
    assert engine.last_stats.selected_count == 2
    assert engine.last_stats.reranker == "lexical"


def test_cross_encoder_engine_falls_back_when_model_unavailable(monkeypatch):
    """Cross-Encoder 로딩 실패 시 lexical 폴백으로 중단 없이 진행해야 한다."""
    collector = _FakeCollector(
        [
            {
                "title": "경제 브리핑",
                "link": "https://example.com/1",
                "content": "거시경제 지표 요약",
            },
            {
                "title": "증시 체크",
                "link": "https://example.com/2",
                "content": "코스피와 나스닥 동향",
            },
        ]
    )
    engine = CrossEncoderRagSearchEngine(
        news_collector=collector,  # type: ignore[arg-type]
        cross_encoder_enabled=True,
        final_top_k=2,
    )
    monkeypatch.setattr(engine, "_get_cross_encoder", lambda: None)

    rows = engine.retrieve(
        keywords=["경제", "증시"],
        query_text="경제 증시",
    )
    assert len(rows) == 2
    assert all(item["rerank_method"] == "lexical" for item in rows)
    assert engine.last_stats.reranker == "lexical"


def test_publisher_ai_image_detection_explicit_provider_only():
    """명시적 AI provider 파일만 AI 활용 설정 대상이어야 한다."""
    publisher = PlaywrightPublisher(blog_id="phase10-test")

    assert publisher._is_ai_generated_image("data/images/together_abc.png") is True
    assert publisher._is_ai_generated_image("data/images/resized/together_abc_w880.png") is True
    assert publisher._is_ai_generated_image("data/images/fal_abc.png") is True
    assert publisher._is_ai_generated_image("data/images/openai_abc.png") is True
    assert publisher._is_ai_generated_image("data/images/dashscope_abc.png") is True
    assert publisher._is_ai_generated_image("data/images/huggingface_abc.png") is True
    assert publisher._is_ai_generated_image("data/images/pollinations_abc.png") is True

    # 실사 스톡/플레이스홀더/알 수 없는 파일명은 대상이 아니다.
    assert publisher._is_ai_generated_image("data/images/pexels_abc.jpg") is False
    assert publisher._is_ai_generated_image("data/images/placeholder_abc.jpg") is False
    assert publisher._is_ai_generated_image("data/images/custom_upload.png") is False


def test_publisher_defaults_body_top_thumbnail(monkeypatch):
    """기본 썸네일 배치 모드는 body_top이어야 한다."""
    monkeypatch.delenv("THUMBNAIL_PLACEMENT_MODE", raising=False)
    publisher = PlaywrightPublisher(blog_id="phase10-mode-test")
    assert publisher._thumbnail_placement_mode == "body_top"


def test_publisher_defaults_image_upload_width_800(monkeypatch):
    """기본 이미지 업로드 폭은 800px이어야 한다 (콜라주 방지 개선 후)."""
    monkeypatch.delenv("IMAGE_UPLOAD_WIDTH", raising=False)
    publisher = PlaywrightPublisher(blog_id="phase10-width-test")
    assert publisher._image_upload_target_width == 800


def test_prepare_image_for_upload_returns_original_on_missing_file():
    """존재하지 않는 파일은 원본 경로를 그대로 반환해야 한다."""
    publisher = PlaywrightPublisher(blog_id="phase10-resize-test")
    missing_path = "data/images/not-exists.png"
    assert publisher._prepare_image_for_upload(missing_path) == missing_path


def test_publisher_detects_draft_recovery_prompt_text():
    """임시저장 복구 팝업 문구를 안정적으로 감지해야 한다."""
    assert PlaywrightPublisher._is_draft_recovery_prompt_text("작성 중인 글이 있습니다.") is True
    assert PlaywrightPublisher._is_draft_recovery_prompt_text("이어서 작성하시겠습니까?") is True
    assert PlaywrightPublisher._is_draft_recovery_prompt_text("일반 안내 팝업") is False


def test_publisher_detects_reserved_publish_popup_text():
    """예약 발행 글 레이어 문구를 안정적으로 감지해야 한다."""
    assert PlaywrightPublisher._is_reserved_publish_popup_text("예약 발행 글") is True
    assert PlaywrightPublisher._is_reserved_publish_popup_text("예약발행글 안내") is True
    assert PlaywrightPublisher._is_reserved_publish_popup_text("임시저장 복구") is False


def test_publisher_uses_image_source_metadata_for_ai_detection():
    """파일명 대신 메타데이터(kind/provider)로 AI 여부를 판별해야 한다."""
    publisher = PlaywrightPublisher(blog_id="phase10-meta-test")
    publisher._set_image_source_lookup(
        {
            "data/images/custom_uploaded.jpg": {"kind": "ai", "provider": "together"},
            "data/images/together_named_but_stock.jpg": {"kind": "stock", "provider": "pexels"},
        }
    )

    assert publisher._is_ai_generated_image("data/images/custom_uploaded.jpg") is True
    assert publisher._is_ai_generated_image("data/images/together_named_but_stock.jpg") is False


def test_publisher_force_ai_toggle_env(monkeypatch):
    """강제 토글 옵션이 켜지면 non-ai도 토글 대상으로 취급할 수 있어야 한다."""
    monkeypatch.setenv("NAVER_AI_TOGGLE_FORCE", "true")
    publisher = PlaywrightPublisher(blog_id="phase10-force-test")
    assert publisher._force_ai_toggle is True


def test_publisher_extract_log_no_from_post_url():
    """PostView URL에서 logNo를 안정적으로 추출해야 한다."""
    publisher = PlaywrightPublisher(blog_id="phase10-url-test")
    url = "https://blog.naver.com/PostView.naver?blogId=ra000sh&logNo=224190425684&Redirect=View"
    assert publisher._extract_log_no_from_post_url(url) == "224190425684"


def test_publisher_build_update_url_from_post_url():
    """발행 URL에서 수정 페이지 URL을 구성할 수 있어야 한다."""
    publisher = PlaywrightPublisher(blog_id="ra000sh")
    post_url = "https://blog.naver.com/PostView.naver?blogId=ra000sh&logNo=224190425684&Redirect=View"
    assert (
        publisher._build_update_url_from_post_url(post_url)
        == "https://blog.naver.com/ra000sh?Redirect=Update&logNo=224190425684"
    )


def test_publisher_build_image_match_tokens_includes_upload_variants():
    """원본/리사이즈 경로를 모두 매칭할 수 있도록 토큰을 생성해야 한다."""
    publisher = PlaywrightPublisher(blog_id="phase10-token-test")
    tokens = publisher._build_image_match_tokens(
        "data/images/pexels_34153ce7-121b-48ce-a6cd-55381aa3cf95.jpg",
        "data/images/resized/pexels_34153ce7-121b-48ce-a6cd-55381aa3cf95_w500.jpg",
    )
    assert "pexels_34153ce7-121b-48ce-a6cd-55381aa3cf95.jpg" in tokens
    assert "pexels_34153ce7-121b-48ce-a6cd-55381aa3cf95" in tokens
    assert "pexels_34153ce7-121b-48ce-a6cd-55381aa3cf95_w500.jpg" in tokens
    assert "pexels_34153ce7-121b-48ce-a6cd-55381aa3cf95_w500" in tokens


def test_publisher_decide_ai_toggle_respects_metadata_and_force(monkeypatch):
    """메타데이터 모드와 force 모드에서 기대값이 다르게 계산되어야 한다."""
    monkeypatch.delenv("NAVER_AI_TOGGLE_FORCE", raising=False)
    publisher = PlaywrightPublisher(blog_id="phase10-decision-test")
    publisher._set_image_source_lookup(
        {"data/images/stock_case.jpg": {"kind": "stock", "provider": "pexels"}}
    )
    decision = publisher._decide_ai_toggle("data/images/stock_case.jpg")
    assert decision["should_toggle"] is False
    assert decision["source_kind"] == "stock"
    assert decision["provider"] == "pexels"
    assert decision["mode"] == "metadata"

    monkeypatch.setenv("NAVER_AI_TOGGLE_FORCE", "true")
    force_publisher = PlaywrightPublisher(blog_id="phase10-force-decision-test")
    force_publisher._set_image_source_lookup(
        {"data/images/stock_case.jpg": {"kind": "stock", "provider": "pexels"}}
    )
    force_decision = force_publisher._decide_ai_toggle("data/images/stock_case.jpg")
    assert force_decision["should_toggle"] is True
    assert force_decision["mode"] == "force"


def test_publisher_ai_toggle_mode_off_disables_all(monkeypatch):
    """off 모드에서는 source_kind와 무관하게 토글을 비활성화해야 한다."""
    monkeypatch.setenv("NAVER_AI_TOGGLE_MODE", "off")
    publisher = PlaywrightPublisher(blog_id="phase10-off-test")
    publisher._set_image_source_lookup(
        {"data/images/ai_case.jpg": {"kind": "ai", "provider": "together"}}
    )
    decision = publisher._decide_ai_toggle("data/images/ai_case.jpg")
    assert decision["mode"] == "off"
    assert decision["should_toggle"] is False


def test_publisher_ai_toggle_alert_background_uses_notifier():
    """AI 토글 경고는 notifier가 활성일 때 background 전송되어야 한다."""

    class _FakeNotifier:
        enabled = True

        def __init__(self):
            self.messages = []

        def send_message_background(self, text, disable_notification=False):
            self.messages.append((text, disable_notification))

    publisher = PlaywrightPublisher(blog_id="phase10-alert-test")
    fake = _FakeNotifier()
    publisher._telegram_notifier = fake
    publisher._notify_ai_toggle_alert_background("title", ["- a", "- b"])
    assert len(fake.messages) == 1
    assert "title" in fake.messages[0][0]


def test_ai_toggle_regression_scenarios_ai_mixed_stock(monkeypatch):
    """AI-only/Mixed/Stock-only 회귀 시나리오의 기대 판정을 유지해야 한다."""
    monkeypatch.setenv("NAVER_AI_TOGGLE_MODE", "metadata")
    publisher = PlaywrightPublisher(blog_id="phase10-regression-test")
    publisher._set_image_source_lookup(
        {
            "data/images/scenario_ai.jpg": {"kind": "ai", "provider": "together"},
            "data/images/scenario_stock.jpg": {"kind": "stock", "provider": "pexels"},
            "data/images/scenario_placeholder.jpg": {"kind": "placeholder", "provider": "pollinations"},
        }
    )

    # AI-only 시나리오
    assert publisher._decide_ai_toggle("data/images/scenario_ai.jpg")["should_toggle"] is True
    # Mixed 시나리오(1: AI, 1: Stock)
    assert publisher._decide_ai_toggle("data/images/scenario_stock.jpg")["should_toggle"] is False
    # Stock-only 시나리오
    assert publisher._decide_ai_toggle("data/images/scenario_placeholder.jpg")["should_toggle"] is False
