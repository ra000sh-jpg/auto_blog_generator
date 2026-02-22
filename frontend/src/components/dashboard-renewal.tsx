"use client";

import Image from "next/image";
import { FormEvent, useEffect, useMemo, useState } from "react";

import { AIToggleSummary } from "@/components/ai-toggle-summary";
import { HealthWidget } from "@/components/health-widget";
import { MetricsSummary } from "@/components/metrics-summary";
import {
  DEFAULT_FALLBACK_CATEGORY,
  completeOnboarding,
  createMagicJob,
  fetchNaverConnectStatus,
  fetchIdeaVaultStats,
  fetchOnboardingStatus,
  fetchRouterSettings,
  ingestIdeaVault,
  quoteRouterSettings,
  saveOnboardingCategories,
  saveOnboardingSchedule,
  saveRouterSettings,
  savePersonaLab,
  startNaverConnect,
  testTelegramSetup,
  type IdeaVaultStatsResponse,
  type NaverConnectStatusResponse,
  type OnboardingStatusResponse,
  type RouterQuoteResponse,
  type ScheduleAllocationItem,
} from "@/lib/api";

type PersonaOption = {
  value: string;
  label: string;
};

const PERSONA_OPTIONS: PersonaOption[] = [
  { value: "P1", label: "Cafe Creator (P1)" },
  { value: "P2", label: "Tech Blogger (P2)" },
  { value: "P3", label: "Parenting Writer (P3)" },
  { value: "P4", label: "Finance Insight (P4)" },
];

const TOPIC_OPTIONS = [
  { value: "cafe", label: "Cafe" },
  { value: "it", label: "IT" },
  { value: "parenting", label: "Parenting" },
  { value: "finance", label: "Finance" },
];

function parseCommaValues(rawText: string): string[] {
  return rawText
    .split(",")
    .map((value) => value.trim())
    .filter((value, index, list) => value.length > 0 && list.indexOf(value) === index);
}

function toIsoDatetime(rawValue: string): string | undefined {
  if (!rawValue) {
    return undefined;
  }
  const parsed = new Date(rawValue);
  if (Number.isNaN(parsed.getTime())) {
    return undefined;
  }
  return parsed.toISOString();
}

function sliderLabel(score: number, labels: [string, string, string]): string {
  if (score <= 33) {
    return labels[0];
  }
  if (score <= 66) {
    return labels[1];
  }
  return labels[2];
}

function formatKrw(value: number): string {
  return new Intl.NumberFormat("ko-KR").format(Math.max(0, Math.round(value)));
}

function compactKeys(input: Record<string, string>): Record<string, string> {
  return Object.entries(input).reduce<Record<string, string>>((acc, [key, value]) => {
    const normalized = String(value || "").trim();
    if (normalized) {
      acc[key] = normalized;
    }
    return acc;
  }, {});
}

function inferTopicMode(categoryName: string): string {
  const lowered = categoryName.toLowerCase();
  if (["경제", "finance", "투자", "주식", "재테크"].some((token) => lowered.includes(token))) {
    return "finance";
  }
  if (["it", "개발", "코드", "자동화", "ai", "테크"].some((token) => lowered.includes(token))) {
    return "it";
  }
  if (["육아", "아이", "부모", "가정"].some((token) => lowered.includes(token))) {
    return "parenting";
  }
  return "cafe";
}

function normalizeAllocations(
  categories: string[],
  dailyTarget: number,
  existingAllocations: ScheduleAllocationItem[] = [],
): ScheduleAllocationItem[] {
  const normalizedCategories = categories
    .map((value) => value.trim())
    .filter((value, index, list) => value.length > 0 && list.indexOf(value) === index);
  const fallbackCategories = normalizedCategories.length > 0 ? normalizedCategories : [DEFAULT_FALLBACK_CATEGORY];

  const existingMap = new Map(existingAllocations.map((item) => [item.category, item]));
  const rows: ScheduleAllocationItem[] = fallbackCategories.map((categoryName) => {
    const existing = existingMap.get(categoryName);
    return {
      category: categoryName,
      topic_mode: existing?.topic_mode || inferTopicMode(categoryName),
      count: Math.max(0, Number(existing?.count || 0)),
    };
  });

  const safeTarget = Math.max(0, dailyTarget);
  if (safeTarget <= 0) {
    return rows.map((item) => ({
      ...item,
      count: 0,
    }));
  }
  let total = rows.reduce((acc, item) => acc + item.count, 0);
  if (total <= 0) {
    for (let index = 0; index < safeTarget; index += 1) {
      rows[index % rows.length].count += 1;
    }
    return rows;
  }

  if (total < safeTarget) {
    rows[0].count += safeTarget - total;
    return rows;
  }

  if (total > safeTarget) {
    let overflow = total - safeTarget;
    for (let index = rows.length - 1; index >= 0; index -= 1) {
      if (overflow <= 0) {
        break;
      }
      const deductible = Math.min(rows[index].count, overflow);
      rows[index].count -= deductible;
      overflow -= deductible;
    }
  }

  total = rows.reduce((acc, item) => acc + item.count, 0);
  if (total !== safeTarget) {
    rows[0].count += safeTarget - total;
  }
  return rows;
}

