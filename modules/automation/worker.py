"""
Job Worker - Heartbeat + Reaper 기반 안정적 작업 처리

P0 #3 해결: running 상태 고착 방지
- Heartbeat: 실행 중인 작업의 lease 갱신
- Reaper: stale 작업 감지 및 재큐잉
- Graceful Shutdown: SIGTERM/SIGINT 처리

사용법:
    worker = Worker(job_store, pipeline_service)
    await worker.run()
"""

import asyncio
import signal
import logging
from typing import Optional, Callable, Awaitable, List
from dataclasses import dataclass

from .job_store import JobStore, Job

logger = logging.getLogger(__name__)


@dataclass
class WorkerConfig:
    """Worker 설정"""
    poll_interval_sec: int = 30  # job 폴링 간격
    max_concurrent_jobs: int = 3  # 동시 실행 최대 job 수
    heartbeat_interval_sec: int = 60  # heartbeat 간격
    reaper_interval_sec: int = 120  # reaper 실행 간격
    graceful_shutdown_timeout_sec: int = 30  # graceful shutdown 대기


class Worker:
    """
    Job 처리 워커.

    주요 기능:
    - 주기적으로 due job을 claim하여 실행
    - 실행 중 heartbeat로 lease 갱신
    - Reaper로 stale running job 감지 및 재큐잉
    - Graceful shutdown 지원
    """

    def __init__(
        self,
        job_store: JobStore,
        process_job: Callable[[Job], Awaitable[None]],
        config: Optional[WorkerConfig] = None,
    ):
        """
        Args:
            job_store: JobStore 인스턴스
            process_job: Job 처리 콜백 (async function)
            config: 워커 설정
        """
        self.job_store = job_store
        self.process_job = process_job
        self.config = config or WorkerConfig()

        self._running = False
        self._active_jobs: dict[str, asyncio.Task[None]] = {}
        self._heartbeat_tasks: dict[str, asyncio.Task[None]] = {}
        self._shutdown_event: Optional[asyncio.Event] = None

    async def run(self):
        """
        워커 메인 루프 시작.

        종료 조건:
        - SIGTERM/SIGINT 수신
        - shutdown() 호출
        """
        self._running = True
        self._shutdown_event = asyncio.Event()
        logger.info(f"Worker starting (worker_id: {self.job_store._worker_id})")

        # 시그널 핸들러 등록
        self._setup_signal_handlers()

        # 기존 running 작업 복구
        await self._recover_running_jobs()

        # 백그라운드 태스크 시작
        reaper_task = asyncio.create_task(self._reaper_loop())

        try:
            while self._running and not self._is_shutdown_requested():
                await self._poll_and_execute()
                await asyncio.sleep(self.config.poll_interval_sec)

        except asyncio.CancelledError:
            logger.info("Worker cancelled")

        finally:
            # Graceful shutdown
            await self._graceful_shutdown()
            reaper_task.cancel()
            try:
                await reaper_task
            except asyncio.CancelledError:
                pass

        logger.info("Worker stopped")

    async def shutdown(self):
        """워커 종료 요청"""
        logger.info("Shutdown requested")
        self._running = False
        if self._shutdown_event is not None:
            self._shutdown_event.set()

    def _is_shutdown_requested(self) -> bool:
        """shutdown 이벤트 상태를 안전하게 조회한다."""
        return self._shutdown_event.is_set() if self._shutdown_event is not None else False

    def _setup_signal_handlers(self):
        """시그널 핸들러 등록"""
        loop = asyncio.get_running_loop()

        def handle_signal(sig):
            logger.info(f"Received signal {sig}")
            asyncio.create_task(self.shutdown())

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))
            except NotImplementedError:
                # Windows에서는 지원 안 됨
                pass

    async def _recover_running_jobs(self):
        """
        워커 시작 시 기존 running 작업 복구.

        자신이 claim했던 작업들의 heartbeat 갱신 후 재실행.
        """
        my_jobs = self.job_store.get_my_running_jobs()

        if not my_jobs:
            logger.info("No running jobs to recover")
            return

        logger.info(f"Recovering {len(my_jobs)} running jobs")

        for job in my_jobs:
            # heartbeat 갱신
            self.job_store.heartbeat(job.job_id)

            # 재실행
            if len(self._active_jobs) < self.config.max_concurrent_jobs:
                await self._start_job(job)
            else:
                # 동시 실행 제한 초과 시 retry_wait으로 전환
                self.job_store.fail_job(
                    job.job_id,
                    "WORKER_CRASH",
                    "Recovered but concurrent limit exceeded"
                )

    async def _poll_and_execute(self):
        """
        Due job 폴링 및 실행.

        동시 실행 제한을 고려하여 필요한 만큼만 claim.
        """
        # 사용 가능한 슬롯 계산
        available_slots = self.config.max_concurrent_jobs - len(self._active_jobs)

        if available_slots <= 0:
            logger.debug("No available slots, skipping poll")
            return

        # Due job claim
        jobs = self.job_store.claim_due_jobs(limit=available_slots)

        for job in jobs:
            await self._start_job(job)

    async def _start_job(self, job: Job):
        """
        Job 실행 시작.

        - Job 처리 태스크 시작
        - Heartbeat 태스크 시작
        """
        logger.info(f"Starting job: {job.job_id} ({job.title})")

        # Job 처리 태스크
        job_task = asyncio.create_task(self._run_job(job))
        self._active_jobs[job.job_id] = job_task

        # Heartbeat 태스크
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(job.job_id))
        self._heartbeat_tasks[job.job_id] = heartbeat_task

        # 완료 콜백
        job_task.add_done_callback(self._build_done_callback(job.job_id))

    def _build_done_callback(self, job_id: str) -> Callable[[asyncio.Task[None]], None]:
        """mypy가 추론 가능한 완료 콜백 생성."""

        def _callback(task: asyncio.Task[None]) -> None:
            self._on_job_done(job_id, task)

        return _callback

    async def _run_job(self, job: Job):
        """
        Job 실행 래퍼.

        - 예외 처리
        - 결과에 따른 상태 전이
        """
        try:
            # 이미 발행된 작업인지 확인 (P0 #2: 중복 방지)
            existing_url = self.job_store.check_already_published(job.job_id)
            if existing_url:
                logger.info(f"Job {job.job_id} already published: {existing_url}")
                return

            # LLM 예산 확인 (P0 #4)
            if not self.job_store.check_llm_budget(job.job_id):
                self.job_store.fail_job(
                    job.job_id,
                    "BUDGET_EXCEEDED",
                    "LLM call limit exceeded"
                )
                return

            # Job 처리 실행
            await self.process_job(job)

        except Exception as e:
            logger.exception(f"Job {job.job_id} failed with exception")
            self.job_store.fail_job(
                job.job_id,
                "PIPELINE_ERROR",
                str(e)[:500]  # 에러 메시지 길이 제한
            )

    def _on_job_done(self, job_id: str, task: asyncio.Task[None]):
        """Job 완료 콜백"""
        # 태스크 정리
        self._active_jobs.pop(job_id, None)

        # Heartbeat 태스크 취소
        heartbeat_task = self._heartbeat_tasks.pop(job_id, None)
        if heartbeat_task:
            heartbeat_task.cancel()

        # 예외 확인
        if task.cancelled():
            logger.info(f"Job {job_id} was cancelled")
        elif task.exception():
            logger.error(f"Job {job_id} raised exception: {task.exception()}")
        else:
            logger.info(f"Job {job_id} completed")

    async def _heartbeat_loop(self, job_id: str):
        """
        Heartbeat 루프.

        P0 #3: 실행 중 lease 갱신
        """
        try:
            while True:
                await asyncio.sleep(self.config.heartbeat_interval_sec)

                if job_id not in self._active_jobs:
                    break

                success = self.job_store.heartbeat(job_id)
                if not success:
                    logger.warning(f"Heartbeat failed for {job_id}, job may have been stolen")
                    break

        except asyncio.CancelledError:
            pass

    async def _reaper_loop(self):
        """
        Reaper 루프.

        P0 #3: stale running job 감지 및 재큐잉
        """
        try:
            while self._running:
                await asyncio.sleep(self.config.reaper_interval_sec)
                await self._reap_stale_jobs()

        except asyncio.CancelledError:
            pass

    async def _reap_stale_jobs(self):
        """
        Stale running job 처리.

        - 다른 워커의 stale job도 처리 (클러스터 환경 대응)
        """
        stale_jobs = self.job_store.get_stale_running_jobs()

        for job in stale_jobs:
            # 자신이 처리 중인 작업은 스킵 (정상 실행 중일 수 있음)
            if job.job_id in self._active_jobs:
                continue

            logger.warning(f"Reaping stale job: {job.job_id} (claimed_by: {job.claimed_by})")
            self.job_store.requeue_stale_job(job.job_id)

        if stale_jobs:
            logger.info(f"Reaped {len(stale_jobs)} stale jobs")

    async def _graceful_shutdown(self):
        """
        Graceful shutdown.

        - 실행 중 작업 완료 대기 (timeout 내)
        - timeout 초과 시 retry_wait으로 전환
        """
        if not self._active_jobs:
            return

        logger.info(f"Graceful shutdown: waiting for {len(self._active_jobs)} jobs")

        # 완료 대기
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._active_jobs.values(), return_exceptions=True),
                timeout=self.config.graceful_shutdown_timeout_sec
            )
            logger.info("All jobs completed during shutdown")

        except asyncio.TimeoutError:
            logger.warning("Shutdown timeout, failing remaining jobs")

            # 남은 작업들 retry_wait으로 전환
            for job_id, task in list(self._active_jobs.items()):
                task.cancel()
                self.job_store.fail_job(
                    job_id,
                    "WORKER_CRASH",
                    "Worker shutdown timeout"
                )

    @property
    def active_job_count(self) -> int:
        """현재 실행 중인 작업 수"""
        return len(self._active_jobs)

    @property
    def active_job_ids(self) -> List[str]:
        """현재 실행 중인 작업 ID 목록"""
        return list(self._active_jobs.keys())


class WorkerPool:
    """
    다중 워커 풀 (선택적 사용).

    여러 워커를 동시에 실행하여 처리량 증가.
    """

    def __init__(
        self,
        job_store: JobStore,
        process_job: Callable[[Job], Awaitable[None]],
        worker_count: int = 2,
        config: Optional[WorkerConfig] = None,
    ):
        self.workers = [
            Worker(job_store, process_job, config)
            for _ in range(worker_count)
        ]

    async def run(self):
        """모든 워커 실행"""
        await asyncio.gather(*[w.run() for w in self.workers])

    async def shutdown(self):
        """모든 워커 종료"""
        await asyncio.gather(*[w.shutdown() for w in self.workers])
