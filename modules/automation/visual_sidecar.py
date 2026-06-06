"""FreeLLMAPI 기반 무료 시각자료 사이드카."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

import httpx

from ..images.flowchart_renderer import render_flowchart
from ..images.market_chart_renderer import render_market_chart
from ..images.pexels_client import PexelsImageClient
from ..images.pollinations_client import PollinationsImageClient
from ..images.summary_card_renderer import render_summary_card
from ..images.table_renderer import extract_and_render_tables_with_validation
from ..images.visual_text_sanitizer import sanitize_visual_lines, sanitize_visual_text

logger = logging.getLogger(__name__)

SIDECAR_PROVIDER = "freellmapi_visual_sidecar"
DEFAULT_FREELLMAPI_BASE_URL = "http://127.0.0.1:3001/v1"
DEFAULT_FREELLMAPI_MODEL = "auto"
SUPPORTED_TOPICS = {"finance", "economy", "it", "cafe", "parenting"}


class VisualPlanner(Protocol):
    """시각자료 제안 플래너 인터페이스."""

    async def plan_visuals(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


@dataclass
class VisualSidecarStats:
    """사이드카 실행 요약."""

    status: str = "skipped"
    added_count: int = 0
    attempted_types: list[str] = field(default_factory=list)
    added: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    model: str = ""
    base_url: str = ""


class FreeLLMAPIVisualPlanner:
    """OpenAI Chat Completions 호환 FreeLLMAPI 플래너."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_FREELLMAPI_BASE_URL,
        model: str = DEFAULT_FREELLMAPI_MODEL,
        api_key: str = "",
        timeout_sec: float = 20.0,
    ):
        self.base_url = str(base_url or DEFAULT_FREELLMAPI_BASE_URL).rstrip("/")
        self.model = str(model or DEFAULT_FREELLMAPI_MODEL).strip() or DEFAULT_FREELLMAPI_MODEL
        self.api_key = str(api_key or "").strip()
        self.timeout_sec = max(3.0, float(timeout_sec or 20.0))

    async def plan_visuals(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        """본문 기반 시각자료 제안 JSON을 반환한다."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a visual planning assistant for Korean blog posts. "
                        "Return compact JSON only. Do not rewrite the article. "
                        "Do not invent numeric market data. Use only supplied market_snapshot values."
                    ),
                },
                {
                    "role": "user",
                    "content": _build_visual_prompt(context),
                },
            ],
            "temperature": 0.2,
            "max_tokens": 900,
        }

        async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        content = ""
        try:
            content = str(data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
        except Exception:
            content = ""
        parsed = _parse_json_object(content)
        if not isinstance(parsed, dict):
            return {}
        return parsed


class VisualSidecar:
    """완성 payload에 무료 시각자료를 추가한다."""

    def __init__(
        self,
        *,
        planner: VisualPlanner,
        output_dir: str = "data/images",
        max_visuals: int = 2,
        pexels_client: Optional[PexelsImageClient] = None,
        pollinations_client: Optional[PollinationsImageClient] = None,
        planner_model: str = "",
        planner_base_url: str = "",
    ):
        self.planner = planner
        self.output_dir = output_dir
        self.max_visuals = max(0, min(4, int(max_visuals or 2)))
        self.pexels_client = pexels_client
        self.pollinations_client = pollinations_client
        self.planner_model = planner_model
        self.planner_base_url = planner_base_url

    async def enrich_payload(self, *, job: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """payload에 추가 시각자료를 붙인다. 실패해도 원본 흐름을 깨지 않는다."""
        if self.max_visuals <= 0:
            return payload
        if str(getattr(job, "platform", "") or "").strip().lower() != "naver":
            return payload

        content = str(payload.get("content", "") or "").strip()
        if not content:
            return payload

        topic_mode = _resolve_topic_mode(job=job, payload=payload)
        if topic_mode not in SUPPORTED_TOPICS:
            return self._record(payload, VisualSidecarStats(status="skipped", skipped=[
                {"type": "all", "reason": "unsupported_topic"}
            ]))

        stats = VisualSidecarStats(
            model=self.planner_model,
            base_url=self.planner_base_url,
        )
        context = {
            "title": str(payload.get("title", getattr(job, "title", "")) or ""),
            "content": _clip_text(content, 4500),
            "keywords": list(getattr(job, "seed_keywords", []) or []),
            "topic_mode": topic_mode,
            "market_snapshot": _market_snapshot(payload),
            "existing_renderers": sorted(_existing_renderers(payload)),
            "max_visuals": self.max_visuals,
        }

        try:
            plan = await self.planner.plan_visuals(context)
        except Exception as exc:
            logger.info(
                "Visual sidecar skipped: planner unavailable",
                extra={"error": str(exc)[:200], "topic_mode": topic_mode},
            )
            stats.status = "failed"
            stats.skipped.append({"type": "planner", "reason": "planner_unavailable"})
            return self._record(payload, stats)

        visuals = _normalize_visuals(plan)
        if not visuals:
            stats.status = "skipped"
            stats.skipped.append({"type": "planner", "reason": "empty_plan"})
            return self._record(payload, stats)

        normalized = dict(payload)
        for visual in _order_visuals(visuals, topic_mode):
            if stats.added_count >= self.max_visuals:
                break
            visual_type = str(visual.get("type", "")).strip().lower()
            stats.attempted_types.append(visual_type)
            try:
                added = await self._try_add_visual(
                    payload=normalized,
                    visual=visual,
                    topic_mode=topic_mode,
                    stats=stats,
                )
            except Exception as exc:
                logger.info(
                    "Visual sidecar visual skipped",
                    extra={"type": visual_type, "error": str(exc)[:200]},
                )
                stats.skipped.append({"type": visual_type, "reason": "render_exception"})
                added = False
            if added:
                stats.added_count += 1

        stats.status = "attached" if stats.added_count else "skipped"
        return self._record(normalized, stats)

    async def _try_add_visual(
        self,
        *,
        payload: dict[str, Any],
        visual: Mapping[str, Any],
        topic_mode: str,
        stats: VisualSidecarStats,
    ) -> bool:
        visual_type = str(visual.get("type", "")).strip().lower()
        existing = _existing_renderers(payload)

        if visual_type == "market_chart":
            if "market_chart" in existing:
                stats.skipped.append({"type": visual_type, "reason": "duplicate"})
                return False
            market_snapshot = _market_snapshot(payload)
            raw_points = market_snapshot.get("data_points", []) if isinstance(market_snapshot, dict) else []
            if not isinstance(raw_points, list) or len(raw_points) < 2:
                stats.skipped.append({"type": visual_type, "reason": "missing_market_snapshot"})
                return False
            result = render_market_chart(
                market_snapshot=market_snapshot,
                title=str(payload.get("title", "") or ""),
                output_dir=self.output_dir,
            )
            if result is None:
                stats.skipped.append({"type": visual_type, "reason": "render_failed"})
                return False
            self._attach_image(
                payload=payload,
                path=result.path,
                renderer="market_chart",
                kind="manual",
                section_hint=_section_hint(visual, "시장 그래프"),
            )
            stats.added.append({"type": visual_type, "path": result.path})
            return True

        if visual_type == "summary_card":
            if "summary_card" in existing:
                stats.skipped.append({"type": visual_type, "reason": "duplicate"})
                return False
            bullets = _string_list(visual.get("bullets"), max_items=5)
            if len(bullets) < 2:
                stats.skipped.append({"type": visual_type, "reason": "missing_bullets"})
                return False
            result = render_summary_card(
                title=str(visual.get("title") or payload.get("title", "") or ""),
                content=str(payload.get("content", "") or ""),
                output_dir=self.output_dir,
                max_bullets=min(5, max(2, len(bullets))),
                style=_style_for_topic(topic_mode),
                bullets_override=bullets,
            )
            if result is None:
                stats.skipped.append({"type": visual_type, "reason": "render_failed"})
                return False
            self._attach_image(
                payload=payload,
                path=result.path,
                renderer="summary_card",
                kind="manual",
                section_hint=_section_hint(visual, "요약 카드"),
            )
            stats.added.append({"type": visual_type, "path": result.path})
            return True

        if visual_type == "table":
            if "table" in existing:
                stats.skipped.append({"type": visual_type, "reason": "duplicate"})
                return False
            markdown = _table_markdown(visual)
            if not markdown:
                stats.skipped.append({"type": visual_type, "reason": "invalid_table"})
                return False
            _modified, paths, validation = extract_and_render_tables_with_validation(
                content=markdown,
                output_dir=self.output_dir,
                style=_style_for_topic(topic_mode),
            )
            if not paths:
                stats.skipped.append({"type": visual_type, "reason": "render_failed"})
                return False
            path = paths[0]
            self._attach_image(
                payload=payload,
                path=path,
                renderer="table",
                kind="manual",
                section_hint=_section_hint(visual, "보조 표"),
            )
            stats.added.append({
                "type": visual_type,
                "path": path,
                "validation": "pass" if validation.passed else "warn",
            })
            return True

        if visual_type == "flowchart":
            if "flowchart" in existing:
                stats.skipped.append({"type": visual_type, "reason": "duplicate"})
                return False
            nodes = _string_list(visual.get("nodes"), max_items=5)
            if len(nodes) < 2:
                stats.skipped.append({"type": visual_type, "reason": "missing_nodes"})
                return False
            result = render_flowchart(
                title=str(visual.get("title") or payload.get("title", "") or "판단 흐름"),
                nodes=nodes,
                output_dir=self.output_dir,
                style=_style_for_topic(topic_mode),
            )
            if result is None:
                stats.skipped.append({"type": visual_type, "reason": "render_failed"})
                return False
            self._attach_image(
                payload=payload,
                path=result.path,
                renderer="flowchart",
                kind="manual",
                section_hint=_section_hint(visual, "흐름도"),
            )
            stats.added.append({"type": visual_type, "path": result.path})
            return True

        if visual_type == "pexels":
            if "pexels" in existing:
                stats.skipped.append({"type": visual_type, "reason": "duplicate"})
                return False
            if self.pexels_client is None:
                stats.skipped.append({"type": visual_type, "reason": "pexels_unavailable"})
                return False
            query = sanitize_visual_text(str(visual.get("query") or visual.get("prompt") or ""), max_chars=90)
            if not query:
                stats.skipped.append({"type": visual_type, "reason": "missing_query"})
                return False
            result = await self.pexels_client.generate(query, size="1024*768")
            if not result.success or not result.local_path:
                stats.skipped.append({"type": visual_type, "reason": "pexels_failed"})
                return False
            self._attach_image(
                payload=payload,
                path=result.local_path,
                renderer="pexels",
                kind="stock",
                section_hint=_section_hint(visual, "보조 사진"),
            )
            stats.added.append({"type": visual_type, "path": result.local_path})
            return True

        if visual_type == "pollinations":
            if "pollinations" in existing:
                stats.skipped.append({"type": visual_type, "reason": "duplicate"})
                return False
            if self.pollinations_client is None:
                stats.skipped.append({"type": visual_type, "reason": "pollinations_unavailable"})
                return False
            prompt = sanitize_visual_text(str(visual.get("prompt") or ""), max_chars=180)
            if not prompt:
                stats.skipped.append({"type": visual_type, "reason": "missing_prompt"})
                return False
            result = await self.pollinations_client.generate(prompt, size="1024*768")
            if not result.success or not result.local_path:
                stats.skipped.append({"type": visual_type, "reason": "pollinations_failed"})
                return False
            self._attach_image(
                payload=payload,
                path=result.local_path,
                renderer="pollinations",
                kind="ai_generated",
                section_hint=_section_hint(visual, "보조 AI 이미지"),
            )
            stats.added.append({"type": visual_type, "path": result.local_path})
            return True

        stats.skipped.append({"type": visual_type or "unknown", "reason": "unsupported_type"})
        return False

    def _attach_image(
        self,
        *,
        payload: dict[str, Any],
        path: str,
        renderer: str,
        kind: str,
        section_hint: str,
    ) -> None:
        content = str(payload.get("content", "") or "")
        image_points = list(payload.get("image_points", [])) if isinstance(payload.get("image_points"), list) else []
        marker_index = _next_image_marker_index(content, image_points)
        marker = f"[IMG_{marker_index}]"
        payload["content"] = _insert_marker(content, marker)
        image_points.append(
            {
                "index": marker_index,
                "path": str(path),
                "marker": marker,
                "section_hint": section_hint,
                "is_thumbnail": False,
            }
        )
        payload["image_points"] = image_points
        image_sources = _normalize_image_sources(payload.get("image_sources", {}))
        image_sources[str(path)] = {
            "kind": kind,
            "provider": SIDECAR_PROVIDER,
            "renderer": renderer,
        }
        payload["image_sources"] = image_sources

    @staticmethod
    def _record(payload: dict[str, Any], stats: VisualSidecarStats) -> dict[str, Any]:
        normalized = dict(payload)
        quality_snapshot = dict(normalized.get("quality_snapshot", {}) or {})
        quality_snapshot["visual_sidecar"] = {
            "status": stats.status,
            "added_count": stats.added_count,
            "attempted_types": stats.attempted_types,
            "added": stats.added,
            "skipped": stats.skipped[:8],
            "model": stats.model,
            "base_url": stats.base_url,
        }
        normalized["quality_snapshot"] = quality_snapshot
        return normalized


def build_visual_sidecar_from_env(
    *,
    output_dir: str = "data/images",
    job_store: Optional[Any] = None,
) -> Optional[VisualSidecar]:
    """환경변수 설정을 기준으로 사이드카를 생성한다."""
    if not _env_bool("FREELLMAPI_VISUAL_SIDECAR_ENABLED", default=False):
        return None

    base_url = os.getenv("FREELLMAPI_BASE_URL", DEFAULT_FREELLMAPI_BASE_URL).strip()
    model = os.getenv("FREELLMAPI_VISUAL_MODEL", DEFAULT_FREELLMAPI_MODEL).strip() or DEFAULT_FREELLMAPI_MODEL
    api_key = os.getenv("FREELLMAPI_API_KEY", "").strip()
    timeout_sec = _env_float("FREELLMAPI_VISUAL_TIMEOUT_SEC", 20.0, min_value=3.0, max_value=90.0)
    max_visuals = _env_int("FREELLMAPI_VISUAL_MAX_ITEMS", 2, min_value=0, max_value=4)

    planner = FreeLLMAPIVisualPlanner(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_sec=timeout_sec,
    )

    pexels_key = _load_image_key(job_store, "pexels") or os.getenv("PEXELS_API_KEY", "").strip()
    pexels_client = (
        PexelsImageClient(api_key=pexels_key, output_dir=output_dir, timeout_sec=30.0)
        if pexels_key
        else None
    )
    pollinations_client = None
    if _env_bool("FREELLMAPI_VISUAL_ALLOW_POLLINATIONS", default=True):
        pollinations_client = PollinationsImageClient(output_dir=output_dir)

    return VisualSidecar(
        planner=planner,
        output_dir=output_dir,
        max_visuals=max_visuals,
        pexels_client=pexels_client,
        pollinations_client=pollinations_client,
        planner_model=model,
        planner_base_url=base_url,
    )


def _build_visual_prompt(context: Mapping[str, Any]) -> str:
    """FreeLLMAPI에 전달할 JSON 전용 프롬프트를 만든다."""
    market_snapshot = context.get("market_snapshot", {})
    return json.dumps(
        {
            "task": "Suggest extra visual assets for this already-written Korean blog post.",
            "rules": [
                "Return JSON object only: {\"visuals\": [...]}",
                "Allowed types: summary_card, table, market_chart, flowchart, pexels, pollinations.",
                "Do not rewrite the article body.",
                "For market_chart, only use supplied market_snapshot. Never create new numbers.",
                "Keep Korean visual labels short.",
                "For pexels, provide an English query.",
                "For pollinations, provide an English prompt with no text in image.",
            ],
            "schema": {
                "summary_card": {"type": "summary_card", "title": "string", "bullets": ["2-5 strings"]},
                "table": {"type": "table", "title": "string", "headers": ["2-4 strings"], "rows": [["strings"]]},
                "market_chart": {"type": "market_chart", "title": "string"},
                "flowchart": {"type": "flowchart", "title": "string", "nodes": ["2-5 strings"]},
                "pexels": {"type": "pexels", "query": "English search query"},
                "pollinations": {"type": "pollinations", "prompt": "English image prompt, no text"},
            },
            "context": {
                "title": context.get("title", ""),
                "topic_mode": context.get("topic_mode", ""),
                "keywords": context.get("keywords", []),
                "existing_renderers": context.get("existing_renderers", []),
                "max_visuals": context.get("max_visuals", 2),
                "market_snapshot": market_snapshot if isinstance(market_snapshot, dict) else {},
                "content": context.get("content", ""),
            },
        },
        ensure_ascii=False,
    )


def _parse_json_object(raw_text: str) -> Mapping[str, Any]:
    text = str(raw_text or "").strip()
    if not text:
        return {}
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _normalize_visuals(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_visuals = plan.get("visuals", [])
    if not isinstance(raw_visuals, list):
        return []
    output: list[dict[str, Any]] = []
    for item in raw_visuals[:8]:
        if not isinstance(item, dict):
            continue
        visual_type = str(item.get("type", "")).strip().lower()
        if visual_type in {"chart", "graph"}:
            visual_type = "market_chart"
        if visual_type in {"flow", "diagram"}:
            visual_type = "flowchart"
        normalized = dict(item)
        normalized["type"] = visual_type
        output.append(normalized)
    return output


def _order_visuals(visuals: Sequence[Mapping[str, Any]], topic_mode: str) -> list[Mapping[str, Any]]:
    if topic_mode in {"finance", "economy"}:
        order = ["market_chart", "table", "summary_card", "flowchart", "pexels", "pollinations"]
    elif topic_mode == "it":
        order = ["flowchart", "table", "summary_card", "pexels", "pollinations", "market_chart"]
    else:
        order = ["summary_card", "table", "pexels", "flowchart", "pollinations", "market_chart"]
    rank = {name: index for index, name in enumerate(order)}
    return sorted(visuals, key=lambda item: rank.get(str(item.get("type", "")).strip().lower(), 99))


def _existing_renderers(payload: Mapping[str, Any]) -> set[str]:
    renderers: set[str] = set()
    raw_sources = payload.get("image_sources", {})
    if isinstance(raw_sources, dict):
        for meta in raw_sources.values():
            if not isinstance(meta, dict):
                continue
            renderer = str(meta.get("renderer", "")).strip().lower()
            provider = str(meta.get("provider", "")).strip().lower()
            if renderer:
                renderers.add(renderer)
            if provider == "summary_card_renderer":
                renderers.add("summary_card")
            elif provider == "market_chart_renderer":
                renderers.add("market_chart")
            elif provider == "table_renderer":
                renderers.add("table")
            elif provider == SIDECAR_PROVIDER:
                renderer = str(meta.get("renderer", "")).strip().lower()
                if renderer:
                    renderers.add(renderer)
    raw_points = payload.get("image_points", [])
    if isinstance(raw_points, list):
        for point in raw_points:
            if not isinstance(point, dict):
                continue
            hint = str(point.get("section_hint", "")).strip()
            if "요약 카드" in hint:
                renderers.add("summary_card")
            if "시장 그래프" in hint:
                renderers.add("market_chart")
            if "흐름도" in hint:
                renderers.add("flowchart")
            if "표" in hint:
                renderers.add("table")
    return renderers


def _table_markdown(visual: Mapping[str, Any]) -> str:
    headers = _string_list(visual.get("headers"), max_items=4)
    rows_raw = visual.get("rows", [])
    rows: list[list[str]] = []
    if isinstance(rows_raw, list):
        for row in rows_raw[:6]:
            if isinstance(row, list):
                cells = [sanitize_visual_text(str(cell), max_chars=56) for cell in row[: len(headers) or 4]]
            elif isinstance(row, dict):
                cells = [sanitize_visual_text(str(row.get(header, "")), max_chars=56) for header in headers]
            else:
                continue
            if any(cells):
                rows.append(cells)
    if len(headers) < 2 or not rows:
        return ""
    headers = headers[:4]
    normalized_rows = []
    for row in rows:
        cells = row[: len(headers)]
        if len(cells) < len(headers):
            cells.extend([""] * (len(headers) - len(cells)))
        normalized_rows.append(cells)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in normalized_rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _string_list(raw_value: Any, *, max_items: int) -> list[str]:
    if not isinstance(raw_value, list):
        return []
    values = [str(item) for item in raw_value[:max_items]]
    return sanitize_visual_lines(values, max_chars=80)


def _resolve_topic_mode(*, job: Any, payload: Mapping[str, Any]) -> str:
    seo_snapshot = payload.get("seo_snapshot", {})
    if isinstance(seo_snapshot, dict):
        topic = str(seo_snapshot.get("topic_mode", "")).strip().lower()
        if topic:
            return topic
    category = str(getattr(job, "category", "") or "").strip().lower()
    if "경제" in category or "재테크" in category or "finance" in category:
        return "finance"
    if "it" in category or "테크" in category:
        return "it"
    if "육아" in category or "parent" in category:
        return "parenting"
    return "cafe"


def _market_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    seo_snapshot = payload.get("seo_snapshot", {})
    if not isinstance(seo_snapshot, dict):
        return {}
    market_snapshot = seo_snapshot.get("market_snapshot", {})
    return dict(market_snapshot) if isinstance(market_snapshot, dict) else {}


def _normalize_image_sources(raw_sources: Any) -> dict[str, dict[str, str]]:
    output: dict[str, dict[str, str]] = {}
    if not isinstance(raw_sources, dict):
        return output
    for path, meta in raw_sources.items():
        normalized_path = str(path or "").strip()
        if not normalized_path:
            continue
        if isinstance(meta, dict):
            normalized_meta = {
                "kind": str(meta.get("kind", "unknown")).strip().lower() or "unknown",
                "provider": str(meta.get("provider", "unknown")).strip().lower() or "unknown",
            }
            renderer = str(meta.get("renderer", "")).strip().lower()
            if renderer:
                normalized_meta["renderer"] = renderer
            output[normalized_path] = normalized_meta
        else:
            output[normalized_path] = {"kind": "unknown", "provider": "unknown"}
    return output


def _next_image_marker_index(content: str, image_points: list[Any]) -> int:
    indices: list[int] = []
    for match in re.finditer(r"\[IMG_(\d+)\]", str(content or "")):
        try:
            indices.append(int(match.group(1)))
        except Exception:
            continue
    for point in image_points:
        if isinstance(point, dict) and "index" in point:
            try:
                indices.append(int(point["index"]))
            except Exception:
                continue
    return max(indices, default=-1) + 1


def _insert_marker(content: str, marker: str) -> str:
    text = str(content or "").strip()
    if not text:
        return marker
    section_match = re.search(r"(?m)^■\s+.+$", text)
    if section_match and section_match.start() > 0:
        pos = section_match.start()
        return f"{text[:pos].rstrip()}\n\n{marker}\n\n{text[pos:].lstrip()}"
    first_blank = text.find("\n\n")
    if first_blank > 0:
        pos = first_blank + 2
        return f"{text[:pos].rstrip()}\n\n{marker}\n\n{text[pos:].lstrip()}"
    return f"{marker}\n\n{text}"


def _section_hint(visual: Mapping[str, Any], fallback: str) -> str:
    return sanitize_visual_text(str(visual.get("section_hint") or visual.get("title") or fallback), max_chars=40)


def _style_for_topic(topic_mode: str) -> str:
    return "market_note" if str(topic_mode).strip().lower() in {"finance", "economy"} else "default"


def _clip_text(text: str, max_chars: int) -> str:
    value = str(text or "")
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip()


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(str(os.getenv(name, str(default))).strip())
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _env_float(name: str, default: float, *, min_value: float, max_value: float) -> float:
    try:
        value = float(str(os.getenv(name, str(default))).strip())
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _load_image_key(job_store: Optional[Any], key_id: str) -> str:
    if job_store is None:
        return ""
    try:
        raw = job_store.get_system_setting("router_image_api_keys", "{}")
        decoded = json.loads(raw) if raw else {}
        if isinstance(decoded, dict):
            return str(decoded.get(key_id, "")).strip()
    except Exception:
        return ""
    return ""
