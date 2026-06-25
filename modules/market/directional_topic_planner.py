"""시장 수치보다 화자의 목적을 먼저 세우는 방향성 주제 플래너."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


NUMERIC_TITLE_TERMS = (
    "금리",
    "환율",
    "지수",
    "코스피",
    "코스닥",
    "나스닥",
    "선물",
    "달러",
    "엔화",
    "비트코인",
    "수익률",
    "유가",
)


@dataclass(frozen=True)
class EvidenceRole:
    """수치 근거가 글에서 맡을 역할."""

    metric_key: str
    label: str
    role: str
    reason: str
    source: str = ""
    value: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON 저장 가능한 dict로 변환한다."""

        payload: dict[str, Any] = {
            "metric_key": self.metric_key,
            "label": self.label,
            "role": self.role,
            "reason": self.reason,
            "source": self.source,
        }
        if self.value is not None:
            payload["value"] = self.value
        return payload


@dataclass(frozen=True)
class EditorialIntent:
    """글 제목과 본문을 이끌 방향성 의도."""

    issue_title: str
    reader_problem: str
    speaker_purpose: str
    angle: str
    title_candidates: tuple[str, ...]
    evidence_roles: tuple[EvidenceRole, ...] = ()
    source: str = ""
    quality_score: float = 0.0
    rejected_titles: tuple[str, ...] = ()
    why_today: tuple[str, ...] = ()
    article_type: str = ""
    supporting_sources: tuple[str, ...] = ()
    do_not_claim: tuple[str, ...] = ()
    direction_signal_plan: dict[str, Any] | None = None

    @property
    def primary_title(self) -> str:
        """대표 제목 후보를 반환한다."""

        return self.title_candidates[0] if self.title_candidates else ""

    def to_dict(self) -> dict[str, Any]:
        """JSON 저장 가능한 dict로 변환한다."""

        return {
            "issue_title": self.issue_title,
            "reader_problem": self.reader_problem,
            "speaker_purpose": self.speaker_purpose,
            "angle": self.angle,
            "title_candidates": list(self.title_candidates),
            "evidence_roles": [role.to_dict() for role in self.evidence_roles],
            "source": self.source,
            "quality_score": round(float(self.quality_score), 4),
            "rejected_titles": list(self.rejected_titles),
            "why_today": list(self.why_today),
            "article_type": self.article_type,
            "supporting_sources": list(self.supporting_sources),
            "do_not_claim": list(self.do_not_claim),
            "direction_signal_plan": self.direction_signal_plan or {},
        }


def plan_directional_topic(
    *,
    base_title: str,
    issues: Sequence[Any] = (),
    confirmed_metrics: Sequence[Any] = (),
    seed_keywords: Sequence[str] = (),
    scope: str = "",
) -> EditorialIntent | None:
    """빅카인즈 이슈와 수치 근거를 분리해 방향성 주제를 만든다."""

    issue = _select_issue(issues, seed_keywords=seed_keywords, scope=scope)
    if issue is None:
        return None

    issue_title = _issue_title(issue)
    if not issue_title:
        return None

    angle = _angle_for_issue(issue_title, seed_keywords=seed_keywords)
    evidence_roles = tuple(
        _role
        for _role in (_evidence_role_from_metric(metric, issue_title=issue_title, angle=angle) for metric in confirmed_metrics)
        if _role is not None
    )[:6]
    speaker_purpose = _speaker_purpose(issue_title=issue_title, angle=angle)
    reader_problem = _reader_problem(issue_title=issue_title, angle=angle)
    raw_candidates = _title_candidates(issue_title=issue_title, angle=angle)
    accepted: list[str] = []
    rejected: list[str] = []
    for candidate in raw_candidates:
        evaluation = evaluate_directional_title(candidate)
        if evaluation["passes"]:
            accepted.append(candidate)
        else:
            rejected.append(candidate)
    if not accepted:
        fallback = _fallback_direction_title(issue_title, angle=angle)
        accepted.append(fallback)

    source = _issue_source(issue)
    quality_score = max(evaluate_directional_title(accepted[0])["score"], 0.0)
    if not source:
        source = "BigKinds public direction"
    supporting_sources = _supporting_sources(issue, evidence_roles)
    why_today = _why_today_reasons(issue=issue, angle=angle, evidence_roles=evidence_roles)
    article_type = _article_type_for_angle(angle)

    return EditorialIntent(
        issue_title=issue_title,
        reader_problem=reader_problem,
        speaker_purpose=speaker_purpose,
        angle=angle,
        title_candidates=tuple(dict.fromkeys(accepted))[:3],
        evidence_roles=evidence_roles,
        source=source,
        quality_score=quality_score,
        rejected_titles=tuple(dict.fromkeys([base_title, *rejected])),
        why_today=why_today,
        article_type=article_type,
        supporting_sources=supporting_sources,
        do_not_claim=_do_not_claims(issue_title=issue_title, article_type=article_type),
        direction_signal_plan=_direction_signal_plan_from_issue(issue),
    )


