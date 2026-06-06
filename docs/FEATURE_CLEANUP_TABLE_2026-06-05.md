# 기능청소표

작성일: 2026-06-05
목표: 네이버 블로그 하루 3편 자동 운영에 필요한 기능만 전면에 남기고, 나머지는 숨김/보류/삭제 후보로 분리한다.

## 1. 청소 기준

현재 프로그램의 1차 목표는 다음 5가지다.

- 하루 3개 글 자동 생성: 국장 전, 통찰형, 미장 전.
- 텔레그램 초안 검토: 승인 또는 수정본입력.
- 네이버 블로그 임시저장: 최종 확인 링크 제공.
- 글 품질 유지: 쉬운 문체, 함께 공부하는 태도, 통찰, 표/이미지 자연 배치.
- 로컬 LaunchAgent 운영: `server`, `scheduler`, `frontend` 안정 실행.

이 기준에서 직접 도움이 되지 않는 기능은 대시보드 전면에서 제거하고, 바로 삭제하지 않는다. 먼저 숨김 처리 후 1~2주 운영 로그를 보고 코드 제거 여부를 결정한다.

## 2. 기능 분류표

| 번호 | 기능 영역 | 관련 위치 | 현재 상태 | 운영 필요도 | 권장 조치 | 우선순위 | 판단 근거 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 오늘 3개 글 운영 콘솔 | `frontend/src/components/dashboard-renewal.tsx` | 사용 중 | 필수 | 유지 | P0 | 실제 운영자가 매일 보는 핵심 화면이다. |
| 2 | 작업 목록/작업 상세 | `frontend/src/components/jobs-table.tsx`, `server/routers/jobs.py` | 사용 중 | 필수 | 유지하되 간소화 | P0 | 승인 대기, 실패, 임시저장 결과 확인에 필요하다. 다만 모든 큐 통계를 전면에 둘 필요는 낮다. |
| 3 | 작업 취소 | `cancelJob`, `/api/jobs/{job_id}/cancel` | 사용 중 | 필수 | 유지 | P0 | 잘못 생성된 글, 대기 작업 중단에 필요하다. |
| 4 | 하루 3개 스케줄 | `modules/automation/scheduler_*`, `server/routers/scheduler.py` | 사용 중 | 필수 | 유지 | P0 | 목표 기능의 중심이다. |
| 5 | 시장 데이터 브리핑 | `modules/market`, `modules/macro`, `scripts/macro_all.py` | 구축 중 | 필수 | 유지하되 수집 소스 경량화 | P0 | 국장/미장 글 품질을 결정한다. 정부 문서 API별 발급 같은 무거운 경로는 보조로 낮춘다. |
| 6 | 텔레그램 승인/수정본입력 | `modules/automation/draft_approval.py`, `server/routers/telegram_webhook.py` | 사용 중 | 필수 | 유지 | P0 | 완전 자동 발행 전 품질 안전장치다. |
| 7 | 네이버 임시저장/확인 링크 | `modules/uploaders/playwright_publisher.py`, `scripts/publish_ready_job_once.py` | 사용 중 | 필수 | 유지 | P0 | 현재 네이버 블로그 목표의 핵심이다. |
| 8 | 네이버 로그인 진단 | `scripts/naver_login.py`, `modules/uploaders/editor_diagnostics.py` | 사용 중 | 높음 | 유지 | P0 | SmartEditor 변화 대응과 세션 유지에 필요하다. |
| 9 | 글 품질 평가 | `modules/automation/quality_evaluator.py`, `modules/seo/quality_gate.py`, `modules/evaluation` | 사용 중 | 높음 | 유지하되 평가 결과 UI는 축약 | P0 | 고품질 글 양산 목적에 직접 연결된다. |
| 10 | 이미지 검색/생성/배치 | `modules/images` | 사용 중 | 높음 | 유지 | P0 | 블로그 글 품질과 자연스러운 배치에 필요하다. |
| 11 | 표/차트/요약 카드 렌더링 | `modules/images/table_renderer.py`, `market_chart_renderer.py`, `summary_card_renderer.py` | 사용 중 | 높음 | 유지 | P0 | 네이버 에디터에서 마크다운 표가 깨진 문제의 대체 경로다. |
| 12 | 모델 라우터 기본 설정 | `modules/llm/llm_router.py`, `server/routers/router_settings.py` | 사용 중 | 높음 | 간소화 UI로 유지 | P1 | DeepSeek/Qwen 등 비용 통제에 필요하지만 사용자가 매일 볼 기능은 아니다. |
| 13 | API 키 설정 | `frontend/src/components/settings/engine-settings-card.tsx` | 사용 중 | 높음 | 유지하되 필수 키 중심으로 단순화 | P1 | DeepSeek, Qwen, Telegram, Naver Search 등 최소 키 관리에 필요하다. |
| 14 | 텔레그램 설정 | `TelegramSettingsCard`, onboarding telegram endpoints | 사용 중 | 높음 | 유지 | P1 | 토큰 갱신과 chat 연결 복구가 반복적으로 필요하다. |
| 15 | 스케줄러 배분 설정 | `AllocationSettingsCard` | 과잉 노출 | 중간 | 하루 3개 고정 프리셋으로 축소 | P1 | 현재 목표는 3편 고정이다. 3~5편 슬라이더와 카테고리 비율은 운영 판단을 흐릴 수 있다. |
| 16 | 새 작업 수동 예약 | `frontend/src/app/jobs/new/page.tsx` | 사용 가능 | 중간 | 숨김 또는 간단 입력으로 축소 | P1 | CSV/텔레그램/스케줄 자동 생성이 주 경로다. 수동 예약은 비상용으로만 남긴다. |
| 17 | 온보딩 전체 마법사 | `frontend/src/app/onboarding`, `server/routers/onboarding.py` | 사용 가능 | 중간 | 초기 설정용으로 유지, 운영 nav에서는 숨김 | P2 | 설치 직후에는 필요하지만 매일 운영 화면에는 불필요하다. |
| 18 | 아이디어 창고 | `server/routers/idea_vault.py`, `modules/collectors/idea_vault_auto_collector.py` | 부분 사용 | 중간 | 백로그 저장소로 유지, 전면 UI 제거 | P2 | CSV 제목 100개와 주말 글에는 유용하지만 매일 조작할 기능은 아니다. |
| 19 | 매직 입력 | `server/routers/magic_input.py`, `modules/llm/magic_input_parser.py` | 부분 사용 | 중간 | 내부 API로 유지, 화면은 축소 | P2 | 한 문장 제목을 작업으로 바꾸는 기능은 유용하지만 별도 화면은 단순화 가능하다. |
| 20 | 성과/LLM 메트릭 | `server/routers/metrics.py`, `DashboardMetrics` | 과잉 노출 | 낮음 | 운영 화면에서 제거, 진단 API로 유지 | P2 | 비용/호출량 점검에는 필요하지만 매일 글 승인에는 직접 필요하지 않다. |
| 21 | 챔피언/챌린저 모델 경쟁 | `champion_history`, `challenger_model`, `text_model_discovery_worker.py` | 실험 기능 | 낮음 | 자동 교체는 비활성, 후보 발견만 유지 | P2 | 비용과 품질을 자동으로 바꾸는 기능은 운영 위험이 크다. 신모델 발견 후 텔레그램 승인 방식이 안전하다. |
| 22 | VLM 모델 자동 발견/가격/검증 | `vlm_*_worker.py`, `modules/llm/vlm_router.py` | 실험 기능 | 낮음 | 고급 기능으로 숨김 | P2 | 요약 카드/이미지 품질 평가에는 유용하지만 초기 운영에는 복잡하다. |
| 23 | AI 토글 리포트 | `server/routers/ai_toggle.py` | 과거 검증 기능 | 낮음 | 숨김 후 2주 미사용이면 삭제 후보 | P3 | 현재 목표인 네이버 하루 3편 운영과 직접 연결이 약하다. |
| 24 | 멀티채널 관리 | `ChannelManagerCard`, `server/routers/channels.py` | 과잉 기능 | 낮음 | 전면 UI 제거, API는 보류 | P3 | 네이버 블로그 하나에 집중하는 목표와 다르다. |
| 25 | 티스토리/워드프레스 퍼블리셔 | `modules/uploaders/tistory_publisher.py`, `wordpress_publisher.py` | 미사용 후보 | 낮음 | 코드 삭제 후보로 표시, 즉시 삭제는 보류 | P3 | 네이버만 타겟한다는 목표와 맞지 않는다. 단, 테스트가 참조하면 바로 삭제는 위험하다. |
| 26 | 업데이트 버튼/업데이트 API | `frontend/src/components/update-button.tsx`, `server/routers/update.py` | 사용 가능 | 낮음 | nav에서 숨김, CLI로 대체 | P3 | 운영 중 화면에서 업데이트 실행은 위험하다. 수동 CLI가 더 안전하다. |
| 27 | Naver DataLab/검색 수집 | `modules/collectors/naver_datalab.py`, `naver_search.py` | 보조 기능 | 중간 | 내부 수집기로 유지 | P2 | 글감과 검색 의도 파악에 유용하지만 UI 노출은 필요 없다. |
| 28 | RSS/Brave/Web Search | `modules/collectors/rss_news_collector.py`, `modules/web_search` | 보조 기능 | 중간 | 내부 수집기로 유지 | P2 | 무료 데이터 확보망이다. 단, API 키 없는 경로가 우선이다. |
| 29 | Memory/RAG | `modules/memory`, `modules/rag` | 품질 보조 | 중간 | 내부 기능으로 유지 | P2 | 기존 글과 연계성 유지에 필요하다. 다만 설정 화면 노출은 줄인다. |
| 30 | 리소스 모니터 | `modules/automation/resource_monitor.py` | 운영 보조 | 중간 | 유지 | P2 | 로컬 24시간 운영 안정성에 도움 된다. |
| 31 | 수동/스모크 스크립트 | `scripts/smoke_*`, `scripts/status_*`, `scripts/check_api_keys.py` | 사용 중 | 높음 | 유지 | P1 | 운영 장애 복구와 검증에 필요하다. |
| 32 | 과거 실험 테스트 | `tests/test_phase*`, `tests/test_smart_router_*`, `tests/test_vlm_*` | 혼재 | 중간 | 당장 유지, 삭제 전 CI 영향 확인 | P3 | 테스트 제거는 실제 기능 삭제 이후에만 진행한다. |

