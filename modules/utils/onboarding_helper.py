import json
from typing import Dict, List, Tuple

from server.schemas.onboarding import PersonaLabRequest, ScheduleAllocationItem
from modules.constants import DEFAULT_FALLBACK_CATEGORY as _DEFAULT_FALLBACK_CATEGORY
from modules.persona.questionnaire import (
    QUESTIONNAIRE_VERSION,
    score_questionnaire_answers,
)

_VALID_MBTI_CODES = {
    "INTJ", "INTP", "ENTJ", "ENTP",
    "INFJ", "INFP", "ENFJ", "ENFP",
    "ISTJ", "ISFJ", "ESTJ", "ESFJ",
    "ISTP", "ISFP", "ESTP", "ESFP",
}

_MBTI_CATEGORY_MAP: Dict[str, List[str]] = {
    # 分析型 (Analyst)
    "INTJ": ["자기계발", "IT 기술", "서평"],
    "INTP": ["IT 리뷰", "과학/기술", "게임 리뷰"],
    "ENTJ": ["재테크", "리더십", "경제 브리핑"],
    "ENTP": ["창업 비즈니스", "사이드 프로젝트", "트렌드 분석"],
    
    # 외교형 (Diplomat)
    "INFJ": ["에세이", "심리학", "자기계발"],
    "INFP": ["감성 에세이", "문학 리뷰", "예술/디자인"],
    "ENFJ": ["교육/강연", "자기계발", "인간관계"],
    "ENFP": ["일상 브이로그", "여행지 추천", "취미 생활"],

    # 관리자형 (Sentinel)
    "ISTJ": ["재테크", "자격증 공부", "경제 브리핑"],
    "ISFJ": ["요리 레시피", "육아 일기", "살림 노하우"],
    "ESTJ": ["부동산 투자", "업무 생산성", "주식 공부"],
    "ESFJ": ["맛집 투어", "육아 일기", "가족 생활"],

    # 탐험가형 (Explorer)
    "ISTP": ["전자기기 리뷰", "DIY/공예", "자동차"],
    "ISFP": ["인테리어", "카페 투어", "예술/디자인"],
    "ESTP": ["스포츠/운동", "아웃도어", "주식 투자"],
    "ESFP": ["패션/뷰티", "맛집 투어", "일상 기록"],
}

_MBTI_LETTER_DELTAS: Dict[str, Dict[str, int]] = {
    "E": {"distance": 10, "density": -2},
    "I": {"distance": -10, "density": 2},
    "S": {"evidence": 10, "density": 4},
    "N": {"evidence": -6, "density": 8},
    "T": {"criticism": 10},
    "F": {"criticism": -10},
    "J": {"structure": 10, "density": 4},
    "P": {"structure": -10, "density": -4},
}

_INTEREST_CATEGORY_MAP: Dict[str, str] = {
    "카페": "카페 운영 노하우",
    "맛집": "맛집 탐방",
    "커피": "커피 기록",
    "개발": "IT 자동화",
    "코딩": "개발 메모",
    "ai": "AI 활용",
    "육아": "육아 일기",
    "아이": "아이 성장 기록",
    "경제": "경제 브리핑",
    "주식": "투자 메모",
    "재테크": "재테크 노트",
}


def mask_secret(value: str) -> str:
    """민감 정보를 부분 마스킹한다. (앞 4자리 노출 + 나머지 *)"""
    stripped = str(value or "").strip()
    if len(stripped) <= 4:
        return "****"
    return stripped[:4] + "*" * (len(stripped) - 4)


def to_json_string(value: object) -> str:
    """값을 JSON 문자열로 직렬화한다."""
    return json.dumps(value, ensure_ascii=False)


def parse_json_list(raw_value: str) -> List[str]:
    """JSON 문자열 리스트를 안전 파싱한다."""
    try:
        decoded = json.loads(raw_value)
        if isinstance(decoded, list):
            normalized = []
            for item in decoded:
                text = str(item).strip()
                if text and text not in normalized:
                    normalized.append(text)
            return normalized
    except Exception:
        pass
    return []


