def test_styles_have_required_fields():
    """스타일 프리셋 필수 필드를 검증한다."""
    from modules.images.styles import CONTENT_STYLES, THUMBNAIL_STYLES

    for style in THUMBNAIL_STYLES.values():
        assert style.name
        assert style.suffix
        assert style.description

    for style in CONTENT_STYLES.values():
        assert style.name
        assert style.suffix
        assert style.description


def test_image_generator_builds_prompt():
    """썸네일 프롬프트 생성 규칙을 검증한다."""
    import asyncio
    from modules.images.image_generator import ImageGenerator

    generator = ImageGenerator.__new__(ImageGenerator)
    generator.prompt_translator = None  # 번역기 없는 경우
    prompt = asyncio.run(generator._build_thumbnail_prompt("테스트 제목", ["키워드1", "키워드2"]))

    assert "키워드1" in prompt
    assert "테스트 제목" in prompt


def test_summary_card_renderer_creates_png(tmp_path):
    """본문 요약 카드가 비용 없이 PNG로 생성되어야 한다."""
    from PIL import Image

    from modules.images.summary_card_renderer import extract_summary_bullets, render_summary_card

    content = (
        "며칠 전 시장이 크게 흔들렸고, 저는 먼저 손실보다 기록을 확인했습니다.\n\n"
        "## 시장 전망보다 먼저 마주해야 하는 오해\n"
        "초심자는 정보 부족보다 불안 때문에 더 자주 흔들립니다.\n\n"
        "## 초기 자산 배분, 왜 첫 번째 경계선인가\n"
        "배분은 수익률을 높이는 기술이라기보다 평범한 하루를 지키는 틀입니다.\n\n"
        "## 손실을 마주하는 연습\n"
        "작은 손실에서 감정 반응을 기록하면 무리한 선택을 줄일 수 있습니다.\n"
    )

    bullets = extract_summary_bullets(
        content=content,
        title="손실을 마주하는 연습",
        max_bullets=4,
    )
    assert len(bullets) >= 3
    assert "시장 전망보다 먼저 마주해야 하는 오해" in bullets

    result = render_summary_card(
        title="손실을 마주하는 연습",
        content=content,
        output_dir=str(tmp_path),
    )

    assert result is not None
    assert result.path.endswith(".png")
    with Image.open(result.path) as image:
        assert image.size == (1080, 1350)


def test_market_chart_renderer_creates_change_bar_png(tmp_path):
    """변동률 데이터가 있으면 시장 막대그래프 PNG가 생성되어야 한다."""
    from PIL import Image

    from modules.images.market_chart_renderer import render_market_chart

    result = render_market_chart(
        title="미장 전 브리핑",
        output_dir=str(tmp_path),
        market_snapshot={
            "slot": "us_preopen",
            "scope": "us",
            "data_points": [
                {"symbol": "SPY", "source": "Stooq", "value": 624.1, "change_percent": 0.72},
                {"symbol": "QQQ", "source": "Stooq", "value": 542.3, "change_percent": -0.35},
                {"symbol": "BTC", "source": "CoinGecko", "value": 104200.0, "change_percent": 1.8},
            ],
        },
    )

    assert result is not None
    assert result.mode == "change_bar"
    assert result.point_count == 3
    with Image.open(result.path) as image:
        assert image.size == (1200, 760)


def test_market_chart_renderer_creates_indicator_board_when_change_missing(tmp_path):
    """단위가 다른 시장 수치는 억지 그래프 대신 지표 보드로 생성되어야 한다."""
    from PIL import Image

    from modules.images.market_chart_renderer import render_market_chart

    result = render_market_chart(
        title="국장 전 브리핑",
        output_dir=str(tmp_path),
        market_snapshot={
            "slot": "kr_preopen",
            "scope": "kr",
            "data_points": [
                {"symbol": "KOSPI", "source": "Stooq", "value": 2870.25},
                {"symbol": "USD/KRW", "source": "Yahoo", "value": 1368.4},
                {"symbol": "US10Y", "source": "FRED", "value": 4.38},
            ],
        },
    )

    assert result is not None
    assert result.mode == "indicator_board"
    assert result.point_count == 3
    with Image.open(result.path) as image:
        assert image.size == (1200, 760)


