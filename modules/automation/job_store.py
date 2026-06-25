"""
SQLite 기반 Job Queue (JobStore)

P0 이슈 해결:
- P0 #1: retry_wait 조기 실행 방지 (claim 쿼리 조건 분리)
- P0 #2: 중복 발행 방지 (idempotency_key)
- P0 #3: running 고착 방지 (lease/heartbeat)
- P0 #4: LLM 호출량 DB 누적
- P0 #5: UTC 시간 표준화

참고:
- https://github.com/litements/litequeue
- https://github.com/coleifer/huey
"""

import sqlite3
import hashlib
import socket
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import json
import logging

from .time_utils import now_utc, add_seconds, calculate_retry_delay, parse_iso
from .. import constants
from ..constants import DEFAULT_FALLBACK_CATEGORY

logger = logging.getLogger(__name__)


@dataclass
class JobConfig:
    """Job 설정값"""
    max_retries: int = 3
    lease_timeout_sec: int = 300  # 5분
    heartbeat_interval_sec: int = 60  # 1분
    max_llm_calls_per_job: int = 15


@dataclass
class Job:
    """Job 데이터 클래스"""
    job_id: str
    status: str
    title: str
    seed_keywords: List[str]
    platform: str
    persona_id: str
    scheduled_at: str
    retry_count: int = 0
    max_retries: int = 3
    next_retry_at: Optional[str] = None
    idempotency_key: Optional[str] = None
    claimed_at: Optional[str] = None
    claimed_by: Optional[str] = None
    heartbeat_at: Optional[str] = None
    publish_attempt_id: Optional[str] = None
    result_url: str = ""
    thumbnail_url: str = ""
    error_code: str = ""
    error_message: str = ""
    quality_snapshot: Dict[str, Any] = field(default_factory=dict)
    seo_snapshot: Dict[str, Any] = field(default_factory=dict)
    llm_call_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""
    job_kind: str = "master"
    master_job_id: Optional[str] = None
    channel_id: Optional[str] = None
    # 플랫폼 유입 전략 관련 필드
    tags: List[str] = field(default_factory=list)        # 발행 시 사용할 태그 목록
    category: str = ""                                    # 발행 카테고리
    prepared_payload: Dict[str, Any] = field(default_factory=dict)  # 선생성 초안 데이터

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        """sqlite3.Row에서 Job 객체 생성"""
        keys = row.keys()
        return cls(
            job_id=row["job_id"],
            status=row["status"],
            title=row["title"],
            seed_keywords=json.loads(row["seed_keywords"]),
            platform=row["platform"],
            persona_id=row["persona_id"],
            scheduled_at=row["scheduled_at"],
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            next_retry_at=row["next_retry_at"],
            idempotency_key=row["idempotency_key"],
            claimed_at=row["claimed_at"],
            claimed_by=row["claimed_by"],
            heartbeat_at=row["heartbeat_at"],
            publish_attempt_id=row["publish_attempt_id"],
            result_url=row["result_url"] or "",
            thumbnail_url=row["thumbnail_url"] or "",
            error_code=row["error_code"] or "",
            error_message=row["error_message"] or "",
            quality_snapshot=json.loads(row["quality_snapshot"] or "{}"),
            seo_snapshot=json.loads(row["seo_snapshot"] or "{}"),
            llm_call_count=row["llm_call_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"] if "completed_at" in keys else "",
            job_kind=row["job_kind"] if "job_kind" in keys else "master",
            master_job_id=row["master_job_id"] if "master_job_id" in keys else None,
            channel_id=row["channel_id"] if "channel_id" in keys else None,
            tags=json.loads(row["tags"] if "tags" in keys else "[]") or [],
            category=row["category"] if "category" in keys else "",
            prepared_payload=json.loads(row["prepared_payload"] if "prepared_payload" in keys else "{}") or {},
        )


