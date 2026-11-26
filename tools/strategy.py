"""Tool: create_strategy & validate_strategy."""

import asyncio
import re
import subprocess
from pathlib import Path
from config import FREQTRADE_BIN, USER_DATA_DIR, STRATEGIES_DIR

_SAFE_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,63}$')

# Validasi timeframe yang diizinkan
_VALID_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"}


def _validate_name(name: str) -> bool:
    return bool(_SAFE_NAME.match(name))


def _build_template(name: str, description: str, timeframe: str) -> str:
    """
    Hasilkan file .py strategy Freqtrade yang syntactically valid.
    Indikator dipilih berdasarkan kata kunci dalam deskripsi.
    Menggunakan pandas_ta (bundled dengan freqtrade).
    """
    desc_lower = description.lower()

    use_rsi  = "rsi"  in desc_lower
    use_ema  = any(w in desc_lower for w in ("ema", "moving average", "trend", "cross", "golden"))
    use_macd = "macd" in desc_lower
    use_bb   = any(w in desc_lower for w in ("bollinger", " bb ", "band"))

    # Default: RSI jika tidak ada kata kunci spesifik
    if not any([use_rsi, use_ema, use_macd, use_bb]):
        use_rsi = True

    # --- blok indikator (8 spasi = 2 level indent di dalam method) ---
    ind = []
    if use_rsi:
        ind.append('        dataframe["rsi"] = ta.rsi(dataframe["close"], length=14)')
    if use_ema:
        ind.append('        dataframe["ema_fast"] = ta.ema(dataframe["close"], length=12)')
        ind.append('        dataframe["ema_slow"] = ta.ema(dataframe["close"], length=26)')
    if use_macd:
        ind.append('        macd = ta.macd(dataframe["close"])')
        ind.append('        dataframe["macd"]      = macd["MACD_12_26_9"]')
        ind.append('        dataframe["macdsgn"]   = macd["MACDs_12_26_9"]')
    if use_bb:
        ind.append('        bb = ta.bbands(dataframe["close"], length=20)')
        ind.append('        dataframe["bb_upper"] = bb["BBU_20_2.0"]')
        ind.append('        dataframe["bb_mid"]   = bb["BBM_20_2.0"]')
        ind.append('        dataframe["bb_lower"] = bb["BBL_20_2.0"]')
    ind_block = "\n".join(ind)

    # --- kondisi entry ---
    entry = []
    if use_rsi:
        entry.append('            (dataframe["rsi"] < self.buy_rsi.value)')
    if use_ema:
        entry.append('            (dataframe["ema_fast"] > dataframe["ema_slow"])')
    if use_macd:
        entry.append('            (dataframe["macd"] > dataframe["macdsgn"])')
    if use_bb:
        entry.append('            (dataframe["close"] < dataframe["bb_lower"])')
    entry.append('            (dataframe["volume"] > 0)')
    entry_block = " &\n".join(entry)

    # --- kondisi exit ---
    ex = []
    if use_rsi:
        ex.append('            (dataframe["rsi"] > self.sell_rsi.value)')
    if use_ema:
        ex.append('            (dataframe["ema_fast"] < dataframe["ema_slow"])')
    if use_macd:
        ex.append('            (dataframe["macd"] < dataframe["macdsgn"])')
    if use_bb:
        ex.append('            (dataframe["close"] > dataframe["bb_upper"])')
    if not ex:
        ex.append('            (dataframe["close"] < dataframe["close"].shift(1))')
    exit_block = " |\n".join(ex)

    # Parameter RSI hanya jika dipakai
    rsi_params = ""
    if use_rsi:
        rsi_params = (
            '\n    buy_rsi  = IntParameter(20, 40, default=30, space="buy")'
            '\n    sell_rsi = IntParameter(60, 80, default=70, space="sell")'
        )

    # Tulis template sebagai raw string — tidak ada textwrap.dedent
    # Sehingga indentasi persis seperti yang ditulis di sini
    lines = [
        f"# Strategy: {name}",
        f"# Description: {description}",
        "",
        "from pandas import DataFrame",
        "import pandas_ta as ta",
        "from freqtrade.strategy import IStrategy, IntParameter",
        "",
        "",
        f"class {name}(IStrategy):",
        f'    """{description}"""',
        "",
        f'    timeframe    = "{timeframe}"',
        "    stoploss     = -0.10",
        '    minimal_roi  = {"0": 0.05, "60": 0.02, "120": 0.01}',
        "    trailing_stop = False",
    ]

    if use_rsi:
        lines += [
            '    buy_rsi  = IntParameter(20, 40, default=30, space="buy")',
            '    sell_rsi = IntParameter(60, 80, default=70, space="sell")',
        ]

    lines += [
        "",
        "    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:",
    ]
    for ind_line in ind:
        lines.append(ind_line)
    lines += [
        "        return dataframe",
        "",
        "    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:",
        "        dataframe.loc[",
        "            (",
    ]
    # Tulis kondisi entry (sudah termasuk volume > 0 dari list entry)
    all_entry = entry + ['            (dataframe["volume"] > 0)']
    for i, cond in enumerate(all_entry):
        sep = " &" if i < len(all_entry) - 1 else ""
        lines.append(f"            {cond.strip()}{sep}")
    lines += [
        "            ),",
        '            "enter_long",',
        "        ] = 1",
        "        return dataframe",
        "",
        "    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:",
        "        dataframe.loc[",
        "            (",
    ]
    for i, cond in enumerate(ex):
        sep = " |" if i < len(ex) - 1 else ""
        lines.append(f"            {cond.strip()}{sep}")
    lines += [
        "            ),",
        '            "exit_long",',
        "        ] = 1",
        "        return dataframe",
    ]

    return "\n".join(lines) + "\n"


