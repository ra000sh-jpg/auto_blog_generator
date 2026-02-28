# Codex Blueprint: 글쓰기 엔진 V2 — 프롬프트 고도화 + 콜별 라우팅 + 3-전략 UI

## 목적
글쓰기 품질을 "인간 상위권 수준"으로 끌어올리면서, 비용을 콜별로 최적화하고, 사용자가 3단계 전략(가성비/균형/품질우선)을 한눈에 비교·선택할 수 있게 한다.

---

## 변경 범위 요약

| 파일 | 작업 |
|------|------|
| `modules/llm/prompts.py` | 5개 글쓰기 모듈 프롬프트 추가 |
| `modules/llm/content_generator.py` | Call A(설계도) + Call D(다듬기) 파이프라인 단계 추가 |
| `modules/llm/llm_router.py` | 3-전략 모드, 콜별 라우팅, TOKEN_BUDGET 확장, 모델 가격 업데이트 |
| `server/routers/router_settings.py` | "balanced" 전략 모드 지원 |
| `frontend/src/components/settings/engine-settings-card.tsx` | 3-카드 전략 선택기 + 월간 비용 |
| `frontend/src/lib/api.ts` | TS 타입 확장 |

---

## PART 1: 글쓰기 프롬프트 고도화 (5개 모듈)

### 파일: `modules/llm/prompts.py`

### 1-A. 모듈 1 — 사전 사고 프롬프트 (Pre-Writing Cognition)

`QUALITY_LAYER_SYSTEM_PROMPT` (line 385) 바로 아래에 새 상수 추가:

```python
# ============================================================================
# 고도화 모듈: 사전 사고 + 인지 깊이 + 감정 아키텍처 + 반AI + 문장 크래프트
# ============================================================================

PRE_WRITING_ANALYSIS_PROMPT = """
당신은 블로그 글의 전략 설계사입니다. 아래 주제에 대해 글을 쓰기 전 분석을 수행하세요.
반드시 JSON 형식으로만 응답하세요.

주제: {title}
키워드: {keywords}
카테고리: {category}

분석 항목:
1. reader_current_knowledge: 이 주제를 검색한 사람이 이미 알고 있을 것 (2-3문장)
2. reader_misconceptions: 가장 흔한 오해 1-2가지 (각 1문장)
3. reader_top_questions: 가장 궁금해할 질문 3가지 (리스트)
4. emotional_curve: 글의 감정 곡선 설계
   - opening_emotion: 도입부에서 유발할 감정 (예: "호기심+약간의 불안")
   - turning_point: 어디서 긴장/발견의 전환이 일어나는지
   - closing_emotion: 마무리에서 남길 여운
5. recommended_structure: H2 소제목 3-4개 초안과 각 섹션의 역할
   (예: "문제제기", "해결책", "심화", "실행")

JSON 출력 형식:
{{
  "reader_current_knowledge": "...",
  "reader_misconceptions": ["...", "..."],
  "reader_top_questions": ["...", "...", "..."],
  "emotional_curve": {{
    "opening_emotion": "...",
    "turning_point": "...",
    "closing_emotion": "..."
  }},
  "recommended_structure": [
    {{"h2": "...", "role": "..."}}
  ]
}}
""".strip()
```

### 1-B. 모듈 2 — 인지적 깊이 기법 (토픽별)

같은 파일에 추가:

```python
COGNITIVE_DEPTH_COMMON = """
[인지적 깊이 규칙 — 반드시 적용]

1. 2차 사고(Second-Order Thinking):
   "X가 좋다"에서 멈추지 말고 "X를 하면 → Y가 바뀌고 → 그러면 Z에 영향"까지 전개.
   최소 한 번은 "그래서 그 다음은?"을 적용할 것.

2. 대조 프레이밍(Contrast Framing):
   핵심 주장을 하기 전에 반대 관점을 먼저 제시하고 왜 다른지 설명.
   "일반적으로 ~라고 알려져 있지만, 실제로는..."

3. 구체성 사다리(Specificity Ladder):
   추상적 → 중간 → 아주 구체적으로 3단계를 오르내릴 것.
   나쁜 예: "많은 비용이 든다"
   좋은 예: "월 매출 300만 원인 카페에서 원두 단가 1kg당 2천 원 차이는 한 달에 6만 원, 1년이면 72만 원의 순이익 차이"
""".strip()

COGNITIVE_DEPTH_BY_TOPIC: Dict[str, str] = {
    "cafe": """[카페·요리 전용 기법: 감각 복원]
시각·후각·촉각 묘사를 최소 1회 포함하세요.
예: "크레마가 호두색으로 올라오는 3초", "잔을 감싼 손바닥에 전해지는 온기"
독자가 그 장면을 상상할 수 있도록 구체적 감각어를 사용하세요.""",

    "parenting": """[육아 전용 기법: 아이 시선 전환]
부모 관점 서술 중 최소 1회는 아이의 시선에서 같은 상황을 재묘사하세요.
예: "내가 훈육이라고 생각한 그 순간, 딸은 아마 '엄마가 왜 갑자기 화를 내지?'라고 느꼈을 것이다"
이 전환이 글에 깊이와 공감을 더합니다.""",

    "it": """[IT·생산성 전용 기법: 비유 브릿지]
기술 개념을 비기술자가 아는 일상 사물에 1:1 대응하세요.
예: "API는 식당의 주문서다. 손님(프론트)이 주문서에 쓰면 주방(서버)이 요리를 보내준다"
복잡한 개념일수록 비유를 먼저 제시하고, 그 다음에 정확한 설명을 추가하세요.""",

    "finance": """[재테크·경제 전용 기법: 역사 앵커링]
현재 경제 현상을 과거 유사 사례와 연결하세요.
예: "2008년에도 이런 신호가 있었다", "1997년 외환위기 직전에도 유사한 패턴이 관측됐다"
역사적 맥락이 독자에게 판단 기준을 제공합니다.""",
}
```

