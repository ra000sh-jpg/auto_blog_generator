#!/usr/bin/env python3
"""
Blog Generation CLI Test Tool
=============================
코덱스(Claude Code)에서 주제를 던지면 파이프라인을 바로 실행하여
생성된 블로그 글을 터미널에서 즉시 확인할 수 있는 스크립트.

사용법:
    # 기본 (cafe 페르소나)
    python3 tools/test_generate.py "봄맞이 카페 인테리어"

    # 키워드 여러 개 + 페르소나 지정
    python3 tools/test_generate.py "AI 활용법" "업무 자동화" --persona it

    # HTML 파일로 저장
    python3 tools/test_generate.py "육아 꿀팁" --persona parenting -o result.html

    # 빠른 초안만 확인 (품질체크/SEO/보이스 생략)
    python3 tools/test_generate.py "다이어트 식단" --fast
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone

# 프로젝트 루트를 sys.path에 추가
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from modules.config import load_config, LLMConfig
from modules.automation.job_store import Job
from modules.llm import get_generator, reset_generator

# ─── 페르소나 매핑 ────────────────────────────────────────────
PERSONA_MAP = {
    "cafe": "P1",
    "it": "P2",
    "parenting": "P3",
    "finance": "P4",
    "economy": "P5",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Blog Generation CLI Test - 주제를 던지면 바로 생성 결과를 확인",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python3 tools/test_generate.py "봄맞이 카페 인테리어"
  python3 tools/test_generate.py "AI 활용법" "업무 자동화" --persona it
  python3 tools/test_generate.py "다이어트 식단" --fast
  python3 tools/test_generate.py "육아 꿀팁" -o result.html
        """,
    )
    parser.add_argument(
        "keywords",
        nargs="+",
        help="생성할 블로그 주제 키워드 (여러 개 가능)",
    )
    parser.add_argument(
        "--persona", "-p",
        default="cafe",
        choices=list(PERSONA_MAP.keys()),
        help="페르소나 선택 (기본: cafe)",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="결과를 파일로 저장 (예: result.html)",
    )
    parser.add_argument(
        "--fast", "-f",
        action="store_true",
        help="빠른 모드: 품질체크/SEO/보이스 생략, 초안만 확인",
    )
    parser.add_argument(
        "--title", "-t",
        default=None,
        help="블로그 제목 직접 지정 (미지정 시 첫 키워드 사용)",
    )
    return parser.parse_args()


def print_header(keywords: list[str], persona: str, fast: bool) -> None:
    persona_id = PERSONA_MAP.get(persona, "P1")
    mode = "FAST (초안만)" if fast else "FULL (전체 파이프라인)"
    print()
    print("=" * 50)
    print(f"  Blog Generation Test")
    print(f"  키워드: {', '.join(keywords)}")
    print(f"  페르소나: {persona} ({persona_id})")
    print(f"  모드: {mode}")
    print("=" * 50)
    print()


def print_result(result, elapsed: float) -> None:
    # 품질 점수 추출
    quality_score = "N/A"
    if result.quality_snapshot:
        score = result.quality_snapshot.get("overall_score")
        if score is None:
            score = result.quality_snapshot.get("score")
        if score is not None:
            quality_score = f"{score}/100"

    print()
    print(f"  생성 완료! ({elapsed:.1f}초 소요)")
    print()
    print(f"  메타 정보:")
    print(f"  - Provider: {result.provider_used or 'N/A'} / {result.provider_model or 'N/A'}")
    if result.provider_fallback_from:
        print(f"  - Fallback From: {result.provider_fallback_from}")
    print(f"  - LLM 호출 수: {result.llm_calls_used}회")
    print(f"  - Voice Rewrite: {'적용됨' if result.voice_rewrite_applied else '미적용'}")
    print(f"  - 품질 점수: {quality_score}")
    print(f"  - 생성 방식: {result.generation_method or 'N/A'}")
    print(f"  - Rewrite 횟수: {result.rewrite_count}")
    print()

    # 토큰 사용량
    if result.llm_token_usage:
        total_input = sum(v.get("input_tokens", 0) for v in result.llm_token_usage.values())
        total_output = sum(v.get("output_tokens", 0) for v in result.llm_token_usage.values())
        if total_input or total_output:
            print(f"  토큰 사용량: 입력 {total_input:,} / 출력 {total_output:,}")
            print()

    # 본문 출력
    print("-" * 50)
    print("  생성된 본문:")
    print("-" * 50)
    print()
    print(result.final_content)
    print()

    # SEO 스냅샷
    if result.seo_snapshot:
        print("-" * 50)
        print("  SEO 스냅샷:")
        print("-" * 50)
        print(json.dumps(result.seo_snapshot, ensure_ascii=False, indent=2))
        print()

    # 이미지 프롬프트
    if result.image_prompts:
        print("-" * 50)
        print("  이미지 프롬프트:")
        print("-" * 50)
        for i, prompt in enumerate(result.image_prompts, 1):
            print(f"  {i}. {prompt}")
        print()

    # 품질 스냅샷 상세
    if result.quality_snapshot:
        print("-" * 50)
        print("  품질 스냅샷:")
        print("-" * 50)
        print(json.dumps(result.quality_snapshot, ensure_ascii=False, indent=2))
        print()


async def run_generation(args: argparse.Namespace) -> None:
    config = load_config()
    llm_config: LLMConfig = config.llm

    # --fast 모드: 품질 체크, SEO, 보이스 리라이트 비활성화
    if args.fast:
        llm_config = replace(
            llm_config,
            enable_quality_check=False,
            enable_seo_optimization=False,
            enable_voice_rewrite=False,
            enable_fact_check=False,
            max_rewrites=0,
        )

    persona_id = PERSONA_MAP.get(args.persona, "P1")
    title = args.title or args.keywords[0]

    job = Job(
        job_id=f"cli-test-{int(time.time())}",
        status="running",
        title=title,
        seed_keywords=args.keywords,
        platform="naver",
        persona_id=persona_id,
        scheduled_at=datetime.now(timezone.utc).isoformat(),
    )

    print_header(args.keywords, args.persona, args.fast)
    print("  생성 중... (전체 파이프라인 약 30-60초, --fast 약 10초)")
    print()

    # 싱글톤 초기화 후 생성
    reset_generator()
    generator = get_generator(config=llm_config, job=job)

    start_time = time.time()
    try:
        result = await generator.generate(job)
    finally:
        try:
            await generator.aclose()
        except Exception:
            pass

    elapsed = time.time() - start_time
    print_result(result, elapsed)

    # --output: 파일 저장
    if args.output:
        output_path = os.path.join(PROJECT_ROOT, args.output)
        with open(output_path, "w", encoding="utf-8") as f:
            if args.output.endswith(".html"):
                f.write(f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        body {{ font-family: 'Noto Sans KR', sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; line-height: 1.8; }}
        h1 {{ color: #333; }}
        h2 {{ color: #555; margin-top: 30px; }}
        img {{ max-width: 100%; height: auto; }}
    </style>
</head>
<body>
{result.final_content}
</body>
</html>""")
            else:
                f.write(result.final_content)
        print(f"  파일 저장됨: {output_path}")
        print()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run_generation(args))
    except KeyboardInterrupt:
        print("\n  중단됨.")
        sys.exit(1)
    except Exception as exc:
        print(f"\n  오류 발생: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