def clamp_score(value: int) -> int:
    """점수를 0~100 범위로 제한한다."""
    return max(0, min(100, int(value)))


def normalize_mbti(raw_value: str) -> str:
    """MBTI 코드를 표준화한다."""
    normalized = str(raw_value or "").strip().upper()
    return normalized if normalized in _VALID_MBTI_CODES else ""


def calculate_mbti_weight(confidence: int) -> float:
    """MBTI 보정 가중치(10~20%)를 계산한다."""
    normalized_confidence = max(0, min(100, int(confidence)))
    return 0.10 + (normalized_confidence / 100.0) * 0.10


def build_mbti_prior_scores(mbti_code: str) -> Dict[str, int]:
    """MBTI로부터 5차원 prior 점수를 계산한다."""
    base = {
        "structure": 50,
        "evidence": 50,
        "distance": 50,
        "criticism": 50,
        "density": 50,
    }
    for letter in mbti_code:
        for dimension, delta in _MBTI_LETTER_DELTAS.get(letter, {}).items():
            base[dimension] = clamp_score(base[dimension] + delta)
    return base


def blend_scores_with_mbti(
    questionnaire_scores: Dict[str, int],
    *,
    mbti_code: str,
    mbti_enabled: bool,
    mbti_confidence: int,
) -> Tuple[Dict[str, int], Dict[str, object]]:
    """질문지 점수와 MBTI prior를 혼합한다."""
    base_scores = {
        key: clamp_score(value)
        for key, value in questionnaire_scores.items()
    }
    if not mbti_enabled:
        return base_scores, {
            "mbti_applied": False,
            "questionnaire_weight": 1.0,
            "mbti_weight": 0.0,
            "mbti_confidence": 0,
            "reason": "disabled",
            "questionnaire_scores": base_scores,
            "mbti_prior_scores": {},
            "final_scores": base_scores,
            "mbti_deltas": {key: 0 for key in base_scores.keys()},
        }

    normalized_mbti = normalize_mbti(mbti_code)
    if not normalized_mbti:
        return base_scores, {
            "mbti_applied": False,
            "questionnaire_weight": 1.0,
            "mbti_weight": 0.0,
            "mbti_confidence": 0,
            "reason": "invalid_or_empty_mbti",
            "questionnaire_scores": base_scores,
            "mbti_prior_scores": {},
            "final_scores": base_scores,
            "mbti_deltas": {key: 0 for key in base_scores.keys()},
        }

    mbti_weight = calculate_mbti_weight(mbti_confidence)
    questionnaire_weight = 1.0 - mbti_weight
    mbti_prior = build_mbti_prior_scores(normalized_mbti)

    blended: Dict[str, int] = {}
    for key, base_value in base_scores.items():
        prior_value = mbti_prior.get(key, 50)
        blended[key] = clamp_score(round(base_value * questionnaire_weight + prior_value * mbti_weight))

    return blended, {
        "mbti_applied": True,
        "questionnaire_weight": round(questionnaire_weight, 3),
        "mbti_weight": round(mbti_weight, 3),
        "mbti_confidence": max(0, min(100, int(mbti_confidence))),
        "reason": "applied",
        "questionnaire_scores": base_scores,
        "mbti_prior_scores": mbti_prior,
        "final_scores": blended,
        "mbti_deltas": {
            key: blended[key] - base_scores[key]
            for key in blended.keys()
        },
    }

