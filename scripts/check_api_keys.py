#!/usr/bin/env python3
"""연결된 API 키 상태를 실제 호출로 점검한다."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.llm.api_health import _check_single
from modules.llm.llm_router import DEFAULT_IMAGE_KEYS, DEFAULT_TEXT_KEYS
from modules.llm.provider_factory import create_client


def parse_env_file(env_path: Path) -> Dict[str, str]:
    """.env 파일을 단순 파싱한다."""
    values: Dict[str, str] = {}
    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def get_env_value(name: str, dotenv_values: Dict[str, str]) -> str:
    """환경변수 우선, 없으면 .env 값을 반환한다."""
    runtime_value = os.getenv(name, "").strip()
    if runtime_value:
        return runtime_value
    return str(dotenv_values.get(name, "")).strip()


def mask_secret(value: str) -> str:
    """키를 마스킹해 로그 노출을 막는다."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) <= 8:
        return "*" * len(raw)
    return f"{raw[:3]}***{raw[-3:]}"


def redact_message(message: str, secret_values: Sequence[str]) -> str:
    """에러 문자열에서 민감정보를 제거한다."""
    redacted = str(message)
    for secret in secret_values:
        token = str(secret or "").strip()
        if not token:
            continue
        redacted = redacted.replace(token, "***REDACTED***")
    return redacted


def parse_json_map(raw_value: str) -> Dict[str, str]:
    """JSON 문자열을 dict[str, str]로 변환한다."""
    try:
        loaded = json.loads(raw_value or "{}")
    except Exception:
        loaded = {}

    if not isinstance(loaded, dict):
        return {}

    return {
        str(key).strip().lower(): str(value or "").strip()
        for key, value in loaded.items()
        if str(key).strip()
    }


def load_db_settings(db_path: str) -> Dict[str, str]:
    """system_settings에서 점검에 필요한 키를 읽는다."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT setting_key, setting_value
        FROM system_settings
        WHERE setting_key IN (
            'router_text_api_keys',
            'router_image_api_keys',
            'telegram_bot_token',
            'telegram_chat_id',
            'telegram_webhook_secret'
        )
        """
    ).fetchall()
    conn.close()
    return {row["setting_key"]: str(row["setting_value"] or "") for row in rows}


def build_key_context(db_path: str, env_path: Path) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    """DB/환경변수를 합쳐 provider별 키 컨텍스트를 만든다."""
    dotenv_values = parse_env_file(env_path)
    db_settings = load_db_settings(db_path)

    text_api_keys = parse_json_map(db_settings.get("router_text_api_keys", ""))
    image_api_keys = parse_json_map(db_settings.get("router_image_api_keys", ""))

    # DB에 없으면 환경변수 키를 보강한다.
    for key_id, env_name in DEFAULT_TEXT_KEYS.items():
        if not text_api_keys.get(key_id):
            text_api_keys[key_id] = get_env_value(env_name, dotenv_values)

    for key_id, env_name in DEFAULT_IMAGE_KEYS.items():
        if not image_api_keys.get(key_id):
            image_api_keys[key_id] = get_env_value(env_name, dotenv_values)

    misc = {
        "telegram_bot_token": db_settings.get("telegram_bot_token", "").strip()
        or get_env_value("TELEGRAM_BOT_TOKEN", dotenv_values),
        "telegram_chat_id": db_settings.get("telegram_chat_id", "").strip()
        or get_env_value("TELEGRAM_CHAT_ID", dotenv_values),
        "telegram_webhook_secret": db_settings.get("telegram_webhook_secret", "").strip()
        or get_env_value("TELEGRAM_WEBHOOK_SECRET", dotenv_values),
        "hf_token": get_env_value("HF_TOKEN", dotenv_values),
        "brave_api_key": get_env_value("BRAVE_API_KEY", dotenv_values),
        "fal_key": get_env_value("FAL_KEY", dotenv_values),
        "naver_blog_id": get_env_value("NAVER_BLOG_ID", dotenv_values),
        "customs_trade_api_key": get_env_value("CUSTOMS_TRADE_API_KEY", dotenv_values)
        or get_env_value("DATA_GO_KR_SERVICE_KEY", dotenv_values),
    }

    return text_api_keys, image_api_keys, misc


async def check_text_provider(
    provider: str,
    model: str,
    api_key: str,
    timeout_sec: float,
    secret_values: Sequence[str],
) -> Dict[str, Any]:
    """텍스트 LLM provider의 최소 ping 호출을 수행한다."""
    start_time = time.perf_counter()

    try:
        client = create_client(
            provider=provider,
            model=model,
            timeout_sec=timeout_sec,
            max_tokens=4,
            api_key=api_key,
        )
        result = await _check_single(client, timeout_sec=timeout_sec, close_client=True)
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)

        return {
            "provider": provider,
            "type": "text",
            "configured": True,
            "masked": mask_secret(api_key),
            "status": "OK" if bool(result.get("ok", False)) else "FAIL",
            "latency_ms": elapsed_ms,
            "detail": redact_message(str(result.get("message", "")), secret_values),
        }
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        return {
            "provider": provider,
            "type": "text",
            "configured": True,
            "masked": mask_secret(api_key),
            "status": "FAIL",
            "latency_ms": elapsed_ms,
            "detail": redact_message(f"{exc.__class__.__name__}: {exc}", secret_values),
        }


