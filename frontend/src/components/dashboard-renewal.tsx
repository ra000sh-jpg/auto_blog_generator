"use client";

import Image from "next/image";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BookOpen,
  BrainCircuit,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock,
  Cpu,
  Flame,
  Inbox,
  Layers,
  Loader2,
  MessageSquare,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Send,
  Settings2,
  Sparkles,
  TrendingUp,
  Wallet,
  XCircle,
  Zap,
} from "lucide-react";

import {
  DEFAULT_FALLBACK_CATEGORY,
  completeOnboarding,
  createMagicJob,
  fetchNaverConnectStatus,
  fetchIdeaVaultStats,
  fetchOnboardingStatus,
  fetchRouterSettings,
  ingestIdeaVault,
  quoteRouterSettings,
  saveOnboardingCategories,
  saveOnboardingSchedule,
  saveRouterSettings,
  savePersonaLab,
  startNaverConnect,
  testTelegramSetup,
  startScheduler,
  stopScheduler,
  fetchDashboard,
  type DashboardResponse,
  type IdeaVaultStatsResponse,
  type NaverConnectStatusResponse,
  type OnboardingStatusResponse,
  type RouterQuoteResponse,
  type ScheduleAllocationItem,
} from "@/lib/api";

// ---------------------------------------------------------------------------
// 온보딩 전용 유틸
// ---------------------------------------------------------------------------

type PersonaOption = { value: string; label: string };

const PERSONA_OPTIONS: PersonaOption[] = [
  { value: "P1", label: "Cafe Creator (P1)" },
  { value: "P2", label: "Tech Blogger (P2)" },
  { value: "P3", label: "Parenting Writer (P3)" },
  { value: "P4", label: "Finance Insight (P4)" },
];

const TOPIC_OPTIONS = [
  { value: "cafe", label: "Cafe" },
  { value: "it", label: "IT" },
  { value: "parenting", label: "Parenting" },
  { value: "finance", label: "Finance" },
];

function parseCommaValues(rawText: string): string[] {
  return rawText.split(",").map((v) => v.trim()).filter((v, i, a) => v.length > 0 && a.indexOf(v) === i);
}

function toIsoDatetime(rawValue: string): string | undefined {
  if (!rawValue) return undefined;
  const parsed = new Date(rawValue);
  if (Number.isNaN(parsed.getTime())) return undefined;
  return parsed.toISOString();
}

function sliderLabel(score: number, labels: [string, string, string]): string {
  if (score <= 33) return labels[0];
  if (score <= 66) return labels[1];
  return labels[2];
}

function formatKrw(value: number): string {
  return new Intl.NumberFormat("ko-KR").format(Math.max(0, Math.round(value)));
}

function compactKeys(input: Record<string, string>): Record<string, string> {
  return Object.entries(input).reduce<Record<string, string>>((acc, [k, v]) => {
    const n = String(v || "").trim();
    if (n) acc[k] = n;
    return acc;
  }, {});
}

function inferTopicMode(cat: string): string {
  const l = cat.toLowerCase();
  if (["경제", "finance", "투자", "주식", "재테크"].some((t) => l.includes(t))) return "finance";
  if (["it", "개발", "코드", "자동화", "ai", "테크"].some((t) => l.includes(t))) return "it";
  if (["육아", "아이", "부모", "가정"].some((t) => l.includes(t))) return "parenting";
  return "cafe";
}

function normalizeAllocations(cats: string[], dailyTarget: number, existing: ScheduleAllocationItem[] = []): ScheduleAllocationItem[] {
  const normalized = cats.map((v) => v.trim()).filter((v, i, a) => v.length > 0 && a.indexOf(v) === i);
  const fallback = normalized.length > 0 ? normalized : [DEFAULT_FALLBACK_CATEGORY];
  const map = new Map(existing.map((r) => [r.category, r]));
  const rows: ScheduleAllocationItem[] = fallback.map((c) => {
    const ex = map.get(c);
    return { category: c, topic_mode: ex?.topic_mode || inferTopicMode(c), count: Math.max(0, Number(ex?.count || 0)) };
  });
  const target = Math.max(0, dailyTarget);
  if (target <= 0) return rows.map((r) => ({ ...r, count: 0 }));
  let total = rows.reduce((a, r) => a + r.count, 0);
  if (total <= 0) { for (let i = 0; i < target; i++) rows[i % rows.length].count += 1; return rows; }
  if (total < target) { rows[0].count += target - total; return rows; }
  if (total > target) {
    let ov = total - target;
    for (let i = rows.length - 1; i >= 0; i--) {
      if (ov <= 0) break;
      const d = Math.min(rows[i].count, ov);
      rows[i].count -= d; ov -= d;
    }
  }
  total = rows.reduce((a, r) => a + r.count, 0);
  if (total !== target) rows[0].count += target - total;
  return rows;
}

// ---------------------------------------------------------------------------
// 공통 UI 컴포넌트
// ---------------------------------------------------------------------------

function GlassCard({ children, className = "", glow }: {
  children: React.ReactNode; className?: string;
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
    <div className={`rounded-2xl border border-white/70 bg-white/65 shadow-lg backdrop-blur-md ${glow ? glowMap[glow] : ""} ${className}`}>
      {children}
    </div>
  );
}

function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-lg bg-slate-200/80 ${className}`} />;
}

function StatCard({ icon, label, value, sub, iconBg, loading }: {
  icon: React.ReactNode; label: string; value: string | number;
  sub?: string; iconBg: string; loading?: boolean;
}) {
  return (
    <div className="flex items-start gap-3 p-4">
      <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${iconBg}`}>{icon}</div>
      <div className="min-w-0 flex-1">
        <p className="truncate text-xs font-medium text-slate-500">{label}</p>
        {loading ? <Skeleton className="mt-1.5 h-6 w-20" /> : <p className="mt-0.5 text-xl font-bold leading-none text-slate-900">{value}</p>}
        {sub && !loading && <p className="mt-0.5 truncate text-xs text-slate-400">{sub}</p>}
      </div>
    </div>
  );
}

function StatusBadge({ ok, labelOk, labelFail }: { ok: boolean; labelOk: string; labelFail: string }) {
  return ok ? (
    <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-semibold text-emerald-700">
      <CheckCircle2 className="h-3 w-3" />{labelOk}
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 rounded-full bg-rose-100 px-2.5 py-0.5 text-xs font-semibold text-rose-700">
      <XCircle className="h-3 w-3" />{labelFail}
    </span>
  );
}

