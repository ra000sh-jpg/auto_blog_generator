"""정부기관 매크로 데이터 소스 설정."""

from __future__ import annotations

from .models import MacroSourceConfig


MOTIE_PRESS_RELEASE_URL = (
    "https://www.motir.go.kr/kor/article/ATCL3f49a5a8c?pageIndex=1&searchKeyword=%EC%88%98%EC%B6%9C%EC%9E%85"
)


DEFAULT_MACRO_SOURCE_CONFIGS = {
    "MOTIE": MacroSourceConfig(
        source="MOTIE",
        list_url=MOTIE_PRESS_RELEASE_URL,
        base_url="https://www.motir.go.kr",
        keywords=("수출입", "수출입동향", "수출입 동향", "수출 동향", "무역수지"),
        max_detail_fetch=10,
    ),
}


def get_macro_source_config(source: str) -> MacroSourceConfig:
    """소스 이름으로 설정을 반환한다."""
    normalized = str(source or "").strip().upper() or "MOTIE"
    if normalized not in DEFAULT_MACRO_SOURCE_CONFIGS:
        raise ValueError(f"Unsupported macro source: {source}")
    return DEFAULT_MACRO_SOURCE_CONFIGS[normalized]
