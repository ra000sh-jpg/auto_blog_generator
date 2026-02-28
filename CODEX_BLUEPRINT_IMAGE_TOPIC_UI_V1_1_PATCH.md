# V1.1 Patch: 코덱스 리뷰 5건 반영 보정

이 문서는 `CODEX_BLUEPRINT_IMAGE_TOPIC_UI.md`의 **보정 패치**입니다.
V1 원본을 먼저 읽은 뒤, 아래 5개 패치를 순서대로 적용하세요.

---

## PATCH 1 (P0): 필드명 및 타입 정정

### 문제
V1 원본이 `onboardingStatus.allocations`를 참조했지만, 실제 응답 필드는 `category_allocations`이다.
또한 프론트엔드 TS 타입 `OnboardingStatusResponse.category_allocations`에 `images_per_post`와 `ai_images` 필드가 누락되어 있다.

### 수정 1-A: `frontend/src/lib/api.ts` (line 161-165)

`OnboardingStatusResponse.category_allocations` 타입 확장:

```typescript
export type OnboardingStatusResponse = {
  completed: boolean;
  persona_id: string;
  interests: string[];
  voice_profile: Record<string, unknown>;
  recommended_categories: string[];
  categories: string[];
  fallback_category: string;
  daily_posts_target: number;
  idea_vault_daily_quota: number;
  category_allocations: Array<{
    category: string;
    topic_mode: string;
    count: number;
    percentage?: number;
    images_per_post?: number;   // 신규: 카테고리별 총 이미지
    ai_images?: number;         // 신규: 카테고리별 AI 이미지
  }>;
  category_mapping: Record<string, string>;
  telegram_configured: boolean;
  telegram_bot_token: string;
  telegram_chat_id: string;
  telegram_webhook_secret: string;
};
```

> **근거**: 백엔드 `ScheduleAllocationItem` (server/schemas/onboarding.py line 91-99)에는 이미 `images_per_post: int = Field(default=2, ge=0, le=4)`와 `ai_images: int = Field(default=0, ge=0, le=4)`가 존재한다.
> 서버는 이 필드를 응답에 포함하지만, 프론트엔드 TS 타입이 이를 선언하지 않아 타입 안전성이 깨져 있었다.

### 수정 1-B: PART 3 부모 prop 전달 교체

V1 원본의 PART 3 전체를 아래로 교체:

```tsx
// frontend/src/app/settings/page.tsx (line 124)
// 기존:
<EngineSettingsCard
    initialRouterSettings={routerData}
    initialNaverStatus={naverStatus}
/>

// 교체:
<EngineSettingsCard
    initialRouterSettings={routerData}
    initialNaverStatus={naverStatus}
    categoryAllocations={onboardingData.category_allocations || []}
/>
```

`onboardingData`는 이미 settings page에서 로딩됨 (line 122: `{!loading && onboardingData && routerData && (`).
추가 API 호출 불필요.

### 수정 1-C: 타입 import

engine-settings-card.tsx 상단 import에 추가:

```typescript
import type { OnboardingStatusResponse } from "@/lib/api";
```

Props 타입:

```typescript
type EngineSettingsCardProps = {
    initialRouterSettings: RouterSettingsResponse;
    initialNaverStatus?: NaverConnectionStatusResponse | null;
    // 신규
    categoryAllocations?: OnboardingStatusResponse["category_allocations"];
};
```

---

## PATCH 2 (P1): dirty map 기반 병합 로직

### 문제
V1의 `{ ...serverOverrides, ...imageTopicQuotaOverrides }` 병합은 초기 로딩값을 포함한 전체 state를 덮어쓴다.
allocation 카드에서 변경한 서버 최신값을 무시하고 engine 카드의 구버전 값으로 되돌릴 수 있다.

### 수정: dirty key 추적

engine-settings-card.tsx에 dirty 추적 state 추가:

```typescript
// 기존 state 아래에 추가
const [dirtyTopicKeys, setDirtyTopicKeys] = useState<Set<string>>(new Set());
```

