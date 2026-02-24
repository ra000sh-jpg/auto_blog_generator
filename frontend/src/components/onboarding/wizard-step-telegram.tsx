"use client";

import type { OnboardingStatusResponse } from "@/lib/api";
import TelegramConnectCard from "@/components/telegram/telegram-connect-card";

type WizardStepTelegramProps = {
    initialOnboardingStatus: OnboardingStatusResponse;
    onNext: () => void;
    onPrev: () => void;
};

export default function WizardStepTelegram({
    initialOnboardingStatus,
    onNext,
    onPrev,
}: WizardStepTelegramProps) {
    return (
        <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4">
            <h2 className="text-xl font-bold">5단계. 알림 설정 (Telegram 연동)</h2>
            <p className="text-sm text-slate-600">
                인증코드 방식으로 Chat ID를 자동 연결합니다. 그룹 오탐 없이 개인 채팅만 안전하게 사용합니다.
            </p>

            <TelegramConnectCard
                initialOnboardingStatus={initialOnboardingStatus}
                mode="onboarding"
                onPrev={onPrev}
                onNext={onNext}
            />
        </div>
    );
}

