#!/usr/bin/env python3
"""ready_to_publish 상태의 특정 Job 1건만 발행/임시저장한다."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from modules.automation.job_store import JobStore
from modules.automation.notifier import TelegramNotifier
from modules.automation.pipeline_service import PipelineService, stub_generate_fn
from modules.config import load_config
from modules.logging_config import setup_logging
from modules.metrics import MetricsStore
from modules.uploaders.playwright_publisher import PlaywrightPublisher


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="특정 ready_to_publish Job 1건만 네이버에 발행/임시저장",
    )
    parser.add_argument("--job-id", required=True, help="처리할 job_id")
    parser.add_argument(
        "--db",
        default=os.getenv("AUTOBLOG_DB_PATH", "data/automation.db"),
        help="스케줄러/큐 DB 경로",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="임시저장이 아니라 실제 발행한다. 기본값은 임시저장",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="브라우저 화면을 표시한다",
    )
    parser.add_argument(
        "--browser-channel",
        default="",
        help="Playwright 브라우저 채널 예: chrome",
    )
    parser.add_argument(
        "--preflight-editor",
        action="store_true",
        help="네이버 에디터 사전 진단을 실행한다",
    )
    parser.add_argument(
        "--preflight-soft",
        action="store_true",
        help="에디터 사전 진단 실패를 경고로만 처리한다",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> int:
    app_config = load_config()
    setup_logging(
        level=args.log_level or app_config.logging.level,
        log_format=app_config.logging.format,
    )

    blog_id = os.getenv("NAVER_BLOG_ID", "").strip()
    if not blog_id:
        print("오류: NAVER_BLOG_ID 환경변수가 필요합니다.")
        return 2

    # 기본은 임시저장이다. 실제 발행은 --publish를 명시해야만 가능하다.
    os.environ["NAVER_PUBLISH_MODE"] = "publish" if args.publish else "draft"
    if args.headful:
        os.environ["PLAYWRIGHT_HEADLESS"] = "false"
    elif "PLAYWRIGHT_HEADLESS" not in os.environ:
        os.environ["PLAYWRIGHT_HEADLESS"] = "true" if app_config.publisher.headless else "false"
    if args.browser_channel:
        os.environ["PLAYWRIGHT_BROWSER_CHANNEL"] = str(args.browser_channel).strip()
    if args.preflight_editor:
        os.environ["NAVER_EDITOR_PREFLIGHT"] = "true"
    if args.preflight_soft:
        os.environ["NAVER_EDITOR_PREFLIGHT_STRICT"] = "false"

    store = JobStore(db_path=args.db)
    job_id = str(args.job_id or "").strip()
    job = store.get_job(job_id)
    if not job:
        print(f"오류: job을 찾지 못했습니다. job_id={job_id}")
        return 3
    if job.status != store.STATUS_READY:
        print(f"오류: ready_to_publish 상태가 아닙니다. status={job.status}")
        return 4
    payload = store.load_prepared_payload(job_id)
    if not payload:
        print("오류: prepared_payload가 없습니다.")
        return 5

    if not store.update_job_status(job_id, store.STATUS_RUNNING):
        print("오류: running 상태 전환 실패")
        return 6
    claimed = store.get_job(job_id)
    if not claimed:
        print("오류: running 전환 후 job 조회 실패")
        return 7

    pipeline = PipelineService(
        job_store=store,
        publisher=PlaywrightPublisher(blog_id=blog_id),
        generate_fn=stub_generate_fn,
        metrics_store=MetricsStore(db_path=args.db),
        retry_max_attempts=app_config.retry.max_retries,
        retry_backoff_base_sec=app_config.retry.backoff_base_sec,
        retry_backoff_max_sec=app_config.retry.backoff_max_sec,
        notifier=TelegramNotifier.from_env(db_path=store.db_path),
    )

    mode = os.environ.get("NAVER_PUBLISH_MODE", "publish")
    print(f"처리 시작: job_id={job_id}, mode={mode}")
    ok = await pipeline.process_publication(claimed)
    updated = store.get_job(job_id)
    print(f"처리 결과: ok={ok}, status={updated.status if updated else 'missing'}")
    if updated and updated.result_url:
        print(f"result_url={updated.result_url}")
    return 0 if ok else 8


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
