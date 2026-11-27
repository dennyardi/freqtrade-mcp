"""
Freqtrade MCP Server — MCP v2.x
Auth: Bearer Token via middleware, dengan role-based tool enforcement per token.
  - MCP_BEARER_TOKEN      → Agent Pembuat Strategi (semua 12 tools)
  - MCP_BEARER_TOKEN_EVAL → Agent Evaluasi (5 read-only tools)
Dashboard: username+password login → returns session token (bukan MCP token langsung)
"""

import hashlib
import hmac
from contextlib import asynccontextmanager
import json
import logging
import os
import time
import uuid
from collections import defaultdict
from contextvars import ContextVar
from pathlib import Path
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Mount, Route
from starlette.applications import Starlette
from mcp.server.mcpserver import MCPServer

from config import (
    MCP_BEARER_TOKEN,
    TOKEN_PERMISSIONS, build_token_permissions,
)
from tools.jobs import bootstrap
from tools.backtest import run_backtest, get_backtest_results
from tools.data import download_data
from tools.evaluation import get_performance_summary, compare_strategies, list_strategies
from tools.hyperopt import run_hyperopt, get_hyperopt_results
from tools.jobs import get_job_status, cancel_job, list_jobs
from tools.strategy import create_strategy, validate_strategy, write_strategy, delete_strategy

logger = logging.getLogger("freqtrade-mcp")

BASE_DIR = Path(__file__).parent

DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

if not DASHBOARD_USERNAME or not DASHBOARD_PASSWORD:
    raise RuntimeError("DASHBOARD_USERNAME dan DASHBOARD_PASSWORD harus di-set di .env")

# ContextVar: menyimpan set tool yang diizinkan untuk request SSE yang sedang berjalan
# Diset oleh middleware, dibaca oleh tool handler
_current_allowed_tools: ContextVar[set | None] = ContextVar("_current_allowed_tools", default=None)

# Build token permission map & load jobs dari SQLite
try:
    build_token_permissions()
    bootstrap()
except Exception as e:
    raise RuntimeError(f"Startup gagal: {e}") from e

# ---------------------------------------------------------------------------
# Patch freqtrade config.json — hapus api_server block yang menyebabkan
# "jwt_secret_key is too short" error meski api_server.enabled = false
# ---------------------------------------------------------------------------
def _patch_freqtrade_config() -> None:
    from config import DEFAULT_EXCHANGE_CONFIG
    import json as _json
    cfg_path = Path(DEFAULT_EXCHANGE_CONFIG)
    if not cfg_path.exists():
        return
    try:
        cfg = _json.loads(cfg_path.read_text())
        if "api_server" in cfg:
            del cfg["api_server"]
            cfg_path.write_text(_json.dumps(cfg, indent=2))
            logger.info("Patched freqtrade config: api_server block removed")
    except Exception as e:
        logger.warning("Could not patch freqtrade config: %s", e)

_patch_freqtrade_config()

# ---------------------------------------------------------------------------
# Cleanup: hapus sample_strategy.py yang rusak ("class does not exist")
# ---------------------------------------------------------------------------
def _remove_broken_sample_strategy() -> None:
    from config import STRATEGIES_DIR
    sample = Path(STRATEGIES_DIR) / "sample_strategy.py"
    if sample.exists():
        try:
            sample.unlink()
            logger.info("Removed broken sample_strategy.py")
        except Exception as e:
            logger.warning("Could not remove sample_strategy.py: %s", e)

_remove_broken_sample_strategy()

