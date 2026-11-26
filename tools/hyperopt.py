"""Tool: run_hyperopt & get_hyperopt_results."""

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path

from config import FREQTRADE_BIN, USER_DATA_DIR, DEFAULT_EXCHANGE_CONFIG
from tools.jobs import _jobs, Job, JobStatus


async def run_hyperopt(
    strategy_name: str,
    epochs: int = 100,
    spaces: list[str] | None = None,
    timerange: str | None = None,
) -> dict:
    job_id = str(uuid.uuid4())[:8]
    job = Job(job_id=job_id, job_type="hyperopt", strategy_name=strategy_name)
    _jobs[job_id] = job
    job.persist()
    if spaces is None:
        spaces = ["roi", "stoploss"]
    asyncio.create_task(_execute_hyperopt(job, epochs, spaces, timerange))
    return {"job_id": job_id, "status": "pending", "message": f"Hyperopt queued ({epochs} epochs)"}


async def _execute_hyperopt(job, epochs, spaces, timerange):
    job.status = JobStatus.RUNNING
    job.persist()

    spaces_arg    = " ".join(spaces)
    timerange_arg = f"--timerange {timerange}" if timerange else ""

    cmd = (
        f"{FREQTRADE_BIN} hyperopt"
        f" --config {DEFAULT_EXCHANGE_CONFIG}"
        f" --strategy {job.strategy_name}"
        f" --hyperopt-loss SharpeHyperOptLoss"
        f" --spaces {spaces_arg}"
        f" --epochs {epochs}"
        f" {timerange_arg}"
        f" --userdir {USER_DATA_DIR}"
        f" --print-json -j -1"
    )

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    job.process = proc

    # Stream output dan update progress berdasarkan epoch
    lines = []
    async for raw in proc.stdout:
        line = raw.decode(errors="replace")
        lines.append(line)
        # Freqtrade hyperopt mencetak "Epoch X/Y" — parse progress
        if "Epoch" in line and "/" in line:
            try:
                parts = [t for t in line.split() if "/" in t][0]
                current, total = parts.split("/")
                job.progress = min(int(int(current) / int(total) * 100), 99)
                job.persist()
            except Exception:
                pass

    await proc.wait()
    output = "".join(lines)
    job.finished_at = datetime.utcnow().isoformat()

    if proc.returncode != 0:
        job.status = JobStatus.FAILED
        job.error  = output[-3000:]
        job.persist()
        return

    job.status   = JobStatus.COMPLETED
    job.progress = 100
    job.result   = {"log": output[-3000:]}
    job.persist()


async def get_hyperopt_results(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        return {"error": f"Job {job_id} not found"}
    if job.status != JobStatus.COMPLETED:
        return {"error": f"Job not completed yet, status: {job.status}"}

    hyperopt_dir = Path(USER_DATA_DIR) / "hyperopt_results"
    files = sorted(hyperopt_dir.glob("*.fthypt"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return {"error": "No hyperopt result file found"}

    show_cmd = (
        f"{FREQTRADE_BIN} hyperopt-show"
        f" --userdir {USER_DATA_DIR}"
        f" --hyperopt-filename {files[0].name}"
        f" --best --print-json --no-header"
    )
    proc = await asyncio.create_subprocess_shell(
        show_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()

    try:
        best = json.loads(stdout.decode())
        return {"job_id": job_id, "best_params": best}
    except Exception:
        return {"job_id": job_id, "raw_output": stdout.decode()[-2000:]}
