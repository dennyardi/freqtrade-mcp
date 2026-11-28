# Freqtrade MCP Server

MCP Server untuk Freqtrade backtesting dan hyperopt, diakses via Hermes Agent (Telegram).

## Stack

- **MCP**: Python `mcp` v2.x, transport HTTP/SSE
- **Framework**: Starlette + Uvicorn
- **Exchange**: Binance (dry-run / backtesting only)
- **Python**: 3.12 via `uv`
- **Domain**: `trade.dennyardi.com` (SSL Let's Encrypt)

## Arsitektur

```
Telegram
   │
   ▼
Hermes Agent (VPS Hermes)
   │  MCP over HTTP/SSE + Bearer Token
   ▼
Freqtrade MCP Server (VPS Neva — trade.dennyardi.com)
   │
   ▼
Freqtrade CLI (backtesting, hyperopt, download-data)
```

## 17 MCP Tools

### Agent Pembuat Strategi
| Tool | Deskripsi |
|---|---|
| `tool_create_strategy` | Generate file strategi dari deskripsi natural language |
| `tool_validate_strategy` | Validasi strategi sebelum backtest |
| `tool_download_data` | Download data historis OHLCV dari Binance. `trading_mode='futures'` mengunduh OHLCV + mark + **funding_rate** eksplisit (`--candle-types futures mark funding_rate`) dan mengembalikan `funding_rows`/`funding_first`/`funding_last` per pair di `coverage` |
| `tool_run_backtest` | Jalankan backtest (async, return `job_id`). Arg `fee` (0-0.01, default 0.001 = 0.1%/sisi) override fee per run via flag `--fee`; config global tidak diubah. Untuk futures: funding rate dicek/diunduh lebih dulu, lalu metadata job memuat `funding.total_funding_fees`, `funding.trades_with_funding`, dan `funding_data_missing` |
| `tool_run_hyperopt` | Optimasi parameter (async, return `job_id`). Arg `fee` sama seperti backtest |
| `tool_get_job_status` | Poll status job |
| `tool_cancel_job` | Batalkan job yang berjalan |

### Agent Evaluasi (read-only)
| Tool | Deskripsi |
|---|---|
| `tool_list_strategies` | Daftar semua strategi yang tersedia |
| `tool_get_backtest_results` | Ambil hasil backtest yang sudah selesai (termasuk `funding_fees_total` per strategi) |
| `tool_get_hyperopt_results` | Ambil parameter terbaik dari hyperopt |
| `tool_get_performance_summary` | Analisis + verdict (PASS/OPTIMIZE/REJECT) + rekomendasi |
| `tool_compare_strategies` | Bandingkan performa beberapa strategi side-by-side |
| `tool_get_funding_history` | Riwayat funding rate satu pair futures (`pair`, `timerange`, `limit`) — dasar riset carry |
| `tool_backtest_carry` | Simulasi carry delta-netral: long spot + short perp dengan notional sama; funding dihitung dari file `funding_rate`, basis P&L dari selisih harga spot vs perp, fee dua leg, rebalance opsional (`none`/`daily`/`weekly`). Baca-only, tidak mengubah data/strategi |

### Carry backtest (delta-netral)

`tool_backtest_carry` menjalankan `tools/carry_sim.py` dengan interpreter freqtrade
(butuh pyarrow; venv MCP tidak punya), membaca candle spot + candle perp + funding:

- `funding_basis="fixed"` (default): funding = `stake x rate` → setara kolom tabel
  funding tahunan dan dipakai sebagai yield carry; `"notional"`: notional short
  aktual x rate (cara freqtrade membebankan funding, ikut membesar saat harga naik).
- `net_pct` = funding + basis P&L - fee dua leg + biaya rebalance; `net_pct_notional`
  = versi `funding_basis="notional"` selalu ikut dilaporkan.
- `liquidation_risk.max_adverse_move_pct` = kenaikan harga perp yang menghabiskan
  margin short (`1/leverage - maintenance_margin`); `max_observed_adverse_move_pct`
  = kenaikan terburuk yang benar-benar terjadi relatif terhadap harga short saat itu.
- `data_gaps` memisahkan gap spot, perp, dan funding (gap funding = jarak > 9 jam).

### Funding rate (futures)

freqtrade **tidak menulis baris log** saat menerapkan biaya pendanaan, dan bila file
funding tidak ada ia hanya `WARNING - No history for <pair>, funding_rate, 1h found`
lalu memakai funding **0** tanpa error. Karena itu server:

1. mengunduh `funding_rate` eksplisit saat `tool_download_data` dengan `trading_mode='futures'`,
   dan mengulang unduhan khusus funding (`--candle-types funding_rate`) bila file belum ada;
2. sebelum backtest futures, memanggil `ensure_funding()` (auto-unduh sekali bila belum ada)
   sehingga backtest tidak pernah diam-diam memakai funding 0;
3. menempelkan baris `funding: applied ±X USDT ...` atau `funding: MISSING ...` di log job,
   dan mengisi `funding_data_missing` (true/false) di metadata job;
4. menjumlahkan `funding_fees` per trade dari artefak freqtrade menjadi `funding_fees_total`.


## Deployment

### Prerequisites

```bash
# Install TA-Lib C library
apt install -y build-essential wget
wget https://github.com/TA-Lib/ta-lib/releases/download/v0.6.4/ta-lib-0.6.4-src.tar.gz
tar -xzf ta-lib-0.6.4-src.tar.gz && cd ta-lib-0.6.4
./configure --prefix=/usr && make -j$(nproc) && make install

# Install Python 3.12 via uv
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12
```

### Install Freqtrade

```bash
mkdir -p /opt/freqtrade && cd /opt/freqtrade
git clone https://github.com/freqtrade/freqtrade .
uv venv .venv --python 3.12
source .venv/bin/activate
pip install -e ".[hyperopt]"
freqtrade create-userdir --userdir user_data
```

### Install MCP Server

```bash
mkdir -p /opt/freqtrade-mcp && cd /opt/freqtrade-mcp
# Clone repo ini
git clone https://github.com/YOUR_USERNAME/freqtrade-mcp .

# Buat virtualenv
uv venv .venv --python 3.12
source .venv/bin/activate
pip install -r requirements.txt

# Konfigurasi environment
cp .env.example .env
# Edit .env dengan token dan credentials
```

### Konfigurasi `.env`

```env
MCP_BEARER_TOKEN=your-secret-token-here
MCP_PORT=8000
MCP_HOST=0.0.0.0
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=your-dashboard-password
```

### Systemd Service

```bash
cp freqtrade-mcp.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now freqtrade-mcp
```

### Nginx + SSL

```bash
# Install certbot
apt install -y nginx certbot python3-certbot-nginx

# Buat config nginx (lihat nginx.conf.example)
# Dapatkan SSL certificate
certbot --nginx -d trade.yourdomain.com
```

## Dashboard

Akses di `https://trade.yourdomain.com/`

- Login: username + password (dari `.env`)
- Overview: stats strategies, jobs aktif, results
- Strategies: daftar semua strategi
- Jobs: monitoring job backtest/hyperopt real-time
- Results: tabel hasil backtest dengan Sharpe, profit, drawdown, verdict
- Koneksi Hermes: panduan konfigurasi Hermes Agent

## Koneksi ke Hermes Agent

```yaml
# ~/.hermes/config.yaml
mcp:
  servers:
    freqtrade:
      type: sse
      url: https://trade.yourdomain.com/sse
      headers:
        Authorization: "Bearer your-secret-token-here"
```

## Contoh Penggunaan via Telegram → Hermes

```
Download data BTC/USDT timeframe 1h untuk 180 hari terakhir

Buat strategi RSI crossover + MACD confirmation untuk BTC/USDT

Backtest strategi RSIStrategy untuk BTC/USDT
timeframe 1h, periode 1 Januari - 1 Desember 2024

Jalankan hyperopt RSIStrategy, 100 epochs,
optimasi roi, stoploss dan trailing

Bandingkan hasil job abc123 dengan def456
```

## Roadmap

Lihat [ROADMAP.md](ROADMAP.md)
