# P2 청사진: 실측 기반 비용 보정 + Groq Rate Limit 하드닝 + 프론트 빌드 검증

> 작성일: 2026-02-22
> 선행 조건: HANDOFF_TO_CODEX_19 의 모든 P0/P1 완료 + 테스트 136/136 통과
> 난이도: 중간 (E2E 실행 환경 필요 없음 — 단위 테스트 + 빌드만으로 완료 가능)

---

## 배경

P1까지 완료 후 남은 세 가지 구조적 취약점:

| # | 항목 | 위험 수준 | 이유 |
|---|------|----------|------|
| A | TOKEN_BUDGET 보수성 | 중간 | 견적 API가 실제 비용의 40~60%만 보여줌 |
| B | Groq 429 미검증 | 중간 | parser 무료 우선 후 rate limit 시 폴백 동작 미확인 |
| C | 프론트 빌드 미검증 | 낮음 | TSX 변경 후 `next build` 미실행 |

---

## P2-A: TOKEN_BUDGET 실측 보정 파이프라인

### 목표
`job_metrics` 테이블에 실제 토큰 사용량이 쌓이면 TOKEN_BUDGET을 자동으로 보정하는 유틸리티 추가.

### 현재 상태 파악

```python
# modules/llm/llm_router.py:16-20
TOKEN_BUDGET = {
    "parser":       {"input": 450,  "output": 180},
    "quality_step": {"input": 3600, "output": 2400},  # 보수적 최솟값
    "voice_step":   {"input": 2900, "output": 2200},  # 보수적 최솟값
}
```

`job_metrics` 테이블 스키마 (modules/automation/job_store.py):
```sql
CREATE TABLE IF NOT EXISTS job_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    metric_type TEXT,      -- "quality_step", "voice_step", "parser" 등
    status TEXT,           -- "ok" / "error"
    duration_ms REAL,
    created_at TEXT
    -- 토큰 컬럼 없음 ← 이게 문제
);
```

현재 코드 기준 메모:
- LLM 응답 객체는 `GenerationResult`가 아니라 `LLMResponse`(`modules/llm/base_client.py`)를 사용함.
- `pipeline_service.py`는 현재 `quality_gate`, `publish` 중심으로 `job_metrics`를 적재하며, LLM 단계별 토큰 적재는 아직 미연동.

### 구현 계획

#### Step 1: job_metrics 테이블에 토큰 컬럼 추가
**파일**: `modules/automation/job_store.py`

```python
# _create_tables() 내 job_metrics DDL에 추가
ALTER TABLE job_metrics ADD COLUMN input_tokens INTEGER DEFAULT 0;
ALTER TABLE job_metrics ADD COLUMN output_tokens INTEGER DEFAULT 0;
ALTER TABLE job_metrics ADD COLUMN provider TEXT DEFAULT '';
```

마이그레이션 방식: `job_store.py`의 `_migrate_schema()` 패턴 따라 `ADD COLUMN IF NOT EXISTS` 구문 사용 (SQLite는 IF NOT EXISTS 지원 안 하므로 try/except로 처리).

#### Step 2: LLM 호출 시 토큰 기록
**파일**: `modules/llm/content_generator.py`, `modules/llm/magic_input_parser.py`

현재 `LLMResponse`에 토큰 필드는 이미 존재하므로, 생성 파이프라인에서 누적/전달 경로를 추가하는 방식으로 구현.

각 클라이언트에서 응답의 usage 정보를 파싱해 채움:
- Qwen: `response.usage.input_tokens`, `response.usage.output_tokens`
- DeepSeek: `response.usage.prompt_tokens`, `response.usage.completion_tokens`
- Groq: `response.usage.prompt_tokens`, `response.usage.completion_tokens`

#### Step 3: pipeline_service.py에서 job_metrics 기록 시 토큰 포함
**파일**: `modules/automation/pipeline_service.py`

```python
# 기존
job_store.record_job_metric(job_id, metric_type="quality_step", status="ok", duration_ms=elapsed)

# 변경
job_store.record_metric(
    job_id,
    metric_type="quality_step",
    status="ok",
    duration_ms=elapsed,
    input_tokens=result.input_tokens,
    output_tokens=result.output_tokens,
    provider=result.provider,
)
```

