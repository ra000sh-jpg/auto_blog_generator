# V2.1 Patch: 코드 리뷰 피드백 6건 반영 보정

이 문서는 `CODEX_BLUEPRINT_WRITING_ENGINE_V2.md`의 **보정 패치**입니다.
V2 원본을 먼저 읽은 뒤, 아래 6개 패치를 순서대로 적용하세요.
패치가 V2 원본의 특정 섹션을 "교체"한다고 명시된 경우, 해당 섹션 전체를 이 패치 내용으로 바꾸세요.

---

## PATCH 1 (P0): content_generator.py 함수명 정정

V2 원본에서 잘못 참조된 함수명을 실제 코드 기준으로 수정합니다.

### 잘못된 참조 → 실제 함수명 매핑

| V2 원본이 참조한 이름 | 실제 코드의 함수명 | 위치 |
|---|---|---|
| `_generate_draft_single()` | **`_generate_single()`** | line 762 |
| `_generate_draft_multistep()` | **`_generate_multistep()`** | line 866 |
| `_call_llm()` | **`_generate_with_usage()`** | line 410 |
| (V2에서 `_generate_draft_single`에 직접 pre_analysis 주입) | **`_generate_draft()`** | line 1034 |

### V2 PART 2-B 교체: Call A 메서드

`_run_pre_writing_analysis`의 LLM 호출을 **`_generate_with_usage()`** 기반으로 수정:

```python
async def _run_pre_writing_analysis(
    self,
    job: Job,
    topic_mode: str,
    *,
    token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Call A: 사전 사고 분석 (저가 모델 — self.parser_client 사용).

    실패해도 파이프라인을 중단하지 않고 빈 dict를 반환한다.
    """
    user_prompt = PRE_WRITING_ANALYSIS_PROMPT.format(
        title=job.title,
        keywords=", ".join(job.seed_keywords),
        category=topic_mode,
    )
    system_prompt = "당신은 블로그 글의 전략 설계사입니다. 반드시 유효한 JSON으로만 응답하세요."

    # 저가 모델 클라이언트 선택: parser_client → secondary → primary 순
    cheap_client = getattr(self, "parser_client", None) or self.secondary or self.primary

    try:
        response = await self._generate_with_usage(
            client=cheap_client,
            role="pre_analysis",
            token_usage=token_usage,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.3,
            max_tokens=1200,
        )
        raw = response.content.strip()
        if "```" in raw:
            json_match = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
            if json_match:
                raw = json_match.group(1).strip()
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Call A (pre_writing_analysis) 실패, 기본값으로 진행: %s", exc)
        return {}
