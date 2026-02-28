# 작업 개요

목표:
`API 키 1개로 사용 가능한 다수 모델`을 자동 탐색하고, 설정 UI에서 확장 목록으로 노출하며,
모델 경쟁 시스템(eval/champion)이 신규 모델을 자동 검증하도록 만든다.

핵심:
1. provider별 모델 목록 자동 수집
2. Settings에서 모델 목록 확장 표시 + 활성/비활성 제어
3. 신규 모델 자동 eval 큐 진입
4. 전략 모드(cost/quality/balanced) 유지
5. 하위호환(`router_registered_models`) 유지

---

# 현재 문제

1. `TEXT_MODEL_MATRIX`가 정적이라 provider의 신규/저가 모델 자동 반영 불가
2. `router_registered_models`는 수동 입력 기반이라 운영 부담 큼
3. API 키는 살아있지만 해당 provider의 추가 모델은 탐색/검증 루프에 안 들어옴

---

# 설계 원칙

1. 자동 발견은 하되, 운영자 통제(활성/비활성/고정) 권한은 유지
2. 신규 모델은 기본 `eval_only`로 등록
3. 샘플이 쌓이고 조건 충족 시 자동 승격/챔피언 교체
4. 기존 설정/응답 필드 하위호환 유지
5. 네트워크 실패 시 조용한 폴백(기존 정적 모델로 계속 동작)

---

# 데이터 모델

## system_settings 신규 키

1. `router_model_catalog` (JSON 배열)
- provider별 발견 모델 메타 저장
- 예시:
```json
[
  {
    "provider": "qwen",
    "model_id": "qwen-plus",
    "display_name": "Qwen Plus",
    "active": true,
    "enrollment": "main",
    "source": "static",
    "supports_text": true,
    "cost_input_per_1m_usd": 0.28,
    "cost_output_per_1m_usd": 0.84,
    "quality_score_hint": 84,
    "speed_score_hint": 90,
    "discovered_at": "2026-02-27T00:00:00Z",
    "last_seen_at": "2026-02-27T00:00:00Z"
  }
]
```

2. `router_model_catalog_last_sync_at` (ISO datetime)
3. `router_model_catalog_sync_enabled` (`true|false`, 기본 true)

## 기존 키 유지

1. `router_registered_models` 유지
2. 초기엔 `router_model_catalog`에서 `active=true` 모델을 registered로 동기화
3. 구버전 클라이언트는 기존 registered 기반으로 계속 동작 가능

---

# 백엔드 변경

## 1) 신규 파일
- `modules/llm/provider_model_discovery.py`
- 책임:
1. provider별 `list_models()` 구현
2. 공통 포맷으로 정규화
3. 정적 매트릭스와 병합
4. catalog 업데이트/동기화 유틸 제공

## 2) llm_router.py 수정
- 파일: `modules/llm/llm_router.py`
- 변경:
1. `get_saved_settings()`에서 `router_model_catalog` 파싱
2. `_available_text_specs()`를 `TEXT_MODEL_MATRIX + catalog(active)` 기반으로 확장
3. `get_competition_state()`에 `catalog_models_count`, `catalog_last_sync_at` 추가
4. 비용/품질 힌트 없는 모델 폴백 규칙:
- cost 모드: cost 없으면 패널티 또는 제외
- quality 모드: quality_score_hint 없으면 기본값 사용

## 3) scheduler_cycles.py 수정
- 파일: `modules/automation/scheduler_cycles.py`
- 신규 사이클:
1. `cycle_run_model_catalog_sync(service)`
- provider 모델 목록 갱신
- 신규 모델은 `active=true`, `enrollment=eval_only`로 등록
2. 기존 `cycle_run_daily_model_eval()` 보강
- eval 후보를 `router_registered_models` 대신 catalog 기준으로 생성
- 우선순위: `eval_only` + 샘플 적은 모델
3. 기존 `cycle_auto_champion_switch()` 보강
- catalog active 모델 대상으로 비교
- `eval_only` 모델도 조건 충족 시 champion 가능(또는 자동 `main` 승격 후 champion)

