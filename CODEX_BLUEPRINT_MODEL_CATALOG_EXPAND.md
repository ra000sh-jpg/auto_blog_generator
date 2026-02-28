# CODEX BLUEPRINT — TEXT_MODEL_MATRIX 확장 + 가격 보정

> **목표**: provider당 1모델(7개) → provider당 2-4모델(~20개)로 확장.
> 기존 eval/champion 파이프라인 변경 없이, 키 하나로 같은 provider의 여러 모델이 자동 활성화되도록 한다.
> 추가로 qwen-plus 가격 오류를 보정한다.
>
> **변경 파일**: 총 2개
> - `modules/llm/llm_router.py` (TEXT_MODEL_MATRIX 확장 + 가격 보정)
> - `modules/llm/provider_factory.py` (신규 모델 default 추가)
>
> **변경하지 않는 파일**:
> - `scheduler_cycles.py` — eval/champion 로직 변경 없음
> - `job_store.py` — 스키마 변경 없음
> - `server/routers/router_settings.py` — 응답 구조 변경 없음
> - 프론트엔드 — 기존 모델 매트릭스 테이블이 자동으로 확장 표시

---

## 설계 원칙

1. `TEXT_MODEL_MATRIX`에 추가하면 `_available_text_specs()`가 자동으로 해당 key_id의 키가 있을 때 모델을 활성화
2. 기존 `router_registered_models`와 eval/champion 시스템은 변경 없이 새 모델을 대상으로 동작
3. Settings UI의 "사용 가능한 텍스트 모델" 테이블은 `matrix.text_models`를 그대로 렌더링하므로 자동 반영
4. `provider_factory.py`의 default 모델은 변경하지 않음 (기존 호환). 신규 모델은 `TEXT_MODEL_MATRIX`의 `model` 필드로 라우터가 지정

---

## PATCH 1 — TEXT_MODEL_MATRIX 확장 + qwen-plus 가격 보정

### 파일: `modules/llm/llm_router.py`

### 변경 위치: line 65-145 (TEXT_MODEL_MATRIX 전체 교체)

기존 `TEXT_MODEL_MATRIX` 블록(7개 모델)을 아래 내용으로 **전체 교체**한다.

