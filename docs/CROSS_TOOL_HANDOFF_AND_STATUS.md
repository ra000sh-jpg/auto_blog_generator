# Auto Blog Generator - Cross Tool Handoff Status

Last updated: 2026-02-22 (KST)
Branch: `main`
Remote: `origin/main` synced

## 1. 목적
- 이 문서는 Codex/Claude/Grok 등 다른 코딩 도구가 현재 상태를 빠르게 인지하고 바로 이어서 작업할 수 있도록 만든 전달 문서입니다.
- 사람이 읽기 쉬운 요약 + 도구가 바로 실행할 수 있는 체크리스트를 함께 제공합니다.

## 2. 현재 완료 상태 요약
- Git commit/push 정리 완료
- `main` 브랜치가 `origin/main`과 동기화됨
- 테스트 통과: `pytest tests/ -q` 기준 `140 passed`
- 프론트 빌드 통과: `bash scripts/check_frontend_build.sh`

## 3. 최근 주요 커밋 (상위)
1. `785e7e6` test: add coverage for api scheduler rag and publishing flows
2. `ab827ac` feat: add operational scripts for publish and health checks
3. `25a1f56` feat: add core modules for llm image rag and automation
4. `d0fd899` feat: add fastapi routers for dashboard operations
5. `92ddada` feat: scaffold nextjs dashboard frontend
6. `7bf6ee0` feat: upgrade settings router panel and ai-toggle timing
7. `9962a31` feat: expand quality gate heuristics and dependencies
8. `059daaf` feat: refine scheduler buffering and notifier stock alerts

## 4. 기능 단위 완료 범위
- Scheduler:
  - CPU hysteresis + moving average
  - generator/publisher worker 분리
  - daily quota + idea vault 연동
- Pipeline:
  - 2-step 생성(quality -> voice) 구조 반영
  - quality gate 확장
  - retry_wait/알림 흐름 보강
- Naver Publisher:
  - 작성중 팝업 대응
  - 이미지 중앙 정렬 자동화
  - AI toggle 시도/리포트/사후검증 경로 강화
- Dashboard:
  - FastAPI API 라우트 확장
  - Next.js 기반 Settings/Jobs/Health 화면 구성
  - 라우터 전략/견적 연동

## 5. 운영상 중요 포인트
- 민감정보:
  - `.env`, `.env.*`는 git ignore
  - 세션 파일 `data/sessions/naver/state.json`도 git ignore
- 로컬 산출물:
  - `data/`, `logs/`, `config/`는 git ignore
- 테스트 사용 단계에서는 Private repo + 개별 API 키/세션 분리 권장

## 6. 남은 일정 (테스트 사용자 2인 기준)
### D0 - 전달 패키지 준비 (오늘)
- [x] 진행상황 문서화
- [x] 최소실행 절차 문서화
- [x] 패키징 스크립트 추가
- [ ] wife 노트북에 번들 전달

### D1 - wife 노트북 기동
- [ ] Python/Node/npm 설치 확인
- [ ] 프로젝트 압축 해제 또는 clone
- [ ] `pip install -r requirements.txt`
- [ ] `python3 -m playwright install chromium`
- [ ] `frontend/npm install`
- [ ] `.env` 작성
- [ ] `python scripts/naver_login.py` 1회
- [ ] `bash scripts/start_dev.sh` 실행 확인

### D2 - 실사용 검증
- [ ] `python scripts/publish_once.py --headful --use-llm ...` 1회 성공
- [ ] 스케줄러 백그라운드 1일 테스트
- [ ] 알림/로그/재시도 동작 체크

### D3 - 안정화 마감
- [ ] wife 노트북에서 3회 이상 무중단 발행 성공
- [ ] 필수 에러 대응 가이드 확정
- [ ] 테스트 단계 종료 판정

## 7. 완료 판정 기준 (테스트 단계)
- 두 노트북 모두 아래를 만족하면 "코딩 마무리 단계"로 간주
1. 대시보드 접속/작업 생성/발행 성공
2. 실발행 3회 이상 성공
3. 스케줄러 24시간 에러 없이 유지
4. 로그/알림으로 장애 상황 추적 가능

## 8. 다음 코딩 도구가 바로 할 일
1. `docs/TEST_USAGE_QUICKSTART_AND_PACKAGING.md` 먼저 실행
2. wife 노트북에서 설치 및 로그인 재현
3. 실패 시 `logs/worker.stderr.log` + `data/screenshots/` 증거 수집
4. 실패 패턴을 이 문서에 append하여 상태 갱신

## 9. Machine Snapshot (for agents)
```yaml
project: auto_blog_generator
status: test-stage
git:
  branch: main
  remote_synced: true
quality:
  tests_passed: 140
  frontend_build: pass
deployment_mode: private_test_only
next_focus:
  - wife_laptop_onboarding
  - repeatable_packaging
  - 3x publish stability
```
