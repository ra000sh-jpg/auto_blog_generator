# HANDOFF: 21_SCHEDULER_DAEMON (무인 블로그 스케줄러 기획 및 자동화 파이프라인 가동)

## 1. 개요 및 목적

온보딩 마법사를 통해 사용자가 설정한 '매일 발행량(Daily Posts)'과 '카테고리 비율', 그리고 '페르소나 톤'을 바탕으로, **사용자의 개입 없이 백그라운드에서 매일 지정된 시각에 블로그 글을 생성하고 포스팅을 발행(Publish)하는 무인 스케줄러(Zero-Touch Scheduler)**를 가동하는 것이 이 단위 작업의 핵심입니다.

---

## 2. 아키텍처 결정 (Claude Code 확정 · 2026-02-22)

### 2.1 옵션 A 채택 — APScheduler In-Process 방식

| 항목 | 결정 내용 |
|------|-----------|
| **선택 방식** | **옵션 A: APScheduler + FastAPI lifespan 통합** |
| **근거 1** | `modules/automation/scheduler_service.py`가 이미 완전히 구현되어 있음 (APScheduler 3.10+, AsyncIOScheduler 사용) |
| **근거 2** | `run_scheduler_forever()` 함수가 완전한 스탠드얼론 진입점으로 이미 구현됨 |
| **근거 3** | macOS/Linux 로컬 구동 환경에서 OS cron 대비 코드-DB 간 의존성 관리가 훨씬 용이 |
| **근거 4** | `apscheduler>=3.10.0`이 `requirements.txt`에 이미 포함됨 |
| **누락 사항** | `server/main.py` lifespan에 스케줄러 기동 코드가 없음 → 이번에 추가 |

### 2.2 현재 구현 상태 (코드베이스 기준)

이미 **완성된** 컴포넌트:

```
modules/automation/
├── scheduler_service.py   ✅ APScheduler 기반 완전 구현
│   ├── SchedulerService   ✅ generator_worker + publisher_worker 루프
│   ├── _run_daily_quota_seed()  ✅ 자정 큐 시드 생성
│   ├── _run_draft_prefetch()    ✅ CPU 여유 시 초안 선생성
│   ├── _run_daily_target_check()  ✅ 발행 슬롯 기반 발행 실행
│   └── run_scheduler_forever()  ✅ 스탠드얼론 진입점
├── pipeline_service.py    ✅ 생성→품질→이미지→발행 파이프라인
├── notifier.py            ✅ Telegram 알림 (발행 성공 / 일일 요약)
├── job_store.py           ✅ SQLite 기반 작업 큐 (DB-first 패턴)
└── resource_monitor.py    ✅ CPU 히스테리시스 모니터 (28%~35%)
```

**이번에 신규 구현할 항목**:

```
server/
├── main.py                 → lifespan에 scheduler.start() 연동 추가
├── routers/
│   └── scheduler.py        🆕 대시보드용 상태 API + 수동 트리거 API

frontend/src/components/
└── scheduler-widget.tsx    🆕 오늘의 발행 현황 프로그레스 위젯
```

---

## 3. 구현 요구사항 (업데이트)

### 3.1 백엔드 — `server/main.py` 수정

- **lifespan에 SchedulerService 시작/중단 통합**
- `DRY_RUN=true` 환경변수 시 Playwright 실제 발행 건너뜀
- `SCHEDULER_DISABLED=true` 환경변수 시 스케줄러 비활성화 가능 (테스트/개발용)

### 3.2 백엔드 — `server/routers/scheduler.py` 신설

신설할 API 엔드포인트:

| Method | Path | 설명 |
|--------|------|------|
| `GET` | `/api/scheduler/status` | 스케줄러 상태, 오늘 발행 현황, 다음 슬롯 시각 반환 |
| `POST` | `/api/scheduler/trigger/seed` | 오늘 큐 시드 수동 실행 (테스트용) |
| `POST` | `/api/scheduler/trigger/draft` | 초안 선생성 1회 수동 실행 |
| `POST` | `/api/scheduler/trigger/publish` | 발행 1회 수동 실행 |

`GET /api/scheduler/status` 응답 스키마:
```json
{
  "scheduler_running": true,
  "today_date": "2026-02-22",
  "daily_target": 3,
  "today_completed": 1,
  "today_failed": 0,
  "ready_to_publish": 2,
  "queued": 5,
  "next_publish_slot_kst": "2026-02-22T12:03:00+09:00",
  "active_hours": "08:00~22:00",
  "last_seed_date": "2026-02-22"
}
```

### 3.3 프론트엔드 — `scheduler-widget.tsx` 신설

- **폴링 주기**: 30초 (SSR이 아닌 클라이언트 사이드 setInterval)
- **표시 항목**:
  - 오늘 발행 현황 프로그레스 바 (예: 1/3 달성)
  - 다음 발행 예정 시각 (KST)
  - 대기 중인 초안 수
  - 스케줄러 실행 상태 (Running / Stopped)
- **수동 트리거 버튼**: "지금 발행 실행" (POST /api/scheduler/trigger/publish)
- **dashboard-renewal.tsx에 위젯 삽입**

### 3.4 알림 연동

Telegram 알림은 이미 `notifier.py`에서 구현 완료:
- 발행 성공 시: `✅ [카테고리] '제목' 발행 완료. (URL)`
- 매일 22:30: 일일 요약 알림

---

## 4. 검증 요구사항

1. `GET /api/scheduler/status` → `scheduler_running: true` 확인
2. `POST /api/scheduler/trigger/seed` → DB에 오늘 날짜 job 생성 확인
3. `POST /api/scheduler/trigger/draft` → `ready_to_publish` 건수 증가 확인 (DRY_RUN 환경)
4. 프론트엔드 위젯에서 발행 현황 프로그레스 바 정상 렌더링 확인

---

## 5. 구현 현황 로그

| 날짜 | 항목 | 상태 |
|------|------|------|
| 2026-02-22 | 아키텍처 결정 및 문서 업데이트 | ✅ 완료 |
| 2026-02-22 | `server/main.py` lifespan 스케줄러 연동 | ✅ 완료 |
| 2026-02-22 | `server/routers/scheduler.py` 신설 | ✅ 완료 |
| 2026-02-22 | `frontend/src/lib/api.ts` 스케줄러 타입/API 추가 | ✅ 완료 |
| 2026-02-22 | `scheduler-widget.tsx` 신설 + 대시보드 연동 | ✅ 완료 |
| 2026-02-22 | E2E 수동 트리거 테스트 (7/7 PASSED) | ✅ 완료 |
