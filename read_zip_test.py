"""Helper: baca zip backtest result Freqtrade 2024+ dan extract summary."""
import zipfile
import json
import glob
import os

results_dir = "/opt/freqtrade/user_data/backtest_results"
files = sorted(glob.glob(f"{results_dir}/*.zip"), key=os.path.getmtime, reverse=True)

output = {}
for zf_path in files[:3]:
    try:
        with zipfile.ZipFile(zf_path) as zf:
            names = zf.namelist()
            for name in names:
                if name.endswith('.json') and 'metadata' not in name and 'trades' not in name:
                    data = json.loads(zf.read(name))
                    keys = list(data.keys())
                    strats = list(data.get('strategy', {}).keys())
                    sample_strat = {}
                    if strats:
                        s = data['strategy'][strats[0]]
                        sample_strat = {k: s.get(k) for k in ['total_trades','wins','profit_total_pct','max_drawdown_account','sharpe','profit_factor']}
                    output[os.path.basename(zf_path)] = {
                        'names_in_zip': names,
                        'top_keys': keys[:8],
                        'strategies': strats,
                        'sample_metrics': sample_strat,
                    }
                    break
    except Exception as e:
        output[os.path.basename(zf_path)] = str(e)

print(json.dumps(output, indent=2))
