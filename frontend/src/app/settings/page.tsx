"use client";

import { useEffect, useMemo, useState } from "react";

import {
  fetchConfig,
  fetchOnboardingStatus,
  saveOnboardingSchedule,
  type ConfigResponse,
  type ScheduleAllocationItem,
} from "@/lib/api";

const TOPIC_OPTIONS = [
  { value: "cafe", label: "Cafe" },
  { value: "it", label: "IT" },
  { value: "parenting", label: "Parenting" },
  { value: "finance", label: "Finance" },
];

function inferTopicMode(categoryName: string): string {
  const lowered = categoryName.toLowerCase();
  if (["경제", "finance", "투자", "주식", "재테크"].some((token) => lowered.includes(token))) {
    return "finance";
  }
  if (["it", "개발", "코드", "자동화", "ai", "테크"].some((token) => lowered.includes(token))) {
    return "it";
  }
  if (["육아", "아이", "부모", "가정"].some((token) => lowered.includes(token))) {
    return "parenting";
  }
  return "cafe";
}

function normalizeAllocations(
  categories: string[],
  target: number,
  existingAllocations: ScheduleAllocationItem[] = [],
): ScheduleAllocationItem[] {
  const normalizedCategories = categories
    .map((value) => value.trim())
    .filter((value, index, list) => value.length > 0 && list.indexOf(value) === index);
  const fallbackCategories = normalizedCategories.length > 0 ? normalizedCategories : ["다양한 생각"];

  const existingMap = new Map(existingAllocations.map((item) => [item.category, item]));
  const rows: ScheduleAllocationItem[] = fallbackCategories.map((categoryName) => {
    const existing = existingMap.get(categoryName);
    return {
      category: categoryName,
      topic_mode: existing?.topic_mode || inferTopicMode(categoryName),
      count: Math.max(0, Number(existing?.count || 0)),
    };
  });

  const safeTarget = Math.max(0, target);
  if (safeTarget <= 0) {
    return rows.map((item) => ({
      ...item,
      count: 0,
    }));
  }

  let total = rows.reduce((acc, item) => acc + item.count, 0);
  if (total <= 0) {
    for (let index = 0; index < safeTarget; index += 1) {
      rows[index % rows.length].count += 1;
    }
    return rows;
  }

  if (total < safeTarget) {
    rows[0].count += safeTarget - total;
    return rows;
  }

  if (total > safeTarget) {
    let overflow = total - safeTarget;
    for (let index = rows.length - 1; index >= 0; index -= 1) {
      if (overflow <= 0) {
        break;
      }
      const deductible = Math.min(rows[index].count, overflow);
      rows[index].count -= deductible;
      overflow -= deductible;
    }
  }

  total = rows.reduce((acc, item) => acc + item.count, 0);
  if (total !== safeTarget) {
    rows[0].count += safeTarget - total;
  }

  return rows;
}