def recommend_categories(interests: List[str], mbti: str = "", age_group: str = "", gender: str = "") -> List[str]:
    """관심사 기반 카테고리 추천을 생성한다."""
    categories: List[str] = []
    for interest in interests:
        cleaned = str(interest).strip()
        if not cleaned:
            continue
        matched = None
        lowered = cleaned.lower()
        for keyword, category_name in _INTEREST_CATEGORY_MAP.items():
            if keyword.lower() in lowered:
                matched = category_name
                break
        if matched is None:
            matched = f"{cleaned} 이야기"
        if matched not in categories:
            categories.append(matched)
            
    # MBTI 기반 추천
    if mbti:
        mbti_upper = mbti.upper()
        if mbti_upper in _MBTI_CATEGORY_MAP:
            for cat in _MBTI_CATEGORY_MAP[mbti_upper]:
                if cat not in categories:
                    categories.append(cat)
                    if len(categories) >= 4:
                        break
                        
    # 연령/성별 기반 약간의 보정 (필요시)
    if age_group == "20대" and "패션/뷰티" not in categories and gender == "여성":
        categories.append("패션/뷰티")
    elif age_group == "30대" and "육아 일기" not in categories and gender != "남성":
        categories.append("육아 일기")
    elif age_group == "40대" and "재테크" not in categories:
        categories.append("재테크")
        
    if _DEFAULT_FALLBACK_CATEGORY not in categories:
        categories.append(_DEFAULT_FALLBACK_CATEGORY)
    return categories[:5]  # 최대 5개까지만 추천


def infer_topic_mode(category_name: str) -> str:
    """카테고리 이름에서 토픽 모드를 추정한다."""
    lowered = str(category_name).strip().lower()
    if any(token in lowered for token in ("경제", "finance", "economy", "투자", "주식", "재테크")):
        return "finance"
    if any(token in lowered for token in ("it", "개발", "코드", "자동화", "ai", "테크")):
        return "it"
    if any(token in lowered for token in ("육아", "아이", "부모", "가정", "parenting", "family")):
        return "parenting"
    return "cafe"


def normalize_topic_mode(raw_mode: str, fallback_category: str = "") -> str:
    """토픽 모드를 허용 범위(cafe/it/parenting/finance)로 정규화한다."""
    lowered = str(raw_mode).strip().lower()
    if lowered == "economy":
        return "finance"
    if lowered in {"cafe", "it", "parenting", "finance"}:
        return lowered
    return infer_topic_mode(fallback_category or raw_mode)


def build_default_allocations(categories: List[str], daily_posts_target: int) -> List[ScheduleAllocationItem]:
    """카테고리 목록 기반 기본 할당량을 생성한다."""
    normalized_categories = [value for value in categories if str(value).strip()]
    if not normalized_categories:
        normalized_categories = [_DEFAULT_FALLBACK_CATEGORY]

    buckets = [
        ScheduleAllocationItem(
            category=category_name,
            topic_mode=infer_topic_mode(category_name),
            count=0,
        )
        for category_name in normalized_categories
    ]
    for index in range(max(0, daily_posts_target)):
        target_index = index % len(buckets)
        buckets[target_index].count += 1
    return buckets


def normalize_allocations(
    requested: List[ScheduleAllocationItem],
    daily_posts_target: int,
    fallback_categories: List[str],
) -> List[ScheduleAllocationItem]:
    """요청된 할당량을 정규화해 정확히 daily_posts_target에 맞춘다."""
    items: List[ScheduleAllocationItem] = []
    for item in requested:
        category_name = str(item.category).strip()
        if not category_name:
            continue
        topic_mode = normalize_topic_mode(item.topic_mode, category_name)
        items.append(
            ScheduleAllocationItem(
                category=category_name,
                topic_mode=topic_mode,
                count=max(0, int(item.count)),
                percentage=item.percentage,
            )
        )

    if not items:
        return build_default_allocations(fallback_categories, daily_posts_target)

    total = sum(item.count for item in items)
    if total <= 0:
        # percentage가 설정된 항목이 있으면 count만 재분배하고 원본을 유지한다.
        has_pct = any(item.percentage is not None and item.percentage > 0 for item in items)
        if has_pct:
            result = build_default_allocations([item.category for item in items], daily_posts_target)
            # percentage를 원본에서 복사해 보존
            pct_map = {item.category: item.percentage for item in items}
            mode_map = {item.category: item.topic_mode for item in items}
            for r in result:
                r.percentage = pct_map.get(r.category)
                r.topic_mode = mode_map.get(r.category, r.topic_mode)
            return result
        return build_default_allocations([item.category for item in items], daily_posts_target)

    if total < daily_posts_target:
        short = daily_posts_target - total
        items[0].count += short
        return items

    if total > daily_posts_target:
        overflow = total - daily_posts_target
        # 뒤에서부터 차감해 앞쪽 우선순위를 최대한 유지한다.
        for item in reversed(items):
            if overflow <= 0:
                break
            deductible = min(item.count, overflow)
            item.count -= deductible
            overflow -= deductible
        # percentage가 있는 항목은 count=0이어도 살려둔다.
        return [
            item for item in items 
            if item.count > 0 or (item.percentage is not None and item.percentage > 0)
        ]

    return items


