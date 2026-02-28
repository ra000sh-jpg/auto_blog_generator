# Codex Blueprint: 토픽별 이미지 배분 UI 완성

## 목표
`engine-settings-card.tsx`에 숨어있는 토픽별 이미지 설정(`imageTopicQuotaOverrides`, `imagesPerPostMin/Max`)을 **사용자에게 보이는 UI**로 노출하고, 토픽별 AI 쿼터를 직접 편집할 수 있게 한다.

---

## 현재 구조와 문제점

### 현재 동작
1. **allocation-settings-card.tsx** — 카테고리별 `images_per_post` (0-4장), `ai_images` (0-n장) 편집 가능
2. **onboarding.py** `_sync_image_settings_from_allocations()` — 저장 시 `ai_images` → `router_image_topic_quota_overrides` 자동 변환
3. **engine-settings-card.tsx** — `imageTopicQuotaOverrides` 상태값을 서버에서 읽고, 저장 시 그대로 전달하지만 **UI에 표시하지 않음**

### 갭 (3건)
| 갭 | 설명 |
|----|------|
| **GAP-1: 토픽 쿼터 UI 없음** | `imageTopicQuotaOverrides`가 state에만 존재, 사용자가 볼 수도 편집할 수도 없음 |
| **GAP-2: 글로벌 min/max UI 없음** | `imagesPerPostMin/Max`가 state에만 존재, UI 미노출 |
| **GAP-3: 양방향 싱크 없음** | allocation 카드에서 편집한 ai_images와 engine 카드의 topic overrides가 서로 연동되지 않음 — 같은 페이지에서 engine 카드 저장 시 allocation 변경사항이 덮어씌워질 수 있음 |

---

## 변경 범위

### 파일 목록

| 파일 | 변경 유형 | 핵심 변경 |
|------|-----------|-----------|
| `frontend/src/components/settings/engine-settings-card.tsx` | **수정** | 토픽별 이미지 테이블 UI 추가 + 글로벌 min/max 편집 UI 추가 |
| `frontend/src/lib/api.ts` | 확인만 | `image_topic_quota_overrides` 타입 이미 존재 — 변경 없음 |
| `server/routers/router_settings.py` | 확인만 | save 엔드포인트가 `image_topic_quota_overrides`를 이미 수용 — 변경 없음 |
| `modules/llm/llm_router.py` | 확인만 | save_settings()가 이미 처리 — 변경 없음 |

**백엔드 변경 없음. 프론트엔드 1파일만 수정.**

---

## PART 1: 토픽별 이미지 배분 테이블 UI

### 위치
`engine-settings-card.tsx`의 **사진 AI 엔진** 섹션(`imageEnabled && (...)` 블록) 내부, 이미지 API 키 입력 필드 바로 위에 삽입한다.

라인 479 (`</div>` — AI 생성 엔진 라디오 그룹 닫는 태그) 바로 아래에 추가:

### UI 레이아웃

```
┌─ 토픽별 AI 이미지 배분 ────────────────────────────────┐
│                                                         │
│  토픽         이미지/포스트    AI 이미지 쿼터            │
│  ─────────────────────────────────────────────────────   │
│  카페/일상     2장 (읽기전용)   [드롭다운: 0|1|2|all]    │
│  IT            2장 (읽기전용)   [드롭다운: 0|1|2|all]    │
│  경제          1장 (읽기전용)   [드롭다운: 0|1|2|all]    │
│  육아          1장 (읽기전용)   [드롭다운: 0|1|2|all]    │
│                                                         │
│  💡 이미지/포스트는 카테고리 배분 탭에서 편집하세요       │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 구현 상세

#### 1-A: 토픽 정의 상수

파일 상단(또는 컴포넌트 바깥)에 추가:

```typescript
const TOPIC_MODE_LABELS: Record<string, string> = {
    cafe: "카페/일상",
    it: "IT",
    finance: "경제",
    parenting: "육아",
};