def test_flowchart_renderer_creates_png(tmp_path):
    """흐름도 렌더러가 단계형 PNG를 생성해야 한다."""
    from modules.images.flowchart_renderer import render_flowchart

    result = render_flowchart(
        title="오늘 투자 판단 흐름",
        nodes=[
            "환율 압력을 먼저 확인한다",
            "금리와 선물 흐름을 함께 본다",
            "포지션을 더하기보다 줄일 기준을 정한다",
        ],
        output_dir=str(tmp_path),
        style="market_note",
    )

    assert result is not None
    assert result.node_count == 3
    assert result.path.endswith(".png")


def test_market_note_table_renderer_sanitizes_cells_and_creates_png(tmp_path):
    """시장노트형 표는 셀 수 불일치와 짧은 오타를 정리한 뒤 PNG로 생성한다."""

    from PIL import Image

    from modules.images.table_renderer import extract_and_render_tables_with_validation
    from modules.images.visual_text_sanitizer import sanitize_visual_text

    assert sanitize_visual_text("수익율과 테그") == "수익률과 태그"

    content = (
        "| 구분 | 확인 기준 | 메모 |\n"
        "| --- | --- | --- |\n"
        "| 반도체 | +1.2% 강세 | 수익율보다 수급 |\n"
        "| 환율 | 하락 | 전일대비 부담 | 추가 셀 |\n"
    )

    modified, paths, validation = extract_and_render_tables_with_validation(
        content=content,
        output_dir=str(tmp_path),
        style="market_note",
    )

    assert "[TABLE_0]" in modified
    assert len(paths) == 1
    assert validation.passed is False
    assert any("trimmed" in issue for issue in validation.issues)
    with Image.open(paths[0]) as image:
        assert image.size[0] > 0
        assert image.size[1] > 0


def test_pollinations_client_size_parsing():
    """size 문자열이 올바르게 (width, height)로 변환되어야 한다."""
    from modules.images.pollinations_client import PollinationsImageClient

    assert PollinationsImageClient._parse_size("1024*1024") == (1024, 1024)
    assert PollinationsImageClient._parse_size("1024*768") == (1024, 768)
    assert PollinationsImageClient._parse_size("invalid") == (1024, 1024)


def test_pexels_client_availability():
    """Pexels 클라이언트 가용성 확인 (API 키 유무에 따라)."""
    from modules.images.pexels_client import PexelsImageClient

    # API 키가 명시적으로 주어진 경우
    client_with_key = PexelsImageClient(api_key="test_key")
    assert client_with_key.is_available() is True
    assert client_with_key.api_key == "test_key"

    # API 키가 빈 문자열이면 환경변수 폴백 (is_available은 self.api_key 체크)
    # 환경변수에 PEXELS_API_KEY가 있으면 True, 없으면 False
    client_default = PexelsImageClient()
    # is_available()은 bool(self.api_key) 반환
    assert client_default.is_available() == bool(client_default.api_key)


def test_pexels_client_orientation_inference():
    """Pexels 클라이언트 orientation 추론 테스트."""
    from modules.images.pexels_client import PexelsImageClient

    assert PexelsImageClient._infer_orientation("1024*768") == "landscape"
    assert PexelsImageClient._infer_orientation("768*1024") == "portrait"
    assert PexelsImageClient._infer_orientation("1024*1024") == "square"
    assert PexelsImageClient._infer_orientation("invalid") is None


def test_pexels_client_prompt_cleaning():
    """Pexels 검색어 정제 테스트."""
    from modules.images.pexels_client import PexelsImageClient

    # 쉼표 구분된 프롬프트 → 앞부분만 사용
    long_prompt = "coffee shop, warm lighting, cozy atmosphere, wooden furniture"
    cleaned = PexelsImageClient._clean_prompt_for_search(long_prompt)
    assert "coffee shop" in cleaned
    assert "warm lighting" in cleaned
    assert len(cleaned.split()) <= 10  # 너무 길지 않아야 함

    # 긴 프롬프트 → 8단어 제한
    very_long = "a b c d e f g h i j k l m n o"
    cleaned_long = PexelsImageClient._clean_prompt_for_search(very_long)
    assert len(cleaned_long.split()) <= 8


