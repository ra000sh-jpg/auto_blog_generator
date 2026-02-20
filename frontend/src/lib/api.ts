export const BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000/api";

export type ProviderHealth = {
  provider: string;
  model: string;
  status: string;
  message: string;
};

export type HealthResponse = {
  status: string;
  timestamp: string;
  summary: {
    total: number;
    ok: number;
    fail: number;
  };
  providers: ProviderHealth[];
  warnings: string[];
};

export type JobsResponse = {
  page: number;
  size: number;
  total: number;
  pages: number;
  queue_stats: Record<string, number>;
  items: Array<{
    job_id: string;
    status: string;
    title: string;
    seed_keywords: string[];
    platform: string;
    persona_id: string;
    scheduled_at: string;
    created_at: string;
    updated_at: string;
    category: string;
  }>;
};

export type CreateJobPayload = {
  title: string;
  seed_keywords: string[];
  platform?: string;
  persona_id: string;
  scheduled_at?: string;
  topic_mode: string;
};

export type CreateJobResponse = {
  job_id: string;
  status: string;
  scheduled_at: string;
  platform: string;
  persona_id: string;
  topic_mode: string;
  category: string;
};

export type ApiKeyStatus = {
  provider: string;
  env_var: string;
  configured: boolean;
  masked: string;
};

export type PersonaOption = {
  value: string;
  label: string;
  topic_mode: string;
};

export type TopicModeOption = {
  value: string;
  label: string;
};

export type ConfigResponse = {
  api_keys: ApiKeyStatus[];
  personas: PersonaOption[];
  topic_modes: TopicModeOption[];
  defaults: {
    platform: string;
    persona_id: string;
    topic_mode: string;
    api_base_url: string;
  };
  llm: {
    primary_provider: string;
    primary_model: string;
    secondary_provider: string;
    secondary_model: string;
  };
};

export type MetricsResponse = {
  page: number;
  size: number;
  total: number;
  pages: number;
  summary: {
    total_posts: number;
    total_views: number;
    total_likes: number;
    total_comments: number;
    avg_views: number;
  };
  items: Array<{
    post_id: string;
    job_id: string;
    title: string;
    url: string;
    views: number;
    likes: number;
    comments: number;
    snapshot_at: string;
  }>;
};

export type OnboardingStatusResponse = {
  completed: boolean;
  persona_id: string;
  interests: string[];
  voice_profile: Record<string, unknown>;
  recommended_categories: string[];
  categories: string[];
  fallback_category: string;
  daily_posts_target: number;
  idea_vault_daily_quota: number;
  category_allocations: Array<{
    category: string;
    topic_mode: string;
    count: number;
  }>;
  telegram_configured: boolean;
};

export type PersonaLabPayload = {
  persona_id: string;
  identity: string;
  target_audience: string;
  tone_hint: string;
  interests: string[];
  structure_score: number;
  evidence_score: number;
  distance_score: number;
  criticism_score: number;
  density_score: number;
  style_strength: number;
};

export type PersonaLabResponse = {
  persona_id: string;
  voice_profile: Record<string, unknown>;
  recommended_categories: string[];
};

export type CategorySetupPayload = {
  categories: string[];
  fallback_category: string;
};

export type CategorySetupResponse = {
  categories: string[];
  fallback_category: string;
};

export type ScheduleAllocationItem = {
  category: string;
  topic_mode: string;
  count: number;
};

export type ScheduleSetupPayload = {
  daily_posts_target: number;
  idea_vault_daily_quota: number;
  allocations: ScheduleAllocationItem[];
};

export type ScheduleSetupResponse = {
  daily_posts_target: number;
  idea_vault_daily_quota: number;
  allocations: ScheduleAllocationItem[];
};

export type TelegramTestPayload = {
  bot_token: string;
  chat_id: string;
  save?: boolean;
};

export type TelegramTestResponse = {
  success: boolean;
  message: string;
};

export type CompleteOnboardingResponse = {
  completed: boolean;
  completed_at: string;
};

export type MagicInputParsePayload = {
  instruction: string;
  platform?: string;
  scheduled_at?: string;
};

export type MagicInputParseResponse = {
  title: string;
  seed_keywords: string[];
  persona_id: string;
  topic_mode: string;
  schedule_time?: string | null;
  confidence: number;
  parser_used: string;
  raw: Record<string, unknown>;
};

export type MagicCreateJobPayload = {
  instruction: string;
  platform?: string;
  scheduled_at?: string;
  title_override?: string;
  persona_id_override?: string;
  topic_mode_override?: string;
  keywords_override?: string[];
  category_override?: string;
  max_retries?: number;
  tags?: string[];
};