handleTopicQuotaChange 함수 교체:

```typescript
function handleTopicQuotaChange(topicMode: string, newQuota: string) {
    setImageTopicQuotaOverrides((prev) => ({
        ...prev,
        [topicMode]: newQuota,
    }));
    setDirtyTopicKeys((prev) => new Set(prev).add(topicMode));
}
```

handleSave 내부 — `image_topic_quota_overrides` 전달 부분 교체:

```typescript
async function handleSave() {
    setSaving(true);
    try {
        // 사용자가 이번 세션에서 편집한 키만 로컬값 사용,
        // 나머지는 서버 최신값(allocation 카드가 저장한 값) 유지
        let finalOverrides = imageTopicQuotaOverrides;
        if (dirtyTopicKeys.size > 0) {
            try {
                const latestSettings = await fetchRouterSettings();
                const serverOverrides =
                    (latestSettings.settings.image_topic_quota_overrides as Record<string, string>) || {};
                // 서버값 기반 + dirty 키만 로컬값으로 덮어쓰기
                finalOverrides = { ...serverOverrides };
                for (const key of dirtyTopicKeys) {
                    finalOverrides[key] = imageTopicQuotaOverrides[key] ?? serverOverrides[key] ?? "0";
                }
            } catch {
                // 서버 조회 실패 시 로컬값 그대로 사용 (기존 동작 유지)
                finalOverrides = imageTopicQuotaOverrides;
            }
        }

        const saved = await saveRouterSettings({
            // ... 기존 필드 그대로 ...
            image_topic_quota_overrides: finalOverrides,
            // ...
        });

        // 저장 후 dirty 초기화
        setDirtyTopicKeys(new Set());

        // ... 기존 후처리 (state 업데이트) ...
    } finally {
        setSaving(false);
    }
}
```

> **`fetchRouterSettings`**: 이미 api.ts에 존재하는지 확인한다.
> 만약 없으면 `/router-settings` GET을 호출하는 함수를 추가한다:
> ```typescript
> export async function fetchRouterSettings(): Promise<RouterSettingsResponse> {
>     const res = await fetch("/api/router-settings");
>     return res.json();
> }
> ```
> 이미 존재한다면 import만 추가한다.

---

## PATCH 3 (P1): 변경 범위 정정

### 문제
V1이 "프론트 1파일만 수정"이라고 선언했지만, 실제로는 3파일 수정이 필요하다.

### 정정된 변경 파일 목록

| 파일 | 변경 유형 | 핵심 변경 |
|------|-----------|-----------|
| `frontend/src/lib/api.ts` | **수정** | `OnboardingStatusResponse.category_allocations` 타입에 `images_per_post`, `ai_images` 추가 |
| `frontend/src/app/settings/page.tsx` | **수정** | `<EngineSettingsCard>` 에 `categoryAllocations` prop 전달 |
| `frontend/src/components/settings/engine-settings-card.tsx` | **수정** | 토픽별 테이블 + min/max UI + dirty map 병합 + props 확장 |

백엔드 변경 없음 (서버 스키마에 이미 `images_per_post`/`ai_images` 필드 존재).

---

## PATCH 4 (P1): API 경로 정정

### 문제
V1이 `/onboarding/status` 경로를 참조했지만, 실제 라우트는 `/onboarding` (alias `/wizard/status`).

### 수정
V1 PART 3에서 "별도 API 호출" 관련 내용 삭제.

**추가 API 호출이 필요 없음.**
settings page (line 122)에서 이미 `onboardingData`를 로딩하고 있다.
이 데이터의 `category_allocations` 필드를 prop으로 전달하면 된다.

PATCH 1-B에서 이미 올바른 prop 전달 코드를 제공.

---

## PATCH 5 (P2): min/max 클램핑 안내 문구 수정

### 문제
V1의 UI 안내 문구가 "카테고리별 설정이 이 범위 내에서 적용됩니다"라고 했지만,
실제 런타임(`runtime_factory.py` line 145-155)은 allocation의 `images_per_post`를 직접 사용하며
`images_per_post_min/max`로 클램핑하지 않는다.
이 값들은 **견적 계산**에만 사용된다.

