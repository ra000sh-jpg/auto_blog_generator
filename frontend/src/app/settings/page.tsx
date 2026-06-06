"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Clock3,
  KeyRound,
  Link2,
  MessageCircle,
  Settings2,
  ShieldCheck,
} from "lucide-react";

import EngineSettingsCard from "@/components/settings/engine-settings-card";
import AllocationSettingsCard from "@/components/settings/allocation-settings-card";
import {
  fetchNaverConnectStatus,
  fetchOnboardingStatus,
  fetchRouterSettings,
  saveRouterSettings,
  startNaverConnect,
  verifyTelegramLink,
  verifyTelegramToken,
  type NaverConnectStatusResponse,
  type OnboardingStatusResponse,
  type RouterSettingsPayload,
  type RouterSettingsResponse,
} from "@/lib/api";
import { formatKrw } from "@/lib/utils/formatters";
import { isTelegramBotTokenFormat } from "@/lib/utils/telegram";

type Feedback = {
  type: "success" | "error" | "info";
  text: string;
} | null;

type StatusTone = "ok" | "warn" | "muted";

const PRIMARY_TEXT_KEYS = ["deepseek", "qwen", "groq", "nvidia", "gemini", "openai"];

export default function SettingsPage() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [onboardingData, setOnboardingData] = useState<OnboardingStatusResponse | null>(null);
  const [routerData, setRouterData] = useState<RouterSettingsResponse | null>(null);
  const [naverStatus, setNaverStatus] = useState<NaverConnectStatusResponse | null>(null);

  useEffect(() => {
    let isMounted = true;

    async function loadConfig() {
      try {
        const [onboardingResult, routerResult, naverResult] = await Promise.allSettled([
          fetchOnboardingStatus(),
          fetchRouterSettings(),
          fetchNaverConnectStatus(),
        ]);
        if (!isMounted) return;

        const errors: string[] = [];

        if (onboardingResult.status === "fulfilled") {
          setOnboardingData(onboardingResult.value);
        } else {
          errors.push(
            `onboarding: ${onboardingResult.reason instanceof Error ? onboardingResult.reason.message : "요청 실패"}`,
          );
        }

        if (routerResult.status === "fulfilled") {
          setRouterData(routerResult.value);
        } else {
          errors.push(`router: ${routerResult.reason instanceof Error ? routerResult.reason.message : "요청 실패"}`);
        }

        if (naverResult.status === "fulfilled") {
          setNaverStatus(naverResult.value);
        } else {
          errors.push(`naver: ${naverResult.reason instanceof Error ? naverResult.reason.message : "요청 실패"}`);
        }

        setError(errors.length > 0 ? `일부 설정을 불러오지 못했습니다. ${errors.join(" | ")}` : "");
      } catch (requestError) {
        if (!isMounted) return;
        setError(requestError instanceof Error ? requestError.message : "설정 정보를 불러오지 못했습니다.");
      } finally {
        if (isMounted) setLoading(false);
      }
    }

    loadConfig();
    return () => {
      isMounted = false;
    };
  }, []);

  return (
    <div className="space-y-4">
      <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <p className="text-xs font-semibold uppercase tracking-wide text-teal-700">
          Daily Blog Operations
        </p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-950">설정</h1>
        <p className="mt-1 text-sm leading-6 text-slate-600">
          매일 쓰는 연결 상태와 운영 프리셋만 먼저 보여줍니다. 모델 실험, 채널 확장, 세부 배분은 고급 설정에 접어두었습니다.
        </p>
      </section>

      {loading && (
        <p className="rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-600">
          설정 정보를 불러오는 중입니다...
        </p>
      )}

      {!loading && error && (
        <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </p>
      )}

      {!loading && onboardingData && routerData && (
        <>
          <SettingsOverview
            onboardingData={onboardingData}
            routerData={routerData}
            naverStatus={naverStatus}
          />

          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
            <QuickModelSettingsCard
              routerData={routerData}
              onRouterDataChange={setRouterData}
            />
            <DailyOperationCard onboardingData={onboardingData} />
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <NaverConnectionCard
              initialStatus={naverStatus}
              onStatusChange={setNaverStatus}
            />
            <TelegramCompactCard initialOnboardingStatus={onboardingData} />
          </div>

          <AdvancedSettingsSection
            routerData={routerData}
            onboardingData={onboardingData}
            naverStatus={naverStatus}
          />
        </>
      )}
    </div>
  );
}