export type MagicCreateJobResponse = {
  job_id: string;
  status: string;
  scheduled_at: string;
  platform: string;
  title: string;
  seed_keywords: string[];
  persona_id: string;
  topic_mode: string;
  category: string;
  parser_used: string;
};

export type IdeaVaultStatsResponse = {
  total: number;
  pending: number;
  queued: number;
  consumed: number;
};

export type IdeaVaultIngestPayload = {
  raw_text: string;
  batch_size?: number;
};

export type IdeaVaultIngestResponse = {
  total_lines: number;
  accepted_count: number;
  rejected_count: number;
  parser_used: string;
  pending_count: number;
  rejected_preview: Array<{
    line: string;
    reason: string;
  }>;
};

type RequestOptions = {
  method?: "GET" | "POST";
  body?: unknown;
};

async function requestJSON<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body } = options;
  const response = await fetch(`${BASE_URL}${path}`, {
    method,
    headers: body
      ? {
          Accept: "application/json",
          "Content-Type": "application/json",
        }
      : {
          Accept: "application/json",
        },
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });

  if (!response.ok) {
    let detailMessage = "";
    try {
      const payload = (await response.json()) as { detail?: string };
      detailMessage = typeof payload.detail === "string" ? payload.detail : "";
    } catch {
      detailMessage = "";
    }
    const baseMessage = `API request failed (${response.status})`;
    throw new Error(detailMessage ? `${baseMessage}: ${detailMessage}` : baseMessage);
  }

  return (await response.json()) as T;
}

export async function fetchHealth(): Promise<HealthResponse> {
  return requestJSON<HealthResponse>("/health");
}

export async function fetchJobs(page = 1, size = 20): Promise<JobsResponse> {
  const query = new URLSearchParams({
    page: String(page),
    size: String(size),
  });
  return requestJSON<JobsResponse>(`/jobs?${query.toString()}`);
}

export async function createJob(payload: CreateJobPayload): Promise<CreateJobResponse> {
  return requestJSON<CreateJobResponse>("/jobs", {
    method: "POST",
    body: payload,
  });
}

export async function fetchConfig(): Promise<ConfigResponse> {
  return requestJSON<ConfigResponse>("/config");
}

export async function fetchMetrics(page = 1, size = 20): Promise<MetricsResponse> {
  const query = new URLSearchParams({
    page: String(page),
    size: String(size),
  });
  return requestJSON<MetricsResponse>(`/metrics?${query.toString()}`);
}

export async function fetchOnboardingStatus(): Promise<OnboardingStatusResponse> {
  return requestJSON<OnboardingStatusResponse>("/onboarding");
}

export async function savePersonaLab(payload: PersonaLabPayload): Promise<PersonaLabResponse> {
  return requestJSON<PersonaLabResponse>("/onboarding/persona", {
    method: "POST",
    body: payload,
  });
}

export async function saveOnboardingCategories(
  payload: CategorySetupPayload,
): Promise<CategorySetupResponse> {
  return requestJSON<CategorySetupResponse>("/onboarding/categories", {
    method: "POST",
    body: payload,
  });
}

export async function saveOnboardingSchedule(
  payload: ScheduleSetupPayload,
): Promise<ScheduleSetupResponse> {
  return requestJSON<ScheduleSetupResponse>("/onboarding/schedule", {
    method: "POST",
    body: payload,
  });
}

export async function testTelegramSetup(
  payload: TelegramTestPayload,
): Promise<TelegramTestResponse> {
  return requestJSON<TelegramTestResponse>("/onboarding/telegram/test", {
    method: "POST",
    body: payload,
  });
}

export async function completeOnboarding(): Promise<CompleteOnboardingResponse> {
  return requestJSON<CompleteOnboardingResponse>("/onboarding/complete", {
    method: "POST",
  });
}

export async function parseMagicInput(
  payload: MagicInputParsePayload,
): Promise<MagicInputParseResponse> {
  return requestJSON<MagicInputParseResponse>("/magic-input/parse", {
    method: "POST",
    body: payload,
  });
}

export async function createMagicJob(
  payload: MagicCreateJobPayload,
): Promise<MagicCreateJobResponse> {
  return requestJSON<MagicCreateJobResponse>("/magic-input/jobs", {
    method: "POST",
    body: payload,
  });
}

export async function fetchIdeaVaultStats(): Promise<IdeaVaultStatsResponse> {
  return requestJSON<IdeaVaultStatsResponse>("/idea-vault/stats");
}

export async function ingestIdeaVault(
  payload: IdeaVaultIngestPayload,
): Promise<IdeaVaultIngestResponse> {
  return requestJSON<IdeaVaultIngestResponse>("/idea-vault/ingest", {
    method: "POST",
    body: payload,
  });
}
