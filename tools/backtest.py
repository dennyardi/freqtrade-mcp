"""Tool: run_backtest & get_backtest_results."""

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path

from config import FREQTRADE_BIN, USER_DATA_DIR, DEFAULT_EXCHANGE_CONFIG, RESULTS_DIR
from tools.jobs import _jobs, Job, JobStatus


async def run_backtest(
    strategy_name: str,
    pairs: list[str] | None = None,
    timeframe: str = "1h",
    timerange: str | None = None,
) -> dict:
    job_id = str(uuid.uuid4())[:8]
    job = Job(job_id=job_id, job_type="backtest", strategy_name=strategy_name)
    _jobs[job_id] = job
    asyncio.create_task(_execute_backtest(job, pairs, timeframe, timerange))
    return {"job_id": job_id, "status": "pending", "message": "Backtest queued"}


async def _execute_backtest(job, pairs, timeframe, timerange):
    job.status = JobStatus.RUNNING
    pairs_args = "--pairs " + " ".join(f'"{p}"' for p in pairs) if pairs else ""
    timerange_arg = f"--timerange {timerange}" if timerange else ""
    result_file = RESULTS_DIR / f"backtest_{job.job_id}.json"

    cmd = (
        f"{FREQTRADE_BIN} backtesting"
        f" --config {DEFAULT_EXCHANGE_CONFIG}"
        f" --strategy {job.strategy_name}"
        f" --timeframe {timeframe}"
        f" {pairs_args} {timerange_arg}"
        f" --userdir {USER_DATA_DIR}"
        f" --export trades"
        f" --export-filename {result_file}"
        f" --breakdown month"
    )

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    job.process = proc
    stdout, _ = await proc.communicate()
    output = stdout.decode()
    job.finished_at = datetime.utcnow().isoformat()

    if proc.returncode != 0:
        job.status = JobStatus.FAILED
        job.error = output[-3000:]
        return

    job.status = JobStatus.COMPLETED
    job.result = {"result_file": str(result_file), "log": output[-2000:]}


async def get_backtest_results(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        return {"error": f"Job {job_id} not found"}
    if job.status != JobStatus.COMPLETED:
        return {"error": f"Job not completed yet, status: {job.status}"}

    result_file = Path(job.result["result_file"])
    if not result_file.exists():
        backtest_dir = Path(USER_DATA_DIR) / "backtest_results"
        files = sorted(backtest_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            return {"error": "No backtest result file found"}
        result_file = files[0]

    with open(result_file) as f:
        data = json.load(f)

    summary = {}
    if "strategy" in data:
        for strat, m in data["strategy"].items():
            total = max(m.get("total_trades", 1), 1)
            summary[strat] = {
                "total_trades": m.get("total_trades"),
                "win_rate": round(m.get("wins", 0) / total * 100, 2),
                "profit_total_pct": m.get("profit_total_pct"),
                "profit_factor": m.get("profit_factor"),
                "max_drawdown_pct": m.get("max_drawdown_account"),
                "sharpe": m.get("sharpe"),
                "calmar": m.get("calmar"),
                "sortino": m.get("sortino"),
                "avg_duration": m.get("holding_avg"),
            }

    return {"job_id": job_id, "summary": summary, "raw_file": str(result_file)}
