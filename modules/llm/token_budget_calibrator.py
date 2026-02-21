"""실측 토큰 기반 TOKEN_BUDGET 보정 유틸리티."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from ..automation.job_store import JobStore

_DEFAULT_BUDGET = {
    "parser": {"input": 450, "output": 180},
    "quality_step": {"input": 3600, "output": 2400},
    "voice_step": {"input": 2900, "output": 2200},
}


@dataclass
class TokenBudgetCalibrationResult:
    """토큰 보정 결과."""

    recommended: Dict[str, Dict[str, int]]
    observed_samples: Dict[str, int]
    min_samples: int
    used_rows: int


def calibrate_token_budget(
    job_store: JobStore,
    min_samples: int = 20,
    safety_margin: float = 1.25,
) -> TokenBudgetCalibrationResult:
    """job_metrics 실측치로 역할별 TOKEN_BUDGET 권장값을 계산한다."""
    safe_margin = max(1.0, float(safety_margin))
    min_count = max(1, int(min_samples))
    observed_samples: Dict[str, int] = {"parser": 0, "quality_step": 0, "voice_step": 0}
    recommended = {
        role: {"input": int(values["input"]), "output": int(values["output"])}
        for role, values in _DEFAULT_BUDGET.items()
    }

    with job_store.connection() as conn:
        rows = conn.execute(
            """
            SELECT
                metric_type,
                COUNT(*) AS samples,
                AVG(input_tokens) AS avg_input_tokens,
                AVG(output_tokens) AS avg_output_tokens
            FROM job_metrics
            WHERE metric_type IN ('parser', 'quality_step', 'voice_step')
              AND status IN ('ok', 'pass', 'success')
              AND (input_tokens > 0 OR output_tokens > 0)
            GROUP BY metric_type
            """
        ).fetchall()

    used_rows = len(rows)
    for row in rows:
        role = str(row["metric_type"])
        if role not in recommended:
            continue
        samples = int(row["samples"] or 0)
        observed_samples[role] = samples
        if samples < min_count:
            continue

        avg_input = float(row["avg_input_tokens"] or 0.0)
        avg_output = float(row["avg_output_tokens"] or 0.0)
        suggested_input = max(recommended[role]["input"], int(avg_input * safe_margin))
        suggested_output = max(recommended[role]["output"], int(avg_output * safe_margin))
        recommended[role] = {"input": suggested_input, "output": suggested_output}

    return TokenBudgetCalibrationResult(
        recommended=recommended,
        observed_samples=observed_samples,
        min_samples=min_count,
        used_rows=used_rows,
    )


def calibration_result_to_dict(result: TokenBudgetCalibrationResult) -> Dict[str, Any]:
    """보정 결과를 직렬화 가능한 dict로 변환한다."""
    return {
        "recommended": result.recommended,
        "observed_samples": result.observed_samples,
        "min_samples": result.min_samples,
        "used_rows": result.used_rows,
    }
