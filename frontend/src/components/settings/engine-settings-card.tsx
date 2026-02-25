"use client";

import { useEffect, useMemo, useState } from "react";
import {
    fetchDashboard,
    quoteRouterSettings,
    saveRouterSettings,
    startNaverConnect,
    fetchNaverConnectStatus,
    type NaverConnectStatusResponse,
    type RouterSettingsResponse,
    type RouterQuoteResponse,
} from "@/lib/api";
import { compactKeys, formatKrw } from "@/lib/utils/formatters";

type EngineSettingsCardProps = {
    initialRouterSettings: RouterSettingsResponse;
    initialNaverStatus: NaverConnectStatusResponse | null;
};

export default function EngineSettingsCard({
    initialRouterSettings,
    initialNaverStatus,
}: EngineSettingsCardProps) {
    const [routerSaving, setRouterSaving] = useState(false);
    const [routerLoading, setRouterLoading] = useState(false);
    const [routerMessage, setRouterMessage] = useState("");

    const [strategyMode, setStrategyMode] = useState<"cost" | "quality">(
        initialRouterSettings.settings.strategy_mode === "quality" ? "quality" : "cost"
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
    const [imageAiQuota, setImageAiQuota] = useState<"0" | "1" | "all">(
        (initialRouterSettings.settings.image_ai_quota as "0" | "1" | "all") || "0"
    );
    const [imageAiEngine, setImageAiEngine] = useState(
        initialRouterSettings.settings.image_ai_engine || "together_flux"
    );
    const [imageTopicQuotaOverrides, setImageTopicQuotaOverrides] = useState<Record<string, string>>(
        (initialRouterSettings.settings.image_topic_quota_overrides as Record<string, string>) || {}
    );
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
        strategy_mode: initialRouterSettings.settings.strategy_mode === "quality" ? "quality" : "cost",
        roles: initialRouterSettings.roles || {},
        estimate: {
            currency: "KRW",
            text_cost_krw: Number(initialRouterSettings.quote.text_cost_krw || 0),
            image_cost_krw: Number(initialRouterSettings.quote.image_cost_krw || 0),
            total_cost_krw: Number(initialRouterSettings.quote.total_cost_krw || 0),
            cost_min_krw: Number(initialRouterSettings.quote.cost_min_krw ?? initialRouterSettings.quote.total_cost_krw ?? 0),
            cost_max_krw: Number(initialRouterSettings.quote.cost_max_krw ?? initialRouterSettings.quote.total_cost_krw ?? 0),
            quality_score: Number(initialRouterSettings.quote.quality_score || 0),
        },
        image: {},
        available_text_models: [],
    });
    const [competitionState, setCompetitionState] = useState(
        initialRouterSettings.competition || {
            phase: "idle",
            week_start: "",
            apply_at: "",
            shadow_mode: false,
            champion_model: "",
            challenger_model: "",
            fallback_category: "다양한 생각들",
            slot_type: "default",
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

    const parserModelLabel = useMemo(() => {
        const role = routerQuote?.roles?.parser;
        if (!role || typeof role !== "object") return "-";
        const label = (role as Record<string, unknown>).label;
        return typeof label === "string" ? label : "-";
    }, [routerQuote]);

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

    useEffect(() => {
        const timer = setTimeout(async () => {
            setRouterLoading(true);
            try {
                const quoted = await quoteRouterSettings({
                    strategy_mode: strategyMode,
                    text_api_keys: compactKeys(textApiKeys),
                    image_api_keys: compactKeys(imageApiKeys),
                    image_engine: imageEngine,
                    image_ai_engine: imageAiEngine,
                    image_ai_quota: imageAiQuota,
                    image_topic_quota_overrides: imageTopicQuotaOverrides,
                    traffic_feedback_strong_mode: trafficFeedbackStrongMode,
                    image_enabled: imageEnabled,
                    images_per_post: imagesPerPostMax,
                    images_per_post_min: imagesPerPostMin,
                    images_per_post_max: imagesPerPostMax,
                });
                setRouterQuote(quoted);
            } catch {
                // 미리보기 실패는 저장 동작을 막지 않는다.
            } finally {
                setRouterLoading(false);
            }
        }, 350);
        return () => clearTimeout(timer);
    }, [strategyMode, textApiKeys, imageApiKeys, imageEngine, imageAiEngine, imageAiQuota, imageTopicQuotaOverrides, trafficFeedbackStrongMode, imageEnabled, imagesPerPostMin, imagesPerPostMax]);

    function handleTextKeyChange(keyId: string, value: string) {
        setTextApiKeys((prev) => ({ ...prev, [keyId]: value }));
    }

    function handleImageKeyChange(keyId: string, value: string) {
        setImageApiKeys((prev) => ({ ...prev, [keyId]: value }));
    }

    async function handleSaveRouterSettings() {
        setRouterSaving(true);
        setRouterMessage("");
        try {
            const saved = await saveRouterSettings({
                strategy_mode: strategyMode,
                text_api_keys: compactKeys(textApiKeys),
                image_api_keys: compactKeys(imageApiKeys),
                image_engine: imageEngine,
                image_ai_engine: imageAiEngine,
                image_ai_quota: imageAiQuota,
                image_topic_quota_overrides: imageTopicQuotaOverrides,
                traffic_feedback_strong_mode: trafficFeedbackStrongMode,
                image_enabled: imageEnabled,
                images_per_post: imagesPerPostMax,
                images_per_post_min: imagesPerPostMin,
                images_per_post_max: imagesPerPostMax,
                challenger_model: challengerModel,
            });
            setStrategyMode(saved.settings.strategy_mode === "quality" ? "quality" : "cost");
            setTextApiMasks(saved.settings.text_api_keys_masked || {});
            setImageApiMasks(saved.settings.image_api_keys_masked || {});
            setImageEngine(saved.settings.image_engine || "pexels");
            setImageEnabled(Boolean(saved.settings.image_enabled));
            setImageAiQuota((saved.settings.image_ai_quota as "0" | "1" | "all") || "0");
            setImageAiEngine(saved.settings.image_ai_engine || "together_flux");
            setImageTopicQuotaOverrides((saved.settings.image_topic_quota_overrides as Record<string, string>) || {});
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
                strategy_mode: saved.settings.strategy_mode === "quality" ? "quality" : "cost",
                roles: saved.roles || prev?.roles || {},
                estimate: {
                    currency: "KRW",
                    text_cost_krw: Number(saved.quote.text_cost_krw || 0),
                    image_cost_krw: Number(saved.quote.image_cost_krw || 0),
                    total_cost_krw: Number(saved.quote.total_cost_krw || 0),
                    cost_min_krw: Number(saved.quote.cost_min_krw ?? saved.quote.total_cost_krw ?? 0),
                    cost_max_krw: Number(saved.quote.cost_max_krw ?? saved.quote.total_cost_krw ?? 0),
                    quality_score: Number(saved.quote.quality_score || 0),
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

                    <div className="mb-4 inline-flex rounded-full border border-slate-300 p-1">
                        <button
                            type="button"
                            onClick={() => setStrategyMode("cost")}
                            className={`rounded-full px-4 py-1 text-sm font-medium transition ${strategyMode === "cost"
                                ? "bg-slate-900 text-white"
                                : "text-slate-700 hover:bg-slate-100"
                                }`}
                        >
                            ⚖️ 가성비 우선
                        </button>
                        <button
                            type="button"
                            onClick={() => setStrategyMode("quality")}
                            className={`rounded-full px-4 py-1 text-sm font-medium transition ${strategyMode === "quality"
                                ? "bg-slate-900 text-white"
                                : "text-slate-700 hover:bg-slate-100"
                                }`}
                        >
                            💎 품질 우선
                        </button>
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
                            {/* ① AI 생성 이미지 상한선 */}
                            <div className="rounded-xl border border-slate-200 bg-white p-4">
                                <p className="mb-3 text-sm font-semibold text-slate-800">
                                    포스팅당 AI 생성 이미지 수
                                    <span className="ml-2 text-xs font-normal text-slate-500">(썸네일 포함)</span>
                                </p>
                                <div className="flex flex-col gap-2 sm:flex-row sm:gap-4">
                                    {[
                                        { value: "0", label: "0장", desc: "무료 실사진만 (Pexels)", icon: "📷" },
                                        { value: "1", label: "1장", desc: "AI 추천 최고점 1장", icon: "✨" },
                                        { value: "all", label: "전체", desc: "최대 4장 AI 생성", icon: "🎨" },
                                    ].map((option) => (
                                        <label
                                            key={option.value}
                                            className={`flex flex-1 cursor-pointer items-center gap-3 rounded-xl border-2 p-3 transition ${imageAiQuota === option.value
                                                ? "border-emerald-500 bg-emerald-50"
                                                : "border-slate-200 hover:border-slate-300"
                                                }`}
                                        >
                                            <input
                                                type="radio"
                                                name="imageAiQuota"
                                                value={option.value}
                                                checked={imageAiQuota === option.value}
                                                onChange={() => setImageAiQuota(option.value as "0" | "1" | "all")}
                                                className="sr-only"
                                            />
                                            <span className="text-lg">{option.icon}</span>
                                            <span className="flex flex-col">
                                                <span className={`text-sm font-semibold ${imageAiQuota === option.value ? "text-emerald-700" : "text-slate-700"}`}>
                                                    {option.label}
                                                </span>
                                                <span className="text-xs text-slate-500">{option.desc}</span>
                                            </span>
                                        </label>
                                    ))}
                                </div>
                            </div>

                            {/* ② AI 생성 엔진 선택 (quota > 0일 때만 표시) */}
                            {imageAiQuota !== "0" && (
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
                            )}

                            {/* ③ 이미지 API 키 */}
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
                <div className="mt-3 grid gap-3 text-sm sm:grid-cols-2">
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
                            { step: "① 문맥분석", label: parserModelLabel, color: "bg-blue-50 text-blue-700 border-blue-200" },
                            { step: "② 품질작성", label: qualityModelLabel, color: "bg-violet-50 text-violet-700 border-violet-200" },
                            { step: "③ 자기검증", label: qualityModelLabel, color: "bg-violet-50 text-violet-700 border-violet-200" },
                            { step: "④ SEO최적화", label: qualityModelLabel, color: "bg-violet-50 text-violet-700 border-violet-200" },
                            { step: "⑤ 이미지슬롯", label: qualityModelLabel, color: "bg-violet-50 text-violet-700 border-violet-200" },
                            { step: "⑥ 교정", label: voiceModelLabel, color: "bg-emerald-50 text-emerald-700 border-emerald-200" },
                        ].map(({ step, label }) => (
                            <span
                                key={step}
                                className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-slate-600"
                            >
                                <span className="font-medium">{step}</span>
                                <span className="ml-1 text-slate-400">→ {label}</span>
                            </span>
                        ))}
                    </div>
                    <p className="mt-2 text-[11px] text-slate-400">
                        ③④⑤ 단계는 ② 품질작성 모델을 재사용합니다. 원가에 모두 반영됩니다.
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
