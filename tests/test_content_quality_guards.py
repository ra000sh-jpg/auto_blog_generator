from modules.llm.content_generator import ContentGenerator
from modules.llm.prompts import get_persona_profile


class DummyClient:
    @property
    def provider_name(self) -> str:
        return "dummy"


def test_keyword_repetition_guard_preserves_market_terms():
    """시장 핵심 용어는 어색한 대명사로 치환하지 않는다."""

    generator = ContentGenerator(
        client=DummyClient(),  # type: ignore[arg-type]
        rss_news_collector=None,
        rag_search_engine=None,
    )

    content = "미장 흐름을 봅니다. 미장 흐름은 어렵습니다. 미장 기준을 기록합니다."
    updated = generator._limit_exact_keyword_repetition(
        content=content,
        keywords=["미장"],
        max_exact_matches=1,
    )

    assert updated == content
    assert "이 주제" not in updated
    assert "이 내용" not in updated
    assert "이 접근" not in updated


def test_keyword_variants_do_not_use_generic_placeholders():
    """키워드 변주 후보에 의미 없는 placeholder를 넣지 않는다."""

    generator = ContentGenerator(
        client=DummyClient(),  # type: ignore[arg-type]
        rss_news_collector=None,
        rag_search_engine=None,
    )

    variants = generator._build_keyword_variants("건강한 습관 만들기")

    assert variants
    assert "이 주제" not in variants
    assert "이 내용" not in variants
    assert "이 접근" not in variants


def test_language_artifact_sanitizer_removes_common_mixed_tokens():
    """일본어/영어/기타 외국 문자 흔적을 최종 후처리에서 정리한다."""

    generator = ContentGenerator(
        client=DummyClient(),  # type: ignore[arg-type]
        rss_news_collector=None,
        rag_search_engine=None,
    )

    updated = generator._sanitize_language_artifacts(
        "## 기준\n\n昨日 선물市場은 最新 흐름과 同時 조건이 interessring 했고 गलत된 기대를 확인해ボ며 기록했습니다."
    )

    assert "昨日" not in updated
    assert "最新" not in updated
    assert "同時" not in updated
    assert "市場" not in updated
    assert "गलत" not in updated
    assert "ボ" not in updated
    assert "interessring" not in updated
    assert "어젯밤" in updated
    assert "최신" in updated
    assert "선물시장" in updated
    assert "잘못된" in updated
    assert "확인해보며" in updated
    assert "## 기준\n\n" in updated


def test_language_artifact_sanitizer_removes_prompt_tail_labels():
    """모델이 본문에 남긴 프롬프트 라벨은 최종 산출물에서 제거한다."""

    generator = ContentGenerator(
        client=DummyClient(),  # type: ignore[arg-type]
        rss_news_collector=None,
        rag_search_engine=None,
    )

    updated = generator._sanitize_language_artifacts("본문입니다.\n\n[출력]\n\n참고 자료: 테스트")

    assert "[출력]" not in updated
    assert "본문입니다." in updated
    assert "참고 자료:" in updated


def test_markdown_table_repair_removes_broken_rows():
    """열 개수가 맞지 않는 깨진 표 행은 제거한다."""

    generator = ContentGenerator(
        client=DummyClient(),  # type: ignore[arg-type]
        rss_news_collector=None,
        rag_search_engine=None,
    )
    content = (
        "| 자산 | 가격 | 출처 |\n"
        "|---|---|---|\n"
        "| EWY | 203.97 USD | Stooq |\n"
        "| BTC | 62012 USD |\n"
        "\n본문입니다."
    )

    updated = generator._repair_markdown_tables(content)

    assert "| EWY | 203.97 USD | Stooq |" in updated
    assert "| BTC | 62012 USD |" not in updated
    assert "본문입니다." in updated


def test_local_plain_language_polish_explains_bullet_terms():
    """체크리스트 문장 안의 시장 용어도 쉬운 설명을 붙인다."""

    generator = ContentGenerator(
        client=DummyClient(),  # type: ignore[arg-type]
        rss_news_collector=None,
        rag_search_engine=None,
    )

    polished = generator._local_plain_language_polish(
        "- 개장 직후 외국인 현물·선물 누적 수급은 순매도인가"
    )

    assert polished.startswith("- ")
    assert "선물(앞으로의 가격을 미리 거래하는 상품)" in polished
    assert "수급(사고파는 힘의 균형)" in polished


