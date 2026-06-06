# 미국 매크로 인텔리전스 엔진 적용 검토

작성일: 2026-06-05

## 1. 결론

미국 자료 조사 기반 블로그 발행 방안은 이 프로그램의 차별화 포지션과 잘 맞는다. 다만 "하나의 데이터에서 20개 글"을 그대로 발행 목표로 두면 품질이 쉽게 무너진다. 적용 방향은 **후보는 많이 만들고, 발행은 적게 고르는 구조**가 맞다.

추천 포지션은 다음 한 줄이다.

```text
미국 경제 -> AI 투자 -> 반도체 -> 한국 수출 -> 한국 ETF
```

이 흐름은 일반 경제 블로그와 겹치지 않고, 승환 님 블로그의 고유한 해설 축으로 만들 수 있다.

## 2. 글 생성 방식

하나의 이벤트는 바로 글로 가지 않고 `MacroEvent`로 저장한다.

예:

```json
{
  "eventType": "us_macro_bundle",
  "facts": ["Fed 금리동결", "ISM 55", "CPI 둔화", "고용 강세"],
  "sourceUrls": ["Fed", "BLS", "ISM"],
  "publishedAt": "2026-06-05",
  "confidence": 0.87
}
```

그 다음 `MultiAngleBlogGenerator`가 후보를 만든다.

```text
1개 이벤트
-> 5~20개 후보 생성
-> 중복/근거/문체/발행간격 필터
-> 1~3개만 텔레그램 승인 요청
```

이 구조가 중요한 이유는, 글 후보가 많아지는 것은 장점이지만 실제 발행량까지 늘어나면 블로그가 얕아 보이기 때문이다.

## 3. 데이터 우선순위

### Tier 1: 공식 미국 거시 데이터

- Fed: FOMC 성명서, Minutes, Dot Plot, Beige Book
- BLS: CPI, PPI, 고용보고서, 실업률
- BEA: GDP, PCE, 개인소득
- Census Bureau: 소매판매, 주택지표

역할은 "숫자의 원천"이다. LLM은 숫자를 만들지 않고, 공식 자료에서 확인된 수치만 해석한다.

### Tier 2: 미국 경기 선행 지표

- ISM 제조업/서비스 PMI
- Conference Board 경기선행지수, 소비자 신뢰지수
- University of Michigan 소비심리, 기대 인플레이션

역할은 "경기 방향성"이다. 수치 하나로 단정하지 않고 Fed/BLS/BEA 자료와 함께 묶어 해석한다.

### Tier 3: 기업/산업 데이터

- NVIDIA 실적/가이던스
- TSMC 월매출/실적
- Samsung Electronics 실적/반도체 전망
- SK Hynix 실적/HBM 데이터

역할은 "AI 투자와 반도체 수요 확인"이다.

### Tier 4: 한국 데이터

- 산업통상부 월별 수출입
- 관세청 10일 단위 수출입

역할은 "미국 데이터가 한국 수출로 번지는지 확인"하는 것이다.

## 4. 추천 모듈

```text
modules/macro_intelligence/
  models.py
  us_sources.py
  event_store.py
  relationship_graph.py
  macro_intelligence_engine.py
  multi_angle_blog_generator.py
  angle_deduper.py
  evidence_guard.py
  publishing_selector.py
```

핵심 역할:

- `MacroIntelligenceEngine`: 여러 자료를 하나의 흐름으로 연결
- `RelationshipGraph`: Fed -> AI -> 반도체 -> 한국 수출 -> ETF 연결 관계 관리
- `MultiAngleBlogGenerator`: 이벤트 하나에서 5~20개 글 후보 생성
- `AngleDeduper`: 이미 쓴 글과 관점 중복 제거
- `EvidenceGuard`: 숫자/주장의 근거 부족 시 보류
- `PublishingSelector`: 오늘 발행할 후보 1~3개만 선택

## 5. 후보 생성 원칙

후보는 아래 6개 angle type으로 분류한다.

```text
news_summary
macro_explainer
ai_semiconductor
korea_export_link
etf_angle
life_business_angle
investment_philosophy
```

하나의 이벤트에서 20개 후보가 나와도 같은 angle type이 과도하게 겹치면 발행하지 않는다. 예를 들어 "나스닥 강세", "AI 투자 사이클", "반도체 회복"은 서로 가까워서 하루에 모두 내보내면 중복으로 보인다.

## 6. 품질 게이트

미국 매크로 글은 다음 조건을 통과해야 한다.

```text
source_count >= 2
official_source_count >= 1
numeric_claims_have_evidence = true
angle_duplicate_score <= 0.35
persona_consistency_score >= 85
beginner_readability_score >= 85
philosophy_frame_score >= 80
```

특히 `numeric_claims_have_evidence`가 false면 자동 발행 금지다.

## 7. 발행 전략

평일:

```text
국장 전 1편: 미국 마감/AI/반도체/한국 수출 영향
미장 전 1편: 아시아/유럽/미국 지표와 본장 관전 포인트
일반 글 1편: 승인된 macro angle 또는 기존 제목 CSV
```

주말/휴장일:

```text
공식 지표 해설형 evergreen 글
지난주 지표 연결 복습
Fed -> AI -> 한국 ETF 장기 관점 글
```

## 8. 적용 순서

1. 산업부/관세청 한국 데이터 파이프라인을 먼저 만든다.
2. 미국 데이터는 Fed/BLS/BEA만 1차로 붙인다.
3. ISM/Conference Board/Michigan은 2차로 붙인다.
4. 기업 데이터는 NVIDIA/TSMC부터 붙인다.
5. `RelationshipGraph`에 "미국 경제 -> AI -> 반도체 -> 한국 수출 -> ETF" 규칙을 넣는다.
6. 후보 20개 생성보다 `PublishingSelector` 품질 필터를 먼저 구현한다.

## 9. 최종 판단

이 기능은 적용하는 것이 좋다. 단, "많이 쓰는 시스템"이 아니라 **많은 관점 중 좋은 글만 고르는 시스템**이어야 한다. 승환 님 블로그의 무기는 자동화 자체가 아니라, 미국 거시경제와 한국 투자자의 현실을 연결하는 일관된 해석이다.