### 1-C. 모듈 3 — 감정 아키텍처

```python
EMOTIONAL_ARCHITECTURE_PROMPT = """
[감정 아키텍처 — 글 전체의 감정 곡선 설계]

아래 사전 분석에서 설계된 감정 곡선을 반드시 따르세요:
- 도입부 감정: {opening_emotion}
- 전환점: {turning_point}
- 마무리 감정: {closing_emotion}

적용 원칙:
- 도입부: 독자가 "나도 이것 잘못 알고 있었을까?" 하는 감정을 느끼도록
- 본문 전반: 문제의 복잡성을 드러내되, 해결의 실마리를 하나씩 제시 → "아, 그래서 그랬구나"
- 본문 후반: 구체적 실행 방법으로 불확실성 해소 → "나도 할 수 있겠다"
- 마무리: 깔끔하게 정리하되 한 가지 질문을 남길 것. 다 알려주지 말 것.
""".strip()
```

### 1-D. 모듈 4 — 반AI 패턴 규칙

```python
ANTI_AI_PATTERN_RULES = """
[반AI 패턴 규칙 — 아래 AI 글의 전형적 패턴을 피하세요]

1. 균일한 문단 에너지 금지:
   → 짧은 한 문장 문단(10자)과 긴 설명 문단(200자)을 의도적으로 섞으세요.
   → 특히 핵심 주장은 짧은 문장으로 단독 배치하세요.

2. 안전한 양비론 금지 ("A도 좋고 B도 좋다. 상황에 따라 다르다"):
   → 입장을 택하세요. "저는 A를 선택했고, 이유는..."
   → 틀릴 수 있음을 인정하되, 명확한 관점을 제시하세요.

3. 결론의 과잉 정리 금지 ("이상으로 ~에 대해 알아보았습니다"):
   → 결론에서 새로운 질문을 던지거나,
   → 도입부의 사례를 다시 불러와서 관점이 어떻게 바뀌었는지 보여주세요 (콜백 기법).

4. 나열식 구조 남용 금지 ("첫째... 둘째... 셋째..."):
   → 하나의 사례를 깊게 파고든 뒤 원칙을 도출하는 방식도 사용하세요.
   → 나열은 3개 이하일 때만, 4개 이상이면 스토리로 풀어주세요.

5. 감정 없는 서술 금지 (팩트 → 팩트 → 팩트):
   → 팩트 사이에 "솔직히 이 숫자를 보고 놀랐다" 같은
   → 필자의 감정 반응을 1-2회 삽입하세요.
""".strip()
```

### 1-E. 모듈 5 — 문장 크래프트 체크리스트

```python
SENTENCE_CRAFT_CHECKLIST = """
[문장 다듬기 체크리스트 — 아래 규칙을 적용하여 원문을 다듬으세요]

원칙: 정보, 구조(H2), 문단 수, URL, 수치는 절대 변경 금지. 문장 표현만 다듬을 것.

1. End-Weight(문장 끝 힘 실기):
   나쁜: "비용 절감이 가능하다는 것이 이 방법의 가장 큰 장점입니다"
   좋은: "이 방법의 가장 큰 장점은, 비용을 절반으로 줄인다는 것입니다"
   → 문장의 마지막 단어가 가장 기억에 남도록 재배치하세요.

2. 구체적 동사 > 형용사 + 일반 동사:
   나쁜: "매출이 크게 상승했다"
   좋은: "매출이 전월 대비 23% 뛰었다"
   → 형용사를 삭제하고 동사와 수치로 대체 가능한 곳을 수정하세요.

3. 콜백(Callback) 기법:
   → 도입부에서 언급한 구체적 이미지/사례가 마무리에서 다시 등장하는지 확인하세요.
   → 없으면 마무리를 수정하여 도입부의 핵심 이미지를 회수하세요.

4. 전략적 짧은 문장:
   → 설명이 3문장 이상 이어진 뒤에 의도적으로 5어절 이내 문장을 배치하세요.
   → 예: "결국 핵심은 하나다." / "그래서 바꿨다." / "효과는 즉각적이었다."

5. Show Don't Tell:
   나쁜: "이 방법은 매우 효율적입니다"
   좋은: "월요일에 적용했더니, 금요일에는 작업 시간이 2시간 줄었다"
   → "효율적", "좋다", "편리하다" 같은 판단어를 증거로 교체 가능한 곳을 수정하세요.

6. 반AI 패턴 최종 감사:
   → 연속 3문단 이상 비슷한 길이면 → 하나를 짧게 또는 길게 변형
   → "이상으로~알아보았습니다" → 콜백 또는 새 질문으로 교체
   → 양비론 결론 → 명확한 입장으로 수정

[원문]
{content}

[출력]
- 다듬어진 Markdown 본문만 출력하세요. 설명 없이.
""".strip()
```

---

## PART 2: 파이프라인 확장 (Call A + Call D)

### 파일: `modules/llm/content_generator.py`

### 2-A. imports 추가

line 31~49의 import 블록에 새 프롬프트 상수 추가:

