"use client";

import { useState, useEffect, type FormEvent } from "react";
import {
    BookOpen,
    ChevronDown,
    ChevronUp,
    Loader2,
    Plus,
    Send,
    Settings2,
    Sparkles,
    Zap,
} from "lucide-react";
import { createMagicJob, ingestIdeaVault, fetchOnboardingStatus, type IdeaVaultStatsResponse } from "@/lib/api";
import { GlassCard } from "./dashboard-ui";

const FALLBACK_PERSONA_OPTIONS = [
    { value: "P1", label: "P1" },
];

const FALLBACK_TOPIC_OPTIONS = [
    { value: "cafe", label: "cafe" },
];

function parseCommaValues(rawText: string): string[] {
    return rawText
        .split(",")
        .map((v) => v.trim())
        .filter((v, i, a) => v.length > 0 && a.indexOf(v) === i);
}

function toIsoDatetime(rawValue: string): string | undefined {
    if (!rawValue) return undefined;
    const parsed = new Date(rawValue);
    if (Number.isNaN(parsed.getTime())) return undefined;
    return parsed.toISOString();
}

interface DashboardWorkspaceProps {
    ideaVaultStats: IdeaVaultStatsResponse | null;
    defaultPersonaId: string;
    onRefreshStats: () => void;
}

