"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
    fetchDashboard,
    fetchRouterSettings,
    quoteRouterSettings,
    saveRouterSettings,
    startNaverConnect,
    fetchNaverConnectStatus,
    type NaverConnectStatusResponse,
    type OnboardingStatusResponse,
    type RouterSettingsResponse,
    type RouterQuoteResponse,
} from "@/lib/api";
import { compactKeys, formatKrw } from "@/lib/utils/formatters";

type EngineSettingsCardProps = {
    initialRouterSettings: RouterSettingsResponse;
    initialNaverStatus: NaverConnectStatusResponse | null;
    categoryAllocations?: OnboardingStatusResponse["category_allocations"];
};

type StrategyMode = "cost" | "balanced" | "quality";

function normalizeStrategyMode(raw: unknown): StrategyMode {
    const value = String(raw || "").trim().toLowerCase();
    if (value === "quality") return "quality";
    if (value === "balanced") return "balanced";
    return "cost";
}

const TOPIC_MODE_LABELS: Array<{ value: string; label: string }> = [
    { value: "cafe", label: "카페/일상" },
    { value: "it", label: "IT" },
    { value: "finance", label: "경제" },
    { value: "parenting", label: "육아" },
];

const AI_QUOTA_OPTIONS = [
    { value: "0", label: "없음" },
    { value: "1", label: "1장" },
    { value: "2", label: "2장" },
    { value: "3", label: "3장" },
    { value: "4", label: "4장" },
    { value: "all", label: "전부 AI" },
] as const;