```python
from .prompts import (
    # 기존 imports 유지...
    ECONOMY_SYSTEM_PROMPT,
    ECONOMY_TOPIC_PROMPT,
    FACT_CHECK_REQUEST,
    FACT_CHECK_REVISION,
    IMAGE_PROMPT_GENERATION,
    OUTLINE_GENERATION,
    QUALITY_LAYER_CONTENT_REQUEST,
    QUALITY_LAYER_ECONOMY_PROMPT,
    QUALITY_LAYER_SYSTEM_PROMPT,
    QUALITY_CHECK,
    REWRITE_REQUEST,
    SECTION_DRAFT,
    SECTION_INTEGRATION,
    SEO_OPTIMIZATION,
    SYSTEM_BLOG_WRITER,
    USER_CONTENT_REQUEST,
    VOICE_REWRITE_REQUEST,
    get_persona_profile,
    get_tone_profile,
    # 신규 추가:
    PRE_WRITING_ANALYSIS_PROMPT,
    COGNITIVE_DEPTH_COMMON,
    COGNITIVE_DEPTH_BY_TOPIC,
    EMOTIONAL_ARCHITECTURE_PROMPT,
    ANTI_AI_PATTERN_RULES,
    SENTENCE_CRAFT_CHECKLIST,
)
```

### 2-B. Call A — 사전 분석 메서드 추가

`ContentGenerator` 클래스에 새 메서드 추가 (generate 메서드 위에 위치):

```python
async def _run_pre_writing_analysis(
    self,
    job: Job,
    topic_mode: str,
    *,
    token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Call A: 사전 사고 분석 (저가 모델 사용).

    글을 쓰기 전에 독자 분석, 감정 곡선, 구조 설계를 수행한다.
    실패해도 파이프라인을 중단하지 않고 빈 dict를 반환한다.
    """
    user_prompt = PRE_WRITING_ANALYSIS_PROMPT.format(
        title=job.title,
        keywords=", ".join(job.seed_keywords),
        category=topic_mode,
    )
    system_prompt = "당신은 블로그 글의 전략 설계사입니다. 반드시 유효한 JSON으로만 응답하세요."

    try:
        response = await self._call_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.3,
            max_tokens=1200,
            role="pre_analysis",
            token_usage=token_usage,
        )
        raw = response.content.strip()
        # JSON 블록 추출 (```json ... ``` 또는 순수 JSON)
        if "```" in raw:
            json_match = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
            if json_match:
                raw = json_match.group(1).strip()
        return json.loads(raw)
    except Exception as exc:
        self.logger.warning("Call A (pre_writing_analysis) 실패, 기본값으로 진행: %s", exc)
        return {}
```

**중요**: `_call_llm` 호출 시 `role="pre_analysis"` 파라미터로 저가 모델이 사용되도록 라우터에서 분기한다 (PART 3에서 구현).

### 2-C. Call D — 최종 다듬기 메서드 추가

```python
async def _run_sentence_polish(
    self,
    content: str,
    *,
    token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """Call D: 문장 크래프트 최종 다듬기 (저가 모델 사용).

    완성된 글의 문장 표현만 다듬는다. 정보/구조는 변경하지 않는다.
    실패 시 원문을 그대로 반환한다.
    """
    user_prompt = SENTENCE_CRAFT_CHECKLIST.format(content=content)
    system_prompt = (
        "당신은 한국어 편집 전문가입니다. "
        "원문의 정보, H2 구조, 문단 수, URL, 수치는 절대 변경하지 마세요. "
        "문장 표현과 리듬만 다듬으세요."
    )

    try:
        response = await self._call_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.3,
            max_tokens=4000,
            role="sentence_polish",
            token_usage=token_usage,
        )
        polished = response.content.strip()

        # 안전 검증: H2 개수가 동일한지 확인
        original_h2_count = content.count("## ")
        polished_h2_count = polished.count("## ")
        if polished_h2_count != original_h2_count:
            self.logger.warning(
                "Call D: H2 개수 불일치 (원본 %d, 다듬기 %d) → 원문 유지",
                original_h2_count, polished_h2_count,
            )
            return content

        # 길이 검증: ±15% 이내
        if abs(len(polished) - len(content)) / max(len(content), 1) > 0.15:
            self.logger.warning("Call D: 길이 변화 15% 초과 → 원문 유지")
            return content

        return polished
    except Exception as exc:
        self.logger.warning("Call D (sentence_polish) 실패, 원문 유지: %s", exc)
        return content
```

### 2-D. generate() 메서드에 Call A, Call D 통합

`generate()` 메서드 (line 168~) 수정:

```python
async def generate(
    self,
    job: Job,
    tone: Optional[str] = None,
    persona_id: Optional[str] = None,
) -> ContentResult:
    """전체 생성 파이프라인 실행."""
    # ... 기존 초기화 코드 유지 ...

    # ── Call A: 사전 사고 분석 (저가 모델) ──
    pre_analysis = await self._run_pre_writing_analysis(
        job=job,
        topic_mode=topic_mode,
        token_usage=token_usage,
    )
    if pre_analysis:
        llm_calls += 1

    # ... 기존 폴백 체인 구성 코드 유지 ...

    # 기존 _generate_draft_single() / _generate_draft_multistep() 호출 시
    # pre_analysis를 파라미터로 전달

    # ... 기존 SEO, 팩트체크, 품질검증, 보이스 리라이트 코드 유지 ...

    # ── Call D: 최종 다듬기 (저가 모델) ──
    # Voice Rewrite 이후, 최종 반환 직전에 실행
    if final_content and len(final_content) > 100:
        final_content = await self._run_sentence_polish(
            content=final_content,
            token_usage=token_usage,
        )
        llm_calls += 1

    # ... 기존 ContentResult 반환 코드 유지 ...
```

### 2-E. Quality Layer에 모듈 2, 3 주입

`_generate_draft_single()` 메서드 (line ~1038) 수정.

quality_only=True일 때 시스템 프롬프트에 인지 깊이 + 감정 아키텍처 추가:

```python
# quality_only 분기 안에서:

# 모듈 2: 인지적 깊이 기법 주입
depth_common = COGNITIVE_DEPTH_COMMON
depth_topic = COGNITIVE_DEPTH_BY_TOPIC.get(topic_mode, "")
cognitive_injection = f"\n\n{depth_common}"
if depth_topic:
    cognitive_injection += f"\n\n{depth_topic}"

