"use client";

import { useEffect, useState } from "react";

import { fetchJobs, fetchJobDetail, type JobsResponse, type JobDetailResponse } from "@/lib/api";

const STATUS_STYLE: Record<string, string> = {
  queued: "bg-violet-100 text-violet-800 border-violet-300",
  publishing: "bg-blue-100 text-blue-800 border-blue-300",
  running: "bg-blue-100 text-blue-800 border-blue-300",
  ready_to_publish: "bg-orange-100 text-orange-800 border-orange-300",
  completed: "bg-emerald-100 text-emerald-800 border-emerald-300",
  retry_wait: "bg-amber-100 text-amber-800 border-amber-300",
  failed: "bg-rose-100 text-rose-800 border-rose-300",
};

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

type JobsTableProps = {
  initialPage?: number;
  size?: number;
  reloadToken?: number;
};

export function JobsTable({ initialPage = 1, size = 20, reloadToken = 0 }: JobsTableProps) {
  const [page, setPage] = useState(initialPage);
  const [data, setData] = useState<JobsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [jobDetail, setJobDetail] = useState<JobDetailResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");

  useEffect(() => {
    setPage(1);
  }, [reloadToken]);

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
    return () => { isMounted = false; };
  }, [selectedJobId]);

  useEffect(() => {
    let isMounted = true;

    async function loadJobs() {
      setLoading(true);
      setError("");
      try {
        const response = await fetchJobs(page, size);
        if (!isMounted) {
          return;
        }
        setData(response);
      } catch (requestError) {
        if (!isMounted) {
          return;
        }
        const message =
          requestError instanceof Error
            ? requestError.message
            : "작업 목록을 불러오지 못했습니다.";
        setError(message);
      } finally {
        if (isMounted) {
          setLoading(false);
        }
      }
    }

    loadJobs();
    const timer = setInterval(loadJobs, 30_000);
    return () => {
      isMounted = false;
      clearInterval(timer);
    };
  }, [page, size, reloadToken]);

  const pages = data?.pages ?? 1;
  const canPrev = page > 1;
  const canNext = page < pages;

  return (
    <section className="space-y-3 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
          예약/발행 작업 목록
        </h2>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => canPrev && setPage((value) => value - 1)}
            disabled={!canPrev}
            className="rounded-full border border-slate-300 px-3 py-1 text-sm disabled:opacity-40"
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
            className="rounded-full border border-slate-300 px-3 py-1 text-sm disabled:opacity-40"
          >
            다음
          </button>
        </div>
      </header>

      {data && (
        <div className="flex flex-wrap gap-2 text-xs text-slate-600">
          {Object.entries(data.queue_stats).map(([key, value]) => (
            <span key={key} className="rounded-full border border-slate-200 bg-slate-50 px-2 py-1">
              {key}: {value}
            </span>
          ))}
        </div>
      )}

      {loading && (
        <p className="rounded-xl bg-slate-50 px-3 py-2 text-sm text-slate-500">
          작업 목록을 불러오는 중입니다...
        </p>
      )}

      {error && (
        <p className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </p>
      )}

      <div className="overflow-x-auto rounded-xl border border-slate-200">
        <table className="min-w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-600">
            <tr>
              <th className="px-3 py-2">Title</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Topic / Persona</th>
              <th className="px-3 py-2">Keywords</th>
              <th className="px-3 py-2">Scheduled</th>
            </tr>
          </thead>
          <tbody>
            {(data?.items ?? []).map((job) => {
              const statusKey = job.status.toLowerCase();
              const statusClass =
                STATUS_STYLE[statusKey] || "bg-slate-100 text-slate-700 border-slate-300";
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
                    <p className="text-xs text-slate-500">{job.job_id}</p>
                  </td>
                  <td className="px-3 py-3">
                    <span className={`rounded-full border px-2 py-1 text-xs ${statusClass}`}>
                      {job.status}
                    </span>
                  </td>
                  <td className="px-3 py-3">
                    <p className="text-slate-700">{job.category || "—"}</p>
                    <p className="text-xs text-slate-400">{job.persona_id}</p>
                  </td>
                  <td className="px-3 py-3">{job.seed_keywords.join(", ")}</td>
                  <td className="px-3 py-3">{formatDate(job.scheduled_at)}</td>
                </tr>
              );
            })}
            {data && data.items.length === 0 && (
              <tr>
                <td colSpan={5} className="px-3 py-8 text-center text-sm text-slate-500">
                  현재 표시할 작업이 없습니다.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {selectedJobId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4 backdrop-blur-sm">
          <div className="flex max-h-[90vh] w-full max-w-2xl flex-col rounded-2xl bg-white shadow-xl">
            <header className="flex items-center justify-between border-b border-slate-200 p-4">
              <h3 className="font-semibold text-slate-900">Job DetailViewer</h3>
              <button
                type="button"
                className="rounded p-1 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
                onClick={() => setSelectedJobId(null)}
              >
                ✕
              </button>
            </header>
            <div className="flex-1 overflow-y-auto p-4">
              {detailLoading && <p className="text-sm text-slate-500">불러오는 중...</p>}
              {detailError && <p className="text-sm text-rose-600">{detailError}</p>}
              {jobDetail && !detailLoading && !detailError && (
                <div className="space-y-4 text-sm">
                  <div>
                    <span className="font-semibold text-slate-800">Job ID:</span>{" "}
                    <span className="font-mono text-xs text-slate-500">{jobDetail.job_id}</span>
                  </div>
                  <div>
                    <span className="font-semibold text-slate-800">Title:</span>{" "}
                    <span className="text-slate-600">{jobDetail.title}</span>
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <span className="font-semibold text-slate-800">Status:</span>{" "}
                      <span className="text-slate-600">{jobDetail.status}</span>
                    </div>
                    <div>
                      <span className="font-semibold text-slate-800">Platform:</span>{" "}
                      <span className="text-slate-600">{jobDetail.platform}</span>
                    </div>
                    <div>
                      <span className="font-semibold text-slate-800">Persona:</span>{" "}
                      <span className="text-slate-600">{jobDetail.persona_id || "—"}</span>
                    </div>
                    <div>
                      <span className="font-semibold text-slate-800">Topic:</span>{" "}
                      <span className="text-slate-600">{jobDetail.topic_mode || "—"}</span>
                    </div>
                    <div className="col-span-2">
                      <span className="font-semibold text-slate-800">Category:</span>{" "}
                      <span className="text-slate-600">{jobDetail.category || "—"}</span>
                    </div>
                  </div>
                  {jobDetail.error_message && (
                    <div className="rounded-xl border border-rose-200 bg-rose-50 p-3">
                      <h4 className="mb-1 font-semibold text-rose-700">오류 메시지</h4>
                      <p className="text-xs text-rose-600 whitespace-pre-wrap">{jobDetail.error_message}</p>
                    </div>
                  )}
                  <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
                    <h4 className="mb-2 font-semibold text-slate-800">Final Content</h4>
                    {jobDetail.final_content ? (
                      <div className="whitespace-pre-wrap text-slate-700 font-mono text-xs">
                        {jobDetail.final_content.replace(/(!\[.*\]\(.*?\))/g, "[📷 이미지 삽입점]")}
                      </div>
                    ) : (
                      <p className="text-slate-500 italic">내용이 생성되지 않았습니다.</p>
                    )}
                  </div>
                </div>
              )}
            </div>
            <footer className="border-t border-slate-200 p-4 text-right">
              <button
                type="button"
                onClick={() => setSelectedJobId(null)}
                className="rounded-full bg-slate-200 px-4 py-2 text-sm font-medium text-slate-800 transition hover:bg-slate-300"
              >
                닫기
              </button>
            </footer>
          </div>
        </div>
      )}
    </section>
  );
}
