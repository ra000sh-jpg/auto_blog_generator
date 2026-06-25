"""빅카인즈 공개 화면 기반 방향성 이슈 수집기."""

from __future__ import annotations

import html
import json
import logging
import re
import ssl
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

logger = logging.getLogger(__name__)


BIGKINDS_WEEKLY_ISSUE_URL = "https://www.kinds.or.kr/v2/news/weekendNews.do"
BIGKINDS_SERVICE_GUIDE_URL = "https://www.kinds.or.kr/v2/intro/service.do"
DEFAULT_CACHE_TTL_HOURS = 6
DEFAULT_MAX_ISSUES = 8


@dataclass(frozen=True)
class BigKindsIssue:
    """방향성 주제 선정에 사용할 빅카인즈 공개 이슈."""

    issue_title: str
    category: str = ""
    news_count: int | None = None
    keywords: tuple[str, ...] = ()
    source_url: str = BIGKINDS_WEEKLY_ISSUE_URL
    collected_at: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON 저장 가능한 dict로 변환한다."""

        payload: dict[str, Any] = {
            "issue_title": self.issue_title,
            "category": self.category,
            "keywords": list(self.keywords),
            "source_url": self.source_url,
            "collected_at": self.collected_at,
            "confidence": self.confidence,
        }
        if self.news_count is not None:
            payload["news_count"] = self.news_count
        return payload


class BigKindsTextFetcher(Protocol):
    """텍스트 HTTP fetcher 프로토콜."""

    def get_text(self, url: str, *, timeout_sec: float = 8.0) -> str:
        """URL의 텍스트 응답을 반환한다."""


class UrllibBigKindsFetcher:
    """추가 의존성 없이 동작하는 빅카인즈 공개 화면 fetcher."""

    def get_text(self, url: str, *, timeout_sec: float = 8.0) -> str:
        """URL에서 텍스트를 읽는다."""

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        ssl_context = _build_ssl_context()
        if ssl_context is None:
            response_cm = urllib.request.urlopen(request, timeout=timeout_sec)  # nosec B310
        else:
            response_cm = urllib.request.urlopen(request, timeout=timeout_sec, context=ssl_context)  # nosec B310
        with response_cm as response:
            raw = response.read(1_000_000)
            encoding = response.headers.get_content_charset() or "utf-8"
            return raw.decode(encoding, errors="replace")


class BigKindsPublicCollector:
    """빅카인즈 공개 화면에서 기사 본문 없이 방향성 후보만 수집한다."""

    def __init__(
        self,
        *,
        fetcher: BigKindsTextFetcher | None = None,
        cache_path: str | Path | None = None,
        timeout_sec: float = 8.0,
        cache_ttl_hours: int = DEFAULT_CACHE_TTL_HOURS,
    ) -> None:
        self.fetcher = fetcher or UrllibBigKindsFetcher()
        self.timeout_sec = timeout_sec
        self.cache_ttl_hours = max(1, int(cache_ttl_hours or DEFAULT_CACHE_TTL_HOURS))
        self.cache_path = Path(cache_path) if cache_path else Path("data/cache/bigkinds_public_issues.json")

    def collect_directional_issues(
        self,
        *,
        max_items: int = DEFAULT_MAX_ISSUES,
        now: datetime | None = None,
        use_cache: bool = True,
    ) -> list[BigKindsIssue]:
        """공개 화면에서 방향성 이슈 후보를 수집한다."""

        current = _ensure_aware_utc(now or datetime.now(timezone.utc))
        if use_cache:
            cached = self._load_cache(now=current)
            if cached:
                return cached[: max(1, int(max_items))]

        try:
            text = self.fetcher.get_text(BIGKINDS_WEEKLY_ISSUE_URL, timeout_sec=self.timeout_sec)
        except Exception as exc:
            logger.debug("BigKinds public page fetch failed: %s", exc)
            return []

        issues = parse_bigkinds_public_issues(
            text,
            source_url=BIGKINDS_WEEKLY_ISSUE_URL,
            collected_at=_iso_utc(current),
            max_items=max_items,
        )
        if issues:
            self._save_cache(issues)
        return issues

    def _load_cache(self, *, now: datetime) -> list[BigKindsIssue]:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        collected_at = _parse_iso_utc(str(payload.get("collected_at", "") if isinstance(payload, Mapping) else ""))
        if collected_at is None:
            return []
        if now - collected_at > timedelta(hours=self.cache_ttl_hours):
            return []
        raw_items = payload.get("issues", []) if isinstance(payload, Mapping) else []
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes, bytearray)):
            return []
        return [issue for issue in (_issue_from_dict(item) for item in raw_items) if issue is not None]

    def _save_cache(self, issues: Sequence[BigKindsIssue]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            collected_at = issues[0].collected_at if issues else _iso_utc(datetime.now(timezone.utc))
            payload = {
                "collected_at": collected_at,
                "source": "BigKinds public page",
                "issues": [issue.to_dict() for issue in issues],
            }
            self.cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug("BigKinds cache save skipped: %s", exc)


def parse_bigkinds_public_issues(
    text: str,
    *,
    source_url: str = BIGKINDS_WEEKLY_ISSUE_URL,
    collected_at: str = "",
    max_items: int = DEFAULT_MAX_ISSUES,
) -> list[BigKindsIssue]:
    """빅카인즈 공개 HTML/JSON 조각에서 이슈 후보를 추출한다."""

    raw_text = str(text or "")
    collected = collected_at or _iso_utc(datetime.now(timezone.utc))
    issues = _issues_from_json_fragments(raw_text, source_url=source_url, collected_at=collected)
    if not issues:
        issues = _issues_from_visible_text(raw_text, source_url=source_url, collected_at=collected)
    return _dedupe_issues(issues)[: max(1, int(max_items or DEFAULT_MAX_ISSUES))]


def _issues_from_json_fragments(
    text: str,
    *,
    source_url: str,
    collected_at: str,
) -> list[BigKindsIssue]:
    issues: list[BigKindsIssue] = []
    for match in re.finditer(r"\{[^{}]{0,1200}?(?:issue|title|keyword|news)[^{}]{0,1200}?\}", text, re.IGNORECASE):
        raw = html.unescape(match.group(0))
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if not isinstance(payload, Mapping):
            continue
        issue = _issue_from_mapping(payload, source_url=source_url, collected_at=collected_at)
        if issue is not None:
            issues.append(issue)
    return issues


def _issue_from_mapping(
    payload: Mapping[str, Any],
    *,
    source_url: str,
    collected_at: str,
) -> BigKindsIssue | None:
    title = _first_text(
        payload,
        (
            "issue_title",
            "issueTitle",
            "issue",
            "title",
            "clusterTitle",
            "keyword",
            "name",
        ),
    )
    title = _clean_issue_title(title)
    if not _is_usable_issue_title(title):
        return None
    category = _first_text(payload, ("category", "categoryName", "section", "sectionName", "cat"))
    count = _parse_int(_first_text(payload, ("news_count", "newsCount", "articleCount", "count", "cnt")))
    raw_keywords = _first_text(payload, ("keywords", "keywordList", "relatedKeywords", "entities"))
    keywords = _split_keywords(raw_keywords) or tuple(_extract_keywords_from_title(title))
    return BigKindsIssue(
        issue_title=title,
        category=_clean_text(category),
        news_count=count,
        keywords=keywords,
        source_url=source_url,
        collected_at=collected_at,
        confidence=0.78,
    )


def _issues_from_visible_text(
    text: str,
    *,
    source_url: str,
    collected_at: str,
) -> list[BigKindsIssue]:
    plain_text = _html_to_text(text)
    normalized = re.sub(r"\s+", " ", plain_text)
    pattern = re.compile(
        r"(?:^|\s)(?:\d{1,2}[.)]\s*)?"
        r"(?P<title>[가-힣A-Za-z0-9][가-힣A-Za-z0-9·ㆍ/()&%+\- ]{3,60})"
        r"(?:\s+(?P<count>\d{1,5})\s*(?:건|개))?"
    )
    issues: list[BigKindsIssue] = []
    blocked_terms = {
        "뉴스 데이터를 검색하고 있습니다",
        "빠른메뉴 설정",
        "데이터 다운로드",
        "비회원은 최근",
        "서비스 안내",
        "로그인",
        "회원가입",
    }
    for match in pattern.finditer(normalized):
        title = _clean_issue_title(match.group("title"))
        if any(term in title for term in blocked_terms):
            continue
        if not _is_usable_issue_title(title):
            continue
        issues.append(
            BigKindsIssue(
                issue_title=title,
                category=_infer_category(title),
                news_count=_parse_int(match.group("count")),
                keywords=tuple(_extract_keywords_from_title(title)),
                source_url=source_url,
                collected_at=collected_at,
                confidence=0.48,
            )
        )
    return issues


def _issue_from_dict(raw: Any) -> BigKindsIssue | None:
    if not isinstance(raw, Mapping):
        return None
    title = _clean_issue_title(str(raw.get("issue_title", "") or ""))
    if not _is_usable_issue_title(title):
        return None
    keywords_raw = raw.get("keywords", ())
    if isinstance(keywords_raw, Sequence) and not isinstance(keywords_raw, (str, bytes, bytearray)):
        keywords = tuple(_clean_text(str(item)) for item in keywords_raw if _clean_text(str(item)))
    else:
        keywords = _split_keywords(str(keywords_raw or ""))
    return BigKindsIssue(
        issue_title=title,
        category=_clean_text(str(raw.get("category", "") or "")),
        news_count=_parse_int(raw.get("news_count")),
        keywords=keywords,
        source_url=_clean_text(str(raw.get("source_url", "") or BIGKINDS_WEEKLY_ISSUE_URL)),
        collected_at=_clean_text(str(raw.get("collected_at", "") or "")),
        confidence=float(raw.get("confidence", 0.0) or 0.0),
    )


def _dedupe_issues(issues: Sequence[BigKindsIssue]) -> list[BigKindsIssue]:
    deduped: list[BigKindsIssue] = []
    seen: set[str] = set()
    for issue in issues:
        key = re.sub(r"\W+", "", issue.issue_title.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    return deduped


def _first_text(payload: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return ", ".join(str(item) for item in value if str(item).strip())
        text = str(value).strip()
        if text:
            return text
    return ""


def _split_keywords(value: str) -> tuple[str, ...]:
    parts = re.split(r"[,;/|·ㆍ]+|\s{2,}", str(value or ""))
    return tuple(dict.fromkeys(_clean_text(part) for part in parts if _clean_text(part)))[:8]


def _extract_keywords_from_title(title: str) -> list[str]:
    tokens = re.findall(r"[가-힣A-Za-z0-9]{2,}", title)
    blocked = {"오늘", "주간", "이슈", "뉴스", "관련", "기준", "브리핑"}
    return [token for token in dict.fromkeys(tokens) if token not in blocked][:6]


def _infer_category(title: str) -> str:
    text = title.lower()
    if any(token in text for token in ("ai", "반도체", "삼성", "하이닉스", "배터리", "전력", "데이터센터")):
        return "경제/IT"
    if any(token in text for token in ("중국", "일본", "미국", "유럽", "중동")):
        return "국제"
    if any(token in text for token in ("금리", "환율", "증시", "코스피", "나스닥", "수출")):
        return "경제"
    return ""


def _is_usable_issue_title(title: str) -> bool:
    if len(title) < 4 or len(title) > 70:
        return False
    if re.fullmatch(r"[\d\s.,:/-]+", title):
        return False
    blocked = (
        "메뉴",
        "본문으로",
        "취소 확인",
        "비밀번호",
        "인증메일",
        "개인정보",
        "저작권",
        "copyright",
    )
    return not any(token in title.lower() for token in blocked)


def _clean_issue_title(value: str) -> str:
    text = _clean_text(value)
    text = re.sub(r"^(전체|정치|경제|사회|문화|국제|지역|스포츠|IT과학)\s+", "", text)
    text = re.sub(r"\s*(?:뉴스\s*)?(?:\d{1,5})\s*(?:건|개)$", "", text)
    return text.strip(" -·ㆍ|")


def _html_to_text(value: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return _clean_text(html.unescape(text))


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _parse_int(value: Any) -> int | None:
    text = re.sub(r"[^\d]", "", str(value or ""))
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return _ensure_aware_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_utc(value: str) -> datetime | None:
    try:
        return datetime.strptime(str(value or "").strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _build_ssl_context() -> ssl.SSLContext | None:
    try:
        import certifi  # type: ignore[import-not-found]

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None

