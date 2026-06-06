from __future__ import annotations

from modules.llm.insight_strategy import (
    InsightQualityEvaluator,
    build_insight_strategy,
)


def test_market_topic_uses_criteria_and_order_frames():
    """시장 브리핑은 정보보다 기준과 흔들릴 때 돌아갈 기준을 선택해야 한다."""
    strategy = build_insight_strategy(
        title="국장 시작 전 반도체와 환율을 함께 보는 법",
        keywords=["국장", "반도체", "환율", "외국인 수급"],
        topic_mode="finance",
    )

    assert strategy.primary_frame.frame_id == "criteria_over_information"
    assert strategy.secondary_frame.frame_id == "order_after_chaos"
    assert "방향을 맞히는 것" in strategy.thesis
    assert "함께 공부" in strategy.to_prompt_block()
    assert "고등학생" in strategy.to_prompt_block()


def test_automation_topic_uses_attention_frame():
    """자동화 주제는 반복 작업보다 판단 에너지 회복을 중심에 둬야 한다."""
    strategy = build_insight_strategy(
        title="AI 블로그 자동화 프로그램을 운영하며 배운 점",
        keywords=["AI", "자동화", "블로그"],
        topic_mode="it",
    )

    assert strategy.primary_frame.frame_id == "automation_attention"
    assert "반복 작업" in strategy.thesis


def test_insight_quality_prefers_learning_plain_language():
    """함께 공부하는 쉬운 문체는 권위적 설명체보다 높은 점수를 받아야 한다."""
    strategy = build_insight_strategy(
        title="미장 시작 전 금리와 달러를 같이 보는 이유",
        keywords=["미장", "금리", "달러", "VIX"],
        topic_mode="finance",
    )
    evaluator = InsightQualityEvaluator()

    learning_content = """
## 오늘 먼저 볼 숫자

저도 미장 흐름을 완벽히 맞히지는 못합니다. 그래서 오늘은 금리와 달러를 먼저 같이 확인해보려 합니다.
금리는 쉽게 말하면 돈을 빌리는 비용입니다. 이 비용이 높아지면 성장주에는 부담이 될 수 있습니다.

## 시장이 흔들릴 때 돌아갈 기준

시장은 매일 흔들리지만, 제 기준까지 같이 흔들릴 필요는 없다고 느낍니다.
오늘은 방향을 맞히기보다 내가 어떤 뉴스에 흔들리는지 점검하는 편이 더 안전해 보입니다.

오늘 함께 확인할 공부 질문은 세 가지입니다.
- 금리가 오른 이유는 무엇일까?
- 달러가 강한 날에 외국인 수급은 어떻게 움직일까?
- 내가 줄여야 할 리스크는 무엇일까?
""".strip()

    hard_content = """
## 결론

금리 상승과 달러 강세는 위험자산 밸류에이션 멀티플을 압박한다.
투자자는 반드시 VIX와 DXY를 확인해야 합니다. 정답은 외국인 수급 추종입니다.
이것만 보면 됩니다. 무조건 리스크 자산 비중을 조절해야 합니다.
""".strip()

    learning_result = evaluator.evaluate(
        content=learning_content,
        title="미장 시작 전 금리와 달러를 같이 보는 이유",
        keywords=["미장", "금리", "달러", "VIX"],
        topic_mode="finance",
        strategy=strategy,
    )
    hard_result = evaluator.evaluate(
        content=hard_content,
        title="미장 시작 전 금리와 달러를 같이 보는 이유",
        keywords=["미장", "금리", "달러", "VIX"],
        topic_mode="finance",
        strategy=strategy,
    )

    assert learning_result.overall_score > hard_result.overall_score
    assert learning_result.learning_tone_score > hard_result.learning_tone_score
    assert learning_result.plain_language_score > hard_result.plain_language_score
    assert hard_result.needs_rewrite is True


def test_plain_language_accepts_term_explanation_after_table_first_hit():
    """표에 먼저 나온 용어도 본문에서 설명하면 쉬운 언어 기준에서 인정한다."""

    strategy = build_insight_strategy(
        title="국장 전 ETF와 선물을 함께 보는 기준",
        keywords=["국장", "ETF", "선물"],
        topic_mode="finance",
    )
    evaluator = InsightQualityEvaluator()
    content = """
| 지표 | 값 |
|---|---|
| ETF | 100 |
| 선물 | 90 |

## 같이 확인할 기준

저도 오늘은 ETF와 선물을 단정적으로 보지 않으려 합니다.
ETF(여러 자산을 한 바구니처럼 담은 상장 펀드)는 시장의 큰 방향을 보는 데 씁니다.
선물(앞으로의 가격을 미리 거래하는 상품)은 장이 열리기 전 분위기를 보는 참고 자료로만 보겠습니다.
오늘 기준은 방향 맞히기가 아니라, 제가 흔들릴 지점을 먼저 확인하는 것입니다.
""".strip()

    result = evaluator.evaluate(
        content=content,
        title="국장 전 ETF와 선물을 함께 보는 기준",
        keywords=["국장", "ETF", "선물"],
        topic_mode="finance",
        strategy=strategy,
    )

    assert result.plain_language_score >= 70


def test_plain_language_sentence_length_ignores_tables_and_lists():
    """표와 체크리스트는 산문 문장 길이 평가에서 제외한다."""

    evaluator = InsightQualityEvaluator()
    text = """
| 지표 | 현재 가격 | 변동률 | 출처 |
|---|---|---|---|
| SOXX 반도체 ETF | 602.72달러 | 미수집 | Stooq |
| BTC 비트코인 | 61,824달러 | -1.07% | CoinGecko |

- 첫 번째 체크 항목은 길어도 표처럼 훑어보는 항목입니다.
- 두 번째 체크 항목도 일반 문단처럼 이어 읽지 않습니다.

오늘은 짧게 보겠습니다. 저는 기준만 남겨두려 합니다.
""".strip()

    sentences = evaluator._split_sentences(text)

    assert all(not sentence.startswith("|") for sentence in sentences)
    assert all(not sentence.startswith("-") for sentence in sentences)
    assert max(len(sentence) for sentence in sentences) < 40
