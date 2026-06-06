"""매크로 글 후보 품질 평가기."""

from __future__ import annotations

from typing import Any, Dict, List


class MacroQualityEvaluator:
    """매크로 데이터 전용 품질 점수를 계산한다."""

    def evaluate(
        self,
        *,
        metrics_json: Dict[str, Any],
        insight_json: Dict[str, Any],
        candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """문서 분석과 후보 글의 품질을 0~100으로 평가한다."""
        metric_count = int(metrics_json.get("metric_count", 0) or 0)
        evidence_ok = bool(metrics_json.get("numeric_claims_have_evidence", False))
        verification = metrics_json.get("verification", {})
        verification_score = 60.0
        confirmed_source_count = 0
        if isinstance(verification, dict):
            verification_score = float(verification.get("verificationScore", 60.0) or 60.0)
            confirmed_source_count = int(verification.get("confirmedSourceCount", 0) or 0)
        requires_two_sources = bool(
            verification.get("requiresTwoSourceConfirmation", False)
        ) if isinstance(verification, dict) else False
        candidate_count = len(candidates)
        insight_summary = str(insight_json.get("summary", "") or "")
        philosophy = str(insight_json.get("philosophyFrame", "") or "")

        data_accuracy = 92 if evidence_ok and metric_count >= 3 else 76 if metric_count else 45
        data_accuracy = round(data_accuracy * 0.75 + verification_score * 0.25, 1)
        if metric_count and requires_two_sources and confirmed_source_count < 2:
            data_accuracy = min(data_accuracy, 82.0)
        source_citation = 90 if evidence_ok else 60
        insight_score = 88 if len(insight_summary) >= 80 else 70
        investment_relevance = 86 if str(insight_json.get("investmentAngle", "") or "") else 65
        small_business = 84 if str(insight_json.get("smallBusinessAngle", "") or "") else 60
        non_clickbait = 88 if self._non_clickbait(candidates) else 68
        persona = 84
        philosophy_score = 86 if philosophy else 60

        overall = round(
            data_accuracy * 0.24
            + source_citation * 0.16
            + insight_score * 0.18
            + investment_relevance * 0.12
            + small_business * 0.10
            + non_clickbait * 0.08
            + persona * 0.06
            + philosophy_score * 0.06,
            1,
        )
        if candidate_count < 3:
            overall = min(overall, 84.0)

        if overall < 85:
            action = "rewrite"
        elif overall < 92:
            action = "needs_review"
        elif overall < 95:
            action = "approval_request"
        else:
            action = "exemplar"

        return {
            "dataAccuracyScore": data_accuracy,
            "sourceCitationScore": source_citation,
            "insightScore": insight_score,
            "investmentRelevanceScore": investment_relevance,
            "smallBusinessRelevanceScore": small_business,
            "nonClickbaitScore": non_clickbait,
            "personaConsistencyScore": persona,
            "philosophyFrameScore": philosophy_score,
            "overallScore": overall,
            "recommendedAction": action,
        }

    def _non_clickbait(self, candidates: List[Dict[str, Any]]) -> bool:
        banned = ("폭등", "무조건", "대박", "몰빵", "급등주", "사야 하는")
        for item in candidates:
            title = str(item.get("title", "") or "")
            if any(word in title for word in banned):
                return False
        return True
