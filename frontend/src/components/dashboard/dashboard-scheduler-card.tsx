"use client";

import { Clock, Cpu, Loader2, Pause, Play } from "lucide-react";
import type { DashboardResponse } from "@/lib/api";
import { GlassCard, ProgressBar, Skeleton } from "./dashboard-ui";

interface DashboardSchedulerCardProps {
    dashboard: DashboardResponse | null;
    dashLoading: boolean;
    schedulerToggling: boolean;
    toggleMsg: string;
    onToggle: () => void;
    onPause: () => void;
    onResume: () => void;
    pauseToggling?: boolean;
    pauseMsg?: string;
    ideaVaultDailyQuota?: number | null;
}

export function DashboardSchedulerCard({
    dashboard,
    dashLoading,
    schedulerToggling,
    toggleMsg,
    onToggle,
    onPause,
    onResume,
    pauseToggling = false,
    pauseMsg = "",
    ideaVaultDailyQuota,
}: DashboardSchedulerCardProps) {
    const s = dashboard?.scheduler;
    const todayPct = s ? Math.min(100, Math.round((s.today_completed / Math.max(1, s.daily_target)) * 100)) : 0;

    return (
        <GlassCard className="lg:col-span-3 p-5" glow="blue">
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <Cpu className="h-4 w-4 text-slate-500" />
                    <h2 className="text-sm font-semibold text-slate-700">스케줄러</h2>
                </div>
                <div className="flex items-center gap-2">
                    {!s?.api_only_mode && (
                        <button
                            type="button"
                            onClick={onToggle}
                            disabled={schedulerToggling || dashLoading}
                            className={`inline-flex items-center gap-1.5 rounded-full px-4 py-1.5 text-sm font-semibold shadow-sm transition disabled:opacity-50 ${s?.scheduler_running
                                    ? "bg-rose-500 text-white hover:bg-rose-600"
                                    : "bg-emerald-500 text-white hover:bg-emerald-600"
                                }`}
                        >
                            {schedulerToggling ? (
                                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            ) : s?.scheduler_running ? (
                                <Pause className="h-3.5 w-3.5" />
                            ) : (
                                <Play className="h-3.5 w-3.5" />
                            )}
                            {s?.scheduler_running ? "중지" : "시작"}
                        </button>
                    )}
                    <button
                        type="button"
                        onClick={s?.paused ? onResume : onPause}
                        disabled={pauseToggling || dashLoading}
                        className={`inline-flex items-center gap-1.5 rounded-full px-4 py-1.5 text-sm font-semibold shadow-sm transition disabled:opacity-50 ${s?.paused
                                ? "bg-emerald-500 text-white hover:bg-emerald-600"
                                : "bg-rose-500 text-white hover:bg-rose-600"
                            }`}
                    >
                        {pauseToggling ? (
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : s?.paused ? (
                            <Play className="h-3.5 w-3.5" />
                        ) : (
                            <Pause className="h-3.5 w-3.5" />
                        )}
                        {s?.paused ? "재가동" : "정지"}
                    </button>
                </div>
            </div>
            {toggleMsg && (
                <p className="mt-2 rounded-lg bg-slate-100 px-3 py-1.5 text-xs text-slate-600">{toggleMsg}</p>
            )}
            {pauseMsg && (
                <p className="mt-2 rounded-lg bg-amber-50 px-3 py-1.5 text-xs text-amber-700">{pauseMsg}</p>
            )}

            <div className="mt-4 space-y-3">
                <div>
                    <div className="mb-1 flex items-center justify-between text-xs text-slate-500">
                        <span>오늘 진행률</span>
                        <span className="font-medium text-slate-700">
                            {dashLoading ? "..." : `${s?.today_completed ?? 0} / ${s?.daily_target ?? 3}편 (${todayPct}%)`}
                        </span>
                    </div>
                    {dashLoading ? (
                        <Skeleton className="h-1.5 w-full" />
                    ) : (
                        <ProgressBar value={s?.today_completed ?? 0} max={s?.daily_target ?? 3} color="bg-blue-500" />
                    )}
                </div>
                <div className="grid grid-cols-2 gap-2 text-xs">
                    {[
                        { label: "실패", value: `${s?.today_failed ?? 0}건`, red: (s?.today_failed ?? 0) > 0 },
                        {
                            label: "아이디어 일일 할당",
                            value: ideaVaultDailyQuota != null ? `${ideaVaultDailyQuota}건` : "—",
                            red: false,
                        },
                        {
                            label: "다음 발행",
                            value: s?.next_publish_slot_kst
                                ? new Date(s.next_publish_slot_kst).toLocaleTimeString("ko-KR", {
                                    hour: "2-digit",
                                    minute: "2-digit",
                                })
                                : "—",
                            red: false,
                        },
                        { label: "활성 시간", value: s?.active_hours ?? "—", red: false },
                    ].map(({ label, value, red }) => (
                        <div key={label} className="rounded-lg bg-white/70 px-3 py-2">
                            <p className="flex items-center gap-0.5 text-slate-500">
                                {label === "다음 발행" && <Clock className="h-3 w-3 mr-0.5" />}
                                {label}
                            </p>
                            {dashLoading ? (
                                <Skeleton className="mt-1 h-4 w-8" />
                            ) : (
                                <p className={`mt-0.5 font-bold ${red ? "text-rose-600" : "text-slate-700"}`}>{value}</p>
                            )}
                        </div>
                    ))}
                </div>
            </div>
        </GlassCard>
    );
}
