#!/usr/bin/env python3
"""정부자료 첨부 텍스트 추출기 점검 CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from modules.automation.job_store import JobConfig, JobStore
from modules.macro.collector import MacroDataCollector


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="정부자료 텍스트 추출기 점검")
    parser.add_argument("--db", default="auto_blog.db", help="SQLite DB 경로")
    parser.add_argument("--source", default="MOTIE", help="수집 기관 코드")
    parser.add_argument("--limit", type=int, default=5, help="점검할 문서 수")
    parser.add_argument("--collect", action="store_true", help="DB 대신 기관 목록에서 새로 후보를 수집")
    parser.add_argument("--download", action="store_true", help="실제 첨부 다운로드와 파싱까지 실행")
    parser.add_argument("--json", action="store_true", help="JSON 출력")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    collector = MacroDataCollector()
    documents = _collect_candidates(collector, args) if args.collect else _load_documents(args)
    rows = []
    for document in documents[: max(1, int(args.limit or 5))]:
        row = {
            "title": document.get("title", ""),
            "url": document.get("url", ""),
            "file_url": document.get("file_url", ""),
            "file_type": document.get("file_type", ""),
            "attachments": document.get("attachments_json", document.get("attachments", [])),
            "toolAvailability": _tool_availability(),
        }
        if args.download:
            result = collector.download_document_text(document)
            parsed = result.get("parsed_json", {}) if isinstance(result.get("parsed_json"), dict) else {}
            row["status"] = result.get("status", "")
            row["textLength"] = len(str(result.get("text", "") or ""))
            row["sourceFileType"] = parsed.get("source_file_type", "")
            row["parser"] = parsed.get("parser", "")
            row["attempts"] = parsed.get("attempts", [])
            row["errorMessage"] = result.get("error_message", "")
        rows.append(row)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    for row in rows:
        print(f"- {row['title']}")
        print(f"  url: {row['url']}")
        print(f"  attachments: {len(row.get('attachments') or [])}")
        print(f"  tools: {row['toolAvailability']}")
        if args.download:
            print(f"  status: {row.get('status')} parser={row.get('parser')} source={row.get('sourceFileType')} text={row.get('textLength')}")
            for attempt in row.get("attempts", [])[:8]:
                print(f"    - {attempt.get('file_type')} {attempt.get('parser')} {attempt.get('status')} text={attempt.get('text_length')}")
    return 0


def _collect_candidates(collector: MacroDataCollector, args: argparse.Namespace) -> list[dict]:
    candidates = collector.check_latest_sources(source=str(args.source or "MOTIE"), limit=max(1, int(args.limit or 5)))
    return [
        {
            "source": item.source,
            "title": item.title,
            "published_at": item.published_at,
            "url": item.url,
            "file_url": item.file_url,
            "file_type": item.file_type,
            "attachments_json": list(item.attachments),
            "hash": item.hash,
        }
        for item in candidates
    ]


def _load_documents(args: argparse.Namespace) -> list[dict]:
    store = JobStore(str(args.db), config=JobConfig(max_llm_calls_per_job=15))
    return store.list_macro_documents(source=str(args.source or "MOTIE"), limit=max(1, int(args.limit or 5)))


def _tool_availability() -> dict:
    return {
        "kordoc": bool(shutil.which("kordoc")),
        "unhwp": bool(shutil.which("unhwp")),
        "hwp5txt": bool(shutil.which("hwp5txt")),
        "npx": bool(shutil.which("npx")),
    }


if __name__ == "__main__":
    raise SystemExit(main())
