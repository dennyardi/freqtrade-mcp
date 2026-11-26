"""Tool: download_data — unduh data historis OHLCV dari Binance."""

import asyncio
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

    return {
        "success": True,
        "pairs": pairs,
        "timeframe": timeframe,
        "days": days,
        "message": f"Data downloaded for {len(pairs)} pair(s)",
    }