export function DashboardWorkspace({
    ideaVaultStats,
    defaultPersonaId,
    onRefreshStats,
}: DashboardWorkspaceProps) {
    const [workspaceTab, setWorkspaceTab] = useState<"magic" | "vault">("magic");

    // Dynamic options loaded from onboarding settings
    const [personaOptions, setPersonaOptions] = useState(FALLBACK_PERSONA_OPTIONS);
    const [topicOptions, setTopicOptions] = useState(FALLBACK_TOPIC_OPTIONS);

    useEffect(() => {
        fetchOnboardingStatus().then((status) => {
            // Build persona options from category_allocations
            if (status.category_allocations && status.category_allocations.length > 0) {
                const seenPersonas = new Set<string>();
                const personas: Array<{ value: string; label: string }> = [];
                // Use persona_id from onboarding status as the single persona
                if (status.persona_id) {
                    personas.push({ value: status.persona_id, label: status.persona_id });
                    seenPersonas.add(status.persona_id);
                }
                if (personas.length > 0) setPersonaOptions(personas);

                // Build topic options from category_allocations
                const seenTopics = new Set<string>();
                const topics: Array<{ value: string; label: string }> = [];
                for (const alloc of status.category_allocations) {
                    if (alloc.topic_mode && !seenTopics.has(alloc.topic_mode)) {
                        seenTopics.add(alloc.topic_mode);
                        topics.push({ value: alloc.topic_mode, label: alloc.category || alloc.topic_mode });
                    }
                }
                if (topics.length > 0) {
                    setTopicOptions(topics);
                    setAdvancedTopicMode(topics[0].value);
                }
            }
        }).catch(() => {
            // silently keep fallback options on error
        });
    }, []);

    // Magic Input State
    const [instruction, setInstruction] = useState("");
    const [advancedOpen, setAdvancedOpen] = useState(false);
    const [advancedPersonaId, setAdvancedPersonaId] = useState(defaultPersonaId || "P1");
    const [advancedTopicMode, setAdvancedTopicMode] = useState("");
    const [advancedScheduleAt, setAdvancedScheduleAt] = useState("");
    const [advancedKeywordsText, setAdvancedKeywordsText] = useState("");
    const [advancedCategory, setAdvancedCategory] = useState("");
    const [submittingMagic, setSubmittingMagic] = useState(false);
    const [magicMessage, setMagicMessage] = useState("");

    // Idea Vault State
    const [ideaVaultText, setIdeaVaultText] = useState("");
    const [ideaVaultSubmitting, setIdeaVaultSubmitting] = useState(false);
    const [ideaVaultMessage, setIdeaVaultMessage] = useState("");

    async function handleMagicSubmit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        setMagicMessage("");
        if (!instruction.trim()) {
            setMagicMessage("자연어 지시문을 입력해 주세요.");
            return;
        }
        const keywordsOverride = parseCommaValues(advancedKeywordsText);
        const scheduledAtIso = toIsoDatetime(advancedScheduleAt);
        if (advancedScheduleAt && !scheduledAtIso) {
            setMagicMessage("예약 시간 형식이 올바르지 않습니다.");
            return;
        }
        setSubmittingMagic(true);
        try {
            const res = await createMagicJob({
                instruction: instruction.trim(),
                platform: "naver",
                scheduled_at: scheduledAtIso,
                persona_id_override: advancedOpen ? advancedPersonaId : undefined,
                topic_mode_override: advancedOpen ? advancedTopicMode : undefined,
                keywords_override: advancedOpen ? keywordsOverride : undefined,
                category_override: advancedOpen && advancedCategory ? advancedCategory : undefined,
            });
            setMagicMessage(
                `✅ 등록 완료: ${res.title} (${res.job_id.slice(0, 8)}...) / parser=${res.parser_used}`
            );
            setInstruction("");
            onRefreshStats();
        } catch (e) {
            setMagicMessage(e instanceof Error ? e.message : "매직 입력 처리 중 오류");
        } finally {
            setSubmittingMagic(false);
        }
    }

    async function handleIdeaVaultSubmit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        setIdeaVaultMessage("");
        if (!ideaVaultText.trim()) {
            setIdeaVaultMessage("아이디어 문장을 한 줄 이상 입력해 주세요.");
            return;
        }
        setIdeaVaultSubmitting(true);
        try {
            const res = await ingestIdeaVault({ raw_text: ideaVaultText, batch_size: 20 });
            setIdeaVaultText("");
            setIdeaVaultMessage(
                `✅ 적재 완료: 승인 ${res.accepted_count}건 / 제외 ${res.rejected_count}건 (pending=${res.pending_count})`
            );
            onRefreshStats();
        } catch (e) {
            setIdeaVaultMessage(e instanceof Error ? e.message : "창고 적재 중 오류");
        } finally {
            setIdeaVaultSubmitting(false);
        }
    }

    return (
        <GlassCard className="p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                    <Sparkles className="h-4 w-4 text-slate-500" />
                    <h2 className="text-sm font-semibold text-slate-700">워크스페이스</h2>
                </div>
                <div className="inline-flex rounded-full border border-slate-200/80 bg-white/80 p-1">
                    {(["magic", "vault"] as const).map((tab) => (
                        <button
                            key={tab}
                            type="button"
                            onClick={() => setWorkspaceTab(tab)}
                            className={`inline-flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs font-semibold transition ${workspaceTab === tab
                                    ? "bg-slate-900 text-white shadow-sm"
                                    : "text-slate-600 hover:bg-slate-100"
                                }`}
                        >
                            {tab === "magic" ? (
                                <>
                                    <Zap className="h-3 w-3" />
                                    Magic Input
                                </>
                            ) : (
                                <>
                                    <BookOpen className="h-3 w-3" />
                                    Idea Vault
                                    {(ideaVaultStats?.pending ?? 0) > 0 && (
                                        <span className="ml-0.5 rounded-full bg-purple-500 px-1.5 text-white text-xs">
                                            {ideaVaultStats?.pending}
                                        </span>
                                    )}
                                </>
                            )}
                        </button>
                    ))}
                </div>
            </div>

            {workspaceTab === "magic" && (
                <div className="mt-4 space-y-3">
                    <p className="text-sm text-slate-500">
                        자연어 문장 1개만 입력하면 title/keywords/persona를 자동 추출해 예약 큐에 넣습니다.
                    </p>
                    <form onSubmit={handleMagicSubmit} className="space-y-3">
                        <textarea
                            value={instruction}
                            onChange={(e) => setInstruction(e.target.value)}
                            className="min-h-28 w-full rounded-xl border border-slate-200 bg-white/80 px-4 py-3 text-sm outline-none transition focus:border-slate-500 focus:ring-2 focus:ring-slate-500/10"
                            placeholder="예) 내일 아침 9시에 스벅 아아 리뷰 올려줘, IT전문가 톤으로."
                        />
                        <button
                            type="button"
                            onClick={() => setAdvancedOpen((p) => !p)}
                            className="inline-flex items-center gap-1 text-xs font-medium text-slate-500 transition hover:text-slate-700"
                        >
                            <Settings2 className="h-3.5 w-3.5" />
                            고급 설정
                            {advancedOpen ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                        </button>
                        {advancedOpen && (
                            <div className="grid gap-3 rounded-xl border border-slate-200 bg-slate-50/80 p-4 sm:grid-cols-2">
                                <label className="block">
                                    <span className="mb-1 block text-xs font-medium text-slate-600">Persona</span>
                                    <select
                                        value={advancedPersonaId}
                                        onChange={(e) => setAdvancedPersonaId(e.target.value)}
                                        className="w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm"
                                    >
                                        {personaOptions.map((o) => (
                                            <option key={o.value} value={o.value}>
                                                {o.label}
                                            </option>
                                        ))}
                                    </select>
                                </label>
                                <label className="block">
                                    <span className="mb-1 block text-xs font-medium text-slate-600">Topic</span>
                                    <select
                                        value={advancedTopicMode}
                                        onChange={(e) => setAdvancedTopicMode(e.target.value)}
                                        className="w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm"
                                    >
                                        {topicOptions.map((o) => (
                                            <option key={o.value} value={o.value}>
                                                {o.label}
                                            </option>
                                        ))}
                                    </select>
                                </label>
                                <label className="block">
                                    <span className="mb-1 block text-xs font-medium text-slate-600">Scheduled At</span>
                                    <input
                                        type="datetime-local"
                                        value={advancedScheduleAt}
                                        onChange={(e) => setAdvancedScheduleAt(e.target.value)}
                                        className="w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm"
                                    />
                                </label>
                                <label className="block">
                                    <span className="mb-1 block text-xs font-medium text-slate-600">Keywords Override</span>
                                    <input
                                        value={advancedKeywordsText}
                                        onChange={(e) => setAdvancedKeywordsText(e.target.value)}
                                        className="w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm"
                                        placeholder="예) 자동화, SEO"
                                    />
                                </label>
                                <label className="block sm:col-span-2">
                                    <span className="mb-1 block text-xs font-medium text-slate-600">Category Override</span>
                                    <input
                                        value={advancedCategory}
                                        onChange={(e) => setAdvancedCategory(e.target.value)}
                                        className="w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm"
                                        placeholder="비워두면 Topic 기준 자동 카테고리"
                                    />
                                </label>
                            </div>
                        )}
                        <button
                            type="submit"
                            disabled={submittingMagic}
                            className="inline-flex items-center gap-2 rounded-full bg-slate-900 px-5 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-slate-700 disabled:opacity-50"
                        >
                            {submittingMagic ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                            {submittingMagic ? "등록 중..." : "매직 예약 생성"}
                        </button>
                    </form>
                    {magicMessage && (
                        <p
                            className={`rounded-xl border px-3 py-2 text-sm ${magicMessage.startsWith("✅")
                                    ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                                    : "border-slate-200 bg-slate-50 text-slate-700"
                                }`}
                        >
                            {magicMessage}
                        </p>
                    )}
                </div>
            )}

            {workspaceTab === "vault" && (
                <div className="mt-4 space-y-3">
                    {ideaVaultStats && (
                        <div className="grid grid-cols-3 gap-2">
                            {[
                                { label: "대기", value: ideaVaultStats.pending, color: "text-purple-600" },
                                { label: "큐잉", value: ideaVaultStats.queued, color: "text-blue-600" },
                                { label: "소비됨", value: ideaVaultStats.consumed, color: "text-slate-500" },
                            ].map((item) => (
                                <div key={item.label} className="rounded-xl bg-white/70 p-3 text-center">
                                    <p className={`text-lg font-bold ${item.color}`}>{item.value}</p>
                                    <p className="text-xs text-slate-500">{item.label}</p>
                                </div>
                            ))}
                        </div>
                    )}
                    <p className="text-sm text-slate-500">
                        100~200줄 아이디어를 한 번에 넣어 대량 적재합니다. 유효 문장만 걸러서 카테고리를 자동 분류합니다.
                    </p>
                    <form onSubmit={handleIdeaVaultSubmit} className="space-y-3">
                        <textarea
                            value={ideaVaultText}
                            onChange={(e) => setIdeaVaultText(e.target.value)}
                            className="min-h-56 w-full rounded-xl border border-slate-200 bg-white/80 px-4 py-3 text-sm outline-none transition focus:border-slate-500 focus:ring-2 focus:ring-slate-500/10"
                            placeholder={`예) 내일 카페 아침 매출을 올리는 오픈 루틴 정리\n예) 자동화 도구로 블로그 글감 수집 시간 절약한 방법\n...`}
                        />
                        <button
                            type="submit"
                            disabled={ideaVaultSubmitting}
                            className="inline-flex items-center gap-2 rounded-full bg-slate-900 px-5 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-slate-700 disabled:opacity-50"
                        >
                            {ideaVaultSubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
                            {ideaVaultSubmitting ? "적재 중..." : "아이디어 창고 적재"}
                        </button>
                    </form>
                    {ideaVaultMessage && (
                        <p
                            className={`rounded-xl border px-3 py-2 text-sm ${ideaVaultMessage.startsWith("✅")
                                    ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                                    : "border-slate-200 bg-slate-50 text-slate-700"
                                }`}
                        >
                            {ideaVaultMessage}
                        </p>
                    )}
                </div>
            )}
        </GlassCard>
    );
}