export default function SettingsPage() {
  const [data, setData] = useState<ConfigResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [dailyPostsTarget, setDailyPostsTarget] = useState(3);
  const [ideaVaultDailyQuota, setIdeaVaultDailyQuota] = useState(2);
  const [categoryAllocations, setCategoryAllocations] = useState<ScheduleAllocationItem[]>([]);
  const [scheduleMessage, setScheduleMessage] = useState("");
  const [savingSchedule, setSavingSchedule] = useState(false);

  useEffect(() => {
    let isMounted = true;

    async function loadConfig() {
      try {
        const [configResponse, onboardingResponse] = await Promise.all([
          fetchConfig(),
          fetchOnboardingStatus(),
        ]);
        if (!isMounted) {
          return;
        }

        setData(configResponse);
        const resolvedTarget = Math.max(3, Math.min(5, Number(onboardingResponse.daily_posts_target || 3)));
        const resolvedIdeaVaultQuota = Math.max(
          0,
          Math.min(
            resolvedTarget,
            Number(onboardingResponse.idea_vault_daily_quota ?? Math.min(2, resolvedTarget)),
          ),
        );
        const categoryPool =
          onboardingResponse.categories.length > 0
            ? onboardingResponse.categories
            : onboardingResponse.recommended_categories;

        setDailyPostsTarget(resolvedTarget);
        setIdeaVaultDailyQuota(resolvedIdeaVaultQuota);
        setCategoryAllocations(
          normalizeAllocations(
            categoryPool,
            Math.max(0, resolvedTarget - resolvedIdeaVaultQuota),
            onboardingResponse.category_allocations || [],
          ),
        );
      } catch (requestError) {
        if (!isMounted) {
          return;
        }
        const message =
          requestError instanceof Error
            ? requestError.message
            : "설정 정보를 불러오지 못했습니다.";
        setError(message);
      } finally {
        if (isMounted) {
          setLoading(false);
        }
      }
    }

    loadConfig();
    return () => {
      isMounted = false;
    };
  }, []);

  const trendDailyTarget = useMemo(
    () => Math.max(0, dailyPostsTarget - ideaVaultDailyQuota),
    [dailyPostsTarget, ideaVaultDailyQuota],
  );
  const allocationTotal = useMemo(
    () => categoryAllocations.reduce((acc, item) => acc + Math.max(0, Number(item.count || 0)), 0),
    [categoryAllocations],
  );

  function handleDailyTargetChange(nextTarget: number) {
    const normalizedTarget = Math.max(3, Math.min(5, nextTarget));
    const normalizedQuota = Math.max(0, Math.min(normalizedTarget, ideaVaultDailyQuota));
    const adjustedTrendTarget = Math.max(0, normalizedTarget - normalizedQuota);
    const currentCategories = categoryAllocations.map((item) => item.category);

    setDailyPostsTarget(normalizedTarget);
    setIdeaVaultDailyQuota(normalizedQuota);
    setCategoryAllocations(
      normalizeAllocations(currentCategories, adjustedTrendTarget, categoryAllocations),
    );
  }

  function handleIdeaVaultQuotaChange(nextQuota: number) {
    const normalizedQuota = Math.max(0, Math.min(dailyPostsTarget, nextQuota));
    const adjustedTrendTarget = Math.max(0, dailyPostsTarget - normalizedQuota);
    const currentCategories = categoryAllocations.map((item) => item.category);
    setIdeaVaultDailyQuota(normalizedQuota);
    setCategoryAllocations(
      normalizeAllocations(currentCategories, adjustedTrendTarget, categoryAllocations),
    );
  }

  function handleAllocationChange(index: number, patch: Partial<ScheduleAllocationItem>) {
    setCategoryAllocations((previous) => {
      const next = [...previous];
      const current = next[index];
      if (!current) {
        return previous;
      }
      const count =
        patch.count === undefined ? current.count : Math.max(0, Math.min(5, Number(patch.count || 0)));
      const topicMode =
        patch.topic_mode === undefined ? current.topic_mode : String(patch.topic_mode || "cafe");
      next[index] = {
        ...current,
        ...patch,
        count,
        topic_mode: topicMode,
      };
      return next;
    });
  }

  async function handleSaveSchedule() {
    setSavingSchedule(true);
    setScheduleMessage("");
    try {
      const normalized = normalizeAllocations(
        categoryAllocations.map((item) => item.category),
        trendDailyTarget,
        categoryAllocations,
      );
      const response = await saveOnboardingSchedule({
        daily_posts_target: dailyPostsTarget,
        idea_vault_daily_quota: ideaVaultDailyQuota,
        allocations: normalized,
      });
      setDailyPostsTarget(response.daily_posts_target);
      setIdeaVaultDailyQuota(response.idea_vault_daily_quota);
      setCategoryAllocations(
        normalizeAllocations(
          response.allocations.map((item) => item.category),
          Math.max(0, response.daily_posts_target - response.idea_vault_daily_quota),
          response.allocations,
        ),
      );
      setScheduleMessage("스케줄 설정이 저장되었습니다.");
    } catch (requestError) {
      const message =
        requestError instanceof Error ? requestError.message : "스케줄 설정 저장에 실패했습니다.";
      setScheduleMessage(message);
    } finally {
      setSavingSchedule(false);
    }
  }

  return (
    <div className="space-y-4">
      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <h1 className="font-[family-name:var(--font-heading)] text-2xl font-semibold tracking-tight">
          Settings
        </h1>
        <p className="mt-1 text-sm text-slate-600">
          API 키 상태와 스케줄러 배분(총 발행량/Idea Vault 할당량/카테고리 비율)을 수정합니다.
        </p>
      </section>

      {loading && (
        <p className="rounded-xl bg-slate-50 px-3 py-2 text-sm text-slate-600">
          설정 정보를 불러오는 중입니다...
        </p>
      )}

      {error && (
        <p className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </p>
      )}

      {!loading && !error && (
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
            Scheduler Allocation
          </h2>
          <p className="mt-1 text-sm text-slate-600">
            하루 총 발행량과 Idea Vault 사용량을 먼저 정한 뒤, 남은 트렌드 슬롯을 카테고리에 배분하세요.
          </p>

          <label className="mt-4 block rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div className="flex items-center justify-between text-sm">
              <span>하루 총 발행량</span>
              <span className="font-semibold">{dailyPostsTarget}편</span>
            </div>
            <input
              type="range"
              min={3}
              max={5}
              value={dailyPostsTarget}
              onChange={(event) => handleDailyTargetChange(Number(event.target.value))}
              className="mt-2 w-full"
            />
          </label>

          <label className="mt-3 block rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div className="flex items-center justify-between text-sm">
              <span>창고 아이디어(Idea Vault) 하루 사용량</span>
              <span className="font-semibold">{ideaVaultDailyQuota}편</span>
            </div>
            <input
              type="range"
              min={0}
              max={dailyPostsTarget}
              value={ideaVaultDailyQuota}
              onChange={(event) => handleIdeaVaultQuotaChange(Number(event.target.value))}
              className="mt-2 w-full"
            />
            <p className="mt-1 text-xs text-slate-600">
              남은 트렌드 슬롯: <strong>{trendDailyTarget}</strong>편
            </p>
          </label>

          <div className="mt-4 rounded-xl border border-slate-200">
            <div className="grid grid-cols-12 border-b border-slate-200 bg-slate-50 px-3 py-2 text-xs font-medium text-slate-600">
              <div className="col-span-5">Category</div>
              <div className="col-span-4">Topic Mode</div>
              <div className="col-span-3">할당량</div>
            </div>
            <div className="divide-y divide-slate-200">
              {categoryAllocations.map((item, index) => (
                <div key={item.category} className="grid grid-cols-12 items-center gap-2 px-3 py-2">
                  <div className="col-span-5 text-sm text-slate-800">{item.category}</div>
                  <div className="col-span-4">
                    <select
                      value={item.topic_mode}
                      onChange={(event) => handleAllocationChange(index, { topic_mode: event.target.value })}
                      className="w-full rounded-lg border border-slate-300 px-2 py-1 text-xs"
                    >
                      {TOPIC_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="col-span-3">
                    <input
                      type="number"
                      min={0}
                      max={5}
                      value={item.count}
                      onChange={(event) =>
                        handleAllocationChange(index, { count: Number(event.target.value) })
                      }
                      className="w-full rounded-lg border border-slate-300 px-2 py-1 text-sm"
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-slate-600">
              현재 트렌드 할당 합계: <strong>{allocationTotal}</strong> / 목표{" "}
              <strong>{trendDailyTarget}</strong>
            </p>
            <button
              type="button"
              onClick={() =>
                setCategoryAllocations(
                  normalizeAllocations(
                    categoryAllocations.map((item) => item.category),
                    trendDailyTarget,
                    [],
                  ),
                )
              }
              className="rounded-full border border-slate-300 px-3 py-1 text-xs font-medium text-slate-700 transition hover:border-slate-500"
            >
              균등 분배 자동 맞춤
            </button>
          </div>

          {allocationTotal !== trendDailyTarget && (
            <p className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
              할당량 합계가 목표와 다릅니다. 저장 시 자동 보정됩니다.
            </p>
          )}

          {trendDailyTarget <= 0 && (
            <p className="mt-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700">
              오늘 발행량이 모두 Idea Vault로 배정되었습니다. 트렌드 카테고리 배분은 0으로 저장됩니다.
            </p>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={handleSaveSchedule}
              disabled={savingSchedule || (trendDailyTarget > 0 && categoryAllocations.length === 0)}
              className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50"
            >
              {savingSchedule ? "저장 중..." : "스케줄 설정 저장"}
            </button>
          </div>

          {scheduleMessage && (
            <p className="mt-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">
              {scheduleMessage}
            </p>
          )}
        </section>
      )}

      {data && (
        <>
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
              API Keys
            </h2>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              {data.api_keys.map((item) => (
                <article key={item.provider} className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                  <p className="text-xs uppercase tracking-wide text-slate-500">{item.provider}</p>
                  <p className="mt-1 text-sm font-medium text-slate-900">{item.env_var}</p>
                  <p className="mt-2 text-xs text-slate-600">
                    상태: {item.configured ? "연결됨" : "미설정"}
                  </p>
                  <p className="mt-1 text-xs text-slate-600">
                    값: {item.configured ? item.masked : "-"}
                  </p>
                </article>
              ))}
            </div>
          </section>

          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
              Personas
            </h2>
            <div className="mt-3 overflow-x-auto rounded-xl border border-slate-200">
              <table className="min-w-full text-sm">
                <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-600">
                  <tr>
                    <th className="px-3 py-2">ID</th>
                    <th className="px-3 py-2">Label</th>
                    <th className="px-3 py-2">Topic Mode</th>
                  </tr>
                </thead>
                <tbody>
                  {data.personas.map((item) => (
                    <tr key={item.value} className="border-t border-slate-200">
                      <td className="px-3 py-3">{item.value}</td>
                      <td className="px-3 py-3">{item.label}</td>
                      <td className="px-3 py-3">{item.topic_mode}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
              Topic Modes
            </h2>
            <div className="mt-3 flex flex-wrap gap-2">
              {data.topic_modes.map((item) => (
                <span
                  key={item.value}
                  className="rounded-full border border-slate-300 bg-slate-50 px-3 py-1 text-xs text-slate-700"
                >
                  {item.label} ({item.value})
                </span>
              ))}
            </div>
          </section>

          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
              Runtime Defaults
            </h2>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <article className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                <p className="text-xs uppercase tracking-wide text-slate-500">Platform</p>
                <p className="mt-1 text-sm font-medium">{data.defaults.platform}</p>
              </article>
              <article className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                <p className="text-xs uppercase tracking-wide text-slate-500">Default Persona</p>
                <p className="mt-1 text-sm font-medium">{data.defaults.persona_id}</p>
              </article>
              <article className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                <p className="text-xs uppercase tracking-wide text-slate-500">Default Topic</p>
                <p className="mt-1 text-sm font-medium">{data.defaults.topic_mode}</p>
              </article>
              <article className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                <p className="text-xs uppercase tracking-wide text-slate-500">API Base URL</p>
                <p className="mt-1 break-all text-sm font-medium">{data.defaults.api_base_url}</p>
              </article>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