def test_image_generator_topic_strategy():
    """토픽별 이미지 소스 전략 확인."""
    from modules.images.image_generator import ImageGenerator

    assert ImageGenerator.TOPIC_IMAGE_STRATEGY["cafe"] == "stock_first"
    assert ImageGenerator.TOPIC_IMAGE_STRATEGY["parenting"] == "stock_first"
    assert ImageGenerator.TOPIC_IMAGE_STRATEGY["it"] == "mixed"
    assert ImageGenerator.TOPIC_IMAGE_STRATEGY["finance"] == "mixed"


def test_image_generator_stock_queries():
    """스톡 포토 검색어 준비 테스트."""
    from modules.images.image_generator import ImageGenerator

    generator = ImageGenerator.__new__(ImageGenerator)
    keywords = ["커피", "카페", "인테리어"]
    prompts = ["cafe interior design", "coffee brewing process"]

    queries = generator._prepare_stock_queries(keywords, prompts)

    assert len(queries) == 2
    assert "커피" in queries[0] or "cafe" in queries[0]


def test_openai_image_client_size_normalization():
    """OpenAI 이미지 size 정규화 규칙을 검증한다."""
    from modules.images.openai_image_client import OpenAIImageClient

    assert OpenAIImageClient._normalize_size("1024*1024") == "1024x1024"
    assert OpenAIImageClient._normalize_size("1024*768") == "1792x1024"
    assert OpenAIImageClient._normalize_size("768*1024") == "1024x1792"
    assert OpenAIImageClient._normalize_size("invalid") == "1024x1024"


def test_runtime_image_factory_uses_router_selected_engine(tmp_path):
    """라우터 엔진 설정값에 따라 런타임 이미지 생성기를 구성해야 한다."""
    import asyncio

    from modules.automation.job_store import JobStore
    from modules.config import load_config
    from modules.images.openai_image_client import OpenAIImageClient
    from modules.images.runtime_factory import build_runtime_image_generator

    config = load_config()
    config.images.enabled = True
    config.images.output_dir = str(tmp_path / "images")

    store = JobStore(str(tmp_path / "router_image.db"))
    store.set_system_setting("router_text_api_keys", '{"openai":"sk-test-openai"}')
    store.set_system_setting("router_image_api_keys", '{"openai_image":"sk-test-openai-img"}')
    store.set_system_setting("router_image_engine", "openai_dalle3")
    store.set_system_setting("router_image_enabled", "true")
    store.set_system_setting("router_images_per_post", "2")

    generator = build_runtime_image_generator(
        app_config=config,
        job_store=store,
        topic_mode="finance",
    )
    assert generator is not None
    assert isinstance(generator.client, OpenAIImageClient)
    assert generator.max_content_images == 2
    asyncio.run(generator.close())


def test_runtime_image_factory_respects_router_disable(tmp_path):
    """라우터에서 이미지 비활성화 시 생성기를 만들지 않아야 한다."""
    from modules.automation.job_store import JobStore
    from modules.config import load_config
    from modules.images.runtime_factory import build_runtime_image_generator

    config = load_config()
    config.images.enabled = True
    config.images.output_dir = str(tmp_path / "images")

    store = JobStore(str(tmp_path / "router_image_disable.db"))
    store.set_system_setting("router_image_enabled", "false")
    store.set_system_setting("router_images_per_post", "0")

    generator = build_runtime_image_generator(
        app_config=config,
        job_store=store,
        topic_mode="cafe",
    )
    assert generator is None


