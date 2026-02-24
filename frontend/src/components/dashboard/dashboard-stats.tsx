"use client";

import { Flame, Inbox, TrendingUp, Wallet } from "lucide-react";
import { formatKrw } from "@/lib/utils/formatters";
import type { DashboardResponse, IdeaVaultStatsResponse } from "@/lib/api";
import { GlassCard, StatCard } from "./dashboard-ui";

interface DashboardStatsProps {
    dashboard: DashboardResponse | null;
    ideaVaultStats: IdeaVaultStatsResponse | null;
    dashLoading: boolean;
}

export function DashboardStats({ dashboard, ideaVaultStats, dashLoading }: DashboardStatsProps) {
    const m = dashboard?.metrics;
    const s = dashboard?.scheduler;

    return (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <GlassCard glow="blue">
                <StatCard
                    icon={<Flame className="h-5 w-5 text-blue-600" />}
                    label="오늘 발행"
                    value={dashLoading ? "—" : `${m?.today_published ?? 0}편`}
                    sub={s ? `목표 ${s.daily_target}편` : undefined}
                    iconBg="bg-blue-100"
                    loading={dashLoading}
                />
            </GlassCard>
            <GlassCard glow="green">
                <StatCard
                    icon={<TrendingUp className="h-5 w-5 text-emerald-600" />}
                    label="누적 발행"
                    value={dashLoading ? "—" : `${m?.total_published ?? 0}편`}
                    sub="전체 기간"
                    iconBg="bg-emerald-100"
                    loading={dashLoading}
                />
            </GlassCard>
            <GlassCard glow="purple">
                <StatCard
                    icon={<Inbox className="h-5 w-5 text-purple-600" />}
                    label="아이디어 창고"
                    value={dashLoading ? "—" : `${m?.idea_vault_pending ?? ideaVaultStats?.pending ?? 0}건`}
                    sub="발행 대기 중"
                    iconBg="bg-purple-100"
                    loading={dashLoading}
                />
            </GlassCard>
            <GlassCard glow="amber">
                <StatCard
                    icon={<Wallet className="h-5 w-5 text-amber-600" />}
                    label="누적 LLM 비용"
                    value={dashLoading ? "—" : `₩${formatKrw(m?.llm_cost_krw ?? 0)}`}
                    sub={m ? `$${m.llm_cost_usd.toFixed(4)} / ${m.llm_total_calls}호출` : undefined}
                    iconBg="bg-amber-100"
                    loading={dashLoading}
                />
            </GlassCard>
        </div>
    );
}