# ---------------------------------------------------------------------------
# Pastikan BaselineEma tersedia dan hapus strategi lama yang rusak
# ---------------------------------------------------------------------------
def _ensure_baseline_strategy() -> None:
    from config import STRATEGIES_DIR
    src  = BASE_DIR / "BaselineEma.py"
    dest = Path(STRATEGIES_DIR) / "BaselineEma.py"
    if src.exists() and not dest.exists():
        try:
            import shutil
            shutil.copy2(str(src), str(dest))
            logger.info("Copied BaselineEma.py to strategies dir")
        except Exception as e:
            logger.warning("Could not copy BaselineEma.py: %s", e)
    for broken in ("conn_test_emacross.py", "mgr_smoke_ema.py",
                   "mgr_test_ema2.py", "mgr_t_macd.py", "mgr_t_rsi.py",
                   "rs_test_ema.py", "TestNew.py", "RsTest2.py",
                   "GenHold.py", "GenRare.py", "GenRare30.py", "GenMacd.py",
                   "DupTest.py", "EmptyDesc.py"):
        p = Path(STRATEGIES_DIR) / broken
        if p.exists():
            try:
                p.unlink()
                logger.info("Removed outdated strategy: %s", broken)
            except Exception:
                pass

_ensure_baseline_strategy()

# ---------------------------------------------------------------------------
# Dashboard HTML cache (baca sekali saat startup)
# ---------------------------------------------------------------------------
_DASHBOARD_HTML = (BASE_DIR / "dashboard.html").read_text()

# ---------------------------------------------------------------------------
# Session store untuk dashboard (token sementara, bukan MCP token)
# Format: { session_token: { "username": ..., "mcp_token": ..., "created_at": ... } }
# Session berlaku 8 jam
# ---------------------------------------------------------------------------
_SESSIONS: dict[str, dict] = {}
SESSION_TTL = 8 * 3600  # detik

def _create_session(username: str) -> str:
    token = uuid.uuid4().hex
    _SESSIONS[token] = {
        "username": username,
        "mcp_token": MCP_BEARER_TOKEN,
        "created_at": time.time(),
    }
    return token

def _get_session(token: str) -> dict | None:
    s = _SESSIONS.get(token)
    if not s:
        return None
    if time.time() - s["created_at"] > SESSION_TTL:
        del _SESSIONS[token]
        return None
    return s

# ---------------------------------------------------------------------------
# Rate limiting untuk /api/login (max 5 attempt per IP per 60 detik)
# ---------------------------------------------------------------------------
_login_attempts: dict[str, list[float]] = defaultdict(list)
LOGIN_WINDOW = 60   # detik
LOGIN_MAX    = 5

def _check_rate_limit(ip: str) -> bool:
    """Return True jika request diizinkan, False jika rate limit tercapai."""
    now = time.time()
    attempts = _login_attempts[ip]
    # Buang attempt yang sudah expired
    _login_attempts[ip] = [t for t in attempts if now - t < LOGIN_WINDOW]
    if len(_login_attempts[ip]) >= LOGIN_MAX:
        return False
    _login_attempts[ip].append(now)
    return True

# ---------------------------------------------------------------------------
# Request body size limit (1 MB)
# ---------------------------------------------------------------------------
MAX_BODY_SIZE = 1 * 1024 * 1024  # 1 MB

# ---------------------------------------------------------------------------
# MCPServer + tools
# ---------------------------------------------------------------------------
mcp = MCPServer("freqtrade-mcp", version="1.0.0")


def _check_tool_permission(tool_name: str) -> str | None:
    """
    Cek apakah tool_name diizinkan untuk token yang sedang aktif.
    Return None jika diizinkan, atau pesan error jika ditolak.
    Hanya berlaku saat dipanggil via SSE (ContextVar di-set oleh middleware).
    """
    allowed = _current_allowed_tools.get()
    if allowed is None:
        # Tidak ada context (misal: internal call) — izinkan
        return None
    if tool_name not in allowed:
        return f"Tool '{tool_name}' not allowed for this token"
    return None

@mcp.tool()
async def tool_create_strategy(name: str, description: str, timeframe: str = "1h") -> str:
    """Generate a Freqtrade strategy Python file from a natural-language description."""
    if err := _check_tool_permission("tool_create_strategy"):
        return json.dumps({"error": err})
    return json.dumps(await create_strategy(name=name, description=description, timeframe=timeframe), indent=2)

@mcp.tool()
async def tool_validate_strategy(strategy_name: str) -> str:
    """Validate a strategy file for syntax and Freqtrade compatibility."""
    if err := _check_tool_permission("tool_validate_strategy"):
        return json.dumps({"error": err})
    return json.dumps(await validate_strategy(strategy_name=strategy_name), indent=2)

