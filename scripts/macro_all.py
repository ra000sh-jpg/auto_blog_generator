#!/usr/bin/env python3
"""정부 매크로 자료 수집/분석 1회 실행 CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.notifier import TelegramNotifier
from modules.macro.pipeline import MacroPipeline
from modules.macro.reference_verifier import MacroReferenceVerifier


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="정부 매크로 자료 수집/분석")
    parser.add_argument("--source", default="MOTIE", help="수집 기관 코드")
    parser.add_argument("--limit", type=int, default=5, help="수집 후보 최대 개수")
    parser.add_argument("--db", default="auto_blog.db", help="SQLite DB 경로")
    parser.add_argument("--send-telegram", action="store_true", help="텔레그램 검토 메시지 전송")
    parser.add_argument("--verify-network", action="store_true", help="KOSIS 등 보조 공식 API 검증 실행")
    parser.add_argument("--json", action="store_true", help="JSON 요약 출력")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    store = JobStore(str(args.db), config=JobConfig(max_llm_calls_per_job=15))
    notifier = TelegramNotifier.from_env(db_path=str(args.db)) if args.send_telegram else None
    pipeline = MacroPipeline(
        job_store=store,
        notifier=notifier,
        reference_verifier=MacroReferenceVerifier(allow_network=bool(args.verify_network)),
    )
    result = pipeline.run_once(
        source=str(args.source or "MOTIE"),
        limit=max(1, int(args.limit or 5)),
        send_telegram=bool(args.send_telegram),
    )

    summary = {
        "source": result["source"],
        "discovered": result["discovered"],
        "stored": result["stored"],
        "analyzed": result["analyzed"],
        "document_titles": [
            str(item.get("document", {}).get("title", ""))
            for item in result.get("documents", [])
        ],
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"source={summary['source']} discovered={summary['discovered']} stored={summary['stored']} analyzed={summary['analyzed']}")
        for title in summary["document_titles"]:
            if title:
                print(f"- {title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
