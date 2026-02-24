"use client";

import { Crown } from "lucide-react";
import type { DashboardResponse } from "@/lib/api";
import { formatKrw } from "@/lib/utils/formatters";
import { GlassCard, Skeleton } from "./dashboard-ui";

interface DashboardChampionHistoryProps {
    dashboard: DashboardResponse | null;
    dashLoading: boolean;
}

function formatWeekLabel(weekStart: string): string {
    const normalized = String(weekStart || "").trim();
    if (!normalized) return "-";
    const date = new Date(normalized);
    if (!Number.isNaN(date.getTime())) {
        return date.toLocaleDateString("ko-KR", { month: "2-digit", day: "2-digit" });
    }
    return normalized;
}

export function DashboardChampionHistory({ dashboard, dashLoading }: DashboardChampionHistoryProps) {
    const history = dashboard?.metrics?.champion_history || [];

    return (
        <GlassCard className="p-5" glow="blue">
            <div className="mb-4 flex items-center gap-2">
                <Crown className="h-4 w-4 text-slate-500" />
                <h2 className="text-sm font-semibold text-slate-700">챔피언십 히스토리 (최근 4주)</h2>
            </div>
            {dashLoading ? (
                <div className="space-y-2">
                    {Array.from({ length: 4 }).map((_, idx) => (
                        <Skeleton key={idx} className="h-8 w-full" />
                    ))}
                </div>
            ) : history.length > 0 ? (
                <div className="space-y-1.5">
                    <div className="grid grid-cols-12 border-b border-slate-200 pb-1 text-xs font-medium text-slate-400">
                        <span className="col-span-2">주차</span>
                        <span className="col-span-4">챔피언</span>
                        <span className="col-span-2 text-right">점수</span>
                        <span className="col-span-4 text-right">비용</span>
                    </div>
                    {history.map((item) => (
                        <div
                            key={`${item.week_start}-${item.champion_model}`}
                            className="grid grid-cols-12 items-center rounded-xl bg-white/70 px-3 py-2 text-xs"
                        >
                            <span className="col-span-2 text-slate-500">{formatWeekLabel(item.week_start)}</span>
                            <span className="col-span-4 font-semibold text-slate-800">{item.champion_model || "-"}</span>
                            <span className="col-span-2 text-right font-semibold text-slate-700">
                                {Number(item.avg_champion_score || 0).toFixed(1)}
                            </span>
                            <div className="col-span-4 flex items-center justify-end gap-1.5">
                                {item.shadow_only ? (
                                    <span className="rounded-full bg-indigo-100 px-1.5 py-0.5 text-[10px] font-semibold text-indigo-700">
                                        SHADOW
                                    </span>
                                ) : null}
                                {item.early_terminated ? (
                                    <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700">
                                        EARLY
                                    </span>
                                ) : null}
                                <span className="font-semibold text-slate-700">₩{formatKrw(Math.round(item.cost_won || 0))}</span>
                            </div>
                        </div>
                    ))}
                </div>
            ) : (
                <p className="text-sm text-slate-400">챔피언십 이력이 아직 없습니다.</p>
            )}
        </GlassCard>
    );
}
