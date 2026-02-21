from __future__ import annotations

import asyncio

from modules.llm.idea_vault_parser import IdeaVaultBatchParser


def _build_parser_without_llm() -> IdeaVaultBatchParser:
    parser = IdeaVaultBatchParser()
    parser._clients = []
    return parser


def test_idea_vault_parser_filters_noise_and_accepts_meaningful_lines():
    parser = _build_parser_without_llm()
    result = asyncio.run(
        parser.parse_bulk(
            "카페 오픈 체크리스트 만들기\n!!!\nAI 자동화로 글감 관리하는 방법\n씨발",
            categories=["IT 자동화", "다양한 생각"],
            batch_size=20,
        )
    )
    assert result.total_lines == 4
    assert len(result.accepted_items) == 2
    assert len(result.rejected_lines) == 2


def test_idea_vault_parser_maps_category_from_text():
    parser = _build_parser_without_llm()
    result = asyncio.run(
        parser.parse_bulk(
            "이번 주 IT 자동화 툴 비교 리뷰",
            categories=["경제 브리핑", "IT 자동화", "다양한 생각"],
            batch_size=20,
        )
    )
    assert len(result.accepted_items) == 1
    assert result.accepted_items[0].mapped_category == "IT 자동화"