# 모듈 3: 감정 아키텍처 주입 (pre_analysis가 있을 때)
emotional_injection = ""
if pre_analysis and pre_analysis.get("emotional_curve"):
    ec = pre_analysis["emotional_curve"]
    emotional_injection = "\n\n" + EMOTIONAL_ARCHITECTURE_PROMPT.format(
        opening_emotion=ec.get("opening_emotion", "호기심"),
        turning_point=ec.get("turning_point", "본문 중반에서 핵심 발견"),
        closing_emotion=ec.get("closing_emotion", "실행 의지와 여운"),
    )

# 모듈 1 심화: pre_analysis 결과를 컨텍스트로 주입
pre_analysis_injection = ""
if pre_analysis:
    reader_knowledge = pre_analysis.get("reader_current_knowledge", "")
    misconceptions = pre_analysis.get("reader_misconceptions", [])
    questions = pre_analysis.get("reader_top_questions", [])
    structure = pre_analysis.get("recommended_structure", [])

    pre_analysis_injection = f"""

[사전 분석 결과 — 이 내용을 글의 방향에 반영하세요]
독자의 현재 지식: {reader_knowledge}
흔한 오해: {', '.join(misconceptions) if misconceptions else '없음'}
독자의 궁금증: {', '.join(questions) if questions else '없음'}
추천 구조: {json.dumps(structure, ensure_ascii=False) if structure else '자유 구성'}

추가 지시:
- 이 주제에서 대부분이 믿지만 실제로는 다른 것을 최소 1개 언급하세요.
- 전혀 다른 분야의 원리와 연결점을 1개 이상 제시하세요.
"""

# 시스템 프롬프트 조합
system_prompt = QUALITY_LAYER_SYSTEM_PROMPT + cognitive_injection + emotional_injection + pre_analysis_injection
```

### 2-F. Voice Layer에 모듈 4 주입

Voice Rewrite 호출 시 (기존 VOICE_REWRITE_REQUEST 사용 부분):

```python
# 기존 voice_system_prompt에 반AI 패턴 규칙 추가
voice_system_prompt = f"{SYSTEM_BLOG_WRITER}{voice_injection}\n\n{ANTI_AI_PATTERN_RULES}"
```

### 2-G. `_generate_draft_single`에 `pre_analysis` 파라미터 추가

```python
async def _generate_draft_single(
    self,
    job: Job,
    persona: Any,
    tone_profile: Any,
    topic_mode: str,
    news_context: Optional[List[Dict[str, str]]] = None,
    quality_only: bool = False,
    token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    pre_analysis: Optional[Dict[str, Any]] = None,  # 신규 추가
) -> Tuple[str, str]:
```

모든 `_generate_draft_single()` 호출부에 `pre_analysis=pre_analysis` 전달.

---

## PART 3: 콜별 라우팅 + 3-전략 모드

### 파일: `modules/llm/llm_router.py`

### 3-A. TOKEN_BUDGET 확장

line 21~28 교체:

```python
TOKEN_BUDGET = {
    "parser": {"input": 450, "output": 180},
    "pre_analysis": {"input": 600, "output": 1000},        # Call A: 사전 분석 (저가)
    "quality_step": {"input": 5500, "output": 2800},        # Call B: 본문 (모듈1,2,3 지침 증가)
    "voice_step": {"input": 4200, "output": 2500},          # Call C: 음성 (모듈4 규칙 증가)
    "self_critique": {"input": 2400, "output": 800},
    "seo_step": {"input": 1200, "output": 600},
    "image_prompt": {"input": 800, "output": 400},
    "sentence_polish": {"input": 4500, "output": 3000},    # Call D: 다듬기 (저가)
}
```

### 3-B. 모델 가격 업데이트

TEXT_MODEL_MATRIX의 가격을 최신 실제 가격으로 수정:

```python
TEXT_MODEL_MATRIX: List[TextModelSpec] = [
    TextModelSpec(
        provider="qwen", model="qwen-plus", label="Qwen Plus",
        key_id="qwen",
        input_cost_per_1m_usd=0.28, output_cost_per_1m_usd=0.84,
        quality_score=84, speed_score=90,
    ),
    TextModelSpec(
        provider="deepseek", model="deepseek-chat", label="DeepSeek V3",
        key_id="deepseek",
        input_cost_per_1m_usd=0.28, output_cost_per_1m_usd=0.42,   # 구 1.10 → 실제 0.42
        quality_score=86, speed_score=88,
    ),
    TextModelSpec(
        provider="gemini", model="gemini-2.0-flash", label="Gemini 2.0 Flash",
        key_id="gemini",
        input_cost_per_1m_usd=0.10, output_cost_per_1m_usd=0.40,   # 구 0.35/1.05 → 실제 0.10/0.40
        quality_score=90, speed_score=93,
    ),
    TextModelSpec(
        provider="openai", model="gpt-4.1-mini", label="OpenAI GPT-4.1 mini",
        key_id="openai",
        input_cost_per_1m_usd=0.40, output_cost_per_1m_usd=1.60,
        quality_score=92, speed_score=89,
    ),
    TextModelSpec(
        provider="claude", model="claude-sonnet-4-20250514", label="Claude Sonnet 4",
        key_id="claude",
        input_cost_per_1m_usd=3.00, output_cost_per_1m_usd=15.00,
        quality_score=97, speed_score=83,
    ),
    TextModelSpec(
        provider="groq", model="llama-3.3-70b-versatile", label="Groq Llama-3.3 70B (무료)",
        key_id="groq",
        input_cost_per_1m_usd=0.0, output_cost_per_1m_usd=0.0,
        quality_score=80, speed_score=95,
    ),
    TextModelSpec(
        provider="cerebras", model="llama3.1-8b", label="Cerebras Llama3.1 8B (무료)",
        key_id="cerebras",
        input_cost_per_1m_usd=0.0, output_cost_per_1m_usd=0.0,
        quality_score=76, speed_score=97,
    ),
]
```

### 3-C. `_pick_role_model` — 3-전략 모드 + 콜별 분기

line ~1118의 `_pick_role_model` 메서드 교체:

```python
# 저가 역할 (Call A, Call D, parser)
_CHEAP_ROLES = {"parser", "pre_analysis", "sentence_polish"}