function SettingsOverview({
  onboardingData,
  routerData,
  naverStatus,
}: {
  onboardingData: OnboardingStatusResponse;
  routerData: RouterSettingsResponse;
  naverStatus: NaverConnectStatusResponse | null;
}) {
  const textKeyMasks = routerData.settings.text_api_keys_masked || {};
  const savedTextKeys = Object.values(textKeyMasks).filter(Boolean).length;
  const telegramOk = Boolean(onboardingData.telegram_configured && onboardingData.telegram_chat_id);
  const naverOk = Boolean(naverStatus?.connected && naverStatus.exists);
  const dailyTargetOk = onboardingData.daily_posts_target === 3;

  return (
    <section className="grid gap-3 md:grid-cols-4">
      <StatusTile
        icon={<KeyRound className="h-4 w-4" />}
        label="글쓰기 API"
        value={savedTextKeys > 0 ? `${savedTextKeys}개 저장` : "키 필요"}
        sub={routerData.settings.strategy_mode || "cost"}
        tone={savedTextKeys > 0 ? "ok" : "warn"}
      />
      <StatusTile
        icon={<MessageCircle className="h-4 w-4" />}
        label="텔레그램"
        value={telegramOk ? "연결됨" : "연동 필요"}
        sub={telegramOk ? maskMiddle(onboardingData.telegram_chat_id) : "초안 승인용"}
        tone={telegramOk ? "ok" : "warn"}
      />
      <StatusTile
        icon={<Link2 className="h-4 w-4" />}
        label="네이버"
        value={naverOk ? "세션 있음" : "재연동 필요"}
        sub={formatEpoch(naverStatus?.updated_at_epoch)}
        tone={naverOk ? "ok" : "warn"}
      />
      <StatusTile
        icon={<Clock3 className="h-4 w-4" />}
        label="하루 편성"
        value={`${onboardingData.daily_posts_target || 3}편`}
        sub={dailyTargetOk ? "국장/통찰/미장" : "고급 설정 확인"}
        tone={dailyTargetOk ? "ok" : "warn"}
      />
    </section>
  );
}

