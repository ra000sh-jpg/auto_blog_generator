"""매크로 수치 해석 생성기."""

from __future__ import annotations

from typing import Any, Dict, List


class MacroInsightGenerator:
    """정부 자료 수치를 블로그 관점으로 해석한다."""

    def generate(self, *, title: str, metrics_json: Dict[str, Any]) -> Dict[str, Any]:
        """핵심 수치 기반 인사이트를 생성한다."""
        metrics = list(metrics_json.get("metrics", []) or [])
        by_key = dict(metrics_json.get("by_key", {}) or {})
        positive = [item for item in metrics if str(item.get("value", "")).startswith("+")]
        negative = [item for item in metrics if str(item.get("value", "")).startswith("-")]

        key_drivers = self._labels(positive[:4])
        weak_points = self._labels(negative[:4])
        summary = self._build_summary(title=title, by_key=by_key, key_drivers=key_drivers, weak_points=weak_points)

        return {
            "summary": summary,
            "keyDrivers": key_drivers,
            "weakPoints": weak_points,
            "investmentAngle": self._investment_angle(key_drivers, weak_points),
            "smallBusinessAngle": (
                "수출 지표가 개선되어도 내수 체감경기가 바로 좋아진다고 단정하기는 어렵습니다. "
                "생활인과 자영업자 관점에서는 소비, 고용, 금리 부담을 함께 확인해야 합니다."
            ),
            "riskFactors": self._risk_factors(by_key, weak_points),
            "nextWatch": self._next_watch(key_drivers, weak_points),
            "philosophyFrame": (
                "정보를 더 모으는 것보다 먼저 해야 할 일은 기준을 세우는 것입니다. "
                "수출입 숫자는 시장의 질서와 혼돈을 동시에 보여주므로, 우리는 좋은 뉴스와 나쁜 뉴스를 "
                "단순히 나누기보다 무엇이 구조이고 무엇이 일시적 변화인지 함께 공부해야 합니다."
            ),
        }

    def _build_summary(
        self,
        *,
        title: str,
        by_key: Dict[str, Any],
        key_drivers: List[str],
        weak_points: List[str],
    ) -> str:
        export_value = by_key.get("export_growth_yoy", "")
        import_value = by_key.get("import_growth_yoy", "")
        balance = by_key.get("trade_balance", "")
        parts = [f"{title} 자료는 수출입 흐름을 다시 점검하게 만드는 발표입니다."]
        if export_value:
            parts.append(f"수출 증감률은 {export_value}로 확인됩니다.")
        if import_value:
            parts.append(f"수입 증감률은 {import_value}입니다.")
        if balance:
            parts.append(f"무역수지는 {balance}로 나타났습니다.")
        if key_drivers:
            parts.append(f"강한 축은 {', '.join(key_drivers)}입니다.")
        if weak_points:
            parts.append(f"약한 축은 {', '.join(weak_points)}입니다.")
        return " ".join(parts)

    def _investment_angle(self, key_drivers: List[str], weak_points: List[str]) -> str:
        if any("반도체" in item for item in key_drivers):
            return (
                "투자 관점에서는 반도체 수출 회복이 AI 투자 사이클, 미국 기술주, 한국 대형 수출주의 연결고리인지 "
                "확인할 필요가 있습니다. 다만 특정 종목 매수 판단이 아니라 환율, 외국인 수급, 다음 달 수출 지속성을 함께 봐야 합니다."
            )
        if key_drivers:
            return (
                f"투자 관점에서는 {', '.join(key_drivers[:2])} 흐름이 일회성인지 반복되는 수요인지 확인하는 것이 우선입니다. "
                "좋은 숫자가 곧바로 좋은 수익률을 의미하지는 않습니다."
            )
        return "투자 관점에서는 숫자보다 기준이 먼저입니다. 확인 가능한 강한 축이 보일 때까지 포지션 확대보다 관찰이 우선입니다."

    def _risk_factors(self, by_key: Dict[str, Any], weak_points: List[str]) -> List[str]:
        risks = ["환율", "미국 금리", "중국 수요", "유가"]
        if weak_points:
            risks.insert(0, f"{weak_points[0]} 약세 지속 여부")
        if "trade_balance" not in by_key:
            risks.append("무역수지 확인 필요")
        return risks[:6]

    def _next_watch(self, key_drivers: List[str], weak_points: List[str]) -> List[str]:
        watches = ["다음 달 수출 증가율", "무역수지 지속성", "대미/대중 수출 방향", "환율과 외국인 수급"]
        if key_drivers:
            watches.insert(0, f"{key_drivers[0]} 증가세 지속 여부")
        if weak_points:
            watches.append(f"{weak_points[0]} 회복 여부")
        return watches[:6]

    def _labels(self, metrics: List[Dict[str, Any]]) -> List[str]:
        labels = []
        for item in metrics:
            label = str(item.get("label", "") or "").replace(" 증감률", "").strip()
            if label and label not in labels:
                labels.append(label)
        return labels
