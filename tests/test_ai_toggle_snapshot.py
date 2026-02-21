from __future__ import annotations

from pathlib import Path

from modules.uploaders.playwright_publisher import PlaywrightPublisher


def test_ai_toggle_snapshot_detects_selected_toggle_inside_wrapper():
    """래퍼가 비활성이어도 내부 토글/버튼이 선택되면 ON으로 판정해야 한다."""
    snapshot = {
        "buttonClass": "se-set-ai-mark-button-wrapper",
        "wrapperClass": "se-set-ai-mark-button-wrapper",
        "markClass": "se-set-ai-mark-button se-is-selected",
        "toggleClass": "se-set-ai-mark-button-toggle se-is-selected",
        "buttonAriaChecked": "",
        "buttonAriaPressed": "",
        "buttonDataActive": "",
        "buttonChecked": None,
        "toggleAriaChecked": "",
        "toggleAriaPressed": "",
        "toggleDataActive": "",
        "toggleChecked": None,
        "wrapperAriaChecked": "",
        "wrapperAriaPressed": "",
        "wrapperDataActive": "",
        "wrapperChecked": None,
    }
    assert PlaywrightPublisher._is_ai_toggle_on_snapshot(snapshot) is True


def test_ai_toggle_snapshot_detects_off_when_no_signal():
    """선택 클래스/속성/checked 신호가 모두 없으면 OFF로 판정해야 한다."""
    snapshot = {
        "buttonClass": "se-set-ai-mark-button-wrapper",
        "wrapperClass": "se-set-ai-mark-button-wrapper",
        "markClass": "se-set-ai-mark-button",
        "toggleClass": "se-set-ai-mark-button-toggle",
        "buttonAriaChecked": "false",
        "buttonAriaPressed": "false",
        "buttonDataActive": "false",
        "buttonChecked": None,
        "toggleAriaChecked": "false",
        "toggleAriaPressed": "false",
        "toggleDataActive": "false",
        "toggleChecked": False,
        "wrapperAriaChecked": "false",
        "wrapperAriaPressed": "false",
        "wrapperDataActive": "false",
        "wrapperChecked": False,
    }
    assert PlaywrightPublisher._is_ai_toggle_on_snapshot(snapshot) is False


def test_ai_toggle_alert_message_includes_summary_counts():
    """텔레그램 경고 메시지에 사전/사후검증 요약 수치가 포함되어야 한다."""

    class FakeNotifier:
        enabled = True

        def __init__(self):
            self.messages = []

        def send_message_background(self, text, disable_notification=False):
            self.messages.append((text, disable_notification))

    publisher = PlaywrightPublisher(blog_id="toggle-summary-test")
    fake = FakeNotifier()
    publisher._telegram_notifier = fake
    publisher._ai_toggle_summary = {
        "prepublish": {"expected_on": 3, "verified_on": 2, "repaired": 1, "failed": 1},
        "postverify": {"expected_on": 3, "passed": 2, "failed": 1},
    }

    publisher._notify_ai_toggle_alert_background("🚨 [AI 토글 검증 실패]", ["- unresolved: a.png"])

    assert len(fake.messages) == 1
    text = fake.messages[0][0]
    assert "prepublish: expected=3 verified=2 repaired=1 failed=1" in text
    assert "postverify: expected=3 passed=2 failed=1" in text


def test_prune_old_debug_files_keeps_latest_n(tmp_path: Path):
    """디버그 파일 보관 정책이 최신 N개만 유지해야 한다."""
    for index in range(5):
        target = tmp_path / f"sample_{index}.png"
        target.write_text(f"payload-{index}", encoding="utf-8")

    PlaywrightPublisher._prune_old_debug_files(tmp_path, "*.png", keep=2)

    survivors = sorted(path.name for path in tmp_path.glob("*.png"))
    assert len(survivors) == 2
    assert survivors == ["sample_3.png", "sample_4.png"]
