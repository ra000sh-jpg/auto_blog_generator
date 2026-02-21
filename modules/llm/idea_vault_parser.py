"""대량 아이디어 창고 입력을 배치 파싱하는 모듈."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import LLMConfig
from .base_client import BaseLLMClient
from .prompts import normalize_topic_mode
from .provider_factory import create_client

logger = logging.getLogger(__name__)

_BANNED_WORDS = (
    "씨발",
    "병신",
    "fuck",
    "shit",
)

_TOPIC_KEYWORDS: Dict[str, List[str]] = {
    "finance": ["경제", "주식", "재테크", "투자", "환율", "금리", "세금", "부동산", "코인"],
    "it": ["it", "개발", "코딩", "ai", "자동화", "앱", "생산성", "노션", "코드"],
    "parenting": ["육아", "아이", "아기", "학습", "가정", "부모", "유치원", "초등"],
    "cafe": ["카페", "맛집", "커피", "요리", "레시피", "디저트", "브런치", "리뷰"],
}


@dataclass
class IdeaVaultParsedItem:
    """아이디어 창고 파싱 완료 아이템."""

    raw_text: str
    mapped_category: str
    topic_mode: str
    parser_used: str


@dataclass
class IdeaVaultParseResult:
    """아이디어 창고 파싱 결과."""

    total_lines: int
    accepted_items: List[IdeaVaultParsedItem] = field(default_factory=list)
    rejected_lines: List[Dict[str, str]] = field(default_factory=list)
    parser_used: str = "heuristic"


class IdeaVaultBatchParser:
    """대량 아이디어 입력을 배치로 필터링/분류한다."""

    def __init__(self, llm_config: Optional[LLMConfig] = None):
        self.llm_config = llm_config or LLMConfig()
        self._clients = self._build_clients()

    async def parse_bulk(
        self,
        raw_text: str,
        *,
        categories: List[str],
        batch_size: int = 20,
    ) -> IdeaVaultParseResult:
        """여러 줄 입력을 파싱해 승인 아이템만 반환한다."""
        lines = [line.strip() for line in str(raw_text).splitlines() if line.strip()]
        if not lines:
            return IdeaVaultParseResult(total_lines=0)

        safe_batch_size = max(1, min(50, int(batch_size)))
        allowed_categories = [category for category in categories if str(category).strip()]
        if not allowed_categories:
            allowed_categories = ["다양한 생각"]

        accepted_items: List[IdeaVaultParsedItem] = []
        rejected_lines: List[Dict[str, str]] = []
        parser_used = "heuristic"

        for start in range(0, len(lines), safe_batch_size):
            chunk = lines[start : start + safe_batch_size]
            chunk_result = await self._parse_chunk_with_fallback(chunk, allowed_categories)
            if chunk_result["parser_used"] != "heuristic":
                parser_used = chunk_result["parser_used"]

            accepted_items.extend(chunk_result["accepted_items"])
            rejected_lines.extend(chunk_result["rejected_lines"])

        return IdeaVaultParseResult(
            total_lines=len(lines),
            accepted_items=accepted_items,
            rejected_lines=rejected_lines,
            parser_used=parser_used,
        )

    def _build_clients(self) -> List[BaseLLMClient]:
        """아이디어 배치 파싱용 LLM 체인을 구성한다."""
        provider_chain: List[str] = []
        if os.getenv("GEMINI_API_KEY", "").strip():
            provider_chain.append("gemini")

        for provider_name in (
            self.llm_config.primary_provider,
            self.llm_config.secondary_provider,
            "qwen",
            "deepseek",
        ):
            normalized = str(provider_name).strip().lower()
            if normalized and normalized not in provider_chain:
                provider_chain.append(normalized)

        clients: List[BaseLLMClient] = []
        for provider_name in provider_chain:
            try:
                clients.append(
                    create_client(
                        provider=provider_name,
                        model=self._model_for_provider(provider_name),
                        timeout_sec=min(45.0, self.llm_config.timeout_sec),
                        max_tokens=1200,
                    )
                )
            except Exception as exc:
                logger.debug("Idea vault parser provider skip: %s (%s)", provider_name, exc)
        return clients

    def _model_for_provider(self, provider_name: str) -> Optional[str]:
        """프로바이더별 기본 모델을 반환한다."""
        if provider_name == "gemini":
            return "gemini-2.0-flash"
        if provider_name == self.llm_config.primary_provider:
            return self.llm_config.primary_model
        if provider_name == self.llm_config.secondary_provider:
            return self.llm_config.secondary_model
        if provider_name == "qwen":
            return "qwen-plus"
        if provider_name == "deepseek":
            return "deepseek-chat"
        return None

    async def _parse_chunk_with_fallback(
        self,
        chunk_lines: List[str],
        allowed_categories: List[str],
    ) -> Dict[str, Any]:
        """LLM 우선, 실패 시 규칙 기반으로 청크를 파싱한다."""
        if self._clients:
            llm_result = await self._parse_chunk_with_llm(chunk_lines, allowed_categories)
            if llm_result is not None:
                return llm_result
        return self._parse_chunk_with_heuristic(chunk_lines, allowed_categories)

    async def _parse_chunk_with_llm(
        self,
        chunk_lines: List[str],
        allowed_categories: List[str],
    ) -> Optional[Dict[str, Any]]:
        """LLM으로 청크 파싱을 시도한다."""
        category_text = ", ".join(allowed_categories)
        indexed_lines = "\n".join(
            f"{index + 1}. {line}" for index, line in enumerate(chunk_lines)
        )
        system_prompt = (
            "당신은 블로그 아이디어 정제기입니다. "
            "각 줄을 독립 평가하여 JSON만 출력하세요."
        )
        user_prompt = f"""
