from modules.automation.claim_ledger import build_claim_ledger


def _source_pack():
    return {
        "sources": [
            {
                "source": "FRED",
                "source_type": "official",
                "title": "US10Y market data",
                "metric_key": "US10Y",
                "value": 4.2,
            },
            {
                "source": "CoinGecko",
                "source_type": "market_data",
                "title": "BTC market data",
                "metric_key": "BTC",
                "value": 104000.0,
            },
        ],
        "confirmed_metrics": [
            {"key": "US10Y", "label": "DGS10", "value": 4.2, "source": "FRED"},
            {"key": "BTC", "label": "bitcoin", "value": 104000.0, "source": "CoinGecko"},
        ],
    }


def test_claim_ledger_passes_supported_numbers():
    ledger = build_claim_ledger(
        content="FRED 기준 US10Y는 4.2이고 CoinGecko 기준 BTC는 104,000 부근입니다.",
        source_pack=_source_pack(),
    )

    assert ledger.ok is True
    assert ledger.checked_claim_count == 1
    assert ledger.supported_claim_count == 1


def test_claim_ledger_blocks_unsupported_number():
    ledger = build_claim_ledger(
        content="오늘 KOSPI는 3000을 반드시 돌파할 가능성이 큽니다.",
        source_pack=_source_pack(),
    )

    assert ledger.ok is False
    assert ledger.unsupported_claim_count == 1
    assert "3000" in ledger.unsupported_claims[0].text


def test_claim_ledger_ignores_source_section_numbers():
    ledger = build_claim_ledger(
        content=(
            "FRED 기준 US10Y는 4.2입니다.\n\n"
            "■ 참고한 공식/시장 데이터\n"
            "• FRED - US10Y data / 기준일: 2026-06-23"
        ),
        source_pack=_source_pack(),
    )

    assert ledger.ok is True
    assert ledger.checked_claim_count == 1