@mcp.tool()
async def tool_write_strategy(name: str, code: str) -> str:
    """Write or overwrite a strategy file with provided Python code. Code must implement IStrategy and pass syntax check."""
    if err := _check_tool_permission("tool_write_strategy"):
        return json.dumps({"error": err})
    return json.dumps(await write_strategy(name=name, code=code), indent=2)

@mcp.tool()
async def tool_delete_strategy(strategy_name: str) -> str:
    """Delete a strategy file from disk. BaselineEma is protected and cannot be deleted."""
    if err := _check_tool_permission("tool_delete_strategy"):
        return json.dumps({"error": err})
    return json.dumps(await delete_strategy(strategy_name=strategy_name), indent=2)

@mcp.tool()
async def tool_download_data(
    pairs: list[str],
    timeframe: str = "1h",
    days: int | None = None,
    timerange: str | None = None,
    full: bool = False,
    trading_mode: str = "spot",
) -> str:
    """Download historical OHLCV data from Binance. Use timerange (YYYYMMDD-YYYYMMDD) for precision or days (1-N) for relative lookback. When the requested start is earlier than local data, the server adds --prepend automatically. full=true re-downloads with --erase.

    trading_mode: "spot" (default) or "futures". Futures requires pairs in BASE/QUOTE:SETTLE form (e.g. BTC/USDT:USDT) and stores data under user_data/data/binance/futures/ (OHLCV + mark price + funding rate).
    """
    if err := _check_tool_permission("tool_download_data"):
        return json.dumps({"error": err})
    return json.dumps(await download_data(pairs=pairs, timeframe=timeframe, days=days,
                                          timerange=timerange, full=full,
                                          trading_mode=trading_mode), indent=2)

@mcp.tool()
async def tool_run_backtest(
    strategy_name: str,
    pairs: list[str] | None = None,
    timeframe: str = "1h",
    timerange: str | None = None,
    fee: float = 0.001,
    trading_mode: str = "spot",
    leverage: float = 1.0,
) -> str:
    """Run a Freqtrade backtest for a strategy. Returns job_id (async).

    fee: biaya per sisi sebagai rasio, 0-0.01, default 0.001 (0.1%). Override ini
    hanya berlaku untuk run tersebut (flag --fee freqtrade), config global tidak diubah.

    trading_mode: "spot" (default) atau "futures". Mode futures memakai config
    sementara (trading_mode=futures, margin_mode=isolated) — config global tidak diubah —
    dan pair wajib berformat BASE/QUOTE:SETTLE (contoh BTC/USDT:USDT).

    leverage: 1-10, default 1. Hanya untuk trading_mode="futures"; strategi dengan
    can_short=True bisa membuka short. Leverage diimplementasikan lewat wrapper
    strategy sementara yang dihapus setelah job selesai.
    """
    if err := _check_tool_permission("tool_run_backtest"):
        return json.dumps({"error": err})
    return json.dumps(await run_backtest(
        strategy_name=strategy_name, pairs=pairs,
        timeframe=timeframe, timerange=timerange, fee=fee,
        trading_mode=trading_mode, leverage=leverage,
    ), indent=2)

@mcp.tool()
async def tool_run_hyperopt(
    strategy_name: str,
    epochs: int = 100,
    spaces: list[str] | None = None,
    timerange: str | None = None,
    pairs: list[str] | None = None,
    fee: float = 0.001,
) -> str:
    """Run Freqtrade hyperopt to optimise strategy parameters. Returns job_id (async).

    fee: biaya per sisi sebagai rasio, 0-0.01, default 0.001 (0.1%) — flag --fee,
    config global tidak diubah.
    """
    if err := _check_tool_permission("tool_run_hyperopt"):
        return json.dumps({"error": err})
    return json.dumps(await run_hyperopt(
        strategy_name=strategy_name, epochs=epochs,
        spaces=spaces, timerange=timerange, pairs=pairs, fee=fee,
    ), indent=2)