function ProgressBar({ value, max, color = "bg-emerald-500" }: { value: number; max: number; color?: string }) {
  const pct = max <= 0 ? 0 : Math.min(100, Math.round((value / max) * 100));
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
      <div className={`h-full rounded-full transition-all duration-500 ${color}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 메인 대시보드 (온보딩 완료 이후)
// ---------------------------------------------------------------------------

function MainDashboard({ onboarding }: { onboarding: OnboardingStatusResponse }) {
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null);
  const [dashLoading, setDashLoading] = useState(true);
  const [dashError, setDashError] = useState("");
  const [schedulerToggling, setSchedulerToggling] = useState(false);
  const [toggleMsg, setToggleMsg] = useState("");
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const [workspaceTab, setWorkspaceTab] = useState<"magic" | "vault">("magic");
  const [instruction, setInstruction] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [advancedPersonaId, setAdvancedPersonaId] = useState(onboarding.persona_id || "P1");
  const [advancedTopicMode, setAdvancedTopicMode] = useState("cafe");
  const [advancedScheduleAt, setAdvancedScheduleAt] = useState("");
  const [advancedKeywordsText, setAdvancedKeywordsText] = useState("");
  const [advancedCategory, setAdvancedCategory] = useState("");
  const [submittingMagic, setSubmittingMagic] = useState(false);
  const [magicMessage, setMagicMessage] = useState("");
  const [ideaVaultText, setIdeaVaultText] = useState("");
  const [ideaVaultSubmitting, setIdeaVaultSubmitting] = useState(false);
  const [ideaVaultMessage, setIdeaVaultMessage] = useState("");
  const [ideaVaultStats, setIdeaVaultStats] = useState<IdeaVaultStatsResponse | null>(null);

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

  async function handleMagicSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMagicMessage("");
    if (!instruction.trim()) { setMagicMessage("자연어 지시문을 입력해 주세요."); return; }
    const keywordsOverride = parseCommaValues(advancedKeywordsText);
    const scheduledAtIso = toIsoDatetime(advancedScheduleAt);
    if (advancedScheduleAt && !scheduledAtIso) { setMagicMessage("예약 시간 형식이 올바르지 않습니다."); return; }
    setSubmittingMagic(true);
    try {
      const res = await createMagicJob({
        instruction: instruction.trim(), platform: "naver", scheduled_at: scheduledAtIso,
        persona_id_override: advancedOpen ? advancedPersonaId : undefined,
        topic_mode_override: advancedOpen ? advancedTopicMode : undefined,
        keywords_override: advancedOpen ? keywordsOverride : undefined,
        category_override: advancedOpen && advancedCategory ? advancedCategory : undefined,
      });
      setMagicMessage(`✅ 등록 완료: ${res.title} (${res.job_id.slice(0, 8)}...) / parser=${res.parser_used}`);
      setInstruction("");
      loadDashboard();
    } catch (e) { setMagicMessage(e instanceof Error ? e.message : "매직 입력 처리 중 오류"); }
    finally { setSubmittingMagic(false); }
  }

  async function handleIdeaVaultSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setIdeaVaultMessage("");
    if (!ideaVaultText.trim()) { setIdeaVaultMessage("아이디어 문장을 한 줄 이상 입력해 주세요."); return; }
    setIdeaVaultSubmitting(true);
    try {
      const res = await ingestIdeaVault({ raw_text: ideaVaultText, batch_size: 20 });
      setIdeaVaultText("");
      setIdeaVaultMessage(`✅ 적재 완료: 승인 ${res.accepted_count}건 / 제외 ${res.rejected_count}건 (pending=${res.pending_count})`);
      const latest = await fetchIdeaVaultStats();
      setIdeaVaultStats(latest);
      loadDashboard();
    } catch (e) { setIdeaVaultMessage(e instanceof Error ? e.message : "창고 적재 중 오류"); }
    finally { setIdeaVaultSubmitting(false); }
  }

  const m = dashboard?.metrics;
  const s = dashboard?.scheduler;
  const t = dashboard?.telegram;
  const h = dashboard?.health;
  const todayPct = s ? Math.min(100, Math.round((s.today_completed / Math.max(1, s.daily_target)) * 100)) : 0;

  return (
    <div className="space-y-5">
      {/* ── 헤더 ── */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">관제 센터</h1>
          <p className="mt-0.5 text-sm text-slate-500">
            {lastRefresh
              ? `마지막 갱신: ${lastRefresh.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`
              : "데이터 로드 중..."}
          </p>
        </div>
        <button
          type="button" onClick={() => { setDashLoading(true); loadDashboard(); }} disabled={dashLoading}
          className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white/80 px-3 py-1.5 text-sm font-medium text-slate-600 shadow-sm backdrop-blur transition hover:border-slate-400 disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${dashLoading ? "animate-spin" : ""}`} />
          새로고침
        </button>
      </div>

      {dashError && (
        <div className="flex items-center gap-2 rounded-xl border border-rose-200 bg-rose-50/80 px-4 py-3 text-sm text-rose-700">
          <AlertTriangle className="h-4 w-4 shrink-0" />{dashError}
        </div>
      )}

      {/* ── Row 1: 핵심 지표 4개 ── */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <GlassCard glow="blue">
          <StatCard icon={<Flame className="h-5 w-5 text-blue-600" />} label="오늘 발행" value={dashLoading ? "—" : `${m?.today_published ?? 0}편`} sub={s ? `목표 ${s.daily_target}편` : undefined} iconBg="bg-blue-100" loading={dashLoading} />
        </GlassCard>
        <GlassCard glow="green">
          <StatCard icon={<TrendingUp className="h-5 w-5 text-emerald-600" />} label="누적 발행" value={dashLoading ? "—" : `${m?.total_published ?? 0}편`} sub="전체 기간" iconBg="bg-emerald-100" loading={dashLoading} />
        </GlassCard>
        <GlassCard glow="purple">
          <StatCard icon={<Inbox className="h-5 w-5 text-purple-600" />} label="아이디어 창고" value={dashLoading ? "—" : `${m?.idea_vault_pending ?? ideaVaultStats?.pending ?? 0}건`} sub="발행 대기 중" iconBg="bg-purple-100" loading={dashLoading} />
        </GlassCard>
        <GlassCard glow="amber">
          <StatCard icon={<Wallet className="h-5 w-5 text-amber-600" />} label="누적 LLM 비용" value={dashLoading ? "—" : `₩${formatKrw(m?.llm_cost_krw ?? 0)}`} sub={m ? `$${m.llm_cost_usd.toFixed(4)} / ${m.llm_total_calls}호출` : undefined} iconBg="bg-amber-100" loading={dashLoading} />
        </GlassCard>
      </div>

      {/* ── Row 2: 스케줄러 + 시스템 상태 ── */}
      <div className="grid gap-3 lg:grid-cols-5">
        {/* 스케줄러 컨트롤 3/5 */}
        <GlassCard className="lg:col-span-3 p-5" glow="blue">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Cpu className="h-4 w-4 text-slate-500" />
              <h2 className="text-sm font-semibold text-slate-700">스케줄러</h2>
            </div>
            <button
              type="button" onClick={handleSchedulerToggle} disabled={schedulerToggling || dashLoading}
              className={`inline-flex items-center gap-1.5 rounded-full px-4 py-1.5 text-sm font-semibold shadow-sm transition disabled:opacity-50 ${dashboard?.scheduler.scheduler_running ? "bg-rose-500 text-white hover:bg-rose-600" : "bg-emerald-500 text-white hover:bg-emerald-600"}`}
            >
              {schedulerToggling ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : dashboard?.scheduler.scheduler_running ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
              {dashboard?.scheduler.scheduler_running ? "중지" : "시작"}
            </button>
          </div>
          {toggleMsg && <p className="mt-2 rounded-lg bg-slate-100 px-3 py-1.5 text-xs text-slate-600">{toggleMsg}</p>}

          <div className="mt-4 space-y-3">
            <div>
              <div className="mb-1 flex items-center justify-between text-xs text-slate-500">
                <span>오늘 진행률</span>
                <span className="font-medium text-slate-700">
                  {dashLoading ? "..." : `${s?.today_completed ?? 0} / ${s?.daily_target ?? 3}편 (${todayPct}%)`}
                </span>
              </div>
              {dashLoading ? <Skeleton className="h-1.5 w-full" /> : <ProgressBar value={s?.today_completed ?? 0} max={s?.daily_target ?? 3} color="bg-blue-500" />}
            </div>
            <div className="grid grid-cols-2 gap-2 text-xs">
              {[
                { label: "실패", value: `${s?.today_failed ?? 0}건`, red: (s?.today_failed ?? 0) > 0 },
                { label: "발행 준비", value: `${s?.ready_to_publish ?? 0}건`, red: false },
                { label: "큐 대기", value: `${s?.queued ?? 0}건`, red: false },
                { label: "다음 발행", value: s?.next_publish_slot_kst ? new Date(s.next_publish_slot_kst).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" }) : "—", red: false },
              ].map(({ label, value, red }) => (
                <div key={label} className="rounded-lg bg-white/70 px-3 py-2">
                  <p className="flex items-center gap-0.5 text-slate-500">{label === "다음 발행" && <Clock className="h-3 w-3 mr-0.5" />}{label}</p>
                  {dashLoading ? <Skeleton className="mt-1 h-4 w-8" /> : <p className={`mt-0.5 font-bold ${red ? "text-rose-600" : "text-slate-700"}`}>{value}</p>}
                </div>
              ))}
            </div>
          </div>
        </GlassCard>

        {/* 시스템 상태 2/5 */}
        <GlassCard className="lg:col-span-2 p-5" glow="green">
          <div className="flex items-center gap-2 mb-4">
            <Activity className="h-4 w-4 text-slate-500" />
            <h2 className="text-sm font-semibold text-slate-700">시스템 상태</h2>
          </div>
          <div className="space-y-3">
            {[
              {
                icon: <BrainCircuit className="h-3.5 w-3.5" />, label: "LLM 프로바이더",
                ok: h?.status === "ok",
                labelOk: `${h?.ok ?? 0}/${h?.total ?? 0} 정상`,
                labelFail: `${h?.fail ?? 0}개 이상`,
                sub: null,
              },
              {
                icon: <MessageSquare className="h-3.5 w-3.5" />, label: "텔레그램 봇",
                ok: t?.live_ok === true,
                labelOk: t?.bot_username ? `@${t.bot_username}` : "연결됨",
                labelFail: t?.configured ? "연결 불량" : "미설정",
                sub: t?.error || null,
              },
              {
                icon: <Zap className="h-3.5 w-3.5" />, label: "스케줄러",
                ok: s?.scheduler_running === true,
                labelOk: "실행 중", labelFail: "중지됨",
                sub: s ? `활성: ${s.active_hours}` : null,
              },
            ].map(({ icon, label, ok, labelOk, labelFail, sub }) => (
              <div key={label} className="rounded-xl bg-white/70 p-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-1.5 text-xs font-medium text-slate-600">{icon}{label}</div>
                  {dashLoading ? <Skeleton className="h-5 w-16" /> : <StatusBadge ok={ok} labelOk={labelOk} labelFail={labelFail} />}
                </div>
                {sub && !dashLoading && <p className="mt-1 text-xs text-slate-400">{sub}</p>}
              </div>
            ))}
          </div>
        </GlassCard>
      </div>

      {/* ── Row 3: 워크스페이스 ── */}
      <GlassCard className="p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-slate-500" />
            <h2 className="text-sm font-semibold text-slate-700">워크스페이스</h2>
          </div>
          <div className="inline-flex rounded-full border border-slate-200/80 bg-white/80 p-1">
            {(["magic", "vault"] as const).map((tab) => (
              <button key={tab} type="button" onClick={() => setWorkspaceTab(tab)}
                className={`inline-flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs font-semibold transition ${workspaceTab === tab ? "bg-slate-900 text-white shadow-sm" : "text-slate-600 hover:bg-slate-100"}`}>
                {tab === "magic" ? <><Zap className="h-3 w-3" />Magic Input</> : (
                  <><BookOpen className="h-3 w-3" />Idea Vault
                    {(ideaVaultStats?.pending ?? 0) > 0 && (
                      <span className="ml-0.5 rounded-full bg-purple-500 px-1.5 text-white text-xs">{ideaVaultStats?.pending}</span>
                    )}
                  </>
                )}
              </button>
            ))}
          </div>
        </div>

        {workspaceTab === "magic" && (
          <div className="mt-4 space-y-3">
            <p className="text-sm text-slate-500">자연어 문장 1개만 입력하면 title/keywords/persona를 자동 추출해 예약 큐에 넣습니다.</p>
            <form onSubmit={handleMagicSubmit} className="space-y-3">
              <textarea value={instruction} onChange={(e) => setInstruction(e.target.value)}
                className="min-h-28 w-full rounded-xl border border-slate-200 bg-white/80 px-4 py-3 text-sm outline-none transition focus:border-slate-500 focus:ring-2 focus:ring-slate-500/10"
                placeholder="예) 내일 아침 9시에 스벅 아아 리뷰 올려줘, IT전문가 톤으로." />
              <button type="button" onClick={() => setAdvancedOpen((p) => !p)}
                className="inline-flex items-center gap-1 text-xs font-medium text-slate-500 transition hover:text-slate-700">
                <Settings2 className="h-3.5 w-3.5" />고급 설정{advancedOpen ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
              </button>
              {advancedOpen && (
                <div className="grid gap-3 rounded-xl border border-slate-200 bg-slate-50/80 p-4 sm:grid-cols-2">
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium text-slate-600">Persona</span>
                    <select value={advancedPersonaId} onChange={(e) => setAdvancedPersonaId(e.target.value)} className="w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm">
                      {PERSONA_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  </label>
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium text-slate-600">Topic</span>
                    <select value={advancedTopicMode} onChange={(e) => setAdvancedTopicMode(e.target.value)} className="w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm">
                      {TOPIC_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  </label>
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium text-slate-600">Scheduled At</span>
                    <input type="datetime-local" value={advancedScheduleAt} onChange={(e) => setAdvancedScheduleAt(e.target.value)} className="w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm" />
                  </label>
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium text-slate-600">Keywords Override</span>
                    <input value={advancedKeywordsText} onChange={(e) => setAdvancedKeywordsText(e.target.value)} className="w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm" placeholder="예) 자동화, SEO" />
                  </label>
                  <label className="block sm:col-span-2">
                    <span className="mb-1 block text-xs font-medium text-slate-600">Category Override</span>
                    <input value={advancedCategory} onChange={(e) => setAdvancedCategory(e.target.value)} className="w-full rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm" placeholder="비워두면 Topic 기준 자동 카테고리" />
                  </label>
                </div>
              )}
              <button type="submit" disabled={submittingMagic}
                className="inline-flex items-center gap-2 rounded-full bg-slate-900 px-5 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-slate-700 disabled:opacity-50">
                {submittingMagic ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                {submittingMagic ? "등록 중..." : "매직 예약 생성"}
              </button>
            </form>
            {magicMessage && (
              <p className={`rounded-xl border px-3 py-2 text-sm ${magicMessage.startsWith("✅") ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-slate-200 bg-slate-50 text-slate-700"}`}>{magicMessage}</p>
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
            <p className="text-sm text-slate-500">100~200줄 아이디어를 한 번에 넣어 대량 적재합니다. 유효 문장만 걸러서 카테고리를 자동 분류합니다.</p>
            <form onSubmit={handleIdeaVaultSubmit} className="space-y-3">
              <textarea value={ideaVaultText} onChange={(e) => setIdeaVaultText(e.target.value)}
                className="min-h-56 w-full rounded-xl border border-slate-200 bg-white/80 px-4 py-3 text-sm outline-none transition focus:border-slate-500 focus:ring-2 focus:ring-slate-500/10"
                placeholder={`예) 내일 카페 아침 매출을 올리는 오픈 루틴 정리\n예) 자동화 도구로 블로그 글감 수집 시간 절약한 방법\n...`} />
              <button type="submit" disabled={ideaVaultSubmitting}
                className="inline-flex items-center gap-2 rounded-full bg-slate-900 px-5 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-slate-700 disabled:opacity-50">
                {ideaVaultSubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
                {ideaVaultSubmitting ? "적재 중..." : "아이디어 창고 적재"}
              </button>
            </form>
            {ideaVaultMessage && (
              <p className={`rounded-xl border px-3 py-2 text-sm ${ideaVaultMessage.startsWith("✅") ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-slate-200 bg-slate-50 text-slate-700"}`}>{ideaVaultMessage}</p>
            )}
          </div>
        )}
      </GlassCard>

      {/* ── Row 4: LLM + 시드 현황 ── */}
      <div className="grid gap-3 lg:grid-cols-2">
        <GlassCard className="p-5" glow="purple">
          <div className="flex items-center gap-2 mb-4">
            <BrainCircuit className="h-4 w-4 text-slate-500" />
            <h2 className="text-sm font-semibold text-slate-700">LLM 사용 현황</h2>
          </div>
          {dashLoading ? (
            <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-9 w-full" />)}</div>
          ) : m && m.llm_total_calls > 0 ? (
            <div className="space-y-1.5">
              <div className="grid grid-cols-3 pb-1 text-xs font-medium text-slate-400 border-b border-slate-200">
                <span>항목</span><span className="text-center">호출</span><span className="text-right">비용</span>
              </div>
              <div className="rounded-xl bg-white/70 px-3 py-2">
                <div className="grid grid-cols-3 text-sm">
                  <span className="font-medium text-slate-700">전체</span>
                  <span className="text-center text-slate-600">{m.llm_total_calls}회</span>
                  <span className="text-right font-bold text-amber-600">₩{formatKrw(m.llm_cost_krw)}</span>
                </div>
              </div>
              <div className="rounded-xl bg-white/70 px-3 py-2">
                <div className="grid grid-cols-3 text-xs text-slate-500">
                  <span>USD 환산</span><span className="text-center">—</span>
                  <span className="text-right">${m.llm_cost_usd.toFixed(4)}</span>
                </div>
              </div>
            </div>
          ) : (
            <p className="text-sm text-slate-400">LLM 호출 기록이 없습니다.</p>
          )}
        </GlassCard>

        <GlassCard className="p-5" glow="amber">
          <div className="flex items-center gap-2 mb-4">
            <Layers className="h-4 w-4 text-slate-500" />
            <h2 className="text-sm font-semibold text-slate-700">시드 & 큐 현황</h2>
          </div>
          {dashLoading ? (
            <div className="space-y-2">{Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}</div>
          ) : (
            <div className="space-y-1.5">
              {[
                { label: "마지막 시드 날짜", value: s?.last_seed_date || "—" },
                { label: "마지막 시드 건수", value: `${s?.last_seed_count ?? 0}건` },
                { label: "발행 준비 완료", value: `${s?.ready_to_publish ?? 0}건` },
                { label: "큐 대기 중", value: `${s?.queued ?? 0}건` },
              ].map(({ label, value }) => (
                <div key={label} className="flex items-center justify-between rounded-xl bg-white/70 px-3 py-2 text-sm">
                  <span className="text-slate-500">{label}</span>
                  <span className="font-semibold text-slate-800">{value}</span>
                </div>
              ))}
            </div>
          )}
        </GlassCard>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 온보딩 위자드 + 루트 컴포넌트
// ---------------------------------------------------------------------------

export function DashboardRenewal() {
  const [loading, setLoading] = useState(true);
  const [loadingError, setLoadingError] = useState("");
  const [onboarding, setOnboarding] = useState<OnboardingStatusResponse | null>(null);

  const [step, setStep] = useState(0);
  const [saving, setSaving] = useState(false);
  const [stepMessage, setStepMessage] = useState("");
  const [routerSaving, setRouterSaving] = useState(false);
  const [routerLoading, setRouterLoading] = useState(false);
  const [routerMessage, setRouterMessage] = useState("");

  const [strategyMode, setStrategyMode] = useState<"cost" | "quality">("cost");
  const [textApiKeys, setTextApiKeys] = useState<Record<string, string>>({ qwen: "", deepseek: "", gemini: "", openai: "", claude: "" });
  const [textApiMasks, setTextApiMasks] = useState<Record<string, string>>({});
  const [imageApiKeys, setImageApiKeys] = useState<Record<string, string>>({ pexels: "", together: "", fal: "", openai_image: "" });
  const [imageApiMasks, setImageApiMasks] = useState<Record<string, string>>({});
  const [imageEngine, setImageEngine] = useState("pexels");
  const [imageEnabled, setImageEnabled] = useState(true);
  const [imagesPerPostMin, setImagesPerPostMin] = useState(0);
  const [imagesPerPostMax, setImagesPerPostMax] = useState(2);
  const [routerQuote, setRouterQuote] = useState<RouterQuoteResponse | null>(null);
  const [textModelMatrix, setTextModelMatrix] = useState<Array<Record<string, unknown>>>([]);
  const [imageModelMatrix, setImageModelMatrix] = useState<Array<Record<string, unknown>>>([]);
  const [naverStatus, setNaverStatus] = useState<NaverConnectStatusResponse | null>(null);
  const [naverConnecting, setNaverConnecting] = useState(false);

  const [personaId, setPersonaId] = useState("P1");
  const [identity, setIdentity] = useState("");
  const [targetAudience, setTargetAudience] = useState("");
  const [toneHint, setToneHint] = useState("");
  const [interestsText, setInterestsText] = useState("");
  const [structureScore, setStructureScore] = useState(50);
  const [evidenceScore, setEvidenceScore] = useState(50);
  const [distanceScore, setDistanceScore] = useState(50);
  const [criticismScore, setCriticismScore] = useState(50);
  const [densityScore, setDensityScore] = useState(50);
  const [styleStrength, setStyleStrength] = useState(40);

  const [recommendedCategories, setRecommendedCategories] = useState<string[]>([]);
  const [categoriesText, setCategoriesText] = useState("");
  const [fallbackCategory, setFallbackCategory] = useState(DEFAULT_FALLBACK_CATEGORY);
  const [dailyPostsTarget, setDailyPostsTarget] = useState(3);
  const [ideaVaultDailyQuota, setIdeaVaultDailyQuota] = useState(2);
  const [categoryAllocations, setCategoryAllocations] = useState<ScheduleAllocationItem[]>([]);
  const [botToken, setBotToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [telegramVerified, setTelegramVerified] = useState(false);

  useEffect(() => {
    let mounted = true;
    async function load() {
      try {
        const [res, routerState, naverState] = await Promise.all([fetchOnboardingStatus(), fetchRouterSettings(), fetchNaverConnectStatus()]);
        if (!mounted) return;
        setOnboarding(res);
        setPersonaId(res.persona_id || "P1");
        setRecommendedCategories(res.recommended_categories || []);
        setCategoriesText((res.categories || []).join(", "));
        setFallbackCategory(res.fallback_category || DEFAULT_FALLBACK_CATEGORY);
        setTelegramVerified(Boolean(res.telegram_configured));
        setInterestsText((res.interests || []).join(", "));
        const rt = Math.max(3, Math.min(5, Number(res.daily_posts_target || 3)));
        const rq = Math.max(0, Math.min(rt, Number(res.idea_vault_daily_quota ?? Math.min(2, rt))));
        setDailyPostsTarget(rt); setIdeaVaultDailyQuota(rq);
        setCategoryAllocations(normalizeAllocations(res.categories || [], Math.max(0, rt - rq), res.category_allocations || []));
        setStrategyMode(routerState.settings.strategy_mode === "quality" ? "quality" : "cost");
        setTextApiMasks(routerState.settings.text_api_keys_masked || {});
        setImageApiMasks(routerState.settings.image_api_keys_masked || {});
        setImageEngine(routerState.settings.image_engine || "pexels");
        setImageEnabled(Boolean(routerState.settings.image_enabled));
        setImagesPerPostMin(Math.max(0, Math.min(4, Number(routerState.settings.images_per_post_min || 0))));
        setImagesPerPostMax(Math.max(0, Math.min(4, Number(routerState.settings.images_per_post_max || Math.max(0, Math.min(4, Number(routerState.settings.images_per_post || 1)))))));
        setRouterQuote({ strategy_mode: routerState.settings.strategy_mode === "quality" ? "quality" : "cost", roles: routerState.roles || {}, estimate: { currency: "KRW", text_cost_krw: Number(routerState.quote.text_cost_krw || 0), image_cost_krw: Number(routerState.quote.image_cost_krw || 0), total_cost_krw: Number(routerState.quote.total_cost_krw || 0), cost_min_krw: Number(routerState.quote.cost_min_krw ?? routerState.quote.total_cost_krw ?? 0), cost_max_krw: Number(routerState.quote.cost_max_krw ?? routerState.quote.total_cost_krw ?? 0), quality_score: Number(routerState.quote.quality_score || 0) }, image: {}, available_text_models: [] });
        setTextModelMatrix(routerState.matrix.text_models || []);
        setImageModelMatrix(routerState.matrix.image_models || []);
        setNaverStatus(naverState);
      } catch (e) {
        if (!mounted) return;
        setLoadingError(e instanceof Error ? e.message : "온보딩 상태를 불러오지 못했습니다.");
      } finally {
        if (mounted) setLoading(false);
      }
    }
    load();
    return () => { mounted = false; };
  }, []);

  const stepTitles = useMemo(() => ["0. Router", "1. Persona Lab", "2. Category Sync", "3. Schedule & Ratio", "4. Telegram Setup"], []);
  const trendDailyTarget = useMemo(() => Math.max(0, dailyPostsTarget - ideaVaultDailyQuota), [dailyPostsTarget, ideaVaultDailyQuota]);
  const allocationTotal = useMemo(() => categoryAllocations.reduce((a, r) => a + Math.max(0, Number(r.count || 0)), 0), [categoryAllocations]);

  const parserModelLabel = useMemo(() => { const r = routerQuote?.roles?.parser; if (!r || typeof r !== "object") return "-"; const l = (r as Record<string, unknown>).label; return typeof l === "string" ? l : "-"; }, [routerQuote]);
  const qualityModelLabel = useMemo(() => { const r = routerQuote?.roles?.quality_step; if (!r || typeof r !== "object") return "-"; const l = (r as Record<string, unknown>).label; return typeof l === "string" ? l : "-"; }, [routerQuote]);
  const voiceModelLabel = useMemo(() => { const r = routerQuote?.roles?.voice_step; if (!r || typeof r !== "object") return "-"; const l = (r as Record<string, unknown>).label; return typeof l === "string" ? l : "-"; }, [routerQuote]);

  const quoteTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (quoteTimerRef.current) clearTimeout(quoteTimerRef.current);
    quoteTimerRef.current = setTimeout(async () => {
      setRouterLoading(true);
      try { const q = await quoteRouterSettings({ strategy_mode: strategyMode, text_api_keys: compactKeys(textApiKeys), image_api_keys: compactKeys(imageApiKeys), image_engine: imageEngine, image_enabled: imageEnabled, images_per_post: imagesPerPostMax, images_per_post_min: imagesPerPostMin, images_per_post_max: imagesPerPostMax }); setRouterQuote(q); }
      catch { /* ignore */ } finally { setRouterLoading(false); }
    }, 350);
    return () => { if (quoteTimerRef.current) clearTimeout(quoteTimerRef.current); };
  }, [strategyMode, textApiKeys, imageApiKeys, imageEngine, imageEnabled, imagesPerPostMin, imagesPerPostMax]);

  const handleTextKeyChange = (k: string, v: string) => setTextApiKeys((p) => ({ ...p, [k]: v }));
  const handleImageKeyChange = (k: string, v: string) => setImageApiKeys((p) => ({ ...p, [k]: v }));

  async function handleSaveRouterStep() {
    setRouterSaving(true); setRouterMessage("");
    try {
      const saved = await saveRouterSettings({ strategy_mode: strategyMode, text_api_keys: compactKeys(textApiKeys), image_api_keys: compactKeys(imageApiKeys), image_engine: imageEngine, image_enabled: imageEnabled, images_per_post: imagesPerPostMax, images_per_post_min: imagesPerPostMin, images_per_post_max: imagesPerPostMax });
      setTextApiMasks(saved.settings.text_api_keys_masked || {}); setImageApiMasks(saved.settings.image_api_keys_masked || {});
      setStrategyMode(saved.settings.strategy_mode === "quality" ? "quality" : "cost");
      setImageEngine(saved.settings.image_engine || "pexels"); setImageEnabled(Boolean(saved.settings.image_enabled));
      setImagesPerPostMin(Math.max(0, Math.min(4, Number(saved.settings.images_per_post_min || 0))));
      setImagesPerPostMax(Math.max(0, Math.min(4, Number(saved.settings.images_per_post_max || Math.max(0, Math.min(4, Number(saved.settings.images_per_post || 1)))))));
      setTextModelMatrix(saved.matrix.text_models || []); setImageModelMatrix(saved.matrix.image_models || []);
      setRouterQuote((p) => ({ strategy_mode: saved.settings.strategy_mode === "quality" ? "quality" : "cost", roles: saved.roles || p?.roles || {}, estimate: { currency: "KRW", text_cost_krw: Number(saved.quote.text_cost_krw || 0), image_cost_krw: Number(saved.quote.image_cost_krw || 0), total_cost_krw: Number(saved.quote.total_cost_krw || 0), cost_min_krw: Number(saved.quote.cost_min_krw ?? saved.quote.total_cost_krw ?? 0), cost_max_krw: Number(saved.quote.cost_max_krw ?? saved.quote.total_cost_krw ?? 0), quality_score: Number(saved.quote.quality_score || 0) }, image: p?.image || {}, available_text_models: p?.available_text_models || [] }));
      setStep(1); setStepMessage("Step 0 저장 완료."); setRouterMessage("라우터 설정 저장 완료");
    } catch (e) { const msg = e instanceof Error ? e.message : "오류"; setRouterMessage(msg); setStepMessage(msg); }
    finally { setRouterSaving(false); }
  }

  async function handleNaverConnect() {
    setNaverConnecting(true); setRouterMessage("");
    try { const res = await startNaverConnect({ timeout_sec: 300 }); const st = await fetchNaverConnectStatus(); setNaverStatus(st); setRouterMessage(res.message); }
    catch (e) { setRouterMessage(e instanceof Error ? e.message : "네이버 연동 실패"); }
    finally { setNaverConnecting(false); }
  }

  async function handleSavePersonaStep() {
    setSaving(true); setStepMessage("");
    try {
      const res = await savePersonaLab({ persona_id: personaId, identity, target_audience: targetAudience, tone_hint: toneHint, interests: parseCommaValues(interestsText), structure_score: structureScore, evidence_score: evidenceScore, distance_score: distanceScore, criticism_score: criticismScore, density_score: densityScore, style_strength: styleStrength, mbti: "", mbti_enabled: false, mbti_confidence: 0, age_group: "30대", gender: "남성" });
      setRecommendedCategories(res.recommended_categories); setCategoriesText(res.recommended_categories.join(", "));
      setStep(2); setStepMessage("Step 1 저장 완료.");
    } catch (e) { setStepMessage(e instanceof Error ? e.message : "오류"); }
    finally { setSaving(false); }
  }

  async function handleSaveCategoryStep() {
    setSaving(true); setStepMessage("");
    try {
      const res = await saveOnboardingCategories({ categories: parseCommaValues(categoriesText), fallback_category: fallbackCategory || DEFAULT_FALLBACK_CATEGORY });
      setCategoriesText(res.categories.join(", ")); setFallbackCategory(res.fallback_category);
      setCategoryAllocations(normalizeAllocations(res.categories, trendDailyTarget, categoryAllocations));
      setStep(3); setStepMessage("Step 2 저장 완료.");
    } catch (e) { setStepMessage(e instanceof Error ? e.message : "오류"); }
    finally { setSaving(false); }
  }

  function handleDailyTargetChange(n: number) {
    const t = Math.max(3, Math.min(5, n));
    setDailyPostsTarget(t);
    const q = Math.max(0, Math.min(t, ideaVaultDailyQuota));
    setIdeaVaultDailyQuota(q);
    setCategoryAllocations(normalizeAllocations(categoryAllocations.map((r) => r.category), Math.max(0, t - q), categoryAllocations));
  }

  function handleIdeaVaultQuotaChange(n: number) {
    const q = Math.max(0, Math.min(dailyPostsTarget, n));
    setIdeaVaultDailyQuota(q);
    setCategoryAllocations(normalizeAllocations(categoryAllocations.map((r) => r.category), Math.max(0, dailyPostsTarget - q), categoryAllocations));
  }

  function handleAllocationChange(i: number, patch: Partial<ScheduleAllocationItem>) {
    setCategoryAllocations((prev) => {
      const next = [...prev]; const cur = next[i];
      if (!cur) return prev;
      next[i] = { ...cur, ...patch, count: patch.count === undefined ? cur.count : Math.max(0, Math.min(5, Number(patch.count || 0))), topic_mode: patch.topic_mode === undefined ? cur.topic_mode : String(patch.topic_mode || "cafe") };
      return next;
    });
  }

  async function handleSaveScheduleStep() {
    setSaving(true); setStepMessage("");
    try {
      const norm = normalizeAllocations(categoryAllocations.map((r) => r.category), trendDailyTarget, categoryAllocations);
      const res = await saveOnboardingSchedule({ daily_posts_target: dailyPostsTarget, idea_vault_daily_quota: ideaVaultDailyQuota, allocations: norm });
      setDailyPostsTarget(res.daily_posts_target); setIdeaVaultDailyQuota(res.idea_vault_daily_quota);
      setCategoryAllocations(normalizeAllocations(res.allocations.map((r) => r.category), Math.max(0, res.daily_posts_target - res.idea_vault_daily_quota), res.allocations));
      setStep(4); setStepMessage("Step 3 저장 완료.");
    } catch (e) { setStepMessage(e instanceof Error ? e.message : "오류"); }
    finally { setSaving(false); }
  }

  async function handleTestTelegram() {
    setSaving(true); setStepMessage("");
    try {
      const res = await testTelegramSetup({ bot_token: botToken, chat_id: chatId, save: true });
      if (res.success) { setTelegramVerified(true); setStepMessage("테스트 발송 성공: 핸드폰 알림 수신을 확인해 주세요."); }
    } catch (e) { setTelegramVerified(false); setStepMessage(e instanceof Error ? e.message : "테스트 발송 실패"); }
    finally { setSaving(false); }
  }

  async function handleCompleteOnboarding() {
    setSaving(true); setStepMessage("");
    try {
      const res = await completeOnboarding();
      if (res.completed) {
        setOnboarding((p) => p ? { ...p, completed: true, persona_id: personaId, categories: parseCommaValues(categoriesText), fallback_category: fallbackCategory || DEFAULT_FALLBACK_CATEGORY, daily_posts_target: dailyPostsTarget, idea_vault_daily_quota: ideaVaultDailyQuota, category_allocations: categoryAllocations, telegram_configured: telegramVerified } : null);
        setStepMessage("온보딩 완료!");
      }
    } catch (e) { setStepMessage(e instanceof Error ? e.message : "온보딩 완료 처리 실패"); }
    finally { setSaving(false); }
  }

  // ── 로딩 스켈레톤 ──
  if (loading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-16 w-full rounded-2xl" />
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">{Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-24 rounded-2xl" />)}</div>
        <Skeleton className="h-48 w-full rounded-2xl" />
      </div>
    );
  }

  // ── 에러 ──
  if (loadingError) {
    return (
      <GlassCard className="p-6">
        <div className="flex items-center gap-2 text-rose-600">
          <AlertTriangle className="h-5 w-5 shrink-0" />
          <p className="text-sm">{loadingError}</p>
        </div>
      </GlassCard>
    );
  }

  // ── 온보딩 완료 → 메인 대시보드 ──
  if (onboarding?.completed) return <MainDashboard onboarding={onboarding} />;

  // ── 온보딩 위자드 ──
  return (
    <div className="space-y-4">
      <GlassCard className="p-5">
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">Onboarding Wizard</h1>
        <p className="mt-1 text-sm text-slate-500">5단계 설정을 완료하면 매직 인풋만으로 예약 발행을 시작할 수 있습니다.</p>
        <div className="mt-4 grid gap-2 sm:grid-cols-5">
          {stepTitles.map((title, i) => {
            const active = step === i; const passed = step > i;
            return (
              <div key={title} className={`rounded-xl border px-3 py-2 text-sm ${active ? "border-slate-800 bg-slate-900 text-white" : passed ? "border-emerald-300 bg-emerald-50 text-emerald-700" : "border-slate-200 bg-slate-50 text-slate-500"}`}>
                {title}
              </div>
            );
          })}
        </div>
      </GlassCard>

      {step === 0 && (
        <GlassCard className="p-5">
          <h2 className="text-lg font-semibold text-slate-900">Step 0. Zero-Config Router</h2>
          <p className="mt-1 text-sm text-slate-500">API 키를 넣고 전략을 선택하면 파서/품질/보이스 모델이 자동 배정되고 1편당 예상 원가가 즉시 계산됩니다.</p>
          <div className="mt-4 inline-flex rounded-full border border-slate-200 p-1">
            {(["cost", "quality"] as const).map((mode) => (
              <button key={mode} type="button" onClick={() => setStrategyMode(mode)}
                className={`rounded-full px-4 py-1 text-sm font-medium transition ${strategyMode === mode ? "bg-slate-900 text-white" : "text-slate-700 hover:bg-slate-100"}`}>
                {mode === "cost" ? "⚖️ 가성비 우선" : "💎 품질 우선"}
              </button>
            ))}
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            {Array.from(new Set(textModelMatrix.map((item) => (typeof item.key_id === "string" ? item.key_id : "")).filter(Boolean))).map((keyId) => (
              <label key={keyId} className="block">
                <span className="mb-1 block text-sm font-medium text-slate-700">{keyId.toUpperCase()} API Key</span>
                <input type="password" value={textApiKeys[keyId] || ""} onChange={(e) => handleTextKeyChange(keyId, e.target.value)} className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm" placeholder={textApiMasks[keyId] ? `${textApiMasks[keyId]} (저장됨)` : "선택 입력"} />
              </label>
            ))}
          </div>
          <div className="mt-4 grid gap-3 rounded-xl border border-slate-200 bg-slate-50 p-4 sm:grid-cols-2">
            <label className="block">
              <span className="mb-1 block text-sm font-medium text-slate-700">이미지 엔진</span>
              <select value={imageEngine} onChange={(e) => setImageEngine(e.target.value)} className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm">
                {imageModelMatrix.map((item, idx) => { const eid = typeof item.engine_id === "string" ? item.engine_id : `e-${idx}`; const lbl = typeof item.label === "string" ? item.label : eid; return <option key={eid} value={eid}>{lbl}</option>; })}
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block text-sm font-medium text-slate-700">이미지/포스트 최대 수</span>
              <input type="number" min={0} max={4} value={imagesPerPostMax} onChange={(e) => setImagesPerPostMax(Math.max(0, Math.min(4, Number(e.target.value))))} className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm" />
            </label>
            <label className="flex items-center gap-2 sm:col-span-2">
              <input type="checkbox" checked={imageEnabled} onChange={(e) => setImageEnabled(e.target.checked)} />
              <span className="text-sm text-slate-700">이미지 엔진 활성화</span>
            </label>
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            {Array.from(new Set(imageModelMatrix.map((item) => (typeof item.key_id === "string" ? item.key_id : "")).filter(Boolean))).map((keyId) => (
              <label key={keyId} className="block">
                <span className="mb-1 block text-sm font-medium text-slate-700">{keyId.toUpperCase()} Key</span>
                <input type="password" value={imageApiKeys[keyId] || ""} onChange={(e) => handleImageKeyChange(keyId, e.target.value)} className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm" placeholder={imageApiMasks[keyId] ? `${imageApiMasks[keyId]} (저장됨)` : "선택 입력"} />
              </label>
            ))}
          </div>
          <div className="mt-4 rounded-xl border border-slate-200 bg-white/80 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-semibold text-slate-800">실시간 견적서</h3>
              {routerLoading && <span className="text-xs text-slate-500">계산 중...</span>}
            </div>
            <div className="mt-2 grid gap-2 text-sm sm:grid-cols-2">
              <p>예상 원가(1편): <strong>{formatKrw(routerQuote?.estimate.cost_min_krw || 0)}원 ~ {formatKrw(routerQuote?.estimate.cost_max_krw || 0)}원</strong></p>
              <p>예상 품질: <strong>{routerQuote?.estimate.quality_score || 0}점</strong></p>
              <p className="sm:col-span-2">모델 배정: Parser <strong>{parserModelLabel}</strong> / Step1 <strong>{qualityModelLabel}</strong> / Step2 <strong>{voiceModelLabel}</strong></p>
            </div>
          </div>
          <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50 p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-sm font-semibold text-slate-800">네이버 블로그 연동</p>
                <p className="text-xs text-slate-600">상태: {naverStatus?.connected ? "연결됨" : "미연결"}</p>
              </div>
              <button type="button" onClick={handleNaverConnect} disabled={naverConnecting} className="rounded-full bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-emerald-500 disabled:opacity-50">{naverConnecting ? "팝업 실행 중..." : "🟢 네이버 연동 시작"}</button>
            </div>
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <button type="button" onClick={handleSaveRouterStep} disabled={routerSaving} className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50">{routerSaving ? "저장 중..." : "Step 0 저장 후 다음"}</button>
          </div>
          {routerMessage && <p className="mt-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">{routerMessage}</p>}
        </GlassCard>
      )}

      {step === 1 && (
        <GlassCard className="p-5">
          <h2 className="text-lg font-semibold text-slate-900">Step 1. Persona Lab</h2>
          <p className="mt-1 text-sm text-slate-500">5차원 슬라이더로 작성 성향을 조정합니다.</p>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <label className="block"><span className="mb-1 block text-sm font-medium text-slate-700">Persona</span><select value={personaId} onChange={(e) => setPersonaId(e.target.value)} className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm">{PERSONA_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select></label>
            <label className="block"><span className="mb-1 block text-sm font-medium text-slate-700">Interests (comma)</span><input value={interestsText} onChange={(e) => setInterestsText(e.target.value)} className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm" placeholder="예) AI 자동화, 카페 브랜딩" /></label>
            <label className="block"><span className="mb-1 block text-sm font-medium text-slate-700">Identity</span><input value={identity} onChange={(e) => setIdentity(e.target.value)} className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm" placeholder="예) IT 직장인" /></label>
            <label className="block"><span className="mb-1 block text-sm font-medium text-slate-700">Target Audience</span><input value={targetAudience} onChange={(e) => setTargetAudience(e.target.value)} className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm" placeholder="예) 20대 개발 입문자" /></label>
            <label className="block sm:col-span-2"><span className="mb-1 block text-sm font-medium text-slate-700">Tone Hint</span><input value={toneHint} onChange={(e) => setToneHint(e.target.value)} className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm" placeholder="예) 공감형이지만 구조적" /></label>
          </div>
          <div className="mt-4 grid gap-3">
            {[
              { label: "구조 (Bottom-up ↔ Top-down)", value: structureScore, set: setStructureScore },
              { label: "근거 (Subjective ↔ Objective)", value: evidenceScore, set: setEvidenceScore },
              { label: "거리 (Authoritative ↔ Inspiring)", value: distanceScore, set: setDistanceScore },
              { label: "비판 (Avoidant ↔ Direct)", value: criticismScore, set: setCriticismScore },
              { label: "밀도 (Light ↔ Dense)", value: densityScore, set: setDensityScore },
              { label: "스타일 반영 강도 (권장 30~45)", value: styleStrength, set: setStyleStrength },
            ].map(({ label, value, set }) => (
              <label key={label} className="block rounded-xl border border-slate-200 bg-slate-50 p-3">
                <div className="flex items-center justify-between text-sm"><span>{label}</span><span className="font-medium">{value}</span></div>
                <input type="range" min={0} max={100} value={value} onChange={(e) => set(Number(e.target.value))} className="mt-2 w-full" />
              </label>
            ))}
          </div>
          <p className="mt-3 text-xs text-slate-600">현재 스타일: 구조 {sliderLabel(structureScore, ["Bottom-up", "Balanced", "Top-down"])} / 근거 {sliderLabel(evidenceScore, ["Subjective", "Balanced", "Objective"])} / 거리 {sliderLabel(distanceScore, ["Authoritative", "Peer", "Inspiring"])}</p>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <button type="button" onClick={handleSavePersonaStep} disabled={saving} className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50">{saving ? "저장 중..." : "Step 1 저장 후 다음"}</button>
          </div>
        </GlassCard>
      )}

      {step === 2 && (
        <GlassCard className="p-5">
          <h2 className="text-lg font-semibold text-slate-900">Step 2. Category Sync</h2>
          <p className="mt-1 text-sm text-slate-500">추천 카테고리를 참고해 네이버 카테고리명을 동기화하세요.</p>
          <Image src="/assets/placeholder_category_guide.gif" alt="카테고리 생성 가이드" width={1280} height={400} unoptimized className="mt-4 h-28 w-full rounded-xl border border-slate-200 object-cover sm:h-40" />
          <div className="mt-4 flex flex-wrap gap-2">{recommendedCategories.map((c) => <span key={c} className="rounded-full border border-emerald-300 bg-emerald-50 px-3 py-1 text-xs text-emerald-700">{c}</span>)}</div>

          <div className="mt-4">
            <p className="mb-2 text-xs font-semibold text-slate-600">💡 수익성(광고 단가)이 높은 추천 주제 (클릭하여 추가)</p>
            <div className="flex flex-wrap gap-2">
              {[
                { label: "📈 IT/테크", value: "IT/테크" },
                { label: "💰 재테크/금융", value: "재테크/금융" },
                { label: "🩺 건강/의학", value: "건강/의학" },
                { label: "🏠 부동산/인테리어", value: "부동산/인테리어" },
              ].map((cat) => (
                <button
                  key={cat.value}
                  type="button"
                  onClick={() => {
                    const current = categoriesText.split(",").map(s => s.trim()).filter(Boolean);
                    if (!current.includes(cat.value)) {
                      setCategoriesText(current.length > 0 ? `${categoriesText}, ${cat.value}` : cat.value);
                    }
                  }}
                  className="px-3 py-1.5 rounded-lg border border-indigo-200 bg-indigo-50 text-indigo-700 text-xs font-semibold hover:bg-indigo-100 transition-colors"
                >
                  {cat.label}
                </button>
              ))}
            </div>
          </div>

          <label className="mt-4 block"><span className="mb-1 block text-sm font-medium text-slate-700">사용할 카테고리 (comma)</span><input value={categoriesText} onChange={(e) => setCategoriesText(e.target.value)} className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm" placeholder="예) AI 자동화, 생산성 팁, 다양한 생각" /></label>
          <label className="mt-3 block"><span className="mb-1 block text-sm font-medium text-slate-700">Fallback Category</span><input value={fallbackCategory} onChange={(e) => setFallbackCategory(e.target.value)} className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm" /></label>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <button type="button" onClick={() => setStep(1)} className="rounded-full border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-500">이전 단계</button>
            <button type="button" onClick={handleSaveCategoryStep} disabled={saving} className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50">{saving ? "저장 중..." : "Step 2 저장 후 다음"}</button>
          </div>
        </GlassCard>
      )}

      {step === 3 && (
        <GlassCard className="p-5">
          <h2 className="text-lg font-semibold text-slate-900">Step 3. Schedule & Ratio</h2>
          <p className="mt-1 text-sm text-slate-500">어뷰징 위험을 줄이기 위해 하루 3~5편을 권장합니다.</p>
          <label className="mt-4 block rounded-xl border border-slate-200 bg-slate-50 p-3"><div className="flex items-center justify-between text-sm"><span>하루 총 발행량</span><span className="font-semibold">{dailyPostsTarget}편</span></div><input type="range" min={3} max={5} value={dailyPostsTarget} onChange={(e) => handleDailyTargetChange(Number(e.target.value))} className="mt-2 w-full" /></label>
          <label className="mt-3 block rounded-xl border border-slate-200 bg-slate-50 p-3"><div className="flex items-center justify-between text-sm"><span>창고 아이디어(Idea Vault) 하루 사용량</span><span className="font-semibold">{ideaVaultDailyQuota}편</span></div><input type="range" min={0} max={dailyPostsTarget} value={ideaVaultDailyQuota} onChange={(e) => handleIdeaVaultQuotaChange(Number(e.target.value))} className="mt-2 w-full" /><p className="mt-1 text-xs text-slate-600">남은 트렌드 슬롯: <strong>{trendDailyTarget}</strong>편</p></label>
          <div className="mt-4 rounded-xl border border-slate-200">
            <div className="grid grid-cols-12 border-b border-slate-200 bg-slate-50 px-3 py-2 text-xs font-medium text-slate-600"><div className="col-span-5">Category</div><div className="col-span-4">Topic Mode</div><div className="col-span-3">할당량</div></div>
            <div className="divide-y divide-slate-200">
              {categoryAllocations.map((item, idx) => (
                <div key={item.category} className="grid grid-cols-12 items-center gap-2 px-3 py-2">
                  <div className="col-span-5 text-sm text-slate-800">{item.category}</div>
                  <div className="col-span-4"><select value={item.topic_mode} onChange={(e) => handleAllocationChange(idx, { topic_mode: e.target.value })} className="w-full rounded-lg border border-slate-300 px-2 py-1 text-xs">{TOPIC_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select></div>
                  <div className="col-span-3"><input type="number" min={0} max={5} value={item.count} onChange={(e) => handleAllocationChange(idx, { count: Number(e.target.value) })} className="w-full rounded-lg border border-slate-300 px-2 py-1 text-sm" /></div>
                </div>
              ))}
            </div>
          </div>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-slate-600">현재 트렌드 할당 합계: <strong>{allocationTotal}</strong> / 목표 <strong>{trendDailyTarget}</strong></p>
            <button type="button" onClick={() => setCategoryAllocations(normalizeAllocations(categoryAllocations.map((r) => r.category), trendDailyTarget, []))} className="rounded-full border border-slate-300 px-3 py-1 text-xs font-medium text-slate-700 transition hover:border-slate-500">균등 분배 자동 맞춤</button>
          </div>
          {allocationTotal !== trendDailyTarget && <p className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">할당량 합계가 목표 트렌드 발행량과 다릅니다. 저장 시 자동 보정됩니다.</p>}
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <button type="button" onClick={() => setStep(2)} className="rounded-full border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-500">이전 단계</button>
            <button type="button" onClick={handleSaveScheduleStep} disabled={saving || (trendDailyTarget > 0 && categoryAllocations.length === 0)} className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50">{saving ? "저장 중..." : "Step 3 저장 후 다음"}</button>
          </div>
        </GlassCard>
      )}

      {step === 4 && (
        <GlassCard className="p-5">
          <h2 className="text-lg font-semibold text-slate-900">Step 4. Telegram Setup</h2>
          <p className="mt-1 text-sm text-slate-500">22:30 요약 보고와 치명 에러 알림을 받을 수 있게 텔레그램을 연결하세요.</p>
          <Image src="/assets/placeholder_telegram_guide.gif" alt="텔레그램 연결 가이드" width={1280} height={400} unoptimized className="mt-4 h-28 w-full rounded-xl border border-slate-200 object-cover sm:h-40" />
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <label className="block"><span className="mb-1 block text-sm font-medium text-slate-700">Bot Token</span><input value={botToken} onChange={(e) => setBotToken(e.target.value)} className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm" placeholder="123456:ABC-..." /></label>
            <label className="block"><span className="mb-1 block text-sm font-medium text-slate-700">Chat ID</span><input value={chatId} onChange={(e) => setChatId(e.target.value)} className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm" placeholder="123456789" /></label>
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <button type="button" onClick={() => setStep(3)} className="rounded-full border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-500">이전 단계</button>
            <button type="button" onClick={handleTestTelegram} disabled={saving} className="rounded-full border border-slate-400 px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-600 disabled:opacity-50">{saving ? "테스트 중..." : "테스트 발송"}</button>
            <button type="button" onClick={handleCompleteOnboarding} disabled={saving || !telegramVerified} className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50">온보딩 완료</button>
          </div>
        </GlassCard>
      )}

      {stepMessage && <p className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">{stepMessage}</p>}
    </div>
  );
}
