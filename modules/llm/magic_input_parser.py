"""단일 자연어 입력을 Job 파라미터로 변환하는 파서."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..automation.time_utils import parse_iso
from ..config import LLMConfig
from .base_client import BaseLLMClient
from .llm_router import LLMRouter
from .prompts import normalize_topic_mode
from .provider_factory import create_client

logger = logging.getLogger(__name__)

_TOPIC_KEYWORDS: Dict[str, List[str]] = {
    "finance": ["주식", "경제", "재테크", "환율", "금리", "투자", "배당", "코인", "부동산"],
    "it": ["개발", "코딩", "ai", "자동화", "앱", "노션", "생산성", "it", "프로그래밍"],
    "parenting": ["육아", "아기", "아이", "딸", "아들", "가정", "교육", "유치원", "학습"],
    "cafe": ["카페", "맛집", "커피", "레시피", "디저트", "요리", "베이킹", "브런치", "식당"],
}

_TOPIC_TO_PERSONA: Dict[str, str] = {
    "cafe": "P1",
    "it": "P2",
    "parenting": "P3",
    "finance": "P4",
    "economy": "P4",
}

_RELATIVE_DAY_OFFSETS: Dict[str, int] = {
    "오늘": 0,
    "내일": 1,
    "모레": 2,
    "글피": 3,
}

_WEEKDAY_MAP: Dict[str, int] = {
    "월": 0,
    "화": 1,
    "수": 2,
    "목": 3,
    "금": 4,
    "토": 5,
    "일": 6,
}

_STOPWORDS = {
    "글",
    "포스팅",
    "작성",
    "예약",
    "오늘",
    "내일",
    "이번",
    "관련",
    "하나",
    "정리",
    "해주세요",
    "써줘",
    "써줘요",
    "작성해줘",
}


@dataclass
class MagicInputParseResult:
    """매직 인풋 파싱 결과."""

    title: str
    seed_keywords: List[str]
    persona_id: str
    topic_mode: str
    schedule_time: Optional[str]
    confidence: float
    parser_used: str
    raw: Dict[str, Any]


class MagicInputParser:
    """자연어 입력을 Job 생성 파라미터로 추출한다."""

    def __init__(
        self,
        llm_config: Optional[LLMConfig] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        llm_router: Optional[LLMRouter] = None,
    ):
        self.llm_config = llm_config or LLMConfig()
        self.now_provider = now_provider
        self.llm_router = llm_router
        self._clients = self._build_clients()

    async def parse(self, instruction: str) -> MagicInputParseResult:
        """자연어 지시문을 파싱한다."""
        normalized_instruction = str(instruction or "").strip()
        if not normalized_instruction:
            raise ValueError("instruction is empty")

        for client in self._clients:
            parsed = await self._parse_with_client(client, normalized_instruction)
            if parsed is not None:
                return parsed

        logger.info("Magic parser fallback mode used")
        return self._parse_with_heuristic(normalized_instruction)

    def _build_clients(self) -> List[BaseLLMClient]:
        """사용 가능한 LLM 파서 클라이언트 체인을 만든다."""
        if self.llm_router:
            router_chain = self.llm_router.build_parser_chain()
            router_clients: List[BaseLLMClient] = []
            for item in router_chain:
                try:
                    router_clients.append(
                        create_client(
                            provider=str(item.get("provider", "")).strip(),
                            model=str(item.get("model", "")).strip() or None,
                            timeout_sec=min(40.0, self.llm_config.timeout_sec),
                            max_tokens=800,
                            api_key=str(item.get("api_key", "")).strip() or None,
                        )
                    )
                except Exception as exc:
                    logger.debug("Magic parser router provider skip: %s (%s)", item, exc)
                    continue
            if router_clients:
                return router_clients

        provider_chain: List[str] = []

        # 파서는 비용/속도 균형을 위해 Gemini를 우선 시도한다.
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
                model = self._model_for_provider(provider_name)
                client = create_client(
                    provider=provider_name,
                    model=model,
                    timeout_sec=min(40.0, self.llm_config.timeout_sec),
                    max_tokens=800,
                )
                clients.append(client)
            except Exception as exc:
                logger.debug("Magic parser provider skip: %s (%s)", provider_name, exc)
                continue
        return clients

    def _model_for_provider(self, provider_name: str) -> Optional[str]:
        """프로바이더별 파서용 모델명을 반환한다."""
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

    async def _parse_with_client(
        self,
        client: BaseLLMClient,
        instruction: str,
    ) -> Optional[MagicInputParseResult]:
        """LLM 기반 파싱을 시도한다."""
        system_prompt = (
            "당신은 블로그 작업 파라미터 추출기입니다. "
            "반드시 JSON 객체만 출력하세요. 설명 문장은 절대 출력하지 마세요."
        )
        user_prompt = f"""
