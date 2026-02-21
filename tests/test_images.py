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
