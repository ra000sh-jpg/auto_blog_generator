"use client";

import { useEffect, useMemo, useState } from "react";
import {
  createChannel,
  deleteChannel,
  distributeSubJobs,
  fetchChannelSettings,
  fetchChannels,
  saveChannelSettings,
  testChannel,
  updateChannel,
  type ChannelItem,
} from "@/lib/api";

type FeedbackState = {
  type: "success" | "error";
  message: string;
} | null;

type CreateFormState = {
  platform: "naver" | "tistory" | "wordpress";
  label: string;
  blogUrl: string;
  personaId: string;
  personaDesc: string;
  publishDelayMinutes: number;
  isMaster: boolean;
  active: boolean;
  authJsonText: string;
};

const DEFAULT_CREATE_FORM: CreateFormState = {
  platform: "naver",
  label: "",
  blogUrl: "",
  personaId: "P1",
  personaDesc: "",
  publishDelayMinutes: 90,
  isMaster: false,
  active: true,
  authJsonText: "{}",
};

export default function ChannelManagerCard() {
  const [loading, setLoading] = useState(true);
  const [channels, setChannels] = useState<ChannelItem[]>([]);
  const [multichannelEnabled, setMultichannelEnabled] = useState(false);

  const [settingsSaving, setSettingsSaving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [testingId, setTestingId] = useState("");
  const [togglingId, setTogglingId] = useState("");
  const [feedback, setFeedback] = useState<FeedbackState>(null);

  const [createForm, setCreateForm] = useState<CreateFormState>(DEFAULT_CREATE_FORM);
  const [distributeJobId, setDistributeJobId] = useState("");
  const [distributeLoading, setDistributeLoading] = useState(false);

  const activeCount = useMemo(
    () => channels.filter((channel) => channel.active).length,
    [channels],
  );

  async function loadData() {
    setLoading(true);
    try {
      const [channelResponse, settingResponse] = await Promise.all([
        fetchChannels(true),
        fetchChannelSettings(),
      ]);
      setChannels(channelResponse.items || []);
      setMultichannelEnabled(Boolean(settingResponse.multichannel_enabled));
    } catch (requestError) {
      const message =
        requestError instanceof Error ? requestError.message : "채널 정보를 불러오지 못했습니다.";
      setFeedback({ type: "error", message });
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadData();
  }, []);

  async function handleSaveSettings() {
    setSettingsSaving(true);
    try {
      const response = await saveChannelSettings({
        multichannel_enabled: multichannelEnabled,
      });
      setMultichannelEnabled(Boolean(response.multichannel_enabled));
      setFeedback({ type: "success", message: "멀티채널 설정이 저장되었습니다." });
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : "설정 저장에 실패했습니다.";
      setFeedback({ type: "error", message });
    } finally {
      setSettingsSaving(false);
    }
  }

  async function handleCreateChannel() {
    setCreating(true);
    try {
      const authPayload = JSON.parse(createForm.authJsonText || "{}") as Record<string, unknown>;
      await createChannel({
        platform: createForm.platform,
        label: createForm.label,
        blog_url: createForm.blogUrl,
        persona_id: createForm.personaId,
        persona_desc: createForm.personaDesc,
        publish_delay_minutes: createForm.publishDelayMinutes,
        is_master: createForm.isMaster,
        active: createForm.active,
        auth_json: authPayload,
      });
      setCreateForm(DEFAULT_CREATE_FORM);
      setFeedback({ type: "success", message: "채널이 생성되었습니다." });
      await loadData();
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : "채널 생성에 실패했습니다.";
      setFeedback({ type: "error", message });
    } finally {
      setCreating(false);
    }
  }

  async function handleTestChannel(channelId: string) {
    setTestingId(channelId);
    try {
      const response = await testChannel(channelId);
      const hasCodeInMessage = /\bcode=/.test(response.message);
      const detailMessage = response.reason_code && !hasCodeInMessage
        ? `${response.message} (reason=${response.reason_code})`
        : response.message;
      setFeedback({
        type: response.success ? "success" : "error",
        message: detailMessage,
      });
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : "연동 테스트에 실패했습니다.";
      setFeedback({ type: "error", message });
    } finally {
      setTestingId("");
    }
  }

  async function handleToggleChannelActive(channel: ChannelItem) {
    setTogglingId(channel.channel_id);
    try {
      if (channel.active) {
        const response = await deleteChannel(channel.channel_id);
        setFeedback({
          type: "success",
          message: `${response.message} (취소 ${response.cancelled_jobs}건)`,
        });
      } else {
        await updateChannel(channel.channel_id, { active: true });
        setFeedback({ type: "success", message: "채널이 활성화되었습니다." });
      }
      await loadData();
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : "채널 상태 변경에 실패했습니다.";
      setFeedback({ type: "error", message });
    } finally {
      setTogglingId("");
    }
  }

  async function handleDistribute() {
    const normalizedJobId = distributeJobId.trim();
    if (!normalizedJobId) {
      setFeedback({ type: "error", message: "마스터 job_id를 입력해 주세요." });
      return;
    }

    setDistributeLoading(true);
    try {
      const response = await distributeSubJobs(normalizedJobId);
      setFeedback({
        type: "success",
        message: `배포 완료: 생성 ${response.created}, 스킵 ${response.skipped}, 실패 ${response.failed}`,
      });
      await loadData();
    } catch (requestError) {
      const message = requestError instanceof Error ? requestError.message : "서브 잡 배포에 실패했습니다.";
      setFeedback({ type: "error", message });
    } finally {
      setDistributeLoading(false);
    }
  }

  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
        Channel Manager
      </h2>
      <p className="mt-1 text-sm text-slate-600">
        마스터/서브 채널을 관리하고 멀티채널 배포를 제어합니다.
      </p>

      <div className="mt-4 flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 p-3">
        <label className="flex items-center gap-2 text-sm font-medium text-slate-700">
          <input
            type="checkbox"
            checked={multichannelEnabled}
            onChange={(event) => setMultichannelEnabled(event.target.checked)}
            className="h-4 w-4 rounded border-slate-300"
          />
          멀티채널 기능 활성화
        </label>
        <button
          type="button"
          onClick={handleSaveSettings}
          disabled={settingsSaving}
          className="rounded-full border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:border-slate-400 disabled:opacity-60"
        >
          {settingsSaving ? "저장 중..." : "설정 저장"}
        </button>
        <span className="text-xs text-slate-500">
          활성 채널 {activeCount}개 / 전체 {channels.length}개
        </span>
      </div>

      <div className="mt-4 rounded-xl border border-slate-200 p-3">
        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          Manual Distribute
        </p>
        <div className="mt-2 flex flex-col gap-2 sm:flex-row">
          <input
            type="text"
            value={distributeJobId}
            onChange={(event) => setDistributeJobId(event.target.value)}
            placeholder="completed 마스터 job_id"
            className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
          />
          <button
            type="button"
            onClick={handleDistribute}
            disabled={distributeLoading}
            className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-800 disabled:opacity-60"
          >
            {distributeLoading ? "배포 중..." : "서브 잡 배포"}
          </button>
        </div>
      </div>

      <div className="mt-4 rounded-xl border border-slate-200 p-4">
        <p className="text-sm font-semibold text-slate-800">새 채널 추가</p>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600">플랫폼</span>
            <select
              value={createForm.platform}
              onChange={(event) =>
                setCreateForm((prev) => ({
                  ...prev,
                  platform: event.target.value as CreateFormState["platform"],
                }))
              }
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            >
              <option value="naver">naver</option>
              <option value="tistory">tistory</option>
              <option value="wordpress">wordpress</option>
            </select>
          </label>

          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600">레이블</span>
            <input
              type="text"
              value={createForm.label}
              onChange={(event) =>
                setCreateForm((prev) => ({
                  ...prev,
                  label: event.target.value,
                }))
              }
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              placeholder="네이버 메인"
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600">Blog URL</span>
            <input
              type="text"
              value={createForm.blogUrl}
              onChange={(event) =>
                setCreateForm((prev) => ({
                  ...prev,
                  blogUrl: event.target.value,
                }))
              }
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              placeholder="https://blog.naver.com/my_blog"
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600">Persona ID</span>
            <input
              type="text"
              value={createForm.personaId}
              onChange={(event) =>
                setCreateForm((prev) => ({
                  ...prev,
                  personaId: event.target.value,
                }))
              }
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              placeholder="P1"
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600">Delay (minutes)</span>
            <input
              type="number"
              min={0}
              value={createForm.publishDelayMinutes}
              onChange={(event) =>
                setCreateForm((prev) => ({
                  ...prev,
                  publishDelayMinutes: Math.max(0, Number(event.target.value || 0)),
                }))
              }
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            />
          </label>

          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600">Persona 설명</span>
            <input
              type="text"
              value={createForm.personaDesc}
              onChange={(event) =>
                setCreateForm((prev) => ({
                  ...prev,
                  personaDesc: event.target.value,
                }))
              }
              className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
              placeholder="톤/관점 힌트"
            />
          </label>
        </div>

        <label className="mt-3 block">
          <span className="mb-1 block text-xs font-medium text-slate-600">auth_json</span>
          <textarea
            value={createForm.authJsonText}
            onChange={(event) =>
              setCreateForm((prev) => ({
                ...prev,
                authJsonText: event.target.value,
              }))
            }
            className="h-24 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            placeholder='{"session_dir":"data/sessions/naver_sub"}'
          />
        </label>

        <div className="mt-3 flex flex-wrap items-center gap-4">
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={createForm.isMaster}
              onChange={(event) =>
                setCreateForm((prev) => ({
                  ...prev,
                  isMaster: event.target.checked,
                }))
              }
              className="h-4 w-4 rounded border-slate-300"
            />
            마스터 채널
          </label>
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={createForm.active}
              onChange={(event) =>
                setCreateForm((prev) => ({
                  ...prev,
                  active: event.target.checked,
                }))
              }
              className="h-4 w-4 rounded border-slate-300"
            />
            활성 상태
          </label>
          <button
            type="button"
            onClick={handleCreateChannel}
            disabled={creating}
            className="rounded-full bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-indigo-700 disabled:opacity-60"
          >
            {creating ? "생성 중..." : "채널 추가"}
          </button>
        </div>
      </div>

      {feedback && (
        <p
          className={`mt-4 rounded-lg px-3 py-2 text-sm ${
            feedback.type === "success"
              ? "border border-emerald-200 bg-emerald-50 text-emerald-700"
              : "border border-rose-200 bg-rose-50 text-rose-700"
          }`}
        >
          {feedback.message}
        </p>
      )}

      <div className="mt-4 space-y-3">
        {loading && (
          <p className="rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-600">
            채널 목록을 불러오는 중입니다...
          </p>
        )}

        {!loading && channels.length === 0 && (
          <p className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600">
            등록된 채널이 없습니다.
          </p>
        )}

        {!loading &&
          channels.map((channel) => (
            <article
              key={channel.channel_id}
              className={`rounded-xl border p-4 ${
                channel.active ? "border-slate-200 bg-white" : "border-slate-200 bg-slate-50"
              }`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <p className="text-sm font-semibold text-slate-900">
                    {channel.label}
                    <span className="ml-2 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-600">
                      {channel.platform}
                    </span>
                    {channel.is_master && (
                      <span className="ml-2 rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium text-amber-700">
                        MASTER
                      </span>
                    )}
                  </p>
                  <p className="mt-1 text-xs text-slate-600">{channel.blog_url}</p>
                </div>

                <span
                  className={`rounded-full px-2 py-1 text-[10px] font-semibold ${
                    channel.active ? "bg-emerald-100 text-emerald-700" : "bg-slate-200 text-slate-600"
                  }`}
                >
                  {channel.active ? "ACTIVE" : "INACTIVE"}
                </span>
              </div>

              <div className="mt-3 grid gap-1 text-xs text-slate-600 md:grid-cols-3">
                <p>persona: {channel.persona_id}</p>
                <p>delay: {channel.publish_delay_minutes}m</p>
                <p>style lv: {channel.style_level}</p>
              </div>

              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => handleTestChannel(channel.channel_id)}
                  disabled={testingId === channel.channel_id}
                  className="rounded-full border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:border-slate-400 disabled:opacity-60"
                >
                  {testingId === channel.channel_id ? "테스트 중..." : "연동 테스트"}
                </button>
                <button
                  type="button"
                  onClick={() => handleToggleChannelActive(channel)}
                  disabled={togglingId === channel.channel_id}
                  className="rounded-full border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:border-slate-400 disabled:opacity-60"
                >
                  {togglingId === channel.channel_id
                    ? "처리 중..."
                    : channel.active
                      ? "비활성화"
                      : "활성화"}
                </button>
              </div>
            </article>
          ))}
      </div>
    </section>
  );
}
