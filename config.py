"""Konfigurasi path dan environment Freqtrade MCP Server."""

import os
from pathlib import Path

# Path Freqtrade
FREQTRADE_DIR = Path("/opt/freqtrade")
FREQTRADE_BIN = str(FREQTRADE_DIR / ".venv/bin/freqtrade")

# User data
USER_DATA_DIR = str(FREQTRADE_DIR / "user_data")
STRATEGIES_DIR = str(FREQTRADE_DIR / "user_data/strategies")
RESULTS_DIR = Path(FREQTRADE_DIR / "user_data/backtest_results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# Config exchange default (Binance, dry-run)
DEFAULT_EXCHANGE_CONFIG = str(FREQTRADE_DIR / "user_data/config.json")

# MCP Server
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "changeme-please-set-env")