[입력 문장]
{instruction}

[출력 규칙]
- 반드시 JSON 객체 1개만 출력
- 키는 정확히 다음만 사용:
  title(string), seed_keywords(array), persona_id(string), topic_mode(string), schedule_time(string|null), confidence(number)
- persona_id는 P1/P2/P3/P4 중 하나
- topic_mode는 cafe/parenting/it/finance/economy 중 하나
- schedule_time은 UTC ISO 8601 ("YYYY-MM-DDTHH:MM:SSZ") 형식 또는 null
- seed_keywords는 1~5개
- title은 8~80자
- confidence는 0~1 실수
""".strip()
        try:
            response = await client.generate_with_retry(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_retries=2,
                temperature=0.1,
                max_tokens=400,
            )
        except Exception as exc:
            logger.debug("Magic parser provider failed: %s", exc)
            return None

        parsed = self._extract_json_dict(response.content)
        if not parsed:
            return None
        validated = self._validate_llm_payload(parsed, instruction=instruction)
        if validated is None:
            return None
        validated.parser_used = client.provider_name
        validated.raw["model"] = response.model
        validated.raw["provider"] = client.provider_name
        validated.raw["input_tokens"] = int(response.input_tokens or 0)
        validated.raw["output_tokens"] = int(response.output_tokens or 0)
        return validated

    def _extract_json_dict(self, raw_text: str) -> Dict[str, Any]:
        """LLM 문자열에서 JSON 객체를 추출한다."""
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

    def _validate_llm_payload(
        self,
        payload: Dict[str, Any],
        instruction: str = "",
    ) -> Optional[MagicInputParseResult]:
        """LLM 결과를 정규화/검증한다."""
        raw_title = str(payload.get("title", "")).strip()
        raw_keywords = payload.get("seed_keywords", [])
        raw_persona_id = str(payload.get("persona_id", "")).strip().upper()
        raw_topic_mode = normalize_topic_mode(str(payload.get("topic_mode", "")).strip() or "cafe")
        raw_schedule_time = payload.get("schedule_time")
        raw_confidence = payload.get("confidence", 0.7)

        if raw_persona_id not in {"P1", "P2", "P3", "P4"}:
            raw_persona_id = _TOPIC_TO_PERSONA.get(raw_topic_mode, "P1")

        if raw_topic_mode not in {"cafe", "parenting", "it", "finance"}:
            raw_topic_mode = "cafe"

        keywords: List[str] = []
        if isinstance(raw_keywords, list):
            for keyword in raw_keywords:
                item = str(keyword).strip()
                if item and item not in keywords:
                    keywords.append(item)

        if not raw_title:
            return None
        if not keywords:
            keywords = self._extract_keywords(raw_title)
        if not keywords:
            keywords = ["자동화", "블로그"]

        schedule_time = self._normalize_schedule_time(
            raw_value=raw_schedule_time,
            fallback_instruction=instruction,
        )

        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            confidence = 0.7
        confidence = max(0.0, min(1.0, confidence))

        return MagicInputParseResult(
            title=raw_title[:80],
            seed_keywords=keywords[:5],
            persona_id=raw_persona_id,
            topic_mode=raw_topic_mode,
            schedule_time=schedule_time,
            confidence=confidence,
            parser_used="llm",
            raw=payload,
        )

    def _parse_with_heuristic(self, instruction: str) -> MagicInputParseResult:
        """규칙 기반 폴백 파서를 실행한다."""
        topic_mode = self._infer_topic_mode(instruction)
        persona_id = self._infer_persona_id(instruction, topic_mode)
        title = self._infer_title(instruction, topic_mode)
        keywords = self._extract_keywords(instruction)
        if not keywords:
            keywords = self._extract_keywords(title)
        if not keywords:
            keywords = [topic_mode, "블로그", "자동화"]

        return MagicInputParseResult(
            title=title,
            seed_keywords=keywords[:5],
            persona_id=persona_id,
            topic_mode=topic_mode,
            schedule_time=self._extract_schedule_time(instruction),
            confidence=0.45,
            parser_used="heuristic",
            raw={"source": "fallback"},
        )

    def _now_kst(self) -> datetime:
        """현재 시각을 KST timezone-aware datetime으로 반환한다."""
        try:
            from zoneinfo import ZoneInfo

            kst = ZoneInfo("Asia/Seoul")
        except Exception:
            kst = timezone(timedelta(hours=9))

        current = self.now_provider() if self.now_provider else datetime.now(kst)
        if current.tzinfo is None:
            current = current.replace(tzinfo=kst)
        return current.astimezone(kst)

    def _to_utc_iso(self, dt: datetime) -> str:
        """datetime을 UTC ISO 문자열로 변환한다."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self._now_kst().tzinfo)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _normalize_schedule_time(
        self,
        raw_value: Any,
        fallback_instruction: str = "",
    ) -> Optional[str]:
        """schedule_time 문자열을 UTC ISO로 정규화한다."""
        text = str(raw_value or "").strip()
        lowered = text.lower()
        if lowered in {"", "none", "null", "n/a"}:
            return self._extract_schedule_time(fallback_instruction)

        try:
            parsed = parse_iso(text)
            return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            # ISO가 아니면 자연어 파서를 시도한다.
            inferred = self._extract_schedule_time(text)
            if inferred:
                return inferred
            return self._extract_schedule_time(fallback_instruction)

    def _contains_schedule_hint(self, text: str) -> bool:
        """문장에 예약/시간 힌트가 있는지 판단한다."""
        if not text:
            return False
        hint_pattern = (
            r"(\d{4}[./-]\d{1,2}[./-]\d{1,2}|"
            r"\d{1,2}\s*:\s*\d{1,2}|"
            r"\d{1,2}\s*시|"
            r"(오늘|내일|모레|글피|오전|오후|아침|점심|저녁|밤|새벽)|"
            r"(월|화|수|목|금|토|일)요일)"
        )
        return bool(re.search(hint_pattern, text))

    def _extract_schedule_time(self, instruction: str) -> Optional[str]:
        """자연어 문장에서 예약 시간을 추출한다."""
        text = str(instruction or "").strip()
        if not text or not self._contains_schedule_hint(text):
            return None

        now_kst = self._now_kst()
        explicit = self._extract_explicit_datetime(text, now_kst)
        if explicit:
            return explicit

        relative = self._extract_relative_datetime(text, now_kst)
        if relative:
            return relative
        return None

    def _extract_explicit_datetime(self, text: str, now_kst: datetime) -> Optional[str]:
        """YYYY-MM-DD 형식의 명시적 날짜/시간을 파싱한다."""
        iso_like = re.search(
            r"(\d{4}-\d{1,2}-\d{1,2}(?:[ T]\d{1,2}:\d{1,2}(?::\d{1,2})?)?(?:Z|[+-]\d{2}:\d{2}))",
            text,
        )
        if iso_like:
            try:
                parsed = parse_iso(iso_like.group(1))
                return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                pass

        date_match = re.search(r"(?P<year>\d{4})[./-](?P<month>\d{1,2})[./-](?P<day>\d{1,2})", text)
        if not date_match:
            return None

        try:
            year = int(date_match.group("year"))
            month = int(date_match.group("month"))
            day = int(date_match.group("day"))
        except (TypeError, ValueError):
            return None

        hour, minute = self._extract_time_components(text)
        if hour is None:
            hour, minute = 9, 0

        try:
            dt = datetime(
                year=year,
                month=month,
                day=day,
                hour=hour,
                minute=minute,
                tzinfo=now_kst.tzinfo,
            )
        except ValueError:
            return None
        return self._to_utc_iso(dt)

    def _extract_relative_datetime(self, text: str, now_kst: datetime) -> Optional[str]:
        """상대 표현(내일/모레/월요일/오후 3시 등)에서 시각을 계산한다."""
        day_offset: Optional[int] = None
        for token, offset in _RELATIVE_DAY_OFFSETS.items():
            if token in text:
                day_offset = offset
                break

        weekday_target: Optional[int] = None
        weekday_match = re.search(r"(월|화|수|목|금|토|일)요일", text)
        if weekday_match:
            weekday_target = _WEEKDAY_MAP.get(weekday_match.group(1))

        hour, minute = self._extract_time_components(text)
        if hour is None:
            # 시간 지정이 없으면 아침 기본값 사용
            if day_offset is not None or weekday_target is not None:
                hour, minute = 9, 0
            else:
                return None

        base_date = now_kst.date()
        if day_offset is not None:
            base_date = base_date + timedelta(days=day_offset)
        elif weekday_target is not None:
            days_ahead = (weekday_target - now_kst.weekday()) % 7
            if "다음주" in text:
                days_ahead += 7
            if days_ahead == 0:
                candidate_today = datetime.combine(
                    base_date,
                    datetime.min.time(),
                    tzinfo=now_kst.tzinfo,
                ).replace(hour=hour, minute=minute)
                if candidate_today <= now_kst:
                    days_ahead = 7
            base_date = base_date + timedelta(days=days_ahead)
        else:
            candidate_today = datetime.combine(
                base_date,
                datetime.min.time(),
                tzinfo=now_kst.tzinfo,
            ).replace(hour=hour, minute=minute)
            if candidate_today <= now_kst:
                base_date = base_date + timedelta(days=1)

        scheduled = datetime.combine(
            base_date,
            datetime.min.time(),
            tzinfo=now_kst.tzinfo,
        ).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return self._to_utc_iso(scheduled)

    def _extract_time_components(self, text: str) -> tuple[Optional[int], int]:
        """문장에서 시/분을 추출한다."""
        lowered = text.lower()
        hour: Optional[int] = None
        minute = 0

        colon_match = re.search(r"(?P<hour>\d{1,2})\s*:\s*(?P<minute>\d{1,2})", lowered)
        if colon_match:
            hour = int(colon_match.group("hour"))
            minute = int(colon_match.group("minute"))
        else:
            korean_match = re.search(r"(?P<hour>\d{1,2})\s*시(?:\s*(?P<minute>\d{1,2})\s*분?)?", lowered)
            if korean_match:
                hour = int(korean_match.group("hour"))
                minute = int(korean_match.group("minute") or 0)

        if hour is None:
            if any(token in lowered for token in ("새벽",)):
                hour, minute = 6, 0
            elif any(token in lowered for token in ("아침", "오전")):
                hour, minute = 9, 0
            elif "점심" in lowered:
                hour, minute = 12, 0
            elif "오후" in lowered:
                hour, minute = 15, 0
            elif "저녁" in lowered:
                hour, minute = 19, 0
            elif "밤" in lowered:
                hour, minute = 21, 0
            else:
                return None, 0

        is_pm = any(token in lowered for token in ("오후", "저녁", "밤", "pm"))
        is_am = any(token in lowered for token in ("오전", "아침", "새벽", "am"))
        if is_pm and hour < 12:
            hour += 12
        if is_am and hour == 12:
            hour = 0

        if hour < 0 or hour > 23:
            return None, 0
        minute = max(0, min(59, minute))
        return hour, minute

    def _infer_topic_mode(self, text: str) -> str:
        """문장에서 토픽 모드를 추론한다."""
        lowered = text.lower()
        best_topic = "cafe"
        best_score = -1
        for topic_name, keywords in _TOPIC_KEYWORDS.items():
            score = sum(1 for keyword in keywords if keyword.lower() in lowered)
            if score > best_score:
                best_topic = topic_name
                best_score = score
        return normalize_topic_mode(best_topic)

    def _infer_persona_id(self, text: str, topic_mode: str) -> str:
        """문장에서 페르소나 ID를 추론한다."""
        matched = re.search(r"\b(P[1-4])\b", text, flags=re.IGNORECASE)
        if matched:
            return matched.group(1).upper()
        return _TOPIC_TO_PERSONA.get(topic_mode, "P1")

    def _infer_title(self, text: str, topic_mode: str) -> str:
        """문장에서 제목 후보를 생성한다."""
        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned:
            return "자동 블로그 초안"

        # 명령형 접미사는 제거해 제목 노이즈를 줄인다.
        cleaned = re.sub(r"(써줘요?|작성해줘요?|예약해줘요?|올려줘요?)$", "", cleaned).strip()
        candidate = cleaned.split(",")[0].strip()
        if len(candidate) < 8:
            topic_label = {
                "cafe": "카페/맛집",
                "it": "IT/자동화",
                "parenting": "육아",
                "finance": "경제/재테크",
            }.get(topic_mode, "라이프")
            candidate = f"{topic_label} 인사이트 정리"
        return candidate[:80]

    def _extract_keywords(self, text: str) -> List[str]:
        """문장에서 키워드 후보를 추출한다."""
        tokens = re.findall(r"[가-힣A-Za-z0-9]{2,20}", text)
        keywords: List[str] = []
        for token in tokens:
            normalized = token.strip()
            if not normalized:
                continue
            if normalized.lower() in _STOPWORDS:
                continue
            if normalized in keywords:
                continue
            keywords.append(normalized)
        return keywords[:5]
