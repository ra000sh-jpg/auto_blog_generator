"""
작업 예약 CLI

사용법:
    python scripts/schedule_post.py \\
        --title "초등 겨울방학 체험학습 추천" \\
        --keywords "겨울방학,체험학습,초등" \\
        --time "2026-02-21T09:00:00+09:00" \\
        --platform naver \\
        --persona P1

옵션:
    --title     포스트 제목 (필수)
    --keywords  쉼표 구분 키워드 (필수)
    --time      예약 시간 ISO 8601 (기본: 즉시)
    --platform  플랫폼 naver|tistory (기본: naver)
    --persona   페르소나 ID (기본: default)
    --db        DB 경로 (기본: data/automation.db)
"""

import argparse
import sys
import uuid
import logging
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.automation.job_store import JobStore
from modules.automation.time_utils import now_utc, parse_iso
from modules.config import load_config
from modules.logging_config import setup_logging

logger = logging.getLogger("schedule_post")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="블로그 발행 작업 예약",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument("--title", required=True, help="포스트 제목")
    parser.add_argument("--keywords", required=True, help="쉼표 구분 키워드")
    parser.add_argument(
        "--time",
        default=None,
        help="예약 시간 ISO 8601 (기본: 즉시 실행)",
    )
    parser.add_argument("--platform", default="naver", choices=["naver", "tistory"])
    parser.add_argument("--persona", default="default")
    parser.add_argument("--db", default="data/automation.db")
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="최대 재시도 횟수 (기본: 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB에 등록만 하고 워커 실행 안 함",
    )

    return parser.parse_args()


def main():
    app_config = load_config()
    setup_logging(level=app_config.logging.level, log_format=app_config.logging.format)
    args = parse_args()

    # 키워드 파싱
    keywords = [kw.strip() for kw in args.keywords.split(",") if kw.strip()]
    if not keywords:
        logger.error("유효한 키워드가 없습니다.")
        sys.exit(1)

    # 예약 시간 처리
    if args.time:
        try:
            dt = parse_iso(args.time)
            scheduled_at = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError as e:
            logger.error(f"시간 형식 오류: {e}")
            sys.exit(1)
    else:
        scheduled_at = now_utc()

    # Job 등록
    store = JobStore(db_path=args.db)
    job_id = str(uuid.uuid4())

    success = store.schedule_job(
        job_id=job_id,
        title=args.title,
        seed_keywords=keywords,
        platform=args.platform,
        persona_id=args.persona,
        scheduled_at=scheduled_at,
        max_retries=args.max_retries,
    )

    if success:
        print("✓ 작업 등록 완료")
        print(f"  job_id     : {job_id}")
        print(f"  title      : {args.title}")
        print(f"  keywords   : {', '.join(keywords)}")
        print(f"  platform   : {args.platform}")
        print(f"  persona    : {args.persona}")
        print(f"  scheduled  : {scheduled_at} (UTC)")

        # 큐 통계 출력
        stats = store.get_queue_stats()
        print(f"\n현재 큐 상태: {stats}")
    else:
        print("✗ 작업 등록 실패 (중복 작업일 수 있습니다)")
        sys.exit(1)


if __name__ == "__main__":
    main()