# 역할별 최소 품질 기준
_ROLE_MIN_QUALITY = {
    "parser": 75,
    "pre_analysis": 75,      # 구조 분석만 하므로 낮아도 됨
    "quality_step": 82,
    "voice_step": 80,
    "sentence_polish": 78,   # 규칙 기반 편집이므로 중간
}

def _pick_role_model(
    self,
    candidates: List[TextModelSpec],
    strategy_mode: str,
    role: str,
) -> Optional[TextModelSpec]:
    """역할별 우선순위로 모델을 선택한다.

    3-전략 모드 지원:
    - "cost": 저가 역할 → 무료 우선, 핵심 역할 → 비용 대비 품질 최적
    - "balanced": 저가 역할 → 무료/저가 우선, 핵심 역할 → 비용·품질 균형
    - "quality": 저가 역할 → 속도 우선 저가, 핵심 역할 → 최고 품질
    """
    if not candidates:
        return None

    threshold = _ROLE_MIN_QUALITY.get(role, 75)
    is_cheap_role = role in _CHEAP_ROLES

    by_cost = sorted(candidates, key=lambda s: (s.avg_cost_per_1k_usd, -s.quality_score))
    by_quality = sorted(candidates, key=lambda s: (-s.quality_score, s.avg_cost_per_1k_usd))
    by_speed = sorted(candidates, key=lambda s: (-s.speed_score, s.avg_cost_per_1k_usd))

    # 저가 역할은 전략과 무관하게 항상 최저비용 (무료 우선)
    if is_cheap_role:
        free_candidates = [
            s for s in candidates
            if s.input_cost_per_1m_usd == 0.0 and s.output_cost_per_1m_usd == 0.0
            and s.quality_score >= threshold
        ]
        if free_candidates:
            return sorted(free_candidates, key=lambda s: -s.speed_score)[0]
        # 무료 없으면 최저가
        for s in by_cost:
            if s.quality_score >= threshold:
                return s
        return by_cost[0]

    # 핵심 역할 (quality_step, voice_step)
    if strategy_mode == "quality":
        return by_quality[0]

    if strategy_mode == "balanced":
        # 품질 80th percentile 이상 중 가장 저렴한 모델
        quality_values = sorted([s.quality_score for s in candidates])
        p80 = quality_values[max(0, int(len(quality_values) * 0.8) - 1)] if quality_values else 0
        balanced_candidates = [s for s in candidates if s.quality_score >= p80]
        if balanced_candidates:
            return sorted(balanced_candidates, key=lambda s: s.avg_cost_per_1k_usd)[0]
        return by_quality[0]

    # cost 전략: 최소 품질 충족하는 최저가
    for s in by_cost:
        if s.quality_score >= threshold:
            return s
    return by_cost[0]
```

### 3-D. 견적 계산에 신규 역할 반영

`_compute_estimate()` 메서드 (line ~1200)에서 추가 단계 비용 계산:

```python
# 기존 코드의 extra_step_cost 루프에 신규 역할 추가:
# pre_analysis와 sentence_polish는 parser_spec (저가 모델)으로 비용 계산

def extra_cheap_cost(role: str) -> float:
    """저가 역할의 비용을 parser_spec 모델로 계산한다."""
    if not parser_spec:
        return 0.0
    budget = TOKEN_BUDGET.get(role)
    if not budget:
        return 0.0
    input_c = (budget["input"] / 1_000_000.0) * parser_spec.input_cost_per_1m_usd
    output_c = (budget["output"] / 1_000_000.0) * parser_spec.output_cost_per_1m_usd
    return (input_c + output_c) * USD_TO_KRW

pre_analysis_cost = extra_cheap_cost("pre_analysis")
sentence_polish_cost = extra_cheap_cost("sentence_polish")

text_cost = (
    parser_cost + quality_cost + voice_cost
    + self_critique_cost + seo_cost + image_prompt_cost
    + pre_analysis_cost + sentence_polish_cost
)
```

### 3-E. 견적에 월간 비용 필드 추가

`_compute_estimate` 반환 dict에 추가:

```python
# 스케줄러에서 하루 편수 가져오기
daily_posts = 8  # 기본값
try:
    raw_alloc = self.job_store.get_system_setting("scheduler_category_allocations", "[]")
    alloc_list = json.loads(raw_alloc) if raw_alloc else []
    if isinstance(alloc_list, list):
        daily_posts = max(1, sum(int(a.get("count", 0)) for a in alloc_list))
except Exception:
    pass

monthly_cost_krw = total_cost * daily_posts * 30
monthly_cost_min_krw = cost_min * daily_posts * 30
monthly_cost_max_krw = cost_max * daily_posts * 30

