"""Funding-rate data untuk trading_mode=futures.

Kenapa modul ini ada (issue funding-data, 2026-11-28):

freqtrade TIDAK menulis baris log apa pun saat menerapkan biaya pendanaan, dan
kalau file funding_rate tidak ada ia hanya menulis WARNING
`No history for <pair>, funding_rate, 1h found` lalu menghitung funding = 0.
Backtest tetap "berhasil" dan profit jadi terlalu tinggi tanpa satu pun error.
Modul ini membuat kondisi itu terlihat dan bisa diperbaiki:

- `funding_file()` / `read_funding()`  → baca file feather funding rate.
- `funding_status()`                   → coverage per pair + flag data_missing.
- `ensure_funding()`                   → unduh funding saja bila belum ada.
- `get_funding_history()`              → baris funding untuk riset carry.

Nama file mengikuti freqtrade: `<PAIR_SAFE>-<funding_fee_timeframe>-funding_rate.feather`
di direktori futures (BTC/USDT:USDT → BTC_USDT_USDT-1h-funding_rate.feather).
Funding Binance dicatat tiap 8 jam, jadi jumlah baris ≈ 3/hari, bukan per candle.

Pembacaan feather memakai interpreter freqtrade (punya pyarrow); venv MCP tidak.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import FREQTRADE_BIN, USER_DATA_DIR, DEFAULT_EXCHANGE_CONFIG

# Binance: funding_fee_timeframe = 1h (nilai funding di-align per jam, dicatat 8 jam sekali)
FUNDING_TIMEFRAME = "1h"
FUNDING_DIR = Path(USER_DATA_DIR) / "data" / "binance" / "futures"
FREQTRADE_PYTHON = str(Path(FREQTRADE_BIN).with_name("python"))

# Script pembaca ringkasan (dijalankan dengan python freqtrade)
_READ_FUNDING = r'''
import json, sys
import pyarrow.feather as feather
import pyarrow.compute as pc
t = feather.read_table(sys.argv[1], columns=["date", "funding_rate"])
n = t.num_rows
dates = t.column("date")
rates = pc.drop_null(t.column("funding_rate"))
m = rates.length()
print(json.dumps({
    "rows": n,
    "rows_valid": m,
    "first": str(pc.min(dates).as_py()) if n else None,
    "last": str(pc.max(dates).as_py()) if n else None,
    "sum_rate": float(pc.sum(rates).as_py() or 0.0) if m else 0.0,
    "mean_rate": float(pc.mean(rates).as_py() or 0.0) if m else 0.0,
    "last_rate": float(rates[m - 1].as_py()) if m else None,
}))
'''

# Script pembaca baris (untuk tool_get_funding_history). Filter/summary dihitung di
# Python, bukan pyarrow.compute: versi pyarrow di venv freqtrade menolak
# `pc.scalar(datetime)` (TypeError: unexpected argument type Expression).
_READ_FUNDING_ROWS = r'''
import json, sys
from datetime import datetime, timezone
import pyarrow.feather as feather

path, start, end, limit = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])

def _dt(raw):
    if not raw:
        return None
    d = datetime.fromisoformat(raw)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

t = feather.read_table(path, columns=["date", "funding_rate"])
dates = t.column("date").to_pylist()
rates = t.column("funding_rate").to_pylist()
s, e = _dt(start), _dt(end)
sel = [(d, r) for d, r in zip(dates, rates)
       if (s is None or d >= s) and (e is None or d < e)]
valid = [r for _, r in sel if r is not None]
shown = sel[-limit:] if limit and limit > 0 else []
total = float(sum(valid)) if valid else 0.0
print(json.dumps({
    "rows": len(sel),
    "rows_valid": len(valid),
    "first": str(sel[0][0]) if sel else None,
    "last": str(sel[-1][0]) if sel else None,
    "sum_rate": total,
    "mean_rate": (total / len(valid)) if valid else 0.0,
    "rows_shown": [{"date": str(d), "funding_rate": r} for d, r in shown],
}))
'''


def funding_file(pair: str) -> Path:
    """Path file funding rate freqtrade untuk pair futures."""
    safe = pair.replace("/", "_").replace(":", "_")
    return FUNDING_DIR / f"{safe}-{FUNDING_TIMEFRAME}-funding_rate.feather"


def _parse_dt(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_timerange(timerange: str | None) -> tuple[datetime | None, datetime | None]:
    """YYYYMMDD atau YYYYMMDD-YYYYMMDD → (start UTC, end UTC exclusive-ish).

    Sama dengan semantik freqtrade: batas akhir = 00:00 tanggal akhir.
    """
    if not timerange:
        return None, None
    try:
        if "-" in timerange:
            a, b = timerange.split("-", 1)
        else:
            a, b = timerange, None
        start = datetime.strptime(a, "%Y%m%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(b, "%Y%m%d").replace(tzinfo=timezone.utc) if b else None
        return start, end
    except Exception:
        return None, None


async def _read_script(script: str, *args: str) -> dict | None:
    """Jalankan pembaca feather dengan interpreter freqtrade."""
    try:
        proc = await asyncio.create_subprocess_exec(
            FREQTRADE_PYTHON, "-c", script, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(out.decode().strip().splitlines()[-1])
    except Exception:
        return None


async def read_funding(pair: str) -> dict:
    """Ringkasan file funding rate satu pair (rows/first/last/mean)."""
    path = funding_file(pair)
    info: dict = {
        "pair": pair,
        "file": str(path),
        "exists": path.exists() and path.stat().st_size > 0,
        "timeframe": FUNDING_TIMEFRAME,
        "rows": 0,
        "first": None,
        "last": None,
        "mean_rate": None,
        "sum_rate": None,
        "last_rate": None,
    }
    if not info["exists"]:
        return info
    raw = await _read_script(_READ_FUNDING, str(path))
    if not raw:
        info["read_error"] = "file funding tidak bisa dibaca (korup atau pyarrow hilang)"
        return info
    info.update(
        rows=int(raw.get("rows") or 0),
        first=raw.get("first"),
        last=raw.get("last"),
        mean_rate=raw.get("mean_rate"),
        sum_rate=raw.get("sum_rate"),
        last_rate=raw.get("last_rate"),
    )
    return info


async def funding_status(pairs: list[str], timerange: str | None = None,
                         now: datetime | None = None) -> dict:
    """Coverage funding per pair + daftar pair tanpa data yang menutupi rentang.

    `data_missing=True` bila salah satu pair tidak punya baris funding, atau
    barisnya tidak menutupi rentang yang diminta (toleransi 1 hari di tiap ujung;
    funding hanya 3 baris/hari).
    """
    start, end = parse_timerange(timerange)
    now = now or datetime.now(timezone.utc)
    tol = timedelta(days=1)
    per_pair: dict[str, dict] = {}
    missing: list[str] = []
    reasons: dict[str, str] = {}

    for pair in pairs:
        info = await read_funding(pair)
        first_dt = _parse_dt(info.get("first"))
        last_dt = _parse_dt(info.get("last"))
        covers_start = True
        covers_end = True
        if start is not None:
            covers_start = bool(first_dt and first_dt <= start + tol)
        if end is not None:
            end_eff = min(end, now)
            covers_end = bool(last_dt and last_dt >= end_eff - tol)
        ok = bool(info["rows"] > 0 and covers_start and covers_end)
        info.update(
            requested_first=start.isoformat() if start else None,
            requested_last=end.isoformat() if end else None,
            covers_requested_start=covers_start,
            covers_requested_end=covers_end,
            data_missing=not ok,
        )
        per_pair[pair] = info
        if not ok:
            missing.append(pair)
            if info["rows"] == 0:
                reasons[pair] = "file funding_rate tidak ada / kosong"
            elif not covers_start:
                reasons[pair] = (
                    f"funding mulai {info['first']}, sedangkan rentang minta "
                    f"{start.isoformat() if start else '-'}"
                )
            else:
                reasons[pair] = (
                    f"funding berakhir {info['last']}, sedangkan rentang minta "
                    f"sampai {end.isoformat() if end else '-'}"
                )

    return {
        "data_missing": bool(missing),
        "missing_pairs": missing,
        "missing_reasons": reasons,
        "requested_timerange": timerange,
        "pairs": per_pair,
    }


async def download_funding(pairs: list[str], timerange: str | None = None,
                           days: int = 365) -> dict:
    """Unduh HANYA funding rate (dengan konteks futures) untuk pair yang diberikan.

    Dipakai sebagai penambal otomatis: kalau file funding hilang, backtest
    futures tidak boleh jalan dengan funding 0 tanpa jejak.
    """
    if not pairs:
        return {"success": False, "error": "pairs kosong"}
    if not timerange:
        end = datetime.now(timezone.utc) + timedelta(days=1)
        start = end - timedelta(days=days + 1)
        timerange = start.strftime("%Y%m%d") + "-" + end.strftime("%Y%m%d")
    cmd = [
        FREQTRADE_BIN, "download-data",
        "--config", DEFAULT_EXCHANGE_CONFIG,
        "--pairs", *pairs,
        "--timeframe", "1h",
        "--userdir", USER_DATA_DIR,
        "--timerange", timerange,
        "--trading-mode", "futures",
        "--candle-types", "funding_rate",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "timerange": timerange, "error": f"{exc.__class__.__name__}: {exc}"}
    text = out.decode(errors="replace")
    return {
        "success": proc.returncode == 0,
        "pairs": pairs,
        "timerange": timerange,
        "command": " ".join(cmd[1:]),
        "log_tail": "\n".join(text.splitlines()[-10:]),
    }


async def ensure_funding(pairs: list[str], timerange: str | None = None,
                         days: int = 365) -> dict:
    """Pastikan funding ada; unduh sekali bila belum. Return status setelah percobaan."""
    status = await funding_status(pairs, timerange)
    if not status["data_missing"]:
        status["download_attempted"] = False
        return status
    dl = await download_funding(pairs, timerange, days=days)
    status = await funding_status(pairs, timerange)
    status["download_attempted"] = True
    status["download"] = dl
    return status


async def get_funding_history(pair: str, timerange: str | None = None,
                              limit: int = 200) -> dict:
    """Baris funding rate untuk satu pair (riset carry / verifikasi)."""
    path = funding_file(pair)
    if not (path.exists() and path.stat().st_size > 0):
        return {
            "pair": pair,
            "error": (
                f"Data funding untuk {pair} belum ada ({path}). Jalankan "
                "tool_download_data dengan trading_mode='futures'."
            ),
            "file": str(path),
        }
    start, end = parse_timerange(timerange)
    limit = max(0, min(int(limit or 0), 5000))
    raw = await _read_script(
        _READ_FUNDING_ROWS, str(path),
        start.isoformat() if start else "",
        end.isoformat() if end else "",
        str(limit),
    )
    if raw is None:
        return {
            "pair": pair,
            "error": "file funding tidak bisa dibaca (korup atau pyarrow tidak tersedia)",
            "file": str(path),
        }
    return {
        "pair": pair,
        "file": str(path),
        "timerange": timerange,
        "rows": raw.get("rows", 0),
        "first": raw.get("first"),
        "last": raw.get("last"),
        "sum_rate": raw.get("sum_rate"),
        "mean_rate": raw.get("mean_rate"),
        "annualized_mean_pct": (
            round(float(raw.get("mean_rate") or 0.0) * 3 * 365 * 100, 4)
            if raw.get("mean_rate") is not None else None
        ),
        "rows_shown": len(raw.get("rows_shown") or []),
        "rows_data": raw.get("rows_shown") or [],
    }
