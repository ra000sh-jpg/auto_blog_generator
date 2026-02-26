"use client";

import { useMemo, useState } from "react";
import {
    saveOnboardingSchedule,
    type OnboardingStatusResponse,
    type ScheduleAllocationItem,
} from "@/lib/api";
import { normalizeAllocations, inferTopicMode } from "@/lib/utils/formatters";

const TOPIC_OPTIONS = [
    { value: "cafe", label: "카페/일상" },
    { value: "it", label: "IT" },
    { value: "finance", label: "경제" },
    { value: "parenting", label: "육아" },
] as const;

type AllocationSettingsCardProps = {
    initialOnboardingStatus: OnboardingStatusResponse;
};

export default function AllocationSettingsCard({
    initialOnboardingStatus,
}: AllocationSettingsCardProps) {
    const withImageDefaults = (rows: ScheduleAllocationItem[]): ScheduleAllocationItem[] =>
        rows.map((row) => {
            const total = Math.max(0, Math.min(4, Number(row.images_per_post ?? 2)));
            const ai = Math.max(0, Math.min(total, Number(row.ai_images ?? 0)));
            return {
                ...row,
                images_per_post: total,
                ai_images: ai,
            };
        });

    const [savingSchedule, setSavingSchedule] = useState(false);
    const [scheduleMessage, setScheduleMessage] = useState("");
    const [savingAllocation, setSavingAllocation] = useState(false);
    const [allocationMessage, setAllocationMessage] = useState("");

    const [dailyPostsTarget, setDailyPostsTarget] = useState(
        Math.max(1, initialOnboardingStatus.daily_posts_target || 3)
    );
    const [ideaVaultDailyQuota, setIdeaVaultDailyQuota] = useState(
        Math.max(0, initialOnboardingStatus.idea_vault_daily_quota || 0)
    );
    // 전체 카테고리 목록 (할당량 0인 카테고리 포함)
    const [allCategories] = useState<string[]>(
        initialOnboardingStatus.categories || []
    );
    // Idea Vault 전용 카테고리("다양한 생각들")는 비율 분배에서 제외
    const IDEA_VAULT_CATEGORY = "다양한 생각들";
    const trendInitial = Math.max(
        0,
        (initialOnboardingStatus.daily_posts_target || 3) - (initialOnboardingStatus.idea_vault_daily_quota || 0)
    );
    const scheduleCategories = (initialOnboardingStatus.categories || []).filter(c => c !== IDEA_VAULT_CATEGORY);
    const [categoryAllocations, setCategoryAllocations] = useState<ScheduleAllocationItem[]>(() =>
        withImageDefaults(normalizeAllocations(
            scheduleCategories,
            trendInitial,
            (initialOnboardingStatus.category_allocations || []).filter(a => a.category !== IDEA_VAULT_CATEGORY),
        ))
    );
    const [categoryMapping, setCategoryMapping] = useState<Record<string, string>>(
        initialOnboardingStatus.category_mapping || {}
    );

    const trendDailyTarget = useMemo(
        () => Math.max(0, dailyPostsTarget - ideaVaultDailyQuota),
        [dailyPostsTarget, ideaVaultDailyQuota]
    );
    const allocationTotal = useMemo(
        () => categoryAllocations.reduce((acc, current) => acc + (current.percentage || 0), 0),
        [categoryAllocations]
    );

    function handleDailyTargetChange(newTarget: number) {
        setDailyPostsTarget(newTarget);
        if (ideaVaultDailyQuota > newTarget) {
            setIdeaVaultDailyQuota(newTarget);
        }
    }

    function handleIdeaVaultQuotaChange(newQuota: number) {
        setIdeaVaultDailyQuota(newQuota);
    }

    function handleMappingChange(categoryName: string, mappedValue: string) {
        setCategoryMapping((prev) => ({
            ...prev,
            [categoryName]: mappedValue,
        }));
    }

    const handleCopy = async (text: string) => {
        try {
            await navigator.clipboard.writeText(text);
        } catch (err) {
            console.error("복사 실패:", err);
        }
    };

    // 할당 비율만 저장 (카테고리 비율 테이블 전용)
    async function handleSaveAllocation() {
        setSavingAllocation(true);
        setAllocationMessage("");
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
            setAllocationMessage("✅ 할당 비율이 저장되었습니다.");
            setTimeout(() => setAllocationMessage(""), 3000);
        } catch (requestError) {
            const message = requestError instanceof Error ? requestError.message : "저장에 실패했습니다.";
            setAllocationMessage(message);
        } finally {
            setSavingAllocation(false);
        }
    }

    // 네이버 카테고리 매핑 저장 (하단 매핑 테이블 전용)
    async function handleSaveSchedule() {
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
            setScheduleMessage("카테고리 매핑이 저장되었습니다.");
        } catch (requestError) {
            const message = requestError instanceof Error ? requestError.message : "저장에 실패했습니다.";
            setScheduleMessage(message);
        } finally {
            setSavingSchedule(false);
        }
    }

    return (
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
                Scheduler Allocation
            </h2>
            <p className="mt-1 text-sm text-slate-600">
                하루 총 발행량과 Idea Vault 사용량을 먼저 정한 뒤, 나머지 비율을 트렌드 카테고리에 배분하세요. (합계 100%)
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
                    <div className="col-span-3">Category</div>
                    <div className="col-span-2">Topic Mode</div>
                    <div className="col-span-3">할당량</div>
                    <div className="col-span-2">총 이미지</div>
                    <div className="col-span-2">AI 이미지</div>
                </div>
                <div className="divide-y divide-slate-200">
                    {categoryAllocations
                        .filter((item) => item.category !== "다양한 생각들")
                        .map((item) => {
                            const originalIndex = categoryAllocations.findIndex((x) => x.category === item.category);
                            return (
                                <div key={item.category} className="grid grid-cols-12 items-center gap-4 px-3 py-3">
                                    <div className="col-span-3 text-sm font-medium text-slate-800">
                                        {item.category}
                                    </div>
                                    <div className="col-span-2">
                                        <select
                                            value={item.topic_mode}
                                            onChange={(event) => {
                                                const newVal = event.target.value;
                                                const temp = [...categoryAllocations];
                                                temp[originalIndex] = { ...temp[originalIndex], topic_mode: newVal };
                                                setCategoryAllocations(temp);
                                            }}
                                            className="w-full rounded-lg border border-slate-300 px-2 py-1 text-xs"
                                        >
                                            {TOPIC_OPTIONS.map((option) => (
                                                <option key={option.value} value={option.value}>
                                                    {option.label}
                                                </option>
                                            ))}
                                        </select>
                                    </div>
                                    <div className="col-span-3 flex items-center gap-3">
                                        <input
                                            type="range"
                                            min={0}
                                            max={100}
                                            step={5}
                                            value={item.percentage || 0}
                                            onChange={(event) => {
                                                const newVal = Number(event.target.value);
                                                // 자유 조작: 즉시 재분배 없이 값만 업데이트
                                                const temp = [...categoryAllocations];
                                                temp[originalIndex] = { ...temp[originalIndex], percentage: newVal };
                                                setCategoryAllocations(temp);
                                            }}
                                            className="flex-1 cursor-pointer accent-indigo-600"
                                        />
                                        <div className="flex w-12 flex-col items-end whitespace-nowrap">
                                            <span className={`text-sm font-bold ${(item.percentage || 0) === 0
                                                ? "text-slate-400"
                                                : "text-indigo-600"
                                                }`}>{item.percentage || 0}%</span>
                                        </div>
                                    </div>
                                    <div className="col-span-2">
                                        <select
                                            value={item.images_per_post ?? 2}
                                            onChange={(event) => {
                                                const newTotal = Number(event.target.value);
                                                const temp = [...categoryAllocations];
                                                const currentAi = Number(temp[originalIndex].ai_images ?? 0);
                                                temp[originalIndex] = {
                                                    ...temp[originalIndex],
                                                    images_per_post: newTotal,
                                                    ai_images: Math.min(currentAi, newTotal),
                                                };
                                                setCategoryAllocations(temp);
                                            }}
                                            className="w-full rounded-lg border border-slate-300 px-2 py-1 text-xs"
                                        >
                                            {[0, 1, 2, 3, 4].map((n) => (
                                                <option key={n} value={n}>{n}장</option>
                                            ))}
                                        </select>
                                    </div>
                                    <div className="col-span-2">
                                        <select
                                            value={item.ai_images ?? 0}
                                            onChange={(event) => {
                                                const temp = [...categoryAllocations];
                                                temp[originalIndex] = {
                                                    ...temp[originalIndex],
                                                    ai_images: Number(event.target.value),
                                                };
                                                setCategoryAllocations(temp);
                                            }}
                                            className="w-full rounded-lg border border-slate-300 px-2 py-1 text-xs"
                                        >
                                            {Array.from(
                                                { length: (item.images_per_post ?? 2) + 1 },
                                                (_, i) => i,
                                            ).map((n) => (
                                                <option key={n} value={n}>{n}장</option>
                                            ))}
                                        </select>
                                    </div>
                                </div>
                            );
                        })}
                </div>
            </div>

            {/* 할당 비율 합계 + 균등 분배 + 저장 버튼 영역 */}
            <div className="mt-4 flex flex-col gap-3 rounded-xl border border-slate-200 bg-slate-50 p-4 shadow-inner">
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="text-sm font-medium text-slate-800">
                        총 할당량 비율:{" "}
                        <strong className={allocationTotal === 100 ? "text-emerald-600" : "text-rose-500"}>
                            {allocationTotal}%
                        </strong>
                        {allocationTotal !== 100 && (
                            <span className="ml-2 text-xs text-rose-500">
                                (합계가 100%가 되도록 조정해 주세요)
                            </span>
                        )}
                    </p>
                    <button
                        type="button"
                        onClick={() => {
                            const cats = categoryAllocations.map((item) => item.category);
                            const evenPct = Math.floor(100 / cats.length / 5) * 5;
                            const evenAllocations: ScheduleAllocationItem[] = cats.map((cat, idx) => ({
                                category: cat,
                                topic_mode: categoryAllocations[idx]?.topic_mode || inferTopicMode(cat),
                                count: 0,
                                percentage: evenPct,
                                images_per_post: categoryAllocations[idx]?.images_per_post ?? 2,
                                ai_images: Math.min(
                                    Number(categoryAllocations[idx]?.ai_images ?? 0),
                                    Number(categoryAllocations[idx]?.images_per_post ?? 2),
                                ),
                            }));
                            const remainder = 100 - evenPct * cats.length;
                            if (evenAllocations.length > 0) evenAllocations[0].percentage! += remainder;
                            setCategoryAllocations(evenAllocations);
                        }}
                        className="rounded-full border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm transition hover:border-slate-500 hover:bg-slate-50"
                    >
                        ✨ 균등 분배 자동 맞춤
                    </button>
                </div>

                {allocationTotal !== 100 && (
                    <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50/80 px-3 py-2 text-xs text-amber-800">
                        <span className="text-amber-500">⚠️</span>
                        <p>합계가 100%가 아닙니다. 저장하면 차이만큼 자동 보정됩니다. &quot;균등 분배 자동 맞춤&quot; 버튼을 사용해 보세요.</p>
                    </div>
                )}

                {trendDailyTarget <= 0 && (
                    <div className="flex items-start gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600">
                        <span className="text-slate-400">ℹ️</span>
                        <p>하루 발행량이 모두 Idea Vault 예약으로 가득 차서, 트렌드 토픽 발굴은 오늘 진행되지 않습니다.</p>
                    </div>
                )}

                {/* 할당 비율 저장 버튼 */}
                <div className="flex items-center justify-end gap-3 border-t border-slate-200 pt-3">
                    {allocationMessage && (
                        <span className={`text-xs font-medium ${allocationMessage.startsWith("✅") ? "text-emerald-600" : "text-rose-500"
                            }`}>
                            {allocationMessage}
                        </span>
                    )}
                    <button
                        type="button"
                        onClick={handleSaveAllocation}
                        disabled={savingAllocation}
                        className="rounded-full bg-indigo-600 px-5 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-700 disabled:opacity-50"
                    >
                        {savingAllocation ? "저장 중..." : "💾 할당 비율 저장"}
                    </button>
                </div>
            </div>

            <div className="mt-8 border-t border-slate-200 pt-6">
                <h3 className="font-[family-name:var(--font-heading)] text-md font-semibold text-slate-800">
                    Naver Category Mapping
                </h3>
                <p className="mt-1 text-xs text-slate-500">
                    위성 블로그/자동 발행 시 네이버 블로그에 실제로 개설되어 있는 카테고리명을 정확히 복사해서 연결해 주세요.
                    미입력 시 게시판(기본 카테고리)으로 발행됩니다.
                </p>

                <div className="mt-4 grid gap-3">
                    {allCategories.length > 0 ? (
                        allCategories.map((categoryName) => (
                            <div key={`mapping-${categoryName}`} className="flex items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 p-3">
                                <div className="flex w-1/3 flex-col gap-1 sm:w-1/4">
                                    <span className="text-xs font-semibold text-slate-700">AI 기획명</span>
                                    <div className="flex items-center gap-2">
                                        <span className="truncate text-sm font-medium text-indigo-600">{categoryName}</span>
                                        <button
                                            onClick={() => handleCopy(categoryName)}
                                            className="rounded-md border border-slate-300 bg-white p-1 text-slate-500 hover:bg-slate-100 hover:text-indigo-600 transition"
                                            title="복사하기"
                                        >
                                            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                                        </button>
                                    </div>
                                </div>
                                <div className="flex flex-1 flex-col justify-center">
                                    <span className="mb-1 text-[10px] font-semibold text-slate-400">네이버 실제 카테고리명</span>
                                    <input
                                        type="text"
                                        placeholder="예: IT와 TECH"
                                        value={categoryMapping[categoryName] || ""}
                                        onChange={(e) => handleMappingChange(categoryName, e.target.value)}
                                        className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                                    />
                                </div>
                            </div>
                        ))
                    ) : (
                        <p className="text-sm text-slate-500 py-3">할당된 카테고리가 없습니다.</p>
                    )}
                </div>
            </div>

            <div className="mt-6 flex flex-wrap items-center justify-end gap-3 border-t border-slate-200 pt-5">
                {scheduleMessage && (
                    <span className={`text-xs font-medium ${scheduleMessage.includes("실패") ? "text-rose-500" : "text-emerald-600"
                        }`}>
                        {scheduleMessage}
                    </span>
                )}
                <button
                    type="button"
                    onClick={handleSaveSchedule}
                    disabled={savingSchedule}
                    className="rounded-full bg-slate-900 px-5 py-2.5 text-sm font-medium text-white shadow-sm transition hover:bg-slate-700 disabled:opacity-50"
                >
                    {savingSchedule ? "저장 중..." : "카테고리 매핑 저장"}
                </button>
            </div>
        </section>
    );
}
