"""Job store — tracks async backtest & hyperopt jobs."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    job_id: str
    job_type: str
    strategy_name: str
    status: JobStatus = JobStatus.PENDING
    progress: int = 0
    result: Optional[Any] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    finished_at: Optional[str] = None
    process: Optional[asyncio.subprocess.Process] = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "strategy_name": self.strategy_name,
            "status": self.status,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


_jobs: dict[str, "Job"] = {}


async def get_job_status(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        return {"error": f"Job {job_id} not found"}
    return job.to_dict()


async def cancel_job(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        return {"error": f"Job {job_id} not found"}
    if job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
        return {"error": f"Job {job_id} is already {job.status}"}
    if job.process:
        job.process.terminate()
    job.status = JobStatus.CANCELLED
    job.finished_at = datetime.utcnow().isoformat()
    return {"job_id": job_id, "status": "cancelled"}
