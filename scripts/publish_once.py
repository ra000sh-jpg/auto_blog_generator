"""
네이버 실발행 E2E 테스트 스크립트

사용법:
    # 1) 먼저 세션 초기화
    python scripts/naver_login.py

    # 2) 블로그 ID 설정 후 실발행 실행
    NAVER_BLOG_ID=your_blog_id python scripts/publish_once.py \
        --title "테스트 포스팅" \
        --keywords "테스트,블로그,자동화" \
        [--headful]   # 브라우저 화면 보기

환경변수:
    NAVER_BLOG_ID       네이버 블로그 ID (필수)
    PLAYWRIGHT_HEADLESS true(기본) / false (--headful 시 자동 설정)
    DRY_RUN             false (이 스크립트는 항상 실발행)

동작:
    1. JobStore에 즉시 실행 작업 등록
    2. stub 또는 LLM으로 콘텐츠 생성
    3. PlaywrightPublisher로 네이버 블로그에 실제 발행
    4. 결과 URL 출력
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent))

# .env 파일 자동 로드
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # python-dotenv 미설치 시 환경변수 직접 설정 필요

from modules.automation.job_store import JobStore
from modules.automation.pipeline_service import PipelineService, stub_generate_fn
from modules.automation.time_utils import now_utc
from modules.config import load_config
from modules.logging_config import setup_logging
from modules.metrics import MetricsStore
from modules.llm import get_tag_generator
from modules.llm.prompts import get_topic_mode, normalize_topic_mode
from modules.seo.platform_strategy import get_category_for_topic
from modules.uploaders.playwright_publisher import PlaywrightPublisher

logger = logging.getLogger("publish_once")
TOPIC_TO_PERSONA = {
    "cafe": "P1",
    "it": "P2",
    "parenting": "P3",
    "finance": "P4",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="네이버 실발행 E2E 테스트")
    parser.add_argument("--title", default="自動블로그 테스트 발행", help="포스트 제목")
    parser.add_argument("--keywords", default="테스트,블로그자동화,파이썬", help="쉼표 구분 키워드")
    parser.add_argument("--db", default="data/automation.db")
    parser.add_argument(
        "--headful",
        action="store_true",
        help="브라우저 화면 표시 (기본: headless)",
    )
    parser.add_argument(
        "--persona", default="P1", help="페르소나 ID"
    )
    parser.add_argument(
        "--category",
        default=None,
        choices=["cafe", "parenting", "it", "finance", "economy"],
        help="토픽 모드 강제 지정(최우선 적용)",
    )
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="LLM 기반 생성기 사용 (기본: stub)",
    )
    parser.add_argument(
        "--ai-only-images",
        action="store_true",
        help="본문 이미지 전략을 ai_only로 강제하고 스톡 이미지를 비활성화",
    )
    parser.add_argument(
        "--ai-toggle-mode",
        choices=["off", "metadata", "force"],
        default=None,
        help="AI 활용 설정 판정 모드 (기본: 환경변수/metadata)",
    )
    parser.add_argument(
        "--verify-ai-toggle",
        action="store_true",
        help="발행 후 AI 토글 리포트를 읽어 expected/passed 조건을 검증",
    )
    parser.add_argument(
        "--verify-min-expected",
        type=int,
        default=1,
        help="AI 토글 검증 시 요구하는 최소 expected_on 개수",
    )
    return parser.parse_args()


def resolve_topic_and_persona(persona_id: str, category: Optional[str]) -> Tuple[str, str]:
    """토픽/페르소나 우선순위를 결정한다.

    규칙:
    1) --category가 있으면 최우선 (persona 무시)
    2) --category가 없으면 기존 --persona 로직 유지
    """
    if category:
        topic_mode = normalize_topic_mode(category)
        resolved_persona = TOPIC_TO_PERSONA.get(topic_mode, "P1")
        return topic_mode, resolved_persona

    topic_mode = get_topic_mode(persona_id).id
    return topic_mode, persona_id


def validate_session_state_file(session_file: Path) -> bool:
    """Playwright storage state 파일의 최소 유효성을 검증한다."""
    if not session_file.exists():
        return False

    try:
        with session_file.open("r", encoding="utf-8") as file:
            state_data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return False

    if not isinstance(state_data, dict):
        return False

    cookies = state_data.get("cookies", [])
    origins = state_data.get("origins", [])
    return isinstance(cookies, list) and isinstance(origins, list) and len(cookies) > 0


def _provider_label(provider: str) -> str:
    if provider == "qwen":
        return "Qwen"
    if provider == "deepseek":
        return "DeepSeek"
    if provider == "claude":
        return "Claude"
    return provider


def _print_api_health(rows: list[dict], primary_provider: str, secondary_provider: str) -> None:
    print("[API Health Check]")
    status_by_provider: dict[str, str] = {}

    for item in rows:
        provider = str(item.get("provider", "unknown"))
        provider_name = _provider_label(provider)
        model_name = str(item.get("model", "unknown"))
        status = str(item.get("status", "FAIL")).upper()
        message = str(item.get("message", "unknown error"))
        status_by_provider[provider] = status

        if status == "OK":
            print(f"  {provider_name:<8} ({model_name}) : OK   ✓")
        else:
            print(f"  {provider_name:<8} ({model_name}) : FAIL ✗  ({message})")

    if (
        status_by_provider.get(primary_provider, "FAIL") != "OK"
        and status_by_provider.get(secondary_provider, "FAIL") == "OK"
    ):
        print(
            f"WARNING: {_provider_label(primary_provider)} API unavailable. "
            f"Will use {_provider_label(secondary_provider)} as primary."
        )
    print()


def _format_provider_summary(seo_snapshot: dict) -> str:
    provider_used = str(seo_snapshot.get("provider_used", "")).strip()
    provider_model = str(seo_snapshot.get("provider_model", "")).strip()
    fallback_from = str(seo_snapshot.get("provider_fallback_from", "")).strip()
    if not provider_used:
        return ""

    label = _provider_label(provider_used)
    if fallback_from:
        return f"{label} ({provider_model}, {_provider_label(fallback_from)} 폴백)"
    return f"{label} ({provider_model})"


async def run(args: argparse.Namespace):
    app_config = load_config()
    use_llm = bool(getattr(args, "use_llm", False))

    # ── 환경변수 설정 ──────────────────────────────────────
    blog_id = os.getenv("NAVER_BLOG_ID", "").strip()
    if not blog_id:
        print()
        print("❌ NAVER_BLOG_ID 환경변수가 필요합니다.")
        print()
        print("  export NAVER_BLOG_ID=your_blog_id")
        print("  NAVER_BLOG_ID=your_blog_id python scripts/publish_once.py ...")
        print()
        sys.exit(1)

    # headful 모드 적용
    if args.headful:
        os.environ["PLAYWRIGHT_HEADLESS"] = "false"
    elif "PLAYWRIGHT_HEADLESS" not in os.environ:
        os.environ["PLAYWRIGHT_HEADLESS"] = "true" if app_config.publisher.headless else "false"

    # DRY_RUN 강제 비활성화
    os.environ["DRY_RUN"] = "false"
    if args.ai_only_images:
        os.environ["IMAGE_CONTENT_STRATEGY_OVERRIDE"] = "ai_only"
        os.environ["IMAGE_DISABLE_STOCK"] = "true"
    if args.ai_toggle_mode:
        os.environ["NAVER_AI_TOGGLE_MODE"] = args.ai_toggle_mode

    # 세션 파일 체크
    session_file = Path("data/sessions/naver/state.json")
    if not validate_session_state_file(session_file):
        print()
        print("❌ 세션 파일이 없거나 유효하지 않습니다.")
        print()
        print("  먼저 로그인을 진행하세요:")
        print("  python scripts/naver_login.py")
        print()
        sys.exit(1)

    keywords = [kw.strip() for kw in args.keywords.split(",") if kw.strip()]
    topic_mode, resolved_persona_id = resolve_topic_and_persona(
        persona_id=args.persona,
        category=args.category,
    )
    resolved_platform_category = get_category_for_topic(topic_mode, platform="naver")
    job_id = str(uuid.uuid4())
    scheduled_at = now_utc()

    print()
    print("=" * 55)
    print("  네이버 실발행 E2E 테스트")
    print("=" * 55)
    print(f"  블로그 ID : {blog_id}")
    print(f"  제목      : {args.title}")
    print(f"  키워드    : {', '.join(keywords)}")
    print(f"  Job ID    : {job_id}")
    print(f"  토픽 모드 : {topic_mode}")
    print(f"  페르소나  : {resolved_persona_id}")
    print(f"  카테고리  : {resolved_platform_category}")
    print(f"  모드      : {'Headful (화면 표시)' if args.headful else 'Headless'}")
    print(f"  생성기    : {'LLM' if use_llm else 'Stub'}")
    if args.ai_only_images:
        print("  이미지전략 : ai_only (stock disabled)")
    if args.ai_toggle_mode:
        print(f"  토글모드  : {args.ai_toggle_mode}")
    print("=" * 55)
    print()

    generate_fn = stub_generate_fn
    if use_llm:
        try:
            from modules.llm import get_generator, llm_generate_fn
            from modules.llm.api_health import check_all_providers

            # 실행 초기에 API 상태를 가시적으로 출력한다.
            health_rows = await check_all_providers(
                skip_expensive=True,
                llm_config=app_config.llm,
            )
            _print_api_health(
                rows=health_rows,
                primary_provider=app_config.llm.primary_provider,
                secondary_provider=app_config.llm.secondary_provider,
            )

            # 생성기 초기화 실패를 조기 감지한다.
            _ = get_generator(app_config.llm)
            generate_fn = llm_generate_fn
        except Exception as exc:
            logger.exception("LLM initialization failed: %s", exc)
            print(f"❌ LLM 초기화 실패: {exc}")
            sys.exit(1)

    image_generator = None
    if app_config.images.enabled:
        try:
            from modules.images import (
                PollinationsImageClient,
                TogetherImageClient,
                HuggingFaceImageClient,
                PexelsImageClient,
                ImageGenerator,
            )
            from modules.llm.provider_factory import create_client as create_llm_client

            # Primary: Pollinations (무료, API 키 불필요)
            image_client = PollinationsImageClient(
                model=app_config.images.model,
                timeout_sec=app_config.llm.timeout_sec,
                output_dir=app_config.images.output_dir,
            )

            # Fallback 체인: HuggingFace → Together.ai
            fallback_clients = []

            # Fallback #1: Hugging Face Inference (HF_TOKEN 있으면 활성화, 무료 추천)
            hf_client = HuggingFaceImageClient(
                timeout_sec=app_config.llm.timeout_sec,
                output_dir=app_config.images.output_dir,
            )
            if hf_client.is_available():
                fallback_clients.append(hf_client)
                logger.info("HuggingFace image fallback enabled")

            # Fallback #2: Together.ai FLUX (TOGETHER_API_KEY 있으면 활성화)
            together_client = TogetherImageClient(
                timeout_sec=app_config.llm.timeout_sec,
                output_dir=app_config.images.output_dir,
            )
            if together_client.is_available():
                fallback_clients.append(together_client)
                logger.info("Together.ai image fallback enabled")

            # 스톡 포토 클라이언트: Pexels (PEXELS_API_KEY 있으면 활성화)
            stock_client = None
            pexels_client = PexelsImageClient(
                timeout_sec=app_config.llm.timeout_sec,
                output_dir=app_config.images.output_dir,
            )
            if (not args.ai_only_images) and pexels_client.is_available():
                stock_client = pexels_client
                logger.info("Pexels stock photo client enabled")
            elif args.ai_only_images:
                logger.info("Pexels stock photo client disabled by --ai-only-images")

            # Gemini 프롬프트 번역 클라이언트 (GEMINI_API_KEY 없으면 None으로 스킵)
            prompt_translator = None
            if app_config.llm.gemini_image_prompt_translation:
                try:
                    prompt_translator = create_llm_client(
                        provider="gemini",
                        model=app_config.llm.gemini_model,
                        timeout_sec=30.0,
                    )
                except Exception as exc:
                    logger.warning("Gemini prompt translator skipped: %s", exc)

            image_generator = ImageGenerator(
                client=image_client,
                fallback_clients=fallback_clients,
                stock_client=stock_client,
                thumbnail_style=app_config.images.thumbnail_style,
                content_style=app_config.images.content_style,
                thumbnail_size=app_config.images.thumbnail_size,
                content_size=app_config.images.content_size,
                max_content_images=app_config.images.max_content_images,
                prompt_translator=prompt_translator,
                parallel=True,  # 병렬 생성 활성화
                topic_mode=topic_mode,  # 토픽 모드 기준 이미지 전략 적용
                content_strategy_override="ai_only" if args.ai_only_images else None,
            )
        except Exception as exc:
            logger.warning("Image generator initialization skipped: %s", exc)

    quality_evaluator = None
    if use_llm:
        try:
            from modules.llm.provider_factory import create_client as create_llm_client
            from modules.automation.quality_evaluator import QualityEvaluator

            eval_client = create_llm_client(
                provider=app_config.llm.primary_provider,
                model=app_config.llm.primary_model,
                timeout_sec=app_config.llm.timeout_sec,
            )
            quality_evaluator = QualityEvaluator(llm_client=eval_client)
            logger.info("QualityEvaluator initialized for publish_once")
        except Exception as exc:
            logger.warning("QualityEvaluator initialization skipped: %s", exc)

    # ── 컴포넌트 초기화 ────────────────────────────────────
    store = JobStore(db_path=args.db)
    metrics_store = MetricsStore(db_path=args.db)
    publisher = PlaywrightPublisher(blog_id=blog_id)

    # 태그 생성기 초기화 (SEO 유입 전략용)
    tag_generator = None
    if app_config.seo.enable_tag_generation:
        try:
            tag_generator = get_tag_generator(app_config.seo)
            logger.info("Tag generator initialized")
        except Exception as exc:
            logger.warning("Tag generator initialization skipped: %s", exc)

    pipeline = PipelineService(
        job_store=store,
        publisher=publisher,
        generate_fn=generate_fn,
        metrics_store=metrics_store,
        retry_max_attempts=app_config.retry.max_retries,
        retry_backoff_base_sec=app_config.retry.backoff_base_sec,
        retry_backoff_max_sec=app_config.retry.backoff_max_sec,
        image_generator=image_generator,
        tag_generator=tag_generator,
        quality_evaluator=quality_evaluator,
    )

    # ── Job 등록 ───────────────────────────────────────────
    success = store.schedule_job(
        job_id=job_id,
        title=args.title,
        seed_keywords=keywords,
        platform="naver",
        persona_id=resolved_persona_id,
        scheduled_at=scheduled_at,
        max_retries=1,
        category=resolved_platform_category,
    )
    if not success:
        print("❌ Job 등록 실패 (중복 작업)")
        sys.exit(1)

    print(f"✅ Job 등록 완료: {job_id}")
    print("콘텐츠 생성 및 발행 시작...")
    print()

    # ── Job 선점 ───────────────────────────────────────────
    # 방금 등록한 job만 선점하기 위해 직접 조회
    job = store.get_job(job_id)
    if not job:
        print("❌ Job 조회 실패")
        sys.exit(1)

    # 직접 running 상태로 전환
    with store.connection() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', updated_at = ? WHERE job_id = ?",
            (scheduled_at, job_id),
        )

    # Job 객체 재조회 (status 반영)
    job = store.get_job(job_id)
    if not job:
        print("❌ Job 재조회 실패")
        sys.exit(1)

    print(f"✅ Job 선점 완료: {job_id}")

    # ── 파이프라인 실행 ────────────────────────────────────
    try:
        await pipeline.run_job(job)
    except Exception as e:
        logger.exception(f"Pipeline 실행 오류: {e}")
        print(f"❌ Pipeline 오류: {e}")
        sys.exit(1)
    finally:
        if image_generator:
            await image_generator.close()

    # ── 결과 확인 ──────────────────────────────────────────
    final_job = store.get_job(job_id)
    if final_job is None:
        print("❌ 결과 조회 실패")
        sys.exit(1)

    print()
    print("=" * 55)
    print(f"  최종 상태 : {final_job.status}")
    if final_job.status == "completed":
        print(f"  발행 URL  : {final_job.result_url}")
        if use_llm:
            provider_summary = _format_provider_summary(final_job.seo_snapshot)
            if provider_summary:
                print(f"  LLM Provider : {provider_summary}")
        print()
        print("🎉 실발행 성공!")
        if args.verify_ai_toggle:
            report_path = Path("data/ai_toggle/last_report.json")
            if not report_path.exists():
                print("❌ AI 토글 검증 실패: 리포트 파일 없음 (data/ai_toggle/last_report.json)")
                sys.exit(2)
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"❌ AI 토글 검증 실패: 리포트 파싱 오류 ({exc})")
                sys.exit(2)

            expected_on = int(report.get("expected_on", 0) or 0)
            post_verify_passed = int(report.get("post_verify_passed", 0) or 0)
            mode = str(report.get("mode", "unknown"))
            summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
            postverify = summary.get("postverify", {}) if isinstance(summary.get("postverify"), dict) else {}
            post_failed = int(postverify.get("failed", 0) or 0)
            skipped_reason = str(postverify.get("skipped", "") or "")

            print()
            print("[AI Toggle Verify]")
            print(f"  mode            : {mode}")
            print(f"  expected_on     : {expected_on}")
            print(f"  post_verify_ok  : {post_verify_passed}")
            if skipped_reason:
                print(f"  post_verify_skip: {skipped_reason}")

            if expected_on < max(0, int(args.verify_min_expected)):
                print(
                    f"❌ AI 토글 검증 실패: expected_on={expected_on} < min_expected={args.verify_min_expected}"
                )
                sys.exit(2)
            if post_failed > 0 and skipped_reason == "":
                print(f"❌ AI 토글 검증 실패: post_verify failed={post_failed}")
                sys.exit(2)
            if skipped_reason == "" and post_verify_passed < expected_on:
                print(
                    f"❌ AI 토글 검증 실패: post_verify_ok={post_verify_passed} < expected_on={expected_on}"
                )
                sys.exit(2)
            print("✅ AI 토글 검증 통과")
    else:
        print(f"  에러 코드 : {final_job.error_code}")
        print(f"  에러 메시지: {final_job.error_message}")
        print()
        # 스크린샷 확인 안내
        screenshots = list(Path("data/screenshots").glob("*.png"))
        if screenshots:
            latest = max(screenshots, key=lambda f: f.stat().st_mtime)
            print(f"  스크린샷  : {latest}")
        print("❌ 실발행 실패")
        sys.exit(1)
    print("=" * 55)


def main():
    app_config = load_config()
    setup_logging(level=app_config.logging.level, log_format=app_config.logging.format)
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