class JobStore:
    """
    SQLite 기반 Job Queue.

    주요 기능:
    - 원자적 job 선점 (claim_due_jobs)
    - Lease/Heartbeat 기반 고착 방지
    - Idempotency key로 중복 방지
    - LLM 호출량 DB 동기화
    """

    # 상태 상수
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_RETRY_WAIT = "retry_wait"

    # 품질 게이트 관련 상태 (Phase 25 신규)
    STATUS_GENERATED = "generated"
    STATUS_EVALUATING = "evaluating"
    STATUS_FAILED_QUALITY = "failed_quality"

    STATUS_READY = "ready_to_publish"
    STATUS_AWAITING_APPROVAL = "awaiting_approval"
    STATUS_AWAITING_IMAGES = "awaiting_images"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"

    JOB_KIND_MASTER = "master"
    JOB_KIND_SUB = "sub"

    IDEA_STATUS_PENDING = "pending"
    IDEA_STATUS_QUEUED = "queued"
    IDEA_STATUS_CONSUMED = "consumed"

    # 재시도 불가 에러 코드
    NON_RETRYABLE_ERRORS = frozenset({
        "AUTH_EXPIRED",
        "CAPTCHA_REQUIRED",
        "CONTENT_REJECTED",
        "QUALITY_REJECTED",
        "BUDGET_EXCEEDED",
    })

    def __init__(self, db_path: str = "data/automation.db", config: Optional[JobConfig] = None):
        self.db_path = db_path
        self.config = config or JobConfig()
        self._worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._ensure_directory()
        self._init_tables()

    def ensure_schema(self) -> None:
        """테이블/인덱스/기본 설정을 재검증한다."""
        self._init_tables()

    def _ensure_directory(self):
        """DB 디렉토리 생성"""
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    @contextmanager
    def connection(self):
        """
        트랜잭션 관리 컨텍스트.

        - IMMEDIATE isolation으로 동시성 충돌 최소화
        - WAL 모드로 읽기/쓰기 동시성 향상
        - Foreign Key 활성화 (P1 #8)
        """
        conn = sqlite3.connect(
            self.db_path,
            timeout=30,
            isolation_level="IMMEDIATE"
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_tables(self):
        """테이블 및 인덱스 초기화"""
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    seed_keywords TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    next_retry_at TEXT,
                    claimed_at TEXT,
                    claimed_by TEXT,
                    heartbeat_at TEXT,
                    publish_attempt_id TEXT,
                    result_url TEXT DEFAULT '',
                    thumbnail_url TEXT DEFAULT '',
                    error_code TEXT DEFAULT '',
                    error_message TEXT DEFAULT '',
                    quality_snapshot TEXT DEFAULT '{}',
                    seo_snapshot TEXT DEFAULT '{}',
                    llm_call_count INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT DEFAULT '',
                    job_kind TEXT NOT NULL DEFAULT 'master',
                    master_job_id TEXT DEFAULT NULL,
                    channel_id TEXT DEFAULT NULL,
                    tags TEXT DEFAULT '[]',
                    category TEXT DEFAULT '',
                    prepared_payload TEXT DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_queued
                ON jobs(status, scheduled_at) WHERE status = 'queued';
            """)
            # 기존 DB 마이그레이션: tags / category 컬럼 추가
            existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "tags" not in existing:
                conn.execute("ALTER TABLE jobs ADD COLUMN tags TEXT DEFAULT '[]'")
            if "category" not in existing:
                conn.execute("ALTER TABLE jobs ADD COLUMN category TEXT DEFAULT ''")
            if "prepared_payload" not in existing:
                conn.execute("ALTER TABLE jobs ADD COLUMN prepared_payload TEXT DEFAULT '{}'")
            if "completed_at" not in existing:
                conn.execute("ALTER TABLE jobs ADD COLUMN completed_at TEXT DEFAULT ''")
            if "job_kind" not in existing:
                conn.execute("ALTER TABLE jobs ADD COLUMN job_kind TEXT NOT NULL DEFAULT 'master'")
            if "master_job_id" not in existing:
                conn.execute("ALTER TABLE jobs ADD COLUMN master_job_id TEXT DEFAULT NULL")
            if "channel_id" not in existing:
                conn.execute("ALTER TABLE jobs ADD COLUMN channel_id TEXT DEFAULT NULL")
            conn.executescript("""

                CREATE INDEX IF NOT EXISTS idx_jobs_retry
                ON jobs(status, next_retry_at) WHERE status = 'retry_wait';

                CREATE INDEX IF NOT EXISTS idx_jobs_running
                ON jobs(status, claimed_by, heartbeat_at) WHERE status = 'running';

                CREATE INDEX IF NOT EXISTS idx_jobs_ready
                ON jobs(status, updated_at) WHERE status = 'ready_to_publish';

                CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_master_channel
                ON jobs(master_job_id, channel_id)
                WHERE master_job_id IS NOT NULL;

                CREATE INDEX IF NOT EXISTS idx_jobs_channel
                ON jobs(channel_id);

                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_job_events_job
                ON job_events(job_id, created_at);

                CREATE TABLE IF NOT EXISTS post_metrics (
                    post_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    views INTEGER DEFAULT 0,
                    likes INTEGER DEFAULT 0,
                    comments INTEGER DEFAULT 0,
                    shares INTEGER DEFAULT 0,
                    ctr REAL DEFAULT 0.0,
                    ai_total REAL DEFAULT 0.0,
                    seo_score REAL DEFAULT 0.0,
                    dup_score REAL DEFAULT 0.0,
                    post_score REAL DEFAULT 0.0,
                    snapshot_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );

                CREATE TABLE IF NOT EXISTS job_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    metric_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT DEFAULT '',
                    duration_ms REAL DEFAULT 0.0,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    provider TEXT DEFAULT '',
                    detail_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );

                CREATE INDEX IF NOT EXISTS idx_job_metrics_job_time
                ON job_metrics(job_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_job_metrics_type_time
                ON job_metrics(metric_type, created_at);

                CREATE TABLE IF NOT EXISTS feedback_rule_candidates (
                    id TEXT PRIMARY KEY,
                    suggestion_hash TEXT NOT NULL UNIQUE,
                    suggestion_text TEXT NOT NULL,
                    mention_count INTEGER NOT NULL DEFAULT 0,
                    priority_score REAL NOT NULL DEFAULT 0.0,
                    avg_visual_score REAL NOT NULL DEFAULT 0.0,
                    status TEXT NOT NULL DEFAULT 'observing',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    notified_at TEXT DEFAULT '',
                    remind_at TEXT DEFAULT '',
                    answered_at TEXT DEFAULT '',
                    callback_token TEXT DEFAULT '',
                    callback_expires_at TEXT DEFAULT '',
                    meta_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_frc_suggestion_hash
                ON feedback_rule_candidates(suggestion_hash);

                CREATE INDEX IF NOT EXISTS idx_frc_status_last_seen
                ON feedback_rule_candidates(status, last_seen_at);

                CREATE TABLE IF NOT EXISTS feedback_rule_active (
                    id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    suggestion_hash TEXT NOT NULL,
                    rule_text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    activated_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    baseline_score REAL NOT NULL DEFAULT 0.0,
                    avg_after_score REAL NOT NULL DEFAULT 0.0,
                    applied_post_count INTEGER NOT NULL DEFAULT 0,
                    decision_score REAL NOT NULL DEFAULT 0.0,
                    last_evaluated_at TEXT DEFAULT '',
                    meta_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_fra_status_activated
                ON feedback_rule_active(status, activated_at);

                CREATE INDEX IF NOT EXISTS idx_fra_hash_status
                ON feedback_rule_active(suggestion_hash, status);

                CREATE TABLE IF NOT EXISTS vlm_model_catalog (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    client_provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'discovered',
                    supports_image INTEGER NOT NULL DEFAULT 1,
                    include_in_competition INTEGER NOT NULL DEFAULT 0,
                    quality_score REAL NOT NULL DEFAULT 0.0,
                    reliability_score REAL NOT NULL DEFAULT 0.0,
                    scoring_bias_offset REAL NOT NULL DEFAULT 0.0,
                    input_cost_per_1m REAL NOT NULL DEFAULT 0.0,
                    output_cost_per_1m REAL NOT NULL DEFAULT 0.0,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    max_image_resolution TEXT NOT NULL DEFAULT '',
                    vision_context_window INTEGER NOT NULL DEFAULT 0,
                    error_rate_24h REAL NOT NULL DEFAULT 0.0,
                    avg_latency_ms REAL NOT NULL DEFAULT 0.0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(provider, model)
                );

                CREATE INDEX IF NOT EXISTS idx_vlm_catalog_status
                ON vlm_model_catalog(status, updated_at);

                CREATE INDEX IF NOT EXISTS idx_vlm_catalog_key
                ON vlm_model_catalog(key_id, status);

                CREATE TABLE IF NOT EXISTS vlm_model_price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_cost_per_1m REAL NOT NULL DEFAULT 0.0,
                    output_cost_per_1m REAL NOT NULL DEFAULT 0.0,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    source TEXT NOT NULL DEFAULT 'official',
                    fx_rate REAL NOT NULL DEFAULT 0.0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_vlm_price_model_time
                ON vlm_model_price_history(provider, model, created_at);

                CREATE TABLE IF NOT EXISTS vlm_discovery_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_vlm_discovery_type_time
                ON vlm_discovery_events(event_type, created_at);

                CREATE TABLE IF NOT EXISTS vlm_eval_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    visual_hash TEXT NOT NULL,
                    dom_hash TEXT NOT NULL DEFAULT '',
                    model_key TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_vlm_eval_cache_lookup
                ON vlm_eval_cache(visual_hash, model_key, expires_at);

                CREATE TABLE IF NOT EXISTS text_model_catalog (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'discovered',
                    include_in_competition INTEGER NOT NULL DEFAULT 0,
                    supports_json INTEGER NOT NULL DEFAULT 0,
                    supports_tool_calls INTEGER NOT NULL DEFAULT 0,
                    supports_thinking INTEGER NOT NULL DEFAULT 0,
                    context_window INTEGER NOT NULL DEFAULT 0,
                    max_output_tokens INTEGER NOT NULL DEFAULT 0,
                    quality_score REAL NOT NULL DEFAULT 0.0,
                    speed_score REAL NOT NULL DEFAULT 0.0,
                    reliability_score REAL NOT NULL DEFAULT 0.0,
                    input_cost_per_1m REAL NOT NULL DEFAULT 0.0,
                    output_cost_per_1m REAL NOT NULL DEFAULT 0.0,
                    cache_hit_input_cost_per_1m REAL NOT NULL DEFAULT 0.0,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    source_url TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(provider, model)
                );

                CREATE INDEX IF NOT EXISTS idx_text_model_catalog_status
                ON text_model_catalog(status, updated_at);

                CREATE INDEX IF NOT EXISTS idx_text_model_catalog_key
                ON text_model_catalog(key_id, status);

                CREATE TABLE IF NOT EXISTS text_model_price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_cost_per_1m REAL NOT NULL DEFAULT 0.0,
                    output_cost_per_1m REAL NOT NULL DEFAULT 0.0,
                    cache_hit_input_cost_per_1m REAL NOT NULL DEFAULT 0.0,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    source TEXT NOT NULL DEFAULT 'official',
                    fx_rate REAL NOT NULL DEFAULT 0.0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_text_model_price_model_time
                ON text_model_price_history(provider, model, created_at);

                CREATE TABLE IF NOT EXISTS text_model_discovery_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_text_model_discovery_type_time
                ON text_model_discovery_events(event_type, created_at);

                CREATE TABLE IF NOT EXISTS macro_documents (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    title TEXT NOT NULL,
                    published_at TEXT DEFAULT '',
                    url TEXT NOT NULL,
                    file_url TEXT DEFAULT '',
                    file_type TEXT DEFAULT '',
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    local_path TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    hash TEXT NOT NULL UNIQUE,
                    raw_text TEXT DEFAULT '',
                    parsed_json TEXT NOT NULL DEFAULT '{}',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    insight_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_macro_documents_source_status
                ON macro_documents(source, status, published_at);

                CREATE INDEX IF NOT EXISTS idx_macro_documents_updated
                ON macro_documents(updated_at);

                CREATE TABLE IF NOT EXISTS macro_blog_candidates (
                    id TEXT PRIMARY KEY,
                    macro_document_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    angle TEXT NOT NULL DEFAULT '',
                    target_reader TEXT DEFAULT '',
                    outline_json TEXT NOT NULL DEFAULT '{}',
                    draft_body TEXT DEFAULT '',
                    quality_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(macro_document_id) REFERENCES macro_documents(id)
                );

                CREATE INDEX IF NOT EXISTS idx_macro_candidates_document
                ON macro_blog_candidates(macro_document_id, status);

                CREATE INDEX IF NOT EXISTS idx_macro_candidates_status
                ON macro_blog_candidates(status, updated_at);

                CREATE TABLE IF NOT EXISTS image_generation_log (
                    id TEXT PRIMARY KEY,
                    post_id TEXT,
                    slot_id TEXT NOT NULL,
                    slot_role TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    latency_ms REAL DEFAULT 0.0,
                    fallback_reason TEXT DEFAULT '',
                    cost_usd REAL DEFAULT 0.0,
                    source_url TEXT DEFAULT '',
                    measured_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_image_generation_log_post
                ON image_generation_log(post_id, measured_at);

                CREATE INDEX IF NOT EXISTS idx_image_generation_log_slot
                ON image_generation_log(slot_id, measured_at);

                CREATE TABLE IF NOT EXISTS persona_profiles (
                    persona_id TEXT PRIMARY KEY,
                    persona_json TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    performance_history TEXT DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS system_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS post_text_archives (
                    job_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    final_content TEXT NOT NULL,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    category TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL DEFAULT 'published_draft',
                    quality_score REAL NOT NULL DEFAULT 0.0,
                    insight_score REAL NOT NULL DEFAULT 0.0,
                    manual_revision_applied INTEGER NOT NULL DEFAULT 0,
                    result_url TEXT NOT NULL DEFAULT '',
                    image_manifest_json TEXT NOT NULL DEFAULT '{}',
                    review_status TEXT NOT NULL DEFAULT 'pending',
                    review_updated_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_post_text_archives_review
                ON post_text_archives(review_status, updated_at);

                CREATE TABLE IF NOT EXISTS channels (
                    channel_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    label TEXT NOT NULL,
                    blog_url TEXT NOT NULL DEFAULT '',
                    persona_id TEXT NOT NULL DEFAULT '',
                    persona_desc TEXT DEFAULT '',
                    daily_target INTEGER DEFAULT 0,
                    style_level INTEGER DEFAULT 2,
                    style_model TEXT DEFAULT '',
                    publish_delay_minutes INTEGER DEFAULT 90,
                    is_master INTEGER DEFAULT 0,
                    auth_json TEXT DEFAULT '{}',
                    active INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_master
                ON channels(is_master)
                WHERE is_master = 1;

                CREATE TABLE IF NOT EXISTS idea_vault (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_text TEXT NOT NULL,
                    mapped_category TEXT NOT NULL DEFAULT '',
                    topic_mode TEXT NOT NULL DEFAULT 'cafe',
                    parser_used TEXT NOT NULL DEFAULT 'heuristic',
                    status TEXT NOT NULL DEFAULT 'pending',
                    queued_job_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    consumed_at TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_idea_vault_status
                ON idea_vault(status, created_at);

                CREATE INDEX IF NOT EXISTS idx_idea_vault_queued_job
                ON idea_vault(queued_job_id);

                CREATE TABLE IF NOT EXISTS model_performance_log (
                    id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    topic_mode TEXT NOT NULL,
                    quality_score REAL NOT NULL,
                    cost_won REAL NOT NULL,
                    is_free_model INTEGER NOT NULL DEFAULT 0,
                    score_per_won REAL,
                    free_model_rank INTEGER,
                    post_id TEXT,
                    slot_type TEXT NOT NULL,
                    feedback_source TEXT NOT NULL DEFAULT 'ai_evaluator',
                    measured_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_model_performance_model_time
                ON model_performance_log(model_id, measured_at);

                CREATE INDEX IF NOT EXISTS idx_model_performance_topic_time
                ON model_performance_log(topic_mode, measured_at);

                CREATE INDEX IF NOT EXISTS idx_model_performance_slot_time
                ON model_performance_log(slot_type, measured_at);

                CREATE TABLE IF NOT EXISTS weekly_competition_state (
                    week_start TEXT PRIMARY KEY,
                    phase TEXT NOT NULL,
                    candidates TEXT NOT NULL,
                    champion_model TEXT,
                    challenger_model TEXT,
                    early_terminated INTEGER NOT NULL DEFAULT 0,
                    apply_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_weekly_competition_apply_at
                ON weekly_competition_state(apply_at);

                CREATE TABLE IF NOT EXISTS champion_history (
                    week_start TEXT PRIMARY KEY,
                    champion_model TEXT NOT NULL,
                    challenger_model TEXT,
                    avg_champion_score REAL NOT NULL,
                    topic_mode_scores TEXT NOT NULL,
                    cost_won REAL NOT NULL,
                    early_terminated INTEGER NOT NULL DEFAULT 0,
                    shadow_only INTEGER NOT NULL DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS idx_champion_history_score
                ON champion_history(avg_champion_score);

                CREATE TABLE IF NOT EXISTS topic_memory (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id        TEXT UNIQUE NOT NULL,
                    title         TEXT NOT NULL,
                    keywords      TEXT NOT NULL DEFAULT '[]',
                    topic_mode    TEXT NOT NULL DEFAULT 'cafe',
                    platform      TEXT NOT NULL DEFAULT 'naver',
                    persona_id    TEXT NOT NULL DEFAULT 'P1',
                    summary       TEXT DEFAULT '',
                    result_url    TEXT DEFAULT '',
                    quality_score INTEGER DEFAULT 0,
                    recorded_at   TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tm_topic_recorded
                ON topic_memory(topic_mode, recorded_at DESC);

                CREATE INDEX IF NOT EXISTS idx_tm_persona_recorded
                ON topic_memory(persona_id, recorded_at DESC);

                CREATE INDEX IF NOT EXISTS idx_tm_recorded
                ON topic_memory(recorded_at DESC);

                CREATE INDEX IF NOT EXISTS idx_tm_platform_recorded
                ON topic_memory(platform, recorded_at DESC);

                CREATE INDEX IF NOT EXISTS idx_tm_topic_platform_recorded
                ON topic_memory(topic_mode, platform, recorded_at DESC);

                CREATE TABLE IF NOT EXISTS topic_memory_embeddings (
                    job_id TEXT PRIMARY KEY,
                    embedding_json TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tme_model_updated
                ON topic_memory_embeddings(model_name, updated_at DESC);
            """)

            existing_metric_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(job_metrics)").fetchall()
            }
            if "input_tokens" not in existing_metric_columns:
                conn.execute("ALTER TABLE job_metrics ADD COLUMN input_tokens INTEGER DEFAULT 0")
            if "output_tokens" not in existing_metric_columns:
                conn.execute("ALTER TABLE job_metrics ADD COLUMN output_tokens INTEGER DEFAULT 0")
            if "provider" not in existing_metric_columns:
                conn.execute("ALTER TABLE job_metrics ADD COLUMN provider TEXT DEFAULT ''")

            existing_candidate_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(feedback_rule_candidates)").fetchall()
            }
            if existing_candidate_columns and "meta_json" not in existing_candidate_columns:
                conn.execute("ALTER TABLE feedback_rule_candidates ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'")
            if existing_candidate_columns and "notified_at" not in existing_candidate_columns:
                conn.execute("ALTER TABLE feedback_rule_candidates ADD COLUMN notified_at TEXT DEFAULT ''")
            if existing_candidate_columns and "remind_at" not in existing_candidate_columns:
                conn.execute("ALTER TABLE feedback_rule_candidates ADD COLUMN remind_at TEXT DEFAULT ''")
            if existing_candidate_columns and "answered_at" not in existing_candidate_columns:
                conn.execute("ALTER TABLE feedback_rule_candidates ADD COLUMN answered_at TEXT DEFAULT ''")
            if existing_candidate_columns and "callback_token" not in existing_candidate_columns:
                conn.execute("ALTER TABLE feedback_rule_candidates ADD COLUMN callback_token TEXT DEFAULT ''")
            if existing_candidate_columns and "callback_expires_at" not in existing_candidate_columns:
                conn.execute("ALTER TABLE feedback_rule_candidates ADD COLUMN callback_expires_at TEXT DEFAULT ''")

            existing_active_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(feedback_rule_active)").fetchall()
            }
            if existing_active_columns and "meta_json" not in existing_active_columns:
                conn.execute("ALTER TABLE feedback_rule_active ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'")
            if existing_active_columns and "decision_score" not in existing_active_columns:
                conn.execute("ALTER TABLE feedback_rule_active ADD COLUMN decision_score REAL NOT NULL DEFAULT 0.0")
            if existing_active_columns and "last_evaluated_at" not in existing_active_columns:
                conn.execute("ALTER TABLE feedback_rule_active ADD COLUMN last_evaluated_at TEXT DEFAULT ''")

            existing_vlm_catalog_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(vlm_model_catalog)").fetchall()
            }
            if existing_vlm_catalog_columns and "client_provider" not in existing_vlm_catalog_columns:
                conn.execute("ALTER TABLE vlm_model_catalog ADD COLUMN client_provider TEXT NOT NULL DEFAULT ''")
            if existing_vlm_catalog_columns and "key_id" not in existing_vlm_catalog_columns:
                conn.execute("ALTER TABLE vlm_model_catalog ADD COLUMN key_id TEXT NOT NULL DEFAULT ''")
            if existing_vlm_catalog_columns and "label" not in existing_vlm_catalog_columns:
                conn.execute("ALTER TABLE vlm_model_catalog ADD COLUMN label TEXT NOT NULL DEFAULT ''")
            if existing_vlm_catalog_columns and "include_in_competition" not in existing_vlm_catalog_columns:
                conn.execute("ALTER TABLE vlm_model_catalog ADD COLUMN include_in_competition INTEGER NOT NULL DEFAULT 0")
            if existing_vlm_catalog_columns and "quality_score" not in existing_vlm_catalog_columns:
                conn.execute("ALTER TABLE vlm_model_catalog ADD COLUMN quality_score REAL NOT NULL DEFAULT 0.0")
            if existing_vlm_catalog_columns and "reliability_score" not in existing_vlm_catalog_columns:
                conn.execute("ALTER TABLE vlm_model_catalog ADD COLUMN reliability_score REAL NOT NULL DEFAULT 0.0")
            if existing_vlm_catalog_columns and "scoring_bias_offset" not in existing_vlm_catalog_columns:
                conn.execute("ALTER TABLE vlm_model_catalog ADD COLUMN scoring_bias_offset REAL NOT NULL DEFAULT 0.0")
            if existing_vlm_catalog_columns and "input_cost_per_1m" not in existing_vlm_catalog_columns:
                conn.execute("ALTER TABLE vlm_model_catalog ADD COLUMN input_cost_per_1m REAL NOT NULL DEFAULT 0.0")
            if existing_vlm_catalog_columns and "output_cost_per_1m" not in existing_vlm_catalog_columns:
                conn.execute("ALTER TABLE vlm_model_catalog ADD COLUMN output_cost_per_1m REAL NOT NULL DEFAULT 0.0")
            if existing_vlm_catalog_columns and "currency" not in existing_vlm_catalog_columns:
                conn.execute("ALTER TABLE vlm_model_catalog ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'")
            if existing_vlm_catalog_columns and "max_image_resolution" not in existing_vlm_catalog_columns:
                conn.execute("ALTER TABLE vlm_model_catalog ADD COLUMN max_image_resolution TEXT NOT NULL DEFAULT ''")
            if existing_vlm_catalog_columns and "vision_context_window" not in existing_vlm_catalog_columns:
                conn.execute("ALTER TABLE vlm_model_catalog ADD COLUMN vision_context_window INTEGER NOT NULL DEFAULT 0")
            if existing_vlm_catalog_columns and "error_rate_24h" not in existing_vlm_catalog_columns:
                conn.execute("ALTER TABLE vlm_model_catalog ADD COLUMN error_rate_24h REAL NOT NULL DEFAULT 0.0")
            if existing_vlm_catalog_columns and "avg_latency_ms" not in existing_vlm_catalog_columns:
                conn.execute("ALTER TABLE vlm_model_catalog ADD COLUMN avg_latency_ms REAL NOT NULL DEFAULT 0.0")
            if existing_vlm_catalog_columns and "metadata_json" not in existing_vlm_catalog_columns:
                conn.execute("ALTER TABLE vlm_model_catalog ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")

            existing_image_log_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(image_generation_log)").fetchall()
            }
            if existing_image_log_columns and "source_url" not in existing_image_log_columns:
                conn.execute("ALTER TABLE image_generation_log ADD COLUMN source_url TEXT DEFAULT ''")

            existing_macro_document_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(macro_documents)").fetchall()
            }
            if existing_macro_document_columns and "attachments_json" not in existing_macro_document_columns:
                conn.execute("ALTER TABLE macro_documents ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'")

            existing_idea_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(idea_vault)").fetchall()
            }
            if "topic_mode" not in existing_idea_columns:
                conn.execute("ALTER TABLE idea_vault ADD COLUMN topic_mode TEXT NOT NULL DEFAULT 'cafe'")
            if "parser_used" not in existing_idea_columns:
                conn.execute("ALTER TABLE idea_vault ADD COLUMN parser_used TEXT NOT NULL DEFAULT 'heuristic'")
            if "status" not in existing_idea_columns:
                conn.execute("ALTER TABLE idea_vault ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
            if "queued_job_id" not in existing_idea_columns:
                conn.execute("ALTER TABLE idea_vault ADD COLUMN queued_job_id TEXT NOT NULL DEFAULT ''")
            if "updated_at" not in existing_idea_columns:
                conn.execute("ALTER TABLE idea_vault ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
            if "consumed_at" not in existing_idea_columns:
                conn.execute("ALTER TABLE idea_vault ADD COLUMN consumed_at TEXT NOT NULL DEFAULT ''")
            if "source_url" not in existing_idea_columns:
                conn.execute("ALTER TABLE idea_vault ADD COLUMN source_url TEXT NOT NULL DEFAULT ''")
            # source_url 중복 차단 인덱스 (빈 문자열 제외)
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_idea_vault_source_url
                ON idea_vault(source_url)
                WHERE source_url != ''
                """
            )

            # 상태값 정합성 마이그레이션: legacy 'published' -> 'completed'
            migrated = conn.execute(
                """
                UPDATE jobs
                SET status = ?
                WHERE status = 'published'
                """,
                (self.STATUS_COMPLETED,),
            ).rowcount
            if migrated:
                logger.info(
                    "Migrated legacy job statuses",
                    extra={"from_status": "published", "to_status": self.STATUS_COMPLETED, "count": migrated},
                )

            # 완료 시각 분리 컬럼이 비어있는 기존 completed 데이터 보정
            completed_backfill = conn.execute(
                """
                UPDATE jobs
                SET completed_at = updated_at
                WHERE status = ?
                AND (completed_at IS NULL OR completed_at = '')
                """,
                (self.STATUS_COMPLETED,),
            ).rowcount
            if completed_backfill:
                logger.info(
                    "Backfilled completed_at from updated_at",
                    extra={"count": completed_backfill},
                )
        self._migrate_competition_settings()

    def _migrate_competition_settings(self) -> None:
        """경쟁 모델 설정을 신규 구조로 백필한다."""
        now = now_utc()
        defaults = [
            ("router_eval_min_samples", "5"),
            ("router_champion_switch_threshold", "2.0"),
            ("router_eval_model_today", ""),
            ("router_eval_last_run_date", ""),
            ("router_auto_champion_switch_enabled", "false"),
        ]
        with self.connection() as conn:
            for setting_key, setting_value in defaults:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (setting_key, setting_value, now),
                )

            row = conn.execute(
                """
                SELECT setting_value
                FROM system_settings
                WHERE setting_key = 'router_registered_models'
                """
            ).fetchone()
            existing_value = str(row["setting_value"] or "").strip() if row else ""
            if existing_value:
                return

            champion_row = conn.execute(
                """
                SELECT setting_value
                FROM system_settings
                WHERE setting_key = 'router_champion_model'
                """
            ).fetchone()
            challenger_row = conn.execute(
                """
                SELECT setting_value
                FROM system_settings
                WHERE setting_key = 'router_challenger_model'
                """
            ).fetchone()
            champion_raw = str(champion_row["setting_value"] or "").strip() if champion_row else ""
            challenger_raw = str(challenger_row["setting_value"] or "").strip() if challenger_row else ""

            def _split_provider_model(model_raw: str) -> Dict[str, str]:
                value = str(model_raw or "").strip()
                if not value:
                    return {"provider": "", "model_id": ""}
                if ":" in value:
                    provider, model_id = value.split(":", 1)
                    return {"provider": provider.strip(), "model_id": model_id.strip()}
                return {"provider": "", "model_id": value}

            registered: List[Dict[str, Any]] = []
            champion = _split_provider_model(champion_raw)
            if champion["model_id"]:
                registered.append(
                    {
                        "model_id": champion["model_id"],
                        "provider": champion["provider"],
                        "active": True,
                    }
                )
            challenger = _split_provider_model(challenger_raw)
            if challenger["model_id"] and challenger["model_id"] != champion["model_id"]:
                registered.append(
                    {
                        "model_id": challenger["model_id"],
                        "provider": challenger["provider"],
                        "active": True,
                    }
                )

            conn.execute(
                """
                INSERT OR REPLACE INTO system_settings (setting_key, setting_value, updated_at)
                VALUES (?, ?, ?)
                """,
                ("router_registered_models", json.dumps(registered, ensure_ascii=False), now),
            )

    def _generate_idempotency_key(
        self,
        title: str,
        scheduled_at: str,
        persona_id: str,
        *,
        job_kind: str = JOB_KIND_MASTER,
        master_job_id: Optional[str] = None,
        channel_id: Optional[str] = None,
    ) -> str:
        """중복 방지용 idempotency key 생성"""
        data = (
            f"{title}|{scheduled_at}|{persona_id}|{job_kind}|"
            f"{master_job_id or ''}|{channel_id or ''}"
        )
        return hashlib.sha256(data.encode()).hexdigest()[:32]

    def schedule_job(
        self,
        job_id: str,
        title: str,
        seed_keywords: List[str],
        platform: str,
        persona_id: str,
        scheduled_at: str,
        max_retries: Optional[int] = None,
        tags: Optional[List[str]] = None,
        category: str = "",
        job_kind: str = JOB_KIND_MASTER,
        master_job_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        status: str = STATUS_QUEUED,
    ) -> bool:
        """
        새 작업 등록.

        Args:
            job_id: 고유 작업 ID
            title: 포스트 제목
            seed_keywords: 시드 키워드 리스트
            platform: 발행 플랫폼 (naver, tistory 등)
            persona_id: 페르소나 ID
            scheduled_at: 예약 시간 (UTC ISO)
            max_retries: 최대 재시도 횟수
            tags: 발행 태그 목록 (미지정 시 파이프라인에서 자동 생성)
            category: 발행 카테고리
            job_kind: 잡 분류(master/sub)
            master_job_id: 서브 잡의 원본 마스터 잡 ID
            channel_id: 대상 채널 ID
            status: 초기 상태

        Returns:
            bool: 등록 성공 여부 (중복 시 False)
        """
        start_time = time.perf_counter()
        if max_retries is None:
            max_retries = self.config.max_retries

        now = now_utc()
        normalized_job_kind = str(job_kind or self.JOB_KIND_MASTER).strip().lower()
        if normalized_job_kind not in {self.JOB_KIND_MASTER, self.JOB_KIND_SUB}:
            normalized_job_kind = self.JOB_KIND_MASTER
        normalized_status = str(status or self.STATUS_QUEUED).strip()
        if not normalized_status:
            normalized_status = self.STATUS_QUEUED
        normalized_master_job_id = str(master_job_id or "").strip() or None
        normalized_channel_id = str(channel_id or "").strip() or None

        idempotency_key = self._generate_idempotency_key(
            title,
            scheduled_at,
            persona_id,
            job_kind=normalized_job_kind,
            master_job_id=normalized_master_job_id,
            channel_id=normalized_channel_id,
        )

        try:
            with self.connection() as conn:
                conn.execute("""
                    INSERT INTO jobs (
                        job_id, idempotency_key, status, title, seed_keywords,
                        platform, persona_id, scheduled_at, max_retries,
                        completed_at, job_kind, master_job_id, channel_id,
                        tags, category,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    job_id, idempotency_key, normalized_status, title,
                    json.dumps(seed_keywords), platform, persona_id,
                    scheduled_at, max_retries,
                    "",
                    normalized_job_kind,
                    normalized_master_job_id,
                    normalized_channel_id,
                    json.dumps(tags or []), category,
                    now, now
                ))

                self._log_event(conn, job_id, "scheduled", {
                    "title": title,
                    "scheduled_at": scheduled_at,
                })

                logger.info(f"Job scheduled: {job_id} at {scheduled_at}")
                logger.debug(
                    "schedule_job latency",
                    extra={"job_id": job_id, "duration_ms": round((time.perf_counter() - start_time) * 1000, 2)},
                )
                return True

        except sqlite3.IntegrityError as e:
            if "idempotency_key" in str(e):
                logger.warning(f"Duplicate job detected: {title} at {scheduled_at}")
            else:
                logger.warning(f"Job already exists: {job_id}")
            return False

    def claim_due_jobs(
        self,
        limit: int = 5,
        now_override: Optional[str] = None,
        job_kind: Optional[str] = None,
        required_tag: Optional[str] = None,
    ) -> List[Job]:
        """
        실행 가능한 작업들을 원자적으로 선점.

        P0 #1 해결: queued와 retry_wait 조건 분리
        - queued: scheduled_at <= now
        - retry_wait: next_retry_at <= now

        Args:
            limit: 최대 선점 개수
            now_override: 테스트용 현재 시각 오버라이드

        Returns:
            List[Job]: 선점된 Job 객체 리스트
        """
        start_time = time.perf_counter()
        now = now_override or now_utc()
        worker_id = self._worker_id

        with self.connection() as conn:
            # P0 #1: 조건 분리된 쿼리
            where_clauses = [
                "("
                "(status = 'queued' AND scheduled_at <= ?)"
                " OR "
                "(status = 'retry_wait' AND next_retry_at <= ?)"
                ")"
            ]
            params: List[Any] = [now, now]
            normalized_kind = str(job_kind or "").strip().lower()
            if normalized_kind:
                where_clauses.append("job_kind = ?")
                params.append(normalized_kind)
            normalized_tag = str(required_tag or "").strip()
            if normalized_tag:
                where_clauses.append("tags LIKE ?")
                params.append(f'%"{normalized_tag}"%')

            query = f"""
                UPDATE jobs
                SET status = ?,
                    claimed_at = ?,
                    claimed_by = ?,
                    heartbeat_at = ?,
                    updated_at = ?
                WHERE job_id IN (
                    SELECT job_id FROM jobs
                    WHERE {' AND '.join(where_clauses)}
                    ORDER BY COALESCE(next_retry_at, scheduled_at) ASC
                    LIMIT ?
                )
                RETURNING *
            """
            cursor = conn.execute(
                query,
                (
                    self.STATUS_RUNNING,
                    now,
                    worker_id,
                    now,
                    now,
                    *params,
                    limit,
                ),
            )

            jobs = [Job.from_row(row) for row in cursor.fetchall()]

            for job in jobs:
                self._log_event(conn, job.job_id, "claimed", {
                    "worker_id": worker_id,
                    "previous_status": "queued" if job.retry_count == 0 else "retry_wait",
                })

            if jobs:
                logger.info(f"Claimed {len(jobs)} jobs: {[j.job_id for j in jobs]}")
            logger.debug(
                "claim_due_jobs latency",
                extra={
                    "worker_id": worker_id,
                    "claimed_count": len(jobs),
                    "duration_ms": round((time.perf_counter() - start_time) * 1000, 2),
                },
            )

            return jobs

    def claim_for_generate(
        self,
        limit: int = 5,
        now_override: Optional[str] = None,
        job_kind: Optional[str] = None,
        required_tag: Optional[str] = None,
    ) -> List[Job]:
        """생성 워커용 claim 래퍼."""
        return self.claim_due_jobs(
            limit=limit,
            now_override=now_override,
            job_kind=job_kind,
            required_tag=required_tag,
        )

    def heartbeat(self, job_id: str) -> bool:
        """
        실행 중인 작업의 heartbeat 갱신.

        P0 #3: Lease 갱신으로 고착 방지

        Returns:
            bool: 갱신 성공 여부
        """
        now = now_utc()
        with self.connection() as conn:
            cursor = conn.execute("""
                UPDATE jobs
                SET heartbeat_at = ?, updated_at = ?
                WHERE job_id = ?
                AND status = ?
                AND claimed_by = ?
            """, (now, now, job_id, self.STATUS_RUNNING, self._worker_id))

            success = cursor.rowcount > 0
            if not success:
                logger.warning(f"Heartbeat failed for job {job_id}")
            return success

    def complete_job(
        self,
        job_id: str,
        result_url: str,
        thumbnail_url: str = "",
        quality_snapshot: Optional[Dict[str, Any]] = None,
        seo_snapshot: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        작업 완료 처리.

        Returns:
            bool: 완료 처리 성공 여부
        """
        now = now_utc()
        with self.connection() as conn:
            cursor = conn.execute("""
                UPDATE jobs
                SET status = ?,
                    result_url = ?,
                    thumbnail_url = ?,
                    quality_snapshot = ?,
                    seo_snapshot = ?,
                    prepared_payload = '{}',
                    error_code = '',
                    error_message = '',
                    completed_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                AND status = ?
            """, (
                self.STATUS_COMPLETED,
                result_url,
                thumbnail_url,
                json.dumps(quality_snapshot or {}),
                json.dumps(seo_snapshot or {}),
                now,
                now,
                job_id,
                self.STATUS_RUNNING,
            ))

            if cursor.rowcount > 0:
                self._log_event(conn, job_id, "completed", {
                    "result_url": result_url,
                })
                logger.info(f"Job completed: {job_id} -> {result_url}")
                return True

            logger.warning(f"Complete failed for job {job_id}")
            return False

    def update_quality_snapshot(self, job_id: str, snapshot: Dict[str, Any]) -> None:
        """quality_snapshot JSON을 갱신한다."""
        with self.connection() as conn:
            conn.execute(
                "UPDATE jobs SET quality_snapshot = ?, updated_at = ? WHERE job_id = ?",
                (json.dumps(snapshot or {}), now_utc(), job_id),
            )

    def archive_post_text(
        self,
        *,
        job_id: str,
        title: str,
        final_content: str,
        tags: Optional[List[str]] = None,
        category: str = "",
        source_type: str = "published_draft",
        quality_snapshot: Optional[Dict[str, Any]] = None,
        result_url: str = "",
        image_manifest: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """완료된 글의 텍스트와 품질 메타데이터를 가볍게 보존한다."""
        normalized_job_id = str(job_id or "").strip()
        if not normalized_job_id:
            return False

        snapshot = quality_snapshot if isinstance(quality_snapshot, dict) else {}
        insight_quality = snapshot.get("insight_quality", {}) if isinstance(snapshot, dict) else {}
        try:
            quality_score = float(snapshot.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            quality_score = 0.0
        try:
            insight_score = float(insight_quality.get("overall_score", 0.0) or 0.0) if isinstance(insight_quality, dict) else 0.0
        except (TypeError, ValueError):
            insight_score = 0.0
        manual_revision_applied = 1 if bool(snapshot.get("manual_revision_applied", False)) else 0

        now = now_utc()
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO post_text_archives (
                    job_id,
                    title,
                    final_content,
                    tags_json,
                    category,
                    source_type,
                    quality_score,
                    insight_score,
                    manual_revision_applied,
                    result_url,
                    image_manifest_json,
                    review_status,
                    review_updated_at,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    title = excluded.title,
                    final_content = excluded.final_content,
                    tags_json = excluded.tags_json,
                    category = excluded.category,
                    source_type = excluded.source_type,
                    quality_score = excluded.quality_score,
                    insight_score = excluded.insight_score,
                    manual_revision_applied = excluded.manual_revision_applied,
                    result_url = excluded.result_url,
                    image_manifest_json = excluded.image_manifest_json,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_job_id,
                    str(title or "").strip(),
                    str(final_content or "").strip(),
                    json.dumps(tags or [], ensure_ascii=False),
                    str(category or "").strip(),
                    str(source_type or "published_draft").strip(),
                    quality_score,
                    insight_score,
                    manual_revision_applied,
                    str(result_url or "").strip(),
                    json.dumps(image_manifest or {}, ensure_ascii=False),
                    "pending",
                    "",
                    now,
                    now,
                ),
            )
        return cursor.rowcount > 0

    def update_post_archive_review_status(self, job_id: str, review_status: str) -> bool:
        """임시저장 확인 링크의 사용자 검토 상태를 갱신한다."""
        normalized_job_id = str(job_id or "").strip()
        normalized_status = str(review_status or "").strip().lower()
        if not normalized_job_id or normalized_status not in {"pending", "confirmed", "held"}:
            return False

        now = now_utc()
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE post_text_archives
                SET review_status = ?,
                    review_updated_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (normalized_status, now, now, normalized_job_id),
            )
        return cursor.rowcount > 0

    def get_post_text_archive(self, job_id: str) -> Optional[Dict[str, Any]]:
        """보존된 텍스트 아카이브 1건을 조회한다."""
        normalized_job_id = str(job_id or "").strip()
        if not normalized_job_id:
            return None
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM post_text_archives
                WHERE job_id = ?
                LIMIT 1
                """,
                (normalized_job_id,),
            ).fetchone()
        if not row:
            return None
        return {key: row[key] for key in row.keys()}

    def list_post_text_archives(
        self,
        *,
        limit: int = 20,
        manual_revision_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """최근 보존 글과 수정본 반영 기록을 운영 화면용으로 조회한다."""
        safe_limit = max(1, min(int(limit or 20), 100))
        where_clause = "WHERE manual_revision_applied = 1" if manual_revision_only else ""
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM post_text_archives
                {where_clause}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()

        return [{key: row[key] for key in row.keys()} for row in rows]

    @staticmethod
    def _normalize_feedback_suggestion_text(raw_text: str) -> str:
        """피드백 제안 텍스트를 집계용으로 정규화한다."""
        collapsed = re.sub(r"\s+", " ", str(raw_text or "").strip())
        return collapsed[:280]

    @staticmethod
    def _hash_feedback_suggestion(normalized_text: str) -> str:
        """정규화된 제안 텍스트를 짧은 해시로 변환한다."""
        return hashlib.sha256(normalized_text.lower().encode("utf-8")).hexdigest()[:16]

    def _serialize_feedback_candidate_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        """feedback_rule_candidates row를 dict로 직렬화한다."""
        return {
            "id": str(row["id"]),
            "suggestion_hash": str(row["suggestion_hash"]),
            "suggestion_text": str(row["suggestion_text"]),
            "mention_count": int(row["mention_count"] or 0),
            "priority_score": float(row["priority_score"] or 0.0),
            "avg_visual_score": float(row["avg_visual_score"] or 0.0),
            "status": str(row["status"] or "observing"),
            "first_seen_at": str(row["first_seen_at"] or ""),
            "last_seen_at": str(row["last_seen_at"] or ""),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "notified_at": str(row["notified_at"] or ""),
            "remind_at": str(row["remind_at"] or ""),
            "answered_at": str(row["answered_at"] or ""),
            "callback_token": str(row["callback_token"] or ""),
            "callback_expires_at": str(row["callback_expires_at"] or ""),
            "meta_json": str(row["meta_json"] or "{}"),
        }

    def _serialize_feedback_active_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        """feedback_rule_active row를 dict로 직렬화한다."""
        return {
            "id": str(row["id"]),
            "candidate_id": str(row["candidate_id"]),
            "suggestion_hash": str(row["suggestion_hash"]),
            "rule_text": str(row["rule_text"]),
            "status": str(row["status"] or "active"),
            "activated_at": str(row["activated_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "baseline_score": float(row["baseline_score"] or 0.0),
            "avg_after_score": float(row["avg_after_score"] or 0.0),
            "applied_post_count": int(row["applied_post_count"] or 0),
            "decision_score": float(row["decision_score"] or 0.0),
            "last_evaluated_at": str(row["last_evaluated_at"] or ""),
            "meta_json": str(row["meta_json"] or "{}"),
        }

    def record_feedback_suggestion_observation(
        self,
        *,
        suggestion_text: str,
        visual_score: float,
        observed_at: str = "",
    ) -> Optional[Dict[str, Any]]:
        """VLM 제안 1건을 후보 집계에 반영하고 승격 여부를 반환한다."""
        normalized_text = self._normalize_feedback_suggestion_text(suggestion_text)
        if not normalized_text:
            return None

        suggestion_hash = self._hash_feedback_suggestion(normalized_text)
        now = str(observed_at or "").strip() or now_utc()
        score_value = float(visual_score or 0.0)
        recent_cutoff = add_seconds(now, -(7 * 24 * 3600))
        promoted = False

        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM feedback_rule_candidates
                WHERE suggestion_hash = ?
                LIMIT 1
                """,
                (suggestion_hash,),
            ).fetchone()

            if row:
                previous_count = int(row["mention_count"] or 0)
                mention_count = previous_count + 1
                prev_avg = float(row["avg_visual_score"] or 0.0)
                avg_score = ((prev_avg * previous_count) + score_value) / max(1, mention_count)
                was_recent = str(row["last_seen_at"] or "") >= recent_cutoff
                recent_weight = 1.2 if was_recent else 1.0
                priority_score = round(float(mention_count) * recent_weight, 3)

                conn.execute(
                    """
                    UPDATE feedback_rule_candidates
                    SET suggestion_text = ?,
                        mention_count = ?,
                        priority_score = ?,
                        avg_visual_score = ?,
                        last_seen_at = ?,
                        updated_at = ?
                    WHERE suggestion_hash = ?
                    """,
                    (
                        normalized_text,
                        mention_count,
                        priority_score,
                        avg_score,
                        now,
                        now,
                        suggestion_hash,
                    ),
                )
            else:
                candidate_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO feedback_rule_candidates (
                        id,
                        suggestion_hash,
                        suggestion_text,
                        mention_count,
                        priority_score,
                        avg_visual_score,
                        status,
                        first_seen_at,
                        last_seen_at,
                        created_at,
                        updated_at,
                        meta_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        suggestion_hash,
                        normalized_text,
                        1,
                        1.0,
                        score_value,
                        "observing",
                        now,
                        now,
                        now,
                        now,
                        "{}",
                    ),
                )

            latest = conn.execute(
                """
                SELECT *
                FROM feedback_rule_candidates
                WHERE suggestion_hash = ?
                LIMIT 1
                """,
                (suggestion_hash,),
            ).fetchone()
            if not latest:
                return None

            status = str(latest["status"] or "observing")
            mention_count = int(latest["mention_count"] or 0)
            can_promote = status in {"observing", "snoozed_timeout"}
            min_observation = max(1, int(constants.FEEDBACK_MIN_OBSERVATION_COUNT))
            if can_promote and mention_count >= min_observation:
                active = conn.execute(
                    """
                    SELECT id
                    FROM feedback_rule_active
                    WHERE suggestion_hash = ?
                    AND status = 'active'
                    LIMIT 1
                    """,
                    (suggestion_hash,),
                ).fetchone()
                if not active:
                    conn.execute(
                        """
                        UPDATE feedback_rule_candidates
                        SET status = 'pending_approval',
                            notified_at = '',
                            remind_at = '',
                            answered_at = '',
                            callback_token = '',
                            callback_expires_at = '',
                            updated_at = ?
                        WHERE suggestion_hash = ?
                        """,
                        (now, suggestion_hash),
                    )
                    promoted = True
                    latest = conn.execute(
                        """
                        SELECT *
                        FROM feedback_rule_candidates
                        WHERE suggestion_hash = ?
                        LIMIT 1
                        """,
                        (suggestion_hash,),
                    ).fetchone()

            if not latest:
                return None
            payload = self._serialize_feedback_candidate_row(latest)
            payload["promoted"] = promoted
            return payload

    def list_feedback_candidates_to_notify(self, limit: int = 5) -> List[Dict[str, Any]]:
        """알림 전송이 필요한 pending 후보를 반환한다."""
        safe_limit = max(1, min(int(limit or 5), 50))
        now = now_utc()
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM feedback_rule_candidates
                WHERE status = 'pending_approval'
                  AND (
                        COALESCE(notified_at, '') = ''
                        OR (
                            COALESCE(remind_at, '') != ''
                            AND remind_at <= ?
                        )
                  )
                ORDER BY priority_score DESC, last_seen_at DESC
                LIMIT ?
                """,
                (now, safe_limit),
            ).fetchall()
        return [self._serialize_feedback_candidate_row(row) for row in rows]

    def prepare_feedback_candidate_notification(
        self,
        candidate_id: str,
        *,
        callback_ttl_hours: int = 24,
    ) -> Optional[Dict[str, Any]]:
        """후보 알림 전송 직전에 callback 토큰을 발급하고 상태를 고정한다."""
        normalized_id = str(candidate_id or "").strip()
        if not normalized_id:
            return None

        now = now_utc()
        ttl_sec = max(1, int(callback_ttl_hours or 24)) * 3600
        expires_at = add_seconds(now, ttl_sec)
        callback_token = secrets.token_hex(8)

        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM feedback_rule_candidates
                WHERE id = ?
                AND status = 'pending_approval'
                LIMIT 1
                """,
                (normalized_id,),
            ).fetchone()
            if not row:
                return None

            conn.execute(
                """
                UPDATE feedback_rule_candidates
                SET callback_token = ?,
                    callback_expires_at = ?,
                    notified_at = ?,
                    remind_at = '',
                    updated_at = ?
                WHERE id = ?
                AND status = 'pending_approval'
                """,
                (callback_token, expires_at, now, now, normalized_id),
            )

            latest = conn.execute(
                """
                SELECT *
                FROM feedback_rule_candidates
                WHERE id = ?
                LIMIT 1
                """,
                (normalized_id,),
            ).fetchone()
            if not latest:
                return None
            payload = self._serialize_feedback_candidate_row(latest)
            payload["callback_token"] = callback_token
            return payload

    def reopen_due_snoozed_feedback_candidates(self, limit: int = 20) -> int:
        """재알림 시점이 도래한 snooze 후보를 pending_approval로 되돌린다."""
        safe_limit = max(1, min(int(limit or 20), 200))
        now = now_utc()
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id
                FROM feedback_rule_candidates
                WHERE status IN ('snoozed_user', 'snoozed_timeout')
                  AND COALESCE(remind_at, '') != ''
                  AND remind_at <= ?
                ORDER BY remind_at ASC
                LIMIT ?
                """,
                (now, safe_limit),
            ).fetchall()
            candidate_ids = [str(row["id"]) for row in rows]
            if not candidate_ids:
                return 0
            for candidate_id in candidate_ids:
                conn.execute(
                    """
                    UPDATE feedback_rule_candidates
                    SET status = 'pending_approval',
                        notified_at = '',
                        answered_at = '',
                        callback_token = '',
                        callback_expires_at = '',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, candidate_id),
                )
            return len(candidate_ids)

    def auto_snooze_stale_feedback_candidates(
        self,
        *,
        stale_hours: int = 24,
        remind_hours: int = 72,
    ) -> int:
        """응답 없는 pending 후보를 timeout snooze 상태로 자동 전환한다."""
        now = now_utc()
        cutoff = add_seconds(now, -(max(1, int(stale_hours or 24)) * 3600))
        remind_at = add_seconds(now, max(1, int(remind_hours or 72)) * 3600)
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE feedback_rule_candidates
                SET status = 'snoozed_timeout',
                    remind_at = ?,
                    callback_token = '',
                    callback_expires_at = '',
                    updated_at = ?
                WHERE status = 'pending_approval'
                  AND COALESCE(answered_at, '') = ''
                  AND COALESCE(notified_at, '') != ''
                  AND notified_at <= ?
                """,
                (remind_at, now, cutoff),
            )
            return int(cursor.rowcount or 0)

    def apply_feedback_candidate_action(
        self,
        *,
        candidate_id: str,
        action: str,
        callback_token: str,
        snooze_hours: Optional[int] = None,
    ) -> Dict[str, Any]:
        """텔레그램 버튼 액션을 후보 상태에 반영한다."""
        normalized_id = str(candidate_id or "").strip()
        normalized_action = str(action or "").strip().lower()
        normalized_token = str(callback_token or "").strip()
        if not normalized_id:
            return {"ok": False, "reason": "missing_candidate_id"}
        if normalized_action not in {"approve", "ignore", "snooze"}:
            return {"ok": False, "reason": "invalid_action"}

        now = now_utc()
        remind_hours_value = (
            int(snooze_hours)
            if snooze_hours is not None
            else int(constants.FEEDBACK_SNOOZE_REMIND_HOURS)
        )
        remind_hours_value = max(1, remind_hours_value)

        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM feedback_rule_candidates
                WHERE id = ?
                LIMIT 1
                """,
                (normalized_id,),
            ).fetchone()
            if not row:
                return {"ok": False, "reason": "candidate_not_found"}

            current_status = str(row["status"] or "")
            if current_status != "pending_approval":
                return {
                    "ok": False,
                    "reason": "already_handled",
                    "status": current_status,
                }

            expected_token = str(row["callback_token"] or "").strip()
            if not expected_token or normalized_token != expected_token:
                return {"ok": False, "reason": "invalid_token"}

            token_expires_at = str(row["callback_expires_at"] or "").strip()
            if token_expires_at and token_expires_at < now:
                return {"ok": False, "reason": "token_expired"}

            if normalized_action == "approve":
                conn.execute(
                    """
                    UPDATE feedback_rule_candidates
                    SET status = 'approved',
                        answered_at = ?,
                        callback_token = '',
                        callback_expires_at = '',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, normalized_id),
                )

                suggestion_hash = str(row["suggestion_hash"] or "").strip()
                rule_text = str(row["suggestion_text"] or "").strip()
                baseline_score = float(row["avg_visual_score"] or 0.0)
                created_rule = False
                expired_rule_id = ""

                existing_active = conn.execute(
                    """
                    SELECT id
                    FROM feedback_rule_active
                    WHERE suggestion_hash = ?
                      AND status = 'active'
                    LIMIT 1
                    """,
                    (suggestion_hash,),
                ).fetchone()

                if not existing_active and suggestion_hash and rule_text:
                    active_count_row = conn.execute(
                        """
                        SELECT COUNT(*) AS cnt
                        FROM feedback_rule_active
                        WHERE status = 'active'
                        """
                    ).fetchone()
                    active_count = int(active_count_row["cnt"] or 0) if active_count_row else 0
                    max_active_rules = max(1, int(constants.FEEDBACK_MAX_CONCURRENT_ACTIVE_RULES))
                    if active_count >= max_active_rules:
                        oldest = conn.execute(
                            """
                            SELECT id
                            FROM feedback_rule_active
                            WHERE status = 'active'
                            ORDER BY activated_at ASC
                            LIMIT 1
                            """
                        ).fetchone()
                        if oldest:
                            expired_rule_id = str(oldest["id"])
                            conn.execute(
                                """
                                UPDATE feedback_rule_active
                                SET status = 'expired',
                                    last_evaluated_at = ?,
                                    updated_at = ?
                                WHERE id = ?
                                """,
                                (now, now, expired_rule_id),
                            )

                    conn.execute(
                        """
                        INSERT INTO feedback_rule_active (
                            id,
                            candidate_id,
                            suggestion_hash,
                            rule_text,
                            status,
                            activated_at,
                            updated_at,
                            baseline_score,
                            avg_after_score,
                            applied_post_count,
                            decision_score,
                            last_evaluated_at,
                            meta_json
                        ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, 0.0, 0, 0.0, '', '{}')
                        """,
                        (
                            str(uuid.uuid4()),
                            normalized_id,
                            suggestion_hash,
                            rule_text,
                            now,
                            now,
                            baseline_score,
                        ),
                    )
                    created_rule = True

                return {
                    "ok": True,
                    "action": "approve",
                    "status": "approved",
                    "candidate_id": normalized_id,
                    "rule_created": created_rule,
                    "expired_rule_id": expired_rule_id,
                }

            if normalized_action == "ignore":
                conn.execute(
                    """
                    UPDATE feedback_rule_candidates
                    SET status = 'ignored',
                        answered_at = ?,
                        callback_token = '',
                        callback_expires_at = '',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, normalized_id),
                )
                return {
                    "ok": True,
                    "action": "ignore",
                    "status": "ignored",
                    "candidate_id": normalized_id,
                }

            remind_at = add_seconds(now, remind_hours_value * 3600)
            conn.execute(
                """
                UPDATE feedback_rule_candidates
                SET status = 'snoozed_user',
                    answered_at = ?,
                    remind_at = ?,
                    callback_token = '',
                    callback_expires_at = '',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, remind_at, now, normalized_id),
            )
            return {
                "ok": True,
                "action": "snooze",
                "status": "snoozed_user",
                "candidate_id": normalized_id,
                "remind_at": remind_at,
            }

    def list_active_feedback_rules(self, limit: int = 3) -> List[Dict[str, Any]]:
        """현재 활성화된 자동 반영 규칙을 최신순으로 조회한다."""
        safe_limit = max(1, min(int(limit or 3), 20))
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM feedback_rule_active
                WHERE status = 'active'
                ORDER BY activated_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [self._serialize_feedback_active_row(row) for row in rows]

    def record_feedback_rule_application(
        self,
        *,
        applied_rules: List[str],
        visual_score: float,
    ) -> int:
        """활성 규칙이 적용된 포스트의 VLM 점수를 active rule 성과에 누적한다."""
        if not applied_rules:
            return 0

        unique_hashes = []
        for raw_rule in applied_rules:
            normalized_text = self._normalize_feedback_suggestion_text(raw_rule)
            if not normalized_text:
                continue
            suggestion_hash = self._hash_feedback_suggestion(normalized_text)
            if suggestion_hash not in unique_hashes:
                unique_hashes.append(suggestion_hash)

        if not unique_hashes:
            return 0

        score_value = float(visual_score or 0.0)
        now = now_utc()
        updated = 0
        with self.connection() as conn:
            for suggestion_hash in unique_hashes:
                row = conn.execute(
                    """
                    SELECT id, applied_post_count, avg_after_score, baseline_score
                    FROM feedback_rule_active
                    WHERE suggestion_hash = ?
                      AND status = 'active'
                    LIMIT 1
                    """,
                    (suggestion_hash,),
                ).fetchone()
                if not row:
                    continue
                prev_count = int(row["applied_post_count"] or 0)
                prev_avg = float(row["avg_after_score"] or 0.0)
                baseline_score = float(row["baseline_score"] or 0.0)
                new_count = prev_count + 1
                new_avg = ((prev_avg * prev_count) + score_value) / max(1, new_count)
                decision_score = new_avg - baseline_score
                conn.execute(
                    """
                    UPDATE feedback_rule_active
                    SET applied_post_count = ?,
                        avg_after_score = ?,
                        decision_score = ?,
                        last_evaluated_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_count,
                        new_avg,
                        decision_score,
                        now,
                        now,
                        str(row["id"]),
                    ),
                )
                updated += 1
        return updated

    def evaluate_feedback_rule_rollbacks(
        self,
        *,
        min_posts: Optional[int] = None,
        noise_floor: Optional[float] = None,
        keep_threshold: Optional[float] = None,
    ) -> Dict[str, int]:
        """활성 규칙 성과를 평가해 유지/롤백 상태를 갱신한다."""
        min_posts_value = max(1, int(min_posts if min_posts is not None else constants.FEEDBACK_DECISION_MIN_POSTS))
        noise_floor_value = float(noise_floor if noise_floor is not None else constants.FEEDBACK_NOISE_FLOOR)
        keep_threshold_value = float(
            keep_threshold if keep_threshold is not None else constants.FEEDBACK_KEEP_THRESHOLD
        )
        now = now_utc()
        kept = 0
        rolled_back = 0
        observed = 0

        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM feedback_rule_active
                WHERE status = 'active'
                  AND applied_post_count >= ?
                ORDER BY activated_at ASC
                """,
                (min_posts_value,),
            ).fetchall()

            for row in rows:
                active_id = str(row["id"])
                suggestion_hash = str(row["suggestion_hash"] or "").strip()
                decision_score = float(row["decision_score"] or 0.0)
                effective_delta = decision_score - noise_floor_value

                if decision_score <= (-1.0 * noise_floor_value):
                    conn.execute(
                        """
                        UPDATE feedback_rule_active
                        SET status = 'rolled_back',
                            last_evaluated_at = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (now, now, active_id),
                    )
                    if suggestion_hash:
                        conn.execute(
                            """
                            UPDATE feedback_rule_candidates
                            SET status = 'rolled_back',
                                updated_at = ?
                            WHERE suggestion_hash = ?
                              AND status IN ('approved', 'pending_approval', 'snoozed_user', 'snoozed_timeout')
                            """,
                            (now, suggestion_hash),
                        )
                    rolled_back += 1
                    continue

                conn.execute(
                    """
                    UPDATE feedback_rule_active
                    SET last_evaluated_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, active_id),
                )
                if effective_delta >= keep_threshold_value:
                    kept += 1
                else:
                    observed += 1

        return {
            "evaluated": kept + observed + rolled_back,
            "kept": kept,
            "observed": observed,
            "rolled_back": rolled_back,
        }

    def fail_job(
        self,
        job_id: str,
        error_code: str,
        error_message: str = "",
        force_final: bool = False,
    ) -> bool:
        """
        작업 실패 처리.

        - 재시도 가능 에러: retry_wait으로 전환
        - 재시도 불가 에러: failed로 전환
        - max_retries 초과: failed로 전환

        Returns:
            bool: 처리 성공 여부
        """
        now = now_utc()

        with self.connection() as conn:
            # 현재 상태 조회
            row = conn.execute(
                "SELECT retry_count, max_retries FROM jobs WHERE job_id = ?",
                (job_id,)
            ).fetchone()

            if not row:
                logger.warning(f"Job not found: {job_id}")
                return False

            retry_count = row["retry_count"]
            max_retries = row["max_retries"]

            # 재시도 가능 여부 판단
            can_retry = (
                not force_final
                and error_code not in self.NON_RETRYABLE_ERRORS
                and retry_count < max_retries
            )

            if can_retry:
                # retry_wait으로 전환
                new_retry_count = retry_count + 1
                delay = calculate_retry_delay(retry_count)
                next_retry_at = add_seconds(now, delay)

                cursor = conn.execute("""
                    UPDATE jobs
                    SET status = ?,
                        retry_count = ?,
                        next_retry_at = ?,
                        error_code = ?,
                        error_message = ?,
                        claimed_at = NULL,
                        claimed_by = NULL,
                        heartbeat_at = NULL,
                        updated_at = ?
                    WHERE job_id = ?
                    AND status = ?
                """, (
                    self.STATUS_RETRY_WAIT,
                    new_retry_count,
                    next_retry_at,
                    error_code,
                    error_message,
                    now,
                    job_id,
                    self.STATUS_RUNNING,
                ))

                if cursor.rowcount > 0:
                    self._log_event(conn, job_id, "retry_scheduled", {
                        "error_code": error_code,
                        "retry_count": new_retry_count,
                        "next_retry_at": next_retry_at,
                        "delay_seconds": delay,
                    })
                    logger.info(
                        f"Job {job_id} scheduled for retry #{new_retry_count} "
                        f"at {next_retry_at} (delay: {delay}s)"
                    )
                    return True

            else:
                final_status = self.STATUS_FAILED_QUALITY if error_code == "QUALITY_REJECTED" else self.STATUS_FAILED
                cursor = conn.execute("""
                    UPDATE jobs
                    SET status = ?,
                        error_code = ?,
                        error_message = ?,
                        claimed_at = NULL,
                        claimed_by = NULL,
                        heartbeat_at = NULL,
                        updated_at = ?
                    WHERE job_id = ?
                    AND status = ?
                """, (
                    final_status,
                    error_code,
                    error_message,
                    now,
                    job_id,
                    self.STATUS_RUNNING,
                ))

                if cursor.rowcount > 0:
                    reason = "non_retryable" if error_code in self.NON_RETRYABLE_ERRORS else "max_retries_exceeded"
                    self._log_event(conn, job_id, "failed", {
                        "error_code": error_code,
                        "reason": reason,
                    })
                    logger.warning(f"Job {job_id} failed permanently: {error_code}")
                    return True

            return False

    def increment_llm_calls(self, job_id: str, count: int = 1) -> int:
        """
        LLM 호출 횟수 증가 (DB 동기화).

        P0 #4 해결: 메모리가 아닌 DB에 직접 저장

        Returns:
            int: 현재 총 호출 횟수
        """
        now = now_utc()
        with self.connection() as conn:
            conn.execute("""
                UPDATE jobs
                SET llm_call_count = llm_call_count + ?,
                    updated_at = ?
                WHERE job_id = ?
            """, (count, now, job_id))

            row = conn.execute(
                "SELECT llm_call_count FROM jobs WHERE job_id = ?",
                (job_id,)
            ).fetchone()

            current_count = row["llm_call_count"] if row else 0
            logger.debug(f"Job {job_id} LLM calls: {current_count}")
            return current_count

    def check_llm_budget(self, job_id: str) -> bool:
        """
        LLM 호출 예산 초과 여부 확인.

        Returns:
            bool: 예산 내이면 True, 초과면 False
        """
        with self.connection() as conn:
            row = conn.execute(
                "SELECT llm_call_count FROM jobs WHERE job_id = ?",
                (job_id,)
            ).fetchone()

            if not row:
                return False

            return row["llm_call_count"] < self.config.max_llm_calls_per_job

    def get_job(self, job_id: str) -> Optional[Job]:
        """Job 조회"""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,)
            ).fetchone()

            return Job.from_row(row) if row else None

    def get_jobs_page(
        self,
        *,
        statuses: Optional[List[str]] = None,
        size: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """작업 목록 페이지 조회 결과(총 건수 + 목록)를 반환한다."""
        safe_size = max(1, min(int(size or 20), 100))
        safe_offset = max(0, int(offset or 0))
        normalized_statuses = [
            str(value).strip()
            for value in (statuses or [])
            if str(value).strip()
        ]

        with self.connection() as conn:
            where_clause = ""
            params: List[Any] = []
            if normalized_statuses:
                placeholders = ",".join(["?"] * len(normalized_statuses))
                where_clause = f" WHERE status IN ({placeholders})"
                params.extend(normalized_statuses)

            total_row = conn.execute(
                "SELECT COUNT(*) AS total FROM jobs" + where_clause,
                tuple(params),
            ).fetchone()
            total = int(total_row["total"]) if total_row else 0

            query = """
                SELECT *
                FROM jobs
            """
            query = query + where_clause + """
                ORDER BY created_at DESC
                LIMIT ?
                OFFSET ?
            """
            rows = conn.execute(
                query,
                tuple(params + [safe_size, safe_offset]),
            ).fetchall()

        return {
            "total": total,
            "items": [Job.from_row(row) for row in rows],
        }

    def get_post_metrics_page(
        self,
        *,
        size: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """post_metrics 페이지 조회 결과(총 건수 + 요약 + 목록)를 반환한다."""
        safe_size = max(1, min(int(size or 20), 100))
        safe_offset = max(0, int(offset or 0))

        with self.connection() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) AS total FROM post_metrics"
            ).fetchone()
            total = int(total_row["total"]) if total_row else 0

            summary_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_posts,
                    COALESCE(SUM(views), 0) AS total_views,
                    COALESCE(SUM(likes), 0) AS total_likes,
                    COALESCE(SUM(comments), 0) AS total_comments,
                    COALESCE(AVG(views), 0.0) AS avg_views
                FROM post_metrics
                """
            ).fetchone()

            rows = conn.execute(
                """
                SELECT
                    pm.post_id,
                    pm.job_id,
                    pm.title,
                    pm.url,
                    pm.published_at,
                    pm.views,
                    pm.likes,
                    pm.comments,
                    pm.shares,
                    pm.ctr,
                    pm.ai_total,
                    pm.seo_score,
                    pm.dup_score,
                    pm.post_score,
                    pm.snapshot_at,
                    j.platform,
                    j.persona_id,
                    j.category
                FROM post_metrics pm
                LEFT JOIN jobs j ON pm.job_id = j.job_id
                ORDER BY pm.snapshot_at DESC
                LIMIT ?
                OFFSET ?
                """,
                (safe_size, safe_offset),
            ).fetchall()

        return {
            "total": total,
            "summary": {
                "total_posts": int(summary_row["total_posts"]) if summary_row else 0,
                "total_views": int(summary_row["total_views"]) if summary_row else 0,
                "total_likes": int(summary_row["total_likes"]) if summary_row else 0,
                "total_comments": int(summary_row["total_comments"]) if summary_row else 0,
                "avg_views": float(summary_row["avg_views"]) if summary_row else 0.0,
            },
            "items": [dict(row) for row in rows],
        }

    def get_dashboard_metrics_snapshot(self, *, today: str) -> Dict[str, Any]:
        """대시보드 메트릭 집계를 위한 원시 통계를 반환한다."""
        normalized_today = str(today or "").strip()
        with self.connection() as conn:
            today_row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM jobs
                WHERE status = 'completed'
                  AND date(updated_at) = ?
                """,
                (normalized_today,),
            ).fetchone()
            total_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM jobs WHERE status = 'completed'"
            ).fetchone()
            vault_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM idea_vault WHERE status = 'pending'"
            ).fetchone()
            vault_total_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM idea_vault"
            ).fetchone()
            llm_rows = conn.execute(
                """
                SELECT
                    metric_type,
                    provider,
                    COUNT(*) AS total_calls,
                    AVG(input_tokens) AS avg_input,
                    AVG(output_tokens) AS avg_output
                FROM job_metrics
                GROUP BY metric_type, provider
                """
            ).fetchall()
            trend_rows = conn.execute(
                """
                SELECT
                    strftime('%Y-%W', measured_at) AS week_key,
                    MIN(substr(measured_at, 1, 10)) AS week_start,
                    AVG(score_per_won) AS avg_score_per_won,
                    AVG(quality_score) AS avg_quality_score
                FROM model_performance_log
                WHERE measured_at >= datetime('now', '-84 days')
                  AND score_per_won IS NOT NULL
                GROUP BY week_key
                ORDER BY week_key ASC
                LIMIT 12
                """
            ).fetchall()
            if not trend_rows:
                trend_rows = conn.execute(
                    """
                    SELECT
                        strftime('%Y-%W', measured_at) AS week_key,
                        MIN(substr(measured_at, 1, 10)) AS week_start,
                        AVG(score_per_won) AS avg_score_per_won,
                        AVG(quality_score) AS avg_quality_score
                    FROM model_performance_log
                    WHERE score_per_won IS NOT NULL
                    GROUP BY week_key
                    ORDER BY week_key ASC
                    LIMIT 12
                    """
                ).fetchall()
            avg_vlm_visual_score = 0.0
            try:
                vlm_row = conn.execute(
                    """
                    SELECT AVG(CAST(json_extract(detail_json, '$.total_score') AS REAL)) AS avg_score
                    FROM job_metrics
                    WHERE metric_type = 'vlm_visual_eval'
                      AND status = 'success'
                      AND datetime(created_at) >= datetime('now', '-7 days')
                    """
                ).fetchone()
                avg_vlm_visual_score = float(vlm_row["avg_score"] or 0.0) if vlm_row else 0.0
            except Exception:
                # json_extract 미지원 환경 대비 파이썬 파싱 폴백
                vlm_rows = conn.execute(
                    """
                    SELECT detail_json
                    FROM job_metrics
                    WHERE metric_type = 'vlm_visual_eval'
                      AND status = 'success'
                      AND datetime(created_at) >= datetime('now', '-7 days')
                    """
                ).fetchall()
                scores: List[float] = []
                for row in vlm_rows:
                    try:
                        detail = json.loads(str(row["detail_json"] or "{}"))
                    except Exception:
                        continue
                    if isinstance(detail, dict):
                        try:
                            scores.append(float(detail.get("total_score", 0.0) or 0.0))
                        except Exception:
                            continue
                avg_vlm_visual_score = (sum(scores) / len(scores)) if scores else 0.0

        return {
            "today_published": int(today_row["cnt"]) if today_row else 0,
            "total_published": int(total_row["cnt"]) if total_row else 0,
            "idea_vault_pending": int(vault_row["cnt"]) if vault_row else 0,
            "idea_vault_total": int(vault_total_row["cnt"]) if vault_total_row else 0,
            "llm_rows": [dict(row) for row in llm_rows],
            "trend_rows": [dict(row) for row in trend_rows],
            "avg_vlm_visual_score": round(avg_vlm_visual_score, 2),
        }

    def list_recent_completed_jobs(
        self,
        limit: int = 200,
        job_kind: Optional[str] = None,
    ) -> List[Job]:
        """최근 완료된 작업 목록을 최신순으로 반환한다."""
        safe_limit = max(1, min(int(limit or 200), 1000))
        normalized_kind = str(job_kind or "").strip().lower()
        with self.connection() as conn:
            if normalized_kind:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM jobs
                    WHERE status = ?
                    AND job_kind = ?
                    ORDER BY COALESCE(NULLIF(completed_at, ''), updated_at) DESC
                    LIMIT ?
                    """,
                    (self.STATUS_COMPLETED, normalized_kind, safe_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM jobs
                    WHERE status = ?
                    ORDER BY COALESCE(NULLIF(completed_at, ''), updated_at) DESC
                    LIMIT ?
                    """,
                    (self.STATUS_COMPLETED, safe_limit),
                ).fetchall()
        return [Job.from_row(row) for row in rows]

    def get_stale_running_jobs(self, now_override: Optional[str] = None) -> List[Job]:
        """
        Lease timeout이 지난 running 상태 작업 조회.

        P0 #3: Reaper가 사용

        Returns:
            List[Job]: stale 상태인 Job 리스트
        """
        now = now_override or now_utc()
        timeout_threshold = add_seconds(now, -self.config.lease_timeout_sec)

        with self.connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM jobs
                WHERE status = ?
                AND heartbeat_at < ?
            """, (self.STATUS_RUNNING, timeout_threshold))

            return [Job.from_row(row) for row in cursor.fetchall()]

    def requeue_stale_job(self, job_id: str, error_code: str = "WORKER_CRASH") -> bool:
        """
        Stale running 작업을 재큐잉.

        P0 #3: Reaper가 호출

        Returns:
            bool: 재큐잉 성공 여부
        """
        return self.fail_job(job_id, error_code, "Worker crashed or timed out")

    def get_my_running_jobs(self) -> List[Job]:
        """
        현재 워커가 claim한 running 작업 조회.

        워커 시작 시 복구용.

        Returns:
            List[Job]: 현재 워커의 running 작업들
        """
        with self.connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM jobs
                WHERE status = ?
                AND claimed_by = ?
            """, (self.STATUS_RUNNING, self._worker_id))

            return [Job.from_row(row) for row in cursor.fetchall()]

    def set_publish_attempt(self, job_id: str, attempt_id: str) -> bool:
        """
        발행 시도 ID 설정 (중복 발행 방지).

        P0 #2: 발행 전 attempt_id 설정

        Returns:
            bool: 설정 성공 여부
        """
        now = now_utc()
        with self.connection() as conn:
            cursor = conn.execute("""
                UPDATE jobs
                SET publish_attempt_id = ?, updated_at = ?
                WHERE job_id = ?
                AND status = ?
            """, (attempt_id, now, job_id, self.STATUS_RUNNING))

            return cursor.rowcount > 0

    def check_already_published(self, job_id: str) -> Optional[str]:
        """
        이미 발행된 작업인지 확인.

        P0 #2: 중복 발행 방지

        Returns:
            Optional[str]: 발행 URL (없으면 None)
        """
        with self.connection() as conn:
            row = conn.execute(
                "SELECT result_url FROM jobs WHERE job_id = ? AND result_url != ''",
                (job_id,)
            ).fetchone()

            return row["result_url"] if row else None

    def get_daily_llm_usage(self) -> int:
        """
        오늘 사용한 총 LLM 호출 횟수.

        Returns:
            int: 오늘 총 호출 횟수
        """
        with self.connection() as conn:
            row = conn.execute("""
                SELECT COALESCE(SUM(llm_call_count), 0) as total
                FROM jobs
                WHERE date(created_at) = date('now')
            """).fetchone()

            return row["total"]

    def get_today_completed_count(self, job_kind: Optional[str] = None) -> int:
        """오늘 완료(completed)된 Job 수를 반환한다."""
        with self.connection() as conn:
            normalized_kind = str(job_kind or "").strip().lower()
            if normalized_kind:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM jobs
                    WHERE status = ?
                    AND job_kind = ?
                    AND date(COALESCE(NULLIF(completed_at, ''), updated_at)) = date('now')
                    """,
                    (self.STATUS_COMPLETED, normalized_kind),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM jobs
                    WHERE status = ?
                    AND date(COALESCE(NULLIF(completed_at, ''), updated_at)) = date('now')
                    """,
                    (self.STATUS_COMPLETED,),
                ).fetchone()
            return int(row["total"]) if row else 0

    def get_today_failed_count(self, job_kind: Optional[str] = None) -> int:
        """오늘 실패(failed)된 Job 수를 반환한다."""
        with self.connection() as conn:
            normalized_kind = str(job_kind or "").strip().lower()
            if normalized_kind:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM jobs
                    WHERE status = ?
                    AND job_kind = ?
                    AND date(updated_at) = date('now')
                    """,
                    (self.STATUS_FAILED, normalized_kind),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM jobs
                    WHERE status = ?
                    AND date(updated_at) = date('now')
                    """,
                    (self.STATUS_FAILED,),
                ).fetchone()
            return int(row["total"]) if row else 0

    def get_last_completed_time(self, job_kind: Optional[str] = None) -> Optional[datetime]:
        """가장 최근 완료된 Job의 완료 시각(UTC)을 반환한다."""
        with self.connection() as conn:
            normalized_kind = str(job_kind or "").strip().lower()
            if normalized_kind:
                row = conn.execute(
                    """
                    SELECT COALESCE(NULLIF(completed_at, ''), updated_at) AS completed_at
                    FROM jobs
                    WHERE status = ?
                    AND job_kind = ?
                    ORDER BY COALESCE(NULLIF(completed_at, ''), updated_at) DESC
                    LIMIT 1
                    """,
                    (self.STATUS_COMPLETED, normalized_kind),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COALESCE(NULLIF(completed_at, ''), updated_at) AS completed_at
                    FROM jobs
                    WHERE status = ?
                    ORDER BY COALESCE(NULLIF(completed_at, ''), updated_at) DESC
                    LIMIT 1
                    """,
                    (self.STATUS_COMPLETED,),
                ).fetchone()
            if not row or not row["completed_at"]:
                return None
            return parse_iso(str(row["completed_at"]))

    def save_prepared_payload(
        self,
        job_id: str,
        payload: Dict[str, Any],
        *,
        mark_ready: bool = True,
    ) -> bool:
        """생성된 초안을 저장한다.

        Args:
            job_id: 대상 잡 ID
            payload: 저장할 발행 페이로드
            mark_ready: True면 ready_to_publish 상태로 전환한다.
        """
        now = now_utc()
        payload_json = json.dumps(payload)
        allowed_statuses = (self.STATUS_RUNNING, self.STATUS_AWAITING_IMAGES)
        success = False
        with self.connection() as conn:
            if mark_ready:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        prepared_payload = ?,
                        claimed_at = NULL,
                        claimed_by = NULL,
                        heartbeat_at = NULL,
                        updated_at = ?
                    WHERE job_id = ?
                    AND status IN (?, ?)
                    """,
                    (
                        self.STATUS_READY,
                        payload_json,
                        now,
                        job_id,
                        *allowed_statuses,
                    ),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET prepared_payload = ?,
                        updated_at = ?
                    WHERE job_id = ?
                    AND status IN (?, ?)
                    """,
                    (
                        payload_json,
                        now,
                        job_id,
                        *allowed_statuses,
                    ),
                )
            if cursor.rowcount > 0:
                self._log_event(
                    conn,
                    job_id,
                    "prepared" if mark_ready else "prepared_cached",
                    {
                        "payload_keys": sorted(payload.keys()),
                        "mark_ready": mark_ready,
                    },
                )
                success = True
        if success and not mark_ready:
            # semi_auto 경로에서 재생성 방지용 캐시를 별도 키에도 보존한다.
            self.set_system_setting(f"prepared_payload_{job_id}", payload_json)
        return success

    def load_prepared_payload(self, job_id: str) -> Dict[str, Any]:
        """저장된 prepared_payload를 반환한다."""
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT prepared_payload
                FROM jobs
                WHERE job_id = ?
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        if not row:
            return {}
        raw = str(row["prepared_payload"] or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass

        cached_raw = self.get_system_setting(f"prepared_payload_{job_id}", "")
        if not cached_raw:
            return {}
        try:
            cached = json.loads(cached_raw)
        except Exception:
            return {}
        return cached if isinstance(cached, dict) else {}

    def clear_prepared_payload(self, job_id: str) -> bool:
        """prepared_payload를 비운다."""
        now = now_utc()
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET prepared_payload = '{}',
                    updated_at = ?
                WHERE job_id = ?
                """,
                (now, job_id),
            )
        self.set_system_setting(f"prepared_payload_{job_id}", "")
        return cursor.rowcount > 0

    def replace_prepared_payload(
        self,
        job_id: str,
        payload: Dict[str, Any],
        *,
        allowed_statuses: Optional[List[str]] = None,
        event_name: str = "prepared_updated",
    ) -> bool:
        """기존 prepared_payload를 상태 전환 없이 교체한다."""
        normalized_job_id = str(job_id or "").strip()
        if not normalized_job_id:
            return False

        normalized_statuses = [
            str(status or "").strip()
            for status in (allowed_statuses or [])
            if str(status or "").strip()
        ]
        payload_json = json.dumps(payload, ensure_ascii=False)
        now = now_utc()
        with self.connection() as conn:
            if normalized_statuses:
                placeholders = ", ".join("?" for _ in normalized_statuses)
                cursor = conn.execute(
                    f"""
                    UPDATE jobs
                    SET prepared_payload = ?,
                        updated_at = ?
                    WHERE job_id = ?
                    AND status IN ({placeholders})
                    """,
                    (payload_json, now, normalized_job_id, *normalized_statuses),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET prepared_payload = ?,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (payload_json, now, normalized_job_id),
                )
            if cursor.rowcount > 0:
                self._log_event(
                    conn,
                    normalized_job_id,
                    str(event_name or "prepared_updated"),
                    {
                        "payload_keys": sorted(payload.keys()),
                        "allowed_statuses": normalized_statuses,
                    },
                )
        success = cursor.rowcount > 0
        if success:
            self.set_system_setting(f"prepared_payload_{normalized_job_id}", payload_json)
        return success

    def update_job_status(self, job_id: str, status: str) -> bool:
        """잡 상태를 갱신한다."""
        normalized_status = str(status or "").strip()
        if not normalized_status:
            return False
        now = now_utc()
        with self.connection() as conn:
            if normalized_status == self.STATUS_RUNNING:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (normalized_status, now, job_id),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        claimed_at = NULL,
                        claimed_by = NULL,
                        heartbeat_at = NULL,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (normalized_status, now, job_id),
                )
            if cursor.rowcount > 0:
                self._log_event(
                    conn,
                    job_id,
                    "status_updated",
                    {"status": normalized_status},
                )
        return cursor.rowcount > 0

    def cancel_job_by_user(self, job_id: str) -> Dict[str, Any]:
        """사용자 요청으로 취소 가능한 대기 작업을 cancelled로 전환한다."""
        normalized_job_id = str(job_id).strip()
        if not normalized_job_id:
            return {
                "ok": False,
                "reason": "not_found",
                "current_status": None,
                "released_idea_locks": 0,
            }

        cancellable_statuses = (
            self.STATUS_QUEUED,
            self.STATUS_RETRY_WAIT,
            self.STATUS_READY,
        )
        now = now_utc()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT status FROM jobs WHERE job_id = ?",
                (normalized_job_id,),
            ).fetchone()
            if not row:
                return {
                    "ok": False,
                    "reason": "not_found",
                    "current_status": None,
                    "released_idea_locks": 0,
                }

            previous_status = str(row["status"] or "").strip()
            if previous_status not in cancellable_statuses:
                return {
                    "ok": False,
                    "reason": "invalid_status",
                    "current_status": previous_status,
                    "released_idea_locks": 0,
                }

            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    next_retry_at = NULL,
                    error_code = ?,
                    error_message = ?,
                    claimed_at = NULL,
                    claimed_by = NULL,
                    heartbeat_at = NULL,
                    updated_at = ?
                WHERE job_id = ?
                AND status IN (?, ?, ?)
                """,
                (
                    self.STATUS_CANCELLED,
                    "USER_CANCELLED",
                    "Cancelled by user request",
                    now,
                    normalized_job_id,
                    *cancellable_statuses,
                ),
            )
            if cursor.rowcount <= 0:
                latest_row = conn.execute(
                    "SELECT status FROM jobs WHERE job_id = ?",
                    (normalized_job_id,),
                ).fetchone()
                latest_status = (
                    str(latest_row["status"] or "").strip()
                    if latest_row
                    else None
                )
                return {
                    "ok": False,
                    "reason": "invalid_status" if latest_row else "not_found",
                    "current_status": latest_status,
                    "released_idea_locks": 0,
                }

            released_idea_locks = 0
            idea_release_error = ""
            try:
                released_cursor = conn.execute(
                    """
                    UPDATE idea_vault
                    SET status = ?, queued_job_id = '', updated_at = ?
                    WHERE queued_job_id = ? AND status = ?
                    """,
                    (
                        self.IDEA_STATUS_PENDING,
                        now,
                        normalized_job_id,
                        self.IDEA_STATUS_QUEUED,
                    ),
                )
                released_idea_locks = max(0, int(released_cursor.rowcount))
            except Exception as exc:
                # 아이디어 락 해제 실패가 취소 자체를 막지 않도록 로그만 남긴다.
                idea_release_error = str(exc)
                logger.warning(
                    "Idea vault release skipped during cancel",
                    extra={"job_id": normalized_job_id, "error": idea_release_error},
                )

            payload: Dict[str, Any] = {
                "previous_status": previous_status,
                "released_idea_locks": released_idea_locks,
            }
            if idea_release_error:
                payload["idea_release_error"] = idea_release_error[:200]
            self._log_event(conn, normalized_job_id, "cancelled_by_user", payload)

            return {
                "ok": True,
                "reason": "cancelled",
                "previous_status": previous_status,
                "current_status": self.STATUS_CANCELLED,
                "released_idea_locks": released_idea_locks,
            }

    def list_awaiting_images_jobs(self) -> List[Job]:
        """이미지 수집 대기 중인 잡 목록을 반환한다."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM jobs
                WHERE status = ?
                ORDER BY created_at ASC
                """,
                (self.STATUS_AWAITING_IMAGES,),
            ).fetchall()
        return [Job.from_row(row) for row in rows]

    def claim_ready_jobs(
        self,
        limit: int = 1,
        now_override: Optional[str] = None,
        job_kind: Optional[str] = None,
        required_tag: Optional[str] = None,
    ) -> List[Job]:
        """발행 가능한 준비 완료 Job을 원자적으로 선점한다."""
        now = now_override or now_utc()
        worker_id = self._worker_id

        with self.connection() as conn:
            where_clauses = [
                "status = ?",
                "scheduled_at <= ?",
            ]
            params: List[Any] = [self.STATUS_READY, now]
            normalized_kind = str(job_kind or "").strip().lower()
            if normalized_kind:
                where_clauses.append("job_kind = ?")
                params.append(normalized_kind)
            normalized_tag = str(required_tag or "").strip()
            if normalized_tag:
                where_clauses.append("tags LIKE ?")
                params.append(f'%"{normalized_tag}"%')

            query = f"""
                UPDATE jobs
                SET status = ?,
                    claimed_at = ?,
                    claimed_by = ?,
                    heartbeat_at = ?,
                    updated_at = ?
                WHERE job_id IN (
                    SELECT job_id FROM jobs
                    WHERE {' AND '.join(where_clauses)}
                    ORDER BY updated_at ASC
                    LIMIT ?
                )
                RETURNING *
            """
            cursor = conn.execute(
                query,
                (
                    self.STATUS_RUNNING,
                    now,
                    worker_id,
                    now,
                    now,
                    *params,
                    limit,
                ),
            )
            jobs = [Job.from_row(row) for row in cursor.fetchall()]
            for job in jobs:
                self._log_event(
                    conn,
                    job.job_id,
                    "publish_claimed",
                    {"worker_id": worker_id},
                )
            return jobs

    def claim_for_publish(
        self,
        limit: int = 1,
        now_override: Optional[str] = None,
        job_kind: Optional[str] = None,
        required_tag: Optional[str] = None,
    ) -> List[Job]:
        """발행 워커용 claim 래퍼."""
        return self.claim_ready_jobs(
            limit=limit,
            now_override=now_override,
            job_kind=job_kind,
            required_tag=required_tag,
        )

    def get_ready_to_publish_count(
        self,
        job_kind: Optional[str] = None,
        required_tag: Optional[str] = None,
    ) -> int:
        """발행 대기(ready) 상태 Job 수를 반환한다."""
        with self.connection() as conn:
            where_clauses = ["status = ?"]
            params: List[Any] = [self.STATUS_READY]
            normalized_kind = str(job_kind or "").strip().lower()
            if normalized_kind:
                where_clauses.append("job_kind = ?")
                params.append(normalized_kind)
            normalized_tag = str(required_tag or "").strip()
            if normalized_tag:
                where_clauses.append("tags LIKE ?")
                params.append(f'%"{normalized_tag}"%')
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM jobs
                WHERE {' AND '.join(where_clauses)}
                """,
                params,
            ).fetchone()
            return int(row["total"]) if row else 0

    def _log_event(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
    ):
        """작업 이벤트 로그 기록"""
        conn.execute("""
            INSERT INTO job_events (job_id, event_type, payload, created_at)
            VALUES (?, ?, ?, ?)
        """, (job_id, event_type, json.dumps(payload or {}), now_utc()))

    def get_job_events(self, job_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """작업 이벤트 조회"""
        with self.connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM job_events
                WHERE job_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (job_id, limit))

            return [
                {
                    "id": row["id"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload"]),
                    "created_at": row["created_at"],
                }
                for row in cursor.fetchall()
            ]

    def update_job_tags(self, job_id: str, tags: List[str], category: str = "") -> bool:
        """파이프라인 실행 중 생성된 태그를 Job에 저장한다."""
        now = now_utc()
        with self.connection() as conn:
            cursor = conn.execute("""
                UPDATE jobs
                SET tags = ?, category = ?, updated_at = ?
                WHERE job_id = ?
            """, (json.dumps(tags), category, now, job_id))
            return cursor.rowcount > 0

    def get_queue_stats(self) -> Dict[str, int]:
        """큐 상태 및 마스터/서브 통계"""
        with self.connection() as conn:
            cursor = conn.execute("""
                SELECT status, COUNT(*) as count
                FROM jobs
                GROUP BY status
            """)
            stats = {row["status"]: row["count"] for row in cursor.fetchall()}

            _row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status='ready_to_publish' AND job_kind='master' THEN 1 ELSE 0 END) AS ready_master,
                    SUM(CASE WHEN status='ready_to_publish' AND job_kind='sub' THEN 1 ELSE 0 END) AS ready_sub,
                    SUM(CASE WHEN status='queued' AND job_kind='master' THEN 1 ELSE 0 END) AS queued_master,
                    SUM(CASE WHEN status='queued' AND job_kind='sub' THEN 1 ELSE 0 END) AS queued_sub
                FROM jobs
                """
            ).fetchone()
            if _row:
                stats["ready_master"] = int(_row["ready_master"] or 0)
                stats["ready_sub"] = int(_row["ready_sub"] or 0)
                stats["queued_master"] = int(_row["queued_master"] or 0)
                stats["queued_sub"] = int(_row["queued_sub"] or 0)
            else:
                stats["ready_master"] = 0
                stats["ready_sub"] = 0
                stats["queued_master"] = 0
                stats["queued_sub"] = 0

            return stats

    def record_job_metric(
        self,
        job_id: str,
        metric_type: str,
        status: str,
        duration_ms: float = 0.0,
        error_code: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        provider: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """작업 단위 메트릭을 기록한다."""
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO job_metrics (
                    job_id,
                    metric_type,
                    status,
                    error_code,
                    duration_ms,
                    input_tokens,
                    output_tokens,
                    provider,
                    detail_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    metric_type,
                    status,
                    error_code,
                    float(duration_ms),
                    max(0, int(input_tokens or 0)),
                    max(0, int(output_tokens or 0)),
                    str(provider or "").strip(),
                    json.dumps(detail or {}),
                    now_utc(),
                ),
            )

    def record_image_generation_log(
        self,
        *,
        post_id: str,
        slot_id: str,
        slot_role: str,
        provider: str,
        status: str,
        latency_ms: float = 0.0,
        fallback_reason: str = "",
        cost_usd: float = 0.0,
        source_url: str = "",
    ) -> None:
        """이미지 생성 단계의 슬롯별 실행 로그를 저장한다."""
        normalized_slot_id = str(slot_id or "").strip()
        normalized_slot_role = str(slot_role or "").strip().lower() or "content"
        normalized_provider = str(provider or "").strip().lower() or "unknown"
        normalized_status = str(status or "").strip().lower() or "failed"
        if not normalized_slot_id:
            return

        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO image_generation_log (
                    id,
                    post_id,
                    slot_id,
                    slot_role,
                    provider,
                    status,
                    latency_ms,
                    fallback_reason,
                    cost_usd,
                    source_url,
                    measured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    str(post_id or "").strip(),
                    normalized_slot_id,
                    normalized_slot_role,
                    normalized_provider,
                    normalized_status,
                    float(latency_ms or 0.0),
                    str(fallback_reason or "").strip(),
                    float(cost_usd or 0.0),
                    str(source_url or "").strip(),
                    now_utc(),
                ),
            )

    def list_image_generation_logs(
        self,
        *,
        post_id: str = "",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """이미지 생성 로그를 최신순으로 조회한다."""
        safe_limit = max(1, min(int(limit or 200), 1000))
        with self.connection() as conn:
            if post_id:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM image_generation_log
                    WHERE post_id = ?
                    ORDER BY measured_at DESC
                    LIMIT ?
                    """,
                    (str(post_id), safe_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM image_generation_log
                    ORDER BY measured_at DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "post_id": str(row["post_id"] or ""),
                "slot_id": str(row["slot_id"]),
                "slot_role": str(row["slot_role"]),
                "provider": str(row["provider"]),
                "status": str(row["status"]),
                "latency_ms": float(row["latency_ms"] or 0.0),
                "fallback_reason": str(row["fallback_reason"] or ""),
                "cost_usd": float(row["cost_usd"] or 0.0),
                "source_url": str(row["source_url"] or ""),
                "measured_at": str(row["measured_at"]),
            }
            for row in rows
        ]

    def upsert_persona_profile(
        self,
        persona_id: str,
        persona_payload: Dict[str, Any],
        profile_payload: Dict[str, Any],
        performance_history: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """페르소나 프로필을 저장/갱신한다."""
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO persona_profiles (
                    persona_id,
                    persona_json,
                    profile_json,
                    performance_history,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(persona_id) DO UPDATE SET
                    persona_json = excluded.persona_json,
                    profile_json = excluded.profile_json,
                    performance_history = excluded.performance_history,
                    updated_at = excluded.updated_at
                """,
                (
                    persona_id,
                    json.dumps(persona_payload),
                    json.dumps(profile_payload),
                    json.dumps(performance_history or []),
                    now_utc(),
                ),
            )

    def get_persona_profile(self, persona_id: str) -> Optional[Dict[str, Any]]:
        """페르소나 프로필 1건을 조회한다."""
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT persona_id, persona_json, profile_json, performance_history, updated_at
                FROM persona_profiles
                WHERE persona_id = ?
                """,
                (persona_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "persona_id": row["persona_id"],
                "persona": json.loads(row["persona_json"] or "{}"),
                "voice_profile": json.loads(row["profile_json"] or "{}"),
                "performance_history": json.loads(row["performance_history"] or "[]"),
                "updated_at": row["updated_at"],
            }

    def list_persona_profiles(self) -> List[Dict[str, Any]]:
        """저장된 페르소나 프로필 목록을 최신순으로 반환한다."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT persona_id, persona_json, profile_json, performance_history, updated_at
                FROM persona_profiles
                ORDER BY updated_at DESC
                """
            ).fetchall()
        results: List[Dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "persona_id": row["persona_id"],
                    "persona": json.loads(row["persona_json"] or "{}"),
                    "voice_profile": json.loads(row["profile_json"] or "{}"),
                    "performance_history": json.loads(row["performance_history"] or "[]"),
                    "updated_at": row["updated_at"],
                }
            )
        return results

    def _serialize_channel_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        """channels 테이블 row를 API 친화 dict로 변환한다."""
        return {
            "channel_id": str(row["channel_id"]),
            "platform": str(row["platform"]),
            "label": str(row["label"]),
            "blog_url": str(row["blog_url"] or ""),
            "persona_id": str(row["persona_id"] or ""),
            "persona_desc": str(row["persona_desc"] or ""),
            "daily_target": int(row["daily_target"] or 0),
            "style_level": int(row["style_level"] or 2),
            "style_model": str(row["style_model"] or ""),
            "publish_delay_minutes": int(row["publish_delay_minutes"] or 90),
            "is_master": bool(int(row["is_master"] or 0)),
            "auth_json": str(row["auth_json"] or "{}"),
            "active": bool(int(row["active"] or 0)),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def list_channels(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        """채널 목록을 조회한다."""
        with self.connection() as conn:
            if include_inactive:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM channels
                    ORDER BY is_master DESC, created_at ASC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM channels
                    WHERE active = 1
                    ORDER BY is_master DESC, created_at ASC
                    """
                ).fetchall()
        return [self._serialize_channel_row(row) for row in rows]

    def get_channel(self, channel_id: str) -> Optional[Dict[str, Any]]:
        """채널 1건을 조회한다."""
        normalized_channel_id = str(channel_id).strip()
        if not normalized_channel_id:
            return None
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM channels WHERE channel_id = ?",
                (normalized_channel_id,),
            ).fetchone()
        return self._serialize_channel_row(row) if row else None

    def has_any_active_channel(self) -> bool:
        """활성 채널 존재 여부를 반환한다."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM channels WHERE active = 1"
            ).fetchone()
        return bool(row and int(row["total"]) > 0)

    def has_active_master_channel(self, exclude_channel_id: str = "") -> bool:
        """활성 마스터 채널 존재 여부를 반환한다."""
        normalized_exclude = str(exclude_channel_id).strip()
        with self.connection() as conn:
            if normalized_exclude:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM channels
                    WHERE active = 1
                    AND is_master = 1
                    AND channel_id != ?
                    """,
                    (normalized_exclude,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM channels
                    WHERE active = 1
                    AND is_master = 1
                    """
                ).fetchone()
        return bool(row and int(row["total"]) > 0)

    def get_active_master_channel(self) -> Optional[Dict[str, Any]]:
        """활성 마스터 채널 1건을 반환한다."""
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM channels
                WHERE active = 1
                AND is_master = 1
                LIMIT 1
                """
            ).fetchone()
        return self._serialize_channel_row(row) if row else None

    def get_active_sub_channels(self) -> List[Dict[str, Any]]:
        """활성 서브 채널 목록을 반환한다."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM channels
                WHERE active = 1
                AND is_master = 0
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [self._serialize_channel_row(row) for row in rows]

    def insert_channel(self, payload: Dict[str, Any]) -> bool:
        """채널을 생성한다."""
        now = now_utc()
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO channels (
                    channel_id,
                    platform,
                    label,
                    blog_url,
                    persona_id,
                    persona_desc,
                    daily_target,
                    style_level,
                    style_model,
                    publish_delay_minutes,
                    is_master,
                    auth_json,
                    active,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload.get("channel_id", "")).strip(),
                    str(payload.get("platform", "")).strip().lower(),
                    str(payload.get("label", "")).strip(),
                    str(payload.get("blog_url", "")).strip(),
                    str(payload.get("persona_id", "")).strip(),
                    str(payload.get("persona_desc", "")).strip(),
                    int(payload.get("daily_target", 0) or 0),
                    int(payload.get("style_level", 2) or 2),
                    str(payload.get("style_model", "")).strip(),
                    int(payload.get("publish_delay_minutes", 90) or 90),
                    1 if bool(payload.get("is_master", False)) else 0,
                    str(payload.get("auth_json", "{}") or "{}"),
                    1 if bool(payload.get("active", True)) else 0,
                    now,
                    now,
                ),
            )
        return cursor.rowcount > 0

    def update_channel_fields(self, channel_id: str, updates: Dict[str, Any]) -> bool:
        """채널 필드를 부분 업데이트한다."""
        normalized_channel_id = str(channel_id).strip()
        if not normalized_channel_id:
            return False

        allowed_fields = {
            "platform",
            "label",
            "blog_url",
            "persona_id",
            "persona_desc",
            "daily_target",
            "style_level",
            "style_model",
            "publish_delay_minutes",
            "is_master",
            "auth_json",
            "active",
        }
        assignments: List[str] = []
        params: List[Any] = []
        for key, value in updates.items():
            if key not in allowed_fields:
                continue
            assignments.append(f"{key} = ?")
            if key in {"daily_target", "style_level", "publish_delay_minutes"}:
                params.append(int(value or 0))
            elif key in {"is_master", "active"}:
                params.append(1 if bool(value) else 0)
            elif key == "platform":
                params.append(str(value or "").strip().lower())
            else:
                params.append(str(value or "").strip())

        if not assignments:
            return False

        assignments.append("updated_at = ?")
        params.append(now_utc())
        params.append(normalized_channel_id)
        with self.connection() as conn:
            cursor = conn.execute(
                f"UPDATE channels SET {', '.join(assignments)} WHERE channel_id = ?",
                tuple(params),
            )
        return cursor.rowcount > 0

    def deactivate_channel_and_cancel_jobs(self, channel_id: str) -> Dict[str, int]:
        """채널을 비활성화하고 queued/ready 서브 잡을 cancelled로 전환한다."""
        normalized_channel_id = str(channel_id).strip()
        if not normalized_channel_id:
            return {"updated_channels": 0, "cancelled_jobs": 0}

        now = now_utc()
        with self.connection() as conn:
            channel_cursor = conn.execute(
                """
                UPDATE channels
                SET active = 0,
                    updated_at = ?
                WHERE channel_id = ?
                """,
                (now, normalized_channel_id),
            )
            jobs_cursor = conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    error_code = ?,
                    error_message = ?,
                    claimed_at = NULL,
                    claimed_by = NULL,
                    heartbeat_at = NULL,
                    updated_at = ?
                WHERE channel_id = ?
                AND job_kind = ?
                AND status IN (?, ?)
                """,
                (
                    self.STATUS_CANCELLED,
                    "CHANNEL_DEACTIVATED",
                    "Channel deactivated by user",
                    now,
                    normalized_channel_id,
                    self.JOB_KIND_SUB,
                    self.STATUS_QUEUED,
                    self.STATUS_READY,
                ),
            )
        return {
            "updated_channels": int(channel_cursor.rowcount),
            "cancelled_jobs": int(jobs_cursor.rowcount),
        }

    def bulk_delete_jobs(
        self,
        statuses: Optional[List[str]] = None,
        channel_id: Optional[str] = None,
    ) -> int:
        """지정된 상태의 작업을 일괄 삭제한다.

        실행 중인 작업(running, queued, retry_wait, ready_to_publish)은 삭제 대상에서
        제외된다. 기본 삭제 대상은 failed, cancelled 상태이다.

        Args:
            statuses: 삭제할 상태 목록. None이면 ["failed", "cancelled"] 사용.
            channel_id: 특정 채널 ID로 필터링 (None이면 전체).

        Returns:
            삭제된 행 수.
        """
        _PROTECTED_STATUSES = {
            self.STATUS_RUNNING,
            self.STATUS_QUEUED,
            self.STATUS_RETRY_WAIT,
            self.STATUS_READY,
        }
        if statuses is None:
            resolved_statuses = [self.STATUS_FAILED, self.STATUS_CANCELLED]
        else:
            # 보호 상태는 삭제 대상에서 강제 제외
            resolved_statuses = [
                s for s in statuses if s not in _PROTECTED_STATUSES
            ]
        if not resolved_statuses:
            return 0

        placeholders = ",".join(["?"] * len(resolved_statuses))
        params: list = list(resolved_statuses)

        if channel_id:
            normalized_channel_id = str(channel_id).strip()
            where_extra = " AND channel_id = ?"
            params.append(normalized_channel_id)
        else:
            where_extra = ""

        with self.connection() as conn:
            cursor = conn.execute(
                f"DELETE FROM jobs WHERE status IN ({placeholders}){where_extra}",
                params,
            )
        return int(cursor.rowcount or 0)

    def get_sub_job_by_master_channel(
        self,
        master_job_id: str,
        channel_id: str,
    ) -> Optional[Job]:
        """마스터/채널 조합의 서브 잡 1건을 조회한다."""
        normalized_master_job_id = str(master_job_id).strip()
        normalized_channel_id = str(channel_id).strip()
        if not normalized_master_job_id or not normalized_channel_id:
            return None
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM jobs
                WHERE master_job_id = ?
                AND channel_id = ?
                LIMIT 1
                """,
                (normalized_master_job_id, normalized_channel_id),
            ).fetchone()
        return Job.from_row(row) if row else None

    def set_system_setting(self, setting_key: str, setting_value: Any) -> None:
        """시스템 설정값을 저장/갱신한다."""
        normalized_key = str(setting_key).strip()
        if not normalized_key:
            raise ValueError("setting_key must not be empty")

        value_text = setting_value
        if not isinstance(setting_value, str):
            value_text = json.dumps(setting_value, ensure_ascii=False)

        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO system_settings(setting_key, setting_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at = excluded.updated_at
                """,
                (normalized_key, str(value_text), now_utc()),
            )

    def get_system_setting(self, setting_key: str, default: str = "") -> str:
        """시스템 설정값 1건을 문자열로 조회한다."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT setting_value FROM system_settings WHERE setting_key = ?",
                (setting_key,),
            ).fetchone()
            if not row:
                return default
            return str(row["setting_value"])

    def get_system_settings(self, setting_keys: Optional[List[str]] = None) -> Dict[str, str]:
        """시스템 설정값 목록을 조회한다."""
        with self.connection() as conn:
            if setting_keys:
                placeholders = ",".join(["?"] * len(setting_keys))
                rows = conn.execute(
                    f"SELECT setting_key, setting_value FROM system_settings WHERE setting_key IN ({placeholders})",
                    tuple(setting_keys),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT setting_key, setting_value FROM system_settings"
                ).fetchall()
        return {str(row["setting_key"]): str(row["setting_value"]) for row in rows}

    def _serialize_vlm_catalog_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        """VLM 카탈로그 row를 dict로 직렬화한다."""
        metadata_text = str(row["metadata_json"] or "{}")
        try:
            metadata_json = json.loads(metadata_text) if metadata_text else {}
            if not isinstance(metadata_json, dict):
                metadata_json = {}
        except Exception:
            metadata_json = {}
        return {
            "provider": str(row["provider"] or "").strip().lower(),
            "client_provider": str(row["client_provider"] or "").strip().lower(),
            "model": str(row["model"] or "").strip(),
            "key_id": str(row["key_id"] or "").strip().lower(),
            "label": str(row["label"] or "").strip(),
            "status": str(row["status"] or "").strip().lower(),
            "supports_image": bool(int(row["supports_image"] or 0)),
            "include_in_competition": bool(int(row["include_in_competition"] or 0)),
            "quality_score": float(row["quality_score"] or 0.0),
            "reliability_score": float(row["reliability_score"] or 0.0),
            "scoring_bias_offset": float(row["scoring_bias_offset"] or 0.0),
            "input_cost_per_1m": float(row["input_cost_per_1m"] or 0.0),
            "output_cost_per_1m": float(row["output_cost_per_1m"] or 0.0),
            "currency": str(row["currency"] or "USD").strip().upper(),
            "max_image_resolution": str(row["max_image_resolution"] or "").strip(),
            "vision_context_window": int(row["vision_context_window"] or 0),
            "error_rate_24h": float(row["error_rate_24h"] or 0.0),
            "avg_latency_ms": float(row["avg_latency_ms"] or 0.0),
            "metadata_json": metadata_json,
            "discovered_at": str(row["discovered_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def upsert_vlm_catalog_entries(self, entries: List[Dict[str, Any]]) -> Dict[str, int]:
        """VLM 모델 카탈로그를 upsert한다."""
        stats = {"inserted": 0, "updated": 0, "unchanged": 0}
        if not entries:
            return stats

        now = now_utc()
        with self.connection() as conn:
            for entry in entries:
                provider = str(entry.get("provider", "")).strip().lower()
                model = str(entry.get("model", "")).strip()
                if not provider or not model:
                    continue

                client_provider = str(entry.get("client_provider", "")).strip().lower()
                if not client_provider:
                    client_provider = f"{provider}_vlm"
                key_id = str(entry.get("key_id", provider)).strip().lower() or provider
                label = str(entry.get("label", model)).strip()
                status = str(entry.get("status", "discovered")).strip().lower() or "discovered"
                supports_image = 1 if bool(entry.get("supports_image", True)) else 0
                include_in_competition = 1 if bool(entry.get("include_in_competition", False)) else 0
                quality_score = float(entry.get("quality_score", 0.0) or 0.0)
                reliability_score = float(entry.get("reliability_score", 0.0) or 0.0)
                scoring_bias_offset = float(entry.get("scoring_bias_offset", 0.0) or 0.0)
                input_cost = float(entry.get("input_cost_per_1m", 0.0) or 0.0)
                output_cost = float(entry.get("output_cost_per_1m", 0.0) or 0.0)
                currency = str(entry.get("currency", "USD")).strip().upper() or "USD"
                max_image_resolution = str(entry.get("max_image_resolution", "")).strip()
                vision_context_window = int(entry.get("vision_context_window", 0) or 0)
                error_rate_24h = float(entry.get("error_rate_24h", 0.0) or 0.0)
                avg_latency_ms = float(entry.get("avg_latency_ms", 0.0) or 0.0)
                metadata_json = entry.get("metadata_json", {})
                if not isinstance(metadata_json, dict):
                    metadata_json = {}
                metadata_text = json.dumps(metadata_json, ensure_ascii=False)

                row = conn.execute(
                    """
                    SELECT *
                    FROM vlm_model_catalog
                    WHERE provider = ? AND model = ?
                    LIMIT 1
                    """,
                    (provider, model),
                ).fetchone()
                if not row:
                    conn.execute(
                        """
                        INSERT INTO vlm_model_catalog (
                            provider, client_provider, model, key_id, label, status,
                            supports_image, include_in_competition,
                            quality_score, reliability_score, scoring_bias_offset,
                            input_cost_per_1m, output_cost_per_1m, currency,
                            max_image_resolution, vision_context_window,
                            error_rate_24h, avg_latency_ms,
                            metadata_json, discovered_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            provider,
                            client_provider,
                            model,
                            key_id,
                            label,
                            status,
                            supports_image,
                            include_in_competition,
                            quality_score,
                            reliability_score,
                            scoring_bias_offset,
                            input_cost,
                            output_cost,
                            currency,
                            max_image_resolution,
                            vision_context_window,
                            error_rate_24h,
                            avg_latency_ms,
                            metadata_text,
                            now,
                            now,
                        ),
                    )
                    stats["inserted"] += 1
                    continue

                current = self._serialize_vlm_catalog_row(row)
                current_status = str(current["status"]).strip().lower()
                next_status = current_status if current_status == "active" and status == "discovered" else status

                changed = any(
                    [
                        current["client_provider"] != client_provider,
                        current["key_id"] != key_id,
                        current["label"] != label,
                        current_status != next_status,
                        int(current["supports_image"]) != supports_image,
                        int(current["include_in_competition"]) != include_in_competition,
                        abs(float(current["quality_score"]) - quality_score) > 1e-9,
                        abs(float(current["reliability_score"]) - reliability_score) > 1e-9,
                        abs(float(current["scoring_bias_offset"]) - scoring_bias_offset) > 1e-9,
                        abs(float(current["input_cost_per_1m"]) - input_cost) > 1e-9,
                        abs(float(current["output_cost_per_1m"]) - output_cost) > 1e-9,
                        str(current["currency"]).upper() != currency,
                        str(current["max_image_resolution"]) != max_image_resolution,
                        int(current["vision_context_window"]) != vision_context_window,
                        abs(float(current["error_rate_24h"]) - error_rate_24h) > 1e-9,
                        abs(float(current["avg_latency_ms"]) - avg_latency_ms) > 1e-9,
                        json.dumps(current["metadata_json"], ensure_ascii=False, sort_keys=True)
                        != json.dumps(metadata_json, ensure_ascii=False, sort_keys=True),
                    ]
                )
                if not changed:
                    stats["unchanged"] += 1
                    continue

                conn.execute(
                    """
                    UPDATE vlm_model_catalog
                    SET client_provider = ?,
                        key_id = ?,
                        label = ?,
                        status = ?,
                        supports_image = ?,
                        include_in_competition = ?,
                        quality_score = ?,
                        reliability_score = ?,
                        scoring_bias_offset = ?,
                        input_cost_per_1m = ?,
                        output_cost_per_1m = ?,
                        currency = ?,
                        max_image_resolution = ?,
                        vision_context_window = ?,
                        error_rate_24h = ?,
                        avg_latency_ms = ?,
                        metadata_json = ?,
                        updated_at = ?
                    WHERE provider = ? AND model = ?
                    """,
                    (
                        client_provider,
                        key_id,
                        label,
                        next_status,
                        supports_image,
                        include_in_competition,
                        quality_score,
                        reliability_score,
                        scoring_bias_offset,
                        input_cost,
                        output_cost,
                        currency,
                        max_image_resolution,
                        vision_context_window,
                        error_rate_24h,
                        avg_latency_ms,
                        metadata_text,
                        now,
                        provider,
                        model,
                    ),
                )
                stats["updated"] += 1
        return stats

    def mark_missing_vlm_models_deprecated(self, source_pairs: List[tuple[str, str]]) -> int:
        """소스에 없는 카탈로그 모델을 deprecated로 전환한다."""
        if not source_pairs:
            return 0
        normalized_pairs = {
            (str(provider).strip().lower(), str(model).strip())
            for provider, model in source_pairs
            if str(provider).strip() and str(model).strip()
        }
        if not normalized_pairs:
            return 0

        now = now_utc()
        changed = 0
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT provider, model, status
                FROM vlm_model_catalog
                WHERE status != 'deprecated'
                """
            ).fetchall()
            for row in rows:
                provider = str(row["provider"] or "").strip().lower()
                model = str(row["model"] or "").strip()
                if (provider, model) in normalized_pairs:
                    continue
                conn.execute(
                    """
                    UPDATE vlm_model_catalog
                    SET status = 'deprecated',
                        updated_at = ?
                    WHERE provider = ? AND model = ?
                    """,
                    (now, provider, model),
                )
                changed += 1
        return changed

    def list_vlm_catalog_entries(self, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        """VLM 카탈로그 목록을 조회한다."""
        safe_limit = max(1, min(1000, int(limit)))
        query = """
            SELECT *
            FROM vlm_model_catalog
        """
        params: List[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(str(status).strip().lower())
        query += " ORDER BY quality_score DESC, reliability_score DESC, updated_at DESC LIMIT ?"
        params.append(safe_limit)

        with self.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._serialize_vlm_catalog_row(row) for row in rows]

    def list_vlm_validation_candidates(self, limit: int = 20) -> List[Dict[str, Any]]:
        """검증 대상 VLM 후보를 조회한다."""
        safe_limit = max(1, min(200, int(limit)))
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM vlm_model_catalog
                WHERE status IN ('discovered', 'shadow')
                ORDER BY
                    CASE status WHEN 'discovered' THEN 0 ELSE 1 END,
                    discovered_at ASC,
                    updated_at ASC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [self._serialize_vlm_catalog_row(row) for row in rows]

    def update_vlm_catalog_status(
        self,
        *,
        provider: str,
        model: str,
        status: str,
        metadata_update: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """VLM 카탈로그 상태를 변경한다."""
        normalized_provider = str(provider or "").strip().lower()
        normalized_model = str(model or "").strip()
        normalized_status = str(status or "").strip().lower()
        if not normalized_provider or not normalized_model or not normalized_status:
            return False

        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT metadata_json
                FROM vlm_model_catalog
                WHERE provider = ? AND model = ?
                LIMIT 1
                """,
                (normalized_provider, normalized_model),
            ).fetchone()
            if not row:
                return False

            metadata_json: Dict[str, Any] = {}
            try:
                metadata_json = json.loads(str(row["metadata_json"] or "{}")) or {}
                if not isinstance(metadata_json, dict):
                    metadata_json = {}
            except Exception:
                metadata_json = {}

            if isinstance(metadata_update, dict) and metadata_update:
                metadata_json.update(metadata_update)

            conn.execute(
                """
                UPDATE vlm_model_catalog
                SET status = ?,
                    metadata_json = ?,
                    updated_at = ?
                WHERE provider = ? AND model = ?
                """,
                (
                    normalized_status,
                    json.dumps(metadata_json, ensure_ascii=False),
                    now_utc(),
                    normalized_provider,
                    normalized_model,
                ),
            )
        return True

    def record_vlm_discovery_event(
        self,
        *,
        event_type: str,
        provider: str,
        model: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """VLM discovery 이벤트를 기록한다."""
        normalized_type = str(event_type or "").strip().lower()
        normalized_provider = str(provider or "").strip().lower()
        normalized_model = str(model or "").strip()
        if not normalized_type or not normalized_provider or not normalized_model:
            return
        detail_json = detail if isinstance(detail, dict) else {}
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO vlm_discovery_events(event_type, provider, model, detail_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    normalized_type,
                    normalized_provider,
                    normalized_model,
                    json.dumps(detail_json, ensure_ascii=False),
                    now_utc(),
                ),
            )

    def update_vlm_catalog_pricing(
        self,
        *,
        provider: str,
        model: str,
        input_cost_per_1m: float,
        output_cost_per_1m: float,
        currency: str = "USD",
        source: str = "official",
        fx_rate: float = 0.0,
    ) -> bool:
        """카탈로그 가격을 갱신하고 변경 시 가격 이력을 남긴다."""
        normalized_provider = str(provider or "").strip().lower()
        normalized_model = str(model or "").strip()
        if not normalized_provider or not normalized_model:
            return False

        input_cost = max(0.0, float(input_cost_per_1m or 0.0))
        output_cost = max(0.0, float(output_cost_per_1m or 0.0))
        normalized_currency = str(currency or "USD").strip().upper() or "USD"
        now = now_utc()

        changed = False
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT input_cost_per_1m, output_cost_per_1m, currency
                FROM vlm_model_catalog
                WHERE provider = ? AND model = ?
                LIMIT 1
                """,
                (normalized_provider, normalized_model),
            ).fetchone()
            if not row:
                return False
            prev_input = float(row["input_cost_per_1m"] or 0.0)
            prev_output = float(row["output_cost_per_1m"] or 0.0)
            prev_currency = str(row["currency"] or "USD").strip().upper()
            changed = (
                abs(prev_input - input_cost) > 1e-12
                or abs(prev_output - output_cost) > 1e-12
                or prev_currency != normalized_currency
            )
            if not changed:
                return False

            conn.execute(
                """
                UPDATE vlm_model_catalog
                SET input_cost_per_1m = ?,
                    output_cost_per_1m = ?,
                    currency = ?,
                    updated_at = ?
                WHERE provider = ? AND model = ?
                """,
                (
                    input_cost,
                    output_cost,
                    normalized_currency,
                    now,
                    normalized_provider,
                    normalized_model,
                ),
            )
            conn.execute(
                """
                INSERT INTO vlm_model_price_history(
                    provider, model, input_cost_per_1m, output_cost_per_1m,
                    currency, source, fx_rate, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_provider,
                    normalized_model,
                    input_cost,
                    output_cost,
                    normalized_currency,
                    str(source or "official").strip().lower(),
                    float(fx_rate or 0.0),
                    now,
                ),
            )
        return changed

    def _serialize_text_model_catalog_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        """텍스트 모델 카탈로그 row를 dict로 직렬화한다."""
        metadata_text = str(row["metadata_json"] or "{}")
        try:
            metadata_json = json.loads(metadata_text) if metadata_text else {}
            if not isinstance(metadata_json, dict):
                metadata_json = {}
        except Exception:
            metadata_json = {}
        return {
            "provider": str(row["provider"] or "").strip().lower(),
            "model": str(row["model"] or "").strip(),
            "key_id": str(row["key_id"] or "").strip().lower(),
            "label": str(row["label"] or "").strip(),
            "status": str(row["status"] or "").strip().lower(),
            "include_in_competition": bool(int(row["include_in_competition"] or 0)),
            "supports_json": bool(int(row["supports_json"] or 0)),
            "supports_tool_calls": bool(int(row["supports_tool_calls"] or 0)),
            "supports_thinking": bool(int(row["supports_thinking"] or 0)),
            "context_window": int(row["context_window"] or 0),
            "max_output_tokens": int(row["max_output_tokens"] or 0),
            "quality_score": float(row["quality_score"] or 0.0),
            "speed_score": float(row["speed_score"] or 0.0),
            "reliability_score": float(row["reliability_score"] or 0.0),
            "input_cost_per_1m": float(row["input_cost_per_1m"] or 0.0),
            "output_cost_per_1m": float(row["output_cost_per_1m"] or 0.0),
            "cache_hit_input_cost_per_1m": float(row["cache_hit_input_cost_per_1m"] or 0.0),
            "currency": str(row["currency"] or "USD").strip().upper(),
            "source_url": str(row["source_url"] or "").strip(),
            "metadata_json": metadata_json,
            "discovered_at": str(row["discovered_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def upsert_text_model_catalog_entries(self, entries: List[Dict[str, Any]]) -> Dict[str, int]:
        """텍스트 모델 카탈로그를 upsert한다."""
        stats = {"inserted": 0, "updated": 0, "unchanged": 0}
        if not entries:
            return stats

        now = now_utc()
        with self.connection() as conn:
            for entry in entries:
                provider = str(entry.get("provider", "")).strip().lower()
                model = str(entry.get("model", "")).strip()
                if not provider or not model:
                    continue

                key_id = str(entry.get("key_id", provider)).strip().lower() or provider
                label = str(entry.get("label", model)).strip()
                status = str(entry.get("status", "discovered")).strip().lower() or "discovered"
                include_in_competition = 1 if bool(entry.get("include_in_competition", False)) else 0
                supports_json = 1 if bool(entry.get("supports_json", False)) else 0
                supports_tool_calls = 1 if bool(entry.get("supports_tool_calls", False)) else 0
                supports_thinking = 1 if bool(entry.get("supports_thinking", False)) else 0
                context_window = int(entry.get("context_window", 0) or 0)
                max_output_tokens = int(entry.get("max_output_tokens", 0) or 0)
                quality_score = float(entry.get("quality_score", 0.0) or 0.0)
                speed_score = float(entry.get("speed_score", 0.0) or 0.0)
                reliability_score = float(entry.get("reliability_score", 0.0) or 0.0)
                input_cost = float(entry.get("input_cost_per_1m", 0.0) or 0.0)
                output_cost = float(entry.get("output_cost_per_1m", 0.0) or 0.0)
                cache_hit_cost = float(entry.get("cache_hit_input_cost_per_1m", 0.0) or 0.0)
                currency = str(entry.get("currency", "USD")).strip().upper() or "USD"
                source_url = str(entry.get("source_url", "")).strip()
                metadata_json = entry.get("metadata_json", {})
                if not isinstance(metadata_json, dict):
                    metadata_json = {}
                metadata_text = json.dumps(metadata_json, ensure_ascii=False)

                row = conn.execute(
                    """
                    SELECT *
                    FROM text_model_catalog
                    WHERE provider = ? AND model = ?
                    LIMIT 1
                    """,
                    (provider, model),
                ).fetchone()
                if not row:
                    conn.execute(
                        """
                        INSERT INTO text_model_catalog (
                            provider, model, key_id, label, status,
                            include_in_competition, supports_json, supports_tool_calls,
                            supports_thinking, context_window, max_output_tokens,
                            quality_score, speed_score, reliability_score,
                            input_cost_per_1m, output_cost_per_1m,
                            cache_hit_input_cost_per_1m, currency, source_url,
                            metadata_json, discovered_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            provider,
                            model,
                            key_id,
                            label,
                            status,
                            include_in_competition,
                            supports_json,
                            supports_tool_calls,
                            supports_thinking,
                            context_window,
                            max_output_tokens,
                            quality_score,
                            speed_score,
                            reliability_score,
                            input_cost,
                            output_cost,
                            cache_hit_cost,
                            currency,
                            source_url,
                            metadata_text,
                            now,
                            now,
                        ),
                    )
                    stats["inserted"] += 1
                    continue

                current = self._serialize_text_model_catalog_row(row)
                current_status = str(current["status"]).strip().lower()
                next_status = current_status if current_status == "active" and status == "discovered" else status
                changed = any(
                    [
                        current["key_id"] != key_id,
                        current["label"] != label,
                        current_status != next_status,
                        int(current["include_in_competition"]) != include_in_competition,
                        int(current["supports_json"]) != supports_json,
                        int(current["supports_tool_calls"]) != supports_tool_calls,
                        int(current["supports_thinking"]) != supports_thinking,
                        int(current["context_window"]) != context_window,
                        int(current["max_output_tokens"]) != max_output_tokens,
                        abs(float(current["quality_score"]) - quality_score) > 1e-9,
                        abs(float(current["speed_score"]) - speed_score) > 1e-9,
                        abs(float(current["reliability_score"]) - reliability_score) > 1e-9,
                        abs(float(current["input_cost_per_1m"]) - input_cost) > 1e-9,
                        abs(float(current["output_cost_per_1m"]) - output_cost) > 1e-9,
                        abs(float(current["cache_hit_input_cost_per_1m"]) - cache_hit_cost) > 1e-9,
                        str(current["currency"]).upper() != currency,
                        str(current["source_url"]) != source_url,
                        json.dumps(current["metadata_json"], ensure_ascii=False, sort_keys=True)
                        != json.dumps(metadata_json, ensure_ascii=False, sort_keys=True),
                    ]
                )
                if not changed:
                    stats["unchanged"] += 1
                    continue

                conn.execute(
                    """
                    UPDATE text_model_catalog
                    SET key_id = ?,
                        label = ?,
                        status = ?,
                        include_in_competition = ?,
                        supports_json = ?,
                        supports_tool_calls = ?,
                        supports_thinking = ?,
                        context_window = ?,
                        max_output_tokens = ?,
                        quality_score = ?,
                        speed_score = ?,
                        reliability_score = ?,
                        input_cost_per_1m = ?,
                        output_cost_per_1m = ?,
                        cache_hit_input_cost_per_1m = ?,
                        currency = ?,
                        source_url = ?,
                        metadata_json = ?,
                        updated_at = ?
                    WHERE provider = ? AND model = ?
                    """,
                    (
                        key_id,
                        label,
                        next_status,
                        include_in_competition,
                        supports_json,
                        supports_tool_calls,
                        supports_thinking,
                        context_window,
                        max_output_tokens,
                        quality_score,
                        speed_score,
                        reliability_score,
                        input_cost,
                        output_cost,
                        cache_hit_cost,
                        currency,
                        source_url,
                        metadata_text,
                        now,
                        provider,
                        model,
                    ),
                )
                stats["updated"] += 1
        return stats

    def mark_missing_text_models_deprecated(
        self,
        source_pairs: List[tuple[str, str]],
        *,
        providers: Optional[List[str]] = None,
    ) -> int:
        """성공적으로 동기화한 provider에서 사라진 텍스트 모델을 deprecated로 전환한다."""
        normalized_pairs = {
            (str(provider).strip().lower(), str(model).strip())
            for provider, model in source_pairs
            if str(provider).strip() and str(model).strip()
        }
        normalized_providers = {
            str(provider).strip().lower()
            for provider in (providers or [])
            if str(provider).strip()
        }
        if not normalized_pairs or not normalized_providers:
            return 0

        now = now_utc()
        changed = 0
        placeholders = ",".join("?" for _ in normalized_providers)
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT provider, model, status
                FROM text_model_catalog
                WHERE status != 'deprecated'
                AND provider IN ({placeholders})
                """,
                tuple(sorted(normalized_providers)),
            ).fetchall()
            for row in rows:
                provider = str(row["provider"] or "").strip().lower()
                model = str(row["model"] or "").strip()
                if (provider, model) in normalized_pairs:
                    continue
                conn.execute(
                    """
                    UPDATE text_model_catalog
                    SET status = 'deprecated',
                        updated_at = ?
                    WHERE provider = ? AND model = ?
                    """,
                    (now, provider, model),
                )
                changed += 1
        return changed

    def list_text_model_catalog_entries(self, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        """텍스트 모델 카탈로그 목록을 조회한다."""
        safe_limit = max(1, min(1000, int(limit)))
        query = """
            SELECT *
            FROM text_model_catalog
        """
        params: List[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(str(status).strip().lower())
        query += " ORDER BY quality_score DESC, speed_score DESC, updated_at DESC LIMIT ?"
        params.append(safe_limit)

        with self.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._serialize_text_model_catalog_row(row) for row in rows]

    def record_text_model_discovery_event(
        self,
        *,
        event_type: str,
        provider: str,
        model: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """텍스트 모델 discovery 이벤트를 기록한다."""
        normalized_type = str(event_type or "").strip().lower()
        normalized_provider = str(provider or "").strip().lower()
        normalized_model = str(model or "").strip()
        if not normalized_type or not normalized_provider or not normalized_model:
            return
        detail_json = detail if isinstance(detail, dict) else {}
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO text_model_discovery_events(event_type, provider, model, detail_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    normalized_type,
                    normalized_provider,
                    normalized_model,
                    json.dumps(detail_json, ensure_ascii=False),
                    now_utc(),
                ),
            )

    def update_text_model_catalog_pricing(
        self,
        *,
        provider: str,
        model: str,
        input_cost_per_1m: float,
        output_cost_per_1m: float,
        cache_hit_input_cost_per_1m: float = 0.0,
        currency: str = "USD",
        source: str = "official",
        fx_rate: float = 0.0,
    ) -> bool:
        """텍스트 모델 가격을 갱신하고 변경 시 가격 이력을 남긴다."""
        normalized_provider = str(provider or "").strip().lower()
        normalized_model = str(model or "").strip()
        if not normalized_provider or not normalized_model:
            return False

        input_cost = max(0.0, float(input_cost_per_1m or 0.0))
        output_cost = max(0.0, float(output_cost_per_1m or 0.0))
        cache_hit_cost = max(0.0, float(cache_hit_input_cost_per_1m or 0.0))
        normalized_currency = str(currency or "USD").strip().upper() or "USD"
        now = now_utc()

        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT input_cost_per_1m, output_cost_per_1m, cache_hit_input_cost_per_1m, currency
                FROM text_model_catalog
                WHERE provider = ? AND model = ?
                LIMIT 1
                """,
                (normalized_provider, normalized_model),
            ).fetchone()
            if not row:
                return False
            changed = (
                abs(float(row["input_cost_per_1m"] or 0.0) - input_cost) > 1e-12
                or abs(float(row["output_cost_per_1m"] or 0.0) - output_cost) > 1e-12
                or abs(float(row["cache_hit_input_cost_per_1m"] or 0.0) - cache_hit_cost) > 1e-12
                or str(row["currency"] or "USD").strip().upper() != normalized_currency
            )
            if not changed:
                return False

            conn.execute(
                """
                UPDATE text_model_catalog
                SET input_cost_per_1m = ?,
                    output_cost_per_1m = ?,
                    cache_hit_input_cost_per_1m = ?,
                    currency = ?,
                    updated_at = ?
                WHERE provider = ? AND model = ?
                """,
                (
                    input_cost,
                    output_cost,
                    cache_hit_cost,
                    normalized_currency,
                    now,
                    normalized_provider,
                    normalized_model,
                ),
            )
            conn.execute(
                """
                INSERT INTO text_model_price_history(
                    provider, model, input_cost_per_1m, output_cost_per_1m,
                    cache_hit_input_cost_per_1m, currency, source, fx_rate, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_provider,
                    normalized_model,
                    input_cost,
                    output_cost,
                    cache_hit_cost,
                    normalized_currency,
                    str(source or "official").strip().lower(),
                    float(fx_rate or 0.0),
                    now,
                ),
            )
        return True

    def _serialize_macro_document_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        """정부 매크로 문서 row를 dict로 직렬화한다."""

        def _json_field(field_name: str) -> Dict[str, Any]:
            try:
                decoded = json.loads(str(row[field_name] or "{}"))
                return decoded if isinstance(decoded, dict) else {}
            except Exception:
                return {}

        def _json_list_field(field_name: str) -> List[Any]:
            try:
                decoded = json.loads(str(row[field_name] or "[]"))
                return decoded if isinstance(decoded, list) else []
            except Exception:
                return []

        return {
            "id": str(row["id"] or ""),
            "source": str(row["source"] or ""),
            "title": str(row["title"] or ""),
            "published_at": str(row["published_at"] or ""),
            "url": str(row["url"] or ""),
            "file_url": str(row["file_url"] or ""),
            "file_type": str(row["file_type"] or ""),
            "attachments_json": _json_list_field("attachments_json"),
            "local_path": str(row["local_path"] or ""),
            "status": str(row["status"] or ""),
            "hash": str(row["hash"] or ""),
            "raw_text": str(row["raw_text"] or ""),
            "parsed_json": _json_field("parsed_json"),
            "metrics_json": _json_field("metrics_json"),
            "insight_json": _json_field("insight_json"),
            "error_message": str(row["error_message"] or ""),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def upsert_macro_document(self, document: Dict[str, Any]) -> Dict[str, Any]:
        """정부 매크로 문서 메타데이터를 저장하고 최신 row를 반환한다."""
        source = str(document.get("source", "")).strip().upper()
        title = str(document.get("title", "")).strip()
        url = str(document.get("url", "")).strip()
        document_hash = str(document.get("hash", "")).strip()
        if not source or not title or not url or not document_hash:
            raise ValueError("source, title, url, hash are required")

        document_id = str(document.get("id", "")).strip() or f"macro-doc-{uuid.uuid4().hex[:12]}"
        now = now_utc()
        parsed_json = document.get("parsed_json", {})
        metrics_json = document.get("metrics_json", {})
        insight_json = document.get("insight_json", {})
        attachments_json = document.get("attachments_json", document.get("attachments", []))
        if not isinstance(parsed_json, dict):
            parsed_json = {}
        if not isinstance(metrics_json, dict):
            metrics_json = {}
        if not isinstance(insight_json, dict):
            insight_json = {}
        if not isinstance(attachments_json, list):
            attachments_json = []

        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO macro_documents(
                    id, source, title, published_at, url, file_url, file_type,
                    attachments_json, local_path, status, hash, raw_text, parsed_json,
                    metrics_json, insight_json, error_message, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hash) DO UPDATE SET
                    title = excluded.title,
                    published_at = excluded.published_at,
                    url = excluded.url,
                    file_url = excluded.file_url,
                    file_type = excluded.file_type,
                    attachments_json = excluded.attachments_json,
                    local_path = CASE
                        WHEN excluded.local_path != '' THEN excluded.local_path
                        ELSE macro_documents.local_path
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    document_id,
                    source,
                    title,
                    str(document.get("published_at", "") or "").strip(),
                    url,
                    str(document.get("file_url", "") or "").strip(),
                    str(document.get("file_type", "") or "").strip().lower(),
                    json.dumps(attachments_json, ensure_ascii=False),
                    str(document.get("local_path", "") or "").strip(),
                    str(document.get("status", "new") or "new").strip().lower(),
                    document_hash,
                    str(document.get("raw_text", "") or ""),
                    json.dumps(parsed_json, ensure_ascii=False),
                    json.dumps(metrics_json, ensure_ascii=False),
                    json.dumps(insight_json, ensure_ascii=False),
                    str(document.get("error_message", "") or ""),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM macro_documents WHERE hash = ? LIMIT 1",
                (document_hash,),
            ).fetchone()
        return self._serialize_macro_document_row(row)

    def update_macro_document_analysis(
        self,
        document_id: str,
        *,
        status: str,
        raw_text: str = "",
        parsed_json: Optional[Dict[str, Any]] = None,
        metrics_json: Optional[Dict[str, Any]] = None,
        insight_json: Optional[Dict[str, Any]] = None,
        error_message: str = "",
    ) -> bool:
        """매크로 문서 분석 결과를 갱신한다."""
        normalized_id = str(document_id or "").strip()
        normalized_status = str(status or "").strip().lower()
        if not normalized_id or not normalized_status:
            return False
        parsed = parsed_json if isinstance(parsed_json, dict) else None
        metrics = metrics_json if isinstance(metrics_json, dict) else None
        insight = insight_json if isinstance(insight_json, dict) else None

        assignments = ["status = ?", "updated_at = ?", "error_message = ?"]
        params: List[Any] = [normalized_status, now_utc(), str(error_message or "")]
        if raw_text:
            assignments.append("raw_text = ?")
            params.append(str(raw_text))
        if parsed is not None:
            assignments.append("parsed_json = ?")
            params.append(json.dumps(parsed, ensure_ascii=False))
        if metrics is not None:
            assignments.append("metrics_json = ?")
            params.append(json.dumps(metrics, ensure_ascii=False))
        if insight is not None:
            assignments.append("insight_json = ?")
            params.append(json.dumps(insight, ensure_ascii=False))
        params.append(normalized_id)

        with self.connection() as conn:
            cursor = conn.execute(
                f"""
                UPDATE macro_documents
                SET {', '.join(assignments)}
                WHERE id = ?
                """,
                tuple(params),
            )
        return int(cursor.rowcount or 0) > 0

    def get_macro_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        """매크로 문서 1건을 조회한다."""
        normalized_id = str(document_id or "").strip()
        if not normalized_id:
            return None
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM macro_documents WHERE id = ? LIMIT 1",
                (normalized_id,),
            ).fetchone()
        return self._serialize_macro_document_row(row) if row else None

    def list_macro_documents(
        self,
        *,
        source: str = "",
        status: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """매크로 문서 목록을 조회한다."""
        safe_limit = max(1, min(500, int(limit)))
        query = "SELECT * FROM macro_documents"
        params: List[Any] = []
        filters: List[str] = []
        if source:
            filters.append("source = ?")
            params.append(str(source).strip().upper())
        if status:
            filters.append("status = ?")
            params.append(str(status).strip().lower())
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY published_at DESC, updated_at DESC LIMIT ?"
        params.append(safe_limit)
        with self.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._serialize_macro_document_row(row) for row in rows]

    def _serialize_macro_candidate_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        """매크로 글 후보 row를 dict로 직렬화한다."""

        def _json_field(field_name: str) -> Dict[str, Any]:
            try:
                decoded = json.loads(str(row[field_name] or "{}"))
                return decoded if isinstance(decoded, dict) else {}
            except Exception:
                return {}

        return {
            "id": str(row["id"] or ""),
            "macro_document_id": str(row["macro_document_id"] or ""),
            "title": str(row["title"] or ""),
            "angle": str(row["angle"] or ""),
            "target_reader": str(row["target_reader"] or ""),
            "outline_json": _json_field("outline_json"),
            "draft_body": str(row["draft_body"] or ""),
            "quality_json": _json_field("quality_json"),
            "status": str(row["status"] or ""),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def replace_macro_blog_candidates(
        self,
        document_id: str,
        candidates: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """문서별 매크로 글 후보를 교체 저장한다."""
        normalized_document_id = str(document_id or "").strip()
        if not normalized_document_id:
            raise ValueError("document_id is required")
        now = now_utc()
        stored_ids: List[str] = []
        with self.connection() as conn:
            conn.execute(
                "DELETE FROM macro_blog_candidates WHERE macro_document_id = ? AND status IN ('draft', 'needs_review')",
                (normalized_document_id,),
            )
            for candidate in candidates:
                title = str(candidate.get("title", "")).strip()
                if not title:
                    continue
                candidate_id = str(candidate.get("id", "")).strip() or f"macro-cand-{uuid.uuid4().hex[:12]}"
                outline_json = candidate.get("outline_json", candidate.get("outline", {}))
                quality_json = candidate.get("quality_json", candidate.get("quality", {}))
                if not isinstance(outline_json, dict):
                    outline_json = {}
                if not isinstance(quality_json, dict):
                    quality_json = {}
                conn.execute(
                    """
                    INSERT INTO macro_blog_candidates(
                        id, macro_document_id, title, angle, target_reader,
                        outline_json, draft_body, quality_json, status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        normalized_document_id,
                        title,
                        str(candidate.get("angle", "") or "").strip(),
                        str(candidate.get("target_reader", "") or "").strip(),
                        json.dumps(outline_json, ensure_ascii=False),
                        str(candidate.get("draft_body", "") or ""),
                        json.dumps(quality_json, ensure_ascii=False),
                        str(candidate.get("status", "draft") or "draft").strip().lower(),
                        now,
                        now,
                    ),
                )
                stored_ids.append(candidate_id)
        if not stored_ids:
            return []
        placeholders = ",".join("?" for _ in stored_ids)
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM macro_blog_candidates WHERE id IN ({placeholders}) ORDER BY created_at",
                tuple(stored_ids),
            ).fetchall()
        return [self._serialize_macro_candidate_row(row) for row in rows]

    def list_macro_blog_candidates(
        self,
        *,
        document_id: str = "",
        status: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """매크로 글 후보 목록을 조회한다."""
        safe_limit = max(1, min(500, int(limit)))
        query = "SELECT * FROM macro_blog_candidates"
        params: List[Any] = []
        filters: List[str] = []
        if document_id:
            filters.append("macro_document_id = ?")
            params.append(str(document_id).strip())
        if status:
            filters.append("status = ?")
            params.append(str(status).strip().lower())
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(safe_limit)
        with self.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._serialize_macro_candidate_row(row) for row in rows]

    def get_macro_blog_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        """매크로 글 후보 1건을 조회한다."""
        normalized_id = str(candidate_id or "").strip()
        if not normalized_id:
            return None
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM macro_blog_candidates WHERE id = ? LIMIT 1",
                (normalized_id,),
            ).fetchone()
        return self._serialize_macro_candidate_row(row) if row else None

    def update_macro_blog_candidate_status(
        self,
        candidate_id: str,
        *,
        status: str,
        draft_body: str = "",
    ) -> bool:
        """매크로 글 후보 상태와 선택적 초안 본문을 갱신한다."""
        normalized_id = str(candidate_id or "").strip()
        normalized_status = str(status or "").strip().lower()
        if not normalized_id or not normalized_status:
            return False
        assignments = ["status = ?", "updated_at = ?"]
        params: List[Any] = [normalized_status, now_utc()]
        if draft_body:
            assignments.append("draft_body = ?")
            params.append(str(draft_body))
        params.append(normalized_id)
        with self.connection() as conn:
            cursor = conn.execute(
                f"""
                UPDATE macro_blog_candidates
                SET {', '.join(assignments)}
                WHERE id = ?
                """,
                tuple(params),
            )
        return int(cursor.rowcount or 0) > 0

    def get_vlm_recent_metrics(self, *, provider: str, model: str, hours: int = 24) -> Dict[str, Any]:
        """최근 VLM 평가 지표를 반환한다."""
        normalized_provider = str(provider or "").strip().lower()
        normalized_model = str(model or "").strip().lower()
        if not normalized_provider or not normalized_model:
            return {
                "total": 0,
                "success": 0,
                "failed": 0,
                "success_rate": 0.0,
                "avg_latency_ms": 0.0,
            }

        threshold = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        success = 0
        failed = 0
        latency_values: List[float] = []

        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT status, duration_ms, provider, detail_json
                FROM job_metrics
                WHERE metric_type = 'vlm_visual_eval'
                AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT 500
                """,
                (threshold,),
            ).fetchall()

        for row in rows:
            metric_provider = str(row["provider"] or "").strip().lower()
            detail_text = str(row["detail_json"] or "{}")
            try:
                detail_json = json.loads(detail_text) if detail_text else {}
                if not isinstance(detail_json, dict):
                    detail_json = {}
            except Exception:
                detail_json = {}

            detail_provider = str(detail_json.get("provider_used", "")).strip().lower()
            detail_model = str(detail_json.get("model_used", "")).strip().lower()
            provider_candidates = {metric_provider, detail_provider}
            if normalized_provider not in provider_candidates and f"{normalized_provider}_vlm" not in provider_candidates:
                continue
            if detail_model and detail_model != normalized_model:
                continue

            status = str(row["status"] or "").strip().lower()
            if status == "success" and not str(detail_json.get("error", "")).strip():
                success += 1
            else:
                failed += 1

            duration_ms = float(row["duration_ms"] or 0.0)
            if duration_ms > 0:
                latency_values.append(duration_ms)

        total = success + failed
        avg_latency_ms = (sum(latency_values) / len(latency_values)) if latency_values else 0.0
        return {
            "total": int(total),
            "success": int(success),
            "failed": int(failed),
            "success_rate": float(success / total) if total > 0 else 0.0,
            "avg_latency_ms": float(avg_latency_ms),
        }

    def put_vlm_eval_cache(
        self,
        *,
        visual_hash: str,
        model_key: str,
        result: Dict[str, Any],
        ttl_hours: int = 24,
        dom_hash: str = "",
    ) -> None:
        """VLM 평가 결과를 캐시에 저장한다."""
        normalized_hash = str(visual_hash or "").strip()
        normalized_model_key = str(model_key or "").strip().lower()
        if not normalized_hash or not normalized_model_key:
            return

        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(hours=max(1, int(ttl_hours)))).strftime("%Y-%m-%dT%H:%M:%SZ")
        now_text = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO vlm_eval_cache(
                    visual_hash, dom_hash, model_key, result_json, created_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_hash,
                    str(dom_hash or "").strip(),
                    normalized_model_key,
                    json.dumps(result or {}, ensure_ascii=False),
                    now_text,
                    expires_at,
                ),
            )

    def get_vlm_eval_cache(self, *, visual_hash: str, model_key: str) -> Optional[Dict[str, Any]]:
        """VLM 평가 캐시를 조회한다."""
        normalized_hash = str(visual_hash or "").strip()
        normalized_model_key = str(model_key or "").strip().lower()
        if not normalized_hash or not normalized_model_key:
            return None

        now_text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT result_json
                FROM vlm_eval_cache
                WHERE visual_hash = ?
                AND model_key = ?
                AND expires_at > ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (normalized_hash, normalized_model_key, now_text),
            ).fetchone()
        if not row:
            return None
        try:
            decoded = json.loads(str(row["result_json"] or "{}"))
            return decoded if isinstance(decoded, dict) else None
        except Exception:
            return None

    def add_idea_vault_items(self, items: List[Dict[str, Any]]) -> int:
        """아이디어 창고 아이템을 대량 저장한다.

        source_url 이 제공된 경우 UNIQUE INDEX 를 통해 중복 URL 을 자동 차단한다.
        중복 충돌 시 해당 아이템을 조용히 건너뛴다(OR IGNORE).
        """
        if not items:
            return 0

        now = now_utc()
        inserted = 0
        with self.connection() as conn:
            for item in items:
                raw_text = str(item.get("raw_text", "")).strip()
                if not raw_text:
                    continue
                mapped_category = str(item.get("mapped_category", "")).strip() or DEFAULT_FALLBACK_CATEGORY
                topic_mode = str(item.get("topic_mode", "cafe")).strip().lower() or "cafe"
                parser_used = str(item.get("parser_used", "heuristic")).strip() or "heuristic"
                source_url = str(item.get("source_url", "")).strip()
                try:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO idea_vault (
                            raw_text,
                            mapped_category,
                            topic_mode,
                            parser_used,
                            status,
                            queued_job_id,
                            created_at,
                            updated_at,
                            consumed_at,
                            source_url
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            raw_text,
                            mapped_category,
                            topic_mode,
                            parser_used,
                            self.IDEA_STATUS_PENDING,
                            "",
                            now,
                            now,
                            "",
                            source_url,
                        ),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0] > 0:
                        inserted += 1
                except Exception:
                    # 중복 URL 등 예외 시 조용히 건너뜀
                    pass
        return inserted

    def get_idea_vault_pending_count(self) -> int:
        """아이디어 창고 pending 재고 수량을 반환한다."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM idea_vault WHERE status = ?",
                (self.IDEA_STATUS_PENDING,),
            ).fetchone()
        return int(row["count"]) if row else 0

    def get_idea_vault_stats(self) -> Dict[str, int]:
        """아이디어 창고 상태별 수량을 반환한다."""
        stats: Dict[str, int] = {
            "total": 0,
            self.IDEA_STATUS_PENDING: 0,
            self.IDEA_STATUS_QUEUED: 0,
            self.IDEA_STATUS_CONSUMED: 0,
        }
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM idea_vault
                GROUP BY status
                """
            ).fetchall()
        for row in rows:
            status_name = str(row["status"])
            count = int(row["count"])
            stats["total"] += count
            stats[status_name] = count
        return stats

    def count_idea_vault_items(self, status_filter: Optional[str] = None) -> int:
        """아이디어 창고 레코드 총 건수를 반환한다."""
        with self.connection() as conn:
            if status_filter:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM idea_vault WHERE status = ?",
                    (status_filter,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM idea_vault",
                ).fetchone()
        return int(row["count"]) if row else 0

    def list_idea_vault_items(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        status_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """아이디어 창고 목록을 최신순으로 조회한다."""
        safe_limit = max(1, min(500, int(limit)))
        safe_offset = max(0, int(offset))
        with self.connection() as conn:
            if status_filter:
                rows = conn.execute(
                    """
                    SELECT id, raw_text, mapped_category, topic_mode, status, queued_job_id, created_at, updated_at, consumed_at
                    FROM idea_vault
                    WHERE status = ?
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (status_filter, safe_limit, safe_offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, raw_text, mapped_category, topic_mode, status, queued_job_id, created_at, updated_at, consumed_at
                    FROM idea_vault
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (safe_limit, safe_offset),
                ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "raw_text": str(row["raw_text"]),
                "mapped_category": str(row["mapped_category"] or ""),
                "topic_mode": str(row["topic_mode"] or "cafe"),
                "status": str(row["status"] or ""),
                "queued_job_id": str(row["queued_job_id"] or ""),
                "created_at": str(row["created_at"] or ""),
                "updated_at": str(row["updated_at"] or ""),
                "consumed_at": str(row["consumed_at"] or ""),
            }
            for row in rows
        ]

    def claim_random_idea_vault_items(self, job_ids: List[str]) -> List[Dict[str, Any]]:
        """pending 아이디어를 랜덤으로 선점해 queued로 전환한다."""
        normalized_job_ids = [str(job_id).strip() for job_id in job_ids if str(job_id).strip()]
        if not normalized_job_ids:
            return []

        now = now_utc()
        claimed_items: List[Dict[str, Any]] = []
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, raw_text, mapped_category, topic_mode
                FROM idea_vault
                WHERE status = ?
                ORDER BY RANDOM()
                LIMIT ?
                """,
                (self.IDEA_STATUS_PENDING, len(normalized_job_ids)),
            ).fetchall()
            for index, row in enumerate(rows):
                job_id = normalized_job_ids[index]
                updated = conn.execute(
                    """
                    UPDATE idea_vault
                    SET status = ?, queued_job_id = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        self.IDEA_STATUS_QUEUED,
                        job_id,
                        now,
                        int(row["id"]),
                        self.IDEA_STATUS_PENDING,
                    ),
                )
                if updated.rowcount <= 0:
                    continue
                claimed_items.append(
                    {
                        "id": int(row["id"]),
                        "raw_text": str(row["raw_text"] or ""),
                        "mapped_category": str(row["mapped_category"] or ""),
                        "topic_mode": str(row["topic_mode"] or "cafe"),
                        "queued_job_id": job_id,
                    }
                )
        return claimed_items

    def release_idea_vault_job_lock(self, job_id: str) -> int:
        """선점 후 작업 생성 실패 시 아이디어 잠금을 해제한다."""
        normalized = str(job_id).strip()
        if not normalized:
            return 0
        now = now_utc()
        with self.connection() as conn:
            updated = conn.execute(
                """
                UPDATE idea_vault
                SET status = ?, queued_job_id = '', updated_at = ?
                WHERE queued_job_id = ? AND status = ?
                """,
                (
                    self.IDEA_STATUS_PENDING,
                    now,
                    normalized,
                    self.IDEA_STATUS_QUEUED,
                ),
            )
            return max(0, int(updated.rowcount))

    def mark_idea_vault_consumed_by_job(self, job_id: str) -> int:
        """발행 완료된 작업과 연결된 아이디어를 consumed 처리한다."""
        normalized = str(job_id).strip()
        if not normalized:
            return 0
        now = now_utc()
        with self.connection() as conn:
            updated = conn.execute(
                """
                UPDATE idea_vault
                SET status = ?, consumed_at = ?, updated_at = ?
                WHERE queued_job_id = ? AND status = ?
                """,
                (
                    self.IDEA_STATUS_CONSUMED,
                    now,
                    now,
                    normalized,
                    self.IDEA_STATUS_QUEUED,
                ),
            )
            return max(0, int(updated.rowcount))

    def record_model_performance(
        self,
        *,
        model_id: str,
        provider: str,
        topic_mode: str,
        quality_score: float,
        cost_won: float,
        is_free_model: bool,
        slot_type: str,
        post_id: str = "",
        feedback_source: str = "ai_evaluator",
        measured_at: Optional[str] = None,
    ) -> Optional[str]:
        """모델 성능 로그를 저장한다."""
        normalized_model_id = str(model_id).strip()
        normalized_provider = str(provider).strip().lower()
        normalized_topic_mode = str(topic_mode).strip().lower() or "cafe"
        normalized_slot_type = str(slot_type).strip().lower() or "main"
        if not normalized_model_id or not normalized_provider:
            return None

        safe_quality = float(max(0.0, min(100.0, quality_score)))
        safe_cost = float(max(0.0, cost_won))
        free_flag = 1 if is_free_model or safe_cost <= 0.0 else 0
        score_per_won = None if free_flag else (safe_quality / safe_cost if safe_cost > 0 else None)
        log_id = str(uuid.uuid4())

        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO model_performance_log (
                    id,
                    model_id,
                    provider,
                    topic_mode,
                    quality_score,
                    cost_won,
                    is_free_model,
                    score_per_won,
                    free_model_rank,
                    post_id,
                    slot_type,
                    feedback_source,
                    measured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_id,
                    normalized_model_id,
                    normalized_provider,
                    normalized_topic_mode,
                    safe_quality,
                    safe_cost,
                    free_flag,
                    score_per_won,
                    None,
                    str(post_id).strip(),
                    normalized_slot_type,
                    str(feedback_source).strip() or "ai_evaluator",
                    measured_at or now_utc(),
                ),
            )
        return log_id

    def get_model_performance_summary(
        self,
        *,
        since: str,
        until: Optional[str] = None,
        slot_types: Optional[List[str]] = None,
        topic_mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """기간 내 모델 성능 집계를 반환한다."""
        clauses = ["measured_at >= ?"]
        params: List[Any] = [since]

        if until:
            clauses.append("measured_at < ?")
            params.append(until)

        normalized_slots = [str(slot).strip().lower() for slot in (slot_types or []) if str(slot).strip()]
        if normalized_slots:
            placeholders = ",".join("?" for _ in normalized_slots)
            clauses.append(f"slot_type IN ({placeholders})")
            params.extend(normalized_slots)

        normalized_topic_mode = str(topic_mode or "").strip().lower()
        if normalized_topic_mode:
            clauses.append("topic_mode = ?")
            params.append(normalized_topic_mode)

        where_clause = " AND ".join(clauses)
        query = f"""
            SELECT
                model_id,
                provider,
                COUNT(*) AS samples,
                AVG(quality_score) AS avg_quality_score,
                AVG(cost_won) AS avg_cost_won,
                AVG(score_per_won) AS avg_score_per_won,
                SUM(CASE WHEN is_free_model = 1 THEN 1 ELSE 0 END) AS free_samples
            FROM model_performance_log
            WHERE {where_clause}
            GROUP BY model_id, provider
            ORDER BY avg_quality_score DESC, samples DESC
        """

        with self.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()

        return [
            {
                "model_id": str(row["model_id"] or ""),
                "provider": str(row["provider"] or ""),
                "samples": int(row["samples"] or 0),
                "avg_quality_score": float(row["avg_quality_score"] or 0.0),
                "avg_cost_won": float(row["avg_cost_won"] or 0.0),
                "avg_score_per_won": float(row["avg_score_per_won"] or 0.0) if row["avg_score_per_won"] is not None else None,
                "free_samples": int(row["free_samples"] or 0),
            }
            for row in rows
        ]

    def get_today_eval_job_count(self, today_key: Optional[str] = None) -> int:
        """오늘(KST 기준) eval 슬롯 작업 수를 반환한다."""
        kst = timezone(timedelta(hours=9))
        if today_key:
            try:
                local_day = datetime.strptime(today_key, "%Y-%m-%d").date()
            except ValueError:
                local_day = datetime.now(kst).date()
        else:
            local_day = datetime.now(kst).date()

        start_local = datetime.combine(local_day, datetime.min.time(), tzinfo=kst)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        query = """
            SELECT COUNT(*) AS total
            FROM model_performance_log
            WHERE slot_type = 'eval'
              AND measured_at >= ?
              AND measured_at < ?
        """
        with self.connection() as conn:
            row = conn.execute(query, (start_utc, end_utc)).fetchone()
        return int(row["total"] or 0) if row else 0

    def get_today_competition_job_count(self) -> int:
        """오늘 현재까지 수행된 eval/shadow/challenger 슬롯 작업 수를 반환한다."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        query = """
            SELECT COUNT(*) AS total
            FROM model_performance_log
            WHERE slot_type IN ('eval', 'shadow', 'challenger')
              AND measured_at LIKE ?
        """
        with self.connection() as conn:
            row = conn.execute(query, (f"{today}%",)).fetchone()
        return int(row["total"] or 0) if row else 0

    def get_weekly_competition_state(self, week_start: str) -> Optional[Dict[str, Any]]:
        """주간 경쟁 상태를 조회한다."""
        normalized_week_start = str(week_start).strip()
        if not normalized_week_start:
            return None
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    week_start,
                    phase,
                    candidates,
                    champion_model,
                    challenger_model,
                    early_terminated,
                    apply_at
                FROM weekly_competition_state
                WHERE week_start = ?
                """,
                (normalized_week_start,),
            ).fetchone()
        if not row:
            return None
        try:
            candidates = json.loads(str(row["candidates"] or "[]"))
            if not isinstance(candidates, list):
                candidates = []
        except Exception:
            candidates = []
        return {
            "week_start": str(row["week_start"] or ""),
            "phase": str(row["phase"] or "testing"),
            "candidates": candidates,
            "champion_model": str(row["champion_model"] or ""),
            "challenger_model": str(row["challenger_model"] or ""),
            "early_terminated": bool(int(row["early_terminated"] or 0)),
            "apply_at": str(row["apply_at"] or ""),
        }

    def upsert_weekly_competition_state(
        self,
        *,
        week_start: str,
        phase: str,
        candidates: List[Dict[str, Any]],
        apply_at: str,
        champion_model: str = "",
        challenger_model: str = "",
        early_terminated: bool = False,
    ) -> None:
        """주간 경쟁 상태를 저장/갱신한다."""
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO weekly_competition_state (
                    week_start,
                    phase,
                    candidates,
                    champion_model,
                    challenger_model,
                    early_terminated,
                    apply_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(week_start) DO UPDATE SET
                    phase = excluded.phase,
                    candidates = excluded.candidates,
                    champion_model = excluded.champion_model,
                    challenger_model = excluded.challenger_model,
                    early_terminated = excluded.early_terminated,
                    apply_at = excluded.apply_at
                """,
                (
                    str(week_start).strip(),
                    str(phase).strip() or "testing",
                    json.dumps(candidates, ensure_ascii=False),
                    str(champion_model).strip(),
                    str(challenger_model).strip(),
                    1 if early_terminated else 0,
                    str(apply_at).strip(),
                ),
            )

    def record_champion_history(
        self,
        *,
        week_start: str,
        champion_model: str,
        challenger_model: str,
        avg_champion_score: float,
        topic_mode_scores: Dict[str, float],
        cost_won: float,
        early_terminated: bool = False,
        shadow_only: bool = True,
    ) -> None:
        """주간 챔피언 이력을 저장/갱신한다."""
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO champion_history (
                    week_start,
                    champion_model,
                    challenger_model,
                    avg_champion_score,
                    topic_mode_scores,
                    cost_won,
                    early_terminated,
                    shadow_only
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(week_start) DO UPDATE SET
                    champion_model = excluded.champion_model,
                    challenger_model = excluded.challenger_model,
                    avg_champion_score = excluded.avg_champion_score,
                    topic_mode_scores = excluded.topic_mode_scores,
                    cost_won = excluded.cost_won,
                    early_terminated = excluded.early_terminated,
                    shadow_only = excluded.shadow_only
                """,
                (
                    str(week_start).strip(),
                    str(champion_model).strip(),
                    str(challenger_model).strip(),
                    float(max(0.0, min(100.0, avg_champion_score))),
                    json.dumps(topic_mode_scores or {}, ensure_ascii=False),
                    float(max(0.0, cost_won)),
                    1 if early_terminated else 0,
                    1 if shadow_only else 0,
                ),
            )

    def list_champion_history(self, *, limit: int = 4) -> List[Dict[str, Any]]:
        """최근 챔피언 이력을 반환한다."""
        safe_limit = max(1, min(52, int(limit or 4)))
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    week_start,
                    champion_model,
                    challenger_model,
                    avg_champion_score,
                    topic_mode_scores,
                    cost_won,
                    early_terminated,
                    shadow_only
                FROM champion_history
                ORDER BY week_start DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()

        payload: List[Dict[str, Any]] = []
        for row in rows:
            try:
                topic_scores_raw = json.loads(str(row["topic_mode_scores"] or "{}"))
                topic_scores = topic_scores_raw if isinstance(topic_scores_raw, dict) else {}
            except Exception:
                topic_scores = {}
            payload.append(
                {
                    "week_start": str(row["week_start"] or ""),
                    "champion_model": str(row["champion_model"] or ""),
                    "challenger_model": str(row["challenger_model"] or ""),
                    "avg_champion_score": float(row["avg_champion_score"] or 0.0),
                    "topic_mode_scores": topic_scores,
                    "cost_won": float(row["cost_won"] or 0.0),
                    "early_terminated": bool(int(row["early_terminated"] or 0)),
                    "shadow_only": bool(int(row["shadow_only"] or 0)),
                }
            )
        return payload

    # ────────────────────────────────────────────
    # topic_memory CRUD
    # ────────────────────────────────────────────

    def insert_topic_memory(
        self,
        job_id: str,
        title: str,
        keywords: List[str],
        topic_mode: str,
        platform: str,
        persona_id: str,
        summary: str,
        result_url: str,
        quality_score: int,
    ) -> None:
        """발행 완료 후 topic_memory에 기록한다."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO topic_memory
                    (job_id, title, keywords, topic_mode, platform, persona_id,
                     summary, result_url, quality_score, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(job_id),
                    str(title),
                    json.dumps(list(keywords), ensure_ascii=False),
                    str(topic_mode),
                    str(platform),
                    str(persona_id),
                    str(summary)[:400],
                    str(result_url),
                    int(quality_score),
                    now_iso,
                ),
            )

    def query_topic_memory(
        self,
        topic_mode: str = "",
        persona_id: str = "",
        lookback_days: int = 56,
        limit: int = 30,
        min_quality_score: int = 0,
        platform: str = "",
    ) -> List[Dict[str, Any]]:
        """최근 발행 이력을 조회한다. topic_mode/persona/platform 필터 가능."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, lookback_days))).isoformat()

        params: list = [cutoff]
        where_clauses = ["recorded_at >= ?"]
        if topic_mode:
            where_clauses.append("topic_mode = ?")
            params.append(str(topic_mode))
        if persona_id:
            where_clauses.append("persona_id = ?")
            params.append(str(persona_id))
        if platform:
            where_clauses.append("platform = ?")
            params.append(str(platform))
        if min_quality_score > 0:
            where_clauses.append("quality_score >= ?")
            params.append(int(min_quality_score))
        params.append(max(1, min(int(limit), 200)))

        sql = f"""
            SELECT job_id, title, keywords, topic_mode, platform, persona_id,
                   summary, result_url, quality_score, recorded_at
            FROM topic_memory
            WHERE {' AND '.join(where_clauses)}
            ORDER BY recorded_at DESC
            LIMIT ?
        """
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        result = []
        for row in rows:
            try:
                kw = json.loads(row[2]) if row[2] else []
            except Exception:
                kw = []
            result.append({
                "job_id": row[0],
                "title": row[1],
                "keywords": kw,
                "topic_mode": row[3],
                "platform": row[4],
                "persona_id": row[5],
                "summary": row[6],
                "result_url": row[7],
                "quality_score": row[8],
                "recorded_at": row[9],
            })
        return result

    def get_topic_coverage_stats(
        self,
        lookback_days: int = 56,
        platform: str = "",
    ) -> Dict[str, int]:
        """lookback 기간의 topic_mode별 발행 수를 반환한다."""
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, lookback_days))).isoformat()
        params: List[Any] = [cutoff]
        where_clauses = ["recorded_at >= ?"]
        if platform:
            where_clauses.append("platform = ?")
            params.append(str(platform))

        sql = f"""
            SELECT topic_mode, COUNT(*) AS cnt
            FROM topic_memory
            WHERE {' AND '.join(where_clauses)}
            GROUP BY topic_mode
            ORDER BY cnt DESC
        """
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return {
            str(row["topic_mode"]): int(row["cnt"])
            for row in rows
            if row["topic_mode"]
        }

    def upsert_topic_embedding(
        self,
        job_id: str,
        embedding: List[float],
        model_name: str,
    ) -> None:
        """topic_memory 임베딩을 저장/갱신한다."""
        normalized_job_id = str(job_id).strip()
        normalized_model = str(model_name).strip() or "unknown"
        vector = [float(value) for value in list(embedding or [])]
        if not normalized_job_id or not vector:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO topic_memory_embeddings (job_id, embedding_json, model_name, dim, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    embedding_json = excluded.embedding_json,
                    model_name = excluded.model_name,
                    dim = excluded.dim,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_job_id,
                    json.dumps(vector, ensure_ascii=False),
                    normalized_model,
                    len(vector),
                    now_iso,
                ),
            )

    def get_topic_embeddings(
        self,
        job_ids: List[str],
        model_name: str = "",
    ) -> Dict[str, List[float]]:
        """job_id 목록의 임베딩을 조회한다."""
        normalized_ids = [str(job_id).strip() for job_id in job_ids if str(job_id).strip()]
        if not normalized_ids:
            return {}

        placeholders = ",".join("?" for _ in normalized_ids)
        params: List[Any] = list(normalized_ids)
        where_clauses = [f"job_id IN ({placeholders})"]
        normalized_model = str(model_name).strip()
        if normalized_model:
            where_clauses.append("model_name = ?")
            params.append(normalized_model)

        sql = f"""
            SELECT job_id, embedding_json
            FROM topic_memory_embeddings
            WHERE {' AND '.join(where_clauses)}
        """
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        result: Dict[str, List[float]] = {}
        for row in rows:
            try:
                parsed = json.loads(row["embedding_json"]) if row["embedding_json"] else []
                vector = [float(value) for value in parsed]
            except Exception:
                vector = []
            if vector:
                result[str(row["job_id"])] = vector
        return result

    def list_topic_embedding_candidates(
        self,
        topic_mode: str = "",
        platform: str = "",
        lookback_days: int = 56,
        limit: int = 80,
    ) -> List[Dict[str, Any]]:
        """임베딩 계산 대상 topic_memory 후보를 조회한다."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days)))
        ).isoformat()
        params: List[Any] = [cutoff]
        where_clauses = ["recorded_at >= ?"]
        if topic_mode:
            where_clauses.append("topic_mode = ?")
            params.append(str(topic_mode))
        if platform:
            where_clauses.append("platform = ?")
            params.append(str(platform))
        params.append(max(1, min(int(limit), 500)))

        sql = f"""
            SELECT job_id, title, keywords, topic_mode, platform, recorded_at
            FROM topic_memory
            WHERE {' AND '.join(where_clauses)}
            ORDER BY recorded_at DESC
            LIMIT ?
        """
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        payload: List[Dict[str, Any]] = []
        for row in rows:
            try:
                keywords = json.loads(row["keywords"]) if row["keywords"] else []
            except Exception:
                keywords = []
            payload.append(
                {
                    "job_id": str(row["job_id"]),
                    "title": str(row["title"] or ""),
                    "keywords": list(keywords) if isinstance(keywords, list) else [],
                    "topic_mode": str(row["topic_mode"] or ""),
                    "platform": str(row["platform"] or ""),
                    "recorded_at": str(row["recorded_at"] or ""),
                }
            )
        return payload

    def get_keyword_frequencies(
        self,
        topic_mode: str = "",
        lookback_days: int = 56,
        top_n: int = 30,
    ) -> List[tuple]:
        """lookback 기간의 키워드 사용 빈도를 반환한다."""
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, lookback_days))).isoformat()
        params: List[Any] = [cutoff]
        where_clauses = ["recorded_at >= ?"]
        if topic_mode:
            where_clauses.append("topic_mode = ?")
            params.append(str(topic_mode))

        sql = f"""
            SELECT keywords
            FROM topic_memory
            WHERE {' AND '.join(where_clauses)}
        """
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        freq: Dict[str, int] = {}
        for row in rows:
            try:
                keywords = json.loads(row["keywords"]) if row["keywords"] else []
            except Exception:
                keywords = []
            for keyword in keywords:
                normalized = str(keyword).strip().lower()
                if not normalized:
                    continue
                freq[normalized] = freq.get(normalized, 0) + 1

        sorted_freq = sorted(freq.items(), key=lambda item: item[1], reverse=True)
        return sorted_freq[: max(1, int(top_n))]

    def has_recent_similar_active_job(
        self,
        keyword: str,
        topic_mode: str = "",
        platform: str = "",
        lookback_days: int = 7,
    ) -> bool:
        """최근 활성 작업 중 키워드가 유사한 작업 존재 여부를 반환한다."""
        del topic_mode
        normalized_keyword = str(keyword).strip().lower()
        if not normalized_keyword:
            return False

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days)))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        keyword_pattern = f"%{normalized_keyword}%"
        active_statuses = (
            self.STATUS_QUEUED,
            self.STATUS_RUNNING,
            self.STATUS_RETRY_WAIT,
            self.STATUS_READY,
            self.STATUS_AWAITING_APPROVAL,
            self.STATUS_AWAITING_IMAGES,
        )

        params: List[Any] = [*active_statuses, cutoff, cutoff]
        where_clauses = [
            "status IN (?, ?, ?, ?, ?, ?)",
            "(updated_at >= ? OR scheduled_at >= ?)",
        ]
        if platform:
            where_clauses.append("platform = ?")
            params.append(str(platform))
        where_clauses.append("(LOWER(title) LIKE ? OR LOWER(seed_keywords) LIKE ?)")
        params.extend([keyword_pattern, keyword_pattern])

        sql = f"""
            SELECT 1
            FROM jobs
            WHERE {' AND '.join(where_clauses)}
            LIMIT 1
        """
        with self.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return row is not None

    def backfill_topic_memory_from_jobs(self, limit: int = 300) -> int:
        """
        기존 completed jobs 테이블에서 topic_memory를 백필한다.
        초기 실행 시 1회 호출. 이미 등록된 job_id는 INSERT OR IGNORE로 스킵.
        반환값: 새로 삽입된 행 수
        """
        completed_jobs = self.list_recent_completed_jobs(limit=limit)
        inserted = 0
        for job in completed_jobs:
            if not job.result_url:
                continue
            # seo_snapshot에서 topic_mode 추출
            seo_snap: Dict[str, Any] = {}
            try:
                seo_raw = getattr(job, "seo_snapshot", None)
                if isinstance(seo_raw, str):
                    seo_snap = json.loads(seo_raw)
                elif isinstance(seo_raw, dict):
                    seo_snap = seo_raw
            except Exception:
                pass
            topic_mode = str(seo_snap.get("topic_mode", "cafe")).strip() or "cafe"

            # quality_snapshot에서 점수 추출
            q_snap: Dict[str, Any] = {}
            try:
                q_raw = getattr(job, "quality_snapshot", None)
                if isinstance(q_raw, str):
                    q_snap = json.loads(q_raw)
                elif isinstance(q_raw, dict):
                    q_snap = q_raw
            except Exception:
                pass
            quality_score = int(q_snap.get("score", 0))

            # 요약: 제목 + 키워드 결합 (LLM 호출 없음)
            kw_list = list(job.seed_keywords) if job.seed_keywords else []
            summary = f"{job.title} / 키워드: {', '.join(kw_list[:5])}"

            try:
                with self.connection() as conn:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO topic_memory
                            (job_id, title, keywords, topic_mode, platform, persona_id,
                             summary, result_url, quality_score, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(job.job_id),
                            str(job.title),
                            json.dumps(kw_list, ensure_ascii=False),
                            topic_mode,
                            str(job.platform),
                            str(job.persona_id or "P1"),
                            summary[:400],
                            str(job.result_url),
                            quality_score,
                            str(getattr(job, "completed_at", "") or ""),
                        ),
                    )
                    inserted += 1
            except Exception:
                pass
        return inserted