function QuickModelSettingsCard({
  routerData,
  onRouterDataChange,
}: {
  routerData: RouterSettingsResponse;
  onRouterDataChange: (value: RouterSettingsResponse) => void;
}) {
  const [strategyMode, setStrategyMode] = useState(routerData.settings.strategy_mode || "cost");
  const [textKeys, setTextKeys] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<Feedback>(null);

  const availableKeys = useMemo(() => {
    const fromMatrix = new Set(
      (routerData.matrix.text_models || [])
        .map((item) => String(item.key_id || "").trim().toLowerCase())
        .filter(Boolean),
    );
    const fromMasks = new Set(Object.keys(routerData.settings.text_api_keys_masked || {}));
    const merged = new Set([...fromMatrix, ...fromMasks, ...PRIMARY_TEXT_KEYS.slice(0, 2)]);
    return PRIMARY_TEXT_KEYS.filter((key) => merged.has(key)).slice(0, 4);
  }, [routerData]);

  async function handleSave() {
    setSaving(true);
    setFeedback(null);
    try {
      const saved = await saveRouterSettings(
        buildRouterPayload(routerData, strategyMode, compactRecord(textKeys)),
      );
      setTextKeys({});
      onRouterDataChange(saved);
      setFeedback({ type: "success", text: "글쓰기 API와 전략 설정을 저장했습니다." });
    } catch (requestError) {
      setFeedback({
        type: "error",
        text: requestError instanceof Error ? requestError.message : "API 설정 저장에 실패했습니다.",
      });
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-950">글쓰기 엔진</h2>
          <p className="mt-1 text-sm leading-6 text-slate-600">
            기본 운영에는 전략과 주요 API 키만 확인하면 충분합니다.
          </p>
        </div>
        <StatusBadge tone="muted">월 {formatKrw(routerData.quote.monthly_cost_min_krw || routerData.quote.monthly_cost_krw || 0)}원~</StatusBadge>
      </div>

      <div className="mt-4 grid gap-2 sm:grid-cols-3">
        {[
          { value: "cost", label: "가성비", desc: "월 비용 우선" },
          { value: "balanced", label: "균형", desc: "비용과 품질" },
          { value: "quality", label: "품질", desc: "품질 우선" },
        ].map((option) => {
          const selected = strategyMode === option.value;
          return (
            <button
              key={option.value}
              type="button"
              onClick={() => setStrategyMode(option.value)}
              className={`rounded-lg border px-3 py-2 text-left transition ${
                selected
                  ? "border-teal-300 bg-teal-50 text-teal-900"
                  : "border-slate-200 bg-white text-slate-700 hover:border-slate-300"
              }`}
            >
              <span className="block text-sm font-semibold">{option.label}</span>
              <span className="mt-0.5 block text-xs text-slate-500">{option.desc}</span>
            </button>
          );
        })}
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        {availableKeys.map((keyId) => {
          const mask = routerData.settings.text_api_keys_masked?.[keyId] || "";
          return (
            <label key={keyId} className="block">
              <span className="mb-1 flex items-center justify-between gap-2 text-sm font-medium text-slate-700">
                <span>{providerLabel(keyId)} API Key</span>
                <StatusBadge tone={mask ? "ok" : "warn"}>{mask ? "저장됨" : "선택"}</StatusBadge>
              </span>
              <input
                type="password"
                value={textKeys[keyId] || ""}
                onChange={(event) => setTextKeys((prev) => ({ ...prev, [keyId]: event.target.value }))}
                placeholder={mask ? `${mask} 유지` : "새 키 입력"}
                className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none transition focus:border-teal-500"
              />
            </label>
          );
        })}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={handleSave}
          disabled={saving}
          className="rounded-lg bg-slate-950 px-4 py-2 text-sm font-semibold text-white transition hover:bg-slate-700 disabled:opacity-50"
        >
          {saving ? "저장 중..." : "기본 설정 저장"}
        </button>
        <p className="text-xs text-slate-500">비워둔 키는 기존 값을 유지합니다.</p>
      </div>

      {feedback && <FeedbackMessage feedback={feedback} />}
    </section>
  );
}

function DailyOperationCard({
  onboardingData,
}: {
  onboardingData: OnboardingStatusResponse;
}) {
  const dailyTarget = onboardingData.daily_posts_target || 3;
  const insightSource = onboardingData.idea_vault_daily_quota > 0 ? "아이디어 창고 포함" : "시장/통찰 자동 편성";

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-950">하루 운영 프리셋</h2>
          <p className="mt-1 text-sm leading-6 text-slate-600">
            현재 목표는 하루 {dailyTarget}편입니다.
          </p>
        </div>
        <StatusBadge tone={dailyTarget === 3 ? "ok" : "warn"}>
          {dailyTarget === 3 ? "권장값" : "확인"}
        </StatusBadge>
      </div>

      <div className="mt-4 space-y-3">
        <ScheduleLine label="국장 전" time="08:10" body="전일 미국장, 환율, 금리, 코인 심리 연결" />
        <ScheduleLine label="통찰형" time="18:30" body={insightSource} />
        <ScheduleLine label="미장 전" time="개장 전" body="아시아 마감, 미국 선물, 매크로 뉴스 연결" />
      </div>

      <div className="mt-4 rounded-lg bg-slate-50 p-3 text-xs leading-5 text-slate-600">
        세부 카테고리 비율과 이미지 장수는 고급 설정에서만 조정합니다. 평소에는 이 프리셋을 그대로 두는 편이 안정적입니다.
      </div>
    </section>
  );
}

