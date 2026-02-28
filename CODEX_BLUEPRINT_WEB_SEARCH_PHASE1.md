# CODEX BLUEPRINT — Phase 1: Web Search + Web Fetch 통합

> **목표**: 모든 토픽(cafe, parenting, IT, finance…)에 실시간 웹 검색 리서치 컨텍스트를 주입하여 글 품질을 개선한다.
> 현재 economy 토픽만 RSS 2개로 컨텍스트를 받는 제한을 제거한다.
>
> **변경 파일**: 5개 수정 + 5개 신규
> - `modules/config.py` (WebSearchConfig 추가)
> - `config/default.yaml` (web_search 섹션 추가)
> - `server/dependencies.py` (DI 팩토리 추가)
> - `modules/llm/__init__.py` (_build_generator에 web 클라이언트 주입)
> - `modules/llm/content_generator.py` (핵심 변경: _collect_web_context + 전 토픽 확장)
> - `modules/web_search/__init__.py` (NEW)
> - `modules/web_search/base_client.py` (NEW)
> - `modules/web_search/brave_client.py` (NEW)
> - `modules/web_search/web_fetch_client.py` (NEW)
> - `modules/web_search/provider_factory.py` (NEW)
>
> **변경하지 않는 파일**:
> - `modules/rag/search_engine.py` — 기존 RSS+CrossEncoder 파이프라인 유지 (async 브릿징 불필요)
> - `modules/automation/pipeline_service.py` — `llm_generate_fn()` 경유로 자동 적용
> - `modules/llm/prompts.py` — 기존 `[NewsData]` 주입 코드가 비경제 토픽에도 이미 동작
> - `requirements.txt` — httpx, beautifulsoup4 이미 존재
> - 프론트엔드 — 변경 없음

---

## Context

현재 `ContentGenerator.generate()` 리서치 경로 (`modules/llm/content_generator.py` line 198-203):
```python
news_context: List[Dict[str, str]] = []
if self._is_economy_topic(topic_mode):
    news_context = self._collect_news_context(job.seed_keywords, max_items=3)
elif is_idea_vault_job:
    idea_query = [job.title] + list(job.seed_keywords)
    news_context = self._collect_news_context(idea_query, max_items=1)
# → 그 외 토픽: news_context = [] (빈 배열)
```

문제:
1. **비경제 토픽(cafe, parenting, IT 등)은 외부 컨텍스트 ZERO** → LLM이 자체 지식만으로 글 생성
2. RSS 소스가 MK, 한경 **2개뿐** → 경제 외 분야는 관련 기사 자체가 없음
3. 팩트 체크 없이 생성 → 날조된 수치/인용 위험

해결:
- Brave Search API(무료 2,000쿼리/월)로 **모든 토픽에 웹 검색 컨텍스트** 주입
- URL 본문 추출로 snippet보다 풍부한 컨텍스트 확보
- 기존 RSS 파이프라인은 그대로 유지 (economy 토픽의 주 소스)

---

## PATCH 1 — WebSearchConfig 추가

### 파일: `modules/config.py`

#### 1-A. `WebSearchConfig` 데이터클래스 추가 (SEOConfig 뒤, AppConfig 앞 — line 98 부근)

```python
@dataclass
class WebSearchConfig:
    """웹 검색 및 콘텐츠 추출 설정."""
    enabled: bool = False
    provider: str = "brave"
    api_key: str = ""
    timeout_sec: float = 10.0
    fetch_timeout_sec: float = 15.0
    max_results: int = 5
    fetch_max_chars: int = 3000
```

#### 1-B. `AppConfig`에 필드 추가 (line 107 부근)

현재:
```python
@dataclass
class AppConfig:
    logging: LoggingConfig
    publisher: PublisherConfig
    pipeline: PipelineConfig
    retry: RetryConfig
    llm: LLMConfig
    images: ImageConfig
    seo: SEOConfig = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.seo is None:
            self.seo = SEOConfig()
```

