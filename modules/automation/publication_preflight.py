"""발행 직전 세션/출처/문구 사전점검."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .claim_ledger import build_claim_ledger
from ..market.directional_topic_planner import evaluate_directional_title
from ..market.source_pack import evaluate_source_pack_dict, source_pack_from_payload


BLOCKED_FACT_GAP_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"확인(?:하지|할 수) 못했습니다", "확인 불가 문구"),
    (r"확인\s*필요", "확인 필요 문구"),
    (r"검증\s*필요", "검증 필요 문구"),
    (r"출처\s*확인\s*필요", "출처 확인 필요 문구"),
    (r"미확인", "미확인 문구"),
    (r"수집(?:하지|할 수) 못했습니다", "수집 실패 문구"),
    (r"자료\s*(?:없음|부족)", "자료 부족 문구"),
    (r"원자료\s*(?:없음|부족)", "원자료 부족 문구"),
    (r"데이터가\s*(?:없습니다|부족합니다)", "데이터 부족 문구"),
    (r"근거가\s*(?:없습니다|부족합니다)", "근거 부족 문구"),
    (r"출처가\s*(?:없습니다|부족합니다)", "출처 부족 문구"),
    (r"구체적\s*수치(?:는|를)?\s*확인", "수치 확인 실패 문구"),
    (r"\bTODO\b|\bTBD\b|\bN/A\b|자료없음|데이터없음", "작성 잔여 토큰"),
)

BLOCKED_INVESTMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"폭등\s*확정", "확정 수익 표현"),
    (r"폭락\s*확정", "확정 손실 표현"),
    (r"수익\s*보장", "수익 보장 표현"),
    (r"100\s*%\s*(?:수익|상승|적중)", "100% 단정 표현"),
    (r"무조건\s*(?:매수|사야|오른다|간다)", "무조건 매수 표현"),
    (r"반드시\s*(?:매수|사야|오른다|간다)", "단정 매수 표현"),
    (r"(?:매수|매도)\s*(?:추천|신호|타이밍)", "매매 권유 표현"),
    (r"목표가\s*(?:확정|제시)", "목표가 단정 표현"),
    (r"손절가\s*(?:확정|제시)", "손절가 단정 표현"),
    (r"단타\s*(?:추천|기회)", "단타 권유 표현"),
    (r"풀매수|몰빵|올인", "과도한 매매 유도 표현"),
)


@dataclass(frozen=True)
class PreflightIssue:
    """발행 전 점검에서 발견한 단일 이슈."""

    code: str
    message: str
    severity: str = "error"
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        """발행을 막는 이슈인지 반환한다."""

        return self.severity == "error"

    def to_dict(self) -> dict[str, Any]:
        """JSON 저장 가능한 dict로 변환한다."""

        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class PublicationPreflightResult:
    """발행 전 점검 결과."""

    ok: bool
    issues: tuple[PreflightIssue, ...] = ()
    source_pack: dict[str, Any] = field(default_factory=dict)
    claim_ledger: dict[str, Any] = field(default_factory=dict)
    checked_at: str = ""

    @property
    def blocking_issues(self) -> tuple[PreflightIssue, ...]:
        """발행 차단 이슈만 반환한다."""

        return tuple(issue for issue in self.issues if issue.blocking)

    @property
    def primary_error_code(self) -> str:
        """Job 실패 처리에 사용할 대표 에러 코드를 반환한다."""

        for issue in self.blocking_issues:
            if issue.code in {"AUTH_EXPIRED", "CAPTCHA_REQUIRED"}:
                return issue.code
        return "QUALITY_REJECTED"

    @property
    def summary(self) -> str:
        """짧은 사람이 읽는 요약을 반환한다."""

        blocking = self.blocking_issues
        if not blocking:
            return "발행 전 점검 통과"
        return "; ".join(issue.message for issue in blocking[:4])

    def to_quality_snapshot(self) -> dict[str, Any]:
        """quality_snapshot에 저장할 사전점검 결과를 만든다."""

        return {
            "status": "passed" if self.ok else "blocked",
            "checked_at": self.checked_at,
            "primary_error_code": self.primary_error_code if not self.ok else "",
            "issues": [issue.to_dict() for issue in self.issues],
            "source_pack": self.source_pack,
            "claim_ledger": self.claim_ledger,
        }


def run_publication_preflight(
    *,
    job: Any,
    payload: Mapping[str, Any],
    publisher: Any,
    publish_mode: str,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
) -> PublicationPreflightResult:
    """발행 직전 공통 사전점검을 수행한다."""

    env_map = env if env is not None else os.environ
    if not _env_enabled(env_map, "AUTOBLOG_PUBLICATION_PREFLIGHT_ENABLED", default=True):
        return PublicationPreflightResult(
            ok=True,
            checked_at=_iso_utc(now or datetime.now(timezone.utc)),
        )

    checked_at = _iso_utc(now or datetime.now(timezone.utc))
    title = str(payload.get("title") or getattr(job, "title", "") or "").strip()
    content = str(payload.get("content", "") or "")
    issues: list[PreflightIssue] = []

    session_file = _resolve_naver_session_file(job=job, publisher=publisher)
    if session_file is not None:
        issues.extend(check_naver_session_state(session_file, now=now, env=env_map))

    issues.extend(scan_blocked_publication_phrases(title=title, content=content))
    title_issue = _directional_title_issue_for_job(job=job, title=title, env=env_map)
    if title_issue is not None:
        issues.append(title_issue)

    source_pack = source_pack_from_payload(payload, topic=title)
    source_pack = _evaluate_source_pack_with_env(source_pack, env_map)
    claim_ledger: dict[str, Any] = {}
    claim_issue = _claim_ledger_issue_for_job(
        job=job,
        content=content,
        source_pack=source_pack,
        env=env_map,
    )
    if claim_issue is not None:
        issues.append(claim_issue)
        claim_ledger = dict(claim_issue.detail.get("claim_ledger", {}) or {})
    elif _should_run_claim_ledger(job=job, source_pack=source_pack, env=env_map):
        claim_ledger = build_claim_ledger(
            content=content,
            source_pack=source_pack,
            max_unsupported_claims=_int_env(env_map, "AUTOBLOG_CLAIM_LEDGER_MAX_UNSUPPORTED", default=0, minimum=0),
        ).to_dict()

    source_issue = _source_pack_issue_for_job(
        job=job,
        source_pack=source_pack,
        publish_mode=publish_mode,
        env=env_map,
    )
    if source_issue is not None:
        issues.append(source_issue)

    return PublicationPreflightResult(
        ok=not any(issue.blocking for issue in issues),
        issues=tuple(issues),
        source_pack=source_pack,
        claim_ledger=claim_ledger,
        checked_at=checked_at,
    )


def check_naver_session_state(
    session_file: Path,
    *,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[PreflightIssue, ...]:
    """Playwright storage_state 파일만으로 네이버 로그인 세션을 빠르게 점검한다."""

    env_map = env if env is not None else os.environ
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    issues: list[PreflightIssue] = []
    if not session_file.exists():
        return (
            PreflightIssue(
                code="AUTH_EXPIRED",
                message="네이버 세션 파일이 없어 로그인 갱신이 필요합니다.",
                detail={"session_file": str(session_file)},
            ),
        )

    try:
        payload = json.loads(session_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return (
            PreflightIssue(
                code="AUTH_EXPIRED",
                message="네이버 세션 파일을 읽을 수 없어 로그인 갱신이 필요합니다.",
                detail={"session_file": str(session_file), "error": str(exc)},
            ),
        )

    cookies = payload.get("cookies") if isinstance(payload, dict) else None
    if not isinstance(cookies, list) or not cookies:
        return (
            PreflightIssue(
                code="AUTH_EXPIRED",
                message="네이버 세션 쿠키가 비어 있어 로그인 갱신이 필요합니다.",
                detail={"session_file": str(session_file)},
            ),
        )

    naver_cookies = [
        cookie
        for cookie in cookies
        if isinstance(cookie, Mapping)
        and "naver" in str(cookie.get("domain", "") or "").lower()
    ]
    login_cookie_names = {
        str(cookie.get("name", "") or "").strip()
        for cookie in naver_cookies
        if isinstance(cookie, Mapping)
    }
    if not ({"NID_AUT", "NID_SES"} & login_cookie_names):
        issues.append(
            PreflightIssue(
                code="AUTH_EXPIRED",
                message="네이버 로그인 핵심 쿠키가 없어 로그인 갱신이 필요합니다.",
                detail={
                    "session_file": str(session_file),
                    "cookie_names": sorted(login_cookie_names)[:20],
                },
            )
        )

    expiring_login_cookies = [
        cookie
        for cookie in naver_cookies
        if str(cookie.get("name", "") or "").strip() in {"NID_AUT", "NID_SES"}
    ]
    if expiring_login_cookies:
        now_ts = current.timestamp()
        expired = []
        for cookie in expiring_login_cookies:
            expires = _float_or_none(cookie.get("expires"))
            if expires is not None and expires > 0 and expires <= now_ts:
                expired.append(str(cookie.get("name", "") or "unknown"))
        if expired and len(expired) == len(expiring_login_cookies):
            issues.append(
                PreflightIssue(
                    code="AUTH_EXPIRED",
                    message="네이버 로그인 쿠키가 만료되어 로그인 갱신이 필요합니다.",
                    detail={"session_file": str(session_file), "expired_cookies": expired},
                )
            )

    max_age_days = _int_env(env_map, "AUTOBLOG_NAVER_SESSION_WARN_AGE_DAYS", default=6, minimum=1)
    try:
        mtime = datetime.fromtimestamp(session_file.stat().st_mtime, tz=timezone.utc)
        age_days = (current - mtime).total_seconds() / 86400
    except Exception:
        age_days = 0.0
    if age_days >= max_age_days:
        issues.append(
            PreflightIssue(
                code="SESSION_STALE",
                message=f"네이버 세션 파일이 {age_days:.1f}일 전 갱신되어 사전 갱신을 권장합니다.",
                severity="warning",
                detail={"session_file": str(session_file), "age_days": round(age_days, 2)},
            )
        )

    return tuple(issues)


def scan_blocked_publication_phrases(
    *,
    title: str,
    content: str,
) -> tuple[PreflightIssue, ...]:
    """본문에 노출되면 안 되는 데이터 누락/투자 단정 문구를 검사한다."""

    text = f"{title}\n{content}"
    issues: list[PreflightIssue] = []
    hits = _pattern_hits(text, BLOCKED_FACT_GAP_PATTERNS)
    if hits:
        issues.append(
            PreflightIssue(
                code="BLOCKED_PUBLICATION_PHRASE",
                message=f"발행 차단 문구가 있습니다: {', '.join(hit['label'] for hit in hits[:3])}",
                detail={"hits": hits[:8]},
            )
        )

    investment_hits = _pattern_hits(text, BLOCKED_INVESTMENT_PATTERNS)
    if investment_hits:
        issues.append(
            PreflightIssue(
                code="BLOCKED_INVESTMENT_PHRASE",
                message=f"투자 단정/권유 위험 문구가 있습니다: {', '.join(hit['label'] for hit in investment_hits[:3])}",
                detail={"hits": investment_hits[:8]},
            )
        )
    return tuple(issues)


def _directional_title_issue_for_job(
    *,
    job: Any,
    title: str,
    env: Mapping[str, str],
) -> PreflightIssue | None:
    """시장 글 제목이 수치 나열형으로 기운 경우 경고/차단 이슈를 만든다."""

    tags = {str(tag or "").strip().lower() for tag in getattr(job, "tags", []) or []}
    if "market_daily" not in tags and not any(tag.startswith("market_slot:") for tag in tags):
        return None
    evaluation = evaluate_directional_title(title)
    if bool(evaluation.get("passes", False)):
        return None
    severity = "error" if _env_enabled(env, "AUTOBLOG_DIRECTIONAL_TITLE_REQUIRED", default=False) else "warning"
    reasons = [str(item) for item in evaluation.get("reasons", []) if str(item).strip()]
    message = "시장 글 제목이 수치 나열형으로 기울었습니다."
    if reasons:
        message = f"{message} {' '.join(reasons[:2])}"
    return PreflightIssue(
        code="DIRECTIONAL_TITLE_WEAK",
        message=message,
        severity=severity,
        detail={
            "title": title,
            "score": evaluation.get("score", 0),
            "numeric_terms": evaluation.get("numeric_terms", []),
            "reasons": reasons,
        },
    )


def _source_pack_issue_for_job(
    *,
    job: Any,
    source_pack: Mapping[str, Any],
    publish_mode: str,
    env: Mapping[str, str],
) -> PreflightIssue | None:
    tags = {str(tag or "").strip().lower() for tag in getattr(job, "tags", []) or []}
    if "market_daily" not in tags and not any(tag.startswith("market_slot:") for tag in tags):
        return None

    scope = str(source_pack.get("scope", "") or "").strip().lower()
    if scope in {"evergreen", "evergreen_insight", "weekly_reflection"}:
        return None

    strict_for_draft = _env_enabled(env, "AUTOBLOG_SOURCE_PACK_REQUIRED_FOR_DRAFT", default=True)
    strict_for_publish = _env_enabled(env, "AUTOBLOG_SOURCE_PACK_REQUIRED_FOR_PUBLISH", default=True)
    should_block = strict_for_draft or (str(publish_mode).strip().lower() == "publish" and strict_for_publish)
    if not should_block:
        if not bool(source_pack.get("publish_allowed", False)):
            return PreflightIssue(
                code="SOURCE_PACK_WARNING",
                message="Source Pack 근거가 부족합니다. 승인형 초안에는 경고만 기록합니다.",
                severity="warning",
                detail=_source_pack_detail(source_pack),
            )
        return None

    if bool(source_pack.get("publish_allowed", False)):
        return None
    return PreflightIssue(
        code="SOURCE_PACK_INSUFFICIENT",
        message="Source Pack 근거가 부족해 자동 공개발행을 보류합니다.",
        detail=_source_pack_detail(source_pack),
    )


def _claim_ledger_issue_for_job(
    *,
    job: Any,
    content: str,
    source_pack: Mapping[str, Any],
    env: Mapping[str, str],
) -> PreflightIssue | None:
    if not _should_run_claim_ledger(job=job, source_pack=source_pack, env=env):
        return None

    max_unsupported = _int_env(env, "AUTOBLOG_CLAIM_LEDGER_MAX_UNSUPPORTED", default=0, minimum=0)
    ledger = build_claim_ledger(
        content=content,
        source_pack=source_pack,
        max_unsupported_claims=max_unsupported,
    )
    if ledger.unsupported_claim_count <= max_unsupported:
        return None

    first = ledger.unsupported_claims[0] if ledger.unsupported_claims else None
    snippet = first.text[:120] if first is not None else "수치 주장"
    return PreflightIssue(
        code="UNSUPPORTED_CLAIM",
        message=f"Source Pack 근거와 연결되지 않은 수치 주장이 있습니다: {snippet}",
        detail={
            "claim_ledger": ledger.to_dict(),
            "max_unsupported_claims": max_unsupported,
        },
    )


def _should_run_claim_ledger(
    *,
    job: Any,
    source_pack: Mapping[str, Any],
    env: Mapping[str, str],
) -> bool:
    if not _env_enabled(env, "AUTOBLOG_CLAIM_LEDGER_ENABLED", default=True):
        return False
    scope = str(source_pack.get("scope", "") or "").strip().lower()
    if scope in {"evergreen", "evergreen_insight", "weekly_reflection"}:
        return False
    tags = {str(tag or "").strip().lower() for tag in getattr(job, "tags", []) or []}
    is_market_job = "market_daily" in tags or any(tag.startswith("market_slot:") for tag in tags)
    has_metrics = bool(source_pack.get("confirmed_metrics"))
    return is_market_job or has_metrics


def _source_pack_detail(source_pack: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "official_source_count": source_pack.get("official_source_count", 0),
        "market_data_source_count": source_pack.get("market_data_source_count", 0),
        "confirmed_metric_count": len(source_pack.get("confirmed_metrics", []) or []),
        "missing_source_count": source_pack.get("missing_source_count", 0),
        "quality_score": source_pack.get("quality_score", 0.0),
        "reasons": list(source_pack.get("reasons", []) or []),
    }


def _evaluate_source_pack_with_env(raw: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    return evaluate_source_pack_dict(
        raw,
        min_official_sources=_int_env(env, "AUTOBLOG_SOURCE_PACK_MIN_OFFICIAL", default=1, minimum=0),
        min_market_data_sources=_int_env(env, "AUTOBLOG_SOURCE_PACK_MIN_MARKET", default=2, minimum=0),
        min_confirmed_metrics=_int_env(env, "AUTOBLOG_SOURCE_PACK_MIN_METRICS", default=3, minimum=0),
        max_missing_sources=_int_env(env, "AUTOBLOG_SOURCE_PACK_MAX_MISSING", default=3, minimum=0),
    )


def _resolve_naver_session_file(*, job: Any, publisher: Any) -> Path | None:
    platform = str(getattr(job, "platform", "") or "").strip().lower()
    if platform != "naver":
        return None

    for candidate in _publisher_candidates(publisher):
        path_getter = getattr(candidate, "_session_state_path", None)
        if callable(path_getter):
            try:
                resolved = path_getter()
                if resolved:
                    return Path(resolved)
            except Exception:
                pass
        session_dir = getattr(candidate, "session_dir", None)
        if session_dir:
            return Path(session_dir) / "state.json"
    return None


def _publisher_candidates(publisher: Any) -> list[Any]:
    candidates = [publisher]
    inner = getattr(publisher, "_publisher", None)
    if inner is not None and inner is not publisher:
        candidates.append(inner)
    return candidates


def _pattern_hits(text: str, patterns: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for pattern, label in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        snippet = text[max(0, match.start() - 20) : min(len(text), match.end() + 20)]
        hits.append({"pattern": pattern, "label": label, "snippet": snippet.strip()})
    return hits


def _env_enabled(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = str(env.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _int_env(env: Mapping[str, str], name: str, *, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(str(env.get(name, default)).strip()))
    except (TypeError, ValueError):
        return default


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
