"""Tool: run_backtest & get_backtest_results."""

import asyncio
import json
import math
import re
import time
import uuid
import zipfile
import io
from datetime import datetime
from pathlib import Path

from config import FREQTRADE_BIN, USER_DATA_DIR, DEFAULT_EXCHANGE_CONFIG, RESULTS_DIR, STRATEGIES_DIR
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


# ---------------------------------------------------------------------------
# Fee (biaya per sisi). Freqtrade memakai rasio: 0.001 = 0.1% per sisi.
# Default server = 0.001, identik dengan perilaku sebelum parameter ini ada
# (tanpa --fee freqtrade memakai "worst case fee from exchange" = 0.1000%).
# ---------------------------------------------------------------------------
FEE_MIN = 0.0
FEE_MAX = 0.01
FEE_DEFAULT = 0.001

_FEE_LOG_RE = re.compile(r"Using fee\s+([0-9.]+)%")


def validate_fee(fee) -> str | None:
    """Return pesan error bila fee tidak valid, None bila OK."""
    if fee is None:
        return None
    if isinstance(fee, bool) or not isinstance(fee, (int, float)):
        return (
            f"fee harus angka (float) antara {FEE_MIN} dan {FEE_MAX}, "
            f"bukan {type(fee).__name__}."
        )
    if not math.isfinite(fee):
        return f"fee harus angka finite, bukan {fee}."
    if fee < FEE_MIN or fee > FEE_MAX:
        return (
            f"fee={fee} di luar rentang yang diizinkan ({FEE_MIN}-{FEE_MAX} = 0%-1% per sisi). "
            "Contoh valid: 0.001 (0.1%, default) atau 0.0005 (0.05%)."
        )
    return None


def fee_to_arg(fee: float) -> str:
    """Format fee untuk --fee tanpa kehilangan presisi (0.0005 tetap 0.0005)."""
    return repr(float(fee))


def parse_effective_fee(log: str) -> float | None:
    """Ambil fee yang benar-benar dipakai freqtrade dari log-nya.

    Baris log: 'Using fee 0.1000% from config.' → 0.001
    """
    matches = _FEE_LOG_RE.findall(log or "")
    if not matches:
        return None
    try:
        return round(float(matches[-1]) / 100, 10)
    except Exception:
        return None


async def run_backtest(
    strategy_name: str,
    pairs: list[str] | None = None,
    timeframe: str = "1h",
    timerange: str | None = None,
    fee: float | None = FEE_DEFAULT,
) -> dict:
    if not _validate_name(strategy_name):
        return {"error": f"Strategy name tidak valid: {strategy_name}"}
    if timeframe not in _VALID_TIMEFRAMES:
        return {"error": f"Timeframe tidak valid: {timeframe}"}
    if (fee_err := validate_fee(fee)) is not None:
        return {"error": fee_err}

    # B-05: pairs kosong diam-diam pakai pairlist config → tolak eksplisit
    if pairs is not None and len(pairs) == 0:
        return {"error": "pairs tidak boleh kosong. Sertakan minimal 1 pair (contoh: [\"BTC/USDT\"]) atau hapus argumen pairs."}
    if pairs and _validate_pairs(pairs) is None:
        return {"error": "Format pair tidak valid. Gunakan format BASE/QUOTE (contoh: BTC/USDT)"}
    if timerange and not _SAFE_TIMERANGE.match(timerange):
        return {"error": f"Timerange tidak valid: {timerange}. Format: YYYYMMDD atau YYYYMMDD-YYYYMMDD"}

    # B-04: validasi keberadaan file strategy sebelum queue
    strategy_file = Path(STRATEGIES_DIR) / f"{strategy_name}.py"
    if not strategy_file.exists():
        return {"error": f"Strategy '{strategy_name}' tidak ditemukan. Jalankan tool_list_strategies untuk melihat daftar yang tersedia."}

    job_id = uuid.uuid4().hex[:12]
    job = Job(job_id=job_id, job_type="backtest", strategy_name=strategy_name,
              fee=fee, fee_effective=None)
    _jobs[job_id] = job
    job.persist()
    asyncio.create_task(_execute_backtest(job, pairs, timeframe, timerange, fee))
    return {
        "job_id": job_id,
        "status": "pending",
        "message": f"Backtest queued (fee={fee})",
        "fee": fee,
    }


async def _execute_backtest(job, pairs, timeframe, timerange, fee=None):
    job.status = JobStatus.RUNNING
    job.persist()

    backtest_dir = Path(USER_DATA_DIR) / "backtest_results"
    backtest_dir.mkdir(parents=True, exist_ok=True)

    # F-2: snapshot mtime semua file yang ada sebelum proses mulai
    before_files: set[Path] = set(backtest_dir.glob("*.zip")) | set(
        f for f in backtest_dir.glob("*.json") if not f.name.startswith(".")
    )
    job_start_mtime = time.monotonic()

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
    # Fee yang benar-benar dipakai freqtrade (dari log "Using fee X%") — jejak audit.
    job.fee_effective = parse_effective_fee(combined) or job.fee

    if proc.returncode != 0:
        job.status = JobStatus.FAILED
        job.error  = combined[-3000:] or f"Process exited with code {proc.returncode}"
        job.persist()
        return

    # F-2: Ambil file result dari daftar file yang ada SEBELUM backtest dijalankan,
    # dan bandingkan dengan yang ada SETELAH — file baru = milik job ini.
    # Fallback ke meta.json lookup jika timestamp approach tidak bisa.
    result_path = _find_new_result_file(job.strategy_name, started_at=job_start_mtime)

    # F-5: completed tanpa artefak = sebenarnya failed
    if not result_path:
        job.status = JobStatus.FAILED
        job.error  = (
            "Backtest selesai (exit 0) tapi tidak ada file hasil yang bisa ditemukan. "
            "Kemungkinan tidak ada candle untuk timerange/pair yang diminta.\n"
            + combined[-3000:]
        )
        job.persist()
        return

    job.status   = JobStatus.COMPLETED
    job.progress = 100
    job.result   = {
        "result_file": str(result_path),
        "log": combined[-2000:],
        "strategy_name": job.strategy_name,
        "fee": job.fee_effective,
        "fee_requested": job.fee,
    }
    job.persist()


