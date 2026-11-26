"""Patch sekali: hapus api_server block dari freqtrade config.json"""
import json
import sys

CONFIG_PATH = "/opt/freqtrade/user_data/config.json"

with open(CONFIG_PATH) as f:
    config = json.load(f)

if "api_server" in config:
    del config["api_server"]
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    print("OK: api_server block dihapus dari config.json")
else:
    print("SKIP: api_server tidak ditemukan (sudah bersih)")