## 3. 화면 청소 우선순위

### P0: 이미 반영됨

- 대시보드 전면을 `오늘의 3개 글`, `승인/임시저장`, `점검 필요`, `운영 상태` 중심으로 단순화.
- 과거 `WORKER_CRASH`가 현재 정상 작업의 오류처럼 보이지 않도록 점검 대상 필터링.
- 대시보드에 `오늘 운영 점검` 버튼을 추가해 API, DB, 텔레그램, 네이버 세션, 프론트 빌드, 월 비용 기준을 한 번에 확인.
- 작업 목록 기본 필터를 `확인 필요` 중심으로 변경하고, `승인 대기`, `임시저장 대기`, `실패/수정`, `완료`, `전체` 필터를 추가.
- `최근 수정본입력 반영`과 `글 백업 인덱스`를 작업 목록 하단에 추가.
- 새 작업 예약 화면을 한 문장 통찰 예약으로 축소하고, 기본값을 네이버/P4/finance로 고정.
- 상단 내비게이션의 업데이트 버튼 제거.
- `scripts/auto-blog restart frontend`처럼 서비스별 재시작을 받을 수 있게 수정하고, `status`에서 HTTP 응답과 포트 점유자를 함께 표시.

### P1: 반영 완료 또는 안정화 관찰 중

