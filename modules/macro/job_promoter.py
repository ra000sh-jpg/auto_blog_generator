"""매크로 글 후보를 블로그 작성 잡으로 승격하는 서비스."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import uuid
from typing import Any, Dict, List

from modules.automation.job_store import JobStore


class MacroCandidatePromoter:
    """검토된 매크로 후보를 기존 블로그 생성 큐에 연결한다."""

    def __init__(self, *, job_store: JobStore) -> None:
        self.job_store = job_store

    def promote_candidate(
        self,
        candidate_id: str,
        *,
        scheduled_at: str = "",
        status: str = "queued",
    ) -> Dict[str, Any]:
        """후보 1건을 블로그 잡으로 등록하고 후보 상태를 approved로 바꾼다."""
        candidate = self.job_store.get_macro_blog_candidate(candidate_id)
        if not candidate:
            raise ValueError(f"Macro candidate not found: {candidate_id}")
        document = self.job_store.get_macro_document(str(candidate.get("macro_document_id", "")))
        if not document:
            raise ValueError(f"Macro document not found for candidate: {candidate_id}")

        schedule_time = str(scheduled_at or "").strip() or (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        title = str(candidate.get("title", "") or "").strip()
        if not title:
            raise ValueError("Candidate title is empty")

        job_id = f"macro-job-{uuid.uuid4().hex[:12]}"
        seed_keywords = self._build_seed_keywords(candidate=candidate, document=document)
        success = self.job_store.schedule_job(
            job_id=job_id,
            title=title,
            seed_keywords=seed_keywords,
            platform="naver",
            persona_id="P4",
            scheduled_at=schedule_time,
            max_retries=3,
            tags=[
                "macro_intelligence",
                f"macro_document:{document.get('id', '')}",
                f"macro_candidate:{candidate.get('id', '')}",
            ],
            category="경제 브리핑",
            status=status,
        )
        if not success:
            raise RuntimeError("Failed to schedule macro blog job")

        self.job_store.update_macro_blog_candidate_status(
            str(candidate.get("id", "")),
            status="approved",
        )
        return {
            "job_id": job_id,
            "candidate_id": candidate.get("id", ""),
            "document_id": document.get("id", ""),
            "title": title,
            "scheduled_at": schedule_time,
            "seed_keywords": seed_keywords,
            "status": status,
        }

    def promote_top_candidates(
        self,
        *,
        document_id: str,
        limit: int = 1,
        min_overall_score: float = 85.0,
    ) -> List[Dict[str, Any]]:
        """문서의 상위 후보를 일정 개수만 큐에 등록한다."""
        candidates = self.job_store.list_macro_blog_candidates(
            document_id=document_id,
            status="needs_review",
            limit=max(1, min(20, int(limit or 1))),
        )
        promoted = []
        for candidate in candidates:
            quality = candidate.get("quality_json", {})
            score = float(quality.get("overallScore", 0.0) or 0.0) if isinstance(quality, dict) else 0.0
            if score < min_overall_score:
                continue
            promoted.append(self.promote_candidate(str(candidate.get("id", ""))))
            if len(promoted) >= max(1, int(limit or 1)):
                break
        return promoted

    def _build_seed_keywords(self, *, candidate: Dict[str, Any], document: Dict[str, Any]) -> List[str]:
        title = str(candidate.get("title", "") or "")
        angle = str(candidate.get("angle", "") or "")
        target_reader = str(candidate.get("target_reader", "") or "")
        metrics = document.get("metrics_json", {}) if isinstance(document.get("metrics_json", {}), dict) else {}
        by_key = metrics.get("by_key", {}) if isinstance(metrics.get("by_key", {}), dict) else {}
        keywords = [
            "미국 경제",
            "AI 산업",
            "반도체",
            "한국 수출",
            "투자 공부",
        ]
        for value in (title, angle, target_reader, str(document.get("title", "") or "")):
            keywords.extend(self._tokenize_korean_keywords(value))
        if "country_us_growth" in by_key:
            keywords.append("대미 수출")
        if "country_china_growth" in by_key:
            keywords.append("대중국 수출")
        if "trade_balance" in by_key:
            keywords.append("무역수지")
        return self._dedupe(keywords)[:8]

    def _tokenize_korean_keywords(self, value: str) -> List[str]:
        text = str(value or "")
        candidates = re.findall(r"[가-힣A-Za-z0-9]{2,}", text)
        banned = {"정리", "이유", "의미", "기준", "독자", "관점"}
        return [item for item in candidates if item not in banned]

    def _dedupe(self, values: List[str]) -> List[str]:
        output = []
        seen = set()
        for value in values:
            item = str(value or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            output.append(item)
        return output
