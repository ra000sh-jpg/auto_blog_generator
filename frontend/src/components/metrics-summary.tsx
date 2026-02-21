"use client";

import { useEffect, useState } from "react";

import { fetchMetrics, fetchLLMMetrics, type MetricsResponse, type LLMMetricsResponse } from "@/lib/api";

type SummaryCard = {
  label: string;
  value: string;
  helper: string;
};

export function MetricsSummary() {
  const [data, setData] = useState<MetricsResponse | null>(null);
  const [llmData, setLlmData] = useState<LLMMetricsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let isMounted = true;

    async function loadMetrics() {
      try {
        const [metricsResponse, llmResponse] = await Promise.allSettled([
          fetchMetrics(),
          fetchLLMMetrics(24),
        ]);
        if (!isMounted) return;
        if (metricsResponse.status === "fulfilled") setData(metricsResponse.value);
        if (llmResponse.status === "fulfilled") setLlmData(llmResponse.value);
        if (metricsResponse.status === "rejected") {
          setError(
            metricsResponse.reason instanceof Error
              ? metricsResponse.reason.message
              : "성과 데이터를 불러오지 못했습니다."
          );
        }
      } finally {
        if (isMounted) setLoading(false);
      }
    }

    loadMetrics();
    return () => {
      isMounted = false;
    };
  }, []);

  const summary = data?.summary;
  const cards: SummaryCard[] = [
    {
      label: "총 포스트",
      value: String(summary?.total_posts ?? 0),
      helper: "집계 대상 수",
    },
    {
      label: "총 조회수",
      value: String(summary?.total_views ?? 0),
      helper: "누적 트래픽",
    },
    {
      label: "총 좋아요",
      value: String(summary?.total_likes ?? 0),
      helper: "독자 반응",
    },
    {
      label: "평균 조회수",
      value: (summary?.avg_views ?? 0).toFixed(1),
      helper: "포스트당 평균",
    },
  ];

  return (
    <section className="space-y-3 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm @container">
      <header className="flex items-center justify-between gap-2">
        <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
          성과 요약
        </h2>
        {data && <span className="text-xs text-slate-500">최근 {data.total}건 기준</span>}
      </header>

      {loading && (
        <p className="rounded-xl bg-slate-50 px-3 py-2 text-sm text-slate-500">
          성과 데이터를 불러오는 중입니다...
        </p>
      )}

      {error && (
        <p className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </p>
      )}

      <div className="grid gap-3 @md:grid-cols-2 xl:grid-cols-4">
        {cards.map((card) => (
          <article
            key={card.label}
            className="rounded-xl border border-slate-200 bg-[linear-gradient(135deg,_#ffffff_0%,_#f0fdf4_100%)] p-4"
          >
            <p className="text-sm text-slate-600">{card.label}</p>
            <p className="mt-1 text-2xl font-semibold tracking-tight">{card.value}</p>
            <p className="mt-1 text-xs text-slate-500">{card.helper}</p>
          </article>
        ))}
      </div>

      {llmData && llmData.total_llm_calls > 0 && (
        <div className="mt-4 space-y-2">
          <h3 className="text-sm font-medium text-slate-600">
            LLM 호출 현황 (최근 24시간 · 총 {llmData.total_llm_calls}회)
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-xs text-left text-slate-600">
              <thead>
                <tr className="border-b border-slate-200 text-slate-500">
                  <th className="pb-1 pr-4">유형</th>
                  <th className="pb-1 pr-4 text-right">호출</th>
                  <th className="pb-1 pr-4 text-right">오류율</th>
                  <th className="pb-1 pr-4 text-right">평균 입력 토큰</th>
                  <th className="pb-1 pr-4 text-right">평균 출력 토큰</th>
                  <th className="pb-1 text-right">평균 응답(ms)</th>
                </tr>
              </thead>
              <tbody>
                {llmData.by_type.map((stat) => (
                  <tr key={stat.metric_type} className="border-b border-slate-100">
                    <td className="py-1 pr-4 font-mono">{stat.metric_type}</td>
                    <td className="py-1 pr-4 text-right">{stat.total_calls}</td>
                    <td className={`py-1 pr-4 text-right ${stat.error_rate > 0.1 ? "text-rose-600 font-semibold" : ""}`}>
                      {(stat.error_rate * 100).toFixed(1)}%
                    </td>
                    <td className="py-1 pr-4 text-right">{stat.avg_input_tokens.toFixed(0)}</td>
                    <td className="py-1 pr-4 text-right">{stat.avg_output_tokens.toFixed(0)}</td>
                    <td className="py-1 text-right">{stat.avg_duration_ms.toFixed(0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}