## 4) scheduler_service.py 수정
- 파일: `modules/automation/scheduler_service.py`
- APScheduler 등록:
1. `model_catalog_sync` (하루 1회, 예: 00:03)
2. 기존 daily eval / auto champion switch는 유지

## 5) job_store.py 수정
- 파일: `modules/automation/job_store.py`
- 추가 메서드:
1. `get_model_catalog() -> list[dict]`
2. `save_model_catalog(catalog: list[dict]) -> None`
3. `sync_registered_models_from_catalog() -> None`
4. `migrate_model_catalog_defaults()` (ensure_schema 마지막 호출)

---

# API 변경

## router_settings.py
- 파일: `server/routers/router_settings.py`
- 응답 확장:
1. `competition`에 신규 필드:
- `catalog_models_count`
- `catalog_last_sync_at`
2. `model_catalog` 섹션 추가:
- `items`
- `sync_enabled`
- `last_sync_at`

- 수정 엔드포인트:
1. `POST /router/settings`에서 모델별 `active`, `enrollment` 수정 반영
2. `POST /router/settings/sync-models` (수동 동기화 버튼용) 추가

---

# 프론트엔드 변경

## 1) api.ts
- 파일: `frontend/src/lib/api.ts`
- 타입 확장:
1. `ModelCatalogItem`
2. `RouterSettingsResponse.model_catalog`
3. 수동 동기화 API 함수 추가

## 2) engine-settings-card.tsx
- 파일: `frontend/src/components/settings/engine-settings-card.tsx`
- UI 추가:
1. "사용 가능한 모델" 확장 리스트
2. provider 그룹별 아코디언
3. 각 모델 행:
- 모델명
- enrollment (`eval_only/main/disabled`)
- active 토글
- 최근 샘플/평균 점수(가능 시)
4. "모델 목록 동기화" 버튼

## 3) settings page
- 파일: `frontend/src/app/settings/page.tsx`
- model catalog 저장/갱신 핸들러 연결

---

# 동작 규칙

1. 신규 발견 모델
- 기본: `active=true`, `enrollment=eval_only`
2. eval 슬롯 선정
- 하루 1건
- `enrollment=eval_only or main` 중 샘플 최소 모델 우선
3. main/champion 후보
- `enrollment=main` 우선
- 옵션: 조건 충족 시 eval_only 자동 승격
4. 비용 모드
- `avg_score_per_won` 기준
- 비용 정보 없는 모델은 패널티

---

# 마이그레이션

1. `router_model_catalog`가 없으면
- `TEXT_MODEL_MATRIX` + `router_registered_models` 합쳐 초기 catalog 생성
2. 기존 `router_registered_models`는 유지
3. 구키 삭제 없음(안전 롤백 가능)

---

# 테스트

## 단위 테스트
1. catalog 병합 시 중복 제거(provider+model_id)
2. 신규 모델 `eval_only` 자동 등록
3. daily eval이 샘플 0 모델 우선 선택
4. auto champion switch가 strategy_mode를 존중
5. cost 정보 없는 모델 처리 규칙 검증

## 통합 테스트
1. sync -> eval 선정 -> performance 기록 -> champion 교체까지 e2e
2. 구버전 응답 필드 하위호환 검증

---

# 변경 파일 목록(예상)

1. `modules/llm/provider_model_discovery.py` (NEW)
2. `modules/llm/llm_router.py`
3. `modules/automation/scheduler_cycles.py`
4. `modules/automation/scheduler_service.py`
5. `modules/automation/job_store.py`
6. `server/routers/router_settings.py`
7. `frontend/src/lib/api.ts`
8. `frontend/src/components/settings/engine-settings-card.tsx`
9. `frontend/src/app/settings/page.tsx`
10. `tests/test_smart_router_p1.py` (확장)
11. `tests/test_model_catalog_sync.py` (NEW)

---

# 수용 기준(Definition of Done)

1. Settings에 provider별 모델 목록이 확장 표시된다.
2. 신규 모델이 자동 발견되어 목록에 뜬다.
3. 신규 모델이 eval 큐에 자동 진입한다.
4. 충분한 샘플 후 전략 모드 기준으로 champion 교체가 동작한다.
5. 기존 registered 기반 흐름과 API 하위호환이 깨지지 않는다.
