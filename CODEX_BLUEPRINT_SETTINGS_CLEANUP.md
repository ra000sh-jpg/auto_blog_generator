# CODEX BLUEPRINT — Settings UI 일괄 정리

> **목표**: Settings 페이지에서 발견된 버그·중복·누락 6건을 한 번에 수정한다.
> **변경 파일**: 총 3개
> - `frontend/src/app/settings/page.tsx`
> - `frontend/src/components/settings/engine-settings-card.tsx`
> - `frontend/src/components/settings/allocation-settings-card.tsx`

---

## PATCH 1 — `imageAiQuota` dead state 제거 (P0 버그)

### 문제
`engine-settings-card.tsx` line 72:
```tsx
const [imageAiQuota] = useState<"0" | "1" | "all">("1");
```
setter가 없어 항상 `"1"` 고정. 토픽별 배분 UI가 이미 역할을 대체했으므로 불필요.

### 수정

**engine-settings-card.tsx**:

1. **line 72** — state 선언 삭제:
```tsx
// 삭제: const [imageAiQuota] = useState<"0" | "1" | "all">("1");
```

2. **line 227** (`basePayload` 내부) — `image_ai_quota: imageAiQuota` 항목 삭제:
```tsx
// 변경 전
const basePayload = {
    text_api_keys: compactKeys(textApiKeys),
    image_api_keys: compactKeys(imageApiKeys),
    image_engine: imageEngine,
    image_ai_engine: imageAiEngine,
    image_ai_quota: imageAiQuota,          // ← 삭제
    image_topic_quota_overrides: imageTopicQuotaOverrides,
    ...
};
// 변경 후 — image_ai_quota 라인 제거
```

3. **line 316** (`handleSaveRouterSettings` payload) — 동일하게 `image_ai_quota: imageAiQuota` 삭제:
```tsx
// 변경 전
const saved = await saveRouterSettings({
    strategy_mode: strategyMode,
    ...
    image_ai_quota: imageAiQuota,          // ← 삭제
    image_topic_quota_overrides: finalOverrides,
    ...
});
```

4. **line 271** (`useEffect` deps 배열) — `imageAiQuota` 제거:
```tsx
// 변경 전
}, [strategyMode, textApiKeys, imageApiKeys, imageEngine, imageAiEngine, imageAiQuota, imageTopicQuotaOverrides, ...]);
// 변경 후
}, [strategyMode, textApiKeys, imageApiKeys, imageEngine, imageAiEngine, imageTopicQuotaOverrides, ...]);
```

### 검증
- `imageAiQuota`를 전체 파일에서 검색 → 0건이어야 함
- tsc, lint 통과

---

## PATCH 2 — API Keys / Personas 중복 카드 제거 (P1 불필요)

### 문제
`page.tsx` line 142-188: `data` (fetchConfig)에 의존하는 API Keys 읽기 전용 카드와 Personas 읽기 전용 카드가 있음.
- API Keys 정보는 EngineSettingsCard에서 마스킹 placeholder로 이미 표시
- Personas는 설정 페이지에서 편집 불가 (읽기 전용 정보의 가치 낮음)

### 수정

**page.tsx**:

1. **import에서 `fetchConfig`, `ConfigResponse` 관련 제거**:
```tsx
// 변경 전
import {
  fetchConfig,
  fetchNaverConnectStatus,
  fetchOnboardingStatus,
  fetchRouterSettings,
  type ConfigResponse,
  type NaverConnectStatusResponse,
  type OnboardingStatusResponse,
  type RouterSettingsResponse,
} from "@/lib/api";

// 변경 후 — fetchConfig, type ConfigResponse 제거
import {
  fetchNaverConnectStatus,
  fetchOnboardingStatus,
  fetchRouterSettings,
  type NaverConnectStatusResponse,
  type OnboardingStatusResponse,
  type RouterSettingsResponse,
} from "@/lib/api";
```

2. **state 선언에서 `data` 제거** (line 21):
```tsx
// 삭제: const [data, setData] = useState<ConfigResponse | null>(null);
```

3. **loadConfig 함수에서 configResult 관련 코드 제거** (line 34-50):
```tsx
// 변경 전
const [configResult, onboardingResult, routerResult, naverResult] = await Promise.allSettled([
    fetchConfig(),
    fetchOnboardingStatus(),
    fetchRouterSettings(),
    fetchNaverConnectStatus(),
]);
// configResult 처리 블록 ...

// 변경 후
const [onboardingResult, routerResult, naverResult] = await Promise.allSettled([
    fetchOnboardingStatus(),
    fetchRouterSettings(),
    fetchNaverConnectStatus(),
]);
// configResult if-block 전체 삭제
```

4. **JSX에서 API Keys + Personas 섹션 전체 삭제** (line 142-188):
```tsx
// 삭제: {data && ( ... API Keys section ... Personas section ... )}
```
`{data && (` 로 시작하는 블록 전체(약 46줄) 삭제.

### 검증
- `fetchConfig`를 page.tsx에서 검색 → 0건
- `data.api_keys`, `data.personas` 검색 → 0건
- tsc, lint 통과

