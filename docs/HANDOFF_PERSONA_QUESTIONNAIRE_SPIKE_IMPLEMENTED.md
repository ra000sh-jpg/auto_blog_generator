# HANDOFF: Persona Questionnaire Spike (Implemented)

## 1) 이번 턴 구현 목표
- 온보딩 Step 2의 페르소나 수집 방식을 단순 입력형에서 **상황형 질문지 기반**으로 고도화.
- 5차원 스케일(구조성/근거성/심리적 거리/비판 수위/문체 밀도)을 실제 답변으로 점수화.
- 기존 MBTI 보정 및 Voice Safety Guard와 충돌 없이 하위 호환 유지.

## 2) 구현 완료 항목

### A. 백엔드 질문지 엔진 신설
- 파일: `/Users/naseunghwan/Desktop/auto_blog_generator/modules/persona/questionnaire.py`
- 추가 내용:
  - 질문지 버전/차원 상수 (`QUESTIONNAIRE_VERSION`, `QUESTIONNAIRE_DIMENSIONS`)
  - 질문/선택지 데이터 모델 (`QuestionnaireQuestion`, `QuestionnaireOption`)
  - 상황형 질문 7개 정의 (각 3지선다 + 차원별 효과값)
  - 질문지 API 전달용 직렬화 함수: `get_question_bank_payload()`
  - 점수 계산 함수: `score_questionnaire_answers()`
    - 입력: `(question_id, option_id)` 배열
    - 출력: 5차원 점수, 응답률, 차원 신뢰도, 누락 문항, 정규화된 응답 목록

### B. 온보딩 라우터 확장
- 파일: `/Users/naseunghwan/Desktop/auto_blog_generator/server/routers/onboarding.py`
- 추가/변경 내용:
  1. `PersonaLabRequest` 확장
     - `questionnaire_version`
     - `questionnaire_answers[]`
  2. 질문지 스키마 응답 모델 추가
     - `PersonaQuestionBankResponse` 및 하위 모델
  3. 신규 API
     - `GET /api/onboarding/persona/questions`
     - 상황형 질문지 뱅크 반환
  4. 점수 계산 경로 통합
     - `_resolve_questionnaire_scores()` 추가
     - 질문지 응답이 있으면 질문지 점수 우선 사용
     - 응답이 없으면 기존 슬라이더 점수 사용(하위 호환)
  5. Voice Profile 메타 강화
     - `voice_profile.questionnaire_meta` 저장
     - source/manual 여부, answered_count, resolved_answers 등 기록

### C. 프론트 온보딩 Step 2 개편
- 파일: `/Users/naseunghwan/Desktop/auto_blog_generator/frontend/src/components/onboarding-wizard.tsx`
- 파일: `/Users/naseunghwan/Desktop/auto_blog_generator/frontend/src/lib/api.ts`
- 추가/변경 내용:
  1. 질문지 API 타입 및 함수 추가
     - `PersonaQuestionBankResponse`, `fetchPersonaQuestionBank()`
  2. Step 2 UI 강화
     - 카드형 상황 질문 7문항 렌더링
     - 3지선다 버튼 선택
     - 진행률 바/응답 개수 표시
     - 5차원 점수 미리보기(실시간 계산)
  3. 저장 로직 변경
     - 기존 고정 50점 제거
     - 질문 응답 기반 점수 + `questionnaire_answers` 서버 전송
     - 최소 응답 개수(`required_count`) 미달 시 저장 차단

## 3) 호환성 정책
- 기존 클라이언트가 `questionnaire_answers` 없이 호출해도 정상 동작.
- 기존 테스트 및 파이프라인과 충돌 없음.
- MBTI 혼합 로직은 기존대로 보조 가중치로만 작동.

## 4) 검증 결과
- API 테스트:
  - `python3 -m pytest tests/test_api_server.py -q`
  - 결과: `15 passed`
- 전체 회귀:
  - `python3 -m pytest tests/ -q`
  - 결과: `144 passed`
- 프론트 빌드:
  - `bash scripts/check_frontend_build.sh`
  - 결과: Lint warning 3건(기존 경미 항목), Build 성공

## 5) 추가된 테스트
- 파일: `/Users/naseunghwan/Desktop/auto_blog_generator/tests/test_api_server.py`
  - `test_onboarding_persona_question_bank_endpoint`
  - `test_onboarding_persona_questionnaire_answers_apply_scores`

## 6) 다음 추천 작업 (P2 후속)
1. 질문 순서 랜덤화 + seed 고정 옵션
   - 반복 사용 시 피로도 완화, A/B 실험 가능
2. Step 2 결과 요약 카드
   - "당신의 페르소나: 구조형/근거중심/직설중간..." 즉시 피드백
3. 관리자 설정에서 문항 on/off
   - 향후 실험 문항 추가 시 운영 유연성 확보
4. 온보딩 이탈 포인트 분석 이벤트
   - 몇 번째 질문에서 이탈하는지 이벤트 로깅

## 7) 참고 사항
- 본 구현은 "질문지 우선 + MBTI 선택 보정" 원칙을 유지.
- Voice Rewrite Safety Guard(원문 훼손 시 롤백)와 독립적으로 동작하며 상호 충돌 없음.
