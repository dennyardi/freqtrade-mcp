"""Tool: run_hyperopt & get_hyperopt_results."""

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path

from config import FREQTRADE_BIN, USER_DATA_DIR, DEFAULT_EXCHANGE_CONFIG
from tools.jobs import _jobs, Job, JobStatus
from tools.strategy import _validate_name
from tools.backtest import _SAFE_TIMERANGE, _validate_pairs, _SAFE_PAIR
from tools.backtest import FEE_DEFAULT, validate_fee, fee_to_arg, parse_effective_fee

# Hyperopt spaces yang diizinkan
_VALID_SPACES = {"buy", "sell", "roi", "stoploss", "trailing", "protection", "default", "all"}


async def run_hyperopt(
    strategy_name: str,
    epochs: int = 100,
    spaces: list[str] | None = None,
    timerange: str | None = None,
    pairs: list[str] | None = None,
    fee: float | None = FEE_DEFAULT,
) -> dict:
    if not _validate_name(strategy_name):
        return {"error": f"Strategy name tidak valid: {strategy_name}"}
    if not isinstance(epochs, int) or epochs < 1 or epochs > 10000:
        return {"error": "epochs harus integer antara 1–10000"}
    if (fee_err := validate_fee(fee)) is not None:
        return {"error": fee_err}
    if spaces is None:
        spaces = ["roi", "stoploss"]
    invalid_spaces = [s for s in spaces if s not in _VALID_SPACES]
    if invalid_spaces:
        return {"error": f"Space tidak valid: {invalid_spaces}. Pilihan: {sorted(_VALID_SPACES)}"}
    if timerange and not _SAFE_TIMERANGE.match(timerange):
        return {"error": f"Timerange tidak valid: {timerange}"}
    # B-07: validasi pairs jika disertakan
    if pairs is not None and len(pairs) == 0:
        return {"error": "pairs tidak boleh kosong. Sertakan minimal 1 pair atau hapus argumen pairs."}
    if pairs and _validate_pairs(pairs) is None:
        return {"error": "Format pair tidak valid. Gunakan format BASE/QUOTE (contoh: BTC/USDT)"}

    # Validasi keberadaan strategy sebelum queue
    from pathlib import Path
    from config import STRATEGIES_DIR
    if not (Path(STRATEGIES_DIR) / f"{strategy_name}.py").exists():
        return {"error": f"Strategy '{strategy_name}' tidak ditemukan."}

    job_id = uuid.uuid4().hex[:12]
    job = Job(job_id=job_id, job_type="hyperopt", strategy_name=strategy_name,
              fee=fee, fee_effective=None)
    _jobs[job_id] = job
    job.persist()
    asyncio.create_task(_execute_hyperopt(job, epochs, spaces, timerange, pairs, fee))
    return {
        "job_id": job_id,
        "status": "pending",
        "message": f"Hyperopt queued ({epochs} epochs, fee={fee})",
        "fee": fee,
    }


async def _execute_hyperopt(job, epochs, spaces, timerange, pairs=None, fee=None):
    job.status = JobStatus.RUNNING
    job.persist()

    # B-07: --pairs di freqtrade hyperopt tidak override pair_whitelist di config,
    # hanya filter. Satu-satunya cara override adalah pakai --config tambahan
    # yang set exchange.pair_whitelist secara eksplisit.
    extra_config_path = None
    if pairs:
        import tempfile, json as _json
        extra_cfg = {
            "exchange": {"pair_whitelist": pairs},
            "pairlists": [{"method": "StaticPairList"}],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
            dir=Path(USER_DATA_DIR),
            prefix=f"hyperopt_pairs_{job.job_id}_"
        ) as f:
            _json.dump(extra_cfg, f)
            extra_config_path = f.name

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
    if extra_config_path:
        # Tambahkan config override setelah config utama — freqtrade merge & override
        cmd += ["--config", extra_config_path]
    if timerange:
        cmd += ["--timerange", timerange]
    # Fee override per run — tidak menyentuh config global.
    if fee is not None:
        cmd += ["--fee", fee_to_arg(fee)]

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
            if "Epoch" in line and "/" in line:
                try:
                    parts = [t for t in line.split() if "/" in t][0]
                    current, total = parts.split("/")
                    job.progress = min(int(int(current) / int(total) * 100), 99)
                    job.persist()
                except Exception:
                    pass

    async def read_stderr():
        async for raw in proc.stderr:
            stderr_lines.append(raw.decode(errors="replace"))

    await asyncio.gather(read_stdout(), read_stderr())
    await proc.wait()

    # Hapus config temp pairs jika ada
    if extra_config_path:
        try:
            Path(extra_config_path).unlink(missing_ok=True)
        except Exception:
            pass

    output = "".join(stdout_lines)
    err_output = "".join(stderr_lines)
    combined = output + err_output
    job.finished_at = datetime.utcnow().isoformat()
    job.fee_effective = parse_effective_fee(combined) or job.fee

    if proc.returncode != 0:
        job.status = JobStatus.FAILED
        job.error  = combined[-3000:] or f"Process exited with code {proc.returncode}"
        job.persist()
        return

    job.status   = JobStatus.COMPLETED
    job.progress = 100
    job.result   = {
        "log": combined[-3000:],
        "fee": job.fee_effective,
        "fee_requested": job.fee,
    }
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
        return {
            "job_id": job_id,
            "best_params": best,
            "fee": job.fee_effective if job.fee_effective is not None else job.fee,
            "fee_requested": job.fee,
        }
    except Exception:
        return {
            "job_id": job_id,
            "raw_output": stdout.decode()[-2000:],
            "fee": job.fee_effective if job.fee_effective is not None else job.fee,
            "fee_requested": job.fee,
        }