[카테고리 후보]
{category_text}

[입력 라인]
{indexed_lines}

[출력 JSON 스키마]
{{
  "items": [
    {{
      "line_no": 1,
      "accepted": true,
      "normalized_text": "정제된 문장",
      "mapped_category": "카테고리 후보 중 하나",
      "topic_mode": "cafe|it|parenting|finance|economy",
      "reason": "reject 사유(허용 시 빈 문자열)"
    }}
  ]
}}

규칙:
- 욕설/무의미 특수문자/한 글자 키워드 나열은 accepted=false
- accepted=true 항목은 mapped_category를 반드시 카테고리 후보 안에서 선택
- normalized_text는 의미를 유지하면서 공백만 정리
""".strip()

        for client in self._clients:
            try:
                response = await client.generate_with_retry(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    max_retries=2,
                    temperature=0.1,
                    max_tokens=1200,
                )
                payload = self._extract_json(response.content)
                parsed = self._validate_llm_items(
                    payload=payload,
                    original_lines=chunk_lines,
                    allowed_categories=allowed_categories,
                    parser_used=client.provider_name,
                )
                if parsed is not None:
                    return parsed
            except Exception as exc:
                logger.debug("Idea vault llm chunk parse failed: %s", exc)
                continue
        return None

    def _extract_json(self, raw_text: str) -> Dict[str, Any]:
        """문자열 응답에서 JSON 객체를 추출한다."""
        text = str(raw_text or "").strip()
        if not text:
            return {}
        fenced = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            pass

        matched = re.search(r"\{.*\}", text, re.DOTALL)
        if not matched:
            return {}
        try:
            value = json.loads(matched.group(0))
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _validate_llm_items(
        self,
        *,
        payload: Dict[str, Any],
        original_lines: List[str],
        allowed_categories: List[str],
        parser_used: str,
    ) -> Optional[Dict[str, Any]]:
        """LLM 응답 아이템을 검증/정규화한다."""
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            return None

        item_map: Dict[int, Dict[str, Any]] = {}
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            try:
                line_no = int(raw_item.get("line_no", 0))
            except (TypeError, ValueError):
                continue
            if line_no < 1 or line_no > len(original_lines):
                continue
            item_map[line_no] = raw_item

        accepted_items: List[IdeaVaultParsedItem] = []
        rejected_lines: List[Dict[str, str]] = []
        for index, original_line in enumerate(original_lines, start=1):
            raw_item = item_map.get(index)
            if raw_item is None:
                return None
            accepted = bool(raw_item.get("accepted", False))
            normalized_text = str(raw_item.get("normalized_text", "")).strip() or original_line
            reason = str(raw_item.get("reason", "")).strip()

            mapped_category = self._normalize_category(
                raw_value=str(raw_item.get("mapped_category", "")).strip(),
                text=normalized_text,
                allowed_categories=allowed_categories,
            )
            topic_mode = self._infer_topic_mode(
                text=normalized_text,
                fallback_text=mapped_category,
            )
            if topic_mode == "economy":
                topic_mode = "finance"

            if not accepted or not self._is_valid_idea_line(normalized_text):
                rejected_lines.append(
                    {
                        "line": original_line,
                        "reason": reason or "품질 미달 또는 금칙어",
                    }
                )
                continue

            accepted_items.append(
                IdeaVaultParsedItem(
                    raw_text=normalized_text,
                    mapped_category=mapped_category,
                    topic_mode=topic_mode,
                    parser_used=parser_used,
                )
            )

        return {
            "accepted_items": accepted_items,
            "rejected_lines": rejected_lines,
            "parser_used": parser_used,
        }

    def _parse_chunk_with_heuristic(
        self,
        chunk_lines: List[str],
        allowed_categories: List[str],
    ) -> Dict[str, Any]:
        """규칙 기반 파서로 청크를 처리한다."""
        accepted_items: List[IdeaVaultParsedItem] = []
        rejected_lines: List[Dict[str, str]] = []
        for line in chunk_lines:
            if not self._is_valid_idea_line(line):
                rejected_lines.append({"line": line, "reason": "품질 미달 또는 금칙어"})
                continue
            mapped_category = self._normalize_category(
                raw_value="",
                text=line,
                allowed_categories=allowed_categories,
            )
            topic_mode = self._infer_topic_mode(text=line, fallback_text=mapped_category)
            if topic_mode == "economy":
                topic_mode = "finance"
            accepted_items.append(
                IdeaVaultParsedItem(
                    raw_text=line,
                    mapped_category=mapped_category,
                    topic_mode=topic_mode,
                    parser_used="heuristic",
                )
            )
        return {
            "accepted_items": accepted_items,
            "rejected_lines": rejected_lines,
            "parser_used": "heuristic",
        }

    def _is_valid_idea_line(self, text: str) -> bool:
        """라인 품질을 검증한다."""
        line = str(text or "").strip()
        if len(line) < 4:
            return False
        lowered = line.lower()
        if any(token in lowered for token in _BANNED_WORDS):
            return False

        allowed_char_count = len(re.findall(r"[가-힣A-Za-z0-9]", line))
        if allowed_char_count < 3:
            return False
        if allowed_char_count / max(len(line), 1) < 0.25:
            return False
        return True

    def _normalize_category(
        self,
        *,
        raw_value: str,
        text: str,
        allowed_categories: List[str],
    ) -> str:
        """카테고리를 허용 목록으로 정규화한다."""
        stripped = str(raw_value).strip()
        if stripped in allowed_categories:
            return stripped

        best_category = allowed_categories[0] if allowed_categories else "다양한 생각"
        best_score = -1
        lowered_text = str(text).lower()
        for category in allowed_categories:
            lowered_category = category.lower()
            score = 0
            if lowered_category in lowered_text:
                score += 3
            tokens = re.findall(r"[가-힣A-Za-z0-9]{2,20}", lowered_category)
            score += sum(1 for token in tokens if token and token in lowered_text)
            if score > best_score:
                best_score = score
                best_category = category
        return best_category

    def _infer_topic_mode(self, *, text: str, fallback_text: str = "") -> str:
        """텍스트 기반으로 토픽 모드를 추정한다."""
        lowered = f"{text} {fallback_text}".lower()
        best_topic = "cafe"
        best_score = -1
        for topic_name, keywords in _TOPIC_KEYWORDS.items():
            score = sum(1 for keyword in keywords if keyword.lower() in lowered)
            if score > best_score:
                best_score = score
                best_topic = topic_name
        return normalize_topic_mode(best_topic)