async def create_strategy(
    name: str,
    description: str,
    timeframe: str = "1h",
) -> dict:
    if not _validate_name(name):
        return {"error": f"Strategy name '{name}' tidak valid. Gunakan huruf, angka, underscore (mulai huruf)."}
    if timeframe not in _VALID_TIMEFRAMES:
        return {"error": f"Timeframe tidak valid: {timeframe}"}

    file_path = Path(STRATEGIES_DIR) / f"{name}.py"
    if file_path.exists():
        return {"error": f"Strategy '{name}' already exists. Gunakan nama lain atau hapus dulu."}

    code = _build_template(name, description, timeframe)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(code)

    # Verifikasi syntax Python sebelum return sukses
    # Pakai python3 dari venv freqtrade (punya semua dependency)
    python_bin = Path(FREQTRADE_BIN).parent / "python3"
    syntax_proc = await asyncio.create_subprocess_exec(
        str(python_bin), "-m", "py_compile", str(file_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, syn_err = await syntax_proc.communicate()

    if syntax_proc.returncode != 0:
        file_path.unlink(missing_ok=True)
        return {
            "success": False,
            "error": f"Syntax error dalam template yang digenerate: {syn_err.decode()[:500]}",
        }

    return {
        "success": True,
        "strategy_name": name,
        "file_path": str(file_path),
        "message": f"Strategy '{name}' created. Jalankan tool_validate_strategy sebelum backtest.",
    }


async def validate_strategy(strategy_name: str) -> dict:
    """
    Validasi strategy dengan 'freqtrade list-strategies'.
    Valid = nama muncul di baris tabel dengan status 'OK' (bukan 'FAILED').
    """
    if not _validate_name(strategy_name):
        return {"error": f"Strategy name '{strategy_name}' tidak valid."}

    proc = await asyncio.create_subprocess_exec(
        FREQTRADE_BIN, "list-strategies",
        "--userdir", USER_DATA_DIR,
        "--no-color",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode() + stderr.decode()

    # Parse tabel output — cari baris yang mengandung nama strategy
    # Format: | StrategyName       | 1234abc | OK     |
    #      atau: | StrategyName  | FAILED   |
    valid = False
    for line in output.splitlines():
        if strategy_name in line:
            # Baris valid hanya jika ada "OK" dan tidak ada "FAILED" / "No load"
            if "FAILED" in line or "No load" in line or "not load" in line.lower():
                valid = False
            elif "| OK" in line or "OK |" in line or "|OK|" in line or " OK " in line:
                valid = True
            break

    return {
        "strategy_name": strategy_name,
        "valid": valid and proc.returncode == 0,
        "output": output[-2000:],
    }