---

## PATCH 3 — AllocationSettingsCard 저장 버튼 통합 (P1 비일관)

### 문제
`allocation-settings-card.tsx`에 저장 버튼 2개:
- "💾 할당 비율 저장" (`handleSaveAllocation`) — line 105-129
- "카테고리 매핑 저장" (`handleSaveSchedule`) — line 132-158

둘 다 동일 API(`saveOnboardingSchedule`)에 동일 payload를 전송하지만, 응답 처리가 미묘하게 다름. 사용자에게 혼란.

### 수정

**allocation-settings-card.tsx**:

1. **`handleSaveAllocation` 함수 삭제** (line 105-129 전체 삭제)

2. **관련 state 삭제**:
```tsx
// 삭제: const [savingAllocation, setSavingAllocation] = useState(false);
// 삭제: const [allocationMessage, setAllocationMessage] = useState("");
```
`savingSchedule`와 `scheduleMessage`만 유지.

3. **`handleSaveSchedule` 함수명을 `handleSave`로 변경** 및 메시지 개선:
```tsx
async function handleSave() {
    setSavingSchedule(true);
    setScheduleMessage("");
    try {
        const normalized = normalizeAllocations(
            categoryAllocations.map((item) => item.category),
            100,
            categoryAllocations,
        );
        const response = await saveOnboardingSchedule({
            daily_posts_target: dailyPostsTarget,
            idea_vault_daily_quota: ideaVaultDailyQuota,
            allocations: normalized,
            category_mapping: categoryMapping,
        });
        setCategoryAllocations(withImageDefaults(response.allocations || []));
        setCategoryMapping(response.category_mapping || {});
        setDailyPostsTarget(response.daily_posts_target || 3);
        setIdeaVaultDailyQuota(response.idea_vault_daily_quota || 0);
        setScheduleMessage("✅ 배분 설정이 저장되었습니다.");
        setTimeout(() => setScheduleMessage(""), 3000);
    } catch (requestError) {
        const message = requestError instanceof Error ? requestError.message : "저장에 실패했습니다.";
        setScheduleMessage(message);
    } finally {
        setSavingSchedule(false);
    }
}
```

4. **JSX — 할당 비율 저장 영역(line 309-380)의 저장 버튼을 삭제**. 구체적으로:
   - line 363-379의 `<div className="flex items-center justify-end ...">` 블록(allocationMessage 표시 + "💾 할당 비율 저장" 버튼) → **전체 삭제**

5. **JSX — 하단 저장 버튼(line 426-441) 업데이트**:
```tsx
// 변경 전
<button ... onClick={handleSaveSchedule} ...>
    {savingSchedule ? "저장 중..." : "카테고리 매핑 저장"}
</button>

// 변경 후
<button
    type="button"
    onClick={handleSave}
    disabled={savingSchedule}
    className="rounded-full bg-slate-900 px-5 py-2.5 text-sm font-medium text-white shadow-sm transition hover:bg-slate-700 disabled:opacity-50"
>
    {savingSchedule ? "저장 중..." : "💾 배분 설정 저장"}
</button>
```

6. **scheduleMessage 표시 위치를 저장 버튼 바로 왼쪽으로 유지** (기존 위치 그대로):
```tsx
{scheduleMessage && (
    <span className={`text-xs font-medium ${
        scheduleMessage.includes("✅") ? "text-emerald-600" : "text-rose-500"
    }`}>
        {scheduleMessage}
    </span>
)}
```

### 검증
- `handleSaveAllocation` 검색 → 0건
- `savingAllocation`, `allocationMessage` 검색 → 0건
- "할당 비율 저장" 문자열 검색 → 0건
- tsc, lint 통과

---

## PATCH 4 — champion_history 주간 이력 테이블 추가 (P2 누락)

### 문제
`engine-settings-card.tsx`에서 `championHistory` state를 로드하지만 주간별 이력을 표시하는 테이블이 없음.

### 수정

**engine-settings-card.tsx** — 주간 모델 경쟁 상태 섹션(`mt-4 rounded-xl border border-blue-100`) 아래, 도전자 설정 `</div>` 닫힌 후 `</div>` (경쟁 카드 닫힘) 직전에 이력 테이블 삽입.

구체적으로 line 840 (`</div>`) 직후 ~ line 841 (`</div>`) 직전에 삽입:

```tsx
{/* 도전자 설정 아래, 경쟁 카드 닫기 전에 삽입 */}
{championHistory.length > 0 && (
    <div className="mt-3 border-t border-blue-100 pt-3">
        <p className="mb-2 text-xs font-semibold text-blue-900">챔피언 이력</p>
        <div className="overflow-x-auto">
            <table className="min-w-full text-xs">
                <thead>
                    <tr className="border-b border-blue-100 text-left text-[10px] uppercase tracking-wide text-blue-700">
                        <th className="py-1.5 pr-3">주차</th>
                        <th className="py-1.5 pr-3">챔피언</th>
                        <th className="py-1.5 pr-3">도전자</th>
                        <th className="py-1.5 pr-3 text-right">평균 점수</th>
                        <th className="py-1.5 pr-3 text-right">비용(₩)</th>
                        <th className="py-1.5 pr-3">토픽별 점수</th>
                        <th className="py-1.5">비고</th>
                    </tr>
                </thead>
                <tbody>
                    {championHistory.slice(0, 8).map((h, idx) => (
                        <tr key={`${h.week_start}-${idx}`} className="border-b border-blue-50">
                            <td className="py-1.5 pr-3 font-medium text-blue-900">{h.week_start}</td>
                            <td className="py-1.5 pr-3 text-blue-800">{h.champion_model}</td>
                            <td className="py-1.5 pr-3 text-slate-600">{h.challenger_model || "—"}</td>
                            <td className="py-1.5 pr-3 text-right font-semibold text-blue-900">
                                {h.avg_champion_score.toFixed(1)}
                            </td>
                            <td className="py-1.5 pr-3 text-right text-slate-600">
                                {h.cost_won > 0 ? `${formatKrw(h.cost_won)}` : "—"}
                            </td>
                            <td className="py-1.5 pr-3">
                                <div className="flex flex-wrap gap-1">
                                    {Object.entries(h.topic_mode_scores || {}).map(([topic, score]) => (
                                        <span
                                            key={topic}
                                            className="rounded-full bg-blue-100 px-1.5 py-0.5 text-[9px] text-blue-700"
                                        >
                                            {topic} {Number(score).toFixed(1)}
                                        </span>
                                    ))}
                                </div>
                            </td>
                            <td className="py-1.5">
                                {h.early_terminated && (
                                    <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[9px] text-amber-700">조기종료</span>
                                )}
                                {h.shadow_only && (
                                    <span className="ml-1 rounded-full bg-slate-200 px-1.5 py-0.5 text-[9px] text-slate-600">shadow</span>
                                )}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
        {championHistory.length > 8 && (
            <p className="mt-1 text-[10px] text-blue-500">최근 8주만 표시됩니다.</p>
        )}
    </div>
)}
```

### 삽입 위치 정확한 지정

현재 코드 구조:
```tsx
{/* line ~840 */}
                </div>       {/* ← 도전자 설정 label 닫힘 */}
            </div>           {/* ← 주간 모델 경쟁 상태 카드 닫힘 */}
```

**도전자 설정 `</div>` 와 경쟁 카드 `</div>` 사이에 삽입**한다.

즉, 도전자 설정의 `</label>` 다음줄 `</div>` (border-t border-blue-100 pt-3 닫힘) 바로 다음,
경쟁 카드의 닫는 `</div>` 바로 전에 넣는다.

### 검증
- `championHistory.length > 0` 조건으로 데이터 없으면 미표시
- `formatKrw`는 이미 import되어 있음 (line 16)
- tsc, lint 통과

---

## PATCH 5 — 에러 시 기형 레이아웃 방지 (P3 버그)

### 문제
`page.tsx`에서 `onboardingData && routerData` 조건과 `data` 조건이 별도 블록.
`fetchConfig`만 성공하고 나머지 실패 시 API Keys/Personas만 보이는 기형 레이아웃.

### 수정
PATCH 2에서 `data` 블록을 이미 삭제하므로, 이 문제는 **PATCH 2로 자동 해결**됨.

추가로, 에러 메시지 위치를 개선:

**page.tsx** — 에러 표시 블록(line 116-120)을 카드 영역 위로 이동하되, `loading`이 false일 때만 표시하도록 조건 수정:
```tsx
// 변경 전
{error && (
    <p className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
        {error}
    </p>
)}

// 변경 후
{!loading && error && (
    <p className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
        {error}
    </p>
)}
```

### 검증
- 로딩 중에는 에러 메시지 미표시
- tsc, lint 통과

---

## 최종 검증 체크리스트

```bash
# 1. TypeScript 컴파일
cd frontend && npx tsc --noEmit

# 2. ESLint
cd frontend && npx eslint src/app/settings/page.tsx src/components/settings/engine-settings-card.tsx src/components/settings/allocation-settings-card.tsx

# 3. 삭제 확인 grep
grep -r "imageAiQuota" frontend/src/           # → 0건
grep -r "fetchConfig" frontend/src/app/settings/  # → 0건
grep -r "handleSaveAllocation" frontend/src/   # → 0건
grep -r "savingAllocation" frontend/src/       # → 0건
grep -r "allocationMessage" frontend/src/      # → 0건
grep -r "data\.api_keys" frontend/src/         # → 0건
grep -r "data\.personas" frontend/src/         # → 0건
```

## 변경 파일 요약

| 파일 | PATCH | 변경 내용 |
|------|-------|-----------|
| `engine-settings-card.tsx` | 1, 4 | `imageAiQuota` 제거 + champion_history 테이블 추가 |
| `settings/page.tsx` | 2, 5 | fetchConfig/data 제거, API Keys·Personas 섹션 삭제, 에러 조건 개선 |
| `allocation-settings-card.tsx` | 3 | 저장 버튼 2개→1개 통합, 중복 state/함수 삭제 |
