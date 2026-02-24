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
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

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
            conn.executescript("""

                CREATE INDEX IF NOT EXISTS idx_jobs_retry
                ON jobs(status, next_retry_at) WHERE status = 'retry_wait';

                CREATE INDEX IF NOT EXISTS idx_jobs_running
                ON jobs(status, claimed_by, heartbeat_at) WHERE status = 'running';

                CREATE INDEX IF NOT EXISTS idx_jobs_ready
                ON jobs(status, updated_at) WHERE status = 'ready_to_publish';

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

    def _generate_idempotency_key(self, title: str, scheduled_at: str, persona_id: str) -> str:
        """중복 방지용 idempotency key 생성"""
        data = f"{title}|{scheduled_at}|{persona_id}"
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

        Returns:
            bool: 등록 성공 여부 (중복 시 False)
        """
        start_time = time.perf_counter()
        if max_retries is None:
            max_retries = self.config.max_retries

        now = now_utc()
        idempotency_key = self._generate_idempotency_key(title, scheduled_at, persona_id)

        try:
            with self.connection() as conn:
                conn.execute("""
                    INSERT INTO jobs (
                        job_id, idempotency_key, status, title, seed_keywords,
                        platform, persona_id, scheduled_at, max_retries,
                        tags, category,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    job_id, idempotency_key, self.STATUS_QUEUED, title,
                    json.dumps(seed_keywords), platform, persona_id,
                    scheduled_at, max_retries,
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

    def claim_due_jobs(self, limit: int = 5, now_override: Optional[str] = None) -> List[Job]:
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
            cursor = conn.execute("""
                UPDATE jobs
                SET status = ?,
                    claimed_at = ?,
                    claimed_by = ?,
                    heartbeat_at = ?,
                    updated_at = ?
                WHERE job_id IN (
                    SELECT job_id FROM jobs
                    WHERE (
                        (status = 'queued' AND scheduled_at <= ?)
                        OR
                        (status = 'retry_wait' AND next_retry_at <= ?)
                    )
                    ORDER BY COALESCE(next_retry_at, scheduled_at) ASC
                    LIMIT ?
                )
                RETURNING *
            """, (
                self.STATUS_RUNNING, now, worker_id, now, now,
                now, now, limit
            ))

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

    def claim_for_generate(self, limit: int = 5, now_override: Optional[str] = None) -> List[Job]:
        """생성 워커용 claim 래퍼."""
        return self.claim_due_jobs(limit=limit, now_override=now_override)

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

    def get_today_completed_count(self) -> int:
        """오늘 완료(completed)된 Job 수를 반환한다."""
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM jobs
                WHERE status = ?
                AND date(updated_at) = date('now')
                """,
                (self.STATUS_COMPLETED,),
            ).fetchone()
            return int(row["total"]) if row else 0

    def get_today_failed_count(self) -> int:
        """오늘 실패(failed)된 Job 수를 반환한다."""
        with self.connection() as conn:
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

    def get_last_completed_time(self) -> Optional[datetime]:
        """가장 최근 완료된 Job의 완료 시각(UTC)을 반환한다."""
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT updated_at
                FROM jobs
                WHERE status = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (self.STATUS_COMPLETED,),
            ).fetchone()
            if not row or not row["updated_at"]:
                return None
            return parse_iso(str(row["updated_at"]))

    def save_prepared_payload(self, job_id: str, payload: Dict[str, Any]) -> bool:
        """생성된 초안을 저장하고 발행 대기 상태로 전환한다."""
        now = now_utc()
        with self.connection() as conn:
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
                AND status = ?
                """,
                (
                    self.STATUS_READY,
                    json.dumps(payload),
                    now,
                    job_id,
                    self.STATUS_RUNNING,
                ),
            )
            if cursor.rowcount > 0:
                self._log_event(
                    conn,
                    job_id,
                    "prepared",
                    {"payload_keys": sorted(payload.keys())},
                )
                return True
            return False

    def claim_ready_jobs(self, limit: int = 1, now_override: Optional[str] = None) -> List[Job]:
        """발행 가능한 준비 완료 Job을 원자적으로 선점한다."""
        now = now_override or now_utc()
        worker_id = self._worker_id

        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    claimed_at = ?,
                    claimed_by = ?,
                    heartbeat_at = ?,
                    updated_at = ?
                WHERE job_id IN (
                    SELECT job_id FROM jobs
                    WHERE status = ?
                    AND scheduled_at <= ?
                    ORDER BY updated_at ASC
                    LIMIT ?
                )
                RETURNING *
                """,
                (
                    self.STATUS_RUNNING,
                    now,
                    worker_id,
                    now,
                    now,
                    self.STATUS_READY,
                    now,
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

    def claim_for_publish(self, limit: int = 1, now_override: Optional[str] = None) -> List[Job]:
        """발행 워커용 claim 래퍼."""
        return self.claim_ready_jobs(limit=limit, now_override=now_override)

    def get_ready_to_publish_count(self) -> int:
        """발행 대기(ready) 상태 Job 수를 반환한다."""
        with self.connection() as conn:
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
        """큐 상태 통계"""
        with self.connection() as conn:
            cursor = conn.execute("""
                SELECT status, COUNT(*) as count
                FROM jobs
                GROUP BY status
            """)

            return {row["status"]: row["count"] for row in cursor.fetchall()}

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
