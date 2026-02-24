# Smart Model Router + Category Specialization

## 자동 AI 모델 최적화 시스템 청사진 (방안 3 — Codex 검토 반영본)

**목표:** 블로그 글 1편당 텍스트 비용 14원 고정, 초기 90점 → 12개월 후 95점 도달

> **이 문서는 Codex 기술 검토를 반영하여 수정된 버전입니다.**
> 핵심 원칙: 기존 코드를 최대한 재사용하고, 임계값·규칙만 확장한다.

---

## 0. 기존 코드와의 관계 (중복 구현 방지 원칙)

| 구성 요소 | 신규 개발 여부 | 참조 경로 |
|-----------|----------------|-----------|
| LLM 모델 라우팅 | ❌ 기존 확장 | `modules/llm/llm_router.py` |
| 글쓰기 품질 파이프라인 | ❌ 기존 임계값 분기 추가 | `modules/llm/content_generator.py` |
| SQLite DB | ❌ 기존 활용 + 테이블 추가 | `modules/automation/job_store.py` |
| 스케줄러 | ❌ 기존 단일 오케스트레이터 확장 | `server/main.py` + `scripts/run_scheduler.py` |
| 트래픽 수집 | ❌ 기존 확장 | `modules/collectors/metrics_collector.py` |
| 품질 채점 프롬프트 | ❌ 기존 재사용 + 임계값 분기 | `modules/llm/prompts.py` → `QUALITY_CHECK` |

---

## 1. 구현 우선순위 (P0 → P1 → P2)

### P0 — 즉시 착수 (핵심 기반)

1. SQLite 신규 테이블 추가 (`model_performance_log`, `weekly_competition_state`, `champion_history`)
2. 상태값 정합성 수정: `published` → `completed` 기준으로 통일 (`server/routers/stats.py`, `job_store.py`)
3. 품질 파이프라인 임계값 분기 추가 (메인 80점 / 테스트 70점)
4. 자기 비평 재작성 루프: 최대 2회(내부 1회 + 큐 1회) 상한 명시

### P1 — 2단계 (동작 가능 MVP)

1. 주간 경쟁 스케줄러 상태 머신 (shadow 모드 먼저)
2. 챔피언 라우팅 반영 (`llm_router.py` 확장)
3. 모델 성능 이력 적재 로직
4. 프론트엔드 Smart Router 설정 카드

### P2 — 3단계 (장기 자산화)

1. 네이버 트래픽 피드백 보정 (MetricsCollector 확장)
2. 카테고리 전문화 라우팅 (10편 이상 이력 누적 후 활성화)
3. 대시보드 원당 품질 점수 트렌드 차트

---

## 2. 확정된 설계 결정 사항 (최종 — ALL CONFIRMED)

| 항목 | 결정 |
|------|------|
| 주간 경쟁 방식 | **Shadow 평가 먼저** → 자동 승격 + 텔레그램 보고 |
| 비용 기준 | **텍스트 14원** + 이미지 비용 별도 표기 (합산 미포함) |
| 테스트 슬롯 식별 | **fallback_category 기준** (운영 중 카테고리명 변경 대응) |
| 카테고리 전문화 단위 | **topic_mode 우선** (데이터 희소성 문제 완화) |
| 조기 종료 최소 샘플 | 모델당 **최소 2편** 이후 조기 종료 허용 |
| 품질 미달 처리 | 메인: **보류 + 텔레그램 알림**, 테스트 슬롯: **실점 처리 후 계속** |
| 모델 자동 탐색 | **정적 레지스트리 + 수동 갱신** (자동 API 파싱 비권장) |
| 챔피언 교체 시점 | 즉시 반영 금지, **다음 주 월요일 00:05** 경계 시각 고정 |
| `score_per_won` 분모 0 처리 | 무료 모델은 **독립 랭킹 지표 사용** (별도 컬럼) |
| Shadow → 실발행 전환 | **자동 승격** (`auto_promote=true`) + 텔레그램으로 변경사항 보고 |
| `published` → `completed` 마이그레이션 | **3단계**: 1차 `IN ('completed','published')` 호환 → 2차 레코드 일괄 변환 → 3차 코드 정리 |
| 모델 레지스트리 파일 경로 | `config/model_registry/latest.json` + `history/registry_YYYY-MM-DD.json` |

---

## 3. 품질 강화 파이프라인 (기존 코드 확장)

`modules/llm/content_generator.py` 내 기존 생성 흐름에 아래 분기를 추가합니다.

