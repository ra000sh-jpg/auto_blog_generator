"""정부 매크로 자료 수집/분석 파이프라인."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from modules.automation.job_store import JobStore
from modules.automation.notifier import TelegramNotifier

from .collector import MacroDataCollector
from .insight_generator import MacroInsightGenerator
from .metric_extractor import MacroMetricExtractor
from .quality_evaluator import MacroQualityEvaluator
from .reference_verifier import MacroReferenceVerifier
from .review_message import build_macro_review_message
from .telegram_approval import build_macro_candidate_keyboard
from .topic_generator import MacroBlogTopicGenerator

logger = logging.getLogger(__name__)


class MacroPipeline:
    """정부기관 자료를 수집해 블로그 후보와 검토 메시지를 생성한다."""

    def __init__(
        self,
        *,
        job_store: JobStore,
        collector: MacroDataCollector | None = None,
        metric_extractor: MacroMetricExtractor | None = None,
        insight_generator: MacroInsightGenerator | None = None,
        topic_generator: MacroBlogTopicGenerator | None = None,
        quality_evaluator: MacroQualityEvaluator | None = None,
        reference_verifier: MacroReferenceVerifier | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.job_store = job_store
        self.collector = collector or MacroDataCollector()
        self.metric_extractor = metric_extractor or MacroMetricExtractor()
        self.insight_generator = insight_generator or MacroInsightGenerator()
        self.topic_generator = topic_generator or MacroBlogTopicGenerator()
        self.quality_evaluator = quality_evaluator or MacroQualityEvaluator()
        self.reference_verifier = reference_verifier or MacroReferenceVerifier()
        self.notifier = notifier

    def run_once(
        self,
        *,
        source: str = "MOTIE",
        limit: int = 5,
        send_telegram: bool = False,
    ) -> Dict[str, Any]:
        """수집부터 후보 저장까지 1회 실행한다."""
        discovered = self.collector.check_latest_sources(source=source, limit=limit)
        stored_documents: List[Dict[str, Any]] = []
        analyzed_documents: List[Dict[str, Any]] = []
        review_messages: List[str] = []

        for candidate in discovered:
            document = self.job_store.upsert_macro_document(
                {
                    "source": candidate.source,
                    "title": candidate.title,
                    "published_at": candidate.published_at,
                    "url": candidate.url,
                    "file_url": candidate.file_url,
                    "file_type": candidate.file_type,
                    "attachments_json": list(candidate.attachments),
                    "status": candidate.status,
                    "hash": candidate.hash,
                }
            )
            stored_documents.append(document)
            analyzed = self.analyze_document(document)
            analyzed_documents.append(analyzed)
            message = build_macro_review_message(
                document=analyzed["document"],
                metrics_json=analyzed["metrics_json"],
                insight_json=analyzed["insight_json"],
                candidates=analyzed["candidates"],
                quality_json=analyzed["quality_json"],
            )
            review_messages.append(message)
            if send_telegram and self.notifier and self.notifier.enabled:
                self._send_telegram_review(
                    message,
                    candidates=analyzed["candidates"],
                )

        return {
            "source": source.upper(),
            "discovered": len(discovered),
            "stored": len(stored_documents),
            "analyzed": len(analyzed_documents),
            "review_messages": review_messages,
            "documents": analyzed_documents,
        }

    def analyze_document(self, document: Dict[str, Any]) -> Dict[str, Any]:
        """문서 1건을 파싱/수치 추출/후보 생성까지 처리한다."""
        download_result = self.collector.download_document_text(document)
        status = str(download_result.get("status", "failed") or "failed")
        raw_text = str(download_result.get("text", "") or "")
        parsed_json = dict(download_result.get("parsed_json", {}) or {})
        error_message = str(download_result.get("error_message", "") or "")

        if status in {"failed", "unsupported"}:
            self.job_store.update_macro_document_analysis(
                str(document["id"]),
                status=status,
                raw_text=raw_text,
                parsed_json=parsed_json,
                error_message=error_message,
            )
            updated = self.job_store.get_macro_document(str(document["id"])) or document
            return {
                "document": updated,
                "metrics_json": {},
                "insight_json": {},
                "candidates": [],
                "quality_json": {},
            }

        metrics_json = self.metric_extractor.extract(raw_text)
        metrics_json["verification"] = self.reference_verifier.verify(
            document=document,
            metrics_json=metrics_json,
        )
        insight_json = self.insight_generator.generate(
            title=str(document.get("title", "") or ""),
            metrics_json=metrics_json,
        )
        candidates = self.topic_generator.generate(
            document_title=str(document.get("title", "") or ""),
            metrics_json=metrics_json,
            insight_json=insight_json,
        )
        quality_json = self.quality_evaluator.evaluate(
            metrics_json=metrics_json,
            insight_json=insight_json,
            candidates=candidates,
        )
        for item in candidates:
            item["quality_json"] = quality_json

        self.job_store.update_macro_document_analysis(
            str(document["id"]),
            status="analyzed",
            raw_text=raw_text,
            parsed_json=parsed_json,
            metrics_json=metrics_json,
            insight_json=insight_json,
            error_message="",
        )
        stored_candidates = self.job_store.replace_macro_blog_candidates(
            str(document["id"]),
            candidates,
        )
        updated = self.job_store.get_macro_document(str(document["id"])) or document
        return {
            "document": updated,
            "metrics_json": metrics_json,
            "insight_json": insight_json,
            "candidates": stored_candidates,
            "quality_json": quality_json,
        }

    def _send_telegram_review(self, message: str, *, candidates: List[Dict[str, Any]] | None = None) -> None:
        """동기/비동기 실행 환경에 맞춰 텔레그램 검토 메시지를 전송한다."""
        if not self.notifier or not self.notifier.enabled:
            return
        reply_markup = build_macro_candidate_keyboard(candidates or [])
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(
                    self.notifier.send_message(
                        message,
                        disable_notification=False,
                        reply_markup=reply_markup,
                    )
                )
            except Exception:
                logger.debug("Macro review Telegram send failed", exc_info=True)
            return
        loop.create_task(
            self.notifier.send_message(
                message,
                disable_notification=False,
                reply_markup=reply_markup,
            ),
            name="macro-review-telegram-send",
        )
