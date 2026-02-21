"use client";

import { useEffect, useMemo, useState } from "react";

import { fetchHealth, type HealthResponse } from "@/lib/api";

const STATUS_CLASS: Record<string, string> = {
  OK: "bg-emerald-100 text-emerald-800 border-emerald-300",
  FAIL: "bg-rose-100 text-rose-800 border-rose-300",
};

export function HealthWidget() {
  const [data, setData] = useState<HealthResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let isMounted = true;

    async function loadHealth() {
      try {
        const response = await fetchHealth();
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
            : "헬스 체크를 불러오지 못했습니다.";
        setError(message);
      } finally {
        if (isMounted) {
          setLoading(false);
        }
      }
    }

    loadHealth();
    return () => {
      isMounted = false;
    };
  }, []);

  const overallClass = useMemo(() => {
    if (!data) {
      return "border-slate-200 bg-white text-slate-700";
    }
    if (data.status === "ok") {
      return "border-emerald-300 bg-emerald-50 text-emerald-900";
    }
    return "border-amber-300 bg-amber-50 text-amber-900";
  }, [data]);

  return (
    <section className="space-y-3 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm @container">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
          API Health
        </h2>
        {data && (
          <span
            className={`rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-wide ${overallClass}`}
          >
            {data.status}
          </span>
        )}
      </header>

      {loading && (
        <p className="rounded-xl bg-slate-50 px-3 py-2 text-sm text-slate-500">
          상태를 확인하는 중입니다...
        </p>
      )}

      {error && (
        <p className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </p>
      )}

      {data && (
        <>
          <div className="grid gap-3 @md:grid-cols-2">
            {data.providers.map((provider) => {
              const status = provider.status.toUpperCase();
              const className =
                STATUS_CLASS[status] || "bg-slate-100 text-slate-800 border-slate-300";
              return (
                <article
                  key={`${provider.provider}-${provider.model}`}
                  className="rounded-xl border border-slate-200 bg-slate-50 p-3"
                >
                  <div className="flex items-center justify-between gap-2">
                    <p className="font-semibold capitalize">{provider.provider}</p>
                    <span className={`rounded-full border px-2 py-0.5 text-xs ${className}`}>
                      {provider.status}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-slate-600">{provider.model}</p>
                  <p className="mt-2 text-xs text-slate-600">{provider.message}</p>
                </article>
              );
            })}
          </div>

          {data.warnings.length > 0 && (
            <div className="rounded-xl border border-amber-300 bg-amber-50 p-3">
              <p className="text-sm font-semibold text-amber-900">설정 확인 필요</p>
              <ul className="mt-1 space-y-1 text-xs text-amber-900">
                {data.warnings.map((warning) => (
                  <li key={warning}>- {warning}</li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </section>
  );
}

