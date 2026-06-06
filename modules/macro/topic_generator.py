"""매크로 문서 기반 블로그 후보 생성기."""

from __future__ import annotations

from typing import Any, Dict, List


class MacroBlogTopicGenerator:
    """문서 하나에서 여러 관점의 블로그 글 후보를 만든다."""

    def generate(
        self,
        *,
        document_title: str,
        metrics_json: Dict[str, Any],
        insight_json: Dict[str, Any],
        min_count: int = 3,
        max_count: int = 5,
    ) -> List[Dict[str, Any]]:
        """후보 제목/각도/독자층을 생성한다."""
        by_key = dict(metrics_json.get("by_key", {}) or {})
        key_drivers = list(insight_json.get("keyDrivers", []) or [])
        weak_points = list(insight_json.get("weakPoints", []) or [])
        candidates: List[Dict[str, Any]] = [
            {
                "title": f"{document_title} 정리: 숫자보다 먼저 봐야 할 기준",
                "angle": "월간 총정리",
                "target_reader": "경제 흐름을 처음 공부하는 투자 초심자",
                "outline_json": {
                    "sections": ["핵심 숫자", "강한 축과 약한 축", "초보자가 착각하기 쉬운 부분", "다음 달 체크포인트"],
                },
                "status": "needs_review",
            }
        ]

        if any("반도체" in item for item in key_drivers) or "industry_semiconductor_growth" in by_key:
            candidates.append(
                {
                    "title": "반도체 수출 회복이 AI 투자 사이클과 연결되는 이유",
                    "angle": "반도체/AI",
                    "target_reader": "반도체와 AI ETF 흐름을 공부하는 투자자",
                    "outline_json": {
                        "sections": ["반도체 수출 수치", "미국 AI 투자와의 연결", "한국 수출주 체크포인트"],
                    },
                    "status": "needs_review",
                }
            )

        if any("미국" in item for item in key_drivers) or "country_us_growth" in by_key:
            candidates.append(
                {
                    "title": "대미 수출 증가는 한국 경제에 어떤 의미를 줄까",
                    "angle": "미국 연결",
                    "target_reader": "미국 매크로와 한국 수출을 함께 보고 싶은 독자",
                    "outline_json": {
                        "sections": ["대미 수출 흐름", "미국 소비/투자와의 관계", "한국 ETF 관점"],
                    },
                    "status": "needs_review",
                }
            )

        if any("중국" in item for item in weak_points) or "country_china_growth" in by_key:
            candidates.append(
                {
                    "title": "대중국 수출 둔화는 일시적일까 구조 변화일까",
                    "angle": "중국 리스크",
                    "target_reader": "한국 수출 구조를 공부하는 투자자",
                    "outline_json": {
                        "sections": ["중국 수출 수치", "구조적 둔화 가능성", "다음 달 확인할 신호"],
                    },
                    "status": "needs_review",
                }
            )

        candidates.append(
            {
                "title": "수출은 좋아지는데 왜 체감경기는 바로 따뜻해지지 않을까",
                "angle": "생활경제",
                "target_reader": "자영업자와 생활경제에 관심 있는 독자",
                "outline_json": {
                    "sections": ["수출과 내수의 시간차", "소비/고용/금리의 영향", "생활인이 볼 체크리스트"],
                },
                "status": "needs_review",
            }
        )

        candidates.append(
            {
                "title": "좋은 경제 뉴스가 나와도 주가가 바로 오르지 않는 이유",
                "angle": "투자 철학",
                "target_reader": "투자 원칙을 세우고 싶은 초심자",
                "outline_json": {
                    "sections": ["좋은 뉴스와 가격의 차이", "기대와 현실", "정보보다 기준"],
                },
                "status": "needs_review",
            }
        )

        deduped: List[Dict[str, Any]] = []
        seen_titles: set[str] = set()
        for item in candidates:
            title = str(item.get("title", "")).strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            deduped.append(item)
        target_count = max(min_count, min(max_count, len(deduped)))
        return deduped[:target_count]
