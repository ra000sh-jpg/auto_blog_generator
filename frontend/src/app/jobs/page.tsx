import { Suspense } from "react";

import { JobsPageContent } from "@/components/jobs-page-content";

function JobsPageFallback() {
  return (
    <div className="space-y-4">
      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <h1 className="font-[family-name:var(--font-heading)] text-2xl font-semibold tracking-tight">
          Jobs
        </h1>
        <p className="mt-1 text-sm text-slate-600">작업 목록 화면을 불러오는 중입니다...</p>
      </section>
    </div>
  );
}

export default function JobsPage() {
  return (
    <Suspense fallback={<JobsPageFallback />}>
      <JobsPageContent />
    </Suspense>
  );
}
