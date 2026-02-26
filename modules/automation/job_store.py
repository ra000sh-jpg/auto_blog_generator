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
import time
import uuid
from datetime import datetime
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import json
import logging

from .time_utils import now_utc, add_seconds, calculate_retry_delay, parse_iso
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
        "BUDGET_EXCEEDED",
    })

    def __init__(self, db_path: str = "data/automation.db", config: Optional[JobConfig] = None):
        self.db_path = db_path
        self.config = config or JobConfig()
        self._worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._ensure_directory()
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

            existing_image_log_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(image_generation_log)").fetchall()
            }
            if existing_image_log_columns and "source_url" not in existing_image_log_columns:
                conn.execute("ALTER TABLE image_generation_log ADD COLUMN source_url TEXT DEFAULT ''")

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
    ) -> List[Job]:
        """생성 워커용 claim 래퍼."""
        return self.claim_due_jobs(
            limit=limit,
            now_override=now_override,
            job_kind=job_kind,
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
                    COUNT(*) AS total_calls,
                    AVG(input_tokens) AS avg_input,
                    AVG(output_tokens) AS avg_output
                FROM job_metrics
                GROUP BY metric_type
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

        return {
            "today_published": int(today_row["cnt"]) if today_row else 0,
            "total_published": int(total_row["cnt"]) if total_row else 0,
            "idea_vault_pending": int(vault_row["cnt"]) if vault_row else 0,
            "idea_vault_total": int(vault_total_row["cnt"]) if vault_total_row else 0,
            "llm_rows": [dict(row) for row in llm_rows],
            "trend_rows": [dict(row) for row in trend_rows],
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
    ) -> List[Job]:
        """발행 워커용 claim 래퍼."""
        return self.claim_ready_jobs(
            limit=limit,
            now_override=now_override,
            job_kind=job_kind,
        )

    def get_ready_to_publish_count(self, job_kind: Optional[str] = None) -> int:
        """발행 대기(ready) 상태 Job 수를 반환한다."""
        with self.connection() as conn:
            normalized_kind = str(job_kind or "").strip().lower()
            if normalized_kind:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM jobs
                    WHERE status = ?
                    AND job_kind = ?
                    """,
                    (self.STATUS_READY, normalized_kind),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS total FROM jobs WHERE status = ?",
                    (self.STATUS_READY,),
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
