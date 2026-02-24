"use client";

import { useState } from "react";
import {
    saveRouterSettings,
    verifyApiKey,
    type RouterSettingsResponse,
} from "@/lib/api";
import { compactKeys } from "@/lib/utils/formatters";

type WizardStepRouterProps = {
    initialRouterSettings: RouterSettingsResponse;
    onNext: () => void;
};

export default function WizardStepRouter({
    initialRouterSettings,
    onNext,
}: WizardStepRouterProps) {
    const normalizedInitialQuota = (() => {
        const raw = String(initialRouterSettings.settings.image_ai_quota || "0").trim().toLowerCase();
        if (raw === "1" || raw === "all") {
            return raw;
        }
        return "0";
    })();
    const normalizedInitialAiEngine = (() => {
        const raw = String(
            initialRouterSettings.settings.image_ai_engine
            || initialRouterSettings.settings.image_engine
            || "together_flux"
        ).trim().toLowerCase();
        if (raw === "fal_flux" || raw === "openai_dalle3") {
            return raw;
        }
        return "together_flux";
    })();

    const [strategyMode] = useState<"cost" | "quality">(
        initialRouterSettings.settings.strategy_mode === "quality" ? "quality" : "cost"
    );
    const [textApiKeys, setTextApiKeys] = useState<Record<string, string>>({});
    const [imageApiKeys, setImageApiKeys] = useState<Record<string, string>>({});

    const [textApiMasks, setTextApiMasks] = useState<Record<string, string>>(
        initialRouterSettings.settings.text_api_keys_masked || {}
    );
    const [imageAiQuota, setImageAiQuota] = useState<"0" | "1" | "all">(normalizedInitialQuota);
    const [imageAiEngine, setImageAiEngine] = useState<string>(normalizedInitialAiEngine);
    const [imageEnabled] = useState(Boolean(initialRouterSettings.settings.image_enabled));
    const [imagesPerPostMin] = useState(
        Math.max(0, Math.min(4, Number(initialRouterSettings.settings.images_per_post_min || 0)))
    );
    const [imagesPerPostMax] = useState(
        Math.max(0, Math.min(4, Number(
            initialRouterSettings.settings.images_per_post_max ?? initialRouterSettings.settings.images_per_post ?? 1
        )))
    );

    const [apiStatuses, setApiStatuses] = useState<Record<string, { valid: boolean; message: string; checking: boolean }>>({});
    const [routerSaving, setRouterSaving] = useState(false);
    const [routerMessage, setRouterMessage] = useState("");

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
            const selectedImageEngine = imageAiQuota === "0" ? "pexels" : imageAiEngine;
            const saved = await saveRouterSettings({
                strategy_mode: strategyMode,
                text_api_keys: compactKeys(textApiKeys),
                image_api_keys: compactKeys(imageApiKeys),
                image_engine: selectedImageEngine,
                image_ai_engine: imageAiEngine,
                image_ai_quota: imageAiQuota,
                image_enabled: imageEnabled,
                images_per_post: imagesPerPostMax,
                images_per_post_min: imagesPerPostMin,
                images_per_post_max: imagesPerPostMax,
            });
            setTextApiMasks(saved.settings.text_api_keys_masked || {});
            onNext();
        } catch (error) {
            setRouterMessage(error instanceof Error ? error.message : "저장 실패");
        } finally {
            setRouterSaving(false);
        }
    }

    return (
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
                    <p className="text-sm text-slate-500 mt-1">
                        기본값은 <strong>무료 실사진(Pexels)</strong>이며, 필요 시 AI 생성 상한선과 엔진을 함께 설정할 수 있습니다.
                    </p>
                </div>

                <div>
                    <label className="text-sm font-semibold text-slate-700 block mb-2">
                        포스팅당 AI 생성 이미지 수 (썸네일 포함)
                    </label>
                    <div className="grid grid-cols-3 gap-2">
                        {[
                            { value: "0", label: "0장", desc: "실사진만 사용" },
                            { value: "1", label: "1장", desc: "AI 추천 최고점 1장" },
                            { value: "all", label: "전체", desc: "최대 4장" },
                        ].map((opt) => (
                            <button
                                key={opt.value}
                                type="button"
                                onClick={() => setImageAiQuota(opt.value as "0" | "1" | "all")}
                                className={`p-3 rounded-xl border-2 text-left transition-all ${imageAiQuota === opt.value ? "border-indigo-500 bg-indigo-50" : "border-slate-200 bg-white hover:border-slate-300"}`}
                            >
                                <div className="font-semibold text-xs text-slate-800">{opt.label}</div>
                                <div className="text-xs text-slate-500 mt-0.5">{opt.desc}</div>
                            </button>
                        ))}
                    </div>
                </div>

                {imageAiQuota !== "0" && (
                    <div>
                        <label className="text-sm font-semibold text-slate-700 block mb-2">AI 생성 엔진</label>
                        <div className="grid grid-cols-3 gap-2">
                            {[
                                { value: "together_flux", label: "Together FLUX", desc: "무료 우선" },
                                { value: "fal_flux", label: "Fal Flux", desc: "유료" },
                                { value: "openai_dalle3", label: "DALL-E 3", desc: "유료" },
                            ].map((opt) => (
                                <button
                                    key={opt.value}
                                    type="button"
                                    onClick={() => setImageAiEngine(opt.value)}
                                    className={`p-3 rounded-xl border-2 text-left transition-all ${imageAiEngine === opt.value ? "border-indigo-500 bg-indigo-50" : "border-slate-200 bg-white hover:border-slate-300"}`}
                                >
                                    <div className="font-semibold text-xs text-slate-800">{opt.label}</div>
                                    <div className="text-xs text-slate-500 mt-0.5">{opt.desc}</div>
                                </button>
                            ))}
                        </div>
                    </div>
                )}

                <div>
                    <label className="flex items-center gap-2 text-sm font-semibold">
                        PEXELS API KEY
                        <span className="bg-emerald-100 text-emerald-700 font-bold px-2 py-0.5 rounded-full text-xs">무료</span>
                        <a href="https://www.pexels.com/api/" target="_blank" rel="noreferrer" className="ml-auto text-xs text-blue-500 hover:underline">키 발급 →</a>
                    </label>
                    <p className="text-xs text-slate-500 mt-1 mb-2">
                        실사진 소스 품질을 높이려면 Pexels 키를 입력하세요. 미입력 시에도 시스템은 계속 동작합니다.
                    </p>
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

                {imageAiQuota !== "0" && (
                    <div className="grid gap-4 sm:grid-cols-2">
                        {[
                            { key: "fal", label: "FAL API KEY", href: "https://fal.ai/", badge: "유료" },
                            { key: "together", label: "TOGETHER API KEY", href: "https://www.together.ai/", badge: "유료 (초기 $5 충전 필요)" },
                            { key: "openai_image", label: "OPENAI 이미지 KEY", href: "https://platform.openai.com/api-keys", badge: "유료 (선택)" },
                        ].map(({ key, label, href, badge }) => {
                            const isOptional = key === "openai_image";
                            const hasTextOpenAI = !!textApiKeys["openai"];

                            return (
                                <div key={key}>
                                    <label className="flex items-center gap-2 text-sm font-semibold">
                                        {label}
                                        <span className={`font-bold px-2 py-0.5 rounded-full text-xs ${badge.includes("무료") ? "bg-emerald-100 text-emerald-700" : "bg-amber-100 text-amber-700"}`}>{badge}</span>
                                        <a href={href} target="_blank" rel="noreferrer" className="ml-auto text-xs text-blue-500 hover:underline">키 발급 →</a>
                                    </label>
                                    <p className="text-xs text-slate-500 mt-1 mb-2">
                                        {isOptional
                                            ? "1단계에서 입력한 OpenAI 텍스트 키가 있다면 자동으로 연동됩니다!"
                                            : "유료 수준의 고품질 모델 사용 시에만 필요하며, 입력하지 않으면 기본 무료 엔진으로 이미지를 생성합니다."
                                        }
                                    </p>
                                    <div className="relative mt-1">
                                        <input
                                            type="password"
                                            value={imageApiKeys[key] || ""}
                                            onChange={(e) => setImageApiKeys(prev => ({ ...prev, [key]: e.target.value }))}
                                            onBlur={(e) => handleVerifyKey(key, e.target.value)}
                                            placeholder={isOptional && hasTextOpenAI ? "OpenAI 텍스트 키가 자동 연동됨" : `${label} 입력`}
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
                            )
                        })}
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
    );
}