export function DashboardRenewal() {
  const [loading, setLoading] = useState(true);
  const [loadingError, setLoadingError] = useState("");
  const [onboarding, setOnboarding] = useState<OnboardingStatusResponse | null>(null);

  const [step, setStep] = useState(0);
  const [saving, setSaving] = useState(false);
  const [stepMessage, setStepMessage] = useState("");
  const [routerSaving, setRouterSaving] = useState(false);
  const [routerLoading, setRouterLoading] = useState(false);
  const [routerMessage, setRouterMessage] = useState("");

  const [strategyMode, setStrategyMode] = useState<"cost" | "quality">("cost");
  const [textApiKeys, setTextApiKeys] = useState<Record<string, string>>({
    qwen: "",
    deepseek: "",
    gemini: "",
    openai: "",
    claude: "",
  });
  const [textApiMasks, setTextApiMasks] = useState<Record<string, string>>({});
  const [imageApiKeys, setImageApiKeys] = useState<Record<string, string>>({
    pexels: "",
    together: "",
    fal: "",
    openai_image: "",
  });
  const [imageApiMasks, setImageApiMasks] = useState<Record<string, string>>({});
  const [imageEngine, setImageEngine] = useState("pexels");
  const [imageEnabled, setImageEnabled] = useState(true);
  const [imagesPerPost, setImagesPerPost] = useState(1);
  const [routerQuote, setRouterQuote] = useState<RouterQuoteResponse | null>(null);
  const [textModelMatrix, setTextModelMatrix] = useState<Array<Record<string, unknown>>>([]);
  const [imageModelMatrix, setImageModelMatrix] = useState<Array<Record<string, unknown>>>([]);
  const [naverStatus, setNaverStatus] = useState<NaverConnectStatusResponse | null>(null);
  const [naverConnecting, setNaverConnecting] = useState(false);

  const [personaId, setPersonaId] = useState("P1");
  const [identity, setIdentity] = useState("");
  const [targetAudience, setTargetAudience] = useState("");
  const [toneHint, setToneHint] = useState("");
  const [interestsText, setInterestsText] = useState("");
  const [structureScore, setStructureScore] = useState(50);
  const [evidenceScore, setEvidenceScore] = useState(50);
  const [distanceScore, setDistanceScore] = useState(50);
  const [criticismScore, setCriticismScore] = useState(50);
  const [densityScore, setDensityScore] = useState(50);
  const [styleStrength, setStyleStrength] = useState(40);

  const [recommendedCategories, setRecommendedCategories] = useState<string[]>([]);
  const [categoriesText, setCategoriesText] = useState("");
  const [fallbackCategory, setFallbackCategory] = useState(DEFAULT_FALLBACK_CATEGORY);

  const [dailyPostsTarget, setDailyPostsTarget] = useState(3);
  const [ideaVaultDailyQuota, setIdeaVaultDailyQuota] = useState(2);
  const [categoryAllocations, setCategoryAllocations] = useState<ScheduleAllocationItem[]>([]);

  const [botToken, setBotToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [telegramVerified, setTelegramVerified] = useState(false);

  const [instruction, setInstruction] = useState("");
  const [workspaceTab, setWorkspaceTab] = useState<"magic" | "vault">("magic");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [advancedPersonaId, setAdvancedPersonaId] = useState("P1");
  const [advancedTopicMode, setAdvancedTopicMode] = useState("cafe");
  const [advancedScheduleAt, setAdvancedScheduleAt] = useState("");
  const [advancedKeywordsText, setAdvancedKeywordsText] = useState("");
  const [advancedCategory, setAdvancedCategory] = useState("");
  const [submittingMagic, setSubmittingMagic] = useState(false);
  const [magicMessage, setMagicMessage] = useState("");
  const [ideaVaultText, setIdeaVaultText] = useState("");
  const [ideaVaultSubmitting, setIdeaVaultSubmitting] = useState(false);
  const [ideaVaultMessage, setIdeaVaultMessage] = useState("");
  const [ideaVaultStats, setIdeaVaultStats] = useState<IdeaVaultStatsResponse | null>(null);

  useEffect(() => {
    let isMounted = true;

    async function loadStatus() {
      try {
        const [response, routerState, naverConnectState] = await Promise.all([
          fetchOnboardingStatus(),
          fetchRouterSettings(),
          fetchNaverConnectStatus(),
        ]);
        if (!isMounted) {
          return;
        }
        setOnboarding(response);
        setPersonaId(response.persona_id || "P1");
        setAdvancedPersonaId(response.persona_id || "P1");
        setRecommendedCategories(response.recommended_categories || []);
        setCategoriesText((response.categories || []).join(", "));
        setFallbackCategory(response.fallback_category || DEFAULT_FALLBACK_CATEGORY);
        setTelegramVerified(Boolean(response.telegram_configured));
        setInterestsText((response.interests || []).join(", "));

        const resolvedTarget = Math.max(3, Math.min(5, Number(response.daily_posts_target || 3)));
        const resolvedIdeaVaultQuota = Math.max(
          0,
          Math.min(
            resolvedTarget,
            Number(response.idea_vault_daily_quota ?? Math.min(2, resolvedTarget)),
          ),
        );
        setDailyPostsTarget(resolvedTarget);
        setIdeaVaultDailyQuota(resolvedIdeaVaultQuota);
        setCategoryAllocations(
          normalizeAllocations(
            response.categories || [],
            Math.max(0, resolvedTarget - resolvedIdeaVaultQuota),
            response.category_allocations || [],
          ),
        );
        setStrategyMode(
          routerState.settings.strategy_mode === "quality" ? "quality" : "cost",
        );
        setTextApiMasks(routerState.settings.text_api_keys_masked || {});
        setImageApiMasks(routerState.settings.image_api_keys_masked || {});
        setImageEngine(routerState.settings.image_engine || "pexels");
        setImageEnabled(Boolean(routerState.settings.image_enabled));
        setImagesPerPost(Math.max(0, Math.min(4, Number(routerState.settings.images_per_post || 1))));
        setRouterQuote({
          strategy_mode:
            routerState.settings.strategy_mode === "quality" ? "quality" : "cost",
          roles: routerState.roles || {},
          estimate: {
            currency: "KRW",
            text_cost_krw: Number(routerState.quote.text_cost_krw || 0),
            image_cost_krw: Number(routerState.quote.image_cost_krw || 0),
            total_cost_krw: Number(routerState.quote.total_cost_krw || 0),
            quality_score: Number(routerState.quote.quality_score || 0),
          },
          image: {},
          available_text_models: [],
        });
        setTextModelMatrix(routerState.matrix.text_models || []);
        setImageModelMatrix(routerState.matrix.image_models || []);
        setNaverStatus(naverConnectState);

        try {
          const vaultStats = await fetchIdeaVaultStats();
          if (isMounted) {
            setIdeaVaultStats(vaultStats);
          }
        } catch {
          if (isMounted) {
            setIdeaVaultStats(null);
          }
        }
      } catch (requestError) {
        if (!isMounted) {
          return;
        }
        const message =
          requestError instanceof Error ? requestError.message : "온보딩 상태를 불러오지 못했습니다.";
        setLoadingError(message);
      } finally {
        if (isMounted) {
          setLoading(false);
        }
      }
    }

    loadStatus();
    return () => {
      isMounted = false;
    };
  }, []);

  const stepTitles = useMemo(
    () => [
      "0. Router",
      "1. Persona Lab",
      "2. Category Sync",
      "3. Schedule & Ratio",
      "4. Telegram Setup",
    ],
    [],
  );

  const allocationTotal = useMemo(
    () => categoryAllocations.reduce((acc, item) => acc + Math.max(0, Number(item.count || 0)), 0),
    [categoryAllocations],
  );
  const trendDailyTarget = useMemo(
    () => Math.max(0, dailyPostsTarget - ideaVaultDailyQuota),
    [dailyPostsTarget, ideaVaultDailyQuota],
  );
  const parserModelLabel = useMemo(() => {
    const role = routerQuote?.roles?.parser;
    if (!role || typeof role !== "object") {
      return "-";
    }
    const label = (role as Record<string, unknown>).label;
    return typeof label === "string" ? label : "-";
  }, [routerQuote]);
  const qualityModelLabel = useMemo(() => {
    const role = routerQuote?.roles?.quality_step;
    if (!role || typeof role !== "object") {
      return "-";
    }
    const label = (role as Record<string, unknown>).label;
    return typeof label === "string" ? label : "-";
  }, [routerQuote]);
  const voiceModelLabel = useMemo(() => {
    const role = routerQuote?.roles?.voice_step;
    if (!role || typeof role !== "object") {
      return "-";
    }
    const label = (role as Record<string, unknown>).label;
    return typeof label === "string" ? label : "-";
  }, [routerQuote]);

  useEffect(() => {
    const timer = setTimeout(async () => {
      setRouterLoading(true);
      try {
        const quoted = await quoteRouterSettings({
          strategy_mode: strategyMode,
          text_api_keys: compactKeys(textApiKeys),
          image_api_keys: compactKeys(imageApiKeys),
          image_engine: imageEngine,
          image_enabled: imageEnabled,
          images_per_post: imagesPerPost,
        });
        setRouterQuote(quoted);
      } catch {
        // 견적 API 실패는 화면 흐름을 막지 않는다.
      } finally {
        setRouterLoading(false);
      }
    }, 350);

    return () => {
      clearTimeout(timer);
    };
  }, [strategyMode, textApiKeys, imageApiKeys, imageEngine, imageEnabled, imagesPerPost]);

  function handleTextKeyChange(keyId: string, value: string) {
    setTextApiKeys((previous) => ({
      ...previous,
      [keyId]: value,
    }));
  }

  function handleImageKeyChange(keyId: string, value: string) {
    setImageApiKeys((previous) => ({
      ...previous,
      [keyId]: value,
    }));
  }

  async function handleSaveRouterStep() {
    setRouterSaving(true);
    setRouterMessage("");
    try {
      const saved = await saveRouterSettings({
        strategy_mode: strategyMode,
        text_api_keys: compactKeys(textApiKeys),
        image_api_keys: compactKeys(imageApiKeys),
        image_engine: imageEngine,
        image_enabled: imageEnabled,
        images_per_post: imagesPerPost,
      });
      setTextApiMasks(saved.settings.text_api_keys_masked || {});
      setImageApiMasks(saved.settings.image_api_keys_masked || {});
      setStrategyMode(saved.settings.strategy_mode === "quality" ? "quality" : "cost");
      setImageEngine(saved.settings.image_engine || "pexels");
      setImageEnabled(Boolean(saved.settings.image_enabled));
      setImagesPerPost(Math.max(0, Math.min(4, Number(saved.settings.images_per_post || 1))));
      setTextModelMatrix(saved.matrix.text_models || []);
      setImageModelMatrix(saved.matrix.image_models || []);
      setRouterQuote((previous) => ({
        strategy_mode: saved.settings.strategy_mode === "quality" ? "quality" : "cost",
        roles: saved.roles || previous?.roles || {},
        estimate: {
          currency: "KRW",
          text_cost_krw: Number(saved.quote.text_cost_krw || 0),
          image_cost_krw: Number(saved.quote.image_cost_krw || 0),
          total_cost_krw: Number(saved.quote.total_cost_krw || 0),
          quality_score: Number(saved.quote.quality_score || 0),
        },
        image: previous?.image || {},
        available_text_models: previous?.available_text_models || [],
      }));
      setStep(1);
      setStepMessage("Step 0 저장 완료: 모델 오토 라우터가 활성화되었습니다.");
      setRouterMessage("라우터 설정 저장 완료");
    } catch (requestError) {
      const message =
        requestError instanceof Error ? requestError.message : "Step 0 저장 중 오류가 발생했습니다.";
      setRouterMessage(message);
      setStepMessage(message);
    } finally {
      setRouterSaving(false);
    }
  }

  async function handleNaverConnect() {
    setNaverConnecting(true);
    setRouterMessage("");
    try {
      const response = await startNaverConnect({ timeout_sec: 300 });
      const statusResponse = await fetchNaverConnectStatus();
      setNaverStatus(statusResponse);
      setRouterMessage(response.message);
    } catch (requestError) {
      const message =
        requestError instanceof Error ? requestError.message : "네이버 연동 실행에 실패했습니다.";
      setRouterMessage(message);
    } finally {
      setNaverConnecting(false);
    }
  }

  async function handleSavePersonaStep() {
    setSaving(true);
    setStepMessage("");
    try {
      const response = await savePersonaLab({
        persona_id: personaId,
        identity,
        target_audience: targetAudience,
        tone_hint: toneHint,
        interests: parseCommaValues(interestsText),
        structure_score: structureScore,
        evidence_score: evidenceScore,
        distance_score: distanceScore,
        criticism_score: criticismScore,
        density_score: densityScore,
        style_strength: styleStrength,
        mbti: "",
        mbti_enabled: false,
        mbti_confidence: 0,
        age_group: "30대",
        gender: "남성",
      });
      setRecommendedCategories(response.recommended_categories);
      setCategoriesText(response.recommended_categories.join(", "));
      setStep(2);
      setStepMessage("Step 1 저장 완료: Voice Profile이 반영되었습니다.");
    } catch (requestError) {
      const message =
        requestError instanceof Error ? requestError.message : "Step 1 저장 중 오류가 발생했습니다.";
      setStepMessage(message);
    } finally {
      setSaving(false);
    }
  }

  async function handleSaveCategoryStep() {
    setSaving(true);
    setStepMessage("");
    try {
      const response = await saveOnboardingCategories({
        categories: parseCommaValues(categoriesText),
        fallback_category: fallbackCategory || DEFAULT_FALLBACK_CATEGORY,
      });
      setCategoriesText(response.categories.join(", "));
      setFallbackCategory(response.fallback_category);
      setCategoryAllocations(
        normalizeAllocations(response.categories, trendDailyTarget, categoryAllocations),
      );
      setStep(3);
      setStepMessage("Step 2 저장 완료: 카테고리가 동기화되었습니다.");
    } catch (requestError) {
      const message =
        requestError instanceof Error ? requestError.message : "Step 2 저장 중 오류가 발생했습니다.";
      setStepMessage(message);
    } finally {
      setSaving(false);
    }
  }

  function handleDailyTargetChange(nextTarget: number) {
    const normalizedTarget = Math.max(3, Math.min(5, nextTarget));
    setDailyPostsTarget(normalizedTarget);
    const normalizedIdeaVaultQuota = Math.max(0, Math.min(normalizedTarget, ideaVaultDailyQuota));
    setIdeaVaultDailyQuota(normalizedIdeaVaultQuota);
    const adjustedTrendTarget = Math.max(0, normalizedTarget - normalizedIdeaVaultQuota);
    const currentCategories = categoryAllocations.map((item) => item.category);
    setCategoryAllocations(
      normalizeAllocations(currentCategories, adjustedTrendTarget, categoryAllocations),
    );
  }

  function handleIdeaVaultQuotaChange(nextQuota: number) {
    const normalizedQuota = Math.max(0, Math.min(dailyPostsTarget, nextQuota));
    setIdeaVaultDailyQuota(normalizedQuota);
    const adjustedTrendTarget = Math.max(0, dailyPostsTarget - normalizedQuota);
    const currentCategories = categoryAllocations.map((item) => item.category);
    setCategoryAllocations(
      normalizeAllocations(currentCategories, adjustedTrendTarget, categoryAllocations),
    );
  }

  function handleAllocationChange(index: number, patch: Partial<ScheduleAllocationItem>) {
    setCategoryAllocations((previous) => {
      const next = [...previous];
      const current = next[index];
      if (!current) {
        return previous;
      }
      const count =
        patch.count === undefined ? current.count : Math.max(0, Math.min(5, Number(patch.count || 0)));
      const topicMode =
        patch.topic_mode === undefined ? current.topic_mode : String(patch.topic_mode || "cafe");
      next[index] = {
        ...current,
        ...patch,
        count,
        topic_mode: topicMode,
      };
      return next;
    });
  }

  async function handleSaveScheduleStep() {
    setSaving(true);
    setStepMessage("");
    try {
      const normalized = normalizeAllocations(
        categoryAllocations.map((item) => item.category),
        trendDailyTarget,
        categoryAllocations,
      );
      const response = await saveOnboardingSchedule({
        daily_posts_target: dailyPostsTarget,
        idea_vault_daily_quota: ideaVaultDailyQuota,
        allocations: normalized,
      });
      setDailyPostsTarget(response.daily_posts_target);
      setIdeaVaultDailyQuota(response.idea_vault_daily_quota);
      setCategoryAllocations(
        normalizeAllocations(
          response.allocations.map((item) => item.category),
          Math.max(0, response.daily_posts_target - response.idea_vault_daily_quota),
          response.allocations,
        ),
      );
      setStep(4);
      setStepMessage("Step 3 저장 완료: 공장 가동 스케줄/비율이 반영되었습니다.");
    } catch (requestError) {
      const message =
        requestError instanceof Error ? requestError.message : "Step 3 저장 중 오류가 발생했습니다.";
      setStepMessage(message);
    } finally {
      setSaving(false);
    }
  }

  async function handleTestTelegram() {
    setSaving(true);
    setStepMessage("");
    try {
      const response = await testTelegramSetup({
        bot_token: botToken,
        chat_id: chatId,
        save: true,
      });
      if (response.success) {
        setTelegramVerified(true);
        setStepMessage("테스트 발송 성공: 핸드폰 알림 수신을 확인해 주세요.");
      }
    } catch (requestError) {
      setTelegramVerified(false);
      const message = requestError instanceof Error ? requestError.message : "테스트 발송 실패";
      setStepMessage(message);
    } finally {
      setSaving(false);
    }
  }

  async function handleCompleteOnboarding() {
    setSaving(true);
    setStepMessage("");
    try {
      const response = await completeOnboarding();
      if (response.completed) {
        setOnboarding((previous) =>
          previous
            ? {
              ...previous,
              completed: true,
              persona_id: personaId,
              categories: parseCommaValues(categoriesText),
              fallback_category: fallbackCategory || DEFAULT_FALLBACK_CATEGORY,
              daily_posts_target: dailyPostsTarget,
              idea_vault_daily_quota: ideaVaultDailyQuota,
              category_allocations: categoryAllocations,
              telegram_configured: telegramVerified,
            }
            : null,
        );
        setStepMessage("온보딩 완료! 이제 매직 인풋으로 바로 예약할 수 있습니다.");
      }
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : "온보딩 완료 처리 실패";
      setStepMessage(message);
    } finally {
      setSaving(false);
    }
  }

  async function handleMagicSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMagicMessage("");

    if (!instruction.trim()) {
      setMagicMessage("자연어 지시문을 입력해 주세요.");
      return;
    }

    const keywordsOverride = parseCommaValues(advancedKeywordsText);
    const scheduledAtIso = toIsoDatetime(advancedScheduleAt);
    if (advancedScheduleAt && !scheduledAtIso) {
      setMagicMessage("예약 시간 형식이 올바르지 않습니다.");
      return;
    }

    setSubmittingMagic(true);
    try {
      const response = await createMagicJob({
        instruction: instruction.trim(),
        platform: "naver",
        scheduled_at: scheduledAtIso,
        persona_id_override: advancedOpen ? advancedPersonaId : undefined,
        topic_mode_override: advancedOpen ? advancedTopicMode : undefined,
        keywords_override: advancedOpen ? keywordsOverride : undefined,
        category_override: advancedOpen && advancedCategory ? advancedCategory : undefined,
      });
      setMagicMessage(
        `작업 등록 완료: ${response.title} (${response.job_id.slice(0, 8)}...) / parser=${response.parser_used}`,
      );
      setInstruction("");
    } catch (requestError) {
      const message =
        requestError instanceof Error ? requestError.message : "매직 입력 처리 중 오류가 발생했습니다.";
      setMagicMessage(message);
    } finally {
      setSubmittingMagic(false);
    }
  }

  async function handleIdeaVaultSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setIdeaVaultMessage("");

    if (!ideaVaultText.trim()) {
      setIdeaVaultMessage("아이디어 문장을 한 줄 이상 입력해 주세요.");
      return;
    }

    setIdeaVaultSubmitting(true);
    try {
      const response = await ingestIdeaVault({
        raw_text: ideaVaultText,
        batch_size: 20,
      });
      setIdeaVaultText("");
      setIdeaVaultMessage(
        `적재 완료: 승인 ${response.accepted_count}건 / 제외 ${response.rejected_count}건 (pending=${response.pending_count})`,
      );
      const latestStats = await fetchIdeaVaultStats();
      setIdeaVaultStats(latestStats);
    } catch (requestError) {
      const message =
        requestError instanceof Error ? requestError.message : "아이디어 창고 적재 중 오류가 발생했습니다.";
      setIdeaVaultMessage(message);
    } finally {
      setIdeaVaultSubmitting(false);
    }
  }

  if (loading) {
    return (
      <section className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
        <p className="text-sm text-slate-600">Dashboard를 준비하는 중입니다...</p>
      </section>
    );
  }

  if (loadingError) {
    return (
      <section className="rounded-2xl border border-rose-200 bg-rose-50 p-6 shadow-sm">
        <p className="text-sm text-rose-700">{loadingError}</p>
      </section>
    );
  }

  if (!onboarding?.completed) {
    return (
      <div className="space-y-4">
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <h1 className="font-[family-name:var(--font-heading)] text-2xl font-semibold tracking-tight">
            Onboarding Wizard
          </h1>
          <p className="mt-1 text-sm text-slate-600">
            5단계 설정을 완료하면 매직 인풋만으로 예약 발행을 시작할 수 있습니다.
          </p>
          <div className="mt-4 grid gap-2 sm:grid-cols-5">
            {stepTitles.map((title, index) => {
              const active = step === index;
              const passed = step > index;
              return (
                <div
                  key={title}
                  className={`rounded-xl border px-3 py-2 text-sm ${active
                      ? "border-slate-800 bg-slate-900 text-white"
                      : passed
                        ? "border-emerald-300 bg-emerald-50 text-emerald-700"
                        : "border-slate-200 bg-slate-50 text-slate-500"
                    }`}
                >
                  {title}
                </div>
              );
            })}
          </div>
        </section>

        {step === 0 && (
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
              Step 0. Zero-Config Router
            </h2>
            <p className="mt-1 text-sm text-slate-600">
              API 키를 넣고 전략을 선택하면 파서/품질/보이스 모델이 자동 배정되고 1편당 예상 원가가 즉시 계산됩니다.
            </p>

            <div className="mt-4 inline-flex rounded-full border border-slate-300 p-1">
              <button
                type="button"
                onClick={() => setStrategyMode("cost")}
                className={`rounded-full px-4 py-1 text-sm font-medium transition ${strategyMode === "cost"
                    ? "bg-slate-900 text-white"
                    : "text-slate-700 hover:bg-slate-100"
                  }`}
              >
                ⚖️ 가성비 우선
              </button>
              <button
                type="button"
                onClick={() => setStrategyMode("quality")}
                className={`rounded-full px-4 py-1 text-sm font-medium transition ${strategyMode === "quality"
                    ? "bg-slate-900 text-white"
                    : "text-slate-700 hover:bg-slate-100"
                  }`}
              >
                💎 품질 우선
              </button>
            </div>

            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              {Array.from(
                new Set(
                  textModelMatrix
                    .map((item) => (typeof item.key_id === "string" ? item.key_id : ""))
                    .filter((value) => value.length > 0),
                ),
              ).map((keyId) => (
                <label key={keyId} className="block">
                  <span className="mb-1 block text-sm font-medium text-slate-700">
                    {keyId.toUpperCase()} API Key
                  </span>
                  <input
                    type="password"
                    value={textApiKeys[keyId] || ""}
                    onChange={(event) => handleTextKeyChange(keyId, event.target.value)}
                    className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                    placeholder={textApiMasks[keyId] ? `${textApiMasks[keyId]} (저장됨)` : "선택 입력"}
                  />
                </label>
              ))}
            </div>

            <div className="mt-4 grid gap-3 rounded-xl border border-slate-200 bg-slate-50 p-4 sm:grid-cols-2">
              <label className="block">
                <span className="mb-1 block text-sm font-medium text-slate-700">이미지 엔진</span>
                <select
                  value={imageEngine}
                  onChange={(event) => setImageEngine(event.target.value)}
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                >
                  {imageModelMatrix.map((item, index) => {
                    const engineId =
                      typeof item.engine_id === "string" ? item.engine_id : `engine-${index}`;
                    const label =
                      typeof item.label === "string" ? item.label : engineId;
                    return (
                      <option key={engineId} value={engineId}>
                        {label}
                      </option>
                    );
                  })}
                </select>
              </label>
              <label className="block">
                <span className="mb-1 block text-sm font-medium text-slate-700">이미지/포스트 수</span>
                <input
                  type="number"
                  min={0}
                  max={4}
                  value={imagesPerPost}
                  onChange={(event) =>
                    setImagesPerPost(Math.max(0, Math.min(4, Number(event.target.value))))
                  }
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                />
              </label>
              <label className="flex items-center gap-2 sm:col-span-2">
                <input
                  type="checkbox"
                  checked={imageEnabled}
                  onChange={(event) => setImageEnabled(event.target.checked)}
                />
                <span className="text-sm text-slate-700">이미지 엔진 활성화</span>
              </label>
            </div>

            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              {Array.from(
                new Set(
                  imageModelMatrix
                    .map((item) => (typeof item.key_id === "string" ? item.key_id : ""))
                    .filter((value) => value.length > 0),
                ),
              ).map((keyId) => (
                <label key={keyId} className="block">
                  <span className="mb-1 block text-sm font-medium text-slate-700">
                    {keyId.toUpperCase()} Key
                  </span>
                  <input
                    type="password"
                    value={imageApiKeys[keyId] || ""}
                    onChange={(event) => handleImageKeyChange(keyId, event.target.value)}
                    className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                    placeholder={imageApiMasks[keyId] ? `${imageApiMasks[keyId]} (저장됨)` : "선택 입력"}
                  />
                </label>
              ))}
            </div>

            <div className="mt-4 rounded-xl border border-slate-200 bg-white p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold text-slate-800">실시간 견적서</h3>
                {routerLoading && <span className="text-xs text-slate-500">계산 중...</span>}
              </div>
              <div className="mt-2 grid gap-2 text-sm sm:grid-cols-2">
                <p>
                  예상 원가(1편):{" "}
                  <strong>{formatKrw(routerQuote?.estimate.total_cost_krw || 0)}원</strong>
                </p>
                <p>
                  예상 품질: <strong>{routerQuote?.estimate.quality_score || 0}점</strong>
                </p>
                <p className="sm:col-span-2">
                  모델 배정: Parser <strong>{parserModelLabel}</strong> / Step1{" "}
                  <strong>{qualityModelLabel}</strong> / Step2 <strong>{voiceModelLabel}</strong>
                </p>
              </div>
            </div>

            <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50 p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-slate-800">네이버 블로그 연동</p>
                  <p className="text-xs text-slate-600">
                    상태: {naverStatus?.connected ? "연결됨" : "미연결"}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={handleNaverConnect}
                  disabled={naverConnecting}
                  className="rounded-full bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-emerald-500 disabled:opacity-50"
                >
                  {naverConnecting ? "팝업 실행 중..." : "🟢 네이버 연동 시작"}
                </button>
              </div>
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={handleSaveRouterStep}
                disabled={routerSaving}
                className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50"
              >
                {routerSaving ? "저장 중..." : "Step 0 저장 후 다음"}
              </button>
            </div>

            {routerMessage && (
              <p className="mt-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">
                {routerMessage}
              </p>
            )}
          </section>
        )}

        {step === 1 && (
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">Step 1. Persona Lab</h2>
            <p className="mt-1 text-sm text-slate-600">5차원 슬라이더로 작성 성향을 조정합니다.</p>
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              <label className="block">
                <span className="mb-1 block text-sm font-medium text-slate-700">Persona</span>
                <select
                  value={personaId}
                  onChange={(event) => setPersonaId(event.target.value)}
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                >
                  {PERSONA_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="mb-1 block text-sm font-medium text-slate-700">Interests (comma)</span>
                <input
                  value={interestsText}
                  onChange={(event) => setInterestsText(event.target.value)}
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  placeholder="예) AI 자동화, 카페 브랜딩"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-sm font-medium text-slate-700">Identity</span>
                <input
                  value={identity}
                  onChange={(event) => setIdentity(event.target.value)}
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  placeholder="예) IT 직장인"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-sm font-medium text-slate-700">Target Audience</span>
                <input
                  value={targetAudience}
                  onChange={(event) => setTargetAudience(event.target.value)}
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  placeholder="예) 20대 개발 입문자"
                />
              </label>
              <label className="block sm:col-span-2">
                <span className="mb-1 block text-sm font-medium text-slate-700">Tone Hint</span>
                <input
                  value={toneHint}
                  onChange={(event) => setToneHint(event.target.value)}
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  placeholder="예) 공감형이지만 구조적"
                />
              </label>
            </div>

            <div className="mt-4 grid gap-3">
              <label className="block rounded-xl border border-slate-200 bg-slate-50 p-3">
                <div className="flex items-center justify-between text-sm">
                  <span>구조 (Bottom-up ↔ Top-down)</span>
                  <span className="font-medium">{structureScore}</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={structureScore}
                  onChange={(event) => setStructureScore(Number(event.target.value))}
                  className="mt-2 w-full"
                />
              </label>
              <label className="block rounded-xl border border-slate-200 bg-slate-50 p-3">
                <div className="flex items-center justify-between text-sm">
                  <span>근거 (Subjective ↔ Objective)</span>
                  <span className="font-medium">{evidenceScore}</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={evidenceScore}
                  onChange={(event) => setEvidenceScore(Number(event.target.value))}
                  className="mt-2 w-full"
                />
              </label>
              <label className="block rounded-xl border border-slate-200 bg-slate-50 p-3">
                <div className="flex items-center justify-between text-sm">
                  <span>거리 (Authoritative ↔ Inspiring)</span>
                  <span className="font-medium">{distanceScore}</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={distanceScore}
                  onChange={(event) => setDistanceScore(Number(event.target.value))}
                  className="mt-2 w-full"
                />
              </label>
              <label className="block rounded-xl border border-slate-200 bg-slate-50 p-3">
                <div className="flex items-center justify-between text-sm">
                  <span>비판 (Avoidant ↔ Direct)</span>
                  <span className="font-medium">{criticismScore}</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={criticismScore}
                  onChange={(event) => setCriticismScore(Number(event.target.value))}
                  className="mt-2 w-full"
                />
              </label>
              <label className="block rounded-xl border border-slate-200 bg-slate-50 p-3">
                <div className="flex items-center justify-between text-sm">
                  <span>밀도 (Light ↔ Dense)</span>
                  <span className="font-medium">{densityScore}</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={densityScore}
                  onChange={(event) => setDensityScore(Number(event.target.value))}
                  className="mt-2 w-full"
                />
              </label>
              <label className="block rounded-xl border border-slate-200 bg-slate-50 p-3">
                <div className="flex items-center justify-between text-sm">
                  <span>스타일 반영 강도 (권장 30~45)</span>
                  <span className="font-medium">{styleStrength}</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={styleStrength}
                  onChange={(event) => setStyleStrength(Number(event.target.value))}
                  className="mt-2 w-full"
                />
              </label>
            </div>

            <p className="mt-3 text-xs text-slate-600">
              현재 스타일: 구조 {sliderLabel(structureScore, ["Bottom-up", "Balanced", "Top-down"])} / 근거{" "}
              {sliderLabel(evidenceScore, ["Subjective", "Balanced", "Objective"])} / 거리{" "}
              {sliderLabel(distanceScore, ["Authoritative", "Peer", "Inspiring"])}
            </p>

            <div className="mt-4 flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={handleSavePersonaStep}
                disabled={saving}
                className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50"
              >
                {saving ? "저장 중..." : "Step 1 저장 후 다음"}
              </button>
            </div>
          </section>
        )}

        {step === 2 && (
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">Step 2. Category Sync</h2>
            <p className="mt-1 text-sm text-slate-600">
              추천 카테고리를 참고해 네이버 카테고리명을 동기화하세요. fallback 카테고리{" "}
              <strong>&quot;다양한 생각&quot;</strong>은 반드시 유지됩니다.
            </p>
            <Image
              src="/assets/placeholder_category_guide.gif"
              alt="카테고리 생성 가이드"
              width={1280}
              height={400}
              unoptimized
              className="mt-4 h-28 w-full rounded-xl border border-slate-200 object-cover sm:h-40"
            />
            <div className="mt-4 flex flex-wrap gap-2">
              {recommendedCategories.map((categoryName) => (
                <span
                  key={categoryName}
                  className="rounded-full border border-emerald-300 bg-emerald-50 px-3 py-1 text-xs text-emerald-700"
                >
                  {categoryName}
                </span>
              ))}
            </div>
            <label className="mt-4 block">
              <span className="mb-1 block text-sm font-medium text-slate-700">사용할 카테고리 (comma)</span>
              <input
                value={categoriesText}
                onChange={(event) => setCategoriesText(event.target.value)}
                className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                placeholder="예) AI 자동화, 생산성 팁, 다양한 생각"
              />
            </label>
            <label className="mt-3 block">
              <span className="mb-1 block text-sm font-medium text-slate-700">Fallback Category</span>
              <input
                value={fallbackCategory}
                onChange={(event) => setFallbackCategory(event.target.value)}
                className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
              />
            </label>
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => setStep(1)}
                className="rounded-full border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-500"
              >
                이전 단계
              </button>
              <button
                type="button"
                onClick={handleSaveCategoryStep}
                disabled={saving}
                className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50"
              >
                {saving ? "저장 중..." : "Step 2 저장 후 다음"}
              </button>
            </div>
          </section>
        )}

        {step === 3 && (
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
              Step 3. Schedule & Ratio
            </h2>
            <p className="mt-1 text-sm text-slate-600">
              어뷰징 위험을 줄이기 위해 하루 3~5편을 권장합니다. 총 발행량과 Idea Vault 사용량을 먼저 정한 뒤,
              남은 트렌드 슬롯을 카테고리별로 배분하세요.
            </p>

            <label className="mt-4 block rounded-xl border border-slate-200 bg-slate-50 p-3">
              <div className="flex items-center justify-between text-sm">
                <span>하루 총 발행량</span>
                <span className="font-semibold">{dailyPostsTarget}편</span>
              </div>
              <input
                type="range"
                min={3}
                max={5}
                value={dailyPostsTarget}
                onChange={(event) => handleDailyTargetChange(Number(event.target.value))}
                className="mt-2 w-full"
              />
            </label>

            <label className="mt-3 block rounded-xl border border-slate-200 bg-slate-50 p-3">
              <div className="flex items-center justify-between text-sm">
                <span>창고 아이디어(Idea Vault) 하루 사용량</span>
                <span className="font-semibold">{ideaVaultDailyQuota}편</span>
              </div>
              <input
                type="range"
                min={0}
                max={dailyPostsTarget}
                value={ideaVaultDailyQuota}
                onChange={(event) => handleIdeaVaultQuotaChange(Number(event.target.value))}
                className="mt-2 w-full"
              />
              <p className="mt-1 text-xs text-slate-600">
                남은 트렌드 슬롯: <strong>{trendDailyTarget}</strong>편
              </p>
            </label>

            <div className="mt-4 rounded-xl border border-slate-200">
              <div className="grid grid-cols-12 border-b border-slate-200 bg-slate-50 px-3 py-2 text-xs font-medium text-slate-600">
                <div className="col-span-5">Category</div>
                <div className="col-span-4">Topic Mode</div>
                <div className="col-span-3">할당량</div>
              </div>
              <div className="divide-y divide-slate-200">
                {categoryAllocations.map((item, index) => (
                  <div key={item.category} className="grid grid-cols-12 items-center gap-2 px-3 py-2">
                    <div className="col-span-5 text-sm text-slate-800">{item.category}</div>
                    <div className="col-span-4">
                      <select
                        value={item.topic_mode}
                        onChange={(event) =>
                          handleAllocationChange(index, { topic_mode: event.target.value })
                        }
                        className="w-full rounded-lg border border-slate-300 px-2 py-1 text-xs"
                      >
                        {TOPIC_OPTIONS.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="col-span-3">
                      <input
                        type="number"
                        min={0}
                        max={5}
                        value={item.count}
                        onChange={(event) =>
                          handleAllocationChange(index, { count: Number(event.target.value) })
                        }
                        className="w-full rounded-lg border border-slate-300 px-2 py-1 text-sm"
                      />
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm text-slate-600">
                현재 트렌드 할당 합계: <strong>{allocationTotal}</strong> / 목표 <strong>{trendDailyTarget}</strong>
              </p>
              <button
                type="button"
                onClick={() =>
                  setCategoryAllocations(
                    normalizeAllocations(
                      categoryAllocations.map((item) => item.category),
                      trendDailyTarget,
                      [],
                    ),
                  )
                }
                className="rounded-full border border-slate-300 px-3 py-1 text-xs font-medium text-slate-700 transition hover:border-slate-500"
              >
                균등 분배 자동 맞춤
              </button>
            </div>

            {allocationTotal !== trendDailyTarget && (
              <p className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
                할당량 합계가 목표 트렌드 발행량과 다릅니다. 저장 시 자동 보정되지만, 원하는 비율로 직접 맞추는 것을 권장합니다.
              </p>
            )}

            {trendDailyTarget <= 0 && (
              <p className="mt-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700">
                오늘 발행량이 모두 Idea Vault로 배정되었습니다. 트렌드 카테고리 배분은 0으로 저장됩니다.
              </p>
            )}

            <div className="mt-4 flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => setStep(2)}
                className="rounded-full border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-500"
              >
                이전 단계
              </button>
              <button
                type="button"
                onClick={handleSaveScheduleStep}
                disabled={saving || (trendDailyTarget > 0 && categoryAllocations.length === 0)}
                className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50"
              >
                {saving ? "저장 중..." : "Step 3 저장 후 다음"}
              </button>
            </div>
          </section>
        )}

        {step === 4 && (
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">Step 4. Telegram Setup</h2>
            <p className="mt-1 text-sm text-slate-600">
              22:30 요약 보고와 치명 에러 알림을 받을 수 있게 텔레그램을 연결하세요.
            </p>
            <Image
              src="/assets/placeholder_telegram_guide.gif"
              alt="텔레그램 연결 가이드"
              width={1280}
              height={400}
              unoptimized
              className="mt-4 h-28 w-full rounded-xl border border-slate-200 object-cover sm:h-40"
            />
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <label className="block">
                <span className="mb-1 block text-sm font-medium text-slate-700">Bot Token</span>
                <input
                  value={botToken}
                  onChange={(event) => setBotToken(event.target.value)}
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  placeholder="123456:ABC-..."
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-sm font-medium text-slate-700">Chat ID</span>
                <input
                  value={chatId}
                  onChange={(event) => setChatId(event.target.value)}
                  className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                  placeholder="123456789"
                />
              </label>
            </div>
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => setStep(3)}
                className="rounded-full border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-500"
              >
                이전 단계
              </button>
              <button
                type="button"
                onClick={handleTestTelegram}
                disabled={saving}
                className="rounded-full border border-slate-400 px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-600 disabled:opacity-50"
              >
                {saving ? "테스트 중..." : "테스트 발송"}
              </button>
              <button
                type="button"
                onClick={handleCompleteOnboarding}
                disabled={saving || !telegramVerified}
                className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50"
              >
                온보딩 완료
              </button>
            </div>
          </section>
        )}

        {stepMessage && (
          <p className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">
            {stepMessage}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h1 className="font-[family-name:var(--font-heading)] text-2xl font-semibold tracking-tight">
            Workspace
          </h1>
          <div className="inline-flex rounded-full border border-slate-300 p-1">
            <button
              type="button"
              onClick={() => setWorkspaceTab("magic")}
              className={`rounded-full px-4 py-1 text-sm font-medium transition ${workspaceTab === "magic"
                  ? "bg-slate-900 text-white"
                  : "text-slate-700 hover:bg-slate-100"
                }`}
            >
              Magic Input
            </button>
            <button
              type="button"
              onClick={() => setWorkspaceTab("vault")}
              className={`rounded-full px-4 py-1 text-sm font-medium transition ${workspaceTab === "vault"
                  ? "bg-slate-900 text-white"
                  : "text-slate-700 hover:bg-slate-100"
                }`}
            >
              Idea Vault
            </button>
          </div>
        </div>

        {workspaceTab === "magic" && (
          <div className="mt-4 space-y-3">
            <p className="text-sm text-slate-600">
              자연어 문장 1개만 입력하면 title/keywords/persona를 자동 추출해 예약 큐에 넣습니다.
            </p>
            <form onSubmit={handleMagicSubmit} className="space-y-3">
              <textarea
                value={instruction}
                onChange={(event) => setInstruction(event.target.value)}
                className="min-h-36 w-full rounded-2xl border border-slate-300 px-4 py-3 text-sm outline-none transition focus:border-slate-600"
                placeholder="예) 내일 아침 9시에 스벅 아아 리뷰 올려줘, IT전문가 톤으로."
              />

              <button
                type="button"
                onClick={() => setAdvancedOpen((previous) => !previous)}
                className="rounded-full border border-slate-300 px-4 py-2 text-xs font-medium text-slate-700 transition hover:border-slate-500"
              >
                {advancedOpen ? "고급 설정 닫기" : "고급 설정 열기"}
              </button>

              {advancedOpen && (
                <div className="grid gap-3 rounded-2xl border border-slate-200 bg-slate-50 p-4 sm:grid-cols-2">
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium text-slate-600">Persona</span>
                    <select
                      value={advancedPersonaId}
                      onChange={(event) => setAdvancedPersonaId(event.target.value)}
                      className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                    >
                      {PERSONA_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium text-slate-600">Topic</span>
                    <select
                      value={advancedTopicMode}
                      onChange={(event) => setAdvancedTopicMode(event.target.value)}
                      className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                    >
                      {TOPIC_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium text-slate-600">Scheduled At</span>
                    <input
                      type="datetime-local"
                      value={advancedScheduleAt}
                      onChange={(event) => setAdvancedScheduleAt(event.target.value)}
                      className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                    />
                  </label>
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium text-slate-600">Keywords Override</span>
                    <input
                      value={advancedKeywordsText}
                      onChange={(event) => setAdvancedKeywordsText(event.target.value)}
                      className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                      placeholder="예) 자동화, SEO, 워크플로우"
                    />
                  </label>
                  <label className="block sm:col-span-2">
                    <span className="mb-1 block text-xs font-medium text-slate-600">Category Override</span>
                    <input
                      value={advancedCategory}
                      onChange={(event) => setAdvancedCategory(event.target.value)}
                      className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
                      placeholder="비워두면 Topic 기준 자동 카테고리"
                    />
                  </label>
                </div>
              )}

              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="submit"
                  disabled={submittingMagic}
                  className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50"
                >
                  {submittingMagic ? "등록 중..." : "매직 예약 생성"}
                </button>
              </div>
            </form>
            {magicMessage && (
              <p className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">
                {magicMessage}
              </p>
            )}
          </div>
        )}

        {workspaceTab === "vault" && (
          <div className="mt-4 space-y-3">
            <p className="text-sm text-slate-600">
              100~200줄 아이디어를 한 번에 넣어 대량 적재합니다. 유효 문장만 걸러서 카테고리를 자동 분류합니다.
            </p>
            <form onSubmit={handleIdeaVaultSubmit} className="space-y-3">
              <textarea
                value={ideaVaultText}
                onChange={(event) => setIdeaVaultText(event.target.value)}
                className="min-h-72 w-full rounded-2xl border border-slate-300 px-4 py-3 text-sm outline-none transition focus:border-slate-600"
                placeholder="예) 내일 카페 아침 매출을 올리는 오픈 루틴 정리&#10;예) 자동화 도구로 블로그 글감 수집 시간 절약한 방법&#10;..."
              />
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="submit"
                  disabled={ideaVaultSubmitting}
                  className="rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:opacity-50"
                >
                  {ideaVaultSubmitting ? "적재 중..." : "아이디어 창고 적재"}
                </button>
                {ideaVaultStats && (
                  <span className="rounded-full border border-slate-300 px-3 py-1 text-xs text-slate-700">
                    pending {ideaVaultStats.pending} / queued {ideaVaultStats.queued} / consumed{" "}
                    {ideaVaultStats.consumed}
                  </span>
                )}
              </div>
            </form>
            {ideaVaultMessage && (
              <p className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">
                {ideaVaultMessage}
              </p>
            )}
          </div>
        )}
      </section>

      <div className="grid gap-4 lg:grid-cols-6">
        <div className="lg:col-span-3">
          <HealthWidget />
        </div>
        <div className="lg:col-span-3">
          <MetricsSummary />
        </div>
        <div className="lg:col-span-6">
          <AIToggleSummary />
        </div>
      </div>
    </div>
  );
}