const AI_QUOTA_OPTIONS = [
    { value: "0", label: "없음" },
    { value: "1", label: "1장" },
    { value: "2", label: "2장" },
    { value: "3", label: "3장" },
    { value: "4", label: "4장" },
    { value: "all", label: "전부 AI" },
] as const;
```

#### 1-B: 토픽별 images_per_post 로딩

engine-settings-card.tsx는 현재 allocation 데이터를 갖고 있지 않다.
**prop으로 전달하거나, 별도 API 호출**이 필요하다.

**방안: prop 전달 (추천)**

engine-settings-card의 부모 컴포넌트(settings page)에서 `initialOnboardingStatus`를 이미 로딩하고 있으므로, allocation 데이터를 prop으로 전달한다.

```typescript
// engine-settings-card.tsx props 확장
type EngineSettingsCardProps = {
    initialRouterSettings: RouterSettingsResponse;
    // 신규: 카테고리 할당 정보 (이미지/포스트 표시용)
    categoryAllocations?: ScheduleAllocationItem[];
};
```

**부모 컴포넌트**에서:
```typescript
<EngineSettingsCard
    initialRouterSettings={routerSettings}
    categoryAllocations={onboardingStatus.allocations}
/>
```

이미 `ScheduleAllocationItem`에 `images_per_post`와 `topic_mode`가 포함되어 있으므로, 토픽별 images_per_post를 추출한다:

```typescript
const topicImagesMap = useMemo(() => {
    const map: Record<string, number> = {};
    for (const item of categoryAllocations || []) {
        const topic = item.topic_mode || "cafe";
        const current = map[topic] ?? 0;
        const itemImages = Math.max(0, Math.min(4, Number(item.images_per_post ?? 2)));
        map[topic] = Math.max(current, itemImages); // 같은 토픽 복수 카테고리 시 최대값
    }
    return map;
}, [categoryAllocations]);
```

#### 1-C: 드롭다운 onChange 핸들러

```typescript
function handleTopicQuotaChange(topicMode: string, newQuota: string) {
    setImageTopicQuotaOverrides((prev) => ({
        ...prev,
        [topicMode]: newQuota,
    }));
}
```

#### 1-D: JSX 테이블

```tsx
{/* 토픽별 AI 이미지 배분 */}
<div className="rounded-xl border border-slate-200 bg-white p-4">
    <p className="mb-3 text-sm font-semibold text-slate-800">
        토픽별 AI 이미지 배분
    </p>
    <div className="space-y-2">
        {Object.entries(TOPIC_MODE_LABELS).map(([topicMode, label]) => {
            const imagesPerPost = topicImagesMap[topicMode] ?? imagesPerPostMax;
            const currentQuota = imageTopicQuotaOverrides[topicMode] || "0";
            // AI 쿼터 옵션을 images_per_post에 맞게 필터
            const maxAiNum = imagesPerPost;
            const filteredOptions = AI_QUOTA_OPTIONS.filter(
                (opt) => opt.value === "0" || opt.value === "all" || Number(opt.value) <= maxAiNum
            );
            return (
                <div
                    key={topicMode}
                    className="flex items-center gap-4 rounded-lg border border-slate-100 bg-slate-50 px-3 py-2"
                >
                    <span className="w-20 text-sm font-medium text-slate-700">{label}</span>
                    <span className="flex items-center gap-1 text-xs text-slate-500">
                        📷 {imagesPerPost}장/포스트
                    </span>
                    <select
                        value={currentQuota}
                        onChange={(e) => handleTopicQuotaChange(topicMode, e.target.value)}
                        className="ml-auto rounded-lg border border-slate-300 px-2 py-1 text-xs"
                    >
                        {filteredOptions.map((opt) => (
                            <option key={opt.value} value={opt.value}>
                                AI {opt.label}
                            </option>
                        ))}
                    </select>
                </div>
            );
        })}
    </div>
    <p className="mt-2 text-xs text-slate-400">
        💡 이미지/포스트 수는 <strong>카테고리 배분</strong> 탭에서 편집하세요
    </p>
