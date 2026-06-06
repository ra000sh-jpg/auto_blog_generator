from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Mapping
import zipfile

from modules.automation.job_store import JobConfig, JobStore
from modules.macro.collector import MacroDataCollector
from modules.macro.job_promoter import MacroCandidatePromoter
from modules.macro.metric_extractor import MacroMetricExtractor
from modules.macro.pipeline import MacroPipeline
from modules.macro.reference_verifier import MacroReferenceVerifier
from modules.macro.review_message import build_macro_review_message
from modules.macro.telegram_approval import (
    apply_macro_candidate_callback,
    build_macro_candidate_keyboard,
    parse_macro_callback_data,
)


class FakeMacroFetcher:
    def __init__(self) -> None:
        self.list_url = "https://www.motir.go.kr/kor/article/ATCL3f49a5a8c?pageIndex=1"
        self.search_url = (
            "https://www.motir.go.kr/kor/article/ATCL3f49a5a8c?pageIndex=1&searchKeyword=%EC%88%98%EC%B6%9C%EC%9E%85"
        )
        self.detail_url = "https://www.motir.go.kr/kor/article/detail-export"

    def get_text(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_sec: float = 10.0,
    ) -> str:
        del headers, timeout_sec
        if url in {self.list_url, self.search_url}:
            return f"""
            <html><body>
              <a href="/kor/article/detail-export">2026년 5월 수출입 동향</a>
              <a href="/kor/article/other">전기용품 안전관리 안내</a>
            </body></html>
            """
        if url == self.detail_url:
            return """
            <html><body>
              <h1>2026년 5월 수출입 동향</h1>
              <p>등록일 2026.06.01</p>
              <p>5월 수출은 전년동월대비 +8.4% 증가했다.</p>
              <p>수입은 전년동월대비 +2.1% 증가했다.</p>
              <p>무역수지는 흑자 45억 달러를 기록했다.</p>
              <p>반도체 수출은 +22% 증가했고 자동차 수출은 +11% 증가했다.</p>
              <p>중국 수출은 -3% 감소했지만 미국 수출은 +14% 증가했다.</p>
            </body></html>
            """
        return ""

    def get_bytes(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_sec: float = 15.0,
    ) -> bytes:
        del url, headers, timeout_sec
        return b""


class FakeMotieTableFetcher:
    def __init__(self) -> None:
        self.search_url = (
            "https://www.motir.go.kr/kor/article/ATCL3f49a5a8c?pageIndex=1&searchKeyword=%EC%88%98%EC%B6%9C%EC%9E%85"
        )
        self.detail_url = "https://www.motir.go.kr/kor/article/ATCL3f49a5a8c/171880/view"
        self.file_url = "https://www.motir.go.kr/attach/down/a/b/c"

    def get_text(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_sec: float = 10.0,
    ) -> str:
        del headers, timeout_sec
        if url == self.search_url:
            return """
            <html><body>
              <table>
                <tr>
                  <td><a href="javascript:article.view('171880');"><i>2026년 5월 수출입 동향</i></a></td>
                  <td>2026-06-01</td>
                  <td><a href="/attach/down/a/b"><img alt="PDF 파일"/></a></td>
                </tr>
              </table>
            </body></html>
            """
        if url == self.detail_url:
            return """
            <html><body>
              <h1>2026년 5월 수출입 동향</h1>
              <p>등록일 2026.06.01</p>
              <a href="javascript:location.href='/attach/down/a/b/c'">2026년 5월 수출입동향_3보.pdf [1,670 KB]</a>
              <p>5월 수출은 전년동월대비 +8.4% 증가했다.</p>
              <p>무역수지는 흑자 45억 달러를 기록했다.</p>
              <p>반도체 수출은 +22% 증가했고 미국 수출은 +14% 증가했다.</p>
            </body></html>
            """
        return ""

    def get_bytes(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_sec: float = 15.0,
    ) -> bytes:
        del url, headers, timeout_sec
        return b"not a valid pdf"


