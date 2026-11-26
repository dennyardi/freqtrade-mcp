"""Tools read-only untuk Agent Evaluasi."""

from pathlib import Path
from config import STRATEGIES_DIR
from tools.jobs import _jobs, JobStatus


async def list_strategies() -> dict:
    strategies = [
        f.stem for f in Path(STRATEGIES_DIR).glob("*.py")
        if not f.name.startswith("__") and f.stem != "sample_strategy"
    ]
    return {"strategies": sorted(strategies), "count": len(strategies)}


async def get_performance_summary(job_id: str) -> dict:
    from tools.backtest import get_backtest_results
    result = await get_backtest_results(job_id)
    if "error" in result:
        return result

    evaluations = []
    for strat, m in result.get("summary", {}).items():
        sharpe       = m.get("sharpe")       or 0
        sortino      = m.get("sortino")      or 0
        drawdown_pct = m.get("max_drawdown_pct") or 0  # sudah dalam %
        win_rate     = m.get("win_rate")     or 0
        profit_pct   = m.get("profit_total_pct") or 0  # sudah dalam %
        trades       = m.get("total_trades") or 0
        pf           = m.get("profit_factor") or 0

        # Score: sharpe-weighted, penalti drawdown
        score = (sharpe * 30) + (profit_pct * 0.5) - (drawdown_pct * 1.5) + (win_rate * 0.1)

        # Verdict berdasarkan drawdown dalam % (bukan fraksi)
        if sharpe > 1.5 and drawdown_pct < 15:
            verdict = "PASS"
        elif sharpe > 1.0 and drawdown_pct < 20:
            verdict = "OPTIMIZE"
        else:
            verdict = "REJECT"

        tips = []
        if trades < 30:
            tips.append("Too few trades — widen timerange or loosen entry conditions")
        if sharpe < 1.0:
            tips.append("Low Sharpe — run hyperopt on roi/stoploss spaces")
        if drawdown_pct > 20:
            tips.append(f"High drawdown ({drawdown_pct:.1f}%) — tighten stoploss or add trailing stop")
        if win_rate < 40:
            tips.append(f"Low win rate ({win_rate:.1f}%) — review exit signal logic")
        if pf < 1.0 and pf > 0:
            tips.append(f"Profit factor {pf:.2f} < 1 — losers outweigh winners")

        evaluations.append({
            "strategy":       strat,
            "verdict":        verdict,
            "score":          round(score, 2),
            "metrics": {
                "total_trades":     trades,
                "win_rate":         win_rate,
                "profit_total_pct": profit_pct,
                "profit_total_abs": m.get("profit_total_abs"),
                "profit_factor":    pf,
                "max_drawdown_pct": drawdown_pct,
                "sharpe":           sharpe,
                "sortino":          sortino,
                "calmar":           m.get("calmar"),
                "cagr":             m.get("cagr"),
                "avg_duration":     m.get("avg_duration"),
            },
            "recommendation": "; ".join(tips) if tips else "Looks solid — proceed to dry-run",
        })

    return {"job_id": job_id, "evaluation": evaluations}


async def compare_strategies(job_ids: list[str]) -> dict:
    from tools.backtest import get_backtest_results
    rows = []
    for job_id in job_ids:
        result = await get_backtest_results(job_id)
        if "error" in result:
            rows.append({"job_id": job_id, "error": result["error"]})
            continue
        for strat, m in result.get("summary", {}).items():
            rows.append({
                "job_id":           job_id,
                "strategy":         strat,
                "sharpe":           m.get("sharpe"),
                "sortino":          m.get("sortino"),
                "profit_total_pct": m.get("profit_total_pct"),  # sudah dalam %
                "profit_total_abs": m.get("profit_total_abs"),
                "max_drawdown_pct": m.get("max_drawdown_pct"),  # sudah dalam %
                "win_rate":         m.get("win_rate"),
                "total_trades":     m.get("total_trades"),
                "profit_factor":    m.get("profit_factor"),
                "cagr":             m.get("cagr"),
                "calmar":           m.get("calmar"),
            })

    rows.sort(key=lambda x: x.get("sharpe") or 0, reverse=True)

    # B-06: hanya pilih best dari baris yang punya data valid (bukan error entries)
    valid_rows = [r for r in rows if "error" not in r and r.get("sharpe") is not None]
    best = valid_rows[0] if valid_rows else None

    return {"comparison": rows, "best": best}
