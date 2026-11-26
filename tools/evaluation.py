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
        sharpe   = m.get("sharpe")   or 0
        drawdown = m.get("max_drawdown_pct") or 0
        win_rate = m.get("win_rate") or 0
        profit   = m.get("profit_total_pct") or 0
        trades   = m.get("total_trades") or 0

        score = (sharpe * 30) + (profit * 20) - (drawdown * 25) + (win_rate * 0.25)

        if sharpe > 1.5 and drawdown < 0.15:
            verdict = "PASS"
        elif sharpe > 1.0 and drawdown < 0.20:
            verdict = "OPTIMIZE"
        else:
            verdict = "REJECT"

        tips = []
        if trades < 30:
            tips.append("Too few trades — widen timerange or loosen entry conditions")
        if sharpe < 1.0:
            tips.append("Low Sharpe — run hyperopt on roi/stoploss spaces")
        if drawdown > 0.20:
            tips.append("High drawdown — tighten stoploss or add trailing stop")
        if win_rate < 40:
            tips.append("Low win rate — review exit signal logic")

        evaluations.append({
            "strategy": strat,
            "verdict": verdict,
            "score": round(score, 2),
            "metrics": m,
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
                "profit_pct":       m.get("profit_total_pct"),
                "max_drawdown_pct": m.get("max_drawdown_pct"),
                "win_rate":         m.get("win_rate"),
                "total_trades":     m.get("total_trades"),
                "profit_factor":    m.get("profit_factor"),
            })

    rows.sort(key=lambda x: x.get("sharpe") or 0, reverse=True)
    return {"comparison": rows, "best": rows[0] if rows else None}