```
[1단계] 키워드/제목 사전 최적화 (Cerebras/Groq 무료)
    └── Groq으로 제목 후보 5개 생성 → 최고 품질 제목 선택

[2단계] 글쓰기
    ├── 메인 카테고리: 기존 OUTLINE_GENERATION → SECTION_DRAFT(×N) → SECTION_INTEGRATION
    └── 다양한 생각들 (테스트 shadow): 기존 단일 생성 방식 유지

[3단계] 품질 채점 (기존 QUALITY_CHECK 재사용, 임계값만 분기)
    ├── 메인 카테고리: 80점 미달 → [4단계]
    └── 테스트 슬롯: 70점 미달 → [4단계]

[4단계] 자기 비평 재작성 (최대 2회 상한)
    ├── 1회차: "문제점 3가지 찾기" → 수정 재작성 → 재채점
    ├── 2회차: 동일 루프 반복
    └── 2회 연속 미달:
        ├── 메인 카테고리: 발행 보류 + 텔레그램 알림
        └── 테스트 슬롯: 해당 모델 실점(-1.0) 처리 후 계속 발행

[5단계] 발행 (shadow 모드일 때는 DB에만 저장, 실 발행 없음)
```

---

## 4. 주간 모델 경쟁 스케줄 (Shadow 모드 우선)

```
월요일 00:05 - 챔피언 교체 기준 시각
  └── 이전 주 1위 모델 → 메인 카테고리 챔피언 적용
  └── 이전 주 2위 모델 → 도전자 슬롯(다양한 생각들) 배정

월~수 (3일) - Shadow 테스트 기간
  └── 다양한 생각들 슬롯에서 모델 A, B, C 각각 shadow 생성 3편
  └── 실제 발행 없이 품질 점수와 비용만 DB에 기록
  └── 최소 2편 이상 생성 후 조기 종료 가능:
       - 특정 모델 평균 5점 이상 격차 → 조기 챔피언 결정
       - 3편 모두 70점 미만 → 즉시 탈락, 다음 후보로 교체

목~일 (4일) - 챔피언 운영
  └── 챔피언 모델: 메인 카테고리 실발행 전담
  └── 도전자 2위 모델: 다양한 생각들 실발행 (추가 실전 데이터 수집)
```

**Shadow → 실발행 자동 승격 조건 (auto_promote=true):**

- 동일 모델이 2주 연속 1위이고 평균 점수 80점 이상
- 조건 충족 시 자동 실발행 전환하고 **텔레그램으로 변경사항 보고**

  ```
  📢 챔피언 모델 교체 알림
  새 챔피언: deepseek-v3 (평균 91.2점, 2주 연속 1위)
  이전 챔피언: qwen-plus
  적용 시각: 2026-03-02 00:05
  ```

---

## 5. 신규 SQLite 테이블 스키마

### `model_performance_log`

```sql
CREATE TABLE model_performance_log (
    id             TEXT PRIMARY KEY,
    model_id       TEXT NOT NULL,
    provider       TEXT NOT NULL,
    topic_mode     TEXT NOT NULL,        -- topic_mode 단위 (카테고리명 대신)
    quality_score  REAL NOT NULL,
    cost_won       REAL NOT NULL,        -- 텍스트 기준 원
    is_free_model  INTEGER NOT NULL DEFAULT 0,
    score_per_won  REAL,                 -- is_free_model=1이면 NULL
    free_model_rank INTEGER,             -- 무료 모델 전용 순위 지표
    post_id        TEXT,
    slot_type      TEXT NOT NULL,        -- 'main' | 'shadow' | 'challenger'
    feedback_source TEXT NOT NULL DEFAULT 'ai_evaluator',  -- 'ai_evaluator' | 'naver_traffic'
    measured_at    TEXT NOT NULL
);
```

### `weekly_competition_state` (재시작/장애 복구용)

```sql
CREATE TABLE weekly_competition_state (
    week_start        TEXT PRIMARY KEY,   -- ISO 날짜 (월요일 기준)
    phase             TEXT NOT NULL,      -- 'testing' | 'champion_ops' | 'completed'
    candidates        TEXT NOT NULL,      -- JSON: [{"model_id": ..., "scores": [...], "eliminated": bool}]
    champion_model    TEXT,
    challenger_model  TEXT,
    early_terminated  INTEGER DEFAULT 0,
    apply_at          TEXT NOT NULL       -- 다음 월요일 00:05 ISO datetime
);
```

### `champion_history`

```sql
CREATE TABLE champion_history (
    week_start        TEXT PRIMARY KEY,
    champion_model    TEXT NOT NULL,
    challenger_model  TEXT,
    avg_champion_score REAL NOT NULL,
    topic_mode_scores TEXT NOT NULL,     -- JSON: {"IT": 91.2, "parenting": 86.0, ...}
    cost_won          REAL NOT NULL,
    early_terminated  INTEGER DEFAULT 0,
    shadow_only       INTEGER DEFAULT 1  -- shadow 모드 해제 시 0
);
```

---

## 6. 스케줄러 통합 원칙 (단일 오케스트레이터)

- **기존 `server/main.py` 내 APScheduler 단일화** 원칙 유지
- 주간 경쟁 로직도 이 스케줄러에 추가 (별도 프로세스 금지)
- `weekly_competition_state`에서 실행 상태 추적 → 재시작 시 중복 실행 방지 Lock

