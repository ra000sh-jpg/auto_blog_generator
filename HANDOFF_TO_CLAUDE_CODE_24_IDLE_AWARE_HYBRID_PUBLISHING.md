# HANDOFF: 24_IDLE_AWARE_HYBRID_PUBLISHING (유휴 상태 감지형 하이브리드 생성/발행 분리)

## 1. 개요 및 목적

현재 Auto Blog Generator는 정해진 시간에 "LLM 글 생성 + 이미지 서칭 + 네이버 발행" 작업을 한 번에 순차적으로 처리합니다. 이는 시스템(맥북)에 간헐적인 고부하를 일으키고, 발행 시점의 유연성을 떨어뜨립니다.

본 Phase 24의 핵심 목표는 **글을 생성하는 워커(Generator)**와 **글을 네이버에 올리는 워커(Publisher)**를 완전히 분리하는 것입니다.

- **Generator**: 사용자의 맥북이 쉬고 있을 때(Idle) 몰래 깨어나 향후 며칠 치 글을 비축(초안 대기 상태)합니다.
- **Publisher**: 지정된 시간(혹은 확률적 분포 기반의 무작위 시간)에 창고에서 꺼내어 발행만 즉시 수행(소요 시간 1초)합니다.

## 2. 핵심 구현 아키텍처 요구사항

### 2.1 📦 데이터베이스(JobStore) 상태 확장

기존 `pending` 상태 외에, 생성(초안)과 발행을 구분하기 위한 새로운 상태값이 필요합니다.

- (기존) `pending` → `processing` → `completed` / `failed`
- **(신규)** `pending_generation` (생성 대기) → `draft_ready` (초안 완성/발행 대기) → `completed`
- SQLite (`automation.db`의 `jobs` 테이블)의 스키마 및 쿼리 수정 파단.

### 2.2 🏭 워커 A: Idle-Aware 생성 공장 (Generator Worker)

- `psutil` 라이브러리(또는 동등한 시스템 모니터링 모듈)를 활용하여 현재 시스템의 CPU/Memory 사용률을 체크해야 합니다.
- **실행 조건**:
  - `CPU 사용률 < 30%` (사용자가 무거운 작업을 하지 않음)
  - `시스템 업타임 후 안정화 기간(예: 부팅 직후 5분 대기)`
- 초안(draft_ready) 재고가 목푯값(예: 6개) 미만일 경우 가동하여, `pending_generation` 상태의 Job들을 LLM을 통해 처리한 후 `draft_ready` 상태로 전환합니다.
- (옵션) 사용자가 마우스를 움직이거나 CPU가 갑자기 치솟을 때 우아하게(Gracefully) 중단할 수 있는 타이머/스레드 인터럽트 처리.

### 2.3 🚀 워커 B: 사람 냄새나는 발행 직원 (Publisher Worker)

- 무거운 LLM 호출 없이 오직 `draft_ready` 상태의 초안을 꺼내서 `playwright_publisher.py`를 통해 발행만 담당합니다.
- 발행 시간은 스케줄러(APScheduler)를 통해 제어하되, 완전히 고정된 시간이 아닌 "출근/점심/퇴근" 시간대 내부의 **가우시안/균등 확률 분포(Randomized Timing)**를 타게 만듭니다. (예: 12시 정각이 아닌 11:40 ~ 12:30 사이 무작위 발행)

### 2.4 🧩 파이프라인 리팩토링 (`pipeline_service.py`)

현재 `process_generation()` 안에 묶여 있는 로직을 다음과 같이 두 개의 명확한 Service Method로 분리해야 합니다.

- `run_draft_generator()` : `pending_generation` -> `draft_ready`
- `run_publisher()` : `draft_ready` -> `completed`

---

## 3. 클로드 코드(Claude Code) 액션 플랜

1. **[Gate 1] 사전 구조 파악 및 Q&A 진행 (코딩 전 필수)**:
   - 이 청사진을 읽고 즉시 코딩을 시작하지 마세요.
   - 프로젝트 내 `modules/automation/job_store.py`와 `modules/automation/pipeline_service.py`, `scripts/run_scheduler.py` 구조를 먼저 분석하세요.
   - 현재 DB가 확장된 Status(`draft_ready`) 처리를 지원하는지, 그리고 `psutil` 의존성 추가가 가능한지 여부를 확인하세요.
   - 이 설계를 보고 **이해되지 않는 점이나 설계상 보완이 필요한 부분을 나(사용자)에게 1~3가지 질문으로 요약하여 물어보세요.** 모든 의문점이 해소된 후 코딩을 시작합니다.

2. **[Step 1] `JobStore` 상태 확장 및 모듈 적용**:
   - `modules/automation/job_store.py` 상태 전환 로직 업데이트 (`draft_ready` 추가 및 조회 기능 추가).
   - 기존의 단일 파이프라인(`process_generation` 등)을 Generator와 Publisher 단위로 분리.

3. **[Step 2] Idle-Aware 시스템 센서 추가**:
   - `psutil`을 활용한 유휴 감지 모듈(`modules/automation/idle_monitor.py` 등) 신설.
   - Generator가 이 모듈의 허락(True)을 받을 때만 도는 루프 구현.

4. **[Step 3] 스케줄러 분리 및 확률적 분포 로직 적용**:
   - `run_scheduler.py` 혹은 관련 스케줄링 로직에서 Generator 스레드와 Publisher 스케줄을 분리.
   - Publisher는 무작위성을 띄는 Jitter(오차범위) 기반 실행 시간 할당.

5. **[Step 4] 통합 시뮬레이션 및 검증**:
   - `--mode generator` / `--mode publisher` 혹은 CLI 명령어로 기능이 따로따로 정상 동작하는지 Dry Run 테스트 결과를 증명하세요.

---

## 4. 구현 현황 로그 (2026-02-22)

| 항목 | 내용 | 상태 |
|------|------|------|
| Gate 1 Q&A 확정 | B(미싱 링크 추가), A+C(가우시안 σ=20+DB앵커), B(mid-gen 인터럽트) | ✅ 완료 |
| `resource_monitor.py` — `make_interrupt_event()` + `run_interrupt_watchdog()` | CPU 급등 시 asyncio.Event set, 별도 백그라운드 태스크로 3초 폴링 | ✅ 완료 |
| `scheduler_service.py` — `_run_draft_prefetch()` mid-gen 인터럽트 | watchdog 태스크 생성, for 루프에서 interrupt_event 매 반복 체크, finally 정리 | ✅ 완료 |
| `scheduler_service.py` — `_get_publish_anchor_hours()` | DB `publish_anchor_hours` 설정 동적 조회, 없으면 `PUBLISH_ANCHOR_HOURS` 상수 | ✅ 완료 |
| `scheduler_service.py` — `_build_daily_publish_slots()` 가우시안 σ 조정 | σ=25→20분, 클램프 ±40분으로 타이트 조정 | ✅ 완료 |
| `server/routers/onboarding.py` — `TelegramTestRequest` 중복 클래스 제거 | IndentationError 버그픽스 | ✅ 완료 |
| E2E 테스트 `tests/test_phase24_idle_aware.py` | 8/8 PASSED (기존 17개 테스트 전체 유지) | ✅ 완료 |