@mcp.tool()
async def tool_list_jobs(limit: int = 200) -> str:
    """List all known jobs (backtest & hyperopt) from this server session, newest first."""
    if err := _check_tool_permission("tool_list_jobs"):
        return json.dumps({"error": err})
    return json.dumps(await list_jobs(limit=limit), indent=2)

@mcp.tool()
async def tool_get_job_status(job_id: str) -> str:
    """Poll status and progress of an async backtest or hyperopt job."""
    if err := _check_tool_permission("tool_get_job_status"):
        return json.dumps({"error": err})
    return json.dumps(await get_job_status(job_id=job_id), indent=2)

@mcp.tool()
async def tool_cancel_job(job_id: str) -> str:
    """Cancel a running backtest or hyperopt job."""
    if err := _check_tool_permission("tool_cancel_job"):
        return json.dumps({"error": err})
    return json.dumps(await cancel_job(job_id=job_id), indent=2)

@mcp.tool()
async def tool_list_strategies() -> str:
    """List all available strategy files on the server."""
    if err := _check_tool_permission("tool_list_strategies"):
        return json.dumps({"error": err})
    return json.dumps(await list_strategies(), indent=2)

@mcp.tool()
async def tool_get_backtest_results(job_id: str) -> str:
    """Retrieve and parse results of a completed backtest job."""
    if err := _check_tool_permission("tool_get_backtest_results"):
        return json.dumps({"error": err})
    return json.dumps(await get_backtest_results(job_id=job_id), indent=2)

@mcp.tool()
async def tool_get_hyperopt_results(job_id: str) -> str:
    """Retrieve best parameters found by a completed hyperopt job."""
    if err := _check_tool_permission("tool_get_hyperopt_results"):
        return json.dumps({"error": err})
    return json.dumps(await get_hyperopt_results(job_id=job_id), indent=2)

@mcp.tool()
async def tool_get_performance_summary(job_id: str) -> str:
    """Get performance summary: Sharpe, drawdown, win rate, verdict, recommendations."""
    if err := _check_tool_permission("tool_get_performance_summary"):
        return json.dumps({"error": err})
    return json.dumps(await get_performance_summary(job_id=job_id), indent=2)

@mcp.tool()
async def tool_compare_strategies(job_ids: list[str]) -> str:
    """Compare performance metrics of multiple completed backtest jobs side by side."""
    if err := _check_tool_permission("tool_compare_strategies"):
        return json.dumps({"error": err})
    return json.dumps(await compare_strategies(job_ids=job_ids), indent=2)

# ---------------------------------------------------------------------------
# Auth middleware (pure ASGI — tidak pakai BaseHTTPMiddleware)
# PUBLIC_PATHS: dapat diakses tanpa token
# ---------------------------------------------------------------------------
PUBLIC_PATHS = {"/health", "/", "/api/login", "/overview", "/strategies", "/jobs", "/results", "/connect"}

# Security headers yang dikirim ke semua response
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]


