"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useMemo, useState } from "react";

import { createJob } from "@/lib/api";

const DEFAULT_KEYWORDS = ["투자공부", "시장흐름", "경제통찰"];

function parseKeywords(rawValue: string): string[] {
  return rawValue
    .split(",")
    .map((keyword) => keyword.trim())
    .filter((keyword, index, list) => keyword.length > 0 && list.indexOf(keyword) === index);
}

function inferKeywords(rawTitle: string, manualText: string): string[] {
  const manualKeywords = parseKeywords(manualText);
  if (manualKeywords.length > 0) {
    return manualKeywords.slice(0, 6);
  }

  const titleKeywords = rawTitle
    .replace(/[^\p{L}\p{N}\s]/gu, " ")
    .split(/\s+/)
    .map((keyword) => keyword.trim())
    .filter((keyword) => keyword.length >= 2)
    .slice(0, 4);

  const merged = [...titleKeywords, ...DEFAULT_KEYWORDS];
  return merged.filter((keyword, index, list) => list.indexOf(keyword) === index).slice(0, 6);
}

function toIsoDatetime(rawValue: string): string | undefined {
  if (!rawValue) {
    return undefined;
  }
  const date = new Date(rawValue);
  if (Number.isNaN(date.getTime())) {
    return undefined;
  }
  return date.toISOString();
}

export default function NewJobPage() {
  const router = useRouter();
  const [sentence, setSentence] = useState("");
  const [scheduledAt, setScheduledAt] = useState("");
  const [keywordsText, setKeywordsText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const previewKeywords = useMemo(
    () => inferKeywords(sentence.trim(), keywordsText),
    [sentence, keywordsText],
  );

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");

    const title = sentence.trim();
    const scheduledAtIso = toIsoDatetime(scheduledAt);

    if (!title) {
      setError("한 문장 제목 또는 메모를 입력해 주세요.");
      return;
    }
    if (scheduledAt && !scheduledAtIso) {
      setError("예약 시각 형식이 올바르지 않습니다.");
      return;
    }

    setSubmitting(true);
    try {
      await createJob({
        title,
        seed_keywords: previewKeywords,
        platform: "naver",
        persona_id: "P4",
        topic_mode: "finance",
        category: "경제 공부와 투자 기록",
        scheduled_at: scheduledAtIso,
        max_retries: 3,
        tags: ["manual_insight", "single_sentence_seed"],
      });
      router.push("/jobs?created=1");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "작업 생성 중 오류가 발생했습니다.");
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-4">
      <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <p className="text-xs font-semibold uppercase tracking-wide text-teal-700">
          Manual Insight Seed
        </p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-950">
          한 문장 통찰 예약
        </h1>
        <p className="mt-1 text-sm leading-6 text-slate-600">
          앞으로 쓸 만한 제목이나 메모 한 줄을 넣으면, 투자 공부형 블로그 초안으로 예약합니다.
        </p>
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        {error && (
          <p className="mb-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {error}
          </p>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <label className="block">
            <span className="mb-1 block text-sm font-medium text-slate-700">
              제목 또는 메모 한 문장
            </span>
            <textarea
              value={sentence}
              onChange={(event) => setSentence(event.target.value)}
              rows={4}
              placeholder="예) 좋은 경제 뉴스가 나와도 주가가 바로 오르지 않는 이유"
              className="w-full resize-none rounded-lg border border-slate-300 px-3 py-2 text-sm leading-6 outline-none transition focus:border-slate-500"
            />
          </label>

          <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
            <p className="text-xs font-semibold text-slate-500">자동 적용값</p>
            <div className="mt-2 grid gap-2 text-sm text-slate-700 sm:grid-cols-3">
              <span>플랫폼: 네이버</span>
              <span>페르소나: P4</span>
              <span>주제: 투자 공부</span>
            </div>
            <p className="mt-2 text-xs text-slate-500">
              키워드: {previewKeywords.join(", ")}
            </p>
          </div>

          <details className="rounded-lg border border-slate-200 bg-white">
            <summary className="cursor-pointer px-3 py-2 text-sm font-medium text-slate-700">
              보조 키워드 직접 입력
            </summary>
            <div className="border-t border-slate-100 p-3">
              <input
                value={keywordsText}
                onChange={(event) => setKeywordsText(event.target.value)}
                placeholder="예) Fed, 반도체, 한국 ETF"
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none transition focus:border-slate-500"
              />
              <p className="mt-1 text-xs text-slate-500">
                비워두면 제목에서 자동 추출합니다. 쉼표로 구분해 주세요.
              </p>
            </div>
          </details>

          <label className="block">
            <span className="mb-1 block text-sm font-medium text-slate-700">예약 시각</span>
            <input
              type="datetime-local"
              value={scheduledAt}
              onChange={(event) => setScheduledAt(event.target.value)}
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none transition focus:border-slate-500"
            />
            <span className="mt-1 block text-xs text-slate-500">
              비워두면 즉시 실행 가능한 대기 작업으로 등록됩니다.
            </span>
          </label>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="submit"
              disabled={submitting}
              className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50"
            >
              {submitting ? "등록 중..." : "통찰 글 예약"}
            </button>
            <Link
              href="/jobs"
              className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-500"
            >
              취소
            </Link>
          </div>
        </form>
      </section>
    </div>
  );
}
