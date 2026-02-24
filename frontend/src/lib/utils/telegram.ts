const TELEGRAM_BOT_TOKEN_PATTERN = /^[0-9]+:[a-zA-Z0-9_-]+$/;

export function isTelegramBotTokenFormat(value: string): boolean {
    return TELEGRAM_BOT_TOKEN_PATTERN.test(String(value || "").trim());
}