변경:
```python
@dataclass
class AppConfig:
    logging: LoggingConfig
    publisher: PublisherConfig
    pipeline: PipelineConfig
    retry: RetryConfig
    llm: LLMConfig
    images: ImageConfig
    seo: SEOConfig = None  # type: ignore[assignment]
    web_search: WebSearchConfig = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.seo is None:
            self.seo = SEOConfig()
        if self.web_search is None:
            self.web_search = WebSearchConfig()
```

#### 1-C. `load_config()` 수정 (line 123-131)

현재 return문에 `web_search=WebSearchConfig(**merged.get("web_search", {})),` 추가:

```python
    return AppConfig(
        logging=LoggingConfig(**merged.get("logging", {})),
        publisher=PublisherConfig(**merged.get("publisher", {})),
        pipeline=PipelineConfig(**merged.get("pipeline", {})),
        retry=RetryConfig(**merged.get("retry", {})),
        llm=LLMConfig(**merged.get("llm", {})),
        images=ImageConfig(**merged.get("images", {})),
        seo=SEOConfig(**merged.get("seo", {})),
        web_search=WebSearchConfig(**merged.get("web_search", {})),
    )
```

#### 1-D. `_apply_env_overrides()` env_map에 추가 (line 210, SEO 항목 뒤)

```python
        # 웹 검색
        "WEB_SEARCH_ENABLED": ("web_search", "enabled", _parse_bool),
        "WEB_SEARCH_PROVIDER": ("web_search", "provider", str),
        "BRAVE_API_KEY": ("web_search", "api_key", str),
        "WEB_SEARCH_TIMEOUT_SEC": ("web_search", "timeout_sec", float),
        "WEB_SEARCH_FETCH_TIMEOUT_SEC": ("web_search", "fetch_timeout_sec", float),
        "WEB_SEARCH_MAX_RESULTS": ("web_search", "max_results", int),
        "WEB_SEARCH_FETCH_MAX_CHARS": ("web_search", "fetch_max_chars", int),
```

---

## PATCH 2 — default.yaml 설정 추가

### 파일: `config/default.yaml`

파일 맨 끝(line 45 뒤)에 추가:

```yaml

web_search:
  enabled: false
  provider: "brave"
  # api_key: ""  # Set via BRAVE_API_KEY env var
  timeout_sec: 10.0
  fetch_timeout_sec: 15.0
  max_results: 5
  fetch_max_chars: 3000
```

---

## PATCH 3 — web_search 모듈 신규 생성 (5개 파일)

### 3-A. `modules/web_search/__init__.py`

```python
"""웹 검색 및 콘텐츠 추출 모듈."""
```

### 3-B. `modules/web_search/base_client.py`

```python
"""웹 검색/추출 공통 인터페이스."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class SearchResult:
    """단일 웹 검색 결과."""

    title: str
    url: str
    snippet: str
    source: str = ""
    rank: int = 0


class BaseWebSearchClient(ABC):
    """모든 웹 검색 Provider가 구현해야 하는 인터페이스."""

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: int = 5,
    ) -> List[SearchResult]:
        """키워드 검색을 수행하고 결과 목록을 반환한다."""

    @abstractmethod
    async def close(self) -> None:
        """내부 HTTP 클라이언트를 정리한다."""


class BaseWebFetchClient(ABC):
    """URL에서 본문 텍스트를 추출하는 인터페이스."""

    @abstractmethod
    async def fetch_content(
        self,
        url: str,
        max_chars: int = 3000,
    ) -> Optional[Dict[str, str]]:
        """URL의 본문 텍스트를 추출한다.

        Returns:
            Dict with keys: title, url, content (RSS 아이템과 동일 구조)
            None if fetch fails
        """

    @abstractmethod
    async def close(self) -> None:
        """내부 HTTP 클라이언트를 정리한다."""
```

