"use client";

import { useEffect, useRef, useState } from "react";
import {
  fetchUpdateCheck,
  fetchUpdateVersion,
  runUpdate,
  type UpdateCheckResponse,
  type UpdateVersionResponse,
} from "@/lib/api";

export default function UpdateButton() {
  const [checkInfo, setCheckInfo] = useState<UpdateCheckResponse | null>(null);
  const [versionInfo, setVersionInfo] = useState<UpdateVersionResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [running, setRunning] = useState(false);
  const [logText, setLogText] = useState("");
  const [statusText, setStatusText] = useState("");
  const logRef = useRef<HTMLPreElement | null>(null);

  async function loadUpdateState() {
    setLoading(true);
    try {
      const [check, version] = await Promise.all([
        fetchUpdateCheck(),
        fetchUpdateVersion(),
      ]);
      setCheckInfo(check);
      setVersionInfo(version);
      setStatusText("");
    } catch (error) {
      const message = error instanceof Error ? error.message : "업데이트 정보를 불러오지 못했습니다.";
      setStatusText(`❌ ${message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadUpdateState();
  }, []);

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [logText]);

  async function handleRunUpdate() {
    setRunning(true);
    setLogText("");
    setStatusText("업데이트를 시작했습니다...");
    try {
      let fullLog = "";
      await runUpdate((chunk) => {
        fullLog += chunk;
        setLogText((prev) => prev + chunk);
      });

      if (fullLog.includes("✅ 업데이트 완료!")) {
        setStatusText("✅ 업데이트 완료! 잠시 후 새로고침됩니다.");
        setTimeout(() => {
          window.location.reload();
        }, 3000);
      } else if (fullLog.includes("❌")) {
        setStatusText("❌ 업데이트 중 오류가 발생했습니다. 로그를 확인하세요.");
      } else {
        setStatusText("업데이트 로그 수신이 종료되었습니다.");
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "업데이트 실행에 실패했습니다.";
      setStatusText(`❌ ${message}`);
    } finally {
      setRunning(false);
      loadUpdateState();
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setModalOpen(true)}
        className="inline-flex items-center gap-2 rounded-full border border-emerald-300 bg-emerald-50 px-4 py-2 text-sm font-semibold text-emerald-800 transition hover:border-emerald-500"
      >
        업데이트 확인
        {checkInfo && checkInfo.behind > 0 && (
          <span className="rounded-full bg-rose-600 px-2 py-0.5 text-xs font-bold text-white">
            🔔 {checkInfo.behind}
          </span>
        )}
      </button>

      {modalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-2xl rounded-2xl border border-slate-200 bg-white p-5 shadow-xl">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="text-lg font-semibold text-slate-900">업데이트 실행</h2>
                <p className="mt-1 text-sm text-slate-600">
                  원격 저장소와 비교해 업데이트를 확인하고 바로 실행할 수 있습니다.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setModalOpen(false)}
                className="rounded-lg border border-slate-200 px-3 py-1 text-sm text-slate-600 hover:border-slate-400"
                disabled={running}
              >
                닫기
              </button>
            </div>

            <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700">
              <p>
                최신 커밋:{" "}
                <span className="font-mono text-xs">
                  {versionInfo ? versionInfo.commit_hash.slice(0, 8) : "-"}
                </span>
              </p>
              <p>커밋 메시지: {versionInfo?.commit_message || "-"}</p>
              <p>커밋 시각: {versionInfo?.committed_at || "-"}</p>
              <p className="mt-2 font-semibold">
                상태:{" "}
                {loading
                  ? "확인 중..."
                  : checkInfo
                    ? checkInfo.up_to_date
                      ? "최신 버전입니다."
                      : `🔔 업데이트 ${checkInfo.behind}개 available`
                    : "확인 실패"}
              </p>
            </div>

            <div className="mt-4 flex items-center gap-2">
              <button
                type="button"
                onClick={loadUpdateState}
                disabled={running || loading}
                className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:border-slate-500 disabled:opacity-50"
              >
                다시 확인
              </button>
              <button
                type="button"
                onClick={handleRunUpdate}
                disabled={running}
                className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-700 disabled:opacity-60"
              >
                {running ? "업데이트 실행 중..." : "업데이트 실행"}
              </button>
            </div>

            {statusText && (
              <p className="mt-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">
                {statusText}
              </p>
            )}

            <div className="mt-4 rounded-xl border border-slate-200 bg-slate-900 p-3">
              <p className="mb-2 text-xs font-semibold text-slate-300">실시간 실행 로그</p>
              <pre
                ref={logRef}
                className="max-h-64 overflow-y-auto whitespace-pre-wrap break-words text-xs leading-5 text-emerald-200"
              >
                {logText || "로그 대기 중..."}
              </pre>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
