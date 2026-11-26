"""Tool: run_backtest & get_backtest_results."""

import asyncio
import json
import re
import uuid
import zipfile
import io
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
    if pairs and _validate_pairs(pairs) is None:
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

    cmd = [
        FREQTRADE_BIN, "backtesting",
        "--config", DEFAULT_EXCHANGE_CONFIG,
        "--strategy", job.strategy_name,
        "--timeframe", timeframe,
        "--userdir", USER_DATA_DIR,
        "--export", "trades",
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

    combined = "".join(stdout_lines) + "".join(stderr_lines)
    job.finished_at = datetime.utcnow().isoformat()

    if proc.returncode != 0:
        job.status = JobStatus.FAILED
        job.error  = combined[-3000:] or f"Process exited with code {proc.returncode}"
        job.persist()
        return

    # Cari file hasil terbaru yang di-generate freqtrade
    # Format baru (2024+): .zip — Format lama: .json
    result_zip  = _find_latest_result_file(job.strategy_name, suffix=".zip")
    result_json = _find_latest_result_file(job.strategy_name, suffix=".json")
    result_path = result_zip or result_json

    job.status   = JobStatus.COMPLETED
    job.progress = 100
    job.result   = {
        "result_file": str(result_path) if result_path else None,
        "log": combined[-2000:],
        "strategy_name": job.strategy_name,
    }
    job.persist()


def _find_latest_result_file(strategy_name: str, suffix: str) -> Path | None:
    """Cari file hasil backtest terbaru untuk strategy tertentu."""
    backtest_dir = Path(USER_DATA_DIR) / "backtest_results"
    # Cari berdasarkan meta.json yang berisi nama strategy
    if suffix == ".zip":
        candidates = sorted(backtest_dir.glob("*.zip"), key=lambda f: f.stat().st_mtime, reverse=True)
        for candidate in candidates:
            meta_path = candidate.with_suffix(".meta.json")
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                    if strategy_name in meta:
                        return candidate
                except Exception:
                    pass
        # Fallback: file zip terbaru
        return candidates[0] if candidates else None
    else:
        candidates = sorted(
            [f for f in backtest_dir.glob("*.json") if not f.name.startswith(".")],
            key=lambda f: f.stat().st_mtime, reverse=True
        )
        for candidate in candidates:
            try:
                data = json.loads(candidate.read_text())
                if "strategy" in data and strategy_name in data["strategy"]:
                    return candidate
            except Exception:
                pass
        return candidates[0] if candidates else None


def _parse_result_file(result_path: Path, strategy_name: str) -> dict:
    """
    Parse file hasil backtest — support format .zip (Freqtrade 2024+) dan .json (lama).
    Return dict summary per strategy.
    """
    if not result_path or not result_path.exists():
        return {}

    data = None

    if result_path.suffix == ".zip":
        try:
            with zipfile.ZipFile(result_path) as zf:
                # File utama biasanya punya nama yang sama dengan zip tapi .json
                # atau nama pertama yang bukan trades/metadata
                for name in zf.namelist():
                    if (name.endswith(".json")
                            and "trade" not in name.lower()
                            and "metadata" not in name.lower()):
                        data = json.loads(zf.read(name))
                        break
                # Fallback: baca semua sampai dapat key 'strategy'
                if data is None or "strategy" not in data:
                    for name in zf.namelist():
                        if name.endswith(".json"):
                            try:
                                candidate = json.loads(zf.read(name))
                                if "strategy" in candidate:
                                    data = candidate
                                    break
                            except Exception:
                                pass
        except Exception as e:
            return {}
    else:
        try:
            data = json.loads(result_path.read_text())
        except Exception:
            return {}

    if not data or "strategy" not in data:
        return {}

    summary = {}
    for strat, m in data["strategy"].items():
        total = max(m.get("total_trades", 0) or 0, 1)
        summary[strat] = {
            "total_trades":     m.get("total_trades", 0),
            "win_rate":         round((m.get("wins", 0) or 0) / total * 100, 2),
            "profit_total_pct": m.get("profit_total_pct"),
            "profit_factor":    m.get("profit_factor"),
            "max_drawdown_pct": m.get("max_drawdown_account"),
            "sharpe":           m.get("sharpe"),
            "calmar":           m.get("calmar"),
            "sortino":          m.get("sortino"),
            "avg_duration":     m.get("holding_avg"),
        }

    return summary


async def get_backtest_results(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        return {"error": f"Job {job_id} not found"}
    if job.status != JobStatus.COMPLETED:
        return {"error": f"Job not completed yet, status: {job.status}"}

    result_file_str = (job.result or {}).get("result_file")
    strategy_name   = (job.result or {}).get("strategy_name") or job.strategy_name

    # Coba pakai path yang tersimpan di job
    result_path = None
    if result_file_str:
        p = Path(result_file_str)
        if p.exists():
            result_path = p

    # Fallback: cari file terbaru yang cocok dengan strategy
    if not result_path:
        result_path = (
            _find_latest_result_file(strategy_name, suffix=".zip")
            or _find_latest_result_file(strategy_name, suffix=".json")
        )

    if not result_path:
        return {"error": "No backtest result file found", "job_id": job_id}

    summary = _parse_result_file(result_path, strategy_name)

    if not summary:
        return {
            "error": f"Hasil backtest tidak bisa diparsing dari {result_path.name}. "
                     f"File ada tapi format tidak dikenali atau strategy '{strategy_name}' tidak ada di hasil.",
            "job_id": job_id,
            "raw_file": str(result_path),
        }

    return {"job_id": job_id, "summary": summary, "raw_file": str(result_path)}