function NaverConnectionCard({
  initialStatus,
  onStatusChange,
}: {
  initialStatus: NaverConnectStatusResponse | null;
  onStatusChange: (value: NaverConnectStatusResponse | null) => void;
}) {
  const [status, setStatus] = useState(initialStatus);
  const [connecting, setConnecting] = useState(false);
  const [feedback, setFeedback] = useState<Feedback>(null);
  const connected = Boolean(status?.connected && status.exists);

  async function handleConnect() {
    setConnecting(true);
    setFeedback(null);
    try {
      const response = await startNaverConnect({ timeout_sec: 300 });
      const refreshed = await fetchNaverConnectStatus();
      setStatus(refreshed);
      onStatusChange(refreshed);
      setFeedback({ type: response.connected ? "success" : "info", text: response.message });
    } catch (requestError) {
      setFeedback({
        type: "error",
        text: requestError instanceof Error ? requestError.message : "네이버 연동 실행에 실패했습니다.",
      });
    } finally {
      setConnecting(false);
    }
  }

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-950">네이버 블로그</h2>
          <p className="mt-1 text-sm leading-6 text-slate-600">
            임시저장 자동화에 쓰는 브라우저 로그인 세션입니다.
          </p>
        </div>
        <StatusBadge tone={connected ? "ok" : "warn"}>{connected ? "연결됨" : "확인 필요"}</StatusBadge>
      </div>

      <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
        <InfoItem label="세션 파일" value={status?.exists ? "있음" : "없음"} />
        <InfoItem label="마지막 갱신" value={formatEpoch(status?.updated_at_epoch)} />
      </dl>

      <button
        type="button"
        onClick={handleConnect}
        disabled={connecting}
        className="mt-4 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-emerald-500 disabled:opacity-50"
      >
        {connecting ? "브라우저 확인 중..." : "네이버 재연동"}
      </button>

      {feedback && <FeedbackMessage feedback={feedback} />}
    </section>
  );
}

