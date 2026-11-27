"""Tool: download_data — unduh data historis OHLCV dari Binance."""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from config import FREQTRADE_BIN, USER_DATA_DIR, DEFAULT_EXCHANGE_CONFIG
from tools.backtest import _VALID_TIMEFRAMES, _validate_pairs

# Timerange opsional: YYYYMMDD-YYYYMMDD
_SAFE_TIMERANGE = re.compile(r'^\d{8}-\d{8}$')


async def download_data(
    pairs: list[str],
    timeframe: str = "1h",
    days: int | None = None,
    timerange: str | None = None,
) -> dict:
    """
    Unduh data historis OHLCV dari Binance.

    Gunakan `timerange` (format YYYYMMDD-YYYYMMDD) untuk kontrol presisi penuh,
    atau `days` untuk N hari ke belakang. Plafon days dihapus — gunakan 1–3650.
    Jika keduanya disertakan, timerange mengalahkan days.
    """
    if not pairs:
        return {"error": "pairs tidak boleh kosong"}
    if _validate_pairs(pairs) is None:
        return {"error": "Format pair tidak valid. Gunakan format BASE/QUOTE (contoh: BTC/USDT)"}
    if timeframe not in _VALID_TIMEFRAMES:
        return {"error": f"Timeframe tidak valid: {timeframe}"}

    # Validasi parameter waktu
    if timerange is not None:
        if not _SAFE_TIMERANGE.match(timerange):
            return {"error": "timerange harus format YYYYMMDD-YYYYMMDD (contoh: 20240101-20261127)"}
    elif days is not None:
        if not isinstance(days, int) or days < 1 or days > 3650:
            return {"error": "days harus integer antara 1–3650 (10 tahun)"}
    else:
        days = 365  # default 1 tahun

    # Catat ukuran/mtime file sebelum download (untuk F-2: deteksi sukses nyata)
    data_dir = Path(USER_DATA_DIR) / "data" / "binance"
    before_mtime: dict[str, float] = {}
    for pair in pairs:
        pair_safe = pair.replace("/", "_")
        for ext in [f"-{timeframe}.feather", f"-{timeframe}.json", f"-{timeframe}.json.gz"]:
            fpath = data_dir / (pair_safe + ext)
            if fpath.exists():
                before_mtime[pair] = fpath.stat().st_mtime
                break

    cmd = [
        FREQTRADE_BIN, "download-data",
        "--config", DEFAULT_EXCHANGE_CONFIG,
        "--pairs", *pairs,
        "--timeframe", timeframe,
        "--userdir", USER_DATA_DIR,
    ]
    # F-5: timerange lebih presisi daripada --days untuk history panjang
    # Freqtrade --days kadang stop terlalu awal untuk timeframe kecil (1h)
    if timerange:
        cmd += ["--timerange", timerange]
    else:
        # Konversi days → timerange eksplisit: lebih reliable daripada --days
        end_dt   = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days)
        tr = start_dt.strftime("%Y%m%d") + "-" + end_dt.strftime("%Y%m%d")
        cmd += ["--timerange", tr]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode()

    if proc.returncode != 0:
        return {"success": False, "error": output[-2000:]}

    # Verifikasi file dan baca cakupan aktual dari data file
    success_pairs = []
    failed_pairs  = []
    coverage: dict[str, dict] = {}

    for pair in pairs:
        pair_safe = pair.replace("/", "_")
        found_path = None
        for ext in [f"-{timeframe}.feather", f"-{timeframe}.json", f"-{timeframe}.json.gz"]:
            fpath = data_dir / (pair_safe + ext)
            if fpath.exists() and fpath.stat().st_size > 0:
                found_path = fpath
                break

        if not found_path:
            # Cek kata kunci di log
            if "No data for" in output or "No candles" in output:
                failed_pairs.append(pair)
            elif "Dataload complete" in output or "Downloaded" in output:
                success_pairs.append(pair)
            else:
                failed_pairs.append(pair)
            continue

        success_pairs.append(pair)

        # F-1: baca cakupan nyata dari log freqtrade
        # Format log: "BTC/USDT, 1h, data starts 2017-08-17 04:00:00"
        first_candle = None
        last_candle  = None
        candle_count = None
        for line in output.splitlines():
            if pair in line and "data starts" in line.lower():
                # "BTC/USDT, 1h, data starts 2017-08-17 04:00:00, data ends 2026-11-27 ..."
                try:
                    parts = line.split("data starts")
                    if len(parts) > 1:
                        rest = parts[1].strip()
                        first_candle = rest.split(",")[0].strip()
                except Exception:
                    pass
            if pair in line and ("data ends" in line.lower() or "data stop" in line.lower()):
                try:
                    for kw in ("data ends", "data stop"):
                        if kw in line.lower():
                            rest = line.lower().split(kw)[1]
                            # ambil tanggal dari sisi kanan
                            last_candle = line.split(kw, 1)[1].strip().split(",")[0].strip()
                except Exception:
                    pass

        # Hitung jumlah candle dari ukuran file sebagai estimasi kasar
        try:
            size_mb = found_path.stat().st_size / (1024 * 1024)
            # feather ~50 bytes/candle rata-rata
            candle_count_est = int(found_path.stat().st_size / 50)
        except Exception:
            candle_count_est = None

        coverage[pair] = {
            "file": str(found_path),
            "first_candle": first_candle,
            "last_candle":  last_candle,
            "candle_count_est": candle_count_est,
        }

    if not success_pairs:
        return {
            "success": False,
            "pairs": pairs,
            "failed_pairs": failed_pairs,
            "error": "Download gagal untuk semua pair. Periksa apakah pair valid di Binance.",
            "log": output[-1000:],
        }

    result: dict = {
        "success":   True,
        "pairs":     success_pairs,
        "timeframe": timeframe,
        "coverage":  coverage,
        "message":   f"Data downloaded for {len(success_pairs)} pair(s)",
    }
    if failed_pairs:
        result["warning"]      = f"Pair tidak ditemukan di exchange: {failed_pairs}"
        result["failed_pairs"] = failed_pairs
    # Sertakan log akhir agar agent bisa konfirmasi cakupan jika coverage parsing kosong
    result["log_tail"] = "\n".join(output.splitlines()[-15:])

    return result