class BearerAuthMiddleware:
    """
    Pure ASGI middleware:
    - Bearer token enforcement untuk semua path non-public
    - Tool-level permission enforcement di /api/tools dan /messages/
    - Stateful replay_receive untuk SSE-compatible downstream
    - Security headers di setiap response
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return

        # Inject security headers ke setiap response
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # Tambah security headers jika belum ada
                existing = {k.lower() for k, _ in headers}
                for k, v in SECURITY_HEADERS:
                    if k not in existing:
                        headers.append((k, v))
                message = {**message, "headers": headers}
            await send(message)

        if scope["type"] != "http":
            await self.app(scope, receive, send_with_headers)
            return

        path = scope.get("path", "")

        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send_with_headers)
            return

        # Ambil Authorization header
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode()

        if not auth.startswith("Bearer "):
            await self._reject(send_with_headers, 401, "Unauthorized")
            return

        raw_token = auth[7:]

        # Cek apakah ini session token dashboard atau MCP bearer token
        session = _get_session(raw_token)
        if session:
            # Resolve ke MCP token yang sebenarnya
            mcp_token = session["mcp_token"]
        else:
            mcp_token = raw_token

        allowed = TOKEN_PERMISSIONS.get(mcp_token)
        if allowed is None:
            logger.warning("Auth failed: unknown token from %s", scope.get("client"))
            await self._reject(send_with_headers, 401, "Unauthorized")
            return

        # SSE: hanya cek token, jangan consume body (stream)
        if path == "/sse":
            # Set ContextVar supaya tool handlers tahu token mana yang aktif di sesi ini
            allowed_set = TOKEN_PERMISSIONS.get(mcp_token, set())
            ctx_token = _current_allowed_tools.set(allowed_set)
            try:
                await self.app(scope, receive, send_with_headers)
            finally:
                _current_allowed_tools.reset(ctx_token)
            return

        # Endpoints yang perlu tool-level enforcement
        needs_enforcement = (
            path == "/api/tools" or path.startswith("/messages")
        )

        if needs_enforcement and scope.get("method", "GET") == "POST":
            # Baca body dengan size limit
            body_chunks = []
            total_size = 0
            more_body = True
            while more_body:
                msg = await receive()
                chunk = msg.get("body", b"")
                total_size += len(chunk)
                if total_size > MAX_BODY_SIZE:
                    await self._reject(send_with_headers, 413, "Payload too large")
                    return
                body_chunks.append(chunk)
                more_body = msg.get("more_body", False)
            body = b"".join(body_chunks)

            if path == "/api/tools":
                # REST endpoint: enforce di HTTP level (403 aman, tidak ada SSE)
                try:
                    data = json.loads(body)
                    tool_name = data.get("tool") or ""
                    if tool_name and tool_name not in (TOKEN_PERMISSIONS.get(mcp_token) or set()):
                        logger.warning("Tool denied: %s not allowed for token (REST)", tool_name)
                        await self._reject(send_with_headers, 403, f"Tool '{tool_name}' not allowed for this token")
                        return
                except Exception:
                    pass
            else:
                # /messages/ via SSE: JANGAN tolak di HTTP level — itu memutus sesi SSE.
                # Set ContextVar; tool handler yang return error ke stream SSE.
                allowed_set = TOKEN_PERMISSIONS.get(mcp_token, set())
                _current_allowed_tools.set(allowed_set)

            # Stateful replay_receive: panggil pertama → body, berikutnya → disconnect
            replay_sent = False

            async def replay_receive():
                nonlocal replay_sent
                if not replay_sent:
                    replay_sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}

            await self.app(scope, replay_receive, send_with_headers)
            return

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _reject(send, status: int, detail: str):
        body = json.dumps({"detail": detail}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ]})
        await send({"type": "http.response.body", "body": body, "more_body": False})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

async def health(request: Request):
    """Health check — cek binary freqtrade + DB."""
    from config import FREQTRADE_BIN, STRATEGIES_DIR
    checks = {}
    # Cek binary freqtrade
    checks["freqtrade_bin"] = Path(FREQTRADE_BIN).exists()
    # Cek strategies dir
    checks["strategies_dir"] = Path(STRATEGIES_DIR).is_dir()
    # Cek SQLite DB
    try:
        from tools.db import _conn
        with _conn() as con:
            con.execute("SELECT 1")
        checks["db"] = True
    except Exception:
        checks["db"] = False

    all_ok = all(checks.values())
    return JSONResponse(
        {"status": "ok" if all_ok else "degraded", "checks": checks},
        status_code=200 if all_ok else 503,
    )


async def dashboard(request: Request):
    return HTMLResponse(_DASHBOARD_HTML, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
        "Content-Security-Policy": (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "frame-ancestors 'none';"
        ),
    })


# Mapping tool_name → fungsi async beserta allowed arguments (whitelist)
_TOOL_MAP = {
    "tool_list_strategies":         (list_strategies,          []),
    "tool_list_jobs":               (list_jobs,                ["limit"]),
    "tool_get_job_status":          (get_job_status,           ["job_id"]),
    "tool_cancel_job":              (cancel_job,               ["job_id"]),
    "tool_get_backtest_results":    (get_backtest_results,     ["job_id"]),
    "tool_get_hyperopt_results":    (get_hyperopt_results,     ["job_id"]),
    "tool_get_performance_summary": (get_performance_summary,  ["job_id"]),
    "tool_compare_strategies":      (compare_strategies,       ["job_ids"]),
    "tool_run_backtest":            (run_backtest,             ["strategy_name", "pairs", "timeframe", "timerange", "fee", "trading_mode", "leverage"]),
    "tool_run_hyperopt":            (run_hyperopt,             ["strategy_name", "epochs", "spaces", "timerange", "pairs", "fee"]),
    "tool_download_data":           (download_data,            ["pairs", "timeframe", "days", "timerange", "full", "trading_mode"]),
    "tool_create_strategy":         (create_strategy,          ["name", "description", "timeframe"]),
    "tool_validate_strategy":       (validate_strategy,        ["strategy_name"]),
    "tool_write_strategy":          (write_strategy,           ["name", "code"]),
    "tool_delete_strategy":         (delete_strategy,          ["strategy_name"]),
}


async def api_tools(request: Request):
    """REST endpoint untuk dashboard — panggil tool functions langsung."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    tool_name = body.get("tool")
    raw_args  = body.get("args", {})

    entry = _TOOL_MAP.get(tool_name)
    if not entry:
        return JSONResponse({"error": f"Unknown tool: {tool_name}"}, status_code=404)

    fn, allowed_params = entry

    # Whitelist args — buang key yang tidak dikenali
    args = {k: v for k, v in raw_args.items() if k in allowed_params}

    try:
        result = await fn(**args)
        return JSONResponse({"ok": True, "result": result})
    except TypeError as e:
        logger.error("Tool %s arg error: %s", tool_name, e)
        return JSONResponse({"error": "Invalid arguments"}, status_code=400)
    except Exception as e:
        logger.exception("Tool %s execution error", tool_name)
        return JSONResponse({"ok": False, "error": "Tool execution failed"}, status_code=500)


