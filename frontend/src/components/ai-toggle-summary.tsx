"use client";

import { useEffect, useMemo, useState } from "react";

import { fetchAIToggleReport, type AIToggleReportResponse } from "@/lib/api";

export function AIToggleSummary() {
  const [data, setData] = useState<AIToggleReportResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let isMounted = true;

    async function loadReport() {
      try {
        const response = await fetchAIToggleReport();
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
            : "AI 토글 리포트를 불러오지 못했습니다.";
        setError(message);
      } finally {
        if (isMounted) {
          setLoading(false);
        }
      }
    }

    loadReport();
    return () => {
      isMounted = false;
    };
  }, []);

  const statusClass = useMemo(() => {
    if (!data || !data.available) {
      return "border-slate-300 bg-slate-50 text-slate-700";
    }
    const failed = Number(data.postverify?.failed || 0);
    if (failed > 0) {
      return "border-rose-300 bg-rose-50 text-rose-800";
    }
    return "border-emerald-300 bg-emerald-50 text-emerald-800";
  }, [data]);

  return (
    <section className="space-y-3 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <header className="flex items-center justify-between gap-2">
        <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
          AI 토글 검증
        </h2>
        <span className={`rounded-full border px-3 py-1 text-xs font-semibold ${statusClass}`}>
          {!data || !data.available
            ? "NO DATA"
            : Number(data.postverify?.failed || 0) > 0
              ? "FAILED"
              : "PASS"}
        </span>
      </header>

      {loading && (
        <p className="rounded-xl bg-slate-50 px-3 py-2 text-sm text-slate-500">
          최근 AI 토글 리포트를 불러오는 중입니다...
        </p>
      )}

      {error && (
        <p className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </p>
      )}

      {!loading && !error && data && !data.available && (
        <p className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600">
          아직 저장된 AI 토글 검증 리포트가 없습니다.
        </p>
      )}

      {!loading && !error && data && data.available && (
        <div className="grid gap-3 sm:grid-cols-2">
          <article className="rounded-xl border border-slate-200 bg-slate-50 p-3">
            <p className="text-xs text-slate-500">사전 점검</p>
            <p className="mt-1 text-sm text-slate-700">
              expected {data.prepublish.expected_on} / verified {data.prepublish.verified_on} / failed{" "}
              {data.prepublish.failed}
            </p>
          </article>
          <article className="rounded-xl border border-slate-200 bg-slate-50 p-3">
            <p className="text-xs text-slate-500">사후 점검</p>
            <p className="mt-1 text-sm text-slate-700">
              expected {data.postverify.expected_on} / passed {data.postverify.passed} / failed{" "}
              {data.postverify.failed}
            </p>
          </article>
          <article className="rounded-xl border border-slate-200 bg-slate-50 p-3 sm:col-span-2">
            <p className="text-xs text-slate-500">최근 연속 실패</p>
            <p className="mt-1 text-sm text-slate-700">
              {data.recent_failure_streak}회
              {data.created_at_iso ? ` · 마지막 갱신 ${new Date(data.created_at_iso).toLocaleString("ko-KR")}` : ""}
            </p>
          </article>
          {data.unresolved_images.length > 0 && (
            <article className="rounded-xl border border-rose-200 bg-rose-50 p-3 sm:col-span-2">
              <p className="text-xs font-semibold text-rose-700">미해결 이미지</p>
              <ul className="mt-2 space-y-1 text-xs text-rose-700">
                {data.unresolved_images.slice(0, 3).map((path) => (
                  <li key={path}>- {path}</li>
                ))}
              </ul>
            </article>
          )}
        </div>
      )}
    </section>
  );
}