async def check_http_provider(
    provider: str,
    url: str,
    headers: Dict[str, str],
    timeout_sec: float,
    secret_values: Sequence[str],
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """HTTP 기반 provider의 인증 상태를 점검한다."""
    start_time = time.perf_counter()

    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            response = await client.get(url, headers=headers, params=params)

        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        is_ok = 200 <= response.status_code < 300
        detail = f"HTTP {response.status_code}"

        if not is_ok:
            response_body = (response.text or "")[:180].replace("\n", " ")
            detail = f"HTTP {response.status_code} {response_body}"

        return {
            "provider": provider,
            "type": "http",
            "configured": True,
            "status": "OK" if is_ok else "FAIL",
            "latency_ms": elapsed_ms,
            "detail": redact_message(detail, secret_values),
        }
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        return {
            "provider": provider,
            "type": "http",
            "configured": True,
            "status": "FAIL",
            "latency_ms": elapsed_ms,
            "detail": redact_message(f"{exc.__class__.__name__}: {exc}", secret_values),
        }


def make_skip_row(provider: str, row_type: str = "text") -> Dict[str, Any]:
    """미설정 provider의 공통 결과를 생성한다."""
    return {
        "provider": provider,
        "type": row_type,
        "configured": False,
        "masked": "",
        "status": "SKIP",
        "latency_ms": 0,
        "detail": "키 미설정",
    }


async def run_checks(db_path: str, env_path: Path, timeout_sec: float) -> Dict[str, Any]:
    """전체 provider 점검을 수행한다."""
    text_api_keys, image_api_keys, misc = build_key_context(db_path=db_path, env_path=env_path)

    model_map = {
        "qwen": "qwen-plus",
        "deepseek": "deepseek-v4-flash",
        "gemini": "gemini-2.0-flash",
        "openai": "gpt-4.1-mini",
        "claude": "claude-sonnet-4-20250514",
        "groq": "llama-3.3-70b-versatile",
        "cerebras": "llama3.1-8b",
        "nvidia": "meta/llama-3.3-70b-instruct",
    }

    secret_values: List[str] = []
    for value in list(text_api_keys.values()) + list(image_api_keys.values()) + list(misc.values()):
        token = str(value or "").strip()
        if token:
            secret_values.append(token)

    results: List[Dict[str, Any]] = []

    for provider, model in model_map.items():
        api_key = str(text_api_keys.get(provider, "")).strip()
        if not api_key:
            results.append(make_skip_row(provider, "text"))
            continue
        results.append(
            await check_text_provider(
                provider=provider,
                model=model,
                api_key=api_key,
                timeout_sec=timeout_sec,
                secret_values=secret_values,
            )
        )

    pexels_key = str(image_api_keys.get("pexels", "")).strip()
    if pexels_key:
        pexels_row = await check_http_provider(
            provider="pexels",
            url="https://api.pexels.com/v1/search",
            headers={"Authorization": pexels_key},
            params={"query": "test", "per_page": 1},
            timeout_sec=timeout_sec,
            secret_values=secret_values,
        )
        pexels_row["masked"] = mask_secret(pexels_key)
        results.append(pexels_row)
    else:
        results.append(make_skip_row("pexels", "http"))

    openai_image_key = (
        str(image_api_keys.get("openai_image", "")).strip()
        or str(text_api_keys.get("openai", "")).strip()
    )
    if openai_image_key:
        openai_image_row = await check_http_provider(
            provider="openai_image",
            url="https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {openai_image_key}"},
            timeout_sec=timeout_sec,
            secret_values=secret_values,
        )
        openai_image_row["masked"] = mask_secret(openai_image_key)
        results.append(openai_image_row)
    else:
        results.append(make_skip_row("openai_image", "http"))

    together_key = str(image_api_keys.get("together", "")).strip()
    if together_key:
        together_row = await check_http_provider(
            provider="together",
            url="https://api.together.xyz/v1/models",
            headers={"Authorization": f"Bearer {together_key}"},
            timeout_sec=timeout_sec,
            secret_values=secret_values,
        )
        together_row["masked"] = mask_secret(together_key)
        results.append(together_row)
    else:
        results.append(make_skip_row("together", "http"))

    fal_key = str(image_api_keys.get("fal", "")).strip() or str(misc.get("fal_key", "")).strip()
    if fal_key:
        fal_row = await check_http_provider(
            provider="fal",
            url="https://fal.run/models",
            headers={"Authorization": f"Key {fal_key}"},
            timeout_sec=timeout_sec,
            secret_values=secret_values,
        )
        fal_row["masked"] = mask_secret(fal_key)
        results.append(fal_row)
    else:
        results.append(make_skip_row("fal", "http"))

    hf_token = str(misc.get("hf_token", "")).strip()
    if hf_token:
        hf_row = await check_http_provider(
            provider="huggingface",
            url="https://huggingface.co/api/whoami-v2",
            headers={"Authorization": f"Bearer {hf_token}"},
            timeout_sec=timeout_sec,
            secret_values=secret_values,
        )
        hf_row["masked"] = mask_secret(hf_token)
        results.append(hf_row)
    else:
        results.append(make_skip_row("huggingface", "http"))

    brave_key = str(misc.get("brave_api_key", "")).strip()
    if brave_key:
        brave_row = await check_http_provider(
            provider="brave_search",
            url="https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": brave_key},
            params={"q": "ping", "count": 1},
            timeout_sec=timeout_sec,
            secret_values=secret_values,
        )
        brave_row["masked"] = mask_secret(brave_key)
        results.append(brave_row)
    else:
        results.append(make_skip_row("brave_search", "http"))

    customs_trade_key = str(misc.get("customs_trade_api_key", "")).strip()
    results.append(
        {
            "provider": "customs_trade",
            "type": "config",
            "configured": bool(customs_trade_key),
            "masked": mask_secret(customs_trade_key),
            "status": "OK" if bool(customs_trade_key) else "SKIP",
            "latency_ms": 0,
            "detail": "관세청 수출입총괄 serviceKey 설정 확인",
        }
    )

    telegram_token = str(misc.get("telegram_bot_token", "")).strip()
    if telegram_token:
        telegram_row = await check_http_provider(
            provider="telegram_bot",
            url=f"https://api.telegram.org/bot{telegram_token}/getMe",
            headers={},
            timeout_sec=timeout_sec,
            secret_values=secret_values,
        )
        telegram_row["masked"] = mask_secret(telegram_token)
        results.append(telegram_row)
    else:
        results.append(make_skip_row("telegram_bot", "http"))

    naver_blog_id = str(misc.get("naver_blog_id", "")).strip()
    results.append(
        {
            "provider": "naver_blog_id",
            "type": "config",
            "configured": bool(naver_blog_id),
            "masked": mask_secret(naver_blog_id),
            "status": "OK" if bool(naver_blog_id) else "SKIP",
            "latency_ms": 0,
            "detail": "설정값 확인",
        }
    )

    summary = {
        "total": len(results),
        "ok": sum(1 for row in results if str(row.get("status", "")).upper() == "OK"),
        "fail": sum(1 for row in results if str(row.get("status", "")).upper() == "FAIL"),
        "skip": sum(1 for row in results if str(row.get("status", "")).upper() == "SKIP"),
    }

    return {
        "db_path": db_path,
        "env_path": str(env_path),
        "summary": summary,
        "results": results,
    }


