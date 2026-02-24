"use client";

import type { OnboardingStatusResponse } from "@/lib/api";
import TelegramConnectCard from "@/components/telegram/telegram-connect-card";

type TelegramSettingsCardProps = {
    initialOnboardingStatus: OnboardingStatusResponse;
};

export default function TelegramSettingsCard({
    initialOnboardingStatus,
}: TelegramSettingsCardProps) {
    return (
        <TelegramConnectCard
            initialOnboardingStatus={initialOnboardingStatus}
            mode="settings"
        />
    );
}

