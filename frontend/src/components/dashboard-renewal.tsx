"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  ExternalLink,
  FileText,
  Pause,
  Play,
  RefreshCw,
  Send,
  Server,
  ShieldCheck,
} from "lucide-react";
import {
  fetchDashboard,
  fetchJobs,
  fetchOpsCheck,
  pauseScheduler,
  resumeScheduler,
  type DashboardResponse,
  type JobsResponse,
  type OpsCheckResponse,
} from "@/lib/api";

type JobItem = JobsResponse["items"][number];

type SlotKey = "kr" | "insight" | "us";

type SlotDefinition = {
  key: SlotKey;
  label: string;
  timeLabel: string;
  description: string;
};

const MARKET_SLOTS: SlotDefinition[] = [
  {
    key: "kr",
    label: "국장 전",
    timeLabel: "08:10",
    description: "지난밤 미국장과 환율 흐름을 국장 기준으로 정리",
  },
  {
    key: "insight",
    label: "통찰형",
    timeLabel: "18:30",
    description: "주말과 휴장일에도 이어지는 공부형 투자 노트",
  },
  {
    key: "us",
    label: "미장 전",
    timeLabel: "20:30/21:30",
    description: "아시아 마감과 미국 선물을 미장 기준으로 정리",
  },
];

const STATUS_META: Record<string, { label: string; className: string }> = {
  queued: {
    label: "예약됨",
    className: "border-slate-200 bg-slate-50 text-slate-700",
  },
  retry_wait: {
    label: "재시도 대기",
    className: "border-amber-200 bg-amber-50 text-amber-700",
  },
  running: {
    label: "생성 중",
    className: "border-blue-200 bg-blue-50 text-blue-700",
  },
  awaiting_images: {
    label: "이미지 대기",
    className: "border-cyan-200 bg-cyan-50 text-cyan-700",
  },
  awaiting_approval: {
    label: "승인 대기",
    className: "border-teal-200 bg-teal-50 text-teal-700",
  },
  ready_to_publish: {
    label: "임시저장 대기",
    className: "border-indigo-200 bg-indigo-50 text-indigo-700",
  },
  completed: {
    label: "임시저장 완료",
    className: "border-emerald-200 bg-emerald-50 text-emerald-700",
  },
  failed: {
    label: "실패",
    className: "border-rose-200 bg-rose-50 text-rose-700",
  },
  failed_quality: {
    label: "수정 필요",
    className: "border-rose-200 bg-rose-50 text-rose-700",
  },
  cancelled: {
    label: "취소됨",
    className: "border-slate-200 bg-slate-50 text-slate-500",
  },
};

const LOCAL_TIME_FORMAT = new Intl.DateTimeFormat("ko-KR", {
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "Asia/Seoul",
});

const LOCAL_DATE_FORMAT = new Intl.DateTimeFormat("ko-KR", {
  month: "2-digit",
  day: "2-digit",
  weekday: "short",
  timeZone: "Asia/Seoul",
});

