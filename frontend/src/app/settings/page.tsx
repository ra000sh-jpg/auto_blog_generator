"use client";

import { useEffect, useState } from "react";
import EngineSettingsCard from "@/components/settings/engine-settings-card";
import TelegramSettingsCard from "@/components/settings/telegram-settings-card";
import AllocationSettingsCard from "@/components/settings/allocation-settings-card";
import ChannelManagerCard from "@/components/settings/channel-manager-card";

import {
  fetchConfig,
  fetchNaverConnectStatus,
  fetchOnboardingStatus,
  fetchRouterSettings,
  type ConfigResponse,
  type NaverConnectStatusResponse,
  type OnboardingStatusResponse,
  type RouterSettingsResponse,
} from "@/lib/api";

export default function SettingsPage() {
  const [data, setData] = useState<ConfigResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [onboardingData, setOnboardingData] = useState<OnboardingStatusResponse | null>(null);
  const [routerData, setRouterData] = useState<RouterSettingsResponse | null>(null);
  const [naverStatus, setNaverStatus] = useState<NaverConnectStatusResponse | null>(null);

  useEffect(() => {
    let isMounted = true;

    async function loadConfig() {
      try {
        const [configResult, onboardingResult, routerResult, naverResult] = await Promise.allSettled([
          fetchConfig(),
          fetchOnboardingStatus(),
          fetchRouterSettings(),
          fetchNaverConnectStatus(),
        ]);
        if (!isMounted) {
          return;
        }

        const errors: string[] = [];

        if (configResult.status === "fulfilled") {
          setData(configResult.value);
        } else {
          errors.push(`config: ${configResult.reason instanceof Error ? configResult.reason.message : "요청 실패"}`);
        }

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

      {error && (
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

      {data && (
        <>
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
              API Keys
            </h2>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              {data.api_keys.map((item) => (
                <article key={item.provider} className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                  <p className="text-xs uppercase tracking-wide text-slate-500">{item.provider}</p>
                  <p className="mt-1 text-sm font-medium text-slate-900">{item.env_var}</p>
                  <p className="mt-2 text-xs text-slate-600">
                    상태: {item.configured ? "연결됨" : "미설정"}
                  </p>
                  <p className="mt-1 text-xs text-slate-600">
                    값: {item.configured ? item.masked : "-"}
                  </p>
                </article>
              ))}
            </div>
          </section>

          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
              Personas
            </h2>
            <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {data.personas.map((item) => (
                <article key={item.value} className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                  <h3 className="text-sm font-medium text-slate-800">{item.label}</h3>
                  <p className="mt-1 text-xs text-slate-500">ID: {item.value}</p>
                  <div className="mt-3">
                    <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">
                      Topic Mode
                    </p>
                    <div className="mt-1 flex flex-wrap gap-1">
                      <span className="rounded-full bg-slate-200 px-2 py-0.5 text-[10px] text-slate-700">
                        {item.topic_mode}
                      </span>
                    </div>
                  </div>
                </article>
              ))}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