```python
# 가격 기준일: 2026-02-27
# 출처:
#   DeepSeek: https://api-docs.deepseek.com/quick_start/pricing
#   Gemini: https://ai.google.dev/gemini-api/docs/pricing
#   OpenAI: https://openai.com/api/pricing
#   Qwen: https://help.aliyun.com/zh/model-studio/getting-started/models
#   Claude: https://docs.anthropic.com/en/docs/about-claude/models
#   Groq: https://groq.com/pricing (무료 Tier rate limit 적용)
#   Cerebras: https://www.cerebras.ai/pricing (무료 Tier rate limit 적용)
TEXT_MODEL_MATRIX: List[TextModelSpec] = [
    # ── Qwen (DashScope) ──
    TextModelSpec(
        provider="qwen",
        model="qwen-turbo",
        label="Qwen Turbo",
        key_id="qwen",
        input_cost_per_1m_usd=0.05,
        output_cost_per_1m_usd=0.20,
        quality_score=78,
        speed_score=94,
    ),
    TextModelSpec(
        provider="qwen",
        model="qwen-plus",
        label="Qwen Plus",
        key_id="qwen",
        input_cost_per_1m_usd=0.40,
        output_cost_per_1m_usd=1.20,
        quality_score=84,
        speed_score=90,
    ),
    TextModelSpec(
        provider="qwen",
        model="qwen-max",
        label="Qwen Max",
        key_id="qwen",
        input_cost_per_1m_usd=1.60,
        output_cost_per_1m_usd=6.40,
        quality_score=91,
        speed_score=82,
    ),
    # ── DeepSeek ──
    TextModelSpec(
        provider="deepseek",
        model="deepseek-chat",
        label="DeepSeek Chat",
        key_id="deepseek",
        input_cost_per_1m_usd=0.28,
        output_cost_per_1m_usd=0.42,
        quality_score=86,
        speed_score=88,
    ),
    TextModelSpec(
        provider="deepseek",
        model="deepseek-reasoner",
        label="DeepSeek Reasoner",
        key_id="deepseek",
        input_cost_per_1m_usd=0.28,
        output_cost_per_1m_usd=0.42,
        quality_score=92,
        speed_score=75,
    ),
    # ── Google Gemini ──
    TextModelSpec(
        provider="gemini",
        model="gemini-2.0-flash-lite",
        label="Gemini 2.0 Flash Lite",
        key_id="gemini",
        input_cost_per_1m_usd=0.075,
        output_cost_per_1m_usd=0.30,
        quality_score=82,
        speed_score=96,
    ),
    TextModelSpec(
        provider="gemini",
        model="gemini-2.0-flash",
        label="Gemini 2.0 Flash",
        key_id="gemini",
        input_cost_per_1m_usd=0.10,
        output_cost_per_1m_usd=0.40,
        quality_score=90,
        speed_score=93,
    ),
    TextModelSpec(
        provider="gemini",
        model="gemini-2.5-flash",
        label="Gemini 2.5 Flash",
        key_id="gemini",
        input_cost_per_1m_usd=0.30,
        output_cost_per_1m_usd=2.50,
        quality_score=94,
        speed_score=90,
    ),
    # ── OpenAI ──
    TextModelSpec(
        provider="openai",
        model="gpt-4.1-nano",
        label="OpenAI GPT-4.1 Nano",
        key_id="openai",
        input_cost_per_1m_usd=0.10,
        output_cost_per_1m_usd=0.40,
        quality_score=85,
        speed_score=95,
    ),
    TextModelSpec(
        provider="openai",
        model="gpt-4.1-mini",
        label="OpenAI GPT-4.1 Mini",
        key_id="openai",
        input_cost_per_1m_usd=0.40,
        output_cost_per_1m_usd=1.60,
        quality_score=92,
        speed_score=89,
    ),
    TextModelSpec(
        provider="openai",
        model="gpt-4.1",
        label="OpenAI GPT-4.1",
        key_id="openai",
        input_cost_per_1m_usd=2.00,
        output_cost_per_1m_usd=8.00,
        quality_score=96,
        speed_score=84,
    ),
    # ── Anthropic Claude ──
    TextModelSpec(
        provider="claude",
        model="claude-3-5-haiku-20241022",
        label="Claude Haiku 3.5",
        key_id="claude",
        input_cost_per_1m_usd=0.80,
        output_cost_per_1m_usd=4.00,
        quality_score=88,
        speed_score=93,
    ),
    TextModelSpec(
        provider="claude",
        model="claude-sonnet-4-20250514",
        label="Claude Sonnet 4",
        key_id="claude",
        input_cost_per_1m_usd=3.00,
        output_cost_per_1m_usd=15.00,
        quality_score=97,
        speed_score=83,
    ),
    # ── 무료 프로바이더: parser·태그 생성 등 단순 역할에 우선 라우팅 ──
    TextModelSpec(
        provider="groq",
        model="llama-3.3-70b-versatile",
        label="Groq Llama-3.3 70B (무료)",
        key_id="groq",
        input_cost_per_1m_usd=0.0,
        output_cost_per_1m_usd=0.0,
        quality_score=80,
        speed_score=95,
    ),
    TextModelSpec(
        provider="groq",
        model="llama-4-scout-17b-16e-instruct",
        label="Groq Llama-4 Scout (무료)",
        key_id="groq",
        input_cost_per_1m_usd=0.0,
        output_cost_per_1m_usd=0.0,
        quality_score=83,
        speed_score=94,
    ),
    TextModelSpec(
        provider="cerebras",
        model="llama3.1-8b",
        label="Cerebras Llama3.1 8B (무료)",
        key_id="cerebras",
        input_cost_per_1m_usd=0.0,
        output_cost_per_1m_usd=0.0,
        quality_score=76,
        speed_score=97,
    ),
]
```

### 변경 요약
| 변경 | 내용 |
|------|------|
| **qwen-plus 가격 보정** | $0.28/$0.84 → $0.40/$1.20 (실제 현재가) |
| **신규 추가 (9개)** | qwen-turbo, qwen-max, deepseek-reasoner, gemini-2.0-flash-lite, gemini-2.5-flash, gpt-4.1-nano, gpt-4.1, claude-haiku-3.5, llama-4-scout |
| **총 모델 수** | 7개 → 16개 |
| **key_id 변경** | 없음 — 기존 key_id 체계 그대로 |

### 모델 정렬 규칙
각 provider 내에서 **비용 오름차순** 정렬. 무료 provider는 맨 아래.

---

## PATCH 2 — provider_factory.py 신규 모델 지원 확인

### 파일: `modules/llm/provider_factory.py`

### 변경 없음 — 이유:

`provider_factory.py`의 `create_client()`는 `provider`와 `model` 파라미터를 받아 클라이언트를 생성한다.
각 provider별 클라이언트는 **`model` 파라미터를 그대로 API에 전달**하므로, 새 모델 ID를 추가할 때 factory 수정이 불필요하다.

검증이 필요한 항목:

1. **Qwen**: `QwenClient`는 DashScope OpenAI-compatible API를 사용 → `model="qwen-turbo"`, `model="qwen-max"` 모두 동일 엔드포인트로 전달됨 ✅
2. **DeepSeek**: `DeepSeekClient`도 OpenAI-compatible → `model="deepseek-reasoner"` 그대로 전달 ✅
3. **Gemini**: `create_gemini_client`는 OpenAI-compatible wrapper → `model="gemini-2.0-flash-lite"`, `model="gemini-2.5-flash"` 전달 ✅
4. **OpenAI**: `create_openai_client`는 공식 OpenAI SDK → `model="gpt-4.1-nano"`, `model="gpt-4.1"` 전달 ✅
5. **Claude**: `ClaudeClient`는 Anthropic SDK → `model="claude-3-5-haiku-20241022"` 전달 ✅
6. **Groq**: `create_groq_client`는 OpenAI-compatible → `model="llama-4-scout-17b-16e-instruct"` 전달 ✅

**모든 신규 모델이 기존 factory를 통해 정상 생성 가능.** 코드 변경 불필요.

---

## 동작 확인 — 기존 시스템과의 연동

### 1. Settings UI (engine-settings-card.tsx)
- `textModelMatrix` state는 `initialRouterSettings.matrix.text_models`를 그대로 사용
- `matrix.text_models`는 `LLMRouter.export_for_ui()`가 `TEXT_MODEL_MATRIX`를 직렬화
- **UI 테이블이 자동으로 16개 모델을 표시** → 프론트 변경 불필요

### 2. 실시간 견적 (quote)
- `quoteRouterSettings`가 `_available_text_specs()`를 호출 → 키가 있는 모든 모델의 비용을 합산
- 같은 provider의 다수 모델은 전략 모드에 따라 역할(parser/quality/voice 등)에 자동 배정
- **견적이 자동으로 새 모델을 포함** → 변경 불필요

### 3. 모델 경쟁 (eval/champion)
- `cycle_run_daily_model_eval()`은 `router_registered_models`에서 후보 선정
- 새 모델을 경쟁에 참여시키려면 **운영자가 Settings에서 모델을 registered에 추가** 필요
- 이 동작은 의도적 — 새 모델이 자동으로 프로덕션 eval에 진입하지 않음 (안전)
- **변경 불필요**

### 4. provider_factory 라우팅
- `LLMRouter._assign_roles()`가 전략에 따라 `TEXT_MODEL_MATRIX`에서 모델을 선택
- 선택된 모델의 `provider`+`model`로 `create_client()` 호출
- factory는 model 파라미터를 그대로 provider API에 전달
- **변경 불필요**

---

## 최종 검증 체크리스트

```bash
# 1. Python 문법 확인
python3 -c "from modules.llm.llm_router import TEXT_MODEL_MATRIX; print(f'{len(TEXT_MODEL_MATRIX)} models loaded')"
# 기대: "16 models loaded"

# 2. 중복 검사 — (provider, model) 쌍이 유일해야 함
python3 -c "
from modules.llm.llm_router import TEXT_MODEL_MATRIX
pairs = [(s.provider, s.model) for s in TEXT_MODEL_MATRIX]
assert len(pairs) == len(set(pairs)), f'Duplicate found: {[p for p in pairs if pairs.count(p) > 1]}'
print('No duplicates')
"

# 3. key_id 유효성 — 기존 provider 이름만 사용하는지 확인
python3 -c "
from modules.llm.llm_router import TEXT_MODEL_MATRIX
valid_keys = {'qwen', 'deepseek', 'gemini', 'openai', 'claude', 'groq', 'cerebras'}
for s in TEXT_MODEL_MATRIX:
    assert s.key_id in valid_keys, f'Invalid key_id: {s.key_id} for {s.model}'
print('All key_ids valid')
"

# 4. 기존 테스트 통과
python3 -m pytest tests/ -x -q --ignore=tests/e2e

# 5. 타입 체크 (프론트엔드 변경 없으므로 백엔드만)
python3 -c "from modules.llm.llm_router import LLMRouter; print('Import OK')"
```

---

## 변경 파일 요약

| 파일 | 변경 내용 | 줄 수 변경 |
|------|-----------|-----------|
| `modules/llm/llm_router.py` | TEXT_MODEL_MATRIX 7개 → 16개 + qwen-plus 가격 보정 | ~80줄 교체 |
| `modules/llm/provider_factory.py` | 변경 없음 (검증만) | 0줄 |
| 프론트엔드 | 변경 없음 (자동 반영) | 0줄 |

## 수용 기준 (Definition of Done)

1. `TEXT_MODEL_MATRIX`에 16개 모델이 중복 없이 등록되어 있다
2. qwen-plus 가격이 $0.40/$1.20으로 보정되어 있다
3. Settings UI "사용 가능한 텍스트 모델" 테이블에 16개가 표시된다
4. 실시간 견적이 같은 provider의 다수 모델을 고려하여 계산된다
5. 기존 테스트가 모두 통과한다
6. `provider_factory.py`에서 신규 model ID로 클라이언트 생성이 가능하다