async def api_login(request: Request):
    # Rate limiting per IP
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        logger.warning("Rate limit hit on /api/login from %s", client_ip)
        return JSONResponse({"detail": "Too many attempts, coba lagi dalam 1 menit"}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    username = body.get("username", "")
    password = body.get("password", "")

    # Constant-time comparison — cegah timing attack
    user_match = hmac.compare_digest(username.encode(), DASHBOARD_USERNAME.encode())
    pass_match = hmac.compare_digest(password.encode(), DASHBOARD_PASSWORD.encode())

    if user_match and pass_match:
        # Buat session token sementara (bukan MCP token langsung)
        session_token = _create_session(username)
        logger.info("Login success: %s from %s", username, client_ip)
        return JSONResponse({
            "token":    session_token,
            "username": username,
        })

    logger.warning("Login failed: %s from %s", username, client_ip)
    return JSONResponse({"detail": "Username atau password salah"}, status_code=401)


sse_app = mcp.sse_app(host="*")
# Streamable HTTP (/mcp). Mount() tidak menjalankan lifespan sub-app, jadi
# session manager streamable dijalankan dari lifespan _app di bawah.
streamable_app = mcp.streamable_http_app(streamable_http_path="/mcp", host="*")


@asynccontextmanager
async def _lifespan(_: "Starlette"):
    async with streamable_app.router.lifespan_context(streamable_app):
        yield

_app = Starlette(
    lifespan=_lifespan,
    routes=[
        Route("/health",        health),
        Route("/",              dashboard),
        Route("/overview",      dashboard),
        Route("/strategies",    dashboard),
        Route("/jobs",          dashboard),
        Route("/results",       dashboard),
        Route("/connect",       dashboard),
        Route("/api/login",     api_login,  methods=["POST"]),
        Route("/api/tools",     api_tools,  methods=["POST"]),
        Route("/mcp",           streamable_app),
        Mount("/",              app=sse_app),
    ],
)

app = BearerAuthMiddleware(_app)
