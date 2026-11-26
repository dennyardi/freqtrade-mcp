# Roadmap — Freqtrade MCP Server

> Platform backtesting strategi trading agentic via Hermes Agent + Telegram.

---

## ✅ Phase 1 — Infrastruktur & Freqtrade (Selesai)

- [x] VPS Neva (160.19.166.123) — swap 2GB aktif
- [x] Install paket dasar, build tools, TA-Lib C library
- [x] Python 3.12 via `uv`
- [x] Freqtrade 2026.10 + hyperopt support di `/opt/freqtrade/.venv`
- [x] `user_data/` struktur folder: strategies, backtest_results, hyperopt_results
- [x] `config.json` Binance dry-run tersimpan

---

## ✅ Phase 2 — MCP Server & Dashboard (Selesai)

- [x] MCP Server di `/opt/freqtrade-mcp/` dengan 12 tools
- [x] Transport: HTTP/SSE, auth Bearer Token
- [x] Systemd service `freqtrade-mcp` aktif + auto-start
- [x] Nginx + SSL Let's Encrypt untuk `trade.dennyardi.com`
- [x] Dashboard login username+password → return Bearer Token
- [x] Route `/` serve dashboard (bukan `/dashboard`)
- [x] Route `/api/login` POST verifikasi credentials
- [x] Dashboard UI: Linear/Raycast dark style, Geist font
  - [x] Login screen
  - [x] Sidebar navigasi (Overview, Strategies, Jobs, Results, Koneksi Hermes)
  - [x] Overview: stat cards + recent strategies + active jobs + latest results
  - [x] Strategies page: daftar semua strategi
  - [x] Jobs page: monitoring semua job (polling setiap 15s)
  - [x] Results page: tabel Sharpe, profit, drawdown, win rate, verdict
  - [x] Connect page: instruksi konfigurasi Hermes Agent + Bearer Token display
- [x] Auto-refresh 15 detik
- [x] Session persist via sessionStorage

---

## ✅ Phase 3 — Koneksi Hermes Agent (Selesai)

- [x] Konfigurasi MCP server entry `freqtrade` di 2 profile Hermes
- [x] Test koneksi HTTP dari VPS Hermes → trade.dennyardi.com ✅ 200 OK
- [x] Test `/api/login` dari VPS Hermes → token diterima ✅
- [x] Buat 2 agent profile di VPS Hermes (`/root/.hermes/profiles/`):
  - [x] **`agentfreqtrade`** — Agent Pembuat Strategi (7 write tools + system prompt)
  - [x] **`agentfreqeval`** — Agent Evaluasi (5 read-only tools + system prompt)
- [x] System prompt bahasa Indonesia per agent dengan workflow detail
- [ ] Test end-to-end via Telegram:
  - [ ] Download data BTC/USDT
  - [ ] Generate strategi RSI + MACD
  - [ ] Run backtest → poll job_id → lihat hasil
  - [ ] Run hyperopt → ambil best params
  - [ ] Compare 2 strategi

---

## ✅ Phase 4 — Hardening & Observability (Sebagian Selesai)

- [x] **Job persistence SQLite** — jobs tersimpan di `/opt/freqtrade-mcp/data/jobs.db`, survive restart
  - [x] `tools/db.py` — SQLite layer (init, save, load)
  - [x] `tools/jobs.py` — bootstrap() load semua jobs saat startup; jobs pending/running otomatis ditandai failed
  - [x] `tools/backtest.py` — persist setiap perubahan status + progress tracking akurat (parse % output)
  - [x] `tools/hyperopt.py` — persist setiap perubahan status + progress tracking akurat (parse Epoch X/Y)
- [x] **Progress tracking akurat** — parse output Freqtrade realtime via async line-by-line streaming
- [ ] Rate limiting per Bearer Token (max 5 concurrent jobs)
- [ ] Log streaming — stream stdout backtest ke SSE event
- [ ] Webhook notifikasi ke Telegram saat job selesai
- [ ] Metrics endpoint (`/metrics`) untuk Prometheus
- [ ] Health check yang lebih detail (disk space, Freqtrade version)

---

## 🚀 Phase 5 — Fitur Lanjutan

- [ ] **Multi-exchange support**: OKX, Bybit (selain Binance)
- [ ] **Walk-forward testing**: split data train/test otomatis
- [ ] **Strategy versioning**: simpan history setiap versi strategi
- [ ] **Portfolio backtesting**: test beberapa pasang pair sekaligus
- [ ] **Live dry-run monitoring**: pantau bot dry-run yang aktif
- [ ] **Telegram notifikasi otomatis**: kiriman ringkasan hasil harian
- [ ] **Dashboard multi-user**: beberapa user dengan token berbeda
- [ ] **Strategy marketplace**: simpan & share strategi antar user

---

## 🏗️ Arsitektur Target (Phase 5)

```
Telegram
   │
   ▼
Hermes Agent (Orchestrator)
   ├── Agent Pembuat Strategi (7 tools)
   └── Agent Evaluasi (5 tools)
         │
         │ MCP over HTTP/SSE
         ▼
Freqtrade MCP Server (trade.dennyardi.com)
   ├── /             → Dashboard UI
   ├── /api/login    → Auth endpoint
   ├── /sse          → MCP SSE transport
   ├── /health       → Health check
   └── /metrics      → Prometheus metrics (Phase 4)
         │
         ▼
Freqtrade CLI
   ├── backtesting
   ├── hyperopt (SharpeHyperOptLoss)
   ├── download-data (Binance)
   └── check-freqtrade-files
         │
         ▼
SQLite (jobs.db)
   └── /opt/freqtrade-mcp/data/jobs.db
```

---

## 📊 Decision Log

| Keputusan | Pilihan | Alasan |
|---|---|---|
| Transport MCP | HTTP/SSE | Kompatibel dengan Hermes Agent remote |
| Auth | Bearer Token | Simple, stateless, cukup untuk 1 instance |
| Python runtime | 3.12 via `uv` | Typing modern (`list[str] \| None`), fast install |
| Hyperopt loss | `SharpeHyperOptLoss` | Risk-adjusted return lebih relevan dari pure profit |
| Job pattern | Async + `job_id` | Backtest/hyperopt bisa >10 menit, non-blocking |
| Dashboard auth | username+password → token | UX lebih friendly dari paste token langsung |
| Install method | Virtualenv (no Docker) | Lebih ringan, latency lebih rendah di VPS kecil |
| Job persistence | SQLite | Lightweight, zero dependency, cukup untuk 1 instance |
| Progress parsing | Async line streaming | Realtime, tidak perlu polling file |
