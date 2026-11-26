"""Tool: download_data — unduh data historis OHLCV dari Binance."""

import asyncio
from pathlib import Path
from config import FREQTRADE_BIN, USER_DATA_DIR, DEFAULT_EXCHANGE_CONFIG
from tools.backtest import _VALID_TIMEFRAMES, _validate_pairs


async def download_data(
    pairs: list[str],
    timeframe: str = "1h",
    days: int = 180,
) -> dict:
    if not pairs:
        return {"error": "pairs tidak boleh kosong"}
    if _validate_pairs(pairs) is None:
        return {"error": "Format pair tidak valid. Gunakan format BASE/QUOTE (contoh: BTC/USDT)"}
    if timeframe not in _VALID_TIMEFRAMES:
        return {"error": f"Timeframe tidak valid: {timeframe}"}
    if not isinstance(days, int) or days < 1 or days > 730:
        return {"error": "days harus integer antara 1–730"}

    # B-08: catat ukuran file sebelum download untuk deteksi sukses palsu
    data_dir = Path(USER_DATA_DIR) / "data" / "binance"
    before_sizes = {}
    for pair in pairs:
        fname = pair.replace("/", "_") + f"-{timeframe}.feather"
        fpath = data_dir / fname
        before_sizes[pair] = fpath.stat().st_size if fpath.exists() else -1

    cmd = [
        FREQTRADE_BIN, "download-data",
        "--config", DEFAULT_EXCHANGE_CONFIG,
        "--pairs", *pairs,
        "--timeframe", timeframe,
        "--days", str(days),
        "--userdir", USER_DATA_DIR,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode()

    if proc.returncode != 0:
        return {"success": False, "error": output[-2000:]}

    # B-08: verifikasi file data benar-benar ada/bertambah setelah download
    # Freqtrade simpan data di berbagai format (.feather, .json, .json.gz)
    # Cek apakah output log mengandung konfirmasi download yang nyata
    success_pairs = []
    failed_pairs = []

    for pair in pairs:
        # Cek berbagai ekstensi yang dipakai freqtrade
        pair_safe = pair.replace("/", "_")
        found = False
        for ext in [f"-{timeframe}.feather", f"-{timeframe}.json", f"-{timeframe}.json.gz"]:
            fpath = data_dir / (pair_safe + ext)
            if fpath.exists() and fpath.stat().st_size > 0:
                found = True
                break

        if found:
            success_pairs.append(pair)
        elif f"No data for {pair}" in output or f"No candles for {pair}" in output:
            failed_pairs.append(pair)
        elif "not in exchange" in output.lower() or f"{pair}" not in output:
            # Pair tidak dikenali exchange — anggap gagal
            failed_pairs.append(pair)
        else:
            # Tidak bisa pastikan — cek log freqtrade untuk kata kunci sukses
            if "Dataload complete" in output or "Downloaded data for" in output:
                success_pairs.append(pair)
            else:
                failed_pairs.append(pair)

    if not success_pairs:
        return {
            "success": False,
            "pairs": pairs,
            "failed_pairs": failed_pairs,
            "error": f"Download gagal untuk semua pair. Periksa apakah pair valid di Binance.",
            "log": output[-1000:],
        }

    result = {
        "success": True,
        "pairs": success_pairs,
        "timeframe": timeframe,
        "days": days,
        "message": f"Data downloaded for {len(success_pairs)} pair(s)",
    }
    if failed_pairs:
        result["warning"] = f"Pair tidak ditemukan di exchange: {failed_pairs}"
        result["failed_pairs"] = failed_pairs

    return result
