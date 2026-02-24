"""
Worker 실행 CLI

사용법:
    python scripts/run_worker.py
    python scripts/run_worker.py --poll-interval 30 --max-concurrent 3
    DRY_RUN=true python scripts/run_worker.py

환경변수:
    NAVER_BLOG_ID       네이버 블로그 ID (필수, DRY_RUN=false 시)
    PLAYWRIGHT_HEADLESS true(기본) / false
    DRY_RUN             true / false(기본)

옵션:
    --poll-interval     폴링 간격 초 (기본: 30)
    --max-concurrent    동시 처리 최대 건수 (기본: 3)
    --mode              all | generator | publisher (기본: all)
    --db                DB 경로 (기본: data/automation.db)
    --log-level         로그 레벨 (기본: INFO)
    --use-llm           Claude 기반 생성기 사용 (기본: stub)
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# .env 파일 자동 로드
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # python-dotenv 미설치 시 환경변수 직접 설정 필요

from modules.config import load_config
from modules.logging_config import setup_logging
from modules.metrics import MetricsStore
from modules.automation.job_store import JobStore
from modules.automation.worker import Worker, WorkerConfig
from modules.automation.pipeline_service import PipelineService, stub_generate_fn
from modules.uploaders.playwright_publisher import PlaywrightPublisher


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="자동 블로그 발행 워커",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--poll-interval", type=int, default=30)
    parser.add_argument("--max-concurrent", type=int, default=3)
    parser.add_argument(
        "--mode",
        default="all",
        choices=["all", "generator", "publisher"],
        help="실행 모드 (all=생성+발행, generator=생성 전용, publisher=발행 전용)",
    )
    parser.add_argument("--db", default="data/automation.db")
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="LLM 기반 생성기 사용 (기본: stub)",
    )
    return parser.parse_args()


async def _run_hybrid_mode_loop(
    mode: str,
    pipeline: PipelineService,
    poll_interval_sec: int,
    max_concurrent: int,
) -> None:
    """하이브리드 큐 모드(generator/publisher)를 단순 폴링으로 실행한다."""
    logger = logging.getLogger("run_worker")

    while True:
        processed_count = 0
        for _ in range(max(1, max_concurrent)):
            try:
                if mode == "generator":
                    handled = await pipeline.prepare_next_pending_job()
                elif mode == "publisher":
                    handled = await pipeline.publish_next_ready_job()
                else:
                    handled = await pipeline.run_next_pending_job()
            except Exception as exc:
                logger.exception("Hybrid mode loop error: %s", exc)
                handled = False

            if not handled:
                break
            processed_count += 1

        if processed_count <= 0:
            await asyncio.sleep(max(1, poll_interval_sec))
        else:
            # 처리 중일 때는 짧게 쉬며 다음 건을 이어간다.
            await asyncio.sleep(0.3)


async def main_async(args: argparse.Namespace):
    app_config = load_config()
    if "PLAYWRIGHT_HEADLESS" not in os.environ:
        os.environ["PLAYWRIGHT_HEADLESS"] = "true" if app_config.publisher.headless else "false"

    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    blog_id = os.getenv("NAVER_BLOG_ID", "")

    if not dry_run and not blog_id:
        print("오류: NAVER_BLOG_ID 환경변수가 필요합니다.")
        print("  export NAVER_BLOG_ID=your_blog_id")
        print("  또는 드라이런: DRY_RUN=true python scripts/run_worker.py")
        sys.exit(1)

    logger = logging.getLogger("run_worker")
    logger.info(f"Worker starting (dry_run={dry_run}, blog_id={blog_id or 'N/A'})")

    # 컴포넌트 초기화
    store = JobStore(db_path=args.db)
    metrics_store = MetricsStore(db_path=args.db)
    publisher = PlaywrightPublisher(blog_id=blog_id or "dry-run")

    generate_fn = stub_generate_fn
    if args.use_llm:
        try:
            from modules.llm import get_generator, llm_generate_fn

            # 시작 시점에 LLM 초기화하여 키/설정 오류를 조기 확인한다.
            get_generator(app_config.llm)
            generate_fn = llm_generate_fn
        except Exception as exc:
            logger.exception("LLM initialization failed: %s", exc)
            print(f"오류: LLM 초기화 실패 - {exc}")
            sys.exit(1)

    image_generator = None
    if app_config.images.enabled:
        try:
            from modules.images.runtime_factory import build_runtime_image_generator

            image_generator = build_runtime_image_generator(
                app_config=app_config,
                job_store=store,
            )
            if image_generator:
                logger.info("Image generator initialized via runtime factory")
        except Exception as exc:
            logger.warning("Image generator initialization skipped: %s", exc)

    quality_evaluator = None
    if args.use_llm:
        try:
            from modules.llm.provider_factory import create_client
            from modules.automation.quality_evaluator import QualityEvaluator

            eval_client = create_client(
                provider=app_config.llm.primary_provider,
                model=app_config.llm.primary_model,
                timeout_sec=app_config.llm.timeout_sec,
            )
            quality_evaluator = QualityEvaluator(llm_client=eval_client)
            logger.info("QualityEvaluator initialized for worker")
        except Exception as exc:
            logger.warning("QualityEvaluator initialization skipped: %s", exc)

    pipeline = PipelineService(
        job_store=store,
        publisher=publisher,
        generate_fn=generate_fn,
        metrics_store=metrics_store,
        retry_max_attempts=app_config.retry.max_retries,
        retry_backoff_base_sec=app_config.retry.backoff_base_sec,
        retry_backoff_max_sec=app_config.retry.backoff_max_sec,
        image_generator=image_generator,
        quality_evaluator=quality_evaluator,
    )

    worker_config = WorkerConfig(
        poll_interval_sec=args.poll_interval,
        max_concurrent_jobs=args.max_concurrent,
    )

    print(f"Worker 시작 (poll={args.poll_interval}s, concurrent={args.max_concurrent})")
    print(f"DB: {args.db}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"Queue Mode: {args.mode}")
    print(f"Generate: {'LLM' if args.use_llm else 'STUB'}")
    print("종료: Ctrl+C")
    print()

    # 초기 큐 상태 출력
    stats = store.get_queue_stats()
    if stats:
        print(f"현재 큐 상태: {stats}")

    try:
        if args.mode == "all":
            worker = Worker(
                job_store=store,
                process_job=pipeline.run_job,
                config=worker_config,
            )
            await worker.run()
        else:
            await _run_hybrid_mode_loop(
                mode=args.mode,
                pipeline=pipeline,
                poll_interval_sec=args.poll_interval,
                max_concurrent=args.max_concurrent,
            )
    finally:
        if image_generator:
            await image_generator.close()


def main():
    args = parse_args()
    app_config = load_config()
    setup_logging(
        level=args.log_level or app_config.logging.level,
        log_format=app_config.logging.format,
    )

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nWorker 종료됨")


if __name__ == "__main__":
    main()
