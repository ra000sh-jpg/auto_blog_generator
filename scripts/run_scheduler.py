#!/usr/bin/env python3
"""자동화 스케줄러 실행 스크립트."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

from modules.automation.scheduler_service import run_scheduler_forever
from modules.config import load_config
from modules.logging_config import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Auto Blog Scheduler")
    parser.add_argument(
        "--daily-target",
        type=int,
        default=3,
        help="일일 포스팅 목표 (기본: 3)",
    )
    parser.add_argument(
        "--min-interval",
        type=int,
        default=65,
        help="최소 포스팅 간격(분) (기본: 65)",
    )
    parser.add_argument(
        "--max-interval",
        type=int,
        default=110,
        help="최대 포스팅 간격(분) (기본: 110)",
    )
    parser.add_argument(
        "--cpu-threshold",
        type=float,
        default=35.0,
        help="(호환) CPU 상한 임계값(%%) - 미지정 시 stop-threshold와 동일",
    )
    parser.add_argument(
        "--cpu-start-threshold",
        type=float,
        default=28.0,
        help="CPU 히스테리시스 시작 임계값(%%) (기본: 28)",
    )
    parser.add_argument(
        "--cpu-stop-threshold",
        type=float,
        default=35.0,
        help="CPU 히스테리시스 정지 임계값(%%) (기본: 35)",
    )
    parser.add_argument(
        "--cpu-window-size",
        type=int,
        default=5,
        help="CPU 이동평균 샘플 수 (기본: 5)",
    )
    parser.add_argument(
        "--memory-threshold",
        type=float,
        default=80.0,
        help="초안 생성 허용 메모리 사용률 상한(%%) (기본: 80)",
    )
    parser.add_argument(
        "--generator-poll-sec",
        type=int,
        default=30,
        help="초안 생성 워커 폴링 간격(초) (기본: 30)",
    )
    parser.add_argument(
        "--publisher-poll-sec",
        type=int,
        default=20,
        help="발행 워커 폴링 간격(초) (기본: 20)",
    )
    parser.add_argument(
        "--schedule-seed",
        type=int,
        default=None,
        help="가중 분포 시간 계산용 고정 시드(테스트용)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config = load_config()
    setup_logging(
        level=config.logging.level,
        log_format=config.logging.format,
    )

    logger = logging.getLogger("run_scheduler")
    logger.info(
        "Starting scheduler",
        extra={
            "daily_target": args.daily_target,
            "min_interval_minutes": args.min_interval,
            "max_interval_minutes": args.max_interval,
            "cpu_start_threshold_percent": args.cpu_start_threshold,
            "cpu_stop_threshold_percent": args.cpu_stop_threshold,
            "cpu_window_size": args.cpu_window_size,
            "memory_threshold_percent": args.memory_threshold,
            "generator_poll_seconds": args.generator_poll_sec,
            "publisher_poll_seconds": args.publisher_poll_sec,
            "schedule_seed": args.schedule_seed,
        },
    )

    try:
        cpu_stop_threshold = args.cpu_stop_threshold
        # 호환 옵션(--cpu-threshold)이 기본값과 다르면 stop 임계값에 반영한다.
        if args.cpu_threshold != 35.0:
            cpu_stop_threshold = args.cpu_threshold

        asyncio.run(
            run_scheduler_forever(
                daily_posts_target=args.daily_target,
                min_post_interval_minutes=args.min_interval,
                publish_interval_max_minutes=args.max_interval,
                cpu_start_threshold_percent=args.cpu_start_threshold,
                cpu_stop_threshold_percent=cpu_stop_threshold,
                cpu_avg_window=args.cpu_window_size,
                memory_threshold_percent=args.memory_threshold,
                generator_poll_seconds=args.generator_poll_sec,
                publisher_poll_seconds=args.publisher_poll_sec,
                random_seed=args.schedule_seed,
            )
        )
    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user")


if __name__ == "__main__":
    main()
