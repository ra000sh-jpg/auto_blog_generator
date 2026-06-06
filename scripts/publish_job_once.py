#!/usr/bin/env python3
"""특정 job_id 하나만 네이버 임시저장/발행 경로로 처리한다."""

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
except Exception:
    pass

from modules.automation.job_store import JobStore
from modules.automation.notifier import TelegramNotifier
from modules.automation.pipeline_service import PipelineService, stub_generate_fn
from modules.config import load_config
from modules.logging_config import setup_logging
from modules.metrics import MetricsStore
from modules.uploaders.playwright_publisher import PlaywrightPublisher


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="특정 job_id 1건만 발행/임시저장 처리")
    parser.add_argument("job_id", help="처리할 job_id")
    parser.add_argument(
        "--db",
        default=os.getenv("AUTOBLOG_DB_PATH", "data/automation.db"),
        help="DB 경로",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="브라우저를 화면에 보이게 실행",
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

    if args.headful:
        os.environ["PLAYWRIGHT_HEADLESS"] = "false"
    elif "PLAYWRIGHT_HEADLESS" not in os.environ:
        os.environ["PLAYWRIGHT_HEADLESS"] = "true" if app_config.publisher.headless else "false"

    dry_run = os.getenv("DRY_RUN", "false").strip().lower() == "true"
    blog_id = os.getenv("NAVER_BLOG_ID", "").strip()
    if not dry_run and not blog_id:
        print("오류: NAVER_BLOG_ID 환경변수가 필요합니다.")
        return 2

    store = JobStore(db_path=args.db)
    job = store.get_job(args.job_id)
    if not job:
        print(f"오류: job을 찾을 수 없습니다: {args.job_id}")
        return 3

    notifier = TelegramNotifier.from_env(db_path=store.db_path)
    pipeline = PipelineService(
        job_store=store,
        publisher=PlaywrightPublisher(blog_id=blog_id or "dry-run"),
        generate_fn=stub_generate_fn,
        metrics_store=MetricsStore(db_path=args.db),
        notifier=notifier,
    )

    print(f"job_id: {job.job_id}")
    print(f"status(before): {job.status}")
    if job.status != store.STATUS_RUNNING:
        if not store.update_job_status(job.job_id, store.STATUS_RUNNING):
            print(f"오류: job을 running 상태로 전환하지 못했습니다: {job.job_id}")
            return 4
        job = store.get_job(job.job_id) or job

    ok = await pipeline.process_publication(job)
    # 텔레그램 확인 링크 전송은 백그라운드 태스크로 예약될 수 있어,
    # 1회성 스크립트 종료 직전 짧게 여유를 둔다.
    if ok:
        await asyncio.sleep(2.0)
    updated = store.get_job(job.job_id)
    print(f"status(after): {updated.status if updated else 'missing'}")
    print(f"result_url: {updated.result_url if updated else ''}")
    return 0 if ok else 1


def main() -> None:
    raise SystemExit(asyncio.run(main_async(parse_args())))


if __name__ == "__main__":
    main()