def bucket_score(score: int, labels: List[str]) -> str:
    """0~100 점수를 3단계 버킷 라벨로 변환한다."""
    if score <= 33:
        return labels[0]
    if score <= 66:
        return labels[1]
    return labels[2]


def resolve_questionnaire_scores(request: PersonaLabRequest) -> Tuple[Dict[str, int], Dict[str, object]]:
    """요청 페이로드에서 최종 질문지 점수를 산출한다."""
    default_scores = {
        "structure": request.structure_score,
        "evidence": request.evidence_score,
        "distance": request.distance_score,
        "criticism": request.criticism_score,
        "density": request.density_score,
    }

    answer_pairs = [
        (item.question_id, item.option_id)
        for item in request.questionnaire_answers
    ]
    if not answer_pairs:
        return default_scores, {
            "version": request.questionnaire_version or QUESTIONNAIRE_VERSION,
            "source": "manual_slider",
            "scores": default_scores,
            "answered_count": 0,
            "total_questions": 0,
            "completion_ratio": 0.0,
            "dimension_confidence": {key: 0.0 for key in default_scores.keys()},
            "resolved_answers": [],
            "missing_question_ids": [],
        }

    scored = score_questionnaire_answers(answer_pairs)
    scored_map = scored.get("scores", {})
    final_scores = {
        "structure": clamp_score(int(scored_map.get("structure", 50))),
        "evidence": clamp_score(int(scored_map.get("evidence", 50))),
        "distance": clamp_score(int(scored_map.get("distance", 50))),
        "criticism": clamp_score(int(scored_map.get("criticism", 50))),
        "density": clamp_score(int(scored_map.get("density", 50))),
    }
    return final_scores, {
        **scored,
        "source": "questionnaire",
        "requested_version": request.questionnaire_version or QUESTIONNAIRE_VERSION,
    }


def compile_voice_profile(request: PersonaLabRequest) -> Dict[str, object]:
    """슬라이더 점수를 Voice_Profile로 변환한다."""
    questionnaire_scores, questionnaire_meta = resolve_questionnaire_scores(request)
    final_scores, blending_meta = blend_scores_with_mbti(
        questionnaire_scores,
        mbti_code=request.mbti,
        mbti_enabled=request.mbti_enabled,
        mbti_confidence=request.mbti_confidence,
    )
    mbti_applied = bool(blending_meta.get("mbti_applied", False))
    normalized_mbti = normalize_mbti(request.mbti) if mbti_applied else ""

    structure_mode = "top_down" if final_scores["structure"] >= 50 else "bottom_up"
    evidence_mode = "objective" if final_scores["evidence"] >= 50 else "subjective"

    return {
        "version": "v1",
        "mbti": normalized_mbti,
        "mbti_enabled": mbti_applied,
        "mbti_confidence": int(blending_meta.get("mbti_confidence", 0)),
        "blending": blending_meta,
        "age_group": request.age_group,
        "gender": request.gender,
        "structure": structure_mode,
        "evidence": evidence_mode,
        "distance": bucket_score(
            final_scores["distance"],
            ["authoritative", "peer", "inspiring"],
        ),
        "criticism": bucket_score(
            final_scores["criticism"],
            ["avoidant", "mitigated", "direct"],
        ),
        "density": bucket_score(
            final_scores["density"],
            ["light", "balanced", "dense"],
        ),
        "style_strength": request.style_strength,
        "scores": final_scores,
        "questionnaire_scores": questionnaire_scores,
        "questionnaire_meta": questionnaire_meta,
    }