| 화면 | 현재 문제 | 권장 변경 |
| --- | --- | --- |
| 설정 | 모델, 이미지, VLM, 챔피언, 비용, 네이버 연결, 배분, 채널 관리가 한 화면에 섞임 | 기본 화면은 API 키, 텔레그램, 네이버 상태, 하루 3편 프리셋으로 축소 완료. 고급 설정은 열 때만 로드 |
| 작업 목록 | 큐 통계와 상세 품질 스냅샷이 운영자에게 과하게 보일 수 있음 | 운영 필터와 접힌 진단 JSON으로 축소 완료 |
| 새 작업 예약 | 카페/IT/육아 등 과거 토픽이 남아 있음 | 한 문장 통찰 예약으로 축소 완료 |
| 상단 내비게이션 | 업데이트 버튼이 운영 화면에 노출됨 | 업데이트 버튼 숨김 완료. 업데이트는 CLI 중심 |

## 4. 코드 청소 우선순위

| 단계 | 조치 | 대상 | 검증 |
| --- | --- | --- | --- |
| 1단계 | UI 숨김 | `ChannelManagerCard`, `UpdateButton`, 챔피언/VLM 상세 설정, 카테고리 과잉 배분 | 완료. `npm run build` 통과 |
| 2단계 | 기능 플래그화 | 멀티채널, AI 토글, metrics, idea-vault, update API | 완료. 기본값은 하위 호환을 위해 on, 운영 서버에서 env로 off 가능 |
| 3단계 | 내부 API 보존 | `channels`, `ai-toggle`, `metrics`, `idea-vault` 라우터 | 프론트에서 호출하지 않아도 서버 시작 정상 |
| 4단계 | 삭제 후보 확정 | Tistory/WordPress 퍼블리셔, 사용하지 않는 Phase 테스트, 과거 대시보드 컴포넌트 | 1-2주 안정화 뒤 진행 |
| 5단계 | 문서 갱신 | 운영 문서와 설치 문서 | 사용자가 실행할 명령이 단순해야 함 |

