"""Konfigurasi path dan environment Freqtrade MCP Server."""

import os
from pathlib import Path

# Path Freqtrade
FREQTRADE_DIR = Path("/opt/freqtrade")
FREQTRADE_BIN = str(FREQTRADE_DIR / ".venv/bin/freqtrade")

# User data
USER_DATA_DIR  = str(FREQTRADE_DIR / "user_data")
STRATEGIES_DIR = str(FREQTRADE_DIR / "user_data/strategies")
RESULTS_DIR    = Path(FREQTRADE_DIR / "user_data/backtest_results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# Config exchange default (Binance, dry-run)
DEFAULT_EXCHANGE_CONFIG = str(FREQTRADE_DIR / "user_data/config.json")

# MCP Server
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))

# Token untuk Agent Pembuat Strategi (write — semua 12 tools)
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "changeme-please-set-env")

# Token untuk Agent Evaluasi (read-only — 5 tools)
MCP_BEARER_TOKEN_EVAL = os.environ.get("MCP_BEARER_TOKEN_EVAL", "changeme-eval-token")

# Map token → set tools yang diizinkan (None = semua tools)
WRITE_TOOLS = {
    "tool_create_strategy",
    "tool_validate_strategy",
    "tool_write_strategy",
    "tool_delete_strategy",
    "tool_download_data",
    "tool_run_backtest",
    "tool_run_hyperopt",
    "tool_list_jobs",
    "tool_get_job_status",
    "tool_cancel_job",
    "tool_list_strategies",
    "tool_get_backtest_results",
    "tool_get_hyperopt_results",
    "tool_get_performance_summary",
    "tool_compare_strategies",
}

EVAL_TOOLS = {
    "tool_list_jobs",
    "tool_list_strategies",
    "tool_get_backtest_results",
    "tool_get_hyperopt_results",
    "tool_get_performance_summary",
    "tool_compare_strategies",
}

TOKEN_PERMISSIONS: dict[str, set[str]] = {}


def build_token_permissions() -> None:
    """Bangun map token → allowed tools setelah env dibaca."""
    TOKEN_PERMISSIONS[MCP_BEARER_TOKEN]      = WRITE_TOOLS
    TOKEN_PERMISSIONS[MCP_BEARER_TOKEN_EVAL] = EVAL_TOOLS
