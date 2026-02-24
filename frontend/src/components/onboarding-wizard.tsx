"use client";

import { useEffect, useState } from "react";
import {
    fetchNaverConnectStatus,
    fetchOnboardingStatus,
    fetchPersonaQuestionBank,
    fetchRouterSettings,
    type NaverConnectStatusResponse,
    type OnboardingStatusResponse,
    type PersonaQuestionBankResponse,
    type RouterSettingsResponse,
} from "@/lib/api";
import WizardStepRouter from "./onboarding/wizard-step-router";
import WizardStepPersona from "./onboarding/wizard-step-persona";
import WizardStepNaverCategory from "./onboarding/wizard-step-naver-category";
import WizardStepSchedule from "./onboarding/wizard-step-schedule";
import WizardStepTelegram from "./onboarding/wizard-step-telegram";

interface OnboardingWizardProps {
    onComplete: () => void;
}

export function OnboardingWizard({ onComplete }: OnboardingWizardProps) {
    const [loading, setLoading] = useState(true);
    const [loadingError, setLoadingError] = useState("");
    const [step, setStep] = useState(0);

    const [onboardingStatus, setOnboardingStatus] = useState<OnboardingStatusResponse | null>(null);
    const [routerSettings, setRouterSettings] = useState<RouterSettingsResponse | null>(null);
    const [naverStatus, setNaverStatus] = useState<NaverConnectStatusResponse | null>(null);
    const [questionBank, setQuestionBank] = useState<PersonaQuestionBankResponse | null>(null);

    useEffect(() => {
        let isMounted = true;
        async function loadStatus() {
            try {
                const [obs, rSettings, ncState, qbResponse] = await Promise.all([
                    fetchOnboardingStatus(),
                    fetchRouterSettings(),
                    fetchNaverConnectStatus(),
                    fetchPersonaQuestionBank().catch(() => null),
                ]);
                if (!isMounted) return;

                setOnboardingStatus(obs);
                setRouterSettings(rSettings);
                setNaverStatus(ncState);
                if (qbResponse && Array.isArray(qbResponse.questions)) {
                    setQuestionBank(qbResponse);
                }
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
        "4. 스케줄 배분",
        "5. 알림 설정 (Telegram)",
    ];

    if (loading) return <div className="text-center py-10">설정 마법사를 불러오는 중입니다...</div>;
    if (loadingError || !onboardingStatus || !routerSettings) {
        return <div className="text-center text-red-500 py-10">{loadingError || "데이터 로딩 실패"}</div>;
    }

    return (
        <div className="mx-auto w-full max-w-3xl space-y-6">
            <div className="text-center mb-10">
                <h1 className="text-3xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-blue-600 to-indigo-600">환영합니다! 시작해볼까요?</h1>
                <p className="mt-2 text-slate-600">간단한 5단계 설정만 마치면 자동 블로그 포스팅이 시작됩니다.</p>
            </div>

            <div className="flex justify-between items-center mb-8 px-4 relative">
                <div className="absolute top-1/2 left-0 right-0 h-1 bg-slate-200 -z-10 -translate-y-1/2 rounded animate-pulse" />
                {stepTitles.map((title, idx) => (
                    <div key={title} className={`py-2 px-4 rounded-full text-sm font-semibold transition-all duration-300 ${step === idx ? "bg-indigo-600 text-white shadow-lg scale-105" : step > idx ? "bg-emerald-500 text-white" : "bg-slate-100 text-slate-400"}`}>
                        {title}
                    </div>
                ))}
            </div>

            <div className="bg-white rounded-3xl shadow-xl border border-slate-100 p-8 min-h-[400px]">
                {step === 0 && (
                    <WizardStepRouter
                        initialRouterSettings={routerSettings}
                        onNext={() => setStep(1)}
                    />
                )}
                {step === 1 && (
                    <WizardStepPersona
                        initialOnboardingStatus={onboardingStatus}
                        questionBank={questionBank}
                        onNext={() => setStep(2)}
                        onPrev={() => setStep(0)}
                    />
                )}
                {step === 2 && (
                    <WizardStepNaverCategory
                        initialOnboardingStatus={onboardingStatus}
                        initialNaverStatus={naverStatus}
                        onNext={() => setStep(3)}
                        onPrev={() => setStep(1)}
                    />
                )}
                {step === 3 && (
                    <WizardStepSchedule
                        initialOnboardingStatus={onboardingStatus}
                        onNext={() => setStep(4)}
                        onPrev={() => setStep(2)}
                    />
                )}
                {step === 4 && (
                    <WizardStepTelegram
                        initialOnboardingStatus={onboardingStatus}
                        onPrev={() => setStep(3)}
                        onNext={onComplete}
                    />
                )}
            </div>
        </div>
    );
}
