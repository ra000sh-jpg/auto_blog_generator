from modules.uploaders.editor_diagnostics import (
    SelectorGroupDiagnostic,
    evaluate_editor_diagnostics,
)


def _group(name: str, *, required: bool, visible_count: int) -> SelectorGroupDiagnostic:
    return SelectorGroupDiagnostic(
        name=name,
        required=required,
        selectors=(f".{name}",),
        matched_selectors=(f".{name}",) if visible_count > 0 else (),
        total_count=visible_count,
        visible_count=visible_count,
    )


def test_editor_diagnostics_healthy_when_required_selectors_visible():
    """필수 제목/본문 영역이 보이면 healthy로 판정한다."""
    report = evaluate_editor_diagnostics(
        current_url="https://blog.naver.com/test/postwrite",
        selector_groups=[
            _group("title_input", required=True, visible_count=1),
            _group("body_input", required=True, visible_count=1),
            _group("publish_button", required=False, visible_count=1),
            _group("image_controls", required=False, visible_count=1),
            _group("ai_toggle_controls", required=False, visible_count=1),
        ],
        dom_summary={"captcha_visible": False},
    )

    assert report.status == "healthy"
    assert report.failures == []


def test_editor_diagnostics_unhealthy_when_title_missing():
    """필수 제목 영역이 사라지면 업로드 전 중단 가능한 unhealthy가 된다."""
    report = evaluate_editor_diagnostics(
        current_url="https://blog.naver.com/test/postwrite",
        selector_groups=[
            _group("title_input", required=True, visible_count=0),
            _group("body_input", required=True, visible_count=1),
        ],
        dom_summary={},
    )

    assert report.status == "unhealthy"
    assert any("title_input" in item for item in report.failures)


def test_editor_diagnostics_detects_session_expired_url():
    """로그인 페이지로 튕기면 세션 만료로 실패 처리한다."""
    report = evaluate_editor_diagnostics(
        current_url="https://nid.naver.com/nidlogin.login",
        selector_groups=[
            _group("title_input", required=True, visible_count=1),
            _group("body_input", required=True, visible_count=1),
        ],
        dom_summary={},
    )

    assert report.status == "unhealthy"
    assert any("세션" in item for item in report.failures)
