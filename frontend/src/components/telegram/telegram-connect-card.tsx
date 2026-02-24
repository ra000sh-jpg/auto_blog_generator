"use client";

import { useMemo, useState } from "react";

import {
    verifyTelegramLink,
    verifyTelegramToken,
    type OnboardingStatusResponse,
} from "@/lib/api";
import { isTelegramBotTokenFormat } from "@/lib/utils/telegram";

type TelegramConnectCardProps = {
    initialOnboardingStatus: OnboardingStatusResponse;
    mode: "onboarding" | "settings";
    onPrev?: () => void;
    onNext?: () => void;
};

type StatusMessage = {
    success: boolean;
    text: string;
};

function QrPreview({ value, size = 104 }: { value: string; size?: number }) {
    const safeValue = encodeURIComponent(value);
    const src = `https://api.qrserver.com/v1/create-qr-code/?size=${size}x${size}&data=${safeValue}`;
    return (
        <img
            src={src}
            width={size}
            height={size}
            alt="qr-code"
            className="h-[104px] w-[104px]"
        />
    );
}

export default function TelegramConnectCard({
    initialOnboardingStatus,
    mode,
    onPrev,
    onNext,
}: TelegramConnectCardProps) {
    const [botToken, setBotToken] = useState("");
    const [botUsername, setBotUsername] = useState("");
    const [authCode, setAuthCode] = useState("");
    const [authCommand, setAuthCommand] = useState("");
    const [deepLink, setDeepLink] = useState("");
    const [expiresInSec, setExpiresInSec] = useState(300);

    const [linkedChatId, setLinkedChatId] = useState(initialOnboardingStatus.telegram_chat_id || "");
    const [isLinked, setIsLinked] = useState(Boolean(initialOnboardingStatus.telegram_configured && initialOnboardingStatus.telegram_chat_id));

    const [verifyingToken, setVerifyingToken] = useState(false);
    const [confirmingLink, setConfirmingLink] = useState(false);
    const [statusMessage, setStatusMessage] = useState<StatusMessage | null>(null);

    const tokenPlaceholder = useMemo(() => {
        if (initialOnboardingStatus.telegram_bot_token) {
            return "기존 토큰이 저장되어 있습니다. 재연동 시에만 새 토큰을 입력하세요.";
        }
        return "1234567890:ABCdefGHIjklMNO";
    }, [initialOnboardingStatus.telegram_bot_token]);

    async function handleVerifyToken() {
        const normalizedToken = String(botToken || "").trim();
        if (!isTelegramBotTokenFormat(normalizedToken)) {
            setStatusMessage({
                success: false,
                text: "Bot Token 형식이 올바르지 않습니다. 숫자:문자열 형태인지 확인해 주세요.",
            });
            return;
        }

        setVerifyingToken(true);
        setStatusMessage(null);

        try {
            const response = await verifyTelegramToken({ bot_token: normalizedToken });
            setBotUsername(response.bot_username || "");
            setAuthCode(response.auth_code || "");
            setAuthCommand(response.auth_command || "");
            setDeepLink(response.deep_link || "");
            setExpiresInSec(response.expires_in_sec || 300);
            setIsLinked(false);
            setLinkedChatId("");
            setStatusMessage({
                success: true,
                text: "토큰 검증 성공! Step 3에서 인증 명령을 봇에게 전송해 주세요.",
            });
        } catch (error) {
            setStatusMessage({
                success: false,
                text: error instanceof Error ? error.message : "토큰 검증 중 오류가 발생했습니다.",
            });
        } finally {
            setVerifyingToken(false);
        }
    }

    async function handleVerifyLink() {
        if (!authCode) {
            setStatusMessage({
                success: false,
                text: "먼저 Step 2에서 토큰 검증을 완료해 주세요.",
            });
            return;
        }

        setConfirmingLink(true);
        setStatusMessage(null);

        try {
            const response = await verifyTelegramLink({ auth_code: authCode });
            setLinkedChatId(response.chat_id || "");
            setIsLinked(Boolean(response.success));
            setStatusMessage({
                success: true,
                text: response.used_fallback
                    ? "연동 완료! (getUpdates 폴백으로 확인됨)"
                    : "연동 완료! Webhook 인증코드가 확인되었습니다.",
            });
        } catch (error) {
            setStatusMessage({
                success: false,
                text: error instanceof Error ? error.message : "연동 확인 중 오류가 발생했습니다.",
            });
        } finally {
            setConfirmingLink(false);
        }
    }

    async function handleCopyCommand() {
        if (!authCommand) return;
        try {
            await navigator.clipboard.writeText(authCommand);
            setStatusMessage({
                success: true,
                text: "인증 명령어를 클립보드에 복사했습니다.",
            });
        } catch {
            setStatusMessage({
                success: false,
                text: "클립보드 복사에 실패했습니다. 수동으로 복사해 주세요.",
            });
        }
    }

    return (
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <h2 className="font-[family-name:var(--font-heading)] text-lg font-semibold">
                Telegram 연동 (Webhook 인증코드)
            </h2>
            <p className="mt-1 text-sm text-slate-600">
                BotFather에서 봇 생성 → 토큰 검증 → 인증 명령 전송 순서로 1분 내 연동할 수 있습니다.
            </p>

            <div className="mt-4 grid gap-4 lg:grid-cols-3">
                <article className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                    <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">Step 1</p>
                    <h3 className="mt-1 text-sm font-semibold text-slate-800">BotFather 열기</h3>
                    <p className="mt-2 text-xs leading-relaxed text-slate-600">
                        QR 또는 버튼으로 BotFather를 열어 <code>/newbot</code>으로 봇을 생성하세요.
                    </p>
                    <div className="mt-3 flex flex-wrap gap-2">
                        <a
                            href="tg://resolve?domain=BotFather"
                            className="rounded-full bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-500"
                        >
                            Telegram 앱에서 열기
                        </a>
                        <a
                            href="https://t.me/BotFather"
                            target="_blank"
                            rel="noreferrer"
                            className="rounded-full border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100"
                        >
                            웹에서 열기
                        </a>
                    </div>
                    <div className="mt-3 flex justify-center rounded-lg bg-white p-3">
                        <QrPreview value="https://t.me/BotFather" size={104} />
                    </div>
                </article>

                <article className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                    <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">Step 2</p>
                    <h3 className="mt-1 text-sm font-semibold text-slate-800">토큰 검증</h3>
                    <label className="mt-2 block text-xs text-slate-600">
                        Bot Token
                        <input
                            type="password"
                            value={botToken}
                            onChange={(event) => setBotToken(event.target.value)}
                            placeholder={tokenPlaceholder}
                            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
                        />
                    </label>
                    <button
                        type="button"
                        onClick={handleVerifyToken}
                        disabled={verifyingToken || !botToken}
                        className="mt-3 w-full rounded-lg bg-indigo-600 px-3 py-2 text-sm font-semibold text-white hover:bg-indigo-500 disabled:opacity-50"
                    >
                        {verifyingToken ? "검증 중..." : "토큰 확인"}
                    </button>
                    {botUsername && (
                        <p className="mt-2 rounded-lg border border-emerald-200 bg-emerald-50 px-2 py-1 text-xs text-emerald-700">
                            검증 완료: @{botUsername}
                        </p>
                    )}
                </article>

                <article className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                    <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">Step 3</p>
                    <h3 className="mt-1 text-sm font-semibold text-slate-800">인증 명령 전송</h3>
                    <p className="mt-2 text-xs leading-relaxed text-slate-600">
                        개인 채팅에서 아래 명령을 전송한 뒤, 연동 확인 버튼을 누르세요.
                    </p>
                    <div className="mt-2 rounded-lg border border-dashed border-slate-300 bg-white p-2 text-xs text-slate-700">
                        {authCommand || "Step 2 완료 시 인증 명령이 생성됩니다."}
                    </div>
                    <div className="mt-2 flex gap-2">
                        <button
                            type="button"
                            onClick={handleCopyCommand}
                            disabled={!authCommand}
                            className="rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-xs text-slate-700 disabled:opacity-40"
                        >
                            명령 복사
                        </button>
                        {deepLink && (
                            <a
                                href={deepLink}
                                target="_blank"
                                rel="noreferrer"
                                className="rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-xs text-slate-700 hover:bg-slate-100"
                            >
                                내 봇 열기
                            </a>
                        )}
                    </div>
                    <div className="mt-3 flex justify-center rounded-lg bg-white p-3">
                        <QrPreview value={deepLink || "https://t.me"} size={104} />
                    </div>
                    <button
                        type="button"
                        onClick={handleVerifyLink}
                        disabled={confirmingLink || !authCode}
                        className="mt-2 w-full rounded-lg bg-emerald-600 px-3 py-2 text-sm font-semibold text-white hover:bg-emerald-500 disabled:opacity-50"
                    >
                        {confirmingLink ? "확인 중..." : "연동 확인 및 완료"}
                    </button>
                    {authCode && (
                        <p className="mt-2 text-[11px] text-slate-500">
                            인증코드 유효시간: 약 {Math.max(1, Math.round(expiresInSec / 60))}분
                        </p>
                    )}
                </article>
            </div>

            {isLinked && (
                <p className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700">
                    연동 완료됨: chat_id {linkedChatId || "(확인됨)"}
                </p>
            )}

            {statusMessage && (
                <p
                    className={`mt-3 rounded-xl border px-3 py-2 text-sm ${
                        statusMessage.success
                            ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                            : "border-rose-200 bg-rose-50 text-rose-700"
                    }`}
                >
                    {statusMessage.text}
                </p>
            )}

            {mode === "onboarding" && onNext && onPrev && (
                <div className="mt-6 flex justify-between pt-2">
                    <button
                        type="button"
                        onClick={onPrev}
                        className="rounded-lg px-4 py-2 font-semibold text-slate-600 hover:bg-slate-100"
                    >
                        ← 이전
                    </button>
                    <div className="flex gap-2">
                        <button
                            type="button"
                            onClick={onNext}
                            className="rounded-lg px-4 py-2 font-semibold text-slate-400 hover:bg-slate-100"
                        >
                            건너뛰기
                        </button>
                        <button
                            type="button"
                            onClick={onNext}
                            disabled={!isLinked}
                            className="rounded-full bg-gradient-to-r from-indigo-600 to-blue-600 px-6 py-2 font-bold text-white disabled:cursor-not-allowed disabled:opacity-30"
                        >
                            다음 단계로 →
                        </button>
                    </div>
                </div>
            )}
        </section>
    );
}
