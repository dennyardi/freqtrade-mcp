#!/usr/bin/env python3
"""Mesin simulasi carry trade: long spot + short perpetual, delta-netral.

Dijalankan dengan interpreter freqtrade (butuh pyarrow), BUKAN venv MCP:

    /opt/freqtrade/.venv/bin/python /opt/freqtrade-mcp/tools/carry_sim.py '<json cfg>'

cfg (satu argumen JSON):
  pair               spot pair, mis. "BTC/USDT"
  perp_pair          futures pair, mis. "BTC/USDT:USDT"
  timerange          "YYYYMMDD-YYYYMMDD" (batas akhir = 00:00 UTC tanggal itu,
                     sama dengan semantik freqtrade/history tool)
  stake              notional awal per leg (USDT)
  fee_spot           fee per sisi leg spot (rasio, mis. 0.001)
  fee_perp           fee per sisi leg perp (rasio, mis. 0.0005)
  rebalance          "none" | "daily" | "weekly"
  leverage           leverage short perp (untuk analisis likuidasi, default 3)
  maintenance_margin maintenance margin exchange (rasio, default 0.005)
  timeframe          timeframe candle (default "1h"); 1h adalah yang tersedia
  user_data_dir      path user_data freqtrade

Model (didokumentasikan supaya angka bisa direproduksi):

- Entry jam pertama dalam timerange yang punya candle spot DAN perp sekaligus.
- qty_spot = stake / spot_entry, qty_perp_short = stake / perp_entry
  (notional sama, qty koin berbeda).
- basis P&L = spot leg P&L + short perp leg P&L, dengan qty tetap selama tidak
  ada rebalance. Karena qty = notional/harga, hasilnya = stake * (r_spot - r_perp)
  tepat, yaitu murni selisih return dua leg (konvergensi/pelebaran basis).
- funding diterima leg short = notional_short_saat_itu * rate (rate positif =
  long bayar short = short terima). Baris funding tepat di jam entry tidak
  dihitung (posisi baru dibuka, funding itu milik periode sebelumnya).
- fee: masuk (stake*fee_spot + stake*fee_perp), keluar (notional_akhir * fee),
  rebalance (notional yang ditukar * fee per leg).
- equity harian = stake + realized + unrealized kedua leg + funding - fee.
  Biaya keluar kedua leg dibebankan di titik akhir.
- max_drawdown_pct dihitung dari kurva equity harian (peak-to-trough).
- Rebalance mengembalikan notional kedua leg ke rata-rata notional saat itu
  (menjual/membeli spot dan menambah/menutup perp), fee dihitung atas notional
  yang ditukar.
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow.feather as feather

TIMEFRAME_HOURS = {"1h": 1, "4h": 4}

# Perkiraan jarak antar funding Binance (8 jam). Dipakai untuk deteksi gap saja.
FUNDING_STEP = timedelta(hours=8)
FUNDING_GAP_TOLERANCE = timedelta(hours=9)


def _utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _pair_safe(pair):
    return pair.replace("/", "_").replace(":", "_")


def parse_timerange(timerange):
    if not timerange:
        return None, None
    a, _, b = timerange.partition("-")
    start = datetime.strptime(a, "%Y%m%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(b, "%Y%m%d").replace(tzinfo=timezone.utc) if b else None
    return start, end


def spot_file(user_data_dir, pair, timeframe):
    safe = pair.replace("/", "_").replace(":", "_")
    return Path(user_data_dir) / "data" / "binance" / f"{safe}-{timeframe}.feather"


def futures_file(user_data_dir, perp_pair, timeframe, kind="futures"):
    safe = _pair_safe(perp_pair)
    return (Path(user_data_dir) / "data" / "binance" / "futures"
            / f"{safe}-{timeframe}-{kind}.feather")


def load_series(path, column="close"):
    """dict[datetime UTC] -> float untuk kolom tertentu (skip null)."""
    table = feather.read_table(path, columns=["date", column])
    dates = table.column("date").to_pylist()
    values = table.column(column).to_pylist()
    out = {}
    for d, v in zip(dates, values):
        if d is None or v is None:
            continue
        out[_utc(d)] = float(v)
    return out


def _gaps(ts_set, start, end, step, max_report=20):
    """Rentang yang tidak ada bar-nya, iterasi per `step` detik/jam."""
    gaps = []
    total = 0
    open_at = None
    total_expected = 0
    t = start
    while t < end:
        total_expected += 1
        if t in ts_set:
            if open_at is not None:
                gaps.append((open_at, t))
                open_at = None
        else:
            if open_at is None:
                open_at = t
        t += step
    if open_at is not None:
        gaps.append((open_at, end))
    out = []
    for a, b in gaps:
        n = int((b - a) / step)
        total += n
        if len(out) < max_report:
            out.append({"from": _iso(a), "to": _iso(b), "bars": n})
    return {
        "bars_missing": total,
        "bars_expected": total_expected,
        "complete": total == 0,
        "ranges": out,
        "ranges_truncated": max(0, len(gaps) - len(out)),
    }


def _funding_gaps(rows, start, end, max_report=20):
    """Gap funding: jarak antar baris > 9 jam, atau tidak menutupi ujung window."""
    gaps = []
    if not rows:
        return {"bars_missing": None, "complete": False,
                "ranges": [{"from": _iso(start), "to": _iso(end), "hours": None}],
                "ranges_truncated": 0,
                "note": "file funding kosong / tidak ada baris di rentang"}
    if rows[0][0] - start > FUNDING_GAP_TOLERANCE:
        gaps.append((start, rows[0][0]))
    for (a, _), (b, _) in zip(rows, rows[1:]):
        if b - a > FUNDING_GAP_TOLERANCE:
            gaps.append((a, b))
    if end - rows[-1][0] > FUNDING_GAP_TOLERANCE:
        gaps.append((rows[-1][0], end))
    out = []
    total_hours = 0
    for a, b in gaps:
        h = int((b - a).total_seconds() // 3600)
        total_hours += h
        if len(out) < max_report:
            out.append({"from": _iso(a), "to": _iso(b), "hours": h})
    return {
        "bars_missing": len(gaps),
        "hours_missing": total_hours,
        "complete": not gaps,
        "ranges": out,
        "ranges_truncated": max(0, len(gaps) - len(out)),
    }


def simulate(cfg):
    pair = cfg["pair"]
    perp_pair = cfg["perp_pair"]
    timeframe = cfg.get("timeframe") or "1h"
    stake = float(cfg.get("stake") or 1000.0)
    fee_spot = float(cfg.get("fee_spot", 0.001))
    fee_perp = float(cfg.get("fee_perp", 0.0005))
    rebalance = (cfg.get("rebalance") or "none").lower()
    leverage = float(cfg.get("leverage") or 3.0)
    mmr = float(cfg.get("maintenance_margin", 0.005))
    user_data_dir = cfg.get("user_data_dir") or "/opt/freqtrade/user_data"

    if rebalance not in ("none", "daily", "weekly"):
        return {"error": f"rebalance tidak dikenal: {rebalance}"}
    if timeframe not in TIMEFRAME_HOURS:
        return {"error": f"timeframe tidak didukung: {timeframe} (pakai 1h)"}
    step = timedelta(hours=TIMEFRAME_HOURS[timeframe])

    start, end = parse_timerange(cfg.get("timerange"))
    if start is None:
        return {"error": "timerange tidak valid (format YYYYMMDD-YYYYMMDD)"}

    paths = {
        "spot": spot_file(user_data_dir, pair, timeframe),
        "perp": futures_file(user_data_dir, perp_pair, timeframe, "futures"),
        "funding": futures_file(user_data_dir, perp_pair, timeframe, "funding_rate"),
    }
    missing = [k for k, p in paths.items() if not (p.exists() and p.stat().st_size > 0)]
    if missing:
        return {
            "error": "data tidak lengkap: " + ", ".join(missing),
            "missing_files": {k: str(paths[k]) for k in missing},
            "hint": ("unduh dengan tool_download_data (spot untuk pair spot, "
                     "trading_mode='futures' untuk pair perp + funding)"),
        }

    spot = load_series(paths["spot"], "close")
    perp = load_series(paths["perp"], "close")
    funding_rows = sorted(load_series(paths["funding"], "funding_rate").items())

    # ---- coverage / data gaps (dihitung atas rentang yang diminta) ----------
    data_gaps = {
        "spot": _gaps(set(spot), start, end, step),
        "perp": _gaps(set(perp), start, end, step),
        "funding": _funding_gaps(
            [(t, r) for t, r in funding_rows if start <= t < end], start, end),
    }
    data_gaps["complete"] = all(v["complete"] for v in data_gaps.values())

    window = [t for t in sorted(set(spot) & set(perp)) if start <= t < end]
    if len(window) < 2:
        return {
            "error": "tidak cukup candle yang beririsan antara spot dan perp di timerange ini",
            "spot_first": _iso(min(spot)) if spot else None,
            "spot_last": _iso(max(spot)) if spot else None,
            "perp_first": _iso(min(perp)) if perp else None,
            "perp_last": _iso(max(perp)) if perp else None,
            "timerange": cfg.get("timerange"),
            "data_gaps": data_gaps,
        }

    entry_ts, exit_ts = window[0], window[-1]
    p_s0, p_p0 = spot[entry_ts], perp[entry_ts]
    days = (exit_ts - entry_ts).total_seconds() / 86400.0
    if days < 1:
        return {"error": "rentang efektif < 1 hari", "entry": _iso(entry_ts), "exit": _iso(exit_ts)}

    me = (cfg.get("funding_basis") or "fixed").lower()
    if me not in ("fixed", "notional"):
        return {"error": f"funding_basis tidak dikenal: {me} (pakai 'fixed' atau 'notional')"}

    qty_s = stake / p_s0
    avg_s = p_s0
    real_s = 0.0
    qty_p = stake / p_p0   # short
    avg_p = p_p0
    real_p = 0.0
    funding_fixed = 0.0       # stake x rate (notional konstan, setara tabel funding)
    funding_actual = 0.0      # notional short aktual x rate (seperti freqtrade)
    funding_usdt = 0.0
    fees_usdt = stake * (fee_spot + fee_perp)   # fee masuk kedua leg
    rebal_count = 0
    rebal_cost = 0.0
    rebal_fees = {"spot": 0.0, "perp": 0.0}

    def spot_unreal(ps):
        return qty_s * (ps - avg_s)

    def perp_unreal(pp):
        return qty_p * (avg_p - pp)

    def equity(ps, pp):
        return stake + real_s + spot_unreal(ps) + real_p + perp_unreal(pp) + funding_usdt - fees_usdt

    # funding per jam (baris funding dipetakan ke jam pertama yang >= waktunya)
    fmap = defaultdict(float)
    f_in_window = [(t, r) for t, r in funding_rows if entry_ts < t <= exit_ts]
    idx = 0
    for ts, rate in f_in_window:
        while idx < len(window) and window[idx] < ts:
            idx += 1
        if idx < len(window):
            fmap[window[idx]] += rate
        elif window:
            fmap[window[-1]] += rate

    prev = None
    last = None
    max_stress = 0.0       # kenaikan harga perp di atas harga short yang sedang terbuka
    daily_eq = {}          # date -> equity (nilai candle terakhir hari itu)
    daily_rates = defaultdict(float)   # date -> jumlah funding rate hari itu
    snapshots = []         # (year, equity, komponen kumulatif) di akhir tahun
    peak_dd = None
    last_ts = None

    for t in window:
        ps, pp = spot[t], perp[t]

        rate = fmap.get(t)
        if rate:
            funding_fixed += stake * rate
            funding_actual += qty_p * pp * rate
            funding_usdt = funding_fixed if me == "fixed" else funding_actual
            daily_rates[t.date()] += rate

        # ---- rebalance di awal hari / awal minggu ----
        if rebalance != "none" and prev is not None:
            due = False
            if rebalance == "daily" and t.date() != prev.date():
                due = True
            elif rebalance == "weekly" and t.weekday() == 0 and t.date() != prev.date():
                due = True
            if due:
                n_s = qty_s * ps
                n_p = qty_p * pp
                target = (n_s + n_p) / 2.0
                d_s = target - n_s      # >0: beli spot, <0: jual spot
                d_p = target - n_p      # >0: tambah short, <0: tutup sebagian short
                if abs(d_s) > 1e-12:
                    fee = abs(d_s) * fee_spot
                    if d_s > 0:
                        new_qty = qty_s + d_s / ps
                        avg_s = (avg_s * qty_s + ps * (d_s / ps)) / new_qty
                        qty_s = new_qty
                    else:
                        sold = min(qty_s, -d_s / ps)
                        real_s += sold * (ps - avg_s)
                        qty_s -= sold
                    fees_usdt += fee
                    rebal_fees["spot"] += fee
                if abs(d_p) > 1e-12:
                    fee = abs(d_p) * fee_perp
                    if d_p > 0:
                        new_qty = qty_p + d_p / pp
                        avg_p = (avg_p * qty_p + pp * (d_p / pp)) / new_qty
                        qty_p = new_qty
                    else:
                        covered = min(qty_p, -d_p / pp)
                        real_p += covered * (avg_p - pp)
                        qty_p -= covered
                    fees_usdt += fee
                    rebal_fees["perp"] += fee
                rebal_count += 1
                rebal_cost = rebal_fees["spot"] + rebal_fees["perp"]

        eq = equity(ps, pp)
        daily_eq[t.date()] = eq
        if avg_p > 0:
            max_stress = max(max_stress, (pp / avg_p - 1.0) * 100.0)

        if prev is not None and t.year != prev.year and last is not None:
            snapshots.append({
                "ts": _iso(last["ts"]),
                "year_end": prev.year,
                "equity": last["eq"],
                "funding": last["funding"],
                "basis": last["basis"],
                "fees": last["fees"],
            })
        last = {
            "ts": t,
            "eq": eq,
            "funding": funding_usdt,
            "fees": fees_usdt,
            "basis": real_s + spot_unreal(ps) + real_p + perp_unreal(pp),
        }
        prev = t

    ps_end, pp_end = spot[exit_ts], perp[exit_ts]
    exit_fees = qty_s * ps_end * fee_spot + qty_p * pp_end * fee_perp
    fees_usdt += exit_fees
    final_eq = equity(ps_end, pp_end)   # exit_fees sudah masuk fees_usdt

    basis_usdt = real_s + spot_unreal(ps_end) + real_p + perp_unreal(pp_end)

    # ---- max drawdown dari kurva equity harian ----
    curve = [(str(d), v) for d, v in sorted(daily_eq.items())]
    peak = curve[0][1]
    peak_date = curve[0][0]
    max_dd = 0.0
    dd_peak = dd_trough = peak_date
    for d, v in curve:
        if v > peak:
            peak = v
            peak_date = d
        dd = (v / peak - 1.0) * 100 if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd
            dd_peak, dd_trough = peak_date, d

    # ---- funding harian ----
    neg_days = 0
    worst_day_pct = 0.0
    worst_day_date = None
    for d, rsum in daily_rates.items():
        pct = rsum * 100.0   # rate dihitung atas notional ≈ stake
        if pct < 0:
            neg_days += 1
            if pct < worst_day_pct:
                worst_day_pct = pct
                worst_day_date = str(d)
    funding_pct = funding_usdt / stake * 100.0
    rate_sum = sum(r for t, r in f_in_window)
    funding_fixed_pct = rate_sum * 100.0
    funding_actual_pct = funding_actual / stake * 100.0
    basis_pct = basis_usdt / stake * 100.0
    fees_pct = -fees_usdt / stake * 100.0
    net_pct = (final_eq / stake - 1.0) * 100.0
    net_notional_pct = net_pct - funding_pct + funding_actual_pct
    net_annual = ((final_eq / stake) ** (365.0 / days) - 1.0) * 100.0 if final_eq > 0 else -100.0

    # ---- rincian per tahun (chained: equity akhir tahun / equity awal) ------
    yearly = {}
    year_start_eq = stake
    for snap in snapshots:
        yearly[snap["year_end"]] = {
            "net_pct": round((snap["equity"] / year_start_eq - 1.0) * 100.0, 4),
            "funding_pct": round(snap["funding"] / stake * 100.0, 4),
            "basis_pct": round(snap["basis"] / stake * 100.0, 4),
            "fees_pct": round(-snap["fees"] / stake * 100.0, 4),
            "equity_usdt": round(snap["equity"], 4),
        }
        year_start_eq = snap["equity"]
    last_year = int(last["ts"].year) if last else exit_ts.year
    yearly[last_year] = {
        "net_pct": round((final_eq / year_start_eq - 1.0) * 100.0, 4),
        "funding_pct": round(funding_pct, 4),
        "basis_pct": round(basis_pct, 4),
        "fees_pct": round(fees_pct, 4),
        "equity_usdt": round(final_eq, 4),
    }
    # funding/basis/fees di yearly untuk tahun-tahun sebelum terakhir adalah kumulatif;
    # ubah jadi delta antar tahun supaya tidak salah dibaca.
    prev_cum = {"funding_pct": 0.0, "basis_pct": 0.0, "fees_pct": 0.0}
    for y in sorted(yearly):
        for k in ("funding_pct", "basis_pct", "fees_pct"):
            yearly[y][k] = round(yearly[y][k] - prev_cum[k], 4)
            prev_cum[k] += yearly[y][k]

    # ---- risiko likuidasi short ----
    # liq_move: kenaikan harga perp di atas harga entry short yang menghabiskan margin.
    # max_stress: kenaikan terburuk yang benar-benar terjadi relatif terhadap harga
    # short yang sedang terbuka (reset saat rebalance menutup/membuka short).
    liq_move = (1.0 / leverage - mmr) * 100.0
    max_stress = max(0.0, max_stress)

    basis_series = [(perp[t] - spot[t]) / spot[t] * 100.0 for t in window]

    return {
        "pair": pair,
        "perp_pair": perp_pair,
        "timerange": cfg.get("timerange"),
        "stake_usdt": stake,
        "period": {
            "requested_start": _iso(start),
            "requested_end": _iso(end),
            "entry": _iso(entry_ts),
            "exit": _iso(exit_ts),
            "days": round(days, 2),
            "hours": len(window),
        },
        "legs": {
            "spot_qty_entry": round(qty_s, 8),
            "spot_entry_price": p_s0,
            "perp_qty_entry": round(stake / p_p0, 8),
            "perp_entry_price": p_p0,
            "spot_exit_price": ps_end,
            "perp_exit_price": pp_end,
            "spot_return_pct": round((ps_end / p_s0 - 1.0) * 100.0, 4),
            "perp_return_pct": round((pp_end / p_p0 - 1.0) * 100.0, 4),
        },
        "funding_basis": me,
        "funding_collected_pct": round(funding_pct, 4),
        "funding_collected_usdt": round(funding_usdt, 4),
        "funding_collected_fixed_pct": round(funding_fixed_pct, 4),
        "funding_collected_notional_pct": round(funding_actual_pct, 4),
        "funding_rate_sum_pct": round(funding_fixed_pct, 4),
        "funding_rates_count": len(f_in_window),
        "basis_pnl_pct": round(basis_pct, 4),
        "basis_pnl_usdt": round(basis_usdt, 4),
        "fees_pct": round(fees_pct, 4),
        "fees_usdt": round(fees_usdt, 4),
        "fee_breakdown": {
            "entry_usdt": round(stake * (fee_spot + fee_perp), 4),
            "exit_usdt": round(exit_fees, 4),
            "rebalance_usdt": round(rebal_cost, 4),
            "rebalance_count": rebal_count,
        },
        "net_pct": round(net_pct, 4),
        "net_pct_notional": round(net_notional_pct, 4),
        "net_usdt": round(final_eq - stake, 4),
        "net_annualized_pct": round(net_annual, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "drawdown_window": {"peak": dd_peak, "trough": dd_trough},
        "funding_days": len(daily_rates),
        "negative_funding_days": neg_days,
        "worst_funding_day_pct": round(worst_day_pct, 4),
        "worst_funding_day": worst_day_date,
        "yearly": {str(y): yearly[y] for y in sorted(yearly)},
        "basis_pct": {
            "entry": round((p_p0 - p_s0) / p_s0 * 100.0, 4),
            "exit": round((pp_end - ps_end) / ps_end * 100.0, 4),
            "avg": round(sum(basis_series) / len(basis_series), 4),
            "min": round(min(basis_series), 4),
            "max": round(max(basis_series), 4),
        },
        "rebalance": rebalance,
        "liquidation_risk": {
            "leverage": leverage,
            "maintenance_margin_pct": round(mmr * 100.0, 4),
            "max_adverse_move_pct": round(liq_move, 4),
            "max_observed_adverse_move_pct": round(max_stress, 4),
            "would_liquidate": max_stress >= liq_move,
            "note": ("max_adverse_move_pct = kenaikan harga perp di atas harga short yang "
                     "sedang terbuka yang menghabiskan margin (1/leverage - maintenance "
                     "margin). max_observed_adverse_move_pct = kenaikan terburuk yang "
                     "benar-benar terjadi relatif terhadap harga short saat itu (reset "
                     "saat rebalance menutup/membuka short). Bila would_liquidate=true, "
                     "posisi hanya bertahan dengan margin cross atau top-up margin dari "
                     "gain leg spot."),
        },
        "assumptions": [
            "notional kedua leg sama saat entry; qty koin berbeda",
            "funding_basis='fixed' (default): funding = stake x rate, setara kolom tabel funding "
            "tahunan dan stabil dipakai sebagai yield carry; funding_basis='notional': funding = "
            "notional short saat itu x rate (konsisten dengan cara freqtrade membebankan funding, "
            "ikut membesar saat harga naik)",
            "rate positif = long bayar short = short terima; baris funding tepat di jam entry tidak dihitung",
            "fee spot dan perp dibebankan saat entry dan exit; rebalance opsional",
            "return dihitung atas stake (notional spot), bukan atas total margin spot+perp",
        ],
        "data_gaps": data_gaps,
        "files": {k: str(v) for k, v in paths.items()},
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "butuh satu argumen JSON cfg"}))
        return 2
    try:
        cfg = json.loads(sys.argv[1])
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"cfg bukan JSON valid: {exc}"}))
        return 2
    try:
        result = simulate(cfg)
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(json.dumps({
            "error": f"{exc.__class__.__name__}: {exc}",
            "traceback": traceback.format_exc().splitlines()[-12:],
        }))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
