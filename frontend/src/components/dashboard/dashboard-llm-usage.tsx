"use client";

import { BrainCircuit } from "lucide-react";
import { formatKrw } from "@/lib/utils/formatters";
import type { DashboardResponse } from "@/lib/api";
import { GlassCard, Skeleton } from "./dashboard-ui";

interface DashboardLlmUsageProps {
    dashboard: DashboardResponse | null;
    dashLoading: boolean;
}

export function DashboardLlmUsage({ dashboard, dashLoading }: DashboardLlmUsageProps) {
    const m = dashboard?.metrics;
    const trend = m?.score_per_won_trend || [];
    const chartWidth = 220;
    const chartHeight = 64;
    const pointValues = trend.map((item) => Number(item.avg_score_per_won || 0));
    const maxValue = pointValues.length > 0 ? Math.max(...pointValues, 0.1) : 1;
    const points = pointValues
        .map((value, index) => {
            const x = pointValues.length <= 1 ? 0 : (index / (pointValues.length - 1)) * chartWidth;
            const y = chartHeight - (value / maxValue) * chartHeight;
            return `${x},${Math.max(0, Math.min(chartHeight, y))}`;
        })
        .join(" ");

    return (
        <GlassCard className="p-5" glow="purple">
            <div className="flex items-center gap-2 mb-4">
                <BrainCircuit className="h-4 w-4 text-slate-500" />
                <h2 className="text-sm font-semibold text-slate-700">LLM 사용 현황</h2>
            </div>
            {dashLoading ? (
                <div className="space-y-2">
                    {Array.from({ length: 3 }).map((_, i) => (
                        <Skeleton key={i} className="h-9 w-full" />
                    ))}
                </div>
            ) : m && m.llm_total_calls > 0 ? (
                <div className="space-y-1.5">
                    <div className="grid grid-cols-3 pb-1 text-xs font-medium text-slate-400 border-b border-slate-200">
                        <span>항목</span>
                        <span className="text-center">호출</span>
                        <span className="text-right">비용</span>
                    </div>
                    <div className="rounded-xl bg-white/70 px-3 py-2">
                        <div className="grid grid-cols-3 text-sm">
                            <span className="font-medium text-slate-700">전체</span>
                            <span className="text-center text-slate-600">{m.llm_total_calls}회</span>
                            <span className="text-right font-bold text-amber-600">
                                ₩{formatKrw(m.llm_cost_krw)}
                            </span>
                        </div>
                    </div>
                    <div className="rounded-xl bg-white/70 px-3 py-2">
                        <div className="grid grid-cols-3 text-xs text-slate-500">
                            <span>USD 환산</span>
                            <span className="text-center">—</span>
                            <span className="text-right">${m.llm_cost_usd.toFixed(4)}</span>
                        </div>
                    </div>
                    <div className="rounded-xl bg-white/70 px-3 py-2">
                        <p className="text-xs font-medium text-slate-500 mb-2">원당 품질 점수 추세 (최근 12주)</p>
                        {trend.length > 1 ? (
                            <svg
                                viewBox={`0 0 ${chartWidth} ${chartHeight}`}
                                className="h-16 w-full"
                                role="img"
                                aria-label="원당 품질 점수 추세 차트"
                            >
                                <polyline
                                    fill="none"
                                    stroke="currentColor"
                                    strokeWidth="2"
                                    className="text-indigo-500"
                                    points={points}
                                />
                            </svg>
                        ) : (
                            <p className="text-xs text-slate-400">추세 데이터가 아직 부족합니다.</p>
                        )}
                    </div>
                </div>
            ) : (
                <p className="text-sm text-slate-400">LLM 호출 기록이 없습니다.</p>
            )}
        </GlassCard>
    );
}