def evaluate_directional_title(title: str) -> dict[str, Any]:
    """제목이 수치 나열형인지 방향성 제목인지 평가한다."""

    normalized = str(title or "").strip()
    lower = normalized.lower()
    term_hits = [term for term in NUMERIC_TITLE_TERMS if term.lower() in lower]
    has_number = bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|bp|원|달러|엔|포인트)?", normalized))
    reader_terms = ("확인", "구분", "줄여야", "정해야", "보자", "먼저", "때", "조건", "순서", "리스크", "판단")
    reader_hit_count = sum(1 for term in reader_terms if term in normalized)
    issue_terms = ("AI", "반도체", "수요", "투자", "수출", "소비", "전력", "데이터센터", "기업")
    issue_hit_count = sum(1 for term in issue_terms if term.lower() in lower)
    generic_hits = sum(1 for term in ("남긴 기준", "말해주는 기준", "보여준 신호", "데이터가 남긴") if term in normalized)

    score = 55.0
    score += min(reader_hit_count, 3) * 12.0
    score += min(issue_hit_count, 2) * 8.0
    score -= max(0, len(term_hits) - 1) * 18.0
    score -= 18.0 if has_number else 0.0
    score -= generic_hits * 18.0
    if normalized and any(normalized.startswith(term) for term in NUMERIC_TITLE_TERMS):
        score -= 15.0

    reasons: list[str] = []
    if len(term_hits) >= 2:
        reasons.append("지표/수치 용어가 2개 이상입니다.")
    if has_number:
        reasons.append("제목에 구체적 숫자가 들어 있습니다.")
    if generic_hits:
        reasons.append("화자의 목적보다 추상적 데이터 해석 표현이 앞섭니다.")
    if reader_hit_count == 0:
        reasons.append("독자가 무엇을 판단해야 하는지 약합니다.")

    return {
        "score": round(max(0.0, min(100.0, score)), 2),
        "passes": score >= 62.0 and not (len(term_hits) >= 3 or has_number),
        "numeric_terms": term_hits,
        "reasons": reasons,
    }


def editorial_intent_to_context(intent: Mapping[str, Any]) -> str:
    """EditorialIntent dict를 프롬프트 컨텍스트 문자열로 변환한다."""

    if not intent:
        return ""
    lines = [
        "방향성 주제:",
        f"- 핵심 이슈: {intent.get('issue_title', '')}",
        f"- 독자 문제: {intent.get('reader_problem', '')}",
        f"- 화자 목적: {intent.get('speaker_purpose', '')}",
        f"- 글 각도: {intent.get('angle', '')}",
        f"- 글 유형: {intent.get('article_type', '')}",
    ]
    why_today = intent.get("why_today", [])
    if isinstance(why_today, Sequence) and not isinstance(why_today, (str, bytes, bytearray)) and why_today:
        lines.append("오늘 다루는 이유:")
        for reason in why_today[:4]:
            if str(reason).strip():
                lines.append(f"- {reason}")
    titles = intent.get("title_candidates", [])
    if isinstance(titles, Sequence) and not isinstance(titles, (str, bytes, bytearray)) and titles:
        lines.append(f"- 권장 제목: {titles[0]}")
    roles = intent.get("evidence_roles", [])
    if isinstance(roles, Sequence) and not isinstance(roles, (str, bytes, bytearray)) and roles:
        lines.append("근거 수치 역할:")
        for raw in roles[:5]:
            if not isinstance(raw, Mapping):
                continue
            lines.append(
                f"- {raw.get('metric_key', '')}: {raw.get('role', '')} / {raw.get('reason', '')}"
            )
    supporting_sources = intent.get("supporting_sources", [])
    if (
        isinstance(supporting_sources, Sequence)
        and not isinstance(supporting_sources, (str, bytes, bytearray))
        and supporting_sources
    ):
        lines.append(f"보조 출처 계층: {', '.join(str(source) for source in supporting_sources[:5] if str(source).strip())}")
    do_not_claim = intent.get("do_not_claim", [])
    if isinstance(do_not_claim, Sequence) and not isinstance(do_not_claim, (str, bytes, bytearray)) and do_not_claim:
        lines.append("금지할 단정:")
        for claim in do_not_claim[:3]:
            if str(claim).strip():
                lines.append(f"- {claim}")
    lines.append("작성 원칙: 제목과 서론은 방향성 주제로 시작하고, 수치는 본문에서 근거로만 사용하세요.")
    return "\n".join(line for line in lines if str(line).strip())


