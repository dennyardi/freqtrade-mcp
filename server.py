"""
Freqtrade MCP Server — MCP v2.x
Auth: Bearer Token via middleware, dengan role-based tool enforcement per token.
  - MCP_BEARER_TOKEN      → Agent Pembuat Strategi (semua 12 tools)
  - MCP_BEARER_TOKEN_EVAL → Agent Evaluasi (5 read-only tools)
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

from config import (
    MCP_BEARER_TOKEN, MCP_BEARER_TOKEN_EVAL,
    TOKEN_PERMISSIONS, build_token_permissions,
)
from tools.jobs import bootstrap
from tools.backtest import run_backtest, get_backtest_results
from tools.data import download_data
from tools.evaluation import get_performance_summary, compare_strategies, list_strategies
from tools.hyperopt import run_hyperopt, get_hyperopt_results
from tools.jobs import get_job_status, cancel_job
from tools.strategy import create_strategy, validate_strategy

BASE_DIR = Path(__file__).parent

DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "freqtrade2026")

# Build token permission map & load jobs dari SQLite
build_token_permissions()
bootstrap()

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
# Auth + tool enforcement middleware
# PUBLIC_PATHS: bisa diakses tanpa token
# ---------------------------------------------------------------------------
PUBLIC_PATHS = {"/health", "/", "/api/login", "/overview", "/strategies", "/jobs", "/results", "/connect"}

class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        token = auth[7:]
        allowed = TOKEN_PERMISSIONS.get(token)
        if allowed is None:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        # Enforce tool-level permission untuk MCP tool calls
        if request.url.path in ("/messages/", "/sse") or request.url.path.startswith("/messages"):
            if request.method == "POST":
                try:
                    body = await request.body()
                    data = json.loads(body)
                    if data.get("method") == "tools/call":
                        tool_name = data.get("params", {}).get("name", "")
                        if tool_name and tool_name not in allowed:
                            return JSONResponse(
                                {"detail": f"Tool '{tool_name}' not allowed for this token"},
                                status_code=403,
                            )
                    # Re-attach body agar bisa dibaca downstream
                    async def receive():
                        return {"type": "http.request", "body": body, "more_body": False}
                    request._receive = receive
                except Exception:
                    pass

        return await call_next(request)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
async def health(request: Request):
    return JSONResponse({"status": "ok", "server": "freqtrade-mcp", "version": "1.0.0"})

SPA_ROUTES = {"/overview", "/strategies", "/jobs", "/results", "/connect"}

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
        return JSONResponse({
            "token":    MCP_BEARER_TOKEN,
            "username": username,
        })

    return JSONResponse({"detail": "Username atau password salah"}, status_code=401)

sse_app = mcp.sse_app(host="*")

app = Starlette(
    routes=[
        Route("/health",    health),
        Route("/",          dashboard),
        Route("/overview",  dashboard),
        Route("/strategies",dashboard),
        Route("/jobs",      dashboard),
        Route("/results",   dashboard),
        Route("/connect",   dashboard),
        Route("/api/login", api_login, methods=["POST"]),
        Mount("/",          app=sse_app),
    ],
)
app.add_middleware(BearerAuthMiddleware)