</div>
```

---

## PART 2: 글로벌 이미지 범위(min/max) UI

### 위치
토픽별 AI 이미지 배분 테이블 **바로 아래**, 이미지 API 키 위에 삽입.

### UI 레이아웃

```
┌─ 글로벌 이미지 범위 ──────────────────────────────────┐
│                                                        │
│  포스트당 이미지 범위:  [min ▼] ~ [max ▼]              │
│                                                        │
│  💡 카테고리별 설정이 이 범위 내에서 적용됩니다         │
│                                                        │
└────────────────────────────────────────────────────────┘
```

### 구현 상세

```tsx
<div className="rounded-xl border border-slate-200 bg-white p-4">
    <p className="mb-3 text-sm font-semibold text-slate-800">
        포스트당 이미지 범위
    </p>
    <div className="flex items-center gap-3">
        <label className="flex items-center gap-2 text-xs text-slate-600">
            최소
            <select
                value={imagesPerPostMin}
                onChange={(e) => {
                    const val = Number(e.target.value);
                    setImagesPerPostMin(val);
                    if (val > imagesPerPostMax) setImagesPerPostMax(val);
                }}
                className="rounded-lg border border-slate-300 px-2 py-1 text-xs"
            >
                {[0, 1, 2, 3, 4].map((n) => (
                    <option key={n} value={n}>{n}장</option>
                ))}
            </select>
        </label>
        <span className="text-slate-400">~</span>
        <label className="flex items-center gap-2 text-xs text-slate-600">
            최대
            <select
                value={imagesPerPostMax}
                onChange={(e) => {
                    const val = Number(e.target.value);
                    setImagesPerPostMax(val);
                    if (val < imagesPerPostMin) setImagesPerPostMin(val);
                }}
                className="rounded-lg border border-slate-300 px-2 py-1 text-xs"
            >
                {[0, 1, 2, 3, 4].map((n) => (
                    <option key={n} value={n}>{n}장</option>
                ))}
            </select>
        </label>
    </div>
    <p className="mt-2 text-xs text-slate-400">
        카테고리별 이미지 설정이 이 범위를 벗어나면 자동으로 클램핑됩니다
    </p>
</div>
```

---

## PART 3: 부모 컴포넌트(Settings Page)에서 prop 전달

### 파일: settings page (engine-settings-card의 부모)

부모 파일을 찾아서 `EngineSettingsCard`가 렌더되는 위치를 확인한 뒤, `categoryAllocations` prop을 추가한다.

**찾는 방법**:
```bash
grep -rn "EngineSettingsCard" frontend/src --include="*.tsx"
```

부모가 이미 `onboardingStatus` (또는 `initialOnboardingStatus`)를 갖고 있다면:

```tsx
<EngineSettingsCard
    initialRouterSettings={routerSettings}
    categoryAllocations={onboardingStatus.allocations || []}
