# 이미지 엔진 개선 청사진 (최종 확정본)

## AI 판단 기반 혼합 이미지 전략 — Codex 검토 전량 반영

---

## 0. 확정된 설계 결정 사항 (ALL CONFIRMED)

| 항목 | 결정 |
|------|------|
| 상한선 쿼터 범위 | **썸네일 포함** (thumbnail + content 통합 카운트) |
| `0장`의 의미 | AI 생성 0장 + 실사진 유지 (Pexels 동작 유지) |
| `all` 최대치 | **4장** (현재 2장 제한 → 4장으로 코드 확장 필요) |
| 무료 Tier 소진 시 | **유료 자동 승격 불허**, Pexels 폴백 + 텔레그램 1회 알림 |
| 텔레그램 알림 정책 | **소진 첫 발생 1회 즉시**, 이후 일일 요약에 포함 |
| 카테고리 override 기준 | **topic_mode** (안정적인 내부 키 사용) |
| 이미지 슬롯 구조 | **`image_slots` 신설** + 기존 `image_prompts` 하위호환 유지 |

---

## 1. 현재 구조 vs 개선 구조

| 항목 | 현재 | 개선 후 |
|------|------|--------|
| 이미지 유형 선택 | 엔진 드롭다운 1개 (전체 동일) | AI가 슬롯별 판단 + 사용자 상한선 설정 |
| AI 생성 수량 | 고정 | 0장 / 1장 / 4장(전체) 중 선택 |
| 본문 이미지 최대치 | **2장 하드코딩** | **4장으로 확장** |
| 판단 주체 | 시스템 고정 정책 | 글쓰기 AI가 슬롯 작성 시 결정 |
| 엔진 ID | 혼재 | `pexels` \| `together_flux` \| `fal_flux` \| `openai_dalle3` 통일 |

---

## 2. image_slots 슬롯 스키마 (신규)

글쓰기 AI가 생성하는 이미지 슬롯 구조체입니다.  
기존 `image_prompts`(문자열 배열)는 파싱 실패 시 폴백으로 유지합니다.

```json
{
  "image_slots": [
    {
      "slot_id": "thumb_0",
      "slot_role": "thumbnail",
      "prompt": "따뜻한 분위기의 카페 인테리어, 자연광",
      "preferred_type": "real",
      "recommended": false,
      "ai_generation_score": 30,
      "reason": "라이프스타일 주제에 실사진이 신뢰감을 줌"
    },
    {
      "slot_id": "content_1",
      "slot_role": "content",
      "prompt": "블로그 자동화 흐름도, 화살표와 아이콘 인포그래픽",
      "preferred_type": "ai_generated",
      "recommended": true,
      "ai_generation_score": 90,
      "reason": "실사진으로 표현 불가능한 추상적 개념"
    },
    {
      "slot_id": "content_2",
      "slot_role": "content",
      "prompt": "수익률 그래프와 통계 시각화",
      "preferred_type": "ai_generated",
      "recommended": true,
      "ai_generation_score": 85,
      "reason": "데이터 시각화는 AI 생성이 정확하고 깔끔"
    },
    {
      "slot_id": "content_3",
      "slot_role": "content",
      "prompt": "노트북 앞에서 작업 중인 사람",
      "preferred_type": "real",
      "recommended": false,
      "ai_generation_score": 45,
      "reason": "자연스러운 실제 상황에는 실사진 적합"
    }
  ]
}
```

**파싱 실패 시 폴백 순서:**

1. `ai_generation_score` 사용 (점수 기반 배정)
2. `recommended` boolean 사용 (true = AI 우선)
3. `preferred_type` 사용 (ai_generated = AI 우선)
4. 모두 실패 시 전부 Pexels 실사진으로 처리

---

## 3. AI 생성 이미지 배정 알고리즘

```
입력: image_slots[], quota (0|1|4), available_engine

1. 썸네일(`slot_role=thumbnail`) + 본문(`slot_role=content`) 통합 풀 구성
2. preferred_type='ai_generated' 슬롯 추출 → ai_generation_score 내림차순 정렬
   동점 시: content 우선 (thumbnail < content)
3. 상위 quota 수만큼 → AI 생성 큐에 배정
4. 나머지 슬롯 + quota 초과분 → Pexels 검색 큐에 배정
5. AI 생성 큐 처리:
   a. 무료 Tier(together_flux) 시도
   b. HTTP 402/429/403 또는 일일 카운터 초과 → 소진 감지
   c. 소진 시 유료 엔진 확인 (설정된 경우)
      - 설정 없음 OR 유료 미허용 → Pexels 폴백 + 텔레그램 알림
   d. 최종 폴백: Pexels
```

---

## 4. 사용자 설정 (UI 변경 사항)

### ① AI 생성 이미지 상한선 (썸네일 포함, 포스팅당)

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| **0장** | 모든 이미지 Pexels 실사진, AI 생성 없음 | ✅ 기본값 |
| **1장** | AI 판단 최고점 1장(썸네일 or 본문 중 상위 1개) AI 생성 | |
| **전체(최대 4장)** | AI 추천 슬롯 전부 AI 생성 (썸네일 포함 최대 4장) | |

### ② AI 생성 엔진 (상한선 1장 이상일 때만 활성)

| 엔진 ID | 이름 | 비용 |
|---------|------|------|
| `together_flux` | Together FLUX (무료 우선) | 무료 Tier |
| `fal_flux` | FAL Flux | 유료 |
| `openai_dalle3` | DALL-E 3 | 유료 |

> **Groq는 텍스트 전용** — 이미지 엔진 목록에서 제거