### 수정: 안내 문구 교체

V1 PART 2의 도움말 텍스트:

```
❌ 삭제: "카테고리별 이미지 설정이 이 범위를 벗어나면 자동으로 클램핑됩니다"
✅ 교체: "견적 비용 계산 시 사용됩니다. 실제 이미지 수는 카테고리 배분 탭의 설정을 따릅니다"
```

JSX:

```tsx
<p className="mt-2 text-xs text-slate-400">
    견적 비용 계산 시 사용됩니다. 실제 이미지 수는 <strong>카테고리 배분</strong> 탭의 설정을 따릅니다
</p>
```

---

## 최종 변경 파일 목록 (V1 + V1.1 합산)

| 파일 | V1 변경 | V1.1 추가 변경 |
|------|---------|---------------|
| `frontend/src/lib/api.ts` | (V1에서 변경 없음 선언) | **PATCH 1**: `OnboardingStatusResponse.category_allocations` 타입 확장 |
| `frontend/src/app/settings/page.tsx` | (V1에서 변경 없음 선언) | **PATCH 1**: `categoryAllocations` prop 전달 |
| `frontend/src/components/settings/engine-settings-card.tsx` | 토픽 테이블 + min/max UI | **PATCH 1**: props 타입 확장, **PATCH 2**: `dirtyTopicKeys` 추적 + 병합 로직, **PATCH 5**: 안내 문구 수정 |

---

## 보정된 구현 순서 (V1.1 반영)

```
Step 1:  api.ts — OnboardingStatusResponse.category_allocations 타입에 images_per_post, ai_images 추가 (PATCH 1-A)
Step 2:  engine-settings-card.tsx — 상수 추가: TOPIC_MODE_LABELS, AI_QUOTA_OPTIONS (V1 그대로)
Step 3:  engine-settings-card.tsx — Props 타입 확장 + categoryAllocations prop 수용 (PATCH 1-C)
Step 4:  engine-settings-card.tsx — dirtyTopicKeys state 추가 (PATCH 2)
Step 5:  engine-settings-card.tsx — topicImagesMap useMemo 추가 (V1 그대로)
Step 6:  engine-settings-card.tsx — handleTopicQuotaChange + dirty 추적 (PATCH 2)
Step 7:  engine-settings-card.tsx — 토픽별 AI 이미지 배분 JSX 삽입 (V1 PART 1-D)
Step 8:  engine-settings-card.tsx — 글로벌 이미지 범위 UI + 수정된 안내 문구 (V1 PART 2 + PATCH 5)
Step 9:  engine-settings-card.tsx — handleSave에서 dirty map 기반 병합 (PATCH 2)
Step 10: settings/page.tsx — categoryAllocations prop 전달 (PATCH 1-B)
Step 11: fetchRouterSettings 함수 존재 확인, 없으면 api.ts에 추가 (PATCH 2)
Step 12: TypeScript 컴파일 확인 (npx tsc --noEmit)
Step 13: ESLint 확인 (npm run lint)
```

---

## 보정된 검증 요구사항

1. **TypeScript**: `npx tsc --noEmit` 에러 0
2. **ESLint**: `npm run lint` 에러 0
3. **타입 안전성**: `OnboardingStatusResponse.category_allocations[0].images_per_post` 접근 시 TS 에러 없음
4. **토픽 테이블 표시**: engine-settings-card의 사진 AI 엔진 섹션에 4개 토픽 행 표시
5. **dirty 병합 시나리오**:
   - allocation 카드에서 cafe AI를 2로 변경 저장
   - engine 카드에서 it AI를 3으로 변경 저장
   - 결과: cafe=2 (allocation 값 유지) + it=3 (engine에서 변경)
6. **min/max 상호 보정**: min > max 설정 시 자동 조정
7. **안내 문구**: "견적 비용 계산 시 사용됩니다" 텍스트 확인
