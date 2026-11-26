# Strategy: BaselineEma
# Description: Simple EMA cross baseline — dipakai untuk verifikasi pipeline backtest.

from pandas import DataFrame
import pandas_ta as ta
from freqtrade.strategy import IStrategy


class BaselineEma(IStrategy):
    """Simple EMA 12/26 crossover baseline strategy."""

    timeframe     = "1h"
    stoploss      = -0.10
    minimal_roi   = {"0": 0.05, "60": 0.02, "120": 0.01}
    trailing_stop = False

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.ema(dataframe["close"], length=12)
        dataframe["ema_slow"] = ta.ema(dataframe["close"], length=26)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["ema_fast"] > dataframe["ema_slow"]) &
                (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["ema_fast"] < dataframe["ema_slow"])
            ),
            "exit_long",
        ] = 1
        return dataframe