def print_table(payload: Dict[str, Any]) -> None:
    """가독성 좋은 텍스트 표를 출력한다."""
    results = list(payload.get("results", []))
    summary = dict(payload.get("summary", {}))

    headers = ["provider", "type", "status", "configured", "latency_ms", "masked", "detail"]
    widths = {header: len(header) for header in headers}

    for row in results:
        widths["provider"] = max(widths["provider"], len(str(row.get("provider", ""))))
        widths["type"] = max(widths["type"], len(str(row.get("type", ""))))
        widths["status"] = max(widths["status"], len(str(row.get("status", ""))))
        widths["configured"] = max(widths["configured"], len(str(row.get("configured", ""))))
        widths["latency_ms"] = max(widths["latency_ms"], len(str(row.get("latency_ms", ""))))
        widths["masked"] = max(widths["masked"], len(str(row.get("masked", ""))))
        widths["detail"] = max(widths["detail"], len(str(row.get("detail", ""))))

    def line(values: Dict[str, Any]) -> str:
        return " | ".join(str(values.get(header, "")).ljust(widths[header]) for header in headers)

    print(f"db_path: {payload.get('db_path', '')}")
    print(f"env_path: {payload.get('env_path', '')}")
    print(
        "summary: "
        f"total={summary.get('total', 0)} "
        f"ok={summary.get('ok', 0)} "
        f"fail={summary.get('fail', 0)} "
        f"skip={summary.get('skip', 0)}"
    )
    print()
    print(line({header: header for header in headers}))
    print("-+-".join("-" * widths[header] for header in headers))
    for row in results:
        print(line(row))


def parse_args() -> argparse.Namespace:
    """CLI 인자를 파싱한다."""
    parser = argparse.ArgumentParser(description="연결된 API 키 점검")
    parser.add_argument(
        "--db",
        default=os.getenv("AUTOBLOG_DB_PATH", "data/automation.db"),
        help="SQLite DB 경로 (기본: AUTOBLOG_DB_PATH 또는 data/automation.db)",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="환경변수 파일 경로 (기본: .env)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="provider 호출 타임아웃(초)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="JSON 형식으로 출력",
    )
    return parser.parse_args()


def main() -> None:
    """점검 엔트리포인트."""
    args = parse_args()
    payload = asyncio.run(
        run_checks(
            db_path=args.db,
            env_path=Path(args.env_file),
            timeout_sec=max(1.0, float(args.timeout)),
        )
    )

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print_table(payload)


if __name__ == "__main__":
    main()