### 3-C. `modules/web_search/brave_client.py`

```python
"""Brave Search API 클라이언트.

Free tier: 2,000 queries/month.
Docs: https://api.search.brave.com/app/documentation/web-search
"""

from __future__ import annotations

import logging
from typing import List

import httpx

from .base_client import BaseWebSearchClient, SearchResult

logger = logging.getLogger(__name__)


class BraveSearchClient(BaseWebSearchClient):
    """Brave Search API v1 웹 검색 클라이언트."""

    BASE_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(
        self,
        api_key: str,
        timeout_sec: float = 10.0,
        country: str = "KR",
        search_lang: str = "ko",
    ):
        if not api_key:
            raise ValueError("Brave API key is required")
        self._api_key = api_key
        self._country = country
        self._search_lang = search_lang
        self._client = httpx.AsyncClient(
            timeout=timeout_sec,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
        )

    async def search(
        self,
        query: str,
        max_results: int = 5,
    ) -> List[SearchResult]:
        """Brave Search API로 웹 검색을 수행한다."""
        if not query.strip():
            return []

        params = {
            "q": query.strip(),
            "count": min(max_results, 20),
            "country": self._country,
            "search_lang": self._search_lang,
            "text_decorations": "false",
        }

        try:
            response = await self._client.get(self.BASE_URL, params=params)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Brave Search HTTP error: %s (status=%d)",
                exc,
                exc.response.status_code,
            )
            return []
        except Exception as exc:
            logger.warning("Brave Search request failed: %s", exc)
            return []

        results: List[SearchResult] = []
        web_results = data.get("web", {}).get("results", [])
        for rank, item in enumerate(web_results[:max_results], start=1):
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", "")).strip()
            snippet = str(item.get("description", "")).strip()
            if not title or not url:
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    source="brave",
                    rank=rank,
                )
            )

        logger.info(
            "Brave search completed",
            extra={"query": query[:50], "result_count": len(results)},
        )
        return results

    async def close(self) -> None:
        """내부 HTTP 클라이언트를 정리한다."""
        await self._client.aclose()
```

### 3-D. `modules/web_search/web_fetch_client.py`

`RssNewsCollector._fetch_article_text()` (rss_news_collector.py line 158-193)와 동일한 BeautifulSoup 파싱 패턴을 async로 재사용.

```python
"""URL 본문 텍스트 추출 클라이언트."""

from __future__ import annotations

import logging
import re
from typing import Dict, Optional

import httpx

logger = logging.getLogger(__name__)

try:
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]
except Exception:  # pragma: no cover
    BeautifulSoup = None


class WebFetchClient:
    """URL에서 본문 텍스트를 추출하는 클라이언트.

    BeautifulSoup 파싱 로직은 RssNewsCollector._fetch_article_text()와
    동일한 패턴을 사용한다.
    """

    def __init__(
        self,
        timeout_sec: float = 15.0,
        max_chars: int = 3000,
    ):
        self._max_chars = max_chars
        self._client = httpx.AsyncClient(
            timeout=timeout_sec,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
            },
        )

    async def fetch_content(
        self,
        url: str,
        max_chars: int = 0,
    ) -> Optional[Dict[str, str]]:
        """URL의 본문 텍스트를 추출한다.

        Returns:
            Dict with keys: title, url, content (RSS 아이템과 동일 구조)
            None if fetch fails
        """
        effective_max = max_chars if max_chars > 0 else self._max_chars
        try:
            response = await self._client.get(url)
            response.raise_for_status()
        except Exception as exc:
            logger.debug("Web fetch failed (%s): %s", url, exc)
            return None

        html = response.text
        if BeautifulSoup is None:
            text = self._strip_tags(html)
            return {
                "title": "",
                "url": url,
                "content": self._truncate(text, effective_max),
            }

        try:
            soup = BeautifulSoup(html, "html.parser")

            for selector in (
                "script", "style", "nav", "footer",
                "header", "aside", "form",
            ):
                for node in soup.select(selector):
                    node.decompose()

            title_tag = soup.find("title")
            page_title = (
                self._clean_text(title_tag.get_text(strip=True))
                if title_tag
                else ""
            )

            article_node = soup.find("article")
            target = article_node if article_node else soup.body
            if target is None:
                return None

            blocks = [
                self._clean_text(node.get_text(" ", strip=True))
                for node in target.find_all(["p", "li"])
            ]
            text = " ".join(item for item in blocks if item)
            if not text:
                text = self._clean_text(target.get_text(" ", strip=True))

            if not text:
                return None

            return {
                "title": page_title,
                "url": url,
                "content": self._truncate(text, effective_max),
            }
        except Exception as exc:
            logger.debug("Web parse failed (%s): %s", url, exc)
            return None

    async def close(self) -> None:
        """내부 HTTP 클라이언트를 정리한다."""
        await self._client.aclose()

    def _strip_tags(self, html: str) -> str:
        return self._clean_text(re.sub(r"<[^>]+>", " ", html))

    def _clean_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def _truncate(self, text: str, max_chars: int) -> str:
        cleaned = self._clean_text(text)
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[:max_chars].rstrip() + "..."
```

