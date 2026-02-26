from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.pipeline_service import PipelineService
from modules.automation.scheduler_workers import _promote_to_ready
from modules.automation.telegram_image_collector import TelegramImageCollector
from modules.uploaders.playwright_publisher import PublishResult


def build_store(tmp_path: Path, name: str = "semi_auto_test.db") -> JobStore:
    return JobStore(str(tmp_path / name), config=JobConfig(max_llm_calls_per_job=15))


class DummyPublisher:
    async def publish(
        self,
        title: str,
        content: str,
        thumbnail: Optional[str] = None,
        images: Optional[List[str]] = None,
        image_sources: Optional[Dict[str, Dict[str, str]]] = None,
        image_points: Optional[List[Any]] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> PublishResult:
        del title, content, thumbnail, images, image_sources, image_points, tags, category
        return PublishResult(success=True, url="https://blog.naver.com/test/1")


class CaptureNotifier:
    def __init__(self, bot_token: str = "token", chat_id: str = "123") -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = True
        self.messages: list[str] = []

    async def send_message(
        self,
        text: str,
        *,
        disable_notification: bool = False,
    ) -> bool:
        del disable_notification
        self.messages.append(text)
        return True


def test_job_store_supports_awaiting_images_and_payload_helpers(tmp_path: Path):
    store = build_store(tmp_path, "semi_auto_job_store.db")
    due_now = "2026-02-26T00:00:00Z"
    assert store.schedule_job(
        job_id="semi-auto-helper-job",
        title="Semi Auto Helper Job",
        seed_keywords=["semi", "auto"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
    )
    claimed = store.claim_due_jobs(limit=1, now_override=due_now)
    assert len(claimed) == 1

    payload = {"title": "draft", "content": "본문", "images": []}
    assert store.save_prepared_payload("semi-auto-helper-job", payload, mark_ready=False) is True
    assert store.update_job_status("semi-auto-helper-job", store.STATUS_AWAITING_IMAGES) is True

    awaiting = store.list_awaiting_images_jobs()
    assert len(awaiting) == 1
    assert awaiting[0].job_id == "semi-auto-helper-job"

    loaded = store.load_prepared_payload("semi-auto-helper-job")
    assert loaded.get("content") == "본문"
    assert store.clear_prepared_payload("semi-auto-helper-job") is True
    assert store.load_prepared_payload("semi-auto-helper-job") == {}


def test_pipeline_semi_auto_moves_job_to_awaiting_images(tmp_path: Path):
    store = build_store(tmp_path, "semi_auto_pipeline.db")
    store.set_system_setting("telegram_image_mode", "semi_auto")
    store.set_system_setting("telegram_chat_id", "123")
    due_now = "2026-02-26T00:00:00Z"
    assert store.schedule_job(
        job_id="semi-auto-pipeline-job",
        title="Semi Auto Pipeline Job",
        seed_keywords=["semi", "auto", "telegram"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
    )
    claimed = store.claim_due_jobs(limit=1, now_override=due_now)
    assert len(claimed) == 1
    job = claimed[0]

    notifier = CaptureNotifier(bot_token="token", chat_id="123")

    async def generate_fn(_job) -> Dict[str, Any]:
        long_content = ("## 소제목\n\nsemi auto 파이프라인 테스트 본문입니다. " * 80).strip()
        return {
            "final_content": long_content,
            "quality_gate": "pass",
            "quality_snapshot": {"score": 90, "issues": []},
            "seo_snapshot": {"provider_used": "stub", "provider_model": "stub", "topic_mode": "it"},
            "image_prompts": ["thumbnail prompt", "content prompt"],
            "image_slots": [
                {"slot_id": "thumb_0", "slot_role": "thumbnail", "prompt": "thumbnail prompt"},
                {"slot_id": "content_1", "slot_role": "content", "prompt": "content prompt"},
            ],
            "llm_token_usage": {},
        }

    pipeline = PipelineService(
        job_store=store,
        publisher=DummyPublisher(),
        generate_fn=generate_fn,
        notifier=notifier,  # type: ignore[arg-type]
    )

    prepared = asyncio.run(pipeline.process_generation(job))
    assert prepared is True

    updated = store.get_job(job.job_id)
    assert updated is not None
    assert updated.status == store.STATUS_AWAITING_IMAGES
    assert bool(updated.prepared_payload)

    slot_raw = store.get_system_setting(f"img_slot_{job.job_id}", "")
    slots = json.loads(slot_raw)
    assert isinstance(slots, list)
    assert slots[0]["status"] == "sent"
    assert len(notifier.messages) == 1
    assert "Semi Auto Pipeline Job" in notifier.messages[0]


def test_promote_to_ready_updates_payload_with_collected_images(tmp_path: Path):
    store = build_store(tmp_path, "semi_auto_promote.db")
    due_now = "2026-02-26T00:00:00Z"
    assert store.schedule_job(
        job_id="semi-auto-promote-job",
        title="Semi Auto Promote Job",
        seed_keywords=["semi", "auto", "promote"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
    )
    claimed = store.claim_due_jobs(limit=1, now_override=due_now)
    assert len(claimed) == 1
    job = claimed[0]

    base_payload = {
        "title": job.title,
        "content": "본문",
        "thumbnail": "",
        "images": [],
        "image_sources": {},
        "image_points": [],
    }
    assert store.save_prepared_payload(job.job_id, base_payload, mark_ready=False) is True
    assert store.update_job_status(job.job_id, store.STATUS_AWAITING_IMAGES) is True
    awaiting_job = store.get_job(job.job_id)
    assert awaiting_job is not None

    class FakeCollector:
        def __init__(self) -> None:
            self.cleared = False

        def get_received_paths(self, _job_id: str) -> Dict[str, str]:
            return {
                "thumb_0": "data/images/tg_thumb.jpg",
                "content_1": "data/images/tg_content_1.jpg",
            }

        def get_slots(self, _job_id: str) -> List[Dict[str, str]]:
            return [
                {"slot_id": "thumb_0", "slot_role": "thumbnail"},
                {"slot_id": "content_1", "slot_role": "content"},
            ]

        def clear_slots(self, _job_id: str) -> None:
            self.cleared = True

    class ServiceStub:
        def __init__(self, job_store: JobStore) -> None:
            self.job_store = job_store
            self.notifier = None

    collector = FakeCollector()
    service = ServiceStub(store)
    asyncio.run(_promote_to_ready(service, awaiting_job, collector))

    updated = store.get_job(job.job_id)
    assert updated is not None
    assert updated.status == store.STATUS_READY
    assert updated.prepared_payload.get("thumbnail") == "data/images/tg_thumb.jpg"
    assert updated.prepared_payload.get("images") == ["data/images/tg_content_1.jpg"]
    assert collector.cleared is True


def test_collector_validates_chat_id_before_collecting(tmp_path: Path):
    store = build_store(tmp_path, "semi_auto_chat_filter.db")
    store.set_system_setting("telegram_chat_id", "123")
    notifier = CaptureNotifier(bot_token="token", chat_id="123")
    collector = TelegramImageCollector(
        job_store=store,
        notifier=notifier,  # type: ignore[arg-type]
        image_output_dir=str(tmp_path / "images"),
    )
    collector.init_slots(
        "semi-auto-chat-filter-job",
        [{"slot_id": "content_1", "slot_role": "content", "prompt": "prompt"}],
    )
    slots = collector.get_slots("semi-auto-chat-filter-job")
    slots[0]["status"] = "sent"
    store.set_system_setting("img_slot_semi-auto-chat-filter-job", json.dumps(slots))

    async def _updates_wrong_chat():
        return [
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": "999"},
                    "photo": [{"file_id": "f1", "file_size": 10}],
                },
            }
        ]

    async def _updates_valid_chat():
        return [
            {
                "update_id": 2,
                "message": {
                    "chat": {"id": "123"},
                    "photo": [{"file_id": "f2", "file_size": 20}],
                },
            }
        ]

    async def _download_ok(file_id: str, job_id: str, slot_id: str):
        del file_id, job_id, slot_id
        path = tmp_path / "images" / "ok.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"img")
        return path

    collector._fetch_updates = _updates_wrong_chat  # type: ignore[method-assign]
    collector._download_file = _download_ok  # type: ignore[method-assign]
    assert asyncio.run(collector.poll_and_collect("semi-auto-chat-filter-job")) is False

    collector._fetch_updates = _updates_valid_chat  # type: ignore[method-assign]
    assert asyncio.run(collector.poll_and_collect("semi-auto-chat-filter-job")) is True
