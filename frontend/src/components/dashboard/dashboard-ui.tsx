"use client";

import { CheckCircle2, XCircle } from "lucide-react";

export function GlassCard({
    children,
    className = "",
    glow,
}: {
    children: React.ReactNode;
    className?: string;
    glow?: "blue" | "green" | "amber" | "rose" | "purple";
}) {
    const glowMap: Record<string, string> = {
        blue: "shadow-blue-100",
        green: "shadow-emerald-100",
        amber: "shadow-amber-100",
        rose: "shadow-rose-100",
        purple: "shadow-purple-100",
    };
    return (
        <div
            className={`rounded-2xl border border-white/70 bg-white/65 shadow-lg backdrop-blur-md ${glow ? glowMap[glow] : ""
                } ${className}`}
        >
            {children}
        </div>
    );
}

export function Skeleton({ className = "" }: { className?: string }) {
    return <div className={`animate-pulse rounded-lg bg-slate-200/80 ${className}`} />;
}

export function StatCard({
    icon,
    label,
    value,
    sub,
    iconBg,
    loading,
}: {
    icon: React.ReactNode;
    label: string;
    value: string | number;
    sub?: string;
    iconBg: string;
    loading?: boolean;
}) {
    return (
        <div className="flex items-start gap-3 p-4">
            <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${iconBg}`}>
                {icon}
            </div>
            <div className="min-w-0 flex-1">
                <p className="truncate text-xs font-medium text-slate-500">{label}</p>
                {loading ? (
                    <Skeleton className="mt-1.5 h-6 w-20" />
                ) : (
                    <p className="mt-0.5 text-xl font-bold leading-none text-slate-900">{value}</p>
                )}
                {sub && !loading && <p className="mt-0.5 truncate text-xs text-slate-400">{sub}</p>}
            </div>
        </div>
    );
}

export function StatusBadge({
    ok,
    labelOk,
    labelFail,
}: {
    ok: boolean;
    labelOk: string;
    labelFail: string;
}) {
    return ok ? (
        <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-semibold text-emerald-700">
            <CheckCircle2 className="h-3 w-3" />
            {labelOk}
        </span>
    ) : (
        <span className="inline-flex items-center gap-1 rounded-full bg-rose-100 px-2.5 py-0.5 text-xs font-semibold text-rose-700">
            <XCircle className="h-3 w-3" />
            {labelFail}
        </span>
    );
}

export function ProgressBar({
    value,
    max,
    color = "bg-emerald-500",
}: {
    value: number;
    max: number;
    color?: string;
}) {
    const pct = max <= 0 ? 0 : Math.min(100, Math.round((value / max) * 100));
    return (
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
            <div
                className={`h-full rounded-full transition-all duration-500 ${color}`}
                style={{ width: `${pct}%` }}
            />
        </div>
    );
}
