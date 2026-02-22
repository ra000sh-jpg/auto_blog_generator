# HANDOFF: 22_IDEA_VAULT_AUTO (아이디어 금고 자동 수집망 구축: RSS + Telegram 결합)

## 1. 개요 및 목적

블로그 자동 포스팅 스케줄러가 매일 끊임없이 글을 쓰기 위해 필요한 "소재(Seed, 글감)"를 자동으로 조달하는 파이프라인을 구축합니다.
이를 위해 **자동 수집(주기적 RSS/뉴스 크롤링)**과 **수동 투입(텔레그램 봇 메세지 수신)** 방식을 결합하여 완벽한 하이브리드 수집망(Idea Refinery)을 만듭니다.

---

## 2. 아키텍처 확정 (Gate 1 Q&A 반영 · 2026-02-22)

### Q&A 결정 사항

| 질문 | 선택 | 결정 내용 |
|------|------|-----------|
| **DB 스키마** | **A** | `idea_vault`에 `source_url TEXT DEFAULT ''` 컬럼 추가 + `CREATE UNIQUE INDEX`로 URL 중복 차단 |
| **텔레그램 보안** | **A** | `X-Telegram-Bot-Api-Secret-Token` 헤더 검증 방식, `TELEGRAM_WEBHOOK_SECRET` 환경변수 사용 |
| **LLM 처리** | **A** | RSS 기사 제목(title) + 짧은 요약(summary) 조합을 기존 `IdeaVaultBatchParser`에 투입 |

### 오프라인 폴백 안전장치 (추가 확정)

- **문제**: 맥북 Sleep 중 텔레그램 Webhook 수신 불가 → 아이디어 유실
- **해결**: FastAPI `lifespan` startup 시점에 Telegram `getUpdates` API를 1회 호출 → 미수신 메시지를 일괄 수거 → 금고 적재
- **위치**: `server/routers/telegram_webhook.py` 내 `collect_pending_updates()` 함수

---

### 2.1 Track A: 스케줄러 기반 RSS/트렌드 자동 수집 (Auto Collector)

- **위치:** `modules/collectors/idea_vault_auto_collector.py` (신설)
- **작동 방식:**
  - `APScheduler`에 매일 **06:00 / 15:00** 두 번 `_run_idea_vault_auto_collect()` 크론 잡 등록
  - `RssNewsCollector.fetch_relevant_news()` 로 기사 제목+요약 수집
  - 기사 제목 + 요약을 1줄씩 `IdeaVaultBatchParser.parse_bulk()`에 투입 (heuristic fallback 활용)
  - 중복 URL은 `source_url UNIQUE INDEX` 로 DB 레벨 차단
  - 정제된 아이템을 `job_store.add_idea_vault_items()` 로 저장

### 2.2 Track B: 텔레그램 봇 Webhook + 오프라인 폴백 (Manual Ingestor)

- **위치:** `server/routers/telegram_webhook.py` (신설)
- **Webhook 엔드포인트:** `POST /api/telegram/webhook`
- **인증:** `X-Telegram-Bot-Api-Secret-Token` 헤더 → `TELEGRAM_WEBHOOK_SECRET` 환경변수와 비교
- **메시지 처리 흐름:**
  1. 텍스트 메시지 → `IdeaVaultBatchParser.parse_bulk()` → DB 저장
  2. URL 포함 → 제목 추출 시도 → 파서 투입 (실패 시 URL 자체를 raw_text로 저장)
  3. 저장 완료 후 텔레그램 봇이 즉시 `"✅ 금고 적재: [키워드]"` 답장
- **오프라인 폴백:** lifespan startup → `getUpdates(offset=last_update_id+1)` → 밀린 메시지 일괄 처리
  - `last_processed_update_id` 를 `system_settings`에 저장하여 중복 처리 방지

---

## 3. 신규 파일 목록

| 파일 | 역할 |
|------|------|
| `modules/collectors/idea_vault_auto_collector.py` | RSS → Triage → Vault 저장 로직 |
| `server/routers/telegram_webhook.py` | Webhook 수신 + 오프라인 폴백 |
| `tests/test_idea_vault_auto.py` | E2E 통합 테스트 |

---

## 4. DB 마이그레이션

```sql
-- idea_vault 테이블에 source_url 컬럼 추가
ALTER TABLE idea_vault ADD COLUMN source_url TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_idea_vault_source_url
    ON idea_vault(source_url)
    WHERE source_url != '';
```

---

## 5. 구현 현황 로그

| 날짜 | 항목 | 상태 |
|------|------|------|
| 2026-02-22 | Gate 1 Q&A 확정 및 문서 업데이트 | ✅ 완료 |
| 2026-02-22 | DB 마이그레이션 (source_url 컬럼 + UNIQUE INDEX) | ✅ 완료 |
| 2026-02-22 | `IdeaVaultAutoCollector` + 스케줄러 크론 훅 (06:00, 15:00) | ✅ 완료 |
| 2026-02-22 | `SchedulerService._run_idea_vault_auto_collect()` 메서드 추가 | ✅ 완료 |
| 2026-02-22 | `telegram_webhook.py` (Webhook + 오프라인 폴백) | ✅ 완료 |
| 2026-02-22 | `server/main.py` — telegram 라우터 등록 + lifespan 오프라인 폴백 연동 | ✅ 완료 |
| 2026-02-22 | E2E 테스트 (7/7 PASSED) | ✅ 완료 |
