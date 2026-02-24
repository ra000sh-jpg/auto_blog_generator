"use client";

import { useEffect, useMemo, useState } from "react";
import {
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
    }, [strategyMode, textApiKeys, imageApiKeys, imageEngine, imageEnabled, imagesPerPostMin, imagesPerPostMax]);

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
                image_enabled: imageEnabled,
                images_per_post: imagesPerPostMax,
                images_per_post_min: imagesPerPostMin,
                images_per_post_max: imagesPerPostMax,
            });
            setStrategyMode(saved.settings.strategy_mode === "quality" ? "quality" : "cost");
            setTextApiMasks(saved.settings.text_api_keys_masked || {});
            setImageApiMasks(saved.settings.image_api_keys_masked || {});
            setImageEngine(saved.settings.image_engine || "pexels");
            setImageEnabled(Boolean(saved.settings.image_enabled));
            setImagesPerPostMin(Math.max(0, Math.min(4, Number(saved.settings.images_per_post_min || 0))));
            setImagesPerPostMax(Math.max(0, Math.min(4, Number(
                saved.settings.images_per_post_max ?? saved.settings.images_per_post ?? 1
            ))));
            setTextModelMatrix(saved.matrix.text_models || []);
            setImageModelMatrix(saved.matrix.image_models || []);
            setCompetitionState(saved.competition || competitionState);
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
                        <div className="space-y-4">
                            <label className="block">
                                <span className="mb-1 block text-sm font-medium text-slate-700">이미지 엔진 선택</span>
                                <select
                                    value={imageEngine}
                                    onChange={(event) => setImageEngine(event.target.value)}
                                    className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                                >
                                    {imageModelMatrix.map((item, index) => {
                                        const engineId =
                                            typeof item.engine_id === "string" ? item.engine_id : `engine-${index}`;
                                        const label = typeof item.label === "string" ? item.label : engineId;
                                        return (
                                            <option key={engineId} value={engineId}>
                                                {label}
                                            </option>
                                        );
                                    })}
                                </select>
                            </label>

                            <div className="rounded-xl border border-slate-200 bg-white p-3">
                                <span className="mb-2 flex items-center justify-between text-sm font-medium text-slate-700">
                                    AI 동적 이미지 수
                                    <span className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-600">
                                        {imagesPerPostMin}장 ~ {imagesPerPostMax}장 범위
                                    </span>
                                </span>
                                <div className="flex items-center gap-3 text-sm">
                                    <label className="flex-1">
                                        <span className="mb-1 block text-xs text-slate-500">최소</span>
                                        <input
                                            type="number"
                                            min={0}
                                            max={imagesPerPostMax}
                                            value={imagesPerPostMin}
                                            onChange={(e) => setImagesPerPostMin(Math.max(0, Math.min(imagesPerPostMax, Number(e.target.value))))}
                                            className="w-full rounded-lg border border-slate-300 px-2 py-1 text-sm text-center"
                                        />
                                    </label>
                                    <span className="text-slate-400">~</span>
                                    <label className="flex-1">
                                        <span className="mb-1 block text-xs text-slate-500">최대</span>
                                        <input
                                            type="number"
                                            min={imagesPerPostMin}
                                            max={4}
                                            value={imagesPerPostMax}
                                            onChange={(e) => setImagesPerPostMax(Math.max(imagesPerPostMin, Math.min(4, Number(e.target.value))))}
                                            className="w-full rounded-lg border border-slate-300 px-2 py-1 text-sm text-center"
                                        />
                                    </label>
                                </div>
                                <p className="mt-2 text-xs text-slate-500">
                                    블로그 주제와 내용 길이에 따라 AI가 범위 내에서 사진 수를 결정합니다. (최대 4장)
                                </p>
                            </div>

                            <div className="grid gap-3 sm:grid-cols-2">
                                {Array.from(
                                    new Set(
                                        imageModelMatrix
                                            .map((item) => (typeof item.key_id === "string" ? item.key_id : ""))
                                            .filter((value) => value.length > 0),
                                    ),
                                ).map((keyId) => (
                                    <label key={keyId} className="block">
                                        <span className="mb-1 block text-sm font-medium text-slate-700">
                                            {keyId.toUpperCase()} Key
                                        </span>
                                        <input
                                            type="password"
                                            value={imageApiKeys[keyId] || ""}
                                            onChange={(event) => handleImageKeyChange(keyId, event.target.value)}
                                            className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                                            placeholder={imageApiMasks[keyId] ? `${imageApiMasks[keyId]} (저장됨)` : "선택 입력"}
                                        />
                                    </label>
                                ))}
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
                <div className="mt-2 grid gap-2 text-sm sm:grid-cols-2">
                    <p>
                        예상 원가:{" "}
                        <strong>
                            {formatKrw(routerQuote?.estimate.cost_min_krw || 0)}원 ~{" "}
                            {formatKrw(routerQuote?.estimate.cost_max_krw || 0)}원
                        </strong>
                        <span className="ml-2 block text-xs text-slate-500 sm:inline sm:ml-2">(글 길이·사진 수 변동)</span>
                    </p>
                    <p>
                        예상 품질: <strong>{routerQuote?.estimate.quality_score || 0}점</strong>
                    </p>
                    <p className="sm:col-span-2 text-xs text-slate-600">
                        배정: [문맥분석] <strong>{parserModelLabel}</strong> / [본문작성]{" "}
                        <strong>{qualityModelLabel}</strong> / [교정] <strong>{voiceModelLabel}</strong>
                    </p>
                </div>
            </div>

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
                        도전자: <strong>{competitionState.challenger_model || "-"}</strong>
                    </p>
                    <p>
                        Shadow 모드: <strong>{competitionState.shadow_mode ? "ON" : "OFF"}</strong>
                    </p>
                    <p>
                        테스트 슬롯 카테고리: <strong>{competitionState.fallback_category || "-"}</strong>
                    </p>
                    <p className="sm:col-span-2">
                        다음 적용 시각: <strong>{competitionState.apply_at || "-"}</strong>
                    </p>
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
