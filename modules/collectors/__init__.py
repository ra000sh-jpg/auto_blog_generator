"""데이터 수집 모듈 공개 API."""

__all__ = [
    "NaverDataLabCollector",
    "MetricsCollector",
    "PostMetric",
    "RssNewsCollector",
    "NaverSearchCollector",
    "NaverSearchItem",
    "BigKindsPublicCollector",
    "BigKindsIssue",
]


def __getattr__(name: str):
    """선택 의존성이 필요한 수집기를 지연 import한다."""

    if name == "NaverDataLabCollector":
        from .naver_datalab import NaverDataLabCollector

        return NaverDataLabCollector
    if name in {"MetricsCollector", "PostMetric"}:
        from .metrics_collector import MetricsCollector, PostMetric

        return {"MetricsCollector": MetricsCollector, "PostMetric": PostMetric}[name]
    if name == "RssNewsCollector":
        from .rss_news_collector import RssNewsCollector

        return RssNewsCollector
    if name in {"NaverSearchCollector", "NaverSearchItem"}:
        from .naver_search import NaverSearchCollector, NaverSearchItem

        return {
            "NaverSearchCollector": NaverSearchCollector,
            "NaverSearchItem": NaverSearchItem,
        }[name]
    if name in {"BigKindsPublicCollector", "BigKindsIssue"}:
        from .bigkinds_public import BigKindsIssue, BigKindsPublicCollector

        return {
            "BigKindsPublicCollector": BigKindsPublicCollector,
            "BigKindsIssue": BigKindsIssue,
        }[name]
    raise AttributeError(name)
