from __future__ import annotations

import asyncio
from pathlib import Path

from modules.evaluation.visual_evaluator import VisualQualityEvaluator


class _DummyVisionResponse:
    def __init__(self, content: str, model: str = "dummy-vlm") -> None:
        self.content = content
        self.model = model


class _DummyVLMClient:
    def __init__(self, content: str, provider_name: str = "dummy_vlm") -> None:
        self.model = "dummy-vlm"
        self._content = content
        self._provider_name = provider_name

    @property
    def provider_name(self) -> str:
        return self._provider_name

    async def generate_vision_with_retry(self, **kwargs):  # noqa: ANN003
        del kwargs
        return _DummyVisionResponse(content=self._content, model=self.model)


class _FailingVLMClient(_DummyVLMClient):
    async def generate_vision_with_retry(self, **kwargs):  # noqa: ANN003
        del kwargs
        raise RuntimeError("forced failure")


def test_parse_eval_response_clamps_and_limits_suggestions(tmp_path: Path):
    raw = """
    {
      "layout": 99,
      "readability": 20,
      "image_quality": 18,
      "visual_consistency": 12,
      "overall_impression": 16,
      "total_score": 999,
      "suggestions": ["a", "b", "c", "d"]
    }
    """
    evaluator = VisualQualityEvaluator(vlm_client=_DummyVLMClient(raw), screenshot_dir=str(tmp_path))
    parsed = evaluator._parse_eval_response(raw)
    assert parsed.layout == 20
    assert parsed.visual_consistency == 12
    assert parsed.total_score == (20 + 20 + 18 + 12 + 16)
    assert parsed.suggestions == ["a", "b", "c"]


def test_parse_eval_response_handles_invalid_json(tmp_path: Path):
    evaluator = VisualQualityEvaluator(vlm_client=_DummyVLMClient("not-json"), screenshot_dir=str(tmp_path))
    parsed = evaluator._parse_eval_response("not-json")
    assert parsed.total_score == 0
    assert parsed.error == "invalid json"


def test_evaluate_success_with_mocked_capture_and_resize(tmp_path: Path):
    raw = """
    {
      "layout": 16,
      "readability": 20,
      "image_quality": 17,
      "visual_consistency": 12,
      "overall_impression": 15,
      "total_score": 80,
      "suggestions": ["여백을 조금 더 확보하세요."]
    }
    """
    client = _DummyVLMClient(raw)
    evaluator = VisualQualityEvaluator(vlm_client=client, screenshot_dir=str(tmp_path))

    fake_image = tmp_path / "fake.png"
    fake_image.write_bytes(b"fake-image-bytes")

    async def _fake_capture(url: str, job_id: str) -> str:
        del url, job_id
        return str(fake_image)

    evaluator._capture_screenshot = _fake_capture  # type: ignore[method-assign]
    evaluator._resize_if_needed = lambda image_path: (image_path, "image/png")  # type: ignore[method-assign]

    result = asyncio.run(
        evaluator.evaluate(
            post_url="https://example.com/post/1",
            job_id="job-1",
        )
    )
    assert result.error == ""
    assert result.total_score == 80
    assert result.layout == 16
    assert result.model_used == "dummy-vlm"
    assert result.screenshot_path.endswith("fake.png")


def test_evaluate_uses_fallback_client_on_primary_failure(tmp_path: Path):
    raw = """
    {
      "layout": 15,
      "readability": 20,
      "image_quality": 18,
      "visual_consistency": 10,
      "overall_impression": 15,
      "total_score": 78,
      "suggestions": ["간격 조정"]
    }
    """
    primary = _FailingVLMClient(raw, provider_name="nvidia_vlm")
    fallback = _DummyVLMClient(raw, provider_name="gemini_vlm")
    evaluator = VisualQualityEvaluator(
        vlm_client=primary,
        fallback_clients=[fallback],
        screenshot_dir=str(tmp_path),
    )

    fake_image = tmp_path / "fallback.png"
    fake_image.write_bytes(b"fake-image-bytes")

    async def _fake_capture(url: str, job_id: str) -> str:
        del url, job_id
        return str(fake_image)

    evaluator._capture_screenshot = _fake_capture  # type: ignore[method-assign]
    evaluator._resize_if_needed = lambda image_path: (image_path, "image/png")  # type: ignore[method-assign]

    result = asyncio.run(
        evaluator.evaluate(
            post_url="https://example.com/post/2",
            job_id="job-2",
        )
    )
    assert result.error == ""
    assert result.total_score == 78
    assert result.provider_used == "gemini_vlm"
