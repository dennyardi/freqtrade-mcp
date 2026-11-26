"""
Freqtrade MCP Server — MCP v2.x
Auth: Bearer Token via middleware
Dashboard: username+password login → returns MCP bearer token
"""

import json
import os
from pathlib import Path
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Mount, Route
from starlette.applications import Starlette
from mcp.server.mcpserver import MCPServer

from config import MCP_BEARER_TOKEN
from tools.backtest import run_backtest, get_backtest_results
from tools.data import download_data
from tools.evaluation import get_performance_summary, compare_strategies, list_strategies
from tools.hyperopt import run_hyperopt, get_hyperopt_results
from tools.jobs import get_job_status, cancel_job
from tools.strategy import create_strategy, validate_strategy

BASE_DIR = Path(__file__).parent

DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "freqtrade2026")

# ---------------------------------------------------------------------------
# MCPServer + tools
# ---------------------------------------------------------------------------
mcp = MCPServer("freqtrade-mcp", version="1.0.0")

@mcp.tool()
async def tool_create_strategy(name: str, description: str, timeframe: str = "1h") -> str:
    """Generate a Freqtrade strategy Python file from a natural-language description."""
    return json.dumps(await create_strategy(name=name, description=description, timeframe=timeframe), indent=2)

@mcp.tool()
async def tool_validate_strategy(strategy_name: str) -> str:
    """Validate a strategy file for syntax and Freqtrade compatibility."""
    return json.dumps(await validate_strategy(strategy_name=strategy_name), indent=2)

@mcp.tool()
async def tool_download_data(pairs: list[str], timeframe: str = "1h", days: int = 180) -> str:
    """Download historical OHLCV data from Binance for given pairs and timeframe."""
    return json.dumps(await download_data(pairs=pairs, timeframe=timeframe, days=days), indent=2)

@mcp.tool()
async def tool_run_backtest(
    strategy_name: str,
    pairs: list[str] | None = None,
    timeframe: str = "1h",
    timerange: str | None = None,
) -> str:
    """Run a Freqtrade backtest for a strategy. Returns job_id (async)."""
    return json.dumps(await run_backtest(
        strategy_name=strategy_name, pairs=pairs,
        timeframe=timeframe, timerange=timerange,
    ), indent=2)

@mcp.tool()
async def tool_run_hyperopt(
    strategy_name: str,
    epochs: int = 100,
    spaces: list[str] | None = None,
    timerange: str | None = None,
) -> str:
    """Run Freqtrade hyperopt to optimise strategy parameters. Returns job_id (async)."""
    return json.dumps(await run_hyperopt(
        strategy_name=strategy_name, epochs=epochs,
        spaces=spaces, timerange=timerange,
    ), indent=2)

@mcp.tool()
async def tool_get_job_status(job_id: str) -> str:
    """Poll status and progress of an async backtest or hyperopt job."""
    return json.dumps(await get_job_status(job_id=job_id), indent=2)

@mcp.tool()
async def tool_cancel_job(job_id: str) -> str:
    """Cancel a running backtest or hyperopt job."""
    return json.dumps(await cancel_job(job_id=job_id), indent=2)

@mcp.tool()
async def tool_list_strategies() -> str:
    """List all available strategy files on the server."""
    return json.dumps(await list_strategies(), indent=2)

@mcp.tool()
async def tool_get_backtest_results(job_id: str) -> str:
    """Retrieve and parse results of a completed backtest job."""
    return json.dumps(await get_backtest_results(job_id=job_id), indent=2)

@mcp.tool()
async def tool_get_hyperopt_results(job_id: str) -> str:
    """Retrieve best parameters found by a completed hyperopt job."""
    return json.dumps(await get_hyperopt_results(job_id=job_id), indent=2)

@mcp.tool()
async def tool_get_performance_summary(job_id: str) -> str:
    """Get performance summary: Sharpe, drawdown, win rate, verdict, recommendations."""
    return json.dumps(await get_performance_summary(job_id=job_id), indent=2)

@mcp.tool()
async def tool_compare_strategies(job_ids: list[str]) -> str:
    """Compare performance metrics of multiple completed backtest jobs side by side."""
    return json.dumps(await compare_strategies(job_ids=job_ids), indent=2)

# ---------------------------------------------------------------------------
# Auth middleware
# PUBLIC_PATHS: bisa diakses tanpa token
# ---------------------------------------------------------------------------
PUBLIC_PATHS = {"/health", "/", "/api/login"}

class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != MCP_BEARER_TOKEN:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
async def health(request: Request):
    return JSONResponse({"status": "ok", "server": "freqtrade-mcp"})

async def dashboard(request: Request):
    return HTMLResponse((BASE_DIR / "dashboard.html").read_text())

async def api_login(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    username = body.get("username", "")
    password = body.get("password", "")

    if username == DASHBOARD_USERNAME and password == DASHBOARD_PASSWORD:
        return JSONResponse({"token": MCP_BEARER_TOKEN, "username": username})

    return JSONResponse({"detail": "Username atau password salah"}, status_code=401)

sse_app = mcp.sse_app(host="*")

app = Starlette(
    routes=[
        Route("/health",     health),
        Route("/",           dashboard),
        Route("/api/login",  api_login, methods=["POST"]),
        Mount("/",           app=sse_app),
    ],
)
app.add_middleware(BearerAuthMiddleware)
