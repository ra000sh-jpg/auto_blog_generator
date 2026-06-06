#!/usr/bin/env python3
"""국장/미장/통찰형 샘플 글 3개를 생성하고 품질 요약을 저장한다."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass

from modules.automation.job_store import Job
from modules.automation.job_store import JobStore
from modules.config import load_config
from modules.llm import get_generator, reset_generator


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="시장 브리핑 샘플 글 3개 품질 스모크")
    parser.add_argument(
        "--output-dir",
        default="data/smoke_samples",
        help="샘플 결과 저장 디렉터리",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="품질 체크/SEO/보이스 리라이트를 생략한 빠른 초안 테스트",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("AUTOBLOG_DB_PATH", "data/automation.db"),
        help="라우터 설정을 읽을 DB 경로",
    )
    parser.add_argument(
        "--fail-on-quality",
        action="store_true",
        help="샘플 중 하나라도 로컬 품질 기준을 통과하지 못하면 비정상 종료",
    )
    parser.add_argument(
        "--slots",
        default="kr,us,evergreen",
        help="생성할 샘플 슬롯 목록. 예: kr 또는 kr,us,evergreen",
    )
    return parser.parse_args()


def build_sample_jobs(slots: str = "kr,us,evergreen") -> list[Job]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    day = datetime.now().strftime("%Y-%m-%d")
    requested = {
        item.strip().lower()
        for item in str(slots or "").split(",")
        if item.strip()
    } or {"kr", "us", "evergreen"}
    jobs: list[Job] = []
    if "kr" in requested:
        jobs.append(
            Job(
                job_id=f"sample-kr-{int(time.time())}",
                status="running",
                title=f"{day} 국장 개장 전 브리핑 - 밤사이 데이터가 남긴 기준",
                seed_keywords=["국장", "미국 증시", "환율", "반도체", "외국인 수급"],
                platform="naver",
                persona_id="P4",
                scheduled_at=now,
                category="경제 브리핑",
                tags=["market_daily", "market_slot:kr_preopen", "market_scope:kr"],
            )
        )
    if "us" in requested:
        jobs.append(
            Job(
                job_id=f"sample-us-{int(time.time())}",
                status="running",
                title=f"{day} 미장 개장 전 브리핑 - 아시아와 선물이 말해주는 기준",
                seed_keywords=["미장", "나스닥 선물", "아시아 증시", "비트코인", "미국 금리"],
                platform="naver",
                persona_id="P4",
                scheduled_at=now,
                category="경제 브리핑",
                tags=["market_daily", "market_slot:us_preopen", "market_scope:us"],
            )
        )
    if "evergreen" in requested:
        jobs.append(
            Job(
                job_id=f"sample-evergreen-{int(time.time())}",
                status="running",
                title=f"{day} 투자 공부 노트 - 예측보다 먼저 세울 기준",
                seed_keywords=["투자 공부", "리스크 관리", "기록 습관", "초심자", "선택과 집중"],
                platform="naver",
                persona_id="P4",
                scheduled_at=now,
                category="경제 브리핑",
                tags=["market_daily", "market_slot:evergreen_insight", "market_scope:evergreen"],
            )
        )
    return jobs


def evaluate_local_quality(content: str, result: Any) -> dict[str, Any]:
    """LLM 점수 외에 운영자가 빠르게 볼 수 있는 휴리스틱을 계산한다."""

    insight_quality = dict(result.quality_snapshot.get("insight_quality", {}) or {})
    market_snapshot = dict(result.seo_snapshot.get("market_snapshot", {}) or {})
    learning_markers = ("함께", "저도", "배워", "공부", "기록", "점검")
    reference_count = content.count("참고 자료:")
    heading_count = content.count("\n#") + (1 if content.lstrip().startswith("#") else 0)
    paragraph_count = len([part for part in content.split("\n\n") if part.strip()])
    forbidden_patterns = (
        "昨日",
        "最新",
        "同時",
        "市場",
        "ボ",
        "गलत",
        "interessring",
        "interesting",
        "이 주제",
        "이 내용",
        "이 접근",
        "카페 운영",
        "카페를 운영",
    )
    forbidden_hits = [pattern for pattern in forbidden_patterns if pattern in content]
    repeated_generic_phrases = [
        phrase
        for phrase in ("중요합니다", "도움이 됩니다", "전략을 조정할 수 있습니다")
        if content.count(phrase) >= 2
    ]
    insight_score = int(insight_quality.get("overall_score", 0) or 0)
    plain_language_score = int(insight_quality.get("plain_language_score", 0) or 0)
    learning_tone_score = int(insight_quality.get("learning_tone_score", 0) or 0)
    insight_pass = (
        insight_score >= 85
        and plain_language_score >= 70
        and learning_tone_score >= 75
        and not bool(insight_quality.get("needs_rewrite", False))
    )
    local_pass = (
        len(content) >= 900
        and heading_count >= 2
        and paragraph_count >= 5
        and any(marker in content for marker in learning_markers)
        and not forbidden_hits
        and not repeated_generic_phrases
        and insight_pass
    )
    return {
        "content_length": len(content),
        "heading_count": heading_count,
        "paragraph_count": paragraph_count,
        "reference_count": reference_count,
        "has_learning_voice": any(marker in content for marker in learning_markers),
        "forbidden_hits": forbidden_hits,
        "repeated_generic_phrases": repeated_generic_phrases,
        "quality_gate": result.quality_gate,
        "quality_score": result.quality_snapshot.get("score"),
        "insight_quality": insight_quality,
        "plain_language_pass": plain_language_score >= 70,
        "learning_tone_pass": learning_tone_score >= 75,
        "insight_pass": insight_pass,
        "market_snapshot": market_snapshot,
        "local_pass": local_pass,
    }


async def generate_one(job: Job, output_dir: Path, fast: bool, job_store: JobStore | None) -> dict[str, Any]:
    config = load_config().llm
    if fast:
        config = replace(
            config,
            enable_quality_check=False,
            enable_seo_optimization=False,
            enable_voice_rewrite=False,
            enable_fact_check=False,
            max_rewrites=0,
        )

    reset_generator()
    generator = get_generator(config=config, job_store=job_store, job=job)
    started_at = time.perf_counter()
    try:
        result = await generator.generate(job)
    finally:
        try:
            await generator.aclose()
        except Exception:
            pass
    elapsed_sec = round(time.perf_counter() - started_at, 2)

    file_stem = job.job_id.replace("sample-", "")
    content_path = output_dir / f"{file_stem}.md"
    content_path.write_text(result.final_content, encoding="utf-8")
    quality = evaluate_local_quality(result.final_content, result)
    return {
        "job_id": job.job_id,
        "title": job.title,
        "content_path": str(content_path),
        "elapsed_sec": elapsed_sec,
        "provider": result.provider_used,
        "model": result.provider_model,
        "llm_calls_used": result.llm_calls_used,
        "quality": quality,
    }


async def main_async() -> None:
    args = parse_args()
    run_dir = PROJECT_ROOT / args.output_dir / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    summaries = []
    job_store = JobStore(str(PROJECT_ROOT / args.db)) if args.db else None
    for job in build_sample_jobs(args.slots):
        print(f"[sample] generating: {job.title}")
        summaries.append(await generate_one(job, run_dir, args.fast, job_store))

    summary_path = run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "fast": bool(args.fast),
                "samples": summaries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"summary_path": str(summary_path), "sample_count": len(summaries)}, ensure_ascii=False))
    if args.fail_on_quality and any(not sample["quality"].get("local_pass") for sample in summaries):
        raise SystemExit(1)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
