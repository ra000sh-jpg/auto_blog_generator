"use client";

import { Activity, BrainCircuit, MessageSquare, Zap } from "lucide-react";
import type { DashboardResponse } from "@/lib/api";
import { GlassCard, Skeleton, StatusBadge } from "./dashboard-ui";

interface DashboardSystemStatusProps {
    dashboard: DashboardResponse | null;
    dashLoading: boolean;
}

export function DashboardSystemStatus({ dashboard, dashLoading }: DashboardSystemStatusProps) {
    const t = dashboard?.telegram;
    const h = dashboard?.health;
    const s = dashboard?.scheduler;

    return (
        <GlassCard className="lg:col-span-2 p-5" glow="green">
            <div className="flex items-center gap-2 mb-4">
                <Activity className="h-4 w-4 text-slate-500" />
                <h2 className="text-sm font-semibold text-slate-700">시스템 상태</h2>
            </div>
            <div className="space-y-3">
                {[
                    {
                        icon: <BrainCircuit className="h-3.5 w-3.5" />,
                        label: "LLM 프로바이더",
                        ok: h?.status === "ok",
                        labelOk: `${h?.ok ?? 0}/${h?.total ?? 0} 정상`,
                        labelFail: `${h?.fail ?? 0}개 이상 실패`,
                        sub: null,
                    },
                    {
                        icon: <MessageSquare className="h-3.5 w-3.5" />,
                        label: "텔레그램 봇",
                        ok: t?.live_ok === true,
                        labelOk: t?.bot_username ? `@${t.bot_username}` : "연결됨",
                        labelFail: t?.configured ? "연결 불량" : "미설정",
                        sub: t?.error || null,
                    },
                    {
                        icon: <Zap className="h-3.5 w-3.5" />,
                        label: "스케줄러",
                        ok: s?.scheduler_running === true,
                        labelOk: "실행 중",
                        labelFail: "중지됨",
                        sub: s ? `활성: ${s.active_hours}` : null,
                    },
                ].map(({ icon, label, ok, labelOk, labelFail, sub }) => (
                    <div key={label} className="rounded-xl bg-white/70 p-3">
                        <div className="flex items-center justify-between">
                            <div className="flex items-center gap-1.5 text-xs font-medium text-slate-600">
                                {icon}
                                {label}
                            </div>
                            {dashLoading ? (
                                <Skeleton className="h-5 w-16" />
                            ) : (
                                <StatusBadge ok={ok} labelOk={labelOk} labelFail={labelFail} />
                            )}
                        </div>
                        {sub && !dashLoading && <p className="mt-1 text-xs text-slate-400">{sub}</p>}
                    </div>
                ))}
            </div>
        </GlassCard>
    );
}
