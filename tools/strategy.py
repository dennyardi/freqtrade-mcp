"""Tool: create_strategy & validate_strategy."""

import asyncio
import textwrap
from pathlib import Path
from config import FREQTRADE_BIN, USER_DATA_DIR, STRATEGIES_DIR


async def create_strategy(
    name: str,
    description: str,
    timeframe: str = "1h",
) -> dict:
    file_path = Path(STRATEGIES_DIR) / f"{name}.py"
    if file_path.exists():
        return {"error": f"Strategy '{name}' already exists at {file_path}"}

    template = textwrap.dedent(f'''
        # Strategy: {name}
        # Description: {description}

        import talib.abstract as ta
        from pandas import DataFrame
        from freqtrade.strategy import IStrategy, IntParameter


        class {name}(IStrategy):
            """
            {description}
            """

            timeframe = "{timeframe}"
            stoploss = -0.10
            minimal_roi = {{"0": 0.02, "30": 0.01, "60": 0.005}}
            trailing_stop = False

            buy_rsi = IntParameter(20, 40, default=30, space="buy")
            sell_rsi = IntParameter(60, 80, default=70, space="sell")

            def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
                dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
                dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=12)
                dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=26)
                macd = ta.MACD(dataframe["close"])
                dataframe["macd"] = macd[0]
                dataframe["macdsignal"] = macd[1]
                dataframe["macdhist"] = macd[2]
                bb = ta.BBANDS(dataframe, timeperiod=20)
                dataframe["bb_upper"] = bb[0]
                dataframe["bb_mid"] = bb[1]
                dataframe["bb_lower"] = bb[2]
                dataframe["volume_mean"] = dataframe["volume"].rolling(20).mean()
                return dataframe

            def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
                dataframe.loc[
                    (
                        (dataframe["rsi"] < self.buy_rsi.value) &
                        (dataframe["ema_fast"] > dataframe["ema_slow"]) &
                        (dataframe["volume"] > dataframe["volume_mean"])
                    ),
                    "enter_long",
                ] = 1
                return dataframe

            def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
                dataframe.loc[
                    (
                        (dataframe["rsi"] > self.sell_rsi.value) |
                        (dataframe["close"] > dataframe["bb_upper"])
                    ),
                    "exit_long",
                ] = 1
                return dataframe
    ''').strip()

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(template)

    return {
        "success": True,
        "strategy_name": name,
        "file_path": str(file_path),
        "message": f"Strategy template created at {file_path}",
    }


async def validate_strategy(strategy_name: str) -> dict:
    cmd = (
        f"{FREQTRADE_BIN} check-freqtrade-files"
        f" --strategy {strategy_name}"
        f" --userdir {USER_DATA_DIR}"
    )
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode()
    return {
        "strategy_name": strategy_name,
        "valid": proc.returncode == 0,
        "output": output,
    }