class FakeMotieHwpxFallbackFetcher:
    def __init__(self) -> None:
        self.search_url = (
            "https://www.motir.go.kr/kor/article/ATCL3f49a5a8c?pageIndex=1&searchKeyword=%EC%88%98%EC%B6%9C%EC%9E%85"
        )
        self.detail_url = "https://www.motir.go.kr/kor/article/ATCL3f49a5a8c/171881/view"
        self.pdf_url = "https://www.motir.go.kr/attach/down/pdf/a/b"
        self.hwpx_url = "https://www.motir.go.kr/attach/down/hwpx/a/b"

    def get_text(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_sec: float = 10.0,
    ) -> str:
        del headers, timeout_sec
        if url == self.search_url:
            return """
            <html><body>
              <table>
                <tr>
                  <td><a href="javascript:article.view('171881');"><i>2026년 5월 수출입 동향</i></a></td>
                  <td>2026-06-01</td>
                </tr>
              </table>
            </body></html>
            """
        if url == self.detail_url:
            return """
            <html><body>
              <h1>2026년 5월 수출입 동향</h1>
              <p>등록일 2026.06.01</p>
              <a href="javascript:location.href='/attach/down/pdf/a/b'">2026년 5월 수출입동향.pdf [1,670 KB]</a>
              <a href="javascript:location.href='/attach/down/hwpx/a/b'">2026년 5월 수출입동향.hwpx [800 KB]</a>
            </body></html>
            """
        return ""

    def get_bytes(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_sec: float = 15.0,
    ) -> bytes:
        del headers, timeout_sec
        if url == self.pdf_url:
            raise RuntimeError("blocked pdf")
        if url == self.hwpx_url:
            return build_hwpx_bytes(
                """
                <root>
                  <p><t>5월 수출은 전년동월대비 +8.4% 증가했다.</t></p>
                  <p><t>수입은 전년동월대비 +2.1% 증가했다.</t></p>
                  <p><t>무역수지는 흑자 45억 달러를 기록했다.</t></p>
                  <p><t>반도체 수출은 +22% 증가했고 미국 수출은 +14% 증가했다.</t></p>
                </root>
                """
            )
        return b""


class FakeBlockedBinaryFetcher:
    def get_text(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_sec: float = 10.0,
    ) -> str:
        del url, headers, timeout_sec
        return "<html><body><p>상세 HTML</p></body></html>"

    def get_bytes(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_sec: float = 15.0,
    ) -> bytes:
        del url, headers, timeout_sec
        raise RuntimeError("blocked")


class FakeBrowserDownloader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.calls = 0

    def download(self, *, detail_url: str, file_url: str, timeout_sec: float = 20.0) -> bytes:
        assert detail_url
        assert file_url
        assert timeout_sec >= 10
        self.calls += 1
        return self.data


def build_hwpx_bytes(section_xml: str) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Contents/section0.xml", section_xml)
    return buffer.getvalue()


