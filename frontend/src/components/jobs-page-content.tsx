"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { JobsTable } from "@/components/jobs-table";

export function JobsPageContent() {
  const searchParams = useSearchParams();
  const isCreated = searchParams.get("created") === "1";
  const reloadToken = isCreated ? 1 : 0;

  return (
    <div className="space-y-4">
      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="font-[family-name:var(--font-heading)] text-2xl font-semibold tracking-tight">
              Jobs
            </h1>
            <p className="mt-1 text-sm text-slate-600">
              예약/진행/완료 작업 리스트를 확인하고 새 작업을 추가합니다.
            </p>
          </div>
          <Link
            href="/jobs/new"
            className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700"
          >
            + 새 작업 예약
          </Link>
        </div>
      </section>

      {isCreated && (
        <p className="rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700">
          새 작업이 등록되었습니다. 최신 목록으로 갱신했습니다.
        </p>
      )}

      <JobsTable initialPage={1} size={20} reloadToken={reloadToken} />
    </div>
  );
}
