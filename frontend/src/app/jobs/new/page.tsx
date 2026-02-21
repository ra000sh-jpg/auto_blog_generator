"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  createJob,
  fetchConfig,
  type PersonaOption,
  type TopicModeOption,
} from "@/lib/api";

const ALLOWED_TOPICS = new Set(["cafe", "parenting", "it", "finance", "economy"]);

const FALLBACK_PERSONAS: PersonaOption[] = [
  { value: "P1", label: "Cafe Creator (P1)", topic_mode: "cafe" },
  { value: "P2", label: "Tech Blogger (P2)", topic_mode: "it" },
  { value: "P3", label: "Parenting Writer (P3)", topic_mode: "parenting" },
  { value: "P4", label: "Finance Insight (P4)", topic_mode: "finance" },
];

const FALLBACK_TOPICS: TopicModeOption[] = [
  { value: "cafe", label: "Cafe" },
  { value: "parenting", label: "Parenting" },
  { value: "it", label: "IT" },
  { value: "finance", label: "Finance" },
  { value: "economy", label: "Economy (Alias)" },
];

function parseKeywords(rawValue: string): string[] {
  return rawValue
    .split(",")
    .map((keyword) => keyword.trim())
    .filter((keyword, index, list) => keyword.length > 0 && list.indexOf(keyword) === index);
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
  const [title, setTitle] = useState("");
  const [keywordsText, setKeywordsText] = useState("");
  const [topicMode, setTopicMode] = useState("cafe");
  const [personaId, setPersonaId] = useState("P1");
  const [scheduledAt, setScheduledAt] = useState("");
  const [personas, setPersonas] = useState<PersonaOption[]>(FALLBACK_PERSONAS);
  const [topics, setTopics] = useState<TopicModeOption[]>(FALLBACK_TOPICS);
  const [loadingConfig, setLoadingConfig] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const filteredTopics = useMemo(
    () => topics.filter((option) => ALLOWED_TOPICS.has(option.value)),
    [topics],
  );

  useEffect(() => {
    let isMounted = true;

    async function loadConfig() {
      try {
        const response = await fetchConfig();
        if (!isMounted) {
          return;
        }

        const safePersonas = response.personas.filter((item) =>
          ["P1", "P2", "P3", "P4"].includes(item.value),
        );
        const safeTopics = response.topic_modes.filter((item) => ALLOWED_TOPICS.has(item.value));

        if (safePersonas.length > 0) {
          setPersonas(safePersonas);
        }
        if (safeTopics.length > 0) {
          setTopics(safeTopics);
        }
        if (safePersonas.some((item) => item.value === response.defaults.persona_id)) {
          setPersonaId(response.defaults.persona_id);
        }
        if (safeTopics.some((item) => item.value === response.defaults.topic_mode)) {
          setTopicMode(response.defaults.topic_mode);
        }
      } catch {
        if (!isMounted) {
          return;
        }
      } finally {
        if (isMounted) {
          setLoadingConfig(false);
        }
      }
    }

    loadConfig();
    return () => {
      isMounted = false;
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");

    const normalizedTitle = title.trim();
    const seedKeywords = parseKeywords(keywordsText);
    const scheduledAtIso = toIsoDatetime(scheduledAt);

    if (!normalizedTitle) {
      setError("제목을 입력해 주세요.");
      return;
    }
    if (seedKeywords.length === 0) {
      setError("Seed Keywords를 1개 이상 입력해 주세요.");
      return;
    }
    if (!ALLOWED_TOPICS.has(topicMode)) {
      setError("지원하지 않는 Topic Mode입니다.");
      return;
    }
    if (!["P1", "P2", "P3", "P4"].includes(personaId)) {
      setError("지원하지 않는 Persona ID입니다.");
      return;
    }
    if (scheduledAt && !scheduledAtIso) {
      setError("Scheduled At 형식이 올바르지 않습니다.");
      return;
    }

    setSubmitting(true);
    try {
      await createJob({
        title: normalizedTitle,
        seed_keywords: seedKeywords,
        platform: "naver",
        persona_id: personaId,
        topic_mode: topicMode,
        scheduled_at: scheduledAtIso,
      });
      router.push("/jobs?created=1");
    } catch (requestError) {
      const message =
        requestError instanceof Error
          ? requestError.message
          : "작업 생성 중 오류가 발생했습니다.";
      setError(message);
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-4">
      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <h1 className="font-[family-name:var(--font-heading)] text-2xl font-semibold tracking-tight">
          New Job
        </h1>
        <p className="mt-1 text-sm text-slate-600">
          대시보드에서 신규 포스팅 작업을 예약합니다.
        </p>
      </section>

      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        {loadingConfig && (
          <p className="mb-4 rounded-xl bg-slate-50 px-3 py-2 text-sm text-slate-600">
            설정 정보를 불러오는 중입니다...
          </p>
        )}

        {error && (
          <p className="mb-4 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {error}
          </p>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <label className="block">
            <span className="mb-1 block text-sm font-medium text-slate-700">Title</span>
            <input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="예) 이번 주 카페 매출 분석"
              className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none ring-0 transition focus:border-slate-500"
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-sm font-medium text-slate-700">Seed Keywords</span>
            <input
              value={keywordsText}
              onChange={(event) => setKeywordsText(event.target.value)}
              placeholder="예) 자동화, 블로그, 네이버"
              className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none ring-0 transition focus:border-slate-500"
            />
            <span className="mt-1 block text-xs text-slate-500">
              쉼표(,)로 구분해 입력해 주세요.
            </span>
          </label>

          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block">
              <span className="mb-1 block text-sm font-medium text-slate-700">Topic Mode</span>
              <select
                value={topicMode}
                onChange={(event) => setTopicMode(event.target.value)}
                className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none ring-0 transition focus:border-slate-500"
              >
                {filteredTopics.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>

            <label className="block">
              <span className="mb-1 block text-sm font-medium text-slate-700">Persona ID</span>
              <select
                value={personaId}
                onChange={(event) => setPersonaId(event.target.value)}
                className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none ring-0 transition focus:border-slate-500"
              >
                {personas.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <label className="block">
            <span className="mb-1 block text-sm font-medium text-slate-700">Scheduled At</span>
            <input
              type="datetime-local"
              value={scheduledAt}
              onChange={(event) => setScheduledAt(event.target.value)}
              className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none ring-0 transition focus:border-slate-500"
            />
            <span className="mt-1 block text-xs text-slate-500">
              비워두면 즉시 실행 가능한 작업으로 등록됩니다.
            </span>
          </label>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="submit"
              disabled={submitting}
              className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50"
            >
              {submitting ? "등록 중..." : "작업 예약 등록"}
            </button>
            <Link
              href="/jobs"
              className="rounded-full border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-500"
            >
              취소
            </Link>
          </div>
        </form>
      </section>
    </div>
  );
}
