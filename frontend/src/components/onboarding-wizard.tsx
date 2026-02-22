"use client";

import { useEffect, useMemo, useState } from "react";
import {
    completeOnboarding,
    fetchNaverConnectStatus,
    fetchOnboardingStatus,
    fetchRouterSettings,
    saveOnboardingCategories,
    saveOnboardingSchedule,
    saveRouterSettings,
    savePersonaLab,
    startNaverConnect,
    type NaverConnectStatusResponse,
    type ScheduleAllocationItem,
} from "@/lib/api";

function parseCommaValues(rawText: string): string[] {
    return rawText
        .split(",")
        .map((value) => value.trim())
        .filter((value, index, list) => value.length > 0 && list.indexOf(value) === index);
}

function compactKeys(input: Record<string, string>): Record<string, string> {
    return Object.entries(input).reduce<Record<string, string>>((acc, [key, value]) => {
        const normalized = String(value || "").trim();
        if (normalized) acc[key] = normalized;
        return acc;
    }, {});
}

function inferTopicMode(categoryName: string): string {
    const lowered = categoryName.toLowerCase();
    if (["경제", "finance", "투자", "주식", "재테크"].some((t) => lowered.includes(t))) return "finance";
    if (["it", "개발", "코드", "자동화", "ai", "테크"].some((t) => lowered.includes(t))) return "it";
    if (["육아", "아이", "부모", "가정"].some((t) => lowered.includes(t))) return "parenting";
    return "cafe";
}