/>
```

부모가 `onboardingStatus`를 갖고 있지 않다면, settings page 로더에서 `/onboarding/status` API를 추가 호출하여 `allocations` 데이터를 가져온다.

---

## PART 4: 양방향 싱크 보강

### 문제
allocation-settings-card에서 `ai_images`를 편집 → 저장하면 `router_image_topic_quota_overrides`가 갱신된다.
하지만 engine-settings-card의 `imageTopicQuotaOverrides` state는 **마운트 시 한 번만 로딩**되므로, 같은 세션에서 allocation을 변경 후 engine 카드를 저장하면 **구버전 overrides로 덮어쓸 수 있다**.

### 해결: engine 카드 저장 전 최신 allocation 기반으로 overrides 병합

**방안 A (추천): engine 카드 저장 시 서버의 최신 overrides를 먼저 읽은 뒤 병합**

`handleSave()` 함수 내부 수정:

```typescript
async function handleSave() {
    setSaving(true);
    try {
        // 최신 서버 상태 읽기 — allocation 카드가 이미 저장한 overrides 포함
        const currentSettings = await fetchRouterSettings();
        const serverOverrides =
            (currentSettings.settings.image_topic_quota_overrides as Record<string, string>) || {};

        // 사용자가 이 세션에서 직접 편집한 overrides만 우선 적용
        // 나머지는 서버 값(allocation 카드에서 갱신한 값) 유지
        const mergedOverrides = { ...serverOverrides, ...imageTopicQuotaOverrides };

        const saved = await saveRouterSettings({
            // ... 기존 필드 그대로 ...
            image_topic_quota_overrides: mergedOverrides,
            // ...
        });
        // ... 기존 후처리 ...
    } finally {
        setSaving(false);
    }
}
```

> **주의**: `fetchRouterSettings` 가 `/router-settings` GET 엔드포인트를 호출하는 함수이다. 이미 api.ts에 존재하는지 확인하고, 없으면 추가한다.

**방안 B (간단): engine 카드에서 topic overrides를 readonly로만 표시 (편집 불가)**

이 경우 topic overrides UI는 `<select disabled>` 또는 텍스트 표시로만 렌더하고, 편집은 allocation-settings-card에서만 가능하게 한다.

**두 방안 중 하나를 선택:**
- 사용자가 engine 카드에서 topic AI 쿼터를 직접 편집하려면 → **방안 A**
- allocation 카드에서만 편집, engine 카드는 요약 표시만 → **방안 B**

**방안 A를 기본으로 구현하고**, 사용성 문제가 생기면 방안 B로 전환 가능하도록 `editable` prop을 컴포넌트에 추가한다.

---

## 구현 순서

```
Step 1: engine-settings-card.tsx 상단에 TOPIC_MODE_LABELS, AI_QUOTA_OPTIONS 상수 추가
Step 2: EngineSettingsCardProps 타입에 categoryAllocations?: ScheduleAllocationItem[] 추가
Step 3: topicImagesMap useMemo 추가
Step 4: handleTopicQuotaChange 함수 추가
Step 5: 사진 AI 엔진 섹션 내부에 토픽별 AI 이미지 배분 JSX 삽입 (PART 1-D)
Step 6: 글로벌 이미지 범위 UI 삽입 (PART 2)
Step 7: 부모 컴포넌트에서 categoryAllocations prop 전달 (PART 3)
Step 8: handleSave에서 최신 overrides 병합 로직 추가 (PART 4 방안 A)
Step 9: TypeScript 컴파일 확인 (npx tsc --noEmit)
Step 10: ESLint 확인 (npm run lint)
```

---

## 검증 요구사항

1. **TypeScript**: `npx tsc --noEmit` 에러 0
2. **ESLint**: `npm run lint` 에러 0
3. **UI 확인**: engine-settings-card의 사진 AI 엔진 섹션에 토픽별 테이블이 표시됨
4. **편집 확인**: 토픽 AI 쿼터 드롭다운 변경 → 저장 → 새로고침 → 변경값 유지
5. **min/max 확인**: 글로벌 이미지 범위 min > max 설정 시 자동 보정
6. **싱크 확인**: allocation 카드에서 ai_images 변경 저장 → engine 카드 새로고침 → 토픽 쿼터 값 반영

---

## 실제 코드 참조 (정확한 함수명/라인)

| 파일 | 위치 | 내용 |
|------|------|------|
| `engine-settings-card.tsx` | line 56-58 | `imageTopicQuotaOverrides` state — 여기에 UI 연결 |
| `engine-settings-card.tsx` | line 62-69 | `imagesPerPostMin/Max` state — 여기에 UI 연결 |
| `engine-settings-card.tsx` | line 439 | `imageEnabled && (` — 이 블록 내부에 UI 삽입 |
| `engine-settings-card.tsx` | line 479 | AI 엔진 라디오 그룹 끝 — 토픽 테이블 삽입 지점 |
| `engine-settings-card.tsx` | line 480 | 이미지 API 키 시작 — 그 위에 min/max 삽입 |
| `allocation-settings-card.tsx` | line 261-302 | 카테고리별 images_per_post / ai_images 편집 UI (기존 — 변경 없음) |
| `onboarding.py` | line 52-86 | `_sync_image_settings_from_allocations()` (기존 — 변경 없음) |
| `modules/llm/llm_router.py` | line 207-213 | `DEFAULT_IMAGE_TOPIC_QUOTA_OVERRIDES` (기존 — 변경 없음) |
| `modules/images/runtime_factory.py` | line 78-97 | `_resolve_ai_quota_for_topic()` (기존 — 변경 없음) |
| `frontend/src/lib/api.ts` | line 371-385 | `RouterSettingsPayload` 타입 (기존 — 변경 없음) |
| `frontend/src/lib/api.ts` | line 238-245 | `ScheduleAllocationItem` 타입 (기존 — import 필요) |
