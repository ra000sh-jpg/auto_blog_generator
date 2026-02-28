from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from modules.automation.job_store import JobConfig, JobStore
from modules.automation.pipeline_service import PipelineService
from modules.automation.scheduler_workers import _promote_to_ready, image_collector_worker_loop
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


def test_send_next_prompt_skips_when_sent_slot_exists(tmp_path: Path):
    store = build_store(tmp_path, "semi_auto_send_guard.db")
    notifier = CaptureNotifier(bot_token="token", chat_id="123")
    collector = TelegramImageCollector(
        job_store=store,
        notifier=notifier,  # type: ignore[arg-type]
        image_output_dir=str(tmp_path / "images"),
    )
    collector.init_slots(
        "semi-auto-send-guard",
        [
            {"slot_id": "content_1", "slot_role": "content", "prompt": "prompt1"},
            {"slot_id": "content_2", "slot_role": "content", "prompt": "prompt2"},
        ],
    )
    assert asyncio.run(collector.send_next_prompt("semi-auto-send-guard", "Guard Job")) is True
    assert len(notifier.messages) == 1

    # sent 슬롯이 존재하면 다음 프롬프트는 전송하지 않는다.
    assert asyncio.run(collector.send_next_prompt("semi-auto-send-guard", "Guard Job")) is False
    assert len(notifier.messages) == 1


def test_poll_and_collect_consumes_updates_one_by_one(tmp_path: Path):
    store = build_store(tmp_path, "semi_auto_update_cursor.db")
    store.set_system_setting("telegram_chat_id", "123")
    notifier = CaptureNotifier(bot_token="token", chat_id="123")
    collector = TelegramImageCollector(
        job_store=store,
        notifier=notifier,  # type: ignore[arg-type]
        image_output_dir=str(tmp_path / "images"),
    )
    collector.init_slots(
        "semi-auto-cursor-job",
        [
            {"slot_id": "content_1", "slot_role": "content", "prompt": "prompt1"},
            {"slot_id": "content_2", "slot_role": "content", "prompt": "prompt2"},
        ],
    )
    slots = collector.get_slots("semi-auto-cursor-job")
    slots[0]["status"] = "sent"
    slots[1]["status"] = "sent"
    store.set_system_setting("img_slot_semi-auto-cursor-job", json.dumps(slots))

    updates = [
        {
            "update_id": 1,
            "message": {
                "chat": {"id": "123"},
                "photo": [{"file_id": "f1", "file_size": 10}],
            },
        },
        {
            "update_id": 2,
            "message": {
                "chat": {"id": "123"},
                "photo": [{"file_id": "f2", "file_size": 20}],
            },
        },
    ]

    async def _mock_fetch_updates():
        last_raw = store.get_system_setting("telegram_last_update_id", "0")
        last_id = int(last_raw or "0")
        return [item for item in updates if int(item["update_id"]) > last_id]

    async def _download_ok(file_id: str, job_id: str, slot_id: str):
        del job_id
        path = tmp_path / "images" / f"{slot_id}_{file_id}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"img")
        return path

    collector._fetch_updates = _mock_fetch_updates  # type: ignore[method-assign]
    collector._download_file = _download_ok  # type: ignore[method-assign]

    assert asyncio.run(collector.poll_and_collect("semi-auto-cursor-job")) is True
    assert store.get_system_setting("telegram_last_update_id", "") == "1"

    slots_after_first = collector.get_slots("semi-auto-cursor-job")
    assert slots_after_first[0]["status"] == "received"
    assert slots_after_first[1]["status"] == "sent"

    assert asyncio.run(collector.poll_and_collect("semi-auto-cursor-job")) is True
    assert store.get_system_setting("telegram_last_update_id", "") == "2"

    slots_after_second = collector.get_slots("semi-auto-cursor-job")
    assert slots_after_second[1]["status"] == "received"


def test_image_collector_worker_runs_with_topic_only_semi_auto(
    tmp_path: Path,
    monkeypatch,
):
    store = build_store(tmp_path, "semi_auto_worker_topic_only.db")
    store.set_system_setting("scheduler_semi_auto_topics", json.dumps(["cafe"]))
    due_now = "2026-02-26T00:00:00Z"
    assert store.schedule_job(
        job_id="semi-auto-worker-job",
        title="Semi Auto Worker Job",
        seed_keywords=["semi", "auto", "worker"],
        platform="naver",
        persona_id="P1",
        scheduled_at=due_now,
    )
    claimed = store.claim_due_jobs(limit=1, now_override=due_now)
    assert len(claimed) == 1
    assert store.update_job_status("semi-auto-worker-job", store.STATUS_AWAITING_IMAGES) is True

    calls = {"poll": 0}

    class FakeCollector:
        def __init__(self, job_store, notifier, image_output_dir):
            del job_store, notifier, image_output_dir

        async def poll_and_collect(self, job_id: str) -> bool:
            del job_id
            calls["poll"] += 1
            return False

        async def send_next_prompt(self, job_id: str, job_title: str) -> bool:
            del job_id, job_title
            return False

        def all_slots_received(self, job_id: str) -> bool:
            del job_id
            return False

    class ServiceStub:
        def __init__(self, job_store: JobStore) -> None:
            self.job_store = job_store
            self.notifier = CaptureNotifier(bot_token="token", chat_id="123")
            self.pipeline_service = None

    import modules.automation.telegram_image_collector as collector_module

    monkeypatch.setattr(collector_module, "TelegramImageCollector", FakeCollector)
    service = ServiceStub(store)

    async def _run_once() -> None:
        task = asyncio.create_task(image_collector_worker_loop(service))
        try:
            for _ in range(50):
                if calls["poll"] > 0:
                    break
                await asyncio.sleep(0.02)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run_once())
    assert calls["poll"] > 0
