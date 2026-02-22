# HANDOFF: 23_DASHBOARD_RENEWAL (풀스택 대시보드 메인 관제 센터 구축)

## 1. 개요 및 목적

지금까지 백엔드 코어에 구현한 **MBTI 융합 엔진, 텔레그램 연동, 스케줄러 통제, 아이디어 금고 수집상태** 등 무인 블로그 운영의 모든 핵심 현황을 한곳에서 파악할 수 있는 **[통합 관제 센터(Main Dashboard)]**를 프론트엔드에 시각화합니다.
사용자(사장님)가 온보딩을 끝낸 후 매일 접속해서 가장 먼저 보는 화면(`frontend/src/components/dashboard-renewal.tsx` 등)을 "아름답고 직관적인 UI"로 탈바꿈합니다.

## 2. 대시보드 핵심 위젯 구성 (요구사항)

### 2.1 📊 핵심 지표 (Metrics Summary)

- 오늘 발행된 자동 포스팅 수 / 누적 달성 포스팅 수
- **Idea Vault 현황:** 현재 금고에 대기 중인(Pending) 영감 단위 개수
- **운영 원가 체감:** 누적 LLM 예상 API 소비 비용 (직관적인 화폐 단위 표기)

### 2.2 🟢 상태 모니터링 (Health & Status Widget)

- **스케줄러 통제기 (Scheduler Status):** 현재 자동화 엔진이 `Running` 인지 `Stopped` 인지 여부 표시 (Play/Stop 토글 버튼 포함)
- **다음 글 발행 예정 시간:** 크론 잡에 등록된 N 번째 발행 예상 시점 표기
- **텔레그램 연결 상태:** 연동 ✅ 완료 여부

### 2.3 ⚡ UI/UX 디자인 가이드 (Modern & Premium)

- **Glassmorphism / Card Layout:** 투명도와 블러를 섞은 모던한 카드 인터페이스.
- TailwindCSS와 아이콘(Lucide React 권장)을 적극 활용한 풍부한 시각적 피드백 제공.
- 로딩 중일 때는 Skeleton UI 또는 깔끔한 Spinner를 띄울 것.

---

## 3. 클로드 코드(Claude Code) 액션 플랜

1. **[Gate 1] 사전 점검 및 Q&A 진행 (코딩 전 필수)**:
   - 이 청사진을 읽고 섣불리 프론트 코딩을 시작하지 마세요.
   - 현재 `frontend/src/components/dashboard-renewal.tsx` 컴포넌트 내부의 렌더링 방식(온보딩과 메인 화면의 분리/결합 로직)을 살펴보고, **백엔드의 FastAPI에서 위 지표들(금고 DB 개수, LLM 비용, 텔레그램 상태 등)을 한 번에 내려주는 Endpoint(`/api/stats/dashboard` 등)가 존재하는지** 확인하세요.
   - 없다면 API 신설이 먼저 필요합니다. 이런 아키텍처나 UI 위젯 라이브러리(shadcn/ui 혹은 순수 Tailwind) 차용에 설계상 문의점이 있다면 나(사용자)에게 먼저 1~3가지로 요약하여 Q&A를 요구하세요.

2. **백엔드 대시보드 종합 API 개발 (필요 시)**:
   - 프론트에서 위젯을 그리기 위해 필요한 데이터를 한 번의 GET 요청으로 모아서 주는 Endpoint를 만드세요. (`SQLite DB 조회`, `job_store.py 상태` 등)

3. **프론트엔드 대시보드 화면(UI) 리뉴얼 적용**:
   - `dashboard-renewal.tsx` (혹은 관련 컴포넌트)에 들어가서 기존의 낡은 화면이나 온보딩 이후 렌더링되는 블록을 위에서 정의한 **프리미엄 위젯 카드(Metrics, Status, Scheduler Control)**들로 싹 바꿔주세요.

4. **통합 시뮬레이션 및 마일스톤 증빙**:
   - 서버를 띄운 뒤 메인 대시보드 컴포넌트가 Error 없이 이쁘게 데이터를 fetch 해 와서 렌더링하는지 확인하세요. (스케줄러 재생/정지 버튼 클릭 시 백엔드와 연동되는 것도 테스트 포함)
