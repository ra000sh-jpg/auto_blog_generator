"""정부기관 매크로 문서 수집기."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any, Dict, List, Mapping, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .document_parser import MacroDocumentParser
from .models import MacroDocumentCandidate, MacroSourceConfig
from .source_config import get_macro_source_config

try:
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - optional dependency
    BeautifulSoup = None

logger = logging.getLogger(__name__)


class MacroTextFetcher(Protocol):
    """텍스트/bytes HTTP fetcher 프로토콜."""

    def get_text(self, url: str, *, headers: Mapping[str, str] | None = None, timeout_sec: float = 10.0) -> str:
        """텍스트 응답을 반환한다."""

    def get_bytes(self, url: str, *, headers: Mapping[str, str] | None = None, timeout_sec: float = 15.0) -> bytes:
        """bytes 응답을 반환한다."""


class MacroBrowserDownloader(Protocol):
    """브라우저 기반 파일 다운로드 프로토콜."""

    def download(self, *, detail_url: str, file_url: str, timeout_sec: float = 20.0) -> bytes:
        """브라우저로 파일을 내려받아 bytes를 반환한다."""


class HttpxMacroFetcher:
    """httpx 기반 기본 fetcher."""

    def __init__(self, timeout_sec: float = 10.0) -> None:
        self.timeout_sec = float(timeout_sec or 10.0)
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
        }

    def get_text(self, url: str, *, headers: Mapping[str, str] | None = None, timeout_sec: float = 10.0) -> str:
        with httpx.Client(timeout=timeout_sec or self.timeout_sec, follow_redirects=True) as client:
            response = client.get(url, headers={**self.headers, **dict(headers or {})})
            response.raise_for_status()
            return response.text

    def get_bytes(self, url: str, *, headers: Mapping[str, str] | None = None, timeout_sec: float = 15.0) -> bytes:
        with httpx.Client(timeout=timeout_sec or self.timeout_sec, follow_redirects=True) as client:
            response = client.get(url, headers={**self.headers, **dict(headers or {})})
            response.raise_for_status()
            return response.content


class MacroDataCollector:
    """기관 사이트에서 신규 매크로 문서 후보를 찾는다."""

    def __init__(
        self,
        *,
        fetcher: MacroTextFetcher | None = None,
        parser: MacroDocumentParser | None = None,
        browser_downloader: MacroBrowserDownloader | None = None,
        timeout_sec: float = 10.0,
    ) -> None:
        self.fetcher = fetcher or HttpxMacroFetcher(timeout_sec=timeout_sec)
        self.parser = parser or MacroDocumentParser()
        self.browser_downloader = browser_downloader
        self.timeout_sec = float(timeout_sec or 10.0)

    def check_latest_sources(self, source: str = "MOTIE", *, limit: int = 10) -> List[MacroDocumentCandidate]:
        """기관별 최신 문서 후보를 반환한다."""
        config = get_macro_source_config(source)
        html = self.fetcher.get_text(config.list_url, timeout_sec=self.timeout_sec)
        return self.detect_new_documents(html, config=config, limit=limit)

    def detect_new_documents(
        self,
        html: str,
        *,
        config: MacroSourceConfig,
        limit: int = 10,
    ) -> List[MacroDocumentCandidate]:
        """목록 HTML에서 키워드에 맞는 문서를 추출한다."""
        table_candidates = self._extract_table_document_candidates(html, config=config, limit=limit)
        if table_candidates:
            return table_candidates

        links = self._extract_links(html, base_url=config.base_url)
        candidates: List[MacroDocumentCandidate] = []
        seen_urls: set[str] = set()
        for item in links:
            title = item["title"]
            url = item["url"]
            if not title or not url or url in seen_urls:
                continue
            if not self._matches_keywords(title, config.keywords):
                continue
            seen_urls.add(url)
            detail = self._fetch_detail_metadata(url, config=config)
            resolved_title = self._normalize_detail_title(detail.get("title", ""), fallback=title)
            published_at = detail.get("published_at", "") or self._extract_date(title)
            attachments = self._normalize_attachments(detail.get("attachments", []))
            file_url = detail.get("file_url", "")
            file_type = detail.get("file_type", "html") or "html"
            candidates.append(
                MacroDocumentCandidate(
                    source=config.source,
                    title=resolved_title,
                    url=url,
                    published_at=published_at,
                    file_url=file_url,
                    file_type=file_type,
                    attachments=tuple(attachments),
                    status="new",
                    hash=self._build_hash(config.source, resolved_title, url),
                )
            )
            if len(candidates) >= max(1, limit):
                break
        return candidates

    def _extract_table_document_candidates(
        self,
        html: str,
        *,
        config: MacroSourceConfig,
        limit: int,
    ) -> List[MacroDocumentCandidate]:
        """산업부 목록 표처럼 제목/날짜/첨부가 한 행에 있는 구조를 우선 처리한다."""
        if BeautifulSoup is None:
            return []
        soup = BeautifulSoup(str(html or ""), "html.parser")
        rows = soup.select("table tr")
        candidates: List[MacroDocumentCandidate] = []
        for row in rows:
            title_link = None
            for link in row.find_all("a"):
                href = str(link.get("href", "") or "")
                link_text = self.parser.normalize_text(link.get_text(" ", strip=True))
                if "article.view" in href or self._matches_keywords(link_text, config.keywords):
                    title_link = link
                    break
            if title_link is None:
                continue
            title = self.parser.normalize_text(title_link.get_text(" ", strip=True))
            if not title or not self._matches_keywords(title, config.keywords):
                continue

            article_id = self._extract_article_id(str(title_link.get("href", "") or ""))
            source_url = self._build_article_detail_url(config.list_url, article_id) if article_id else config.list_url
            row_text = self.parser.normalize_text(row.get_text(" ", strip=True))
            file_info = self._extract_row_file(row, base_url=config.base_url)
            detail = self._fetch_detail_metadata(source_url, config=config) if article_id else {}
            resolved_title = self._normalize_detail_title(detail.get("title", ""), fallback=title)
            published_at = detail.get("published_at", "") or self._extract_date(row_text)
            attachments = self._normalize_attachments(detail.get("attachments", []))
            if not attachments and file_info.get("url"):
                attachments = [self._build_attachment(file_info.get("url", ""), file_info.get("file_type", "html"))]
            file_url = detail.get("file_url", "") or file_info.get("url", "")
            file_type = detail.get("file_type", "") or file_info.get("file_type", "html") or "html"
            candidates.append(
                MacroDocumentCandidate(
                    source=config.source,
                    title=resolved_title,
                    url=source_url,
                    published_at=published_at,
                    file_url=file_url,
                    file_type=file_type,
                    attachments=tuple(attachments),
                    status="new",
                    hash=self._build_hash(config.source, resolved_title, source_url),
                )
            )
            if len(candidates) >= max(1, limit):
                break
        return candidates

    def _extract_row_file(self, row: object, *, base_url: str) -> Dict[str, str]:
        """목록 행 안의 첨부파일 링크를 추출한다."""
        if not hasattr(row, "find_all"):
            return {}
        for link in row.find_all("a"):  # type: ignore[attr-defined]
            href = str(link.get("href", "") or "").strip()
            if not href or "article.view" in href:
                continue
            alt_text = " ".join(str(img.get("alt", "") or "") for img in link.find_all("img"))
            link_text = self.parser.normalize_text(f"{link.get_text(' ', strip=True)} {alt_text} {href}")
            file_type = self._guess_file_type(link_text)
            if not file_type and "/attach/" in href:
                # 산업부 첨부 링크는 확장자가 없고 아이콘 alt에 파일 형식이 들어간다.
                file_type = "pdf"
            if file_type:
                return {"url": urljoin(base_url, href), "file_type": file_type}
        return {}

    def _extract_article_id(self, href: str) -> str:
        match = re.search(r"article\.view\(['\"]?(\d+)['\"]?\)", str(href or ""))
        return match.group(1) if match else ""

    def _build_article_detail_url(self, list_url: str, article_id: str) -> str:
        """목록 URL과 게시글 번호로 산업부 상세 URL을 만든다."""
        parts = urlsplit(str(list_url or ""))
        path = parts.path.rstrip("/")
        if not path or not article_id:
            return str(list_url or "")
        return urlunsplit((parts.scheme, parts.netloc, f"{path}/{article_id}/view", "", ""))

    def _normalize_detail_title(self, title: str, *, fallback: str = "") -> str:
        """상세 페이지 title 태그에 섞인 메뉴 경로를 제거한다."""
        text = self.parser.normalize_text(str(title or ""))
        if "<" in text:
            text = text.split("<", 1)[0].strip()
        if " 보도·참고자료 " in text:
            text = text.split(" 보도·참고자료 ", 1)[0].strip()
        if text and len(text) <= 80:
            return text
        return self.parser.normalize_text(str(fallback or "")) or text

    def download_document_text(self, document: Mapping[str, str]) -> Dict[str, object]:
        """문서 원문 또는 첨부파일에서 본문을 추출한다."""
        url = str(document.get("url", "") or "").strip()
        targets = self._build_extraction_targets(document)
        attempts: List[Dict[str, object]] = []
        try:
            for target in targets:
                target_url = str(target.get("url", "") or "").strip()
                file_type = str(target.get("file_type", "") or "html").strip().lower()
                if file_type == "html":
                    parsed = self._parse_html_target(target_url)
                else:
                    parsed = self._parse_binary_target(target_url, file_type, detail_url=str(document.get("url", "") or ""))
                text = str(parsed.get("text", "") or "")
                tables = parsed.get("tables", []) or []
                attempt = {
                    "url": target_url,
                    "file_type": file_type,
                    "parser": parsed.get("parser", ""),
                    "text_length": len(text),
                    "table_count": len(tables),
                    "error": parsed.get("error", ""),
                    "status": "parsed" if text or tables else "failed",
                }
                attempts.append(attempt)
                if text or tables:
                    parsed["source_url"] = target_url
                    parsed["source_file_type"] = file_type
                    parsed["attempts"] = attempts
                    return {
                        "status": "parsed",
                        "text": text,
                        "parsed_json": parsed,
                        "error_message": "",
                    }

            return {
                "status": "failed",
                "text": "",
                "parsed_json": {"parser": "extraction_chain_failed", "attempts": attempts},
                "error_message": self._summarize_attempt_errors(attempts),
            }
        except Exception as exc:
            return {
                "status": "failed",
                "text": "",
                "parsed_json": {"parser": "extraction_chain_exception", "attempts": attempts},
                "error_message": str(exc),
            }

    def _build_extraction_targets(self, document: Mapping[str, object]) -> List[Dict[str, str]]:
        """대표 파일과 전체 첨부, 상세 HTML을 중복 없이 추출 후보로 만든다."""
        attachments = document.get("attachments_json", [])
        targets: List[Dict[str, str]] = []
        if isinstance(attachments, list):
            for item in attachments:
                if not isinstance(item, dict):
                    continue
                targets.append(
                    {
                        "url": str(item.get("url", "") or "").strip(),
                        "file_type": str(item.get("file_type", "") or "").strip().lower(),
                    }
                )
        file_url = str(document.get("file_url", "") or "").strip()
        file_type = str(document.get("file_type", "") or "").strip().lower()
        if file_url and file_type:
            targets.insert(0, {"url": file_url, "file_type": file_type})
        url = str(document.get("url", "") or "").strip()
        if url:
            targets.append({"url": url, "file_type": "html"})

        priority = {"pdf": 0, "hwpx": 1, "hwp": 2, "html": 3}
        deduped: List[Dict[str, str]] = []
        seen: set[str] = set()
        for item in sorted(targets, key=lambda item: priority.get(item.get("file_type", ""), 9)):
            target_url = str(item.get("url", "") or "").strip()
            file_type = str(item.get("file_type", "") or "").strip().lower()
            if not target_url or not file_type or target_url in seen:
                continue
            seen.add(target_url)
            deduped.append({"url": target_url, "file_type": file_type})
        return deduped

    def _parse_html_target(self, url: str) -> Dict[str, object]:
        html = self.fetcher.get_text(url, timeout_sec=self.timeout_sec)
        return self.parser.parse_html(html)

    def _parse_binary_target(self, url: str, file_type: str, *, detail_url: str = "") -> Dict[str, object]:
        try:
            data = self.fetcher.get_bytes(url, timeout_sec=max(15.0, self.timeout_sec))
        except Exception as exc:
            try:
                data = self._download_with_browser(detail_url=detail_url, file_url=url)
            except Exception as browser_exc:
                return {
                    "title": "",
                    "text": "",
                    "tables": [],
                    "parser": f"{file_type}_download_failed",
                    "error": f"http={exc}; browser={browser_exc}",
                }
        if file_type == "pdf":
            return self.parser.parse_pdf_bytes(data)
        if file_type == "hwpx":
            return self.parser.parse_hwpx_bytes(data)
        if file_type == "hwp":
            return self.parser.parse_hwp_bytes(data)
        return {"title": "", "text": "", "tables": [], "parser": f"unsupported_{file_type}", "error": "Unsupported file type"}

    def _download_with_browser(self, *, detail_url: str, file_url: str) -> bytes:
        downloader = self.browser_downloader
        if downloader is None and os.environ.get("MACRO_ENABLE_PLAYWRIGHT_DOWNLOAD", "").strip().lower() in {"1", "true", "yes"}:
            from .browser_downloader import PlaywrightAttachmentDownloader

            downloader = PlaywrightAttachmentDownloader()
        if downloader is None:
            raise RuntimeError("browser downloader is disabled")
        return downloader.download(detail_url=detail_url, file_url=file_url, timeout_sec=max(20.0, self.timeout_sec))

    def _summarize_attempt_errors(self, attempts: List[Dict[str, object]]) -> str:
        errors = [
            f"{item.get('file_type', '-')}/{item.get('parser', '-')}: {item.get('error')}"
            for item in attempts
            if item.get("error")
        ]
        return " | ".join(errors[-5:]) or "No text extracted"

    def _extract_links(self, html: str, *, base_url: str) -> List[Dict[str, str]]:
        if BeautifulSoup is None:
            return self._extract_links_regex(html, base_url=base_url)
        soup = BeautifulSoup(str(html or ""), "html.parser")
        links: List[Dict[str, str]] = []
        for node in soup.find_all("a"):
            href = str(node.get("href", "") or "").strip()
            title = self.parser.normalize_text(node.get_text(" ", strip=True))
            if not href or not title:
                continue
            if href.startswith("#") or href.lower().startswith("javascript:"):
                continue
            links.append({"title": title, "url": urljoin(base_url, href)})
        return links

    def _extract_links_regex(self, html: str, *, base_url: str) -> List[Dict[str, str]]:
        links: List[Dict[str, str]] = []
        pattern = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
        for href, raw_title in pattern.findall(str(html or "")):
            title = self.parser.normalize_text(re.sub(r"<[^>]+>", " ", raw_title))
            if title and href and not href.startswith("#"):
                links.append({"title": title, "url": urljoin(base_url, href)})
        return links

    def _fetch_detail_metadata(self, url: str, *, config: MacroSourceConfig) -> Dict[str, Any]:
        try:
            html = self.fetcher.get_text(url, timeout_sec=self.timeout_sec)
        except Exception as exc:
            logger.debug("Macro detail fetch failed (%s): %s", url, exc)
            return {}
        parsed = self.parser.parse_html(html)
        text = str(parsed.get("text", "") or "")
        title = str(parsed.get("title", "") or "")
        file_links = self._extract_file_links(html, base_url=config.base_url)
        selected = self._select_file(file_links)
        return {
            "title": title,
            "published_at": self._extract_date(text) or self._extract_date(title),
            "file_url": selected.get("url", ""),
            "file_type": selected.get("file_type", "html"),
            "attachments": file_links,
        }

    def _extract_file_links(self, html: str, *, base_url: str) -> List[Dict[str, str]]:
        links = self._extract_links(html, base_url=base_url)
        if BeautifulSoup is not None:
            soup = BeautifulSoup(str(html or ""), "html.parser")
            for node in soup.find_all("a"):
                href = str(node.get("href", "") or "").strip()
                url = self._resolve_href(href, base_url=base_url)
                if not url:
                    continue
                alt_text = " ".join(str(img.get("alt", "") or "") for img in node.find_all("img"))
                title = self.parser.normalize_text(f"{node.get_text(' ', strip=True)} {alt_text}")
                links.append({"title": title, "url": url})
        output: List[Dict[str, str]] = []
        seen: set[str] = set()
        for item in links:
            url = item["url"]
            title = item["title"]
            file_type = self._guess_file_type(f"{url} {title}")
            if file_type in {"pdf", "hwp", "hwpx"} and url not in seen:
                seen.add(url)
                output.append(self._build_attachment(url, file_type, title=title))
        return output

    def _build_attachment(self, url: str, file_type: str, *, title: str = "") -> Dict[str, str]:
        """첨부파일 메타데이터를 작고 일관된 dict로 만든다."""
        normalized_url = str(url or "").strip()
        normalized_type = str(file_type or "html").strip().lower()
        return {
            "title": self.parser.normalize_text(title),
            "url": normalized_url,
            "file_type": normalized_type,
            "source": "detail",
        }

    def _normalize_attachments(self, value: Any) -> List[Dict[str, str]]:
        """외부 입력에서 URL과 파일 형식이 있는 첨부만 남긴다."""
        output: List[Dict[str, str]] = []
        seen: set[str] = set()
        if not isinstance(value, list):
            return output
        for item in value:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "") or "").strip()
            file_type = str(item.get("file_type", "") or "").strip().lower()
            if not url or not file_type or url in seen:
                continue
            seen.add(url)
            output.append(
                {
                    "title": self.parser.normalize_text(str(item.get("title", "") or "")),
                    "url": url,
                    "file_type": file_type,
                    "source": str(item.get("source", "") or "detail"),
                }
            )
        return output

    def _resolve_href(self, href: str, *, base_url: str) -> str:
        """일반 href와 javascript location.href 값을 실제 URL로 정규화한다."""
        value = str(href or "").strip()
        if not value or value.startswith("#"):
            return ""
        location_match = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", value)
        if location_match:
            value = location_match.group(1)
        elif value.lower().startswith("javascript:"):
            return ""
        return urljoin(base_url, value)

    def _select_file(self, links: List[Dict[str, str]]) -> Dict[str, str]:
        for preferred in ("pdf", "hwpx", "hwp"):
            for item in links:
                if item.get("file_type") == preferred:
                    return item
        return {}

    def _guess_file_type(self, value: str) -> str:
        normalized = str(value or "").lower()
        if ".pdf" in normalized or "pdf" in normalized:
            return "pdf"
        if ".hwpx" in normalized or "hwpx" in normalized:
            return "hwpx"
        if ".hwp" in normalized or "hwp" in normalized:
            return "hwp"
        if ".html" in normalized or ".htm" in normalized:
            return "html"
        return ""

    def _matches_keywords(self, value: str, keywords: tuple[str, ...]) -> bool:
        haystack = re.sub(r"\s+", "", str(value or "").lower())
        for keyword in keywords:
            needle = re.sub(r"\s+", "", str(keyword or "").lower())
            if needle and needle in haystack:
                return True
        return False

    def _extract_date(self, value: str) -> str:
        text = str(value or "")
        match = re.search(r"(20\d{2})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})", text)
        if match:
            year, month, day = match.groups()
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        match = re.search(r"(20\d{2})[.\-/년]\s*(\d{1,2})", text)
        if match:
            year, month = match.groups()
            return f"{int(year):04d}-{int(month):02d}-01"
        return ""

    def _build_hash(self, source: str, title: str, url: str) -> str:
        payload = f"{source.strip().upper()}|{title.strip()}|{url.strip()}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