```

### V2 PART 2-C 교체: Call D 메서드

```python
async def _run_sentence_polish(
    self,
    content: str,
    *,
    token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """Call D: 문장 크래프트 최종 다듬기 (저가 모델 — self.parser_client 사용).

    실패 시 원문을 그대로 반환한다.
    """
    user_prompt = SENTENCE_CRAFT_CHECKLIST.format(content=content)
    system_prompt = (
        "당신은 한국어 편집 전문가입니다. "
        "원문의 정보, H2 구조, 문단 수, URL, 수치는 절대 변경하지 마세요. "
        "문장 표현과 리듬만 다듬으세요."
    )

    cheap_client = getattr(self, "parser_client", None) or self.secondary or self.primary

    try:
        response = await self._generate_with_usage(
            client=cheap_client,
            role="sentence_polish",
            token_usage=token_usage,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.3,
            max_tokens=4000,
        )
        polished = response.content.strip()

        # 안전 검증: H2 개수
        original_h2 = content.count("## ")
        polished_h2 = polished.count("## ")
        if polished_h2 != original_h2:
            logger.warning("Call D: H2 불일치 (%d→%d) → 원문 유지", original_h2, polished_h2)
            return content

        # 길이 검증: ±15%
        if abs(len(polished) - len(content)) / max(len(content), 1) > 0.15:
            logger.warning("Call D: 길이 변화 15%% 초과 → 원문 유지")
            return content

        return polished
    except Exception as exc:
        logger.warning("Call D (sentence_polish) 실패, 원문 유지: %s", exc)
        return content
```

### V2 PART 2-E 교체: Quality Layer에 모듈 2,3 주입

주입 대상은 `_generate_draft()` (line 1034)이며, `_generate_single()`이 아닙니다.

`_generate_draft()` 시그니처에 `pre_analysis` 파라미터 추가:

```python
async def _generate_draft(
    self,
    job: Job,
    client: BaseLLMClient,
    persona: Any,
    tone_profile: Any,
    topic_mode: str,
    news_context: Optional[List[Dict[str, str]]] = None,
    quality_only: bool = False,
    token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    pre_analysis: Optional[Dict[str, Any]] = None,   # 신규
) -> Tuple[str, str]:
    """초안 생성."""
    topic_mode = normalize_topic_mode(topic_mode or "cafe")
    # ... 기존 news_data_text, seo_snippet, voice_injection 준비 코드 유지 ...

    # ── 모듈 2,3 주입 (quality_only일 때) ──
    cognitive_injection = ""
    emotional_injection = ""
    pre_analysis_injection = ""

    if quality_only:
        # 모듈 2: 인지적 깊이
        cognitive_injection = f"\n\n{COGNITIVE_DEPTH_COMMON}"
        depth_topic = COGNITIVE_DEPTH_BY_TOPIC.get(topic_mode, "")
        if depth_topic:
            cognitive_injection += f"\n\n{depth_topic}"

        # 모듈 3: 감정 아키텍처
        if pre_analysis and pre_analysis.get("emotional_curve"):
            ec = pre_analysis["emotional_curve"]
            emotional_injection = "\n\n" + EMOTIONAL_ARCHITECTURE_PROMPT.format(
                opening_emotion=ec.get("opening_emotion", "호기심"),
                turning_point=ec.get("turning_point", "본문 중반에서 핵심 발견"),
                closing_emotion=ec.get("closing_emotion", "실행 의지와 여운"),
            )

        # 모듈 1 심화: pre_analysis 결과 컨텍스트
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

    # 기존 분기 로직에서 system_prompt에 주입 추가:
    # quality_only 분기의 system_prompt 마지막에:
    #   system_prompt = QUALITY_LAYER_SYSTEM_PROMPT + cognitive_injection + emotional_injection + pre_analysis_injection
    # 또는 non-quality 분기에서도 cognitive_injection만 추가 가능

    # ... 나머지 기존 코드 유지 ...
```

### V2 PART 2-D 교체: generate()에서의 호출 체인

`_generate_single()` (line 762)과 `_generate_multistep()` (line 866)에 `pre_analysis` 전달:

```python
# generate() 메서드 내부 (line ~200):

# Call A: 사전 사고 분석
pre_analysis = await self._run_pre_writing_analysis(
    job=job,
    topic_mode=topic_mode,
    token_usage=token_usage,
)
if pre_analysis:
    llm_calls += 1

# Step 1: 품질 레이어 원문 생성
if self.use_multistep:
    draft, provider_model, calls = await self._generate_multistep(
        job, persona, tone_profile, fallback_chain,
        topic_mode=topic_mode,
        news_context=news_context,
        quality_only=True,
        token_usage=token_usage,
        pre_analysis=pre_analysis,          # 신규 전달
    )
    # ...
else:
    draft, provider_model, provider_used, provider_fallback_from = await self._generate_single(
        job, persona, tone_profile, fallback_chain,
        topic_mode=topic_mode,
        news_context=news_context,
        quality_only=True,
        token_usage=token_usage,
        pre_analysis=pre_analysis,          # 신규 전달
    )
    # ...
```

**`_generate_single()`** (line 762) 시그니처 변경:

```python
async def _generate_single(
    self,
    job: Job,
    persona: Any,
    tone_profile: Any,
    fallback_chain: List[BaseLLMClient],
    topic_mode: str,
    news_context: Optional[List[Dict[str, str]]] = None,
    quality_only: bool = False,
    token_usage: Optional[Dict[str, Dict[str, Any]]] = None,
    pre_analysis: Optional[Dict[str, Any]] = None,   # 신규
) -> Tuple[str, str, str, str]:
    # 내부에서 _generate_draft() 호출 시 pre_analysis 전달:
    # draft, provider_model = await self._generate_draft(
    #     job, client, persona, tone_profile,
    #     topic_mode=topic_mode,
    #     news_context=news_context,
    #     quality_only=quality_only,
    #     token_usage=token_usage,
    #     pre_analysis=pre_analysis,   # 추가
    # )
```

**`_generate_multistep()`** (line 866) 시그니처도 동일하게 `pre_analysis` 추가.

### V2 PART 2-G 삭제

V2의 PART 2-G(`_generate_draft_single`에 파라미터 추가)는 함수명이 잘못되었으므로 삭제.
위의 PATCH 1에서 올바른 함수(`_generate_single`, `_generate_multistep`, `_generate_draft`)에 대한 수정을 이미 포함.

---

## PATCH 2 (P0): 저가 클라이언트(parser_client) 주입 경로 완성

### 변경 파일 1: `modules/llm/llm_router.py`

`build_generation_plan()` (line 729)의 반환 dict에 `parser_step` 추가:

```python
def build_generation_plan(self, overrides=None):
    planned = self.build_plan(overrides=overrides)
    saved = self.get_saved_settings()
    text_keys = saved["text_api_keys"]
    quality = planned["roles"]["quality_step"]
    voice = planned["roles"]["voice_step"]
    # 신규: parser 역할 (저가 모델)
    parser = planned["roles"]["parser"]
    selected_slot_type = "default"

    def role_to_runtime(role_payload):
        provider = str(role_payload.get("provider", "")).strip().lower()
        model = str(role_payload.get("model", "")).strip()
        return {
            "provider": provider,
            "model": model,
            "api_key": str(text_keys.get(role_payload.get("key_id", ""), "")).strip(),
            "label": str(role_payload.get("label", f"{provider}/{model}")),
            "fallback_chain": [...],  # 기존 폴백 로직 유지
        }

    return {
        "strategy_mode": planned["strategy_mode"],
        "parser_step": role_to_runtime(parser),       # 신규 추가
        "quality_step": role_to_runtime(quality),
        "voice_step": role_to_runtime(voice),
        "estimate": planned["estimate"],
        "competition": self.get_competition_state(slot_type=selected_slot_type),
    }
```

**`build_generation_plan_for_job()`** (line 766)도 동일하게 `parser_step` 추가.

**`build_plan()`** 메서드에서 이미 `roles["parser"]`를 계산하고 있으므로, 위의 변경으로 parser role이 반환됩니다.

### 변경 파일 2: `modules/llm/__init__.py`

`_build_generator()` 함수 (line 27)에서 parser 클라이언트 생성 추가:

```python
def _build_generator(config, *, job_store=None, notifier=None, job=None):
    router = LLMRouter(job_store=job_store, llm_config=config)
    if job is None:
        generation_plan = router.build_generation_plan()
    else:
        generation_plan = router.build_generation_plan_for_job(job=job)

    # ... 기존 _build_client_from_spec 함수 유지 ...

    quality_step = dict(generation_plan.get("quality_step", {}))
    voice_step = dict(generation_plan.get("voice_step", {}))
    parser_step = dict(generation_plan.get("parser_step", {}))     # 신규

    primary_client = _build_client_from_spec(quality_step)
    # ... 기존 폴백 로직 유지 ...

    voice_client = _build_client_from_spec(voice_step) or secondary_client

    # 신규: parser_client (저가 모델) 생성
    parser_client = _build_client_from_spec(parser_step)
    if parser_client is None:
        # parser 키가 없으면 secondary를 사용 (primary보다는 저가)
        parser_client = secondary_client

    # ... 기존 circuit_breaker 코드 유지 ...

    return ContentGenerator(
        primary_client=primary_client,
        secondary_client=secondary_client,
        voice_client=voice_client,
        parser_client=parser_client,              # 신규 전달
        additional_clients=additional_clients,
        # ... 나머지 기존 파라미터 유지 ...
    )
```

### 변경 파일 3: `modules/llm/content_generator.py`

`ContentGenerator.__init__()` (line ~120)에 `parser_client` 파라미터 추가:

```python
class ContentGenerator:
    def __init__(
        self,
        # 기존 파라미터...
        client: Optional[BaseLLMClient] = None,
        primary_client: Optional[BaseLLMClient] = None,
        secondary_client: Optional[BaseLLMClient] = None,
        voice_client: Optional[BaseLLMClient] = None,
        parser_client: Optional[BaseLLMClient] = None,   # 신규
        additional_clients: Optional[List[BaseLLMClient]] = None,
        # ... 나머지 기존 파라미터 ...
    ):
        resolved_primary = primary_client or client or ClaudeClient()
        self.primary = resolved_primary
        self.secondary = secondary_client or resolved_primary
        self.voice_client = voice_client or self.secondary
        self.parser_client = parser_client or self.secondary   # 신규: 저가 모델 클라이언트
        self.additional_clients = additional_clients or []
        # ... 나머지 기존 초기화 ...
```

---

## PATCH 3 (P1): 월간 비용에 Idea Vault 쿼터 합산

V2 PART 3-E의 daily_posts 계산 교체:

```python
# 스케줄러에서 하루 총 편수 가져오기 (일반 할당 + Idea Vault 쿼터)
daily_posts = 8  # 기본값
try:
    raw_alloc = self.job_store.get_system_setting("scheduler_category_allocations", "[]")
    alloc_list = json.loads(raw_alloc) if raw_alloc else []
    alloc_count = 0
    if isinstance(alloc_list, list):
        alloc_count = sum(int(a.get("count", 0)) for a in alloc_list)

    # Idea Vault 일일 쿼터 합산
    idea_vault_quota = int(
        self.job_store.get_system_setting("scheduler_idea_vault_daily_quota", "0") or 0
    )

    computed = alloc_count + idea_vault_quota
    if computed > 0:
        daily_posts = computed
except Exception:
    pass
```

---

## PATCH 4 (P1): 토큰 버킷에 신규 역할 등록

### 변경 파일: `modules/llm/content_generator.py`

`_init_token_usage()` (line 373) 교체:

```python
def _init_token_usage(self) -> Dict[str, Dict[str, Any]]:
    """단계별 토큰 집계 버킷을 초기화한다."""
    return {
        "parser": {"input_tokens": 0, "output_tokens": 0, "calls": 0, "provider": "", "model": ""},
        "pre_analysis": {"input_tokens": 0, "output_tokens": 0, "calls": 0, "provider": "", "model": ""},
        "quality_step": {"input_tokens": 0, "output_tokens": 0, "calls": 0, "provider": "", "model": ""},
        "voice_step": {"input_tokens": 0, "output_tokens": 0, "calls": 0, "provider": "", "model": ""},
        "sentence_polish": {"input_tokens": 0, "output_tokens": 0, "calls": 0, "provider": "", "model": ""},
    }
```

**주의**: `_accumulate_token_usage()` (line 381)는 `token_usage.get(role)`을 사용하므로, 위의 초기화에 role이 등록되어 있으면 자동으로 집계됩니다. 추가 수정 불필요.

---

## PATCH 5 (P1): 프론트엔드 병렬 견적 레이스 컨디션 방어

V2 PART 4-E의 useEffect 교체:

```typescript
// 요청 세대 카운터로 stale 응답 무시
const quoteGenerationRef = useRef(0);

useEffect(() => {
    const timer = setTimeout(async () => {
        const generation = ++quoteGenerationRef.current;
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

            const [costQ, balancedQ, qualityQ] = await Promise.all([
                quoteRouterSettings({ ...basePayload, strategy_mode: "cost" }),
                quoteRouterSettings({ ...basePayload, strategy_mode: "balanced" }),
                quoteRouterSettings({ ...basePayload, strategy_mode: "quality" }),
            ]);

            // 이 응답이 최신 요청의 결과인지 확인 (stale 응답 무시)
            if (generation !== quoteGenerationRef.current) return;

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

            const currentQuote = strategyMode === "quality" ? qualityQ
                : strategyMode === "balanced" ? balancedQ
                : costQ;
            setRouterQuote(currentQuote);
        } catch {
            // 미리보기 실패는 저장 동작을 막지 않는다.
        } finally {
            if (generation === quoteGenerationRef.current) {
                setRouterLoading(false);
            }
        }
    }, 350);
    return () => clearTimeout(timer);
}, [strategyMode, textApiKeys, imageApiKeys, imageEngine, imageAiEngine, imageAiQuota, imageTopicQuotaOverrides, trafficFeedbackStrongMode, imageEnabled, imagesPerPostMin, imagesPerPostMax]);
```

**추가 import 필요**: `useRef`를 기존 React import 라인에 추가.

```typescript
import { useEffect, useMemo, useRef, useState } from "react";
```

---

## PATCH 6 (P2): 모델 가격 기준일 및 출처 명시

V2 PART 3-B의 TEXT_MODEL_MATRIX 위에 주석 추가:

```python
# ── 가격 기준일: 2026-02-27 ──
# 출처:
#   DeepSeek: https://api-docs.deepseek.com/quick_start/pricing (V3, 2025-09 이후)
#   Gemini:   https://ai.google.dev/gemini-api/docs/pricing (2.0 Flash, Paid Tier)
#   OpenAI:   https://openai.com/api/pricing (GPT-4.1 mini)
#   Qwen:     https://help.aliyun.com/zh/model-studio/getting-started/models (qwen-plus)
#   Claude:   https://docs.anthropic.com/en/docs/about-claude/models (Sonnet 4)
#   Groq/Cerebras: 무료 Tier (rate limit 적용)
#
# 주의: 가격은 수시 변동됩니다. 분기 1회 이상 실제 가격과 대조하세요.
TEXT_MODEL_MATRIX: List[TextModelSpec] = [
    # ... 기존 스펙 유지 ...
]
```

---

## 변경 파일 최종 목록 (V2 + V2.1 합산)

| 파일 | V2 변경 | V2.1 추가 변경 |
|------|---------|---------------|
| `modules/llm/prompts.py` | 5개 모듈 프롬프트 추가 | 변경 없음 |
| `modules/llm/content_generator.py` | Call A/D, generate() 통합, 모듈 주입 | **함수명 정정** (PATCH 1), **parser_client 추가** (PATCH 2), **토큰 버킷 확장** (PATCH 4) |
| `modules/llm/__init__.py` | 변경 없음 (V2에서 누락) | **parser_client 생성 및 주입** (PATCH 2) |
| `modules/llm/llm_router.py` | TOKEN_BUDGET, 가격, 3-전략, 월간 비용 | **build_generation_plan에 parser_step 추가** (PATCH 2), **Idea Vault 쿼터 합산** (PATCH 3), **가격 출처 주석** (PATCH 6) |
| `server/routers/router_settings.py` | balanced 지원 | 변경 없음 |
| `frontend/src/components/settings/engine-settings-card.tsx` | 3-카드 UI, 월간 비용 | **useRef import + generation 카운터** (PATCH 5) |
| `frontend/src/lib/api.ts` | TS 타입 확장 | 변경 없음 |

---

## 구현 순서 (V2.1 보정 반영)

```
Step 1: prompts.py — 5개 모듈 프롬프트 상수 추가 (V2 PART 1 그대로)
Step 2: llm_router.py — TOKEN_BUDGET, 가격(+출처 주석), 3-전략, 월간 비용(+Idea Vault), build_generation_plan에 parser_step
Step 3: content_generator.py — parser_client 파라미터 추가, 토큰 버킷 확장, Call A/D 메서드, generate() 통합, _generate_draft에 모듈 주입
Step 4: modules/llm/__init__.py — parser_client 생성 및 ContentGenerator에 전달
Step 5: router_settings.py — balanced 검증
Step 6: api.ts — TS 타입 확장
Step 7: engine-settings-card.tsx — useRef import, generation 카운터, 3-카드 UI, 월간 비용
Step 8: 통합 테스트
```

---

## 테스트 요구사항 (V2.1 추가분)

V2 원본의 8개 항목에 추가:

9. **parser_client 주입 검증**: `_build_generator()`에서 parser_step 키가 비어있을 때 secondary_client로 폴백하는지
10. **토큰 집계 검증**: `pre_analysis`와 `sentence_polish` 역할의 토큰이 `llm_token_usage`에 정상 기록되는지
11. **Idea Vault 쿼터 합산 검증**: `scheduler_idea_vault_daily_quota=2`이고 allocations 합계=6이면 daily_posts=8로 계산되는지
12. **레이스 컨디션 검증**: API key를 빠르게 3번 바꿨을 때 마지막 요청의 결과만 UI에 반영되는지