### ③ openai 키 공유 정책 (UI 표기 명확화)

`OPENAI_IMAGE Key`와 `OPENAI API Key`는 같은 키를 공유합니다.  
UI에서 "(OpenAI Text와 동일 키 사용)"라는 안내 문구를 표시합니다.

---

## 5. 백엔드 변경 사항

### P0 — 즉시 착수

| 항목 | 대상 파일 | 변경 내용 |
|------|-----------|-----------|
| images_per_post 최대치 4장 확장 | `modules/llm/content_generator.py:1302` `modules/images/placement.py:384` | 하드코딩 2 → 4로 변경 |
| 온보딩 엔진 enum 정합성 수정 | `frontend/src/components/onboarding/wizard-step-router.tsx:142` | `mixed\|ai_only` → 현재 시스템 ID 일치 |
| `image_slots` 구조체 파서 추가 | `modules/llm/prompts.py:754` `modules/llm/content_generator.py:1294` | JSON 스키마 + 파싱 블록 추가 |
| 기존 `image_prompts` 폴백 유지 | `modules/llm/content_generator.py:1264` | 문자열 배열 구조 병행 유지 |
| 신규 설정 키 추가 | `modules/llm/llm_router.py:296` | `router_*` 네이밍 체계 준수 |

### 신규 설정 키 (router_* 체계 일치)

```
router_image_ai_quota: "0" | "1" | "all"   (기본: "0")
router_image_ai_engine: "together_flux" | "fal_flux" | "openai_dalle3"  (기본: "together_flux")
router_image_topic_quota_overrides: JSON  (P2, topic_mode 기준)
```

### P1 — AI 배정 + 무료 소진 감지

| 항목 | 대상 파일 | 변경 내용 |
|------|-----------|-----------|
| AI 배정 알고리즘 구현 | `modules/images/runtime_factory.py:23` | quota 기반 슬롯 분류 |
| 무료 Tier 소진 감지 | `modules/images/image_generator.py:249` | HTTP 402/429/403 + 일일 카운터 병행 |
| Pexels 폴백 + 알림 | `modules/automation/pipeline_service.py:277` | 소진 시 폴백 + 텔레그램 1회 트리거 |
| `image_generation_log` 테이블 추가 | `modules/automation/job_store.py` | provider, status, latency, fallback_reason 기록 |
| 비용 견적 분리 | `modules/llm/llm_router.py` | `ai_image_count` / `stock_image_count` 별도 집계 |

### `image_generation_log` 테이블 스키마

```sql
CREATE TABLE IF NOT EXISTS image_generation_log (
    id          TEXT PRIMARY KEY,
    post_id     TEXT,
    slot_id     TEXT NOT NULL,
    slot_role   TEXT NOT NULL,       -- 'thumbnail' | 'content'
    provider    TEXT NOT NULL,       -- 'together_flux' | 'fal_flux' | 'pexels' | ...
    status      TEXT NOT NULL,       -- 'success' | 'fallback' | 'failed'
    latency_ms  REAL,
    fallback_reason TEXT,
    cost_usd    REAL DEFAULT 0.0,
    measured_at TEXT NOT NULL
);
```

### P2 — topic_mode 기반 카테고리별 기본 상한선

```json
{
  "cafe": "0",
  "it": "1",
  "finance": "1",
  "parenting": "0"
}
```

---

## 6. 프론트엔드 변경 사항

### 설정 페이지 — 사진 AI 엔진 섹션 재구성

```
📸 사진 AI 엔진

[✓] 이미지 엔진 활성화

포스팅당 AI 생성 이미지 수 (썸네일 포함)
● 0장 (무료 실사진만)   ○ 1장 (AI 추천 최고점)   ○ 전체 (최대 4장)

AI 생성 엔진 (1장 이상 선택 시 활성)
● Together FLUX (무료 우선)   ○ FAL Flux (유료)   ○ DALL-E 3 (유료)

[함께 사용 중인 키]
PEXELS Key ──────── [저장됨]
TOGETHER Key ─────── [선택 입력]
FAL Key ──────────── [선택 입력]
OPENAI_IMAGE Key ─── [sk-... (저장됨)] (OpenAI Text와 동일 키 사용)
```

---

## 7. 운영 안전장치

### 텔레그램 알림 정책 (확정)

- 무료 Tier 소진 **첫 발생**: 즉시 1회 알림
- 이후 소진 반복: **일일 요약**에 포함 (rate-limit)

### Pexels 출처 저장 정책

`image_generation_log`에 photographer/source_url 저장 → 향후 저작권/투명성 대응

### 이미지 소스 기본값

`image_sources` 누락 시 기본값 → **`stock`** (unknown 아님)

### E2E 테스트 기준

기존 "발행 성공" 외 **"quota 기대치 vs 실제 AI 생성 이미지 개수 일치"** 추가

---

## 8. 미결 사항 (코딩 착수 전 Codex 확인)

| 항목 | 내용 |
|------|------|
| `run_worker.py` 경로 통합 | `scripts/run_worker.py:140` — 구형 DashScope 직접 경로를 runtime_factory 기반으로 통합 (권장) |
| 썸네일 분리 파서 | `modules/images/image_generator.py:140` — 썸네일 prompt가 본문으로 재사용되는 구조 분리 |
| 스케줄러 job 단위 topic 반영 | `modules/automation/scheduler_service.py:1385` — 단일 인스턴스 이미지 제너레이터에 job.topic 반영 경로 확인 |

> **✅ 모든 설계 결정 확정 완료. P0부터 코딩 착수 가능.**
