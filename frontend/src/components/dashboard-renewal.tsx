"use client";

import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, RefreshCw, Send, Sparkles } from "lucide-react";
import {
  fetchDashboard,
  fetchIdeaVaultStats,
  fetchOnboardingStatus,
  startScheduler,
  stopScheduler,
  type DashboardResponse,
  type IdeaVaultStatsResponse,
} from "@/lib/api";

import { DashboardStats } from "./dashboard/dashboard-stats";
import { DashboardSchedulerCard } from "./dashboard/dashboard-scheduler-card";
import { DashboardSystemStatus } from "./dashboard/dashboard-system-status";
import { DashboardWorkspace } from "./dashboard/dashboard-workspace";
import { DashboardLlmUsage } from "./dashboard/dashboard-llm-usage";
import { DashboardSeedStatus } from "./dashboard/dashboard-seed-status";
import { DashboardChampionHistory } from "./dashboard/dashboard-champion-history";

export function DashboardRenewal() {
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null);
  const [dashLoading, setDashLoading] = useState(true);
  const [dashError, setDashError] = useState("");
  const [schedulerToggling, setSchedulerToggling] = useState(false);
  const [toggleMsg, setToggleMsg] = useState("");
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [ideaVaultStats, setIdeaVaultStats] = useState<IdeaVaultStatsResponse | null>(null);
  const [ideaVaultDailyQuota, setIdeaVaultDailyQuota] = useState<number | null>(null);

  const loadDashboard = useCallback(async () => {
    try {
      const [data, vaultStats] = await Promise.all([
        fetchDashboard(),
        fetchIdeaVaultStats().catch(() => null),
      ]);
      setDashboard(data);
      if (vaultStats) setIdeaVaultStats(vaultStats);
      setLastRefresh(new Date());
      setDashError("");
    } catch (e) {
      setDashError(e instanceof Error ? e.message : "대시보드 데이터 로드 실패");
    } finally {
      setDashLoading(false);
    }
  }, []);

  // Load idea_vault_daily_quota once from onboarding settings
  useEffect(() => {
    fetchOnboardingStatus()
      .then((s) => setIdeaVaultDailyQuota(s.idea_vault_daily_quota ?? null))
      .catch(() => {});
  }, []);

  useEffect(() => {
    loadDashboard();
    const timer = setInterval(loadDashboard, 30_000);
    return () => clearInterval(timer);
  }, [loadDashboard]);

  async function handleSchedulerToggle() {
    if (!dashboard) return;
    setSchedulerToggling(true);
    setToggleMsg("");
    try {
      const running = dashboard.scheduler.scheduler_running;
      const res = running ? await stopScheduler() : await startScheduler();
      setToggleMsg(res.message);
      await loadDashboard();
    } catch (e) {
      setToggleMsg(e instanceof Error ? e.message : "토글 실패");
    } finally {
      setSchedulerToggling(false);
    }
  }

  return (
    <div className="space-y-5">
      {/* ── 헤더 ── */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">관제 센터</h1>
          <p className="mt-0.5 text-sm text-slate-500">
            {lastRefresh
              ? `마지막 갱신: ${lastRefresh.toLocaleTimeString("ko-KR", {
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
              })}`
              : "데이터 로드 중..."}
          </p>
        </div>
        <button
          type="button"
          onClick={() => {
            setDashLoading(true);
            loadDashboard();
          }}
          disabled={dashLoading}
          className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white/80 px-3 py-1.5 text-sm font-medium text-slate-600 shadow-sm backdrop-blur transition hover:border-slate-400 disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${dashLoading ? "animate-spin" : ""}`} />
          새로고침
        </button>
      </div>

      {dashError && (
        <div className="flex items-center gap-2 rounded-xl border border-rose-200 bg-rose-50/80 px-4 py-3 text-sm text-rose-700">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          {dashError}
        </div>
      )}

      {/* ── 가이드 투어 배너 (초기 사용자용) ── */}
      {!dashLoading && (dashboard?.scheduler.queued === 0 && dashboard?.scheduler.today_completed === 0) && (
        <div className="animate-in fade-in slide-in-from-top-4 duration-700">
          <div className="relative overflow-hidden rounded-2xl bg-gradient-to-r from-indigo-600 to-blue-600 p-6 shadow-lg">
            <div className="absolute -right-8 -top-8 h-32 w-32 rounded-full bg-white/10 blur-2xl" />
            <div className="absolute -left-8 -bottom-8 h-32 w-32 rounded-full bg-blue-400/10 blur-2xl" />

            <div className="relative flex flex-col md:flex-row items-center gap-6">
              <div className="flex h-16 w-16 shrink-0 items-center justify-center rounded-2xl bg-white/20 backdrop-blur-md">
                <Sparkles className="h-8 w-8 text-white animate-pulse" />
              </div>
              <div className="flex-1 text-center md:text-left">
                <h2 className="text-xl font-bold text-white">온보딩 완료를 축하합니다! 🎉</h2>
                <p className="mt-1 text-blue-100">이제 첫 번째 글감을 입력하여 자동 포스팅 시스템을 깨워보세요.</p>
                <div className="mt-4 flex flex-wrap justify-center md:justify-start gap-3">
                  <div className="flex items-center gap-1.5 rounded-lg bg-white/10 px-3 py-1.5 text-xs font-medium text-white backdrop-blur-sm border border-white/20">
                    <Send className="h-3.5 w-3.5" />
                    하단 <span className="font-bold underline">Magic Input</span>에 주제 입력
                  </div>
                  <div className="flex items-center gap-1.5 rounded-lg bg-white/10 px-3 py-1.5 text-xs font-medium text-white backdrop-blur-sm border border-white/20">
                    <RefreshCw className="h-3.5 w-3.5" />
                    자동 생성된 큐 확인
                  </div>
                </div>
              </div>
              <div className="shrink-0 leading-none">
                <p className="text-[40px] font-black text-white/20 select-none">STEP 1</p>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── Row 1: 핵심 지표 4개 ── */}
      <DashboardStats
        dashboard={dashboard}
        ideaVaultStats={ideaVaultStats}
        dashLoading={dashLoading}
      />

      {/* ── Row 2: 스케줄러 + 시스템 상태 ── */}
      <div className="grid gap-3 lg:grid-cols-5">
        <DashboardSchedulerCard
          dashboard={dashboard}
          dashLoading={dashLoading}
          schedulerToggling={schedulerToggling}
          toggleMsg={toggleMsg}
          onToggle={handleSchedulerToggle}
          ideaVaultDailyQuota={ideaVaultDailyQuota}
        />
        <DashboardSystemStatus dashboard={dashboard} dashLoading={dashLoading} />
      </div>

      {/* ── Row 3: 워크스페이스 ── */}
      <DashboardWorkspace
        ideaVaultStats={ideaVaultStats}
        defaultPersonaId="P1" // P1 is explicitly default if unknown
        onRefreshStats={loadDashboard}
      />

      {/* ── Row 4: LLM + 시드 현황 ── */}
      <div className="grid gap-3 lg:grid-cols-3">
        <DashboardLlmUsage dashboard={dashboard} dashLoading={dashLoading} />
        <DashboardSeedStatus dashboard={dashboard} dashLoading={dashLoading} />
        <DashboardChampionHistory dashboard={dashboard} dashLoading={dashLoading} />
      </div>
    </div>
  );
}
