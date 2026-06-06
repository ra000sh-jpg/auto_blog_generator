"use client";

import { useEffect, useMemo, useState } from "react";

import {
  cancelJob,
  fetchJobDetail,
  fetchJobs,
  fetchPostBackups,
  fetchRevisionArchives,
  type JobDetailResponse,
  type JobsResponse,
  type PostArchiveItem,
} from "@/lib/api";
import { JOB_DETAIL_LABEL, JOB_TABLE_HEADER, QUEUE_STAT_LABEL, STATUS_LABEL } from "@/lib/labels";

type JobItem = JobsResponse["items"][number];

const STATUS_STYLE: Record<string, string> = {
  queued: "bg-slate-100 text-slate-700 border-slate-300",
  publishing: "bg-blue-100 text-blue-800 border-blue-300",
  running: "bg-blue-100 text-blue-800 border-blue-300",
  awaiting_images: "bg-cyan-100 text-cyan-800 border-cyan-300",
  awaiting_approval: "bg-teal-100 text-teal-800 border-teal-300",
  ready_to_publish: "bg-indigo-100 text-indigo-800 border-indigo-300",
  completed: "bg-emerald-100 text-emerald-800 border-emerald-300",
  retry_wait: "bg-amber-100 text-amber-800 border-amber-300",
  failed: "bg-rose-100 text-rose-800 border-rose-300",
  failed_quality: "bg-rose-100 text-rose-800 border-rose-300",
  cancelled: "bg-slate-200 text-slate-700 border-slate-300",
};

const CANCELLABLE_STATUSES = new Set(["queued", "retry_wait", "ready_to_publish"]);

const FILTERS = [
  {
    key: "attention",
    label: "확인 필요",
    status: "awaiting_approval,ready_to_publish,failed,failed_quality,retry_wait",
  },
  { key: "approval", label: "승인 대기", status: "awaiting_approval" },
  { key: "ready", label: "임시저장 대기", status: "ready_to_publish" },
  { key: "failed", label: "실패/수정", status: "failed,failed_quality,retry_wait" },
  { key: "completed", label: "완료", status: "completed" },
  { key: "all", label: "전체", status: "" },
] as const;

function formatDate(raw: string): string {
  if (!raw) {
    return "-";
  }
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) {
    return raw;
  }
  return date.toLocaleString("ko-KR");
}

function statusLabel(status: string): string {
  return STATUS_LABEL[status.toLowerCase()] ?? status;
}

function statusClass(status: string): string {
  return STATUS_STYLE[status.toLowerCase()] || "bg-slate-100 text-slate-700 border-slate-300";
}