def _select_issue(issues: Sequence[Any], *, seed_keywords: Sequence[str], scope: str) -> Any | None:
    scored: list[tuple[float, Any]] = []
    keywords = [str(item).strip().lower() for item in seed_keywords if str(item).strip()]
    for issue in issues:
        title = _issue_title(issue)
        if not title:
            continue
        score = float(_issue_count(issue) or 0) * 0.2 + float(_issue_confidence(issue)) * 20.0
        score += float(_issue_direction_score(issue)) * 0.6
        lowered = title.lower()
        score += sum(12.0 for keyword in keywords if keyword and keyword in lowered)
        if _is_market_related(title):
            score += 18.0
        if str(scope).lower() in {"kr", "global"} and any(token in title for token in ("반도체", "AI", "수출", "전력")):
            score += 10.0
        scored.append((score, issue))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def _evidence_role_from_metric(metric: Any, *, issue_title: str, angle: str) -> EvidenceRole | None:
    key = _metric_value(metric, "key") or _metric_value(metric, "symbol") or _metric_value(metric, "metric_key")
    source = _metric_value(metric, "source")
    label = _metric_value(metric, "label") or key
    if not key:
        return None
    key_upper = key.upper()
    role = "보조 근거"
    reason = "방향성 주제를 과장하지 않도록 시장 배경을 확인합니다."
    if "US10Y" in key_upper or "TREASURY" in key_upper:
        role = "성장주 부담 확인"
        reason = "금리 변화가 AI/반도체 같은 성장 기대의 부담 요인인지 봅니다."
    elif "USD" in key_upper or "JPY" in key_upper or "KRW" in key_upper:
        role = "외국인 수급과 위험 선호 확인"
        reason = "환율은 국내 시장 수급과 아시아 위험 선호를 읽는 보조 지표입니다."
    elif "KOSPI" in key_upper or "KOSDAQ" in key_upper:
        role = "국내 시장 기준점"
        reason = "오늘 이슈가 국내 지수 흐름과 따로 움직이는지 확인합니다."
    elif "BTC" in key_upper or "ETH" in key_upper:
        role = "위험 선호 보조 지표"
        reason = "코인은 투자 심리의 과열 또는 방어 전환을 보조적으로 봅니다."
    elif "CPI" in key_upper or "PPI" in key_upper:
        role = "물가 압력 확인"
        reason = "소비/가격 이슈가 금리 기대와 연결되는지 확인합니다."
    value = _metric_float(metric)
    return EvidenceRole(metric_key=key, label=label, role=role, reason=reason, source=source, value=value)


def _angle_for_issue(issue_title: str, *, seed_keywords: Sequence[str]) -> str:
    issue_text = issue_title.lower()
    text = " ".join([issue_title, *[str(item) for item in seed_keywords]]).lower()
    if any(token in issue_text for token in ("ai", "반도체", "전력", "데이터센터", "배터리", "수출")):
        return "산업 흐름형"
    if any(token in issue_text for token in ("리스크", "환율", "금리", "중동", "전쟁", "유가")):
        return "리스크 방어형"
    if any(token in issue_text for token in ("소비", "물가", "고용", "실적")):
        return "확인 조건형"
    if any(token in text for token in ("리스크", "환율", "금리", "중동", "전쟁", "유가")):
        return "리스크 방어형"
    if any(token in text for token in ("ai", "반도체", "전력", "데이터센터", "배터리", "수출")):
        return "산업 흐름형"
    if any(token in text for token in ("소비", "물가", "고용", "실적")):
        return "확인 조건형"
    return "판단 순서형"


