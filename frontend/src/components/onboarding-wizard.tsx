"use client";

import { useEffect, useMemo, useState } from "react";
import {
    DEFAULT_FALLBACK_CATEGORY,
    completeOnboarding,
    fetchNaverConnectStatus,
    fetchOnboardingStatus,
    fetchRouterSettings,
    saveOnboardingCategories,
    saveOnboardingSchedule,
    saveRouterSettings,
    savePersonaLab,
    startNaverConnect,
    verifyApiKey,
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
    const fallbackCategories = normalizedCategories.length > 0 ? normalizedCategories : [DEFAULT_FALLBACK_CATEGORY];

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
    const [imageApiKeys, setImageApiKeys] = useState<Record<string, string>>({
        pexels: "", together: "", fal: "", openai_image: "",
    });
    const [imageEngine, setImageEngine] = useState("pexels");
    const [imageEnabled, setImageEnabled] = useState(true);
    const [imagesPerPost, setImagesPerPost] = useState(1);
    const [apiStatuses, setApiStatuses] = useState<Record<string, { valid: boolean; message: string; checking: boolean }>>({});

    // Step 2: Persona Edit
    const [personaId, setPersonaId] = useState("P1");
    const [identity, setIdentity] = useState("");
    const [toneHint, setToneHint] = useState("");
    const [interestsText, setInterestsText] = useState("");
    const [mbtiEnabled, setMbtiEnabled] = useState(false);
    const [mbti, setMbti] = useState("");
    const [mbtiConfidence, setMbtiConfidence] = useState(70);
    const [ageGroup, setAgeGroup] = useState("30대");
    const [gender, setGender] = useState("남성");

    // Step 3: Naver & Blog Info
    const [naverStatus, setNaverStatus] = useState<NaverConnectStatusResponse | null>(null);
    const [naverConnecting, setNaverConnecting] = useState(false);

    const [categoriesText, setCategoriesText] = useState("");

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

                // fallback category가 항상 포함되도록 보장
                const cats = response.categories || [];
                if (!cats.includes(DEFAULT_FALLBACK_CATEGORY)) cats.push(DEFAULT_FALLBACK_CATEGORY);
                setCategoriesText(cats.join(", "));

                setInterestsText((response.interests || []).join(", "));
                if (response.voice_profile) {
                    const savedMbti = ((response.voice_profile.mbti as string) || "").trim().toUpperCase();
                    const savedMbtiEnabled = Boolean(
                        response.voice_profile.mbti_enabled && savedMbti,
                    );
                    setMbti(savedMbti);
                    setMbtiEnabled(savedMbtiEnabled);
                    setMbtiConfidence(
                        Math.max(0, Math.min(100, Number(response.voice_profile.mbti_confidence ?? 70))),
                    );
                    setAgeGroup((response.voice_profile.age_group as string) || "30대");
                    setGender((response.voice_profile.gender as string) || "남성");
                }

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
        "2. 페르소나 설계",
        "3. 네이버 & 주제 설정",
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

    const mbtiWeightPercent = useMemo(() => {
        if (!mbtiEnabled) return 0;
        return Math.round(10 + (mbtiConfidence / 100) * 10);
    }, [mbtiEnabled, mbtiConfidence]);

    async function handleVerifyKey(provider: string, key: string) {
        if (!key) return;
        setApiStatuses((prev) => ({ ...prev, [provider]: { valid: false, message: "", checking: true } }));
        try {
            const res = await verifyApiKey({ provider, api_key: key });
            setApiStatuses((prev) => ({ ...prev, [provider]: { valid: res.valid, message: res.message, checking: false } }));
        } catch {
            setApiStatuses((prev) => ({ ...prev, [provider]: { valid: false, message: "검증 실패", checking: false } }));
        }
    }

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
            // fallback category 강제 포함 로직
            let modifiedCatText = categoriesText;
            if (!modifiedCatText.includes(DEFAULT_FALLBACK_CATEGORY)) {
                modifiedCatText = modifiedCatText ? modifiedCatText + `, ${DEFAULT_FALLBACK_CATEGORY}` : DEFAULT_FALLBACK_CATEGORY;
            }

            await saveOnboardingCategories({
                categories: parseCommaValues(modifiedCatText),
                fallback_category: DEFAULT_FALLBACK_CATEGORY,
            });
            setCategoriesText(modifiedCatText);
            setStep(3);
        } catch (error) {
            setStepMessage(error instanceof Error ? error.message : "저장 실패");
        } finally {
            setSaving(false);
        }
    }

    async function handleSavePersonaStep() {
        setSaving(true);
        try {
            const resolvedMbti = (mbti || "").trim().toUpperCase();
            if (mbtiEnabled && !resolvedMbti) {
                setStepMessage("MBTI 보정을 사용하려면 MBTI를 선택해주세요.");
                setSaving(false);
                return;
            }
            await savePersonaLab({
                persona_id: personaId,
                identity,
                target_audience: "일반 대중",
                tone_hint: toneHint,
                interests: parseCommaValues(interestsText),
                mbti: mbtiEnabled ? resolvedMbti : "",
                mbti_enabled: mbtiEnabled,
                mbti_confidence: mbtiEnabled ? mbtiConfidence : 0,
                age_group: ageGroup,
                gender,
                structure_score: 50,
                evidence_score: 50,
                distance_score: 50,
                criticism_score: 50,
                density_score: 50,
                style_strength: 40,
            });
            setStep(2);
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
                                    <label className="flex items-center gap-2 text-sm font-semibold uppercase">
                                        {key}
                                        {key === 'qwen' || key === 'deepseek' || key === 'openai' || key === 'claude' ? (
                                            <span className="bg-indigo-100 text-indigo-700 font-bold px-2 py-0.5 rounded-full text-xs">필수/유료</span>
                                        ) : (
                                            <span className="bg-emerald-100 text-emerald-700 font-bold px-2 py-0.5 rounded-full text-xs">무료가능</span>
                                        )}
                                        <a
                                            href={
                                                key === 'qwen' ? 'https://dash.aliyun.com/' :
                                                    key === 'deepseek' ? 'https://platform.deepseek.com/' :
                                                        key === 'gemini' ? 'https://aistudio.google.com/' :
                                                            key === 'openai' ? 'https://platform.openai.com/' :
                                                                key === 'claude' ? 'https://console.anthropic.com/' : '#'
                                            }
                                            target="_blank"
                                            rel="noreferrer"
                                            className="ml-auto text-xs text-blue-500 hover:underline"
                                        >
                                            키 발급 →
                                        </a>
                                    </label>
                                    <div className="relative mt-1">
                                        <input
                                            type="password"
                                            value={textApiKeys[key] || ""}
                                            onChange={(e) => setTextApiKeys(prev => ({ ...prev, [key]: e.target.value }))}
                                            onBlur={(e) => handleVerifyKey(key, e.target.value)}
                                            placeholder={textApiMasks[key] ? `${textApiMasks[key]} (이미 등록됨)` : "API 키 입력"}
                                            className="w-full rounded-xl border border-slate-300 px-4 py-3 bg-slate-50 focus:bg-white focus:ring-2 focus:ring-indigo-500 transition-all pr-12"
                                        />
                                        <div className="absolute right-3 top-1/2 -translate-y-1/2">
                                            {apiStatuses[key]?.checking ? "⏳" : apiStatuses[key]?.valid ? "✅" : apiStatuses[key]?.message ? "❌" : ""}
                                        </div>
                                    </div>
                                    {apiStatuses[key]?.message && !apiStatuses[key].valid && (
                                        <p className="text-red-500 text-xs mt-1">{apiStatuses[key].message}</p>
                                    )}
                                </div>
                            ))}
                        </div>

                        {/* 이미지 설정 섹션 */}
                        <div className="border-t border-slate-200 pt-6 space-y-4">
                            <div>
                                <h3 className="text-base font-bold text-slate-800">이미지 설정 (선택사항)</h3>
                                <p className="text-sm text-slate-500 mt-1">포스팅에 자동으로 이미지를 삽입할 경우 설정합니다.</p>
                            </div>

                            {/* 이미지 엔진 선택 */}
                            <div>
                                <label className="text-sm font-semibold text-slate-700 block mb-2">이미지 소스 전략</label>
                                <div className="grid grid-cols-3 gap-2">
                                    {[
                                        { value: "pexels", label: "무료 스톡만", desc: "Pexels 무료 사진" },
                                        { value: "mixed", label: "혼합 (권장)", desc: "스톡 + AI 교대" },
                                        { value: "ai_only", label: "AI 생성만", desc: "Together/Fal" },
                                    ].map((opt) => (
                                        <button
                                            key={opt.value}
                                            type="button"
                                            onClick={() => setImageEngine(opt.value)}
                                            className={`p-3 rounded-xl border-2 text-left transition-all ${imageEngine === opt.value ? "border-indigo-500 bg-indigo-50" : "border-slate-200 bg-white hover:border-slate-300"}`}
                                        >
                                            <div className="font-semibold text-xs text-slate-800">{opt.label}</div>
                                            <div className="text-xs text-slate-500 mt-0.5">{opt.desc}</div>
                                        </button>
                                    ))}
                                </div>
                            </div>

                            {/* 조건부: pexels 또는 mixed 선택 시 Pexels 키 입력 */}
                            {(imageEngine === "pexels" || imageEngine === "mixed") && (
                                <div>
                                    <label className="flex items-center gap-2 text-sm font-semibold">
                                        PEXELS API KEY
                                        <span className="bg-emerald-100 text-emerald-700 font-bold px-2 py-0.5 rounded-full text-xs">무료</span>
                                        <a href="https://www.pexels.com/api/" target="_blank" rel="noreferrer" className="ml-auto text-xs text-blue-500 hover:underline">키 발급 →</a>
                                    </label>
                                    <div className="relative mt-1">
                                        <input
                                            type="password"
                                            value={imageApiKeys["pexels"] || ""}
                                            onChange={(e) => setImageApiKeys(prev => ({ ...prev, pexels: e.target.value }))}
                                            onBlur={(e) => handleVerifyKey("pexels", e.target.value)}
                                            placeholder="Pexels API 키 입력"
                                            className="w-full rounded-xl border border-slate-300 px-4 py-3 bg-slate-50 focus:bg-white focus:ring-2 focus:ring-indigo-500 transition-all pr-12"
                                        />
                                        <div className="absolute right-3 top-1/2 -translate-y-1/2">
                                            {apiStatuses["pexels"]?.checking ? "⏳" : apiStatuses["pexels"]?.valid ? "✅" : apiStatuses["pexels"]?.message ? "❌" : ""}
                                        </div>
                                    </div>
                                    {apiStatuses["pexels"]?.message && !apiStatuses["pexels"].valid && (
                                        <p className="text-red-500 text-xs mt-1">{apiStatuses["pexels"].message}</p>
                                    )}
                                </div>
                            )}

                            {/* 조건부: mixed 또는 ai_only 선택 시 AI 이미지 키 입력 */}
                            {(imageEngine === "mixed" || imageEngine === "ai_only") && (
                                <div className="grid gap-4 sm:grid-cols-2">
                                    {[
                                        { key: "fal", label: "FAL API KEY", href: "https://fal.ai/", badge: "유료" },
                                        { key: "together", label: "TOGETHER API KEY", href: "https://www.together.ai/", badge: "무료가능" },
                                    ].map(({ key, label, href, badge }) => (
                                        <div key={key}>
                                            <label className="flex items-center gap-2 text-sm font-semibold">
                                                {label}
                                                <span className={`font-bold px-2 py-0.5 rounded-full text-xs ${badge === "무료가능" ? "bg-emerald-100 text-emerald-700" : "bg-amber-100 text-amber-700"}`}>{badge}</span>
                                                <a href={href} target="_blank" rel="noreferrer" className="ml-auto text-xs text-blue-500 hover:underline">키 발급 →</a>
                                            </label>
                                            <div className="relative mt-1">
                                                <input
                                                    type="password"
                                                    value={imageApiKeys[key] || ""}
                                                    onChange={(e) => setImageApiKeys(prev => ({ ...prev, [key]: e.target.value }))}
                                                    onBlur={(e) => handleVerifyKey(key, e.target.value)}
                                                    placeholder={`${label} 입력`}
                                                    className="w-full rounded-xl border border-slate-300 px-4 py-3 bg-slate-50 focus:bg-white focus:ring-2 focus:ring-indigo-500 transition-all pr-12"
                                                />
                                                <div className="absolute right-3 top-1/2 -translate-y-1/2">
                                                    {apiStatuses[key]?.checking ? "⏳" : apiStatuses[key]?.valid ? "✅" : apiStatuses[key]?.message ? "❌" : ""}
                                                </div>
                                            </div>
                                            {apiStatuses[key]?.message && !apiStatuses[key].valid && (
                                                <p className="text-red-500 text-xs mt-1">{apiStatuses[key].message}</p>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            )}
                        </div>

                        <div className="flex justify-end pt-4">
                            <button onClick={handleSaveRouterStep} disabled={routerSaving} className="bg-gradient-to-r from-indigo-600 to-blue-600 text-white px-8 py-3 rounded-full font-bold shadow-md hover:shadow-lg transition-all active:scale-95 text-lg">
                                {routerSaving ? "저장 중..." : "다음 단계로 →"}
                            </button>
                        </div>
                        {routerMessage && <p className="text-red-500 text-sm mt-2">{routerMessage}</p>}
                    </div>
                )}

                {step === 2 && (
                    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4">
                        <h2 className="text-xl font-bold">3단계. 네이버 로그인 & 카테고리 (Naver 연동)</h2>
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
                            <p className="text-sm text-indigo-600 mt-2">✨ <b>{DEFAULT_FALLBACK_CATEGORY}</b> 카테고리는 다양한 주제의 글을 모으기 위해 필수적으로 자동 추가됩니다. 블로그에도 <b>{DEFAULT_FALLBACK_CATEGORY}</b> 카테고리를 꼭 하나 만들어주세요!</p>
                        </div>

                        <div className="flex justify-between pt-4">
                            <button onClick={() => setStep(1)} className="text-slate-500 font-semibold px-4 py-2 hover:bg-slate-100 rounded-lg transition-colors">← 이전</button>
                            <button onClick={handleSaveCategoryStep} disabled={saving} className="bg-gradient-to-r from-indigo-600 to-blue-600 text-white px-8 py-3 rounded-full font-bold shadow-md hover:shadow-lg transition-all active:scale-95 text-lg">
                                {saving ? "저장 중..." : "다음 단계로 →"}
                            </button>
                        </div>
                    </div>
                )}

                {step === 1 && (
                    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4">
                        <h2 className="text-xl font-bold">2단계. 나만의 AI 페르소나 설계</h2>
                        <p className="text-sm text-slate-600">블로그를 대신 작성해줄 AI의 직업, 성격, 성향을 세밀하게 설정합니다.</p>

                        <div className="space-y-4">
                            <div className="grid grid-cols-3 gap-4">
                                <div>
                                    <label className="font-semibold text-slate-800 block mb-1">성별</label>
                                    <select value={gender} onChange={(e) => setGender(e.target.value)} className="w-full rounded-xl border border-slate-300 px-4 py-2 bg-white">
                                        <option value="남성">남성</option>
                                        <option value="여성">여성</option>
                                        <option value="비공개">비공개</option>
                                    </select>
                                </div>
                                <div>
                                    <label className="font-semibold text-slate-800 block mb-1">연령대</label>
                                    <select value={ageGroup} onChange={(e) => setAgeGroup(e.target.value)} className="w-full rounded-xl border border-slate-300 px-4 py-2 bg-white">
                                        <option value="20대">20대</option>
                                        <option value="30대">30대</option>
                                        <option value="40대">40대</option>
                                        <option value="50대 이상">50대 이상</option>
                                    </select>
                                </div>
                                <div>
                                    <label className="font-semibold text-slate-800 block mb-1">MBTI 보정 (선택)</label>
                                    <label className="flex items-center gap-2 text-sm text-slate-600 mb-2">
                                        <input
                                            type="checkbox"
                                            checked={mbtiEnabled}
                                            onChange={(e) => {
                                                const enabled = e.target.checked;
                                                setMbtiEnabled(enabled);
                                                if (enabled && !mbti) {
                                                    setMbti("ENFP");
                                                }
                                            }}
                                        />
                                        MBTI를 질문지 결과에 보조 반영
                                    </label>
                                    <select
                                        value={mbti}
                                        onChange={(e) => setMbti(e.target.value)}
                                        disabled={!mbtiEnabled}
                                        className="w-full rounded-xl border border-slate-300 px-4 py-2 bg-white disabled:bg-slate-100 disabled:text-slate-400"
                                    >
                                        <option value="">선택 안함</option>
                                        {["ISTJ", "ISFJ", "INFJ", "INTJ", "ISTP", "ISFP", "INFP", "INTP", "ESTP", "ESFP", "ENFP", "ENTP", "ESTJ", "ESFJ", "ENFJ", "ENTJ"].map((m) => (
                                            <option key={m} value={m}>{m}</option>
                                        ))}
                                    </select>
                                    {mbtiEnabled && (
                                        <div className="mt-3">
                                            <label className="text-xs text-slate-600 flex items-center justify-between">
                                                MBTI 확신도
                                                <span className="font-semibold text-indigo-700">{mbtiConfidence}</span>
                                            </label>
                                            <input
                                                type="range"
                                                min={0}
                                                max={100}
                                                value={mbtiConfidence}
                                                onChange={(e) => setMbtiConfidence(Number(e.target.value))}
                                                className="w-full accent-indigo-600"
                                            />
                                            <p className="text-[11px] text-slate-500 mt-1">
                                                반영 비율: 질문지 {100 - mbtiWeightPercent}% + MBTI {mbtiWeightPercent}%
                                            </p>
                                        </div>
                                    )}
                                </div>
                            </div>
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
                            <button onClick={() => setStep(0)} className="text-slate-500 font-semibold px-4 py-2 hover:bg-slate-100 rounded-lg transition-colors">← 이전</button>
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
                                type="range" min={3} max={5} value={dailyPostsTarget}
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