function toNumber(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function getQueueCount(stats: Record<string, number>, statuses: string): number {
  if (!statuses) {
    return Object.entries(stats)
      .filter(([key]) => !["ready_master", "ready_sub", "queued_master", "queued_sub"].includes(key))
      .reduce((total, [, value]) => total + Number(value || 0), 0);
  }
  return statuses
    .split(",")
    .map((status) => status.trim())
    .filter(Boolean)
    .reduce((total, status) => total + Number(stats[status] || 0), 0);
}

type JobsTableProps = {
  initialPage?: number;
  size?: number;
  reloadToken?: number;
};

export function JobsTable({ initialPage = 1, size = 20, reloadToken = 0 }: JobsTableProps) {
  const [page, setPage] = useState(initialPage);
  const [filterKey, setFilterKey] = useState<(typeof FILTERS)[number]["key"]>("attention");
  const [data, setData] = useState<JobsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [jobDetail, setJobDetail] = useState<JobDetailResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [cancelTargetId, setCancelTargetId] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState("");
  const [actionError, setActionError] = useState("");

  const [revisionArchives, setRevisionArchives] = useState<PostArchiveItem[]>([]);
  const [backupArchives, setBackupArchives] = useState<PostArchiveItem[]>([]);
  const [archiveLoading, setArchiveLoading] = useState(true);

  const activeFilter = useMemo(
    () => FILTERS.find((item) => item.key === filterKey) ?? FILTERS[0],
    [filterKey],
  );

  useEffect(() => {
    setPage(1);
  }, [reloadToken, filterKey]);

  useEffect(() => {
    if (!selectedJobId) {
      setJobDetail(null);
      setDetailError("");
      return;
    }

    let isMounted = true;
    async function loadDetail() {
      setDetailLoading(true);
      setDetailError("");
      try {
        const res = await fetchJobDetail(selectedJobId as string);
        if (isMounted) setJobDetail(res);
      } catch (err) {
        if (isMounted) {
          setDetailError(err instanceof Error ? err.message : "상세 정보를 불러오지 못했습니다.");
        }
      } finally {
        if (isMounted) setDetailLoading(false);
      }
    }
    loadDetail();
    return () => {
      isMounted = false;
    };
  }, [selectedJobId]);

  useEffect(() => {
    let isMounted = true;

    async function loadJobs() {
      setLoading(true);
      setError("");
      try {
        const response = await fetchJobs(page, size, activeFilter.status);
        if (isMounted) {
          setData(response);
        }
      } catch (requestError) {
        if (isMounted) {
          setError(requestError instanceof Error ? requestError.message : "작업 목록을 불러오지 못했습니다.");
        }
      } finally {
        if (isMounted) {
          setLoading(false);
        }
      }
    }

    loadJobs();
    const timer = window.setInterval(loadJobs, 30_000);
    return () => {
      isMounted = false;
      window.clearInterval(timer);
    };
  }, [activeFilter.status, page, size, reloadToken]);

  useEffect(() => {
    let isMounted = true;

    async function loadArchives() {
      setArchiveLoading(true);
      try {
        const [revisionResult, backupResult] = await Promise.all([
          fetchRevisionArchives(5),
          fetchPostBackups(8),
        ]);
        if (isMounted) {
          setRevisionArchives(revisionResult.items);
          setBackupArchives(backupResult.items);
        }
      } catch {
        if (isMounted) {
          setRevisionArchives([]);
          setBackupArchives([]);
        }
      } finally {
        if (isMounted) {
          setArchiveLoading(false);
        }
      }
    }

    loadArchives();
    return () => {
      isMounted = false;
    };
  }, [reloadToken]);

  const pages = data?.pages ?? 1;
  const canPrev = page > 1;
  const canNext = page < pages;

  const vlmVisual = useMemo(() => {
    if (!jobDetail?.quality_snapshot || typeof jobDetail.quality_snapshot !== "object") {
      return null;
    }
    const entry = jobDetail.quality_snapshot.vlm_visual;
    if (!entry || typeof entry !== "object") {
      return null;
    }
    return entry as Record<string, unknown>;
  }, [jobDetail]);

  async function reloadCurrentList() {
    const listResponse = await fetchJobs(page, size, activeFilter.status);
    setData(listResponse);
  }

  async function handleCancelJob(job: JobItem) {
    if (cancelTargetId) {
      return;
    }

    setActionError("");
    setActionMessage("");
    const confirmed = window.confirm(
      `"${job.title}" 작업을 취소할까요?\n상태가 cancelled로 변경됩니다.`,
    );
    if (!confirmed) {
      return;
    }

    setCancelTargetId(job.job_id);
    try {
      const result = await cancelJob(job.job_id);
      setActionMessage(`${result.message} (아이디어 락 해제 ${result.released_idea_locks}건)`);
      await reloadCurrentList();

      if (selectedJobId === job.job_id) {
        const detailResponse = await fetchJobDetail(job.job_id);
        setJobDetail(detailResponse);
        setDetailError("");
      }
    } catch (requestError) {
      setActionError(requestError instanceof Error ? requestError.message : "작업 취소 요청에 실패했습니다.");
    } finally {
      setCancelTargetId(null);
    }
  }

  return (
    <section className="space-y-4">
      <div className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <header className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-slate-950">운영 작업</h2>
            <p className="mt-1 text-sm text-slate-500">
              기본 화면은 승인과 오류처럼 손이 필요한 작업만 모아 보여줍니다.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => canPrev && setPage((value) => value - 1)}
              disabled={!canPrev}
              className="rounded-lg border border-slate-300 px-3 py-1 text-sm disabled:opacity-40"
            >
              이전
            </button>
            <span className="text-sm text-slate-600">
              {page} / {pages}
            </span>
            <button
              type="button"
              onClick={() => canNext && setPage((value) => value + 1)}
              disabled={!canNext}
              className="rounded-lg border border-slate-300 px-3 py-1 text-sm disabled:opacity-40"
            >
              다음
            </button>
          </div>
        </header>

        <div className="mt-4 flex flex-wrap gap-2">
          {FILTERS.map((filter) => {
            const count = getQueueCount(data?.queue_stats ?? {}, filter.status);
            const active = filter.key === filterKey;
            return (
              <button
                key={filter.key}
                type="button"
                onClick={() => setFilterKey(filter.key)}
                className={`rounded-lg border px-3 py-1.5 text-sm font-medium transition ${active
                  ? "border-slate-900 bg-slate-900 text-white"
                  : "border-slate-200 bg-white text-slate-600 hover:border-slate-400"
                  }`}
              >
                {filter.label}
                <span className={active ? "ml-2 text-slate-200" : "ml-2 text-slate-400"}>
                  {count}
                </span>
              </button>
            );
          })}
        </div>

        {data && (
          <details className="mt-3 rounded-lg border border-slate-200 bg-slate-50">
            <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-slate-600">
              전체 큐 상태 보기
            </summary>
            <div className="flex flex-wrap gap-2 border-t border-slate-200 p-3 text-xs text-slate-600">
              {Object.entries(data.queue_stats).map(([key, value]) => (
                <span key={key} className="rounded-lg border border-slate-200 bg-white px-2 py-1">
                  {QUEUE_STAT_LABEL[key] ?? key}: {value}
                </span>
              ))}
            </div>
          </details>
        )}

        {loading && (
          <p className="mt-3 rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-500">
            작업 목록을 불러오는 중입니다...
          </p>
        )}

        {error && (
          <p className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {error}
          </p>
        )}
        {actionMessage && (
          <p className="mt-3 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700">
            {actionMessage}
          </p>
        )}
        {actionError && (
          <p className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {actionError}
          </p>
        )}

        <div className="mt-4 overflow-x-auto rounded-lg border border-slate-200">
          <table className="min-w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-600">
              <tr>
                <th className="px-3 py-2">{JOB_TABLE_HEADER.title}</th>
                <th className="px-3 py-2">{JOB_TABLE_HEADER.status}</th>
                <th className="px-3 py-2">예약/갱신</th>
                <th className="px-3 py-2">{JOB_TABLE_HEADER.action}</th>
              </tr>
            </thead>
            <tbody>
              {(data?.items ?? []).map((job) => {
                const statusKey = job.status.toLowerCase();
                const isCancellable = CANCELLABLE_STATUSES.has(statusKey);
                const isCancelling = cancelTargetId === job.job_id;
                return (
                  <tr key={job.job_id} className="border-t border-slate-200">
                    <td className="px-3 py-3">
                      <button
                        type="button"
                        onClick={() => setSelectedJobId(job.job_id)}
                        className="text-left font-medium text-slate-900 transition hover:text-blue-600 hover:underline"
                      >
                        {job.title}
                      </button>
                      <p className="mt-1 text-xs text-slate-500">
                        {job.category || "카테고리 없음"} · {job.persona_id}
                      </p>
                    </td>
                    <td className="px-3 py-3">
                      <span className={`rounded-lg border px-2 py-1 text-xs ${statusClass(statusKey)}`}>
                        {statusLabel(statusKey)}
                      </span>
                    </td>
                    <td className="px-3 py-3">
                      <p className="text-slate-700">{formatDate(job.scheduled_at)}</p>
                      <p className="text-xs text-slate-400">갱신 {formatDate(job.updated_at)}</p>
                    </td>
                    <td className="px-3 py-3">
                      <div className="flex flex-wrap gap-2">
                        <button
                          type="button"
                          onClick={() => setSelectedJobId(job.job_id)}
                          className="rounded-lg border border-slate-300 px-3 py-1 text-xs text-slate-700 transition hover:border-slate-500"
                        >
                          상세
                        </button>
                        {job.result_url && (
                          <a
                            href={job.result_url}
                            target="_blank"
                            rel="noreferrer"
                            className="rounded-lg border border-teal-300 bg-teal-50 px-3 py-1 text-xs text-teal-700 transition hover:bg-teal-100"
                          >
                            확인 링크
                          </a>
                        )}
                        {isCancellable && (
                          <button
                            type="button"
                            disabled={isCancelling}
                            onClick={() => handleCancelJob(job)}
                            className="rounded-lg border border-rose-300 bg-rose-50 px-3 py-1 text-xs text-rose-700 transition hover:bg-rose-100 disabled:cursor-not-allowed disabled:opacity-60"
                          >
                            {isCancelling ? "취소 중..." : "대기 취소"}
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
              {data && data.items.length === 0 && (
                <tr>
                  <td colSpan={4} className="px-3 py-8 text-center text-sm text-slate-500">
                    현재 표시할 작업이 없습니다.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <section className="grid gap-4 lg:grid-cols-2">
        <ArchivePanel
          title="최근 수정본입력 반영"
          body="스마트폰에서 고쳐 보낸 초안이 반영된 기록입니다."
          items={revisionArchives}
          loading={archiveLoading}
          emptyText="아직 수정본입력 반영 기록이 없습니다."
        />
        <ArchivePanel
          title="글 백업 인덱스"
          body="최종 텍스트, 이미지와 표 개수를 가볍게 보존한 목록입니다."
          items={backupArchives}
          loading={archiveLoading}
          emptyText="아직 보존된 글이 없습니다."
        />
      </section>

      {selectedJobId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4 backdrop-blur-sm">
          <div className="flex max-h-[90vh] w-full max-w-2xl flex-col rounded-lg bg-white shadow-xl">
            <header className="flex items-center justify-between border-b border-slate-200 p-4">
              <h3 className="font-semibold text-slate-900">작업 상세</h3>
              <button
                type="button"
                className="rounded-lg border border-slate-200 px-3 py-1 text-sm text-slate-600 transition hover:border-slate-400"
                onClick={() => setSelectedJobId(null)}
              >
                닫기
              </button>
            </header>
            <div className="flex-1 overflow-y-auto p-4">
              {detailLoading && <p className="text-sm text-slate-500">불러오는 중...</p>}
              {detailError && <p className="text-sm text-rose-600">{detailError}</p>}
              {jobDetail && !detailLoading && !detailError && (
                <div className="space-y-4 text-sm">
                  <div>
                    <span className="font-semibold text-slate-800">{JOB_DETAIL_LABEL.jobId}:</span>{" "}
                    <span className="font-mono text-xs text-slate-500">{jobDetail.job_id}</span>
                  </div>
                  <div>
                    <span className="font-semibold text-slate-800">{JOB_DETAIL_LABEL.title}:</span>{" "}
                    <span className="text-slate-600">{jobDetail.title}</span>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <DetailLine label={JOB_DETAIL_LABEL.status} value={statusLabel(jobDetail.status)} />
                    <DetailLine label={JOB_DETAIL_LABEL.platform} value={jobDetail.platform} />
                    <DetailLine label={JOB_DETAIL_LABEL.persona} value={jobDetail.persona_id || "-"} />
                    <DetailLine label={JOB_DETAIL_LABEL.topic} value={jobDetail.topic_mode || "-"} />
                    <DetailLine label={JOB_DETAIL_LABEL.category} value={jobDetail.category || "-"} wide />
                  </div>
                  {jobDetail.error_message && (
                    <div className="rounded-lg border border-rose-200 bg-rose-50 p-3">
                      <h4 className="mb-1 font-semibold text-rose-700">오류 메시지</h4>
                      <p className="whitespace-pre-wrap text-xs text-rose-600">{jobDetail.error_message}</p>
                    </div>
                  )}
                  <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                    <h4 className="mb-2 font-semibold text-slate-800">{JOB_DETAIL_LABEL.finalContent}</h4>
                    {jobDetail.final_content ? (
                      <div className="whitespace-pre-wrap font-mono text-xs leading-6 text-slate-700">
                        {jobDetail.final_content.replace(/(!\[.*\]\(.*?\))/g, "[이미지 삽입점]")}
                      </div>
                    ) : (
                      <p className="italic text-slate-500">내용이 생성되지 않았습니다.</p>
                    )}
                  </div>
                  {vlmVisual && (
                    <details className="rounded-lg border border-indigo-200 bg-indigo-50">
                      <summary className="cursor-pointer px-3 py-2 text-sm font-semibold text-indigo-800">
                        시각 품질 평가 {toNumber(vlmVisual.total_score)}/100
                      </summary>
                      <div className="border-t border-indigo-100 p-3 text-xs text-indigo-700">
                        레이아웃 {toNumber(vlmVisual.layout)}/20 · 가독성 {toNumber(vlmVisual.readability)}/25 · 이미지 {toNumber(vlmVisual.image_quality)}/20
                        <br />
                        일관성 {toNumber(vlmVisual.visual_consistency)}/15 · 인상 {toNumber(vlmVisual.overall_impression)}/20
                        {Array.isArray(vlmVisual.suggestions) && vlmVisual.suggestions.length > 0 && (
                          <p className="mt-1">{String(vlmVisual.suggestions[0] || "")}</p>
                        )}
                      </div>
                    </details>
                  )}
                  <details className="rounded-lg border border-slate-200 bg-white">
                    <summary className="cursor-pointer px-3 py-2 text-sm font-medium text-slate-700">
                      진단 JSON 보기
                    </summary>
                    <pre className="max-h-72 overflow-auto border-t border-slate-100 p-3 text-xs text-slate-600">
                      {JSON.stringify(
                        {
                          quality_snapshot: jobDetail.quality_snapshot,
                          seo_snapshot: jobDetail.seo_snapshot,
                        },
                        null,
                        2,
                      )}
                    </pre>
                  </details>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function DetailLine({
  label,
  value,
  wide = false,
}: {
  label: string;
  value: string;
  wide?: boolean;
}) {
  return (
    <div className={wide ? "sm:col-span-2" : ""}>
      <span className="font-semibold text-slate-800">{label}:</span>{" "}
      <span className="text-slate-600">{value}</span>
    </div>
  );
}

function ArchivePanel({
  title,
  body,
  items,
  loading,
  emptyText,
}: {
  title: string;
  body: string;
  items: PostArchiveItem[];
  loading: boolean;
  emptyText: string;
}) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <h2 className="text-base font-semibold text-slate-950">{title}</h2>
      <p className="mt-1 text-xs leading-5 text-slate-500">{body}</p>
      <div className="mt-4 space-y-3">
        {loading ? (
          <p className="rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-500">기록을 불러오는 중입니다...</p>
        ) : items.length > 0 ? (
          items.map((item) => <ArchiveItemRow key={`${title}-${item.job_id}`} item={item} />)
        ) : (
          <p className="rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-500">{emptyText}</p>
        )}
      </div>
    </div>
  );
}

function ArchiveItemRow({ item }: { item: PostArchiveItem }) {
  return (
    <article className="rounded-lg border border-slate-200 bg-slate-50 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="line-clamp-2 text-sm font-semibold text-slate-900">{item.title}</p>
          <p className="mt-1 text-xs text-slate-500">
            {formatDate(item.updated_at)} · {item.slot || item.category || "일반"} · {item.content_length.toLocaleString("ko-KR")}자
          </p>
        </div>
        <span className="rounded-lg border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600">
          이미지 {item.image_count} / 표 {item.table_count}
        </span>
      </div>
      <details className="mt-2">
        <summary className="cursor-pointer text-xs font-medium text-slate-600">미리보기</summary>
        <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-slate-600">
          {item.final_content_preview || "미리보기 없음"}
        </p>
      </details>
    </article>
  );
}