#### Step 4: /metrics/llm 엔드포인트에 토큰 집계 추가
**파일**: `server/routers/metrics.py`

```python
# LLMProviderStat에 추가
avg_input_tokens: float = 0.0
avg_output_tokens: float = 0.0
```

SQL:
```sql
AVG(input_tokens) AS avg_input_tokens,
AVG(output_tokens) AS avg_output_tokens
```

#### Step 5: TOKEN_BUDGET 자동 보정 유틸리티 (선택)
**파일**: `modules/llm/llm_router.py` 또는 신규 `modules/llm/token_budget_calibrator.py`

```python
def calibrate_token_budget(job_store: JobStore, min_samples: int = 50) -> dict:
    """job_metrics 실측치로 TOKEN_BUDGET을 보정한다."""
    ...
```

#### 검증
```bash
# 토큰 컬럼 마이그레이션 확인
python3 -c "
from modules.automation.job_store import JobStore
js = JobStore('data/automation.db')
with js.connection() as c:
    cols = [r[1] for r in c.execute('PRAGMA table_info(job_metrics)').fetchall()]
    print(cols)
"
# 기대: [..., 'input_tokens', 'output_tokens', 'provider']

pytest tests/ -k "metric" -v
```

---

## P2-B: Groq Rate Limit 폴백 하드닝

### 목표
parser 역할이 Groq/Cerebras를 1순위로 사용하기 시작했으므로, 429 응답 시 Qwen으로 자동 폴백되는지 검증하고 누락된 경우 구현.

### 현재 상태 파악

**파일**: `modules/llm/content_generator.py` — `_generate_single()` / fallback chain

현재 폴백 체인:
1. `LLMRouter.build_generation_plan()`으로 quality/voice 1순위 + fallback chain 생성
2. `modules/llm/__init__.py`에서 생성기에 fallback client 주입
3. `ContentGenerator._generate_single()`에서 provider 실패 시 다음 provider로 순차 폴백

**문제**: 429 발생 시 "클라이언트 내부 재시도 → 체인 내 provider 전환"이 동작하지만,
회귀 테스트가 약해 Groq/Cerebras 무료 provider 구간의 내구성 증빙이 부족함.

### 구현 계획

#### Step 1: 현재 폴백 체인 동작 확인
**파일**: `modules/llm/content_generator.py`

`_generate_single()`에서 예외 발생 시 next provider로 전환되는지 확인.

체크포인트:
- LLM 호출 try/except에서 `429` (rate limit) 에러를 별도 분기로 처리하는지 확인
- 단순 retry인지, 다른 provider로 switching인지 확인

#### Step 2: 429 처리 분기 하드닝
**파일**: `modules/llm/openai_compat_client.py` (Groq/Cerebras/Gemini OpenAI 호환 클라이언트)

```python
except httpx.HTTPStatusError as e:
    if e.response.status_code == 429:
        raise RateLimitError(f"Rate limit: {self.provider_name}") from e
    raise
```

`modules/exceptions.py`에 `RateLimitError` 없으면 추가:
```python
class RateLimitError(Exception):
    """LLM 프로바이더 Rate Limit 초과."""
```

#### Step 3: ContentGenerator에서 RateLimitError 관측성 보강
**파일**: `modules/llm/content_generator.py`

```python
# 기존 fallback 흐름은 유지하되, 429 기반 폴백 로그를 명시적으로 남긴다.
except RateLimitError as exc:
    logger.warning("rate limited: %s -> fallback", exc)
    ...
```

#### Step 4: 테스트 작성
**파일**: `tests/test_multi_provider.py` 또는 신규 `tests/test_rate_limit_fallback.py`

```python
def test_groq_rate_limit_falls_back_to_qwen(monkeypatch):
    """Groq 429 발생 시 Qwen으로 폴백되는지 검증."""
    from modules.exceptions import RateLimitError

    call_count = {"n": 0}

    async def mock_groq_generate(*args, **kwargs):
        raise RateLimitError("429 Too Many Requests")

    async def mock_qwen_generate(*args, **kwargs):
        call_count["n"] += 1
        return LLMResponse(content="fallback result", input_tokens=10, output_tokens=20, model="qwen", stop_reason="stop")

    # monkeypatch 적용 후 pipeline 호출
    ...
    assert call_count["n"] == 1  # Qwen이 호출됨
```