### 3-E. `modules/web_search/provider_factory.py`

```python
"""웹 검색 프로바이더 팩토리."""

from __future__ import annotations

import logging
from typing import Optional

from .base_client import BaseWebSearchClient
from .brave_client import BraveSearchClient

logger = logging.getLogger(__name__)


def create_web_search_client(
    provider: str = "brave",
    api_key: str = "",
    timeout_sec: float = 10.0,
) -> Optional[BaseWebSearchClient]:
    """프로바이더에 따른 웹 검색 클라이언트를 생성한다.

    Returns:
        BaseWebSearchClient instance, or None if api_key is missing
    """
    normalized = str(provider).strip().lower()
    if not api_key:
        logger.info("Web search disabled: no API key for provider=%s", normalized)
        return None

    if normalized == "brave":
        return BraveSearchClient(
            api_key=api_key,
            timeout_sec=timeout_sec,
        )

    logger.warning("Unknown web search provider '%s', falling back to brave", normalized)
    return BraveSearchClient(api_key=api_key, timeout_sec=timeout_sec)
```

---

## PATCH 4 — DI 등록

### 파일: `server/dependencies.py`

파일 끝(line 56 뒤)에 추가:

```python
@lru_cache(maxsize=1)
def get_web_search_client():
    """웹 검색 클라이언트를 반환한다 (disabled이면 None)."""
    config = get_app_config()
    ws = config.web_search
    if not ws.enabled:
        return None

    api_key = ws.api_key or os.getenv("BRAVE_API_KEY", "")
    if not api_key:
        return None

    from modules.web_search.provider_factory import create_web_search_client
    return create_web_search_client(
        provider=ws.provider,
        api_key=api_key,
        timeout_sec=ws.timeout_sec,
    )


@lru_cache(maxsize=1)
def get_web_fetch_client():
    """웹 콘텐츠 추출 클라이언트를 반환한다 (disabled이면 None)."""
    config = get_app_config()
    ws = config.web_search
    if not ws.enabled:
        return None

    from modules.web_search.web_fetch_client import WebFetchClient
    return WebFetchClient(
        timeout_sec=ws.fetch_timeout_sec,
        max_chars=ws.fetch_max_chars,
    )
```

---

## PATCH 5 — ContentGenerator에 웹 검색 통합 (핵심 변경)

### 파일: `modules/llm/content_generator.py`

#### 5-A. `__init__` 시그니처에 파라미터 추가 (line 142, `circuit_breaker` 뒤)

현재:
```python
        circuit_breaker: Optional[ProviderCircuitBreaker] = None,
    ):
```