def build_store(tmp_path: Path, name: str = "macro_pipeline.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


class FakeTelegramNotifier:
    """텔레그램 전송 내용을 테스트 안에서만 기록한다."""

    enabled = True

    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    async def send_message(self, text: str, *, disable_notification: bool = False, reply_markup=None) -> bool:
        self.sent_messages.append(
            {
                "text": text,
                "disable_notification": disable_notification,
                "reply_markup": reply_markup,
            }
        )
        return True


def test_macro_pipeline_collects_analyzes_and_stores_candidates(tmp_path: Path):
    """산업부 수출입 자료를 수집해 수치/인사이트/후보를 저장해야 한다."""
    store = build_store(tmp_path)
    collector = MacroDataCollector(fetcher=FakeMacroFetcher())
    pipeline = MacroPipeline(job_store=store, collector=collector)

    result = pipeline.run_once(source="MOTIE", limit=5, send_telegram=False)

    assert result["discovered"] == 1
    assert result["stored"] == 1
    assert result["analyzed"] == 1

    documents = store.list_macro_documents(source="MOTIE", limit=10)
    assert len(documents) == 1
    assert documents[0]["status"] == "analyzed"
    assert documents[0]["metrics_json"]["by_key"]["export_growth_yoy"] == "+8.4%"
    assert documents[0]["metrics_json"]["by_key"]["trade_balance"] == "흑자 45억 달러"

    candidates = store.list_macro_blog_candidates(document_id=documents[0]["id"], limit=10)
    assert len(candidates) >= 3
    assert any("반도체" in item["title"] for item in candidates)

    message = result["review_messages"][0]
    assert "요약" in message
    assert "핵심 수치" in message
    assert "글 후보" in message
    assert "수출 증감률: +8.4%" in message


def test_macro_pipeline_resolves_motie_table_detail_and_js_attachment(tmp_path: Path):
    """산업부 표 목록의 article.view 링크와 JS 첨부 링크를 상세 URL로 정규화해야 한다."""
    store = build_store(tmp_path, name="macro_pipeline_table.db")
    fetcher = FakeMotieTableFetcher()
    collector = MacroDataCollector(fetcher=fetcher)
    pipeline = MacroPipeline(job_store=store, collector=collector)

    result = pipeline.run_once(source="MOTIE", limit=1, send_telegram=False)

    assert result["discovered"] == 1
    documents = store.list_macro_documents(source="MOTIE", limit=10)
    assert documents[0]["url"] == fetcher.detail_url
    assert documents[0]["file_url"] == fetcher.file_url
    assert documents[0]["attachments_json"][0]["file_type"] == "pdf"
    assert documents[0]["status"] == "analyzed"
    assert documents[0]["metrics_json"]["by_key"]["export_growth_yoy"] == "+8.4%"
    assert documents[0]["metrics_json"]["verification"]["period"] == "202605"


def test_macro_pipeline_falls_back_from_blocked_pdf_to_hwpx(tmp_path: Path):
    """PDF가 막히면 HWPX 첨부에서 텍스트와 수치를 추출해야 한다."""
    store = build_store(tmp_path, name="macro_pipeline_hwpx.db")
    fetcher = FakeMotieHwpxFallbackFetcher()
    collector = MacroDataCollector(fetcher=fetcher)
    pipeline = MacroPipeline(job_store=store, collector=collector)

    result = pipeline.run_once(source="MOTIE", limit=1, send_telegram=False)

    assert result["discovered"] == 1
    documents = store.list_macro_documents(source="MOTIE", limit=10)
    document = documents[0]
    assert len(document["attachments_json"]) == 2
    assert document["status"] == "analyzed"
    assert document["metrics_json"]["by_key"]["export_growth_yoy"] == "+8.4%"
    assert document["parsed_json"]["source_file_type"] == "hwpx"
    assert document["parsed_json"]["attempts"][0]["status"] == "failed"
    assert document["parsed_json"]["attempts"][1]["parser"] == "hwpx_zip_xml"


def test_macro_collector_can_use_browser_downloader_after_http_failure():
    """HTTP 첨부 다운로드가 실패하면 선택적 브라우저 다운로더를 사용할 수 있어야 한다."""
    hwpx_data = build_hwpx_bytes("<root><p><t>5월 수출은 전년동월대비 +8.4% 증가했다.</t></p></root>")
    browser_downloader = FakeBrowserDownloader(hwpx_data)
    collector = MacroDataCollector(
        fetcher=FakeBlockedBinaryFetcher(),
        browser_downloader=browser_downloader,
    )

    result = collector.download_document_text(
        {
            "url": "https://www.motir.go.kr/kor/article/detail",
            "file_url": "https://www.motir.go.kr/attach/down/hwpx/a/b",
            "file_type": "hwpx",
            "attachments_json": [
                {
                    "url": "https://www.motir.go.kr/attach/down/hwpx/a/b",
                    "file_type": "hwpx",
                }
            ],
        }
    )

    assert result["status"] == "parsed"
    assert browser_downloader.calls == 1
    assert result["parsed_json"]["parser"] == "hwpx_zip_xml"
    assert "8.4%" in result["text"]


def test_macro_metric_extractor_uses_percent_near_keyword():
    """한 문장에 여러 수치가 있으면 키워드 가까운 수치를 선택해야 한다."""
    extractor = MacroMetricExtractor()

    result = extractor.extract(
        """
        수출 월 기준 역대 최대, 사상 첫 3개월 연속 800억 달러 상회.
        5월 수출 877.5억 달러(+53.2%), 수입 608.0억 달러(+20.8%), 수지 269.5억 달러 흑자.
        유가103.2(+61.9%), 석유제품1,216(+92.4%), 석유화학1,649(+49.2%).
        """
    )

    assert result["by_key"]["export_growth_yoy"] == "+53.2%"
    assert result["by_key"]["import_growth_yoy"] == "+20.8%"
    assert result["by_key"]["export_amount_usd_eok"] == "877.5억 달러"
    assert result["by_key"]["import_amount_usd_eok"] == "608.0억 달러"
    assert result["by_key"]["industry_petrochemical_growth"] == "+49.2%"


def test_macro_reference_verifier_compares_customs_trade_xml():
    """관세청 수출입총괄 XML을 산업부 추출 금액과 대조해야 한다."""
    verifier = MacroReferenceVerifier(env={"CUSTOMS_TRADE_API_KEY": "sample"}, allow_network=False)
    metrics_json = {
        "metric_count": 4,
        "by_key": {
            "export_amount_usd_eok": "877.5억 달러",
            "import_amount_usd_eok": "608.0억 달러",
            "trade_balance": "흑자 269.5억 달러",
        },
    }
    parsed = verifier._parse_customs_trade_xml(
        """
        <response>
          <body>
            <items>
              <item>
                <year>2026.05</year>
                <expDlr>87750000000</expDlr>
                <impDlr>60800000000</impDlr>
                <balPayments>26950000000</balPayments>
              </item>
            </items>
          </body>
        </response>
        """
    )

    comparisons = verifier._compare_customs_metrics(parsed, metrics_json)

    assert parsed["exportAmountUsd"] == 87750000000
    assert len(comparisons) == 3
    assert all(item["matched"] for item in comparisons)


def test_macro_reference_verifier_marks_customs_configured_without_network():
    """관세청 API 키가 있으면 선택 검증 가능 상태를 남겨야 한다."""
    verifier = MacroReferenceVerifier(
        env={"CUSTOMS_TRADE_API_KEY": "sample"},
        allow_network=False,
    )

    result = verifier.verify(
        document={"title": "2026년 5월 수출입 동향", "published_at": "2026-06-01"},
        metrics_json={
            "metric_count": 3,
            "by_key": {
                "export_amount_usd_eok": "877.5억 달러",
                "import_amount_usd_eok": "608.0억 달러",
                "trade_balance": "흑자 269.5억 달러",
            },
        },
    )

    assert result["sources"][0]["source"] == "CUSTOMS_TRADE"
    assert result["sources"][0]["status"] == "configured"
    assert result["sourcePolicy"] == "light"
    assert result["readyForAutoDraft"] is True
    assert result["verificationScore"] >= 86
    assert result["recommendedNextAction"] == "ready_for_draft_optional_verification"


def test_macro_reference_verifier_light_policy_allows_keyless_draft():
    """가벼운 블로그 모드에서는 API 키 없이도 원문 수치 기반 초안을 허용해야 한다."""
    verifier = MacroReferenceVerifier(env={}, allow_network=False)

    result = verifier.verify(
        document={"title": "2026년 5월 수출입 동향", "published_at": "2026-06-01"},
        metrics_json={
            "metric_count": 3,
            "by_key": {
                "export_growth_yoy": "+8.4%",
                "industry_semiconductor_growth": "+22%",
                "country_us_growth": "+14%",
            },
        },
    )

    assert result["sourcePolicy"] == "light"
    assert result["requiresTwoSourceConfirmation"] is False
    assert result["readyForAutoDraft"] is True
    assert result["recommendedNextAction"] == "ready_for_draft_light_source"
    assert result["verificationScore"] >= 82


def test_macro_reference_verifier_strict_policy_requires_second_source():
    """엄격 모드에서는 API 키가 없으면 2차 검증 필요 상태로 남겨야 한다."""
    verifier = MacroReferenceVerifier(
        env={"MACRO_SOURCE_POLICY": "strict"},
        allow_network=False,
    )

    result = verifier.verify(
        document={"title": "2026년 5월 수출입 동향", "published_at": "2026-06-01"},
        metrics_json={
            "metric_count": 3,
            "by_key": {"export_growth_yoy": "+8.4%"},
        },
    )

    assert result["sourcePolicy"] == "strict"
    assert result["requiresTwoSourceConfirmation"] is True
    assert result["readyForAutoDraft"] is False
    assert result["recommendedNextAction"] == "source_cross_check_required"


def test_macro_candidate_promoter_schedules_blog_job(tmp_path: Path):
    """매크로 후보를 기존 블로그 작성 큐로 승격할 수 있어야 한다."""
    store = build_store(tmp_path, name="macro_promoter.db")
    collector = MacroDataCollector(fetcher=FakeMacroFetcher())
    pipeline = MacroPipeline(job_store=store, collector=collector)
    pipeline.run_once(source="MOTIE", limit=1, send_telegram=False)
    document = store.list_macro_documents(source="MOTIE", limit=1)[0]
    candidate = store.list_macro_blog_candidates(document_id=document["id"], limit=1)[0]

    promoter = MacroCandidatePromoter(job_store=store)
    result = promoter.promote_candidate(candidate["id"], scheduled_at="2026-06-05T10:00:00Z")

    job = store.get_job(result["job_id"])
    updated_candidate = store.get_macro_blog_candidate(candidate["id"])
    assert job is not None
    assert job.title == candidate["title"]
    assert job.persona_id == "P4"
    assert "macro_intelligence" in job.tags
    assert "반도체" in job.seed_keywords
    assert updated_candidate["status"] == "approved"


def test_macro_telegram_candidate_callback_promotes_once(tmp_path: Path):
    """텔레그램 후보 버튼은 후보 1건을 초안 생성 큐에 한 번만 올려야 한다."""
    store = build_store(tmp_path, name="macro_telegram_callback.db")
    collector = MacroDataCollector(fetcher=FakeMacroFetcher())
    pipeline = MacroPipeline(job_store=store, collector=collector)
    pipeline.run_once(source="MOTIE", limit=1, send_telegram=False)
    document = store.list_macro_documents(source="MOTIE", limit=1)[0]
    candidate = store.list_macro_blog_candidates(document_id=document["id"], limit=1)[0]

    keyboard = build_macro_candidate_keyboard([candidate])
    callback_data = keyboard["inline_keyboard"][0][0]["callback_data"]
    parsed = parse_macro_callback_data(callback_data)

    assert parsed == {"action": "promote", "candidate_id": candidate["id"]}

    result = apply_macro_candidate_callback(
        store,
        candidate_id=parsed["candidate_id"],
        action=parsed["action"],
    )
    duplicate = apply_macro_candidate_callback(
        store,
        candidate_id=parsed["candidate_id"],
        action=parsed["action"],
    )

    job = store.get_job(result["job_id"])
    updated_candidate = store.get_macro_blog_candidate(candidate["id"])
    assert result["ok"] is True
    assert result["reason"] == "macro_candidate_promoted"
    assert job is not None
    assert job.title == candidate["title"]
    assert updated_candidate["status"] == "approved"
    assert duplicate["ok"] is False
    assert duplicate["reason"] == "already_handled"


def test_macro_pipeline_sends_telegram_review_with_candidate_buttons(tmp_path: Path):
    """매크로 검토 메시지는 후보 선택 버튼을 함께 전송해야 한다."""
    store = build_store(tmp_path, name="macro_telegram_review.db")
    collector = MacroDataCollector(fetcher=FakeMacroFetcher())
    notifier = FakeTelegramNotifier()
    pipeline = MacroPipeline(job_store=store, collector=collector, notifier=notifier)

    pipeline.run_once(source="MOTIE", limit=1, send_telegram=True)

    assert notifier.sent_messages
    sent = notifier.sent_messages[0]
    assert "글 후보" in sent["text"]
    assert sent["reply_markup"]["inline_keyboard"]
    callback_data = sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert parse_macro_callback_data(callback_data)["action"] == "promote"


def test_macro_review_message_includes_summary_metrics_and_titles():
    """텔레그램 검토 메시지는 요약+핵심 수치+후보 제목을 함께 담아야 한다."""
    message = build_macro_review_message(
        document={
            "title": "2026년 5월 수출입 동향",
            "source": "MOTIE",
            "published_at": "2026-06-01",
            "url": "https://example.test/source",
        },
        metrics_json={
            "metrics": [
                {
                    "label": "반도체 증감률",
                    "value": "+22%",
                    "evidence": "반도체 수출은 +22% 증가했다.",
                }
            ]
        },
        insight_json={"summary": "반도체가 수출 회복을 이끌었지만 중국 수요는 아직 약하다."},
        candidates=[
            {
                "title": "반도체 수출 회복이 AI 투자 사이클과 연결되는 이유",
                "angle": "반도체/AI",
                "target_reader": "투자 초심자",
            }
        ],
        quality_json={"overallScore": 92, "recommendedAction": "approval_request"},
    )

    assert "요약" in message
    assert "핵심 수치" in message
    assert "글 후보" in message
    assert "반도체 증감률: +22%" in message
    assert "종합: 92/100" in message