def test_append_news_sources_replaces_existing_reference_block():
    """모델이 만든 참고자료 블록은 제거하고 표준 출처만 한 번 붙인다."""

    generator = ContentGenerator(
        client=DummyClient(),  # type: ignore[arg-type]
        rss_news_collector=None,
        rag_search_engine=None,
    )

    content = "본문입니다.\n\n참고 자료: 예전 출처 (https://old.example.com)"
    updated = generator._append_news_sources(
        content,
        [
            {
                "title": "시장 데이터 스냅샷: US_PREOPEN",
                "link": "https://example.com/new",
                "content": "요약",
            }
        ],
    )

    assert "old.example.com" not in updated
    assert updated.count("참고 자료:") == 1
    assert "https://example.com/new" in updated


def test_market_number_format_is_blog_friendly():
    """긴 소수점 시장 숫자를 블로그에 쓰기 쉬운 길이로 줄인다."""

    generator = ContentGenerator(
        client=DummyClient(),  # type: ignore[arg-type]
        rss_news_collector=None,
        rag_search_engine=None,
    )

    assert generator._format_market_number(-1.9457545218823817) == "-1.95"
    assert generator._format_market_number(93.0) == "93"


def test_heading_normalizer_promotes_h3_sections_under_h2_title():
    """제목이 H2로 밀리고 본문이 H3가 된 경우 네이버용 계층으로 복구한다."""

    generator = ContentGenerator(
        client=DummyClient(),  # type: ignore[arg-type]
        rss_news_collector=None,
        rag_search_engine=None,
    )

    updated = generator._normalize_heading_levels("## 제목\n\n### 첫 섹션\n본문\n\n### 둘째 섹션\n본문")

    assert updated.startswith("# 제목")
    assert "\n## 첫 섹션" in updated
    assert "\n## 둘째 섹션" in updated
    assert "###" not in updated


def test_market_sensitive_claim_sanitizer_removes_unsupported_fed_person_claim():
    """시장 브리핑은 공식 확인 전 연준 인물/정책 단정을 낮춘다."""

    generator = ContentGenerator(
        client=DummyClient(),  # type: ignore[arg-type]
        rss_news_collector=None,
        rag_search_engine=None,
    )

    content = (
        "## 미국 금리\n\n"
        "Kevin Warsh가 연준 의장으로 취임했다는 소식이 있습니다.\n"
        "그는 과거 금융위기 당시 조기 경고를 한 인물입니다.\n"
        "오늘은 공식 자료를 확인해야 합니다."
    )
    updated = generator._sanitize_market_sensitive_claims(content, [])

    assert "Kevin Warsh" not in updated
    assert "연준 의장으로 취임" not in updated
    assert "그는 과거" not in updated
    assert "공식 발표 원문" in updated


def test_generic_blog_phrase_sanitizer_lowers_teaching_tone():
    """강의식 일반론 문장을 함께 기록하는 문장으로 낮춘다."""

    generator = ContentGenerator(
        client=DummyClient(),  # type: ignore[arg-type]
        rss_news_collector=None,
        rag_search_engine=None,
    )

    content = (
        "오늘 브리핑에서는 데이터를 분석하여 투자에 참고할 수 있는 정보를 제공하겠습니다.\n"
        "예측을 자제하는 것이 중요합니다.\n"
        "리스크 관리는 성공적인 투자를 할 수 있습니다."
    )
    updated = generator._sanitize_generic_blog_phrases(content)

    assert "정보를 제공하겠습니다" not in updated
    assert "중요합니다" not in updated
    assert "성공적인 투자를 할 수 있습니다" not in updated
    assert "같이 확인한 뒤" in updated
    assert "예측을 자제하는 쪽으로 기준을 남겨보겠습니다" in updated


def test_finance_persona_does_not_force_cafe_operation_story():
    """금융/시장 글은 카페 운영담을 억지로 끌어오지 않는다."""

    persona = get_persona_profile("P4")

    assert persona.name == "투자를 공부하는 아빠"
    assert "카페 운영하며 부딪힌" not in persona.prompt_prefix
    assert "시장을 기록" in persona.prompt_prefix
    assert "리스크 점검표" in persona.prompt_prefix