#### 검증
```bash
pytest tests/ -k "rate_limit or fallback" -v
```

---

## P2-C: 프론트엔드 TypeScript 빌드 검증

### 목표
`metrics-summary.tsx`에 LLM 위젯 추가, `api.ts`에 타입/함수 추가 후 `next build`를 실행해 TypeScript 컴파일 오류 없음을 확인.

### 구현 계획

#### Step 1: 빌드 실행
```bash
cd frontend
npm run build 2>&1 | tee /tmp/nextjs_build.log
# 샌드박스/CI에서 Turbopack 포트 바인딩 이슈가 있으면:
npx next build --webpack 2>&1 | tee /tmp/nextjs_build_webpack.log
```

#### Step 2: 예상 오류 유형 및 대응

| 오류 유형 | 원인 | 대응 |
|-----------|------|------|
| `Type 'LLMMetricsResponse \| null' is not assignable` | null 체크 누락 | optional chaining 추가 |
| `Property 'X' does not exist on type` | 타입 미스매치 | `api.ts`의 타입 정의 확인 |
| `Cannot find module '@/lib/api'` | import 경로 | tsconfig paths 확인 |

#### Step 3: ESLint 검사
```bash
cd frontend && npm run lint
```

#### 검증 완료 기준
```
✓ Compiled successfully
Route (app)                              Size     First Load JS
┌ ○ /                                   ...
└ ○ /settings                           ...
```

---

## 구현 우선순위

| 순서 | 항목 | 소요 예상 | 의존성 |
|------|------|----------|--------|
| 1 | P2-C (프론트 빌드) | 30분 | Turbopack 환경 제약 시 `--webpack` 폴백 |
| 2 | P2-B (Groq 폴백) | 2~3시간 | pipeline_service 코드 파악 필요 |
| 3 | P2-A (토큰 기록) | 3~4시간 | DB 마이그레이션 포함 |

---

## 코덱스 시작 전 확인 사항

```bash
# 1. 테스트 전체 통과 확인
python3 -m pytest tests/ -q --tb=short

# 2. 현재 라우터 선택 동작 확인
python3 -c "
from modules.llm.llm_router import LLMRouter
r = LLMRouter()
plan = r.build_plan({
    'strategy_mode': 'cost',
    'text_api_keys': {
        'qwen': 'x', 'deepseek': 'x', 'gemini': 'x', 'groq': 'x', 'cerebras': 'x'
    }
})
print(f\"parser/cost → {plan['roles']['parser']['provider']} ({plan['roles']['parser']['model']})\")
print(f\"quality_step/cost → {plan['roles']['quality_step']['provider']} ({plan['roles']['quality_step']['model']})\")
"

# 3. job_metrics 컬럼 현황
python3 -c "
from modules.automation.job_store import JobStore
import os
db = 'data/automation.db'
if os.path.exists(db):
    js = JobStore(db)
    with js.connection() as c:
        cols = [r[1] for r in c.execute('PRAGMA table_info(job_metrics)').fetchall()]
        print('job_metrics columns:', cols)
else:
    print('DB not found - will be created on first run')
"
```

---

## 주요 파일 위치 참조

```
modules/
  llm/
    llm_router.py          ← TOKEN_BUDGET, TEXT_MODEL_MATRIX, build_plan()
    base_client.py         ← BaseLLMClient, LLMResponse
    openai_compat_client.py ← Groq/Cerebras 클라이언트 구현
    provider_factory.py    ← create_client()
  automation/
    job_store.py           ← job_metrics 테이블, record_metric()
    pipeline_service.py    ← LLM 호출 오케스트레이션
  exceptions.py            ← 커스텀 예외 클래스
server/routers/
  metrics.py               ← /metrics/llm 엔드포인트
frontend/src/
  lib/api.ts               ← fetchLLMMetrics()
  components/metrics-summary.tsx  ← LLM 위젯
```