def _speaker_purpose(*, issue_title: str, angle: str) -> str:
    if angle == "산업 흐름형":
        return f"{issue_title} 이슈를 맞히는 이야기가 아니라 실제 수요와 투자로 이어지는 조건을 확인하게 한다."
    if angle == "리스크 방어형":
        return f"{issue_title} 이슈 앞에서 오늘 먼저 줄여야 할 판단과 확인해야 할 리스크를 정리한다."
    if angle == "확인 조건형":
        return f"{issue_title} 흐름이 시장 기대와 실제 지표 사이에서 어디까지 확인됐는지 구분한다."
    return f"{issue_title} 이슈를 오늘의 매매 결론이 아니라 판단 순서로 바꿔 설명한다."


def _reader_problem(*, issue_title: str, angle: str) -> str:
    if angle == "산업 흐름형":
        return "뉴스 기대와 실제 기업/시장 확인 신호를 구분하기 어렵다."
    if angle == "리스크 방어형":
        return "시장 변수가 많아 무엇을 먼저 피하고 확인해야 할지 헷갈린다."
    if angle == "확인 조건형":
        return "좋은 뉴스와 실제 숫자 사이의 간격을 판단하기 어렵다."
    return "이슈는 보이지만 오늘 내 판단 기준으로 바꾸기 어렵다."


def _title_candidates(*, issue_title: str, angle: str) -> tuple[str, ...]:
    core = _short_issue(issue_title)
    if angle == "산업 흐름형":
        return (
            f"오늘 국장, {core}를 맞히기보다 실제 수요가 확인되는지를 보자",
            f"{core} 기대감이 숫자로 바뀌는 조건을 먼저 보자",
            f"{core} 뉴스보다 중요한 것은 확인되는 수요다",
        )
    if angle == "리스크 방어형":
        return (
            f"{core} 앞에서 오늘 먼저 줄여야 할 판단",
            f"오늘 시장, {core}보다 리스크 확인 순서가 먼저다",
            f"{core}가 커질 때 초보 투자자가 먼저 볼 조건",
        )
    if angle == "확인 조건형":
        return (
            f"{core} 흐름, 기대와 확인된 숫자를 구분할 때",
            f"오늘은 {core}보다 확인된 조건을 먼저 보자",
            f"{core} 이슈를 시장 기준으로 바꿔 읽는 법",
        )
    return (
        f"{core} 이슈를 오늘의 판단 순서로 바꿔보자",
        f"오늘 시장, {core}보다 먼저 정할 기준",
        f"{core} 뉴스 앞에서 흔들리지 않는 확인 순서",
    )


def _fallback_direction_title(issue_title: str, *, angle: str) -> str:
    core = _short_issue(issue_title)
    if angle == "산업 흐름형":
        return f"{core} 기대보다 실제 확인 조건을 먼저 보자"
    if angle == "리스크 방어형":
        return f"{core} 앞에서 오늘의 리스크 순서를 정하자"
    return f"{core} 이슈를 판단 기준으로 바꿔보자"


def _issue_title(issue: Any) -> str:
    if isinstance(issue, Mapping):
        return _clean_text(str(issue.get("issue_title") or issue.get("title") or issue.get("keyword") or ""))
    return _clean_text(str(getattr(issue, "issue_title", "") or getattr(issue, "title", "") or ""))


def _issue_source(issue: Any) -> str:
    if isinstance(issue, Mapping):
        return _clean_text(str(issue.get("source_url") or issue.get("source") or ""))
    return _clean_text(str(getattr(issue, "source_url", "") or getattr(issue, "source", "") or ""))


def _issue_count(issue: Any) -> int:
    if isinstance(issue, Mapping):
        raw = issue.get("news_count") or issue.get("count")
    else:
        raw = getattr(issue, "news_count", None)
        if raw is None:
            raw = getattr(issue, "count", 0)
    try:
        return int(raw or 0)
    except Exception:
        return 0


def _issue_confidence(issue: Any) -> float:
    raw = issue.get("confidence") if isinstance(issue, Mapping) else getattr(issue, "confidence", 0.0)
    try:
        return float(raw or 0.0)
    except Exception:
        return 0.0


def _issue_direction_score(issue: Any) -> float:
    raw = issue.get("direction_score") if isinstance(issue, Mapping) else getattr(issue, "direction_score", 0.0)
    try:
        return float(raw or 0.0)
    except Exception:
        return 0.0


def _issue_source_tier(issue: Any) -> str:
    if isinstance(issue, Mapping):
        return _clean_text(str(issue.get("source_tier", "") or ""))
    return _clean_text(str(getattr(issue, "source_tier", "") or ""))


