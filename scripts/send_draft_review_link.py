#!/usr/bin/env python3
"""임시저장 확인 링크를 텔레그램으로 재전송한다."""

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="네이버 임시저장 확인 링크 텔레그램 재전송")
    parser.add_argument("job_id", help="확인 링크를 보낼 job_id")
    parser.add_argument("--url", default="", help="직접 지정할 확인 URL")
    parser.add_argument(
        "--db",
        default=os.getenv("AUTOBLOG_DB_PATH", "data/automation.db"),
        help="DB 경로",
    )
    parser.add_argument(
        "--mark-completed",
        action="store_true",
        help="전송 후 job 상태를 completed로 보정",
    )
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> int:
    store = JobStore(args.db)
    job = store.get_job(args.job_id)
    if not job:
        print(f"오류: job을 찾을 수 없습니다: {args.job_id}")
        return 2

    payload = store.load_prepared_payload(args.job_id)
    archive = store.get_post_text_archive(args.job_id) or {}
    url = str(args.url or archive.get("result_url") or job.result_url or "").strip()
    if not url:
        print("오류: 확인 URL이 없습니다.")
        return 3

    notifier = TelegramNotifier.from_env(db_path=store.db_path)
    if not notifier.enabled:
        print("오류: Telegram 설정이 없습니다.")
        return 4

    publish_mode = str(os.getenv("NAVER_PUBLISH_MODE", "publish")).strip().lower()
    is_draft_mode = publish_mode == "draft"
    title = str(payload.get("title", job.title)).strip()
    content_len = len(str(payload.get("content", "") or ""))
    message = "\n".join(
        [
            "네이버 임시저장 완료" if is_draft_mode else "네이버 발행 완료",
            "",
            f"제목: {title}",
            f"job_id: {job.job_id}",
            f"본문 길이: {content_len}자",
            "",
            "확인 링크:",
            url,
            "",
            "스마트폰에서 최종 확인 후 네이버 화면에서 직접 발행해 주세요."
            if is_draft_mode
            else "스마트폰에서 게시 상태를 확인해 주세요.",
        ]
    )
    reply_markup = {
        "inline_keyboard": [
            [{"text": "임시저장 열기" if is_draft_mode else "게시글 열기", "url": url}],
            [
                {"text": "확인완료", "callback_data": f"ads:v1:c:{job.job_id}"},
                {"text": "보류", "callback_data": f"ads:v1:h:{job.job_id}"},
            ],
        ]
    }
    sent = await notifier.send_message(
        message,
        disable_notification=False,
        reply_markup=reply_markup,
    )
    if not sent:
        print("오류: 텔레그램 전송 실패")
        return 5

    if args.mark_completed:
        store.update_job_status(args.job_id, store.STATUS_RUNNING)
        store.complete_job(
            args.job_id,
            result_url=url,
            thumbnail_url=str(payload.get("thumbnail", "")),
            quality_snapshot=payload.get("quality_snapshot", {}),
            seo_snapshot=payload.get("seo_snapshot", {}),
        )
    print(f"sent: yes")
    print(f"url: {url}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async(parse_args())))


if __name__ == "__main__":
    main()