변경:
```python
        circuit_breaker: Optional[ProviderCircuitBreaker] = None,
        web_search_client: Optional[Any] = None,
        web_fetch_client: Optional[Any] = None,
    ):
```

#### 5-B. `__init__` 본문에 저장 (line 174, `self.circuit_breaker = circuit_breaker` 뒤)

추가:
```python
        self.web_search_client = web_search_client
        self.web_fetch_client = web_fetch_client
```

#### 5-C. 리서치 컨텍스트 수집 로직 변경 (line 198-203)

현재:
```python
        news_context: List[Dict[str, str]] = []
        if self._is_economy_topic(topic_mode):
            news_context = self._collect_news_context(job.seed_keywords, max_items=3)
        elif is_idea_vault_job:
            idea_query = [job.title] + list(job.seed_keywords)
            news_context = self._collect_news_context(idea_query, max_items=1)
```

변경:
```python
        # ── 리서치 컨텍스트 수집 (모든 토픽) ──
        news_context: List[Dict[str, str]] = []
        if self._is_economy_topic(topic_mode):
            # Economy: RSS(주) + 웹 검색(보조)
            news_context = self._collect_news_context(job.seed_keywords, max_items=3)
            if not news_context:
                news_context = await self._collect_web_context(
                    job.seed_keywords, max_items=3,
                )
        elif is_idea_vault_job:
            idea_query = [job.title] + list(job.seed_keywords)
            news_context = self._collect_news_context(idea_query, max_items=1)
            if not news_context:
                news_context = await self._collect_web_context(
                    idea_query, max_items=1,
                )
        else:
            # 비경제 토픽: 웹 검색으로 리서치 컨텍스트 확보
            news_context = await self._collect_web_context(
                job.seed_keywords, max_items=2,
            )
```

#### 5-D. `_collect_web_context()` 신규 메서드 추가

`_collect_news_context()` 메서드 바로 아래(line 683 뒤)에 삽입:

```python
    async def _collect_web_context(
        self,
        keywords: List[str],
        max_items: int = 2,
    ) -> List[Dict[str, str]]:
        """웹 검색 기반 외부 컨텍스트를 수집한다.

        반환 형식은 _collect_news_context()와 동일:
        List[Dict] with keys: title, link, content
        """
        if self.web_search_client is None:
            return []

        query = " ".join(
            str(k).strip() for k in keywords if str(k).strip()
        )
        if not query:
            return []

        try:
            search_results = await self.web_search_client.search(
                query,
                max_results=max_items + 2,
            )
        except Exception as exc:
            logger.warning("Web search failed: %s", exc)
            return []

        if not search_results:
            return []

        candidates: List[Dict[str, str]] = []
        for sr in search_results:
            if len(candidates) >= max_items:
                break

            content = sr.snippet
            if self.web_fetch_client is not None:
                try:
                    fetched = await self.web_fetch_client.fetch_content(sr.url)
                    if fetched and isinstance(fetched, dict):
                        fetched_content = str(fetched.get("content", "")).strip()
                        if fetched_content:
                            content = fetched_content
                except Exception as exc:
                    logger.debug("Web fetch failed for %s: %s", sr.url, exc)

            candidates.append({
                "title": sr.title,
                "link": sr.url,
                "content": content,
            })

        if candidates:
            logger.info(
                "Web context collected",
                extra={
                    "query": query[:50],
                    "result_count": len(candidates),
                },
            )
        return candidates
```

---

## PATCH 6 — _build_generator에 웹 클라이언트 주입

### 파일: `modules/llm/__init__.py`

#### 6-A. `_build_generator()` 함수의 return문 직전(line 133, circuit_breaker 로딩 뒤)에 추가:

```python
    # ── 웹 검색 클라이언트 초기화 ──
    web_search_client = None
    web_fetch_client = None
    try:
        from ..config import load_config as _load_web_cfg

        ws_config = _load_web_cfg().web_search
        if ws_config.enabled:
            ws_api_key = ws_config.api_key or os.getenv("BRAVE_API_KEY", "")
            if ws_api_key:
                from ..web_search.provider_factory import create_web_search_client
                from ..web_search.web_fetch_client import WebFetchClient

                web_search_client = create_web_search_client(
                    provider=ws_config.provider,
                    api_key=ws_api_key,
                    timeout_sec=ws_config.timeout_sec,
                )
                web_fetch_client = WebFetchClient(
                    timeout_sec=ws_config.fetch_timeout_sec,
                    max_chars=ws_config.fetch_max_chars,
                )
    except Exception as exc:
        logger.warning("Web search client init failed: %s", exc)
```

#### 6-B. ContentGenerator 생성자 호출에 파라미터 추가 (line 134-153)

현재:
```python
    return ContentGenerator(
        ...
        circuit_breaker=circuit_breaker,
    )
```

변경 (끝에 2줄 추가):
```python
    return ContentGenerator(
        ...
        circuit_breaker=circuit_breaker,
        web_search_client=web_search_client,
        web_fetch_client=web_fetch_client,
    )
```

---

## 설계 결정 근거

### 왜 RAG SearchEngine이 아닌 ContentGenerator 레벨에서 통합하는가?

`CrossEncoderRagSearchEngine.retrieve()`는 **동기(sync)** 함수이다.
`BraveSearchClient.search()`와 `WebFetchClient.fetch_content()`은 **비동기(async)** 함수이다.

sync 함수 안에서 async를 호출하려면 `asyncio.run()` + ThreadPool 해킹이 필요하고,
FastAPI/APScheduler의 실행 중인 이벤트 루프와 충돌한다.

`ContentGenerator.generate()`는 이미 **async** 함수이므로,
여기서 `await self._collect_web_context()`를 직접 호출하면 자연스럽다.

### 왜 프롬프트 변경이 불필요한가?

`_generate_draft()` (content_generator.py line 1230-1243)에 이미 비경제 토픽용 `[NewsData]` 주입 코드가 있다:

```python
elif quality_only:
    ...
    if news_data_text:
        user_prompt = (
            f"{user_prompt}\n\n"
            f"[NewsData]\n{news_data_text}\n\n"
            "추가 규칙:\n"
            "1) NewsData의 최신 사실을 본문에 최소 1회 반영\n"
            "2) NewsData에 없는 수치/인용은 생성 금지\n"
        )
```

웹 검색 결과가 `news_context`에 담기면 이 코드 경로를 자동으로 타므로 프롬프트 수정 불필요.

### 비경제 토픽 max_items=2인 이유

- 경제 토픽: 전용 RSS 피드가 있어 정밀도 높음 → max_items=3
- 비경제 토픽: 웹 검색은 노이즈가 상대적으로 많음 → max_items=2로 제한
- Brave 무료 티어 (2,000쿼리/월): 하루 5포스트 × 30일 = 150쿼리/월 → 충분

### Graceful Degradation 체인

```
web_search_client 존재?
  YES → 검색 + 본문 추출 → news_context에 주입
  NO  → news_context = [] (기존 동작)

economy 토픽:
  RSS 결과 있음? → RSS 사용 (기존)
  RSS 결과 없음? → 웹 검색 폴백 (NEW)

비경제 토픽:
  웹 검색 가능? → 웹 결과 사용 (NEW)
  웹 검색 불가? → news_context = [] (기존, 컨텍스트 없이 생성)
```

---

## 검증 체크리스트