export default function EngineSettingsCard({
    initialRouterSettings,
    initialNaverStatus,
    categoryAllocations = [],
}: EngineSettingsCardProps) {
    const [routerSaving, setRouterSaving] = useState(false);
    const [routerLoading, setRouterLoading] = useState(false);
    const [routerMessage, setRouterMessage] = useState("");

    const [strategyMode, setStrategyMode] = useState<StrategyMode>(
        normalizeStrategyMode(initialRouterSettings.settings.strategy_mode)
    );
    const [textApiKeys, setTextApiKeys] = useState<Record<string, string>>({});
    const [imageApiKeys, setImageApiKeys] = useState<Record<string, string>>({});

    const [textApiMasks, setTextApiMasks] = useState<Record<string, string>>(
        initialRouterSettings.settings.text_api_keys_masked || {}
    );
    const [imageApiMasks, setImageApiMasks] = useState<Record<string, string>>(
        initialRouterSettings.settings.image_api_keys_masked || {}
    );
    const [imageEngine, setImageEngine] = useState(initialRouterSettings.settings.image_engine || "pexels");
    const [imageEnabled, setImageEnabled] = useState(Boolean(initialRouterSettings.settings.image_enabled));
    const [imageAiEngine, setImageAiEngine] = useState(
        initialRouterSettings.settings.image_ai_engine || "together_flux"
    );
    const [imageTopicQuotaOverrides, setImageTopicQuotaOverrides] = useState<Record<string, string>>(
        (initialRouterSettings.settings.image_topic_quota_overrides as Record<string, string>) || {}
    );
    const [dirtyTopicKeys, setDirtyTopicKeys] = useState<Set<string>>(new Set());
    const [trafficFeedbackStrongMode, setTrafficFeedbackStrongMode] = useState(
        Boolean(initialRouterSettings.settings.traffic_feedback_strong_mode)
    );
    const [imagesPerPostMin, setImagesPerPostMin] = useState(
        Math.max(0, Math.min(4, Number(initialRouterSettings.settings.images_per_post_min || 0)))
    );
    const [imagesPerPostMax, setImagesPerPostMax] = useState(
        Math.max(0, Math.min(4, Number(
            initialRouterSettings.settings.images_per_post_max ?? initialRouterSettings.settings.images_per_post ?? 1
        )))
    );

    const [textModelMatrix, setTextModelMatrix] = useState<Array<Record<string, unknown>>>(
        initialRouterSettings.matrix.text_models || []
    );
    const [imageModelMatrix, setImageModelMatrix] = useState<Array<Record<string, unknown>>>(
        initialRouterSettings.matrix.image_models || []
    );

    const [routerQuote, setRouterQuote] = useState<RouterQuoteResponse | null>({
        strategy_mode: normalizeStrategyMode(initialRouterSettings.settings.strategy_mode),
        roles: initialRouterSettings.roles || {},
        estimate: {
            currency: "KRW",
            text_cost_krw: Number(initialRouterSettings.quote.text_cost_krw || 0),
            image_cost_krw: Number(initialRouterSettings.quote.image_cost_krw || 0),
            total_cost_krw: Number(initialRouterSettings.quote.total_cost_krw || 0),
            cost_min_krw: Number(initialRouterSettings.quote.cost_min_krw ?? initialRouterSettings.quote.total_cost_krw ?? 0),
            cost_max_krw: Number(initialRouterSettings.quote.cost_max_krw ?? initialRouterSettings.quote.total_cost_krw ?? 0),
            quality_score: Number(initialRouterSettings.quote.quality_score || 0),
            daily_posts: Number(initialRouterSettings.quote.daily_posts || 0),
            monthly_cost_krw: Number(initialRouterSettings.quote.monthly_cost_krw || 0),
            monthly_cost_min_krw: Number(initialRouterSettings.quote.monthly_cost_min_krw || 0),
            monthly_cost_max_krw: Number(initialRouterSettings.quote.monthly_cost_max_krw || 0),
        },
        image: {},
        available_text_models: [],
    });
    const [strategyPreviews, setStrategyPreviews] = useState<Record<StrategyMode, {
        total_cost_krw: number;
        monthly_cost_krw: number;
        quality_score: number;
        main_model_label: string;
        cheap_model_label: string;
    } | null>>({
        cost: null,
        balanced: null,
        quality: null,
    });
    const quoteGenerationRef = useRef(0);
    const [competitionState, setCompetitionState] = useState(
        initialRouterSettings.competition || {
            phase: "eval_continuous",
            week_start: "",
            apply_at: "",
            shadow_mode: false,
            champion_model: "",
            challenger_model: "",
            fallback_category: "다양한 생각들",
            slot_type: "default",
            eval_model_today: "",
            registered_models: [],
        },
    );
    const [challengerModel, setChallengerModel] = useState<string>(
        String(initialRouterSettings.competition?.challenger_model || "")
    );
    const [championHistory, setChampionHistory] = useState<Array<{
        week_start: string;
        champion_model: string;
        challenger_model: string;
        avg_champion_score: number;
        topic_mode_scores: Record<string, number>;
        cost_won: number;
        early_terminated: boolean;
        shadow_only: boolean;
    }>>([]);

    // Load champion_history for test result column
    useEffect(() => {
        fetchDashboard().then((d) => {
            if (d.metrics?.champion_history) {
                setChampionHistory(d.metrics.champion_history as typeof championHistory);
            }
        }).catch(() => {});
    }, []);

    const [naverStatus, setNaverStatus] = useState<NaverConnectStatusResponse | null>(initialNaverStatus);
    const [naverConnecting, setNaverConnecting] = useState(false);

    const topicImagesMap = useMemo(() => {
        const map: Record<string, number> = {};
        for (const item of categoryAllocations || []) {
            const topicMode = String(item?.topic_mode || "cafe").trim().toLowerCase();
            if (!topicMode) continue;
            const current = map[topicMode] ?? 0;
            const itemImages = Math.max(0, Math.min(4, Number(item?.images_per_post ?? 2)));
            map[topicMode] = Math.max(current, itemImages);
        }
        return map;
    }, [categoryAllocations]);

    const parserModelLabel = useMemo(() => {
        const role = routerQuote?.roles?.parser;
        if (!role || typeof role !== "object") return "-";
        const label = (role as Record<string, unknown>).label;
        return typeof label === "string" ? label : "-";
    }, [routerQuote]);

    const preAnalysisModelLabel = useMemo(() => {
        const role = routerQuote?.roles?.pre_analysis;
        if (!role || typeof role !== "object") return parserModelLabel;
        const label = (role as Record<string, unknown>).label;
        return typeof label === "string" ? label : parserModelLabel;
    }, [routerQuote, parserModelLabel]);

    const qualityModelLabel = useMemo(() => {
        const role = routerQuote?.roles?.quality_step;
        if (!role || typeof role !== "object") return "-";
        const label = (role as Record<string, unknown>).label;
        return typeof label === "string" ? label : "-";
    }, [routerQuote]);

    const voiceModelLabel = useMemo(() => {
        const role = routerQuote?.roles?.voice_step;
        if (!role || typeof role !== "object") return "-";
        const label = (role as Record<string, unknown>).label;
        return typeof label === "string" ? label : "-";
    }, [routerQuote]);

    const sentencePolishModelLabel = useMemo(() => {
        const role = routerQuote?.roles?.sentence_polish;
        if (!role || typeof role !== "object") return parserModelLabel;
        const label = (role as Record<string, unknown>).label;
        return typeof label === "string" ? label : parserModelLabel;
    }, [routerQuote, parserModelLabel]);

    useEffect(() => {
        const timer = setTimeout(async () => {
            const generation = ++quoteGenerationRef.current;
            setRouterLoading(true);
            try {
                const basePayload = {
                    text_api_keys: compactKeys(textApiKeys),
                    image_api_keys: compactKeys(imageApiKeys),
                    image_engine: imageEngine,
                    image_ai_engine: imageAiEngine,
                    image_topic_quota_overrides: imageTopicQuotaOverrides,
                    traffic_feedback_strong_mode: trafficFeedbackStrongMode,
                    image_enabled: imageEnabled,
                    images_per_post: imagesPerPostMax,
                    images_per_post_min: imagesPerPostMin,
                    images_per_post_max: imagesPerPostMax,
                };
                const [costQuote, balancedQuote, qualityQuote] = await Promise.all([
                    quoteRouterSettings({ ...basePayload, strategy_mode: "cost" }),
                    quoteRouterSettings({ ...basePayload, strategy_mode: "balanced" }),
                    quoteRouterSettings({ ...basePayload, strategy_mode: "quality" }),
                ]);
                if (generation !== quoteGenerationRef.current) {
                    return;
                }
                const extractPreview = (quote: RouterQuoteResponse) => ({
                    total_cost_krw: quote.estimate.total_cost_krw,
                    monthly_cost_krw: quote.estimate.monthly_cost_krw
                        || quote.estimate.total_cost_krw * (quote.estimate.daily_posts || 8) * 30,
                    quality_score: quote.estimate.quality_score,
                    main_model_label: String((quote.roles?.quality_step as Record<string, unknown>)?.label || "-"),
                    cheap_model_label: String((quote.roles?.pre_analysis as Record<string, unknown>)?.label || "-"),
                });
                setStrategyPreviews({
                    cost: extractPreview(costQuote),
                    balanced: extractPreview(balancedQuote),
                    quality: extractPreview(qualityQuote),
                });
                const currentQuote = strategyMode === "quality"
                    ? qualityQuote
                    : strategyMode === "balanced"
                        ? balancedQuote
                        : costQuote;
                setRouterQuote(currentQuote);
            } catch {
                // 미리보기 실패는 저장 동작을 막지 않는다.
            } finally {
                if (generation === quoteGenerationRef.current) {
                    setRouterLoading(false);
                }
            }
        }, 350);
        return () => clearTimeout(timer);
    }, [strategyMode, textApiKeys, imageApiKeys, imageEngine, imageAiEngine, imageTopicQuotaOverrides, trafficFeedbackStrongMode, imageEnabled, imagesPerPostMin, imagesPerPostMax]);

    function handleTextKeyChange(keyId: string, value: string) {
        setTextApiKeys((prev) => ({ ...prev, [keyId]: value }));
    }

    function handleImageKeyChange(keyId: string, value: string) {
        setImageApiKeys((prev) => ({ ...prev, [keyId]: value }));
    }

    function handleTopicQuotaChange(topicMode: string, newQuota: string) {
        setImageTopicQuotaOverrides((prev) => ({
            ...prev,
            [topicMode]: newQuota,
        }));
        setDirtyTopicKeys((prev) => {
            const next = new Set(prev);
            next.add(topicMode);
            return next;
        });
    }

    async function handleSaveRouterSettings() {
        setRouterSaving(true);
        setRouterMessage("");
        try {
            let finalOverrides = imageTopicQuotaOverrides;
            try {
                const latestSettings = await fetchRouterSettings();
                const serverOverrides = (latestSettings.settings.image_topic_quota_overrides as Record<string, string>) || {};
                finalOverrides = { ...serverOverrides };
                if (dirtyTopicKeys.size > 0) {
                    for (const key of dirtyTopicKeys) {
                        finalOverrides[key] = imageTopicQuotaOverrides[key] ?? serverOverrides[key] ?? "0";
                    }
                }
            } catch {
                finalOverrides = imageTopicQuotaOverrides;
            }
            const saved = await saveRouterSettings({
                strategy_mode: strategyMode,
                text_api_keys: compactKeys(textApiKeys),
                image_api_keys: compactKeys(imageApiKeys),
                image_engine: imageEngine,
                image_ai_engine: imageAiEngine,
                image_topic_quota_overrides: finalOverrides,
                traffic_feedback_strong_mode: trafficFeedbackStrongMode,
                image_enabled: imageEnabled,
                images_per_post: imagesPerPostMax,
                images_per_post_min: imagesPerPostMin,
                images_per_post_max: imagesPerPostMax,
                challenger_model: challengerModel,
            });
            setStrategyMode(normalizeStrategyMode(saved.settings.strategy_mode));
            setTextApiMasks(saved.settings.text_api_keys_masked || {});
            setImageApiMasks(saved.settings.image_api_keys_masked || {});
            setImageEngine(saved.settings.image_engine || "pexels");
            setImageEnabled(Boolean(saved.settings.image_enabled));
            setImageAiEngine(saved.settings.image_ai_engine || "together_flux");
            setImageTopicQuotaOverrides((saved.settings.image_topic_quota_overrides as Record<string, string>) || {});
            setDirtyTopicKeys(new Set());
            setTrafficFeedbackStrongMode(Boolean(saved.settings.traffic_feedback_strong_mode));
            setImagesPerPostMin(Math.max(0, Math.min(4, Number(saved.settings.images_per_post_min || 0))));
            setImagesPerPostMax(Math.max(0, Math.min(4, Number(
                saved.settings.images_per_post_max ?? saved.settings.images_per_post ?? 1
            ))));
            setTextModelMatrix(saved.matrix.text_models || []);
            setImageModelMatrix(saved.matrix.image_models || []);
            setCompetitionState(saved.competition || competitionState);
            setChallengerModel(String(saved.competition?.challenger_model || challengerModel));
            setRouterQuote((prev) => ({
                strategy_mode: normalizeStrategyMode(saved.settings.strategy_mode),
                roles: saved.roles || prev?.roles || {},
                estimate: {
                    currency: "KRW",
                    text_cost_krw: Number(saved.quote.text_cost_krw || 0),
                    image_cost_krw: Number(saved.quote.image_cost_krw || 0),
                    total_cost_krw: Number(saved.quote.total_cost_krw || 0),
                    cost_min_krw: Number(saved.quote.cost_min_krw ?? saved.quote.total_cost_krw ?? 0),
                    cost_max_krw: Number(saved.quote.cost_max_krw ?? saved.quote.total_cost_krw ?? 0),
                    quality_score: Number(saved.quote.quality_score || 0),
                    daily_posts: Number(saved.quote.daily_posts || 0),
                    monthly_cost_krw: Number(saved.quote.monthly_cost_krw || 0),
                    monthly_cost_min_krw: Number(saved.quote.monthly_cost_min_krw || 0),
                    monthly_cost_max_krw: Number(saved.quote.monthly_cost_max_krw || 0),
                },
                image: prev?.image || {},
                available_text_models: prev?.available_text_models || [],
            }));
            setRouterMessage("라우터/견적 설정이 저장되었습니다.");
        } catch (requestError) {
            const message = requestError instanceof Error ? requestError.message : "라우터 설정 저장에 실패했습니다.";
            setRouterMessage(message);
        } finally {
            setRouterSaving(false);
        }
    }

    async function handleStartNaverConnect() {
        setNaverConnecting(true);
        setRouterMessage("");
        try {
            const response = await startNaverConnect({ timeout_sec: 300 });
            const statusResponse = await fetchNaverConnectStatus();
            setNaverStatus(statusResponse);
            setRouterMessage(response.message);
        } catch (requestError) {
            const message = requestError instanceof Error ? requestError.message : "네이버 연동 실행에 실패했습니다.";
            setRouterMessage(message);
        } finally {
            setNaverConnecting(false);
        }
    }

    return (
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
                Zero-Config Router
            </h2>
            <p className="mt-1 text-sm text-slate-600">
                키 조합과 전략에 따라 모델을 자동 배정하고 예상 원가/품질을 실시간으로 계산합니다.
            </p>

            {/* 분리된 두 구역 (Flex wrap) */}
            <div className="mt-4 flex flex-col gap-6 lg:flex-row lg:items-start">

                {/* 글쓰기 AI 모델 구역 */}
                <div className="flex-1 rounded-2xl border border-slate-200 bg-slate-50/50 p-5 backdrop-blur">
                    <h3 className="mb-4 flex items-center gap-2 font-semibold text-slate-800">
                        <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-blue-100 text-blue-600">📝</span>
                        글쓰기 AI 모델
                    </h3>

                    <div className="mb-4">
                        <p className="mb-2 text-sm font-semibold text-slate-700">전략 선택</p>
                        <div className="grid grid-cols-3 gap-3">
                            {([
                                { key: "cost" as const, icon: "💰", label: "가성비", desc: "최저 비용" },
                                { key: "balanced" as const, icon: "⚖️", label: "균형", desc: "비용·품질 밸런스" },
                                { key: "quality" as const, icon: "💎", label: "품질우선", desc: "최고 품질" },
                            ]).map(({ key, icon, label, desc }) => {
                                const preview = strategyPreviews[key];
                                const selected = strategyMode === key;
                                return (
                                    <button
                                        key={key}
                                        type="button"
                                        onClick={() => setStrategyMode(key)}
                                        className={`relative rounded-xl border-2 p-4 text-left transition ${selected
                                            ? "border-indigo-500 bg-indigo-50 shadow-md"
                                            : "border-slate-200 hover:border-slate-300"
                                            }`}
                                    >
                                        {selected && (
                                            <span className="absolute right-2 top-2 flex h-5 w-5 items-center justify-center rounded-full bg-indigo-500 text-[10px] text-white">✓</span>
                                        )}
                                        <div className="text-lg">{icon}</div>
                                        <p className={`mt-1 text-sm font-bold ${selected ? "text-indigo-700" : "text-slate-800"}`}>
                                            {label}
                                        </p>
                                        <p className="text-[11px] text-slate-500">{desc}</p>
                                        {preview ? (
                                            <div className="mt-3 space-y-1 border-t border-slate-100 pt-2">
                                                <p className="text-xs text-slate-600">
                                                    ~{formatKrw(preview.total_cost_krw)}원<span className="text-slate-400">/편</span>
                                                </p>
                                                <p className="text-xs font-semibold text-slate-800">
                                                    월 {formatKrw(preview.monthly_cost_krw)}원
                                                </p>
                                                <p className="text-[11px] text-slate-500">품질 {preview.quality_score}점</p>
                                                <p className="mt-1 text-[10px] text-slate-400">본문: {preview.main_model_label}</p>
                                                <p className="text-[10px] text-slate-400">보조: {preview.cheap_model_label}</p>
                                            </div>
                                        ) : routerLoading ? (
                                            <p className="mt-3 text-[11px] text-slate-400">계산 중...</p>
                                        ) : null}
                                    </button>
                                );
                            })}
                        </div>
                        <p className="mt-2 text-[11px] text-slate-400">
                            설계/분석/다듬기 단계는 자동으로 저가 모델을 우선 사용하며, 월 비용은 일일 편수 기준으로 계산됩니다.
                        </p>
                    </div>

                    <div className="grid gap-3 sm:grid-cols-2">
                        {Array.from(
                            new Set(
                                textModelMatrix
                                    .map((item) => (typeof item.key_id === "string" ? item.key_id : ""))
                                    .filter((value) => value.length > 0),
                            ),
                        ).map((keyId) => (
                            <label key={keyId} className="block">
                                <span className="mb-1 block text-sm font-medium text-slate-700">
                                    {keyId.toUpperCase()} API Key
                                </span>
                                <input
                                    type="password"
                                    value={textApiKeys[keyId] || ""}
                                    onChange={(event) => handleTextKeyChange(keyId, event.target.value)}
                                    className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                                    placeholder={textApiMasks[keyId] ? `${textApiMasks[keyId]} (저장됨)` : "선택 입력"}
                                />
                            </label>
                        ))}
                    </div>
                </div>

                {/* 사진 AI 엔진 구역 */}
                <div className="flex-1 rounded-2xl border border-slate-200 bg-slate-50/50 p-5 backdrop-blur">
                    <h3 className="mb-4 flex items-center gap-2 font-semibold text-slate-800">
                        <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-100 text-emerald-600">🖼️</span>
                        사진 AI 엔진
                    </h3>

                    <label className="mb-4 flex items-center gap-2">
                        <input
                            type="checkbox"
                            checked={imageEnabled}
                            onChange={(event) => setImageEnabled(event.target.checked)}
                            className="h-4 w-4 rounded border-slate-300 text-slate-900 focus:ring-slate-900"
                        />
                        <span className="text-sm font-medium text-slate-700">이미지 엔진 활성화</span>
                    </label>

                    {imageEnabled && (
                        <div className="space-y-5">
                            <div className="rounded-xl border border-slate-200 bg-white p-4">
                                <p className="mb-3 text-sm font-semibold text-slate-800">
                                    AI 생성 엔진
                                </p>
                                <div className="flex flex-col gap-2">
                                    {[
                                        { value: "together_flux", label: "Together FLUX", desc: "무료 Tier 우선 사용", badge: "무료", badgeColor: "text-emerald-700 bg-emerald-100" },
                                        { value: "fal_flux", label: "FAL Flux", desc: "고품질 유료 이미지", badge: "유료", badgeColor: "text-amber-700 bg-amber-100" },
                                        { value: "openai_dalle3", label: "DALL-E 3", desc: "OpenAI 키 공유 사용", badge: "유료", badgeColor: "text-amber-700 bg-amber-100" },
                                    ].map((option) => (
                                        <label
                                            key={option.value}
                                            className={`flex cursor-pointer items-center gap-3 rounded-xl border-2 p-3 transition ${imageAiEngine === option.value
                                                ? "border-indigo-500 bg-indigo-50"
                                                : "border-slate-200 hover:border-slate-300"
                                                }`}
                                        >
                                            <input
                                                type="radio"
                                                name="imageAiEngine"
                                                value={option.value}
                                                checked={imageAiEngine === option.value}
                                                onChange={() => setImageAiEngine(option.value)}
                                                className="sr-only"
                                            />
                                            <span className="flex flex-1 items-center gap-2">
                                                <span className={`text-sm font-semibold ${imageAiEngine === option.value ? "text-indigo-700" : "text-slate-700"}`}>
                                                    {option.label}
                                                </span>
                                                <span className={`rounded-full px-1.5 py-0.5 text-[10px] font-semibold ${option.badgeColor}`}>
                                                    {option.badge}
                                                </span>
                                            </span>
                                            <span className="text-xs text-slate-500">{option.desc}</span>
                                        </label>
                                    ))}
                                </div>
                            </div>

                            <div className="rounded-xl border border-slate-200 bg-white p-4">
                                <p className="mb-3 text-sm font-semibold text-slate-800">토픽별 AI 이미지 배분</p>
                                <div className="space-y-2">
                                    {TOPIC_MODE_LABELS.map(({ value: topicMode, label }) => {
                                        const imagesPerPost = topicImagesMap[topicMode] ?? imagesPerPostMax;
                                        const maxAiNum = Math.max(0, Math.min(4, Number(imagesPerPost)));
                                        const rawQuota = String(imageTopicQuotaOverrides[topicMode] || "0").trim().toLowerCase();
                                        const currentQuota = rawQuota === "all"
                                            ? "all"
                                            : String(Math.max(0, Math.min(maxAiNum, Number(rawQuota) || 0)));
                                        const filteredOptions = AI_QUOTA_OPTIONS.filter(
                                            (opt) => opt.value === "0" || opt.value === "all" || Number(opt.value) <= maxAiNum
                                        );
                                        return (
                                            <div
                                                key={topicMode}
                                                className="flex items-center gap-4 rounded-lg border border-slate-100 bg-slate-50 px-3 py-2"
                                            >
                                                <span className="w-20 text-sm font-medium text-slate-700">{label}</span>
                                                <span className="text-xs text-slate-500">📷 {imagesPerPost}장/포스트</span>
                                                <select
                                                    value={currentQuota}
                                                    onChange={(event) => handleTopicQuotaChange(topicMode, event.target.value)}
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
                                    이미지/포스트 수는 <strong>카테고리 배분</strong> 탭에서 편집하세요
                                </p>
                            </div>

                            <div className="rounded-xl border border-slate-200 bg-white p-4">
                                <p className="mb-3 text-sm font-semibold text-slate-800">포스트당 이미지 범위</p>
                                <div className="flex items-center gap-3">
                                    <label className="flex items-center gap-2 text-xs text-slate-600">
                                        최소
                                        <select
                                            value={imagesPerPostMin}
                                            onChange={(event) => {
                                                const value = Number(event.target.value);
                                                setImagesPerPostMin(value);
                                                if (value > imagesPerPostMax) setImagesPerPostMax(value);
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
                                            onChange={(event) => {
                                                const value = Number(event.target.value);
                                                setImagesPerPostMax(value);
                                                if (value < imagesPerPostMin) setImagesPerPostMin(value);
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
                                    견적 비용 계산 시 사용됩니다. 실제 이미지 수는 <strong>카테고리 배분</strong> 탭의 설정을 따릅니다
                                </p>
                            </div>

                            {/* 이미지 API 키 */}
                            <div className="grid gap-3 sm:grid-cols-2">
                                {Array.from(
                                    new Set(
                                        imageModelMatrix
                                            .map((item) => (typeof item.key_id === "string" ? item.key_id : ""))
                                            .filter((value) => value.length > 0),
                                    ),
                                ).map((keyId) => {
                                    const isOpenAiImage = keyId === "openai_image";
                                    return (
                                        <label key={keyId} className="block">
                                            <span className="mb-1 block text-sm font-medium text-slate-700">
                                                {keyId.toUpperCase().replace("_", " ")} Key
                                            </span>
                                            {isOpenAiImage && (
                                                <p className="mb-1 text-xs text-slate-500">
                                                    💡 OpenAI Text 키와 동일한 키를 사용합니다
                                                </p>
                                            )}
                                            <input
                                                type="password"
                                                value={imageApiKeys[keyId] || ""}
                                                onChange={(event) => handleImageKeyChange(keyId, event.target.value)}
                                                className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                                                placeholder={imageApiMasks[keyId] ? `${imageApiMasks[keyId]} (저장됨)` : "선택 입력"}
                                            />
                                        </label>
                                    );
                                })}
                            </div>
                        </div>
                    )}
                </div>

            </div>

            <div className="mt-4 rounded-xl border border-slate-200 bg-white p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <h3 className="text-sm font-semibold text-slate-800">실시간 견적서</h3>
                    {routerLoading && <span className="text-xs text-slate-500">계산 중...</span>}
                </div>
                <div className="mt-3 grid gap-3 text-sm lg:grid-cols-3">
                    <div className="rounded-lg bg-slate-50 p-3">
                        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">예상 원가 (포스팅당)</p>
                        <p className="mt-1 text-lg font-bold text-slate-900">
                            {formatKrw(routerQuote?.estimate.cost_min_krw || 0)}원
                            {" ~ "}
                            {formatKrw(routerQuote?.estimate.cost_max_krw || 0)}원
                        </p>
                        <p className="mt-0.5 text-xs text-slate-500">글 길이·사진 수에 따라 변동</p>
                    </div>
                    <div className="rounded-lg bg-slate-50 p-3">
                        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">월 예상 원가</p>
                        <p className="mt-1 text-lg font-bold text-slate-900">
                            {formatKrw(
                                routerQuote?.estimate.monthly_cost_min_krw
                                || ((routerQuote?.estimate.cost_min_krw || 0) * (routerQuote?.estimate.daily_posts || 0) * 30)
                            )}원
                            {" ~ "}
                            {formatKrw(
                                routerQuote?.estimate.monthly_cost_max_krw
                                || ((routerQuote?.estimate.cost_max_krw || 0) * (routerQuote?.estimate.daily_posts || 0) * 30)
                            )}원
                        </p>
                        <p className="mt-0.5 text-xs text-slate-500">
                            기준: 일 {routerQuote?.estimate.daily_posts || 0}편 × 30일
                        </p>
                    </div>
                    <div className="rounded-lg bg-slate-50 p-3">
                        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">예상 품질</p>
                        <p className="mt-1 text-lg font-bold text-slate-900">
                            {routerQuote?.estimate.quality_score || 0}점
                        </p>
                        <p className="mt-0.5 text-xs text-slate-500">모델 품질 가중 평균 (100점 만점)</p>
                    </div>
                </div>

                {/* 파이프라인 단계 breakdown */}
                <div className="mt-3 border-t border-slate-100 pt-3">
                    <p className="mb-2 text-xs font-semibold text-slate-500">전체 파이프라인 배정</p>
                    <div className="flex flex-wrap gap-1.5 text-xs">
                        {[
                            { step: "① 사전분석", label: preAnalysisModelLabel, color: "border-blue-200 bg-blue-50 text-blue-700" },
                            { step: "② 문맥파싱", label: parserModelLabel, color: "border-blue-200 bg-blue-50 text-blue-700" },
                            { step: "③ 품질작성", label: qualityModelLabel, color: "border-violet-200 bg-violet-50 text-violet-700" },
                            { step: "④ 자기검증", label: qualityModelLabel, color: "border-violet-200 bg-violet-50 text-violet-700" },
                            { step: "⑤ SEO최적화", label: qualityModelLabel, color: "border-violet-200 bg-violet-50 text-violet-700" },
                            { step: "⑥ 이미지슬롯", label: qualityModelLabel, color: "border-violet-200 bg-violet-50 text-violet-700" },
                            { step: "⑦ 보이스리라이트", label: voiceModelLabel, color: "border-emerald-200 bg-emerald-50 text-emerald-700" },
                            { step: "⑧ 문장폴리시", label: sentencePolishModelLabel, color: "border-blue-200 bg-blue-50 text-blue-700" },
                        ].map(({ step, label, color }) => (
                            <span
                                key={step}
                                className={`rounded-full border px-2 py-0.5 ${color}`}
                            >
                                <span className="font-medium">{step}</span>
                                <span className="ml-1 text-slate-500">→ {label}</span>
                            </span>
                        ))}
                    </div>
                    <p className="mt-2 text-[11px] text-slate-400">
                        ④⑤⑥ 단계는 ③ 품질작성 모델을 재사용하며, ①②⑧ 단계는 저비용 보조 모델을 우선 사용합니다.
                    </p>
                </div>
            </div>


            {/* ── 모델 매트릭스 테이블 ── */}
            <div className="mt-4 rounded-xl border border-slate-200 bg-white p-4">
                <h3 className="mb-3 text-sm font-semibold text-slate-800">사용 가능한 텍스트 모델</h3>
                <div className="overflow-x-auto">
                    <table className="min-w-full text-xs">
                        <thead>
                            <tr className="border-b border-slate-100 text-left text-[10px] uppercase tracking-wide text-slate-500">
                                <th className="py-2 pr-3">프로바이더</th>
                                <th className="py-2 pr-3">모델</th>
                                <th className="py-2 pr-3 text-right">품질</th>
                                <th className="py-2 pr-3 text-right">속도</th>
                                <th className="py-2 pr-3 text-right">입력($/1M)</th>
                                <th className="py-2 pr-3 text-right">출력($/1M)</th>
                                <th className="py-2 text-right">최근 챔피언 점수</th>
                            </tr>
                        </thead>
                        <tbody>
                            {textModelMatrix.map((m) => {
                                const modelId = String(m.model || "");
                                const label = String(m.label || modelId);
                                const provider = String(m.provider || "");
                                const qualityScore = Number(m.quality_score || 0);
                                const speedScore = Number(m.speed_score || 0);
                                const inputCost = Number(m.input_cost_per_1m_usd ?? 0);
                                const outputCost = Number(m.output_cost_per_1m_usd ?? 0);
                                const isFree = inputCost === 0 && outputCost === 0;
                                // champion_history에서 이 모델이 챔피언이었던 최신 기록 찾기
                                const histEntry = championHistory.find((h) => h.champion_model === modelId || h.challenger_model === modelId);
                                const testScore = histEntry
                                    ? (histEntry.champion_model === modelId ? histEntry.avg_champion_score : null)
                                    : null;
                                const isChampion = competitionState.champion_model === modelId;
                                const isChallenger = challengerModel === modelId;
                                return (
                                    <tr key={modelId} className={`border-b border-slate-50 ${isChampion ? "bg-amber-50/60" : isChallenger ? "bg-blue-50/60" : ""}`}>
                                        <td className="py-2 pr-3 font-medium text-slate-700">
                                            {provider}
                                            {isChampion && <span className="ml-1 rounded-full bg-amber-100 px-1.5 py-0.5 text-[9px] font-bold text-amber-700">챔피언</span>}
                                            {isChallenger && <span className="ml-1 rounded-full bg-blue-100 px-1.5 py-0.5 text-[9px] font-bold text-blue-700">도전자</span>}
                                        </td>
                                        <td className="py-2 pr-3 text-slate-600">{label}</td>
                                        <td className="py-2 pr-3 text-right font-semibold text-slate-700">{qualityScore}</td>
                                        <td className="py-2 pr-3 text-right text-slate-600">{speedScore}</td>
                                        <td className="py-2 pr-3 text-right text-slate-600">
                                            {isFree ? <span className="text-emerald-600 font-semibold">무료</span> : `$${inputCost}`}
                                        </td>
                                        <td className="py-2 pr-3 text-right text-slate-600">
                                            {isFree ? "—" : `$${outputCost}`}
                                        </td>
                                        <td className="py-2 text-right">
                                            {testScore != null
                                                ? <span className="font-semibold text-slate-800">{testScore.toFixed(1)}점</span>
                                                : <span className="text-slate-300">—</span>
                                            }
                                        </td>
                                    </tr>
                                );
                            })}
                        </tbody>
                    </table>
                </div>
            </div>

            {/* ── 주간 모델 경쟁 상태 + 도전자 설정 ── */}
            <div className="mt-4 rounded-xl border border-blue-100 bg-blue-50/70 p-4">
                <div className="flex items-center justify-between">
                    <h3 className="text-sm font-semibold text-blue-900">주간 모델 경쟁 상태</h3>
                    <span className="rounded-full bg-blue-100 px-2 py-1 text-xs font-semibold text-blue-700">
                        {competitionState.phase || "idle"}
                    </span>
                </div>
                <div className="mt-3 grid gap-2 text-xs text-blue-900 sm:grid-cols-2">
                    <p>
                        챔피언: <strong>{competitionState.champion_model || "-"}</strong>
                    </p>
                    <p>
                        Shadow 모드: <strong>{competitionState.shadow_mode ? "ON" : "OFF"}</strong>
                    </p>
                    <p>
                        테스트 슬롯 카테고리: <strong>{competitionState.fallback_category || "-"}</strong>
                    </p>
                    <p>
                        다음 적용 시각: <strong>{competitionState.apply_at || "-"}</strong>
                    </p>
                </div>
                <div className="mt-3 border-t border-blue-100 pt-3">
                    <label className="block">
                        <span className="mb-1.5 block text-xs font-semibold text-blue-900">
                            도전자 모델 설정
                            <span className="ml-1 font-normal text-blue-600">(저장 시 반영)</span>
                        </span>
                        <select
                            value={challengerModel}
                            onChange={(e) => setChallengerModel(e.target.value)}
                            className="w-full rounded-lg border border-blue-200 bg-white px-3 py-1.5 text-sm text-slate-800 focus:border-blue-400 focus:outline-none focus:ring-2 focus:ring-blue-200"
                        >
                            <option value="">— 자동 선택 (시스템 결정) —</option>
                            {textModelMatrix.map((m) => {
                                const modelId = String(m.model || "");
                                const label = String(m.label || modelId);
                                return (
                                    <option key={modelId} value={modelId}>
                                        {label}
                                    </option>
                                );
                            })}
                        </select>
                        <p className="mt-1 text-[11px] text-blue-600">
                            선택한 모델이 다음 주 챔피언 모델과 A/B 테스트됩니다.
                        </p>
                    </label>
                </div>
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
                                    {championHistory.slice(0, 8).map((history, idx) => (
                                        <tr key={`${history.week_start}-${idx}`} className="border-b border-blue-50">
                                            <td className="py-1.5 pr-3 font-medium text-blue-900">{history.week_start}</td>
                                            <td className="py-1.5 pr-3 text-blue-800">{history.champion_model}</td>
                                            <td className="py-1.5 pr-3 text-slate-600">{history.challenger_model || "—"}</td>
                                            <td className="py-1.5 pr-3 text-right font-semibold text-blue-900">
                                                {Number(history.avg_champion_score || 0).toFixed(1)}
                                            </td>
                                            <td className="py-1.5 pr-3 text-right text-slate-600">
                                                {history.cost_won > 0 ? `${formatKrw(history.cost_won)}` : "—"}
                                            </td>
                                            <td className="py-1.5 pr-3">
                                                <div className="flex flex-wrap gap-1">
                                                    {Object.entries(history.topic_mode_scores || {}).map(([topic, score]) => (
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
                                                {history.early_terminated && (
                                                    <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[9px] text-amber-700">조기종료</span>
                                                )}
                                                {history.shadow_only && (
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
            </div>

            <div className="mt-4 rounded-xl border border-amber-100 bg-amber-50/70 p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                        <p className="text-sm font-semibold text-amber-900">트래픽 피드백 고급 설정</p>
                        <p className="text-xs text-amber-800">
                            토픽별 데이터가 100편 이상일 때 50:50(품질:트래픽) 보정을 수동으로 켭니다.
                        </p>
                    </div>
                    <label className="inline-flex items-center gap-2 text-sm font-medium text-amber-900">
                        <input
                            type="checkbox"
                            checked={trafficFeedbackStrongMode}
                            onChange={(event) => setTrafficFeedbackStrongMode(event.target.checked)}
                            className="h-4 w-4 rounded border-amber-300 text-amber-600 focus:ring-amber-500"
                        />
                        강한 보정 모드
                    </label>
                </div>
            </div>

            <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50 p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                        <p className="text-sm font-semibold text-slate-800">네이버 블로그 연동</p>
                        <p className="text-xs text-slate-600">
                            상태: {naverStatus?.connected ? "연결됨" : "미연결"}
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={handleStartNaverConnect}
                        disabled={naverConnecting}
                        className="rounded-full bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-emerald-500 disabled:opacity-50"
                    >
                        {naverConnecting ? "팝업 실행 중..." : "🟢 네이버 연동 시작"}
                    </button>
                </div>
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-2">
                <button
                    type="button"
                    onClick={handleSaveRouterSettings}
                    disabled={routerSaving}
                    className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50"
                >
                    {routerSaving ? "저장 중..." : "라우터 설정 저장"}
                </button>
            </div>

            {routerMessage && (
                <p className="mt-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">
                    {routerMessage}
                </p>
            )}
        </section>
    );
}
