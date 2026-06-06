#!/usr/bin/env python3
"""매크로 글 후보를 블로그 작성 큐로 승격하는 CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from modules.automation.job_store import JobConfig, JobStore
from modules.macro.job_promoter import MacroCandidatePromoter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="매크로 후보를 블로그 작성 큐로 승격")
    parser.add_argument("--db", default="auto_blog.db", help="SQLite DB 경로")
    parser.add_argument("--candidate-id", default="", help="승격할 후보 ID")
    parser.add_argument("--document-id", default="", help="상위 후보를 승격할 문서 ID")
    parser.add_argument("--limit", type=int, default=1, help="문서 기준 승격 개수")
    parser.add_argument("--min-score", type=float, default=85.0, help="승격 최소 품질 점수")
    parser.add_argument("--scheduled-at", default="", help="UTC ISO 예약 시각")
    parser.add_argument("--status", default="queued", choices=["queued", "pending"], help="생성할 잡 상태")
    parser.add_argument("--json", action="store_true", help="JSON 출력")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    store = JobStore(str(args.db), config=JobConfig(max_llm_calls_per_job=15))
    promoter = MacroCandidatePromoter(job_store=store)
    if args.candidate_id:
        result = promoter.promote_candidate(
            str(args.candidate_id),
            scheduled_at=str(args.scheduled_at or ""),
            status=str(args.status or "queued"),
        )
        results = [result]
    elif args.document_id:
        results = promoter.promote_top_candidates(
            document_id=str(args.document_id),
            limit=max(1, int(args.limit or 1)),
            min_overall_score=float(args.min_score or 85.0),
        )
    else:
        raise SystemExit("--candidate-id 또는 --document-id 중 하나가 필요합니다.")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for item in results:
            print(f"created {item['job_id']}: {item['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