def test_image_generator_fallback_skips_placeholder_and_uses_next_provider():
    """첫 번째 결과가 placeholder면 다음 폴백 프로바이더를 사용해야 한다."""
    import asyncio

    from modules.images.dashscope_image_client import ImageResult
    from modules.images.image_generator import ImageGenerator

    class PollinationsImageClient:
        """placeholder 경로를 반환하는 테스트용 클라이언트."""

        async def generate(self, prompt: str, style_suffix: str, size: str, n: int = 1):  # noqa: ARG002
            return ImageResult(
                success=True,
                image_url="https://example.com/placeholder",
                local_path="data/images/placeholder_test.jpg",
            )

    class HuggingFaceImageClient:
        """정상 AI 이미지를 반환하는 테스트용 클라이언트."""

        async def generate(self, prompt: str, style_suffix: str, size: str, n: int = 1):  # noqa: ARG002
            return ImageResult(
                success=True,
                image_url="hf://test",
                local_path="data/images/huggingface_test.png",
            )

    generator = ImageGenerator(
        client=PollinationsImageClient(),  # type: ignore[arg-type]
        fallback_clients=[HuggingFaceImageClient()],  # type: ignore[list-item]
        parallel=False,
    )

    result, source_kind, provider = asyncio.run(
        generator._generate_with_fallback("prompt", "", "1024*768")
    )

    assert result.success is True
    assert source_kind == "ai"
    assert "huggingface" in provider
    assert result.local_path == "data/images/huggingface_test.png"


def test_image_generator_fallback_returns_placeholder_when_no_next_provider():
    """후속 폴백이 없으면 마지막 placeholder 결과를 반환해야 한다."""
    import asyncio

    from modules.images.dashscope_image_client import ImageResult
    from modules.images.image_generator import ImageGenerator

    class PollinationsImageClient:
        """placeholder 경로를 반환하는 테스트용 클라이언트."""

        async def generate(self, prompt: str, style_suffix: str, size: str, n: int = 1):  # noqa: ARG002
            return ImageResult(
                success=True,
                image_url="https://example.com/placeholder",
                local_path="data/images/placeholder_only.jpg",
            )

    generator = ImageGenerator(
        client=PollinationsImageClient(),  # type: ignore[arg-type]
        fallback_clients=[],
        parallel=False,
    )

    result, source_kind, provider = asyncio.run(
        generator._generate_with_fallback("prompt", "", "1024*768")
    )

    assert result.success is True
    assert source_kind == "placeholder"
    assert "pollinations" in provider
    assert result.local_path == "data/images/placeholder_only.jpg"


def test_runtime_image_factory_applies_topic_quota_override(tmp_path):
    """topic_mode override가 있으면 기본 quota 대신 override를 사용해야 한다."""
    import asyncio

    from modules.automation.job_store import JobStore
    from modules.config import load_config
    from modules.images.runtime_factory import build_runtime_image_generator

    config = load_config()
    config.images.enabled = True
    config.images.output_dir = str(tmp_path / "images")

    store = JobStore(str(tmp_path / "router_quota_override.db"))
    store.set_system_setting("router_text_api_keys", '{"qwen":"test"}')
    store.set_system_setting("router_image_api_keys", '{"together":"test", "pexels":"test"}')
    store.set_system_setting("router_image_ai_engine", "together_flux")
    store.set_system_setting("router_image_ai_quota", "0")
    store.set_system_setting("router_image_topic_quota_overrides", '{"finance":"1"}')
    store.set_system_setting("router_image_enabled", "true")
    store.set_system_setting("router_images_per_post", "4")
    store.set_system_setting("router_images_per_post_min", "0")
    store.set_system_setting("router_images_per_post_max", "4")

    generator = build_runtime_image_generator(
        app_config=config,
        job_store=store,
        topic_mode="finance",
    )
    assert generator is not None
    assert generator.ai_image_quota == "0"
    assert generator._resolve_ai_quota_for_topic("finance") == "1"
    asyncio.run(generator.close())


