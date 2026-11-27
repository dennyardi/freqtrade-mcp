"""Tool: download_data — unduh data historis OHLCV dari Binance.

Perilaku penting (issue 1h-prepend):
- Rentang lokal (first/last candle + jumlah candle) dibaca langsung dari file
  feather, bukan dari parsing log.
- Bila start yang diminta lebih awal dari data lokal terawal, freqtrade WAJIB
  diberi `--prepend` (tanpa itu ia menolak dan menulis "length 0").
- `full=True` memakai `--erase` (unduh ulang seluruh rentang).
- Tidak pernah balas success:true bila freqtrade tidak mengunduh candle apa pun.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import FREQTRADE_BIN, USER_DATA_DIR, DEFAULT_EXCHANGE_CONFIG
from tools.backtest import (
    _VALID_TIMEFRAMES,
    _validate_pairs,
    check_pairs_for_mode,
    validate_trading_mode,
)

# Timerange opsional: YYYYMMDD-YYYYMMDD
_SAFE_TIMERANGE = re.compile(r'^\d{8}-\d{8}$')

DATA_DIR = Path(USER_DATA_DIR) / "data" / "binance"
FREQTRADE_PYTHON = str(Path(FREQTRADE_BIN).with_name("python"))
_EXT_ORDER = ("feather", "json", "json.gz")

# Batas atas days: praktis tanpa plafon (100 tahun), CEX tidak punya data lebih tua.
MAX_DAYS = 36500

# Pembaca feather freqtrade. Dijalankan dengan interpreter freqtrade (punya pyarrow),
# karena venv MCP tidak menginstal pyarrow.
_READ_FEATHER = r'''
import json, sys
import pyarrow.feather as feather
import pyarrow.compute as pc
t = feather.read_table(sys.argv[1], columns=["date"])
col = t.column("date")
n = t.num_rows
print(json.dumps({
    "candles": n,
    "first": str(pc.min(col).as_py()) if n else None,
    "last": str(pc.max(col).as_py()) if n else None,
}))
'''

_TF_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def _tf_seconds(timeframe: str) -> int:
    try:
        return int(timeframe[:-1]) * _TF_SECONDS[timeframe[-1]]
    except Exception:
        return 3600


def _data_dir(trading_mode: str = "spot") -> Path:
    """Direktori data: spot → data/binance, futures → data/binance/futures."""
    return DATA_DIR / "futures" if trading_mode == "futures" else DATA_DIR


def _data_file(pair: str, timeframe: str, trading_mode: str = "spot") -> Path | None:
    # freqtrade menamai file pair: BTC/USDT:USDT → BTC_USDT_USDT, dan untuk candle
    # non-spot menambahkan suffix tipe candle + subdir futures/
    # (mis. futures/BTC_USDT_USDT-1h-futures.feather).
    pair_safe = pair.replace("/", "_").replace(":", "_")
    base = _data_dir(trading_mode)
    suffix = "-futures" if trading_mode == "futures" else ""
    for ext in _EXT_ORDER:
        fpath = base / f"{pair_safe}-{timeframe}{suffix}.{ext}"
        if fpath.exists() and fpath.stat().st_size > 0:
            return fpath
    return None


def _parse_dt(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _read_range(fpath: Path | None) -> dict | None:
    """Rentang lokal nyata (first/last/candles) dari file data freqtrade."""
    if fpath is None:
        return None
    fpath = Path(fpath)
    if not fpath.exists() or fpath.stat().st_size == 0:
        return None
    if fpath.suffix != ".feather":
        return None  # hanya feather yang didukung untuk pembacaan rentang
    try:
        proc = await asyncio.create_subprocess_exec(
            FREQTRADE_PYTHON, "-c", _READ_FEATHER, str(fpath),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            return None
        raw = json.loads(out.decode().strip().splitlines()[-1])
        return {
            "candles": int(raw["candles"]),
            "first_dt": _parse_dt(raw["first"]),
            "last_dt": _parse_dt(raw["last"]),
            "first": raw["first"],
            "last": raw["last"],
        }
    except Exception:
        return None


def _parse_timerange(tr: str) -> tuple[datetime, datetime]:
    a, b = tr.split("-")
    return (
        datetime.strptime(a, "%Y%m%d").replace(tzinfo=timezone.utc),
        datetime.strptime(b, "%Y%m%d").replace(tzinfo=timezone.utc),
    )


async def _run_download(pairs: list[str], timeframe: str, timerange: str,
                        flags: list[str], trading_mode: str = "spot") -> tuple[int, str]:
    cmd = [
        FREQTRADE_BIN, "download-data",
        "--config", DEFAULT_EXCHANGE_CONFIG,
        "--pairs", *pairs,
        "--timeframe", timeframe,
        "--userdir", USER_DATA_DIR,
        "--timerange", timerange,
        *flags,
    ]
    if trading_mode == "futures":
        # Data masuk ke user_data/data/binance/futures/ (OHLCV + mark + funding rate).
        cmd += ["--trading-mode", "futures"]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode, stdout.decode()


def _pair_downloaded_zero(pair: str, output: str) -> bool:
    """True bila log freqtrade menyatakan pair ini terunduh dengan length 0."""
    for line in output.splitlines():
        if pair in line and re.search(r"with length\s+0\b", line):
            return True
    return False


async def download_data(
    pairs: list[str],
    timeframe: str = "1h",
    days: int | None = None,
    timerange: str | None = None,
    full: bool = False,
    trading_mode: str = "spot",
) -> dict:
    """
    Unduh data historis OHLCV dari Binance.

    Gunakan `timerange` (format YYYYMMDD-YYYYMMDD) untuk kontrol presisi penuh,
    atau `days` untuk N hari ke belakang. Jika keduanya disertakan, timerange
    mengalahkan days.

    `full=True` memakai `--erase` (unduh ulang seluruh rentang yang diminta).
    Tanpa `full`, bila rentang yang diminta lebih awal dari data lokal, server
    otomatis memakai `--prepend`.

    `trading_mode='futures'` mengunduh data futures Binance (pair wajib
    BASE/QUOTE:SETTLE, contoh BTC/USDT:USDT) termasuk mark price & funding rate
    ke user_data/data/binance/futures/.
    """
    if not pairs:
        return {"error": "pairs tidak boleh kosong"}
    if (mode_err := validate_trading_mode(trading_mode)) is not None:
        return {"error": mode_err}
    trading_mode = trading_mode.strip().lower()
    if _validate_pairs(pairs) is None:
        return {"error": "Format pair tidak valid. Gunakan format BASE/QUOTE (contoh: BTC/USDT)"}
    if (pair_mode_err := check_pairs_for_mode(pairs, trading_mode)) is not None:
        return {"error": pair_mode_err}
    if timeframe not in _VALID_TIMEFRAMES:
        return {"error": f"Timeframe tidak valid: {timeframe}"}

    # Validasi parameter waktu
    if timerange is not None:
        if not _SAFE_TIMERANGE.match(timerange):
            return {"error": "timerange harus format YYYYMMDD-YYYYMMDD (contoh: 20240101-20261127)"}
        start_dt, end_dt = _parse_timerange(timerange)
        download_end_dt = end_dt
    else:
        if days is not None:
            if not isinstance(days, int) or days < 1 or days > MAX_DAYS:
                return {"error": f"days harus integer antara 1–{MAX_DAYS}"}
        else:
            days = 365  # default 1 tahun
        # Konversi days → timerange eksplisit: lebih reliable daripada --days
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days)
        # Timerange freqtrade berbutir per hari (stop = 00:00 tanggal akhir), jadi
        # akhir jendela harus H+1 supaya candle hari ini ikut terunduh.
        download_end_dt = end_dt + timedelta(days=1)
        timerange = start_dt.strftime("%Y%m%d") + "-" + download_end_dt.strftime("%Y%m%d")

    if end_dt <= start_dt:
        return {"error": "timerange tidak valid: tanggal akhir harus setelah tanggal mulai"}

    tf_delta = timedelta(seconds=_tf_seconds(timeframe))

    # --- Rencana per pair: butuh prepend? butuh erase? butuh append? ---
    before: dict[str, dict | None] = {}
    plans: dict[str, dict] = {}
    for pair in pairs:
        fpath = _data_file(pair, timeframe, trading_mode)
        local = await _read_range(fpath)
        before[pair] = local
        need_prepend = bool(local and local["first_dt"] and start_dt < local["first_dt"])
        # Timerange freqtrade berbutir per hari: stop = 00:00 tanggal akhir.
        # Jadi append hanya berguna kalau tanggal akhir > tanggal candle terakhir.
        need_append = bool(
            local and local["last_dt"] and end_dt.date() > local["last_dt"].date()
        )
        plans[pair] = {
            "file": str(fpath) if fpath else None,
            "need_prepend": need_prepend,
            "need_append": need_append,
        }

    # --- Jalankan download bertahap (erase → satu pass; prepend → + pass append) ---
    runs: list[tuple[str, list[str]]] = []
    if full:
        runs.append((timerange, ["--erase"]))
    else:
        any_prepend = any(p["need_prepend"] for p in plans.values())
        any_append = any(p["need_append"] for p in plans.values())
        if any_prepend:
            # --prepend menonaktifkan appending, jadi data baru di ujung kanan
            # (bila ada) diambil di pass kedua.
            runs.append((timerange, ["--prepend"]))
            if any_append:
                last_dt = max(
                    (b["last_dt"] for b in before.values() if b and b["last_dt"]),
                    default=None,
                )
                if last_dt:
                    append_tr = last_dt.strftime("%Y%m%d") + "-" + download_end_dt.strftime("%Y%m%d")
                    runs.append((append_tr, []))
        else:
            runs.append((timerange, []))

    outputs: list[str] = []
    returncodes: list[int] = []
    for tr, flags in runs:
        rc, out = await _run_download(pairs, timeframe, tr, flags, trading_mode)
        returncodes.append(rc)
        outputs.append(out)

    # Pass prepend/erase memotong candle terakhir yang belum tutup
    # (freqtrade memuat cache dengan drop_incomplete=True lalu menulis ulang).
    # Kalau ekor data jadi lebih pendek dari sebelumnya, pulihkan dengan pass append.
    if runs and runs[0][1]:  # ada --prepend atau --erase
        regressed: dict[str, datetime] = {}
        for pair in pairs:
            b = before[pair]
            if not b or not b["last_dt"]:
                continue
            a = await _read_range(_data_file(pair, timeframe, trading_mode))
            if a and a["last_dt"] and a["last_dt"] < b["last_dt"]:
                regressed[pair] = a["last_dt"]
        if regressed:
            # Timerange freqtrade berbutir per hari (stop = 00:00 tanggal akhir) dan
            # pass append memakai start = candle terakhir file. Jadi akhir jendela
            # harus H+1 dari ekor yang sekarang, kalau tidak hasilnya 0 candle.
            fix_start = min(regressed.values())
            fix_end = fix_start.date() + timedelta(days=1)
            fix_tr = fix_start.strftime("%Y%m%d") + "-" + fix_end.strftime("%Y%m%d")
            rc, out = await _run_download(list(regressed), timeframe, fix_tr, [], trading_mode)
            returncodes.append(rc)
            outputs.append(out)
            runs.append((fix_tr, ["tail-restore"]))

    output = "\n".join(outputs)

    if any(rc != 0 for rc in returncodes):
        return {
            "success": False,
            "pairs": pairs,
            "error": output[-2000:],
            "commands": [f"{tr} {' '.join(flags)}".strip() for tr, flags in runs],
        }

    # --- Coverage nyata setelah download (dibaca dari feather, bukan dari log) ---
    success_pairs: list[str] = []
    failed_pairs: list[str] = []
    noprogress: list[str] = []
    at_floor: list[str] = []
    coverage: dict[str, dict] = {}
    # freqtrade hanya mencetak ini kalau dia MENOLAK mundur karena --prepend tidak ada.
    refused = "earlier than local data start" in output

    for pair in pairs:
        plan = plans[pair]
        fpath = _data_file(pair, timeframe, trading_mode)
        after = await _read_range(fpath)
        local = before[pair]

        if after is None:
            failed_pairs.append(pair)
            coverage[pair] = {
                "file": str(fpath) if fpath else None,
                "first_candle": None,
                "last_candle": None,
                "candles": 0,
            }
            continue

        rows_added = after["candles"] - (local["candles"] if local else 0)
        zero = _pair_downloaded_zero(pair, output)
        # Cakupan start yang diminta terpenuhi? (toleransi 1 candle + 1 hari untuk
        # batas ketersediaan data di exchange)
        covers_start = bool(after["first_dt"] and after["first_dt"] <= start_dt + timedelta(days=1))
        # Akhir jendela: jangan menuntut candle masa depan. Candle terakhir yang
        # sudah tutup bisa tertinggal 1-2 candle dari jam sekarang (lag exchange),
        # jadi toleransi 3 candle.
        end_eff = min(end_dt, datetime.now(timezone.utc))
        covers_end = bool(after["last_dt"] and after["last_dt"] >= end_eff - tf_delta * 3)

        coverage[pair] = {
            "file": str(fpath) if fpath else None,
            "first_candle": after["first"],
            "last_candle": after["last"],
            "candles": after["candles"],
            # kompatibilitas field lama
            "candle_count_est": after["candles"],
            "rows_added": rows_added,
            "need_prepend": plan["need_prepend"],
            "covers_requested_start": covers_start,
            "covers_requested_end": covers_end,
        }

        # Tidak ada candle yang terunduh padahal ada yang diminta → bukan sukses,
        # KECUALI freqtrade menerima --prepend dan exchange memang tidak punya
        # candle lebih awal (file lokal sudah di batas ketersediaan exchange).
        if (zero or rows_added <= 0) and not (covers_start and covers_end):
            if plan["need_prepend"] and not refused and after["candles"] > 0:
                at_floor.append(pair)
                success_pairs.append(pair)
            else:
                noprogress.append(pair)
            continue

        if after["candles"] == 0:
            failed_pairs.append(pair)
            continue

        success_pairs.append(pair)

    if not success_pairs:
        if refused:
            reason = "no data downloaded (prepend/erase required)"
        elif failed_pairs and not noprogress:
            reason = "no data file produced; periksa pair/timeframe di exchange"
        else:
            firsts = {
                p: (coverage.get(p, {}).get("first_candle"))
                for p in noprogress
            }
            reason = (
                "no data downloaded (0 candle untuk rentang yang diminta; "
                f"data lokal mulai {firsts})"
            )
        return {
            "success": False,
            "reason": reason,
            "pairs": pairs,
            "failed_pairs": failed_pairs + noprogress,
            "timeframe": timeframe,
            "timerange": timerange,
            "trading_mode": trading_mode,
            "coverage": coverage,
            "log_tail": "\n".join(output.splitlines()[-20:]),
        }

    result: dict = {
        "success": True,
        "pairs": success_pairs,
        "timeframe": timeframe,
        "timerange": timerange,
        "trading_mode": trading_mode,
        "data_dir": str(_data_dir(trading_mode)),
        "flag_used": "erase" if full else ("prepend" if any(p["need_prepend"] for p in plans.values()) else "append"),
        "coverage": coverage,
        "message": f"Data downloaded for {len(success_pairs)} pair(s)",
    }
    if noprogress:
        result["warning"] = f"Tidak ada candle baru untuk: {noprogress}"
        result["no_new_data_pairs"] = noprogress
    if at_floor:
        firsts = {p: coverage.get(p, {}).get("first_candle") for p in at_floor}
        msg = (
            "0 candle baru; data lokal sudah mulai di batas ketersediaan exchange "
            f"({firsts}). Tidak ada data lebih awal untuk diunduh."
        )
        result["warning"] = (result.get("warning", "") + " " + msg).strip()
        result["at_exchange_floor_pairs"] = at_floor
    if failed_pairs:
        result["warning"] = (result.get("warning", "") + f" Pair gagal/tidak ada data: {failed_pairs}").strip()
        result["failed_pairs"] = failed_pairs
    result["log_tail"] = "\n".join(output.splitlines()[-15:])
    result["commands"] = [f"{tr} {' '.join(flags)}".strip() for tr, flags in runs]

    return result