```bash
# 1. 모듈 임포트 확인
python3 -c "
from modules.web_search.base_client import SearchResult, BaseWebSearchClient
from modules.web_search.brave_client import BraveSearchClient
from modules.web_search.web_fetch_client import WebFetchClient
from modules.web_search.provider_factory import create_web_search_client
print('All web_search imports OK')
"

# 2. Config 파싱 확인
python3 -c "
from modules.config import load_config
cfg = load_config()
assert hasattr(cfg, 'web_search'), 'web_search field missing'
assert cfg.web_search.provider == 'brave'
assert cfg.web_search.enabled is False  # default
print(f'WebSearchConfig OK: enabled={cfg.web_search.enabled}, provider={cfg.web_search.provider}')
"

# 3. Factory: 키 없으면 None
python3 -c "
from modules.web_search.provider_factory import create_web_search_client
result = create_web_search_client(provider='brave', api_key='')
assert result is None, 'Should be None without key'
print('Factory None-on-empty-key OK')
"

# 4. ContentGenerator 시그니처 확인
python3 -c "
import inspect
from modules.llm.content_generator import ContentGenerator
sig = inspect.signature(ContentGenerator.__init__)
assert 'web_search_client' in sig.parameters, 'web_search_client param missing'
assert 'web_fetch_client' in sig.parameters, 'web_fetch_client param missing'
print('ContentGenerator signature OK')
"

# 5. _collect_web_context 메서드 존재 확인
python3 -c "
from modules.llm.content_generator import ContentGenerator
assert hasattr(ContentGenerator, '_collect_web_context'), 'Method not found'
print('_collect_web_context exists')
"

# 6. 기존 테스트 통과 (web_search disabled 상태이므로 기존 동작 유지)
python3 -m pytest tests/ -x -q --ignore=tests/e2e

# 7. 타입/임포트 체크
python3 -c "from modules.llm import get_generator; print('LLM module import OK')"
python3 -c "from server.dependencies import get_web_search_client, get_web_fetch_client; print('DI import OK')"
```

---

## 활성화 방법

```bash
# 환경변수로 활성화
export WEB_SEARCH_ENABLED=true
export BRAVE_API_KEY=BSA_xxxxxxxxxxxxxxxxx

# 또는 config/local.yaml
web_search:
  enabled: true
  api_key: "BSA_xxxxxxxxxxxxxxxxx"
```

---

## 수용 기준 (Definition of Done)

1. `modules/web_search/` 패키지가 생성되고 5개 파일 모두 임포트 가능하다
2. `AppConfig.web_search` 필드가 존재하고 `BRAVE_API_KEY` 환경변수로 오버라이드 가능하다
3. `web_search.enabled=false`(기본값)일 때 기존 동작이 100% 유지된다
4. `ContentGenerator.__init__`에 `web_search_client`, `web_fetch_client` 파라미터가 존재한다
5. `_collect_web_context()` async 메서드가 존재하고 웹 검색 결과를 RSS 형식으로 반환한다
6. 비경제 토픽에서도 `news_context`가 수집 시도된다 (web_search 활성화 시)
7. 기존 테스트가 모두 통과한다

---

## 변경 파일 요약

| 파일 | 변경 종류 | 설명 |
|------|----------|------|
| `modules/config.py` | 수정 | WebSearchConfig 추가, AppConfig 확장, env override |
| `config/default.yaml` | 수정 | web_search 섹션 추가 |
| `server/dependencies.py` | 수정 | get_web_search_client, get_web_fetch_client 추가 |
| `modules/llm/__init__.py` | 수정 | _build_generator에 web 클라이언트 주입 |
| `modules/llm/content_generator.py` | 수정 | web_search_client/web_fetch_client 파라미터, _collect_web_context(), 전 토픽 리서치 확장 |
| `modules/web_search/__init__.py` | **신규** | 패키지 초기화 |
| `modules/web_search/base_client.py` | **신규** | ABC 인터페이스 + SearchResult |
| `modules/web_search/brave_client.py` | **신규** | Brave Search API 클라이언트 |
| `modules/web_search/web_fetch_client.py` | **신규** | URL 본문 추출 클라이언트 |
| `modules/web_search/provider_factory.py` | **신규** | 팩토리 함수 |
