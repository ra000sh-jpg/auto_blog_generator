"use client";

import { useEffect, useMemo, useState } from "react";
import {
    fetchOnboardingStatus,
    saveOnboardingSchedule,
    type OnboardingStatusResponse,
    type ScheduleAllocationItem,
} from "@/lib/api";
import { normalizeAllocations } from "@/lib/utils/formatters";

type WizardStepScheduleProps = {
    initialOnboardingStatus: OnboardingStatusResponse;
    onPrev: () => void;
    onNext: () => void;
};

export default function WizardStepSchedule({
    initialOnboardingStatus,
    onPrev,
    onNext,
}: WizardStepScheduleProps) {
    const [loading, setLoading] = useState(true);
    const [dailyPostsTarget, setDailyPostsTarget] = useState(initialOnboardingStatus.daily_posts_target ?? 3);
    const [ideaVaultDailyQuota, setIdeaVaultDailyQuota] = useState(initialOnboardingStatus.idea_vault_daily_quota ?? 0);
    const [categoryAllocations, setCategoryAllocations] = useState<ScheduleAllocationItem[]>([]);
    const [saving, setSaving] = useState(false);
    const [stepMessage, setStepMessage] = useState("");

    useEffect(() => {
        const IDEA_VAULT_CATEGORY = "다양한 생각들";
        let isMounted = true;
        async function loadLatestStatus() {
            try {
                // Fetch the latest categories that might have been updated in Step 3
                const response = await fetchOnboardingStatus();
                if (!isMounted) return;
                const cats = (response.categories || []).filter(c => c !== IDEA_VAULT_CATEGORY);
                setCategoryAllocations(
                    normalizeAllocations(
                        cats,
                        100,
                        (response.category_allocations || []).filter(a => a.category !== IDEA_VAULT_CATEGORY),
                    ),
                );
            } catch (error) {
                console.error("Failed to load latest onboarding status", error);
                // Fallback to initial status
                const cats = (initialOnboardingStatus.categories || []).filter(c => c !== IDEA_VAULT_CATEGORY);
                setCategoryAllocations(
                    normalizeAllocations(
                        cats,
                        100,
                        (initialOnboardingStatus.category_allocations || []).filter(a => a.category !== IDEA_VAULT_CATEGORY),
                    ),
                );
            } finally {
                if (isMounted) setLoading(false);
            }
        }
        loadLatestStatus();
        return () => {
            isMounted = false;
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []); // trendDailyTarget is not in the dependency array to only run on mount and not clobber edits

    const allocationTotal = useMemo(
        () => categoryAllocations.reduce((acc, item) => acc + Math.max(0, Number(item.percentage || 0)), 0),
        [categoryAllocations],
    );

    function handleDailyTargetChange(nextTarget: number) {
        const normalizedTarget = Math.max(3, Math.min(5, nextTarget));
        setDailyPostsTarget(normalizedTarget);
        const normalizedIdeaVaultQuota = Math.max(0, Math.min(normalizedTarget, ideaVaultDailyQuota));
        setIdeaVaultDailyQuota(normalizedIdeaVaultQuota);

        setCategoryAllocations((previous) => {
            const currentCategories = previous.map((item) => item.category);
            return normalizeAllocations(currentCategories, 100, previous);
        });
    }

    function handleAllocationChange(index: number, patch: Partial<ScheduleAllocationItem>) {
        setCategoryAllocations((previous) => {
            const next = [...previous];
            const current = next[index];
            if (!current) return previous;
            next[index] = { ...current, ...patch, percentage: patch.percentage ?? current.percentage, topic_mode: patch.topic_mode ?? current.topic_mode };

            // 즉시 100% 재분배 (percentage 변경의 경우에만)
            if (patch.percentage !== undefined) {
                return normalizeAllocations(next.map(x => x.category), 100, next);
            }
            return next;
        });
    }

    async function handleCompleteSetup() {
        setSaving(true);
        setStepMessage("");
        try {
            const normalized = normalizeAllocations(
                categoryAllocations.map((item) => item.category),
                100,
                categoryAllocations
            );
            await saveOnboardingSchedule({
                daily_posts_target: dailyPostsTarget,
                idea_vault_daily_quota: ideaVaultDailyQuota,
                allocations: normalized,
                category_mapping: initialOnboardingStatus.category_mapping || {},
            });
            onNext();
        } catch (error) {
            setStepMessage(error instanceof Error ? error.message : "저장 실패");
        } finally {
            setSaving(false);
        }
    }

    if (loading) {
        return <div className="text-center py-10 animate-pulse text-slate-500">최신 설정을 불러오는 중입니다...</div>;
    }

    return (
        <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4">
            <h2 className="text-xl font-bold">4단계. 스케줄링 완성!</h2>
            <p className="text-sm text-slate-600">마지막으로 매일 몇 개의 글을 쓸지 스케줄을 확정합니다.</p>

            <div className="bg-slate-50 p-6 rounded-2xl border border-slate-200">
                <label className="flex items-center justify-between text-base font-semibold text-slate-800">
                    하루 총 발행 목표량
                    <span className="text-indigo-600 font-bold bg-indigo-100 px-3 py-1 rounded-lg">{dailyPostsTarget} 포스트</span>
                </label>
                <input
                    type="range" min={3} max={5} value={dailyPostsTarget}
                    onChange={(e) => handleDailyTargetChange(Number(e.target.value))}
                    className="mt-4 w-full accent-indigo-600"
                />
            </div>

            <div className="rounded-2xl border border-slate-200 overflow-hidden">
                <div className="bg-slate-100 flex justify-between px-4 py-3 font-semibold text-slate-700 text-sm border-b">
                    <span>카테고리별 발행 비중</span>
                    <span className={allocationTotal === 100 ? "text-indigo-600" : "text-amber-600"}>총 {allocationTotal}%</span>
                </div>
                <div className="divide-y divide-slate-100 p-2">
                    {categoryAllocations.map((item, index) => (
                        <div key={item.category} className="flex flex-col gap-2 px-2 py-3 hover:bg-slate-50 transition-colors rounded-xl">
                            <div className="flex items-center gap-4">
                                <div className="flex-1 font-medium text-slate-800">{item.category}</div>
                                <select
                                    value={item.topic_mode}
                                    onChange={(e) => handleAllocationChange(index, { topic_mode: e.target.value })}
                                    className="rounded-lg border-slate-300 text-sm bg-white"
                                >
                                    <option value="cafe">일상/카페</option>
                                    <option value="it">IT/테크</option>
                                    <option value="parenting">육아</option>
                                    <option value="finance">경제/재테크</option>
                                </select>
                            </div>
                            <div className="flex items-center gap-3 w-full">
                                <input
                                    type="range"
                                    min={0}
                                    max={100}
                                    step={5}
                                    value={item.percentage || 0}
                                    onChange={(e) => handleAllocationChange(index, { percentage: Number(e.target.value) })}
                                    className="flex-1 accent-indigo-600"
                                />
                                <div className="w-12 text-right font-bold text-indigo-700 text-sm">
                                    {item.percentage || 0}%
                                </div>
                            </div>
                        </div>
                    ))}
                </div>
            </div>

            <div className="flex justify-between pt-6 mt-4 border-t border-slate-100">
                <button onClick={onPrev} className="text-slate-500 font-semibold px-4 py-2 hover:bg-slate-100 rounded-lg transition-colors">← 이전</button>
                <button onClick={handleCompleteSetup} disabled={saving || allocationTotal !== 100} className="bg-gradient-to-r from-indigo-600 to-blue-600 text-white px-8 py-3 rounded-full font-bold shadow-lg hover:shadow-xl transition-all active:scale-95 text-lg">
                    {saving ? "저장 중..." : "다음 단계로 →"}
                </button>
            </div>
            {allocationTotal !== 100 && (
                <p className="text-amber-600 text-sm text-right mt-2 font-medium">✨ 카테고리 비중(총합 {allocationTotal}%)이 정확히 100%여야 합니다.</p>
            )}
            {stepMessage && <p className="text-red-500 text-sm text-right mt-2">{stepMessage}</p>}
        </div>
    );
}
