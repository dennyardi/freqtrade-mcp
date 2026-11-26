"""Tool: run_hyperopt & get_hyperopt_results."""

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path

from config import FREQTRADE_BIN, USER_DATA_DIR, DEFAULT_EXCHANGE_CONFIG
from tools.jobs import _jobs, Job, JobStatus
from tools.strategy import _validate_name
from tools.backtest import _SAFE_TIMERANGE

# Hyperopt spaces yang diizinkan
_VALID_SPACES = {"buy", "sell", "roi", "stoploss", "trailing", "protection", "default", "all"}


async def run_hyperopt(
    strategy_name: str,
    epochs: int = 100,
    spaces: list[str] | None = None,
    timerange: str | None = None,
) -> dict:
    if not _validate_name(strategy_name):
        return {"error": f"Strategy name tidak valid: {strategy_name}"}
    if not isinstance(epochs, int) or epochs < 1 or epochs > 10000:
        return {"error": "epochs harus integer antara 1–10000"}
    if spaces is None:
        spaces = ["roi", "stoploss"]
    invalid_spaces = [s for s in spaces if s not in _VALID_SPACES]
    if invalid_spaces:
        return {"error": f"Space tidak valid: {invalid_spaces}. Pilihan: {sorted(_VALID_SPACES)}"}
    if timerange and not _SAFE_TIMERANGE.match(timerange):
        return {"error": f"Timerange tidak valid: {timerange}"}

    job_id = uuid.uuid4().hex[:12]
    job = Job(job_id=job_id, job_type="hyperopt", strategy_name=strategy_name)
    _jobs[job_id] = job
    job.persist()
    asyncio.create_task(_execute_hyperopt(job, epochs, spaces, timerange))
    return {"job_id": job_id, "status": "pending", "message": f"Hyperopt queued ({epochs} epochs)"}


async def _execute_hyperopt(job, epochs, spaces, timerange):
    job.status = JobStatus.RUNNING
    job.persist()

    cmd = [
        FREQTRADE_BIN, "hyperopt",
        "--config", DEFAULT_EXCHANGE_CONFIG,
        "--strategy", job.strategy_name,
        "--hyperopt-loss", "SharpeHyperOptLoss",
        "--spaces", *spaces,
        "--epochs", str(epochs),
        "--userdir", USER_DATA_DIR,
        "--print-json", "-j", "-1",
    ]
    if timerange:
        cmd += ["--timerange", timerange]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    job.process = proc

    lines = []
    async for raw in proc.stdout:
        line = raw.decode(errors="replace")
        lines.append(line)
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

    proc = await asyncio.create_subprocess_exec(
        FREQTRADE_BIN, "hyperopt-show",
        "--userdir", USER_DATA_DIR,
        "--hyperopt-filename", files[0].name,
        "--best", "--print-json", "--no-header",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()

    try:
        best = json.loads(stdout.decode())
        return {"job_id": job_id, "best_params": best}
    except Exception:
        return {"job_id": job_id, "raw_output": stdout.decode()[-2000:]}
