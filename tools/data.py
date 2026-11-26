"""Tool: download_data — unduh data historis OHLCV dari Binance."""

import asyncio
from config import FREQTRADE_BIN, USER_DATA_DIR, DEFAULT_EXCHANGE_CONFIG


async def download_data(
    pairs: list[str],
    timeframe: str = "1h",
    days: int = 180,
) -> dict:
    pairs_args = " ".join(f'"{p}"' for p in pairs)
    cmd = (
        f"{FREQTRADE_BIN} download-data"
        f" --config {DEFAULT_EXCHANGE_CONFIG}"
        f" --pairs {pairs_args}"
        f" --timeframe {timeframe}"
        f" --days {days}"
        f" --userdir {USER_DATA_DIR}"
    )

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode()

    if proc.returncode != 0:
        return {"success": False, "error": output}

    return {
        "success": True,
        "pairs": pairs,
        "timeframe": timeframe,
        "days": days,
        "message": f"Data downloaded for {len(pairs)} pair(s)",
    }
