"use client";

import { useEffect, useState } from "react";

import { fetchJobs, type JobsResponse } from "@/lib/api";

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

  useEffect(() => {
    setPage(1);
  }, [reloadToken]);

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
    return () => {
      isMounted = false;
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
              <th className="px-3 py-2">Platform</th>
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
                    <p className="font-medium text-slate-900">{job.title}</p>
                    <p className="text-xs text-slate-500">{job.job_id}</p>
                  </td>
                  <td className="px-3 py-3">
                    <span className={`rounded-full border px-2 py-1 text-xs ${statusClass}`}>
                      {job.status}
                    </span>
                  </td>
                  <td className="px-3 py-3">{job.platform}</td>
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
    </section>
  );
}
