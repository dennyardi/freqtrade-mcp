"""Job store — tracks async backtest & hyperopt jobs (SQLite-backed)."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from tools.db import init_db, save_job, save_job_async, load_all_jobs


class JobStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    job_id:        str
    job_type:      str
    strategy_name: str
    status:        JobStatus = JobStatus.PENDING
    progress:      int = 0
    result:        Optional[Any] = None
    error:         Optional[str] = None
    created_at:    str = field(default_factory=lambda: datetime.utcnow().isoformat())
    finished_at:   Optional[str] = None
    process:       Optional[asyncio.subprocess.Process] = None

    def to_dict(self) -> dict:
        return {
            "job_id":        self.job_id,
            "job_type":      self.job_type,
            "strategy_name": self.strategy_name,
            "status":        self.status,
            "progress":      self.progress,
            "result":        self.result,
            "error":         self.error,
            "created_at":    self.created_at,
            "finished_at":   self.finished_at,
        }

    def persist(self) -> None:
        """Gunakan async save jika event loop aktif, fallback sync untuk bootstrap."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(save_job_async(self))
        except RuntimeError:
            save_job(self)


_jobs: dict[str, "Job"] = {}


def bootstrap() -> None:
    """Muat jobs dari SQLite saat startup (proses yang tidak running ditandai failed)."""
    init_db()
    for row in load_all_jobs():
        status = row["status"]
        # Jobs yang masih pending/running saat startup berarti mati di tengah jalan
        if status in ("pending", "running"):
            status = "failed"
            error = row.get("error") or "Service restarted while job was active"
        else:
            error = row.get("error")

        job = Job(
            job_id=        row["job_id"],
            job_type=      row["job_type"],
            strategy_name= row["strategy_name"],
            status=        JobStatus(status),
            progress=      row["progress"],
            result=        row["result"],
            error=         error,
            created_at=    row["created_at"],
            finished_at=   row["finished_at"],
        )
        _jobs[job.job_id] = job
        # Simpan kembali jika status diubah
        if status != row["status"]:
            save_job(job)


def _trim_error(text: str, max_lines: int = 20) -> str:
    """Kembalikan N baris terakhir dari teks error (tidak terpotong di tengah kata)."""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


async def list_jobs(limit: int = 200) -> dict:
    """Return semua jobs yang diketahui server (dari memory/SQLite), terbaru dulu."""
    all_jobs = sorted(
        _jobs.values(),
        key=lambda j: j.created_at,
        reverse=True,
    )
    if limit:
        all_jobs = all_jobs[:limit]
    return {
        "jobs": [j.to_dict() for j in all_jobs],
        "total": len(_jobs),
    }


async def get_job_status(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        return {"error": f"Job {job_id} not found"}
    d = job.to_dict()
    # F-5: kembalikan error sebagai 20 baris terakhir utuh (tidak terpotong di tengah kata)
    if d.get("error"):
        d["error"] = _trim_error(d["error"], max_lines=20)
    return d


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
    job.persist()
    return {"job_id": job_id, "status": "cancelled"}
