"""매크로 자료 텔레그램 검토 메시지 생성."""

from __future__ import annotations

from typing import Any, Dict, List


def build_macro_review_message(
    *,
    document: Dict[str, Any],
    metrics_json: Dict[str, Any],
    insight_json: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    quality_json: Dict[str, Any] | None = None,
) -> str:
    """요약 + 핵심 수치 + 후보 제목을 한 번에 담은 검토 메시지를 만든다."""
    quality = quality_json if isinstance(quality_json, dict) else {}
    lines = [
        "매크로 자료 검토 요청",
        "",
        f"자료: {document.get('title', '-')}",
        f"출처: {document.get('source', '-')}",
        f"발표일: {document.get('published_at') or '-'}",
        f"원문: {document.get('url', '-')}",
    ]
    file_url = str(document.get("file_url", "") or "").strip()
    if file_url:
        lines.append(f"첨부: {file_url}")

    lines.extend(["", "요약"])
    summary = str(insight_json.get("summary", "") or "").strip()
    lines.append(_trim(summary or "요약을 만들기 위한 수치가 아직 충분하지 않습니다.", 650))

    lines.extend(["", "핵심 수치"])
    metrics = list(metrics_json.get("metrics", []) or [])
    if metrics:
        for idx, item in enumerate(metrics[:8], start=1):
            label = str(item.get("label", "") or "-")
            value = str(item.get("value", "") or "-")
            evidence = _trim(str(item.get("evidence", "") or ""), 120)
            lines.append(f"{idx}. {label}: {value}")
            if evidence:
                lines.append(f"   근거: {evidence}")
    else:
        lines.append("- 추출된 핵심 수치가 없습니다. 원문 확인이 필요합니다.")

    verification = metrics_json.get("verification", {})
    if isinstance(verification, dict):
        source_policy = str(verification.get("sourcePolicy", "light") or "light").strip().lower()
        requires_two = bool(verification.get("requiresTwoSourceConfirmation", False))
        lines.extend(["", "검증 상태"])
        lines.append(f"- 기간: {verification.get('period') or '-'}")
        if requires_two:
            lines.append(f"- 확인 출처: {verification.get('confirmedSourceCount', 0)}/2")
        else:
            lines.append(f"- 소스 정책: {source_policy} (원문 기반 초안 허용)")
            lines.append(f"- 확보 출처: {verification.get('confirmedSourceCount', 0)}개")
        lines.append(f"- 점수: {verification.get('verificationScore', '-')}/100")
        lines.append(f"- 다음 조치: {verification.get('recommendedNextAction', '-')}")
        sources = verification.get("sources", [])
        if isinstance(sources, list):
            for source in sources[:2]:
                if not isinstance(source, dict):
                    continue
                lines.append(f"- {source.get('source', '-')}: {source.get('status', '-')}")
                comparisons = source.get("comparisons", [])
                if isinstance(comparisons, list):
                    for comparison in comparisons[:3]:
                        if not isinstance(comparison, dict):
                            continue
                        mark = "일치" if comparison.get("matched") else "불일치"
                        lines.append(
                            f"  · {comparison.get('label', '-')}: {mark} "
                            f"(문서 {comparison.get('extractedEokUsd', '-')}, API {comparison.get('apiEokUsd', '-')}억 달러)"
                        )

    lines.extend(["", "글 후보"])
    if candidates:
        for idx, item in enumerate(candidates[:5], start=1):
            title = str(item.get("title", "") or "-")
            angle = str(item.get("angle", "") or "-")
            reader = str(item.get("target_reader", "") or "-")
            lines.append(f"{idx}. {title}")
            candidate_id = str(item.get("id", "") or "").strip()
            if candidate_id:
                lines.append(f"   후보ID: {candidate_id}")
            lines.append(f"   관점: {angle} / 독자: {reader}")
    else:
        lines.append("- 생성된 후보가 없습니다.")

    if quality:
        lines.extend(
            [
                "",
                "품질 판정",
                f"- 종합: {quality.get('overallScore', '-')}/100",
                f"- 권장: {quality.get('recommendedAction', '-')}",
            ]
        )

    lines.extend(
        [
            "",
            "운영 메모",
            "- 이 단계는 자동 발행이 아니라 검토 대기입니다.",
            "- 숫자가 어색하면 원문 링크를 먼저 확인합니다.",
            "- 승인할 후보는 다음 단계에서 블로그 초안으로 승격합니다.",
        ]
    )
    return "\n".join(lines)


def _trim(value: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max(0, max_chars - 1)]}…"