def test_image_generator_quota_assigns_single_ai_slot():
    """quota=1이면 점수가 가장 높은 슬롯 1개만 AI로 배정되어야 한다."""
    import asyncio

    from modules.images.dashscope_image_client import ImageResult
    from modules.images.image_generator import ImageGenerator

    class AIClient:
        async def generate(self, prompt: str, style_suffix: str = "", size: str = "1024*768", n: int = 1):  # noqa: ARG002
            return ImageResult(success=True, image_url="ai://generated", local_path=f"/tmp/ai_{abs(hash(prompt))}.png")

    class StockClient:
        async def generate(self, prompt: str, size: str = "1024*768"):  # noqa: ARG002
            return ImageResult(success=True, image_url="stock://photo", local_path=f"/tmp/stock_{abs(hash(prompt))}.jpg")

    generator = ImageGenerator(
        client=AIClient(),  # type: ignore[arg-type]
        fallback_clients=[],
        stock_client=StockClient(),  # type: ignore[arg-type]
        ai_image_quota="1",
        parallel=False,
    )

    result = asyncio.run(
        generator.generate_for_post(
            title="테스트",
            keywords=["테스트", "이미지"],
            image_slots=[
                {
                    "slot_id": "thumb_0",
                    "slot_role": "thumbnail",
                    "prompt": "thumbnail",
                    "preferred_type": "real",
                    "recommended": False,
                    "ai_generation_score": 10,
                },
                {
                    "slot_id": "content_1",
                    "slot_role": "content",
                    "prompt": "ai first",
                    "preferred_type": "ai_generated",
                    "recommended": True,
                    "ai_generation_score": 95,
                },
                {
                    "slot_id": "content_2",
                    "slot_role": "content",
                    "prompt": "stock second",
                    "preferred_type": "real",
                    "recommended": False,
                    "ai_generation_score": 20,
                },
            ],
        )
    )

    assert result.thumbnail_path is not None
    assert len(result.content_paths) == 2
    ai_logs = [row for row in result.generation_logs if row.get("source_kind") == "ai"]
    assert len(ai_logs) == 1
    assert ai_logs[0]["slot_id"] == "content_1"


def test_image_generator_detects_free_tier_exhaustion_and_fallbacks_to_stock():
    """무료 티어 소진(429) 시 AI 슬롯도 스톡으로 폴백되어야 한다."""
    import asyncio

    from modules.images.dashscope_image_client import ImageResult
    from modules.images.image_generator import ImageGenerator

    class TogetherFailClient:
        async def generate(self, prompt: str, style_suffix: str = "", size: str = "1024*768", n: int = 1):  # noqa: ARG002
            return ImageResult(success=False, error_message="HTTP 429 rate limit exceeded")

    class PollinationsSuccessClient:
        async def generate(self, prompt: str, style_suffix: str = "", size: str = "1024*768", n: int = 1):  # noqa: ARG002
            return ImageResult(success=True, image_url="ai://pollinations", local_path="/tmp/pollinations.png")

    class StockClient:
        async def generate(self, prompt: str, size: str = "1024*768"):  # noqa: ARG002
            return ImageResult(success=True, image_url="stock://fallback", local_path="/tmp/stock_fallback.jpg")

    generator = ImageGenerator(
        client=TogetherFailClient(),  # type: ignore[arg-type]
        fallback_clients=[PollinationsSuccessClient()],  # type: ignore[list-item]
        stock_client=StockClient(),  # type: ignore[arg-type]
        ai_image_quota="1",
        parallel=False,
    )

    result = asyncio.run(
        generator.generate_for_post(
            title="무료 티어 소진",
            keywords=["테스트"],
            image_slots=[
                {
                    "slot_id": "content_1",
                    "slot_role": "content",
                    "prompt": "flow chart",
                    "preferred_type": "ai_generated",
                    "recommended": True,
                    "ai_generation_score": 90,
                }
            ],
        )
    )

    assert result.free_tier_exhausted is True
    assert result.content_paths
    assert result.source_kind_by_path[result.content_paths[0]] == "stock"
    fallback_logs = [row for row in result.generation_logs if row.get("fallback_reason") == "free_tier_exhausted"]
    assert fallback_logs