```python
# 추가될 스케줄 예시
scheduler.add_job(run_weekly_model_competition, "cron", day_of_week="mon", hour=0, minute=5)
scheduler.add_job(collect_naver_traffic_feedback, "cron", day_of_week="mon", hour=1)
```

### `published` → `completed` 마이그레이션 3단계

| 단계 | 내용 | 대상 |
|------|------|------|
| 1차 (즉시) | 조회 조건을 `IN ('completed','published')`으로 임시 호환 | `server/routers/stats.py` |
| 2차 (P0) | 기존 `published` 레코드 일괄 `completed` 변환 SQL 실행 | `job_store.py` DB |
| 3차 (P0) | `published` 문자열 참조 코드 완전 제거 | 전체 코드베이스 |

---

## 7. 모델 레지스트리 (정적 JSON + 수동 반자동 갱신)

```json
{
  "version": "2026-02-24",
  "models": {
    "qwen-plus": {
      "input_per_1m_usd": 0.5, "output_per_1m_usd": 1.5,
      "is_free": false, "base_quality_score": 87,
      "providers": ["qwen-us", "qwen-cn"],
      "available_via_keys": ["QWEN_API_KEY"]
    },
    "deepseek-v3": {
      "input_per_1m_usd": 0.28, "output_per_1m_usd": 1.1,
      "is_free": false, "base_quality_score": 89,
      "available_via_keys": ["DEEPSEEK_API_KEY"]
    },
    "groq-llama-3.3-70b": {
      "input_per_1m_usd": 0, "output_per_1m_usd": 0,
      "is_free": true, "base_quality_score": 83,
      "available_via_keys": ["GROQ_API_KEY"]
    },
    "cerebras-llama3.1-8b": {
      "input_per_1m_usd": 0, "output_per_1m_usd": 0,
      "is_free": true, "base_quality_score": 75,
      "available_via_keys": ["CEREBRAS_API_KEY"]
    }
  }
}
```

> **Qwen 미국 키 vs 중국 키**: base URL로 구분하여 사용 가능 모델 목록 분기 처리

> **파일 경로 확정:**
>
> - 실사용: `config/model_registry/latest.json`
> - 스냅샷: `config/model_registry/history/registry_YYYY-MM-DD.json`
> - 버전: `system_settings`에 `model_registry_version` 저장, Git으로 이력 관리

---

## 8. topic_mode 기반 카테고리 전문화 (P2, 12개월 목표)

이력이 **topic_mode별 10편 이상** 누적된 경우에 자동 배정 활성화합니다.

```python
def get_specialist_model(topic_mode: str) -> str:
    logs = db.query(
        "SELECT model_id, AVG(quality_score) AS avg_score FROM model_performance_log "
        "WHERE topic_mode = ? AND slot_type = 'main' AND measured_at > ? "
        "GROUP BY model_id HAVING COUNT(*) >= 10 ORDER BY avg_score DESC LIMIT 1",
        [topic_mode, ninety_days_ago]
    )
    if logs:
        return logs[0].model_id
    return current_champion_model()  # 이력 부족 시 챔피언 폴백
```

---

## 9. 트래픽 피드백 보정 (P2, 단계적 도입)

`modules/collectors/metrics_collector.py` 확장으로 발행 후 7일 시점에 조회수·공감 수집합니다.

```
최종 품질 점수 반영 규칙:
  - 데이터 없음 (발행 직후): AI 평가 100%
  - topic_mode당 데이터 10편 이상: AI 70% + 트래픽 30%
  - topic_mode당 데이터 100편 이상: AI 50% + 트래픽 50% (옵션, 수동 활성화)
```

---

## 10. 프론트엔드 추가 UI (P1)

### 설정 페이지 — Smart Router 카드

- 목표 비용 슬라이더 (편당 최대 X원, 기본 15원)
- 최소 품질 슬라이더 (최소 Y점, 기본 80점)
- 현재 챔피언 모델 표시 + 현재 평균 점수
- 주간 경쟁 상태: "Shadow 테스트 중 / 챔피언 운영 중" + 다음 교체 예정 시각

### 대시보드 — 추가 위젯 (P2)

- 원당 품질 점수 추세 차트 (주별 score/won, 최근 12주)
- 챔피언십 히스토리 표 (최근 4주 변천)

---

## 11. 미결 사항

| 항목 | 상태 |
|------|------|
| `published` → `completed` 마이그레이션 범위 | ✅ 확정 (3단계 계획, 섹션 6 참조) |
| Shadow → 실발행 전환 자동화 | ✅ 확정 (`auto_promote=true` + 텔레그램 보고) |
| 모델 레지스트리 파일 경로 | ✅ 확정 (`config/model_registry/`, 섹션 7 참조) |

> **✅ 모든 미결 사항 해소 완료. 코딩 착수 가능.**
