"""Tool: run_backtest & get_backtest_results."""

import asyncio
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from config import FREQTRADE_BIN, USER_DATA_DIR, DEFAULT_EXCHANGE_CONFIG, RESULTS_DIR
from tools.jobs import _jobs, Job, JobStatus
from tools.strategy import _validate_name

# Whitelist timeframe yang diizinkan
_VALID_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"}

# Timerange: YYYYMMDD-YYYYMMDD atau YYYYMMDD
_SAFE_TIMERANGE = re.compile(r'^\d{8}(-\d{8})?$')

# Pair format: BASE/QUOTE atau BASE/QUOTE:SETTLE
_SAFE_PAIR = re.compile(r'^[A-Z0-9]{2,10}/[A-Z0-9]{2,10}(:[A-Z0-9]{2,10})?$')


def _validate_pairs(pairs: list[str]) -> list[str] | None:
    """Return sanitized pairs atau None jika ada yang invalid."""
    for p in pairs:
        if not _SAFE_PAIR.match(p):
            return None
    return pairs


async def run_backtest(
    strategy_name: str,
    pairs: list[str] | None = None,
    timeframe: str = "1h",
    timerange: str | None = None,
) -> dict:
    if not _validate_name(strategy_name):
        return {"error": f"Strategy name tidak valid: {strategy_name}"}
    if timeframe not in _VALID_TIMEFRAMES:
        return {"error": f"Timeframe tidak valid: {timeframe}"}
    if pairs:
        if _validate_pairs(pairs) is None:
            return {"error": "Format pair tidak valid. Gunakan format BASE/QUOTE (contoh: BTC/USDT)"}
    if timerange and not _SAFE_TIMERANGE.match(timerange):
        return {"error": f"Timerange tidak valid: {timerange}. Format: YYYYMMDD atau YYYYMMDD-YYYYMMDD"}

    job_id = uuid.uuid4().hex[:12]
    job = Job(job_id=job_id, job_type="backtest", strategy_name=strategy_name)
    _jobs[job_id] = job
    job.persist()
    asyncio.create_task(_execute_backtest(job, pairs, timeframe, timerange))
    return {"job_id": job_id, "status": "pending", "message": "Backtest queued"}


async def _execute_backtest(job, pairs, timeframe, timerange):
    job.status = JobStatus.RUNNING
    job.persist()

    result_file = RESULTS_DIR / f"backtest_{job.job_id}.json"

    # Bangun argumen sebagai list (tidak lewat shell — tidak ada injection)
    cmd = [
        FREQTRADE_BIN, "backtesting",
        "--config", DEFAULT_EXCHANGE_CONFIG,
        "--strategy", job.strategy_name,
        "--timeframe", timeframe,
        "--userdir", USER_DATA_DIR,
        "--export", "trades",
        "--export-filename", str(result_file),
        "--breakdown", "month",
    ]
    if pairs:
        cmd += ["--pairs"] + pairs
    if timerange:
        cmd += ["--timerange", timerange]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    job.process = proc

    stdout_lines = []
    stderr_lines = []

    async def read_stdout():
        async for raw in proc.stdout:
            line = raw.decode(errors="replace")
            stdout_lines.append(line)
            if "%" in line:
                try:
                    pct_str = [t for t in line.split() if "%" in t][0].replace("%", "")
                    job.progress = min(int(float(pct_str)), 99)
                    job.persist()
                except Exception:
                    pass

    async def read_stderr():
        async for raw in proc.stderr:
            stderr_lines.append(raw.decode(errors="replace"))

    await asyncio.gather(read_stdout(), read_stderr())
    await proc.wait()

    output = "".join(stdout_lines)
    err_output = "".join(stderr_lines)
    combined = output + err_output
    job.finished_at = datetime.utcnow().isoformat()

    if proc.returncode != 0:
        job.status = JobStatus.FAILED
        job.error  = combined[-3000:] or f"Process exited with code {proc.returncode}"
        job.persist()
        return

    job.status   = JobStatus.COMPLETED
    job.progress = 100
    job.result   = {"result_file": str(result_file), "log": combined[-2000:]}
    job.persist()


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
                "total_trades":     m.get("total_trades"),
                "win_rate":         round(m.get("wins", 0) / total * 100, 2),
                "profit_total_pct": m.get("profit_total_pct"),
                "profit_factor":    m.get("profit_factor"),
                "max_drawdown_pct": m.get("max_drawdown_account"),
                "sharpe":           m.get("sharpe"),
                "calmar":           m.get("calmar"),
                "sortino":          m.get("sortino"),
                "avg_duration":     m.get("holding_avg"),
            }

    return {"job_id": job_id, "summary": summary, "raw_file": str(result_file)}
