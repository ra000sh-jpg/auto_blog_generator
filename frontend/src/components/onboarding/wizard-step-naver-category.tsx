"use client";

import { useState } from "react";
import {
    DEFAULT_FALLBACK_CATEGORY,
    fetchNaverConnectStatus,
    saveOnboardingCategories,
    startNaverConnect,
    type NaverConnectStatusResponse,
    type OnboardingStatusResponse,
} from "@/lib/api";
import { parseCommaValues } from "@/lib/utils/formatters";

type WizardStepNaverCategoryProps = {
    initialOnboardingStatus: OnboardingStatusResponse;
    initialNaverStatus: NaverConnectStatusResponse | null;
    onNext: () => void;
    onPrev: () => void;
};

export default function WizardStepNaverCategory({
    initialOnboardingStatus,
    initialNaverStatus,
    onNext,
    onPrev,
}: WizardStepNaverCategoryProps) {
    const cats = initialOnboardingStatus.categories || [];
    if (!cats.includes(DEFAULT_FALLBACK_CATEGORY)) {
        cats.push(DEFAULT_FALLBACK_CATEGORY);
    }

    const [naverStatus, setNaverStatus] = useState<NaverConnectStatusResponse | null>(initialNaverStatus);
    const [naverConnecting, setNaverConnecting] = useState(false);
    const [categoriesText, setCategoriesText] = useState(cats.join(", "));
    const [saving, setSaving] = useState(false);
    const [stepMessage, setStepMessage] = useState("");

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
            let modifiedCatText = categoriesText;
            if (!modifiedCatText.includes(DEFAULT_FALLBACK_CATEGORY)) {
                modifiedCatText = modifiedCatText ? modifiedCatText + `, ${DEFAULT_FALLBACK_CATEGORY}` : DEFAULT_FALLBACK_CATEGORY;
            }

            await saveOnboardingCategories({
                categories: parseCommaValues(modifiedCatText),
                fallback_category: DEFAULT_FALLBACK_CATEGORY,
            });
            setCategoriesText(modifiedCatText);
            onNext();
        } catch (error) {
            setStepMessage(error instanceof Error ? error.message : "저장 실패");
        } finally {
            setSaving(false);
        }
    }

    return (
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

                <div className="mb-4">
                    <p className="text-xs font-semibold text-slate-600 mb-2">💡 수익성(광고 단가)이 높은 추천 주제 (클릭하여 추가)</p>
                    <div className="flex flex-wrap gap-2">
                        {[
                            { label: "📈 IT/테크", value: "IT/테크" },
                            { label: "💰 재테크/금융", value: "재테크/금융" },
                            { label: "🩺 건강/의학", value: "건강/의학" },
                            { label: "🏠 부동산/인테리어", value: "부동산/인테리어" },
                        ].map((cat) => (
                            <button
                                key={cat.value}
                                type="button"
                                onClick={() => {
                                    const current = categoriesText.split(",").map(s => s.trim()).filter(Boolean);
                                    if (!current.includes(cat.value)) {
                                        setCategoriesText(current.length > 0 ? `${categoriesText}, ${cat.value}` : cat.value);
                                    }
                                }}
                                className="px-3 py-1.5 rounded-lg border border-indigo-200 bg-indigo-50 text-indigo-700 text-xs font-semibold hover:bg-indigo-100 transition-colors"
                            >
                                {cat.label}
                            </button>
                        ))}
                    </div>
                </div>

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
                <button onClick={onPrev} className="text-slate-500 font-semibold px-4 py-2 hover:bg-slate-100 rounded-lg transition-colors">← 이전</button>
                <button onClick={handleSaveCategoryStep} disabled={saving} className="bg-gradient-to-r from-indigo-600 to-blue-600 text-white px-8 py-3 rounded-full font-bold shadow-md hover:shadow-lg transition-all active:scale-95 text-lg">
                    {saving ? "저장 중..." : "다음 단계로 →"}
                </button>
            </div>
            {stepMessage && <p className="text-red-500 text-sm mt-2">{stepMessage}</p>}
        </div>
    );
}