## 5. 삭제하면 아직 안 되는 것

아래 기능은 겉으로 복잡해 보여도 지금 삭제하면 품질이나 운영 안정성이 떨어질 수 있다.

- `modules/memory`, `modules/rag`: 기존 글과의 연계성을 만드는 기반이다.
- `modules/images`: 무료 이미지, AI 이미지, 요약 카드, 차트, 표 렌더링 모두 글 품질에 직결된다.
- `modules/macro`, `modules/market`: 국장/미장 브리핑의 차별화 포인트다.
- `modules/automation/quality_evaluator.py`, `modules/seo/quality_gate.py`: 고품질 글을 위한 안전장치다.
- `editor_diagnostics`, `naver_login`, `playwright_publisher`: 네이버 에디터 변화 대응에 필요하다.
- `scripts/smoke_*`: 실제 운영 전 확인 루틴이다.

## 6. 다음 실행안

1. 1-2주 안정화 관찰을 진행한다.
   - 삭제 후보 기능은 실제 삭제하지 않는다.
   - `scripts/auto-blog status`에서 HTTP 응답과 포트 점유자를 함께 확인한다.

2. 라이브 재시작 루틴을 확정한다.
   - `bash scripts/auto-blog restart frontend`가 실제 프론트 재시작까지 완료되는지 확인한다.
   - stale 포트 점유가 반복되면 LaunchAgent bootout/bootstrap 절차를 운영 문서에 고정한다.

3. 운영 점검 패널을 매일 사용한다.
   - 비용 경고가 월 3달러 기준을 넘으면 텔레그램 알림을 보낸다.
   - 네이버 세션과 텔레그램 연결이 깨졌을 때만 설정 화면으로 이동한다.

4. 백업 인덱스와 수정본 반영 기록을 품질 피드백에 연결한다.
   - 수정본입력 반영 글을 모아 문체/구조 개선 규칙으로 승격한다.
   - 텍스트 백업은 용량이 적은 원문 중심으로 유지한다.

5. 자동 모델 교체는 폐기하고 후보 발견만 남긴다.
   - 새 모델 발견: 자동 등록.
   - 실제 전환: 텔레그램 승인 또는 설정 화면 수동 저장.

## 7. 최종 판단

지금 프로그램은 기능을 많이 품고 있지만, 아직 바로 삭제할 단계는 아니다. 네이버 블로그 자동 운영은 로그인 세션, 브라우저 자동화, 텔레그램 승인, 이미지/표 삽입, 시장 데이터 수집이 서로 물려 있다. 따라서 첫 청소는 코드 삭제가 아니라 운영 화면 단순화와 기능 플래그화가 맞다.

추천 다음 작업은 **설정 화면 단순화**다. 이 작업을 끝내면 사용자가 매일 보는 화면은 대시보드와 작업 목록 정도로 줄고, 나머지는 고급 설정 안으로 들어간다.