function TelegramCompactCard({
  initialOnboardingStatus,
}: {
  initialOnboardingStatus: OnboardingStatusResponse;
}) {
  const [botToken, setBotToken] = useState("");
  const [authCode, setAuthCode] = useState("");
  const [authCommand, setAuthCommand] = useState("");
  const [deepLink, setDeepLink] = useState("");
  const [chatId, setChatId] = useState(initialOnboardingStatus.telegram_chat_id || "");
  const [linked, setLinked] = useState(
    Boolean(initialOnboardingStatus.telegram_configured && initialOnboardingStatus.telegram_chat_id),
  );
  const [verifying, setVerifying] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [feedback, setFeedback] = useState<Feedback>(null);

  async function handleVerifyToken() {
    const normalizedToken = botToken.trim();
    if (!isTelegramBotTokenFormat(normalizedToken)) {
      setFeedback({ type: "error", text: "Bot Token 형식이 올바르지 않습니다." });
      return;
    }

    setVerifying(true);
    setFeedback(null);
    try {
      const response = await verifyTelegramToken({ bot_token: normalizedToken });
      setAuthCode(response.auth_code || "");
      setAuthCommand(response.auth_command || "");
      setDeepLink(response.deep_link || "");
      setLinked(false);
      setChatId("");
      setFeedback({ type: "success", text: "토큰 확인 완료. 인증 명령을 봇에게 보내주세요." });
    } catch (requestError) {
      setFeedback({
        type: "error",
        text: requestError instanceof Error ? requestError.message : "토큰 확인에 실패했습니다.",
      });
    } finally {
      setVerifying(false);
    }
  }

  async function handleCopyCommand() {
    if (!authCommand) return;
    try {
      await navigator.clipboard.writeText(authCommand);
      setFeedback({ type: "success", text: "인증 명령을 복사했습니다." });
    } catch {
      setFeedback({ type: "error", text: "복사에 실패했습니다. 명령을 직접 복사해 주세요." });
    }
  }

  async function handleVerifyLink() {
    if (!authCode) {
      setFeedback({ type: "error", text: "먼저 토큰 확인을 완료해 주세요." });
      return;
    }

    setConfirming(true);
    setFeedback(null);
    try {
      const response = await verifyTelegramLink({ auth_code: authCode });
      setLinked(Boolean(response.success));
      setChatId(response.chat_id || "");
      setFeedback({ type: "success", text: "텔레그램 승인 채팅이 연결되었습니다." });
    } catch (requestError) {
      setFeedback({
        type: "error",
        text: requestError instanceof Error ? requestError.message : "연동 확인에 실패했습니다.",
      });
    } finally {
      setConfirming(false);
    }
  }

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-950">텔레그램 승인</h2>
          <p className="mt-1 text-sm leading-6 text-slate-600">
            초안 승인과 수정본입력을 받을 개인 채팅입니다.
          </p>
        </div>
        <StatusBadge tone={linked ? "ok" : "warn"}>{linked ? "연결됨" : "연동 필요"}</StatusBadge>
      </div>

      {chatId && (
        <p className="mt-3 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">
          Chat ID: {maskMiddle(chatId)}
        </p>
      )}

      <div className="mt-4 space-y-3">
        <label className="block">
          <span className="mb-1 block text-sm font-medium text-slate-700">새 Bot Token</span>
          <input
            type="password"
            value={botToken}
            onChange={(event) => setBotToken(event.target.value)}
            placeholder={initialOnboardingStatus.telegram_bot_token ? "재연동할 때만 새 토큰 입력" : "1234567890:ABCdefGHIjklMNO"}
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none transition focus:border-teal-500"
          />
        </label>

        <button
          type="button"
          onClick={handleVerifyToken}
          disabled={verifying || !botToken}
          className="rounded-lg bg-slate-950 px-4 py-2 text-sm font-semibold text-white transition hover:bg-slate-700 disabled:opacity-50"
        >
          {verifying ? "확인 중..." : "토큰 확인"}
        </button>

        {authCommand && (
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
            <p className="text-xs font-semibold text-slate-700">봇에게 보낼 명령</p>
            <p className="mt-2 break-all rounded border border-dashed border-slate-300 bg-white p-2 text-xs text-slate-700">
              {authCommand}
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              <button
                type="button"
                onClick={handleCopyCommand}
                className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700"
              >
                명령 복사
              </button>
              {deepLink && (
                <a
                  href={deepLink}
                  target="_blank"
                  rel="noreferrer"
                  className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700"
                >
                  내 봇 열기
                </a>
              )}
              <button
                type="button"
                onClick={handleVerifyLink}
                disabled={confirming}
                className="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
              >
                {confirming ? "확인 중..." : "연동 확인"}
              </button>
            </div>
          </div>
        )}
      </div>

      {feedback && <FeedbackMessage feedback={feedback} />}
    </section>
  );
}

