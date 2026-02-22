# HANDOFF: MBTI Optional Blend + Voice Safety Guard

Updated at: 2026-02-22 (KST)
Branch: `main`

## 1) 작업 목적
- 온보딩 페르소나 생성 시 MBTI를 선택 입력으로 지원한다.
- MBTI 미입력/비활성 상태에서는 질문지 결과만으로 페르소나를 생성한다.
- Voice rewrite 단계에서 사실/수치/구조 훼손 시 자동으로 원문 폴백하여 품질을 보호한다.

## 2) 구현 요약
### A. 온보딩 MBTI 혼합 (옵션)
- 위치: `server/routers/onboarding.py`
- 변경:
  - `PersonaLabRequest`에 필드 추가
    - `mbti_enabled: bool = False`
    - `mbti_confidence: int(0~100, default 60)`
  - MBTI 정규화/검증 함수 추가
  - 질문지 점수와 MBTI prior를 가중 혼합하는 함수 추가
    - MBTI 가중치: 10%~20%
    - 질문지 가중치: 90%~80%
  - `voice_profile.blending` 메타데이터 저장
    - `mbti_applied`, `questionnaire_weight`, `mbti_weight`, `mbti_confidence`, `final_scores` 등
  - MBTI 비활성/미입력/유효하지 않음이면 질문지 단독 결과 사용

### B. 카테고리 추천 연동 정책
- 위치: `server/routers/onboarding.py`
- 변경:
  - 추천 카테고리 계산 시 `voice_profile.mbti_enabled == True`일 때만 MBTI 추천 로직을 반영
  - 비활성 시 관심사 + fallback 기반 추천만 사용

### C. 프론트 온보딩 입력 UX
- 위치: `frontend/src/components/onboarding-wizard.tsx`
- 변경:
  - Step2에 "MBTI 보정(선택)" 토글 추가
  - 토글 ON일 때만 MBTI 선택 + 확신도 슬라이더 표시
  - 반영 비율 안내 문구 표시 (질문지 % / MBTI %)
  - 저장 요청에 `mbti_enabled`, `mbti_confidence` 전달

### D. 기존 대시보드 경로 안전화
- 위치: `frontend/src/components/dashboard-renewal.tsx`
- 변경:
  - 기존 하드코딩 MBTI(`ENFP`) 제거
  - 기본 저장 시 `mbti_enabled: false`, `mbti: ""`로 전달

### E. API 타입 반영
- 위치: `frontend/src/lib/api.ts`
- 변경:
  - `PersonaLabPayload`에 `mbti_enabled`, `mbti_confidence` 추가

### F. 글 품질 보호 가드 (Voice 단계)
- 위치: `modules/llm/content_generator.py`
- 변경:
  - Voice rewrite 후 검증 함수 추가
    - H2 구조 변경 감지
    - URL set 변경 감지
    - 숫자 토큰 누락/변조 감지
  - 길이 드리프트 임계 강화 (0.9~1.1)
  - 위 검증 실패 시 원문(raw_content)으로 자동 폴백

## 3) 테스트 추가
- `tests/test_api_server.py`
  - `test_onboarding_persona_mbti_blending_optional`
  - 검증 내용:
    - MBTI 비활성 시 질문지 점수 그대로 반영
    - MBTI 활성 시 혼합 점수/메타데이터 반영

- `tests/test_llm.py`
  - `test_voice_rewrite_falls_back_when_numeric_fact_changes`
  - 검증 내용:
    - Voice rewrite가 숫자를 바꾸면 최종 본문은 원문으로 폴백

## 4) 권장 검증 명령
```bash
python3 -m pytest tests/test_api_server.py tests/test_llm.py -q
bash scripts/check_frontend_build.sh
```

## 5) 운영 기대 효과
- 사용자 경험:
  - MBTI를 모르는 사용자도 질문지만으로 정확한 페르소나 생성 가능
  - MBTI를 아는 사용자는 보조 보정치로 미세 조정 가능
- 품질:
  - 정보/수치/구조 훼손 리스크를 Voice 단계에서 차단
  - LLM 원문 품질(quality layer)을 보전

## 6) 다음 개선 후보
1. MBTI 반영 비율 상한 A/B 테스트 (15% vs 20%)
2. Voice rewrite 품질 가드에 "핵심 키워드 보존율" 추가
3. 온보딩 결과 화면에 "질문지 기여도 vs MBTI 기여도" 시각화