def _find_new_result_file(strategy_name: str, started_at: float) -> Path | None:
    """
    F-2: Cari file hasil yang BARU muncul setelah backtest dimulai.
    Prioritas: .zip yang muncul setelah `started_at`, cocok strategy_name via meta.json.
    Fallback: .zip terbaru setelah started_at tanpa cek strategy (1 job aktif sekaligus).
    Fallback terakhir: meta.json lookup (backward compat).
    """
    backtest_dir = Path(USER_DATA_DIR) / "backtest_results"
    now = time.time()
    # started_at adalah monotonic, ubah ke epoch approx (hanya untuk estimasi window)
    # Gunakan wall-clock cutoff: file yang mtime-nya setelah proses kira-kira mulai
    cutoff_epoch = now - (time.monotonic() - started_at) - 5  # +5 detik toleransi

    new_zips = [
        f for f in backtest_dir.glob("*.zip")
        if f.stat().st_mtime >= cutoff_epoch
    ]
    new_zips.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    # Prioritas: zip baru yang meta.json-nya memuat strategy_name
    for candidate in new_zips:
        meta_path = candidate.with_suffix(".meta.json")
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                if strategy_name in meta:
                    return candidate
            except Exception:
                pass

    # Fallback: zip baru paling muda (1 job aktif per waktu)
    if new_zips:
        return new_zips[0]

    # Fallback terakhir: cari semua zip dengan meta.json
    return _find_latest_result_file(strategy_name, suffix=".zip") or \
           _find_latest_result_file(strategy_name, suffix=".json")


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
    Field names dari dokumentasi resmi Freqtrade:
      profit_total        → total profit sebagai desimal (0.0572 = 5.72%)
      profit_total_abs    → absolute profit dalam stake currency
      profit_factor       → profit factor
      max_drawdown        → max drawdown sebagai desimal
      max_drawdown_abs    → max drawdown dalam stake currency
      sharpe              → Sharpe ratio
      sortino             → Sortino ratio
      calmar              → Calmar ratio
      holding_avg         → avg duration string
      wins, draws, losses → int
      total_trades        → int
    """
    if not result_path or not result_path.exists():
        return {}

    data = None

    if result_path.suffix == ".zip":
        try:
            with zipfile.ZipFile(result_path) as zf:
                for name in zf.namelist():
                    if (name.endswith(".json")
                            and "trade" not in name.lower()
                            and "metadata" not in name.lower()):
                        try:
                            candidate = json.loads(zf.read(name))
                            if "strategy" in candidate:
                                data = candidate
                                break
                        except Exception:
                            pass
        except Exception:
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
        wins  = m.get("wins", 0) or 0

        # profit_total_pct sudah dalam persen di zip (Freqtrade hitung sendiri via round(x*100, 2))
        # Jika field ini tidak ada, fallback ke profit_total (desimal) × 100
        if "profit_total_pct" in m:
            profit_pct = m["profit_total_pct"]
        elif "profit_total" in m and m["profit_total"] is not None:
            profit_pct = round(m["profit_total"] * 100, 4)
        else:
            profit_pct = None

        # max_drawdown_account adalah fraksi akun (0.0202 = 2.02%) → ×100 untuk persen
        # Field ini berbeda dengan max_drawdown (per-trade drawdown)
        dd_account = m.get("max_drawdown_account")
        drawdown_pct = round(dd_account * 100, 4) if dd_account is not None else None

        summary[strat] = {
            "total_trades":      m.get("total_trades", 0),
            "wins":              wins,
            "draws":             m.get("draws", 0),
            "losses":            m.get("losses", 0),
            "win_rate":          round(wins / total * 100, 2),
            "profit_total_pct":  profit_pct,          # sudah dalam %
            "profit_total_abs":  m.get("profit_total_abs"),   # USDT absolute
            "profit_factor":     m.get("profit_factor"),
            "max_drawdown_pct":  drawdown_pct,         # % dari akun (account-level)
            "max_drawdown_abs":  m.get("max_drawdown_abs"),
            "sharpe":            m.get("sharpe"),
            "sortino":           m.get("sortino"),
            "calmar":            m.get("calmar"),
            "avg_duration":      m.get("holding_avg"),
            "cagr":              m.get("cagr"),
            "expectancy":        m.get("expectancy"),
            "sqn":               m.get("sqn"),
        }

    return summary


async def get_backtest_results(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        return {"error": f"Job {job_id} not found"}

    # B-02: tolak eksplisit jika bukan job backtest
    if job.job_type != "backtest":
        return {"error": f"Job {job_id} adalah job '{job.job_type}', bukan backtest. Gunakan tool_get_hyperopt_results untuk job hyperopt."}

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

    return {
        "job_id": job_id,
        "summary": summary,
        "fee": job.fee_effective if job.fee_effective is not None else job.fee,
        "fee_requested": job.fee,
        "raw_file": str(result_path),
    }
