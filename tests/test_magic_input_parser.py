from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from modules.llm.magic_input_parser import MagicInputParser


def _fixed_now_kst() -> datetime:
    """KST 기준 고정 시각을 반환한다."""
    return datetime(2026, 2, 20, 10, 30, tzinfo=timezone(timedelta(hours=9)))


def _build_heuristic_parser() -> MagicInputParser:
    """LLM 없이 규칙 기반 파서만 동작하도록 구성한다."""
    parser = MagicInputParser(now_provider=_fixed_now_kst)
    parser._clients = []
    return parser


def test_magic_input_parser_extracts_relative_schedule_time():
    """내일 아침 9시 표현을 UTC ISO로 변환해야 한다."""
    parser = _build_heuristic_parser()
    result = asyncio.run(parser.parse("내일 아침 9시에 스벅 리뷰 올려줘"))
    assert result.schedule_time == "2026-02-21T00:00:00Z"


def test_magic_input_parser_extracts_same_day_time():
    """당일 미래 시각(오후 3시)은 같은 날 예약으로 계산해야 한다."""
    parser = _build_heuristic_parser()
    result = asyncio.run(parser.parse("오후 3시에 IT 도구 비교 글 발행해줘"))
    assert result.schedule_time == "2026-02-20T06:00:00Z"


def test_magic_input_parser_extracts_explicit_date_time():
    """명시 날짜/시간 형식을 UTC ISO로 정규화해야 한다."""
    parser = _build_heuristic_parser()
    result = asyncio.run(parser.parse("2026-03-01 18시 카페 운영 노하우 글 예약"))
    assert result.schedule_time == "2026-03-01T09:00:00Z"