function AdvancedSettingsSection({
  routerData,
  onboardingData,
  naverStatus,
}: {
  routerData: RouterSettingsResponse;
  onboardingData: OnboardingStatusResponse;
  naverStatus: NaverConnectStatusResponse | null;
}) {
  return (
    <section className="space-y-3">
      <div className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex items-start gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-slate-200 bg-slate-50 text-slate-600">
            <Settings2 className="h-4 w-4" />
          </div>
          <div>
            <h2 className="text-lg font-semibold text-slate-950">고급 설정</h2>
            <p className="mt-1 text-sm leading-6 text-slate-600">
              평소에는 닫아두는 영역입니다. 모델 라우터, 이미지/VLM, 카테고리 배분, 멀티채널 실험을 수정할 때만 열어주세요.
            </p>
          </div>
        </div>
      </div>

      <AdvancedDetails title="AI·이미지 라우터" body="모델별 비용, 이미지 엔진, VLM 평가, 챔피언 후보를 조정합니다.">
        <EngineSettingsCard
          initialRouterSettings={routerData}
          initialNaverStatus={naverStatus}
          categoryAllocations={onboardingData.category_allocations || []}
        />
      </AdvancedDetails>

      <AdvancedDetails title="스케줄·카테고리 배분" body="하루 총량, 아이디어 창고 사용량, 카테고리별 이미지 장수를 세부 조정합니다.">
        <AllocationSettingsCard initialOnboardingStatus={onboardingData} />
      </AdvancedDetails>

      <AdvancedDetails title="멀티채널 관리" body="현재 목표는 네이버 단일 블로그입니다. 티스토리·워드프레스 확장은 보류 상태로 둡니다.">
        <div className="rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm leading-6 text-slate-600">
          멀티채널 기능은 삭제하지 않고 보류했습니다. 1-2주 안정화 뒤 필요성이 확인되면 다시 열고,
          그 전까지는 네이버 블로그 단일 운영에 집중합니다.
        </div>
      </AdvancedDetails>
    </section>
  );
}

function AdvancedDetails({
  title,
  body,
  children,
}: {
  title: string;
  body: string;
  children: ReactNode;
}) {
  const [hasOpened, setHasOpened] = useState(false);

  return (
    <details
      className="group rounded-lg border border-slate-200 bg-white shadow-sm"
      onToggle={(event) => {
        if (event.currentTarget.open) {
          setHasOpened(true);
        }
      }}
    >
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-5 py-4">
        <span>
          <span className="block text-sm font-semibold text-slate-900">{title}</span>
          <span className="mt-0.5 block text-xs text-slate-500">{body}</span>
        </span>
        <ChevronDown className="h-4 w-4 text-slate-400 transition group-open:rotate-180" />
      </summary>
      <div className="border-t border-slate-100 p-4">
        {hasOpened ? children : null}
      </div>
    </details>
  );
}

