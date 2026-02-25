"use client";

import React, { useCallback, useEffect, useState } from "react";
import {
  fetchSchedulerStatus,
  triggerSchedulerDraft,
  triggerSchedulerPublish,
  triggerSchedulerSeed,
  type SchedulerStatusResponse,
} from "@/lib/api";

// ─────────────────────────────────────────────────────────────────────────────
// 타입
// ─────────────────────────────────────────────────────────────────────────────
type TriggerState = "idle" | "loading" | "ok" | "error";

// ─────────────────────────────────────────────────────────────────────────────
// 서브 컴포넌트: 프로그레스 바
// ─────────────────────────────────────────────────────────────────────────────
function ProgressBar({ completed, target }: { completed: number; target: number }) {
  const pct = target > 0 ? Math.min(100, Math.round((completed / target) * 100)) : 0;
  const barColor =
    pct >= 100 ? "bg-green-500" : pct >= 50 ? "bg-blue-500" : "bg-amber-400";

  return (
    <div className="w-full">
      <div className="flex items-center justify-between mb-1">
        <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
          오늘 발행 현황
        </span>
        <span className="text-sm font-semibold text-gray-800 dark:text-gray-100">
          {completed} / {target} 건
        </span>
      </div>
      <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-3 overflow-hidden">
        <div
          className={`${barColor} h-3 rounded-full transition-all duration-500`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="text-right text-xs text-gray-500 dark:text-gray-400 mt-0.5">
        {pct}% 달성
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// 서브 컴포넌트: 상태 배지
// ─────────────────────────────────────────────────────────────────────────────
function StatusBadge({ running }: { running: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-semibold ${
        running
          ? "bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300"
          : "bg-gray-200 text-gray-500 dark:bg-gray-700 dark:text-gray-400"
      }`}
    >
      <span
        className={`w-1.5 h-1.5 rounded-full ${running ? "bg-green-500 animate-pulse" : "bg-gray-400"}`}
      />
      {running ? "Running" : "Stopped"}
    </span>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// 서브 컴포넌트: 트리거 버튼
// ─────────────────────────────────────────────────────────────────────────────
function TriggerButton({
  label,
  onClick,
  state,
  disabled,
}: {
  label: string;
  onClick: () => void;
  state: TriggerState;
  disabled?: boolean;
}) {
  const baseClass =
    "px-3 py-1.5 rounded-md text-xs font-medium border transition-all focus:outline-none focus:ring-2 focus:ring-offset-1";
  const stateClass =
    state === "loading"
      ? "bg-gray-100 text-gray-400 border-gray-200 cursor-not-allowed"
      : state === "ok"
      ? "bg-green-50 text-green-700 border-green-300"
      : state === "error"
      ? "bg-red-50 text-red-700 border-red-300"
      : "bg-white text-gray-700 border-gray-300 hover:bg-gray-50 dark:bg-gray-800 dark:text-gray-200 dark:border-gray-600 dark:hover:bg-gray-700";

  const icon =
    state === "loading" ? "⏳" : state === "ok" ? "✅" : state === "error" ? "❌" : "";

  return (
    <button
      className={`${baseClass} ${stateClass}`}
      onClick={onClick}
      disabled={disabled || state === "loading"}
      title={label}
    >
      {icon} {label}
    </button>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// 메인 위젯
// ─────────────────────────────────────────────────────────────────────────────
export default function SchedulerWidget() {
  const [status, setStatus] = useState<SchedulerStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const [seedState, setSeedState] = useState<TriggerState>("idle");
  const [draftState, setDraftState] = useState<TriggerState>("idle");
  const [publishState, setPublishState] = useState<TriggerState>("idle");
  const [triggerMsg, setTriggerMsg] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      const data = await fetchSchedulerStatus();
      setStatus(data);
      setError(null);
      setLastUpdated(new Date());
    } catch (e) {
      setError(e instanceof Error ? e.message : "상태 조회 실패");
    } finally {
      setLoading(false);
    }
  }, []);

  // 초기 로드 + 30초 폴링
  useEffect(() => {
    loadStatus();
    const id = setInterval(loadStatus, 30_000);
    return () => clearInterval(id);
  }, [loadStatus]);

  // 트리거 버튼 상태 리셋
  const resetAfter = (setter: React.Dispatch<React.SetStateAction<TriggerState>>) => {
    setTimeout(() => setter("idle"), 3000);
  };

  const handleSeed = async () => {
    setSeedState("loading");
    setTriggerMsg(null);
    try {
      const res = await triggerSchedulerSeed();
      setSeedState(res.ok ? "ok" : "error");
      setTriggerMsg(res.message);
      await loadStatus();
    } catch (e) {
      setSeedState("error");
      setTriggerMsg(e instanceof Error ? e.message : "시드 실행 실패");
    }
    resetAfter(setSeedState);
  };

  const handleDraft = async () => {
    setDraftState("loading");
    setTriggerMsg(null);
    try {
      const res = await triggerSchedulerDraft();
      setDraftState(res.ok ? "ok" : "error");
      setTriggerMsg(res.message);
      await loadStatus();
    } catch (e) {
      setDraftState("error");
      setTriggerMsg(e instanceof Error ? e.message : "초안 생성 실패");
    }
    resetAfter(setDraftState);
  };

  const handlePublish = async () => {
    setPublishState("loading");
    setTriggerMsg(null);
    try {
      const res = await triggerSchedulerPublish();
      setPublishState(res.ok ? "ok" : "error");
      setTriggerMsg(res.message);
      await loadStatus();
    } catch (e) {
      setPublishState("error");
      setTriggerMsg(e instanceof Error ? e.message : "발행 실패");
    }
    resetAfter(setPublishState);
  };

  // ───────────────────────────────────
  // 렌더
  // ───────────────────────────────────
  if (loading) {
    return (
      <div className="rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 shadow-sm">
        <p className="text-sm text-gray-400 animate-pulse">스케줄러 상태 로딩 중…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-xl border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 p-4 shadow-sm">
        <p className="text-sm text-red-600 dark:text-red-400">⚠️ {error}</p>
      </div>
    );
  }

  if (!status) return null;

  const nextSlotDisplay = status.next_publish_slot_kst
    ? new Date(status.next_publish_slot_kst).toLocaleTimeString("ko-KR", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "–";

  return (
    <div className="rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-5 shadow-sm space-y-4">
      {/* 헤더 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-base font-semibold text-gray-800 dark:text-gray-100">
            📅 자동 발행 스케줄러
          </span>
          <StatusBadge running={status.scheduler_running} />
        </div>
        <button
          onClick={loadStatus}
          className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 transition-colors"
          title="새로고침"
        >
          🔄
        </button>
      </div>

      {/* 프로그레스 바 */}
      <ProgressBar completed={status.today_completed} target={status.daily_target} />

      {/* 통계 그리드 */}
      <div className="grid grid-cols-2 gap-3 text-sm">
        <div className="rounded-lg bg-gray-50 dark:bg-gray-700/50 px-3 py-2">
          <p className="text-xs text-gray-500 dark:text-gray-400">대기 초안</p>
          <p className="font-semibold text-gray-800 dark:text-gray-100">
            {status.ready_to_publish}건
          </p>
          <p className="text-[11px] text-gray-500 dark:text-gray-400">
            마스터 {status.ready_master} / 서브 {status.ready_sub}
          </p>
        </div>
        <div className="rounded-lg bg-gray-50 dark:bg-gray-700/50 px-3 py-2">
          <p className="text-xs text-gray-500 dark:text-gray-400">큐 대기</p>
          <p className="font-semibold text-gray-800 dark:text-gray-100">{status.queued}건</p>
          <p className="text-[11px] text-gray-500 dark:text-gray-400">
            마스터 {status.queued_master} / 서브 {status.queued_sub}
          </p>
        </div>
        <div className="rounded-lg bg-gray-50 dark:bg-gray-700/50 px-3 py-2">
          <p className="text-xs text-gray-500 dark:text-gray-400">다음 발행 예정</p>
          <p className="font-semibold text-gray-800 dark:text-gray-100">{nextSlotDisplay}</p>
        </div>
        <div className="rounded-lg bg-gray-50 dark:bg-gray-700/50 px-3 py-2">
          <p className="text-xs text-gray-500 dark:text-gray-400">오늘 실패</p>
          <p
            className={`font-semibold ${
              status.today_failed > 0
                ? "text-red-500 dark:text-red-400"
                : "text-gray-800 dark:text-gray-100"
            }`}
          >
            {status.today_failed}건
          </p>
        </div>
      </div>

      {/* 수동 트리거 */}
      <div>
        <p className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-2">수동 트리거</p>
        <div className="flex flex-wrap gap-2">
          <TriggerButton
            label="큐 시드"
            onClick={handleSeed}
            state={seedState}
            disabled={!status.scheduler_running}
          />
          <TriggerButton
            label="초안 생성"
            onClick={handleDraft}
            state={draftState}
            disabled={!status.scheduler_running}
          />
          <TriggerButton
            label="지금 발행"
            onClick={handlePublish}
            state={publishState}
            disabled={!status.scheduler_running}
          />
        </div>
        {triggerMsg && (
          <p className="text-xs mt-2 text-gray-600 dark:text-gray-400">{triggerMsg}</p>
        )}
      </div>

      {/* 푸터 */}
      <div className="flex justify-between text-xs text-gray-400 dark:text-gray-500 pt-1 border-t border-gray-100 dark:border-gray-700">
        <span>활성 시간: {status.active_hours}</span>
        <span>
          {lastUpdated
            ? `업데이트: ${lastUpdated.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}`
            : ""}
        </span>
      </div>
    </div>
  );
}
