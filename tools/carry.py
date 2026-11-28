"""Backtest carry: long spot + short perpetual dengan notional sama (delta-netral).

Kenapa modul ini ada (issue carry-tool, 2026-11-28):

Freqtrade tidak bisa memegang dua leg sekaligus dalam satu strategi, jadi panen
funding (long spot + short perp) tidak bisa diukur dengan `tool_run_backtest`.
Backtest futures hanya mengukur satu leg dan funding jadi biaya, bukan pendapatan.

Modul ini memanggil mesin simulasi terpisah `carry_sim.py` memakai interpreter
freqtrade (butuh pyarrow untuk membaca feather; venv MCP tidak punya pyarrow).
Simulasi memakai candle spot + candle perp yang sudah ada dan file
`funding_rate` freqtrade, jadi tidak ada unduhan data baru dan tidak ada
perubahan config/strategi.

Model lengkap ada di docstring `carry_sim.py`; ringkasnya:
  - entry: beli spot `stake`, short perp dengan notional sama (qty koin beda)
  - P&L = basis (selisih return dua leg) + funding diterima short - fee dua leg
  - funding_basis="fixed" (default) = stake x rate (setara tabel funding tahunan),
    "notional" = notional short saat itu x rate (seperti freqtrade membebankan)
  - rebalance "none" | "daily" | "weekly" menyamakan notional kedua leg
"""

import asyncio
import json
import re
from pathlib import Path

from config import FREQTRADE_BIN, USER_DATA_DIR

FREQTRADE_PYTHON = str(Path(FREQTRADE_BIN).with_name("python"))
SIM_SCRIPT = str(Path(__file__).with_name("carry_sim.py"))
SIM_TIMEOUT = 300.0

REBALANCE_MODES = ("none", "daily", "weekly")
FUNDING_BASES = ("fixed", "notional")
_TIMERANGE_RE = re.compile(r"^\d{8}(-\d{8})?$")
FEE_MAX = 0.01


def _validate(pair: str, perp_pair: str, stake: float, fee_spot: float,
              fee_perp: float, rebalance: str, leverage: float,
              funding_basis: str, maintenance_margin: float,
              timerange: str | None) -> str | None:
    """Return pesan error, atau None bila argumen valid."""
    if not pair or ":" in pair:
        return ("pair harus pair spot tanpa settle currency (contoh \"BTC/USDT\"). "
                "Pair futures masuk ke perp_pair.")
    if not perp_pair or ":" not in perp_pair:
        return ("perp_pair harus pair futures berformat BASE/QUOTE:SETTLE "
                "(contoh \"BTC/USDT:USDT\").")
    if timerange is not None and not _TIMERANGE_RE.match(timerange):
        return "timerange harus format YYYYMMDD atau YYYYMMDD-YYYYMMDD"
    try:
        stake = float(stake)
    except (TypeError, ValueError):
        return "stake harus angka (USDT)"
    if not (1.0 <= stake <= 100_000_000.0):
        return "stake harus antara 1 dan 100000000 USDT"
    for name, fee in (("fee_spot", fee_spot), ("fee_perp", fee_perp)):
        if isinstance(fee, bool) or not isinstance(fee, (int, float)):
            return f"{name} harus angka (rasio, contoh 0.001 = 0.1%)"
        if not (0.0 <= float(fee) <= FEE_MAX):
            return f"{name} harus antara 0 dan {FEE_MAX}"
    if rebalance not in REBALANCE_MODES:
        return f"rebalance harus salah satu dari {list(REBALANCE_MODES)}"
    if funding_basis not in FUNDING_BASES:
        return f"funding_basis harus salah satu dari {list(FUNDING_BASES)}"
    try:
        leverage = float(leverage)
    except (TypeError, ValueError):
        return "leverage harus angka"
    if not (1.0 <= leverage <= 20.0):
        return "leverage harus antara 1 dan 20"
    try:
        maintenance_margin = float(maintenance_margin)
    except (TypeError, ValueError):
        return "maintenance_margin harus angka (rasio, contoh 0.005)"
    if not (0.0 <= maintenance_margin <= 0.2):
        return "maintenance_margin harus antara 0 dan 0.2"
    return None


async def backtest_carry(
    pair: str = "BTC/USDT",
    perp_pair: str = "BTC/USDT:USDT",
    timerange: str | None = "20240101-20241130",
    stake: float = 1000.0,
    fee_spot: float = 0.001,
    fee_perp: float = 0.0005,
    rebalance: str = "none",
    leverage: float = 3.0,
    funding_basis: str = "fixed",
    maintenance_margin: float = 0.005,
    timeframe: str = "1h",
) -> dict:
    """Simulasikan long spot + short perp (delta-netral) dan panen funding."""
    error = _validate(pair, perp_pair, stake, fee_spot, fee_perp, rebalance,
                      leverage, funding_basis, maintenance_margin, timerange)
    if error:
        return {"error": error}

    cfg = {
        "pair": pair,
        "perp_pair": perp_pair,
        "timerange": timerange,
        "stake": float(stake),
        "fee_spot": float(fee_spot),
        "fee_perp": float(fee_perp),
        "rebalance": rebalance,
        "leverage": float(leverage),
        "funding_basis": funding_basis,
        "maintenance_margin": float(maintenance_margin),
        "timeframe": timeframe,
        "user_data_dir": USER_DATA_DIR,
    }

    try:
        proc = await asyncio.create_subprocess_exec(
            FREQTRADE_PYTHON, SIM_SCRIPT, json.dumps(cfg),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=SIM_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"error": f"simulasi carry timeout > {SIM_TIMEOUT:.0f}s", "config": cfg}
    except FileNotFoundError as exc:
        return {"error": f"interpreter/skrip simulasi tidak ditemukan: {exc}", "config": cfg}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{exc.__class__.__name__}: {exc}", "config": cfg}

    text = out.decode(errors="replace").strip()
    line = text.splitlines()[-1] if text else ""
    try:
        result = json.loads(line)
    except Exception:
        return {
            "error": "output simulasi bukan JSON",
            "stdout_tail": text.splitlines()[-10:],
            "stderr_tail": err.decode(errors="replace").splitlines()[-10:],
            "config": cfg,
        }
    if proc.returncode != 0 and "error" not in result:
        result["error"] = f"simulasi keluar dengan kode {proc.returncode}"
    result["config"] = cfg
    return result