function StatusTile({
  icon,
  label,
  value,
  sub,
  tone,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  sub: string;
  tone: StatusTone;
}) {
  const toneClass = {
    ok: "border-teal-100 bg-teal-50 text-teal-700",
    warn: "border-amber-100 bg-amber-50 text-amber-700",
    muted: "border-slate-100 bg-slate-50 text-slate-600",
  }[tone];

  return (
    <article className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-start gap-3">
        <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border ${toneClass}`}>
          {icon}
        </div>
        <div className="min-w-0">
          <p className="text-xs font-medium text-slate-500">{label}</p>
          <p className="mt-1 text-lg font-bold leading-none text-slate-950">{value}</p>
          <p className="mt-1 truncate text-xs text-slate-500">{sub}</p>
        </div>
      </div>
    </article>
  );
}

function ScheduleLine({ label, time, body }: { label: string; time: string; body: string }) {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-slate-100 bg-slate-50 px-3 py-2">
      <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-white text-slate-500">
        <Clock3 className="h-3.5 w-3.5" />
      </div>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-sm font-semibold text-slate-900">{label}</p>
          <span className="text-xs text-slate-500">{time}</span>
        </div>
        <p className="mt-0.5 text-xs leading-5 text-slate-500">{body}</p>
      </div>
    </div>
  );
}

function InfoItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-slate-50 px-3 py-2">
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="mt-1 truncate text-sm font-semibold text-slate-900">{value}</dd>
    </div>
  );
}

function FeedbackMessage({ feedback }: { feedback: Exclude<Feedback, null> }) {
  const className = {
    success: "border-emerald-200 bg-emerald-50 text-emerald-700",
    error: "border-rose-200 bg-rose-50 text-rose-700",
    info: "border-slate-200 bg-slate-50 text-slate-700",
  }[feedback.type];

  return <p className={`mt-3 rounded-lg border px-3 py-2 text-sm ${className}`}>{feedback.text}</p>;
}

function StatusBadge({ tone, children }: { tone: StatusTone; children: ReactNode }) {
  const icon = tone === "ok" ? <CheckCircle2 className="h-3.5 w-3.5" /> : tone === "warn" ? <AlertTriangle className="h-3.5 w-3.5" /> : <ShieldCheck className="h-3.5 w-3.5" />;
  const className = {
    ok: "border-teal-200 bg-teal-50 text-teal-700",
    warn: "border-amber-200 bg-amber-50 text-amber-700",
    muted: "border-slate-200 bg-slate-50 text-slate-600",
  }[tone];

  return (
    <span className={`inline-flex items-center gap-1 rounded-lg border px-2 py-0.5 text-xs font-semibold ${className}`}>
      {icon}
      {children}
    </span>
  );
}

function buildRouterPayload(
  routerData: RouterSettingsResponse,
  strategyMode: string,
  textApiKeys: Record<string, string>,
): RouterSettingsPayload {
  const settings = routerData.settings;
  return {
    strategy_mode: strategyMode,
    text_api_keys: textApiKeys,
    image_api_keys: {},
    cost_strict_mode: settings.cost_strict_mode,
    cost_free_only_fallback: settings.cost_free_only_fallback,
    cost_max_fallback_usd_per_1m: settings.cost_max_fallback_usd_per_1m,
    cost_retry_max_retries: settings.cost_retry_max_retries,
    cost_retry_base_delay_sec: settings.cost_retry_base_delay_sec,
    cost_retry_max_delay_sec: settings.cost_retry_max_delay_sec,
    cost_lock_quality_provider: settings.cost_lock_quality_provider,
    image_engine: settings.image_engine,
    image_ai_engine: settings.image_ai_engine,
    image_ai_quota: settings.image_ai_quota as RouterSettingsPayload["image_ai_quota"],
    image_topic_quota_overrides: settings.image_topic_quota_overrides || {},
    traffic_feedback_strong_mode: settings.traffic_feedback_strong_mode,
    image_enabled: settings.image_enabled,
    images_per_post: settings.images_per_post,
    images_per_post_min: settings.images_per_post_min,
    images_per_post_max: settings.images_per_post_max,
    vlm_enabled: settings.vlm_enabled,
    vlm_model: settings.vlm_model,
    vlm_strategy_mode: settings.vlm_strategy_mode,
    vlm_eval_sampling_rate: settings.vlm_eval_sampling_rate,
    vlm_quality_floor: settings.vlm_quality_floor,
    vlm_max_cost_guard_krw: settings.vlm_max_cost_guard_krw,
    challenger_model: routerData.competition?.challenger_model || "",
  };
}

function compactRecord(values: Record<string, string>) {
  return Object.fromEntries(
    Object.entries(values)
      .map(([key, value]) => [key, value.trim()])
      .filter(([, value]) => value.length > 0),
  );
}

function providerLabel(provider: string) {
  const labelMap: Record<string, string> = {
    deepseek: "DeepSeek",
    qwen: "Qwen",
    groq: "Groq",
    nvidia: "NVIDIA",
    gemini: "Gemini",
    openai: "OpenAI",
  };
  return labelMap[provider] || provider.toUpperCase();
}

function maskMiddle(value: string) {
  if (!value) return "-";
  if (value.length <= 8) return value;
  return `${value.slice(0, 4)}...${value.slice(-4)}`;
}

function formatEpoch(value?: number) {
  if (!value) return "기록 없음";
  const date = new Date(value * 1000);
  if (Number.isNaN(date.getTime())) return "기록 없음";
  return date.toLocaleString("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