return {
    # 기존 필드 유지...
    "text_cost_krw": round(text_cost, 1),
    "image_cost_krw": round(image_cost, 1),
    "total_cost_krw": round(total_cost, 1),
    "cost_min_krw": round(cost_min, 1),
    "cost_max_krw": round(cost_max, 1),
    "quality_score": quality_score,
    # 신규 필드:
    "daily_posts": daily_posts,
    "monthly_cost_krw": round(monthly_cost_krw, 0),
    "monthly_cost_min_krw": round(monthly_cost_min_krw, 0),
    "monthly_cost_max_krw": round(monthly_cost_max_krw, 0),
}
```

### 3-F. 파이프라인 배정 정보에 신규 역할 추가

`export_for_ui()` 메서드에서 roles에 신규 역할 포함:

```python
roles = {
    "parser": role_to_runtime(parser_role),
    "pre_analysis": role_to_runtime(parser_role),     # parser와 같은 저가 모델
    "quality_step": role_to_runtime(quality_role),
    "voice_step": role_to_runtime(voice_role),
    "sentence_polish": role_to_runtime(parser_role),  # parser와 같은 저가 모델
}
```

---

## PART 4: 3-카드 전략 선택 UI

### 파일: `frontend/src/lib/api.ts`

### 4-A. 타입 확장

`RouterQuoteResponse` (line 387) 수정:

```typescript
export type RouterQuoteResponse = {
  strategy_mode: string;
  roles: Record<string, Record<string, unknown>>;
  estimate: {
    currency: string;
    text_cost_krw: number;
    image_cost_krw: number;
    total_cost_krw: number;
    cost_min_krw: number;
    cost_max_krw: number;
    quality_score: number;
    // 신규 필드
    daily_posts: number;
    monthly_cost_krw: number;
    monthly_cost_min_krw: number;
    monthly_cost_max_krw: number;
  };
  image: Record<string, unknown>;
  available_text_models: Array<Record<string, unknown>>;
};
```

### 4-B. `RouterSettingsPayload`에 strategy_mode 값 확장

strategy_mode 필드를 기존 `"cost" | "quality"`에서 `"cost" | "balanced" | "quality"`로 확장.
TypeScript에서는 이미 `string` 타입이므로 타입 변경 불필요. 상수만 추가:

```typescript
export const STRATEGY_MODES = ["cost", "balanced", "quality"] as const;
export type StrategyMode = (typeof STRATEGY_MODES)[number];
```

### 파일: `frontend/src/components/settings/engine-settings-card.tsx`

### 4-C. 상태 타입 변경

line 29:

```typescript
// 기존:
// const [strategyMode, setStrategyMode] = useState<"cost" | "quality">(...)

// 변경:
const [strategyMode, setStrategyMode] = useState<"cost" | "balanced" | "quality">(
    initialRouterSettings.settings.strategy_mode === "quality"
        ? "quality"
        : initialRouterSettings.settings.strategy_mode === "balanced"
        ? "balanced"
        : "cost"
);
```

### 4-D. 3개 전략 미리보기 상태 추가

```typescript
// 3개 전략 각각의 견적을 미리 계산해서 카드에 표시
const [strategyPreviews, setStrategyPreviews] = useState<
    Record<"cost" | "balanced" | "quality", {
        total_cost_krw: number;
        monthly_cost_krw: number;
        quality_score: number;
        main_model_label: string;
        cheap_model_label: string;
    } | null>
>({ cost: null, balanced: null, quality: null });
```

### 4-E. 3개 전략 미리보기 견적 호출

기존 useEffect (line 145~171) 교체:

```typescript
useEffect(() => {
    const timer = setTimeout(async () => {
        setRouterLoading(true);
        try {
            const basePayload = {
                text_api_keys: compactKeys(textApiKeys),
                image_api_keys: compactKeys(imageApiKeys),
                image_engine: imageEngine,
                image_ai_engine: imageAiEngine,
                image_ai_quota: imageAiQuota,
                image_topic_quota_overrides: imageTopicQuotaOverrides,
                traffic_feedback_strong_mode: trafficFeedbackStrongMode,
                image_enabled: imageEnabled,
                images_per_post: imagesPerPostMax,
                images_per_post_min: imagesPerPostMin,
                images_per_post_max: imagesPerPostMax,
            };

            // 3개 전략을 병렬로 견적 요청
            const [costQ, balancedQ, qualityQ] = await Promise.all([
                quoteRouterSettings({ ...basePayload, strategy_mode: "cost" }),
                quoteRouterSettings({ ...basePayload, strategy_mode: "balanced" }),
                quoteRouterSettings({ ...basePayload, strategy_mode: "quality" }),
            ]);

            const extractPreview = (q: RouterQuoteResponse) => ({
                total_cost_krw: q.estimate.total_cost_krw,
                monthly_cost_krw: q.estimate.monthly_cost_krw || q.estimate.total_cost_krw * (q.estimate.daily_posts || 8) * 30,
                quality_score: q.estimate.quality_score,
                main_model_label: String((q.roles?.quality_step as Record<string, unknown>)?.label || "-"),
                cheap_model_label: String((q.roles?.pre_analysis as Record<string, unknown>)?.label || "-"),
            });

            setStrategyPreviews({
                cost: extractPreview(costQ),
                balanced: extractPreview(balancedQ),
                quality: extractPreview(qualityQ),
            });

            // 현재 선택된 전략의 견적을 메인 quote로 설정
            const currentQuote = strategyMode === "quality" ? qualityQ
                : strategyMode === "balanced" ? balancedQ
                : costQ;
            setRouterQuote(currentQuote);
        } catch {
            // 미리보기 실패는 저장 동작을 막지 않는다.
        } finally {
            setRouterLoading(false);
        }
    }, 350);
    return () => clearTimeout(timer);
}, [strategyMode, textApiKeys, imageApiKeys, imageEngine, imageAiEngine, imageAiQuota, imageTopicQuotaOverrides, trafficFeedbackStrongMode, imageEnabled, imagesPerPostMin, imagesPerPostMax]);
```

### 4-F. 2버튼 필 토글 → 3-카드 선택기 교체

line 275~296 교체:

```tsx
{/* 전략 선택: 3-카드 */}
<div className="mb-4">
    <p className="mb-2 text-sm font-semibold text-slate-700">전략 선택</p>
    <div className="grid grid-cols-3 gap-3">
        {([
            { key: "cost" as const, icon: "💰", label: "가성비", desc: "최저 비용" },
            { key: "balanced" as const, icon: "⚖️", label: "균형", desc: "비용·품질 밸런스" },
            { key: "quality" as const, icon: "💎", label: "품질우선", desc: "최고 품질" },
        ]).map(({ key, icon, label, desc }) => {
            const preview = strategyPreviews[key];
            const isSelected = strategyMode === key;
            return (
                <button
                    key={key}
                    type="button"
                    onClick={() => setStrategyMode(key)}
                    className={`relative rounded-xl border-2 p-4 text-left transition ${
                        isSelected
                            ? "border-indigo-500 bg-indigo-50 shadow-md"
                            : "border-slate-200 hover:border-slate-300"
                    }`}
                >
                    {isSelected && (
                        <span className="absolute right-2 top-2 flex h-5 w-5 items-center justify-center rounded-full bg-indigo-500 text-[10px] text-white">✓</span>
                    )}
                    <div className="text-lg">{icon}</div>
                    <p className={`mt-1 text-sm font-bold ${isSelected ? "text-indigo-700" : "text-slate-800"}`}>
                        {label}
                    </p>
                    <p className="text-[11px] text-slate-500">{desc}</p>
                    {preview ? (
                        <div className="mt-3 space-y-1 border-t border-slate-100 pt-2">
                            <p className="text-xs text-slate-600">
                                ~{formatKrw(preview.total_cost_krw)}원<span className="text-slate-400">/편</span>
                            </p>
                            <p className="text-xs font-semibold text-slate-800">
                                월 {formatKrw(preview.monthly_cost_krw)}원
                            </p>
                            <p className="text-[11px] text-slate-500">
                                품질 {preview.quality_score}점
                            </p>
                            <p className="mt-1 text-[10px] text-slate-400">
                                본문: {preview.main_model_label}
                            </p>
                            <p className="text-[10px] text-slate-400">
                                보조: {preview.cheap_model_label}
                            </p>
                        </div>
                    ) : routerLoading ? (
                        <p className="mt-3 text-[11px] text-slate-400">계산 중...</p>
                    ) : null}
                </button>
            );
        })}
    </div>
    <p className="mt-2 text-[11px] text-slate-400">
        💡 보조 단계(설계도·다듬기)는 무료/저가 모델을 자동 사용합니다. 월 비용은 하루 {strategyPreviews.cost?.monthly_cost_krw ? `${Math.round((strategyPreviews.cost.monthly_cost_krw) / 30 / (strategyPreviews.cost.total_cost_krw || 1))}` : "8"}편 × 30일 기준.
    </p>
