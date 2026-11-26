"""Tool: create_strategy & validate_strategy."""

import asyncio
import re
import textwrap
from pathlib import Path
from config import FREQTRADE_BIN, USER_DATA_DIR, STRATEGIES_DIR

# Hanya nama strategy yang aman: huruf, angka, underscore, mulai huruf
_SAFE_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,63}$')


def _validate_name(name: str) -> bool:
    return bool(_SAFE_NAME.match(name))


def _build_template(name: str, description: str, timeframe: str) -> str:
    """
    Bangun template strategy Freqtrade yang valid.
    Template ini menggunakan pandas-ta (tersedia di freqtrade) tanpa TA-Lib,
    sehingga tidak ada KeyError dari BBANDS API yang berbeda-beda.
    """
    desc_lower = description.lower()

    # Tentukan indikator berdasarkan kata kunci dalam deskripsi
    use_bb  = any(w in desc_lower for w in ["bollinger", "bb", "band"])
    use_rsi = "rsi" in desc_lower or not use_bb  # default pakai RSI jika tidak ada BB
    use_ema = any(w in desc_lower for w in ["ema", "moving average", "trend", "cross"])
    use_macd = "macd" in desc_lower

    # Bangun blok populate_indicators
    indicator_lines = []
    entry_conditions = []
    exit_conditions  = []

    if use_rsi:
        indicator_lines.append('        dataframe["rsi"] = ta.RSI(dataframe["close"], timeperiod=14)')
        entry_conditions.append("(dataframe[\"rsi\"] < self.buy_rsi.value)")
        exit_conditions.append("(dataframe[\"rsi\"] > self.sell_rsi.value)")

    if use_ema:
        indicator_lines.append('        dataframe["ema_fast"] = ta.EMA(dataframe["close"], timeperiod=12)')
        indicator_lines.append('        dataframe["ema_slow"] = ta.EMA(dataframe["close"], timeperiod=26)')
        entry_conditions.append("(dataframe[\"ema_fast\"] > dataframe[\"ema_slow\"])")
        exit_conditions.append("(dataframe[\"ema_fast\"] < dataframe[\"ema_slow\"])")

    if use_macd:
        indicator_lines += [
            '        macd = ta.MACD(dataframe["close"])',
            '        dataframe["macd"]       = macd["MACD"]',
            '        dataframe["macdsignal"] = macd["MACDs"]',
            '        dataframe["macdhist"]   = macd["MACDh"]',
        ]
        entry_conditions.append("(dataframe[\"macd\"] > dataframe[\"macdsignal\"])")
        exit_conditions.append("(dataframe[\"macd\"] < dataframe[\"macdsignal\"])")

    if use_bb:
        indicator_lines += [
            '        bb = ta.BBANDS(dataframe["close"], timeperiod=20)',
            '        dataframe["bb_upper"] = bb["BBU_20_2.0"]',
            '        dataframe["bb_mid"]   = bb["BBM_20_2.0"]',
            '        dataframe["bb_lower"] = bb["BBL_20_2.0"]',
        ]
        entry_conditions.append("(dataframe[\"close\"] < dataframe[\"bb_lower\"])")
        exit_conditions.append("(dataframe[\"close\"] > dataframe[\"bb_upper\"])")

    # Volume filter — selalu tambahkan
    indicator_lines.append('        dataframe["volume_mean"] = dataframe["volume"].rolling(20).mean()')
    entry_conditions.append("(dataframe[\"volume\"] > 0)")

    # Fallback jika tidak ada kondisi
    if not entry_conditions:
        entry_conditions = ["(dataframe[\"close\"] > 0)"]
    if not exit_conditions:
        exit_conditions = ["(dataframe[\"close\"] < 0)"]

    # Import lines
    imports = [
        "import pandas_ta as ta",
        "from pandas import DataFrame",
        "from freqtrade.strategy import IStrategy, IntParameter",
    ]
    if use_rsi:
        imports_extra = []
    else:
        imports_extra = []

    indicators_block = "\n".join(indicator_lines) if indicator_lines else '        pass'
    entry_block = " &\n                ".join(entry_conditions)
    exit_block  = " |\n                ".join(exit_conditions)

    # Hanya tambahkan parameter RSI jika digunakan
    param_block = ""
    if use_rsi:
        param_block = """
    buy_rsi  = IntParameter(20, 40, default=30, space="buy")
    sell_rsi = IntParameter(60, 80, default=70, space="sell")
"""

    return textwrap.dedent(f'''
        # Strategy: {name}
        # Description: {description}

        from pandas import DataFrame
        import pandas_ta as ta
        from freqtrade.strategy import IStrategy, IntParameter


        class {name}(IStrategy):
            """
            {description}
            """

            timeframe    = "{timeframe}"
            stoploss     = -0.10
            minimal_roi  = {{"0": 0.05, "60": 0.02, "120": 0.01}}
            trailing_stop = False
            {param_block}

            def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        {indicators_block}
                return dataframe

            def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
                dataframe.loc[
                    (
                        {entry_block}
                    ),
                    "enter_long",
                ] = 1
                return dataframe

            def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
                dataframe.loc[
                    (
                        {exit_block}
                    ),
                    "exit_long",
                ] = 1
                return dataframe
    ''').strip()


async def create_strategy(
    name: str,
    description: str,
    timeframe: str = "1h",
) -> dict:
    if not _validate_name(name):
        return {"error": f"Strategy name '{name}' tidak valid. Gunakan huruf, angka, underscore (mulai huruf)."}

    file_path = Path(STRATEGIES_DIR) / f"{name}.py"
    if file_path.exists():
        return {"error": f"Strategy '{name}' already exists at {file_path}"}

    template = _build_template(name, description, timeframe)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(template)

    return {
        "success": True,
        "strategy_name": name,
        "file_path": str(file_path),
        "message": f"Strategy '{name}' created. Run tool_validate_strategy to check before backtesting.",
    }


async def validate_strategy(strategy_name: str) -> dict:
    """
    Validasi strategy dengan 'freqtrade list-strategies' dan cek nama strategy ada.
    'check-freqtrade-files' tidak valid — gunakan list-strategies --no-color.
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

    # Strategy valid jika namanya muncul di output list-strategies
    found = strategy_name in output

    return {
        "strategy_name": strategy_name,
        "valid": found and proc.returncode == 0,
        "output": output[-2000:],
    }


async def fix_sample_strategy() -> dict:
    """Hapus sample_strategy.py yang rusak agar tidak mengganggu."""
    sample_path = Path(STRATEGIES_DIR) / "sample_strategy.py"
    if sample_path.exists():
        sample_path.unlink()
        return {"removed": str(sample_path)}
    return {"message": "sample_strategy.py tidak ditemukan"}
