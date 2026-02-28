"use client";

import { useEffect, useState } from "react";
import EngineSettingsCard from "@/components/settings/engine-settings-card";
import TelegramSettingsCard from "@/components/settings/telegram-settings-card";
import AllocationSettingsCard from "@/components/settings/allocation-settings-card";
import ChannelManagerCard from "@/components/settings/channel-manager-card";

import {
  fetchNaverConnectStatus,
  fetchOnboardingStatus,
  fetchRouterSettings,
  type NaverConnectStatusResponse,
  type OnboardingStatusResponse,
  type RouterSettingsResponse,
} from "@/lib/api";

export default function SettingsPage() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [onboardingData, setOnboardingData] = useState<OnboardingStatusResponse | null>(null);
  const [routerData, setRouterData] = useState<RouterSettingsResponse | null>(null);
  const [naverStatus, setNaverStatus] = useState<NaverConnectStatusResponse | null>(null);

  useEffect(() => {
    let isMounted = true;

    async function loadConfig() {
      try {
        const [onboardingResult, routerResult, naverResult] = await Promise.allSettled([
          fetchOnboardingStatus(),
          fetchRouterSettings(),
          fetchNaverConnectStatus(),
        ]);
        if (!isMounted) {
          return;
        }

        const errors: string[] = [];

        if (onboardingResult.status === "fulfilled") {
          setOnboardingData(onboardingResult.value);
        } else {
          errors.push(
            `onboarding: ${onboardingResult.reason instanceof Error ? onboardingResult.reason.message : "요청 실패"}`
          );
        }

        if (routerResult.status === "fulfilled") {
          setRouterData(routerResult.value);
        } else {
          errors.push(`router: ${routerResult.reason instanceof Error ? routerResult.reason.message : "요청 실패"}`);
        }

        if (naverResult.status === "fulfilled") {
          setNaverStatus(naverResult.value);
        } else {
          errors.push(`naver: ${naverResult.reason instanceof Error ? naverResult.reason.message : "요청 실패"}`);
        }

        if (errors.length > 0) {
          setError(`일부 설정을 불러오지 못했습니다. ${errors.join(" | ")}`);
        } else {
          setError("");
        }
      } catch (requestError) {
        if (!isMounted) {
          return;
        }
        const message =
          requestError instanceof Error
            ? requestError.message
            : "설정 정보를 불러오지 못했습니다.";
        setError(message);
      } finally {
        if (isMounted) {
          setLoading(false);
        }
      }
    }

    loadConfig();
    return () => {
      isMounted = false;
    };
  }, []);

  return (
    <div className="space-y-4">
      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <h1 className="font-[family-name:var(--font-heading)] text-2xl font-semibold tracking-tight">
          Settings
        </h1>
        <p className="mt-1 text-sm text-slate-600">
          API 키 상태와 스케줄러 배분(총 발행량/Idea Vault 할당량/카테고리 비율)을 수정합니다.
        </p>
      </section>

      {loading && (
        <p className="rounded-xl bg-slate-50 px-3 py-2 text-sm text-slate-600">
          설정 정보를 불러오는 중입니다...
        </p>
      )}

      {!loading && error && (
        <p className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </p>
      )}

      {!loading && onboardingData && routerData && (
        <>
          <EngineSettingsCard
            initialRouterSettings={routerData}
            initialNaverStatus={naverStatus}
            categoryAllocations={onboardingData.category_allocations || []}
          />

          <TelegramSettingsCard
            initialOnboardingStatus={onboardingData}
          />

          <AllocationSettingsCard
            initialOnboardingStatus={onboardingData}
          />

          <ChannelManagerCard />
        </>
      )}
    </div>
  );
}