</div>
```

### 4-G. 견적서에 월간 비용 추가

line 417~438 수정. 기존 2칸 그리드에 월간 비용 카드 추가:

```tsx
<div className="mt-3 grid gap-3 text-sm sm:grid-cols-3">
    <div className="rounded-lg bg-slate-50 p-3">
        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">포스팅당 원가</p>
        <p className="mt-1 text-lg font-bold text-slate-900">
            {formatKrw(routerQuote?.estimate.cost_min_krw || 0)}원
            {" ~ "}
            {formatKrw(routerQuote?.estimate.cost_max_krw || 0)}원
        </p>
    </div>
    <div className="rounded-lg bg-indigo-50 p-3">
        <p className="text-xs font-semibold uppercase tracking-wide text-indigo-600">월간 예상 비용</p>
        <p className="mt-1 text-lg font-bold text-indigo-900">
            {formatKrw(routerQuote?.estimate.monthly_cost_min_krw || 0)}원
            {" ~ "}
            {formatKrw(routerQuote?.estimate.monthly_cost_max_krw || 0)}원
        </p>
        <p className="mt-0.5 text-xs text-indigo-500">
            하루 {routerQuote?.estimate.daily_posts || 8}편 × 30일
        </p>
    </div>
    <div className="rounded-lg bg-slate-50 p-3">
        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">예상 품질</p>
        <p className="mt-1 text-lg font-bold text-slate-900">
            {routerQuote?.estimate.quality_score || 0}점
        </p>
    </div>
</div>
```

### 4-H. 파이프라인 배정 8단계로 확장

line 444~465 수정:

```tsx
{/* 파이프라인 단계 breakdown */}
<div className="mt-3 border-t border-slate-100 pt-3">
    <p className="mb-2 text-xs font-semibold text-slate-500">전체 파이프라인 배정 (8단계)</p>
    <div className="flex flex-wrap gap-1.5 text-xs">
        {[
            { step: "① 설계도", roleKey: "pre_analysis", color: "bg-teal-50 text-teal-700" },
            { step: "② 문맥분석", roleKey: "parser", color: "bg-teal-50 text-teal-700" },
            { step: "③ 품질작성", roleKey: "quality_step", color: "bg-violet-50 text-violet-700" },
            { step: "④ 자기검증", roleKey: "quality_step", color: "bg-violet-50 text-violet-700" },
            { step: "⑤ SEO최적화", roleKey: "quality_step", color: "bg-violet-50 text-violet-700" },
            { step: "⑥ 이미지슬롯", roleKey: "quality_step", color: "bg-violet-50 text-violet-700" },
            { step: "⑦ 음성교정", roleKey: "voice_step", color: "bg-emerald-50 text-emerald-700" },
            { step: "⑧ 최종다듬기", roleKey: "sentence_polish", color: "bg-teal-50 text-teal-700" },
        ].map(({ step, roleKey }) => {
            const role = routerQuote?.roles?.[roleKey];
            const label = typeof role === "object" && role
                ? String((role as Record<string, unknown>).label || "-")
                : "-";
            return (
                <span
                    key={step}
                    className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-slate-600"
                >
                    <span className="font-medium">{step}</span>
                    <span className="ml-1 text-slate-400">→ {label}</span>
                </span>
            );
        })}
    </div>
    <p className="mt-2 text-[11px] text-slate-400">
        ①② 설계·분석과 ⑧ 다듬기는 무료/저가 모델을 자동 사용합니다.
    </p>