function normalizeAllocations(
    categories: string[],
    dailyTarget: number,
    existingAllocations: ScheduleAllocationItem[] = [],
): ScheduleAllocationItem[] {
    const normalizedCategories = categories
        .map((value) => value.trim())
        .filter((value, index, list) => value.length > 0 && list.indexOf(value) === index);
    const fallbackCategories = normalizedCategories.length > 0 ? normalizedCategories : ["다양한 생각"];

    const existingMap = new Map(existingAllocations.map((item) => [item.category, item]));
    const rows: ScheduleAllocationItem[] = fallbackCategories.map((categoryName) => ({
        category: categoryName,
        topic_mode: existingMap.get(categoryName)?.topic_mode || inferTopicMode(categoryName),
        count: Math.max(0, Number(existingMap.get(categoryName)?.count || 0)),
    }));

    const safeTarget = Math.max(0, dailyTarget);
    if (safeTarget <= 0) {
        return rows.map((item) => ({ ...item, count: 0 }));
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
            if (overflow <= 0) break;
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

interface OnboardingWizardProps {
    onComplete: () => void;
}

export function OnboardingWizard({ onComplete }: OnboardingWizardProps) {
    const [loading, setLoading] = useState(true);
    const [loadingError, setLoadingError] = useState("");

    const [step, setStep] = useState(0);
    const [saving, setSaving] = useState(false);
    const [stepMessage, setStepMessage] = useState("");
    const [routerSaving, setRouterSaving] = useState(false);
    const [routerMessage, setRouterMessage] = useState("");

    // Step 1: API Keys & Router
    const [strategyMode, setStrategyMode] = useState<"cost" | "quality">("cost");
    const [textApiKeys, setTextApiKeys] = useState<Record<string, string>>({
        qwen: "", deepseek: "", gemini: "", openai: "", claude: "",
    });
    const [textApiMasks, setTextApiMasks] = useState<Record<string, string>>({});
    const [imageApiKeys, _setImageApiKeys] = useState<Record<string, string>>({
        pexels: "", together: "", fal: "", openai_image: "",
    });
    const [imageEngine, setImageEngine] = useState("pexels");
    const [imageEnabled, setImageEnabled] = useState(true);
    const [imagesPerPost, setImagesPerPost] = useState(1);

    // Step 2: Naver & Blog Info
    const [naverStatus, setNaverStatus] = useState<NaverConnectStatusResponse | null>(null);
    const [naverConnecting, setNaverConnecting] = useState(false);

    const [categoriesText, setCategoriesText] = useState("");


    // Step 3: Persona Edit
    const [personaId, setPersonaId] = useState("P1");
    const [identity, setIdentity] = useState("");
    const [targetAudience, _setTargetAudience] = useState("");
    const [toneHint, setToneHint] = useState("");
    const [interestsText, setInterestsText] = useState("");
    const [structureScore, _setStructureScore] = useState(50);
    const [evidenceScore, _setEvidenceScore] = useState(50);
    const [distanceScore, _setDistanceScore] = useState(50);
    const [criticismScore, _setCriticismScore] = useState(50);
    const [densityScore, _setDensityScore] = useState(50);
    const [styleStrength, _setStyleStrength] = useState(40);

    // Step 4: Schedule
    const [dailyPostsTarget, setDailyPostsTarget] = useState(3);
    const [ideaVaultDailyQuota, setIdeaVaultDailyQuota] = useState(2);
    const [categoryAllocations, setCategoryAllocations] = useState<ScheduleAllocationItem[]>([]);


    useEffect(() => {
        let isMounted = true;
        async function loadStatus() {
            try {
                const [response, routerState, naverConnectState] = await Promise.all([
                    fetchOnboardingStatus(),
                    fetchRouterSettings(),
                    fetchNaverConnectStatus(),
                ]);
                if (!isMounted) return;

                setPersonaId(response.persona_id || "P1");

                // Ensure "다양한 생각들" category exists
                const cats = response.categories || [];
                if (!cats.includes("다양한 생각들")) cats.push("다양한 생각들");
                setCategoriesText(cats.join(", "));

                setInterestsText((response.interests || []).join(", "));

                const resolvedTarget = Math.max(3, Math.min(5, Number(response.daily_posts_target || 3)));
                const resolvedIdeaVaultQuota = Math.max(0, Math.min(resolvedTarget, Number(response.idea_vault_daily_quota ?? Math.min(2, resolvedTarget))));
                setDailyPostsTarget(resolvedTarget);
                setIdeaVaultDailyQuota(resolvedIdeaVaultQuota);
                setCategoryAllocations(
                    normalizeAllocations(
                        cats,
                        Math.max(0, resolvedTarget - resolvedIdeaVaultQuota),
                        response.category_allocations || [],
                    ),
                );
                setStrategyMode(routerState.settings.strategy_mode === "quality" ? "quality" : "cost");
                setTextApiMasks(routerState.settings.text_api_keys_masked || {});
                setImageEngine(routerState.settings.image_engine || "pexels");
                setImageEnabled(Boolean(routerState.settings.image_enabled));
                setImagesPerPost(Math.max(0, Math.min(4, Number(routerState.settings.images_per_post || 1))));
                setNaverStatus(naverConnectState);

            } catch (error) {
                if (!isMounted) return;
                setLoadingError(error instanceof Error ? error.message : "온보딩 상태를 불러오지 못했습니다.");
            } finally {
                if (isMounted) setLoading(false);
            }
        }
        loadStatus();
        return () => { isMounted = false; };
    }, []);

    const stepTitles = [
        "1. API 키 설정",
        "2. 네이버 & 주제 설정",
        "3. 페르소나 설계",
        "4. 스케줄 완성",
    ];

    const allocationTotal = useMemo(
        () => categoryAllocations.reduce((acc, item) => acc + Math.max(0, Number(item.count || 0)), 0),
        [categoryAllocations],
    );

    const trendDailyTarget = useMemo(
        () => Math.max(0, dailyPostsTarget - ideaVaultDailyQuota),
        [dailyPostsTarget, ideaVaultDailyQuota],
    );

    async function handleSaveRouterStep() {
        setRouterSaving(true);
        setRouterMessage("");
        try {
            const saved = await saveRouterSettings({
                strategy_mode: strategyMode,
                text_api_keys: compactKeys(textApiKeys),
                image_api_keys: compactKeys(imageApiKeys),
                image_engine: imageEngine,
                image_enabled: imageEnabled,
                images_per_post: imagesPerPost,
            });
            setTextApiMasks(saved.settings.text_api_keys_masked || {});
            setTextApiMasks(saved.settings.text_api_keys_masked || {});
            setStep(1);
        } catch (error) {
            setRouterMessage(error instanceof Error ? error.message : "저장 실패");
        } finally {
            setRouterSaving(false);
        }
    }

    async function handleNaverConnect() {
        setNaverConnecting(true);
        try {
            await startNaverConnect({ timeout_sec: 300 });
            const statusResponse = await fetchNaverConnectStatus();
            setNaverStatus(statusResponse);
        } catch (error) {
            console.error(error);
        } finally {
            setNaverConnecting(false);
        }
    }

    async function handleSaveCategoryStep() {
        setSaving(true);
        try {
            // 강제 포함로직
            let modifiedCatText = categoriesText;
            if (!modifiedCatText.includes("다양한 생각들")) {
                modifiedCatText = modifiedCatText ? modifiedCatText + ", 다양한 생각들" : "다양한 생각들";
            }

            await saveOnboardingCategories({
                categories: parseCommaValues(modifiedCatText),
                fallback_category: "다양한 생각들",
            });
            setCategoriesText(modifiedCatText);
            setStep(2);
        } catch (error) {
            setStepMessage(error instanceof Error ? error.message : "저장 실패");
        } finally {
            setSaving(false);
        }
    }

    async function handleSavePersonaStep() {
        setSaving(true);
        try {
            await savePersonaLab({
                persona_id: personaId,
                identity,
                target_audience: targetAudience,
                tone_hint: toneHint,
                interests: parseCommaValues(interestsText),
                structure_score: structureScore,
                evidence_score: evidenceScore,
                distance_score: distanceScore,
                criticism_score: criticismScore,
                density_score: densityScore,
                style_strength: styleStrength,
            });
            setStep(3);
        } catch (error) {
            setStepMessage(error instanceof Error ? error.message : "저장 실패");
        } finally {
            setSaving(false);
        }
    }

    function handleDailyTargetChange(nextTarget: number) {
        const normalizedTarget = Math.max(3, Math.min(5, nextTarget));
        setDailyPostsTarget(normalizedTarget);
        const normalizedIdeaVaultQuota = Math.max(0, Math.min(normalizedTarget, ideaVaultDailyQuota));
        setIdeaVaultDailyQuota(normalizedIdeaVaultQuota);
        const adjustedTrendTarget = Math.max(0, normalizedTarget - normalizedIdeaVaultQuota);
        const currentCategories = categoryAllocations.map((item) => item.category);
        setCategoryAllocations(normalizeAllocations(currentCategories, adjustedTrendTarget, categoryAllocations));
    }

    function handleIdeaVaultQuotaChange(nextQuota: number) {
        const normalizedQuota = Math.max(0, Math.min(dailyPostsTarget, nextQuota));
        setIdeaVaultDailyQuota(normalizedQuota);
        const adjustedTrendTarget = Math.max(0, dailyPostsTarget - normalizedQuota);
        const currentCategories = categoryAllocations.map((item) => item.category);
        setCategoryAllocations(normalizeAllocations(currentCategories, adjustedTrendTarget, categoryAllocations));
    }

    function handleAllocationChange(index: number, patch: Partial<ScheduleAllocationItem>) {
        setCategoryAllocations((previous) => {
            const next = [...previous];
            const current = next[index];
            if (!current) return previous;
            next[index] = { ...current, ...patch, count: patch.count ?? current.count, topic_mode: patch.topic_mode ?? current.topic_mode };
            return next;
        });
    }

    async function handleCompleteSetup() {
        setSaving(true);
        try {
            const normalized = normalizeAllocations(categoryAllocations.map((item) => item.category), trendDailyTarget, categoryAllocations);
            await saveOnboardingSchedule({
                daily_posts_target: dailyPostsTarget,
                idea_vault_daily_quota: ideaVaultDailyQuota,
                allocations: normalized,
            });
            await completeOnboarding();
            onComplete(); // Navigate to Dashboard
        } catch (error) {
            setStepMessage(error instanceof Error ? error.message : "저장 실패");
        } finally {
            setSaving(false);
        }
    }

    if (loading) return <div className="text-center py-10">설정 마법사를 불러오는 중입니다...</div>;
    if (loadingError) return <div className="text-center text-red-500 py-10">{loadingError}</div>;

    return (
        <div className="mx-auto w-full max-w-3xl space-y-6">
            <div className="text-center mb-10">
                <h1 className="text-3xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-blue-600 to-indigo-600">환영합니다! 시작해볼까요?</h1>
                <p className="mt-2 text-slate-600">간단한 4단계 설정만 마치면 자동 블로그 포스팅이 시작됩니다.</p>
            </div>

            <div className="flex justify-between items-center mb-8 px-4 relative">
                <div className="absolute top-1/2 left-0 right-0 h-1 bg-slate-200 -z-10 -translate-y-1/2 rounded animate-pulse" />
                {stepTitles.map((title, idx) => (
                    <div key={title} className={`py-2 px-4 rounded-full text-sm font-semibold transition-all duration-300 ${step === idx ? "bg-indigo-600 text-white shadow-lg scale-105" : step > idx ? "bg-emerald-500 text-white" : "bg-slate-100 text-slate-400"}`}>
                        {title}
                    </div>
                ))}
            </div>

            <div className="bg-white/80 backdrop-blur-sm rounded-3xl shadow-xl border border-white/40 p-8 transition-all duration-500">
                {step === 0 && (
                    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4">
                        <h2 className="text-xl font-bold">1단계. API 파트너 연결 (API Keys)</h2>
                        <p className="text-sm text-slate-600">가장 핵심이 되는 AI 두뇌와 연결합니다. 최소 QWEN 이나 DEEPSEEK 키 중 하나가 필요합니다.</p>

                        <div className="grid gap-4 sm:grid-cols-2">
                            {["qwen", "deepseek", "gemini", "openai"].map((key) => (
                                <div key={key}>
                                    <label className="text-sm font-semibold uppercase">{key}</label>
                                    <input
                                        type="password"
                                        value={textApiKeys[key] || ""}
                                        onChange={(e) => setTextApiKeys(prev => ({ ...prev, [key]: e.target.value }))}
                                        placeholder={textApiMasks[key] ? `${textApiMasks[key]} (이미 등록됨)` : "API 키 입력"}
                                        className="mt-1 w-full rounded-xl border border-slate-300 px-4 py-3 bg-slate-50 focus:bg-white focus:ring-2 focus:ring-indigo-500 transition-all"
                                    />
                                </div>
                            ))}
                        </div>

                        <div className="flex justify-end pt-4">
                            <button onClick={handleSaveRouterStep} disabled={routerSaving} className="bg-gradient-to-r from-indigo-600 to-blue-600 text-white px-8 py-3 rounded-full font-bold shadow-md hover:shadow-lg transition-all active:scale-95 text-lg">
                                {routerSaving ? "저장 중..." : "다음 단계로 →"}
                            </button>
                        </div>
                        {routerMessage && <p className="text-red-500 text-sm mt-2">{routerMessage}</p>}
                    </div>
                )}

                {step === 1 && (
                    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4">
                        <h2 className="text-xl font-bold">2단계. 네이버 로그인 & 카테고리 (Naver 연동)</h2>
                        <p className="text-sm text-slate-600">포스팅을 업로드 할 네이버와 연결하고, 블로그 카테고리를 설정해주세요.</p>

                        <div className="bg-indigo-50/50 p-6 rounded-2xl border border-indigo-100 flex items-center justify-between">
                            <div>
                                <h3 className="font-semibold text-indigo-900">네이버 블로그 계정 연결</h3>
                                <p className="text-sm text-indigo-600/80 mt-1">{naverStatus?.connected ? "✅ 현재 연결되어 있습니다." : "❌ 연결되지 않았습니다."}</p>
                            </div>
                            <button onClick={handleNaverConnect} disabled={naverConnecting} className="bg-[#03C75A] text-white px-6 py-2 rounded-xl font-bold hover:bg-[#02b350] transition-colors shadow-sm">
                                {naverConnecting ? "팝업 창 확인해주세요..." : "네이버 로그인"}
                            </button>
                        </div>

                        <div>
                            <label className="font-semibold text-slate-800">어떤 주제의 글을 발행할까요? (콤마로 구분)</label>
                            <p className="text-xs text-slate-500 mb-2 mt-1">예: IT 리뷰, 주식 공부, 강남역 맛집</p>
                            <input
                                type="text"
                                value={categoriesText}
                                onChange={(e) => setCategoriesText(e.target.value)}
                                className="w-full rounded-xl border border-slate-300 px-4 py-3 bg-slate-50 focus:bg-white focus:ring-2 focus:ring-indigo-500"
                                placeholder="카테고리를 입력해주세요"
                            />
                            <p className="text-sm text-indigo-600 mt-2">✨ <b>다양한 생각들</b> 카테고리는 다양한 주제의 글을 모으기 위해 필수적으로 자동 추가됩니다. 블로그에도 <b>다양한 생각들</b> 카테고리를 꼭 하나 만들어주세요!</p>
                        </div>

                        <div className="flex justify-between pt-4">
                            <button onClick={() => setStep(0)} className="text-slate-500 font-semibold px-4 py-2 hover:bg-slate-100 rounded-lg transition-colors">← 이전</button>
                            <button onClick={handleSaveCategoryStep} disabled={saving} className="bg-gradient-to-r from-indigo-600 to-blue-600 text-white px-8 py-3 rounded-full font-bold shadow-md hover:shadow-lg transition-all active:scale-95 text-lg">
                                {saving ? "저장 중..." : "다음 단계로 →"}
                            </button>
                        </div>
                    </div>
                )}

                {step === 2 && (
                    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4">
                        <h2 className="text-xl font-bold">3단계. 나만의 AI 페르소나 설계</h2>
                        <p className="text-sm text-slate-600">블로그를 대신 작성해줄 AI의 직업, 성격, 말투를 세밀하게 설정합니다.</p>

                        <div className="space-y-4">
                            <div>
                                <label className="font-semibold text-slate-800 block mb-1">나는 누구인가요? (정체성 / 직업)</label>
                                <input type="text" value={identity} onChange={(e) => setIdentity(e.target.value)} placeholder="예: 5년 차 IT 개발자, 주식 투자 3년차 직장인" className="w-full rounded-xl border border-slate-300 px-4 py-2" />
                            </div>

                            <div>
                                <label className="font-semibold text-slate-800 block mb-1">말투는 어떤가요? (Tone)</label>
                                <input type="text" value={toneHint} onChange={(e) => setToneHint(e.target.value)} placeholder="예: 친절하고 전문적인 존댓말, 유머러스한 반말" className="w-full rounded-xl border border-slate-300 px-4 py-2" />
                            </div>

                            <div>
                                <label className="font-semibold text-slate-800 block mb-1">관심사 / 특징 (콤마로 구분)</label>
                                <input type="text" value={interestsText} onChange={(e) => setInterestsText(e.target.value)} placeholder="예: 최신 전자기기 탐구, 카페 인테리어" className="w-full rounded-xl border border-slate-300 px-4 py-2" />
                            </div>
                        </div>

                        <div className="flex justify-between pt-4">
                            <button onClick={() => setStep(1)} className="text-slate-500 font-semibold px-4 py-2 hover:bg-slate-100 rounded-lg transition-colors">← 이전</button>
                            <button onClick={handleSavePersonaStep} disabled={saving} className="bg-gradient-to-r from-indigo-600 to-blue-600 text-white px-8 py-3 rounded-full font-bold shadow-md hover:shadow-lg transition-all active:scale-95 text-lg">
                                {saving ? "저장 중..." : "다음 단계로 →"}
                            </button>
                        </div>
                        {stepMessage && <p className="text-red-500 text-sm mt-2">{stepMessage}</p>}
                    </div>
                )}

                {step === 3 && (
                    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4">
                        <h2 className="text-xl font-bold">4단계. 스케줄링 완성!</h2>
                        <p className="text-sm text-slate-600">마지막으로 매일 몇 개의 글을 쓸지 스케줄을 확정합니다.</p>

                        <div className="bg-slate-50 p-6 rounded-2xl border border-slate-200">
                            <label className="flex items-center justify-between text-base font-semibold text-slate-800">
                                하루 총 발행 목표량
                                <span className="text-indigo-600 font-bold bg-indigo-100 px-3 py-1 rounded-lg">{dailyPostsTarget} 포스트</span>
                            </label>
                            <input
                                type="range" min={1} max={5} value={dailyPostsTarget}
                                onChange={(e) => handleDailyTargetChange(Number(e.target.value))}
                                className="mt-4 w-full accent-indigo-600"
                            />
                        </div>

                        <div className="rounded-2xl border border-slate-200 overflow-hidden">
                            <div className="bg-slate-100 px-4 py-3 font-semibold text-slate-700 text-sm border-b">
                                카테고리별 발행 비중
                            </div>
                            <div className="divide-y divide-slate-100 p-2">
                                {categoryAllocations.map((item, index) => (
                                    <div key={item.category} className="flex items-center gap-4 px-2 py-3 hover:bg-slate-50 transition-colors rounded-xl">
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
                                        <div className="w-20"><input type="number" min={0} max={5} value={item.count} onChange={(e) => handleAllocationChange(index, { count: Number(e.target.value) })} className="w-full rounded-lg border-slate-300 text-center font-bold text-indigo-700 bg-indigo-50" /></div>
                                    </div>
                                ))}
                            </div>
                        </div>

                        <div className="flex justify-between pt-6 mt-4 border-t border-slate-100">
                            <button onClick={() => setStep(2)} className="text-slate-500 font-semibold px-4 py-2 hover:bg-slate-100 rounded-lg transition-colors">← 이전</button>
                            <button onClick={handleCompleteSetup} disabled={saving || allocationTotal !== trendDailyTarget} className="bg-gradient-to-r from-emerald-500 to-green-500 text-white px-8 py-3 rounded-full font-bold shadow-lg hover:shadow-xl transition-all active:scale-95 text-lg hover:-translate-y-1">
                                {saving ? "설정 완료 처리중..." : "🎉 설정 끝내고 시작하기!"}
                            </button>
                        </div>
                        {allocationTotal !== trendDailyTarget && (
                            <p className="text-amber-600 text-sm text-right mt-2 font-medium">✨ 카테고리 비중(총합 {allocationTotal})이 일간 목표({trendDailyTarget})와 일치해야 합니다.</p>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}
