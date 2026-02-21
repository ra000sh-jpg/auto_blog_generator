import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any, Dict

import pytest
import requests  # type: ignore[import-untyped]

from modules.collectors.naver_datalab import NaverDataLabCollector
from scripts import naver_login, publish_once


def build_args() -> argparse.Namespace:
    """publish_once.run() 테스트용 인자를 생성한다."""
    return argparse.Namespace(
        title="테스트 제목",
        keywords="테스트,자동화",
        db="data/automation.db",
        headful=False,
        persona="P1",
        category=None,
        use_llm=False,
        ai_only_images=False,
        ai_toggle_mode=None,
        verify_ai_toggle=False,
        verify_min_expected=1,
    )


def test_datalab_collector_init(tmp_path: Path):
    """수집기 초기화와 카테고리 구성을 검증한다."""
    collector = NaverDataLabCollector(cache_file=str(tmp_path / "cache.json"))

    assert collector.session is not None
    assert len(collector.CATEGORIES) == 11
    assert "디지털/가전" in collector.CATEGORIES
    assert "생활/건강" in collector.CATEGORIES


def test_datalab_invalid_category(tmp_path: Path):
    """없는 카테고리 요청 시 빈 리스트를 반환하는지 검증한다."""
    collector = NaverDataLabCollector(cache_file=str(tmp_path / "cache.json"))
    assert collector.fetch_trending_keywords("없는카테고리", count=5) == []


def test_datalab_fetch_trending_mock(tmp_path: Path):
    """정상 응답 파싱 결과를 검증한다."""
    collector = NaverDataLabCollector(cache_file=str(tmp_path / "cache.json"))

    class MockResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> Dict[str, Any]:
            return {"ranks": [{"keyword": "에어팟"}, {"keyword": "아이폰"}]}

    collector.session.post = lambda *args, **kwargs: MockResponse()  # type: ignore[method-assign]

    keywords = collector.fetch_trending_keywords("디지털/가전", count=5)
    assert keywords == ["에어팟", "아이폰"]


def test_datalab_api_error_handling(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """요청 예외 시 빈 리스트 반환과 로그 출력을 검증한다."""
    collector = NaverDataLabCollector(
        cache_file=str(tmp_path / "cache.json"),
        max_retries=1,
    )

    def raise_request_error(*_args, **_kwargs):
        raise requests.RequestException("network down")

    collector.session.post = raise_request_error  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        keywords = collector.fetch_trending_keywords("디지털/가전", count=3)

    assert keywords == []
    assert "DataLab request failed" in caplog.text


def test_publish_once_missing_blog_id(monkeypatch: pytest.MonkeyPatch):
    """NAVER_BLOG_ID가 없으면 종료하는지 검증한다."""
    monkeypatch.delenv("NAVER_BLOG_ID", raising=False)

    with pytest.raises(SystemExit) as exc:
        asyncio.run(publish_once.run(build_args()))

    assert exc.value.code == 1


def test_publish_once_missing_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """세션 파일이 없으면 종료하는지 검증한다."""
    monkeypatch.setenv("NAVER_BLOG_ID", "test_blog")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        asyncio.run(publish_once.run(build_args()))

    assert exc.value.code == 1


def test_naver_login_stealth_fallback(monkeypatch: pytest.MonkeyPatch):
    """playwright_stealth import 실패 시 noop 함수로 폴백되는지 검증한다."""
    original_import_module = naver_login.importlib.import_module

    def mock_import(name: str):
        if name == "playwright_stealth":
            raise ImportError("not installed")
        return original_import_module(name)

    monkeypatch.setattr(naver_login.importlib, "import_module", mock_import)
    stealth_fn = naver_login.resolve_stealth_function()

    assert asyncio.iscoroutinefunction(stealth_fn)
    asyncio.run(stealth_fn(object()))