</div>
```

### 4-I. handleSaveRouterSettings에서 balanced 처리

line 200:

```typescript
// 기존:
// setStrategyMode(saved.settings.strategy_mode === "quality" ? "quality" : "cost");

// 변경:
setStrategyMode(
    saved.settings.strategy_mode === "quality" ? "quality"
    : saved.settings.strategy_mode === "balanced" ? "balanced"
    : "cost"
);
```

---

## PART 5: 백엔드 — balanced 전략 모드 지원

### 파일: `server/routers/router_settings.py`

변경 없음. `strategy_mode`는 이미 `str` 타입이므로 "balanced" 값을 그대로 전달/저장 가능.

### 파일: `modules/llm/llm_router.py`

`normalize_settings()` 또는 설정 저장 부분에서 strategy_mode 검증 로직이 있다면 "balanced" 추가:

```python
# strategy_mode 정규화 부분에서:
VALID_STRATEGY_MODES = {"cost", "balanced", "quality"}

strategy_mode = str(raw.get("strategy_mode", "cost")).strip().lower()
if strategy_mode not in VALID_STRATEGY_MODES:
    strategy_mode = "cost"
```

---

## PART 6: content_generator.py의 _call_llm에서 role 기반 모델 분기

### 핵심: `_call_llm`에 role 파라미터로 저가/프리미엄 분기

현재 `_call_llm`이 모델 선택을 어떻게 하는지에 따라 구현 방식이 달라짐.

**방안 A (권장)**: `_call_llm`에서 `role` 파라미터를 받아 라우터에서 해당 role에 배정된 모델 사용.

```python
async def _call_llm(
    self,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.7,
    max_tokens: int = 4000,
    role: str = "quality_step",      # 신규: 역할별 모델 분기
    token_usage: Optional[Dict] = None,
) -> LLMResponse:
    """role에 따라 다른 모델 클라이언트를 선택한다."""

    _CHEAP_ROLES = {"parser", "pre_analysis", "sentence_polish"}

    if role in _CHEAP_ROLES and self.parser_client:
        # parser_client = 저가 모델 전용 클라이언트
        client = self.parser_client
    else:
        client = self.primary

    # ... 기존 호출 로직 ...
```

**방안 B**: pipeline_service에서 역할별로 다른 클라이언트를 주입. 이 경우 content_generator 생성 시 `cheap_client`를 별도로 전달.

어느 방안이든 핵심은: **pre_analysis와 sentence_polish 호출 시 parser_client(저가 모델)를 사용하는 것**.

현재 코드 구조에서 `self.primary`와 별도로 `self.parser_client`가 이미 존재한다면 방안 A로 진행. 존재하지 않는다면 `ContentGenerator.__init__`에 `parser_client` 파라미터를 추가하고, `pipeline_service.py`에서 라우터가 배정한 parser 모델로 클라이언트를 생성하여 주입.

---

## 테스트 요구사항

1. **프롬프트 단위 테스트**: PRE_WRITING_ANALYSIS_PROMPT에 임의 주제를 넣어 JSON 출력 파싱 검증
2. **Call A 실패 내성**: `_run_pre_writing_analysis`가 실패해도 generate()가 정상 완료되는지
3. **Call D 안전 검증**: H2 개수 불일치 시 원문 유지, 길이 15% 초과 시 원문 유지
4. **3-전략 모드**: cost/balanced/quality 각각 다른 모델이 배정되는지 검증
5. **가격 업데이트 검증**: DeepSeek output $0.42, Gemini Flash $0.10/$0.40 반영 확인
6. **월간 비용 계산**: daily_posts × 30 × cost_per_post 정확성 검증
7. **UI 3-카드**: 3개 전략의 견적이 병렬 호출되어 각 카드에 표시되는지
8. **하위호환**: 기존 "cost"/"quality" 설정이 마이그레이션 없이 동작하는지 (balanced가 없으면 cost로 폴백)

---

## 구현 순서 (의존성 기반)

```
Step 1: prompts.py — 5개 모듈 프롬프트 상수 추가
Step 2: llm_router.py — TOKEN_BUDGET, 가격 업데이트, 3-전략 _pick_role_model, 월간 비용
Step 3: content_generator.py — Call A/D 메서드 + generate() 통합 + 모듈 주입
Step 4: router_settings.py — balanced 검증 (이미 str이므로 최소 변경)
Step 5: api.ts — TS 타입 확장
Step 6: engine-settings-card.tsx — 3-카드 UI + 월간 비용
Step 7: 통합 테스트
```

---

## 리스크 주의사항

| # | 리스크 | 대응 |
|---|--------|------|
| P0 | Call A의 JSON 파싱 실패 → 파이프라인 중단 | try/except로 빈 dict 반환, 기존 흐름 유지 |
| P0 | Call D가 원문 구조 훼손 | H2 개수 + 길이 ±15% 검증, 위반 시 원문 유지 |
| P0 | 프롬프트 토큰 폭증 → 모델 컨텍스트 초과 | TOKEN_BUDGET을 보수적으로 설정, 모듈 주입 시 총 토큰 확인 |
| P1 | 3-전략 병렬 견적 API → 서버 부하 | debounce 350ms로 제한, 동시 3콜은 경량 연산이므로 OK |
| P1 | balanced 모드에서 기대와 다른 모델 선택 | p80 로직의 후보군이 1개뿐일 수 있음 → 3개 미만이면 by_quality[0] 폴백 |
| P2 | 기존 설정 "cost"/"quality"만 저장된 사용자 | "balanced" 없으면 "cost"로 폴백, 마이그레이션 불필요 |