export function DashboardRenewal() {
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null);
  const [jobs, setJobs] = useState<JobItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionLoading, setActionLoading] = useState<"pause" | "resume" | null>(null);
  const [actionMessage, setActionMessage] = useState("");
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [opsCheck, setOpsCheck] = useState<OpsCheckResponse | null>(null);
  const [opsLoading, setOpsLoading] = useState(false);

  const loadDashboard = useCallback(async () => {
    try {
      const [dashboardData, jobsData] = await Promise.all([
        fetchDashboard(),
        fetchJobs(1, 100),
      ]);
      setDashboard(dashboardData);
      setJobs(jobsData.items);
      setLastRefresh(new Date());
      setError("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "대시보드 데이터 로드 실패");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDashboard();
    const timer = window.setInterval(loadDashboard, 30_000);
    return () => window.clearInterval(timer);
  }, [loadDashboard]);

  const todayJobs = useMemo(() => {
    const todayKey = dashboard?.scheduler.today_date || "";
    return jobs
      .filter((job) => isTodayMarketJob(job, todayKey))
      .sort((left, right) => scheduledTime(left) - scheduledTime(right));
  }, [dashboard?.scheduler.today_date, jobs]);

  const jobsBySlot = useMemo(() => {
    const map = new Map<SlotKey, JobItem>();
    for (const job of todayJobs) {
      const slot = inferSlotKey(job);
      if (slot && !map.has(slot)) {
        map.set(slot, job);
      }
    }
    return map;
  }, [todayJobs]);

  const attentionJobs = useMemo(
    () => todayJobs.filter((job) => needsAttention(job)),
    [todayJobs],
  );

  const approvalCount = todayJobs.filter((job) => job.status === "awaiting_approval").length;
  const completedCount = todayJobs.filter((job) => job.status === "completed").length;
  const runningCount = todayJobs.filter((job) => job.status === "running").length;
  const readyCount = todayJobs.filter((job) => job.status === "ready_to_publish").length;
  const schedulerOperational = Boolean(
    dashboard?.scheduler.daemon_alive ||
    dashboard?.scheduler.scheduler_running ||
    todayJobs.some((job) => isOperationalJobStatus(job.status)),
  );
  const serviceOk = Boolean(
    schedulerOperational &&
    dashboard?.health.status === "ok" &&
    dashboard?.telegram.configured,
  );

  async function handlePause() {
    setActionLoading("pause");
    setActionMessage("");
    try {
      const response = await pauseScheduler();
      setActionMessage(response.message);
      await loadDashboard();
    } catch (exc) {
      setActionMessage(exc instanceof Error ? exc.message : "일시정지 실패");
    } finally {
      setActionLoading(null);
    }
  }

  async function handleResume() {
    setActionLoading("resume");
    setActionMessage("");
    try {
      const response = await resumeScheduler();
      setActionMessage(response.message);
      await loadDashboard();
    } catch (exc) {
      setActionMessage(exc instanceof Error ? exc.message : "재개 실패");
    } finally {
      setActionLoading(null);
    }
  }

  async function handleOpsCheck() {
    setOpsLoading(true);
    setActionMessage("");
    try {
      const response = await fetchOpsCheck(true);
      setOpsCheck(response);
      setActionMessage(
        response.ok
          ? "오늘 운영 점검이 완료되었습니다."
          : "운영 점검에서 확인할 항목이 있습니다.",
      );
    } catch (exc) {
      setActionMessage(exc instanceof Error ? exc.message : "운영 점검 실패");
    } finally {
      setOpsLoading(false);
    }
  }

  return (
    <div className="space-y-4">
      <header className="flex flex-col gap-3 border-b border-slate-200 pb-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-teal-700">
            Naver Blog Daily Ops
          </p>
          <h1 className="mt-1 text-2xl font-bold tracking-tight text-slate-950">
            오늘의 블로그 운영 콘솔
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            {lastRefresh
              ? `마지막 갱신 ${lastRefresh.toLocaleTimeString("ko-KR", {
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
              })}`
              : "데이터를 불러오는 중입니다"}
          </p>
        </div>

        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={handleOpsCheck}
            disabled={opsLoading}
            className="inline-flex h-9 items-center gap-2 rounded-lg border border-teal-200 bg-teal-50 px-3 text-sm font-semibold text-teal-800 shadow-sm transition hover:border-teal-400 disabled:opacity-50"
          >
            <ShieldCheck className="h-4 w-4" />
            {opsLoading ? "점검 중" : "운영 점검"}
          </button>
          <button
            type="button"
            onClick={loadDashboard}
            disabled={loading}
            className="inline-flex h-9 items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 text-sm font-medium text-slate-700 shadow-sm transition hover:border-slate-400 disabled:opacity-50"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            새로고침
          </button>
          {dashboard?.scheduler.paused ? (
            <button
              type="button"
              onClick={handleResume}
              disabled={Boolean(actionLoading)}
              className="inline-flex h-9 items-center gap-2 rounded-lg border border-teal-200 bg-teal-50 px-3 text-sm font-semibold text-teal-800 transition hover:border-teal-400 disabled:opacity-50"
            >
              <Play className="h-4 w-4" />
              운영 재개
            </button>
          ) : (
            <button
              type="button"
              onClick={handlePause}
              disabled={Boolean(actionLoading)}
              className="inline-flex h-9 items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 text-sm font-semibold text-amber-800 transition hover:border-amber-400 disabled:opacity-50"
            >
              <Pause className="h-4 w-4" />
              일시정지
            </button>
          )}
        </div>
      </header>

      {(error || actionMessage) && (
        <div
          className={`rounded-lg border px-4 py-3 text-sm ${error
            ? "border-rose-200 bg-rose-50 text-rose-700"
            : "border-slate-200 bg-white text-slate-600"
            }`}
        >
          {error || actionMessage}
        </div>
      )}

      <section className="grid gap-3 md:grid-cols-4">
        <SummaryTile
          icon={<ShieldCheck className="h-4 w-4" />}
          label="운영 상태"
          value={serviceOk ? "정상" : "확인 필요"}
          sub={dashboard?.scheduler.paused ? "일시정지 중" : "자동 운영 중"}
          tone={serviceOk ? "teal" : "amber"}
          loading={loading}
        />
        <SummaryTile
          icon={<FileText className="h-4 w-4" />}
          label="오늘 완료"
          value={`${completedCount}/3`}
          sub={`생성 중 ${runningCount}개`}
          tone="blue"
          loading={loading}
        />
        <SummaryTile
          icon={<Send className="h-4 w-4" />}
          label="승인/임시저장"
          value={`${approvalCount + readyCount}개`}
          sub={`승인 대기 ${approvalCount}개`}
          tone="slate"
          loading={loading}
        />
        <SummaryTile
          icon={<AlertTriangle className="h-4 w-4" />}
          label="점검 필요"
          value={`${attentionJobs.length}개`}
          sub={attentionJobs[0]?.error_code || "최근 오류 없음"}
          tone={attentionJobs.length > 0 ? "rose" : "teal"}
          loading={loading}
        />
      </section>

      <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="rounded-lg border border-slate-200 bg-white shadow-sm">
          <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
            <div>
              <h2 className="text-base font-semibold text-slate-900">오늘의 3개 글</h2>
              <p className="mt-0.5 text-xs text-slate-500">
                {dashboard?.scheduler.today_date || "오늘"} 기준 자동 생성 흐름
              </p>
            </div>
            <span className="rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1 text-xs font-medium text-slate-600">
              목표 {dashboard?.scheduler.daily_target ?? 3}개
            </span>
          </div>

          <div className="divide-y divide-slate-100">
            {MARKET_SLOTS.map((slot) => (
              <SlotRow
                key={slot.key}
                slot={slot}
                job={jobsBySlot.get(slot.key)}
                loading={loading}
              />
            ))}
          </div>
        </div>

        <aside className="space-y-4">
          <OpsCheckPanel opsCheck={opsCheck} loading={opsLoading} />
          <SystemPanel
            dashboard={dashboard}
            loading={loading}
            schedulerOperational={schedulerOperational}
          />
          <NextStepPanel dashboard={dashboard} todayJobs={todayJobs} />
        </aside>
      </section>

      <section className="rounded-lg border border-slate-200 bg-white shadow-sm">
        <div className="border-b border-slate-200 px-4 py-3">
          <h2 className="text-base font-semibold text-slate-900">최근 점검 기록</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            오늘 글 중 오류나 수정 신호가 있는 항목만 보여줍니다.
          </p>
        </div>
        {loading ? (
          <div className="p-4 text-sm text-slate-500">확인 중입니다...</div>
        ) : attentionJobs.length > 0 ? (
          <div className="divide-y divide-slate-100">
            {attentionJobs.map((job) => (
              <div key={job.job_id} className="px-4 py-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-sm font-medium text-slate-900">{job.title}</p>
                  <StatusPill status={job.status} />
                </div>
                <p className="mt-1 text-xs text-rose-700">
                  {job.error_code || "품질 점검에서 수정 필요로 표시됨"}
                </p>
                {job.error_message && (
                  <p className="mt-1 line-clamp-2 text-xs text-slate-500">{job.error_message}</p>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="p-4 text-sm text-slate-500">
            오늘 시장 글에서 표시할 오류가 없습니다.
          </div>
        )}
      </section>
    </div>
  );
}

function OpsCheckPanel({
  opsCheck,
  loading,
}: {
  opsCheck: OpsCheckResponse | null;
  loading: boolean;
}) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center gap-2">
        <ShieldCheck className="h-4 w-4 text-slate-500" />
        <h2 className="text-base font-semibold text-slate-900">오늘 운영 점검</h2>
      </div>
      {!opsCheck && !loading ? (
        <p className="mt-3 text-sm leading-6 text-slate-500">
          운영 점검 버튼을 누르면 API, 텔레그램, 네이버 세션, 월 비용을 한 번에 확인합니다.
        </p>
      ) : (
        <div className="mt-4 space-y-3">
          {loading ? (
            <p className="rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-500">점검 중입니다...</p>
          ) : (
            <>
              <div className="rounded-lg bg-slate-50 p-3 text-xs text-slate-600">
                월 예상 비용 {formatNumber(opsCheck?.monthly_cost_krw ?? 0)}원
                {" / "}
                기준 {formatNumber(opsCheck?.monthly_cost_warning_threshold_krw ?? 0)}원
              </div>
              {(opsCheck?.checks ?? []).map((item) => (
                <SystemLine
                  key={item.key}
                  label={item.label}
                  ok={item.ok}
                  loading={false}
                />
              ))}
              {(opsCheck?.warnings ?? []).length > 0 && (
                <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs leading-5 text-amber-800">
                  {opsCheck?.warnings.join("\n")}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function SummaryTile({
  icon,
  label,
  value,
  sub,
  tone,
  loading,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  sub: string;
  tone: "teal" | "blue" | "amber" | "rose" | "slate";
  loading: boolean;
}) {
  const toneClass = {
    teal: "bg-teal-50 text-teal-700 border-teal-100",
    blue: "bg-blue-50 text-blue-700 border-blue-100",
    amber: "bg-amber-50 text-amber-700 border-amber-100",
    rose: "bg-rose-50 text-rose-700 border-rose-100",
    slate: "bg-slate-50 text-slate-700 border-slate-100",
  }[tone];

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-start gap-3">
        <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border ${toneClass}`}>
          {icon}
        </div>
        <div className="min-w-0">
          <p className="text-xs font-medium text-slate-500">{label}</p>
          {loading ? (
            <div className="mt-2 h-5 w-16 animate-pulse rounded bg-slate-200" />
          ) : (
            <p className="mt-1 text-xl font-bold leading-none text-slate-950">{value}</p>
          )}
          {!loading && <p className="mt-1 truncate text-xs text-slate-500">{sub}</p>}
        </div>
      </div>
    </div>
  );
}

function SlotRow({
  slot,
  job,
  loading,
}: {
  slot: SlotDefinition;
  job?: JobItem;
  loading: boolean;
}) {
  return (
    <div className="grid gap-3 px-4 py-4 sm:grid-cols-[104px_minmax(0,1fr)_auto] sm:items-center">
      <div className="flex items-center gap-2">
        <Clock3 className="h-4 w-4 text-slate-400" />
        <div>
          <p className="text-sm font-semibold text-slate-900">{slot.label}</p>
          <p className="text-xs text-slate-500">{slot.timeLabel}</p>
        </div>
      </div>

      <div className="min-w-0">
        {loading ? (
          <div className="h-5 w-2/3 animate-pulse rounded bg-slate-200" />
        ) : job ? (
          <>
            <p className="truncate text-sm font-medium text-slate-900">{job.title}</p>
            <p className="mt-1 text-xs text-slate-500">
              예약 {formatLocalTime(job.scheduled_at)} · 갱신 {formatLocalDateTime(job.updated_at)}
            </p>
          </>
        ) : (
          <>
            <p className="text-sm font-medium text-slate-500">아직 생성된 작업이 없습니다</p>
            <p className="mt-1 text-xs text-slate-400">{slot.description}</p>
          </>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 sm:justify-end">
        {job ? <StatusPill status={job.status} /> : <EmptyPill />}
        {job?.result_url ? (
          <a
            href={job.result_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex h-8 items-center gap-1 rounded-lg border border-slate-200 px-2.5 text-xs font-medium text-slate-600 transition hover:border-slate-400"
          >
            확인
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
        ) : null}
      </div>
    </div>
  );
}

function SystemPanel({
  dashboard,
  loading,
  schedulerOperational,
}: {
  dashboard: DashboardResponse | null;
  loading: boolean;
  schedulerOperational: boolean;
}) {
  const schedulerOk = schedulerOperational;
  const apiOk = dashboard?.health.status === "ok";
  const telegramOk = Boolean(dashboard?.telegram.configured && dashboard.telegram.live_ok);

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center gap-2">
        <Server className="h-4 w-4 text-slate-500" />
        <h2 className="text-base font-semibold text-slate-900">운영 상태</h2>
      </div>
      <div className="mt-4 space-y-3">
        <SystemLine label="스케줄러" ok={schedulerOk} loading={loading} />
        <SystemLine label="API" ok={apiOk} loading={loading} />
        <SystemLine label="텔레그램" ok={telegramOk} loading={loading} />
      </div>
      <div className="mt-4 rounded-lg bg-slate-50 p-3 text-xs text-slate-600">
        다음 슬롯: {dashboard?.scheduler.next_publish_slot_kst || "계산 대기"}
      </div>
    </div>
  );
}

function NextStepPanel({
  dashboard,
  todayJobs,
}: {
  dashboard: DashboardResponse | null;
  todayJobs: JobItem[];
}) {
  const awaiting = todayJobs.find((job) => job.status === "awaiting_approval");
  const running = todayJobs.find((job) => job.status === "running");
  const ready = todayJobs.find((job) => job.status === "ready_to_publish");

  let title = "초안 도착 대기";
  let body = "자동 생성이 끝나면 텔레그램에서 승인 또는 수정본입력으로 처리합니다.";

  if (awaiting) {
    title = "텔레그램 승인 확인";
    body = "초안을 읽고 승인하거나 수정본입력으로 보정하면 됩니다.";
  } else if (ready) {
    title = "네이버 임시저장 대기";
    body = "승인된 글이 네이버 임시저장으로 넘어갈 차례입니다.";
  } else if (running) {
    title = "초안 생성 중";
    body = "데이터 수집, 글 생성, 이미지와 표 렌더링이 진행 중입니다.";
  } else if (dashboard?.scheduler.paused) {
    title = "운영 일시정지";
    body = "검토가 끝나면 운영 재개 버튼으로 스케줄러를 다시 열 수 있습니다.";
  }

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <h2 className="text-base font-semibold text-slate-900">다음 확인</h2>
      <p className="mt-3 text-sm font-medium text-slate-800">{title}</p>
      <p className="mt-1 text-sm leading-6 text-slate-500">{body}</p>
    </div>
  );
}

function SystemLine({
  label,
  ok,
  loading,
}: {
  label: string;
  ok: boolean;
  loading: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-sm text-slate-600">{label}</span>
      {loading ? (
        <span className="h-5 w-14 animate-pulse rounded bg-slate-200" />
      ) : ok ? (
        <span className="inline-flex items-center gap-1 rounded-lg border border-teal-200 bg-teal-50 px-2 py-0.5 text-xs font-semibold text-teal-700">
          <CheckCircle2 className="h-3.5 w-3.5" />
          정상
        </span>
      ) : (
        <span className="inline-flex items-center gap-1 rounded-lg border border-amber-200 bg-amber-50 px-2 py-0.5 text-xs font-semibold text-amber-700">
          <AlertTriangle className="h-3.5 w-3.5" />
          확인
        </span>
      )}
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  const meta = STATUS_META[status] || {
    label: status || "확인 필요",
    className: "border-slate-200 bg-slate-50 text-slate-600",
  };

  return (
    <span className={`inline-flex h-8 items-center rounded-lg border px-2.5 text-xs font-semibold ${meta.className}`}>
      {meta.label}
    </span>
  );
}

function EmptyPill() {
  return (
    <span className="inline-flex h-8 items-center rounded-lg border border-slate-200 bg-slate-50 px-2.5 text-xs font-medium text-slate-400">
      대기
    </span>
  );
}

function isTodayMarketJob(job: JobItem, todayKey: string) {
  const tags = job.tags || [];
  if (tags.includes("market_daily")) {
    if (!todayKey) return true;
    return tags.includes(`local_date:${todayKey}`) || job.title.includes(todayKey);
  }
  return Boolean(todayKey && job.title.includes(todayKey));
}

function needsAttention(job: JobItem) {
  if (job.status === "failed" || job.status === "failed_quality") return true;
  if (job.status === "retry_wait") return true;
  return false;
}

function isOperationalJobStatus(status: string) {
  return [
    "running",
    "awaiting_images",
    "awaiting_approval",
    "ready_to_publish",
    "completed",
    "retry_wait",
  ].includes(status);
}

function inferSlotKey(job: JobItem): SlotKey | null {
  const tags = job.tags || [];
  if (tags.includes("market_slot:kr_preopen")) return "kr";
  if (tags.includes("market_slot:us_preopen")) return "us";
  if (tags.some((tag) => tag.includes("evergreen") || tag.includes("weekly_reflection"))) {
    return "insight";
  }
  if (job.title.includes("국장")) return "kr";
  if (job.title.includes("미장")) return "us";
  if (job.title.includes("통찰")) return "insight";
  return null;
}

function scheduledTime(job: JobItem) {
  return new Date(job.scheduled_at).getTime() || 0;
}

function formatNumber(value: number) {
  return Number(value || 0).toLocaleString("ko-KR");
}

function formatLocalTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return LOCAL_TIME_FORMAT.format(date);
}

function formatLocalDateTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return `${LOCAL_DATE_FORMAT.format(date)} ${LOCAL_TIME_FORMAT.format(date)}`;
}
