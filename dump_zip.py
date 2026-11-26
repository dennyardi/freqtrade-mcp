"""Dump semua fields dari zip backtest terbaru untuk debug."""
import zipfile, json, glob, os

results_dir = "/opt/freqtrade/user_data/backtest_results"
zips = sorted(glob.glob(f"{results_dir}/*.zip"), key=os.path.getmtime, reverse=True)

for zf_path in zips[:3]:
    print(f"\n=== {os.path.basename(zf_path)} ===")
    with zipfile.ZipFile(zf_path) as zf:
        print("Files in zip:", zf.namelist())
        for name in zf.namelist():
            if name.endswith(".json") and "trade" not in name.lower():
                data = json.loads(zf.read(name))
                print(f"File: {name}")
                print("Top keys:", list(data.keys()))
                if "strategy" in data:
                    for strat_name, m in data["strategy"].items():
                        print(f"Strategy: {strat_name}")
                        # Cetak SEMUA field yang ada
                        for k, v in sorted(m.items()):
                            if v is not None and v != 0 and "trade" not in k.lower():
                                print(f"  {k}: {v}")
                        break
                break