def _issue_keywords(issue: Any) -> tuple[str, ...]:
    raw = issue.get("keywords", ()) if isinstance(issue, Mapping) else getattr(issue, "keywords", ())
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return tuple(_clean_text(str(item)) for item in raw if _clean_text(str(item)))[:8]
    return tuple(_clean_text(part) for part in re.split(r"[,;/|·ㆍ]+", str(raw or "")) if _clean_text(part))[:8]


def _supporting_sources(issue: Any, evidence_roles: Sequence[EvidenceRole]) -> tuple[str, ...]:
    sources: list[str] = []
    issue_source = _issue_source(issue)
    if issue_source:
        sources.append(issue_source)
    source_tier = _issue_source_tier(issue)
    if source_tier:
        sources.append(source_tier)
    for role in evidence_roles:
        if role.source:
            sources.append(role.source)
    return tuple(dict.fromkeys(source for source in sources if source))[:6]


def _why_today_reasons(
    *,
    issue: Any,
    angle: str,
    evidence_roles: Sequence[EvidenceRole],
) -> tuple[str, ...]:
    title = _issue_title(issue)
    source_tier = _issue_source_tier(issue)
    direction_score = _issue_direction_score(issue)
    reasons: list[str] = []
    if source_tier:
        reasons.append(f"{source_tier} 계층에서 '{_short_issue(title)}' 신호가 잡혔습니다.")
    else:
        reasons.append(f"'{_short_issue(title)}' 이슈가 오늘 글의 사회적 관심 축으로 잡혔습니다.")
    if direction_score:
        reasons.append(f"방향성 점수 {direction_score:.1f}점으로 시장 글감 적합도가 높습니다.")
    if evidence_roles:
        role_names = ", ".join(role.role for role in evidence_roles[:3] if role.role)
        if role_names:
            reasons.append(f"수치는 결론이 아니라 {role_names} 근거로 배치합니다.")
    if angle == "산업 흐름형":
        reasons.append("산업 기대가 실제 수요와 투자로 이어지는지 확인하기 좋은 주제입니다.")
    elif angle == "리스크 방어형":
        reasons.append("시장 변수가 많아 먼저 줄일 판단과 확인할 리스크를 정리하기 좋습니다.")
    elif angle == "확인 조건형":
        reasons.append("기대와 실제로 확인된 숫자를 구분해야 하는 주제입니다.")
    return tuple(dict.fromkeys(reason for reason in reasons if reason))[:5]


def _article_type_for_angle(angle: str) -> str:
    if angle == "산업 흐름형":
        return "이슈 해설형"
    if angle == "리스크 방어형":
        return "리스크 경고형"
    if angle == "확인 조건형":
        return "데이터 해석형"
    return "투자 원칙형"


def _do_not_claims(*, issue_title: str, article_type: str) -> tuple[str, ...]:
    base = [
        "단일 뉴스나 수치만으로 시장 방향을 확정하지 않는다.",
        "특정 종목 매수/매도를 권유하지 않는다.",
        "공식 출처로 확인되지 않은 수치나 인과관계를 단정하지 않는다.",
    ]
    if article_type == "리스크 경고형":
        base.append(f"{issue_title} 때문에 모든 위험자산이 같은 방향으로 움직인다고 단정하지 않는다.")
    elif article_type == "이슈 해설형":
        base.append(f"{issue_title} 기대가 곧바로 실적이나 주가로 연결된다고 단정하지 않는다.")
    return tuple(base[:4])


def _direction_signal_plan_from_issue(issue: Any) -> dict[str, Any] | None:
    if isinstance(issue, Mapping):
        raw = issue.get("direction_signal_plan")
    else:
        raw = getattr(issue, "direction_signal_plan", None)
    return raw if isinstance(raw, dict) else None


def _metric_value(metric: Any, key: str) -> str:
    if isinstance(metric, Mapping):
        return _clean_text(str(metric.get(key, "") or ""))
    return _clean_text(str(getattr(metric, key, "") or ""))


def _metric_float(metric: Any) -> float | None:
    raw = metric.get("value") if isinstance(metric, Mapping) else getattr(metric, "value", None)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _is_market_related(title: str) -> bool:
    text = title.lower()
    return any(
        token in text
        for token in ("ai", "반도체", "삼성", "하이닉스", "전력", "배터리", "수출", "소비", "금리", "환율", "증시")
    )


def _short_issue(issue_title: str) -> str:
    text = _clean_text(issue_title)
    text = re.sub(r"\s+", " ", text)
    if len(text) <= 18:
        return text
    return text[:18].rstrip()


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
