"""플랫폼별 SEO 유입 전략 정의.

각 플랫폼(네이버, 티스토리 등)은 서로 다른 검색 알고리즘과
유입 최적화 규칙을 가진다.

네이버:  C-Rank + D.I.A.+ 알고리즘 → 태그·카테고리·본문 키워드 밀도 중심
티스토리: Google E-E-A-T → 제목 구조·스키마 마크업·롱테일 키워드 중심
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PlatformInFlowStrategy:
    """플랫폼 유입 전략 규칙 묶음."""

    platform: str

    # 제목 제약
    title_max_chars: int = 60
    title_keyword_placement: str = "first_50_chars"  # first_30_chars | first_50_chars | anywhere

    # 본문 구조
    h2_count_min: int = 3
    h2_count_max: int = 6
    recommended_body_chars: int = 2000  # 한국어 기준

    # 키워드 밀도 (0.0 ~ 1.0)
    keyword_density_target: float = 0.015  # 1.5 %

    # 태그 규칙
    tag_count_min: int = 5
    tag_count_max: int = 20
    tag_language: str = "korean_first"  # korean_first | mixed | english_first

    # 카테고리
    category_required: bool = False
    default_category: str = ""

    # 알고리즘 힌트 (LLM 프롬프트 삽입용)
    algorithm_signals: List[str] = field(default_factory=list)

    # 추가 메타 기능
    schema_markup: bool = False
    open_graph: bool = False
    image_required: bool = False

    # 발행 주기 권고
    recommended_post_frequency: str = "weekly"  # daily | weekly | biweekly

    # LLM 프롬프트 인젝션용 규칙 문자열
    seo_instructions: str = ""

    def tag_count_target(self) -> int:
        """태그 목표 개수 (최소·최대 중간값)."""
        return (self.tag_count_min + self.tag_count_max) // 2

    def to_prompt_snippet(self) -> str:
        """콘텐츠 생성 프롬프트에 삽입할 SEO 지침 스니펫."""
        lines = [
            f"## 플랫폼 SEO 지침 ({self.platform})",
            f"- 제목 길이: 최대 {self.title_max_chars}자",
            f"- 주 키워드 위치: 제목 앞부분 포함",
            f"- H2 헤딩: {self.h2_count_min}~{self.h2_count_max}개",
            f"- 권장 본문 길이: {self.recommended_body_chars}자 이상",
            f"- 키워드 밀도: {self.keyword_density_target * 100:.1f}%",
        ]
        if self.algorithm_signals:
            lines.append(f"- 알고리즘 신호: {', '.join(self.algorithm_signals)}")
        if self.seo_instructions:
            lines.append(self.seo_instructions)
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 플랫폼별 기본 전략 레지스트리
# ─────────────────────────────────────────────────────────────────────────────

_NAVER_STRATEGY = PlatformInFlowStrategy(
    platform="naver",
    title_max_chars=40,
    title_keyword_placement="first_30_chars",
    h2_count_min=3,
    h2_count_max=5,
    recommended_body_chars=2000,
    keyword_density_target=0.015,
    tag_count_min=10,
    tag_count_max=20,
    tag_language="korean_first",
    category_required=True,
    algorithm_signals=["C-Rank", "D.I.A+"],
    schema_markup=False,
    open_graph=False,
    image_required=True,
    recommended_post_frequency="weekly",
    seo_instructions=(
        "- 네이버 C-Rank를 위해 꾸준한 발행과 댓글 유도 문장 포함\n"
        "- D.I.A+ 개선을 위해 구체적 수치·경험담·개인 의견 포함\n"
        "- 정확한 검색어와 일치하는 태그 사용 (띄어쓰기 없이)\n"
        "- 첫 문단에 주 키워드 자연스럽게 포함"
    ),
)

_TISTORY_STRATEGY = PlatformInFlowStrategy(
    platform="tistory",
    title_max_chars=60,
    title_keyword_placement="first_50_chars",
    h2_count_min=3,
    h2_count_max=6,
    recommended_body_chars=2500,
    keyword_density_target=0.012,
    tag_count_min=5,
    tag_count_max=10,
    tag_language="mixed",
    category_required=False,
    algorithm_signals=["Google E-E-A-T"],
    schema_markup=True,
    open_graph=True,
    image_required=False,
    recommended_post_frequency="biweekly",
    seo_instructions=(
        "- Google E-E-A-T: 경험(Experience)·전문성(Expertise) 강조\n"
        "- 롱테일 키워드를 H2/H3 헤딩에 자연스럽게 포함\n"
        "- 카카오톡 공유를 위한 흥미로운 첫 문장\n"
        "- 메타 디스크립션에 사용할 2-3문장 요약 포함"
    ),
)

_STRATEGY_REGISTRY: Dict[str, PlatformInFlowStrategy] = {
    "naver": _NAVER_STRATEGY,
    "tistory": _TISTORY_STRATEGY,
}

# ─────────────────────────────────────────────────────────────────────────────
# TopicMode별 기본 카테고리 매핑
# ─────────────────────────────────────────────────────────────────────────────

# 네이버 블로그 주요 카테고리 (32개 중 주요 항목)
NAVER_TOPIC_CATEGORY_MAP: Dict[str, str] = {
    "cafe": "생활·노하우·쇼핑",
    "parenting": "출산·육아",
    "it": "IT·컴퓨터",
    "finance": "경제·비즈니스",
    "economy": "경제·비즈니스",  # finance 별칭
    # 추가 매핑 (필요시 확장)
    "travel": "여행·맛집",
    "health": "건강·의학",
    "cooking": "요리·레시피",
    "beauty": "패션·미용",
    "education": "교육·학문",
    "hobby": "취미·여가",
}

# 티스토리 카테고리 (사용자 정의 가능하므로 참고용)
TISTORY_TOPIC_CATEGORY_MAP: Dict[str, str] = {
    "cafe": "라이프스타일",
    "parenting": "육아",
    "it": "IT/테크",
    "finance": "경제/재테크",
    "economy": "경제/재테크",  # finance 별칭
    "travel": "여행",
    "health": "건강",
    "cooking": "요리",
    "beauty": "뷰티",
    "education": "교육",
    "hobby": "취미",
}

# 커스텀 전략 오버라이드 저장소 (런타임 수정용)
_CUSTOM_OVERRIDES: Dict[str, Dict] = {}


def get_platform_strategy(platform: str) -> PlatformInFlowStrategy:
    """플랫폼 이름으로 유입 전략을 반환한다.

    알 수 없는 플랫폼은 기본 네이버 전략을 반환한다.
    커스텀 오버라이드가 있으면 필드를 덮어쓴다.
    """
    base = _STRATEGY_REGISTRY.get(platform.lower(), _NAVER_STRATEGY)
    overrides = _CUSTOM_OVERRIDES.get(platform.lower(), {})
    if not overrides:
        return base

    import dataclasses
    updated = dataclasses.replace(base, **overrides)
    return updated


def register_platform_strategy(strategy: PlatformInFlowStrategy) -> None:
    """새 플랫폼 전략을 레지스트리에 등록한다."""
    _STRATEGY_REGISTRY[strategy.platform.lower()] = strategy


def update_strategy_field(platform: str, field_name: str, value) -> None:
    """피드백 루프가 특정 전략 필드를 업데이트할 때 사용한다.

    변경 사항은 런타임 오버라이드로 저장되며 레지스트리 기본값은
    보존된다. 재시작 시 초기화된다 (영속화는 feedback_analyzer 담당).
    """
    _CUSTOM_OVERRIDES.setdefault(platform.lower(), {})[field_name] = value


def list_platforms() -> List[str]:
    """등록된 플랫폼 목록을 반환한다."""
    return sorted(_STRATEGY_REGISTRY.keys())


def get_category_for_topic(
    topic_mode: str,
    platform: str = "naver",
    fallback: str = "생활·노하우·쇼핑",
) -> str:
    """TopicMode에 맞는 플랫폼 카테고리를 반환한다.

    Args:
        topic_mode: 주제 모드 (cafe, parenting, it, finance 등)
        platform: 플랫폼 (naver, tistory)
        fallback: 매핑이 없을 때 기본값

    Returns:
        해당 플랫폼의 카테고리 문자열
    """
    topic_key = topic_mode.lower().strip()
    if topic_key == "economy":
        topic_key = "finance"

    if platform.lower() == "tistory":
        return TISTORY_TOPIC_CATEGORY_MAP.get(topic_key, fallback)

    # 기본은 네이버
    return NAVER_TOPIC_CATEGORY_MAP.get(topic_key, fallback)
