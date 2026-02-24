"use client";

import { Layers } from "lucide-react";
import type { DashboardResponse } from "@/lib/api";
import { GlassCard, Skeleton } from "./dashboard-ui";

interface DashboardSeedStatusProps {
    dashboard: DashboardResponse | null;
    dashLoading: boolean;
}

export function DashboardSeedStatus({ dashboard, dashLoading }: DashboardSeedStatusProps) {
    const s = dashboard?.scheduler;

    return (
        <GlassCard className="p-5" glow="amber">
            <div className="flex items-center gap-2 mb-4">
                <Layers className="h-4 w-4 text-slate-500" />
                <h2 className="text-sm font-semibold text-slate-700">시드 & 큐 현황</h2>
            </div>
            {dashLoading ? (
                <div className="space-y-2">
                    {Array.from({ length: 4 }).map((_, i) => (
                        <Skeleton key={i} className="h-8 w-full" />
                    ))}
                </div>
            ) : (
                <div className="space-y-1.5">
                    {[
                        { label: "마지막 시드 날짜", value: s?.last_seed_date || "—" },
                        { label: "마지막 시드 건수", value: `${s?.last_seed_count ?? 0}건` },
                        { label: "발행 준비 완료", value: `${s?.ready_to_publish ?? 0}건` },
                        { label: "큐 대기 중", value: `${s?.queued ?? 0}건` },
                    ].map(({ label, value }) => (
                        <div
                            key={label}
                            className="flex items-center justify-between rounded-xl bg-white/70 px-3 py-2 text-sm"
                        >
                            <span className="text-slate-500">{label}</span>
                            <span className="font-semibold text-slate-800">{value}</span>
                        </div>
                    ))}
                </div>
            )}
        </GlassCard>
    );
}
