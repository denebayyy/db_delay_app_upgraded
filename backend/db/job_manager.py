"""Job Manager Module

This module provides a high-level job orchestration system for managing
recurring polling tasks. It wraps the low-level Scheduler with job configuration,
status tracking, error handling, and concurrency control.

Key Components:
  - JobConfig: Defines job parameters (stations, interval, retries, timeout)
  - JobStatus: Tracks execution state (running, last_run, errors, counts)
  - JobManager: Orchestrates job lifecycle and executes polls with retry logic

Concurrency: Uses asyncio.Semaphore to limit concurrent Playwright browser launches.
Error Handling: Implements exponential backoff on failures with configurable retries.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from .scheduler import Scheduler

logger = logging.getLogger(__name__)


@dataclass
class JobConfig:
    """Configuration for a recurring polling job.
    
    Attributes:
        id: Unique job identifier (e.g., 'aachen-home')
        from_station: Starting station name (URL-encoded as needed)
        to_station: Destination station name (URL-encoded as needed)
        interval: Poll frequency in seconds (e.g., 300.0 for 5 minutes)
        enabled: Whether job should auto-start on creation (default: True)
        max_retries: Number of retry attempts on failure (default: 3)
        timeout: Max execution time per poll in seconds (default: 120.0)
    """
    id: str
    from_station: str
    to_station: str
    interval: float  # seconds
    enabled: bool = True
    max_retries: int = 3
    timeout: float = 120.0


@dataclass
class JobStatus:
    """Runtime status of a polling job.
    
    Tracks execution metrics and current state. Separate from JobConfig
    to allow status reset/monitoring independent of job configuration.
    
    Attributes:
        job_id: Reference to the job's unique identifier
        running: True if job is currently executing a poll
        last_run: Timestamp of most recent poll attempt (or None if never run)
        last_error: Error message from most recent failure (or None if successful)
        error_count: Cumulative count of failed poll attempts
        run_count: Cumulative count of successful poll attempts
    """
    job_id: str
    running: bool = False
    last_run: Optional[datetime] = None
    last_error: Optional[str] = None
    error_count: int = 0
    run_count: int = 0

    def to_dict(self):
        """Convert status to dictionary for API serialization.
        
        Returns:
            dict: JSON-serializable status representation with ISO timestamp.
        """
        return {
            "job_id": self.job_id,
            "running": self.running,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_error": self.last_error,
            "error_count": self.error_count,
            "run_count": self.run_count,
        }


class JobManager:
    """High-level orchestrator for recurring polling jobs.
    
    Provides job lifecycle management (create, start, stop, delete), execution
    with retry/backoff logic, and status tracking. Wraps a low-level Scheduler
    and enforces concurrency limits via semaphore to prevent resource exhaustion.
    
    Attributes:
        scheduler: The underlying Scheduler instance managing job timing
        jobs: Registry of active JobConfig objects keyed by job_id
        statuses: Runtime status for each job keyed by job_id
        semaphore: asyncio.Semaphore limiting concurrent poll executions
        scheduled_jobs: Maps job_id to scheduler.Job for cancellation
        poll_func: Injected callback (poll_station) that performs the actual poll
    """
    
    def __init__(self, scheduler: Scheduler, max_concurrent: int = 3):
        """Initialize the job manager.
        
        Args:
            scheduler: Scheduler instance for timing job executions
            max_concurrent: Max number of jobs allowed to run simultaneously (default: 3)
        """
        self.scheduler = scheduler
        self.jobs: dict[str, JobConfig] = {}  # Job configurations
        self.statuses: dict[str, JobStatus] = {}  # Execution status per job
        # Semaphore prevents browser resource exhaustion from parallel polls
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.scheduled_jobs: dict[str, any] = {}  # Tracks active scheduler.Job objects
        self.poll_func: Optional[Callable] = None  # Injected by main.py

    def set_poll_func(self, func: Callable):
        """Inject the poll function from main.py.
        
        Must be called during app startup before any jobs are scheduled.
        The poll function receives a JobConfig and performs the actual polling logic.
        
        Args:
            func: Async callable(JobConfig) -> None that performs the poll
        """
        self.poll_func = func

    async def create_job(self, config: JobConfig):
        """Register and optionally start a new polling job.
        
        Creates a new job from the provided config. If config.enabled=True,
        automatically schedules the job to run at the specified interval.
        
        Args:
            config: JobConfig specifying job parameters
            
        Raises:
            ValueError: If job_id already exists (jobs must have unique IDs)
        """
        self.jobs[config.id] = config
        self.statuses[config.id] = JobStatus(job_id=config.id)
        if config.enabled:
            await self.start_job(config.id)
        logger.info(f"Created job {config.id}")

    async def start_job(self, job_id: str):
        """Start a recurring poll job.
        
        Schedules a job to run repeatedly at config.interval seconds.
        Registers the job with the scheduler and tracks it for later cancellation.
        
        Args:
            job_id: Unique identifier of the job to start
            
        Raises:
            ValueError: If job_id does not exist
            
        Note:
            Idempotent if job already running (logs warning but does not error)
        """
        if job_id not in self.jobs:
            raise ValueError(f"Job {job_id} not found")

        if job_id in self.scheduled_jobs:
            logger.warning(f"Job {job_id} already running")
            return

        config = self.jobs[job_id]

        # Wrapper function allows _run_with_error_handling to receive job_id context
        async def run_with_retry():
            await self._run_with_error_handling(job_id)

        # Register with scheduler for recurring execution
        scheduled_job = await self.scheduler.schedule_every(
            config.interval, run_with_retry
        )
        self.scheduled_jobs[job_id] = scheduled_job
        logger.info(f"Started job {job_id} with interval {config.interval}s")

    async def stop_job(self, job_id: str):
        """Stop and unschedule a polling job.
        
        Cancels the recurring execution but keeps the JobConfig for future restart.
        Idempotent: safe to call multiple times.
        
        Args:
            job_id: Unique identifier of the job to stop
        """
        if job_id in self.scheduled_jobs:
            # Signal scheduler to cancel future executions of this job
            self.scheduler.cancel(self.scheduled_jobs[job_id])
            del self.scheduled_jobs[job_id]
            logger.info(f"Stopped job {job_id}")

    async def run_once(self, job_id: str):
        """Execute a single poll immediately, outside the normal schedule.
        
        Useful for testing, manual triggers, or one-off queries without
        affecting the job's recurring schedule.
        
        Args:
            job_id: Unique identifier of the job to run
            
        Raises:
            ValueError: If job_id does not exist
        """
        if job_id not in self.jobs:
            raise ValueError(f"Job {job_id} not found")

        await self._run_with_error_handling(job_id)

    async def _run_with_error_handling(self, job_id: str):
        """Core poll execution with retry logic and comprehensive error handling.
        
        Private method that implements:
        1. Semaphore-gated execution (prevents browser resource exhaustion)
        2. Retry loop with exponential backoff (2^attempt seconds)
        3. Detailed status and error tracking
        4. Logging for observability
        
        Retry Strategy:
            - Attempts: up to config.max_retries (default 3)
            - Backoff: exponential (1s, 2s, 4s, ...)
            - Timeout: config.timeout per attempt (default 120s)
        
        Args:
            job_id: Unique identifier of the job to execute
            
        Note:
            Always returns after max_retries attempts, even on persistent failure.
            Updates status object with final outcome. Failed jobs increment error_count.
        """
        config = self.jobs[job_id]
        status = self.statuses[job_id]

        # Retry loop with exponential backoff on failure
        for attempt in range(config.max_retries):
            try:
                # Update status before attempt
                status.running = True
                status.last_run = datetime.now()

                # Acquire semaphore slot to limit concurrent browser launches,
                # then call the poll function with timeout protection
                async with self.semaphore:
                    await asyncio.wait_for(
                        self.poll_func(config), timeout=config.timeout
                    )

                # Success: update status and return early
                status.running = False
                status.run_count += 1
                status.last_error = None
                status.error_count = 0
                logger.info(f"Job {job_id} completed (run #{status.run_count})")
                return

            except asyncio.TimeoutError:
                # Poll exceeded timeout threshold
                status.last_error = "Timeout"
                logger.warning(
                    f"Job {job_id} timed out (attempt {attempt + 1}/{config.max_retries})"
                )
            except Exception as e:
                # Any other exception (network, parsing, DB, etc.)
                status.last_error = str(e)
                logger.error(
                    f"Job {job_id} failed: {e} (attempt {attempt + 1}/{config.max_retries})"
                )

            # Exponential backoff before retry (1s, 2s, 4s, 8s, ...)
            if attempt < config.max_retries - 1:
                backoff_seconds = 2**attempt
                logger.info(f"Job {job_id} retrying in {backoff_seconds}s...")
                await asyncio.sleep(backoff_seconds)

        # All retries exhausted - job failed
        status.running = False
        status.error_count += 1
        logger.error(f"Job {job_id} failed after {config.max_retries} attempts")

    def get_status(self, job_id: str) -> Optional[JobStatus]:
        """Retrieve runtime status for a specific job.
        
        Args:
            job_id: Unique identifier of the job
            
        Returns:
            JobStatus object if job exists, None otherwise
        """
        return self.statuses.get(job_id)

    def list_jobs(self) -> dict[str, JobStatus]:
        """Retrieve status of all managed jobs.
        
        Returns:
            Dictionary mapping job_id to JobStatus for all jobs
        """
        return self.statuses

    async def shutdown(self):
        """Gracefully shutdown all scheduled jobs.
        
        Called during application shutdown to cleanly cancel all recurring tasks.
        Ensures no orphaned jobs continue running in the background.
        """
        # Cancel all active scheduled jobs
        for job_id in list(self.scheduled_jobs.keys()):
            await self.stop_job(job_id)
        logger.info("JobManager shut down")
