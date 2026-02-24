"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { fetchOnboardingStatus, completeOnboarding, triggerSchedulerSeed } from "@/lib/api";
import { OnboardingWizard } from "@/components/onboarding-wizard";

export default function OnboardingPage() {
    const router = useRouter();
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let isMounted = true;
        async function checkStatus() {
            try {
                const status = await fetchOnboardingStatus();
                if (!isMounted) return;

                if (status.completed) {
                    // 온보딩이 이미 완료되었으면 홈으로 리다이렉트
                    router.replace("/");
                } else {
                    setLoading(false);
                }
            } catch {
                if (!isMounted) return;
                // API 에러 시 일단 마법사를 띄움
                setLoading(false);
            }
        }
        checkStatus();
        return () => { isMounted = false; };
    }, [router]);

    if (loading) {
        return (
            <div className="flex min-h-screen items-center justify-center">
                <p className="text-slate-500">정보를 불러오는 중입니다...</p>
            </div>
        );
    }

    const handleOnboardingComplete = async () => {
        try {
            await completeOnboarding();
            // 온보딩 완료 시 오늘의 첫 큐를 즉시 생성하도록 트리거
            await triggerSchedulerSeed().catch((err) => console.error("Auto-seeding failed:", err));
            router.replace("/");
        } catch (error) {
            console.error("Redirect failed:", error);
            router.replace("/");
        }
    };

    return (
        <div className="mx-auto w-full max-w-4xl py-10">
            <OnboardingWizard onComplete={handleOnboardingComplete} />
        </div>
    );
}
