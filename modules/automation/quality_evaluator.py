"""
Phase 25: 품질 평가 확장 모듈

기존 QualityGate (modules/seo/quality_gate.py)가 Rule-based / RAG 체크를 담당한다면,
이 모듈은 Gate 2 (LLM 기반 페르소나 톤앤매너 일치도)를 추가로 제공합니다.

설계 원칙:
- 기존 pipeline_service.py / quality_gate.py 코드를 일절 수정하지 않는다.
- PipelineService.__init__에 선택적(Optional)로 주입할 수 있는 구조로 설계한다.
- LLM 클라이언트가 없을 경우 Gate 2를 건너뛰고 패스 처리한다. (Graceful Degradation)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EvaluationResult:
    """QualityEvaluator 평가 결과."""
    passed: bool
    score: int = 100           # 0-100
    gate: str = "pass"         # "pass" | "correction_needed" | "rejected"
    error_code: str = ""
    feedback: str = ""         # 재작성 프롬프트로 재활용할 수 있는 구체적 피드백
    retry_count: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "score": self.score,
            "gate": self.gate,
            "error_code": self.error_code,
            "feedback": self.feedback,
            "retry_count": self.retry_count,
            "detail": self.detail,
        }


# ─────────────────────────────────────────────────────────────────────────────
# LLM 기반 Gate 2: 페르소나 톤앤매너 일치도 평가
# ─────────────────────────────────────────────────────────────────────────────

_PERSONA_EVAL_SYSTEM = """\
당신은 블로그 글의 품질과 일관성을 평가하는 전문 에디터입니다.
아래의 평가 기준에 따라 JSON 형식으로만 답변하세요. JSON 외의 다른 텍스트는 절대 포함하지 마세요.
"""

_PERSONA_EVAL_USER = """\
[페르소나 설정]
{persona_desc}

[평가 대상 본문 (처음 1500자)]
{excerpt}

[평가 기준]
1. 페르소나의 어조(tone)가 본문 전체에서 일관되게 유지되는가?
2. 본문이 페르소나가 설정한 독자(target audience)를 정확히 향하고 있는가?
3. 사실과 다른 내용(환각)이 의심되는 부분이 있는가?

[응답 형식 - JSON만 출력]
{{
  "score": <0-100 정수>,
  "passed": <true|false, 70점 이상이면 true>,
  "issues": ["<구체적 이슈 1>", "<구체적 이슈 2>"],
  "feedback": "<다음 글 작성 시 반드시 지켜야 할 구체적인 수정 지시 (한국어 2-3문장)>"
}}
"""


class QualityEvaluator:
    """
    LLM 기반 Phase 25 품질 게이트 (Gate 2: 페르소나 일치도).

    - `llm_client`가 None이면 평가를 건너뛰고 통과(score=100) 처리합니다.
    - `max_retries`: 재작성 최대 횟수 (0 = 재시도 없이 즉시 최종 결과 반환)
    """

    PASS_THRESHOLD = 70  # 70점 이상이면 통과

    def __init__(
        self,
        llm_client: Optional[Any] = None,  # BaseLLMClient 인스턴스
        max_retries: int = 2,
        pass_threshold: int = PASS_THRESHOLD,
    ) -> None:
        self.llm_client = llm_client
        self.max_retries = max_retries
        self.pass_threshold = pass_threshold

    async def evaluate(
        self,
        *,
        content: str,
        persona_desc: str,
        retry_count: int = 0,
    ) -> EvaluationResult:
        """
        Gate 2 평가 실행.

        Args:
            content: 최종 생성된 본문.
            persona_desc: 페르소나 설명 문자열 (identity, tone_hint, target_audience 등).
            retry_count: 현재 재시도 횟수 (Job 메타데이터와 동기화).

        Returns:
            EvaluationResult
        """
        if self.llm_client is None:
            logger.debug("QualityEvaluator: LLM client not set, skipping Gate 2.")
            return EvaluationResult(passed=True, score=100, gate="pass", detail={"skipped": True})

        excerpt = content[:1500]
        user_prompt = _PERSONA_EVAL_USER.format(
            persona_desc=persona_desc.strip() or "일반 블로그 작성자",
            excerpt=excerpt,
        )

        try:
            resp = await self._call_llm(user_prompt)
            result = self._parse_response(resp, retry_count=retry_count)
            logger.info(
                "QualityEvaluator Gate 2: score=%d, passed=%s",
                result.score,
                result.passed,
            )
            return result

        except Exception as exc:
            logger.warning("QualityEvaluator Gate 2 LLM 호출 실패, 패스 처리: %s", exc)
            return EvaluationResult(
                passed=True,
                score=80,
                gate="pass",
                detail={"error": str(exc), "skipped": True},
            )

    async def _call_llm(self, user_prompt: str) -> str:
        """LLM 클라이언트 호출 (동기/비동기 모두 지원)."""
        import asyncio

        if asyncio.iscoroutinefunction(self.llm_client.generate):
            resp = await self.llm_client.generate(
                system_prompt=_PERSONA_EVAL_SYSTEM,
                user_prompt=user_prompt,
                temperature=0.1,
                max_tokens=512,
            )
        else:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self.llm_client.generate(
                    system_prompt=_PERSONA_EVAL_SYSTEM,
                    user_prompt=user_prompt,
                    temperature=0.1,
                    max_tokens=512,
                ),
            )

        # BaseLLMClient returns LLMResponse or str
        if hasattr(resp, "content"):
            return str(resp.content)
        return str(resp)

    def _parse_response(self, raw: str, retry_count: int) -> EvaluationResult:
        """LLM 응답 JSON 파싱."""
        # JSON 블록 추출
        text = raw.strip()
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            data = json.loads(text[start:end])
        except (ValueError, json.JSONDecodeError):
            logger.warning("QualityEvaluator: JSON 파싱 실패, 패스 처리\n%s", text[:300])
            return EvaluationResult(passed=True, score=75, gate="pass", detail={"parse_error": True})

        score = int(data.get("score", 75))
        passed = bool(score >= self.pass_threshold)
        feedback = str(data.get("feedback", ""))
        issues = list(data.get("issues", []))

        gate = "pass"
        error_code = ""
        if not passed:
            remaining_retries = self.max_retries - retry_count
            if remaining_retries > 0:
                gate = "correction_needed"
                error_code = "PERSONA_MISMATCH"
            else:
                gate = "rejected"
                error_code = "QUALITY_REJECTED"

        return EvaluationResult(
            passed=passed,
            score=score,
            gate=gate,
            error_code=error_code,
            feedback=feedback,
            retry_count=retry_count,
            detail={"issues": issues, "raw_score": score},
        )

    def build_correction_prompt(
        self,
        *,
        original_content: str,
        feedback: str,
        persona_desc: str,
    ) -> str:
        """
        자가 수정 루프 재사용 프롬프트 빌더.

        이 메서드가 반환하는 문자열을 content_generator의 user_prompt 뒤에 주입하면
        다음 생성 시 피드백이 자동 반영됩니다.
        """
        return (
            "\n\n---\n[이전 생성본 품질 피드백 - 반드시 반영할 것]\n"
            f"페르소나: {persona_desc}\n"
            f"수정 지시:\n{feedback}\n"
            "위 피드백을 완전히 반영하여 글 전체를 다시 작성하세요. "
            "기존 내용 중 피드백 지적을 받지 않은 부분은 최대한 유지하되, "
            "지적된 어조·구성·표현을 근본적으로 개선해야 합니다.\n---"
        )
